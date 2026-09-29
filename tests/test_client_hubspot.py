"""HubSpot client: request shapes, pagination, and the SPEC 1.2 write limits."""

from datetime import UTC, datetime

import pytest

from tests.fakes import FakeTransport
from us_outbound.clients.guard import Boundaries, Guard, GuardViolation
from us_outbound.clients.hubspot import HubSpot

BASE = "https://api.hubapi.com"
BOUNDS = Boundaries(hubspot_pipeline_id="pipe-spill3", hubspot_deal_stage_id="stage-first")


def make(live=True):
    transport = FakeTransport()
    guard = Guard(live=live, bounds=BOUNDS)
    return HubSpot(guard, transport, "tok"), transport, guard


def test_search_companies_by_domain_exact_root_and_paginates():
    hs, t, _ = make()
    pages = {
        None: {"results": [{"id": "1", "properties": {"name": "Acme", "domain": "acme.com"}}], "paging": {"next": {"after": "1"}}},
        "1": {"results": [{"id": "2", "properties": {"name": "Acme 2", "domain": "www.acme.com"}}]},
    }
    t.route("POST", "/crm/v3/objects/companies/search", fn=lambda req: pages[req.json.get("after")])

    found = hs.search_companies_by_domain("WWW.Acme.com ")

    assert [c["id"] for c in found] == ["1", "2"]
    assert found[0]["properties"]["name"] == "Acme"
    first = t.requests[0]
    assert first.url == f"{BASE}/crm/v3/objects/companies/search"
    assert first.headers["Authorization"] == "Bearer tok"
    values = [g["filters"][0] for g in first.json["filterGroups"]]
    assert values == [
        {"propertyName": "domain", "operator": "EQ", "value": "acme.com"},
        {"propertyName": "domain", "operator": "EQ", "value": "www.acme.com"},
    ]
    for prop in ("name", "domain", "lifecyclestage", "hs_lead_status", "hubspot_owner_id", "us_outbound_account_id"):
        assert prop in first.json["properties"]
    assert "after" not in first.json and t.requests[1].json["after"] == "1"


def test_search_contacts_by_email():
    hs, t, _ = make()
    t.route("POST", "/contacts/search", body={"results": [{"id": "9", "properties": {"email": "jane@acme.com"}}]})
    assert hs.search_contacts_by_email("Jane@Acme.com") == [{"id": "9", "properties": {"email": "jane@acme.com"}}]
    assert t.requests[0].json["filterGroups"][0]["filters"][0] == {
        "propertyName": "email", "operator": "EQ", "value": "jane@acme.com"
    }


def test_find_pipeline_returns_first_stage_by_display_order():
    hs, t, _ = make()
    t.route("GET", "/crm/v3/pipelines/deals", body={"results": [
        {"id": "default", "label": "Sales", "stages": [{"id": "s0", "displayOrder": 0}]},
        {"id": "p3", "label": "Spill 3.0", "stages": [
            {"id": "later", "displayOrder": 2}, {"id": "first", "displayOrder": 0}, {"id": "mid", "displayOrder": 1}]},
    ]})
    assert hs.find_pipeline("Spill 3.0") == ("p3", "first")
    assert hs.find_pipeline("Nope") is None


def test_owner_id_for_email():
    hs, t, _ = make()
    t.route("GET", "/crm/v3/owners", body={"results": [{"id": "77", "email": "harry@spill.chat"}]})
    assert hs.owner_id_for_email("harry@spill.chat") == "77"
    assert t.requests[0].params["email"] == "harry@spill.chat"
    t.route("GET", "/crm/v3/owners", body={"results": []})
    assert hs.owner_id_for_email("nobody@spill.chat") is None


def test_open_deals_for_company_drops_closed():
    hs, t, _ = make()
    t.route("POST", "/crm/v3/objects/deals/search", body={"results": [
        {"id": "d1", "properties": {"hs_is_closed": "false"}},
        {"id": "d2", "properties": {"hs_is_closed": "true"}},
        {"id": "d3", "properties": {}},  # unknown counts as open
    ]})
    assert [d["id"] for d in hs.open_deals_for_company("c1")] == ["d1", "d3"]
    f = t.requests[0].json["filterGroups"][0]["filters"][0]
    assert f == {"propertyName": "associations.company", "operator": "EQ", "value": "c1"}


# -- writes ---------------------------------------------------------------------


def test_create_company_live_shape():
    hs, t, _ = make()
    t.route("POST", "/crm/v3/objects/companies", body={"id": "c1", "properties": {}})
    props = {"name": "Acme", "domain": "acme.com", "us_outbound_tier": "Priority", "lifecyclestage": "lead"}
    assert hs.create_company(props, reply_class="positive")["id"] == "c1"
    req = t.requests[0]
    assert (req.method, req.url, req.json) == ("POST", f"{BASE}/crm/v3/objects/companies", {"properties": props})


