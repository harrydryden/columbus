"""Instantly's plan limit (enrol/plan.py; Harry, 7 Oct 2026): a lead left out because the plan is full is not refused,
so its contact is kept, the run adds no more, a send approval is held, and the approvers are asked to make room."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.fakes import FakeTransport
from tests.test_enrol import added, instantly_posts, make
from tests.test_registry import slack_routes
from tests.test_render import make_settings
from tests.test_send_approvals import HARRY_ID, item_for, poll, proposed
from us_outbound.clients import instantly
from us_outbound.clients.http import ApiError
from us_outbound.enrol import enrol, plan

FULL_ALERT = ("<@U_HARRY> Instantly's plan:\nInstantly's plan has no room for new leads, so nothing new is being sent. "
              "Upgrade the Instantly plan, or delete leads that finished their sequence. Contacts are kept and will be "
              "added when there is room.")


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
    assert posts(t) == [
        "<@U_HARRY> Instantly's plan:\nInstantly's plan has room for 200 more leads, under 2 weeks at weekly_enrol_cap "
        "(150 a week). Upgrade the Instantly plan, or delete leads that finished their sequence, before it fills: then "
        "nothing new is sent."]
    ctx.now += timedelta(days=1)
    enrol.plan.after_add(ctx, {"remaining_in_plan": 150})
    assert len(posts(t)) == 1  # the same week
    ctx.now += timedelta(days=7)
    enrol.plan.after_add(ctx, {"remaining_in_plan": 150})
    assert len(posts(t)) == 2


# -- a send approval's ✅ ---------------------------------------------------------------------------------------------


def full_then_room(t, state):
    def answer(req):
        if state["full"]:
            return {"status": "success", "total_sent": 1, "leads_uploaded": 0, "remaining_in_plan": 0,
                    "created_leads": []}
        return {**added(req), "remaining_in_plan": 5_000}

    t.route("POST", "/leads/add", fn=answer)
    t.route("POST", "/leads/list", {"items": []})


def test_an_approval_the_full_plan_stops_is_held_and_goes_through_once_there_is_room():
    ctx, t, sl, _ = proposed()
    state = {"full": True}
    full_then_room(t, state)
    for aid in ("acc-1", "acc-2"):
        sl.react("white_check_mark", HARRY_ID, ts=item_for(ctx, aid)["slack_ts"])
    out = poll(ctx)
    assert out["instantly_plan_full"] is True and len(adds(t)) == 1  # the second card is held without an add
    assert [h["why"] for h in out["held"]] == [[plan.HOLD], [plan.HOLD]] and out["outcomes"] == {}
    for aid in ("acc-1", "acc-2"):
        row = item_for(ctx, aid)
        assert (row["status"], row["payload"]["state"], row["payload"]["outcome"]) == ("open", "waiting", "")
        assert row["payload"]["held"]["reasons"] == {"instantly_plan": plan.HOLD}
    assert not any(c.get("suppressed") for c in ctx.store.select("contacts"))  # kept, never _refused
    assert sum(x.startswith("⏸ Approved") and "Instantly's plan has no room for new leads" in x for x in sl.texts()) == 2
    assert [x for x in sl.texts() if x.startswith("<@U_HARRY> Instantly's plan:")] == [FULL_ALERT]
    poll(ctx)  # still full: tried again, no new thread note, no second alert today
    assert sum(x.startswith("⏸ Approved") for x in sl.texts()) == 2 and len(adds(t)) == 2
    state["full"] = False  # Harry upgraded the plan
    out = poll(ctx)
    assert out["outcomes"] == {"approved": 2} and len(adds(t)) == 4


def test_an_add_left_sending_after_a_full_plan_is_held_not_refused():
    ctx, t, sl, _ = proposed()
    row = item_for(ctx, "acc-1")
    payload = {**row["payload"], "state": "sending", "held": {}, "sending": {
        "at": (ctx.now - timedelta(minutes=20)).isoformat(), "by": HARRY_ID, "via": "✅", "answered": True,
        "plan_full": True}}
    ctx.store.update("hitl_items", {"item_id": row["item_id"]}, {"status": "sending", "payload": payload})
    t.route("POST", "/leads/list", {"items": []})
    out = poll(ctx)
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["payload"]["outcome"]) == ("open", "") and out["held"][0]["why"] == [plan.HOLD]
    assert not any(c.get("suppressed") for c in ctx.store.select("contacts"))
