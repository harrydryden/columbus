"""The Apollo sources (SPEC 7, 9): source_universe (sources/apollo_universe.py) and apollo_signals
(sources/apollo_jobs.py), against a fake Apollo. Filters from the sheet, one label and one account per
company, facts for scoring, the two-week queue, the Focus tab first, credits paced and recorded."""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
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
        if o.get("_band") and len(ranges) == 1 and ranges[0] != o["_band"].replace("-", ","):
            return False  # a size-band search, for a company whose record has no employee count
        if o.get("_always"):  # Apollo matched it on something its record does not show
            return True
        if "organization_naics_codes" in body:
            return any(c.startswith(p) for c in o.get("naics_codes") or () for p in body["organization_naics_codes"])
        return bool(set(body.get("q_organization_keyword_tags") or ()) & set(o.get("keywords") or ()))

    def search(self, req):
        body = req.json
        self.searches.append(body)
        if "organization_ids" in body and "organization_num_employees_ranges" in body:  # the size-band backfill
            ids, ranges = set(body["organization_ids"]), body["organization_num_employees_ranges"]
            found = [{"id": o["id"]} for o in self.orgs
                     if o["id"] in ids and (o.get("_band") or "").replace("-", ",") in ranges]
        elif "organization_ids" in body:  # the apollo_jobs screen: the ids with a current posting
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


@pytest.fixture(autouse=True)
def unbanded_first_page(request, monkeypatch):
    """Most tests here were written for searches that start without a size filter, a path the job
    still takes for a search split over 50,000 companies. Since 2 Oct 2026 every search starts by
    size band (START_BANDS); tests marked `banded` run with that default."""
    if "banded" not in request.keywords:
        monkeypatch.setattr(uni, "START_BANDS", ("",))


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
    assert first["organization_num_employees_ranges"] == ["10,19", "20,49", "50,99", "100,249"]
    assert first["not_organization_naics_codes"] == ["524"]  # insurers and brokers, partners (SPEC 9)
    assert (first["page"], first["per_page"]) == (1, 100)
    tech, agencies = fake.searches[0]["organization_naics_codes"], fake.searches[2]["organization_naics_codes"]
    assert {"5112", "51321", "5415", "51821"} <= set(tech) and "5418" in agencies
    assert all(2 <= len(c) <= 5 for c in tech + agencies)  # PHASE0-CONFIRM: Apollo takes 2 to 5 digits
    assert not any(c.startswith(p) for c in agencies for p in tech)  # nothing paid for twice
    # Tech, Agencies and Legal Teams (on from launch, review Appendix A) in two states; Digital health's
    # codes are Tech's, so it has no search of its own.
    assert len(fake.searches) == 6
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
    facts = {e["fact"]: e for e in ctx.store.select("signal_events", {"account_id": a["account_id"]})}
    assert set(facts) == {"employees", "naics", "hq_state", "headcount_growth_12m", "days_since_funding", "funding_stage",
                          "funding_amount_usd", "founded_year", "technologies", "keywords", "apollo_industry"}
    assert set(facts) <= SOURCE_FIELDS["apollo_org"] and "open_roles" not in facts  # open_roles is apollo_jobs's
    assert facts["days_since_funding"]["value"] == 218  # 1 Mar to 5 Oct, as of observed_at (an AGED fact)
    assert facts["headcount_growth_12m"]["value"] == 0.12 and facts["technologies"]["value"] == ["Slack", "Google Workspace"]
    for e in facts.values():
        assert e["source"] == "apollo_org" and e["observed_at"] == NOW and len(e["quote"]) <= 300 and e["quote"]
        assert e["source_url"] == "https://app.apollo.io/#/organizations/org001"


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
])
def test_best_label(naics, keywords, label):
    got = uni.best_label(list(naics), " ; ".join(keywords), BASE)
    assert (got.industry if got else None) == label


