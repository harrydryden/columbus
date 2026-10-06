"""monday_readout (learn/readout.py; Harry, 6 Oct 2026): last week in plain words, the targets and exit criteria, the
cuts and the signal table from the readout views, and the tests at a look. Small numbers read as small."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from tests.test_db import db, dsn, learning_world, store  # noqa: F401  (Postgres fixtures, for the end-to-end read)
from tests.test_registry import SETTINGS, slack_routes
from us_outbound.clients.guard import Guard
from us_outbound.learn import readout, signal_value
from us_outbound.ops import cli, heartbeat, schedule

MON = datetime(2026, 10, 12, 7, 30, tzinfo=UTC)  # Monday 08:30 UK (BST)
LAST_WEEK = datetime(2026, 10, 7, 15, tzinfo=UTC)  # Wednesday of the week read
LIVE = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, live_sending=True,
                                                                  optout_tested=True))


def week_row(cut: str, value: str, start: date = date(2026, 10, 5), **numbers) -> dict:
    """A v_readout_weekly row: zeros unless given."""
    cols = (*readout.COHORT, *readout.ACTIVITY)
    return {"week_start": start, "cut": cut, "cut_value": value, **{c: 0 for c in cols}, **numbers}


def world(settings=SETTINGS, *, live=False, now=MON, weekly=None, signals=None):
    t = slack_routes(FakeTransport())
    ctx = make_context(settings, live=live, transport=t, now=now, job=readout.JOB)
    if weekly is not None:
        ctx.store.query_handlers["v_readout_weekly"] = lambda s, p: [dict(r) for r in weekly]
    if signals is not None:
        ctx.store.query_handlers["v_signal_value"] = lambda s, p: [dict(r) for r in signals]
    return ctx, t


def ev(i, type_, at=LAST_WEEK, **kw):
    return {"event_id": kw.pop("event_id", f"e{i}"), "type": type_, "occurred_at": at + timedelta(minutes=i), **kw}


def text(ctx) -> str:
    return "\n".join(readout.build(ctx)[0])


# -- small numbers ------------------------------------------------------------------------------------------------


def test_small_numbers_read_too_few_to_read_never_a_rate():
    weekly = [week_row("all", "all", accounts_enrolled=12, accounts_delivered=12, accounts_replied=2,
                       accounts_positive=1, sends=12, replies=2, positive_replies=1),
              week_row("tier", "Priority", accounts_enrolled=3, accounts_delivered=3, accounts_replied=1, sends=3,
                       replies=1)]
    ctx, _ = world(weekly=weekly)
    out = text(ctx)
    assert "Replied so far: 2 of 12 companies emailed (too few to read; the working assumption is 3 to 5%)" in out
    assert "Positive: 1 (too few to read; about 1% assumed). Meetings: 0 (too few to read; 0.5 to 1% assumed)" in out
    [tier] = [line for line in out.splitlines() if line.startswith("  Priority:")]
    assert tier.endswith("3 companies emailed, 1 replied (too few to read)") and "33" not in tier
    assert "16.7%" not in out  # 2 of 12 is never given as a rate


def test_from_30_companies_a_rate_is_given():
    weekly = [week_row("all", "all", accounts_enrolled=120, accounts_delivered=120, accounts_replied=5,
                       accounts_positive=1, accounts_meeting=1, accounts_window_closed=40)]
    out = text(world(weekly=weekly)[0])
    assert "Replied so far: 5 of 120 companies emailed (4.2%; the working assumption is 3 to 5%)." in out
    assert "Meetings: 1 (0.8%; 0.5 to 1% assumed)" in out
    assert "So far: 120 companies emailed, 120 delivered; 40 of their 28-day reply windows have closed, so the " \
           "rates still rise as replies come in." in out
    assert "Stop rule: 1 meeting from the first 120 companies emailed; enrolment pauses if the first 1,500 bring " \
           "fewer than 5." in out
    assert readout.rate(5, 29) == "too few to read" and readout.rate(5, 30) == "16.7%"


# -- last week and the cuts -----------------------------------------------------------------------------------------


def test_last_week_counts_only_last_week_in_uk_time():
    ctx, _ = world(weekly=[])
    ctx.store.insert("events", [
        ev(1, "sent", step=1, account_id="a1"), ev(2, "sent", step=2, account_id="a2"),
        ev(3, "replied", account_id="a1", reply_class="positive"),
        ev(4, "replied", account_id="a2", reply_class="out_of_office"),  # not a human reply
        ev(5, "replied", account_id="a3", reply_class=None),  # not yet classified: counts as human
        ev(6, "meeting_booked", account_id="a1", source="hubspot_meeting"),
        ev(7, "meeting_booked", account_id="a1", source="hubspot_deal"),  # the same company: one meeting
        ev(8, "bounced", contact_id="k2", step=1), ev(9, "unsubscribed", account_id="a2"),
        # Sunday 23:30 UK is last week; Monday 00:30 UK is this week; the Sunday before is the week before.
        ev(10, "sent", at=datetime(2026, 10, 11, 22, 30, tzinfo=UTC), step=1, account_id="a4"),
        ev(11, "sent", at=datetime(2026, 10, 11, 23, 30, tzinfo=UTC), step=1, account_id="a5"),
        ev(12, "sent", at=datetime(2026, 10, 4, 22, 0, tzinfo=UTC), step=1, account_id="a6"),
    ])
    lines, nums = readout.build(ctx)
    assert nums["last_week"] == {"sent": 3, "replies": 2, "positive": 1, "meetings": 1, "bounces": 1,
                                 "unsubscribes": 1}
    assert lines[1] == ("*Last week* (Mon 05 Oct to Sun 11 Oct, UK): 3 emails sent · 2 replies, 1 positive · "
                        "1 meeting booked · 1 bounce · 1 unsubscribe")
    assert lines[0] == "*Monday readout, Mon 12 Oct* (dry-run)"


def test_the_cuts_show_last_week_and_so_far_busiest_first():
    weekly = [
        week_row("all", "all", sends=40),
        week_row("angle", "General", sends=10, replies=1, accounts_enrolled=40, accounts_delivered=40,
                 accounts_replied=2),
        week_row("angle", "Upgrade the EAP", sends=30, replies=2, positive_replies=1, meetings_booked=1, bounces=1),
        week_row("angle", "Upgrade the EAP", start=date(2026, 9, 28), accounts_enrolled=20, accounts_delivered=20,
                 accounts_replied=3),
        week_row("sender", "Hannah Spalding", sends=40),
        week_row("step", "step 2", sends=15, replies=1), week_row("step", "step 1", sends=25, replies=2, bounces=1),
        week_row("tier", "Held", start=date(2026, 9, 28)),  # nothing in it: not listed
    ]
    out = text(world(weekly=weekly)[0]).splitlines()
    angle = out.index("*By angle* (last week · so far)")
    assert out[angle + 1] == ("  Upgrade the EAP: 30 sent, 2 replies (1 positive), 1 meeting, 1 bounce · 20 companies "
                              "emailed, 3 replied (too few to read)")
    assert out[angle + 2] == "  General: 10 sent, 1 reply (0 positive), 0 meetings, 0 bounces · 40 companies emailed, " \
                             "2 replied (5.0%)"
    step = out.index("*By step* (last week)")
    assert out[step + 1:step + 3] == ["  step 1: 25 sent, 2 replies (0 positive), 1 bounce",
                                      "  step 2: 15 sent, 1 reply (0 positive), 0 bounces"]
    assert "*By tier* (last week · so far)" not in out and "*By sender* (last week · so far)" in out


def test_without_the_views_the_rest_still_posts():
    ctx, _ = world()  # a MemoryStore with no view handlers
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["views"] is False and "need the database's views" in out
    assert "*Exit criteria to scale*" in out and "*Tests*" in out and "*By tier*" not in out


# -- the exit criteria to scale ---------------------------------------------------------------------------------------


def test_exit_criteria_met():
    ctx, _ = world(LIVE, weekly=[])
    sends = [ev(i, "sent", step=1, account_id=f"a{i}", at=MON - timedelta(days=20)) for i in range(150)]
    ctx.store.insert("events", [*sends, ev(200, "bounced", contact_id="k1", step=1),
                                ev(201, "replied", event_id="em-1", account_id="a1", reply_class="positive"),
                                ev(202, "replied", event_id="em-2", account_id="a2", reply_class="out_of_office"),
                                ev(203, "replied", event_id="em-3", account_id="a3", reply_class="unsubscribe"),
                                ev(204, "unsubscribed", event_id="unsubscribed:em-3", contact_id="k3")])
    ctx.store.insert("contacts", [{"contact_id": "k3", "account_id": "a3", "suppressed": True,
                                   "suppressed_reason": "unsubscribe"}])
    ctx.store.insert("hitl_items", [{"item_id": "reply:em-1", "kind": "reply", "status": "handled"},
                                    {"item_id": "out_of_office:em-2", "kind": "out_of_office", "status": "handled"}])
    ctx.store.insert("heartbeats", [{"run_id": "e1", "job": "enrol", "status": "ok", "started_at": LAST_WEEK,
                                     "detail": {"skipped": {"copy blocked": 2}}}])
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["exit_criteria"] == {"bounces": True, "complaints": True, "unsubscribes": True, "replies": True,
                                     "refusals": True}
    assert "  Met: bounces under 2%: 1 of 150 sends so far (0.7%)." in out
    assert "  Met: unsubscribes confirmed end to end: the seed test passed; 1 opt-out so far, each recorded" in out
    assert "  Met: every reply classified and routed: 3 replies so far." in out
    assert ("  Met: no unexplained refusal: no guard refusal last week; 2 emails held back by the copy rules, each "
            "with its reason in enrol's run summary.") in out


def test_exit_criteria_not_met_say_why():
    ctx, _ = world(weekly=[])  # optout_tested is no
    ctx.store.insert("events", [
        *[ev(i, "sent", step=1, account_id=f"a{i}") for i in range(40)], ev(50, "bounced", contact_id="k1"),
        ev(51, "complained", contact_id="k2"),
        ev(52, "replied", event_id="em-9", account_id="a9", reply_class=None, at=MON - timedelta(hours=3)),
        ev(53, "replied", event_id="em-8", account_id="a8", reply_class="objection"),  # no card
    ])
    ctx.store.insert("contacts", [{"contact_id": "k7", "suppressed": True, "suppressed_reason": "unsubscribe"}])
    ctx.store.insert("heartbeats", [{"run_id": "x", "job": "enrol", "status": "error", "started_at": LAST_WEEK,
                                     "detail": {"violation": True}, "error": "GuardViolation: ..."}])
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert not any(nums["exit_criteria"].values())
    assert ("  Not met: bounces under 2%: 1 of 40 sends so far (2.5%; on under 100 sends one bounce moves it a "
            "lot).") in out
    assert "  Not met: no complaints: 1 spam complaint recorded." in out
    assert "  Not met: unsubscribes confirmed end to end: optout_tested is no on the General tab" in out
    assert "; 1 opt-out not yet recorded in Instantly's blocklist and HubSpot" in out
    assert ("  Not met: every reply classified and routed: 1 not classified after an hour; 1 with no reply card, "
            "out-of-office record or opt-out (`us-outbound replies list`).") in out
    assert "  Not met: no unexplained refusal: 1 guard refusal last week, in enrol, each with its error" in out


# -- the signal table and the tests ---------------------------------------------------------------------------------


def signal_row(name: str, **numbers) -> dict:
    base = {"signal": name, "weight": 10, "below_control": False, "accounts_enrolled": 0, "accounts_sent": 0,
            "accounts_delivered": 0, "accounts_replied": 0, "accounts_positive": 0, "accounts_meeting": 0,
            "without_delivered": 0, "without_replied": 0}
    return {**base, **numbers}


def test_the_signal_headline_says_too_few_and_reads_the_rest():
    signals = [
        signal_row("EAP named", accounts_enrolled=60, accounts_sent=60, accounts_delivered=60, accounts_replied=12,
                   accounts_positive=3, accounts_meeting=2, without_delivered=100, without_replied=4),
        signal_row("Hiring and growth", accounts_enrolled=40, accounts_sent=40, accounts_delivered=40,
                   accounts_replied=2, without_delivered=120, without_replied=6),
        signal_row("New People leader", accounts_enrolled=4, accounts_sent=3, accounts_delivered=3,
                   accounts_replied=1, without_delivered=157, without_replied=17),
        signal_row("Layoffs"),
    ]
    out = text(world(weekly=[], signals=signals)[0])
    assert ("  EAP named (weight 10): 60 enrolled with it, 60 emailed, 12 replied, 3 positive, 2 meetings (20.0% "
            "replied, against 4.0% of 100 without): replies more (p = 0.00)") in out
    assert "  Hiring and growth (weight 10): 40 enrolled with it, 40 emailed, 2 replied, 0 positive, 0 meetings " \
           "(5.0% replied, against 5.0% of 120 without): no clear difference yet (p = 1.00)" in out
    assert "  Too few to read (under 30 companies emailed with or without it): New People leader (3)." in out
    assert "33.3%" not in out and "Layoffs" not in out
    assert signal_value.verdict(signals[2]) == "too few to read"


def test_tests_at_a_look_are_listed_with_their_read(monkeypatch):
    from us_outbound.settings.model import Test

    test = Test("t1", "h", "eap-v1", "general-v1", 400, "running", date(2026, 8, 1), date(2026, 10, 8), "reply rate")
    ctx, _ = world(dataclasses.replace(SETTINGS, tests=(test,)), weekly=[])
    ctx.store.insert("contacts", [{"contact_id": f"k{i}", "account_id": f"a{i}", "test_id": "t1",
                                   "copy_version": "eap-v1" if i % 2 else "general-v1"} for i in range(4)])
    ctx.store.insert("events", [ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}",
                                   at=datetime(2026, 9, 1, tzinfo=UTC)) for i in range(4)])
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["tests_at_a_look"] == ["t1"]
    assert ("  t1 (ab) reached look 1 (the read date): Thu 08 Oct 2026 on Thu 08 Oct: eap-v1 0 of 2 replied (0.0%); "
            "general-v1 0 of 2 replied (0.0%)") in out


# -- the job: schedule, heartbeat, cli, the post -------------------------------------------------------------------------


def test_registered_on_the_schedule_heartbeat_and_cli():
    job = schedule.by_name()["monday_readout"]
    assert (job.cron, job.live, job.enabled) == ("30 8 * * 1", True, True)
    assert cli.JOBS["monday_readout"] == "us_outbound.learn.readout:run" and "monday_readout" in heartbeat.scheduled_jobs()
    assert heartbeat.EXPECTED["monday_readout"] == 8 * 24 * 60


def test_dry_run_posts_to_the_dev_channel_and_writes_only_its_heartbeat():
    ctx, t = world(weekly=[])
    before = {k: list(v) for k, v in ctx.store.tables.items()}
    summary = heartbeat.run_job(ctx, readout.run)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_DEV" and post.json["text"].startswith("[dry-run → #us-outbound] *Monday readout")
    assert summary["dry_run"] and summary["alert"]["posted"]
    after = {k: v for k, v in ctx.store.tables.items() if v != before[k]}
    assert set(after) == {"heartbeats"}


def test_live_posts_to_the_alert_channel():
    ctx, t = world(LIVE, live=True, weekly=[])
    readout.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_ALERT" and "*Exit criteria to scale*" in post.json["text"]


def test_the_cli_prints_it_and_posts_nothing(capsys):
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("readout") == 0
    out = capsys.readouterr().out
    assert "*Monday readout" in out and "*Exit criteria to scale*" in out
    assert not [r for r in h.transport.requests if r.url.endswith("chat.postMessage")]


def test_signals_value_prints_the_table(capsys):
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("signals", "value") == 0
    assert "reads the database's views" in capsys.readouterr().out  # a MemoryStore has none
    h.store.query_handlers["v_signal_value"] = lambda s, p: [
        signal_row("EAP named", accounts_enrolled=4, accounts_sent=3, accounts_delivered=3, accounts_replied=1,
                   without_delivered=40, without_replied=2),
        signal_row("Layoffs")]
    assert h.run("signals", "value") == 0
    out = capsys.readouterr().out
    assert ("  EAP named (weight 10): 4 enrolled with it, 3 emailed, 1 replied, 0 positive, 0 meetings: too few to "
            "read (under 30 emailed with it)") in out
    assert "  No company enrolled with: Layoffs." in out and "%" not in out


# -- end to end on Postgres: the views behind the readout --------------------------------------------------------------


def test_the_readout_reads_the_views_on_postgres(store):  # noqa: F811
    now = learning_world(store)
    store.guard = Guard()
    ctx = make_context(SETTINGS, store=store, now=now)
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["views"] is True and nums["so_far"]["accounts_enrolled"] == 70
    assert "Replied so far: 12 of 69 companies emailed (17.4%" in out
    assert "  Priority: 0 sent, 0 replies (0 positive), 0 meetings, 0 bounces · 35 companies emailed, 10 replied " \
           "(28.6%)" in out
    assert "  eap_named (weight 10): 36 enrolled with it, 35 emailed, 10 replied, 3 positive, 2 meetings (28.6% " \
           "replied, against 5.9% of 34 without): replies more" in out
    assert "hiring" not in out.split("*Signal value*")[1].split("*Tests*")[0]  # nobody emailed with it
