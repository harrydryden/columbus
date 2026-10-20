"""The Apollo sources (SPEC 7, 9): source_universe (sources/apollo_universe.py) and apollo_signals
(sources/apollo_jobs.py), against a fake Apollo. Filters from the sheet, one label and one account per
company, facts for scoring, the two-week queue, the Focus tab first, credits paced and recorded."""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context, sendable_general_copy
from us_outbound import budget, suppression
from us_outbound.clean.people import USPS_STATES, state_code
from us_outbound.clients.guard import APOLLO_READ_ACTIONS
from us_outbound.scoring.score import score_account
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import SOURCE_FIELDS, Focus, Override, Role, State
from us_outbound.settings.validate import validate_all
from us_outbound.sources import apollo_jobs, apollo_universe as uni

NOW = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)  # Monday 5 Oct, 03:00 UK: the pilot's first run
BASE, _ = validate_all(default_tabs())
# The General Copy rows sendable, as on the live sheet: ready and findable supply are accounts enrol could email (W1-6).
BASE = dataclasses.replace(BASE, copy=sendable_general_copy(BASE.copy))
STATE_OF = {f"{name}, US": code for code, name in USPS_STATES.items()}


def settings_with(*, states=("NY", "IL"), weekly_enrol_cap=150, focus=(), overrides=(), **general):
    g = dataclasses.replace(BASE.general, weekly_enrol_cap=weekly_enrol_cap, **general)
    rows = tuple(State(s.state, s.state in states, s.note) for s in BASE.states)
    return dataclasses.replace(BASE, general=g, states=rows, focus=tuple(focus), overrides=tuple(overrides))


def org(i: int, *, domain: str | None = "", state: str = "New York", naics=("541511",), keywords=("fintech",),
        employees: int | None = 64, **kw) -> dict:
    d = f"company{i}.com" if domain == "" else domain
    return {
        "id": f"org{i:03d}", "name": f"Company {i}, Inc.", "primary_domain": d,
        "website_url": f"http://www.{d}" if d else None, "estimated_num_employees": employees,
        "city": "New York", "state": state, "country": "United States", "naics_codes": list(naics),
        "keywords": list(keywords), "industry": "financial services", "founded_year": 2015,
        "organization_headcount_twelve_month_growth": 0.12, "latest_funding_round_date": "2026-03-01",
        "latest_funding_stage": "Series A", "funding_events": [{"date": "2026-03-01", "type": "Series A", "amount": 8000000}],
        "technology_names": ["Slack", "Google Workspace"], **kw,
    }


class FakeApollo:
    """mixed_companies/search, job postings and the credit balance over a list of organizations.

    A company is found by the search for its state (its _in key, else its state) and its NAICS
    codes (prefix) or keywords; the ids screen returns the listed ids with open postings.
    """

    def __init__(self, orgs=(), *, balance=30_000, postings=None, total=None):
        self.orgs, self.balance, self.postings, self.total = list(orgs), balance, postings or {}, total
        self.searches: list[dict] = []

    def install(self, t: FakeTransport) -> FakeTransport:
        t.route("POST", "/usage_stats/credit_usage_stats",
                body={"credit_usage_stats": {"lead_credit": {"left_over": self.balance}}})
        t.route("POST", "/mixed_companies/search", fn=self.search)
        t.route("GET", "/job_postings", fn=self.job_postings)
        return t

    def _found(self, o: dict, body: dict) -> bool:
        if o.get("_in", state_code(o.get("state") or "")) != STATE_OF[body["organization_locations"][0]]:
            return False
        ranges = body.get("organization_num_employees_ranges") or ()
        if len(ranges) == 1:  # every search is one size band (W2-1: the unbanded search went)
            if o.get("_band"):  # Apollo's own band for the company, whatever its record shows
                if ranges[0] != o["_band"].replace("-", ","):
                    return False
            else:
                lo, hi = (int(x) for x in ranges[0].split(","))
                n = o.get("estimated_num_employees")
                if n is None or not lo <= n <= hi:
                    return False
        if o.get("_always"):  # Apollo matched it on something its record does not show
            return True
        if "organization_naics_codes" in body:
            return any(c.startswith(p) for c in o.get("naics_codes") or () for p in body["organization_naics_codes"])
        return bool(set(body.get("q_organization_keyword_tags") or ()) & set(o.get("keywords") or ()))

    def search(self, req):
        body = req.json
        self.searches.append(body)
        if "organization_ids" in body:  # the apollo_jobs screen: the ids with a current posting
            found = [{"id": i} for i in body["organization_ids"] if self.postings.get(i)]
        else:
            found = [o for o in self.orgs if self._found(o, body)]
        page, per = body["page"], body["per_page"]
        total = self.total(body) if self.total else len(found)
        return {"organizations": found[(page - 1) * per : page * per],
                "pagination": {"page": page, "per_page": per, "total_entries": total, "total_pages": math.ceil(total / per)}}

    def job_postings(self, req):
        org_id = req.url.rsplit("/organizations/", 1)[1].split("/", 1)[0]
        posts = [{"id": f"{org_id}-{n}", "title": t, "url": f"https://jobs.example/{org_id}/{n}"}
                 for n, t in enumerate(self.postings.get(org_id, ()))]
        return {"organization_job_postings": posts, "pagination": {"total_entries": len(posts)}}


def make(orgs=(), *, settings=None, now=NOW, live=False, store=None, **apollo):
    fake = FakeApollo(orgs, **apollo)
    t = fake.install(FakeTransport())
    ctx = make_context(settings or settings_with(), job=uni.JOB, now=now, transport=t, live=live, store=store)
    return ctx, t, fake


def accounts_by_domain(ctx) -> dict[str, dict]:
    return {a["domain"]: a for a in ctx.store.select("accounts")}


def ledger(ctx, job=uni.JOB) -> list[dict]:
    return ctx.store.select("credit_ledger", {"job": job})


# -- the filters come from the sheet ----------------------------------------------------------------


