"""apollo_enrich (sources/apollo_enrich.py; Harry, 2 Oct 2026): funding and an exact headcount from Apollo's
organization enrich, for the General apollo_enrich_groups, against a fake Apollo. Which accounts and in what
order, the facts and their ages, not found against no funding, the 180-day refresh, the account's size, the
credits, and the funding signals firing in the rescore that follows."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound import verify
from us_outbound.clients.guard import APOLLO_READ_ACTIONS
from us_outbound.ops import cli, schedule
from us_outbound.ops import heartbeat as hb
from us_outbound.scoring import score
from us_outbound.settings import load as loader
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import SOURCE_FIELDS, Focus, Override
from us_outbound.settings.validate import validate_all
from us_outbound.sources import apollo_enrich as enrich
from us_outbound.sources import apollo_jobs, apollo_universe as uni

NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # Monday 5 Oct, 05:00 UK, the weekday start since W1-3a
BASE, _ = validate_all(default_tabs())
TECH, AGENCIES = "Technology & Startups", "Marketing & Creative Agencies"


def settings_with(*, focus=(), overrides=(), **general):
    g = dataclasses.replace(BASE.general, **general)
    return dataclasses.replace(BASE, general=g, focus=tuple(focus), overrides=tuple(overrides))


def company(domain: str, *, funded: str | None = "2026-03-01", stage="Series A", amount="8M", employees=64, **kw) -> dict:
    """An organization enrich record (PHASE0-CONFIRM shapes: funding_events[].amount as text, with a currency)."""
    org = {
        "id": f"org-{domain.split('.')[0]}", "name": domain.split(".")[0].title(), "primary_domain": domain,
        "website_url": f"https://www.{domain}", "estimated_num_employees": employees,
        "organization_headcount_twelve_month_growth": 0.18, "short_description": "Payments software for clinics.",
        "keywords": ["fintech", "payments"], "technology_names": ["Slack", "HubSpot"], "naics_codes": ["541511"],
        "founded_year": 2015, "industry": "financial services", "city": "Austin", "state": "Texas",
    }
    if funded:
        org.update(latest_funding_round_date=funded, latest_funding_stage=stage,
                   funding_events=[{"date": f"{funded}T00:00:00.000+00:00", "type": stage, "amount": amount, "currency": "$"}])
    return {**org, **kw}


class FakeApollo:
    """Organization enrich, single and bulk, and the credit balance, over records keyed by the domain asked."""

    def __init__(self, orgs=(), *, balance=30_000, bulk_status=200, filed_under=None):
        self.records = {o["primary_domain"]: o for o in orgs}
        self.records.update(filed_under or {})  # a domain asked -> the record Apollo files it under
        self.balance, self.bulk_status = balance, bulk_status
        self.bulk_asked: list[list[str]] = []
        self.single_asked: list[str] = []

    def install(self, t: FakeTransport) -> FakeTransport:
        t.route("POST", "/usage_stats/credit_usage_stats",
                body={"credit_usage_stats": {"lead_credit": {"left_over": self.balance}}})
        t.route("POST", "/organizations/bulk_enrich", fn=self.bulk, status=self.bulk_status)
        t.route("GET", "/organizations/enrich", fn=self.single)
        return t

    def bulk(self, req):
        self.bulk_asked.append(list(req.json["domains"]))
        if self.bulk_status >= 300:
            return {"error": "not found"}
        found = [self.records[d] for d in req.json["domains"] if d in self.records]
        return {"status": "success", "organizations": found, "unique_enriched_records": len(found),
                "missing_records": len(req.json["domains"]) - len(found)}

    def single(self, req):
        self.single_asked.append(req.params["domain"])
        org = self.records.get(req.params["domain"])
        return {"organization": org} if org else {}


def acct(aid: str, domain: str | None = None, *, group=TECH, industry="Fintech", status="verified", **kw) -> dict:
    return {"account_id": aid, "domain": f"{aid}.com" if domain is None else domain, "apollo_org_id": f"org-{aid}",
            "status": status, "industry": industry, "industry_group": group, "size_band": "20-49", "employees": None,
            "hq_state": "NY", "first_seen": NOW - timedelta(days=1), **kw}


def make(accounts=(), orgs=(), *, settings=None, now=NOW, live=False, **apollo):
    fake = FakeApollo(orgs, **apollo)
    t = fake.install(FakeTransport())
    ctx = make_context(settings or settings_with(), job=enrich.JOB, now=now, transport=t, live=live)
    if accounts:
        ctx.store.insert("accounts", list(accounts))
    return ctx, t, fake


def facts_of(ctx, aid) -> dict:
    return {e["fact"]: e for e in ctx.store.select("signal_events", {"account_id": aid, "source": "apollo_org"})}


def marker(aid, days_ago, outcome=enrich.FOUND) -> dict:
    return {"event_id": f"m-{aid}-{days_ago}", "account_id": aid, "source": "apollo_org", "fact": enrich.MARKER,
            "value": {"outcome": outcome, "run_id": "earlier"}, "observed_at": NOW - timedelta(days=days_ago)}


def ledger(ctx) -> list[dict]:
    return ctx.store.select("credit_ledger", {"job": enrich.JOB})


# -- which accounts, in what order ---------------------------------------------------------------------


def test_targets_are_verified_accounts_in_the_named_groups_never_enriched_first_in_enrol_order():
    """W1-3a (code-sources B4): only accounts verify_accounts has passed are paid for. A new or queued account, which
    the label check may still leave out (13 Oct: 62 of 103), is not enriched however high it scores."""
    ctx, _, fake = make([
        acct("standard", tier="Standard", score=30),
        acct("priority", tier="Priority", score=60),
        acct("control", tier="Control", score=0),
        acct("old", tier="Priority", score=90),  # enriched 200 days ago: due again, after the never-enriched
        acct("recent", tier="Priority", score=90),  # enriched 30 days ago: not due
        acct("excluded", tier="Excluded"), acct("held", tier="Held"), acct("enrolled", status="enrolled"),
        acct("agency", group=AGENCIES, industry="Advertising agencies"), acct("nodomain", domain=""),
        acct("new", status="new", tier="Priority", score=99), acct("queued", status="queued", tier="Priority", score=99),
    ])
    ctx.store.insert("signal_events", [marker("old", 200), marker("recent", 30)])
    todo, _ = enrich.candidates(ctx)
    assert [a["account_id"] for a in todo] == ["priority", "standard", "control", "old"]
    enrich.run(ctx)
    assert fake.bulk_asked == [["priority.com", "standard.com", "control.com", "old.com"]]
    assert facts_of(ctx, "new") == {} and facts_of(ctx, "queued") == {}

    both = settings_with(apollo_enrich_groups=(TECH, AGENCIES), focus=(Focus(AGENCIES, 0.6), Focus(TECH, 0.4)))
    ctx.settings = both
    ctx.store.delete("signal_events", {"fact": enrich.MARKER})
    # The Focus tab's largest share first, then enrol's order (tier, then score) within the group.
    assert [a["account_id"] for a in enrich.candidates(ctx)[0]] == ["agency", "old", "recent", "priority", "standard",
                                                                    "control"]


def test_a_blank_apollo_enrich_groups_enriches_nothing():
    ctx, t, _ = make([acct("a1")], settings=settings_with(apollo_enrich_groups=()))
    out = enrich.run(ctx)
    assert out["skipped"] is True and "apollo_enrich_groups is blank" in out["reason"] and t.requests == []


def test_nothing_due_spends_nothing():
    ctx, t, _ = make([acct("a1")])
    ctx.store.insert("signal_events", [marker("a1", 10)])
    out = enrich.run(ctx)
    assert out["candidates"] == 0 and t.requests == []


# -- the facts -------------------------------------------------------------------------------------------


def test_the_enrich_record_gives_the_funding_headcount_and_extra_facts_in_apollo_org_s_shape():
    ctx, _, _ = make([acct("acme", "acme.com")], [company("acme.com")])
    out = enrich.run(ctx)
    facts = facts_of(ctx, "acme")
    assert set(facts) == {"days_since_funding", "funding_stage", "funding_amount_usd", "employees",
                          "headcount_growth_12m", "description", "technologies", "keywords", enrich.MARKER}
    assert set(facts) - {enrich.MARKER} <= SOURCE_FIELDS["apollo_org"]  # naics, HQ, founded: source_universe's
    assert facts["days_since_funding"]["value"] == 218  # 1 Mar to 5 Oct (UK), as of observed_at
    assert facts["funding_stage"]["value"] == "Series A" and facts["funding_amount_usd"]["value"] == 8_000_000  # "8M"
    assert facts["employees"]["value"] == 64 and facts["headcount_growth_12m"]["value"] == 0.18
    assert facts["technologies"]["value"] == ["Slack", "HubSpot"] and facts["description"]["value"].startswith("Payments")
    for e in facts.values():
        assert e["source"] == "apollo_org" and e["observed_at"] == NOW and e["quote"] and len(e["quote"]) <= 300
        assert e["source_url"] == "https://app.apollo.io/#/organizations/org-acme"
    assert facts[enrich.MARKER]["value"] == {
        "outcome": "found", "via": "bulk", "run_id": ctx.run_id, "organization_id": "org-acme", "funding": True,
        "days_since_funding": 218, "funding_stage": "Series A", "employees": 64}
    assert (out["enriched"], out["found"], out["not_found"], out["with_funding"], out["funding_within_180"],
            out["funding_within_365"], out["with_employees"]) == (1, 1, 0, 1, 0, 1, 1)
    # days_since_funding is an AGED fact: scoring adds the days since it was read.
    later = (NOW + timedelta(days=30)).date()
    assert score.aged_value("days_since_funding", facts["days_since_funding"], later) == 248


def test_description_technologies_and_keywords_are_not_stored_twice():
    ctx, _, _ = make([acct("acme", "acme.com")], [company("acme.com")])
    ctx.store.insert("signal_events", [
        {"event_id": "d", "account_id": "acme", "source": "apollo_org", "fact": "description", "value": "From search.",
         "observed_at": NOW - timedelta(days=3)},
        {"event_id": "k", "account_id": "acme", "source": "apollo_org", "fact": "keywords", "value": ["fintech"],
         "observed_at": NOW - timedelta(days=3)}])
    enrich.run(ctx)
    rows = ctx.store.select("signal_events", {"account_id": "acme", "source": "apollo_org"})
    counts = {f: sum(e["fact"] == f for e in rows) for f in ("description", "keywords", "technologies", "employees")}
    assert counts == {"description": 1, "keywords": 1, "technologies": 1, "employees": 1}


@pytest.mark.parametrize("event, usd", [
    ({"amount": 8_000_000}, 8_000_000), ({"amount": "8M", "currency": "$"}, 8_000_000), ({"amount": "$1.5B"}, 1_500_000_000),
    ({"amount": "750K"}, 750_000), ({"amount": "2,500,000"}, 2_500_000), ({"amount": "5M", "currency": "EUR"}, None),
    ({"amount": "undisclosed"}, None), ({"amount": None}, None),
])
def test_funding_amounts_in_apollo_s_text_are_read_as_dollars(event, usd):
    assert uni.funding_usd(event) == usd


def test_the_amount_is_the_latest_round_s_own():
    org = {"latest_funding_round_date": "2026-06-01", "latest_funding_stage": "Series B",
           "funding_events": [{"date": "2026-06-01", "type": "Series B"}, {"date": "2024-01-10", "type": "Seed", "amount": "2M"}]}
    got = uni.org_funding(org, NOW.date())
    assert got["funding_stage"] == "Series B" and got["days_since_funding"] == 126 and "funding_amount_usd" not in got


# -- not found is not "no funding" ------------------------------------------------------------------------


def test_a_company_apollo_has_no_record_for_is_not_found_and_one_without_a_round_has_no_funding():
    ctx, _, fake = make([acct("quiet", "quiet.com"), acct("ghost", "ghost.com")], [company("quiet.com", funded=None)])
    out = enrich.run(ctx)
    quiet, ghost = facts_of(ctx, "quiet"), facts_of(ctx, "ghost")
    assert quiet[enrich.MARKER]["value"]["outcome"] == "found" and quiet[enrich.MARKER]["value"]["funding"] is False
    assert "no funding round listed" in quiet[enrich.MARKER]["quote"] and quiet["employees"]["value"] == 64
    assert set(ghost) == {enrich.MARKER}  # nothing else: no funding fact, no employee count
    assert ghost[enrich.MARKER]["value"]["outcome"] == "not_found" and ghost[enrich.MARKER]["value"]["funding"] is None
    assert ghost[enrich.MARKER]["value"]["via"] == "single"  # the bulk answer left it out: asked once more
    assert ghost[enrich.MARKER]["quote"] == "Apollo organization enrich: no record for ghost.com"
    assert fake.single_asked == ["ghost.com"]
    assert (out["found"], out["not_found"], out["with_funding"], out["credits"]) == (1, 1, 0, 1.0)  # not found is free
    assert enrich.candidates(ctx)[0] == []  # neither is asked again for 180 days


def test_a_failed_call_writes_nothing_and_its_account_is_tried_again():
    ctx, t, _ = make([acct("acme", "acme.com")], bulk_status=500)
    t.route("GET", "/organizations/enrich", status=503, body={"error": "busy"})
    out = enrich.run(ctx)
    assert facts_of(ctx, "acme") == {} and out["enriched"] == 0 and len(out["errors"]) == 2
    assert [a["account_id"] for a in enrich.candidates(ctx)[0]] == ["acme"]
    assert sorted(r["credits"] for r in ledger(ctx)) == [1.0, 1.0]  # a 5xx may have charged: counted


# -- the 180-day refresh ------------------------------------------------------------------------------------


def test_an_account_is_enriched_again_only_after_180_days():
    ctx, _, fake = make([acct("fresh", "fresh.com"), acct("stale", "stale.com")],
                        [company("fresh.com"), company("stale.com", funded="2026-09-01", stage="Series B")])
    ctx.store.insert("signal_events", [marker("fresh", 179), marker("stale", 181, enrich.NOT_FOUND)])
    out = enrich.run(ctx)
    assert fake.bulk_asked == [["stale.com"]] and out["found"] == 1
    assert facts_of(ctx, "stale")["funding_stage"]["value"] == "Series B"
    ctx.now = NOW + timedelta(days=2)
    assert [a["account_id"] for a in enrich.candidates(ctx)[0]] == ["fresh"]  # now 181 days


# -- the account's size --------------------------------------------------------------------------------------


def test_the_exact_count_supersedes_the_searched_band_and_one_outside_the_range_is_left_out():
    """W1-3a (code-sources B3): the exact count is the last size check. The W43 hand-check drew a 5,100-staff company
    marked fine: sent back to new, an account sized out stayed in the queue, failed by verify every morning with no
    mark. Now it is disqualified and Excluded at once, with status_reason size, the code verify writes for it."""
    ctx, _, _ = make([acct("acme", "acme.com"), acct("big", "big.com", tier="Standard"),
                      acct("edge", "edge.com", employees=249, size_band="100-249")],
                     [company("acme.com", employees=64), company("big.com", employees=5100),
                      company("edge.com", employees=249)])
    out = enrich.run(ctx)
    a, big, edge = (ctx.store.get("accounts", account_id=x) for x in ("acme", "big", "edge"))
    assert (a["employees"], a["size_band"], a["status"], a.get("status_reason")) == (64, "50-99", "verified", None)
    assert (big["employees"], big["size_band"], big["status"], big["tier"], big["status_reason"]) == (
        5100, "5000-9999", "disqualified", "Excluded", "size")
    assert big["tier_reason"] == "size: 5100 outside 10–249 employees"
    assert verify.REASON_CODES["size"] == verify.TERMINAL  # the code means: left out for good
    # verify's own check names the same code (W1-4: check() returns the code and the text).
    assert verify.check(big, {}, ctx.settings, set(), set()) == ("size", "outside 10 to 249 employees")
    # 249 is inside 10 to 249 and the row already says so: nothing written, still verified.
    assert (edge["status"], edge["employees"]) == ("verified", 249)
    assert (out["size_written"], out["sized_out"]) == (2, 1)
    assert [x["account_id"] for x in enrich.candidates(ctx)[0]] == []  # and it is never paid for again


def test_overrides_and_clay_keep_the_account_s_size():
    s = settings_with(overrides=(Override("acme.com", "employees", "30"),))
    ctx, _, _ = make([acct("acme", "acme.com", employees=30, size_band="20-49"),
                      acct("clayed", "clayed.com", employees=45, clay_checked_at=NOW - timedelta(days=5))],
                     [company("acme.com", employees=64), company("clayed.com", employees=120)], settings=s)
    out = enrich.run(ctx)
    a, c = ctx.store.get("accounts", account_id="acme"), ctx.store.get("accounts", account_id="clayed")
    assert (a["employees"], a["size_band"], c["employees"], c["size_band"]) == (30, "20-49", 45, "20-49")
    assert facts_of(ctx, "acme")["employees"]["value"] == 64  # Apollo's view; the override wins at scoring
    assert out["size_kept"] == {"an Overrides row sets the size": 1, "Clay confirmed the size": 1}
    assert out["size_written"] == 0


# -- credits -------------------------------------------------------------------------------------------------------


def test_each_call_is_reserved_then_settled_at_the_companies_found():
    ctx, _, _ = make([acct("a1", "one.com"), acct("a2", "two.com"), acct("a3", "three.com")],
                     [company("one.com"), company("two.com")])
    out = enrich.run(ctx)
    rows = sorted(ledger(ctx), key=lambda r: r["credits"])
    assert [(r["credits"], r["account_id"]) for r in rows] == [(0.0, "a3"), (2.0, None)]  # three.com: not found, free
    assert json.loads(rows[1]["note"]) == {"bulk_enrich": 3, "found": 2, "reported": 2.0}
    assert all(r["system"] == "apollo" and r["run_id"] == ctx.run_id and r["occurred_at"] == NOW for r in rows)
    assert out["credits"] == 2.0 and (out["bulk_calls"], out["single_calls"]) == (1, 1)


def test_enrich_spends_only_its_share_paced_by_the_weekday():
    # 15% of 2,000 is 300 a month; 240 spent leaves 60 over 20 weekdays: 3 today, one bulk call of 3 domains.
    ctx, _, fake = make([acct(f"a{i:02d}", f"c{i:02d}.com", score=50 - i) for i in range(12)],
                        [company(f"c{i:02d}.com") for i in range(12)])
    ctx.store.insert("credit_ledger", [{"entry_id": "x", "system": "apollo", "job": enrich.JOB, "credits": 240.0,
                                        "occurred_at": datetime(2026, 10, 1, 9, tzinfo=UTC), "note": ""}])
    out = enrich.run(ctx)
    assert fake.bulk_asked == [["c00.com", "c01.com", "c02.com"]] and fake.single_asked == []
    assert (out["enriched"], out["credits"], out["left_for_next_run"]) == (3, 3.0, 9)
    assert out["stopped_by"] == "today's Apollo credits for enrichment are used" and out["budget"]["today"] == 3.0


def test_enrich_never_takes_what_is_left_of_the_whole_apollo_budget_today():
    ctx, _, fake = make([acct("a1")], [company("a1.com")])
    ctx.store.insert("credit_ledger", [{"entry_id": "x", "system": "apollo", "job": "pick_contacts", "credits": 1990.0,
                                        "occurred_at": datetime(2026, 10, 2, 9, tzinfo=UTC), "note": ""}])
    out = enrich.run(ctx)
    assert out["stopped_by"] == "today's Apollo credits for enrichment are used" and fake.bulk_asked == []


def test_no_credit_is_spent_below_apollo_floor():
    ctx, t, fake = make([acct("a1")], [company("a1.com")], balance=4_000)
    out = enrich.run(ctx)
    assert out["skipped"] is True and "below apollo_floor (5,000)" in out["reason"]
    assert fake.bulk_asked == [] and [r.url.rsplit("/", 1)[-1] for r in t.requests] == ["credit_usage_stats"]


def test_the_apollo_shares_add_up_to_at_most_the_whole_budget():
    shares = uni.SOURCING_SHARE + apollo_jobs.SIGNALS_SHARE + enrich.ENRICH_SHARE
    assert enrich.ENRICH_SHARE == 0.15 and shares == pytest.approx(0.65)  # 35% left for pick_contacts' reveals


def test_dry_run_reads_apollo_and_writes_only_the_database():
    ctx, _, _ = make([acct("acme", "acme.com")], [company("acme.com")])
    assert ctx.dry_run
    enrich.run(ctx)
    assert facts_of(ctx, "acme") and ledger(ctx) and ctx.store.get("accounts", account_id="acme")["employees"] == 64
    assert {c.system for c in ctx.guard.writes()} == {"db"}
    apollo = [c for c in ctx.guard.calls if c.system == "apollo"]
    assert apollo and all(not c.write and c.sent for c in apollo) and {c.action for c in apollo} <= APOLLO_READ_ACTIONS


# -- bulk, and single calls when bulk cannot be trusted ---------------------------------------------------------


def test_a_refused_bulk_call_falls_back_to_single_calls_for_the_run():
    accounts = [acct(f"a{i:02d}", f"c{i:02d}.com", score=50 - i) for i in range(12)]
    ctx, _, fake = make(accounts, [company(f"c{i:02d}.com") for i in range(12)], bulk_status=404)
    out = enrich.run(ctx)
    assert len(fake.bulk_asked) == 1 and len(fake.single_asked) == 12 and out["found"] == 12
    assert {facts_of(ctx, a["account_id"])[enrich.MARKER]["value"]["via"] for a in accounts} == {"single"}
    refused = [r for r in ledger(ctx) if "bulk_enrich" in r["note"]]
    assert [(r["credits"], json.loads(r["note"])["failed"]) for r in refused] == [(0.0, 404)]  # a 4xx charges nothing
    assert out["credits"] == 12.0 and len(out["errors"]) == 1


def test_a_company_apollo_files_under_another_domain_is_matched_by_its_id_and_paid_for_once():
    """code-sources B5: the bulk record of acmehq.com comes back as acme.com. Matched by domain alone it was charged
    in the bulk call and again by a single call (3 credits for 2 companies, a mismatch every run from 14 Oct). Its
    organization id is the one Apollo's search gave the account (apollo_org_id org-acme), so it is acme's: 1 credit
    each, 2 in all, no single call, and bulk calls stay on."""
    ctx, _, fake = make([acct("acme", "acmehq.com"), acct("beta", "beta.com")], [company("beta.com")],
                        filed_under={"acmehq.com": company("acme.com")})
    out = enrich.run(ctx)
    assert fake.single_asked == [] and facts_of(ctx, "acme")[enrich.MARKER]["value"]["via"] == "bulk"
    assert facts_of(ctx, "acme")["employees"]["value"] == 64 and out["bulk_unmatched"] == 0
    assert out["credits"] == 2.0 and sum(r["credits"] for r in ledger(ctx)) == 2.0
    assert out["errors"] == [] and out["bulk_off"] is None


def test_a_record_that_matches_no_account_by_id_or_domain_is_asked_once_more():
    """The case the id cannot settle: an account with no apollo_org_id (a site visitor, an Overrides row) whose
    company Apollo files elsewhere. The record names nobody asked, so the domain is asked once more and bulk calls
    stop for the run, so no further mismatch is paid for unseen."""
    ctx, _, fake = make([acct("acme", "acmehq.com", apollo_org_id=None), acct("beta", "beta.com")],
                        [company("beta.com")], filed_under={"acmehq.com": company("acme.com")})
    out = enrich.run(ctx)
    assert fake.single_asked == ["acmehq.com"] and facts_of(ctx, "acme")[enrich.MARKER]["value"]["via"] == "single"
    assert out["bulk_unmatched"] == 1 and out["credits"] == 3.0  # the bulk record Apollo filed elsewhere, and the call
    # Handled, so no error for heartbeat_check to post (14 Oct 2026): the summary says why bulk calls stopped.
    assert out["errors"] == [] and out["bulk_off"].startswith("bulk_enrich carried 2 records, 1 matching no account")


def test_neither_paid_enrichment_job_rescores(monkeypatch):
    """RS P0-8: overlapping rescores could lose the scoring match rows, so apollo_enrich (05:00) and apollo_signals
    (05:10) add facts and leave the scoring to pick_contacts' one rescore at 05:30 (tests/test_pick_contacts.py)."""
    calls: list[str] = []
    monkeypatch.setattr(score, "rescore", lambda c: calls.append(c.job) or {})
    ctx, _, _ = make([acct("acme", "acme.com")], [company("acme.com")])
    assert enrich.run(ctx)["found"] == 1
    assert apollo_jobs.run(dataclasses.replace(ctx, job=apollo_jobs.JOB))["status"] == "ok"
    assert calls == []


