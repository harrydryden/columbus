"""Mailbox capacity three weeks ahead (learn/capacity_ahead.py; Harry, 7 Oct 2026): on Mondays, ask for mailboxes
while a new one still has the three weeks it needs to warm up."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from tests.test_daily_post import world as post_world
from tests.test_registry import SETTINGS, mailbox, slack_routes
from us_outbound.learn import capacity_ahead, daily_post

MON_9 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # Monday 09:00 UK (GMT), ISO week 2026-W44
HEAD = ":warning: *Sending mailboxes: add more now, as a new one takes 3 weeks to warm up*"
ASK = ("*Add 3 mailboxes now.* Fully warmed up, your 4 sending mailboxes take about 155 new companies a week; the weekly "
       "target (`weekly_enrol_cap`) is 300, and 600 companies are ready (3.9 weeks' worth).")


def world(now=MON_9, *, verified=140, mailboxes=None, **general):
    """SETTINGS' four Active mailboxes at 30 a day (Hannah, Sam, and Harry's two), on the ramp: no send yet."""
    s = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, **general),
                            mailboxes=mailboxes if mailboxes is not None else SETTINGS.mailboxes)
    ctx = make_context(s, job="daily_post", now=now, transport=slack_routes(FakeTransport()), live=True)
    if verified:  # verify_accounts' runs over the last fortnight: the recent supply
        ctx.store.upsert("heartbeats", [{"run_id": f"v{i}", "job": "verify_accounts", "status": "ok",
                                         "started_at": now - timedelta(days=2 + 7 * i), "dry_run": False,
                                         "detail": {"verified": verified // 2}} for i in range(2)])
    return ctx


def posts(ctx) -> list[str]:
    return [r.json["text"] for r in ctx.clients.transport.requests if r.url.endswith("chat.postMessage")]


def test_the_arithmetic_is_the_ramp_s_and_the_capacity_code_s():
    a = capacity_ahead.plan(world(weekly_enrol_cap=300), ready=600)
    # Full ramp is each mailbox's sheet cap (the ramp holds them at 10 a day today); each sender's steady pace is its
    # cap ÷ 4 rounded up (Hannah 8, Sam 8, Harry's two 15), over five send days.
    assert a.weekly == (8 + 8 + 15) * 5 == 155 and a.per_mailbox == 8 * 5
    # Needed: weekly_enrol_cap, as far as 600 ready over three weeks plus 70 verified a week can fill it.
    assert a.supply == 70 and a.need == 270 and a.add == 3  # (270 - 155) / 40, rounded up


def test_on_monday_it_asks_for_mailboxes_with_the_approvers_mentioned_once_that_week():
    ctx = world(weekly_enrol_cap=300)
    out = capacity_ahead.check(ctx, ready=600)
    [text] = posts(ctx)
    assert text == (
        f"<@U_HARRY> {HEAD}\n{ASK}\n*What to do:*\n1. Set up each new mailbox and connect it in Instantly.\n"
        '2. Add it here: `railway ssh -- us-outbound mailbox add ADDRESS --owner "NAME" --live` (NAME: the person '
        "named on the address). It warms up for 21 days, then its sending builds up week by week.\n"
        "_Without them, sending stays at about 155 new companies a week._")
    assert out["add"] == 3 and out["alert"]["keys"] == ["mailboxes:2026-W44"]
    capacity_ahead.check(ctx, ready=600)  # a rerun that Monday
    assert len(posts(ctx)) == 1


def test_mailboxes_already_warming_count_towards_the_ask():
    boxes = (*SETTINGS.mailboxes, mailbox("new@meetspill.org", "Sam Jackson", status="Warming"))
    ctx = world(weekly_enrol_cap=300, mailboxes=boxes)
    capacity_ahead.check(ctx, ready=600)
    [text] = posts(ctx)
    assert "*Add 2 mailboxes now.*" in text and "1 mailbox is already warming up, counted in." in text


def test_nothing_is_posted_when_no_mailbox_is_needed():
    ctx = world()  # weekly_enrol_cap 150: the four mailboxes take 155 a week at full ramp
    assert capacity_ahead.check(ctx, ready=600)["add"] == 0 and posts(ctx) == []
    ctx = world(weekly_enrol_cap=300, verified=0)  # too few companies to fill more mailboxes
    assert capacity_ahead.check(ctx, ready=60)["add"] == 0 and posts(ctx) == []
    ctx = world(MON_9 + timedelta(days=1), weekly_enrol_cap=300)  # Tuesday: Mondays only
    assert capacity_ahead.check(ctx, ready=600) == {"checked": False} and posts(ctx) == []


def test_with_no_active_mailbox_it_says_so():
    ctx = world(weekly_enrol_cap=150, mailboxes=())
    capacity_ahead.check(ctx, ready=300)
    [text] = posts(ctx)
    assert ("*Add 4 mailboxes now.* Fully warmed up, your 0 sending mailboxes take about 0 new companies a week; the "
            "weekly target (`weekly_enrol_cap`) is 150, and 300 companies are ready.\n") in text


def test_the_monday_daily_post_runs_the_check():
    ctx, _ = post_world(now=MON_9)
    out = daily_post.run(ctx)
    assert out["mailboxes_ahead"]["checked"] is True and out["mailboxes_ahead"]["add"] == 0
