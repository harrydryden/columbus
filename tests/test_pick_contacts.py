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
    assert limits.behind_ready(ctx, 0, {"apollo": limits.budget.monthly(ctx.store, ctx.settings, "apollo",
                                                                         ctx.now)}) == (
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


def pause_source(ctx, source: str, reason: str = "5% bounced", status: str = "open") -> None:
    from us_outbound.base import holds

    ctx.store.upsert("hitl_items", [{"item_id": f"k-{source}", "kind": holds.KIND, "status": status,
                                     "payload": {"action": holds.PAUSE_SOURCE, "target": source, "reason": reason}}])


def test_a_kill_rule_pausing_the_apollo_source_stops_every_reveal_and_says_so(ctx, transport):
    """Harry, 7 Oct 2026: no Apollo reveal (no credit) while a kill rule pauses the apollo email source. (Clay's
    email waterfall went with W2-1, so there is no other source to fall back on.)"""
    apollo = setup(ctx, transport, [account()], {ORG: TEAM}, everyone_verified())
    pause_source(ctx, "apollo")
    out = pick.run(ctx)
    why = ("a kill rule pauses the apollo email source (5% bounced): no Apollo reveals; `us-outbound killrules clear "
           "ITEM_ID --live` lifts it once checked")
    assert (out["status"], out["reason"], out["apollo_paused"]) == ("skipped", why, why)
    assert apollo.searches == [] and apollo.revealed == [] and transport.requests == []  # nor the balance
    assert ctx.store.select("credit_ledger") == [] and ctx.store.select("contacts") == []
    assert facts(ctx, pick.OUTCOME_FACT) == []  # nobody is marked "no contact": it is tried again once cleared
    # Another source's pause (an old clay hold) leaves Apollo's reveals going.
    pause_source(ctx, "apollo", status="handled")
    pause_source(ctx, "clay")
    out = pick.run(ctx)
    assert out["picked"] == 1 and apollo.revealed == ["p-ceo"] and "apollo_paused" not in out


def test_the_heartbeat_says_the_apollo_source_is_paused(default_settings):
    from tests.test_cli import Harness

    h = Harness(default_settings)
    h.store.insert("accounts", [account()])
    FakeApollo(h.transport, {ORG: TEAM}, everyone_verified())
    pause_source(h, "apollo")
    assert h.run("run", "pick_contacts") == 0
    [beat] = h.beats("pick_contacts")
    # The job said it skipped, so the row says so (W1-1: the heartbeat read only a truthy "skipped" key and stored
    # this run as ok). A skip for a reason of its own still shows the job alive (base/heartbeats._shows_alive).
    assert beat["status"] == "skipped" and beat["detail"]["status"] == "skipped"
    assert beat["detail"]["reason"].startswith("a kill rule pauses the apollo email source (5% bounced)")
    from us_outbound.ops.health import latest_runs

    assert latest_runs(h.store)["pick_contacts"]["last_alive_at"] is not None


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
    # The order and the hold come from the facts, as the rescore pick_contacts starts with sets them (W1-3a): top.com
    # is hiring (apollo_signals' open_roles, "Hiring and growth"), so it outranks next.com; a competitor's counselling
    # on h.com's careers page is the default Signals tab's Hold row ("Modern mental-health vendor named").
    ctx.store.insert("signal_events", [
        {"event_id": "top-roles", "account_id": "acc-top", "source": "apollo_jobs", "fact": "open_roles", "value": 5,
         "quote": "Apollo lists 5 open job postings", "source_url": "", "observed_at": ctx.now},
        {"event_id": "held-lyra", "account_id": "acc-held", "source": "clay_careers", "fact": "benefit",
         "value": {"item": "Lyra"}, "quote": "Counseling through Lyra.", "source_url": "https://h.com/careers",
         "observed_at": ctx.now}])
    out = pick.run(ctx)
    assert ctx.store.get("accounts", account_id="acc-held")["tier"] == "Held"
    assert (out["ready_before"], out["wanted"], out["waiting"], out["picked"]) == (1, 1, 2, 1)
    assert [s["organization_ids"] for s in apollo.searches] == [[ORG]]  # the top of the queue only
    assert pick.run(ctx)["reason"] == "enough accounts are ready for the next two send days"


def test_ready_accounts_are_the_ones_with_a_sendable_contact_in_queue_order(ctx):
    """The W1-C contract (19 Oct 2026): ready_accounts(ctx) is the ready set itself, in enrol's order, and waiting()
    is built on the same read. Four verified Priority accounts: b (score 90) and a (40) have a sendable contact; c (70)
    has only a contact in CA, whom enrol may never email (SPEC 1.5), so it still needs one; d (99) has a sendable
    contact but is a partner, which enrol never takes. queue.order_key is tier, then score high first, so the ready
    set is [b, a] (90 before 40), waiting() is ([c], 2), and d is in neither."""
    ctx.store.insert("accounts", [
        account(account_id="acc-a", domain="a.com", score=40),
        account(account_id="acc-b", domain="b.com", score=90),
        account(account_id="acc-c", domain="c.com", score=70),
        account(account_id="acc-d", domain="d.com", score=99),
    ])
    ctx.store.insert("partners", [{"domain": "d.com", "name": "D", "reason": "a partner"}])
    ctx.store.insert("contacts", [
        {"contact_id": f"k-{x}", "account_id": f"acc-{x}", "email": f"jo@{x}.com", "email_status": "verified",
         "person_state": "CA" if x == "c" else "NY", "title": "CEO"} for x in "abcd"])
    assert [a["account_id"] for a in pick.ready_accounts(ctx)] == ["acc-b", "acc-a"]
    need, ready = pick.waiting(ctx)
    assert ([a["account_id"] for a in need], ready) == (["acc-c"], 2)


# -- the random explore arm and enrol's gates (W1-6) -------------------------------------------------------------


def arm_world(ctx, transport, monkeypatch, n_tech=15, n_creative=15, **general):
    """n_tech Technology & Startups and n_creative Marketing & Creative Agencies accounts, verified, none with a
    contact, each with a CEO Apollo reveals; scores 100 down (Tech and Creative alternate), and kept as set (no
    rescore). weekly_enrol_cap 25 over 5 send days: a lookahead of 2 × ceil(25 ÷ 5) = 10."""
    monkeypatch.setattr(pick, "_rescore", lambda ctx: {})
    accounts, people, matches = [], {}, {}
    for i in range(n_tech + n_creative):
        tech = i % 2 == 0 if i < 2 * min(n_tech, n_creative) else n_tech > n_creative
        group = "Technology & Startups" if tech else "Marketing & Creative Agencies"
        industry = "Fintech" if tech else "Advertising agencies"
        aid, domain = f"acc-{i:02d}", f"co{i:02d}.com"
        accounts.append(account(account_id=aid, domain=domain, apollo_org_id="", score=100 - i, industry=industry,
                                industry_group=group))
        people[domain] = [person(f"p-{i:02d}", "CEO")]
        matches[f"p-{i:02d}"] = match(f"p-{i:02d}", f"ceo@{domain}")
    apollo = setup(ctx, transport, accounts, people, matches, weekly_enrol_cap=25, **general)
    return apollo, {a["account_id"]: a for a in accounts}


def searched(apollo) -> list[str]:
    return [s["q_organization_domains_list"][0] for s in apollo.searches]


def test_explore_share_0_picks_in_queue_order_and_stamps_ranked(ctx, transport, monkeypatch):
    """No explore_share row (default 0): the lookahead's 10 are the top 10 by score, as before W1-6, and each picked
    account's arm is ranked."""
    apollo, _ = arm_world(ctx, transport, monkeypatch)
    out = pick.run(ctx)
    assert (out["wanted"], out["picked"]) == (10, 10)
    assert searched(apollo) == [f"co{i:02d}.com" for i in range(10)]
    assert out["explore"] == {"share": 0.0, "target": 0, "drawn": 0, "picked": 0, "realised": None, "by_group": {}}
    arms = {a["account_id"]: a.get("arm") for a in ctx.store.select("accounts")}
    assert {arms[f"acc-{i:02d}"] for i in range(10)} == {"ranked"}
    assert {arms[f"acc-{i:02d}"] for i in range(10, 30)} == {None}  # not picked: no arm


def test_the_random_arm_is_drawn_first_within_the_focus_shares(ctx, transport, monkeypatch):
    """explore_share 0.15, want 10: k = explore_count(10, 0.15, 30) = round(1.5) = 2 (half up). The default Focus tab
    is Tech 50%, Creative 30%, Legal 20%: apportion(2) gives exact 1.0, 0.6, 0.4, whole parts 1, 0, 0, and the place
    left to Creative's 0.6, so 1 Tech and 1 Creative, drawn at random over all 30 (seed "pick:2026-10-27"), searched
    first; then the top 8 of the rest by score. The 2 are stamped random and the 8 ranked."""
    apollo, by_id = arm_world(ctx, transport, monkeypatch, explore_share=0.15)
    out = pick.run(ctx)
    assert out["picked"] == 10 and out["explore"]["drawn"] == 2 and out["explore"]["picked"] == 2
    assert out["explore"]["by_group"] == {"Technology & Startups": 1, "Marketing & Creative Agencies": 1}
    first_two = searched(apollo)[:2]
    random_ids = sorted(a["account_id"] for a in ctx.store.select("accounts") if a.get("arm") == "random")
    assert sorted(f"acc-{d[2:4]}" for d in first_two) == random_ids and len(random_ids) == 2
    assert {by_id[a]["industry_group"] for a in random_ids} == {"Technology & Startups", "Marketing & Creative Agencies"}
    rest = [f"co{i:02d}.com" for i in range(30) if f"co{i:02d}.com" not in first_two][:8]
    assert searched(apollo)[2:] == rest
    assert sum(a.get("arm") == "ranked" for a in ctx.store.select("accounts")) == 8
    # The draw is the day's: the same seed as queue.split would use, so a rerun draws the same accounts.
    from us_outbound.enrol import queue

    drawn, _ = queue.split(list(by_id.values()), ctx.settings, "pick:2026-10-27", 2)
    assert sorted(a["account_id"] for a in drawn) == random_ids


def test_a_random_account_with_nobody_to_contact_is_replaced_from_its_own_group(ctx, transport, monkeypatch):
    """The whole-wave review's scenario 5: half of each group's accounts have nobody at Apollo. explore_share
    0.15, want 10: k = 2 places, 1 Tech and 1 Creative (as above). Before, the 2 drawn were tried once each and an
    account with nobody was not replaced, so the arm kept about p_contact × 2 (2 in only a quarter of days). Now each
    stratum's accounts are tried in their random order (queue.strata, seed "pick:2026-10-27") until its place has a
    contact: the arm keeps exactly 2, one of each group, each the first account with a contact in its group's order,
    and the accounts tried before it are the ones with nobody (recorded, so they cool off like any). The other 8
    contacts are ranked; the summary reports the realised share, 2 of 10."""
    from us_outbound.enrol import queue

    apollo, by_id = arm_world(ctx, transport, monkeypatch, explore_share=0.15)
    def has_contact(i: int) -> bool:  # arm_world alternates Tech (even i) and Creative (odd i): half of each
        return (i // 2) % 2 == 0

    for i in range(30):
        if not has_contact(i):
            apollo.people[f"co{i:02d}.com"] = []
    out = pick.run(ctx)
    assert out["picked"] == 10
    ex = out["explore"]
    assert (ex["target"], ex["picked"], ex["realised"]) == (2, 2, 0.2)
    assert ex["by_group"] == {"Technology & Startups": 1, "Marketing & Creative Agencies": 1}
    random_ids = sorted(a["account_id"] for a in ctx.store.select("accounts") if a.get("arm") == "random")
    expected, tried = [], set()
    for _, places, pool in queue.strata(list(by_id.values()), ctx.settings, "pick:2026-10-27", 2):
        assert places == 1
        firsts = [a for a in pool if has_contact(int(a["account_id"][-2:]))]
        expected.append(firsts[0]["account_id"])
        tried |= {a["account_id"] for a in pool[: pool.index(firsts[0]) + 1]}
    assert random_ids == sorted(expected)
    assert ex["drawn"] == len(tried)
    assert {f"acc-{d[2:4]}" for d in searched(apollo)[: len(tried)]} == tried  # the random lane is searched first
    assert sum(a.get("arm") == "ranked" for a in ctx.store.select("accounts")) == 8


def test_a_kill_rule_stopping_a_group_frees_the_lookahead_for_the_others(ctx, transport, monkeypatch):
    """L1 (code-contacts-enrol): 60 Tech accounts ready (each with a sendable contact) and 5 Creative accounts
    waiting, under a kill rule that stops Technology & Startups. Before W1-6 waiting() counted the 60 as ready, 60 >=
    the lookahead of 60 (weekly 150 over 5 days: 2 × 30), so "enough accounts are ready" and nothing was revealed;
    enrol could email none of them. Now the stopped group's accounts are in neither list: ready 0, want 60, and the 5
    Creative accounts are searched and picked."""
    from us_outbound.base import holds

    monkeypatch.setattr(pick, "_rescore", lambda ctx: {})
    tech = [account(account_id=f"t{i:02d}", domain=f"t{i:02d}.com", apollo_org_id="", score=90) for i in range(60)]
    creative = [account(account_id=f"c{i}", domain=f"c{i}.com", apollo_org_id="", score=50,
                        industry="Advertising agencies", industry_group="Marketing & Creative Agencies")
                for i in range(5)]
    people = {f"c{i}.com": [person(f"pc{i}", "CEO")] for i in range(5)}
    matches = {f"pc{i}": match(f"pc{i}", f"ceo@c{i}.com") for i in range(5)}
    apollo = setup(ctx, transport, tech + creative, people, matches)
    ctx.store.insert("contacts", [{"contact_id": f"k-{a['account_id']}", "account_id": a["account_id"],
                                   "email": f"jo@{a['domain']}", "email_status": "verified", "person_state": "NY",
                                   "title": "CEO", "role": "Founder or executive"} for a in tech])
    assert pick.waiting(ctx)[1] == 60  # no kill rule yet: all 60 ready
    ctx.store.insert("hitl_items", [{"item_id": "kr-1", "kind": holds.KIND, "status": "open", "created_at": ctx.now,
                                     "payload": {"action": holds.STOP_GROUP, "target": "Technology & Startups",
                                                 "reason": "low replies"}}])
    need, ready = pick.waiting(ctx)
    assert (sorted(a["account_id"] for a in need), ready) == ([f"c{i}" for i in range(5)], 0)
    out = pick.run(ctx)
    assert (out["ready_before"], out["wanted"], out["picked"]) == (0, 60, 5)
    assert sorted(searched(apollo)) == [f"c{i}.com" for i in range(5)]


def test_a_paused_source_s_contact_is_not_ready_and_no_copy_means_no_search(ctx, transport, monkeypatch):
    """enrol.eligible's other gates: an account whose only contact came from an email source a kill rule paused needs
    a contact (enrol would leave the contact out); an account no sendable Copy row could serve is in neither list."""
    import dataclasses

    from us_outbound.base import holds

    ctx.store.insert("accounts", [account(account_id="acc-clay", domain="clay.com"),
                                  account(account_id="acc-ok", domain="ok.com")])
    ctx.store.insert("contacts", [
        {"contact_id": "k-clay", "account_id": "acc-clay", "email": "jo@clay.com", "email_status": "verified",
         "person_state": "NY", "title": "CEO", "email_source": "clay"},
        {"contact_id": "k-ok", "account_id": "acc-ok", "email": "jo@ok.com", "email_status": "verified",
         "person_state": "NY", "title": "CEO", "email_source": "apollo"}])
    ctx.store.insert("hitl_items", [{"item_id": "kr-2", "kind": holds.KIND, "status": "open", "created_at": ctx.now,
                                     "payload": {"action": holds.PAUSE_SOURCE, "target": "clay", "reason": "bounces"}}])
    need, ready = pick.waiting(ctx)
    assert ([a["account_id"] for a in need], ready) == (["acc-clay"], 1)
    no_copy = tuple(dataclasses.replace(c, status="draft") for c in ctx.settings.copy)
    ctx.settings = dataclasses.replace(ctx.settings, copy=no_copy)
    assert pick.waiting(ctx) == ([], 0) and pick.ready_accounts(ctx) == []


def test_dry_run_writes_contacts_and_makes_only_apollo_reads(ctx, transport):
    assert ctx.dry_run
    setup(ctx, transport, [account()], {ORG: TEAM}, everyone_verified())
    pick.run(ctx)
    assert len(ctx.store.select("contacts")) == 1
    apollo_calls = [c for c in ctx.guard.calls if c.system == "apollo"]
    assert apollo_calls and all(not c.write and c.sent and c.action in APOLLO_READ_ACTIONS for c in apollo_calls)
    assert {c.action for c in apollo_calls} == {"usage.credits", "people.search", "people.bulk_match"}


def test_it_rescores_once_before_it_reads_the_queue(ctx, transport, monkeypatch):
    """W1-3a (RS P0-8): apollo_enrich and apollo_signals no longer rescore; pick_contacts rescores once, first, so
    the queue it reads is in the order the morning's facts give. The rescore is called exactly once, and before
    the first Apollo search."""
    from us_outbound.scoring import score

    apollo = setup(ctx, transport, [account()], {ORG: TEAM}, everyone_verified())
    calls: list[int] = []
    real = score.rescore

    def counted(c):
        calls.append(len(apollo.searches))  # how many searches were made before the rescore
        return real(c)

    monkeypatch.setattr(score, "rescore", counted)
    out = pick.run(ctx)
    assert calls == [0] and out["picked"] == 1 and "tiers" in out["rescore"]


def test_a_failed_rescore_is_reported_and_the_contacts_are_still_found(ctx, transport, monkeypatch):
    from us_outbound.scoring import score

    setup(ctx, transport, [account()], {ORG: TEAM}, everyone_verified())

    def broken(c):
        raise RuntimeError("deadlock detected")

    monkeypatch.setattr(score, "rescore", broken)
    out = pick.run(ctx)
    # The error under "error" is what heartbeat_check reads as a caught failure (ops/health.py).
    assert out["rescore"] == {"error": "RuntimeError: deadlock detected"} and out["picked"] == 1


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
    assert h.run("run", "pick_contacts") == 0
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
