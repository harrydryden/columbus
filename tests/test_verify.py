"""verify_accounts (us_outbound/verify.py): the checks on Apollo data and HubSpot, the HubSpot exclusions, the
rescore, the two client reads it needs, and the pipeline from source to verified. (The General key clay_verification
and its required mode went with Clay: W2-1.)"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_client_claude import FakeSDK
from tests.test_sources_apollo import FakeApollo, org, settings_with
from us_outbound import icp, labels, suppression, verify
from us_outbound.clients.apollo import Apollo
from us_outbound.clients.guard import Boundaries, Guard
from us_outbound.clients.hubspot import HubSpot
from us_outbound.context import Secrets
from us_outbound.ops import cli
from us_outbound.scoring import score
from us_outbound.settings.model import Focus, Override
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
    assert (out["verified"], out["checked"]) == (2, 2) and "clay_verification" not in out
    assert status(ctx, "a1") == status(ctx, "a2") == "verified"
    a = ctx.store.get("accounts", account_id="a1")
    assert a["tier"] == "Queue" and a["last_scored"] == NOW  # the rescore ran
    bodies = [r.json for r in hubspot_requests(t)]
    assert {"propertyName": "domain", "operator": "EQ", "value": "a1co.com"} in [g["filters"][0] for g in bodies[0]["filterGroups"]]
    assert any(f == {"propertyName": "hs_email_domain", "operator": "EQ", "value": "a1co.com"}
               for b in bodies for g in b["filterGroups"] for f in g["filters"])
    assert {c.system for c in ctx.guard.writes()} == {"db"}  # reads only, outside the database


@pytest.mark.parametrize("change, code, text, after", [
    # W1-4 (code-verify-labels B7): a failed check writes its code and does what the code says (verify.REASON_CODES),
    # where it used to keep the status, so the same never-pass accounts were checked every run and counted in the
    # queue. Terminal: disqualified for good. An industry switched off: parked, for when it is switched on again.
    ({"domain": None}, "incomplete", "no domain", "disqualified"),
    ({"domain": "gmail.com"}, "suppressed", "a personal email domain", "disqualified"),
    # The texts are the ICP gate's (icp.gate, W2-2), which names the state: "HQ in CA or WA" before.
    ({"hq_state": "CA"}, "state", "HQ in CA, which is never contacted", "disqualified"),
    ({"hq_state": "Washington"}, "state", "HQ in WA, which is never contacted", "disqualified"),
    ({"hq_state": "OH"}, "state", "HQ state OH is not active", "disqualified"),
    ({"employees": 300, "size_band": None}, "size", "outside 10 to 249 employees", "disqualified"),
    ({"employees": 6}, "size", "outside 10 to 249 employees", "disqualified"),
    ({"industry": "Staffing agencies"}, "industry_off", "industry switched off", "parked"),
    ({"industry": "Underwater basket weaving"}, "no_label", "no Industries label", "disqualified"),
    ({"naics": "561330"}, "partner", "PEO, a partner (NAICS 561330)", "disqualified"),
])
def test_a_failed_check_is_recorded_by_its_code_and_does_what_the_code_says(change, code, text, after):
    ctx, t = make([account(**change)])
    out = verify.run(ctx)
    assert out["verified"] == 0 and out["not_verified"] == {code: 1}
    assert out["not_verified_accounts"] == [{"account_id": "a1", "domain": account(**change)["domain"], "code": code,
                                             "reason": text}]
    a = ctx.store.get("accounts", account_id="a1")
    assert (a["status"], a["status_reason"], a["tier"], a["tier_reason"], a["recheck_after"]) == (
        after, code, "Excluded", f"{code}: {text}", None)
    assert hubspot_requests(t) == []  # HubSpot is asked only once the free checks pass
    again = verify.run(ctx)  # left out for good, or parked while Staffing agencies stays off: not checked again
    assert (again["checked"], again["not_verified"], again["parked"]["back_to_new"]) == (0, {}, 0)
    assert status(ctx) == after


def test_suppressed_and_partner_domains_are_not_verified():
    ctx, _ = make([account("a1"), account("a2")])
    suppression.add(ctx.store, domain="a1co.com", reason="test", source="test", now=NOW)
    ctx.store.insert("partners", [{"domain": "a2co.com", "reason": "broker", "added_at": NOW}])
    assert verify.run(ctx)["not_verified"] == {"suppressed": 1, "partner": 1}
    assert status(ctx, "a1") == status(ctx, "a2") == "disqualified"


@pytest.mark.parametrize("routes, fact, code, reason, recheck", [
    # D22 (the plan's default): a customer, an opt-out and a bounce are for good; an open deal or another owner can
    # end, so they are looked up again after RECHECK_DAYS: NOW is 5 Oct 2026, + 30 days = 4 Nov 2026.
    ({"companies": [{"id": "c1", "properties": {"lifecyclestage": "customer"}}]}, "hubspot_customer",
     "hubspot_customer", "a customer in HubSpot", None),
    ({"companies": [{"id": "c1", "properties": {"hubspot_owner_id": "someone-else"}}]}, "hubspot_other_owner",
     "hubspot_owner", "owned by someone else in HubSpot", date(2026, 11, 4)),
    ({"companies": [{"id": "c1", "properties": {}}], "deals": [{"id": "d1", "properties": {"hs_is_closed": "false"}}]},
     "hubspot_open_deal", "hubspot_open_deal", "an open deal in HubSpot", date(2026, 11, 4)),
    ({"contacts": [{"id": "k1", "properties": {"hs_email_optout": "true"}}]}, "hubspot_opted_out_or_bounced",
     "hubspot_opted_out", verify.OPTED_OUT_REASON, None),
    # The search's second filter group: a contact on the domain who hard-bounced, and never opted out.
    ({"contacts": [{"id": "k2", "properties": {"hs_email_optout": "false"}}]}, "hubspot_opted_out_or_bounced",
     "hubspot_bounced", verify.BOUNCED_REASON, None),
])
def test_hubspot_excludes_a_customer_another_owner_an_open_deal_or_an_opted_out_company(routes, fact, code, reason,
                                                                                         recheck):
    s = settings_with(hubspot_owner_id="owner-harry")
    ctx, t = make([account()], settings=s, **routes)
    out = verify.run(ctx)
    assert out["excluded"] == [{"account_id": "a1", "code": code, "reason": reason}]
    assert out["not_verified"] == {code: 1}
    [e] = ctx.store.select("signal_events", {"account_id": "a1", "source": "hubspot"})
    assert (e["fact"], e["value"]) == (fact, True)  # the fact the rescore reads, whatever the code
    a = ctx.store.get("accounts", account_id="a1")
    assert (a["status"], a["status_reason"], a["tier"], a["recheck_after"]) == ("disqualified", code, "Excluded", recheck)
    assert a["tier_reason"] == f"{code}: {reason}"
    asked = len(hubspot_requests(t))
    assert verify.run(ctx)["checked"] == 0  # not checked again: for good, or not due till 4 Nov
    assert len(hubspot_requests(t)) == asked


def test_a_hubspot_error_leaves_the_account_for_the_next_run():
    """Transient (RS P0-3): a HubSpot 5xx must never leave a company out for good."""
    ctx, t = make([account()])
    t.route("POST", "/crm/v3/objects/companies/search", status=503, body={"message": "down"})
    out = verify.run(ctx)
    assert out["not_verified"] == {"hubspot_check_failed": 1} and out["errors"] and status(ctx) == "new"
    a = ctx.store.get("accounts", account_id="a1")
    assert (a.get("status_reason"), a.get("recheck_after")) == (None, None)
    hubspot_routes(t)  # HubSpot is back: the next run verifies it
    assert verify.run(ctx)["verified"] == 1 and status(ctx) == "verified"


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

    assert cli.main(["run", job], context_factory=factory) == 0
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
    # The morning's order (W1-3a, 19 Oct 2026): apollo_signals reads postings only once verify has passed a company,
    # so before verify it has nothing to read and spends nothing.
    assert apollo_jobs.run(dataclasses.replace(ctx, job=apollo_jobs.JOB))["candidates"] == 0
    out = verify.run(dataclasses.replace(ctx, job=verify.JOB))
    after_verify = {e["value"]["signal"] for e in ctx.store.select("signal_events", {"source": "scoring"})}
    apollo_jobs.run(dataclasses.replace(ctx, job=apollo_jobs.JOB))
    score.rescore(ctx)  # pick_contacts' rescore at 05:30 scores the postings
    # Sure of Fintech, but from Apollo's facts alone (no home page read in this run): its group's copy until the page
    # is read and it is asked again (11 Oct 2026).
    assert out["verified"] == 1 and out["labels"]["checked"] == 1 and out["labels"]["umbrella"] == 1
    [a] = ctx.store.select("accounts")
    assert (a["domain"], a["status"], a["industry"], a["size_band"]) == (
        "company1.com", "verified", "Technology & Startups", "50-99")
    assert (a["label_source"], a["label_confidence"]) == ("umbrella", "high")
    matched = {e["value"]["signal"] for e in ctx.store.select("signal_events", {"source": "scoring"})}
    # apollo_org's funding fact, scored by the rescore (funding split by age, review Appendix A)
    assert matched & {"Recent funding", "Funding 6–12 months ago"}
    jobs = {e["fact"]: e["value"] for e in ctx.store.select("signal_events", {"source": "apollo_jobs"})}
    assert (jobs["open_roles"], jobs["open_people_roles"]) == (3, 1)
    assert "People role open" in matched and "People role open" not in after_verify
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


@pytest.mark.parametrize("change, missing", [
    ({"hq_state": None}, [verify.NO_STATE]),
    ({"employees": None, "size_band": None}, [verify.NO_SIZE]),
    ({"industry": None, "industry_group": None}, [verify.NO_INDUSTRY]),
    ({"hq_state": "", "employees": None, "size_band": None}, [verify.NO_STATE, verify.NO_SIZE]),
])
def test_a_company_with_incomplete_information_is_left_out(change, missing):
    # Harry, 14 Oct 2026: "for any company where we have incomplete information, filter out"
    ctx, t = make([account(**change), account("a2")])
    out = verify.run(ctx)
    reason = "; ".join(missing)
    assert out["not_verified"] == {"incomplete": 1} and status(ctx, "a2") == "verified"
    a1 = ctx.store.get("accounts", account_id="a1")
    assert (a1["status"], a1["tier"], a1["status_reason"], a1["tier_reason"]) == (
        "disqualified", "Excluded", "incomplete", f"incomplete: {reason}")
    assert out["incomplete_accounts"] == [{"account_id": "a1", "domain": "a1co.com", "missing": missing}]
    assert [r for r in hubspot_requests(t) if "a1co.com" in str(r.json)] == []
    assert ctx.store.select("signal_events", {"source": verify.DOUBT_SOURCE}) == []  # nothing for a hand-check
    assert verify.run(ctx)["checked"] == 0  # left out for good: not checked again


@pytest.mark.parametrize("change", [
    {"employees": 49, "size_band": "20-49"},  # near the 50-staff edge
    {"employees": 51},
    {"employees": 11, "size_band": "10-19"},
    {"employees": 248, "size_band": "100-249"},
])
def test_a_borderline_size_is_taken_as_apollo_gives_it(change):
    ctx, _ = make([account(**change)])
    out = verify.run(ctx)
    assert out["verified"] == 1 and out["incomplete"] == 0 and status(ctx) == "verified"
    assert ctx.store.select("signal_events", {"source": verify.DOUBT_SOURCE}) == []


def test_a_count_and_band_that_disagree_are_checked_on_the_count():
    ctx, _ = make([account(employees=64, size_band="20-49"), account("a2", employees=300, size_band="100-249")])
    out = verify.run(ctx)
    assert status(ctx, "a1") == "verified" and status(ctx, "a2") == "disqualified"
    assert out["not_verified"] == {"size": 1}


def test_an_overrides_row_completes_a_company_so_it_is_not_left_out():
    s = settings_with(overrides=(Override("a1co.com", "hq_state", "NY"),))
    ctx, _ = make([account(hq_state=None)], settings=s)
    assert verify.run(ctx)["verified"] == 1 and status(ctx) == "verified"


def test_an_overrides_row_added_later_brings_a_left_out_company_back():
    ctx, _ = make([account(hq_state=None), account("a2", employees=None, size_band=None)])
    verify.run(ctx)
    assert status(ctx, "a1") == status(ctx, "a2") == "disqualified"
    s = settings_with(overrides=(Override("a1co.com", "hq_state", "NY"), Override("a2co.com", "industry", "Fintech")))
    out = verify.run(dataclasses.replace(ctx, settings=s))
    assert out["completed_by_overrides"] == ["a1"]  # a2's row does not supply its missing size
    assert status(ctx, "a1") == "verified" and status(ctx, "a2") == "disqualified"


def test_a_label_the_industries_tab_lacks_is_not_missing_information():
    ctx, _ = make([account(industry="Not a label")])
    assert verify.run(ctx)["not_verified"] == {"no_label": 1} and status(ctx) == "disqualified"
    assert ctx.store.get("accounts", account_id="a1")["status_reason"] == "no_label"  # not incomplete


def test_an_overrides_row_settles_a_doubt():
    s = settings_with(overrides=(Override("a1co.com", "employees", "45"),))
    ctx, _ = make([account(employees=49, size_band="20-49")], settings=s)
    assert verify.run(ctx)["verified"] == 1


# -- the industry label check (labels.py; Harry, 7 Oct 2026: "industry categorisation is critical") --------------------


MAX_TOKENS = "max_tokens"  # LabelSDK: this company's answer stops at max_tokens (an UnusableAnswer)


class LabelSDK:
    """The task model's answers by the company's domain (FakeSDK's shape); a domain with none raises an API error
    (a 500: the service is down), and one answered MAX_TOKENS stops before the JSON is finished (that answer alone)."""

    def __init__(self, answers: dict[str, dict], *, fail: bool = False, status: int = 500):
        self.answers, self.fail, self.status, self.calls = answers, fail, status, []
        self.messages = self

    def create(self, **kwargs):
        import anthropic

        from tests.test_client_claude import sdk_error

        self.calls.append(kwargs)
        domain = next(line.split(": ", 1)[1] for line in kwargs["messages"][0]["content"].splitlines()
                      if line.startswith("Domain: "))
        if self.fail or domain not in self.answers:
            raise sdk_error(anthropic.AuthenticationError if self.status in (401, 403) else anthropic.InternalServerError,
                            self.status)
        if self.answers[domain] == MAX_TOKENS:
            return FakeSDK(text='{"label": "Fin', stop_reason="max_tokens", input_tokens=3900,
                           output_tokens=700).create(**kwargs)
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


def home_page(aid: str, at: datetime, text: str = "Payments software for small businesses.") -> dict:
    return {"event_id": f"home-{aid}-{at.isoformat()}", "account_id": aid, "source": "careers_pages",
            "fact": "home_page", "value": {"title": aid, "text": text}, "quote": "", "source_url": "",
            "observed_at": at}


def labelled(accounts, answers, *, settings=None, facts=None, fail=False, home=True, **routes):
    """verify's world with each account's Apollo facts and the task model answering by domain. home: each account's
    home page already read, as read_pages does before verify_accounts (a sure verdict earns the label's own copy only
    with it, labels.decide)."""
    sdk = LabelSDK(answers, fail=fail)
    t = hubspot_routes(FakeTransport(), **routes)
    ctx = make_context(settings or settings_with(), job=verify.JOB, now=NOW, transport=t, claude_sdk=sdk)
    ctx.store.insert("accounts", accounts)
    for a in accounts:
        ctx.store.insert("signal_events", (facts or {}).get(a["account_id"]) or apollo_facts(a["account_id"]))
        if home:
            ctx.store.insert("signal_events", [home_page(a["account_id"], NOW - timedelta(days=1))])
    return ctx, sdk


def acc(ctx, aid="a1") -> dict:
    return ctx.store.get("accounts", account_id=aid)


def test_agreement_verifies_with_the_labels_own_copy_and_the_verdict_is_asked_once():
    ctx, sdk = labelled([account()], {"a1co.com": {"label": "Fintech", "evidence": "Payments software"}})
    out = verify.run(ctx)
    assert out["verified"] == 1 and out["labels"]["checked"] == 1 and out["labels"]["model"] == 1
    assert out["labels"]["usd"] > 0 and out["labels"]["unavailable_reason"] == ""
    a = acc(ctx)
    assert (a["status"], a["industry"], a["label_source"], a["label_confidence"]) == (
        "verified", "Fintech", "model", "high")
    [call] = sdk.calls
    assert call["model"] == "claude-sonnet-5-5" and "Fintech" not in call["messages"][0]["content"].split("Keywords")[0]
    [fact] = ctx.store.select("signal_events", {"source": "label_check"})
    assert fact["value"]["decision"]["source"] == "model" and fact["value"]["asked"] is True
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
    assert (a["status"], a["tier"], a["status_reason"]) == ("disqualified", "Excluded", "public_body")
    assert a["tier_reason"] == "public_body: a public body, never prospected (the label check: “Payments software”)"
    assert out["not_verified"] == {"public_body": 2}  # one by the model, one by its .gov domain
    assert sdk.domains() == ["a1co.com"] and out["labels"]["disqualified"] == 1
    a2 = acc(ctx, "a2")  # the domain rule: no call, as before, and now left out for good with the same code
    assert (a2["status"], a2["tier_reason"]) == ("disqualified", "public_body: a public body, never prospected")


def test_an_unsure_answer_is_verified_under_the_models_group_whatever_the_rules_say():
    """Harry, 11 Oct 2026: the model's label decides; unsure, its group's own label and copy."""
    ctx, _ = labelled([account()], {"a1co.com": {"label": "Advertising agencies", "confidence": "medium",
                                                 "evidence": "Payments software"}})
    out = verify.run(ctx)
    a = acc(ctx)
    assert out["verified"] == 1 and (a["industry"], a["label_source"]) == ("Marketing & Creative Agencies", "umbrella")
    assert labels.copy_level(a) == "group"
    [fact] = ctx.store.select("signal_events", {"source": "label_check"})
    assert fact["value"]["decision"]["reason"] == "the model says Advertising agencies (medium): the group's copy"


def test_a_doubt_whether_it_is_an_employer_leaves_it_out_with_no_hand_check():
    """14 Oct 2026: a company the model cannot place in our industries is filtered out, not held for a person."""
    ctx, sdk = labelled([account()], {"a1co.com": {"label": "Fintech", "confidence": "low",
                                                   "entity": "association", "evidence": "Payments software"}})
    out = verify.run(ctx)
    assert out["labels"]["disqualified"] == 1 and out["labels"].get("held", 0) == 0
    assert (status(ctx), acc(ctx)["tier"], acc(ctx)["status_reason"]) == ("disqualified", "Excluded", "association")
    assert acc(ctx)["tier_reason"] == "association: probably a membership body or society (the label check, low)"
    assert verify.open_doubts(ctx) == [] and len(sdk.calls) == 1


def test_a_company_held_on_thin_facts_is_asked_again_once_its_home_page_is_read_and_verified():
    """Harry, 7 Oct 2026: the first audit held half the queue, the model unsure on Apollo's facts alone. read_pages
    reads the home page of each (sources/pages.home_pass); the next verify_accounts asks once more with it."""
    thin = {"a1": apollo_facts("a1", naics=(), keywords=("design",), industry="design services", description="")}
    ctx, sdk = labelled([account(industry="")], {"a1co.com": {"label": "Technology & Startups", "confidence": "low"}},
                        facts=thin, home=False)
    out = verify.run(ctx)
    # Unsure, the model's group decides (11 Oct 2026): verified with the group's copy, not held.
    assert out["labels"]["umbrella"] == 1 and status(ctx) == "verified" and verify.open_doubts(ctx) == []
    assert acc(ctx)["label_source"] == "umbrella"
    ctx.store.insert("signal_events", [{
        "event_id": "home-a1", "account_id": "a1", "source": "careers_pages", "fact": "home_page",
        "value": {"title": "Brightline", "text": "Brightline builds scheduling software for clinics."}, "quote": "",
        "source_url": "https://a1co.com/", "observed_at": NOW + timedelta(hours=1)}])
    sdk.answers["a1co.com"] = {"label": "Technology & Startups", "confidence": "high",
                               "evidence": "Brightline builds scheduling software for clinics"}
    ctx.now = NOW + timedelta(days=1)
    verify.run(ctx)
    assert len(sdk.calls) == 2 and "Home page: Brightline" in sdk.calls[-1]["messages"][0]["content"]
    assert status(ctx) == "verified" and verify.open_doubts(ctx) == []
    assert (acc(ctx)["industry"], acc(ctx)["label_source"]) == ("Technology & Startups", "model")
    verify.run(ctx)
    assert len(sdk.calls) == 2  # asked with its page: the verdict stands


def test_with_the_model_unavailable_a_new_account_waits_and_the_next_run_asks_again():
    ctx, sdk = labelled([account()], {}, fail=True)
    out = verify.run(ctx)
    assert out["not_verified"] == {"label_not_checked": 1} and status(ctx) == "new"  # transient: it waits
    assert out["not_verified_accounts"][0]["reason"].startswith("label not checked: Claude did not answer")
    assert acc(ctx).get("status_reason") is None
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


def test_skip_keeps_the_label_when_the_rules_give_none():
    """9 Oct 2026: codes no Industries row matches cleared the label, and the company was held for "Apollo gives no
    industry", which the hand-check cannot clear. relabel keeps the label; so does the check now."""
    facts = apollo_facts("a1", naics=("999999",), keywords=("quantum widgets",), industry="widgets")
    ctx, sdk = labelled([account()], {}, settings=settings_with(label_check="skip"), facts={"a1": facts})
    out = verify.run(ctx)
    a = acc(ctx)
    assert out["verified"] == 1 and sdk.calls == [] and (a["industry"], a["label_source"]) == ("Fintech", "rules")


def test_an_overrides_row_stands_and_nothing_is_asked():
    s = settings_with(overrides=(Override("a1co.com", "industry", "Edtech"),))
    ctx, sdk = labelled([account()], {}, settings=s)
    assert verify.run(ctx)["verified"] == 1 and sdk.calls == []
    a = acc(ctx)
    assert (a["industry"], a["label_source"], labels.copy_level(a)) == ("Edtech", "override", "label")


def test_a_company_with_nothing_to_check_against_is_verified_on_its_label_with_the_groups_copy():
    ctx, sdk = labelled([account()], {}, facts={"a1": [{"event_id": "x", "account_id": "a1", "source": "apollo_org",
                                                        "fact": "employees", "value": 64, "observed_at": NOW}]},
                        home=False)
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
    # Transient (RS P0-3): past the run's share of label calls, a company waits; it is never left out for it.
    assert out["not_verified"] == {"label_not_checked": 1} and acc(ctx, "t1").get("status_reason") is None
    assert out["not_verified_accounts"][0]["reason"] == (
        "label not checked: this run's 1 label checks are used; the next run goes on")
    verify.run(ctx)
    assert sdk.domains() == ["m1co.com", "t1co.com"] and status(ctx, "t1") == "verified"


# -- the queue converges: accounts already verified are checked too (Harry, 7 Oct 2026) ------------------------------


def test_a_verified_account_with_no_verdict_is_checked_and_its_label_put_right():
    ctx, sdk = labelled([account(status="verified", industry="Adtech & martech")],
                        {"a1co.com": {"label": "Fintech", "evidence": "Payments software"}})
    out = verify.run(ctx)
    a = acc(ctx)
    # The model is sure of Fintech, with the home page read: its label (the one it was admitted under was the old
    # tie-break).
    assert (a["status"], a["industry"], a["label_source"]) == ("verified", "Fintech", "model")
    assert out["labels"]["changed"] == [{"account_id": "a1", "domain": "a1co.com", "from": "Adtech & martech",
                                         "to": "Fintech", "source": "model", "action": "verify",
                                         "reason": "the model says Fintech (high)"}]
    assert hubspot_requests_count(ctx) == 0  # nothing else about it is checked again
    verify.run(ctx)
    assert len(sdk.calls) == 1


def test_a_verified_account_the_model_cannot_place_leaves():
    accounts = [account("a1", status="verified"), account("a2", status="verified")]
    ctx, _ = labelled(accounts, {
        "a1co.com": {"label": "none", "confidence": "low", "evidence": "Payments software"},
        "a2co.com": {"label": "none", "confidence": "medium", "entity": "association", "evidence": "Payments software"}})
    out = verify.run(ctx)
    assert [status(ctx, a) for a in ("a1", "a2")] == ["disqualified", "disqualified"]
    assert verify.open_doubts(ctx) == [] and out["labels"]["disqualified"] == 2


def test_required_never_unverifies_a_verified_account_when_the_model_is_unavailable():
    ctx, sdk = labelled([account(status="verified", industry="Adtech & martech")], {}, fail=True)
    out = verify.run(ctx)
    a = acc(ctx)
    assert (a["status"], a["industry"], a.get("label_source")) == ("verified", "Adtech & martech", None)
    assert labels.copy_level(a) == "group"  # the safe default: its group's copy
    assert out["labels"]["verified_unchecked"] == 1 and out["labels"]["unavailable_reason"]


@pytest.mark.parametrize("mode", ["required", "skip"])
def test_an_overrides_row_reaches_an_account_already_verified(mode):
    """9 Oct 2026 (the refactoring scan's defect 5): an Overrides industry row never reached an account the check had
    decided, so its columns, and enrol's copy, kept the old label for good. The next run puts the row in its columns,
    asking nothing, and the run after changes nothing."""
    s = settings_with(label_check=mode, overrides=(Override("a1co.com", "industry", "Edtech"),))
    ctx, sdk = labelled([account(status="verified", label_source="rules+model", label_confidence="high")], {},
                        settings=s)
    out = verify.run(ctx)
    a = acc(ctx)
    assert (a["status"], a["industry"], a["industry_group"], a["label_source"]) == (
        "verified", "Edtech", "Technology & Startups", "override")
    assert sdk.calls == [] and [c["to"] for c in out["labels"]["changed"]] == ["Edtech"]
    assert verify.run(ctx)["labels"]["changed"] == []


def test_an_overrides_row_with_only_the_group_places_the_company_under_the_groups_own_label():
    s = settings_with(overrides=(Override("a1co.com", "industry_group", "Marketing & Creative Agencies"),))
    ctx, sdk = labelled([account(status="verified", label_source="rules+model")], {}, settings=s)
    verify.run(ctx)
    a = acc(ctx)
    assert (a["industry"], a["industry_group"], a["label_source"]) == (
        "Marketing & Creative Agencies", "Marketing & Creative Agencies", "override")
    assert sdk.calls == []


def test_the_reason_codes_contract_splits_every_code_into_one_kind():
    """The W1-C contract (19 Oct 2026; RS P0-3): accounts.status_reason codes, each with what it does to the account,
    defined before the packages that write them. A transient failure (no label call left, a HubSpot error) must never
    be filed as terminal, and an industry switched off is parked, never disqualified for good: those are the two
    mistakes that would lose companies for good (the ~50 of 13 Oct). W1-4 added clay_pending (Clay's answer not in
    yet), which went with Clay (W2-1). W2-2's ICP gate added founded (terminal) and held (the hand-check's hold,
    which only enrol's send checks make: transient, nothing written). 16 terminal + 2 parked + 2 re-checkable + 3
    transient = 23 codes, and every code the gate returns is one of them."""
    by_kind: dict[str, set[str]] = {}
    for code, kind in verify.REASON_CODES.items():
        by_kind.setdefault(kind, set()).add(code)
    assert by_kind == {
        verify.TERMINAL: {"size", "state", "state_unknown", "partner", "suppressed", "public_body", "association",
                          "label_other", "no_label", "incomplete", "hubspot_customer", "hubspot_opted_out",
                          "hubspot_bounced", "hand_check", "declined", "founded"},
        verify.PARKED: {"industry_off", "label_failed"},
        verify.RECHECK: {"hubspot_open_deal", "hubspot_owner"},
        verify.TRANSIENT: {"label_not_checked", "hubspot_check_failed", "held"},
    }
    assert len(verify.REASON_CODES) == 23
    assert icp.CODES <= set(verify.REASON_CODES)
    # Every code the label check and the HubSpot facts produce is in the contract, of the kind it should be.
    assert {verify.REASON_CODES[c] for c in (labels.PUBLIC_BODY_CODE, labels.ASSOCIATION_CODE, labels.OTHER_CODE,
                                              labels.NO_LABEL_CODE)} == {verify.TERMINAL}
    assert verify.REASON_CODES[labels.INDUSTRY_OFF] == verify.REASON_CODES[labels.FAILED_CODE] == verify.PARKED
    assert set(verify.HUBSPOT_CODES) == {f for f, _ in icp.HUBSPOT_EXCLUSIONS}
    assert {f: verify.REASON_CODES[c] for f, c in verify.HUBSPOT_CODES.items()} == {
        "hubspot_customer": verify.TERMINAL, "hubspot_former_customer": verify.TERMINAL,
        "hubspot_open_deal": verify.RECHECK, "hubspot_active_sequence": verify.RECHECK,
        "hubspot_opted_out_or_bounced": verify.TERMINAL, "hubspot_other_owner": verify.RECHECK,
        "hubspot_other_activity_90d": verify.RECHECK}


# -- W1-4: no poison pill, parked industries, re-checked HubSpot exclusions, the lost 50 ----------------------------


FINTECH = {"label": "Fintech", "evidence": "Payments software"}


def failed_facts(ctx, aid="a1") -> list[dict]:
    return ctx.store.select("signal_events", {"account_id": aid, "source": "label_check", "fact": "label_failed"})


def test_one_company_the_model_cannot_finish_is_skipped_not_the_run_and_parked_after_three_runs(monkeypatch):
    """code-verify-labels B1, the poison pill (probe scratchpad/vl/poison.py: 1 call, 0 of 3 verified). a1 sorts
    first (score 90 of 90, 50, 10) and its answer stops at max_tokens; before W1-4 that set Checker.unavailable and
    a2 and a3 waited "label not checked", every run, as a1 sorted first again."""
    accounts = [account("a1", score=90), account("a2", score=50), account("a3", score=10)]
    ctx, sdk = labelled(accounts, {"a1co.com": MAX_TOKENS, "a2co.com": FINTECH, "a3co.com": FINTECH})
    out = verify.run(ctx)
    # 3 calls: a1 (unusable), then a2 and a3, both answered and verified.
    assert sdk.domains() == ["a1co.com", "a2co.com", "a3co.com"]
    assert out["verified"] == 2 and out["labels"]["unavailable_reason"] == ""
    assert out["not_verified"] == {"label_failed": 1} and status(ctx, "a1") == "new"  # 1 failure of 3: it waits
    assert len(failed_facts(ctx)) == 1 and acc(ctx).get("status_reason") is None
    # The next run's order: a1, failed under this label list, goes after the others whatever its score.
    h = labels.labels_hash(ctx.settings)
    retried = {aid for aid, evs in verify._events(ctx, ["a1", "a2", "a3"]).items() if labels.failures(evs, h)}
    assert retried == {"a1"}
    assert [a["account_id"] for a in verify.check_order(accounts, ctx.settings, failed=retried)] == ["a2", "a3", "a1"]
    # Runs 2 and 3, a weekday apart: a1 alone is asked (a2 and a3 are verified on fresh verdicts); its third failure
    # parks it.
    ctx.now = NOW + timedelta(days=1)
    second = verify.run(ctx)
    assert len(sdk.calls) == 4 and second["not_verified"] == {"label_failed": 1} and status(ctx, "a1") == "new"
    ctx.now = NOW + timedelta(days=2)
    third = verify.run(ctx)
    assert len(sdk.calls) == 5 and third["not_verified"] == {"label_failed": 1} and len(failed_facts(ctx)) == 3
    a1 = acc(ctx)
    assert (a1["status"], a1["status_reason"], a1["tier"]) == ("parked", "label_failed", "Excluded")
    assert a1["tier_reason"].startswith("label_failed: the label check could not use 3 answers under this label list")
    # Run 4: parked under the same label list, so not asked again; the others are untouched.
    ctx.now = NOW + timedelta(days=3)
    fourth = verify.run(ctx)
    assert len(sdk.calls) == 5 and fourth["parked"] == {"parked": 1, "back_to_new": 0, "back_accounts": []}
    assert status(ctx, "a2") == status(ctx, "a3") == "verified"
    # A new prompt (a new labels_hash) gives it another chance: back to new and asked once more, now answered.
    monkeypatch.setattr(labels, "PROMPT_VERSION", "2026-10-19-w14")
    sdk.answers["a1co.com"] = FINTECH
    ctx.now = NOW + timedelta(days=4)
    fifth = verify.run(ctx)
    assert fifth["parked"]["back_to_new"] == 1 and "a1co.com" in sdk.domains()[5:] and status(ctx, "a1") == "verified"


@pytest.mark.parametrize("trouble, calls, why", [
    # The service, not the company: nothing more is asked this run and nobody is failed or parked. A 500 is the one
    # call that failed; no key refuses before any call; a 401 (clients/claude raises it as ClaudeError(status=401),
    # never http.AuthError) is the one call that failed, and it says the key was refused, not that Claude was down.
    ("outage", 1, "Claude did not answer"),
    ("no_key", 0, "no Claude API key"),
    ("refused_key", 1, "Claude refused the key"),
])
def test_an_outage_still_stops_the_asking_and_fails_no_company(trouble, calls, why):
    accounts = [account("a1", score=90), account("a2", score=50), account("a3", score=10)]
    ctx, sdk = labelled(accounts, {"a2co.com": FINTECH, "a3co.com": FINTECH} if trouble != "no_key" else {})
    if trouble == "refused_key":
        sdk.fail, sdk.status = True, 401
    if trouble == "no_key":
        ctx.clients.claude_sdk = None
        ctx.clients.secrets = Secrets(ctx.guard, fetch=lambda name: "")
    out = verify.run(ctx)
    assert len(sdk.calls) == calls and out["verified"] == 0
    assert out["labels"]["unavailable_reason"].startswith(why)
    assert out["not_verified"] == {"label_not_checked": 3}  # transient: all three wait
    assert [status(ctx, a) for a in ("a1", "a2", "a3")] == ["new", "new", "new"]
    assert ctx.store.select("signal_events", {"fact": "label_failed"}) == []


def test_label_not_checked_and_a_hubspot_503_change_nothing(monkeypatch):
    """RS P0-3: the two transient failures that go through the same counting must never leave a company out: one
    past the run's share of label calls (here 1: a1, score 90, is asked; a2 waits) and one whose HubSpot search gets
    a 503 (a1, once its label is decided)."""
    monkeypatch.setattr(labels, "MAX_LABEL_CALLS_PER_RUN", 1)
    ctx, sdk = labelled([account("a1", score=90), account("a2", score=10)], {"a1co.com": FINTECH, "a2co.com": FINTECH})
    ctx.clients.transport.route("POST", "/crm/v3/objects/companies/search", status=503, body={"message": "down"})
    out = verify.run(ctx)
    assert sdk.domains() == ["a1co.com"]
    assert out["not_verified"] == {"hubspot_check_failed": 1, "label_not_checked": 1}
    for aid in ("a1", "a2"):
        a = acc(ctx, aid)
        assert (a["status"], a.get("status_reason"), a.get("recheck_after")) == ("new", None, None)


def with_active(settings, *names):
    """The settings with these Industries labels switched on."""
    return dataclasses.replace(settings, industries=tuple(
        dataclasses.replace(i, active=True) if i.industry in names else i for i in settings.industries))


PHYSICIANS = {"label": "Physician practices", "confidence": "high", "evidence": "A family physician practice"}


def physicians(aid: str) -> dict:
    return {**account(aid, industry="Physician practices", industry_group="Healthcare"),
            "domain": f"{aid}co.com"}


def physician_facts(aid: str) -> list[dict]:
    return apollo_facts(aid, naics=("621111",), keywords=("physician practice",), industry="medical practice",
                        description="A family physician practice in Brooklyn.")


def test_a_company_parked_for_an_industry_switched_off_comes_back_when_it_is_switched_on_with_no_call():
    """code-verify-labels B2. Physician practices is off on the default tab, and so is its group's own label
    (Healthcare), while Digital health keeps the group prospected: the model's sure "Physician practices" is a
    company whose industry is switched off. It is parked, not disqualified for good."""
    ctx, sdk = labelled([physicians("a1")], {"a1co.com": PHYSICIANS}, facts={"a1": physician_facts("a1")})
    out = verify.run(ctx)
    a = acc(ctx)
    assert out["not_verified"] == {"industry_off": 1} and len(sdk.calls) == 1
    assert (a["status"], a["status_reason"], a["tier"]) == ("parked", "industry_off", "Excluded")
    assert a["tier_reason"] == "industry_off: its industry (Healthcare) is switched off on the Industries tab (the label check)"
    assert verify.run(ctx)["parked"] == {"parked": 1, "back_to_new": 0, "back_accounts": []}  # still off: stays
    # Switched on. The label list names every label of a prospected group, on or off, so the hash is unchanged and
    # the stored verdict decides again with no call.
    on = with_active(ctx.settings, "Physician practices")
    assert labels.labels_hash(on) == labels.labels_hash(ctx.settings)
    out = verify.run(dataclasses.replace(ctx, settings=on))
    a = acc(ctx)
    assert out["parked"] == {"parked": 1, "back_to_new": 1, "back_accounts": ["a1co.com"]}
    assert out["verified"] == 1 and len(sdk.calls) == 1  # 0 calls this run
    assert (a["status"], a.get("status_reason"), a["industry"], a["label_source"]) == (
        "verified", None, "Physician practices", "model")


def test_fifty_parked_companies_come_back_and_are_verified_with_no_model_call():
    """The plan's 50 (the 13 Oct pattern, after the migration): 50 parked for Physician practices off, then switched
    on. One run: 50 back to new, 50 verified, 0 model calls; HubSpot asked twice each (the company search, then the
    opted-out search, both empty) = 100 requests, and nothing else."""
    ids = [f"p{i:02d}" for i in range(50)]
    ctx, sdk = labelled([physicians(i) for i in ids], {f"{i}co.com": PHYSICIANS for i in ids},
                        facts={i: physician_facts(i) for i in ids})
    first = verify.run(ctx)
    assert first["not_verified"] == {"industry_off": 50} and len(sdk.calls) == 50
    assert {status(ctx, i) for i in ids} == {"parked"}
    asked = hubspot_requests_count(ctx)
    out = verify.run(dataclasses.replace(ctx, settings=with_active(ctx.settings, "Physician practices")))
    assert (out["parked"]["parked"], out["parked"]["back_to_new"], out["verified"]) == (50, 50, 50)
    assert len(sdk.calls) == 50  # none this run
    assert hubspot_requests_count(ctx) - asked == 100
    assert {status(ctx, i) for i in ids} == {"verified"}


def test_a_group_switched_on_changes_the_label_list_so_its_parked_companies_are_asked_again():
    """Professional Services is a group with no label on (one row in the label list); switching its own label on
    lists the group's labels, so the hash changes and the stored verdict is stale: the company comes back to new and
    the label check asks again, within the run's share (1 call)."""
    facts = {"a1": apollo_facts("a1", naics=("541211",), keywords=("accounting",), industry="accounting",
                                description="An accounting firm for restaurants.")}
    answer = {"label": "Professional Services", "confidence": "medium", "evidence": "An accounting firm"}
    ctx, sdk = labelled([account(industry="Professional Services", industry_group="Professional Services")],
                        {"a1co.com": answer}, facts=facts)
    verify.run(ctx)
    assert (status(ctx), acc(ctx)["status_reason"], len(sdk.calls)) == ("parked", "industry_off", 1)
    on = with_active(ctx.settings, "Professional Services")
    assert labels.labels_hash(on) != labels.labels_hash(ctx.settings)
    out = verify.run(dataclasses.replace(ctx, settings=on))
    assert out["parked"]["back_to_new"] == 1 and len(sdk.calls) == 2 and out["verified"] == 1
    assert (acc(ctx)["industry"], acc(ctx)["label_source"]) == ("Professional Services", "umbrella")


def parked_label_failed():
    """a1 alone, its answer stopping at max_tokens on three runs a weekday apart: 1 call a run, 3 label_failed facts
    under today's labels_hash, and the third parks it (labels.FAILED_AFTER)."""
    ctx, sdk = labelled([account("a1", score=90)], {"a1co.com": MAX_TOKENS})
    for day in range(3):
        ctx.now = NOW + timedelta(days=day)
        verify.run(ctx)
    a = acc(ctx)
    assert (a["status"], a["status_reason"], len(sdk.calls), len(failed_facts(ctx))) == ("parked", "label_failed", 3, 3)
    ctx.now = NOW + timedelta(days=3)
    return ctx, sdk


@pytest.mark.parametrize("remedy", ["override", "approver"])
def test_an_overrides_row_or_an_approvers_label_brings_back_a_company_parked_for_failed_answers(remedy):
    """W1-4 review (19 Oct 2026): still_parked asked about label_failed before it asked about an Overrides row, so a
    company parked after 3 unusable answers stayed parked under Harry's own row until the label list changed, which
    may never happen. The documented remedy (docs/pipeline.md) is an Overrides industry row or `labels set` (an
    approver's label): either stands and asks nothing (labels.judge), so it comes back and is verified on the label
    given. Derivation: the labels_hash is unchanged (neither an Overrides row nor label_source is in the label list),
    so without the fix the run reads {"parked": 1, "back_to_new": 0}; with it, 1 back to new, verified on Fintech
    (active on the default tab), and no call: still the 3 of the failed runs."""
    ctx, sdk = parked_label_failed()
    h = labels.labels_hash(ctx.settings)
    if remedy == "override":
        ctx.settings = dataclasses.replace(ctx.settings, overrides=(Override("a1co.com", "industry", "Fintech"),))
    else:
        ctx.store.upsert("accounts", [{"account_id": "a1", "industry": "Fintech", "label_source": labels.APPROVER}])
    assert labels.labels_hash(ctx.settings) == h  # the remedy, not a new label list, brings it back
    out = verify.run(ctx)
    a = acc(ctx)
    assert out["parked"] == {"parked": 1, "back_to_new": 1, "back_accounts": ["a1co.com"]}
    assert out["verified"] == 1 and len(sdk.calls) == 3
    assert (a["status"], a.get("status_reason"), a["industry"], a["label_source"]) == (
        "verified", None, "Fintech", remedy)


def test_an_overrides_row_naming_an_active_label_brings_back_a_company_parked_for_its_industry():
    """The same remedy for industry_off: a1 is parked as Physician practices (off), and Harry's Overrides row files it
    under Digital health (on, the group's prospected label). redecide_parked alone sends it back to new with its tier
    cleared (the end-of-run rescore would re-tier it anyway, so the clearing is asserted here, straight after
    redecide_parked); the run then verifies it on the row's label with no call (still 1, the parking run's)."""
    ctx, sdk = labelled([physicians("a1")], {"a1co.com": PHYSICIANS}, facts={"a1": physician_facts("a1")})
    verify.run(ctx)
    assert (status(ctx), acc(ctx)["status_reason"], len(sdk.calls)) == ("parked", "industry_off", 1)
    ctx.settings = dataclasses.replace(ctx.settings, overrides=(Override("a1co.com", "industry", "Digital health"),))
    assert verify.redecide_parked(ctx) == {"parked": 1, "back_to_new": 1, "back_accounts": ["a1co.com"]}
    a = acc(ctx)
    assert (a["status"], a.get("status_reason"), a.get("tier"), a.get("tier_reason")) == ("new", None, None, None)
    out = verify.run(ctx)
    a = acc(ctx)
    assert out["verified"] == 1 and len(sdk.calls) == 1
    assert (a["status"], a.get("status_reason"), a["industry"], a["label_source"]) == (
        "verified", None, "Digital health", "override")


def thirteen_oct(ctx, aid: str, *, tier_reason: str | None = None, status_reason: str | None = None,
                 entity: str = "company") -> None:
    """An account the label check left out on 13 Oct 2026, before parked existed: disqualified and Excluded with the
    decision's reason as tier_reason and no status_reason, its verdict stored under that day's settings (Professional
    Services off)."""
    s = ctx.settings
    v = labels.Verdict("Professional Services" if entity == "company" else "none", "medium", entity,
                       labels_hash=labels.labels_hash(s))
    d = labels.decide(None, v, s)
    ctx.store.insert("accounts", [{**account(aid), "status": "disqualified", "tier": "Excluded",
                                   "tier_reason": tier_reason or d.reason, "status_reason": status_reason,
                                   "industry": "Professional Services", "industry_group": "Professional Services"}])
    ctx.store.insert("signal_events", [
        *apollo_facts(aid, naics=("541211",), keywords=("accounting",), industry="accounting",
                      description="An accounting firm for restaurants."),
        {"event_id": f"v-{aid}", "account_id": aid, "source": "label_check", "fact": "label_verdict",
         "value": labels.verdict_value(None, v, d, asked=True), "quote": "", "source_url": "",
         "observed_at": NOW - timedelta(days=6)}])


def test_migrate_parked_recovers_the_fifty_lost_on_13_october(capsys):
    """RS P0-3: the ~50 companies the label check left out on 13 Oct while their group was off are disqualified, and
    verify never reads a disqualified account, so redecide_parked alone would never see them. migrate-parked moves
    them to parked; the next run brings back those whose industry is on. Three disqualified accounts must stay: a
    public body (left out for good whatever is switched on), one a person left out at the hand-check (its tier_reason
    is theirs), and one sized out (another check's code)."""
    sdk = LabelSDK({f"p{i:02d}co.com": {"label": "Professional Services", "confidence": "medium",
                                        "evidence": "An accounting firm"} for i in range(50)})
    store = None

    def factory(name, live_flag, operator=False):
        nonlocal store
        t = hubspot_routes(FakeTransport())
        c = make_context(settings_with(), job=name, now=NOW, transport=t, store=store, claude_sdk=sdk,
                         live=bool(live_flag))
        store = c.store
        return c

    ctx = factory("setup", False)
    for i in range(50):
        thirteen_oct(ctx, f"p{i:02d}")
    thirteen_oct(ctx, "pub", entity="public_body")
    thirteen_oct(ctx, "hand", tier_reason="left out at the hand-check (2026-W42)")
    thirteen_oct(ctx, "big", status_reason="size")
    # The dry run (the default) counts 50 of the 53 and moves none.
    assert cli.main(["verify", "migrate-parked"], context_factory=factory) == 0
    printed = capsys.readouterr().out
    assert "Would park 50 of 53 disqualified companies" in printed and "Dry-run: no company was moved." in printed
    assert {a["status"] for a in store.select("accounts")} == {"disqualified"}
    # --live parks the 50, with the code; the three stay as they were.
    out = verify.migrate_parked(factory("verify_migrate_parked", True))
    assert (out["to_park"], out["parked"], out["disqualified"]) == (50, 50, 53)
    parked = store.select("accounts", {"status": "parked"})
    assert len(parked) == 50 and {a["status_reason"] for a in parked} == {"industry_off"}
    # medium counts as sure (labels.decide), so the reason carries no confidence.
    assert parked[0]["tier_reason"] == ("industry_off: its industry (Professional Services) is switched off on the "
                                        "Industries tab (the label check)")
    assert {a["account_id"] for a in store.select("accounts", {"status": "disqualified"})} == {"pub", "hand", "big"}
    # Professional Services still off: the next run keeps them parked, asking nothing.
    ctx = factory(verify.JOB, False)
    first = verify.run(ctx)
    assert (first["parked"]["back_to_new"], first["checked"], len(sdk.calls)) == (0, 0, 0)
    # Switched on (Harry's sheet had all 108 on from 14 Oct): a new label list, so all 50 come back and are asked
    # again, 50 calls within the run's 150, and verified under the group's own label.
    out = verify.run(dataclasses.replace(ctx, settings=with_active(ctx.settings, "Professional Services")))
    assert (out["parked"]["back_to_new"], len(sdk.calls), out["verified"]) == (50, 50, 50)


def six_never_pass(t: FakeTransport, owned: dict[str, bool]) -> None:
    """HubSpot by domain: own1 and own2 owned by someone else while owned[domain]; opt1 and opt2 with a contact who
    opted out; bnc with one who hard-bounced (the search's second group: hs_email_optout false)."""
    def domain_of(req) -> str:
        return req.json["filterGroups"][0]["filters"][0]["value"]

    def companies(req):
        d = domain_of(req)
        return {"results": [{"id": f"c-{d}", "properties": {"hubspot_owner_id": "someone-else"}}]
                if owned.get(d) else []}

    def contacts(req):
        d = domain_of(req)
        optout = {"opt1co.com": "true", "opt2co.com": "true", "bncco.com": "false"}.get(d)
        return {"results": [{"id": f"k-{d}", "properties": {"hs_email_optout": optout}}] if optout else []}

    t.route("POST", "/crm/v3/objects/companies/search", fn=companies)
    t.route("POST", "/crm/v3/objects/contacts/search", fn=contacts)
    t.route("POST", "/crm/v3/objects/deals/search", {"results": []})


def test_the_six_never_pass_accounts_are_settled_once_and_only_the_two_owned_ones_are_looked_up_again():
    """code-verify-labels B7: the 16 and 19 Oct runs checked the same 6 accounts that could never pass (1 outside
    the size range, 5 HubSpot). After one run each has its code; the next run checks none; 31 days on, the two owned
    by someone else are looked up in HubSpot again (D22), one company search each = 2 requests, and nothing else."""
    s = settings_with(hubspot_owner_id="owner-harry")
    t = FakeTransport()
    owned = {"own1co.com": True, "own2co.com": True}
    six_never_pass(t, owned)
    ctx = make_context(s, job=verify.JOB, now=NOW, transport=t)
    ctx.store.insert("accounts", [account("big", employees=300, size_band=None), account("own1"), account("own2"),
                                  account("opt1"), account("opt2"), account("bnc")])
    out = verify.run(ctx)
    assert out["not_verified"] == {"size": 1, "hubspot_owner": 2, "hubspot_opted_out": 2, "hubspot_bounced": 1}
    # NOW is 5 Oct 2026 (UTC): recheck_after = 5 Oct + 30 days = 4 Nov.
    got = {a["account_id"]: (a["status"], a["status_reason"], a["recheck_after"]) for a in ctx.store.select("accounts")}
    assert got == {"big": ("disqualified", "size", None),
                   "own1": ("disqualified", "hubspot_owner", date(2026, 11, 4)),
                   "own2": ("disqualified", "hubspot_owner", date(2026, 11, 4)),
                   "opt1": ("disqualified", "hubspot_opted_out", None),
                   "opt2": ("disqualified", "hubspot_opted_out", None),
                   "bnc": ("disqualified", "hubspot_bounced", None)}
    # HubSpot requests: big none (the size check is free); own1, own2 one each (the company search finds the owner);
    # opt1, opt2, bnc two each (no company, then the opted-out search) = 0 + 2 + 6 = 8.
    assert len(hubspot_requests(t)) == 8
    second = verify.run(ctx)
    assert (second["checked"], second["not_verified"], second["rechecked"]["due"]) == (0, {}, 0)
    assert len(hubspot_requests(t)) == 8
    ctx.now = NOW + timedelta(days=31)  # Thu 5 Nov: own1 and own2 are due (4 Nov <= 5 Nov), nothing else
    third = verify.run(ctx)
    assert third["rechecked"] == {"due": 2, "still_excluded": 2, "back_to_new": 0, "failed": 0}
    assert third["checked"] == 0 and len(hubspot_requests(t)) == 8 + 2
    # Still owned: due again 30 days on, 5 Nov + 30 = 5 Dec.
    assert ctx.store.get("accounts", account_id="own1")["recheck_after"] == date(2026, 12, 5)


def test_a_recheck_that_finds_hubspot_clear_brings_the_company_back_and_the_next_run_verifies_it():
    s = settings_with(hubspot_owner_id="owner-harry")
    t = FakeTransport()
    owned = {"own1co.com": True}
    six_never_pass(t, owned)
    ctx = make_context(s, job=verify.JOB, now=NOW, transport=t)
    ctx.store.insert("accounts", [account("own1")])
    verify.run(ctx)
    assert ctx.store.get("accounts", account_id="own1")["status_reason"] == "hubspot_owner"
    owned["own1co.com"] = False  # the other owner let it go
    ctx.now = NOW + timedelta(days=30)  # 4 Nov: due today
    out = verify.run(ctx)
    a = ctx.store.get("accounts", account_id="own1")
    assert out["rechecked"] == {"due": 1, "still_excluded": 0, "back_to_new": 1, "failed": 0}
    assert (a["status"], a.get("status_reason"), a.get("recheck_after")) == ("new", None, None)
    facts_now = verify.latest_facts(ctx.store.select("signal_events", {"account_id": "own1"}))
    assert facts_now["hubspot_other_owner"] is False  # the stored exclusion is lifted, so check() and the rescore pass
    assert a["tier"] != "Excluded"  # the rescore at the end of the run re-tiered it
    ctx.now = NOW + timedelta(days=31)
    assert verify.run(ctx)["verified"] == 1 and ctx.store.get("accounts", account_id="own1")["status"] == "verified"


def test_a_verified_account_out_of_the_queue_is_never_checked_again():
    """M37 (tests-mutation-targeting): verified accounts tiered Excluded or Held are out of the queue, so the label
    check never spends a call on them."""
    ctx, sdk = labelled([account("x", status="verified", tier="Excluded"), account("h", status="verified", tier="Held")],
                        {"xco.com": FINTECH, "hco.com": FINTECH})
    out = verify.run(ctx)
    assert sdk.calls == [] and out["labels"]["checked"] == 0
    assert ctx.store.select("signal_events", {"source": "label_check"}) == []
    assert status(ctx, "x") == status(ctx, "h") == "verified" and hubspot_requests_count(ctx) == 0


def test_every_reason_in_not_verified_is_a_code():
    """The acceptance line: not_verified is keyed by REASON_CODES only, whatever failed."""
    s = settings_with(hubspot_owner_id="owner-harry")
    ctx, _ = make([account("a1", hq_state="OH"), account("a2", employees=None, size_band=None),
                   account("a3", domain="gmail.com"), account("a4")], settings=s,
                  companies=[{"id": "c1", "properties": {"lifecyclestage": "customer"}}])
    out = verify.run(ctx)
    assert out["not_verified"] == {"state": 1, "incomplete": 1, "suppressed": 1, "hubspot_customer": 1}
    assert set(out["not_verified"]) <= set(verify.REASON_CODES)


def test_the_log_line_cuts_every_list_to_ten_and_the_summary_keeps_them(monkeypatch):
    """W1-1's rule for scheduled runs: one short log line. 12 customers excluded: the summary lists 12, the
    verify_accounts_done line 10."""
    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(verify, "log", lambda event, **kw: logged.append((event, kw)))
    ctx, _ = make([account(f"c{i:02d}") for i in range(12)],
                  companies=[{"id": "c1", "properties": {"lifecyclestage": "customer"}}])
    out = verify.run(ctx)
    assert len(out["excluded"]) == 12 and len(out["not_verified_accounts"]) == 12
    [(_, line)] = [x for x in logged if x[0] == "verify_accounts_done"]
    assert len(line["excluded"]) == 10 and "not_verified_accounts" not in line
    assert line["not_verified"] == {"hubspot_customer": 12}  # counts are kept whole


def test_a_new_label_list_asks_a_parked_company_again_even_while_its_industry_stays_off(monkeypatch):
    """A verdict asked under another labels_hash is stale (a definition or the prompt changed): the parked company
    comes back to new and is asked again, rather than staying parked on an answer to an older question. Asked again
    with Physician practices still off, it is parked again on a fresh verdict, and stays (1 call each time)."""
    ctx, sdk = labelled([physicians("a1")], {"a1co.com": PHYSICIANS}, facts={"a1": physician_facts("a1")})
    verify.run(ctx)
    assert (status(ctx), len(sdk.calls)) == ("parked", 1)
    monkeypatch.setattr(labels, "PROMPT_VERSION", "2026-10-19-w14")
    # Each run on its own weekday, as the schedule runs them: two verdicts written at one instant would be ordered by
    # their random event ids (facts.order), which no production run does.
    ctx.now = NOW + timedelta(days=1)
    out = verify.run(ctx)
    assert out["parked"]["back_to_new"] == 1 and len(sdk.calls) == 2
    assert (status(ctx), acc(ctx)["status_reason"]) == ("parked", "industry_off")
    ctx.now = NOW + timedelta(days=2)
    assert verify.run(ctx)["parked"]["back_to_new"] == 0 and len(sdk.calls) == 2


def test_the_status_column_comment_lists_every_status():
    """The acceptance line: the COMMENT ON accounts.status names every status a reader may meet, parked included
    (ops/accounts_view.STATUSES is the lifecycle list the CLI filters on)."""
    from pathlib import Path

    from us_outbound.ops import accounts_view

    ddl = (Path(__file__).resolve().parents[1] / "sql" / "ddl" / "01_accounts.sql").read_text(encoding="utf-8")
    [line] = [x for x in ddl.splitlines() if x.startswith("COMMENT ON COLUMN us_outbound.accounts.status IS")]
    listed = line.split("One of: ", 1)[1].split(".", 1)[0].split(", ")
    assert sorted(listed) == sorted(accounts_view.STATUSES) and "parked" in listed