def test_each_search_is_a_group_s_naics_codes_in_one_active_state_at_10_to_249_employees():
    ctx, t, fake = make()  # Apollo has nothing: every search comes back empty
    out = uni.run(ctx)
    assert out["created"] == 0 and out["credits"] == 0.0
    assert out["stopped_by"].startswith("every search is read to its end this month")
    states = [STATE_OF[b["organization_locations"][0]] for b in fake.searches]
    assert sorted(set(states)) == ["IL", "NY"]
    first = fake.searches[0]
    # One size band a search (W2-1: the unbanded search and its split went): the first is the smallest band.
    assert first["organization_num_employees_ranges"] == ["10,19"]
    assert {b["organization_num_employees_ranges"][0] for b in fake.searches} == {"10,19", "20,49", "50,99", "100,249"}
    assert first["not_organization_naics_codes"] == ["524"]  # insurers and brokers, partners (SPEC 9)
    assert (first["page"], first["per_page"]) == (1, 100)
    # Tech's 2 states × 4 bands = 8 searches come first, then the Agencies'.
    tech, agencies = fake.searches[0]["organization_naics_codes"], fake.searches[8]["organization_naics_codes"]
    assert {"5112", "51321", "5415", "51821"} <= set(tech) and "5418" in agencies
    assert all(2 <= len(c) <= 5 for c in tech + agencies)  # PHASE0-CONFIRM: Apollo takes 2 to 5 digits
    assert not any(c.startswith(p) for c in agencies for p in tech)  # nothing paid for twice
    # Tech, Agencies and Legal Teams (on from launch, review Appendix A) in two states; Digital health's
    # codes are Tech's, so it has no search of its own. Each in four size bands: 3 × 2 × 4 = 24 searches.
    assert len(fake.searches) == 24
    assert all(r.method in ("GET", "POST") for r in t.requests)
    assert {c.action for c in ctx.guard.calls if c.system == "apollo"} <= APOLLO_READ_ACTIONS


def test_ca_and_wa_are_never_searched_whatever_the_states_tab_says():
    s = settings_with(states=("NY", "CA", "WA"))  # validation refuses this; the job refuses it anyway
    assert uni.allowed_states(s) == ("NY",)
    assert uni.location("GA") == "Georgia, US"


def test_the_focus_tab_s_groups_come_first_with_their_share_of_the_queue():
    s = settings_with(focus=(Focus("Marketing & Creative Agencies", 0.6),))
    groups = uni.active_groups(s)
    assert groups[0] == "Marketing & Creative Agencies" and "Technology & Startups" in groups
    assert uni.pools(s, groups, 300) == [(("Marketing & Creative Agencies",), 180),
                                          (tuple(g for g in groups if g != "Marketing & Creative Agencies"), 120)]
    ctx, _, fake = make(settings=s)
    uni.run(ctx)
    assert "5418" in fake.searches[0]["organization_naics_codes"]


SEVEN = ("GA", "IL", "MA", "NJ", "NY", "PA", "TX")  # the live sheet's active states (19 Oct 2026)


def test_the_states_alternate_from_a_start_that_moves_each_iso_week():
    """code-sources B2: read state by state in the States tab's alphabetical order, GA came first and two Georgia
    pages filled the Technology pool: the queue was 85% Georgia, none from New York (16 of 59 US customers). Within
    each search and size band the states now come in turn, from week mod 7: ISO week 43 (43 = 6 × 7 + 1) starts at
    the second state, IL, so the first seven slices are IL, MA, NJ, NY, PA, TX, GA, all in the 10-19 band; week 44
    (44 = 6 × 7 + 2) starts at MA."""
    s = settings_with(states=SEVEN)
    states = uni.allowed_states(s)
    assert states == SEVEN  # tab order
    tech = uni.plan(s, uni.active_groups(s), states, week=43)["Technology & Startups"]
    assert [sl.state for sl in tech[:7]] == ["IL", "MA", "NJ", "NY", "PA", "TX", "GA"]
    assert {sl.band for sl in tech[:7]} == {"10-19"} and [sl.state for sl in tech[7:9]] == ["IL", "MA"]
    assert [sl.state for sl in uni.plan(s, uni.active_groups(s), states, week=44)["Technology & Startups"][:2]] == [
        "MA", "NJ"]
    assert uni.rotated(SEVEN, 49) == list(SEVEN)  # 49 = 7 × 7: back to GA


def test_the_run_reads_the_states_in_turn_and_stamps_each_account_with_the_search_that_found_it():
    """Mon 19 Oct 2026 is ISO week 43. One company in each of the seven states, all 20-49 staff: the run reads the
    10-19 band of every state first (empty), then 20-49 from IL round to GA, and each company it creates carries
    one sourced_by fact naming the search, its state and band."""
    monday = datetime(2026, 10, 19, 2, 0, tzinfo=UTC)
    names = {code: USPS_STATES[code] for code in SEVEN}
    orgs = [org(i, state=names[st], employees=None, _band="20-49") for i, st in enumerate(SEVEN, start=1)]
    ctx, _, fake = make(orgs, settings=settings_with(states=SEVEN), now=monday)
    out = uni.run(ctx)
    tech = [(STATE_OF[b["organization_locations"][0]], b["organization_num_employees_ranges"][0])
            for b in fake.searches if "5415" in b.get("organization_naics_codes", ())]
    assert tech[:8] == [("IL", "10,19"), ("MA", "10,19"), ("NJ", "10,19"), ("NY", "10,19"), ("PA", "10,19"),
                        ("TX", "10,19"), ("GA", "10,19"), ("IL", "20,49")]
    assert out["created"] == 7
    by_account = {}
    for e in ctx.store.select("signal_events", {"fact": "sourced_by"}):
        by_account.setdefault(e["account_id"], []).append(e["value"])
    accounts = {a["account_id"]: a for a in ctx.store.select("accounts")}
    assert set(by_account) == set(accounts) and all(len(v) == 1 for v in by_account.values())
    for aid, [v] in by_account.items():
        assert (v["state"], v["band"], v["group"], v["page"]) == (accounts[aid]["hq_state"], "20-49",
                                                                  "Technology & Startups", 1)


# -- one company: one label, one account, its facts ---------------------------------------------------


