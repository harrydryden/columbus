"""apollo_people (sources/apollo_people.py; Harry, 5 Oct 2026): the People leaders at every queue account from Apollo's
free people search, against a fake Apollo. Which accounts and in what order, the two searches' filters, coverage and the
rule that a count of 0 is written only at coverage 0.5 or more, the days in title from the time-in-title filter, facts
written only on change, the caps, pacing and a 429, dry-run, and the People signals firing in the rescore that follows,
for accounts nobody has contacted."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_context
from us_outbound.clients.guard import APOLLO_READ_ACTIONS
from us_outbound.clients.http import Response
from us_outbound.ops import cli, schedule
from us_outbound.ops import heartbeat as hb
from us_outbound.scoring import score
from us_outbound.settings import load as loader
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import SOURCE_FIELDS, Focus, Override
from us_outbound.settings.validate import validate_all
from us_outbound.sources import apollo_people as people

NOW = datetime(2026, 10, 6, 3, 20, tzinfo=UTC)  # Tuesday 6 Oct, 04:20 UK
BASE, _ = validate_all(default_tabs())
TECH, AGENCIES = "Technology & Startups", "Marketing & Creative Agencies"
SHOWN = ("id", "first_name", "last_name_obfuscated", "title", "employment_history")  # what a search row carries


def person(pid: str, title: str, days: int | None = None, *, us: bool = True, verified: bool = True, **kw) -> dict:
    """A person Apollo holds: days is their time in title, which the search filters on but never returns."""
    return {"id": pid, "first_name": "Pat", "last_name_obfuscated": "Le***", "title": title, "days": days, "us": us,
            "verified": verified, **kw}


class FakeApollo:
    """People API Search as a transport: filters by organization, title, US, email status and time in title.

    orgs: Apollo id or domain -> {"people": [...], "total": the people Apollo holds there, "no_total": True to leave
    total_entries out}. fail: Apollo id or domain -> the HTTP status its searches get.
    """

    def __init__(self, orgs=None, *, fail=None):
        self.orgs, self.fail = orgs or {}, fail or {}
        self.searches: list[dict] = []

    def send(self, method, url, *, headers, params=None, json=None, data=None, timeout=30.0, idempotent=True):
        assert (method, url.rsplit("/api/v1", 1)[-1]) == ("POST", "/mixed_people/api_search"), url
        body = json or {}
        self.searches.append(body)
        key = (body.get("organization_ids") or body.get("q_organization_domains_list"))[0]
        if key in self.fail:
            return Response(self.fail[key], {"error": "refused"})
        org = self.orgs.get(key, {})
        rows = list(org.get("people", []))
        titled = "person_titles" in body
        if titled:
            want = [t.lower() for t in body["person_titles"]]
            rows = [p for p in rows if any(w in p["title"].lower() for w in want)]
        if "person_locations" in body:
            rows = [p for p in rows if p["us"]]
        if "contact_email_status" in body:
            rows = [p for p in rows if p["verified"]]
        if rng := body.get("person_days_in_current_title_range"):
            rows = [p for p in rows if p["days"] is not None and rng.get("min", 0) <= p["days"] <= rng.get("max", 10**6)]
        page, per = body["page"], body["per_page"]
        out = {"people": [{k: p[k] for k in SHOWN if k in p} for p in rows[(page - 1) * per: page * per]]}
        if not org.get("no_total"):
            out["total_entries"] = len(rows) if titled else org.get("total", len(rows))
        return Response(200, out)

    def kinds(self) -> list[str]:
        """Each search as "leaders", "coverage" or "days<=N"."""
        out = []
        for b in self.searches:
            rng = b.get("person_days_in_current_title_range")
            out.append(f"days<={rng['max']}" if rng else "leaders" if "person_titles" in b else "coverage")
        return out


def acct(aid: str, *, group=TECH, industry="Fintech", status="new", band="50-99", **kw) -> dict:
    return {"account_id": aid, "domain": f"{aid}.com", "apollo_org_id": f"org-{aid}", "status": status,
            "industry": industry, "industry_group": group, "size_band": band, "employees": None, "hq_state": "NY",
            "first_seen": NOW - timedelta(days=1), **kw}


def make(accounts=(), orgs=None, *, settings=None, now=NOW, live=False, fail=None):
    fake = FakeApollo(orgs, fail=fail)
    ctx = make_context(settings or BASE, job=people.JOB, now=now, transport=fake, live=live)
    if accounts:
        ctx.store.insert("accounts", list(accounts))
    return ctx, fake


def facts_of(ctx, aid) -> dict:
    out: dict = {}
    for e in sorted(ctx.store.select("signal_events", {"account_id": aid, "source": "apollo_people"}),
                    key=lambda e: e["observed_at"]):
        out[e["fact"]] = e
    return out


def values(ctx, aid) -> dict:
    return {f: e["value"] for f, e in facts_of(ctx, aid).items() if f != people.MARKER}


def marker(aid, days_ago) -> dict:
    return {"event_id": f"m-{aid}-{days_ago}", "account_id": aid, "source": "apollo_people", "fact": people.MARKER,
            "value": {"run_id": "earlier"}, "quote": "", "source_url": "", "observed_at": NOW - timedelta(days=days_ago)}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Pacing is recorded, not slept."""
    slept: list[float] = []
    monkeypatch.setattr(people, "_sleep", slept.append)
    return slept


