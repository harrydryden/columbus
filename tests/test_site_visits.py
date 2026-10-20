"""The site_visits job (sources/site_visits.py; Harry, 5 Oct 2026), against a fake Apollo: the visitor searches and
their bodies, credits and caps, matching through aliases and Apollo ids, facts written only when they change and
cleared when visits stop, a site_visit event for a company's first appearance on the 30-day list since the last run
(W3-3), new US visitors of 10 to 249 people by the front door (and no longer held for the hand-check), weekly runs
while quiet, the tracker message when Apollo has no data, and the default Signals then scoring the visit."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound import suppression, verify
from us_outbound.clients.guard import APOLLO_READ_ACTIONS
from us_outbound.ops.health import run_job
from us_outbound.settings.model import Override
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all
from us_outbound.sources import site_visits as sv

NOW = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # Monday 5 Oct, 06:00 UK: the job's time
YESTERDAY = "2026-10-04"  # the UK day of the last run's list (seen())
BASE, _ = validate_all(default_tabs())
MESSAGE = ("No website-visitor data from Apollo for spill.chat: check that Apollo's website tracker is installed on "
           "spill.chat (Apollo → Settings → Website Visitors) and that the plan includes website visitors.")


def visitor(i: int, *, domain: str | None = None, paths=("/us",), ago: int = 0, us: bool = True,
            employees: int | None = 35, state: str = "New York", bucket: str = "organizations", found: bool = True,
            enriched: dict | None = None, **kw) -> dict:
    """A visiting company: its search row, and what Apollo's enrich adds (_employees, _enriched; none if not _found)."""
    d = domain or f"visitor{i}.com"
    return {"id": f"org{i:03d}", "name": f"Visitor {i}, Inc.", "primary_domain": d, "website_url": f"http://www.{d}",
            "city": "New York", "state": state, "country": "United States" if us else "United Kingdom",
            "naics_codes": ["541511"], "keywords": ["fintech"], "industry": "financial services", "founded_year": 2015,
            "_paths": list(paths), "_ago": ago, "_employees": employees, "_bucket": bucket, "_found": found,
            "_enriched": enriched or {}, **kw}


class FakeApollo:
    """Organization search with the website-visitor filters, bulk organization enrich, and the credit balance.

    A visitor is listed by a visitor search when it visited, within the window (_ago days ago, under
    from_past), a page whose path contains one of the searched paths. Enrich answers the asked domains
    Apollo knows (_found) with the row, its employee count (_employees) and any _enriched fields.
    """

    def __init__(self, visitors=(), *, balance=30_000, total=None):
        self.visitors, self.balance, self.total = list(visitors), balance, total
        self.bodies: list[dict] = []

    def install(self, t: FakeTransport) -> FakeTransport:
        t.route("POST", "/usage_stats/credit_usage_stats",
                body={"credit_usage_stats": {"lead_credit": {"left_over": self.balance}}})
        t.route("POST", "/mixed_companies/search", fn=self.search)
        t.route("POST", "/organizations/bulk_enrich", fn=self.enrich)
        return t

    @property
    def visitor_searches(self) -> list[dict]:
        return [b for b in self.bodies if "website_visitors_from_domains" in b]

    @property
    def enriches(self) -> list[list[str]]:
        return [b["domains"] for b in self.bodies if "domains" in b]

    def enrich(self, req):
        body = req.json
        self.bodies.append(body)
        found = [{**self._row({**o, "_bucket": "organizations"}), "estimated_num_employees": o["_employees"],
                  **o["_enriched"]} for o in self.visitors if o["primary_domain"] in body["domains"] and o["_found"]]
        return {"organizations": found}

    @staticmethod
    def _row(o: dict) -> dict:
        if o["_bucket"] == "accounts":  # a company saved in Apollo: an account id, and the organization id apart
            return {"id": f"acct-{o['id']}", "organization_id": o["id"], "domain": o["primary_domain"], "name": o["name"],
                    "state": o["state"], "country": o["country"]}
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def search(self, req):
        body = req.json
        self.bodies.append(body)
        if "website_visitors_from_domains" in body:
            assert body["website_visitors_from_domains"] == ["spill.chat"]
            days, paths = body["website_visitors_from_past"], body.get("website_visitors_domain_pages") or [""]
            found = [self._row(o) for o in self.visitors
                     if o["_ago"] < days and any(p in path for path in o["_paths"] for p in paths)]
        else:
            raise AssertionError(f"an organization search other than the visitor list: {sorted(body)}")
        page, per = body["page"], body["per_page"]
        total = self.total if self.total is not None else len(found)
        return {"organizations": [r for r in found if "organization_id" not in r][(page - 1) * per : page * per],
                "accounts": [r for r in found if "organization_id" in r][(page - 1) * per : page * per],
                "pagination": {"page": page, "per_page": per, "total_entries": total}}


