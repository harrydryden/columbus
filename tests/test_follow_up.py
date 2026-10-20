"""follow_up (replies/follow_up.py; W2-4, 20 Oct 2026; code-replies-crm M5): a not-now reply's date kept. Each reply
item whose follow_up_date has come gets one Slack line and one HubSpot task for Harry, once; tomorrow's waits."""

from __future__ import annotations

from datetime import timedelta

from tests.test_hubspot_writes import NOW, reply_row, world
from tests.test_registry import slack_routes
from us_outbound import suppression
from us_outbound.ops import schedule
from us_outbound.ops import health as hb
from us_outbound.replies import follow_up

ITEM = "item-0001-aaaa"


def not_now(when, item_id=ITEM, **payload):
    row = reply_row(reply_class="not_now", follow_up_date=when.isoformat(), follow_up_source="reply", **payload)
    return {**row, "item_id": item_id, "status": "handled"}  # answered long ago; the date is what is left


def setup(settings, when_days=0, *, live=True):
    ctx, t, crm = world(settings, live=live, row=not_now(NOW.date() + timedelta(days=when_days)))
    slack_routes(t)
    ctx.job = follow_up.JOB
    return ctx, t, crm


def posts(t):
    return [r.json for r in t.requests if r.url.endswith("chat.postMessage")]


def test_a_not_now_reply_dated_today_gets_one_slack_line_and_one_hubspot_task_once(default_settings):
    """Mon 5 Oct 2026, 16:00 UK, an item dated today: one post (the line names Jane Doe at Acme Creative and the
    mailbox she wrote to), one task for Harry's owner id due today, not linked to any record (a not-now reply is not a
    warm lead: SPEC 1.2), and the follow_up row; a second run posts and tasks nothing."""
    ctx, t, crm = setup(default_settings)
    out = follow_up.sweep(ctx)
    [post] = posts(t)
    assert post["channel"] == "C_ALERT" and "Jane Doe at Acme Creative" in post["text"]
    assert "write to them again from hannah@meetspill.org" in post["text"]
    [task] = crm.objects["tasks"].values()
    assert task["hubspot_owner_id"] == "owner-harry" and "not now" in task["hs_task_subject"]
    assert crm.linked("tasks", next(iter(crm.objects["tasks"])), "contacts") == []
    assert out["followed_up"] == [{"item": ITEM[:8], "slack": True, "task": True}] and out["errors"] == []
    assert ctx.store.get("events", event_id=f"follow_up:{ITEM}")["type"] == "follow_up"
    again = follow_up.sweep(ctx)
    assert again["due"] == 0 and len(posts(t)) == 1 and len(crm.objects["tasks"]) == 1


def test_an_item_dated_tomorrow_waits(default_settings):
    ctx, t, crm = setup(default_settings, when_days=1)
    out = follow_up.sweep(ctx)
    assert out["due"] == 0 and posts(t) == [] and crm.objects["tasks"] == {}
    assert ctx.store.select("events", {"type": "follow_up"}) == []
    ctx.now = NOW + timedelta(days=1)  # the next day: due
    assert follow_up.sweep(ctx)["followed_up"][0]["task"] is True


def test_someone_who_asked_to_stop_since_is_passed_by_and_dry_run_writes_nothing(default_settings):
    ctx, t, crm = setup(default_settings)
    contact = ctx.store.get("contacts", contact_id="con-1")
    suppression.add(ctx.store, email=contact["email"], reason="unsubscribe", source="reply", now=NOW)
    out = follow_up.sweep(ctx)
    assert out["suppressed"] == [ITEM[:8]] and posts(t) == [] and crm.objects["tasks"] == {}
    assert ctx.store.get("events", event_id=f"follow_up:{ITEM}")["approval"] == "suppressed"
    dry, t2, crm2 = setup(default_settings, live=False)
    out = follow_up.sweep(dry)
    assert out["would_follow_up"] == [ITEM[:8]] and out["followed_up"] == []
    assert [p for p in posts(t2) if p["channel"] != "C_DEV"] == [] and crm2.objects["tasks"] == {}
    assert dry.store.select("events", {"type": "follow_up"}) == []


def test_the_job_is_registered_daily_and_expected():
    job = schedule.by_name()["follow_up"]
    assert (job.cron, job.live, job.enabled) == ("45 8 * * *", True, True)
    assert hb.EXPECTED["follow_up"] == 26 * 60 and "follow_up" in hb.IMPACT