def test_a_company_becomes_one_account_with_one_label_and_the_facts_scoring_reads():
    ctx, _, _ = make([org(1)])
    out = uni.run(ctx)
    assert out["created"] == 1 and out["credits"] == 1.0
    a = accounts_by_domain(ctx)["company1.com"]
    assert {k: a[k] for k in ("source", "status", "industry", "industry_group", "hq_state", "hq_city", "employees",
                               "size_band", "apollo_org_id", "naics", "founded_year", "clean_name", "legal_name")} == {
        "source": "apollo", "status": "new", "industry": "Fintech", "industry_group": "Technology & Startups",
        "hq_state": "NY", "hq_city": "New York", "employees": 64, "size_band": "50-99", "apollo_org_id": "org001",
        "naics": "541511", "founded_year": 2015, "clean_name": "Company 1", "legal_name": "Company 1, Inc.",
    }
    facts = {e["fact"]: e for e in ctx.store.select("signal_events", {"account_id": a["account_id"], "source": "apollo_org"})}
    assert set(facts) == {"employees", "naics", "hq_state", "headcount_growth_12m", "days_since_funding", "funding_stage",
                          "funding_amount_usd", "founded_year", "technologies", "keywords", "apollo_industry"}
    assert set(facts) <= SOURCE_FIELDS["apollo_org"] and "open_roles" not in facts  # open_roles is apollo_jobs's
    assert facts["days_since_funding"]["value"] == 218  # 1 Mar to 5 Oct, as of observed_at (an AGED fact)
    assert facts["headcount_growth_12m"]["value"] == 0.12 and facts["technologies"]["value"] == ["Slack", "Google Workspace"]
    for e in facts.values():
        assert e["source"] == "apollo_org" and e["observed_at"] == NOW and len(e["quote"]) <= 300 and e["quote"]
        assert e["source_url"] == "https://app.apollo.io/#/organizations/org001"
    # And the search that found it (W1-3a; code-sources §6), under the job's own source, which no Signals row reads.
    [found] = ctx.store.select("signal_events", {"account_id": a["account_id"], "source": uni.JOB})
    assert found["fact"] == "sourced_by" and found["value"] == {
        "slice": found["value"]["slice"], "group": "Technology & Startups", "state": "NY", "band": "50-99", "page": 1,
        "run_id": ctx.run_id}
    assert found["value"]["slice"].startswith("Technology & Startups|naics|NY|") and uni.JOB not in SOURCE_FIELDS


@pytest.mark.parametrize("naics, keywords, label", [
    (("541511",), ("fintech", "payments"), "Fintech"),  # shared tech NAICS: the keyword decides
    (("541511",), ("saas", "software development"), "Technology & Startups"),
    # Codes alone place a company in its group's umbrella; a label within it needs its own words (7 Oct 2026).
    (("541810",), (), "Marketing & Creative Agencies"),
    (("541810",), ("advertising agency",), "Advertising agencies"),  # then its 6-digit code helps it win
    (("541511",), (), "Technology & Startups"),  # not Games studios: custom programming says nothing of games
    (("541511",), ("logistics", "supply chain consulting"), "Technology & Startups"),
    # Harry, 7 Oct 2026: 541511 is off Games studios' codes, so a games studio needs a code of its own (513210); on
    # custom programming alone the rules place it in the group, and the label check (labels.py) names it.
    (("541511",), ("mobile games", "game development"), "Technology & Startups"),
    (("513210",), ("mobile games", "game development"), "Games studios"),
    (("541715",), (), None),  # R&D alone is no AI company (a surgeons' society, an orthopaedic practice)
    (("541715",), ("machine learning",), "AI & deep tech"),
    (("5415",), (), "Technology & Startups"),  # not Adtech & martech, the first label of the group
    (("541810",), ("marketing agency",), "Marketing & Creative Agencies"),
    ((), ("digital health", "telehealth"), "Digital health"),  # no NAICS: keywords alone
    (("111110",), ("soybeans",), None),
    # Insurance and Financial Services exclude 524, so the keyword decides; the partner check then refuses it.
    (("524210",), ("insurtech",), "Insurtech"),
    # Apollo's five-digit codes (the crosswalk, 10 Oct 2026): one the tab names a single code under stands for it,
    # one it names several under reads at four digits, and an exclusion alone never decides it.
    (("54161",), (), "Professional Services"),  # consulting: was Marketing, through 541613
    (("54151",), (), "Technology & Startups"),  # computer systems design: was none, through 541512's exclusion
    (("54181",), (), "Marketing & Creative Agencies"),  # 541810, the one code named under it
    (("51321",), (), "Technology & Startups"),  # 513210
    (("54111",), (), "Legal Teams"),  # 541110
    (("54121",), (), "Professional Services"),  # 541214 is only an exclusion: read as 5412
])
def test_best_label(naics, keywords, label):
    got = uni.best_label(list(naics), " ; ".join(keywords), BASE)
    assert (got.industry if got else None) == label


def test_skipped_companies_and_why():
    ctx, _, _ = make([
        org(1, domain="gmail.com"),
        org(2, domain=None),
        org(3, state="California", _in="NY"),
        org(4, employees=600, _band="100-249"),  # Apollo's band search found it; its record says 600
        org(5, naics=("541511",), keywords=("hr software", "payroll")),  # an HR-tech vendor: a partner
        org(6, naics=("111110",), keywords=("soybeans",), industry="farming", _always=True),
        org(11, naics=("524210",), keywords=("insurtech",), _always=True),  # an insurance agency: a partner
        org(7, naics=("541611",), keywords=()),  # management consulting, switched off
        org(8, domain="optedout.com"),
        org(9, domain="dupe.com"), org(10, domain="www.dupe.com"),
        org(12, naics=("813910",), keywords=("trade association",), _always=True),  # before any label is tried
        org(13, naics=("541511",), keywords=("fintech",), industry="Government Administration"),
    ])
    suppression.add(ctx.store, domain="optedout.com", reason="test", source="test", now=NOW)
    out = uni.run(ctx)
    assert out["skipped"] == {
        "a personal email domain": 1, "no website": 1, "HQ outside the active states": 1, "outside 10 to 249 employees": 1,
        "a partner, never prospected": 2, "no Industries label fits": 1, "its best Industries label is switched off": 1,
        "suppressed": 1, "an association or chamber, never prospected": 1, "a public body, never prospected": 1,
    }
    assert sorted(accounts_by_domain(ctx)) == ["dupe.com"] and out["created"] == 1  # one account per root domain
    assert ctx.store.get("partners", domain="company5.com")["reason"] == "hr_tech"
    assert ctx.store.get("partners", domain="company11.com")["reason"] == "broker"


