"""verify_accounts (us_outbound/verify.py) and the General key clay_verification (Harry, 1 Oct 2026):
the checks on Apollo data and HubSpot, the HubSpot exclusions, the rescore, the required mode, the
setting's default, validation and load, the two client reads it needs, and the pipeline from source
to verified."""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_sources_apollo import FakeApollo, org, settings_with
from us_outbound import suppression, verify
from us_outbound.clients.apollo import Apollo
from us_outbound.clients.guard import Boundaries, Guard
from us_outbound.clients.hubspot import HubSpot
from us_outbound.ops import cli
from us_outbound.ops.heartbeat import run_job
from us_outbound.scoring import tiers
from us_outbound.settings import load as loader
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import CLAY_VERIFICATION_MODES, Focus, General, Override
from us_outbound.settings.validate import validate_all
from us_outbound.sources import apollo_jobs, apollo_universe

NOW = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)  # Monday 5 Oct, 04:30 UK
HS = "https://api.hubapi.com"


def account(aid="a1", **kw) -> dict:
    return {"account_id": aid, "domain": f"{aid}co.com", "hq_state": "NY", "employees": 64, "size_band": "50-99",
            "industry": "Fintech", "industry_group": "Technology & Startups", "status": "new", "source": "apollo",
            "first_seen": NOW - timedelta(hours=1), **kw}


def hubspot_routes(t: FakeTransport, *, companies=(), contacts=(), deals=()) -> FakeTransport:
    t.route("POST", "/crm/v3/objects/companies/search", {"results": list(companies)})
    t.route("POST", "/crm/v3/objects/contacts/search", {"results": list(contacts)})
    t.route("POST", "/crm/v3/objects/deals/search", {"results": list(deals)})
    return t


def make(accounts, *, settings=None, **routes):
    t = hubspot_routes(FakeTransport(), **routes)
    ctx = make_context(settings or settings_with(), job=verify.JOB, now=NOW, transport=t)
    ctx.store.insert("accounts", accounts)
    return ctx, t


def status(ctx, aid="a1") -> str:
    return ctx.store.get("accounts", account_id=aid)["status"]


def hubspot_requests(t):
    return [r for r in t.requests if r.url.startswith(HS)]


# -- skip: verified on Apollo data and HubSpot -----------------------------------------------------


def test_an_account_that_passes_every_check_is_verified_and_scored():
    ctx, t = make([account(), account("a2", status="queued", employees=None, size_band="20-49")])
    out = verify.run(ctx)
    assert (out["verified"], out["checked"], out["clay_verification"]) == (2, 2, "skip")
    assert status(ctx, "a1") == status(ctx, "a2") == "verified"
    a = ctx.store.get("accounts", account_id="a1")
    assert a["tier"] in ("Priority", "Standard", "Control") and a["last_scored"] == NOW  # the rescore ran
    assert a.get("clay_checked_at") is None  # how to find the accounts Clay has not seen
    bodies = [r.json for r in hubspot_requests(t)]
    assert {"propertyName": "domain", "operator": "EQ", "value": "a1co.com"} in [g["filters"][0] for g in bodies[0]["filterGroups"]]
    assert any(f == {"propertyName": "hs_email_domain", "operator": "EQ", "value": "a1co.com"}
               for b in bodies for g in b["filterGroups"] for f in g["filters"])
    assert {c.system for c in ctx.guard.writes()} == {"db"}  # reads only, outside the database


