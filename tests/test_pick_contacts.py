"""The pick_contacts job (SPEC 9; Harry, 1 Oct 2026): who we write to.

Ranking at 10-49 and 50-249 staff, the reveal fallback, at most two reveals, the budget and
the floor, CA/WA and outside-US exclusion, the email checks, the two-day lookahead, nobody
paid for twice, and the wiring. Apollo answers on a FakeTransport.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from us_outbound import limits
from us_outbound.clients.guard import APOLLO_READ_ACTIONS
from us_outbound.contacts import pick
from us_outbound.logs import hash_email
from us_outbound.ops import cli
from us_outbound.ops.schedule import by_name

ORG = "5e66b6381e05b4008c8331b8"
ORG2 = "5e66b6381e05b4008c8331b9"
ORG3 = "5e66b6381e05b4008c8331ba"
BALANCE = 30000.0


class FakeApollo:
    """People search and bulk match on the transport; records who was searched for and revealed."""

    def __init__(self, transport, people=None, matches=None, balance=BALANCE):
        self.people = people or {}  # org id or domain -> search rows
        self.matches = matches or {}  # person id -> bulk_match row
        self.searches: list[dict] = []
        self.revealed: list[str] = []
        transport.route("POST", "/mixed_people/api_search", fn=self.search)
        transport.route("POST", "/people/bulk_match", fn=self.match)
        transport.route("POST", "/usage_stats/credit_usage_stats",
                        body={"credit_usage_stats": {"lead_credit": {"left_over": balance}}})

    def search(self, req):
        self.searches.append(req.json)
        key = (req.json.get("organization_ids") or req.json.get("q_organization_domains_list"))[0]
        return {"people": self.people.get(key, [])}

    def match(self, req):
        ids = [d["id"] for d in req.json["details"]]
        self.revealed += ids
        found = [self.matches.get(i) for i in ids]
        return {"matches": found, "credits_consumed": float(sum(1 for m in found if m and m.get("email")))}


def account(**kw) -> dict:
    return {
        "account_id": "acc-1", "domain": "brightfin.com", "clean_name": "Brightfin", "apollo_org_id": ORG,
        "industry": "Fintech", "industry_group": "Technology & Startups", "employees": 30, "size_band": "20-49",
        "hq_state": "NY", "tier": "Priority", "score": 60, "status": "verified", **kw,
    }


def person(pid: str, title: str, **kw) -> dict:
    return {"id": pid, "first_name": "Pat", "last_name_obfuscated": "Le***", "title": title, **kw}


def match(pid: str, email: str, status: str = "verified", **kw) -> dict:
    return {"id": pid, "first_name": "Pat", "last_name": "Lee", "email": email, "email_status": status,
            "state": "New York", "country": "United States", **kw}


def setup(ctx, transport, accounts, people, matches, **general):
    if general:
        ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general, **general))
    ctx.store.insert("accounts", accounts)
    return FakeApollo(transport, people, matches)


def facts(ctx, fact):
    return [e for e in ctx.store.select("signal_events", {"source": pick.SOURCE}) if e["fact"] == fact]


TEAM = [
    person("p-hr", "HR Manager"),
    person("p-office", "Office Manager"),
    person("p-coo", "COO"),
    person("p-hop", "Head of People"),
    person("p-ceo", "Co-Founder & CEO"),
    person("p-ea", "Executive Assistant to the CEO"),
    person("p-cfo", "CFO"),
]


def everyone_verified():
    return {p["id"]: match(p["id"], f"{p['id'][2:]}@brightfin.com") for p in TEAM}


# -- ranking ---------------------------------------------------------------------------------------


def test_at_10_to_49_the_founder_is_revealed_and_written(ctx, transport):
    matches = everyone_verified()
    matches["p-ceo"] = match("p-ceo", "Omar.Reyes@Brightfin.com", first_name="OMAR", last_name="REYES, MBA",
                             state="Illinois", title="Co-Founder & CEO")
    apollo = setup(ctx, transport, [account()], {ORG: TEAM}, matches)
    out = pick.run(ctx)
    assert (out["status"], out["picked"], out["reveals"], out["credits"]) == ("ok", 1, 1, 1.0)
    assert apollo.revealed == ["p-ceo"]
    [c] = ctx.store.select("contacts")
    assert {k: c[k] for k in ("account_id", "role", "title", "first_name", "last_name", "email", "email_sha256",
                              "email_status", "email_source", "person_state", "created_at")} == {
        "account_id": "acc-1", "role": "Founder or executive", "title": "Co-Founder & CEO", "first_name": "Omar",
        "last_name": "Reyes", "email": "omar.reyes@brightfin.com", "email_sha256": hash_email("omar.reyes@brightfin.com"),
        "email_status": "verified", "email_source": "apollo", "person_state": "IL", "created_at": ctx.now,
    }
    [body] = apollo.searches
    assert body["organization_ids"] == [ORG] and body["person_locations"] == ["United States"]
    assert body["contact_email_status"] == ["verified"] and body["per_page"] == 100
    assert {"CEO", "Head of People", "COO", "Office Manager"} <= set(body["person_titles"])
    assert not {"HR Manager", "Partner", "Principal", "CFO"} & set(body["person_titles"])  # not contacted at 10-49
    [ledger] = ctx.store.select("credit_ledger")
    assert (ledger["system"], ledger["job"], ledger["account_id"], ledger["credits"], ledger["note"]) == (
        "apollo", "pick_contacts", "acc-1", 1.0, pick.LEDGER_NOTE)
    [outcome] = facts(ctx, pick.OUTCOME_FACT)
    assert outcome["value"]["outcome"] == "picked" and outcome["value"]["row"] == "Founder or executive"
    assert outcome["value"]["seniority"] == "C-level, founder or owner"
    # The person's Apollo id, so the opener knows when the contact is the new People leader (Harry, 2 Oct 2026).
    assert (outcome["value"]["contact_id"], outcome["value"]["apollo_person_id"]) == (c["contact_id"], "p-ceo")
    assert "omar" not in str(facts(ctx, pick.REVEAL_FACT) + facts(ctx, pick.OUTCOME_FACT)).lower()  # no names or emails


def test_at_50_to_249_the_senior_people_leader_is_revealed(ctx, transport):
    team = [person("p-ceo", "CEO"), person("p-hr", "HR Manager"), person("p-vp", "VP, People & Culture"),
            person("p-coo", "COO")]
    apollo = setup(ctx, transport, [account(employees=120, size_band="100-249")], {ORG: team},
                   {p["id"]: match(p["id"], f"{p['id']}@brightfin.com") for p in team})
    pick.run(ctx)
    assert apollo.revealed == ["p-vp"]
    [c] = ctx.store.select("contacts")
    assert (c["role"], c["title"]) == ("People leader", "VP, People & Culture")
    assert "HR Manager" in apollo.searches[0]["person_titles"]


@pytest.mark.parametrize("employees, band, revealed, role", [
    (120, "100-249", "p-hr", "People leader"),  # fourth at 50-249: before the office
    (30, "20-49", "p-office", "Operations"),  # not contacted at 10-49
])
def test_hr_managers_come_fourth_at_50_to_249_and_not_at_all_below(ctx, transport, employees, band, revealed, role):
    team = [person("p-office", "Office Manager"), person("p-hr", "HR Manager")]
    apollo = setup(ctx, transport, [account(employees=employees, size_band=band)], {ORG: team},
                   {p["id"]: match(p["id"], f"{p['id']}@brightfin.com") for p in team})
    pick.run(ctx)
    assert apollo.revealed == [revealed]
    assert ctx.store.select("contacts")[0]["role"] == role


def test_a_partner_is_the_founder_at_a_law_firm(ctx, transport):
    ctx.settings = dataclasses.replace(ctx.settings, industries=tuple(
        dataclasses.replace(i, active=True) if i.industry == "Legal Teams" else i for i in ctx.settings.industries))
    team = [person("p-para", "Paralegal"), person("p-om", "Office Manager"), person("p-partner", "Partner")]
    apollo = setup(ctx, transport, [account(domain="smithlaw.com", industry="Legal Teams", industry_group="Legal Teams")],
                   {ORG: team}, {p["id"]: match(p["id"], f"{p['id']}@smithlaw.com") for p in team})
    pick.run(ctx)
    assert apollo.revealed == ["p-partner"]
    assert ctx.store.select("contacts")[0]["role"] == "Founder or executive"
    assert "Partner" in apollo.searches[0]["person_titles"]


def test_without_an_apollo_id_the_search_uses_the_domain(ctx, transport):
    apollo = setup(ctx, transport, [account(apollo_org_id=None)], {"brightfin.com": TEAM}, everyone_verified())
    pick.run(ctx)
    assert apollo.searches[0]["q_organization_domains_list"] == ["brightfin.com"]
    assert "organization_ids" not in apollo.searches[0]
    assert apollo.revealed == ["p-ceo"]


# -- the reveal fallback ---------------------------------------------------------------------------------


def test_an_unverified_email_falls_back_to_the_next_candidate(ctx, transport):
    matches = everyone_verified()
    matches["p-ceo"] = match("p-ceo", "ceo@brightfin.com", status="unverified")
    apollo = setup(ctx, transport, [account()], {ORG: TEAM}, matches)
    out = pick.run(ctx)
    assert apollo.revealed == ["p-ceo", "p-hop"]
    assert ctx.store.select("contacts")[0]["role"] == "People leader"
    assert (out["reveals"], out["credits"], out["credits_per_account"]) == (2, 2.0, 2.0)
    first, second = sorted(facts(ctx, pick.REVEAL_FACT), key=lambda e: e["value"]["kept"])
    assert (first["value"]["apollo_person_id"], first["value"]["kept"], first["value"]["reason"]) == (
        "p-ceo", False, "email status unverified")
    assert second["value"]["kept"] is True


def test_at_most_two_reveals_then_no_contact_and_limits_say_so(ctx, transport):
    matches = {p["id"]: match(p["id"], f"{p['id']}@brightfin.com", status="unverified") for p in TEAM}
    apollo = setup(ctx, transport, [account()], {ORG: TEAM}, matches)
    out = pick.run(ctx)
    assert apollo.revealed == ["p-ceo", "p-hop"]
    assert ctx.store.select("contacts") == []
    reason = "no sendable email among the top 2 (email status unverified (2))"
    assert out["no_contact"] == {reason: 1} and out["picked"] == 0
    [outcome] = facts(ctx, pick.OUTCOME_FACT)
    assert outcome["value"]["outcome"] == "none" and outcome["value"]["reason"] == reason
    assert pick.no_contact(ctx.store, ctx.now) == {"acc-1": reason}
    assert limits.behind_ready(ctx, 0, {s: limits.budget.monthly(ctx.store, ctx.settings, s, ctx.now)
                                        for s in ("clay", "apollo")}) == (
        "Behind it: 1 verified accounts have no email yet. At 1 of them pick_contacts found no suitable contact "
        f"(mostly: {reason}); it tries again after 14 days.")


def test_nobody_is_paid_for_twice_and_a_hopeless_account_waits_14_days(ctx, transport):
    matches = {p["id"]: match(p["id"], f"{p['id']}@brightfin.com", status="unverified") for p in TEAM}
    apollo = setup(ctx, transport, [account()], {ORG: TEAM}, matches)
    pick.run(ctx)
    ctx.now += timedelta(days=1)
    again = pick.run(ctx)
    assert (again["status"], again["tried_recently"]) == ("skipped", 1) and len(apollo.revealed) == 2
    ctx.now += timedelta(days=14)
    pick.run(ctx)
    assert apollo.revealed == ["p-ceo", "p-hop", "p-coo", "p-office"]  # the next two, never the first two again


def test_apollo_finding_nobody_is_recorded(ctx, transport):
    setup(ctx, transport, [account()], {}, {})
    out = pick.run(ctx)
    assert out["no_contact"] == {
        "nobody at Apollo with a Roles-tab title for its size, in the US, with a verified email": 1}
    assert out["reveals"] == 0


# -- budget ----------------------------------------------------------------------------------------------


def test_a_spent_budget_spends_nothing_and_calls_nothing(ctx, transport):
    apollo = setup(ctx, transport, [account()], {ORG: TEAM}, everyone_verified())
    ctx.store.insert("credit_ledger", [{"entry_id": "e1", "system": "apollo", "credits": 2000.0,
                                        "occurred_at": ctx.now - timedelta(days=1)}])
    out = pick.run(ctx)
    assert (out["status"], out["reason"]) == ("skipped", "today's share of the month's Apollo budget is used")
    assert apollo.searches == [] and apollo.revealed == [] and transport.requests == []
    assert ctx.store.select("contacts") == []


def test_the_budget_stops_the_batch_partway(ctx, transport):
    # 4 credits for October with 4 weekdays left from Tue 27 Oct: 1 credit today.
    accounts = [account(), account(account_id="acc-2", domain="loop.com", apollo_org_id=ORG2, score=50)]
    loop = [person("q-ceo", "CEO")]
    apollo = setup(ctx, transport, accounts, {ORG: TEAM, ORG2: loop},
                   {**everyone_verified(), "q-ceo": match("q-ceo", "ceo@loop.com")}, apollo_monthly_credits=4)
    out = pick.run(ctx)
    assert apollo.revealed == ["p-ceo"] and out["picked"] == 1
    assert out["stopped"] == "today's share of the month's Apollo budget is used"
    assert sum(r["credits"] for r in ctx.store.select("credit_ledger")) == 1.0
    assert {e["account_id"] for e in facts(ctx, pick.OUTCOME_FACT)} == {"acc-1"}  # acc-2 is tried next run


def test_apollo_balance_at_the_floor_spends_nothing(ctx, transport):
    ctx.store.insert("accounts", [account()])
    apollo = FakeApollo(transport, {ORG: TEAM}, everyone_verified(), balance=5000.0)
    out = pick.run(ctx)
    assert out["status"] == "skipped" and "apollo_floor" in out["reason"]
    assert apollo.revealed == []


def test_a_failed_reveal_is_counted_in_case_apollo_charged(ctx, transport):
    setup(ctx, transport, [account()], {ORG: TEAM}, {})
    transport.route("POST", "/people/bulk_match", status=500, body={"error": "down"})
    out = pick.run(ctx)
    [ledger] = ctx.store.select("credit_ledger")
    assert ledger["credits"] == 1.0 and "failed (HTTP 500)" in ledger["note"]
    assert out["errors"] and out["credits"] == 1.0 and ctx.store.select("contacts") == []


# -- who may not be emailed (SPEC 1.5, 13) -----------------------------------------------------------------


def test_people_in_ca_wa_or_outside_the_us_are_left_out(ctx, transport):
    team = [person("p-ceo", "CEO", state="California"), person("p-hop", "Head of People", country="Canada"),
            person("p-coo", "COO"), person("p-om", "Office Manager")]
    matches = {"p-coo": match("p-coo", "coo@brightfin.com", state="WA"), "p-om": match("p-om", "om@brightfin.com", state="TX")}
    apollo = setup(ctx, transport, [account()], {ORG: team}, matches)
    pick.run(ctx)
    assert apollo.revealed == ["p-coo", "p-om"]  # the CEO and the Canadian are never paid for
    [c] = ctx.store.select("contacts")
    assert (c["title"], c["person_state"]) == ("Office Manager", "TX")
    kept = {e["value"]["apollo_person_id"]: e["value"]["reason"] for e in facts(ctx, pick.REVEAL_FACT)}
    assert kept == {"p-coo": "contact in CA or WA", "p-om": None}


@pytest.mark.parametrize("change, reason", [
    ({"email": "omar@gmail.com"}, "personal email domain"),
    ({"email": "info@brightfin.com"}, "shared mailbox"),
    ({"email": "omar@otherco.com"}, "email not at the company's domain"),
    ({"email": "omar@brightfin.com", "state": ""}, "contact state unknown"),
    ({"email": "omar@brightfin.com", "country": "Canada", "state": "Ontario"}, "contact state unknown"),
    ({"email": "jane@brightfin.com"}, "contact suppressed"),
    ({"email": "sam@brightfin.com"}, "already a contact"),
    ({"email": "omar@brightfin.com", "first_name": "", "last_name": ""}, "no first name"),
])
def test_a_revealed_email_that_may_not_be_sent_is_not_kept(ctx, transport, change, reason):
    matches = {"p-ceo": {**match("p-ceo", ""), **change}}
    apollo = setup(ctx, transport, [account()], {ORG: [person("p-ceo", "CEO", first_name="")]}, matches)
    ctx.store.insert("suppression", [{"email_sha256": hash_email("jane@brightfin.com"), "domain": None,
                                      "reason": "unsubscribe"}])
    ctx.store.insert("contacts", [{"contact_id": "old", "account_id": "acc-old", "email": "sam@brightfin.com",
                                   "email_sha256": hash_email("sam@brightfin.com")}])
    pick.run(ctx)
    assert apollo.revealed == ["p-ceo"]
    assert [c["contact_id"] for c in ctx.store.select("contacts")] == ["old"]
    [r] = facts(ctx, pick.REVEAL_FACT)
    assert r["value"]["reason"] == reason


def test_an_alias_of_the_company_domain_is_its_own(ctx, transport):
    ctx.store.insert("domain_aliases", [{"alias": "brightfin.io", "root_domain": "brightfin.com", "source": "redirect"}])
    setup(ctx, transport, [account()], {ORG: [person("p-ceo", "CEO")]}, {"p-ceo": match("p-ceo", "omar@brightfin.io")})
    assert pick.run(ctx)["picked"] == 1


# -- which accounts -----------------------------------------------------------------------------------------


def test_only_the_next_two_send_days_and_only_accounts_without_a_sendable_contact(ctx, transport):
    # 5 a week over 5 send days is 1 a day: the next two days need 2, and one is ready already.
    accounts = [
        account(account_id="acc-ready", domain="ready.com", apollo_org_id=ORG3, score=90),
        account(account_id="acc-top", domain="top.com", apollo_org_id=ORG, score=80),
        account(account_id="acc-next", domain="next.com", apollo_org_id=ORG2, score=40),
        account(account_id="acc-queued", domain="q.com", status="queued", score=99),
        account(account_id="acc-held", domain="h.com", tier="Held", score=99),
    ]
    team = [person("t-ceo", "CEO")]
    apollo = setup(ctx, transport, accounts, {ORG: team, ORG2: team, ORG3: team},
                   {"t-ceo": match("t-ceo", "ceo@top.com")}, weekly_enrol_cap=5)
    ctx.store.insert("contacts", [{"contact_id": "k-ready", "account_id": "acc-ready", "email": "jo@ready.com",
                                   "email_status": "verified", "person_state": "NY", "title": "CEO"}])
    out = pick.run(ctx)
    assert (out["ready_before"], out["wanted"], out["waiting"], out["picked"]) == (1, 1, 2, 1)
    assert [s["organization_ids"] for s in apollo.searches] == [[ORG]]  # the top of the queue only
    assert pick.run(ctx)["reason"] == "enough accounts are ready for the next two send days"


def test_dry_run_writes_contacts_and_makes_only_apollo_reads(ctx, transport):
    assert ctx.dry_run
    setup(ctx, transport, [account()], {ORG: TEAM}, everyone_verified())
    pick.run(ctx)
    assert len(ctx.store.select("contacts")) == 1
    apollo_calls = [c for c in ctx.guard.calls if c.system == "apollo"]
    assert apollo_calls and all(not c.write and c.sent and c.action in APOLLO_READ_ACTIONS for c in apollo_calls)
    assert {c.action for c in apollo_calls} == {"usage.credits", "people.search", "people.bulk_match"}


def test_the_job_is_wired_before_enrol():
    assert cli.resolve_job("pick_contacts") is pick.run
    j, enrol = by_name()["pick_contacts"], by_name()["enrol"]
    assert (j.cron, j.enabled, j.live) == ("30 5 * * 1-5", True, False)
    assert (j.cron.split()[1], enrol.cron.split()[1]) == ("5", "12")


def test_the_cli_runs_it_with_a_heartbeat(default_settings):
    from tests.test_cli import Harness

    h = Harness(default_settings)
    h.store.insert("accounts", [account()])
    FakeApollo(h.transport, {ORG: TEAM}, everyone_verified())
    assert h.run("dry-run", "pick_contacts") == 0
    [beat] = h.beats("pick_contacts")
    assert beat["status"] == "ok" and beat["dry_run"] is True
    assert (beat["detail"]["picked"], beat["detail"]["picked_by_row"]) == (1, {"Founder or executive": 1})


def test_the_search_writes_people_leader_facts_for_the_people_signals(ctx, transport):
    # 1 Oct 2026: no other job writes apollo_people, so "New People leader" could never fire.
    from datetime import date

    hired = (ctx.today_uk() - timedelta(days=40)).isoformat()
    people = [person("p1", "Head of People", state="New York", country="United States",
                     employment_history=[{"current": True, "start_date": hired}]),
              person("p2", "CEO", state="New York", country="United States")]
    facts = pick.people_facts(account(employees=80, size_band="50-99"), people, ctx.settings, ctx.today_uk(), ctx.now)
    by = {f["fact"]: f["value"] for f in facts}
    assert {f["source"] for f in facts} == {"apollo_people"}
    assert by == {"people_leader_count": 1, "people_leader_days_in_title": 40,
                  # Who the newest leader is, so the opener can congratulate them when they are the contact.
                  "people_leader_newest": {"apollo_person_id": "p1", "title": "Head of People", "days_in_title": 40}}
    # No People leader found: nothing is written (a verified-email search can miss one), so
    # "First People hire (likely)" (count = 0) never fires on a gap in the search.
    assert pick.people_facts(account(), [people[1]], ctx.settings, date.today(), ctx.now) == []


@pytest.mark.parametrize("searched_days_ago, written", [(None, True), (5, False), (31, True)])
def test_the_pick_leaves_apollo_people_s_fresher_full_search_alone(ctx, transport, searched_days_ago, written):
    """Harry, 5 Oct 2026: apollo_people searches every account without the email filter, and may write a count of 0
    and the days in title. pick_contacts' narrower view does not overwrite that within its 30 days."""
    from us_outbound.sources import apollo_people

    setup(ctx, transport, [account()], {ORG: TEAM}, everyone_verified())
    if searched_days_ago is not None:
        ctx.store.insert("signal_events", [{
            "event_id": "m1", "account_id": "acc-1", "source": "apollo_people", "fact": apollo_people.MARKER,
            "value": {"run_id": "r"}, "quote": "", "source_url": "", "observed_at": ctx.now - timedelta(days=searched_days_ago)}])
    out = pick.run(ctx)
    assert out["picked"] == 1  # the pick itself is unchanged
    counts = [e for e in ctx.store.select("signal_events", {"source": "apollo_people"}) if e["fact"] == "people_leader_count"]
    assert bool(counts) is written and all(e["value"] == 1 for e in counts)