def make(visitors=(), *, now=NOW, store=None, settings=BASE, **apollo):
    fake = FakeApollo(visitors, **apollo)
    t = fake.install(FakeTransport())
    ctx = make_context(settings, job=sv.JOB, now=now, transport=t, store=store)
    return ctx, t, fake


def account(ctx, domain: str, *, account_id: str | None = None, status: str = "verified", **kw) -> str:
    aid = account_id or f"acct-{domain}"
    ctx.store.upsert("accounts", [{"account_id": aid, "domain": domain, "status": status, "hq_state": "NY",
                                   "industry": "Fintech", "industry_group": "Technology & Startups",
                                   "size_band": "20-49", "source": "apollo", "first_seen": NOW - timedelta(days=3), **kw}])
    return aid


def facts(ctx, aid: str, fact: str | None = None) -> list[dict]:
    rows = [e for e in ctx.store.select("signal_events", {"account_id": aid, "source": sv.SOURCE})
            if fact is None or e["fact"] == fact]
    return sorted(rows, key=lambda e: e["observed_at"])


def matched_signals(ctx, aid: str) -> set[str]:
    return {e["value"]["signal"] for e in ctx.store.select("signal_events", {"account_id": aid, "source": "scoring"})}


def ledger(ctx) -> list[dict]:
    return ctx.store.select("credit_ledger", {"job": sv.JOB})


def seen(ctx, keys=(), days_ago=1):
    """The last run's 30-day list, as source_state keeps it: these row keys, read days_ago."""
    ctx.store.upsert("source_state", [{"key": sv.LIST_KEY, "value": {"keys": list(keys), "at": (
        NOW - timedelta(days=days_ago)).isoformat()}, "updated_at": NOW}])


# -- the searches -------------------------------------------------------------------------------------


def test_two_read_only_visitor_searches_for_the_tracked_domain():
    """The 30-day lists of the US pages and of the pricing and demo pages; the one-day search went with W3-3."""
    ctx, t, fake = make()  # nobody visited: every page is empty and free
    out = sv.run(ctx)
    assert out["status"] == "ok" and out["credits"] == 0.0
    assert [(b["website_visitors_from_past"], b["website_visitors_domain_pages"]) for b in fake.visitor_searches] == [
        (30, ["/us"]), (30, ["/us/pricing", "/us/demo", "/us/book"])]
    assert all((b["page"], b["per_page"]) == (1, 100) for b in fake.visitor_searches)
    assert fake.enriches == []  # no visitor without an account: nothing looked up
    apollo = [c for c in ctx.guard.calls if c.system == "apollo"]
    assert {c.action for c in apollo} == {"usage.credits", "website_visitors.search"} <= APOLLO_READ_ACTIONS
    assert not any(c.write for c in apollo)
    assert not any("tracker" in r.url for r in t.requests)  # the tracker's settings are never read or changed
    assert [r["credits"] for r in ledger(ctx)] == [0.0, 0.0]  # recorded all the same: what was asked for