@pytest.mark.parametrize("change, reason", [
    ({"domain": None}, "no domain"),
    ({"domain": "gmail.com"}, "a personal email domain"),
    ({"hq_state": None}, verify.DOUBTFUL),  # Harry, 2 Oct 2026: to the hand-check, not dropped unseen
    ({"hq_state": "CA"}, "HQ in CA or WA"),
    ({"hq_state": "Washington"}, "HQ in CA or WA"),
    ({"hq_state": "OH"}, "HQ state not active"),
    ({"employees": 300, "size_band": None}, "outside 10 to 249 employees"),
    ({"employees": 6}, "outside 10 to 249 employees"),
    ({"employees": None, "size_band": None}, verify.DOUBTFUL),
    ({"industry": "Staffing agencies"}, "industry switched off"),
    ({"industry": "Underwater basket weaving"}, "no Industries label"),
    ({"naics": "561330"}, "a partner, never prospected"),  # a PEO
])
def test_an_account_that_fails_a_check_keeps_its_status(change, reason):
    ctx, t = make([account(**change)])
    out = verify.run(ctx)
    assert out["verified"] == 0 and out["not_verified"] == {reason: 1} and status(ctx) == "new"
    assert hubspot_requests(t) == []  # HubSpot is asked only once the free checks pass


def test_suppressed_and_partner_domains_are_not_verified():
    ctx, _ = make([account("a1"), account("a2")])
    suppression.add(ctx.store, domain="a1co.com", reason="test", source="test", now=NOW)
    ctx.store.insert("partners", [{"domain": "a2co.com", "reason": "broker", "added_at": NOW}])
    assert verify.run(ctx)["not_verified"] == {"domain suppressed": 1, "a partner, never prospected": 1}


@pytest.mark.parametrize("routes, fact, reason", [
    ({"companies": [{"id": "c1", "properties": {"lifecyclestage": "customer"}}]}, "hubspot_customer", "a customer in HubSpot"),
    ({"companies": [{"id": "c1", "properties": {"hubspot_owner_id": "someone-else"}}]}, "hubspot_other_owner",
     "owned by someone else in HubSpot"),
    ({"companies": [{"id": "c1", "properties": {}}], "deals": [{"id": "d1", "properties": {"hs_is_closed": "false"}}]},
     "hubspot_open_deal", "an open deal in HubSpot"),
    ({"contacts": [{"id": "k1", "properties": {"hs_email_optout": "true"}}]}, "hubspot_opted_out_or_bounced",
     verify.OPTED_OUT_REASON),
])
def test_hubspot_excludes_a_customer_another_owner_an_open_deal_or_an_opted_out_company(routes, fact, reason):
    s = settings_with(hubspot_owner_id="owner-harry")
    ctx, t = make([account()], settings=s, **routes)
    out = verify.run(ctx)
    assert out["excluded"] == [{"account_id": "a1", "reason": reason}] and status(ctx) == "new"
    [e] = ctx.store.select("signal_events", {"account_id": "a1", "source": "hubspot"})
    assert (e["fact"], e["value"]) == (fact, True)
    assert ctx.store.get("accounts", account_id="a1")["tier"] == "Excluded"  # and the rescore keeps it so
    asked = len(hubspot_requests(t))
    assert verify.run(ctx)["not_verified"] == {f"HubSpot: {dict(tiers.HUBSPOT_EXCLUSIONS)[fact]}": 1}
    assert len(hubspot_requests(t)) == asked  # the fact keeps it out: HubSpot is not asked again


def test_a_hubspot_error_leaves_the_account_for_the_next_run():
    ctx, t = make([account()])
    t.route("POST", "/crm/v3/objects/companies/search", status=500, body={"message": "down"})
    out = verify.run(ctx)
    assert out["not_verified"] == {"HubSpot check failed": 1} and out["errors"] and status(ctx) == "new"


def test_overrides_win_over_the_account_columns():
    s = settings_with(overrides=(Override("a1co.com", "hq_state", "IL"),))
    ctx, _ = make([account(hq_state="OH")], settings=s)
    assert verify.run(ctx)["verified"] == 1


def test_the_focus_tab_s_groups_are_checked_first(monkeypatch):
    s = settings_with(focus=(Focus("Marketing & Creative Agencies", 0.5),))
    ctx, _ = make([account("t1", score=90, tier="Priority"),
                   account("m1", industry="Advertising agencies", industry_group="Marketing & Creative Agencies")], settings=s)
    order = []
    original = verify.check
    monkeypatch.setattr(verify, "check", lambda a, *rest: order.append(a["account_id"]) or original(a, *rest))
    verify.run(ctx)
    assert order == ["m1", "t1"]


