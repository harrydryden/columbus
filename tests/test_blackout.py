"""The campaigns paused over the blackout dates and started again after them (Harry, 7 Oct 2026; open question 78).

Enrol skips a blackout date, but Instantly's schedule knows weekdays only, so the follow-ups of leads in flight went
out on one. The hourly blackout job (registry/blackout.py) now pauses every US Outbound campaign before a blackout
date, in US Eastern dates, and starts again exactly the campaigns it paused once it is over, unless a stop, a kill
rule or an empty sending list holds them since. Made-up campaigns and companies only.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.test_cli import Harness
from tests.test_registry import (
    C_HANNAH,
    C_HARRY,
    C_SAM,
    HANNAH,
    HARRY,
    SAM,
    SETTINGS,
    go_live,
    instantly_writes,
    mailbox,
    posts,
    setup,
)
from us_outbound import config_version
from us_outbound.base import heartbeats, holds
from us_outbound.context import ET
from us_outbound.enrol import capacity
from us_outbound.learn import cohorts
from us_outbound.ops import heartbeat as hb
from us_outbound.ops.schedule import by_name
from us_outbound.registry import blackout
from us_outbound.registry import mailboxes as reg
from us_outbound.settings.model import DateRange

LIVE = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, live_sending=True))


def et(*args: int) -> datetime:
    """A US Eastern wall-clock time, as the UTC instant the jobs get."""
    return datetime(*args, tzinfo=ET).astimezone(UTC)


BEFORE = et(2026, 11, 20, 15, 40)  # Fri 20 Nov, the last send day before Thanksgiving week, in the send window
EVE = et(2026, 11, 20, 16, 40)  # the window has closed: the next send day Instantly knows is Mon 23 Nov
DURING = et(2026, 11, 24, 10, 40)  # Tue 24 Nov
AFTER = et(2026, 11, 28, 0, 40)  # Sat 28 Nov, the first run after the blackout's last day
THANKSGIVING_ENDS = date(2026, 11, 30)  # the first send day after it


def with_blackouts(settings, *ranges: tuple[date, date]):
    g = dataclasses.replace(settings.general, blackout_dates=tuple(DateRange(a, b) for a, b in ranges))
    return dataclasses.replace(settings, general=g)


# -- when -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("when, first, until", [
    (BEFORE, None, None),
    (EVE, date(2026, 11, 23), THANKSGIVING_ENDS),
    (et(2026, 11, 22, 12, 0), date(2026, 11, 23), THANKSGIVING_ENDS),  # Sunday
    (DURING, date(2026, 11, 24), THANKSGIVING_ENDS),
    (et(2026, 11, 27, 23, 40), date(2026, 11, 27), THANKSGIVING_ENDS),  # the last blackout date, after its window
    (AFTER, None, None),
    # 18 Dec 2026 to 4 Jan 2027, across the year end.
    (et(2026, 12, 17, 15, 59), None, None),
    (et(2026, 12, 17, 16, 0), date(2026, 12, 18), date(2027, 1, 5)),
    (et(2026, 12, 31, 23, 40), date(2026, 12, 31), date(2027, 1, 5)),
    (et(2027, 1, 2, 12, 0), date(2027, 1, 4), date(2027, 1, 5)),  # a Saturday: Monday 4 Jan is a blackout date
    (et(2027, 1, 4, 23, 40), date(2027, 1, 4), date(2027, 1, 5)),
    (et(2027, 1, 5, 0, 40), None, None),
])
def test_the_hold_is_read_in_us_eastern_dates(when, first, until):
    h = blackout.hold(SETTINGS, when)
    if first is None:
        assert h is None
    else:
        assert (h.first, h.until) == (first, until)
        assert h.words() == f"paused for the blackout until {until:%a} {until.day} {until:%b}"


def test_adjacent_and_overlapping_ranges_are_one_hold_and_a_weekend_alone_holds_nothing():
    adjacent = with_blackouts(SETTINGS, (date(2026, 11, 23), date(2026, 11, 27)), (date(2026, 11, 30), date(2026, 12, 4)))
    for when in (AFTER, et(2026, 11, 29, 12, 0), et(2026, 12, 2, 12, 0)):  # the weekend between them holds too
        assert blackout.hold(adjacent, when).until == date(2026, 12, 7)
    overlapping = with_blackouts(SETTINGS, (date(2026, 11, 23), date(2026, 11, 27)), (date(2026, 11, 25), date(2026, 12, 1)))
    assert blackout.hold(overlapping, et(2026, 11, 30, 12, 0)).until == date(2026, 12, 2)
    weekend = with_blackouts(SETTINGS, (date(2026, 10, 31), date(2026, 11, 1)))  # Saturday and Sunday
    assert blackout.hold(weekend, et(2026, 10, 31, 12, 0)) is None and blackout.hold(weekend, et(2026, 10, 30, 17, 0)) is None


@pytest.mark.parametrize("start, end", [
    (date(2026, 11, 23), date(2026, 11, 27)),  # UK on GMT, US on EST: 5 hours apart
    (date(2026, 10, 30), date(2026, 10, 30)),  # UK back on GMT, US still on EDT: 4 hours apart
    (date(2027, 3, 19), date(2027, 3, 22)),  # US on EDT, UK still on GMT: 4 hours apart
    (date(2027, 7, 2), date(2027, 7, 5)),  # BST and EDT: 5 hours apart
    (date(2026, 12, 18), date(2027, 1, 4)),
])
def test_hourly_runs_pause_before_09_00_et_on_the_first_day_and_start_before_09_00_et_on_the_next_send_day(start, end):
    """The job runs at :40 UK every hour, which is :40 ET too: both offsets are whole hours."""
    s = with_blackouts(SETTINGS, (start, end))
    runs = [datetime.combine(start - timedelta(days=4), datetime.min.time(), UTC) + timedelta(hours=i, minutes=40)
            for i in range(24 * ((end - start).days + 10))]
    first_day_opens = et(start.year, start.month, start.day, 9, 0)
    held = [r for r in runs if blackout.hold(s, r) is not None]
    assert held[0] < first_day_opens and len([r for r in held if r < first_day_opens]) >= 10
    assert all(blackout.hold(s, r) for r in runs if first_day_opens <= r and r.astimezone(ET).date() <= end)
    until = capacity.next_send_day(end + timedelta(days=1), s)
    reopens = et(until.year, until.month, until.day, 9, 0)
    started = [r for r in runs if r > held[-1]]
    assert started[0] < reopens and len([r for r in started if r < reopens]) >= 8
    assert held[-1].astimezone(ET).date() <= end  # never held past the blackout's last day


def test_the_send_forecast_and_the_hold_agree():
    """capacity.step_days moves a step due on a blackout date to the next send day: the day the job starts the
    campaigns for."""
    assert capacity.step_days(date(2026, 11, 16), SETTINGS) == [date(2026, 11, 16), THANKSGIVING_ENDS,
                                                                 date(2026, 12, 7), date(2026, 12, 14)]
    assert capacity.step_days(date(2026, 12, 14), SETTINGS)[1] == date(2027, 1, 5) == blackout.hold(
        SETTINGS, et(2026, 12, 21, 12, 0)).until


# -- the job ----------------------------------------------------------------------------------------------------------


def world(now: datetime = EVE, *, live: bool = True, settings=LIVE):
    ctx, t, inst, sheets = setup(settings, live=live, now=now)
    ctx.job = "blackout"
    go_live(ctx, at=now - timedelta(days=30))
    inst.standard(C_HANNAH, [HANNAH], 30, status=1)
    inst.standard(C_SAM, [SAM], 30, status=3)  # completed: no lead left, starts again when one is added
    inst.standard(C_HARRY, [HARRY], 30, status=2)  # paused by someone else before the blackout
    ctx.store.insert("contacts", [{"contact_id": f"k{i}", "account_id": f"a{i}", "instantly_lead_id": f"L{i}",
                                   "instantly_campaign": C_HANNAH, "enrolled_at": et(2026, 11, 16, 11, 0)}
                                  for i in range(3)])
    return ctx, t, inst


def at(ctx, now: datetime):
    ctx.now = now
    ctx.run_id = f"run-{now:%m%d%H%M}"
    return ctx


def statuses(inst) -> dict[str, int]:
    return {c["name"]: c["status"] for c in inst.campaigns.values()}


def rows(ctx, kind=None) -> list[dict]:
    found = ctx.store.select("config_log", {"kind": [config_version.BLACKOUT_PAUSE, config_version.BLACKOUT_RESUME]})
    return [r for r in found if kind is None or r["kind"] == kind]


def test_outside_a_blackout_with_nothing_paused_it_reads_nothing():
    ctx, t, inst = world(BEFORE)
    t.requests.clear()
    assert blackout.run(ctx) == {"dry_run": False, "held": False, "until": None, "paused_by_blackout": []}
    assert not [r for r in t.requests if "instantly" in r.url]


def test_before_a_blackout_it_pauses_what_sends_and_records_each_pause():
    ctx, t, inst = world(EVE)
    out = blackout.run(ctx)
    assert out["paused"] == [C_HANNAH, C_SAM] and out["left_alone"] == [C_HARRY] and out["errors"] == []
    assert statuses(inst) == {C_HANNAH: 2, C_SAM: 2, C_HARRY: 2}
    calls = [c for c in ctx.guard.writes("instantly") if c.action == "campaign.pause"]
    assert [(c.target, c.sent) for c in calls] == [(C_HANNAH, True), (C_SAM, True)]  # through the guard, live
    by = {r["campaign"]: r for r in rows(ctx)}
    assert set(by) == {C_HANNAH, C_SAM}
    assert by[C_HANNAH]["detail"] == {"from": "active", "resumes_on": "2026-11-30"}
    assert by[C_HANNAH]["leads_in_flight"] == 3 and by[C_SAM]["leads_in_flight"] == 0
    assert by[C_HANNAH]["changed_by"] == "blackout"
    [post] = posts(t)
    assert post["channel"] == "C_ALERT"
    assert ("Paused 2 US Outbound campaigns for the blackout (US Outbound – Hannah Spalding, US Outbound – Sam "
            "Jackson), so no follow-up goes out on a blackout date. They start again by themselves for Mon 30 Nov, "
            "when Instantly sends the follow-ups that fell due.") in post["text"]
    # The next runs find them paused: nothing to do, nothing posted again.
    out = blackout.run(at(ctx, DURING))
    assert out["paused"] == [] and out["still_paused"] == [C_HANNAH, C_SAM] and out["left_alone"] == [C_HARRY]
    assert len(posts(t)) == 1 and len(rows(ctx)) == 2


def test_one_started_by_hand_over_the_blackout_is_paused_again():
    ctx, t, inst = world(EVE)
    blackout.run(ctx)
    inst.by_name(C_HANNAH)["status"] = 1
    assert blackout.run(at(ctx, DURING))["paused"] == [C_HANNAH] and inst.by_name(C_HANNAH)["status"] == 2
    assert len(posts(t)) == 1  # the line is posted once a blackout


def test_after_it_exactly_the_campaigns_it_paused_start_again():
    ctx, t, inst = world(EVE)
    blackout.run(ctx)
    blackout.run(at(ctx, DURING))
    out = blackout.run(at(ctx, AFTER))
    assert out["held"] is False and out["resumed"] == [C_HANNAH, C_SAM] and out["left_paused"] == {}
    assert statuses(inst) == {C_HANNAH: 1, C_SAM: 1, C_HARRY: 2}  # Harry's was paused before: left alone
    resumed = rows(ctx, config_version.BLACKOUT_RESUME)
    assert {r["campaign"]: r["detail"]["outcome"] for r in resumed} == {C_HANNAH: "resumed", C_SAM: "resumed"}
    assert blackout.paused(ctx.store) == {}
    assert "Started 2 US Outbound campaigns again after the blackout (US Outbound – Hannah Spalding, US Outbound – " \
           "Sam Jackson): Instantly sends the follow-ups that fell due from Mon 30 Nov." in posts(t)[-1]["text"]
    t.requests.clear()
    assert blackout.run(at(ctx, AFTER + timedelta(hours=1)))["paused_by_blackout"] == []  # once
    assert not [r for r in t.requests if "instantly" in r.url]


def test_an_operator_stop_over_the_blackout_keeps_them_paused_and_says_so():
    ctx, t, inst = world(EVE)
    blackout.run(ctx)
    ctx.store.insert("heartbeats", [{"run_id": "stop-1", "job": heartbeats.OPERATOR_STOP, "status": "ok",
                                     "dry_run": False, "started_at": DURING}])
    out = blackout.run(at(ctx, AFTER))
    assert out["resumed"] == [] and set(out["left_paused"]) == {C_HANNAH, C_SAM}
    assert out["left_paused"][C_HANNAH] == ("an operator stop is in force since Tue 24 Nov 15:40 UK: "
                                            "`us-outbound start --live` starts it")
    assert statuses(inst)[C_HANNAH] == 2 and blackout.paused(ctx.store) == {}  # settled: `start` is the way now
    assert "Left paused: US Outbound – Hannah Spalding (an operator stop is in force" in posts(t)[-1]["text"]
    assert not [r for r in instantly_writes(t) if r.url.endswith("/activate")]


def test_a_kill_rule_pause_or_an_empty_sending_list_since_keeps_one_paused():
    ctx, t, inst = world(EVE)
    blackout.run(ctx)
    ctx.store.insert("hitl_items", [{"item_id": "k-1", "kind": holds.KIND, "status": "open", "created_at": DURING,
                                     "payload": {"action": holds.PAUSE_MAILBOX, "mailboxes": [HANNAH],
                                                 "campaigns_paused": [C_HANNAH], "reason": "6% bounced"}}])
    out = blackout.run(at(ctx, AFTER))
    assert out["resumed"] == [C_SAM]
    assert out["left_paused"] == {C_HANNAH: "a kill rule paused it since (6% bounced): it starts again once its "
                                            "mailbox is Active"}
    assert statuses(inst) == {C_HANNAH: 2, C_SAM: 1, C_HARRY: 2}

    no_sam = dataclasses.replace(LIVE, mailboxes=(mailbox(HANNAH, "Hannah Spalding"), mailbox(SAM, "Sam Jackson", "Paused"),
                                                  mailbox(HARRY, "Harry Dryden")))
    ctx, t, inst = world(EVE, settings=no_sam)
    blackout.run(ctx)
    assert blackout.run(at(ctx, AFTER))["left_paused"] == {C_SAM: "its owner has no Active mailbox now"}


def test_one_instantly_shows_completed_or_active_needs_nothing():
    ctx, t, inst = world(EVE)
    blackout.run(ctx)
    inst.by_name(C_SAM)["status"] = 3  # Instantly marked it completed: no lead left to email
    inst.by_name(C_HANNAH)["status"] = 1  # someone started it after the blackout
    out = blackout.run(at(ctx, AFTER))
    assert out["resumed"] == [] and out["not_needed"] == {C_HANNAH: "Instantly shows it active",
                                                          C_SAM: "Instantly shows it completed"}
    assert not [r for r in instantly_writes(t) if r.url.endswith("/activate")] and blackout.paused(ctx.store) == {}


def test_a_failed_start_is_tried_again_by_the_next_run():
    ctx, t, inst = world(EVE)
    blackout.run(ctx)
    t.route("POST", f"/campaigns/{inst.by_name(C_SAM)['id']}/activate", status=500, body={"error": "busy"})
    out = blackout.run(at(ctx, AFTER))
    assert out["resumed"] == [C_HANNAH] and out["errors"][0].startswith(C_SAM)
    assert set(blackout.paused(ctx.store)) == {C_SAM}
    t.routes.pop()
    assert blackout.run(at(ctx, AFTER + timedelta(hours=1)))["resumed"] == [C_SAM]
    assert statuses(inst)[C_SAM] == 1


def test_a_dry_run_says_what_it_would_do_and_changes_nothing():
    ctx, t, inst = world(EVE, live=False, settings=SETTINGS)
    out = blackout.run(ctx)
    assert out["would_pause"] == [C_HANNAH, C_SAM] and "paused" not in out
    assert statuses(inst) == {C_HANNAH: 1, C_SAM: 3, C_HARRY: 2} and instantly_writes(t) == [] and rows(ctx) == []
    [post] = posts(t)
    assert post["channel"] == "C_DEV" and "Would pause 2 US Outbound campaigns" in post["text"]
    assert "Running dry, so nothing was paused: `us-outbound stop --live` pauses them by hand." in post["text"]


def test_the_job_is_hourly_live_and_its_heartbeat_is_expected():
    job = by_name()["blackout"]
    assert (job.cron, job.live, job.enabled, job.timeout_minutes) == ("40 * * * *", True, True, 10)
    assert hb.EXPECTED["blackout"] == 150 and "blackout" not in hb.WEEKDAY_JOBS


# -- the rest of the system knows ---------------------------------------------------------------------------------


def test_mailbox_health_leaves_a_blackout_pause_alone_and_starts_nothing_over_a_blackout():
    ctx, t, inst = world(EVE)
    blackout.run(ctx)
    inst.by_name(C_HARRY)["status"] = 0  # a draft, which mailbox_health would start after go-live
    t.requests.clear()
    out = reg.mailbox_health(at(ctx, DURING))
    starting = out["campaign_start"]
    assert starting["started"] == [] and starting["paused"] == []
    assert starting["blackout"] == {
        "Hannah Spalding": "paused for the blackout until Mon 30 Nov; the blackout job starts it again then",
        "Sam Jackson": "paused for the blackout until Mon 30 Nov; the blackout job starts it again then",
        "Harry Dryden": "not started over the blackout; the first mailbox check after it starts it (sends resume "
                        "Mon 30 Nov)"}
    assert not [r for r in instantly_writes(t) if r.url.endswith("/activate")]
    assert "campaign_start" not in out["post_reasons"]  # never a reason to post on its own
    # After it, the draft starts as before.
    blackout.run(at(ctx, AFTER))
    assert reg.mailbox_health(at(ctx, AFTER + timedelta(hours=7)))["campaign_start"]["started"] == [C_HARRY]


def test_enrol_and_a_send_approval_read_paused_for_the_blackout_not_run_start():
    ctx, t, inst = world(EVE)
    blackout.run(ctx)
    at(ctx, DURING)
    why = capacity.campaigns_not_sending(ctx, ["Hannah Spalding", "Harry Dryden"])
    assert why == {"Hannah Spalding": "Hannah Spalding's campaign is paused for the blackout until Mon 30 Nov",
                   "Harry Dryden": "Harry Dryden's campaign is not active in Instantly (paused): run "
                                   "`us-outbound start --live`"}


def test_start_over_a_blackout_resumes_enrolment_and_leaves_the_campaigns_for_the_job(capsys):
    class During(Harness):
        def __call__(self, job, live_flag, operator=False):
            ctx = super().__call__(job, live_flag, operator)
            ctx.now = DURING + timedelta(minutes=len(self.contexts))
            return ctx

    h = During(LIVE)
    from tests.test_ramp import past_ramp

    past_ramp(h.store, LIVE.mailboxes, DURING)
    for name, accounts in ((C_HANNAH, [HANNAH]), (C_SAM, [SAM]), (C_HARRY, [HARRY, "harry@tryspill.org"])):
        h.instantly.standard(name, accounts, 30 * len(accounts), status=2)
    assert h.run("stop", "--live") == 0
    assert h.run("start", "--live") == 0
    assert heartbeats.enrolment_paused(h.store) is None  # enrolment resumes
    assert all(c["status"] == 2 for c in h.instantly.campaigns.values())  # the campaigns wait for the job
    assert "paused for the blackout until Mon 30 Nov: the blackout job starts them then" in capsys.readouterr().out
    assert set(blackout.paused(h.store)) == {C_HANNAH, C_SAM, C_HARRY}
    ctx = h("blackout", True)
    ctx.now = AFTER
    assert blackout.run(ctx)["resumed"] == [C_HANNAH, C_HARRY, C_SAM]
    assert all(c["status"] == 1 for c in h.instantly.campaigns.values())


def test_status_golive_and_the_cohort_report_say_paused_for_the_blackout(capsys, monkeypatch):
    from tests.test_golive import lines_of, ready_world
    from us_outbound.ops import cli

    f = ready_world(monkeypatch)
    f.ctx.now = EVE
    f.ctx.job = "blackout"
    f.ctx.guard.configure(live=True)
    blackout.run(f.ctx)
    f.ctx.guard.configure(live=False)
    f.ctx.now = DURING
    assert cli.main(["golive"], context_factory=f) in (0, 1)
    out = capsys.readouterr().out
    assert lines_of(out)["Campaigns"] == "PASS  Campaigns: all 3 exist and match; 3 paused for the blackout until Mon 30 Nov"
    assert "US Outbound – Hannah Spalding: matches (paused); paused for the blackout until Mon 30 Nov, when the " \
           "blackout job starts it again" in out
    assert cli.main(["status"], context_factory=f) == 0
    assert "  US Outbound – Hannah Spalding: paused for the blackout until Mon 30 Nov" in capsys.readouterr().out
    lines = cohorts.log_lines(f.ctx, None, None)
    assert any(line.endswith("US Outbound – Hannah Spalding paused for the blackout until 2026-11-30 (0 leads in "
                             "flight)") for line in lines), lines