def test_the_constants_choose_the_domain_and_the_pages(monkeypatch):
    """The domain and pages are constants in code since W2-4 (settings/model.SITE_VISIT_*, at the live sheet's
    values); the searches follow them, and no intent paths means no intent search."""
    assert (sv.SITE_VISIT_DOMAIN, sv.SITE_VISIT_US_PATHS, sv.SITE_VISIT_INTENT_PATHS) == (
        "spill.chat", ("/us",), ("/us/pricing", "/us/demo", "/us/book"))
    for path in sv.SITE_VISIT_US_PATHS + sv.SITE_VISIT_INTENT_PATHS:
        assert path.startswith("/")  # what the sheet checked while they were General keys
    monkeypatch.setattr(sv, "SITE_VISIT_US_PATHS", ("/us/", "/en-us"))
    monkeypatch.setattr(sv, "SITE_VISIT_INTENT_PATHS", ())
    ctx, _, fake = make()
    sv.run(ctx)
    assert [b["website_visitors_domain_pages"] for b in fake.visitor_searches] == [["/us/", "/en-us"]]  # no intent search
    monkeypatch.setattr(sv, "SITE_VISIT_DOMAIN", "")
    ctx, _, fake = make()
    out = sv.run(ctx)
    assert out["skipped"] and "SITE_VISIT_DOMAIN is blank" in out["reason"] and fake.bodies == []


# -- matching, facts, events and the score ------------------------------------------------------------


def test_a_matched_account_gets_both_facts_an_event_and_the_default_signals_fire():
    """acme.com was not on yesterday's list: its first appearance is a visit, dated at yesterday's read."""
    ctx, _, _ = make([visitor(1, domain="acme.com", paths=("/us", "/us/pricing"))])
    aid = account(ctx, "acme.com")
    seen(ctx)
    out = sv.run(ctx)
    assert [(e["fact"], e["value"]) for e in facts(ctx, aid)] == [("us_visits_30d", 1), ("pricing_or_demo_visits_30d", 1)]
    us = facts(ctx, aid, "us_visits_30d")[0]
    assert us["quote"] == "Apollo: visited the US site on spill.chat (pages containing /us) in the last 30 days"
    assert us["source_url"] == "https://app.apollo.io/#/organizations/org001"
    [ev] = ctx.store.select("events", {"account_id": aid})
    assert (ev["event_id"], ev["type"], ev["occurred_at"]) == (f"site-visit:{aid}:{YESTERDAY}", "site_visit",
                                                               NOW - timedelta(days=1))
    # The rescore at the end: the visit signals count before the 12:00 enrol (the default Signals rows).
    assert {"Visited the US site", "Viewed US pricing or demo page"} <= matched_signals(ctx, aid)
    assert ctx.store.get("accounts", account_id=aid)["tier"] == "Queue"
    assert out["credits"] == 2.0 and len(ledger(ctx)) == 2 and out["matched"] == {"us": 1, "intent": 1}
    assert (out["new_visits"], out["events"]) == (1, 1)
    assert [r["key"] for r in ctx.store.select("raw_site_visits")] == ["org001"]  # once a run, as received
    assert ctx.store.select("raw_site_visits")[0]["payload"]["searches"] == ["us", "intent"]
    assert ctx.store.get("source_state", key=sv.LIST_KEY)["value"]["keys"] == ["org001"]


def test_a_visit_is_a_first_appearance_on_the_30_day_list_since_the_last_run():
    """W3-3 (code-sources B9): Monday's list holds acme; Tuesday's holds acme and newco (an account of ours): one event,
    newco's, dated at Monday's read; Wednesday's is unchanged: none. The first look of all (no list kept, no earlier
    raw rows) only keeps the list."""
    ctx, _, fake = make([visitor(1, domain="acme.com")])
    acme, newco = account(ctx, "acme.com"), account(ctx, "newco.com")
    first = sv.run(ctx)
    assert (first["first_look"], first["new_visits"], first["events"]) == (True, 0, 0)
    fake.visitors.append(visitor(2, domain="newco.com", ago=0))
    tue = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=1), run_id="tue"))
    assert (tue["new_visits"], tue["events"]) == (1, 1)
    [ev] = ctx.store.select("events", {"type": "site_visit"})
    assert (ev["account_id"], ev["occurred_at"], ev["event_id"]) == (newco, NOW, f"site-visit:{newco}:2026-10-05")
    wed = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=2), run_id="wed"))
    assert (wed["new_visits"], wed["events"]) == (0, 0) and acme not in {e["account_id"] for e in ctx.store.select("events")}


