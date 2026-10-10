"""Heartbeats: one row per run, missed-job detection and alerting, the operator enrolment pause (SPEC 9, 13)."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound.base import heartbeats
from us_outbound.clients.guard import GuardViolation
from us_outbound.ops import heartbeat as hb
from us_outbound.settings.model import General, Settings

SETTINGS = Settings(general=General())
MON_NOON = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)  # Monday 12:00 UK (BST ended on 25 Oct)


def slack_transport() -> FakeTransport:
    t = FakeTransport()
    t.route("GET", "conversations.list", body={"ok": True, "channels": [
        {"id": "C_DEV", "name": "us-outbound-dev"}, {"id": "C_ALERT", "name": "us-outbound"}]})
    t.route("POST", "chat.postMessage", fn=lambda req: {"ok": True, "channel": req.json["channel"], "ts": "1.1"})
    return t


def ctx_at(now, job="test", transport=None, store=None, live=False):
    return make_context(SETTINGS, job=job, now=now, transport=transport or slack_transport(), store=store, live=live)


def posts(t: FakeTransport) -> list[dict]:
    return [r.json for r in t.requests if r.url.endswith("chat.postMessage")]


def beat(store, job, started, status="ok", finished=None, dry_run=True, run_id=None, error=None, detail=None):
    store.upsert("heartbeats", [{
        "run_id": run_id or f"{job}-{started.isoformat()}", "job": job, "started_at": started,
        "finished_at": finished if finished is not None else started + timedelta(minutes=1),
        "status": status, "dry_run": dry_run, "detail": detail, "error": error,
    }])


# -- run_job ---------------------------------------------------------------------------


def test_ok_run_writes_one_row_with_the_summary():
    ctx = ctx_at(MON_NOON, job="suppression_load")
    out = hb.run_job(ctx, lambda c: {"added": 2, "when": c.now})
    [row] = ctx.store.tables["heartbeats"]
    assert row["run_id"] == ctx.run_id and row["job"] == "suppression_load"
    assert row["status"] == "ok" and row["dry_run"] is True and row["error"] is None
    assert row["started_at"] == MON_NOON and row["finished_at"] >= MON_NOON
    assert row["detail"] == {"added": 2, "when": MON_NOON.isoformat()} == out


def test_error_run_writes_error_and_reraises():
    ctx = ctx_at(MON_NOON, job="mailbox_health")

    def boom(c):
        raise RuntimeError("Instantly said no for jane@acme.com " + "x" * 2000)

    with pytest.raises(RuntimeError):
        hb.run_job(ctx, boom)
    [row] = ctx.store.tables["heartbeats"]
    assert row["status"] == "error" and row["finished_at"] is not None
    assert row["error"].startswith("RuntimeError: Instantly said no for email:")
    assert "jane@acme.com" not in row["error"] and len(row["error"]) <= hb.ERROR_LIMIT + 1


def test_guard_violation_is_recorded_and_never_swallowed():
    ctx = ctx_at(MON_NOON, job="enrol")

    def bad(c):
        raise GuardViolation("Instantly lead.add targets campaign 'EU Outbound – Anna'")

    with pytest.raises(GuardViolation):
        hb.run_job(ctx, bad)
    assert ctx.store.tables["heartbeats"][0]["detail"] == {"violation": True}


def test_skips_are_recorded_as_skipped():
    ctx = ctx_at(MON_NOON, job="enrol")
    hb.run_job(ctx, lambda c: {"skipped": True, "reason": "blackout date"})
    ctx2 = ctx_at(MON_NOON, job="enrol", store=ctx.store)

    def skip(c):
        raise hb.Skip("live_sending is no")

    assert hb.run_job(ctx2, skip) == {"skipped": True, "reason": "live_sending is no"}
    assert [r["status"] for r in ctx.store.tables["heartbeats"]] == ["skipped", "skipped"]


def test_a_second_run_while_one_is_going_is_skipped():
    ctx = ctx_at(MON_NOON, job="poll_approvals")
    beat(ctx.store, "poll_approvals", MON_NOON - timedelta(minutes=3), status="running", finished=None, run_id="r0")
    ctx.store.tables["heartbeats"][0]["finished_at"] = None
    ran = []
    out = hb.run_job(ctx, lambda c: ran.append(1) or {})
    assert not ran and out["reason"] == hb.OVERLAP_REASON == "previous run still going" and out["other_run_id"] == "r0"
    # A run that died more than an hour ago no longer blocks.
    ctx.store.tables["heartbeats"][0]["started_at"] = MON_NOON - timedelta(minutes=61)
    ctx2 = ctx_at(MON_NOON, job="poll_approvals", store=ctx.store)
    hb.run_job(ctx2, lambda c: ran.append(1) or {})
    assert ran == [1]


def test_non_dict_results_are_wrapped():
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    assert hb.run_job(ctx, lambda c: ["enrol"]) == {"result": ["enrol"]}


# -- expected intervals ------------------------------------------------------------------


def test_expected_matches_the_spec9_schedules():
    assert hb.EXPECTED["poll_replies"] == 45
    assert hb.EXPECTED["poll_approvals"] == 20
    assert hb.EXPECTED["kill_rules"] == 150
    assert hb.EXPECTED["settings_sync"] == hb.EXPECTED["mailbox_health"] == hb.EXPECTED["daily_post"] == 26 * 60
    assert hb.EXPECTED["monday_readout"] == hb.EXPECTED["public_signals"] == 8 * 24 * 60
    # Build, 1 Oct 2026: the sources and verify_accounts run each weekday (ops/schedule.py), in weekday time.
    for job in ("source_universe", "apollo_signals", "read_pages", "apollo_enrich", "apollo_people", "verify_accounts"):
        assert hb.EXPECTED[job] == 26 * 60 and job in hb.WEEKDAY_JOBS
    assert "score" not in hb.EXPECTED  # no schedule of its own


def test_the_monthly_lookalikes_job_is_missed_only_when_a_1st_passes_without_it():
    """Harry, 5 Oct 2026: lookalikes runs on the 1st at 02:30 UK. A month without it is no alert; a 1st missed is."""
    t = slack_transport()
    oct_1 = datetime(2026, 10, 1, 1, 30, tzinfo=UTC)  # 02:30 BST
    for when in (datetime(2026, 10, 30, 9, 5, tzinfo=UTC), datetime(2026, 11, 1, 12, 5, tzinfo=UTC)):
        ctx = ctx_at(when, job="heartbeat_check", transport=t)
        beat(ctx.store, "lookalikes", oct_1)
        assert hb.check_heartbeats(ctx, jobs=["lookalikes"]) == []  # 29 and 31 days on: still healthy
    ctx = ctx_at(datetime(2026, 11, 2, 2, 5, tzinfo=UTC), job="heartbeat_check", transport=t)
    beat(ctx.store, "lookalikes", oct_1)
    assert hb.check_heartbeats(ctx, jobs=["lookalikes"]) == ["lookalikes"]  # the 1 Nov run never came
    assert "expected at least every 32 days" in posts(t)[-1]["text"]
    beat(ctx.store, "lookalikes", datetime(2026, 11, 1, 2, 30, tzinfo=UTC))
    assert hb.check_heartbeats(ctx, jobs=["lookalikes"]) == []


# -- check_heartbeats --------------------------------------------------------------------


def test_stale_job_is_flagged_and_alerted_once_newly_missed():
    t = slack_transport()
    ctx = ctx_at(MON_NOON, job="heartbeat_check", transport=t)
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(minutes=50))  # finished 49 min ago: 4 min overdue
    beat(ctx.store, "settings_sync", MON_NOON - timedelta(hours=10))
    missed = hb.check_heartbeats(ctx, jobs=["poll_replies", "settings_sync", "mailbox_health"])
    assert missed == ["poll_replies"]
    [post] = posts(t)
    assert post["channel"] == "C_DEV" and "[dry-run → #us-outbound]" in post["text"]
    assert "poll_replies" in post["text"] and "settings_sync" not in post["text"]


def test_long_missed_job_is_flagged_but_only_reminded_at_nine():
    t = slack_transport()
    ctx = ctx_at(MON_NOON, job="heartbeat_check", transport=t)  # 12:00 UK
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(hours=5))
    assert hb.check_heartbeats(ctx, jobs=["poll_replies"]) == ["poll_replies"]
    assert posts(t) == []
    nine = ctx_at(MON_NOON - timedelta(hours=3) + timedelta(minutes=5), transport=t, store=ctx.store)
    assert hb.check_heartbeats(nine, jobs=["poll_replies"]) == ["poll_replies"]
    assert len(posts(t)) == 1


def test_error_runs_count_as_missed_after_the_last_ok_run():
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    beat(ctx.store, "mailbox_health", MON_NOON - timedelta(hours=30), run_id="a")
    beat(ctx.store, "mailbox_health", MON_NOON - timedelta(hours=5), status="error", run_id="b", error="Boom")
    assert hb.check_heartbeats(ctx, jobs=["mailbox_health"]) == ["mailbox_health"]
    beat(ctx.store, "mailbox_health", MON_NOON - timedelta(hours=1), run_id="c")
    assert hb.check_heartbeats(ctx, jobs=["mailbox_health"]) == []


def test_a_skip_for_its_own_reason_and_a_first_run_still_going_are_healthy():
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    beat(ctx.store, "settings_sync", MON_NOON - timedelta(hours=2), status="skipped",
         detail={"skipped": True, "reason": "blackout date"})
    beat(ctx.store, "kill_rules", MON_NOON - timedelta(minutes=10), status="running")
    assert hb.check_heartbeats(ctx, jobs=["settings_sync", "kill_rules"]) == []


def test_an_overlap_skip_alone_is_no_sign_of_life():
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    beat(ctx.store, "settings_sync", MON_NOON - timedelta(hours=30), run_id="ok")
    beat(ctx.store, "settings_sync", MON_NOON - timedelta(hours=2), status="skipped", run_id="skip",
         detail={"skipped": True, "reason": hb.OVERLAP_REASON, "other_run_id": "x"})
    assert hb.check_heartbeats(ctx, jobs=["settings_sync"]) == ["settings_sync"]


def test_a_job_killed_every_run_is_missed():
    """Last ok two days ago; since then each hourly run is killed (stays "running") and the
    15-minute runs between are skipped behind it. Only the lock frees itself; the job is missed."""
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(days=2), run_id="last-ok")
    start = MON_NOON - timedelta(days=2) + timedelta(minutes=15)
    i = 0
    while start < MON_NOON:
        if i % 4 == 0:
            ctx.store.upsert("heartbeats", [{
                "run_id": f"killed-{i}", "job": "poll_replies", "started_at": start, "finished_at": None,
                "status": "running", "dry_run": True, "detail": None, "error": None,
            }])
        else:
            beat(ctx.store, "poll_replies", start, status="skipped", finished=start, run_id=f"skip-{i}",
                 detail={"skipped": True, "reason": hb.OVERLAP_REASON, "other_run_id": f"killed-{i - i % 4}"})
        start += timedelta(minutes=15)
        i += 1
    assert heartbeats.latest_runs(ctx.store)["poll_replies"]["status"] in {"running", "skipped"}
    assert hb.check_heartbeats(ctx, jobs=["poll_replies"]) == ["poll_replies"]


def test_a_job_that_never_succeeds_is_missed_even_while_a_run_is_fresh():
    """Every run is killed and none ever finished ok: a fresh "running" row is no excuse."""
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    for hours in (3, 2, 1):
        beat(ctx.store, "kill_rules", MON_NOON - timedelta(hours=hours), status="running", run_id=f"k{hours}")
    beat(ctx.store, "kill_rules", MON_NOON - timedelta(minutes=5), status="running", run_id="k0")
    for r in ctx.store.tables["heartbeats"]:
        r["finished_at"] = None
    assert hb.check_heartbeats(ctx, jobs=["kill_rules"]) == ["kill_rules"]


def test_a_first_run_killed_long_ago_is_missed():
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    beat(ctx.store, "kill_rules", MON_NOON - timedelta(hours=3), status="running", finished=None)
    ctx.store.tables["heartbeats"][-1]["finished_at"] = None
    assert hb.check_heartbeats(ctx, jobs=["kill_rules"]) == ["kill_rules"]



def test_weekday_jobs_do_not_miss_over_the_weekend():
    ctx = ctx_at(MON_NOON, job="heartbeat_check")  # Monday 12:00 UK
    friday_noon = MON_NOON - timedelta(days=3)
    beat(ctx.store, "enrol", friday_noon)
    assert hb.check_heartbeats(ctx, jobs=["enrol"]) == []
    tuesday_2pm = ctx_at(MON_NOON + timedelta(hours=26), store=ctx.store)
    assert hb.check_heartbeats(tuesday_2pm, jobs=["enrol"]) == ["enrol"]


def test_never_run_jobs_are_not_flagged():
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    assert hb.check_heartbeats(ctx, jobs=["settings_sync", "mailbox_health"]) == []


def test_default_jobs_are_the_built_scheduled_ones():
    jobs = hb.scheduled_jobs()
    assert {"settings_sync", "poll_replies", "sync_outcomes", "poll_approvals", "hubspot_readback",
            "mailbox_health", "heartbeat_check", "suppression_load"} <= set(jobs)
    assert "score" not in jobs and "enrol" in jobs  # enrol runs dry until live_sending = yes


def test_latest_runs_uses_the_view_when_the_store_has_one():
    ctx = ctx_at(MON_NOON)
    seen = []
    ctx.store.query_handlers["v_heartbeats"] = lambda store, params: seen.append(1) or [
        {"job": "poll_replies", "status": "ok", "started_at": MON_NOON, "finished_at": MON_NOON, "last_ok_at": MON_NOON}
    ]
    assert heartbeats.latest_runs(ctx.store)["poll_replies"]["status"] == "ok" and seen == [1]


# -- operator stop / start -------------------------------------------------------------------


def test_enrolment_paused_follows_stop_and_live_start():
    ctx = ctx_at(MON_NOON)
    s = ctx.store
    assert heartbeats.enrolment_paused(s) is None
    beat(s, heartbeats.OPERATOR_STOP, MON_NOON - timedelta(hours=3), status="error")  # a failed stop still stops
    assert heartbeats.enrolment_paused(s)["job"] == heartbeats.OPERATOR_STOP
    beat(s, heartbeats.OPERATOR_START, MON_NOON - timedelta(hours=2), dry_run=True)  # a dry-run start does not
    assert heartbeats.enrolment_paused(s) is not None
    beat(s, heartbeats.OPERATOR_START, MON_NOON - timedelta(hours=1), dry_run=False)
    assert heartbeats.enrolment_paused(s) is None
    beat(s, heartbeats.OPERATOR_STOP, MON_NOON, dry_run=True)
    assert heartbeats.enrolment_paused(s) is not None


def test_operator_commands_are_never_skipped_by_the_lock():
    ctx = ctx_at(MON_NOON, job=heartbeats.OPERATOR_STOP)
    beat(ctx.store, heartbeats.OPERATOR_STOP, MON_NOON - timedelta(minutes=2), status="running", run_id="stuck")
    ran = []
    hb.run_job(ctx, lambda c: ran.append(1) or {})
    assert ran == [1]
