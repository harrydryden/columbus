"""Instantly's plan limit (enrol/plan.py; Harry, 7 Oct 2026): a lead left out because the plan is full is not refused,
so its contact is kept, the run adds no more, and the approvers are asked to make room."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.fakes import FakeTransport
from tests.test_enrol import added, instantly_posts, make
from tests.test_registry import slack_routes
from tests.test_render import make_settings
from us_outbound.clients import instantly
from us_outbound.clients.http import ApiError
from us_outbound.enrol import enrol, plan

HARRY_ID = "U_HARRY"  # approver_slack_ids: the alert mentions the approvers

FULL_ALERT = ("<@U_HARRY> :rotating_light: *Instantly's plan is full: nobody new is being emailed*\n"
              "Instantly's plan holds only so many people (leads) at once, and it has no room left.\n"
              "People already in the campaign still get their follow-ups. The new ones are kept, not dropped, and wait "
              "for room.\n"
              "*What to do:* Make room: upgrade the plan in Instantly, or delete leads there that have finished all "
              "their emails. Nothing else is needed: adding people to the email campaign tries again by itself.")


# -- what Instantly's answer says -------------------------------------------------------------------------------


@pytest.mark.parametrize("answer, full", [
    ({"status": "success", "total_sent": 3, "leads_uploaded": 3, "remaining_in_plan": 4_000}, False),
    ({"status": "success", "total_sent": 3, "leads_uploaded": 0, "remaining_in_plan": 0}, True),
    ({"status": "success", "total_sent": 3, "leads_uploaded": 3, "remaining_in_plan": 0}, True),  # full from now on
    ({"status": "Lead upload limit reached for your plan", "total_sent": 3, "leads_uploaded": 1}, True),
    ({"status": "success", "total_sent": 3, "leads_uploaded": 1, "in_blocklist": 2}, False),  # refused, not full
    ({"status": "limit reached", "total_sent": 3, "leads_uploaded": 3}, False),  # nothing left out
    ({}, False),
])
def test_plan_full_reads_remaining_in_plan_and_the_status(answer, full):
    assert instantly.plan_full(answer) is full


def test_a_whole_add_refused_for_the_plan():
    assert instantly.plan_full_error(ApiError("instantly", 402, {"message": "payment required"}))
    assert instantly.plan_full_error(ApiError("instantly", 400, {"message": "You have reached your plan's lead limit"}))
    assert not instantly.plan_full_error(ApiError("instantly", 400, {"message": "custom variable exceeds limit"}))
    assert not instantly.plan_full_error(ApiError("instantly", 500, {"message": "plan limit"}))


# -- enrol --------------------------------------------------------------------------------------------------------


def enrol_world(answer):
    t = slack_routes(FakeTransport())
    ctx, _ = make(live=True, transport=t, settings=make_settings(live_sending=True, approver_slack_ids=(HARRY_ID,)))
    t.route("POST", "/leads/add", fn=answer)
    t.route("POST", "/leads/list", {"items": []})  # not in the campaign either
    return ctx, t


def posts(t) -> list[str]:
    return [r.json["text"] for r in t.requests if r.url.endswith("chat.postMessage")]


def adds(t) -> list:
    return [r for r in instantly_posts(t) if r.url.endswith("/leads/add")]


def test_a_full_plan_keeps_every_contact_adds_no_more_and_asks_for_room_once_a_day():
    def full(req):
        return {"status": "success", "total_sent": len(req.json["leads"]), "leads_uploaded": 0, "remaining_in_plan": 0,
                "created_leads": []}

    ctx, t = enrol_world(full)
    out = enrol.run(ctx)
    assert out["enrolled"] == 0 and out["skipped"] == {"Instantly plan limit": 3}
    assert len(adds(t)) == 1  # the first owner's add said so; the other owners' leads are not tried
    assert not any(c.get("suppressed") for c in ctx.store.select("contacts"))  # kept, never mark_not_added
    assert {a["status"] for a in ctx.store.select("accounts")} == {"verified"}  # tried again next run
    assert {x["detail"][0] for x in out["skipped_accounts"]} == {plan.KEPT}
    assert out["instantly_plan"]["full"] is True and out["instantly_plan"]["remaining_in_plan"] == 0
    assert posts(t) == [FULL_ALERT]
    enrol.run(ctx)  # a rerun that day
    assert posts(t) == [FULL_ALERT]


def test_a_status_naming_the_limit_counts_as_a_full_plan():
    def limited(req):  # no remaining_in_plan: the lead left out, and a status that names the limit
        return {"status": "upload limit reached", "total_sent": len(req.json["leads"]), "leads_uploaded": 0,
                "created_leads": []}

    ctx, t = enrol_world(limited)
    out = enrol.run(ctx)
    assert out["enrolled"] == 0 and out["skipped"] == {"Instantly plan limit": 3} and len(adds(t)) == 1
    assert not any(c.get("suppressed") for c in ctx.store.select("contacts"))


def test_an_add_refused_for_the_plan_keeps_the_contacts_too():
    ctx, t = enrol_world(None)
    t.route("POST", "/leads/add", {"message": "Payment required: upgrade your plan"}, status=402)
    out = enrol.run(ctx)
    assert out["skipped"] == {"Instantly plan limit": 3} and out["errors"] == [] and len(adds(t)) == 1
    assert posts(t) == [FULL_ALERT]


def test_little_room_left_warns_once_a_week():
    def low(req):
        return {**added(req), "remaining_in_plan": 200}  # under two weeks of weekly_enrol_cap (150)

    ctx, t = enrol_world(low)
    out = enrol.run(ctx)
    assert out["enrolled"] == 3 and out["instantly_plan"]["full"] is False
    [low] = posts(t)
    head, facts, do = low.splitlines()
    assert head == "<@U_HARRY> :warning: *Instantly's plan is nearly full: room for 200 more people*"
    assert facts.startswith("That is under 2 weeks at the current pace: `weekly_enrol_cap` (people added a week) is "
                            "150 on the ") and "General tab" in facts
    assert do == ("*What to do:* Before it fills, upgrade the plan in Instantly, or delete leads there that have "
                  "finished all their emails. Once it is full, nobody new is emailed until there is room.")
    ctx.now += timedelta(days=1)
    enrol.plan.after_add(ctx, {"remaining_in_plan": 150})
    assert len(posts(t)) == 1  # the same week
    ctx.now += timedelta(days=7)
    enrol.plan.after_add(ctx, {"remaining_in_plan": 150})
    assert len(posts(t)) == 2