def test_a_switched_off_label_in_a_group_we_prospect_goes_under_the_groups_own_label():
    """9 Oct 2026: sourcing dropped a company whose best label was switched off, though the label check would have
    placed it under its group's own label. Both now follow the check's rule (Settings.placeable)."""
    remote = uni.best_label([], "remote-first ; distributed team ; saas", BASE)
    assert (remote.industry, remote.active) == ("Remote & hybrid teams", False)
    assert BASE.placeable(remote).industry == "Technology & Startups"
    consulting = uni.best_label(["541611"], "", BASE)
    assert consulting.industry_group == "Professional Services" and BASE.placeable(consulting) is None  # group off
    ctx, _, _ = make([org(1, naics=(), keywords=("remote-first", "distributed team"), _always=True)])
    assert uni.run(ctx)["created"] == 1
    [a] = accounts_by_domain(ctx).values()
    assert (a["industry"], a["industry_group"]) == ("Technology & Startups", "Technology & Startups")


def test_an_account_found_again_keeps_its_source_and_status_and_overrides_win():
    s = settings_with(overrides=(Override("company2.com", "hq_state", "IL"), Override("company2.com", "employees", "30")))
    ctx, _, _ = make([org(1, domain="named.co"), org(2)], settings=s)
    ctx.store.insert("accounts", [{"account_id": "n1", "domain": "named.co", "source": "named", "status": "queued",
                                   "first_seen": NOW - timedelta(days=3)}])
    out = uni.run(ctx)
    assert out["created"] == 1 and out["updated"] == 1
    named, other = accounts_by_domain(ctx)["named.co"], accounts_by_domain(ctx)["company2.com"]
    assert (named["source"], named["status"], named["industry"], named["apollo_org_id"]) == ("named", "queued", "Fintech", "org001")
    assert (other["hq_state"], other["employees"], other["size_band"]) == ("IL", 30, "20-49")
    hq = ctx.store.select("signal_events", {"account_id": other["account_id"], "fact": "hq_state"})
    assert [e["value"] for e in hq] == ["NY"]  # the fact is what Apollo said; scoring applies the override


def test_an_open_account_whose_label_was_checked_keeps_it_when_found_again_and_a_new_one_gets_the_rules():
    """Harry, 7 Oct 2026: once the label check has decided (accounts.label_source), the monthly refresh never puts the
    raw rules' label back; an account not checked yet takes the rules' label, and verify_accounts checks it."""
    ctx, _, _ = make([org(1, domain="checked.co"), org(2, domain="unchecked.co"), org(3)])
    ctx.store.insert("accounts", [
        {"account_id": "c1", "domain": "checked.co", "source": "apollo", "status": "verified", "industry": "Edtech",
         "industry_group": "Technology & Startups", "label_source": "model"},
        {"account_id": "u1", "domain": "unchecked.co", "source": "apollo", "status": "queued", "industry": "Edtech",
         "industry_group": "Technology & Startups"},
    ])
    out = uni.run(ctx)
    assert out["created"] == 1 and out["updated"] == 2
    by = accounts_by_domain(ctx)
    assert (by["checked.co"]["industry"], by["checked.co"]["label_source"]) == ("Edtech", "model")
    assert by["unchecked.co"]["industry"] == "Fintech" and not by["unchecked.co"].get("label_source")
    assert by["company3.com"]["industry"] == "Fintech" and not by["company3.com"].get("label_source")


def test_a_row_with_no_count_never_blanks_a_size_the_account_already_has():
    """A13: apollo_enrich (or an earlier row) gave an exact count; a later search row has none. The count and its
    band stay; a blank size is still filled from the search's band; an Overrides row still wins."""
    s = settings_with(states=("NY",), overrides=(Override("company3.com", "employees", "30"),))
    orgs = [org(1, employees=None, _band="20-49"), org(2, employees=None, _band="20-49"),
            org(3, employees=None, _band="20-49")]
    ctx, _, _ = make(orgs, settings=s)
    ctx.store.insert("accounts", [
        {"account_id": "q1", "domain": "company1.com", "source": "apollo", "status": "queued", "employees": 64,
         "size_band": "50-99"},
        {"account_id": "q2", "domain": "company2.com", "source": "apollo", "status": "verified", "employees": None,
         "size_band": None},
        {"account_id": "q3", "domain": "company3.com", "source": "apollo", "status": "new", "employees": 64,
         "size_band": "50-99"},
    ])
    out = uni.run(ctx)
    assert out["updated"] == 3
    got = {d: (a["employees"], a["size_band"]) for d, a in accounts_by_domain(ctx).items()}
    assert got == {"company1.com": (64, "50-99"), "company2.com": (None, "20-49"), "company3.com": (30, "20-49")}
    assert accounts_by_domain(ctx)["company1.com"]["industry"] == "Fintech"  # the other columns are Apollo's latest


def test_an_enrolled_account_gets_facts_but_keeps_its_columns():
    ctx, _, _ = make([org(1, employees=80)])
    ctx.store.insert("accounts", [{"account_id": "e1", "domain": "company1.com", "status": "enrolled", "employees": 64,
                                   "industry": "Advertising agencies"}])
    uni.run(ctx)
    a = ctx.store.get("accounts", account_id="e1")
    assert (a["employees"], a["industry"]) == (64, "Advertising agencies")
    assert ctx.store.select("signal_events", {"account_id": "e1", "fact": "employees"})[0]["value"] == 80


# -- when to stop: two weeks of queue, today's credits, the floor -----------------------------------


LONG_AGO = NOW - timedelta(days=60)  # admitted outside the 28-day yield window