# -- which accounts, in what order -------------------------------------------------------------------------------


def test_every_queue_account_is_searched_never_searched_first_then_focus_then_queue_order():
    ctx, fake = make([
        acct("standard", tier="Standard", score=30),
        acct("priority", status="queued", tier="Priority", score=60),
        acct("control", status="verified", tier="Control", score=0),
        acct("agency", group=AGENCIES, industry="Advertising agencies", tier="Priority", score=90),
        acct("old", tier="Priority", score=90),  # searched 40 days ago: due again, after the never-searched
        acct("recent", tier="Priority", score=90),  # searched 10 days ago: not due
        acct("excluded", tier="Excluded"), acct("held", tier="Held"), acct("enrolled", status="enrolled"),
        acct("nodomain", domain=""), acct("unsized", band=None),
    ], settings=dataclasses.replace(BASE, focus=(Focus(TECH, 0.6), Focus(AGENCIES, 0.4))))
    ctx.store.insert("signal_events", [marker("old", 40), marker("recent", 10)])
    todo = people.candidates(ctx)
    assert [a["account_id"] for a in todo.accounts] == ["priority", "standard", "control", "agency", "old"]
    assert dict(todo.left_out) == {"company size unknown": 1}
    out = people.run(ctx)
    assert out["searched"] == 5 and out["left_out"] == {"company size unknown": 1}
    assert [b.get("organization_ids") for b in fake.searches[::2]] == [
        ["org-priority"], ["org-standard"], ["org-control"], ["org-agency"], ["org-old"]]
    assert out["credits"] == 0 and out["requests"] == 10  # two searches an account with nobody found


def test_nothing_due_sends_nothing():
    ctx, fake = make([acct("recent")])
    ctx.store.insert("signal_events", [marker("recent", 3)])
    out = people.run(ctx)
    assert (out["candidates"], out["stopped_by"]) == (0, "no account is due a People search") and fake.searches == []


# -- the two searches ---------------------------------------------------------------------------------------------


def test_the_leaders_search_has_the_people_titles_in_the_us_without_the_email_filter():
    ctx, fake = make([acct("mid", band="50-99"), acct("small", band="20-49", apollo_org_id="")])
    people.run(ctx)
    leaders, coverage, small, small_coverage = fake.searches
    assert leaders["organization_ids"] == ["org-mid"] and leaders["person_locations"] == ["United States"]
    assert "contact_email_status" not in leaders and leaders["include_similar_titles"] is True
    assert leaders["per_page"] == 100 and leaders["page"] == 1
    # Exactly the titles pick_contacts contacts as a People leader at the size: HR managers count at 50-249 only.
    assert {"Head of People", "Chief People Officer", "HR Director", "HR Manager"} <= set(leaders["person_titles"])
    assert not {"CEO", "COO", "Office Manager", "CFO"} & set(leaders["person_titles"])
    assert "HR Manager" not in small["person_titles"] and "Head of People" in small["person_titles"]
    # The coverage search: the organization alone, one row a page, read for its total.
    assert coverage == {"organization_ids": ["org-mid"], "page": 1, "per_page": 1}
    assert small_coverage == {"q_organization_domains_list": ["small.com"], "page": 1, "per_page": 1}  # no Apollo id