def test_with_no_list_kept_the_last_run_s_raw_rows_are_the_list():
    """The first run after W3-3 deploys: no site_visits:list yet, so the earlier run's raw_site_visits rows (the us
    search's) are the last list. acme was on it; newco was not, so newco is the one visit."""
    ctx, _, _ = make([visitor(1, domain="acme.com"), visitor(2, domain="newco.com")])
    account(ctx, "acme.com")
    newco = account(ctx, "newco.com")
    ctx.store.insert("raw_site_visits", [{"loaded_at": NOW - timedelta(days=1), "run_id": "before", "key": "org001",
                                          "payload": {"searches": ["us_today", "us"], "record": {}}}])
    out = sv.run(ctx)
    assert (out["new_visits"], out["first_look"]) == (1, False)
    assert [e["account_id"] for e in ctx.store.select("events", {"type": "site_visit"})] == [newco]


def test_matching_goes_through_aliases_the_accounts_bucket_and_apollo_ids():
    ctx, _, _ = make([visitor(1, domain="acme.io"), visitor(2, domain="betaco.com", bucket="accounts"),
                      visitor(3, domain="gamma-group.com")])
    ctx.store.upsert("domain_aliases", [{"alias": "acme.io", "root_domain": "acme.com", "source": "test", "added_at": NOW}])
    acme, beta = account(ctx, "acme.com"), account(ctx, "betaco.com")
    gamma = account(ctx, "gamma.com", apollo_org_id="org003")
    out = sv.run(ctx)
    for aid in (acme, beta, gamma):
        assert [e["value"] for e in facts(ctx, aid, "us_visits_30d")] == [1], aid
        assert "Visited the US site" in matched_signals(ctx, aid)
    assert out["created"] == [] and len(ctx.store.select("accounts")) == 3  # no second account for an alias


def test_facts_are_written_only_when_they_change_and_cleared_when_the_visits_stop():
    ctx, _, fake = make([visitor(1, domain="acme.com", paths=("/us", "/us/demo"))])
    aid = account(ctx, "acme.com")
    seen(ctx)
    sv.run(ctx)
    again = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(hours=4), run_id="rerun"))  # a rerun the same morning
    assert again["facts_written"] == 0 and again["events"] == 0
    assert len(facts(ctx, aid)) == 2 and len(ctx.store.select("events")) == 1  # one event a day, whatever the reruns
    next_day = dataclasses.replace(ctx, now=NOW + timedelta(days=1), run_id="tuesday")
    fake.visitors[0]["_ago"] = 1  # no visit since: still in the 30-day window, so no new visit
    out = sv.run(next_day)
    assert out["facts_written"] == 0 and out["events"] == 0
    fake.visitors.clear()  # out of Apollo's window
    out = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=2), run_id="wednesday"))
    assert out["facts_cleared"] == 2
    assert [e["value"] for e in facts(ctx, aid, "us_visits_30d")] == [1, 0]
    assert [e["value"] for e in facts(ctx, aid, "pricing_or_demo_visits_30d")] == [1, 0]
    assert not {"Visited the US site", "Viewed US pricing or demo page"} & matched_signals(ctx, aid)  # the signal stops
    out = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=3), run_id="thursday"))
    assert out["facts_cleared"] == 0 and out["rescore"] == "nothing changed"  # a 0 is written once


def test_a_company_that_keeps_visiting_is_written_again_before_its_fact_goes_stale():
    ctx, _, _ = make([visitor(1, domain="acme.com")])
    aid = account(ctx, "acme.com")
    sv.run(ctx)
    assert sv.refresh_days(ctx.settings) == sv.REFRESH_DAYS == 7  # the visit signals count facts for 30 days
    out = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=6), run_id="day6"))
    assert out["facts_written"] == 0
    out = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=7), run_id="day7"))
    assert out["facts_written"] == 1 and [e["value"] for e in facts(ctx, aid, "us_visits_30d")] == [1, 1]