def test_the_401_of_a_wrong_key_stops_the_job():
    ctx, t, _ = make([acct("acme", "acme.com")], bulk_status=401)
    with pytest.raises(Exception, match="401"):
        enrich.run(ctx)


# -- the funding signals now fire -------------------------------------------------------------------------------


def test_the_funding_signals_fire_in_the_rescore_of_an_enriched_account():
    ctx, _, _ = make([acct("recent", "recent.com"), acct("older", "older.com"), acct("ghost", "ghost.com")],
                     [company("recent.com", funded="2026-08-01", stage="Seed"), company("older.com")])

    def matched():
        score.rescore(ctx)
        out: dict[str, set[str]] = {}
        for e in ctx.store.select("signal_events", {"source": "scoring"}):
            out.setdefault(e["account_id"], set()).add(e["value"]["signal"])
        return out

    before = matched()
    assert not any({"Funding in the last 6 months", "Funding 6–12 months ago"} & s for s in before.values())
    enrich.run(ctx)
    after = matched()
    assert "Funding in the last 6 months" in after["recent"]  # 65 days
    assert "Funding 6–12 months ago" in after["older"] and "Funding in the last 6 months" not in after["older"]  # 218
    assert "Hiring and growth" in after["older"]  # headcount_growth_12m 0.18
    assert not after.get("ghost", set()) & {"Funding in the last 6 months", "Funding 6–12 months ago"}
    # Five months on, the 65-day round has aged past 180 days: the 6-to-12-month signal takes over.
    ctx.now = NOW + timedelta(days=130)
    assert "Funding 6–12 months ago" in matched()["recent"]


