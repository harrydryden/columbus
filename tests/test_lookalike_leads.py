"""The monthly lookalike leads (sources/lookalike_leads.py; Harry, 5 Oct 2026): Apollo's US companies like Spill's
active customers, UK ones included, into new accounts by the front door.

Seeds are lookalikes.Customer values (or a fake HubSpot read through lookalikes.read_customers); Apollo is a
FakeTransport that answers the domain search for the seeds' ids and the lookalike search by seed id and size
band. The fake leaves the location filter to the job on purpose, so the row checks are what keep a non-US or
CA, WA or FL company out. Nothing here calls a real service.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound.clients.http import ApiError
from us_outbound.ops import cli, heartbeat, schedule
from us_outbound.ops import scheduler as sch
from us_outbound.scoring.score import score_account
from us_outbound.settings.model import SOURCE_FIELDS
from us_outbound.sources import lookalike_leads as ll
from us_outbound.sources import lookalikes as lk
from us_outbound.sources.lookalikes import Customer

NOW = datetime(2026, 11, 1, 2, 50, tzinfo=UTC)  # Sunday 1 Nov 2026, 02:50 UK (GMT): the job's slot
TECH, AGENCIES, LEGAL = "Technology & Startups", "Marketing & Creative Agencies", "Legal Teams"


def cust(domain, *, us=False, label="Fintech", group=TECH, band="10-49", status=lk.ACTIVE) -> Customer:
    return Customer(status, domain, label, group, band, us)


def org(i: int, domain: str = "", *, state="New York", country="United States", naics=("541511",),
        keywords=("fintech",)) -> dict:
    """An Apollo search row as the API sends it: no employee count (2 Oct 2026)."""
    d = domain or f"lead{i}.com"
    return {"id": f"org{i}", "name": f"Lead {i}, Inc.", "primary_domain": d, "website_url": f"https://www.{d}",
            "city": "Somewhere", "state": state, "country": country, "naics_codes": list(naics),
            "keywords": list(keywords), "industry": "financial services"}


AGENCY = {"naics": ("541810",), "keywords": ("advertising agency",)}


class FakeApollo:
    """The credit balance, the seeds' ids by domain, and lookalike searches by (seed id, band).

    A search whose seeds include one in no_data returns nothing, as Apollo does when it holds no
    lookalike data for a seed.
    """

    def __init__(self, ids: dict[str, str], lookalikes: dict[tuple[str, str], list[dict]], *, no_data=(),
                 balance=30_000, fail: int | None = None):
        self.ids, self.lookalikes, self.no_data, self.balance, self.fail = ids, lookalikes, set(no_data), balance, fail
        self.bodies: list[dict] = []

    def install(self, t: FakeTransport) -> FakeTransport:
        t.route("POST", "/usage_stats/credit_usage_stats",
                body={"credit_usage_stats": {"lead_credit": {"left_over": self.balance}}})
        t.route("POST", "/mixed_companies/search", fn=self.search, status=self.fail or 200)
        return t

    def search(self, req):
        body = req.json
        self.bodies.append(body)
        if self.fail:
            return {"error": "seed uk-seed1.co.uk is not valid"}
        if "lookalike_organization_ids" in body:
            ids = body["lookalike_organization_ids"]
            if set(ids) & self.no_data:
                return {"organizations": []}
            band = body["organization_num_employees_ranges"][0].replace(",", "-")
            return {"organizations": [dict(r) for i in ids for r in self.lookalikes.get((i, band), ())]}
        wanted = set(body.get("q_organization_domains_list") or ())
        return {"organizations": [{"id": oid, "name": "never stored", "primary_domain": d}
                                  for d, oid in self.ids.items() if d in wanted]}

    def lookalike_bodies(self) -> list[dict]:
        return [b for b in self.bodies if "lookalike_organization_ids" in b]


CUSTOMERS = [
    cust("us-seed.com", us=True),  # Tech 10-49, in the US
    cust("uk-seed1.co.uk"), cust("uk-seed2.co.uk"),  # Tech 10-49, in the UK; Apollo has no lookalikes for seed 2
    cust("uk-agency.co.uk", label="Advertising agencies", group=AGENCIES, band="50-99"),
    cust("nowhere.co.uk", band="50-99"),  # unknown to Apollo
    cust("former.com", us=True, status=lk.CHURNED),  # a former customer: never a seed, never admitted
]
IDS = {"us-seed.com": "o-us1", "uk-seed1.co.uk": "o-uk1", "uk-seed2.co.uk": "o-uk2", "uk-agency.co.uk": "o-uk3"}
LOOKALIKES = {
    ("o-us1", "10-19"): [org(1), org(2, state="California"), org(3, "existing.com")],
    ("o-us1", "20-49"): [org(4, country="United Kingdom", state="England"), org(5, "supp.com"), org(6, "former.com")],
    ("o-uk1", "10-19"): [org(7, state="Texas"), org(8, country="United Kingdom", state="London"),
                         org(9, country="", state="Ontario")],
    ("o-uk1", "20-49"): [org(10, state="Florida")],  # FL is off on the States tab
    ("o-uk3", "50-99"): [org(11, state="Illinois", **AGENCY)],
}
SEED_DOMAINS = {c.domain for c in CUSTOMERS}


def world(settings, monkeypatch, *, customers=CUSTOMERS, now=NOW, store=None, **apollo):
    apollo.setdefault("no_data", ("o-uk2",))
    fake = FakeApollo(apollo.pop("ids", IDS), apollo.pop("lookalikes", LOOKALIKES), **apollo)
    t = fake.install(FakeTransport())
    ctx = make_context(settings, transport=t, now=now, job=ll.JOB, store=store)
    if store is None:
        ctx.store.upsert("accounts", [{"account_id": "acc-old", "domain": "existing.com", "status": "queued",
                                       "source": "apollo", "first_seen": now - timedelta(days=9)}])
        ctx.store.upsert("suppression", [{"email_sha256": None, "domain": "supp.com", "reason": "test",
                                          "source": "test", "added_at": now, "expires_at": None}])
    monkeypatch.setattr(lk, "read_customers", lambda c: list(customers))
    return ctx, fake


def created(ctx) -> dict[str, dict]:
    return {a["domain"]: a for a in ctx.store.select("accounts", {"source": ll.ACCOUNT_SOURCE})}


# -- seeds -----------------------------------------------------------------------------------------------


def test_seeds_are_active_customers_in_any_country_in_the_target_bands_and_active_groups(default_settings):
    health = next(i for i in default_settings.industries if i.industry_group == "Healthcare" and i.active)
    off = next(i for i in default_settings.industries if i.industry_group == TECH and not i.active)
    customers = [
        cust("a-us.com", us=True), cust("b-uk.co.uk"), cust("c-de.de"),
        cust("small.co.uk", band="1-9"), cust("big.co.uk", band="250+"), cust("unsized.co.uk", band="unknown"),
        cust("gone.co.uk", status=lk.CHURNED), cust(None),  # no domain
        cust("label-off.co.uk", label=off.industry),  # its own label is switched off
        cust("hospital.com", us=True, label=health.industry, group="Healthcare"),  # active, but outside the Focus
        cust("nolabel.co.uk", label="", group=""),
    ]
    got = ll.eligible(customers, default_settings)
    assert {c.domain for c in got} == {"a-us.com", "b-uk.co.uk", "c-de.de"}  # UK and German customers seed too
    # The Focus tab's shares fill the week (Tech 50%, Agencies 30%, Legal 20%): only its groups seed.
    assert ll.seed_groups(default_settings) == [TECH, AGENCIES, LEGAL]
    # With no Focus tab, every group with an active label seeds, Healthcare included.
    open_focus = dataclasses.replace(default_settings, focus=())
    assert "Healthcare" in ll.seed_groups(open_focus)
    assert "hospital.com" in {c.domain for c in ll.eligible(customers, open_focus)}


def test_seeds_rank_us_first_take_turns_by_cell_and_rotate_by_month(default_settings):
    customers = [cust(f"uk{i}.co.uk") for i in range(8)] + [cust("us1.com", us=True), cust("us2.com", us=True)]
    customers += [cust("agency.co.uk", label="Advertising agencies", group=AGENCIES),
                  cust("mid.co.uk", band="50-99")]
    seeds = ll.select_seeds(customers, default_settings, NOW)
    # One from each cell in turn (Tech 10-49, Tech 50-99, Agencies 10-49), then the second of each, and so on.
    assert [s.cell for s in seeds[:3]] == [f"{TECH}|10-49", f"{TECH}|50-99", f"{AGENCIES}|10-49"]
    tech = [s for s in seeds if s.cell == f"{TECH}|10-49"]
    assert len(tech) == ll.SEEDS_PER_CELL == 5
    assert {s.domain for s in tech[:2]} == {"us1.com", "us2.com"}  # US customers first
    assert {s.country for s in tech[:2]} == {"US"} and {s.country for s in tech[2:]} == {"non-US"}
    # Stable within the month; other UK customers in other months.
    assert ll.select_seeds(customers, default_settings, NOW + timedelta(days=20)) == seeds
    months = {frozenset(s.domain for s in ll.select_seeds(customers, default_settings, NOW + timedelta(days=31 * m))
                        if s.country == "non-US" and s.cell == f"{TECH}|10-49") for m in range(6)}
    assert len(months) > 1
    assert len(ll.select_seeds(customers, default_settings, NOW, limit=2)) == 2  # the run's cap


def test_searches_keep_us_and_other_seeds_apart_five_at_most():
    seeds = [ll.Seed(f"s{i}.com", TECH, "10-49", "non-US") for i in range(7)]
    seeds += [ll.Seed("us.com", TECH, "10-49", "US"), ll.Seed("a.com", AGENCIES, "50-99", "non-US")]
    groups = ll.searches(seeds)
    assert [len(g) for g in groups] == [5, 2, 1, 1]
    assert all(len({(s.cell, s.country) for s in g}) == 1 for g in groups)


# -- the run -------------------------------------------------------------------------------------------


def test_uk_seeds_bring_only_us_companies_in_active_states(default_settings, monkeypatch, capsys):
    ctx, apollo = world(default_settings, monkeypatch)
    out = ll.run(ctx)
    assert out["status"] == "ok" and out["provider"] == "apollo"

    # Three new accounts, all in active US states, by the front door with source "lookalike".
    new = created(ctx)
    assert set(new) == {"lead1.com", "lead7.com", "lead11.com"}
    assert {(d, a["hq_state"], a["size_band"], a["status"]) for d, a in new.items()} == {
        ("lead1.com", "NY", "10-19", "new"), ("lead7.com", "TX", "10-19", "new"), ("lead11.com", "IL", "50-99", "new")}
    assert new["lead11.com"]["industry_group"] == AGENCIES and new["lead1.com"]["apollo_org_id"] == "org1"
    assert out["admitted"] == 3 and out["admitted_by_group"] == {TECH: 2, AGENCIES: 1}
    # Refused: an account already, a suppressed domain, a former customer. Not admitted: two not US, CA and FL.
    assert out["refused"] == {"already an account": 1, "suppressed": 1, "a Spill customer": 1}
    assert out["skipped_companies"] == {"not US": 3, "HQ outside the active states": 2}
    assert out["found"] == 11

    # The yield of US and non-US seeds, apart.
    us, other = out["by_seed_country"]["US"], out["by_seed_country"]["non-US"]
    assert (us["seeds"], us["searches"], us["empty_searches"], us["results"], us["admitted"], us["refused"],
            us["not_US"], us["other_skips"]) == (1, 2, 0, 6, 1, 3, 1, 1)
    # Tech 10-49's two UK seeds: empty together (seed 2 has no data), so each alone, in both bands; then Agencies.
    assert (other["seeds"], other["unknown_to_apollo"], other["searches"], other["empty_searches"], other["results"],
            other["admitted"], other["refused"], other["not_US"], other["other_skips"]) == (3, 1, 7, 4, 5, 2, 0, 2, 1)
    assert out["seeds"] == {"customers": 6, "eligible": 5, "used": 4, "unknown_to_apollo": 1,
                            "by_cell": {f"{TECH}|10-49": 3, f"{AGENCIES}|50-99": 1},
                            "by_country": {"US": 1, "non-US": 3}}

    # Each new account: the lookalike_lead fact with the seed's cell and country, and the flag the signal reads.
    facts = [e for e in ctx.store.select("signal_events", {"source": ll.SOURCE})]
    by = {(e["account_id"], e["fact"]): e for e in facts}
    a1, a7, a11 = (new[d]["account_id"] for d in ("lead1.com", "lead7.com", "lead11.com"))
    assert by[(a1, "lookalike_lead")]["value"] == {"provider": "apollo", "seed_group": TECH, "seed_band": "10-49",
                                                   "seed_country": "US"}
    assert by[(a7, "lookalike_lead")]["value"]["seed_country"] == "non-US"
    assert by[(a11, "lookalike_lead")]["value"] == {"provider": "apollo", "seed_group": AGENCIES, "seed_band": "50-99",
                                                    "seed_country": "non-US"}
    assert all(by[(a, "found_as_lookalike")]["value"] is True for a in (a1, a7, a11)) and len(facts) == 6
    assert "outside the US, in Technology & Startups at 10-49 staff" in by[(a7, "found_as_lookalike")]["quote"]
    assert {e["fact"] for e in facts} <= {"lookalike_lead"} | SOURCE_FIELDS["lookalike_lead"]
    # Apollo's own facts too, as source_universe writes them, so verify_accounts and scoring have them.
    assert ctx.store.select("signal_events", {"account_id": a7, "source": "apollo_org", "fact": "hq_state"})[0]["value"] == "TX"
    assert not ctx.store.select("signal_events", {"account_id": "acc-old", "source": ll.SOURCE})  # found again: no fact

    # Every lookalike search asks for the US, never CA or WA, the band, no insurers, and alike seeds, 5 at most.
    bodies = apollo.lookalike_bodies()
    for b in bodies:
        assert b["organization_locations"] == ["United States"]
        assert b["organization_not_locations"] == ["California, US", "Washington, US"]
        assert b["not_organization_naics_codes"] == ["524"] and len(b["organization_num_employees_ranges"]) == 1
        assert 1 <= len(b["lookalike_organization_ids"]) <= 5
        assert not ({"o-us1"} & set(b["lookalike_organization_ids"]) and len(b["lookalike_organization_ids"]) > 1)
    assert sorted(b["organization_num_employees_ranges"][0] for b in bodies if b["lookalike_organization_ids"] == ["o-us1"]) == [
        "10,19", "20,49"]
    [ids] = [b for b in apollo.bodies if "lookalike_organization_ids" not in b]
    assert ids == {"q_organization_domains_list": sorted(SEED_DOMAINS - {"former.com"}), "page": 1, "per_page": 100}

    # Credits: a page with results costs 1, an empty one 0; every page is in credit_ledger under the job.
    ledger = ctx.store.select("credit_ledger", {"job": ll.JOB})
    assert len(ledger) == len(apollo.bodies) == 10 and sum(r["credits"] for r in ledger) == out["credits"] == 6
    assert all(r["system"] == "apollo" and r["run_id"] == ctx.run_id for r in ledger)

    # No customer's domain or name anywhere: tables, the summary, the logs.
    dump = json.dumps({t: rows for t, rows in ctx.store.tables.items()}, default=str)
    printed = capsys.readouterr().out + json.dumps(out)
    for d in SEED_DOMAINS:  # the seeds, and the former customer Apollo returned as a lookalike
        assert d not in dump and d not in printed, d
    assert "never stored" not in dump  # nor the name Apollo gave a seed

    # The summary in plain words.
    text = "\n".join(out["report"])
    assert "Apollo's US companies like 4 of Spill's active customers (1 in the US, 3 elsewhere)" in text
    assert "US seeds: 1 used (0 unknown to Apollo), 2 searches (0 empty), 6 results, 1 new accounts" in text
    assert "non-US seeds: 3 used (1 unknown to Apollo), 7 searches (4 empty), 5 results, 2 new accounts" in text
    assert "Credits: 6 Apollo (10 searches)." in text


def test_a_lookalike_lead_scores_the_signal(default_settings, monkeypatch):
    ctx, _ = world(default_settings, monkeypatch)
    ll.run(ctx)
    account = created(ctx)["lead1.com"]
    events = ctx.store.select("signal_events", {"account_id": account["account_id"]})
    r = score_account(account, events, ctx.settings, ctx.today_uk())
    matched = {m.signal.signal: m.weight_applied for m in r.matches}
    assert matched["Found as a lookalike of a customer"] == 10
    assert matched["Team of 10–49"] == 15 and r.tier == "Standard"  # measured on the company: out of Control
    [m] = [m for m in r.matches if m.signal.signal == "Found as a lookalike of a customer"]
    assert "Found by Apollo's search by likeness as like an active Spill customer in the US" in m.evidence[0].text


def test_the_customers_are_read_from_hubspot_read_only_and_company_fields_only(default_settings):
    companies = {
        "101": {"domain": "uk-hs.co.uk", "company_industry": "Tech", "subscription_status": "Active",
                "employees_covered": "20", "country": "United Kingdom", "lifecyclestage": "customer"},
        "102": {"domain": "us-hs.com", "company_industry": "Creative Agency", "subscription_status": "Active",
                "employees_covered": "60", "country": "United States", "lifecyclestage": "customer"},
    }
    asked: list[dict] = []

    def search(req):
        asked.append(req.json)
        if "/companies/" in req.url:
            return {"results": [{"id": i, "properties": {k: p.get(k) for k in req.json["properties"]}}
                                for i, p in companies.items()]}
        return {"results": []}

    fake = FakeApollo({"uk-hs.co.uk": "o-a", "us-hs.com": "o-b"},
                      {("o-a", "10-19"): [org(21, state="Georgia")], ("o-b", "50-99"): [org(22, state="Illinois", **AGENCY)]},
                      no_data=())
    t = fake.install(FakeTransport())
    t.route("POST", "/crm/v3/objects/", fn=search)
    ctx = make_context(default_settings, transport=t, now=NOW, job=ll.JOB)
    out = ll.run(ctx)
    assert out["seeds"]["by_country"] == {"US": 1, "non-US": 1} and set(created(ctx)) == {"lead21.com", "lead22.com"}
    hubspot = [r for r in t.requests if "hubapi" in r.url]
    assert hubspot and all(r.method == "POST" and r.url.endswith("/search") for r in hubspot)
    for body in asked:
        assert set(body["properties"]) <= set(lk.COMPANY_FIELDS) | {"hs_object_id", "dealstage"}
    assert not [c for c in ctx.guard.calls if c.write and c.system != "db"]


def test_the_run_s_credits_are_capped(default_settings, monkeypatch):
    monkeypatch.setattr(ll, "APOLLO_CREDITS_PER_RUN", 2)
    ctx, _ = world(default_settings, monkeypatch)
    out = ll.run(ctx)
    assert sum(r["credits"] for r in ctx.store.select("credit_ledger", {"job": ll.JOB})) == out["credits"] == 2
    assert out["stopped_by"] == "the run's Apollo credits are used (2)"
    assert "  Stopped: the run's Apollo credits are used (2)." in out["report"]


def test_never_more_than_the_month_has_left(default_settings, monkeypatch):
    g = dataclasses.replace(default_settings.general, apollo_monthly_credits=100)
    ctx, _ = world(dataclasses.replace(default_settings, general=g), monkeypatch)
    ctx.store.insert("credit_ledger", [{"entry_id": "e1", "system": "apollo", "job": "pick_contacts", "run_id": "r0",
                                        "account_id": None, "credits": 99.0, "usd": 0.0, "occurred_at": NOW,
                                        "note": "reveals"}])
    out = ll.run(ctx)
    assert out["credits"] == 1 and out["stopped_by"].startswith("the run's Apollo credits are used")
    ctx.store.insert("credit_ledger", [{"entry_id": "e2", "system": "apollo", "job": "pick_contacts", "run_id": "r0",
                                        "account_id": None, "credits": 5.0, "usd": 0.0, "occurred_at": NOW,
                                        "note": "reveals"}])
    ctx2 = make_context(ctx.settings, transport=ctx.clients.transport, now=NOW, job=ll.JOB, store=ctx.store)
    again = ll.run(ctx2)
    assert again["skipped"] and "apollo_monthly_credits" in again["reason"]


def test_below_apollo_floor_nothing_is_read_or_spent(default_settings, monkeypatch):
    ctx, apollo = world(default_settings, monkeypatch, balance=100)
    monkeypatch.setattr(lk, "read_customers", lambda c: pytest.fail("HubSpot read below the floor"))
    out = ll.run(ctx)
    assert out["skipped"] and "below apollo_floor" in out["reason"]
    assert apollo.bodies == [] and ctx.store.select("credit_ledger") == [] and not created(ctx)
    assert out["report"][0].startswith("Lookalike leads: nothing done this month: Apollo has 100 credits left")


def test_an_apollo_error_is_counted_in_our_words_and_a_bad_key_stops_the_run(default_settings, monkeypatch):
    ctx, _ = world(default_settings, monkeypatch, fail=500)
    out = ll.run(ctx)
    assert out["status"] == "ok" and out["admitted"] == 0
    assert out["errors"] == ["Apollo seed ids: HTTP 500"]  # never Apollo's text, which may echo a seed
    assert "uk-seed1" not in json.dumps(out)
    ctx, _ = world(default_settings, monkeypatch, fail=401)
    with pytest.raises(ApiError):
        ll.run(ctx)


def test_no_seed_and_no_customer_skip_the_run(default_settings, monkeypatch):
    ctx, _ = world(default_settings, monkeypatch, customers=[cust("big.co.uk", band="250+")])
    out = ll.run(ctx)
    assert out["skipped"] and out["reason"].startswith("no active customer at 10 to 249 staff")
    ctx, _ = world(default_settings, monkeypatch, customers=[])
    assert ll.run(ctx)["reason"] == "HubSpot returned no Spill customers"


def test_dry_run_writes_only_the_database(default_settings, monkeypatch):
    ctx, _ = world(default_settings, monkeypatch)
    assert ctx.dry_run
    out = ll.run(ctx)
    assert out["dry_run"] is True and out["admitted"] == 3  # database writes happen in dry-run, as source_universe's
    assert not [c for c in ctx.guard.calls if c.write and c.system != "db"]
    assert {c.system for c in ctx.guard.calls} <= {"apollo", "db", "secrets"}


# -- once a month, the schedule, the heartbeat and the daily post ----------------------------------------------


def test_once_a_month_and_the_daily_post(default_settings, monkeypatch):
    ctx, _ = world(default_settings, monkeypatch)
    first = heartbeat.run_job(ctx, ll.run)
    assert first["status"] == "ok" and first["admitted"] == 3
    later = NOW + timedelta(hours=6)
    ctx2, _ = world(default_settings, monkeypatch, now=later, store=ctx.store)
    second = heartbeat.run_job(ctx2, ll.run)
    assert second["skipped"] and second["reason"].startswith("it ran on 01 Nov already")
    assert len(created(ctx2)) == 3  # nothing spent or added twice

    # The daily post shows the run that morning (the skip is newer, so it shows that), and nothing a few days on.
    lines = ll.post_lines(ctx2)
    assert lines and lines[0].startswith("Lookalike leads: nothing done this month: it ran on 01 Nov already")
    ctx.store.delete("heartbeats", {"run_id": ctx2.run_id})
    lines = ll.post_lines(ctx2)
    assert lines[0].startswith("Lookalike leads (Nov 2026): Apollo's US companies like 4") and "(ran Sun 01 Nov 02:50 UK)" in lines[0]
    assert any(line.strip().startswith("non-US seeds:") for line in lines)
    ctx3, _ = world(default_settings, monkeypatch, now=NOW + timedelta(days=2), store=ctx.store)
    assert ll.post_lines(ctx3) == []

    # Next month it runs again, with that month's seeds.
    ctx4, _ = world(default_settings, monkeypatch, now=datetime(2026, 12, 1, 2, 50, tzinfo=UTC), store=ctx.store)
    assert heartbeat.run_job(ctx4, ll.run)["status"] == "ok"


def test_schedule_heartbeat_and_registry():
    job = schedule.by_name()[ll.JOB]
    assert (job.cron, job.live, job.enabled, job.timeout_minutes) == ("50 2 1 * *", False, True, 30)
    assert cli.JOBS[ll.JOB] == "us_outbound.sources.lookalike_leads:run" and ll.JOB in heartbeat.scheduled_jobs()
    # The 1st at 02:50 UK, after lookalikes (Mondays 02:30): 1 Nov 2026 is GMT.
    assert sch.next_run(sch.Cron.parse(job.cron), datetime(2026, 10, 5, 12, tzinfo=UTC)) == datetime(
        2026, 11, 1, 2, 50, tzinfo=UTC)
    # A monthly job is never "missed" between its runs (31 days at most), and is once it skips a month.
    assert heartbeat.EXPECTED[ll.JOB] == 32 * 24 * 60 and ll.JOB not in heartbeat.WEEKDAY_JOBS
    last = datetime(2026, 10, 1, 1, 55, tzinfo=UTC)
    run = {"status": "ok", "started_at": last, "finished_at": last, "last_ok_at": last, "last_alive_at": last, "runs": 1}
    assert heartbeat.overdue_minutes(ll.JOB, run, datetime(2026, 11, 1, 3, 0, tzinfo=UTC)) < 0
    assert heartbeat.overdue_minutes(ll.JOB, run, datetime(2026, 11, 3, 3, 0, tzinfo=UTC)) > 0


def test_run_from_the_command_line(default_settings, monkeypatch, capsys):
    ctx, _ = world(default_settings, monkeypatch)
    assert cli.main(["run", ll.JOB], context_factory=lambda job, live, **kw: ctx) == 0
    assert "Lookalike leads (Nov 2026)" in capsys.readouterr().out  # the summary, with its report, is printed
    [row] = ctx.store.select("heartbeats", {"job": ll.JOB})
    assert row["status"] == "ok" and row["detail"]["admitted"] == 3
