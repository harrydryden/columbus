"""verify_accounts (us_outbound/verify.py) and the General key clay_verification (Harry, 1 Oct 2026):
the checks on Apollo data and HubSpot, the HubSpot exclusions, the rescore, the required mode, the
setting's default, validation and load, the two client reads it needs, and the pipeline from source
to verified."""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_client_claude import FakeSDK
from tests.test_sources_apollo import FakeApollo, org, settings_with
from us_outbound import labels, suppression, verify
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
    assert list(dict.fromkeys(order)) == ["m1", "t1"]  # check() runs again after the label check


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
    # The label check (labels.py; Harry, 7 Oct 2026): the task model agrees with the rules' Fintech.
    sdk = FakeSDK(text=json.dumps({"label": "Fintech", "confidence": "high", "entity": "company",
                                   "evidence": "fintech", "what_they_do": "fintech"}))
    ctx = make_context(settings_with(), job="pipeline", now=NOW, transport=t, claude_sdk=sdk)
    apollo_universe.run(dataclasses.replace(ctx, job=apollo_universe.JOB))
    apollo_jobs.run(dataclasses.replace(ctx, job=apollo_jobs.JOB))
    out = verify.run(dataclasses.replace(ctx, job=verify.JOB))
    assert out["verified"] == 1 and out["labels"]["checked"] == 1 and out["labels"]["rules+model"] == 1
    [a] = ctx.store.select("accounts")
    assert (a["domain"], a["status"], a["industry"], a["size_band"]) == ("company1.com", "verified", "Fintech", "50-99")
    assert (a["label_source"], a["label_confidence"]) == ("rules+model", "high")
    matched = {e["value"]["signal"] for e in ctx.store.select("signal_events", {"source": "scoring"})}
    # apollo_org's funding fact, scored by the rescore (funding split by age, review Appendix A)
    assert matched & {"Recent funding", "Funding 6–12 months ago"}
    jobs = {e["fact"]: e["value"] for e in ctx.store.select("signal_events", {"source": "apollo_jobs"})}
    assert (jobs["open_roles"], jobs["open_people_roles"]) == (3, 1)
    assert {r["job"] for r in ctx.store.select("credit_ledger")} == {"source_universe", "apollo_signals", "label_check"}


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
    ({"industry": None}, ["Apollo gives no industry"]),
    ({"hq_state": "", "industry": ""}, ["Apollo gives no HQ state", "Apollo gives no industry"]),
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
    assert out["doubts_cleared"] == ["a1"] and out["pulled_account_ids"] == ["a3"]
    assert out["still_missing_a_fact"] == ["a2"]  # approving cannot supply an HQ state (Harry, 6 Oct 2026)
    assert "The account missing a fact (HQ state, size or industry) stays on the check" in out["message"]
    live.guard.configure(live=False)
    again = verify.run(ctx)
    assert status(ctx, "a1") == "verified"  # checked by Harry: verified on its facts
    assert status(ctx, "a2") == "new" and "HQ state unknown" not in again["not_verified"]  # never failed unseen
    assert status(ctx, "a3") == "new" and again["to_hand_check"] == 2  # a2 still missing its state; a3 pulled
    assert [d["account_id"] for d in verify.open_doubts(ctx)] == ["a2", "a3"]
    filled = dataclasses.replace(ctx, settings=settings_with(overrides=(Override("a2co.com", "hq_state", "NY"),)))
    assert [d["account_id"] for d in verify.open_doubts(filled)] == ["a3"]  # the Overrides row settles it
    verify.run(filled)
    assert status(ctx, "a2") == "verified"


def test_an_account_with_no_industry_goes_to_the_hand_check():
    ctx, _ = make([account(industry=None, industry_group=None)])
    out = verify.run(ctx)
    assert out["to_hand_check_accounts"] == [{"account_id": "a1", "domain": "a1co.com", "reasons": [verify.NO_INDUSTRY]}]
    ctx, _ = make([account(industry="Not a label")])  # a label the Industries tab lacks is no doubt: it fails as before
    assert verify.run(ctx)["not_verified"] == {"no Industries label": 1}


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


# -- the industry label check (labels.py; Harry, 7 Oct 2026: "industry categorisation is critical") --------------------