def test_skipped_companies_and_why():
    ctx, _, _ = make([
        org(1, domain="gmail.com"),
        org(2, domain=None),
        org(3, state="California", _in="NY"),
        org(4, employees=600),
        org(5, naics=("541511",), keywords=("hr software", "payroll")),  # an HR-tech vendor: a partner
        org(6, naics=("111110",), keywords=("soybeans",), industry="farming", _always=True),
        org(11, naics=("524210",), keywords=("insurtech",), _always=True),  # an insurance agency: a partner
        org(7, naics=("541611",), keywords=()),  # management consulting, switched off
        org(8, domain="optedout.com"),
        org(9, domain="dupe.com"), org(10, domain="www.dupe.com"),
    ])
    suppression.add(ctx.store, domain="optedout.com", reason="test", source="test", now=NOW)
    out = uni.run(ctx)
    assert out["skipped"] == {
        "a personal email domain": 1, "no website": 1, "HQ outside the active states": 1, "outside 10 to 249 employees": 1,
        "a partner, never prospected": 2, "no Industries label fits": 1, "its best Industries label is switched off": 1,
        "suppressed": 1,
    }
    assert sorted(accounts_by_domain(ctx)) == ["dupe.com"] and out["created"] == 1  # one account per root domain
    assert ctx.store.get("partners", domain="company5.com")["reason"] == "hr_tech"
    assert ctx.store.get("partners", domain="company11.com")["reason"] == "broker"


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


@pytest.mark.banded
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


def test_it_stops_when_the_queue_holds_two_weeks():
    s = settings_with(weekly_enrol_cap=1)  # two weeks = 2 accounts
    ctx, _, fake = make([org(i) for i in range(1, 4)], settings=s)
    out = uni.run(ctx)
    # IL is read first (an empty page, 0 credits), then NY's first page fills the queue.
    assert (out["target"], out["created"], out["pages"], out["credits"]) == (2, 3, 2, 1.0)
    assert out["stopped_by"] == "the queue holds two weeks"
    again = uni.run(ctx)
    assert again["stopped_by"] == "the queue already holds two weeks" and len(fake.searches) == 2  # no call at all


def test_the_next_run_carries_on_from_the_next_page_and_a_new_month_starts_again():
    s = settings_with(weekly_enrol_cap=2)  # four accounts
    ctx, _, fake = make([org(i) for i in range(1, 151)], settings=s)
    uni.run(ctx)
    ctx.store.update("accounts", {}, {"status": "enrolled"})  # the queue empties
    ctx.now = NOW + timedelta(days=1)
    uni.run(ctx)
    ny = [b["page"] for b in fake.searches if b["organization_locations"] == ["New York, US"]
          and "5415" in b.get("organization_naics_codes", ())]
    assert ny == [1, 2]
    [first] = [n for n in (json.loads(r["note"]) for r in ledger(ctx)) if n["page"] == 1 and n["results"]]
    assert first["pages"] == 2 and first["results"] == 100 and first["total"] == 150
    ctx.store.update("accounts", {}, {"status": "enrolled"})
    ctx.now = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)  # a new month: the universe is read again from page 1
    before = len(fake.searches)
    uni.run(ctx)
    november = [b["page"] for b in fake.searches[before:] if b["organization_locations"] == ["New York, US"]
                and "5415" in b.get("organization_naics_codes", ())]
    assert november == [1, 2]  # page 1 again; its companies are accounts already, so page 2 too


def test_a_page_with_results_costs_one_credit_and_an_empty_page_none():
    ctx, _, _ = make([org(1)])
    uni.run(ctx)
    rows = ledger(ctx)
    assert sorted(r["credits"] for r in rows) == [0.0, 0.0, 0.0, 0.0, 0.0, 1.0]  # Legal Teams' two empty searches
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


def test_a_search_over_50_000_companies_is_split_by_size_band():
    def total(body):
        return 60_000 if len(body["organization_num_employees_ranges"]) == 4 and "5415" in body.get("organization_naics_codes", ()) else 0

    s = settings_with(states=("NY",))
    ctx, _, fake = make([org(i) for i in range(1, 3)], settings=s, total=total)
    uni.run(ctx)
    tech = [b for b in fake.searches if "5415" in b.get("organization_naics_codes", ())]
    assert [b["organization_num_employees_ranges"] for b in tech] == [
        ["10,19", "20,49", "50,99", "100,249"], ["10,19"], ["20,49"], ["50,99"], ["100,249"]]
    assert json.loads(ledger(ctx)[0]["note"])["split"] is True
    later = uni.cursors(ctx)
    assert later[uni.plan(s, uni.active_groups(s), ("NY",))["Technology & Startups"][0].key].split


def test_without_employee_counts_the_size_band_comes_from_a_size_band_search():
    s = settings_with(states=("NY",))
    ctx, _, fake = make([org(1, employees=None, _band="20-49"), org(2, employees=None, _band="100-249")], settings=s)
    out = uni.run(ctx)
    tech = [b["organization_num_employees_ranges"] for b in fake.searches if "5415" in b.get("organization_naics_codes", ())]
    assert tech == [["10,19", "20,49", "50,99", "100,249"], ["10,19"], ["20,49"], ["50,99"], ["100,249"]]
    got = {d: (a["employees"], a["size_band"]) for d, a in accounts_by_domain(ctx).items()}
    assert got == {"company1.com": (None, "20-49"), "company2.com": (None, "100-249")}
    assert out["created"] == 2 and json.loads(ledger(ctx)[0]["note"])["why"] == "no employee counts"
    assert len(ctx.store.select("signal_events", {"fact": "founded_year"})) == 2  # facts once per company a run


