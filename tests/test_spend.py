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

TUE_9 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)  # 09:00 UK (GMT)
FLOOR_TEXT = ("Apollo has 6,400 credits; at this pace it reaches apollo_floor (5,000) in about 14 days, after which "
              "sourcing and email reveals stop. Buy credits, or lower apollo_monthly_credits on the General tab.")


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
    assert text == f"<@U_HARRY> Credits and spend: a purchase or a setting needs you.\n{FLOOR_TEXT}"
    assert sent["keys"] == ["apollo_floor:2026-10-27"]


def test_the_floor_alert_is_once_a_day_and_survives_a_restart():
    ctx, t = world(balance=4_000)
    spend.check(ctx)
    again, _ = world(balance=4_000)
    again.store = ctx.store  # a restart: a new process over the same database
    again.store.guard = again.guard
    spend.check(again)
    assert len(posts(t)) == 1 and posts(again.clients.transport) == []
    assert posts(t)[0].endswith("Apollo has 4,000 credits, under apollo_floor (5,000), so sourcing and email reveals "
                                "have stopped. Buy credits in Apollo (or, to spend into that reserve, lower apollo_floor "
                                "on the General tab).")
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
    assert asked(ctx) == ["Apollo has 4,900 credits left, below apollo_floor (5,000), so sourcing and email reveals have "
                          "stopped. Buy credits in Apollo (or, to spend into that reserve, lower apollo_floor on the "
                          "General tab)."]


# -- the monthly budgets -------------------------------------------------------------------------------------------


def test_the_monthly_budgets_at_80_and_100_percent_once_a_month_each():
    ctx, t = world()
    spent(ctx, "apollo", 1_650, days_ago=2)
    spend.check(ctx)
    assert posts(t)[-1].endswith("Apollo: 1,650 of 2,000 credits used this month (82%). At 100%, sourcing and email "
                                 "reveals stop until 1 Nov. To allow more, raise apollo_monthly_credits on the General tab.")
    spend.check(ctx)
    assert len(posts(t)) == 1
    spent(ctx, "apollo", 400)
    spent(ctx, "clay", 1_900)
    spend.check(ctx)
    text = posts(t)[-1]
    assert ("Apollo: this month's 2,000 credits are used (2,050 spent), so sourcing and email reveals have stopped until "
            "1 Nov. To allow more, raise apollo_monthly_credits on the General tab.") in text
    assert ("Clay: 1,900 of 2,000 credits used this month (95%). At 100%, Clay's verification and email lookups stop "
            "until 1 Nov. To allow more, raise clay_monthly_credits on the General tab.") in text
    november, t2 = world(now=datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    november.store = ctx.store
    november.store.guard = november.guard
    assert asked(november) == []  # a new month, nothing spent in it


def test_a_month_that_jumps_past_80_gets_only_the_100_line():
    ctx, _ = world()
    spent(ctx, "apollo", 2_000)
    lines = asked(ctx)
    assert len(lines) == 1 and lines[0].startswith("Apollo: this month's 2,000 credits are used")


# -- Claude ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("usd, line", [
    (20.0, None),
    (26.0, 'Claude: $26.00 of the $50 monthly cap spent this month (52%). At the cap, replies are classified "other" '
           "with no draft, copy QA stops, and new companies wait unverified for the industry label check."),
    (41.0, 'Claude: $41.00 of the $50 monthly cap spent this month (82%). At the cap, replies are classified "other" '
           "with no draft, copy QA stops, and new companies wait unverified for the industry label check."),
    (49.6, 'Claude\'s monthly cap is used up: $49.60 of $50 this month, so replies are classified "other" with no draft, '
           "copy QA stops, and new companies wait unverified for the industry label check until 1 Nov (UTC). To go on, "
           "raise claude_monthly_cap_usd on the General tab (up to $100) and the Anthropic Console spend limit."),
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


def test_the_website_visitor_credits_under_15_percent_while_site_visits_are_on():
    ctx, _ = world(visitors=150)
    assert asked(ctx) == ["Apollo's website-visitor credits are running low: 150 of 1,200 left (12%). Once they run "
                          "out, Apollo stops naming the companies that visit the US site, so the site-visit signals "
                          "stop. Buy more in Apollo."]
    ctx, _ = world(visitors=300)
    assert asked(ctx) == []
    ctx, _ = world(visitors=10, site_visit_domain="")  # site visits off: not asked about
    assert asked(ctx) == [] and spend.read(ctx).visitors is None


# -- the modes, and the daily post ----------------------------------------------------------------------------------


def test_a_dry_run_preview_never_stops_the_live_ask():
    ctx, t = world(balance=4_000, live=False)
    spend.check(ctx)
    assert posts(t)[0].startswith("[dry-run → #us-outbound] <@U_HARRY> Credits and spend")
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
    assert ask.startswith("<@U_HARRY> Credits and spend") and "under apollo_floor (5,000)" in ask
    assert post.startswith("*Daily post")
    assert ("\nSpend: Apollo balance 4,000 credits (apollo_floor 5,000, under it: sourcing and email reveals have "
            "stopped) · Claude $2.50 of $10 this month (25%) · website-visitor credits 1,150 of 1,200 left.\n") in post
    assert out["spend_alert"]["keys"] == ["apollo_floor:2026-10-27"] and out["spend"]["apollo_left"] == 4_000
    assert out["mailboxes_ahead"] == {"checked": False}  # a Tuesday


def test_the_spend_line_with_the_balance_unknown_or_far_from_the_floor():
    ctx, t = world(balance=30_000)
    spent(ctx, "apollo", 140, days_ago=3)
    assert spend.summary_line(spend.read(ctx)) == ("Spend: Apollo balance 30,000 credits (apollo_floor 5,000, about 2,500 "
                                                   "days away at 10 a day) · Claude $0.00 of $10 this month (0%)")
    t.route("POST", "/usage_stats/credit_usage_stats", body={"credit_usage_stats": {}})
    assert spend.summary_line(spend.read(ctx)).startswith(
        "Spend: Apollo balance unknown: Apollo's answer had no lead-credit balance · Claude")