def seed_supply(ctx, *, ready=0, findable=0, raw=0, verified=(0, 0), picked=(0, 0), group_of=None):
    """The queue in the three kinds contactable supply counts, and the two yields' evidence.

    ready: verified, with a sendable contact; findable: verified, nobody tried yet; raw: new, not checked. All admitted
    60 days ago, so none of them counts in p_v. verified=(k, n): n companies admitted yesterday, k of them gone on to
    enrolment (passed their first check, and in no supply count) and n - k disqualified. picked=(k, n): one
    pick_contacts run two days ago that tried n accounts and found a contact at k."""
    base = {"industry": "Fintech", "industry_group": "Technology & Startups", "hq_state": "NY", "employees": 30,
            "size_band": "20-49", "source": "apollo", "first_seen": LONG_AGO}
    rows, contacts = [], []
    for kind, n, status in (("ready", ready, "verified"), ("find", findable, "verified"), ("raw", raw, "new")):
        for i in range(n):
            aid = f"{kind}{i}"
            rows.append({**base, "account_id": aid, "domain": f"{aid}.com", "status": status,
                         "tier": "Standard" if status == "verified" else None,
                         **({"industry_group": group_of(kind, i)} if group_of else {})})
            if kind == "ready":
                contacts.append({"contact_id": f"k-{aid}", "account_id": aid, "email": f"jo@{aid}.com",
                                 "email_status": "verified", "person_state": "NY", "title": "CEO"})
    k, n = verified
    rows += [{**base, "account_id": f"chk{i}", "domain": f"chk{i}.com", "first_seen": NOW - timedelta(days=1),
              "status": "enrolled" if i < k else "disqualified"} for i in range(n)]
    ctx.store.insert("accounts", rows)
    if contacts:
        ctx.store.insert("contacts", contacts)
    if picked[1]:
        ctx.store.upsert("heartbeats", [{"run_id": "pick-1", "job": "pick_contacts", "status": "ok", "dry_run": False,
                                         "started_at": NOW - timedelta(days=2),
                                         "detail": {"tried": picked[1], "picked": picked[0]}}])


def test_contactable_supply_counts_ready_findable_and_raw_at_their_yields():
    ctx, _, _ = make()
    seed_supply(ctx, ready=60, findable=100, raw=200, verified=(20, 40), picked=(20, 40))
    sup = uni.contactable_supply(ctx)
    # p_v = 20 of 40 first checks passed = 0.5; p_c = 20 of 40 tried = 0.5, both from 20 or more: measured.
    # contactable = 60 + 100 × 0.5 + 200 × 0.5 × 0.5 = 60 + 50 + 50 = 160.
    assert (sup.ready, sup.findable, sup.raw, sup.p_v, sup.p_c, sup.contactable) == (60, 100, 200, 0.5, 0.5, 160.0)
    assert sup.basis == {"p_v": "measured", "p_c": "measured"} and sup.by_group == {"Technology & Startups": 160.0}


def test_short_of_two_weeks_it_creates_what_the_yields_need_up_to_twice_the_cap_a_run():
    """code-sources B1: on 13 to 19 Oct the queue held 375 to 500 accounts, 305 to 338 with nobody to email, so
    sourcing stopped every morning while ready supply fell to 1.3 days. Supply now counts in companies enrol could
    email. Contactable 160 of a 300 target: raw_need = ceil((300 - 160) ÷ (0.5 × 0.5)) = ceil(140 ÷ 0.25) = 560,
    above the run's ceiling of 2 × 150 = 300, so it creates 300: three full pages of New York's 400 companies."""
    ctx, _, fake = make([org(i) for i in range(1, 401)])
    seed_supply(ctx, ready=60, findable=100, raw=200, verified=(20, 40), picked=(20, 40))
    out = uni.run(ctx)
    assert (out["target"], out["raw_need"], out["raw_allowance"], out["created"]) == (300, 560, 300, 300)
    # The old rule counted 60 + 100 + 200 = 360 open accounts against the 300 and created nothing.
    assert out["queue_before"] == {"Technology & Startups": 360}
    assert out["supply"]["contactable"] == 160.0 and out["supply"]["basis"] == {"p_v": "measured", "p_c": "measured"}
    assert out["stopped_by"] == (
        "the ceiling of 2 × weekly_enrol_cap (300) raw accounts a run; contactable supply was 160 of 300 (two weeks "
        "of weekly_enrol_cap); p_v 0.50 (measured: 20 of 40), p_c 0.50 (measured: 20 of 40)")
    ny_tech = [b["page"] for b in fake.searches if b["organization_locations"] == ["New York, US"]
               and "5415" in b.get("organization_naics_codes", ()) and b["organization_num_employees_ranges"] == ["50,99"]]
    assert ny_tech == [1, 2, 3] and out["credits"] == 3.0
    created = [a for a in ctx.store.select("accounts") if a["domain"].startswith("company")]
    assert len(created) == 300 and len(ctx.store.select("signal_events", {"fact": "sourced_by"})) == 300


def test_two_weeks_of_contactable_supply_reads_no_page():
    # p_v 30 of 50 = 0.6, p_c 20 of 40 = 0.5: 150 + 200 × 0.5 + 300 × 0.6 × 0.5 = 150 + 100 + 90 = 340 >= 300.
    ctx, _, fake = make([org(1)])
    seed_supply(ctx, ready=150, findable=200, raw=300, verified=(30, 50), picked=(20, 40))
    out = uni.run(ctx)
    assert (out["supply"]["contactable"], out["raw_need"], out["created"], out["pages"]) == (340.0, 0, 0, 0)
    assert fake.searches == [] and out["stopped_by"] == (
        "contactable supply holds two weeks: 340 of 300 (two weeks of weekly_enrol_cap); "
        "p_v 0.60 (measured: 30 of 50), p_c 0.50 (measured: 20 of 40)")
    # The old count, every new, queued or verified account, is still printed, and decides nothing.
    assert out["queue_before"] == {"Technology & Startups": 650}


def test_an_account_nobody_was_found_at_or_with_no_candidates_is_no_findable_supply():
    """B1's fix: an account whose last pick found nobody waits RETRY_DAYS (14) before pick tries it again, and one the
    contact pre-check marked no_contact_candidates has nobody to try; neither counts as findable. 4 verified accounts
    with no contact: find0 tried 3 days ago with nobody found, find1 tried 15 days ago (due again), find2 marked by
    the pre-check, find3 never tried: findable = 2 (find1, find3)."""
    ctx, _, _ = make()
    seed_supply(ctx, findable=4)
    ctx.store.update("accounts", {"account_id": "find2"}, {"status_reason": uni.NO_CANDIDATES})
    ctx.store.insert("signal_events", [
        {"event_id": f"pick-{aid}", "account_id": aid, "source": "pick_contacts", "fact": "contact_pick",
         "value": {"outcome": "none", "reason": "nobody suitable"}, "observed_at": NOW - timedelta(days=days)}
        for aid, days in (("find0", 3), ("find1", 15))])
    assert uni.contactable_supply(ctx).findable == 2