def test_create_company_dry_run_sends_nothing():
    hs, t, guard = make(live=False)
    out = hs.create_company({"name": "Acme", "domain": "acme.com"}, reply_class="positive")
    assert out == {"id": None, "dry_run": True, "properties": {"name": "Acme", "domain": "acme.com"}}
    assert t.requests == []
    assert [c.action for c in guard.writes("hubspot", sent=False)] == ["company.create"]


@pytest.mark.parametrize("reply_class", ["objection", "not_now", None])
def test_create_contact_needs_warm_reply(reply_class):
    hs, t, _ = make()
    with pytest.raises(GuardViolation):
        hs.create_contact({"email": "jane@acme.com"}, reply_class=reply_class)
    assert t.requests == []


def test_create_contact_with_disallowed_property():
    hs, t, _ = make()
    with pytest.raises(GuardViolation):
        hs.create_contact({"email": "jane@acme.com", "phone": "555"}, reply_class="positive")
    assert t.requests == []


def test_update_company_cannot_set_name():
    hs, t, _ = make()
    with pytest.raises(GuardViolation):
        hs.update_company("c1", {"name": "Renamed"}, current={})
    assert t.requests == []


def test_update_company_empty_only_refused_when_set():
    hs, t, _ = make()
    with pytest.raises(GuardViolation):
        hs.update_company("c1", {"hubspot_owner_id": "owner-harry"}, current={"hubspot_owner_id": "someone-else"})
    assert t.requests == []


def test_update_company_reads_current_when_not_supplied():
    hs, t, _ = make()
    t.route("GET", "/crm/v3/objects/companies/c1", body={"id": "c1", "properties": {"lifecyclestage": "customer"}})
    with pytest.raises(GuardViolation):
        hs.update_company("c1", {"lifecyclestage": "lead"}, current=None)
    assert [(r.method, r.params) for r in t.requests] == [("GET", {"properties": "lifecyclestage"})]


def test_update_contact_live_when_empty():
    hs, t, _ = make()
    t.route("PATCH", "/crm/v3/objects/contacts/k1", body={"id": "k1"})
    props = {"hs_lead_status": "CONNECTED", "us_outbound_reply_class": "positive"}
    assert hs.update_contact("k1", props, current={"hs_lead_status": ""}) == {"id": "k1"}
    assert [(r.method, r.json) for r in t.requests] == [("PATCH", {"properties": props})]


def test_associate_uses_v4_default():
    hs, t, _ = make()
    hs.associate("contact", "k1", "company", "c1")
    assert (t.requests[0].method, t.requests[0].url) == (
        "PUT", f"{BASE}/crm/v4/objects/contacts/k1/associations/default/companies/c1"
    )


def test_note_and_task_associations():
    hs, t, _ = make()
    hs.create_note("Reply: sounds good", [("companies", "c1"), ("contact", "k1")], at=datetime(2026, 10, 27, 14, 0, tzinfo=UTC))
    note = t.requests[0].json
    assert note["properties"] == {"hs_note_body": "Reply: sounds good", "hs_timestamp": "2026-10-27T14:00:00.000Z"}
    assert [a["types"][0]["associationTypeId"] for a in note["associations"]] == [190, 202]
    assert note["associations"][0]["to"] == {"id": "c1"}

    hs.create_task("Reply to Jane", "Draft in Slack", "owner-harry", datetime(2026, 10, 27, 15, 0, tzinfo=UTC),
                   [("companies", "c1"), ("contacts", "k1")])
    task = t.requests[1]
    assert task.url == f"{BASE}/crm/v3/objects/tasks"
    assert task.json["properties"]["hubspot_owner_id"] == "owner-harry"
    assert task.json["properties"]["hs_timestamp"] == "2026-10-27T15:00:00.000Z"
    assert [a["types"][0]["associationTypeId"] for a in task.json["associations"]] == [192, 204]