def test_a_search_not_read_to_its_end_clears_nothing(monkeypatch):
    monkeypatch.setattr(sv, "MAX_PAGES_PER_SEARCH", 1)
    many = [visitor(i, ago=5) for i in range(1, 151)]  # 150 companies: two pages of 100
    ctx, _, fake = make(many)
    aid = account(ctx, "gone.com")
    ctx.store.insert("signal_events", [{"event_id": "old", "account_id": aid, "source": sv.SOURCE, "fact": "us_visits_30d",
                                        "value": 1, "quote": "", "source_url": "", "observed_at": NOW - timedelta(days=2)}])
    out = sv.run(ctx)
    assert out["searches"]["us"]["complete"] is False and out["facts_cleared"] == 0
    assert [e["value"] for e in facts(ctx, aid, "us_visits_30d")] == [1]


# -- credits ------------------------------------------------------------------------------------------


def test_credits_are_recorded_and_capped(monkeypatch):
    monkeypatch.setattr(sv, "MAX_CREDITS_PER_RUN", 1)
    ctx, _, fake = make([visitor(1, domain="acme.com", paths=("/us", "/us/pricing"))])
    account(ctx, "acme.com")
    out = sv.run(ctx)
    assert out["credits"] == 1.0 and out["credit_cap"] == 1.0
    assert [b["website_visitors_domain_pages"] for b in fake.visitor_searches] == [["/us"]]  # the US list first
    assert out["searches"]["intent"]["stopped"] == "the run's cap of 1 credits"
    assert [(r["credits"], r["job"], r["run_id"]) for r in ledger(ctx)] == [(1.0, "site_visits", ctx.run_id)]


def test_nothing_is_spent_below_the_floor_or_once_the_month_is_used():
    ctx, _, fake = make([visitor(1)], balance=4_000)
    out = sv.run(ctx)
    assert out["skipped"] and "below apollo_floor" in out["reason"] and fake.bodies == []
    ctx, _, fake = make([visitor(1)])
    ctx.store.insert("credit_ledger", [{"entry_id": "e1", "system": "apollo", "job": "pick_contacts", "credits": 2000.0,
                                        "usd": 0.0, "occurred_at": NOW - timedelta(days=1), "note": ""}])
    out = sv.run(ctx)
    assert out["skipped"] and "apollo_monthly_credits" in out["reason"] and fake.bodies == []


def test_dry_run_reads_and_writes_only_the_database():
    ctx, t, _ = make([visitor(1, domain="acme.com")])
    assert ctx.dry_run
    aid = account(ctx, "acme.com")
    seen(ctx)
    sv.run(ctx)
    assert facts(ctx, aid) and ctx.store.select("events")  # database writes, as every source makes in dry-run
    assert ctx.store.select("source_state")
    outside = [c for c in ctx.guard.calls if c.system != "db" and c.write]
    assert outside == [] and {r.url.rsplit("/", 1)[-1] for r in t.requests} <= {"credit_usage_stats", "search"}


# -- new visitors ---------------------------------------------------------------------------------------


def test_a_new_us_visitor_of_10_to_249_comes_in_by_the_front_door():
    ctx, _, fake = make([visitor(1, domain="newco.com", paths=("/us/book-demo",), employees=60)])
    out = sv.run(ctx)
    assert fake.enriches == [["newco.com"]]  # looked up in Apollo: search rows carry no employee count
    [new] = ctx.store.select("accounts")
    assert (new["domain"], new["source"], new["status"], new["apollo_org_id"]) == ("newco.com", "site_visit", "new", "org001")
    assert (new["hq_state"], new["industry"], new["industry_group"]) == ("NY", "Fintech", "Technology & Startups")
    assert (new["employees"], new["size_band"]) == (60, "50-99")
    assert out["created"] == ["newco.com"]
    assert out["credits"] == 3.0 and out["enriched"] == 1  # two searches and the one company Apollo found
    org_facts = {e["fact"] for e in ctx.store.select("signal_events", {"account_id": new["account_id"], "source": "apollo_org"})}
    assert {"naics", "hq_state", "keywords", "employees"} <= org_facts
    assert ctx.store.select("signal_events", {"source": verify.DOUBT_SOURCE}) == []
    assert [(e["fact"], e["value"]) for e in facts(ctx, new["account_id"])] == [("us_visits_30d", 1),
                                                                               ("pricing_or_demo_visits_30d", 1)]
    assert "Visited the US site" in matched_signals(ctx, new["account_id"])


