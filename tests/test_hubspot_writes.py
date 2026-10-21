"""SPEC 11 HubSpot writes on a positive or referral reply (crm/hubspot_writes.record_reply).

Standard properties only (W1-2, 19 Oct 2026; Harry's D9): identity fields on create and the three empty-only
fields. The fake HubSpot answers 400 to any property outside its declared schema, as the portal did to the six
us_outbound_* properties nobody created (code-replies-crm H1), so a test cannot pass on a write HubSpot refuses.
Idempotent; a domain with two companies is held for a manual merge; one deal per company; a colleague who replied is
the HubSpot contact, and the note and task name both people (M9).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound.clients.guard import (
    HUBSPOT_COMPANY_CREATE_FIELDS,
    HUBSPOT_CONTACT_CREATE_FIELDS,
    HUBSPOT_EMPTY_ONLY,
    GuardViolation,
)
from us_outbound.clients.http import ApiError, Response
from us_outbound.crm import hubspot_writes as hw
from us_outbound.logs import hash_email

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # Monday 16:00 UK
HANNAH = "hannah@meetspill.org"
JANE = "jane.doe@acmecreative.com"
STAGES = [
    {"id": "stage-first", "label": "Demo requested", "displayOrder": 0},
    {"id": "stage-created", "label": "Demo created", "displayOrder": 1},
    {"id": "stage-held", "label": "Demo held", "displayOrder": 2},
    {"id": "stage-onboarding", "label": "Onboarding", "displayOrder": 3},
    {"id": "stage-won", "label": "Closed won", "displayOrder": 5},
    {"id": "stage-lost", "label": "Closed lost", "displayOrder": 6},
]


class Transport(FakeTransport):
    """FakeTransport whose route functions may answer with a whole Response (a 404, a 500)."""

    def send(self, method, url, **kw):
        resp = super().send(method, url, **kw)
        return resp.body if isinstance(resp.body, Response) else resp


# The properties the fake portal has, per object type: HubSpot's standard ones the jobs use. Anything else in a create
# or an update is a 400, as HubSpot answers a property that does not exist (the six us_outbound_* never did).
SCHEMA = {
    "companies": {"name", "domain", "lifecyclestage", "hs_lead_status", "hubspot_owner_id"},
    "contacts": {"email", "firstname", "lastname", "jobtitle", "lifecyclestage", "hs_lead_status", "hubspot_owner_id",
                 "hs_email_optout", "associatedcompanyid"},
    "deals": {"dealname", "pipeline", "dealstage", "hubspot_owner_id", "hs_is_closed", "closedate"},
    "notes": {"hs_note_body", "hs_timestamp"},
    "tasks": {"hs_task_subject", "hs_task_body", "hubspot_owner_id", "hs_timestamp", "hs_task_status",
              "hs_task_priority", "hs_task_type"},
    "meetings": set(),
}


def _ms(value: str) -> int:
    """Milliseconds since the epoch, from milliseconds or ISO text: a search compares dates as HubSpot does."""
    return int(value) if value.isdigit() else int(datetime.fromisoformat(value).timestamp() * 1000)


class FakeHubSpot:
    """An in-memory HubSpot CRM behind the transport: objects, search, associations, pipelines, opt-outs. A create or
    update with a property outside SCHEMA answers 400 (PROPERTY_DOESNT_EXIST), and is kept in rejected."""

    def __init__(self, t: FakeTransport):
        self.objects: dict[str, dict[str, dict]] = {k: {} for k in ("companies", "contacts", "deals", "notes", "tasks",
                                                                     "meetings")}
        self.links: set[tuple[str, str, str, str]] = set()  # (from type, from id, to type, to id)
        self.fail: dict[str, int] = {}  # object type -> HTTP status its next create answers
        self.rejected: list[tuple[str, list[str]]] = []  # (object type, unknown properties) of each 400
        self.unsubscribed: list[str] = []
        self._n = 0
        for method in ("GET", "POST", "PATCH", "PUT"):
            t.route(method, "api.hubapi.com", fn=self.handle)

    def _unknown(self, obj: str, properties: dict) -> Response | None:
        unknown = sorted(set(properties) - SCHEMA.get(obj, set()))
        if not unknown:
            return None
        self.rejected.append((obj, unknown))
        return Response(400, {"status": "error", "category": "VALIDATION_ERROR",
                              "message": f"Property values were not valid: {unknown}",
                              "errors": [{"code": "PROPERTY_DOESNT_EXIST", "name": n} for n in unknown]})

    def add(self, obj: str, properties: dict, **links: list[str]) -> str:
        self._n += 1
        rid = f"{obj[:2]}{self._n}"
        self.objects[obj][rid] = {"hs_is_closed": "false", **properties} if obj == "deals" else dict(properties)
        for to, ids in links.items():
            for i in ids:
                self.link(obj, rid, to, i)
        return rid

    def link(self, a: str, a_id: str, b: str, b_id: str) -> None:
        self.links |= {(a, a_id, b, b_id), (b, b_id, a, a_id)}

    def linked(self, a: str, a_id: str, b: str) -> list[str]:
        return sorted(x[3] for x in self.links if x[:3] == (a, a_id, b))

    def _match(self, obj: str, rid: str, f: dict) -> bool:
        prop, op, value = f["propertyName"], f["operator"], str(f.get("value", ""))
        if prop.startswith("associations."):
            kind = {"company": "companies", "contact": "contacts", "deal": "deals"}[prop.split(".", 1)[1]]
            return value in self.linked(obj, rid, kind)
        have = str(self.objects[obj][rid].get(prop) or "")
        if op == "EQ":
            return have == value
        if op == "GTE":  # a date as HubSpot stores it, ISO text (createdate), or as milliseconds (the filter's form)
            return have != "" and _ms(have) >= int(value)
        if op == "HAS_PROPERTY":
            return have != ""
        raise AssertionError(f"unexpected operator {op}")

    def handle(self, req) -> Any:
        path = urlparse(req.url).path
        parts = path.strip("/").split("/")
        if parts[:2] == ["communication-preferences", "v4"]:  # POST .../statuses/{email}/unsubscribe-all
            self.unsubscribed.append(parts[3])
            return {}
        if parts[:3] == ["crm", "v3", "pipelines"]:
            return {"id": parts[4], "stages": STAGES} if len(parts) > 4 else {"results": [{"id": "pipe-spill3", "stages": STAGES}]}
        if parts[:3] == ["crm", "v4", "objects"]:  # PUT /crm/v4/objects/{a}/{id}/associations/default/{b}/{id}
            self.link(parts[3], parts[4], parts[7], parts[8])
            return {}
        obj = parts[3]
        if req.method == "POST" and parts[-1] == "search":
            groups = req.json["filterGroups"]
            hits = [rid for rid in self.objects[obj]
                    if any(all(self._match(obj, rid, f) for f in g["filters"]) for g in groups)]
            return {"results": [{"id": rid, "properties": dict(self.objects[obj][rid])} for rid in hits]}
        if req.method == "POST":
            if obj in self.fail:
                return Response(self.fail.pop(obj), {"message": "boom"})
            refused = self._unknown(obj, req.json["properties"])
            if refused is not None:
                return refused
            rid = self.add(obj, req.json["properties"])
            for a in req.json.get("associations") or ():
                self.link(obj, rid, {190: "companies", 192: "companies", 5: "companies", 202: "contacts",
                                     204: "contacts", 214: "deals", 216: "deals"}[a["types"][0]["associationTypeId"]],
                          a["to"]["id"])
            return {"id": rid, "properties": req.json["properties"]}
        rid = parts[4]
        if rid not in self.objects[obj]:
            return Response(404, {"message": "not found"})
        if req.method == "PATCH":
            refused = self._unknown(obj, req.json["properties"])
            if refused is not None:
                return refused
            self.objects[obj][rid].update(req.json["properties"])
            return {"id": rid, "properties": self.objects[obj][rid]}
        query = parse_qs(urlparse(req.url).query) or (req.params or {})
        kinds = (query.get("associations") or [""])
        kinds = kinds[0] if isinstance(kinds, list) else kinds
        assoc = {k: {"results": [{"id": i} for i in self.linked(obj, rid, k)]} for k in str(kinds).split(",") if k}
        return {"id": rid, "properties": dict(self.objects[obj][rid]), "associations": assoc}

    def writes(self, t: FakeTransport) -> list:
        return [r for r in t.writes() if "api.hubapi.com" in r.url and not r.url.endswith("/search")]


ACCOUNT = {"account_id": "acc-1", "domain": "acmecreative.com", "clean_name": "Acme Creative", "tier": "Priority",
           "industry": "Advertising agencies", "industry_group": "Marketing & Creative Agencies", "status": "engaged",
           "hq_state": "IL"}
CONTACT = {"contact_id": "con-1", "account_id": "acc-1", "first_name": "Jane", "last_name": "Doe", "email": JANE,
           "email_sha256": hash_email(JANE),
           "title": "Head of People", "role": "People leader", "angle": "Upgrade the EAP", "copy_version": "eap-v1",
           "mailbox": HANNAH, "instantly_campaign": "US Outbound – Hannah Spalding", "instantly_lead_id": "lead-1",
           "enrolled_at": NOW - timedelta(days=10)}


def reply_row(**payload: Any) -> dict:
    p = {"contact_id": "con-1", "account_id": "acc-1", "mailbox": HANNAH, "owner": "Hannah Spalding",
         "instantly_email_id": "em-1", "reply_class": "positive",
         "reply_excerpt": "Sounds interesting, could you send over some times next week?",
         "draft": "Hi Jane,\n\nGreat to hear. My colleague Harry Dryden runs our US demos; you can grab a time with "
                  "him here: https://meetings.hubspot.com/harry336/us-demo-link\n\nBest wishes,\nHannah",
         "referral": None, "received_at": (NOW - timedelta(minutes=40)).isoformat(),
         "slack_channel": "#us-outbound", "slack_ts": "100.000100", **payload}
    return {"item_id": "item-0001-aaaa", "kind": "reply", "status": "open", "account_id": "acc-1", "contact_id": "con-1",
            "event_id": "em-1", "payload": p, "created_at": NOW - timedelta(minutes=30)}


def world(settings, *, live: bool = True, now: datetime = NOW, row: dict | None = None):
    t = Transport()
    crm = FakeHubSpot(t)
    ctx = make_context(settings, live=live, transport=t, now=now, job="poll_approvals")
    ctx.store.upsert("accounts", [dict(ACCOUNT)])
    ctx.store.upsert("contacts", [dict(CONTACT)])
    ctx.store.upsert("events", [{"event_id": "em-1", "type": "replied", "account_id": "acc-1", "contact_id": "con-1",
                                 "mailbox": HANNAH, "reply_class": "positive",
                                 "reply_text": "Sounds interesting, could you send over some times next week?\nJane",
                                 "occurred_at": NOW - timedelta(minutes=40)}])
    ctx.store.upsert("hitl_items", [row or reply_row()])
    return ctx, t, crm


def item(ctx, item_id: str = "item-0001-aaaa"):
    from us_outbound.replies.desk import ReplyItem

    return ReplyItem(ctx.store.get("hitl_items", item_id=item_id))


ALLOWED = {
    "company.create": HUBSPOT_EMPTY_ONLY | HUBSPOT_COMPANY_CREATE_FIELDS,
    "company.update": HUBSPOT_EMPTY_ONLY,
    "contact.create": HUBSPOT_EMPTY_ONLY | HUBSPOT_CONTACT_CREATE_FIELDS,
    "contact.update": HUBSPOT_EMPTY_ONLY,
}


def assert_only_allowed_properties(ctx) -> None:
    for w in ctx.guard.writes("hubspot"):
        if w.action in ALLOWED:
            assert set(w.detail.get("properties") or ()) <= ALLOWED[w.action], w


# -- the writes ---------------------------------------------------------------------------------------


def test_new_company_contact_note_and_task(default_settings):
    ctx, t, crm = world(default_settings)
    out = hw.record_reply(ctx, item(ctx))
    assert out["status"] == "written"
    # Standard properties only: the identity fields and the three empty-only fields, and HubSpot took every write.
    assert crm.rejected == []
    [(cid, company)] = crm.objects["companies"].items()
    assert company == {"name": "Acme Creative", "domain": "acmecreative.com", "hubspot_owner_id": "owner-harry",
                       "lifecyclestage": "lead"}
    [(kid, contact)] = crm.objects["contacts"].items()
    assert contact == {"email": JANE, "firstname": "Jane", "lastname": "Doe", "jobtitle": "Head of People",
                       "hubspot_owner_id": "owner-harry", "lifecyclestage": "lead", "hs_lead_status": "CONNECTED"}
    assert crm.linked("contacts", kid, "companies") == [cid]
    [note] = crm.objects["notes"].values()
    assert "Sounds interesting" in note["hs_note_body"] and HANNAH in note["hs_note_body"]
    [(task_id, task)] = crm.objects["tasks"].items()
    assert task["hubspot_owner_id"] == "owner-harry" and task["hs_timestamp"].startswith("2026-10-05")
    assert "replies approve item-000" in task["hs_task_body"] and "harry@spill.chat" in task["hs_task_body"]
    assert set(crm.linked("tasks", task_id, "companies")) == {cid}
    # ids kept, so the next run finds the records without a search
    assert ctx.store.get("accounts", account_id="acc-1")["hubspot_company_id"] == cid
    assert ctx.store.get("contacts", contact_id="con-1")["hubspot_contact_id"] == kid
    assert item(ctx).hubspot["status"] == "written" and out["link"].endswith(f"/record/0-2/{cid}")
    assert crm.objects["deals"] == {}  # no demo asked for
    assert_only_allowed_properties(ctx)


def test_writing_again_writes_nothing(default_settings):
    ctx, t, crm = world(default_settings)
    hw.record_reply(ctx, item(ctx))
    n = len(crm.writes(t))
    assert hw.record_reply(ctx, item(ctx))["status"] == "written"
    assert len(crm.writes(t)) == n
    # Even with the item's progress lost, the stored ids find the records and nothing is duplicated
    # except the note and task, which are per reply (SPEC 11 step 3).
    ctx.store.update("hitl_items", {"item_id": "item-0001-aaaa"}, {"payload": reply_row()["payload"]})
    hw.record_reply(ctx, item(ctx))
    assert len(crm.objects["companies"]) == 1 and len(crm.objects["contacts"]) == 1


def test_a_failed_step_carries_on_where_it_stopped(default_settings):
    ctx, t, crm = world(default_settings)
    crm.fail["tasks"] = 500
    with pytest.raises(ApiError):
        hw.record_reply(ctx, item(ctx))
    progress = item(ctx).hubspot
    assert progress["company_id"] and progress["contact_id"] and progress["note_id"] and "task_id" not in progress
    assert hw.record_reply(ctx, item(ctx))["status"] == "written"
    assert len(crm.objects["companies"]) == len(crm.objects["contacts"]) == len(crm.objects["notes"]) == 1
    assert len(crm.objects["tasks"]) == 1


def test_existing_records_keep_their_owner_lifecycle_and_lead_status(default_settings):
    ctx, t, crm = world(default_settings)
    co = crm.add("companies", {"name": "ACME CREATIVE INC", "domain": "acmecreative.com", "hubspot_owner_id": "owner-sally",
                               "lifecyclestage": "opportunity"})
    crm.add("contacts", {"email": JANE, "firstname": "Janet", "hs_lead_status": "OPEN", "lifecyclestage": ""})
    hw.record_reply(ctx, item(ctx))
    company = crm.objects["companies"][co]
    assert company["name"] == "ACME CREATIVE INC" and company["hubspot_owner_id"] == "owner-sally"
    assert company["lifecyclestage"] == "opportunity" and crm.rejected == []
    [contact] = crm.objects["contacts"].values()
    assert contact["firstname"] == "Janet" and contact["hs_lead_status"] == "OPEN"  # never overwritten
    assert contact["lifecyclestage"] == "lead" and contact["hubspot_owner_id"] == "owner-harry"  # they were empty
    updates = [w for w in ctx.guard.writes("hubspot") if w.action.endswith(".update")]
    assert updates and all(set(w.detail["properties"]).isdisjoint({"name", "firstname", "email", "domain"}) for w in updates)
    assert_only_allowed_properties(ctx)


def test_two_companies_for_the_domain_hold_for_a_manual_merge(default_settings):
    ctx, t, crm = world(default_settings)
    a = crm.add("companies", {"name": "Acme", "domain": "acmecreative.com"})
    b = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    assert hw.record_reply(ctx, item(ctx))["status"] == "merge_needed"
    assert crm.writes(t) == [] and crm.objects["contacts"] == {}
    merge = ctx.store.get("hitl_items", item_id="manual_merge:acc-1")
    assert merge["kind"] == "manual_merge" and merge["status"] == "open"
    assert merge["payload"]["hubspot_company_ids"] == sorted([a, b])
    del crm.objects["companies"][a]  # Harry merges them in HubSpot
    assert hw.record_reply(ctx, item(ctx))["status"] == "written"
    assert ctx.store.get("hitl_items", item_id="manual_merge:acc-1")["status"] == "handled"
    assert ctx.store.get("accounts", account_id="acc-1")["hubspot_company_id"] == b


@pytest.mark.parametrize("reply_class", ["objection", "not_now", "wrong_person", "unsubscribe", ""])
def test_only_positive_or_referral_replies_reach_hubspot(default_settings, reply_class):
    ctx, t, crm = world(default_settings, row=reply_row(reply_class=reply_class))
    assert hw.record_reply(ctx, item(ctx))["status"] == "skipped"
    assert [r for r in t.requests if "api.hubapi.com" in r.url] == []


def test_a_referral_is_written_with_whom_they_referred(default_settings):
    referral = {"name": "Sam Lee", "title": "COO", "email": "sam.lee@acmecreative.com"}
    ctx, t, crm = world(default_settings, row=reply_row(reply_class="referral", referral=referral))
    ctx.store.upsert("accounts", [{"account_id": "acc-1", "status": "enrolled"}])
    assert hw.record_reply(ctx, item(ctx))["status"] == "written"
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "engaged"
    assert len(crm.objects["contacts"]) == 1  # the referred person is not created (SPEC 1.2)
    [note] = crm.objects["notes"].values()
    assert "Referred us to: Sam Lee, COO" in note["hs_note_body"]


@pytest.mark.parametrize("prop", ["us_outbound_account_id", "us_outbound_reply_class", "annualrevenue"])
def test_a_custom_or_other_property_is_refused_before_any_request(default_settings, prop):
    """The guard holds the allowlist: a custom property (the retired six included) never reaches HubSpot."""
    ctx, t, crm = world(default_settings)
    hs = ctx.clients.hubspot
    with pytest.raises(GuardViolation, match=prop):
        hs.create_company({"name": "Acme", "domain": "acmecreative.com", prop: "x"}, reply_class="positive")
    with pytest.raises(GuardViolation, match=prop):
        hs.create_contact({"email": JANE, prop: "x"}, reply_class="positive")
    assert crm.writes(t) == [] and crm.objects["companies"] == {} and crm.objects["contacts"] == {}


def test_the_fake_portal_answers_400_to_a_property_it_does_not_have():
    """The fake is strict, as HubSpot is: the old six-property write fails here as it did live (H1)."""
    t = Transport()
    crm = FakeHubSpot(t)
    resp = t.send("POST", "https://api.hubapi.com/crm/v3/objects/companies", headers={},
                  json={"properties": {"name": "Acme", "us_outbound_account_id": "acc-1"}})
    assert resp.status == 400 and crm.objects["companies"] == {}
    assert crm.rejected == [("companies", ["us_outbound_account_id"])]


def test_a_colleagues_reply_puts_the_colleague_in_hubspot_and_names_both(default_settings):
    """M9: Sam Lee answered for Jane Doe, whom we emailed. HubSpot gets Sam (from the reply's From line), linked to the
    company; the note and the task say Sam replied for Jane; our contact keeps no HubSpot id of Sam's."""
    row = reply_row(from_is_contact=False, from_address="sam.lee@acmecreative.com", from_name="Sam Lee")
    ctx, t, crm = world(default_settings, row=row)
    out = hw.record_reply(ctx, item(ctx))
    assert out["status"] == "written" and out["replier"] == "sam.lee@acmecreative.com" and crm.rejected == []
    [(kid, person)] = crm.objects["contacts"].items()
    assert (person["email"], person["firstname"], person["lastname"]) == ("sam.lee@acmecreative.com", "Sam", "Lee")
    assert crm.linked("contacts", kid, "companies") == [out["company_id"]]
    [task] = crm.objects["tasks"].values()
    assert task["hs_task_subject"] == "US Outbound: positive reply from Sam Lee at Acme Creative"
    assert task["hs_task_body"].startswith("Sam Lee replied for Jane Doe, Head of People, the person we emailed.")
    [note] = crm.objects["notes"].values()
    assert "from Sam Lee at Acme Creative" in note["hs_note_body"]
    assert "We emailed Jane Doe, Head of People; Sam Lee answered for them." in note["hs_note_body"]
    assert ctx.store.get("contacts", contact_id="con-1").get("hubspot_contact_id") is None


def test_an_old_item_without_the_from_address_records_the_contact(default_settings):
    ctx, t, crm = world(default_settings, row=reply_row(from_is_contact=False))
    hw.record_reply(ctx, item(ctx))
    [person] = crm.objects["contacts"].values()
    assert person["email"] == JANE


def test_a_warm_reply_that_asked_to_stop_is_opted_out_in_hubspot_too(default_settings):
    """The contact this write creates must not be left subscribed: it is opted out once, the same run."""
    ctx, t, crm = world(default_settings, row=reply_row(also_asked_to_stop=True))
    out = hw.record_reply(ctx, item(ctx))
    assert out["status"] == "written" and out["opted_out"] is True
    assert crm.unsubscribed == ["jane.doe%40acmecreative.com"]
    [task] = crm.objects["tasks"].values()
    assert "They also asked to be taken off the list, so they are suppressed" in task["hs_task_body"]
    # Nothing to approve (W1-2 review: the task said the desk would not send, then asked for the ✅).
    assert "Approve the answer" not in task["hs_task_body"] and "✅" not in task["hs_task_body"]
    assert task["hs_task_body"].endswith("Never reply to the prospect from harry@spill.chat.")
    hw.record_reply(ctx, item(ctx))
    assert crm.unsubscribed == ["jane.doe%40acmecreative.com"]  # once


def test_a_contact_hubspot_has_opted_out_already_is_not_unsubscribed_again(default_settings):
    """W1-2 review: the opt-out was sent whatever HubSpot had, so an answer refusing an address it had unsubscribed
    already would fail the write every 5 minutes. HubSpot's own hs_email_optout is read first, as optout.py does."""
    ctx, t, crm = world(default_settings, row=reply_row(also_asked_to_stop=True))
    crm.add("contacts", {"email": JANE, "firstname": "Jane", "hs_email_optout": "true"})
    out = hw.record_reply(ctx, item(ctx))
    assert out["status"] == "written" and out["opted_out"] is True and crm.unsubscribed == []


def test_owner_over_someone_elses_is_refused_by_the_guard(default_settings):
    ctx, t, crm = world(default_settings)
    co = crm.add("companies", {"name": "Acme", "domain": "acmecreative.com", "hubspot_owner_id": "owner-sally"})
    with pytest.raises(GuardViolation, match="only be set when empty"):
        ctx.clients.hubspot.update_company(co, {"hubspot_owner_id": "owner-harry"}, current={"hubspot_owner_id": ""})
    assert crm.objects["companies"][co]["hubspot_owner_id"] == "owner-sally"


# -- the deal (SPEC 11 step 5) ---------------------------------------------------------------------------


def test_demo_requested_makes_one_deal_per_company(default_settings):
    ctx, t, crm = world(default_settings, row=reply_row(demo_requested=True))
    out = hw.record_reply(ctx, item(ctx))
    [(deal_id, deal)] = crm.objects["deals"].items()
    assert deal["dealname"] == "US Outbound – Acme Creative"
    assert (deal["pipeline"], deal["dealstage"], deal["hubspot_owner_id"]) == ("pipe-spill3", "stage-first", "owner-harry")
    assert crm.linked("deals", deal_id, "companies") == [out["company_id"]]
    assert ctx.store.get("events", event_id=f"hs-deal:{deal_id}")["type"] == "deal_created"
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "demo_requested"
    # Again, for the same account: the deal is found by its id (search may lag), so no second one.
    assert hw.ensure_deal(ctx, ACCOUNT, out["company_id"])["created"] is False
    assert len(crm.objects["deals"]) == 1


def test_no_deal_when_the_company_has_an_open_one(default_settings):
    ctx, t, crm = world(default_settings, row=reply_row(classification={"demo_requested": True}))
    co = crm.add("companies", {"name": "Acme", "domain": "acmecreative.com"})
    crm.add("deals", {"dealname": "UK renewal", "pipeline": "pipe-uk"}, companies=[co])
    out = hw.record_reply(ctx, item(ctx))
    assert len(crm.objects["deals"]) == 1 and out["deal_id"] != ""
    assert ctx.store.select("events", {"type": "deal_created"}) == []


def test_a_closed_deal_does_not_block_a_new_one(default_settings):
    ctx, t, crm = world(default_settings)
    co = crm.add("companies", {"name": "Acme", "domain": "acmecreative.com"})
    crm.add("deals", {"dealname": "US Outbound – Acme", "pipeline": "pipe-spill3", "hs_is_closed": "true"}, companies=[co])
    assert hw.ensure_deal(ctx, ACCOUNT, co)["created"] is True
    assert len(crm.objects["deals"]) == 2


def test_no_deal_without_the_pipeline_ids(default_settings):
    settings = dataclasses.replace(default_settings, general=dataclasses.replace(
        default_settings.general, hubspot_pipeline_id="", hubspot_deal_stage_id=""))
    ctx, t, crm = world(settings, row=reply_row(demo_requested=True))
    out = hw.record_reply(ctx, item(ctx))
    assert crm.objects["deals"] == {} and "hubspot_pipeline_id" in out["why"]


def test_dry_run_reads_and_writes_nothing(default_settings):
    ctx, t, crm = world(default_settings, live=False, row=reply_row(demo_requested=True))
    out = hw.record_reply(ctx, item(ctx))
    assert out["status"] == "dry_run"
    assert crm.writes(t) == [] and all(not w.sent for w in ctx.guard.writes("hubspot"))
    assert item(ctx).hubspot == {} and ctx.store.get("accounts", account_id="acc-1").get("hubspot_company_id") is None
    co = crm.add("companies", {"name": "Acme", "domain": "acmecreative.com"})
    out = hw.record_reply(ctx, item(ctx))  # with the company there, every later write is worked out too
    assert out["status"] == "dry_run" and out["company_id"] == co and crm.writes(t) == []
    assert {w.action for w in ctx.guard.writes("hubspot")} >= {"contact.create"}


def test_the_property_setup_is_gone():
    """Harry's D9 (W1-2): no property group or property create is left to call, and the guard refuses both."""
    from us_outbound.clients.guard import Guard, Op

    for name in ("ensure_properties", "PROPERTY_SPECS", "top_signals", "company_properties", "contact_properties"):
        assert not hasattr(hw, name), name
    guard = Guard(live=True)
    for action in ("property_group.create", "property.create"):
        with pytest.raises(GuardViolation, match="is not allowed"):
            guard.authorize("hubspot", Op(action, target="us_outbound", write=True))