# -- coverage, and the count of 0 -----------------------------------------------------------------------------------


def test_a_count_of_0_is_written_only_where_apollo_holds_half_the_headcount():
    ctx, _ = make([
        acct("deep", employees=80),  # 60 people at Apollo: 0.75
        acct("thin", employees=80),  # 30: 0.38
        acct("band", band="20-49"),  # no count: the band's top, 49; 30 people is at least 0.61
        acct("unknown", employees=80),  # Apollo gives no total
    ], {"org-deep": {"total": 60}, "org-thin": {"total": 30}, "org-band": {"total": 30}, "org-unknown": {"no_total": True}})
    out = people.run(ctx)
    assert values(ctx, "deep") == {"people_leader_count": 0, "people_leader_days_in_title": people.HORIZON_DAYS + 1,
                                   "people_search_coverage": 0.75, "people_found": 60}
    assert values(ctx, "thin") == {"people_search_coverage": 0.38, "people_found": 30}  # a 0 here would mean little
    assert values(ctx, "band") == {"people_leader_count": 0, "people_leader_days_in_title": people.HORIZON_DAYS + 1,
                                   "people_search_coverage": 0.61, "people_found": 30}
    assert values(ctx, "unknown") == {}
    mark = facts_of(ctx, "band")[people.MARKER]
    assert mark["value"]["employees_from"] == "size_band_top" and mark["value"]["count_written"] is True
    assert mark["quote"] == "Apollo people search: 0 People leaders in the US; 30 people at the company, 0.61 of 49 employees"
    assert (out["count_zero_written"], out["no_count_written"]) == (2, 2)
    assert out["coverage"] == {"at least 0.5": 2, "below 0.5": 1, "unknown": 1}
    assert out["coverage_by_band"] == {"20-49": {"searched": 1, "coverage at least 0.5": 1},
                                       "50-99": {"searched": 3, "coverage at least 0.5": 1}}


def test_coverage_reads_an_override_then_the_column_then_apollo_s_employees_fact():
    ctx, _ = make()
    a = acct("acme", employees=None, band="50-99")
    assert people.headcount(a, ctx.settings, 70) == (70, "apollo_org")
    assert people.headcount({**a, "employees": 64}, ctx.settings, 70) == (64, "employees")
    assert people.headcount(a, ctx.settings, None) == (99, "size_band_top")
    assert people.headcount({**a, "size_band": None}, ctx.settings, None) == (None, "")
    over = dataclasses.replace(BASE, overrides=(Override("acme.com", "employees", "120"),))
    assert people.headcount({**a, "employees": 64}, over, 70) == (120, "override")


# -- the leaders and their days in title ----------------------------------------------------------------------------


def test_a_new_leader_s_days_in_title_come_from_the_time_in_title_filter():
    team = [person("p-hop", "Head of People", 40), person("p-hrd", "HR Director", 400),
            person("p-ea", "Assistant to the Head of People", 10),  # junior: never a People leader
            person("p-uk", "VP of People", 5, us=False),  # outside the US
            person("p-ceo", "CEO", 900)]
    ctx, fake = make([acct("acme", employees=80)], {"org-acme": {"people": team, "total": 70}})
    out = people.run(ctx)
    v = values(ctx, "acme")
    assert v["people_leader_count"] == 2 and v["people_search_coverage"] == 0.88 and v["people_found"] == 70
    # Narrowed to PRECISION_DAYS; the upper end is written, so the 90-day window ends early, never late.
    assert 40 <= v["people_leader_days_in_title"] <= 40 + people.PRECISION_DAYS
    assert v["people_leader_newest"] == {"apollo_person_id": "p-hop", "title": "Head of People",
                                         "days_in_title": v["people_leader_days_in_title"]}
    assert fake.kinds()[:3] == ["leaders", "coverage", f"days<={people.HORIZON_DAYS}"]
    assert all(k.startswith("days<=") for k in fake.kinds()[3:]) and len(fake.searches) <= 8
    assert out["leader_new_in_title"] == 1 and out["requests"] == len(fake.searches)
    mark = facts_of(ctx, "acme")[people.MARKER]["value"]
    assert mark["leader_titles"] == ["HR Director", "Head of People"] and mark["leaders"] == 2