# -- required: accounts wait for verify_in_clay ----------------------------------------------------


def test_with_clay_required_accounts_wait_and_the_run_says_why():
    s = settings_with(clay_verification="required")
    ctx, t = make([account()], settings=s)
    out = run_job(ctx, verify.run)
    assert out["skipped"] is True and "verify_in_clay is not built" in out["reason"] and out["waiting"] == 1
    assert status(ctx) == "new" and t.requests == []
    assert ctx.store.get("heartbeats", run_id=ctx.run_id)["status"] == "skipped"  # alive, for heartbeat_check


# -- the setting --------------------------------------------------------------------------------------


def test_clay_verification_defaults_to_skip_with_harry_s_note():
    assert General().clay_verification == "skip" and CLAY_VERIFICATION_MODES == ("required", "skip")
    [row] = [r for r in default_tabs()["General"] if r["key"] == "clay_verification"]
    assert row["value"] == "skip"
    assert "Harry, 1 Oct 2026: go live on 5 Oct before the Clay functions exist; set to required once they do" in row["note"]


def test_clay_verification_must_be_required_or_skip():
    tabs = copy.deepcopy(default_tabs())
    row = next(r for r in tabs["General"] if r["key"] == "clay_verification")
    row["value"] = "required"
    assert validate_all(tabs)[0].general.clay_verification == "required"
    for bad in ("maybe", "Skip", ""):
        row["value"] = bad
        [e] = validate_all(tabs)[1]["General"]
        assert e.label == "clay_verification" and ("required, skip" in e.message or "is required" in e.message)
    tabs["General"].remove(row)
    assert validate_all(tabs)[0].general.clay_verification == "skip"  # a sheet without the row reads as skip


def test_settings_load_adds_the_key_to_harry_s_sheet_and_sets_it():
    sheet = [r for r in default_tabs()["General"] if r["key"] != "clay_verification"]
    plan = loader.plan_tab("General", sheet, default_tabs()["General"])
    assert "clay_verification = skip" in plan.added
    plan = loader.plan_tab("General", sheet, default_tabs()["General"], {"clay_verification": "required"})
    assert {r["key"]: r["value"] for r in plan.rows}["clay_verification"] == "required"


# -- the client reads verify and apollo_signals use -------------------------------------------------------


def test_hubspot_opted_out_contacts_at_a_domain_is_one_search_page():
    t = FakeTransport()
    t.route("POST", "/crm/v3/objects/contacts/search", body={
        "results": [{"id": "k1", "properties": {"hs_email_optout": "true"}}], "paging": {"next": {"after": "1"}}})
    hs = HubSpot(Guard(live=False, bounds=Boundaries()), t, "tok")
    assert [c["id"] for c in hs.opted_out_contacts_at_domain("WWW.Acme.com")] == ["k1"]
    [req] = t.requests
    groups = req.json["filterGroups"]
    assert req.json["limit"] == 1 and len(groups) == 2
    assert all(g["filters"][0] == {"propertyName": "hs_email_domain", "operator": "EQ", "value": "acme.com"} for g in groups)
    assert groups[0]["filters"][1]["propertyName"] == "hs_email_optout"
    assert groups[1]["filters"][1] == {"propertyName": "hs_email_hard_bounce_reason_enum", "operator": "HAS_PROPERTY"}