@pytest.mark.parametrize("k, n, value, basis", [
    (10, 30, 10 / 30, "measured"),  # 0.333: 30 checks are enough to measure (YIELD_MIN_N 20)
    (10, 100, 0.20, "floored"),  # 0.10 is below the floor: 0.20, said as such
    (1, 5, 0.60, "default"),  # 5 checks are too few: the default
    (10, 19, 0.60, "default"),  # 19 is still under 20 (10 ÷ 19 = 0.526 is not used)
    (12, 20, 0.60, "measured"),  # exactly 20: measured, 12 ÷ 20 = 0.60
])
def test_a_yield_is_measured_from_20_floored_at_0_2_and_the_default_before(k, n, value, basis):
    y = uni.measured_yield(k, n, uni.DEFAULT_P_VERIFY)
    assert (y.value, y.basis) == (pytest.approx(value), basis)


def test_p_v_is_distinct_companies_on_their_first_check_not_heartbeat_sums():
    """RT §3 L1: Σ verified ÷ Σ checked over verify's heartbeats counted every re-check of a never-pass account again
    (13 of 141 = 0.09). p_v is each company once, by where its first check left it, over 28 days of admissions.
    Passed 10: 6 verified, 2 enrolled, 2 engaged. Failed 21: 14 disqualified, 4 parked, 2 still new but tiered
    Excluded by a HubSpot check, and 1 verified then tiered Excluded (it left the verified pool). Not counted: 7 new
    and 3 queued (no decision yet) and 5 verified 29 days ago (outside the window). p_v = 10 ÷ 31 = 0.323, measured."""
    ctx, _, _ = make()
    spec = [(6, "verified", 1, None), (2, "enrolled", 1, None), (2, "engaged", 1, None),
            (14, "disqualified", 1, None), (4, "parked", 1, None), (2, "new", 1, "Excluded"),
            (1, "verified", 1, "Excluded"), (7, "new", 1, None), (3, "queued", 1, None), (5, "verified", 29, None)]
    rows = [{"status": status, "tier": tier, "first_seen": NOW - timedelta(days=days)}
            for n, status, days, tier in spec for _ in range(n)]
    ctx.store.insert("accounts", [{**r, "account_id": f"a{i}", "domain": f"a{i}.com"} for i, r in enumerate(rows)])
    y = uni.verify_yield(ctx)
    assert (y.k, y.n, y.basis) == (10, 31, "measured") and y.value == pytest.approx(10 / 31)
    # Heartbeat sums are not read: a verify run that "checked" 141 and verified 13 changes nothing.
    ctx.store.upsert("heartbeats", [{"run_id": "v", "job": "verify_accounts", "status": "ok", "dry_run": False,
                                     "started_at": NOW - timedelta(days=1), "detail": {"checked": 141, "verified": 13}}])
    assert uni.verify_yield(ctx) == y


def test_the_weekly_ceiling_lets_thursday_create_only_what_monday_to_wednesday_left():
    """3 × 150 = 450 raw accounts a week (RS §2 W1-3). 400 created Monday to Wednesday leave 50 for Thursday. Its first
    page carries 100 companies: the 50 after the 50th wait, and the next run reads the page again (its note says
    "cut"), so none is lost for the month. Friday has nothing left; the next Monday takes the other 50 and the rest."""
    thursday = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)  # Thu 8 Oct, 03:00 UK
    ctx, _, fake = make([org(i) for i in range(1, 151)], now=thursday)
    ctx.store.insert("accounts", [
        {"account_id": f"w{i}", "domain": f"w{i}.com", "source": "apollo", "status": "verified", "tier": "Standard",
         "industry": "Fintech", "industry_group": "Technology & Startups", "size_band": "20-49",
         "first_seen": datetime(2026, 10, 5 + i % 3, 2, 0, tzinfo=UTC)} for i in range(400)])
    out = uni.run(ctx)
    assert (out["created_this_week_before"], out["raw_allowance"], out["created"], out["left_on_cut_pages"]) == (
        400, 50, 50, 50)
    assert out["stopped_by"].startswith("the ceiling of 3 × weekly_enrol_cap (450) raw accounts a week, 400 created "
                                        "already this week; contactable supply")
    [cut] = [json.loads(r["note"]) for r in ledger(ctx) if '"cut"' in (r["note"] or "")]
    assert (cut["page"], cut["cut"]) == (1, 50)
    ctx.now = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)  # Friday: 450 this week
    before = len(fake.searches)
    friday = uni.run(dataclasses.replace(ctx, run_id="fri"))
    assert (friday["raw_allowance"], friday["created"], len(fake.searches)) == (0, 0, before)
    ctx.now = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)  # Monday: a new week; the cut page first
    monday = uni.run(dataclasses.replace(ctx, run_id="mon"))
    ny = [b["page"] for b in fake.searches[before:] if b["organization_locations"] == ["New York, US"]
          and "5415" in b.get("organization_naics_codes", ()) and b["organization_num_employees_ranges"] == ["50,99"]]
    assert ny == [1, 2] and monday["created"] == 100  # page 1's other 50, then page 2's 50
    assert len({a["domain"] for a in ctx.store.select("accounts") if a["domain"].startswith("company")}) == 150


def test_the_next_run_carries_on_from_the_next_page_and_a_new_month_starts_again():
    # weekly_enrol_cap 50: 100 raw accounts a run, 150 a week. Monday's run takes page 1 (100, the run's ceiling);
    # Tuesday's the 50 the week has left, page 2's 50.
    s = settings_with(weekly_enrol_cap=50)
    ctx, _, fake = make([org(i) for i in range(1, 151)], settings=s)
    assert uni.run(ctx)["created"] == 100
    ctx.store.update("accounts", {}, {"status": "enrolled"})  # the queue empties
    ctx.now = NOW + timedelta(days=1)
    assert uni.run(ctx)["created"] == 50
    ny = [b["page"] for b in fake.searches if b["organization_locations"] == ["New York, US"]
          and "5415" in b.get("organization_naics_codes", ()) and b["organization_num_employees_ranges"] == ["50,99"]]
    assert ny == [1, 2]
    [first] = [n for n in (json.loads(r["note"]) for r in ledger(ctx)) if n["page"] == 1 and n["results"]]
    assert first["pages"] == 2 and first["results"] == 100 and first["total"] == 150
    ctx.store.update("accounts", {}, {"status": "enrolled"})
    ctx.now = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)  # a new month: the universe is read again from page 1
    before = len(fake.searches)
    uni.run(ctx)
    november = [b["page"] for b in fake.searches[before:] if b["organization_locations"] == ["New York, US"]
                and "5415" in b.get("organization_naics_codes", ()) and b["organization_num_employees_ranges"] == ["50,99"]]
    assert november == [1, 2]  # page 1 again; its companies are accounts already, so page 2 too


