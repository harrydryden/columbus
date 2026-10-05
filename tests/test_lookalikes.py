"""The lookalikes job (sources/lookalikes.py; Harry, 1 Oct 2026): Spill's HubSpot customers into lookalike
cells, kept out of the queue, graded into a lookalike fit by industry, size and growth (Harry, 5 Oct 2026),
scored, reported, and offered to sourcing as priorities.

HubSpot is a FakeTransport that answers the company and deal searches by their filters and pages on
hs_object_id like the real search API; Apollo's organization search answers by domain list and growth range
(FakeApollo); the database is a MemoryStore. Nothing here calls a real service.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound import accounts
from us_outbound.clients.http import ApiError
from us_outbound.ops import cli
from us_outbound.scoring import score
from us_outbound.scoring.score import score_account
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all
from us_outbound.sources import lookalikes as lk

NOW = datetime(2026, 10, 5, 1, 30, tzinfo=UTC)  # Monday 5 Oct 2026, 02:30 UK (the pilot's first day)
SEARCH = "/search"
ASSOC = "/associations/companies"


# -- a fake HubSpot ------------------------------------------------------------------------------------


def _holds(props: dict, f: dict) -> bool:
    have = props.get(f["propertyName"])
    op = f["operator"]
    if op == "EQ":
        return have is not None and str(have) == str(f["value"])
    if op == "IN":
        return have is not None and str(have) in {str(v) for v in f["values"]}
    if op == "HAS_PROPERTY":
        return have not in (None, "")
    if op == "GT":
        return int(have) > int(f["value"])
    raise AssertionError(f"unexpected operator {op}")


class FakeHubSpot:
    """Companies and deals in memory, served the way the CRM search and v4 association APIs serve them."""

    def __init__(self, companies: dict[str, dict], deals: dict[str, tuple[str, str, list[str]]] | None = None):
        self.companies = companies  # id -> properties
        self.deals = deals or {}  # id -> (pipeline, dealstage, company ids)
        self.searches: list[tuple[str, dict]] = []

    def install(self, t: FakeTransport) -> FakeTransport:
        t.route("POST", "/crm/v3/objects/companies/search", fn=lambda req: self._search("companies", req.json))
        t.route("POST", "/crm/v3/objects/deals/search", fn=lambda req: self._search("deals", req.json))
        t.route("GET", ASSOC, fn=self._assoc)
        return t

    def _records(self, obj: str) -> dict[str, dict]:
        if obj == "companies":
            return {i: {**p, "hs_object_id": i} for i, p in self.companies.items()}
        return {i: {"pipeline": p, "dealstage": s, "hs_object_id": i} for i, (p, s, _) in self.deals.items()}

    def _search(self, obj: str, body: dict) -> dict:
        self.searches.append((obj, body))
        records = self._records(obj)
        hits = [i for i, p in records.items()
                if any(all(_holds(p, f) for f in g["filters"]) for g in body["filterGroups"])]
        hits.sort(key=int)
        page = hits[: body["limit"]]
        return {"results": [{"id": i, "properties": {k: records[i].get(k) for k in body["properties"]}} for i in page]}

    def _assoc(self, req) -> dict:
        deal_id = req.url.split("/deals/")[1].split("/")[0]
        return {"results": [{"toObjectId": int(c)} for c in self.deals[deal_id][2]]}


def company(domain, industry, status="Active", *, covered=None, employees=None, country="united kingdom",
            stage="customer", state=None):
    return {"domain": domain, "company_industry": industry, "subscription_status": status,
            "employees_covered": covered, "numberofemployees": employees, "country": country,
            "lifecyclestage": stage, "state": state, "name": "never asked for"}


def customers() -> dict[str, dict]:
    """Twenty-five HubSpot companies covering every rule the job has."""
    c: dict[str, dict] = {}
    n = iter(range(100, 1000))
    for i in range(6):  # Tech 10-49: 6 active UK, 2 active US, 4 churned UK -> active 8, strength 11
        c[str(next(n))] = company(f"tech{i}.co.uk", "Tech", covered=20)
    c[str(next(n))] = company("acme-tech.com", "Tech", covered=25, country="united states")
    c[str(next(n))] = company("beta-tech.com", "Tech", employees=50, country="United States")  # bucket 11-50
    for i in range(4):
        c[str(next(n))] = company(f"oldtech{i}.co.uk", "Tech", "Churned", covered=30)
    for i in range(3):  # Agencies 10-49: 3 active, 1 churned -> under the threshold
        c[str(next(n))] = company(f"studio{i}.co.uk", "Creative Agency", covered=15)
    c[str(next(n))] = company("formerstudio.com", "Creative Agency", "Churned", covered=12, country="united states")
    # A law firm at lifecycle "lead" with an Active subscription: enrol's HubSpot re-check would miss it.
    c[str(next(n))] = company("smithlaw.com", "Law", covered=12, country="united states", stage="lead")
    c[str(next(n))] = company("msp-one.com", "IT Services", covered=30)  # deliberately unmapped
    c[str(next(n))] = company("bigco.com", "Tech", employees=250)  # 51-250: size unknown
    # The same root domain twice, churned and active: counted once, as active.
    c[str(next(n))] = company("www.dupe.io", "Tech", "Churned", covered=40)
    c[str(next(n))] = company("dupe.io", "Tech", covered=40)
    c["950"] = company("gmail.com", "Tech", covered=10)  # a personal domain: counted, never suppressed
    # Lifecycle Churned with no subscription status: a former customer.
    c["960"] = company("gonelaw.com", "Law", None, covered=20, stage="23168024", country="united states")
    # Not customers: neither returned by the company search nor counted.
    c["970"] = company("prospect.com", "Tech", None, covered=30, stage="lead")
    # On a won Spill 3.0 deal but not yet a customer anywhere else: found through the deal.
    c["980"] = company("newwin.com", "Tech", None, covered=35, stage="opportunity", country="united states")
    return c


DEALS = {
    "501": ("pipe-spill3", "154381891", ["980"]),  # Closed won
    "502": ("pipe-spill3", "154381892", ["970"]),  # Closed lost: ignored
    "503": ("other-pipe", "154381891", ["970"]),  # another pipeline: ignored
}


@pytest.fixture
def hub():
    return FakeHubSpot(customers(), dict(DEALS))


@pytest.fixture
def world(default_settings, hub):
    t = hub.install(FakeTransport())
    ctx = make_context(default_settings, transport=t, now=NOW, job="lookalikes")
    seen = NOW - timedelta(days=3)
    ctx.store.upsert("accounts", [
        {"account_id": "acc-fintech", "domain": "fintechprospect.com", "industry": "Fintech",
         "industry_group": "Technology & Startups", "size_band": "20-49", "hq_state": "NY", "status": "queued",
         "first_seen": seen},
        {"account_id": "acc-agency", "domain": "adprospect.com", "industry": "Advertising agencies",
         "industry_group": "Marketing & Creative Agencies", "size_band": "50-99", "hq_state": "NY",
         "status": "queued", "first_seen": seen},
        {"account_id": "acc-customer", "domain": "acme-tech.com", "industry": "Technology & Startups",
         "industry_group": "Technology & Startups", "size_band": "20-49", "hq_state": "NY", "status": "queued",
         "first_seen": seen},
        {"account_id": "acc-former", "domain": "gonelaw.com", "industry": "Legal Teams", "industry_group": "Legal Teams",
         "size_band": "10-19", "hq_state": "NY", "status": "verified", "first_seen": seen},
    ])
    return ctx, t


# -- a fake Apollo: organization search by domains and headcount growth range ---------------------------


# 12-month headcount growth in percent, as Apollo would filter on it. Customers in no band are unknown.
GROWTH = {
    "tech0.co.uk": 12, "tech1.co.uk": 15, "tech2.co.uk": 5, "tech3.co.uk": 0, "tech4.co.uk": 10, "tech5.co.uk": -4,
    "acme-tech.com": 45, "beta-tech.com": 30, "oldtech0.co.uk": 2, "oldtech1.co.uk": 3, "oldtech2.co.uk": 20,
    "studio0.co.uk": 8, "studio1.co.uk": 8, "smithlaw.com": 25, "msp-one.com": 1, "bigco.com": 60, "dupe.io": 18,
    "gonelaw.com": -10, "newwin.com": 35,
    "fintechprospect.com": 22,  # a prospect; adprospect.com has none
}


class FakeApollo:
    """Apollo's organization search, answering by q_organization_domains_list and the headcount growth range
    (inclusive bounds), and its credit balance. ignore_filter: every known domain comes back, whatever the range."""

    def __init__(self, growth=None, *, balance=30_000, ignore_filter=False, saved=()):
        self.growth = dict(GROWTH if growth is None else growth)
        self.balance, self.ignore_filter, self.saved = balance, ignore_filter, set(saved)
        self.asked: list[tuple[dict, list[str]]] = []

    def install(self, t: FakeTransport) -> FakeTransport:
        t.route("POST", "/usage_stats/credit_usage_stats",
                body={"credit_usage_stats": {"lead_credit": {"left_over": self.balance}}})
        t.route("POST", "/mixed_companies/search", fn=self.search)
        return t

    def search(self, req) -> dict:
        body = req.json
        rng, domains = body["organization_headcount_growth_range"], body["q_organization_domains_list"]
        assert body["organization_headcount_growth_past_n_months"] == 12 and body["per_page"] == 100
        self.asked.append((dict(rng), list(domains)))
        lo, hi = rng.get("min", -1e9), rng.get("max", 1e9)
        hits = [d for d in domains if d in self.growth and (self.ignore_filter or lo <= self.growth[d] <= hi)]
        # Companies someone saved in Apollo come back in `accounts`, with `domain` (clients/apollo.organizations_in).
        return {"organizations": [{"id": f"org-{d.split('.')[0]}", "primary_domain": d, "name": "Never stored"}
                                  for d in hits if d not in self.saved],
                "accounts": [{"id": "acct-1", "organization_id": f"org-{d.split('.')[0]}", "domain": d}
                             for d in hits if d in self.saved],
                "pagination": {"page": body["page"], "per_page": 100, "total_entries": len(hits)}}


def install_apollo(t, **kw) -> FakeApollo:
    fake = FakeApollo(**kw)
    fake.install(t)
    return fake


@pytest.fixture
def apollo(world):
    return install_apollo(world[1], saved={"fintechprospect.com"})


def cells(ctx) -> dict[str, dict]:
    return {r["cell_id"]: r for r in ctx.store.select("lookalike_cells")}


def facts(ctx, account_id, source):
    return [e for e in ctx.store.select("signal_events", {"account_id": account_id, "source": source})]


# -- reading HubSpot -----------------------------------------------------------------------------------


def test_reads_company_fields_only_and_writes_nothing_to_hubspot(world, hub):
    ctx, t = world
    lk.run(ctx)
    hubspot = [r for r in t.requests if "api.hubapi.com" in r.url]
    assert hubspot and all(r.method == "GET" and ASSOC in r.url or r.method == "POST" and r.url.endswith(SEARCH)
                           for r in hubspot)
    assert not any("/contacts" in r.url for r in hubspot)  # company level only (Harry, 1 Oct 2026)
    for obj, body in hub.searches:
        allowed = set(lk.COMPANY_FIELDS) if obj == "companies" else {"dealstage"}
        assert set(body["properties"]) <= allowed | {"hs_object_id"}, obj
    assert "name" not in {p for _, body in hub.searches for p in body["properties"]}
    assert ctx.guard.writes("hubspot") == []
    # The customer search: lifecycle customer or Churned, or any subscription status, paged on hs_object_id.
    [first, *_] = [b for o, b in hub.searches if o == "companies"]
    assert [g["filters"][0] for g in first["filterGroups"]] == [
        {"propertyName": "lifecyclestage", "operator": "EQ", "value": "customer"},
        {"propertyName": "lifecyclestage", "operator": "EQ", "value": "23168024"},
        {"propertyName": "subscription_status", "operator": "HAS_PROPERTY"},
    ]
    deal_search = next(b for o, b in hub.searches if o == "deals")
    assert deal_search["filterGroups"][0]["filters"][:2] == [
        {"propertyName": "pipeline", "operator": "EQ", "value": "pipe-spill3"},
        {"propertyName": "dealstage", "operator": "IN", "values": ["1287546140", "154381891", "258214822"]},
    ]


def test_the_spill3_pipeline_id_is_the_default(world, hub):
    ctx, _ = world
    ctx = dataclasses.replace(ctx, settings=dataclasses.replace(
        ctx.settings, general=dataclasses.replace(ctx.settings.general, hubspot_pipeline_id="")))
    lk.read_customers(ctx)
    deal_search = next(b for o, b in hub.searches if o == "deals")
    assert deal_search["filterGroups"][0]["filters"][0]["value"] == "82002613"


def test_who_counts_as_a_customer(world):
    ctx, _ = world
    got = {c.domain: c for c in lk.read_customers(ctx)}
    assert got["smithlaw.com"].status == "active"  # lifecycle lead, subscription Active
    assert got["gonelaw.com"].status == "churned"  # lifecycle Churned
    assert got["newwin.com"].status == "active" and got["newwin.com"].us  # a won Spill 3.0 deal
    assert "prospect.com" not in got  # lost deal, other pipeline, never a customer
    assert got["dupe.io"].status == "active"  # one record per root domain; the active one wins
    assert None in got and got[None].status == "active"  # gmail.com: counted, with no domain to match
    assert len(got) == 25 - 1 - 1  # 25 companies: one was never a customer, one is a duplicate


# -- the cells ---------------------------------------------------------------------------------------------


def test_cells_count_active_and_churned_with_us_weighting(world):
    ctx, _ = world
    summary = lk.run(ctx)
    got = cells(ctx)
    tech = got["Technology & Startups|10-49"]
    # tech0-5, acme-tech, beta-tech (headcount 50 = 11-50), dupe.io, gmail.com, newwin.com: 11 active, 4 US
    assert (tech["active_customers"], tech["churned_customers"], tech["us_active"], tech["us_churned"]) == (11, 4, 3, 0)
    assert tech["strength"] == 8 * 1 + 3 * 2 + 4 * 0.25  # an active customer 1, a churned one 0.25, US double
    assert tech["industry_group"] == "Technology & Startups" and tech["size_band"] == "10-49"
    agencies = got["Marketing & Creative Agencies|10-49"]
    assert (agencies["active_customers"], agencies["churned_customers"], agencies["us_churned"]) == (3, 1, 1)
    assert agencies["strength"] == 3 + 0.5
    assert got["Legal Teams|10-49"]["active_customers"] == 1 and got["Legal Teams|10-49"]["churned_customers"] == 1
    assert got["-|10-49"]["active_customers"] == 1 and got["-|10-49"]["industry_label"] is None  # IT Services
    assert got["Technology & Startups|unknown"]["active_customers"] == 1  # 51-250 spans two bands
    assert all(r["computed_at"] == NOW and r["run_id"] == ctx.run_id for r in got.values())
    assert summary["customers"] == {"active": 17, "churned": 6, "us_active": 4, "us_churned": 2,
                                    "without_domain": 1}
    assert summary["unmapped_industries"] == {"IT Services": 1}


def test_a_second_run_changes_nothing(world):
    ctx, _ = world
    lk.run(ctx)
    before = {k: {c: v for c, v in r.items() if c not in ("computed_at", "run_id")} for k, r in cells(ctx).items()}
    def source_facts():
        return [e for e in ctx.store.tables["signal_events"] if e["source"] != "scoring"]

    written, suppression = source_facts(), list(ctx.store.tables["suppression"])
    second = lk.run(ctx)
    after = {k: {c: v for c, v in r.items() if c not in ("computed_at", "run_id")} for k, r in cells(ctx).items()}
    assert after == before
    assert ctx.store.tables["suppression"] == suppression
    assert second["excluded"]["suppressed"] == 0 and second["excluded"]["accounts_marked"] == 0
    assert second["lookalike_facts"]["written"] == 0 and second["rescored"] is False
    assert source_facts() == written


def test_a_cell_that_disappears_is_removed(world, hub):
    ctx, _ = world
    lk.run(ctx)
    assert "-|10-49" in cells(ctx)
    del hub.companies[next(i for i, p in hub.companies.items() if p["domain"] == "msp-one.com")]
    summary = lk.run(dataclasses.replace(ctx, now=NOW + timedelta(days=7)))
    assert "-|10-49" not in cells(ctx) and summary["removed"] == 1


def test_no_customers_keeps_the_cells(world, hub):
    ctx, _ = world
    lk.run(ctx)
    kept = cells(ctx)
    hub.companies.clear()
    hub.deals.clear()
    with pytest.raises(lk.NoCustomers):
        lk.run(ctx)
    assert cells(ctx) == kept


# -- keeping customers out -------------------------------------------------------------------------------


def test_customers_are_kept_out_of_the_queue_early(world):
    ctx, _ = world
    lk.run(ctx)
    supp = {r["domain"]: r for r in ctx.store.select("suppression")}
    assert supp["acme-tech.com"]["reason"] == "hubspot_customer"
    assert supp["smithlaw.com"]["reason"] == "hubspot_customer"  # the lead with an Active subscription
    assert supp["gonelaw.com"]["reason"] == "hubspot_former_customer"
    assert supp["newwin.com"]["reason"] == "hubspot_customer"
    assert all(r["source"] == "lookalikes" and r["email_sha256"] is None for r in supp.values())
    assert all(r["expires_at"] == NOW + timedelta(days=lk.SUPPRESS_DAYS) for r in supp.values())
    assert "gmail.com" not in supp and "prospect.com" not in supp and "www.dupe.io" not in supp
    # The front door refuses a customer's domain before any Apollo or Clay spend.
    assert accounts.admit(ctx.store, "https://www.smithlaw.com/team", source="apollo", now=NOW).outcome == "suppressed"
    # Accounts already on a customer domain are Excluded at the rescore the job runs.
    customer = ctx.store.get("accounts", account_id="acc-customer")
    former = ctx.store.get("accounts", account_id="acc-former")
    assert (customer["tier"], customer["tier_reason"]) == ("Excluded", "Excluded: a customer in HubSpot")
    assert (former["tier"], former["tier_reason"]) == ("Excluded", "Excluded: a former customer in HubSpot")
    assert ctx.store.get("accounts", account_id="acc-fintech")["tier"] != "Excluded"


def test_each_run_renews_the_suppression_and_never_shortens_it(world):
    ctx, _ = world
    lk.run(ctx)
    later = NOW + timedelta(days=7)
    ctx.store.upsert("suppression", [{"email_sha256": None, "domain": "smithlaw.com", "reason": "unsubscribe",
                                      "source": "instantly", "added_at": NOW, "expires_at": None}])
    lk.run(dataclasses.replace(ctx, now=later))
    supp = {r["domain"]: r for r in ctx.store.select("suppression")}
    assert supp["acme-tech.com"]["expires_at"] == later + timedelta(days=lk.SUPPRESS_DAYS)
    assert supp["acme-tech.com"]["added_at"] == NOW  # the first entry's date is kept
    assert supp["smithlaw.com"]["reason"] == "unsubscribe" and supp["smithlaw.com"]["expires_at"] is None


def test_an_alias_domain_reaches_its_account(world, hub):
    ctx, _ = world
    ctx.store.upsert("domain_aliases", [{"alias": "acmeco.com", "root_domain": "fintechprospect.com",
                                         "source": "redirect", "added_at": NOW}])
    hub.companies["990"] = company("acmeco.com", "Tech", covered=20)
    lk.run(ctx)
    assert ctx.store.get("accounts", account_id="acc-fintech")["tier"] == "Excluded"


# -- the signal ---------------------------------------------------------------------------------------------


FINTECH_QUOTE = ("Spill's customers: Technology & Startups is the strongest industry (1.00); 10–49 staff is its most "
                 "common size (1.00); growing 10–30% a year like 32% of them (0.89). Fit 98.")


def latest_facts(ctx, account_id, source="lookalike") -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in sorted(facts(ctx, account_id, source), key=lambda e: e["observed_at"]):
        out[e["fact"]] = e
    return out


def test_the_fit_scores_an_account_close_to_spills_customers(world, apollo):
    ctx, _ = world
    lk.run(ctx)
    got = latest_facts(ctx, "acc-fintech")
    assert {f: e["value"] for f, e in got.items()} == {
        "lookalike_fit": 98, "lookalike_industry_fit": 1.0, "lookalike_size_fit": 1.0, "lookalike_growth_fit": 0.89,
        "lookalike_active": 11, "lookalike_strength": 15.0}
    assert got["lookalike_fit"]["quote"] == FINTECH_QUOTE  # aggregate numbers only: no customer's name or domain
    assert got["lookalike_active"]["quote"] == ("Spill's HubSpot customers in Technology & Startups at 10-49 staff: "
                                                "11 active and 4 churned; 3 of the active ones are in the US.")
    a = ctx.store.get("accounts", account_id="acc-fintech")
    r = score_account(a, ctx.store.select("signal_events", {"account_id": "acc-fintech"}), ctx.settings, NOW.date())
    matched = {m.signal.signal: m for m in r.matches}
    assert matched["Close match to Spill's customers"].weight_applied == 15
    assert matched["Close match to Spill's customers"].evidence[0].text == FINTECH_QUOTE
    assert "Some match to Spill's customers" not in matched and "Looks like Spill's customers" not in matched  # off
    # Agencies at 50-99: a weak industry, no customer of that size and growth unknown, so a low fit.
    agency = latest_facts(ctx, "acc-agency")
    assert agency["lookalike_fit"]["value"] == 13 and agency["lookalike_growth_fit"]["value"] is None
    assert agency["lookalike_fit"]["quote"] == (
        "Spill's customers: Marketing & Creative Agencies is the 2nd strongest industry (0.23); none at 50–99 staff "
        "overall, as Marketing & Creative Agencies has under 5 active (0.00); its growth unknown, so left out. Fit 13.")


def test_a_thin_group_reads_the_size_mix_of_all_customers(world):
    ctx, _ = world
    lk.run(ctx)
    ctx.store.upsert("accounts", [{"account_id": "acc-studio", "domain": "newstudio.com",
                                   "industry": "Advertising agencies", "industry_group": "Marketing & Creative Agencies",
                                   "size_band": "10-19", "hq_state": "NY", "status": "queued", "first_seen": NOW}])
    lk.apply(ctx)
    got = {f: e["value"] for f, e in latest_facts(ctx, "acc-studio").items()}
    # 3 active agencies at 10 to 249 staff: under SMALL_GROUP, so 10-49 is read against every customer's sizes.
    assert got == {"lookalike_active": 3, "lookalike_strength": 3.5, "lookalike_industry_fit": 0.23,
                   "lookalike_size_fit": 1.0, "lookalike_growth_fit": None, "lookalike_fit": 57}
    assert "10–49 staff is their most common size overall, as Marketing & Creative Agencies has under 5 active" in (
        latest_facts(ctx, "acc-studio")["lookalike_fit"]["quote"])
    a = ctx.store.get("accounts", account_id="acc-studio")
    r = score_account(a, ctx.store.select("signal_events", {"account_id": "acc-studio"}), ctx.settings, NOW.date())
    assert {m.signal.signal: m.weight_applied for m in r.matches if m.signal.sources == ("lookalike",)} == {
        "Some match to Spill's customers": 8}


def test_apply_writes_only_changes(world, apollo):
    ctx, _ = world
    lk.run(ctx)
    assert lk.apply(ctx)["written"] == 0
    assert lk.apply(dataclasses.replace(ctx, now=NOW + timedelta(days=lk.REFRESH_DAYS)))["written"] == 4  # refreshed
    later = dataclasses.replace(ctx, now=NOW + timedelta(days=lk.REFRESH_DAYS + 1))
    assert lk.apply(later)["written"] == 0
    # A new growth figure for the agency changes its fit, and only its facts are written.
    ctx.store.insert("signal_events", [{"event_id": "g1", "account_id": "acc-agency", "source": "apollo_org",
                                        "fact": "headcount_growth_12m", "value": 0.05, "quote": "",
                                        "source_url": "", "observed_at": later.now}])
    assert lk.apply(later)["written"] == 1
    assert latest_facts(later, "acc-agency")["lookalike_growth_fit"]["value"] == 0.79  # flat: 5.5 of fast's 7
    # Tech's cell goes: the fintech's industry and size parts fall, and its cell facts are 0.
    ctx.store.delete("lookalike_cells", {"cell_id": "Technology & Startups|10-49"})
    assert lk.apply(later)["written"] >= 1
    got = latest_facts(later, "acc-fintech")
    assert got["lookalike_industry_fit"]["value"] == 0.0 and got["lookalike_active"]["value"] == 0
    assert got["lookalike_active"]["quote"].startswith("No Spill customers counted in Technology & Startups")


def test_a_fit_that_falls_to_nothing_is_written_once(world):
    ctx, _ = world
    lk.run(ctx)
    hotel = {"account_id": "acc-hotel", "domain": "hotel.com", "industry": "Hotels", "industry_group": "Hospitality",
             "size_band": None, "hq_state": "NY", "status": "queued", "first_seen": NOW}
    ctx.store.upsert("accounts", [hotel])
    lk.apply(ctx)
    assert facts(ctx, "acc-hotel", "lookalike") == []  # no customers in Hospitality, size unknown: nothing scores
    ctx.store.insert("signal_events", [{"event_id": "old-fit", "account_id": "acc-hotel", "source": "lookalike",
                                        "fact": "lookalike_fit", "value": 60, "quote": "", "source_url": "",
                                        "observed_at": NOW - timedelta(days=1)}])
    assert lk.apply(ctx)["written"] == 1
    assert latest_facts(ctx, "acc-hotel")["lookalike_fit"]["value"] == 0
    n = len(facts(ctx, "acc-hotel", "lookalike"))
    lk.apply(dataclasses.replace(ctx, now=NOW + timedelta(days=400)))
    assert len(facts(ctx, "acc-hotel", "lookalike")) == n  # zeros are not written again


def test_apply_before_the_first_run_does_nothing(ctx):
    assert lk.apply(ctx) == {"cells": 0, "accounts": 0, "written": 0}


def test_settings_sync_writes_the_facts_before_its_rescore(world, monkeypatch):
    ctx, _ = world
    lk.run(ctx)
    from us_outbound.settings import sync

    ctx.store.upsert("accounts", [{"account_id": "acc-new", "domain": "newedtech.com", "industry": "Edtech",
                                   "industry_group": "Technology & Startups", "employees": 30, "hq_state": "NY",
                                   "status": "new", "first_seen": NOW}])
    assert sync._lookalikes(ctx)["written"] == 1  # employees 30 reads as 10-49 when size_band is not set yet
    assert {e["fact"] for e in facts(ctx, "acc-new", "lookalike")} == {
        "lookalike_active", "lookalike_strength", "lookalike_fit", "lookalike_industry_fit", "lookalike_size_fit",
        "lookalike_growth_fit"}
    assert ctx.guard.writes("hubspot") == [] and ctx.guard.writes("apollo") == []


def test_open_statuses_are_the_scored_ones():
    assert lk.OPEN_STATUSES == score.SCORED_STATUSES


# -- mapping one company --------------------------------------------------------------------------------------


@pytest.mark.parametrize("props, want", [
    ({"employees_covered": 12, "numberofemployees": 250}, "10-49"),  # covered staff first
    ({"employees_covered": 20.44}, "10-49"),
    ({"employees_covered": 0, "numberofemployees": 50}, "10-49"),  # 11-50
    ({"numberofemployees": 10}, "1-9"),  # 1-10
    ({"numberofemployees": 250}, "unknown"),  # 51-250 spans two bands
    ({"numberofemployees": 1000}, "250+"),
    ({"numberofemployees": 72}, "50-99"),  # an exact count
    ({"numberofemployees": "180"}, "100-249"),
    ({}, "unknown"),
])
def test_customer_band(props, want):
    assert lk.customer_band(props) == want


def test_customer_label(default_settings):
    s = default_settings
    assert lk.customer_label({"company_industry": "Tech"}, s) == "Technology & Startups"
    assert lk.customer_label({"company_industry": "Law"}, s) == "Legal Teams"
    assert lk.customer_label({"company_industry": "Creative Agency"}, s) == "Marketing & Creative Agencies"
    assert lk.customer_label({"industry": "COMPUTER_GAMES"}, s) == "Games studios"
    assert lk.customer_label({"company_industry": "IT Services"}, s) == ""  # tech rows exclude IT services
    assert lk.customer_label({"company_industry": "Other"}, s) == ""
    # HubSpot's standard industry values, read when company_industry is blank.
    assert lk.customer_label({"industry": "COMPUTER_SOFTWARE"}, s) == "Technology & Startups"
    assert lk.customer_label({"industry": "MARKETING_AND_ADVERTISING"}, s) == "Marketing & Creative Agencies"
    assert lk.customer_label({"industry": "INFORMATION_TECHNOLOGY_AND_SERVICES"}, s) == ""  # IT services stays out
    # The pipeline's label for the same domain (NAICS and Apollo keywords) wins over HubSpot's coarse field.
    assert lk.customer_label({"company_industry": "Tech"}, s, account_label="Fintech") == "Fintech"
    # Text HubSpot's map does not know is matched against the Industries tab.
    assert lk.customer_label({"industry": "Restaurants"}, s) == "Restaurants"
    assert lk.customer_label({"industry": "Indie video games publisher"}, s) == "Games studios"
    assert lk.customer_label({"industry": "Bricks"}, s) == ""
    # Every mapped label is on the Industries tab, so a renamed label shows up here.
    assert all(s.industry(label) for label in lk.HUBSPOT_INDUSTRY_LABELS.values() if label)


def test_is_us():
    assert lk.is_us({"country": "united states"}) and lk.is_us({"country": "USA"})
    assert not lk.is_us({"country": "united kingdom", "state": "NY"})
    assert lk.is_us({"country": "", "state": "ny"}) and not lk.is_us({"state": "London"})


# -- for sourcing and for Harry --------------------------------------------------------------------------------


def test_lookalike_priorities(world):
    ctx, _ = world
    assert set(lk.lookalike_priorities(ctx.settings, ctx.store).values()) == {0.0}  # before the first run
    lk.run(ctx)
    p = lk.lookalike_priorities(ctx.settings, ctx.store)
    assert set(p) == {i.industry for i in ctx.settings.industries}
    assert p["Fintech"] == p["Technology & Startups"] == p["Games studios"] == 1.0  # by group
    assert p["Advertising agencies"] == round(3.5 / 15.0, 3)
    assert p["Legal Teams"] == round((2 + 0.5) / 15.0, 3)  # smithlaw (US, active) and gonelaw (US, churned)
    assert p["Restaurants"] == 0.0


def test_report_lists_the_top_cells(world):
    ctx, _ = world
    assert "No lookalike cells yet" in lk.report(ctx.settings, ctx.store)[0]
    lk.run(ctx)
    lines = lk.report(ctx.settings, ctx.store, top=3)
    text = "\n".join(lines)
    assert "computed Mon 05 Oct 2026 02:30 UK" in lines[0] and "Top 3 of" in lines[1]
    row = next(line for line in lines if line.strip().startswith("Technology & Startups") and "10-49" in line)
    assert row.split()[-4:] == ["11", "4", "20%", "15.00"]  # active, churned, US share, strength
    assert "(no website label)" not in text  # only 10 to 249 staff by default, top 3
    assert "(no website label)" in "\n".join(lk.report(ctx.settings, ctx.store, top=50, all_bands=True))
    group = next(line for line in lines if line.strip().startswith("Technology & Startups") and "1.00" in line)
    assert group.split()[-4:] == ["11", "4", "20%", "1.00"]  # no per-cell column: the graded rows read the fit
    assert "Signal \"Close match to Spill's customers\" (+15): lookalike_fit >= 70." in text
    assert "Signal \"Some match to Spill's customers\" (+8): lookalike_fit >= 45 AND lookalike_fit < 70." in text
    assert ("Signal \"Looks like Spill's customers\" (+4, inactive on the Signals tab): lookalike_active >= 5 AND "
            "lookalike_strength >= 10.") in text
    # Nothing from Apollo in these fixtures: every customer's growth is unknown.
    assert "Customers' 12-month headcount growth (Apollo, Mon 05 Oct 2026 02:30 UK): shrinking 0, flat 0, " \
           "growing 0, fast 0; unknown 23 (0 of 23 known)." in text
    # A sheet still on the old row: the per-cell column says where it fires.
    old = dataclasses.replace(ctx.settings, signals=tuple(
        dataclasses.replace(s, active=True) if s.signal == "Looks like Spill's customers" else s
        for s in ctx.settings.signals if not s.signal.endswith("match to Spill's customers")))
    old_lines = lk.report(old, ctx.store, top=3)
    tech = next(line for line in old_lines if line.strip().startswith("Technology & Startups") and "1.00" in line)
    assert tech.rstrip().endswith("10-49")


def test_lookalikes_show_from_the_command_line(world, capsys):
    ctx, _ = world
    lk.run(ctx)
    assert cli.main(["lookalikes", "show", "--top", "2"], context_factory=lambda job, live, **kw: ctx) == 0
    out = capsys.readouterr().out
    assert "Lookalike cells from Spill's HubSpot customers" in out and "Top 2 of" in out
    assert cli.main(["lookalikes", "show", "--top", "0"], context_factory=lambda job, live, **kw: ctx) == 2


def test_the_job_is_registered_monthly_before_source_universe():
    from us_outbound.ops import heartbeat, schedule

    assert cli.JOBS["lookalikes"] == "us_outbound.sources.lookalikes:run"
    job = schedule.by_name()["lookalikes"]
    # Harry, 5 Oct 2026: the 1st of each month at 02:30 UK; HubSpot and Apollo reads and database writes only.
    assert (job.cron, job.enabled, job.live) == ("30 2 1 * *", True, False)
    assert schedule.by_name()["source_universe"].cron.split()[1] == "3"  # 03:00, after it
    assert heartbeat.EXPECTED["lookalikes"] == 32 * 24 * 60  # the longest month, and a day's grace
    # A customer's suppression outlives one missed monthly run; an unchanged fit is rewritten inside its signals' days.
    assert lk.SUPPRESS_DAYS > 2 * 31
    settings, _ = validate_all(default_tabs())
    rows = [s for s in settings.signals if s.sources == ("lookalike",)]
    assert rows and all(lk.REFRESH_DAYS < s.counts_for_days for s in rows)


# -- the lookalike fit -------------------------------------------------------------------------------------


def cell(label, group, band, active, strength) -> dict:
    return {"cell_id": f"{label}|{band}", "industry_label": label, "industry_group": group, "size_band": band,
            "active_customers": active, "churned_customers": 0, "us_active": 0, "us_churned": 0, "strength": strength}


def grown(group, band, n, strength) -> dict:
    return {"cell_id": f"{group or '-'}|{band}", "industry_group": group, "growth_band": band,
            "active_customers": n, "churned_customers": 0, "strength": strength}


TECH, AGENCIES, LEGAL = "Technology & Startups", "Marketing & Creative Agencies", "Legal Teams"
MODEL = lk.FitModel(
    [cell(TECH, TECH, "10-49", 20, 30.0), cell(TECH, TECH, "50-99", 8, 15.0), cell("Games studios", TECH, "10-49", 2, 3.0),
     cell(AGENCIES, AGENCIES, "10-49", 10, 12.0), cell(LEGAL, LEGAL, "10-49", 3, 6.0), cell(LEGAL, LEGAL, "100-249", 1, 2.0),
     cell(TECH, TECH, "unknown", 9, 10.0), cell(TECH, TECH, "250+", 4, 40.0)],  # outside 10 to 249: not read
    [grown(TECH, "flat", 10, 12.0), grown(TECH, "growing", 8, 10.0), grown(TECH, "fast", 2, 3.0),
     grown(TECH, "unknown", 5, 6.0), grown(AGENCIES, "flat", 4, 4.0), grown(None, "shrinking", 1, 1.0)],
)


def test_the_fit_weighs_industry_size_and_growth():
    f = MODEL.fit("Fintech", TECH, "10-49", "growing")
    # Tech is 20 customers with a known band, so its own growth mix: growing 10 of flat's 12.
    assert (f.industry, f.size, f.growth) == (1.0, 1.0, 0.83)
    assert f.fit == round(100 * (0.45 * 1.0 + 0.35 * 1.0 + 0.20 * 0.83)) == 97
    assert f.quote == ("Spill's customers: Technology & Startups is the strongest industry (1.00); 10–49 staff is its "
                       "most common size (1.00); growing 10–30% a year like 40% of its customers (0.83). Fit 97.")
    assert (lk.INDUSTRY_WEIGHT, lk.SIZE_WEIGHT, lk.GROWTH_WEIGHT) == (0.45, 0.35, 0.20)


def test_unknown_growth_is_left_out_and_the_weights_renormalised():
    f = MODEL.fit("Fintech", TECH, "50-99", None)
    assert (f.industry, f.size, f.growth) == (1.0, 0.45, None)  # 50-99 is 15 of 10-49's 33 in Tech
    assert f.fit == round(100 * (0.45 * 1.0 + 0.35 * 0.45) / 0.80) == 76
    assert f.quote.endswith("50–99 staff is its 2nd most common size (0.45); its growth unknown, so left out. Fit 76.")
    # Unknown never costs points: the same account with a growth band no customer has scores lower than unknown.
    assert MODEL.fit("Fintech", TECH, "50-99", "shrinking").fit < f.fit
    # Size unknown too: industry alone.
    assert MODEL.fit("Fintech", TECH, None, None).fit == 100


def test_a_small_group_reads_every_customers_size_mix_and_growth():
    f = MODEL.fit(LEGAL, LEGAL, "10-49", "flat")
    # Legal: 8 of Tech's 48 at 10 to 249 staff; 4 active there, under SMALL_GROUP, so every customer's sizes:
    # 10-49 is 51 (30 + 3 + 12 + 6). Legal has no growth counts, so every customer's: flat is 14 of the 25 with a
    # known band, and the strongest band (16).
    assert (f.industry, f.size, f.growth) == (0.17, 1.0, 1.0)
    assert f.fit == round(100 * (0.45 * 0.17 + 0.35 + 0.20)) == 63
    assert f.quote == ("Spill's customers: Legal Teams is the 3rd strongest industry (0.17); 10–49 staff is their most "
                       "common size overall, as Legal Teams has under 5 active (1.00); flat (0–10% a year) like 56% "
                       "of them (1.00). Fit 63.")


def test_a_label_spills_hubspot_field_names_apart_is_read_by_its_own_customers():
    games = MODEL.fit("Games studios", TECH, "50-99", None)
    assert games.industry == round(3.0 / 48.0, 2) == 0.06  # Games studios' own 3, not Tech's 48
    assert games.quote.startswith("Spill's customers: Games studios is the 4th strongest industry (0.06); 50–99 staff "
                                  "is the 2nd most common size in Technology & Startups (0.45)")
    assert lk.fit_industry("Games studios", TECH) == "Games studios"
    assert lk.fit_industry("Fintech", TECH) == TECH  # HubSpot cannot tell fintech from other tech
    assert lk.fit_industry(TECH, TECH) == TECH  # the group's umbrella label reads as the group
    assert lk.fit_industry("Creative & design agencies", AGENCIES) == AGENCIES  # HubSpot's standard field only
    none = MODEL.fit("Restaurants", "Hospitality", "10-49", None)
    assert none.industry == 0.0 and none.quote.startswith("Spill's customers: none in Hospitality (0.00)")


def test_growth_band_of_a_12_month_figure():
    assert [lk.growth_band_of(v) for v in (-0.2, 0, 0.099, 0.1, 0.29, 0.3, 1.5, None, "x")] == [
        "shrinking", "flat", "flat", "growing", "growing", "fast", "fast", None, None]


# -- the growth search ---------------------------------------------------------------------------------------


def ledger(ctx) -> list[dict]:
    return ctx.store.select("credit_ledger", {"job": "lookalikes"})


def test_growth_bands_from_apollo_search_by_domains_and_growth_range(world, apollo):
    ctx, t = world
    summary = lk.run(ctx)
    # One search per band for the 22 customer domains (one chunk), fastest band first, each over the domains no
    # earlier band placed; then the two queue accounts not on a customer's domain.
    ranges = [rng for rng, _ in apollo.asked]
    assert ranges == [{"min": 30}, {"min": 10, "max": 30}, {"min": 0, "max": 10}, {"min": -100, "max": 0}] * 2
    customers = [d for _, d in apollo.asked[:4]]
    assert len(customers[0]) == 22 and len(customers[1]) == 22 - 4 and "acme-tech.com" not in customers[1]
    assert sorted(apollo.asked[4][1]) == ["adprospect.com", "fintechprospect.com"]
    body = next(r.json for r in t.requests if r.url.endswith("/mixed_companies/search"))
    assert body["organization_headcount_growth_past_n_months"] == 12 and body["page"] == 1
    # Boundaries go to the higher band: 30% is fast, 10% growing, 0% flat.
    rows = {r["cell_id"]: r for r in ctx.store.select("lookalike_growth")}
    assert rows["Technology & Startups|fast"]["active_customers"] == 4  # acme, beta (30%), bigco, newwin
    assert rows["Technology & Startups|growing"]["active_customers"] == 4  # tech0, tech1, tech4 (10%), dupe.io
    assert rows["Technology & Startups|growing"]["churned_customers"] == 1
    assert rows["Technology & Startups|flat"]["active_customers"] == 2  # tech2, tech3 (0%)
    assert rows["Technology & Startups|unknown"]["strength"] == 1.25  # gmail.com (no domain), oldtech3
    assert rows["Legal Teams|shrinking"]["churned_customers"] == 1 and rows["-|flat"]["active_customers"] == 1
    # Counts only: no customer domain or name anywhere in the table, the ledger or the facts' quotes.
    stored = repr(ctx.store.select("lookalike_growth")) + repr(ledger(ctx))
    assert not any(d in stored for d in GROWTH) and "Never stored" not in stored
    # Accounts: a band fact each (source apollo_org), found in the `accounts` bucket by its domain, or unknown.
    fin = latest_facts(ctx, "acc-fintech", "apollo_org")["headcount_growth_band"]
    assert (fin["value"], fin["source_url"]) == ("growing", "https://app.apollo.io/#/organizations/org-fintechprospect")
    assert fin["quote"] == "Apollo search by 12-month headcount growth: growing 10–30% a year"
    agency = latest_facts(ctx, "acc-agency", "apollo_org")["headcount_growth_band"]
    assert (agency["value"], agency["source_url"]) == ("unknown", "")  # not searched again for GROWTH_REFRESH_DAYS
    assert facts(ctx, "acc-customer", "apollo_org") == []  # a customer's domain: searched as a customer only
    g = summary["growth"]
    assert g["customers"] == {"domains": 22, "stored": True, "placed": 19, "unknown": 3, "cells": 10, "removed": 0,
                              "bands": {"fast": 4, "growing": 6, "flat": 7, "shrinking": 2}}
    assert (g["accounts"]["searched"], g["accounts"]["placed"], g["accounts"]["left_for_next_run"]) == (2, 1, 0)
    # 1 credit per page with results, 0 for an empty one, each page in credit_ledger as it is read.
    assert g["credits"] == 5.0 and g["pages"] == 8
    assert [e["credits"] for e in ledger(ctx)] == [1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0.0, 0.0]
    assert all(json.loads(e["note"])["for"] in ("customers", "accounts") for e in ledger(ctx))
    # Dry-run, as the other Apollo sources: the searches are reads, so they happen and their credits count.
    assert ctx.dry_run and ctx.guard.writes("apollo") == []


def test_a_fresh_growth_figure_wins_and_the_search_fills_gaps(world, apollo):
    ctx, _ = world
    ctx.store.insert("signal_events", [
        {"event_id": "g-agency", "account_id": "acc-agency", "source": "apollo_org", "fact": "headcount_growth_12m",
         "value": 0.31, "quote": "", "source_url": "", "observed_at": NOW - timedelta(days=10)},
        {"event_id": "b-fin", "account_id": "acc-fintech", "source": "apollo_org", "fact": "headcount_growth_band",
         "value": "flat", "quote": "", "source_url": "", "observed_at": NOW - timedelta(days=30)},
    ])
    summary = lk.run(ctx)
    assert len(apollo.asked) == 4  # the customers only: neither account is due a search
    band = latest_facts(ctx, "acc-agency", "apollo_org")["headcount_growth_band"]
    assert (band["value"], band["quote"]) == ("fast", "Apollo: headcount +31% over 12 months, so growing 30% or more a year")
    assert summary["growth"]["accounts"] == {"from_growth_figures": 1, "due_a_search": 0, "band_facts": 1,
                                             "left_for_next_run": 0}
    assert latest_facts(ctx, "acc-fintech", "lookalike")["lookalike_growth_fit"]["value"] == round(5.5 / 7, 2)
    # After GROWTH_REFRESH_DAYS the band is searched again; the figure, now old, no longer stands in.
    later = dataclasses.replace(ctx, now=NOW + timedelta(days=lk.GROWTH_REFRESH_DAYS))
    figures, todo = lk.growth_todo(later, [])
    assert figures == [] and [a["account_id"] for a in todo] == ["acc-fintech", "acc-agency"]  # Tech first (Focus)


def test_the_growth_searches_are_capped_and_customers_are_all_or_nothing(world, monkeypatch):
    ctx, t = world
    fake = install_apollo(t)
    monkeypatch.setattr(lk, "LOOKALIKE_GROWTH_CREDITS", 4)  # the customers' worst case exactly
    g = lk.run(ctx)["growth"]
    assert g["customers"]["stored"] is True and g["credits"] == 4.0
    assert g["accounts"] == {"from_growth_figures": 0, "due_a_search": 2, "band_facts": 0, "left_for_next_run": 2}
    assert len(fake.asked) == 4 and sum(e["credits"] for e in ledger(ctx)) == 4.0
    # Under the customers' worst case: their stored counts are kept, and the accounts wait too.
    before = ctx.store.select("lookalike_growth")
    monkeypatch.setattr(lk, "LOOKALIKE_GROWTH_CREDITS", 3)
    g = lk.run(dataclasses.replace(ctx, now=NOW + timedelta(days=31)))["growth"]
    assert g["customers"]["stored"] is False and "stored growth counts are kept" in g["customers"]["kept"]
    assert len(fake.asked) == 4 and ctx.store.select("lookalike_growth") == before


def test_no_growth_search_below_apollo_floor_but_figures_still_count(world):
    ctx, t = world
    fake = install_apollo(t, balance=100)  # apollo_floor is 5,000
    ctx.store.insert("signal_events", [{"event_id": "g-agency", "account_id": "acc-agency", "source": "apollo_org",
                                        "fact": "headcount_growth_12m", "value": -0.05, "quote": "", "source_url": "",
                                        "observed_at": NOW - timedelta(days=3)}])
    g = lk.run(ctx)["growth"]
    assert fake.asked == [] and ledger(ctx) == [] and "below apollo_floor" in g["skipped"]
    assert latest_facts(ctx, "acc-agency", "apollo_org")["headcount_growth_band"]["value"] == "shrinking"


def test_a_growth_filter_apollo_ignores_is_caught_and_nothing_is_used(world):
    ctx, t = world
    fake = install_apollo(t, ignore_filter=True)
    g = lk.run(ctx)["growth"]
    assert "ignore the growth filter" in g["stopped_by"] and g["customers"]["stored"] is False
    assert len(fake.asked) == 1 and ctx.store.select("lookalike_growth") == []
    assert facts(ctx, "acc-fintech", "apollo_org") == []  # no account is searched, no band written


def test_a_failed_search_keeps_the_stored_counts(world):
    ctx, t = world
    install_apollo(t)
    lk.run(ctx)
    before = ctx.store.select("lookalike_growth")
    t.route("POST", "/mixed_companies/search", status=500, body={"error": "busy"})
    g = lk.run(dataclasses.replace(ctx, now=NOW + timedelta(days=31)))["growth"]
    assert g["customers"]["stored"] is False and g["errors"] and ctx.store.select("lookalike_growth") == before
    t.route("POST", "/mixed_companies/search", status=401, body={"error": "bad key"})
    with pytest.raises(ApiError):
        lk.run(dataclasses.replace(ctx, now=NOW + timedelta(days=62)))


# -- `us-outbound lookalikes fit` ----------------------------------------------------------------------------


def live_sheet(settings):
    """The sheet as it is on 5 Oct 2026: the old row on, the graded rows not there yet."""
    return dataclasses.replace(settings, signals=tuple(
        dataclasses.replace(s, active=True) if s.signal == "Looks like Spill's customers" else s
        for s in settings.signals if not s.signal.endswith("match to Spill's customers")))


def test_lookalikes_fit_shows_the_fits_and_the_tier_mix_and_writes_nothing(world, apollo, capsys):
    ctx, _ = world
    lk.run(ctx)
    ctx.store.upsert("accounts", [
        {"account_id": f"acc-{i}", "domain": f"tech{i}.example", "industry": "Fintech",
         "industry_group": "Technology & Startups", "size_band": "20-49", "hq_state": "NY", "status": "queued",
         "first_seen": NOW} for i in range(3)])
    ctx = dataclasses.replace(ctx, settings=live_sheet(ctx.settings))
    snapshot = {name: [dict(r) for r in rows] for name, rows in ctx.store.tables.items()}
    calls = len(ctx.guard.calls)
    capsys.readouterr()
    assert cli.main(["lookalikes", "fit"], context_factory=lambda job, live, **kw: ctx) == 0
    out = "\n".join(line for line in capsys.readouterr().out.splitlines() if not line.startswith('{"event"'))
    assert out.splitlines()[0] == "Lookalike fit of 7 open accounts, worked out now from the cells of Mon 05 Oct 2026 02:30 UK."
    # The fintech prospect, the three new Tech accounts at 10-49 and acme-tech (a customer, Excluded) fit 90 or more.
    assert "  90-100          5" in out and "  10-19           1" in out and "  50-59           1" in out
    assert "Growth known for 1 of 7; the rest are fitted on industry and size alone." in out
    assert "\"Close match to Spill's customers\" (lookalike_fit >= 70, +15): 5 accounts." in out
    assert "\"Some match to Spill's customers\" (lookalike_fit >= 45 AND lookalike_fit < 70, +8): 1 account." in out
    # Today the 10-49 Tech accounts score 15 + 4 = 19 (Control); with the graded rows 30 (Standard).
    assert "  Standard       0 (0%) !          4 (80%) !" in out
    assert "  Control      5 (100%) !            1 (20%)" in out
    assert "  Excluded              2                  2" in out
    assert "Nothing was written." in out
    assert {name: [dict(r) for r in rows] for name, rows in ctx.store.tables.items()} == snapshot
    assert all(not c.write for c in ctx.guard.calls[calls:])  # reads only, and only the database
    assert {c.system for c in ctx.guard.calls[calls:]} == {"db"}


def test_lookalikes_fit_tries_other_cut_offs_and_weights_in_memory(world, apollo, capsys):
    ctx, _ = world
    lk.run(ctx)
    ctx.store.upsert("accounts", [
        {"account_id": f"acc-{i}", "domain": f"tech{i}.example", "industry": "Fintech",
         "industry_group": "Technology & Startups", "size_band": "20-49", "hq_state": "NY", "status": "queued",
         "first_seen": NOW} for i in range(3)])
    ctx = dataclasses.replace(ctx, settings=live_sheet(ctx.settings))
    snapshot = {name: [dict(r) for r in rows] for name, rows in ctx.store.tables.items()}
    capsys.readouterr()
    factory = lambda job, live, **kw: ctx  # noqa: E731
    assert cli.main(["lookalikes", "fit", "--close", "90:5", "--some", "off"], context_factory=factory) == 0
    out = capsys.readouterr().out
    assert "\"Close match to Spill's customers\" (lookalike_fit >= 90, +5): 5 accounts." in out
    assert "Some match" not in out
    # 15 for 10-49 and 5 for the close match: 20, Standard, as with +15; the sheet is not touched.
    assert "  Standard       0 (0%) !          4 (80%) !" in out
    assert "tried in memory only" in out
    assert cli.main(["lookalikes", "fit", "--close", "90:10", "--some", "60:4"], context_factory=factory) == 0
    out = capsys.readouterr().out
    assert "\"Some match to Spill's customers\" (lookalike_fit >= 60 AND lookalike_fit < 90, +4)" in out
    assert {name: [dict(r) for r in rows] for name, rows in ctx.store.tables.items()} == snapshot
    for bad, why in ((["--close", "ninety"], "LOWEST_FIT:WEIGHT"), (["--close", "60:10", "--some", "70:4"],
                                                                     "must start below Close match's 60")):
        assert cli.main(["lookalikes", "fit", *bad], context_factory=factory) == 2
        assert why in capsys.readouterr().err


def test_lookalikes_fit_before_the_first_run(ctx, capsys):
    assert cli.main(["lookalikes", "fit"], context_factory=lambda job, live, **kw: ctx) == 0
    assert "No lookalike cells yet" in capsys.readouterr().out


# -- the settings load switches the old row off ---------------------------------------------------------------


def test_a_signals_load_switches_the_old_row_off_and_adds_the_graded_rows():
    from us_outbound.settings.load import plan_tab

    build = default_tabs()["Signals"]
    sheet = [dict(r) for r in build if not r["signal"].endswith("match to Spill's customers")]
    old = next(r for r in sheet if r["signal"] == "Looks like Spill's customers")
    old.update(active="yes", weight="6", note="Harry's note")  # as on the live sheet, with an edit of his
    plan = plan_tab("Signals", sheet, build)
    by = {r["signal"]: r for r in plan.rows}
    assert by["Looks like Spill's customers"]["active"] == "no" and by["Looks like Spill's customers"]["weight"] == "6"
    assert by["Looks like Spill's customers"]["note"].startswith("Replaced by Close match and Some match")
    assert ("Looks like Spill's customers switched off (replaced by Close match to Spill's customers and Some match "
            "to Spill's customers)") in plan.updated
    assert plan.added == ["Close match to Spill's customers", "Some match to Spill's customers"]
    assert by["Close match to Spill's customers"]["active"] == "yes"
    settings, errors = validate_all({**default_tabs(), "Signals": plan.rows})
    assert not any(errors.values())
    assert [s.signal for s in settings.active_signals() if s.sources == ("lookalike",)] == [
        "Close match to Spill's customers", "Some match to Spill's customers"]
    # A second load changes nothing more.
    again = plan_tab("Signals", plan.rows, build)
    assert again.added == [] and not [u for u in again.updated if "switched off" in u]