def test_visitors_surely_not_for_us_never_come_in_and_are_not_judged_again():
    ctx, _, fake = make([visitor(1, domain="ukco.co.uk", us=False), visitor(2, domain="bigco.com", employees=900),
                         visitor(3, domain="tinyco.com", employees=4)])
    out = sv.run(ctx)
    assert ctx.store.select("accounts") == [] and out["created"] == []
    assert out["not_admitted"] == {"outside the US": 1, "outside 10 to 249 employees": 2}
    assert fake.enriches == [["bigco.com", "tinyco.com"]]  # the UK company is out on its search row, at no cost
    sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=1), run_id="tuesday"))
    assert len(fake.enriches) == 1  # decided on in the last 30 days: not judged again
    later = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=31), run_id="later"))
    assert len(fake.enriches) == 2 and later["not_admitted"]["outside the US"] == 1


def test_a_visitor_apollo_leaves_in_doubt_comes_in_as_any_other_for_verify_to_decide():
    """W3-3 (code-sources B10): no doubtful_facts fact and no hold for the hand-check. Each comes in new, and
    verify_accounts leaves out the ones missing a fact (Harry, 14 Oct 2026)."""
    ctx, _, fake = make([
        visitor(1, domain="nostate.com", state="", country="", enriched={"state": None, "country": None}),
        visitor(2, domain="nosize.com", employees=None),
        visitor(3, domain="unknown.com", found=False),  # Apollo's enrich does not know it: its row is all there is
        visitor(4, domain="edgeco.com", employees=251),  # near the 250 edge: maybe ours
        visitor(5, domain="bare.com", naics_codes=[], keywords=[], industry=""),
    ])
    out = sv.run(ctx)
    assert sorted(out["created"]) == ["bare.com", "edgeco.com", "nosize.com", "nostate.com", "unknown.com"]
    assert "held_for_hand_check" not in out
    assert out["credits"] == 5.0  # the US list (the pricing pages had none) and the four Apollo found
    assert ctx.store.select("signal_events", {"source": verify.DOUBT_SOURCE}) == [] and verify.open_doubts(ctx) == []
    bare = ctx.store.get("accounts", domain="bare.com")
    assert (bare["industry"], bare["industry_group"], bare["status"]) == (None, None, "new")


def test_an_overrides_row_fills_a_visitor_s_missing_fact():
    s = dataclasses.replace(BASE, overrides=(Override("nostate.com", "hq_state", "TX"),))
    ctx, _, _ = make([visitor(1, domain="nostate.com", state="", country="")], settings=s)
    out = sv.run(ctx)
    assert out["created"] == ["nostate.com"]
    assert ctx.store.get("accounts", domain="nostate.com")["hq_state"] == "TX"


def test_lookups_are_capped_and_the_rest_are_looked_up_next_run(monkeypatch):
    monkeypatch.setattr(sv, "MAX_ENRICH_PER_RUN", 10)
    ctx, _, fake = make([visitor(i) for i in range(1, 13)])
    out = sv.run(ctx)
    assert (out["enriched"], out["deferred"], len(out["created"])) == (10, 2, 10)
    assert [len(d) for d in fake.enriches] == [10]
    nxt = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=1), run_id="tuesday"))
    assert (nxt["enriched"], nxt["deferred"], len(nxt["created"])) == (2, 0, 2)