def test_deal_only_in_spill3_first_stage():
    hs, t, _ = make()
    with pytest.raises(GuardViolation):
        hs.create_deal("US Outbound – Acme", "other-pipeline", "stage-first", "owner-harry", "c1")
    with pytest.raises(GuardViolation):
        hs.create_deal("US Outbound – Acme", "pipe-spill3", "stage-later", "owner-harry", "c1")
    with pytest.raises(GuardViolation):
        hs.create_deal("Acme deal", "pipe-spill3", "stage-first", "owner-harry", "c1")
    assert t.requests == []

    t.route("POST", "/crm/v3/objects/deals", body={"id": "d1"})
    assert hs.create_deal("US Outbound – Acme", "pipe-spill3", "stage-first", "owner-harry", "c1") == {"id": "d1"}
    body = t.requests[0].json
    assert body["properties"] == {
        "dealname": "US Outbound – Acme", "pipeline": "pipe-spill3", "dealstage": "stage-first", "hubspot_owner_id": "owner-harry"
    }
    assert body["associations"] == [
        {"to": {"id": "c1"}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}]}
    ]


def test_dry_run_writes_send_nothing():
    hs, t, guard = make(live=False)
    assert hs.create_note("x", [("companies", "c1")]) == {"id": None, "dry_run": True}
    assert hs.create_deal("US Outbound – Acme", "pipe-spill3", "stage-first", "o", "c1") == {"id": None, "dry_run": True}
    assert hs.update_contact("k1", {"us_outbound_angle": "General v1"}, current={}) == {
        "id": "k1", "dry_run": True, "properties": {"us_outbound_angle": "General v1"}
    }
    hs.associate("contacts", "k1", "companies", "c1")
    hs.unsubscribe("jane@acme.com")
    assert t.requests == []
    assert len(guard.writes("hubspot", sent=False)) == 5


def test_property_group_created_only_when_missing():
    hs, t, _ = make()
    t.route("GET", "/crm/v3/properties/companies/groups", body={"results": [{"name": "companyinformation"}]})
    hs.ensure_property_group("companies")
    post = t.writes()[0]
    assert post.url == f"{BASE}/crm/v3/properties/companies/groups"
    assert post.json == {"name": "us_outbound", "label": "US Outbound", "displayOrder": -1}

    t.route("GET", "/crm/v3/properties/contacts/groups", body={"results": [{"name": "us_outbound"}]})
    hs.ensure_property_group("contacts")
    assert len(t.writes()) == 1


def test_create_property_only_the_six():
    hs, t, _ = make()
    spec = {"name": "us_outbound_tier", "label": "US Outbound tier", "type": "string", "fieldType": "text",
            "groupName": "us_outbound"}
    hs.create_property("companies", spec)
    assert (t.requests[0].url, t.requests[0].json) == (f"{BASE}/crm/v3/properties/companies", spec)
    with pytest.raises(GuardViolation):
        hs.create_property("companies", {**spec, "name": "annualrevenue"})
    assert len(t.requests) == 1


def test_unsubscribe_and_gdpr_delete_shapes():
    hs, t, _ = make()
    hs.unsubscribe("Jane+x@Acme.com")
    req = t.requests[0]
    assert req.url == f"{BASE}/communication-preferences/v4/statuses/jane%2Bx%40acme.com/unsubscribe-all"
    assert req.params == {"channel": "EMAIL"}

    hs.gdpr_delete_contact("jane@acme.com")
    req = t.requests[1]
    assert (req.url, req.json) == (
        f"{BASE}/crm/v3/objects/contacts/gdpr-delete", {"objectId": "jane@acme.com", "idProperty": "email"}
    )


def test_iter_opted_out_or_bounced_pages_on_object_id():
    hs, t, _ = make()
    optout = [{"id": str(i), "properties": {"email": f"p{i}@x.com"}} for i in range(1, 101)] + [
        {"id": "101", "properties": {"email": "P1@x.com"}},  # duplicate after normalising
        {"id": "102", "properties": {"email": ""}},
    ]
    bounced = [{"id": "5", "properties": {"email": "p5@x.com"}}, {"id": "900", "properties": {"email": "b@y.com"}}]

    def search(req):
        cond, gt = req.json["filterGroups"][0]["filters"]
        rows = optout if cond["propertyName"] == "hs_email_optout" else bounced
        after = [r for r in rows if int(r["id"]) > int(gt["value"])]
        return {"results": after[: req.json["limit"]]}

    t.route("POST", "/crm/v3/objects/contacts/search", fn=search)
    emails = list(hs.iter_opted_out_or_bounced_emails())

    assert len(emails) == 101 and emails[-1] == "b@y.com" and len(set(emails)) == 101
    assert [r.json["filterGroups"][0]["filters"][1]["value"] for r in t.requests] == ["0", "100", "0"]
    assert t.requests[0].json["sorts"] == [{"propertyName": "hs_object_id", "direction": "ASCENDING"}]
    assert t.requests[2].json["filterGroups"][0]["filters"][0] == {
        "propertyName": "hs_email_hard_bounce_reason_enum", "operator": "HAS_PROPERTY"
    }
    assert all("after" not in r.json for r in t.requests)