class LabelSDK:
    """The task model's answers by the company's domain (FakeSDK's shape); a domain with none raises an API error."""

    def __init__(self, answers: dict[str, dict], *, fail: bool = False):
        self.answers, self.fail, self.calls = answers, fail, []
        self.messages = self

    def create(self, **kwargs):
        import anthropic

        from tests.test_client_claude import sdk_error

        self.calls.append(kwargs)
        domain = next(line.split(": ", 1)[1] for line in kwargs["messages"][0]["content"].splitlines()
                      if line.startswith("Domain: "))
        if self.fail or domain not in self.answers:
            raise sdk_error(anthropic.InternalServerError, 500)
        base = {"label": "none", "confidence": "high", "entity": "company", "evidence": "", "what_they_do": ""}
        return FakeSDK(text=json.dumps({**base, **self.answers[domain]}), input_tokens=3900,
                       output_tokens=250).create(**kwargs)

    def domains(self) -> list[str]:
        return [next(line.split(": ", 1)[1] for line in c["messages"][0]["content"].splitlines()
                     if line.startswith("Domain: ")) for c in self.calls]


def apollo_facts(aid: str, *, naics=("541511",), keywords=("fintech", "payments"), industry="financial services",
                 description="Payments software for small businesses.") -> list[dict]:
    seen = NOW - timedelta(days=1)
    return [{"event_id": f"{aid}-{f}", "account_id": aid, "source": "apollo_org", "fact": f, "value": v, "quote": "",
             "source_url": "", "observed_at": seen}
            for f, v in (("naics", list(naics)), ("keywords", list(keywords)), ("apollo_industry", industry),
                         ("description", description)) if v]


def labelled(accounts, answers, *, settings=None, facts=None, fail=False, **routes):
    """verify's world with each account's Apollo facts and the task model answering by domain."""
    sdk = LabelSDK(answers, fail=fail)
    t = hubspot_routes(FakeTransport(), **routes)
    ctx = make_context(settings or settings_with(), job=verify.JOB, now=NOW, transport=t, claude_sdk=sdk)
    ctx.store.insert("accounts", accounts)
    for a in accounts:
        ctx.store.insert("signal_events", (facts or {}).get(a["account_id"]) or apollo_facts(a["account_id"]))
    return ctx, sdk


def acc(ctx, aid="a1") -> dict:
    return ctx.store.get("accounts", account_id=aid)


def test_agreement_verifies_with_the_labels_own_copy_and_the_verdict_is_asked_once():
    ctx, sdk = labelled([account()], {"a1co.com": {"label": "Fintech", "evidence": "Payments software"}})
    out = verify.run(ctx)
    assert out["verified"] == 1 and out["labels"]["checked"] == 1 and out["labels"]["rules+model"] == 1
    assert out["labels"]["usd"] > 0 and out["labels"]["unavailable_reason"] == ""
    a = acc(ctx)
    assert (a["status"], a["industry"], a["label_source"], a["label_confidence"]) == (
        "verified", "Fintech", "rules+model", "high")
    [call] = sdk.calls
    assert call["model"] == "claude-sonnet-5-5" and "Fintech" not in call["messages"][0]["content"].split("Keywords")[0]
    [fact] = ctx.store.select("signal_events", {"source": "label_check"})
    assert fact["value"]["decision"]["source"] == "rules+model" and fact["value"]["asked"] is True
    verify.run(ctx)
    assert len(sdk.calls) == 1  # kept for good: no call the second run
    assert len(ctx.store.select("signal_events", {"source": "label_check"})) == 1


def test_a_games_studio_by_the_rules_the_model_doubts_goes_out_under_the_groups_own_label():
    facts = {"a1": apollo_facts("a1", naics=("513210",), keywords=("video games", "fulfillment software"),
                                industry="computer software",
                                description="Software that runs fulfillment for online brands.")}
    ctx, _ = labelled([account(industry="Games studios")], {"a1co.com": {
        "label": "Technology & Startups", "confidence": "medium", "evidence": "Software that runs fulfillment"}},
        facts=facts)
    out = verify.run(ctx)
    a = acc(ctx)
    assert out["verified"] == 1 and out["labels"]["umbrella"] == 1
    assert (a["industry"], a["industry_group"], a["label_source"]) == (
        "Technology & Startups", "Technology & Startups", "umbrella")


def test_a_public_body_is_disqualified_by_the_model_and_a_gov_domain_without_a_call():
    accounts = [account("a1"), account("a2", domain="braintreema.gov")]
    ctx, sdk = labelled(accounts, {"a1co.com": {"label": "none", "confidence": "low", "entity": "public_body",
                                                "evidence": "Payments software"}})
    out = verify.run(ctx)
    a = acc(ctx)
    assert (a["status"], a["tier"]) == ("disqualified", "Excluded")
    assert a["tier_reason"] == "a public body, never prospected (the label check: “Payments software”)"
    assert out["not_verified"] == {f"disqualified: {a['tier_reason']}": 1, "a public body, never prospected": 1}
    assert sdk.domains() == ["a1co.com"] and out["labels"]["disqualified"] == 1
    assert acc(ctx, "a2")["status"] == "new"  # the domain rule: no call, as before