def test_a_leader_in_title_longer_than_the_horizon_takes_one_more_search_and_is_not_new():
    ctx, fake = make([acct("acme", employees=80)], {"org-acme": {"people": [person("p", "Head of HR", 700)], "total": 20}})
    people.run(ctx)
    assert fake.kinds() == ["leaders", "coverage", f"days<={people.HORIZON_DAYS}"]
    # A leader found: the count stands at any coverage (0.25 here); nobody new, and no newest leader named.
    assert values(ctx, "acme") == {"people_leader_count": 1, "people_leader_days_in_title": people.HORIZON_DAYS + 1,
                                   "people_search_coverage": 0.25, "people_found": 20}


def test_a_row_with_employment_history_is_read_directly():
    started = (NOW.date() - timedelta(days=12)).isoformat()
    row = person("p", "Chief People Officer", 12, employment_history=[{"current": True, "start_date": started}])
    ctx, fake = make([acct("acme", employees=80)], {"org-acme": {"people": [row], "total": 50}})
    people.run(ctx)
    assert fake.kinds() == ["leaders", "coverage"]
    assert values(ctx, "acme")["people_leader_days_in_title"] == 12


# -- written only on change -----------------------------------------------------------------------------------------


def test_facts_are_written_only_when_they_change_or_are_refresh_days_old():
    started = (NOW.date() - timedelta(days=40)).isoformat()
    row = person("p-hop", "Head of People", 40, employment_history=[{"current": True, "start_date": started}])
    ctx, _ = make([acct("acme", employees=80)], {"org-acme": {"people": [row], "total": 60}})

    def stored(fact, value, days_ago):
        return {"event_id": f"{fact}-{days_ago}", "account_id": "acme", "source": "apollo_people", "fact": fact,
                "value": value, "quote": "", "source_url": "", "observed_at": NOW - timedelta(days=days_ago)}

    # What pick_contacts wrote three days ago (37 days in title then: 40 today), and an old coverage.
    ctx.store.insert("signal_events", [
        stored("people_leader_count", 1, 3), stored("people_leader_days_in_title", 37, 3),
        stored("people_leader_newest", {"apollo_person_id": "p-hop", "title": "Head of People", "days_in_title": 37}, 3),
        stored("people_search_coverage", 0.75, 31), stored("people_found", 50, 3)])
    out = people.run(ctx)
    written = {e["fact"] for e in ctx.store.select("signal_events", {"source": "apollo_people"})
               if e["observed_at"] == NOW}
    # The same leader, count and days: not again. The coverage is as it was but 31 days old: again. 60 found, not 50.
    assert written == {people.MARKER, "people_search_coverage", "people_found"}
    assert out["unchanged"] == 3 and out["facts"] == 3


# -- caps, pacing, errors -----------------------------------------------------------------------------------------


def test_the_run_stops_at_its_caps_and_paces_its_requests(monkeypatch, no_waiting):
    accounts = [acct(f"a{i}", score=90 - i) for i in range(5)]
    ctx, fake = make(accounts)
    monkeypatch.setattr(people, "MAX_ACCOUNTS_PER_RUN", 2)
    out = people.run(ctx)
    assert (out["searched"], out["left_for_next_run"], out["stopped_by"]) == (2, 3, "the run's cap of 2 accounts")
    assert no_waiting == [people.PACE_SECONDS] * (len(fake.searches) - 1)  # between requests, never before the first

    monkeypatch.setattr(people, "MAX_ACCOUNTS_PER_RUN", 150)
    monkeypatch.setattr(people, "MAX_REQUESTS_PER_RUN", 3)
    out = people.run(ctx)  # the two searched above are not due; the next two start before 3 requests are sent
    assert (out["searched"], out["stopped_by"]) == (2, "the run's cap of 3 requests")

    clock = iter([0.0, 0.0, people.RUN_SECONDS + 1, people.RUN_SECONDS + 1])  # start, 1st account, 2nd, the summary
    monkeypatch.setattr(people, "_clock", lambda: next(clock))
    monkeypatch.setattr(people, "MAX_REQUESTS_PER_RUN", 300)
    ctx, _ = make(accounts)
    out = people.run(ctx)
    assert out["searched"] == 1 and out["stopped_by"].startswith("9 minutes")


