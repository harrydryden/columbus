"""Heartbeats: one row per run, missed-job detection and alerting, the operator enrolment pause (SPEC 9, 13)."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound.ops import health
from us_outbound.clients.guard import GuardViolation
from us_outbound.ops import health as hb
from us_outbound.settings.model import General, Settings
from us_outbound.clients.slack import dry_run_note

DRY = dry_run_note("#us-outbound") + "\n"

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
    # The row's detail is the summary plus how long the run took (W1-1); the job's own summary is returned as it was.
    duration = row["detail"].pop("duration_s")
    assert isinstance(duration, float) and duration >= 0
    assert row["detail"] == {"added": 2, "when": MON_NOON.isoformat()} == out


@pytest.mark.parametrize("summary, status", [
    # enrol's live summary: "skipped" is a Counter of the accounts it passed over, so a truthy "skipped" is no skip.
    # Every live enrol run was stored as skipped until 19 Oct 2026 (ops/health.py:150; enrol.py:1091).
    ({"enrolled": 21, "skipped": {"no contact": 335}}, "ok"),
    ({"enrolled": 0, "skipped": {}}, "ok"),
    ({"status": "skipped", "reason": "a blackout date"}, "skipped"),  # enrol's own skip (enrol.py:960)
    ({"skipped": True, "reason": "no registry mailboxes"}, "skipped"),
    ({"status": "ok", "skipped": 3}, "ok"),
])
def test_the_run_status_follows_the_summary_not_a_truthy_skipped_key(summary, status):
    ctx = ctx_at(MON_NOON, job="enrol")
    hb.run_job(ctx, lambda c: summary)
    [row] = ctx.store.tables["heartbeats"]
    assert row["status"] == status
    assert hb.summary_status(summary) == status


def test_a_run_on_the_settings_fallback_names_it_in_its_detail():
    import dataclasses

    fallback = dataclasses.replace(SETTINGS, fallback_tabs=frozenset({"Copy"}), fallback_mode="earlier versions")
    ctx = make_context(fallback, job="kill_rules", now=MON_NOON, transport=slack_transport())
    hb.run_job(ctx, lambda c: {"fired": []})
    [row] = ctx.store.tables["heartbeats"]
    assert row["status"] == "ok" and row["detail"]["settings_fallback"] == ["Copy"]


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
    """The overlap lock lasts the job's own timeout (W2-4; code-platform-ops B9): poll_approvals has 4 minutes, so a
    run left "running" 3 minutes ago refuses the next, and one left 5 minutes ago (killed at its timeout) no longer
    does. It held the next run off for 60 minutes before. A job with no timeout of its own keeps 60."""
    assert hb.lock_minutes("poll_approvals") == 4 and hb.lock_minutes("enrol") == 15
    assert hb.lock_minutes("no_such_job") == hb.OVERLAP_MINUTES == 60
    ctx = ctx_at(MON_NOON, job="poll_approvals")
    beat(ctx.store, "poll_approvals", MON_NOON - timedelta(minutes=3), status="running", finished=None, run_id="r0")
    ctx.store.tables["heartbeats"][0]["finished_at"] = None
    ran = []
    out = hb.run_job(ctx, lambda c: ran.append(1) or {})
    assert not ran and out["reason"] == hb.OVERLAP_REASON == "previous run still going" and out["other_run_id"] == "r0"
    # 5 minutes on, past the 4-minute timeout: the run left "running" no longer blocks.
    ctx.store.tables["heartbeats"][0]["started_at"] = MON_NOON - timedelta(minutes=5)
    ctx2 = ctx_at(MON_NOON, job="poll_approvals", store=ctx.store)
    hb.run_job(ctx2, lambda c: ran.append(1) or {})
    assert ran == [1]


def test_a_run_records_its_duration_and_exit_code_on_their_columns():
    """W2-4: duration_s and exit_code persist on the row (the logs expire in 7 days): 0 for ok and skipped, 1 for an
    error, 3 for a guardrail, 143 for SIGTERM; a JobResult is stored as its own status."""
    store = None
    for fn, code, status in ((lambda c: {}, 0, "ok"), (lambda c: {"skipped": True, "reason": "x"}, 0, "skipped"),
                             (lambda c: hb.JobResult(status="error", reason="no mailbox answered"), 1, "error")):
        ctx = ctx_at(MON_NOON, job="kill_rules", store=store)
        store = ctx.store
        hb.run_job(ctx, fn)
        row = store.get("heartbeats", run_id=ctx.run_id)
        assert (row["status"], row["exit_code"]) == (status, code) and row["duration_s"] >= 0
    assert row["error"] == "no mailbox answered" and row["detail"]["reason"] == "no mailbox answered"
    for exc, code in ((GuardViolation("no"), 3), (hb.Terminated("SIGTERM"), 143), (RuntimeError("x"), 1)):
        ctx = ctx_at(MON_NOON, job="kill_rules", store=store)

        def boom(c, exc=exc):
            raise exc

        with pytest.raises(type(exc)):
            hb.run_job(ctx, boom)
        assert store.get("heartbeats", run_id=ctx.run_id)["exit_code"] == code
    with pytest.raises(ValueError):
        hb.JobResult(status="fine").summary()


def test_two_starts_for_one_due_minute_run_once():
    """W2-4 (RS P0-4): a scheduled start carries its due minute and claims "<job>:<due minute UTC>". Two starts for
    (enrol, 2026-10-20T11:00) (two schedulers over a deploy, or a catch-up of a run already begun): one row, one run;
    the second returns "previous run still going" and writes nothing."""
    when = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)  # 12:00 BST
    ran = []
    ctx = ctx_at(when, job="enrol")
    ctx.due, ctx.run_id = when, hb.due_run_id("enrol", when)
    assert ctx.run_id == "enrol:2026-10-20T11:00"
    hb.run_job(ctx, lambda c: ran.append("first") or {"enrolled": 0})
    ctx2 = ctx_at(when + timedelta(seconds=5), job="enrol", store=ctx.store)
    ctx2.due, ctx2.run_id = when, hb.due_run_id("enrol", when)
    out = hb.run_job(ctx2, lambda c: ran.append("second") or {})
    assert ran == ["first"] and out["reason"] == hb.OVERLAP_REASON and out["claimed"] == "enrol:2026-10-20T11:00"
    [row] = ctx.store.tables["heartbeats"]
    assert row["run_id"] == "enrol:2026-10-20T11:00" and row["status"] == "ok"
    # The next minute is its own claim: the claim is per due minute, not per job.
    ctx3 = ctx_at(when + timedelta(days=1), job="enrol", store=ctx.store)
    ctx3.due, ctx3.run_id = when + timedelta(days=1), hb.due_run_id("enrol", when + timedelta(days=1))
    hb.run_job(ctx3, lambda c: ran.append("tomorrow") or {})
    assert ran == ["first", "tomorrow"] and len(ctx.store.tables["heartbeats"]) == 2


def test_non_dict_results_are_wrapped():
    ctx = ctx_at(MON_NOON, job="heartbeat_check")
    assert hb.run_job(ctx, lambda c: ["enrol"]) == {"result": ["enrol"]}


# -- expected intervals ------------------------------------------------------------------


def test_expected_matches_the_spec9_schedules():
    assert hb.EXPECTED["poll_replies"] == 45
    assert hb.EXPECTED["poll_approvals"] == 20
    assert hb.EXPECTED["kill_rules"] == 150  # hourly, unchanged (RS P0-7)
    # W2-4 (RS §2 W2-4): hubspot_readback hourly at :00, sync_outcomes hourly at :07 outside the ET send window.
    assert (hb.EXPECTED["hubspot_readback"], hb.EXPECTED["sync_outcomes"]) == (150, 75)
    assert hb.EXPECTED["settings_sync"] == hb.EXPECTED["mailbox_health"] == 26 * 60
    # Tuesday to Saturday (19 Oct 2026): Saturday 09:00 to Tuesday 09:00 is 72 hours, 73 when the clocks go back on
    # the Sunday between; in wall time, as the job runs on Saturdays.
    assert hb.EXPECTED["daily_post"] == 74 * 60 and "daily_post" not in hb.WEEKDAY_JOBS
    assert hb.EXPECTED["monday_readout"] == hb.EXPECTED["hand_check_draw"] == 8 * 24 * 60
    assert not {"public_signals", "verify_in_clay"} & (set(hb.EXPECTED) | set(hb.IMPACT))  # removed with W2-1
    # Build, 1 Oct 2026: the sources and verify_accounts run each weekday (ops/schedule.py), in weekday time.
    for job in ("source_universe", "apollo_signals", "read_pages", "apollo_enrich", "contact_precheck",
                "verify_accounts"):
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
    assert "due at least every 32 days" in posts(t)[-1]["text"]
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
    assert post["channel"] == "C_DEV" and post["text"].startswith(DRY)
    assert "checking for replies" in post["text"] and "reading the settings sheet" not in post["text"]


def test_the_missed_message_names_the_jobs_in_one_sentence_says_what_stopped_and_where_to_look():
    t = slack_transport()
    ctx = ctx_at(MON_NOON, job="heartbeat_check", transport=t)
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(minutes=50))
    beat(ctx.store, "mailbox_health", MON_NOON - timedelta(hours=30), run_id="a")
    beat(ctx.store, "mailbox_health", MON_NOON - timedelta(minutes=50), status="error", run_id="b",
         error="ApiError: instantly HTTP 502 for /accounts: bad gateway")
    hb.check_heartbeats(ctx, jobs=["poll_replies", "mailbox_health"])
    [post] = posts(t)
    assert post["text"] == DRY + "\n".join([
        ":warning: *2 automatic jobs have stopped running*",
        "Overdue: checking for replies (last good run Mon 26 Oct 11:11 UK; due at least every 45 minutes) and checking "
        "the sending mailboxes (last good run Sun 25 Oct 06:01 UK; due at least every 26 hours).",
        "What this means: replies from prospects are not being read and the sending mailboxes are not being checked.",
        "Why checking the sending mailboxes stopped, from its latest run: “ApiError: instantly HTTP 502 for /accounts: "
        "bad gateway”",
        "*What to do:* See why in the worker's logs in Railway, or run `railway ssh -- us-outbound status` for each "
        "job's last run. This clears by itself once they run again.",
    ])


def test_unusable_settings_are_the_cause_said_once_with_the_tab_its_errors_and_the_fix():
    t = slack_transport()
    ctx = ctx_at(MON_NOON, job="heartbeat_check", transport=t)
    refused = "settings are unusable (Industries); fix the sheet, then run `us-outbound sync`"
    for job in ("poll_replies", "poll_approvals", "hubspot_readback"):
        # hubspot_readback is hourly from W2-4 (EXPECTED 150 minutes): its last good run three hours ago is missed.
        ago = 180 if job == "hubspot_readback" else 60
        beat(ctx.store, job, MON_NOON - timedelta(minutes=ago), run_id=f"{job}-ok")
        beat(ctx.store, job, MON_NOON - timedelta(minutes=5), status="error", error=refused)
    assert hb.check_heartbeats(ctx, jobs=["poll_replies", "poll_approvals", "hubspot_readback"])
    [post] = posts(t)
    text = post["text"]
    assert ":rotating_light: *3 automatic jobs have stopped running*" in text
    assert ("Overdue: checking for replies (last good run Mon 26 Oct 11:01 UK; due at least every 45 minutes), "
            "checking the approvals in Slack (") in text
    assert ("What this means: replies from prospects are not being read, approvals given in Slack are not being acted "
            "on and HubSpot is not being updated.") in text
    assert text.count("settings sheet has errors") == 1 and "settings are unusable" not in text  # said once
    assert "Why: the settings sheet has errors on the <https://docs.google.com/spreadsheets/d/" in text
    assert "|Industries tab>, so every job but the safety jobs refuses to start (the safety jobs run on a fallback that only pauses)." in text
    assert "   ◦ Industries row 1:" in text  # the first errors of the saved version, without the logs
    assert ("2. Run `railway ssh -- us-outbound sync` to read the sheet now. Otherwise it is read each time the worker "
            "starts, and at 02:00 UK daily and 11:30 UK on weekdays.") in text
    assert "3. The jobs start again by themselves at their next run." in text


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
    assert health.latest_runs(ctx.store)["poll_replies"]["status"] in {"running", "skipped"}
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
    assert health.latest_runs(ctx.store)["poll_replies"]["status"] == "ok" and seen == [1]


# -- operator stop / start -------------------------------------------------------------------


def test_enrolment_paused_follows_stop_and_live_start():
    ctx = ctx_at(MON_NOON)
    s = ctx.store
    assert health.enrolment_paused(s) is None
    beat(s, health.OPERATOR_STOP, MON_NOON - timedelta(hours=3), status="error")  # a failed stop still stops
    assert health.enrolment_paused(s)["job"] == health.OPERATOR_STOP
    beat(s, health.OPERATOR_START, MON_NOON - timedelta(hours=2), dry_run=True)  # a dry-run start does not
    assert health.enrolment_paused(s) is not None
    beat(s, health.OPERATOR_START, MON_NOON - timedelta(hours=1), dry_run=False)
    assert health.enrolment_paused(s) is None
    beat(s, health.OPERATOR_STOP, MON_NOON, dry_run=True)
    assert health.enrolment_paused(s) is not None


def test_operator_commands_are_never_skipped_by_the_lock():
    ctx = ctx_at(MON_NOON, job=health.OPERATOR_STOP)
    beat(ctx.store, health.OPERATOR_STOP, MON_NOON - timedelta(minutes=2), status="running", run_id="stuck")
    ran = []
    hb.run_job(ctx, lambda c: ran.append(1) or {})
    assert ran == [1]