def test_a_cross_group_doubt_is_verified_with_general_copy_and_flagged():
    ctx, _ = labelled([account()], {"a1co.com": {"label": "Advertising agencies", "confidence": "medium",
                                                 "evidence": "Payments software"}})
    out = verify.run(ctx)
    a = acc(ctx)
    assert out["verified"] == 1 and (a["industry"], a["label_source"]) == ("Fintech", "disputed")
    assert labels.copy_level(a) == "general"
    [fact] = ctx.store.select("signal_events", {"source": "label_check"})
    assert fact["value"]["decision"]["reason"] == "the rules say Fintech, the model says Advertising agencies (medium)"


def test_a_doubt_whether_it_is_an_employer_goes_to_the_hand_check_and_approving_verifies_it():
    from us_outbound.enrol import hand_check

    ctx, sdk = labelled([account()], {"a1co.com": {"label": "Fintech", "confidence": "medium",
                                                   "entity": "association", "evidence": "Payments software"}})
    out = verify.run(ctx)
    reason = ("industry uncertain: the model thinks this is a membership body or society (medium); approving keeps "
              "Fintech")
    assert out["not_verified"] == {verify.DOUBTFUL: 1} and out["labels"]["held"] == 1 and status(ctx) == "new"
    assert out["to_hand_check_accounts"] == [{"account_id": "a1", "domain": "a1co.com", "reasons": [reason]}]
    [d] = verify.open_doubts(ctx)
    assert d["reasons"] == [reason]
    live = dataclasses.replace(ctx, job="handcheck_show")
    live.guard.configure(live=True)
    assert hand_check.approve(live, [], "harry")["doubts_cleared"] == ["a1"]
    live.guard.configure(live=False)
    again = verify.run(ctx)
    assert again["verified"] == 1 and status(ctx) == "verified" and len(sdk.calls) == 1
    assert acc(ctx)["label_source"] == "disputed"  # approved: General copy, as the doubt stays a doubt


def test_with_the_model_unavailable_a_new_account_waits_and_the_next_run_asks_again():
    ctx, sdk = labelled([account()], {}, fail=True)
    out = verify.run(ctx)
    [why] = out["not_verified"]
    assert why.startswith("label not checked: Claude did not answer") and status(ctx) == "new"
    assert out["labels"]["unchecked"] == 1 and out["labels"]["unavailable_reason"].startswith("Claude did not answer")
    assert hubspot_requests_count(ctx) == 0
    sdk.fail, sdk.answers = False, {"a1co.com": {"label": "Fintech", "evidence": "Payments software"}}
    assert verify.run(ctx)["verified"] == 1 and len(sdk.calls) == 2


def hubspot_requests_count(ctx) -> int:
    return len([r for r in ctx.clients.transport.requests if r.url.startswith(HS)])


def test_skip_verifies_on_the_rules_alone_with_the_groups_copy():
    ctx, sdk = labelled([account()], {}, settings=settings_with(label_check="skip"))
    out = verify.run(ctx)
    a = acc(ctx)
    assert out["verified"] == 1 and sdk.calls == [] and out["labels"]["rules"] == 1
    assert (a["industry"], a["label_source"], labels.copy_level(a)) == ("Fintech", "rules", "group")


def test_an_overrides_row_stands_and_nothing_is_asked():
    s = settings_with(overrides=(Override("a1co.com", "industry", "Edtech"),))
    ctx, sdk = labelled([account()], {}, settings=s)
    assert verify.run(ctx)["verified"] == 1 and sdk.calls == []
    a = acc(ctx)
    assert (a["industry"], a["label_source"], labels.copy_level(a)) == ("Edtech", "override", "label")


def test_a_company_with_nothing_to_check_against_is_verified_on_its_label_with_the_groups_copy():
    ctx, sdk = labelled([account()], {}, facts={"a1": [{"event_id": "x", "account_id": "a1", "source": "apollo_org",
                                                        "fact": "employees", "value": 64, "observed_at": NOW}]})
    assert verify.run(ctx)["verified"] == 1 and sdk.calls == []
    assert (acc(ctx)["industry"], acc(ctx)["label_source"]) == ("Fintech", "rules")