def test_apollo_job_postings_is_a_paid_read_by_organization_id():
    t = FakeTransport()
    t.route("GET", "/job_postings", body={"organization_job_postings": [{"title": "HR Manager"}]})
    guard = Guard(live=False, bounds=Boundaries())
    apollo = Apollo(guard, t, "k")
    assert apollo.job_postings("5e66b6381e05b4008c8331b8", per_page=50)["organization_job_postings"]
    [req] = t.requests
    assert req.url == "https://api.apollo.io/api/v1/organizations/5e66b6381e05b4008c8331b8/job_postings"
    assert req.params == {"page": 1, "per_page": 50} and req.idempotent is False  # never resent: it costs a credit
    assert guard.calls[-1].action == "organizations.job_postings" and not guard.calls[-1].write
    for bad in ("../people", "a/b", ""):
        with pytest.raises(ValueError):
            apollo.job_postings(bad)
    assert len(t.requests) == 1


# -- by hand, and the pipeline from source to verified ---------------------------------------------------


@pytest.mark.parametrize("job", ["source_universe", "apollo_signals", "verify_accounts"])
def test_harry_can_run_each_job_by_hand(job, capsys):
    store = None
    contexts = []

    def factory(name, live_flag, operator=False):
        nonlocal store
        t = hubspot_routes(FakeApollo([org(1)]).install(FakeTransport()))
        ctx = make_context(settings_with(), job=name, now=NOW, transport=t, store=store)
        store = ctx.store
        contexts.append(ctx)
        return ctx

    assert cli.main(["dry-run", job], context_factory=factory) == 0
    assert cli.main(["run", job], context_factory=factory) == 0
    assert contexts[-1].dry_run and contexts[-1].job == job
    assert store.get("heartbeats", run_id=contexts[-1].run_id)["status"] in ("ok", "skipped")


def test_from_apollo_to_a_verified_scored_account():
    fake = FakeApollo([org(1, keywords=("fintech",)), org(2, domain="gmail.com")],
                      postings={"org001": ["Head of People", "Engineer", "Engineer II"]})
    t = hubspot_routes(fake.install(FakeTransport()))
    ctx = make_context(settings_with(), job="pipeline", now=NOW, transport=t)
    apollo_universe.run(dataclasses.replace(ctx, job=apollo_universe.JOB))
    apollo_jobs.run(dataclasses.replace(ctx, job=apollo_jobs.JOB))
    out = verify.run(dataclasses.replace(ctx, job=verify.JOB))
    assert out["verified"] == 1
    [a] = ctx.store.select("accounts")
    assert (a["domain"], a["status"], a["industry"], a["size_band"]) == ("company1.com", "verified", "Fintech", "50-99")
    matched = {e["value"]["signal"] for e in ctx.store.select("signal_events", {"source": "scoring"})}
    # apollo_org's funding fact, scored by the rescore (funding split by age, review Appendix A)
    assert matched & {"Recent funding", "Funding 6–12 months ago"}
    jobs = {e["fact"]: e["value"] for e in ctx.store.select("signal_events", {"source": "apollo_jobs"})}
    assert (jobs["open_roles"], jobs["open_people_roles"]) == (3, 1)
    assert {r["job"] for r in ctx.store.select("credit_ledger")} == {"source_universe", "apollo_signals"}


# -- doubtful Apollo facts go to the weekly hand-check (Harry, 2 Oct 2026) ------------------------------


@pytest.mark.parametrize("change, reasons", [
    ({}, []),
    ({"hq_state": None}, ["Apollo gives no HQ state"]),
    ({"employees": None, "size_band": None}, ["Apollo gives no employee count or size band"]),
    ({"employees": None, "size_band": "20-49"}, []),  # a band from Apollo's size filter is not in doubt
    ({"employees": 64, "size_band": "20-49"}, ["the employee count (64) and the size band (20-49) disagree"]),
    ({"employees": 49, "size_band": "20-49"}, ["Apollo's estimate of 49 staff is within 2 of the 50-staff edge"]),
    ({"employees": 51}, ["Apollo's estimate of 51 staff is within 2 of the 50-staff edge"]),
    ({"employees": 52}, []),
    ({"employees": 11, "size_band": "10-19"}, ["Apollo's estimate of 11 staff is within 2 of the 10-staff edge"]),
    ({"employees": 248, "size_band": "100-249"}, ["Apollo's estimate of 248 staff is within 2 of the 250-staff edge"]),
])
def test_what_counts_as_a_doubtful_apollo_fact(change, reasons):
    assert verify.doubts(account(**change)) == reasons