@pytest.mark.banded
def test_every_search_starts_by_size_band_so_no_account_lacks_one():
    """2 Oct 2026: Apollo's search rows carry no employee count, so a search with no size filter gave
    accounts no size band. Every search is now one per band, and the band is the filter's."""
    s = settings_with(states=("NY",))
    ctx, _, fake = make([org(1, employees=None, _band="20-49"), org(2, employees=None, _band="100-249")], settings=s)
    out = uni.run(ctx)
    assert all(len(b["organization_num_employees_ranges"]) == 1 for b in fake.searches)
    got = {d: (a["employees"], a["size_band"]) for d, a in accounts_by_domain(ctx).items()}
    assert got == {"company1.com": (None, "20-49"), "company2.com": (None, "100-249")}
    assert out["created"] == 2 and out["size_bands_backfilled"] == 0


def test_a_page_with_no_employee_counts_is_not_taken_unbanded():
    s = settings_with(states=("NY",))
    ctx, _, _ = make([org(1, employees=None, _band="20-49")], settings=s)
    uni.run(ctx)
    assert all(a["size_band"] for a in ctx.store.select("accounts"))  # only the band searches made accounts


@pytest.mark.banded
def test_accounts_left_with_no_size_band_get_one_from_a_search_by_their_ids():
    s = settings_with(states=("NY",), weekly_enrol_cap=1)  # the queue is already full: no new searches
    old = [org(1, employees=None, _band="20-49"), org(2, employees=None, _band="50-99"), org(3, employees=None)]
    ctx, _, fake = make(old, settings=s)
    ctx.store.insert("accounts", [
        {"account_id": f"a{i}", "domain": f"company{i}.com", "apollo_org_id": f"org{i:03d}", "status": "new",
         "industry": "Fintech", "industry_group": "Technology & Startups", "size_band": None, "employees": None,
         "first_seen": NOW}
        for i in (1, 2, 3)])
    out = uni.run(ctx)
    bands = {a["account_id"]: a["size_band"] for a in ctx.store.select("accounts")}
    assert bands == {"a1": "20-49", "a2": "50-99", "a3": None}  # no band search returned company 3
    assert out["size_bands_backfilled"] == 2 and out["stopped_by"] == "the queue already holds two weeks"
    backfill = [b for b in fake.searches if "organization_ids" in b]
    assert len(backfill) == 4 and all(len(b["organization_num_employees_ranges"]) == 1 for b in backfill)
    assert out["credits"] == 2.0  # a page with results costs 1; the two empty bands cost nothing


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


def acct(aid, org_id, status="new", **kw):
    return {"account_id": aid, "domain": f"{aid}.com", "apollo_org_id": org_id, "status": status,
            "industry": "Fintech", "industry_group": "Technology & Startups", "first_seen": NOW, **kw}


def facts_of(ctx, aid) -> dict:
    return {e["fact"]: e["value"] for e in ctx.store.select("signal_events", {"account_id": aid, "source": "apollo_jobs"})}


def test_job_postings_give_open_roles_open_people_roles_and_titles():
    ctx, t, fake = jobs_ctx(
        [acct("a1", "org001"), acct("a2", "org002", status="queued"), acct("a3", "org003", status="verified"),
         acct("a4", "org004", tier="Excluded"), acct("a5", None), acct("a6", "org006", status="enrolled")],
        postings={"org001": ["Senior Engineer", "HR Manager", "Senior Recruiter"], "org003": ["Designer"]},
    )
    ctx.store.insert("signal_events", [{"event_id": "f3", "account_id": "a3", "source": "apollo_jobs", "fact": "open_roles",
                                        "value": 1, "observed_at": NOW - timedelta(days=5)}])
    out = apollo_jobs.run(ctx)
    assert (out["candidates"], out["screened"], out["postings_read"], out["without_postings"]) == (2, 2, 1, 1)
    assert facts_of(ctx, "a1") == {"open_roles": 3, "open_people_roles": 1,
                                   "posting_titles": ["Senior Engineer", "HR Manager", "Senior Recruiter"]}
    assert facts_of(ctx, "a2") == {"open_roles": 0, "open_people_roles": 0, "posting_titles": []}
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