def test_the_runs_share_goes_to_the_focus_groups_first_and_the_rest_wait(monkeypatch):
    monkeypatch.setattr(labels, "MAX_LABEL_CALLS_PER_RUN", 1)
    s = settings_with(focus=(Focus("Marketing & Creative Agencies", 0.5),))
    agencies = apollo_facts("m1", naics=("541810",), keywords=("advertising agency",), industry="marketing",
                            description="An advertising agency for consumer brands.")
    ctx, sdk = labelled(
        [account("t1", score=90, tier="Priority"),
         account("m1", industry="Advertising agencies", industry_group="Marketing & Creative Agencies")],
        {"m1co.com": {"label": "Advertising agencies", "evidence": "An advertising agency"},
         "t1co.com": {"label": "Fintech", "evidence": "Payments software"}}, settings=s, facts={"m1": agencies})
    out = verify.run(ctx)
    assert sdk.domains() == ["m1co.com"] and status(ctx, "m1") == "verified" and status(ctx, "t1") == "new"
    assert out["not_verified"] == {"label not checked: this run's 1 label checks are used; the next run goes on": 1}
    verify.run(ctx)
    assert sdk.domains() == ["m1co.com", "t1co.com"] and status(ctx, "t1") == "verified"


# -- the queue converges: accounts already verified are checked too (Harry, 7 Oct 2026) ------------------------------


def test_a_verified_account_with_no_verdict_is_checked_and_its_label_put_right():
    ctx, sdk = labelled([account(status="verified", industry="Adtech & martech")],
                        {"a1co.com": {"label": "Fintech", "evidence": "Payments software"}})
    out = verify.run(ctx)
    a = acc(ctx)
    # Its facts say fintech: the rules now agree with the model (the label it was admitted under was the old tie-break).
    assert (a["status"], a["industry"], a["label_source"]) == ("verified", "Fintech", "rules+model")
    assert out["labels"]["changed"] == [{"account_id": "a1", "domain": "a1co.com", "from": "Adtech & martech",
                                         "to": "Fintech", "source": "rules+model", "action": "verify", "reason": ""}]
    assert hubspot_requests_count(ctx) == 0  # nothing else about it is checked again
    verify.run(ctx)
    assert len(sdk.calls) == 1


def test_a_verified_account_the_model_doubts_goes_back_to_the_hand_check_and_one_it_rules_out_leaves():
    accounts = [account("a1", status="verified"), account("a2", status="verified")]
    ctx, _ = labelled(accounts, {
        "a1co.com": {"label": "none", "confidence": "medium", "evidence": "Payments software"},
        "a2co.com": {"label": "none", "confidence": "high", "entity": "association", "evidence": "Payments software"}})
    out = verify.run(ctx)
    assert status(ctx, "a1") == "queued" and [d["account_id"] for d in verify.open_doubts(ctx)] == ["a1"]
    assert (status(ctx, "a2"), acc(ctx, "a2")["tier"]) == ("disqualified", "Excluded")
    assert (out["labels"]["held"], out["labels"]["disqualified"]) == (1, 1)


def test_required_never_unverifies_a_verified_account_when_the_model_is_unavailable():
    ctx, sdk = labelled([account(status="verified", industry="Adtech & martech")], {}, fail=True)
    out = verify.run(ctx)
    a = acc(ctx)
    assert (a["status"], a["industry"], a.get("label_source")) == ("verified", "Adtech & martech", None)
    assert labels.copy_level(a) == "group"  # the safe default: its group's copy
    assert out["labels"]["verified_unchecked"] == 1 and out["labels"]["unavailable_reason"]


def test_a_card_rendered_before_its_label_was_decided_is_withdrawn_when_it_no_longer_fits():
    from us_outbound.enrol import approvals

    ctx, _ = labelled([account(status="verified", industry="Adtech & martech")],
                      {"a1co.com": {"label": "Fintech", "evidence": "Payments software"}})
    ctx.store.insert("hitl_items", [{
        "item_id": "card-1", "kind": approvals.KIND, "account_id": "a1", "contact_id": "c1", "status": "open",
        "created_at": NOW - timedelta(hours=16), "slack_channel": "", "slack_ts": "",
        "payload": {"industry": "Adtech & martech", "industry_group": "Technology & Startups", "company": "A1",
                    "copy_version": "adtech-martech-people-v1", "state": "waiting"}}])
    assert approvals.unfit_cards(ctx) == []  # nothing decided since the card
    verify.run(ctx)
    [(item, why, back)] = approvals.unfit_cards(ctx)
    assert (item.id, why, back) == ("card-1", "its industry was Adtech & martech and is now Fintech", True)