def test_a_lookup_apollo_did_not_answer_is_reserved_first_kept_and_asked_again():
    """Defect 9 (9 Oct 2026): the lookup reserved nothing and counted 0 when it failed. It is now reserved at a credit
    a company before the call; a 5xx keeps it (it may have been charged); its companies are not taken as judged."""
    ctx, t, fake = make([visitor(1, domain="newco.com"), visitor(2, domain="other.com")])
    t.route("POST", "/organizations/bulk_enrich", status=503, body={"error": "unavailable"})
    out = sv.run(ctx)
    assert out["created"] == [] and out["credits"] == 3.0  # the US list with results, and the lookup's 2
    [lookup] = [r for r in ledger(ctx) if "enrich" in json.loads(r["note"]).get("reserved", {})]
    assert lookup["credits"] == 2.0 and json.loads(lookup["note"])["failed"] == 503
    assert sv.judged_recently(ctx) == set()
    t.routes.pop()  # Apollo answers again
    nxt = sv.run(dataclasses.replace(ctx, now=NOW + timedelta(days=1), run_id="tuesday"))
    assert nxt["created"] == ["newco.com", "other.com"]
    settled = [json.loads(r["note"]) for r in ledger(ctx) if r["run_id"] == "tuesday"]
    assert {"enrich": 2, "found": 2} in settled  # what was paid for; the ledger holds no job state (W3-3)
    assert sv.judged_recently(dataclasses.replace(ctx, now=NOW + timedelta(days=1))) == {"org001", "org002"}


def test_visitors_the_old_screen_turned_away_are_judged_again():
    ctx, _, fake = make([visitor(1, domain="nostate.com", state="", country="")])
    ctx.store.insert("credit_ledger", [{"entry_id": "e1", "system": "apollo", "job": sv.JOB, "credits": 1.0, "usd": 0.0,
                                        "occurred_at": NOW - timedelta(days=1), "run_id": "old",
                                        "note": '{"screen": 1, "asked": ["org001"], "kept": ["org001"]}'}])
    out = sv.run(ctx)
    assert fake.enriches == [["nostate.com"]] and out["created"] == ["nostate.com"]


def test_a_visitor_in_any_us_state_comes_in_and_the_front_door_refuses_suppressed_and_partners():
    ctx, _, fake = make([visitor(1, domain="calco.com", state="California"), visitor(2, domain="hushed.com"),
                      visitor(3, domain="broker.com"), visitor(4, domain="txco.com", state="Texas")])
    suppression.add(ctx.store, domain="hushed.com", reason="test", source="test", now=NOW)
    ctx.store.upsert("partners", [{"domain": "broker.com", "name": "Broker", "reason": "broker", "naics": None,
                                   "added_at": NOW}])
    out = sv.run(ctx)
    assert out["created"] == ["calco.com", "txco.com"]  # never excluded on its state (Harry, 6 Oct 2026)
    assert out["not_admitted"] == {"suppressed": 1, "a partner, never prospected": 1}
    assert sorted(d for call in fake.enriches for d in call) == ["broker.com", "calco.com", "hushed.com", "txco.com"]
    calco = ctx.store.get("accounts", domain="calco.com")
    assert (calco["hq_state"], calco["hq_country"], calco["source"]) == ("CA", "United States", "site_visit")
    assert verify.check(calco, {}, ctx.settings, set(), set()) is None  # verify takes it, CA and all


# -- no data ---------------------------------------------------------------------------------------------


def test_apollo_refusing_the_visitor_filters_ends_ok_with_the_tracker_message():
    t = FakeTransport()
    t.route("POST", "/usage_stats/credit_usage_stats", body={"credit_usage_stats": {"lead_credit": {"left_over": 30_000}}})
    t.route("POST", "/mixed_companies/search", status=403, body={"error": "website visitors are not on this plan"})
    ctx = make_context(BASE, job=sv.JOB, now=NOW, transport=t)
    summary = run_job(ctx, sv.run)
    assert summary["status"] == "ok" and summary["tracker_check"] == MESSAGE
    assert "HTTP 403" in summary["refused"]
    assert len([r for r in t.requests if r.url.endswith("/mixed_companies/search")]) == 1  # not asked again this run
    assert ctx.store.select("heartbeats")[0]["status"] == "ok"
    assert sv.post_line(ctx) == MESSAGE  # the daily post's Sources section


