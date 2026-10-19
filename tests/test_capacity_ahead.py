"""Mailbox capacity three weeks ahead (learn/capacity_ahead.py; Harry, 7 Oct 2026): on Mondays, ask for mailboxes
while a new one still has the three weeks it needs to warm up."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_daily_post import world as post_world
from tests.test_registry import SETTINGS, mailbox, slack_routes
from us_outbound.learn import capacity_ahead, daily_post

MON_9 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # Monday 09:00 UK (GMT), ISO week 2026-W44
HEAD = ":warning: *Sending mailboxes: add more now, as a new one takes 3 weeks to warm up*"
ASK = ("*Add 2 mailboxes now.* Fully warmed up, your 4 sending mailboxes take about 155 new companies a week; the weekly "
       "target (`weekly_enrol_cap`) is 300, and 600 companies are ready (3.9 weeks' worth).")


def world(now=MON_9, *, verified=140, picked=(20, 40), mailboxes=None, **general):
    """SETTINGS' four Active mailboxes at 30 a day (Hannah, Sam, and Harry's two), on the ramp: no send yet.

    picked=(k, n): pick_contacts' runs in the last 28 days found a contact at k of the n accounts they tried, the
    contact yield p_c the recent supply is discounted by (W1-3a); (20, 40) is 0.5, measured (20 or more tried)."""
    s = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, **general),
                            mailboxes=mailboxes if mailboxes is not None else SETTINGS.mailboxes)
    ctx = make_context(s, job="daily_post", now=now, transport=slack_routes(FakeTransport()), live=True)
    if verified:  # verify_accounts' runs over the last fortnight: the recent supply
        ctx.store.upsert("heartbeats", [{"run_id": f"v{i}", "job": "verify_accounts", "status": "ok",
                                         "started_at": now - timedelta(days=2 + 7 * i), "dry_run": False,
                                         "detail": {"verified": verified // 2}} for i in range(2)])
    if picked[1]:
        ctx.store.upsert("heartbeats", [{"run_id": "p0", "job": "pick_contacts", "status": "ok", "dry_run": False,
                                         "started_at": now - timedelta(days=3),
                                         "detail": {"picked": picked[0], "tried": picked[1]}}])
    return ctx


def posts(ctx) -> list[str]:
    return [r.json["text"] for r in ctx.clients.transport.requests if r.url.endswith("chat.postMessage")]


def test_the_arithmetic_is_the_ramp_s_and_the_capacity_code_s():
    a = capacity_ahead.plan(world(weekly_enrol_cap=300), ready=600)
    # Full ramp is each mailbox's sheet cap (the ramp holds them at 10 a day today); each sender's steady pace is its
    # cap ÷ 4 rounded up (Hannah 8, Sam 8, Harry's two 15), over five send days.
    assert a.weekly == (8 + 8 + 15) * 5 == 155 and a.per_mailbox == 8 * 5
    # Needed: weekly_enrol_cap, as far as 600 ready over three weeks plus the 70 verified a week pick_contacts will
    # find a contact at (p_c 0.5: 35) can fill it: min(300, 200 + 35) = 235; (235 - 155) / 40 = 2 mailboxes.
    assert (a.supply, a.p_c, a.p_c_basis, a.need, a.add) == (35, 0.5, "measured", 235, 2)


def test_the_recent_supply_counts_only_the_verified_accounts_a_contact_is_found_at():
    """code-learn-reporting B11 / metrics-audit M8, 19 Oct 2026: 60 ready and 128 verified a week read as a need of
    60 / 3 + 128 = 148 a week while about 35 were enrolled: pick found a contact at 21 of the 46 it tried. Now the
    128 count at that yield: 60 / 3 + 128 × 21 / 46 = 20 + 58.43 = 78.43 a week."""
    a = capacity_ahead.plan(world(verified=256, picked=(21, 46)), ready=60)
    assert a.supply == pytest.approx(128 * 21 / 46) and a.need == pytest.approx(20 + 128 * 21 / 46)
    assert a.p_c_basis == "measured" and a.as_dict()["p_c"] == 0.457
    # With fewer than 20 tried the yield is loop L1's default, 0.46, and says so.
    b = capacity_ahead.plan(world(verified=256, picked=(5, 10)), ready=60)
    assert (b.p_c, b.p_c_basis) == (0.46, "default") and b.supply == pytest.approx(128 * 0.46)


def test_on_monday_it_asks_for_mailboxes_with_the_approvers_mentioned_once_that_week():
    ctx = world(weekly_enrol_cap=300)
    out = capacity_ahead.check(ctx, ready=600)
    [text] = posts(ctx)
    assert text == (
        f"<@U_HARRY> {HEAD}\n{ASK}\n*What to do:*\n1. Set up each new mailbox and connect it in Instantly.\n"
        '2. Add it here: `railway ssh -- us-outbound mailbox add ADDRESS --owner "NAME" --live` (NAME: the person '
        "named on the address). It warms up for 21 days, then its sending builds up week by week.\n"
        "_Without them, sending stays at about 155 new companies a week._")
    assert out["add"] == 2 and out["alert"]["keys"] == ["mailboxes:2026-W44"]
    capacity_ahead.check(ctx, ready=600)  # a rerun that Monday
    assert len(posts(ctx)) == 1


def test_mailboxes_already_warming_count_towards_the_ask():
    boxes = (*SETTINGS.mailboxes, mailbox("new@meetspill.org", "Sam Jackson", status="Warming"))
    ctx = world(weekly_enrol_cap=300, mailboxes=boxes)
    capacity_ahead.check(ctx, ready=600)
    [text] = posts(ctx)
    assert "*Add 1 mailbox now.*" in text and "1 mailbox is already warming up, counted in." in text


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


def test_the_monday_readout_runs_the_check_and_the_daily_post_does_not():
    """From 19 Oct 2026 the daily post runs Tuesday to Saturday, so the Monday readout (08:30) makes the ask."""
    from us_outbound.learn import readout

    ctx, _ = post_world(now=MON_9)
    out = readout.run(ctx)
    assert out["mailboxes_ahead"]["checked"] is True and out["mailboxes_ahead"]["add"] == 0
    assert out["mailboxes_ahead"]["ready"] == 0  # enrol/today.py's count, as the daily post took it
    assert "mailboxes_ahead" not in daily_post.run(ctx)  # by hand on a Monday: the readout's ask, not the post's