def test_a_429_stops_the_run_and_its_account_waits_for_the_next():
    ctx, fake = make([acct("a", score=90), acct("b", score=80), acct("c", score=70)],
                     {"org-a": {"total": 60}, "org-c": {"total": 60}}, fail={"org-b": 429})
    out = people.run(ctx)
    assert out["stopped_by"].startswith("Apollo's rate limit") and out["searched"] == 1
    assert facts_of(ctx, "a") and facts_of(ctx, "b") == {} and facts_of(ctx, "c") == {}  # b is due again tomorrow
    assert [a["account_id"] for a in people.candidates(ctx).accounts] == ["b", "c"]


def test_another_error_skips_the_account_and_a_wrong_key_stops_the_job():
    ctx, _ = make([acct("a", score=90), acct("b", score=80)], {"org-b": {"total": 60}}, fail={"org-a": 500})
    out = people.run(ctx)
    assert out["searched"] == 1 and len(out["errors"]) == 1 and facts_of(ctx, "a") == {} and facts_of(ctx, "b")
    ctx, _ = make([acct("a")], fail={"org-a": 401})
    with pytest.raises(Exception, match="401"):
        people.run(ctx)


def test_dry_run_reads_apollo_and_writes_only_the_database():
    ctx, _ = make([acct("acme", employees=80)], {"org-acme": {"people": [person("p", "Head of People", 30)], "total": 60}})
    assert ctx.dry_run
    people.run(ctx)
    assert facts_of(ctx, "acme") and ctx.store.select("credit_ledger") == []  # 0 credits: nothing to record
    assert {c.system for c in ctx.guard.writes()} == {"db"}
    apollo = [c for c in ctx.guard.calls if c.system == "apollo"]
    assert apollo and all(not c.write and c.sent and c.action == "people.search" for c in apollo)
    assert {c.action for c in apollo} <= APOLLO_READ_ACTIONS


# -- the signals fire before the queue is sorted --------------------------------------------------------------------


def matched(ctx) -> dict[str, set[str]]:
    score.rescore(ctx)
    out: dict[str, set[str]] = {}
    for e in ctx.store.select("signal_events", {"source": "scoring"}):
        out.setdefault(e["account_id"], set()).add(e["value"]["signal"])
    return out


def jobs(aid, open_people_roles=1) -> list[dict]:
    return [{"event_id": f"j-{aid}-{f}", "account_id": aid, "source": "apollo_jobs", "fact": f, "value": v, "quote": "",
             "source_url": "", "observed_at": NOW - timedelta(days=2)}
            for f, v in (("open_people_roles", open_people_roles), ("open_roles", 2))]


def test_new_people_leader_fires_for_an_account_nobody_has_contacted():
    ctx, _ = make([acct("acme", employees=80)], {"org-acme": {"people": [person("p", "VP of People", 30)], "total": 60}})
    assert "New People leader" not in matched(ctx).get("acme", set())
    people.run(ctx)
    got = matched(ctx)["acme"]
    assert {"New People leader", "People leader in place"} <= got and ctx.store.select("contacts") == []
    tier = ctx.store.get("accounts", account_id="acme")
    assert "New People leader (+30)" in tier["tier_reason"]


def test_first_people_hire_likely_fires_only_where_coverage_makes_the_0_meaningful():
    ctx, _ = make([acct("deep", employees=80), acct("thin", employees=80), acct("staffed", employees=80)],
                  {"org-deep": {"total": 60}, "org-thin": {"total": 20},
                   "org-staffed": {"people": [person("p", "Head of People", 900)], "total": 60}})
    ctx.store.insert("signal_events", [*jobs("deep"), *jobs("thin"), *jobs("staffed")])
    people.run(ctx)
    got = matched(ctx)
    assert {"First People hire (likely)", "People role open"} <= got["deep"]
    assert "First People hire (likely)" not in got["thin"] and "People role open" in got["thin"]
    assert "First People hire (likely)" not in got["staffed"] and "People leader in place" in got["staffed"]
    assert "New People leader" not in got["deep"] | got["staffed"]
    # A later search that finds the coverage thin turns it off: the latest coverage is in the condition.
    ctx.store.insert("signal_events", [{"event_id": "later", "account_id": "deep", "source": "apollo_people",
                                        "fact": "people_search_coverage", "value": 0.3, "quote": "", "source_url": "",
                                        "observed_at": NOW + timedelta(days=1)}])
    ctx.now = NOW + timedelta(days=1)
    assert "First People hire (likely)" not in matched(ctx)["deep"]