def test_a_doubtful_account_goes_to_the_hand_check_once_instead_of_being_verified():
    ctx, t = make([account(employees=49, size_band="20-49"), account("a2")])
    out = verify.run(ctx)
    assert (out["verified"], out["to_hand_check"]) == (1, 1)
    assert out["not_verified"] == {verify.DOUBTFUL: 1} and status(ctx) == "new" and status(ctx, "a2") == "verified"
    assert out["to_hand_check_accounts"] == [
        {"account_id": "a1", "domain": "a1co.com", "reasons": ["Apollo's estimate of 49 staff is within 2 of the 50-staff edge"]}]
    assert [r for r in hubspot_requests(t) if "a1co.com" in str(r.json)] == []  # nothing asked about it meanwhile
    [fact] = ctx.store.select("signal_events", {"source": verify.DOUBT_SOURCE, "fact": verify.DOUBT_FACT})
    assert fact["value"] == {"reasons": ["Apollo's estimate of 49 staff is within 2 of the 50-staff edge"],
                             "facts": {"hq_state": "NY", "employees": 49, "size_band": "20-49"}}
    verify.run(ctx)  # the same doubt is not recorded twice
    assert len(ctx.store.select("signal_events", {"source": verify.DOUBT_SOURCE})) == 1
    assert [d["account_id"] for d in verify.open_doubts(ctx)] == ["a1"]


def test_the_hand_check_lists_doubts_and_approving_it_clears_them_so_the_next_run_verifies():
    from us_outbound.enrol import hand_check

    accounts = [account(employees=49, size_band="20-49"), account("a2", hq_state=None), account("a3", employees=50)]
    ctx, _ = make(accounts)
    verify.run(ctx)
    live = dataclasses.replace(ctx, job="handcheck_show")
    live.guard.configure(live=True)
    item, payload = hand_check.show(live)
    assert item is None and hand_check.has_work(payload)  # show never writes; approve records the week itself
    assert [d["account_id"] for d in payload["doubtful"]] == ["a1", "a2", "a3"]
    words = hand_check.text(payload)
    assert "3 accounts have doubtful Apollo facts, so they won't get a card until you look." in words
    assert "(a1co.com) · HQ NY · 20-49 (49 staff) · Apollo's estimate of 49 staff is within 2 of the 50-staff edge" in words
    out = hand_check.approve(live, ["a3co.com"], "harry")
    assert out["doubts_cleared"] == ["a1", "a2"] and out["pulled_account_ids"] == ["a3"]
    live.guard.configure(live=False)
    again = verify.run(ctx)
    assert status(ctx, "a1") == "verified"  # checked by Harry: verified on its facts
    assert status(ctx, "a2") == "new" and again["not_verified"]["HQ state unknown"] == 1  # needs an Overrides row
    assert status(ctx, "a3") == "new" and again["to_hand_check"] == 1  # pulled: still held


def test_an_overrides_row_settles_a_doubt():
    s = settings_with(overrides=(Override("a1co.com", "employees", "45"),))
    ctx, _ = make([account(employees=49, size_band="20-49")], settings=s)
    assert verify.run(ctx)["verified"] == 1


def test_the_cross_check_hook_can_settle_a_doubt(monkeypatch):
    seen = []

    def clay_agrees(ctx, account, found):
        seen.append((account["account_id"], list(found)))
        return []

    monkeypatch.setattr(verify, "cross_check", clay_agrees)
    ctx, _ = make([account(employees=49, size_band="20-49")])
    assert verify.run(ctx)["verified"] == 1
    assert seen == [("a1", ["Apollo's estimate of 49 staff is within 2 of the 50-staff edge"])]