def test_an_empty_30_day_list_brings_the_tracker_message():
    ctx, _, _ = make()
    assert sv.run(ctx)["tracker_check"] == MESSAGE


def test_five_quiet_runs_make_it_weekly_and_seven_bring_the_tracker_message():
    """One visitor that stays on the list: no new visit, so every run is quiet. Runs on days 0 to 4 make 5 quiet runs;
    from then a start less than 7 days after the last read is skipped (alive for heartbeat_check): days 5 to 10. Day 11
    reads (6), day 18 reads (7: the tracker message). A new visitor on day 25's read ends the quiet."""
    ctx, _, fake = make([visitor(1, domain="acme.com")])
    account(ctx, "acme.com")

    def on(day):
        return run_job(dataclasses.replace(ctx, now=NOW + timedelta(days=day), run_id=f"day{day}"), sv.run)

    for day in range(5):
        assert on(day)["quiet_runs"] == day + 1
    for day in range(5, 11):
        out = on(day)
        assert out["skipped"] and out["reason"].startswith("quiet: 5 runs in a row found no new visitor"), day
    assert ctx.store.latest("heartbeats", "started_at", {"job": sv.JOB})["status"] == "skipped"
    assert (on(11)["quiet_runs"], on(12)["skipped"]) == (6, True)
    out = on(18)
    assert out["quiet_runs"] == 7 and out["tracker_check"] == MESSAGE
    fake.visitors.append(visitor(2, domain="newco.com"))
    out = on(25)
    assert (out["quiet_runs"], out["new_visits"], out["tracker_check"]) == (0, 1, "")
    assert not on(26).get("skipped")  # daily again


def test_an_implausible_total_means_the_filters_were_ignored_and_nothing_is_used():
    ctx, _, fake = make([visitor(1, domain="acme.com")], total=2_000_000)
    aid = account(ctx, "acme.com")
    out = sv.run(ctx)
    assert out["searches"]["us"]["problem"].startswith("implausible: Apollo says 2,000,000 companies visited")
    assert len(fake.visitor_searches) == 1 and out["credits"] == 1.0  # the others would be ignored the same way
    assert facts(ctx, aid) == [] and ctx.store.select("events") == [] and fake.enriches == []
    assert ctx.store.select("raw_site_visits") == [] and out["created"] == []
    assert out["tracker_check"] == MESSAGE and "filters were ignored" in out["refused"]


# -- the daily post ---------------------------------------------------------------------------------------


def test_the_daily_post_line():
    ctx, _, _ = make([visitor(1, domain="acme.com", paths=("/us", "/us/pricing")), visitor(2, domain="newco.com", ago=3)])
    assert sv.post_line(ctx) == "Site visits (spill.chat): not read yet (site_visits, daily 06:00)."
    account(ctx, "acme.com")
    run_job(ctx, sv.run)
    assert sv.post_line(ctx) == ("Site visits (spill.chat, Apollo): 2 companies on the US pages in the last 30 days "
                                 "(2 of them ours, 1 on pricing or demo pages); 0 new to the list since the last run; "
                                 "1 new account.")


@pytest.mark.parametrize("days, want", [(30, 7), (5, 4), (1, 1)])
def test_refresh_follows_the_shortest_visit_signal(days, want):
    signals = tuple(dataclasses.replace(s, counts_for_days=days) if "site_visits" in s.sources else s
                    for s in BASE.signals)
    assert sv.refresh_days(dataclasses.replace(BASE, signals=signals)) == want


def test_an_association_or_a_public_body_is_judged_out_before_any_label():
    ctx = make_context(BASE, job=sv.JOB, now=NOW)
    assert sv.judge(ctx, visitor(1, naics_codes=["813910"], keywords=["networking"])).out == (
        "an association or chamber, never prospected")
    assert sv.judge(ctx, visitor(2, industry="Government Administration")).out == "a public body, never prospected"
    assert sv.judge(ctx, visitor(3)).out == ""  # the fintech visitor is still in
