"""HubSpot setup: the "US Outbound" group and the six us_outbound_* properties; clashes stop it (SPEC 11, 14)."""

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound.clients.guard import HUBSPOT_SIX_PROPS
from us_outbound.crm import hubspot_writes as hw
from us_outbound.settings.model import TIERS, General, Settings

SETTINGS = Settings(general=General())
BASE = "https://api.hubapi.com"


def hubspot(existing=None, groups=None) -> FakeTransport:
    existing = existing or {}
    groups = groups or {}
    t = FakeTransport()
    for obj in ("companies", "contacts"):
        t.route("GET", f"/crm/v3/properties/{obj}", body={"results": existing.get(obj, [])})
        t.route("GET", f"/crm/v3/properties/{obj}/groups", body={"results": groups.get(obj, [{"name": "companyinformation"}])})
    return t


def created(t: FakeTransport) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"companies": [], "contacts": []}
    for r in t.requests:
        if r.method == "POST" and r.url.startswith(f"{BASE}/crm/v3/properties/") and not r.url.endswith("/groups"):
            out[r.url.rsplit("/", 1)[1]].append(r.json)
    return out


def test_creates_the_group_and_the_six_properties():
    t = hubspot()
    ctx = make_context(SETTINGS, live=True, transport=t)
    summary = hw.ensure_properties(ctx)
    groups = [r for r in t.requests if r.method == "POST" and r.url.endswith("/groups")]
    assert [g.url for g in groups] == [f"{BASE}/crm/v3/properties/companies/groups", f"{BASE}/crm/v3/properties/contacts/groups"]
    assert all(g.json["name"] == "us_outbound" and g.json["label"] == "US Outbound" for g in groups)
    props = created(t)
    assert [p["name"] for p in props["companies"]] == [
        "us_outbound_account_id", "us_outbound_tier", "us_outbound_industry_group", "us_outbound_top_signals"]
    assert [p["name"] for p in props["contacts"]] == ["us_outbound_angle", "us_outbound_reply_class"]
    by_name = {p["name"]: p for ps in props.values() for p in ps}
    assert set(by_name) == HUBSPOT_SIX_PROPS
    assert all(p["groupName"] == "us_outbound" and p["formField"] is False for p in by_name.values())
    assert by_name["us_outbound_account_id"]["hasUniqueValue"] is True
    assert by_name["us_outbound_account_id"]["type"] == "string"
    assert by_name["us_outbound_top_signals"]["fieldType"] == "textarea"
    tier = by_name["us_outbound_tier"]
    assert (tier["type"], tier["fieldType"]) == ("enumeration", "select")
    assert [o["value"] for o in tier["options"]] == list(TIERS)
    rc = by_name["us_outbound_reply_class"]
    assert [o["value"] for o in rc["options"]] == [
        "positive", "referral", "objection", "not_now", "negative", "out_of_office", "wrong_person", "unsubscribe", "other"]
    assert summary["created"] == [p for p in by_name]
    assert not [r for r in t.requests if r.method in ("PATCH", "PUT", "DELETE")]


def test_existing_properties_are_left_alone():
    existing = {"contacts": [
        {"name": "us_outbound_angle", "type": "string", "groupName": "us_outbound"},
        {"name": "us_outbound_reply_class", "type": "enumeration", "groupName": "us_outbound",
         "options": [{"value": "positive"}]},
    ]}
    t = hubspot(existing, groups={"contacts": [{"name": "us_outbound"}]})
    ctx = make_context(SETTINGS, live=True, transport=t)
    summary = hw.ensure_properties(ctx)
    assert created(t)["contacts"] == []
    assert len(created(t)["companies"]) == 4
    assert summary["existing"] == ["us_outbound_angle", "us_outbound_reply_class"]
    assert any("lacks options" in n for n in summary["notes"])
    groups = [r.url for r in t.requests if r.method == "POST" and r.url.endswith("/groups")]
    assert groups == [f"{BASE}/crm/v3/properties/companies/groups"]  # the contacts group exists
    assert not [r for r in t.requests if r.method in ("PATCH", "PUT", "DELETE")]


def test_a_clash_stops_before_any_write():
    existing = {"companies": [{"name": "us_outbound_tier", "type": "string", "groupName": "sales"}]}
    t = hubspot(existing)
    ctx = make_context(SETTINGS, live=True, transport=t)
    with pytest.raises(hw.PropertyClash, match="us_outbound_tier"):
        hw.ensure_properties(ctx)
    assert t.writes() == []


def test_dry_run_creates_nothing():
    t = hubspot()
    ctx = make_context(SETTINGS, live=False, transport=t)
    summary = hw.ensure_properties(ctx)
    assert t.writes() == []
    assert len(summary["would_create"]) == 6
    assert len(ctx.guard.writes("hubspot", sent=False)) == 8  # two groups and six properties, all skipped


def test_lookup_pipeline_reads_the_ids():
    t = FakeTransport()
    t.route("GET", "/crm/v3/pipelines/deals", body={"results": [
        {"id": "1", "label": "Sales", "stages": [{"id": "s", "displayOrder": 0}]},
        {"id": "82002613", "label": "Spill 3.0", "stages": [
            {"id": "154381889", "displayOrder": 1}, {"id": "154381888", "displayOrder": 0}]},
    ]})
    t.route("GET", "/crm/v3/owners", body={"results": [{"id": "82221891", "email": "harry@spill.chat"}]})
    ctx = make_context(SETTINGS, transport=t)
    assert hw.lookup_pipeline(ctx) == {
        "hubspot_pipeline_id": "82002613", "hubspot_deal_stage_id": "154381888", "hubspot_owner_id": "82221891"}
    assert t.writes() == []