def test_a_saved_apollo_account_without_an_organization_id_stores_no_apollo_org_id():
    """Defect 8 (9 Oct 2026): a search's `accounts` row whose organization_id is None gave its account id as the
    account's apollo_org_id, later fed to paid job-postings lookups. It now gives none."""
    ctx, t, fake = make([org(1)])

    def as_saved_accounts(req):
        page = fake.search(req)
        page["accounts"] = [{**{k: v for k, v in o.items() if k not in ("id", "primary_domain")}, "id": "acct-77",
                             "organization_id": None, "domain": o["primary_domain"]} for o in page.pop("organizations")]
        return page

    t.route("POST", "/mixed_companies/search", fn=as_saved_accounts)
    assert uni.run(ctx)["created"] == 1
    a = accounts_by_domain(ctx)["company1.com"]
    assert a["apollo_org_id"] is None
    facts = ctx.store.select("signal_events", {"account_id": a["account_id"]})
    assert facts and all(e["source_url"] == "" for e in facts)  # no Apollo page named after the account id


def test_a_page_apollo_did_not_answer_keeps_its_credit_and_is_read_again():
    """Defect 9 (9 Oct 2026): a search page is reserved before it is asked for. One that fails with a 5xx may have
    been charged, so it keeps its credit; its reservation is no page read, so the next run asks for it again."""
    ctx, t, _ = make([org(1)])
    t.route("POST", "/mixed_companies/search", status=503, body={"error": "unavailable"})
    out = uni.run(ctx)
    assert out["created"] == 0 and out["errors"]
    failed = ledger(ctx)
    assert failed and all(r["credits"] == 1.0 and json.loads(r["note"])["failed"] == 503 for r in failed)
    assert out["credits"] == len(failed) and uni.cursors(ctx) == {}
    t.routes.pop()  # Apollo answers again
    again = uni.run(dataclasses.replace(ctx, run_id="next"))
    assert again["created"] == 1


def test_a_page_with_results_costs_one_credit_and_an_empty_page_none():
    ctx, _, _ = make([org(1)])
    uni.run(ctx)
    rows = ledger(ctx)
    # 3 groups (Tech, Agencies, Legal Teams) × 2 states × 4 size bands = 24 pages; only Tech's New York 50-99 page
    # holds company 1 (64 staff), so 23 empty pages cost nothing and 1 costs a credit.
    assert sorted(r["credits"] for r in rows) == [0.0] * 23 + [1.0]
    assert all(r["system"] == "apollo" and r["run_id"] == ctx.run_id and r["occurred_at"] == NOW for r in rows)


def test_sourcing_spends_only_its_share_of_the_month_paced_by_the_weekday():
    # 25% of 2,000 is 500 a month; 490 spent leaves 10, and 20 weekdays are left: 0.5 today, less than a page.
    ctx, _, fake = make([org(1)])
    ctx.store.insert("credit_ledger", [{"entry_id": "x", "system": "apollo", "job": uni.JOB, "credits": 490.0,
                                        "occurred_at": datetime(2026, 10, 1, 9, tzinfo=UTC), "note": ""}])
    out = uni.run(ctx)
    assert out["stopped_by"] == "today's Apollo credits for sourcing are used" and fake.searches == []
    assert out["budget"]["today"] == 0.5


def test_sourcing_never_takes_what_is_left_of_the_whole_apollo_budget_today():
    ctx, _, fake = make([org(1)])
    ctx.store.insert("credit_ledger", [{"entry_id": "x", "system": "apollo", "job": "pick_contacts", "credits": 1990.0,
                                        "occurred_at": datetime(2026, 10, 2, 9, tzinfo=UTC), "note": ""}])
    assert uni.run(ctx)["stopped_by"] == "today's Apollo credits for sourcing are used" and fake.searches == []


def test_no_credit_is_spent_below_apollo_floor():
    ctx, t, fake = make([org(1)], balance=4_000)
    out = uni.run(ctx)
    assert out["skipped"] is True and "below apollo_floor (5,000)" in out["reason"]
    assert fake.searches == [] and [r.url.rsplit("/", 1)[-1] for r in t.requests] == ["credit_usage_stats"]


def test_every_search_starts_by_size_band_so_no_account_lacks_one():
    """2 Oct 2026: Apollo's search rows carry no employee count, so a search with no size filter gave
    accounts no size band. Every search is now one per band, and the band is the filter's."""
    s = settings_with(states=("NY",))
    ctx, _, fake = make([org(1, employees=None, _band="20-49"), org(2, employees=None, _band="100-249")], settings=s)
    out = uni.run(ctx)
    assert all(len(b["organization_num_employees_ranges"]) == 1 for b in fake.searches)
    got = {d: (a["employees"], a["size_band"]) for d, a in accounts_by_domain(ctx).items()}
    assert got == {"company1.com": (None, "20-49"), "company2.com": (None, "100-249")}
    assert out["created"] == 2 and "size_bands_backfilled" not in out  # the backfill went with W2-1


def test_dry_run_reads_apollo_and_writes_only_the_database():
    ctx, _, _ = make([org(1)])
    assert ctx.dry_run
    uni.run(ctx)
    assert accounts_by_domain(ctx) and ledger(ctx)  # SPEC 0.3: dry-run still writes the database
    assert {c.system for c in ctx.guard.writes()} == {"db"}
    assert all(not c.write and c.sent for c in ctx.guard.calls if c.system == "apollo")


# -- apollo_signals: job postings ------------------------------------------------------------------------


def jobs_ctx(accounts, *, postings, now=NOW, settings=None):
    ctx, t, fake = make(settings=settings, now=now, postings=postings)
    ctx.job = apollo_jobs.JOB
    ctx.store.insert("accounts", accounts)
    return ctx, t, fake


def acct(aid, org_id, status="verified", **kw):
    return {"account_id": aid, "domain": f"{aid}.com", "apollo_org_id": org_id, "status": status,
            "industry": "Fintech", "industry_group": "Technology & Startups", "first_seen": NOW, **kw}