# -- the daily post, the schedule, the registry and the CLI ------------------------------------------------------


def test_the_daily_post_lines():
    ctx, _, _ = make([acct("a1", "one.com"), acct("a2", "two.com")], [company("one.com", funded="2026-08-01")])
    assert enrich.post_lines(ctx) == [
        "Funding (Apollo enrich, Technology & Startups): no account enriched yet (apollo_enrich, weekdays 05:00)."]
    enrich.run(ctx)
    assert enrich.post_lines(ctx) == [
        "Funding (Apollo enrich, Technology & Startups): last run 2 accounts (1 found, 1 not found): 1 with funding "
        "(1 in the last 180 days, 1 in the last 365); 1 with an employee count.",
        "  So far 2 accounts (1 found, 1 not found): 1 with funding (1 in the last 180 days, 1 in the last 365); "
        "1 with an employee count."]


def test_it_runs_weekdays_at_05_00_between_verify_accounts_and_pick_contacts():
    """W1-3a: after verify_accounts, for the accounts it passed, and before pick_contacts, whose rescore scores the
    facts (it was 04:10, before verify, for every new account: code-sources B4)."""
    table = schedule.by_name()
    job = table["apollo_enrich"]
    assert (job.cron, job.live, job.enabled) == ("0 5 * * 1-5", False, True) and job.timeout_minutes <= 30

    def at(name):
        minute, hour = table[name].cron.split()[:2]
        return int(hour) * 60 + int(minute)

    assert at("verify_accounts") < at("apollo_enrich") < at("apollo_signals") < at("pick_contacts")
    assert cli.JOBS["apollo_enrich"] == "us_outbound.sources.apollo_enrich:run"
    assert cli.resolve_job("apollo_enrich") is enrich.run
    assert hb.EXPECTED["apollo_enrich"] == 26 * 60 and "apollo_enrich" in hb.WEEKDAY_JOBS
    assert "apollo_enrich" in schedule.enabled_names()


