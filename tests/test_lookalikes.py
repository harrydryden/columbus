"""The lookalikes job (sources/lookalikes.py; Harry, 1 Oct 2026): Spill's HubSpot customers into lookalike
cells, kept out of the queue, scored as a signal, reported, and offered to sourcing as priorities.

HubSpot is a FakeTransport that answers the company and deal searches by their filters and pages on
hs_object_id like the real search API; the database is a MemoryStore. Nothing here calls a real service.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound import accounts
from us_outbound.ops import cli
from us_outbound.scoring import score
from us_outbound.scoring.score import score_account
from us_outbound.sources import lookalikes as lk

NOW = datetime(2026, 10, 5, 1, 30, tzinfo=UTC)  # Monday 02:30 UK, the job's slot
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


def test_the_signal_scores_an_account_in_a_strong_cell(world):
    ctx, _ = world
    lk.run(ctx)
    [active, strength] = sorted(facts(ctx, "acc-fintech", "lookalike"), key=lambda e: e["fact"])
    assert (active["fact"], active["value"], strength["fact"], strength["value"]) == (
        "lookalike_active", 11, "lookalike_strength", 15.0)
    assert active["quote"] == ("Spill's HubSpot customers in Technology & Startups at 10-49 staff: 11 active and "
                               "4 churned; 3 of the active ones are in the US.")
    a = ctx.store.get("accounts", account_id="acc-fintech")
    events = ctx.store.select("signal_events", {"account_id": "acc-fintech"})
    r = score_account(a, events, ctx.settings, NOW.date())
    [m] = [m for m in r.matches if m.signal.signal == "Looks like Spill's customers"]
    assert m.weight_applied == 4 and m.evidence[0].text == active["quote"]
    # Agencies at 50-99 have no customers in these fixtures: no fact, no points.
    assert facts(ctx, "acc-agency", "lookalike") == []


def test_a_thin_cell_does_not_score(world):
    ctx, _ = world
    lk.run(ctx)
    ctx.store.upsert("accounts", [{"account_id": "acc-studio", "domain": "newstudio.com",
                                   "industry": "Advertising agencies", "industry_group": "Marketing & Creative Agencies",
                                   "size_band": "10-19", "hq_state": "NY", "status": "queued", "first_seen": NOW}])
    lk.apply(ctx)
    got = {e["fact"]: e["value"] for e in facts(ctx, "acc-studio", "lookalike")}
    assert got == {"lookalike_active": 3, "lookalike_strength": 3.5}  # under 5 active and strength 10
    a = ctx.store.get("accounts", account_id="acc-studio")
    r = score_account(a, ctx.store.select("signal_events", {"account_id": "acc-studio"}), ctx.settings, NOW.date())
    assert "Looks like Spill's customers" not in {m.signal.signal for m in r.matches}


def test_apply_writes_only_changes_and_zeroes_a_cell_that_went(world):
    ctx, _ = world
    lk.run(ctx)
    assert lk.apply(ctx)["written"] == 0
    assert lk.apply(dataclasses.replace(ctx, now=NOW + timedelta(days=lk.REFRESH_DAYS)))["written"] == 3  # refreshed
    ctx.store.delete("lookalike_cells", {"cell_id": "Technology & Startups|10-49"})
    later = dataclasses.replace(ctx, now=NOW + timedelta(days=lk.REFRESH_DAYS + 1))
    assert lk.apply(later)["written"] >= 1
    latest = max(facts(ctx, "acc-fintech", "lookalike"), key=lambda e: e["observed_at"])
    assert latest["value"] == 0 and latest["quote"].startswith("No Spill customers counted in Technology & Startups")
    n = len(facts(ctx, "acc-fintech", "lookalike"))
    lk.apply(dataclasses.replace(ctx, now=NOW + timedelta(days=400)))
    assert len(facts(ctx, "acc-fintech", "lookalike")) == n  # zeros are not written again


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
    assert {e["fact"] for e in facts(ctx, "acc-new", "lookalike")} == {"lookalike_active", "lookalike_strength"}


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
    assert group.rstrip().endswith("10-49")  # the cells where the signal scores
    assert 'Signal "Looks like Spill\'s customers" (+4)' in text
    assert "lookalike_active >= 5 AND lookalike_strength >= 10" in text


def test_lookalikes_show_from_the_command_line(world, capsys):
    ctx, _ = world
    lk.run(ctx)
    assert cli.main(["lookalikes", "show", "--top", "2"], context_factory=lambda job, live, **kw: ctx) == 0
    out = capsys.readouterr().out
    assert "Lookalike cells from Spill's HubSpot customers" in out and "Top 2 of" in out
    assert cli.main(["lookalikes", "show", "--top", "0"], context_factory=lambda job, live, **kw: ctx) == 2


def test_the_job_is_registered_weekly_before_source_universe():
    from us_outbound.ops import heartbeat, schedule

    assert cli.JOBS["lookalikes"] == "us_outbound.sources.lookalikes:run"
    job = schedule.by_name()["lookalikes"]
    assert (job.cron, job.enabled, job.live) == ("30 2 * * 1", True, False)  # Monday 02:30 UK; database writes only
    assert schedule.by_name()["source_universe"].cron.split()[1] == "3"  # 03:00, after it
    assert heartbeat.EXPECTED["lookalikes"] == 8 * 24 * 60
