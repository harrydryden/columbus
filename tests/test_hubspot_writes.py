"""SPEC 11 HubSpot writes on a positive or referral reply (crm/hubspot_writes.record_reply).

Only the six us_outbound_* properties, identity fields on create and the three empty-only fields;
idempotent; a domain with two companies is held for a manual merge; one deal per company.
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
    HUBSPOT_COMPANY_PROPS,
    HUBSPOT_CONTACT_CREATE_FIELDS,
    HUBSPOT_CONTACT_PROPS,
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


class FakeHubSpot:
    """An in-memory HubSpot CRM behind the transport: objects, search, associations, pipelines."""

    def __init__(self, t: FakeTransport):
        self.objects: dict[str, dict[str, dict]] = {k: {} for k in ("companies", "contacts", "deals", "notes", "tasks",
                                                                     "meetings")}
        self.links: set[tuple[str, str, str, str]] = set()  # (from type, from id, to type, to id)
        self.fail: dict[str, int] = {}  # object type -> HTTP status its next create answers
        self._n = 0
        for method in ("GET", "POST", "PATCH", "PUT"):
            t.route(method, "api.hubapi.com", fn=self.handle)

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
        if op == "GTE":
            return have != "" and int(have) >= int(value)
        if op == "HAS_PROPERTY":
            return have != ""
        raise AssertionError(f"unexpected operator {op}")

    def handle(self, req) -> Any:
        path = urlparse(req.url).path
        parts = path.strip("/").split("/")
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
    from us_outbound.replies.items import ReplyItem

    return ReplyItem(ctx.store.get("hitl_items", item_id=item_id))


ALLOWED = {
    "company.create": HUBSPOT_COMPANY_PROPS | HUBSPOT_EMPTY_ONLY | HUBSPOT_COMPANY_CREATE_FIELDS,
    "company.update": HUBSPOT_COMPANY_PROPS | HUBSPOT_EMPTY_ONLY,
    "contact.create": HUBSPOT_CONTACT_PROPS | HUBSPOT_EMPTY_ONLY | HUBSPOT_CONTACT_CREATE_FIELDS,
    "contact.update": HUBSPOT_CONTACT_PROPS | HUBSPOT_EMPTY_ONLY,
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
    [(cid, company)] = crm.objects["companies"].items()
    assert company == {
        "name": "Acme Creative", "domain": "acmecreative.com", "us_outbound_account_id": "acc-1",
        "us_outbound_tier": "Priority", "us_outbound_industry_group": "Marketing & Creative Agencies",
        **({"us_outbound_top_signals": company["us_outbound_top_signals"]}
           if "us_outbound_top_signals" in company else {}),  # blank (left out) with no fresh signal
        "hubspot_owner_id": "owner-harry",
        "lifecyclestage": "lead",
    }
    [(kid, contact)] = crm.objects["contacts"].items()
    assert contact == {"email": JANE, "firstname": "Jane", "lastname": "Doe", "jobtitle": "Head of People",
                       "us_outbound_angle": "Upgrade the EAP (eap-v1)", "us_outbound_reply_class": "positive",
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
    assert company["lifecyclestage"] == "opportunity" and company["us_outbound_account_id"] == "acc-1"
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


def test_a_disallowed_property_is_refused_before_any_request(default_settings, monkeypatch):
    """The guard catches a mistake in this module: nothing but the six properties is ever sent."""
    ctx, t, crm = world(default_settings)
    original = hw.company_properties
    monkeypatch.setattr(hw, "company_properties", lambda c, a: {**original(c, a), "annualrevenue": "5000000"})
    with pytest.raises(GuardViolation, match="annualrevenue"):
        hw.record_reply(ctx, item(ctx))
    assert crm.writes(t) == [] and crm.objects["companies"] == {}


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