def test_the_new_signal_row_and_its_facts_validate():
    assert {"people_search_coverage", "people_found"} <= SOURCE_FIELDS["apollo_people"]
    row = {s.signal: s for s in BASE.signals}["First People hire (likely)"]
    assert (row.weight, row.counts_for_days, row.active, row.suggests_angle) == (20, 60, True, "Growing team")
    assert row.condition.fields == {"open_people_roles", "people_leader_count", "people_search_coverage"}
    assert row.role_openers == {s.signal: s for s in BASE.signals}["People role open"].role_openers
    assert people.sheet_notice(BASE) is None


def test_a_signals_load_switches_the_old_row_off_and_brings_the_new_one():
    build = default_tabs()["Signals"]
    old = {**next(r for r in build if r["signal"] == "First People hire (likely)"), "signal": "First People hire",
           "looks_for": "open_people_roles >= 1 AND people_leader_count = 0", "weight": "25", "counts_for_days": "90",
           "note": "SPEC 5 default."}
    sheet = [r for r in build if r["signal"] != "First People hire (likely)"] + [old]
    before, _ = validate_all({**default_tabs(), "Signals": sheet})
    assert "still on" in people.sheet_notice(before)
    plan = loader.plan_tab("Signals", sheet, build)
    by = {r["signal"]: r for r in plan.rows}
    assert by["First People hire"]["active"] == "no"
    assert by["First People hire"]["note"] == "Replaced by First People hire (likely) (5 Oct 2026). SPEC 5 default."
    assert "First People hire switched off (replaced by First People hire (likely))" in plan.updated
    assert "First People hire (likely)" in plan.added and by["First People hire (likely)"]["weight"] == "20"
    after, errors = validate_all({**default_tabs(), "Signals": plan.rows})
    assert not any(errors.values()) and people.sheet_notice(after) is None
    assert [s.signal for s in after.active_signals() if s.signal.startswith("First People hire")] == [
        "First People hire (likely)"]


# -- the schedule, the registry and the CLI -------------------------------------------------------------------------


def test_it_runs_weekdays_at_04_20_after_apollo_enrich_and_before_verify_accounts():
    table = schedule.by_name()
    job = table["apollo_people"]
    assert (job.cron, job.live, job.enabled, job.timeout_minutes) == ("20 4 * * 1-5", False, True, 20)

    def at(name):
        minute, hour = table[name].cron.split()[:2]
        return int(hour) * 60 + int(minute)

    assert at("apollo_enrich") < at("apollo_people") < at("verify_accounts") < at("pick_contacts")
    assert at("apollo_people") + people.RUN_SECONDS // 60 < at("verify_accounts")  # no account starts after 04:29
    assert cli.resolve_job("apollo_people") is people.run
    assert hb.EXPECTED["apollo_people"] == 26 * 60 and "apollo_people" in hb.WEEKDAY_JOBS
    assert "apollo_people" in schedule.enabled_names()


def test_harry_can_run_it_by_hand():
    contexts = []
    store = None

    def factory(name, live_flag, operator=False):
        nonlocal store
        fake = FakeApollo({"org-acme": {"people": [person("p", "Head of People", 30)], "total": 60}})
        ctx = make_context(BASE, job=name, now=NOW, transport=fake, store=store)
        if store is None:
            ctx.store.insert("accounts", [acct("acme", employees=80)])
        store = ctx.store
        contexts.append(ctx)
        return ctx

    assert cli.main(["dry-run", "apollo_people"], context_factory=factory) == 0
    assert cli.main(["run", "apollo_people"], context_factory=factory) == 0
    first, second = (store.get("heartbeats", run_id=c.run_id) for c in contexts)
    assert first["status"] == second["status"] == "ok"
    assert first["detail"]["with_people_leader"] == 1 and second["detail"]["candidates"] == 0  # not again for 30 days