def facts_of(ctx, aid) -> dict:
    return {e["fact"]: e["value"] for e in ctx.store.select("signal_events", {"account_id": aid, "source": "apollo_jobs"})}


def test_job_postings_give_open_roles_open_people_roles_and_titles():
    """Verified accounts only (W1-3a; code-sources B4): a new or queued account, which the label check may still
    leave out, is neither screened nor read, so no credit buys postings at a company never emailed."""
    ctx, t, fake = jobs_ctx(
        [acct("a1", "org001"), acct("a2", "org002"), acct("a3", "org003"),
         acct("a4", "org004", tier="Excluded"), acct("a5", None), acct("a6", "org006", status="enrolled"),
         acct("a7", "org007", status="new"), acct("a8", "org008", status="queued")],
        postings={"org001": ["Senior Engineer", "HR Manager", "Senior Recruiter"], "org003": ["Designer"],
                  "org007": ["HR Director"], "org008": ["People Partner"]},
    )
    ctx.store.insert("signal_events", [{"event_id": "f3", "account_id": "a3", "source": "apollo_jobs", "fact": "open_roles",
                                        "value": 1, "observed_at": NOW - timedelta(days=5)}])
    out = apollo_jobs.run(ctx)
    assert (out["candidates"], out["screened"], out["postings_read"], out["without_postings"]) == (2, 2, 1, 1)
    assert facts_of(ctx, "a1") == {"open_roles": 3, "open_people_roles": 1,
                                   "posting_titles": ["Senior Engineer", "HR Manager", "Senior Recruiter"]}
    assert facts_of(ctx, "a2") == {"open_roles": 0, "open_people_roles": 0, "posting_titles": []}
    assert facts_of(ctx, "a7") == {} and facts_of(ctx, "a8") == {}
    [screen] = [b for b in fake.searches if "organization_ids" in b]
    assert screen["organization_ids"] == ["org001", "org002"] and screen["organization_num_jobs_range"] == {"min": 1}
    [get] = [r for r in t.requests if r.method == "GET"]
    assert get.url.endswith("/organizations/org001/job_postings") and get.params == {"page": 1, "per_page": 100}
    assert sorted(r["credits"] for r in ledger(ctx, apollo_jobs.JOB)) == [1.0, 1.0]  # the screen, the postings
    assert out["credits"] == 2.0 and {c.system for c in ctx.guard.writes()} == {"db"}


def test_an_account_read_over_30_days_ago_is_read_again():
    ctx, _, fake = jobs_ctx([acct("a1", "org001")], postings={})
    ctx.store.insert("signal_events", [{"event_id": "f1", "account_id": "a1", "source": "apollo_jobs", "fact": "open_roles",
                                        "value": 4, "observed_at": NOW - timedelta(days=31)}])
    assert apollo_jobs.run(ctx)["screened"] == 1


def test_postings_wait_when_today_s_credits_are_used():
    # 25% of 2,000 = 500; 470 spent leaves 30 over 20 weekdays: 1.5 today, so the screen (1) but no postings.
    ctx, _, _ = jobs_ctx([acct("a1", "org001"), acct("a2", "org002")], postings={"org001": ["HR Generalist"]})
    ctx.store.insert("credit_ledger", [{"entry_id": "x", "system": "apollo", "job": apollo_jobs.JOB, "credits": 470.0,
                                        "occurred_at": datetime(2026, 10, 1, 9, tzinfo=UTC), "note": ""}])
    out = apollo_jobs.run(ctx)
    assert (out["postings_read"], out["waiting"], out["stopped_by"]) == (0, 1, "today's Apollo credits for job postings are used")
    assert facts_of(ctx, "a1") == {} and facts_of(ctx, "a2")["open_roles"] == 0


def test_nothing_to_read_spends_nothing():
    ctx, t, _ = jobs_ctx([acct("a1", None)], postings={})
    out = apollo_jobs.run(ctx)
    assert out["candidates"] == 0 and t.requests == []


@pytest.mark.parametrize("title, people", [
    ("HR Business Partner", True), ("People Operations Manager", True), ("Chief People Officer", True),
    ("Human Resources Generalist", True), ("Senior Recruiter", False), ("Engineering Manager", False),
    ("Talent Acquisition Lead", False), ("Head of Culture", True),  # from the Roles tab below
])
def test_people_titles_are_hr_or_people_titles(title, people):
    s = dataclasses.replace(BASE, roles=(*BASE.roles, Role("People manager", ("Head of Culture",))))
    assert apollo_jobs.is_people_title(title, apollo_jobs.people_titles(s)) is people


def test_the_first_people_hire_signal_reads_these_facts():
    events = [
        {"event_id": "1", "account_id": "a1", "source": "apollo_jobs", "fact": "open_people_roles", "value": 1, "observed_at": NOW},
        {"event_id": "2", "account_id": "a1", "source": "apollo_people", "fact": "people_leader_count", "value": 0, "observed_at": NOW},
        {"event_id": "3", "account_id": "a1", "source": "apollo_people", "fact": "people_search_coverage", "value": 0.7,
         "observed_at": NOW},
    ]
    r = score_account({"account_id": "a1", "domain": "a1.com", "hq_state": "NY"}, events, BASE, NOW.date())
    assert "First People hire (likely)" in [m.signal.signal for m in r.matches]


# -- the share of the month ----------------------------------------------------------------------------


def test_a_job_s_share_is_paced_like_the_whole_budget():
    ctx, _, _ = make()
    ctx.store.insert("credit_ledger", [
        {"entry_id": "a", "system": "apollo", "job": uni.JOB, "credits": 100.0, "occurred_at": datetime(2026, 10, 1, 9, tzinfo=UTC)},
        {"entry_id": "b", "system": "apollo", "job": "pick_contacts", "credits": 300.0, "occurred_at": datetime(2026, 10, 2, 9, tzinfo=UTC)},
    ])
    part = budget.share_for_jobs(ctx.store, ctx.settings, "apollo", NOW, jobs=(uni.JOB,), share=0.25)
    assert (part.budget, part.used, part.weekdays_left) == (500.0, 100.0, 20)
    left, whole, _ = budget.room_today(ctx.store, ctx.settings, "apollo", NOW, jobs=(uni.JOB,), share=0.25)
    assert whole.used == 400.0 and left == pytest.approx(min(1600 / 20, 400 / 20)) == 20.0
