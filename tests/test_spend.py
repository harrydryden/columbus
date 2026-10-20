"""Credit and spend alerts (learn/spend.py; Harry, 7 Oct 2026): before the 09:00 daily post, ask for a purchase or a
raise before Apollo's balance, the monthly budgets, Claude's cap or Apollo's visitor credits run out."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_daily_post import world as post_world
from tests.test_registry import SETTINGS, slack_routes
from us_outbound.clients.db import new_id
from us_outbound.learn import daily_post, spend
from us_outbound.clients.slack import dry_run_note

DRY = dry_run_note("#us-outbound") + "\n"

TUE_9 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)  # 09:00 UK (GMT)
GENERAL = "<https://docs.google.com/spreadsheets/d/sheet-test/edit|General tab>"
STOPS_APOLLO = "finding new companies and looking up contacts' emails"
FLOOR_TEXT = (f":warning: *Apollo runs out in about 14 days (around Tue 10 Nov); then we stop {STOPS_APOLLO}.*\n"
              "6,400 credits left, spending about 100 a day; spending stops under `apollo_floor`, the 5,000 credits we "
              "keep in reserve.\n"
              "*What to do:* buy credits in Apollo (about 3,000 credits covers a month at this pace). Or spend less: "
              f"lower `apollo_monthly_credits` (now 2,000) on the {GENERAL}.")
UNDER_FLOOR = f":rotating_light: *Apollo is below our reserve, so we have stopped {STOPS_APOLLO}.*"


def usage(balance=None, visitors=None) -> dict:
    stats = {}
    if balance is not None:
        stats["lead_credit"] = {"limit": 40_000, "consumed": 40_000 - balance, "left_over": balance}
    if visitors is not None:
        stats["inbound_website_visitor_credit"] = {"limit": 1200, "consumed": 1200 - visitors, "left_over": visitors}
    return {"credit_usage_stats": stats}


def world(*, balance=30_000, visitors=None, now=TUE_9, live=True, **general):
    t = slack_routes(FakeTransport())
    t.route("POST", "/usage_stats/credit_usage_stats", body=usage(balance, visitors))
    s = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, **general))
    ctx = make_context(s, job="daily_post", now=now, transport=t, live=live)
    return ctx, t


def spent(ctx, system, amount, *, days_ago=1, usd=0.0):
    ctx.store.insert("credit_ledger", [{"entry_id": new_id(), "system": system, "job": "test", "run_id": None,
                                        "account_id": None, "credits": float(amount), "usd": float(usd),
                                        "occurred_at": ctx.now - timedelta(days=days_ago), "note": ""}])


def posts(t) -> list[str]:
    return [r.json["text"] for r in t.requests if r.url.endswith("chat.postMessage")]


def asked(ctx) -> list[str]:
    return [line for _, line in spend.asks(ctx, spend.read(ctx))]


# -- Apollo's balance against apollo_floor ---------------------------------------------------------------------


def test_the_floor_is_asked_about_three_weeks_ahead_at_the_last_fortnight_s_pace():
    ctx, t = world(balance=6_400)
    for day in range(1, 15):
        spent(ctx, "apollo", 100, days_ago=day)  # 1,400 in 14 days: 100 a day
    sp, sent = spend.check(ctx)
    assert sp.apollo_pace == 100 and round(sp.days_to_floor) == 14
    [text] = posts(t)
    assert text == f"<@U_HARRY> :warning: *Credits and settings: 1 thing needs a decision*\n{FLOOR_TEXT}"
    assert sent["keys"] == ["apollo_floor:2026-10-27"]


def test_the_floor_alert_is_once_a_day_and_survives_a_restart():
    ctx, t = world(balance=4_000)
    spend.check(ctx)
    again, _ = world(balance=4_000)
    again.store = ctx.store  # a restart: a new process over the same database
    again.store.guard = again.guard
    spend.check(again)
    assert len(posts(t)) == 1 and posts(again.clients.transport) == []
    assert posts(t)[0] == (
        "<@U_HARRY> :rotating_light: *Credits and settings: 1 thing needs a decision*\n"
        f"{UNDER_FLOOR}\n"
        "4,000 credits left; spending stops under `apollo_floor`, the 5,000 credits we keep in reserve.\n"
        "*What to do:* buy credits in Apollo (at least 1,000 credits to get back above the reserve). Or, to spend into "
        f"the reserve, lower `apollo_floor` (now 5,000) on the {GENERAL}.")
    tomorrow, t2 = world(balance=4_000, now=TUE_9 + timedelta(days=1))
    tomorrow.store = ctx.store
    tomorrow.store.guard = tomorrow.guard
    spend.check(tomorrow)
    assert len(posts(t2)) == 1  # once a day: the next day asks again


def test_plenty_of_balance_or_no_spend_asks_nothing():
    ctx, t = world(balance=30_000)
    spent(ctx, "apollo", 300)
    assert asked(ctx) == []
    ctx, t = world(balance=5_500)  # near the floor, but nothing spent lately: no pace to project
    assert asked(ctx) == [] and spend.check(ctx)[1] == {"posted": False, "keys": []} and posts(t) == []


def test_an_unread_balance_falls_back_on_a_source_s_skip_for_the_floor():
    """The sources skip silently below the floor (a skip is healthy for heartbeat_check): the daily check says it."""
    ctx, t = world(balance=None)
    t.route("POST", "/usage_stats/credit_usage_stats", body={"error": "busy"}, status=503)
    ctx.store.upsert("heartbeats", [{"run_id": "r1", "job": "source_universe", "status": "skipped",
                                     "started_at": ctx.now - timedelta(hours=6), "finished_at": ctx.now,
                                     "detail": {"skipped": True,
                                                "reason": "Apollo has 4,900 credits left, below apollo_floor (5,000)"}}])
    sp = spend.read(ctx)
    assert sp.apollo_left is None and sp.apollo_error == "Apollo could not be read (ApiError)"
    assert asked(ctx) == [f"{UNDER_FLOOR}\nApollo has 4,900 credits left, below apollo_floor (5,000) (the last job to "
                          "check said so; Apollo's balance could not be read today).\n*What to do:* buy credits in "
                          f"Apollo. Or, to spend into the reserve, lower `apollo_floor` (now 5,000) on the {GENERAL}."]


# -- the monthly budgets -------------------------------------------------------------------------------------------


def test_the_monthly_budgets_at_80_and_100_percent_once_a_month_each():
    ctx, t = world()
    spent(ctx, "apollo", 1_650, days_ago=2)
    spend.check(ctx)
    assert posts(t)[-1].endswith(
        f":warning: *Apollo's monthly budget is 82% used; at 100% we stop {STOPS_APOLLO} until 1 Nov.*\n"
        "1,650 of the 2,000 credits we allow a month are spent.\n"
        "Nothing to do yet: at this month's pace it lasts until the month ends.")
    spend.check(ctx)
    assert len(posts(t)) == 1
    spent(ctx, "apollo", 400)
    spent(ctx, "clay", 1_900)  # an old Clay row: Clay's budget went with W2-1, so it asks nothing
    spend.check(ctx)
    text = posts(t)[-1]
    assert text.startswith("<@U_HARRY> :rotating_light: *Credits and settings: 1 thing needs a decision*\n")
    assert (f":rotating_light: *Apollo's monthly budget is used up, so we have stopped {STOPS_APOLLO} until 1 Nov.*\n"
            "2,050 of the 2,000 credits we allow a month are spent.\n"
            "*What to do:* to carry on this month, raise `apollo_monthly_credits` (now 2,000) to about 2,374, what this "
            f"month needs at its pace so far, on the {GENERAL}; it applies at the next settings sync, or now with "
            "`railway ssh -- us-outbound sync`. Otherwise it starts again on 1 Nov.") in text
    assert "Clay" not in text
    november, t2 = world(now=datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    november.store = ctx.store
    november.store.guard = november.guard
    assert asked(november) == []  # a new month, nothing spent in it


def test_a_month_that_jumps_past_80_gets_only_the_100_line():
    ctx, _ = world()
    spent(ctx, "apollo", 2_000)
    lines = asked(ctx)
    assert len(lines) == 1 and lines[0].startswith(":rotating_light: *Apollo's monthly budget is used up")


# -- Claude ----------------------------------------------------------------------------------------------------------


CLAUDE = "Claude (the AI that reads replies, drafts answers and checks labels)"
CLAUDE_STOPS = ('replies get no draft and are filed as "other", the copy check stops, and new companies wait unchecked '
                "for their industry label")


@pytest.mark.parametrize("usd, line", [
    (20.0, None),
    (26.0, f":warning: *{CLAUDE} has used 52% of its monthly cap.* At the cap, until 1 Nov, {CLAUDE_STOPS}.\n"
           "$26.00 of the $50 monthly cap is spent; at this pace the month needs about $30, inside the cap.\n"
           "Nothing to do yet."),
    (41.0, f":warning: *{CLAUDE} has used 82% of its monthly cap.* At the cap, until 1 Nov, {CLAUDE_STOPS}.\n"
           "$41.00 of the $50 monthly cap is spent; at this pace the month needs about $47, inside the cap.\n"
           "Nothing to do yet."),
    (49.6, f":rotating_light: *{CLAUDE} has reached its monthly cap: until 1 Nov (UTC), {CLAUDE_STOPS}.*\n"
           "$49.60 of the $50 monthly cap is spent; this month needs about $57 at its pace so far.\n"
           "*What to do:* raise `claude_monthly_cap_usd` (now $50, at most $100) on the "
           f"{GENERAL}, and the spend limit in the Anthropic Console to match."),
])
def test_claude_s_spend_against_its_cap(usd, line):
    ctx, _ = world(claude_monthly_cap_usd=50.0)
    spent(ctx, "claude", 0, usd=usd)
    spent(ctx, "claude", 0, usd=30.0, days_ago=40)  # September's spend counts nowhere now
    assert asked(ctx) == ([line] if line else [])


def test_each_claude_step_is_asked_once_in_its_month():
    ctx, t = world(claude_monthly_cap_usd=50.0)
    spent(ctx, "claude", 0, usd=26.0)
    spend.check(ctx)
    spend.check(ctx)
    spent(ctx, "claude", 0, usd=15.0)
    spend.check(ctx)
    keys = sorted(e["event_id"] for e in ctx.store.select("events", {"type": "alert"}))
    assert keys == ["alert:claude_cap_50:2026-10", "alert:claude_cap_80:2026-10"] and len(posts(t)) == 2


# -- Apollo's website-visitor credits --------------------------------------------------------------------------------


def test_the_website_visitor_credits_under_15_percent_while_site_visits_are_on(monkeypatch):
    ctx, _ = world(visitors=150)
    assert asked(ctx) == [":warning: *Apollo's website-visitor credits are running low; when they run out, we stop "
                          "learning which companies visit the US site.*\n150 of 1,200 left (12%).\n"
                          "*What to do:* buy more website-visitor credits in Apollo (1,050 used so far in this "
                          "allowance)."]
    ctx, _ = world(visitors=300)
    assert asked(ctx) == []
    monkeypatch.setattr(spend, "SITE_VISIT_DOMAIN", "")  # site visits off: not asked about
    ctx, _ = world(visitors=10)
    assert asked(ctx) == [] and spend.read(ctx).visitors is None


# -- the modes, and the daily post ----------------------------------------------------------------------------------


def test_a_dry_run_preview_never_stops_the_live_ask():
    ctx, t = world(balance=4_000, live=False)
    spend.check(ctx)
    assert posts(t)[0].startswith(DRY + "<@U_HARRY> :rotating_light: *Credits and settings")
    ctx.guard.configure(live=True)
    spend.check(ctx)
    assert len(posts(t)) == 2 and {e["event_id"] for e in ctx.store.select("events", {"type": "alert"})} == {
        "alert:dry-run:apollo_floor:2026-10-27", "alert:apollo_floor:2026-10-27"}


def test_the_daily_post_asks_first_and_carries_the_spend_line():
    ctx, t = post_world()
    t.route("POST", "/usage_stats/credit_usage_stats", body=usage(4_000, 1_150))
    spent(ctx, "claude", 0, usd=2.5)
    out = daily_post.run(ctx)
    ask, post = posts(t)
    assert ask.startswith("<@U_HARRY> :rotating_light: *Credits and settings") and UNDER_FLOOR in ask
    assert post.startswith(":warning: *Daily summary")
    assert ("\nSpend: Apollo 4,000 credits left, below the 5,000 reserve: finding companies and emails has stopped · "
            "Claude $2.50 of $10 this month (25%) · website-visitor credits 1,150 of 1,200 left\n") in post
    assert out["spend_alert"]["keys"] == ["apollo_floor:2026-10-27"] and out["spend"]["apollo_left"] == 4_000
    assert "mailboxes_ahead" not in out  # a Monday ask, made by the Monday readout


def test_the_monday_readout_makes_monday_s_asks_first():
    """There is no Monday daily post from 19 Oct 2026 (it runs Tuesday to Saturday), so the Monday readout makes
    Monday's asks before its own post: the credit and spend asks, keyed by Monday's date, and the mailboxes ahead."""
    from us_outbound.learn import readout

    ctx, t = post_world(now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))  # Mon 26 Oct 07:30 UK (GMT)
    t.route("POST", "/usage_stats/credit_usage_stats", body=usage(4_000, 1_150))
    out = readout.run(ctx)
    ask, post = posts(t)
    assert ask.startswith("<@U_HARRY> :rotating_light: *Credits and settings") and UNDER_FLOOR in ask
    assert post.startswith(":warning: *Monday readout, 19–25 Oct")
    assert out["spend_alert"]["keys"] == ["apollo_floor:2026-10-26"]
    assert out["mailboxes_ahead"]["checked"] is True and out["mailboxes_ahead"]["add"] == 0
    readout.run(ctx)  # a rerun asks nothing twice (base/notify.post_once)
    assert len(posts(t)) == 3 and posts(t)[2].startswith(":warning: *Monday readout")


def test_the_spend_line_with_the_balance_unknown_or_far_from_the_floor():
    ctx, t = world(balance=30_000)
    spent(ctx, "apollo", 140, days_ago=3)
    assert spend.summary_line(spend.read(ctx)) == ("Spend: Apollo 30,000 credits left (about 2,500 days to the 5,000 "
                                                   "reserve at 10 a day) · Claude $0.00 of $10 this month (0%)")
    t.route("POST", "/usage_stats/credit_usage_stats", body={"credit_usage_stats": {}})
    assert spend.summary_line(spend.read(ctx)).startswith(
        "Spend: Apollo balance unknown (Apollo's answer had no lead-credit balance) · Claude")