def test_harry_can_run_it_by_hand():
    contexts = []
    store = None

    def factory(name, live_flag, operator=False):
        nonlocal store
        t = FakeApollo([company("acme.com")]).install(FakeTransport())
        ctx = make_context(settings_with(), job=name, now=NOW, transport=t, store=store)
        if store is None:
            ctx.store.insert("accounts", [acct("acme", "acme.com")])
        store = ctx.store
        contexts.append(ctx)
        return ctx

    assert cli.main(["dry-run", "apollo_enrich"], context_factory=factory) == 0
    assert cli.main(["run", "apollo_enrich"], context_factory=factory) == 0
    assert contexts[-1].dry_run and contexts[-1].job == "apollo_enrich"
    first, second = (store.get("heartbeats", run_id=c.run_id) for c in contexts)
    assert first["status"] == second["status"] == "ok"
    assert first["detail"]["found"] == 1 and second["detail"]["candidates"] == 0  # enriched once, not again


# -- the General key ----------------------------------------------------------------------------------------------


def _general_with(value: str) -> dict:
    tabs = default_tabs()
    for r in tabs["General"]:
        if r["key"] == "apollo_enrich_groups":
            r["value"] = value
    return tabs


def test_apollo_enrich_groups_names_industry_groups_on_the_industries_tab():
    assert BASE.general.apollo_enrich_groups == (TECH,)
    s, _ = validate_all(_general_with("technology & startups, Legal Teams"))
    assert s.general.apollo_enrich_groups == (TECH, "Legal Teams")  # as the Industries tab writes them
    s, _ = validate_all(_general_with(""))
    assert s.general.apollo_enrich_groups == ()
    s, errors = validate_all(_general_with("Technology & Startup"))
    assert s is None and [e.message for e in errors["General"]] == [
        "apollo_enrich_groups: 'Technology & Startup' is not an industry_group on the Industries tab "
        "(did you mean Technology & Startups?)"]


def test_settings_load_brings_the_key_to_a_sheet_without_it_and_the_default_applies_until_then():
    sheet = [r for r in default_tabs()["General"] if r["key"] != "apollo_enrich_groups"]
    plan = loader.plan_tab("General", sheet, default_tabs()["General"])
    assert "apollo_enrich_groups = Technology & Startups" in plan.added
    tabs = default_tabs()
    tabs["General"] = sheet
    assert validate_all(tabs)[0].general.apollo_enrich_groups == (TECH,)
