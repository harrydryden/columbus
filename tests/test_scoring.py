"""Scoring (SPEC 9 "Scoring"; SPEC 5 Signals; SPEC 8 "not read"): matching, freshness, caps, the score job.

Settings are built here from us_outbound.settings.model directly; test_tiers.py and
test_angle.py import these builders.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound.clients.db import MemoryStore
from us_outbound.scoring import score as scoring
from us_outbound.scoring.score import match_signal, rescore, score_account
from us_outbound.settings.conditions import parse_condition, parse_context_rule, parse_terms, try_parse_condition
from us_outbound.settings.model import TEXT_SOURCES, Angle, General, Override, Settings, Signal, State

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
VENDOR_CONTEXT = "Headspace: for Work, app, subscription; Calm: app, premium, business, subscription"


# -- builders ------------------------------------------------------------------------


def sig(
    name: str,
    sources: str,
    looks_for: str,
    weight: int = 0,
    action: str = "Score",
    days: int = 540,
    *,
    max_weight: int | None = None,
    angle: str = "",
    opener: str = "",
    context_rule: str = "",
    active: bool = True,
) -> Signal:
    srcs = tuple(s.strip() for s in sources.split(","))
    condition = try_parse_condition(looks_for)
    if condition is None and not any(s in TEXT_SOURCES for s in srcs):
        condition = parse_condition(looks_for)  # raises: a field source needs a condition
    terms = () if condition else parse_terms(looks_for)
    context = parse_context_rule(context_rule, terms) if context_rule and terms else {}
    return Signal(
        signal=name, sources=srcs, looks_for=looks_for, weight=weight, action=action, counts_for_days=days,
        active=active, context_rule=context_rule, max_weight=max_weight, suggests_angle=angle, opener=opener,
        terms=terms, condition=condition, context=context,
    )


# The SPEC 5 default signals.
MENTAL_HEALTH = sig(
    "Mental health support listed", "clay_careers",
    "mental health; EAP; employee assistance; therapy; counseling; counselling; wellbeing support; well-being support",
    25, angle="Progressive employer",
)
EAP_NAMED = sig("EAP named", "clay_careers", "EAP; employee assistance; ComPsych; GuidanceResources; Magellan", 10,
                angle="Upgrade the EAP")
VENDOR = sig(
    "Modern mental-health vendor named", "clay_careers, job_posts",
    "Talkspace; Lyra; Modern Health; Spring Health; Headspace; Calm; BetterUp; Nivati; Tava; Wellhub; Gympass; "
    "Wellbound; Justworks Plus",
    0, "Hold", context_rule=VENDOR_CONTEXT, angle="Switch from a competitor",
)
PROGRESSIVE = sig(
    "Progressive benefits", "clay_careers, job_posts",
    "wellness stipend; mental health day; unlimited PTO; four-day week; 4-day week; parental leave; sabbatical; "
    "100% employer-paid",
    10, max_weight=30, angle="Progressive employer",
)
VALUES = sig("Culture or values page", "clay_careers", "values_page = true", 10, angle="Progressive employer")
LEADER = sig("People leader in place", "apollo_people", "people_leader_count >= 1", 10, days=365)
NEW_LEADER = sig("New People leader", "apollo_people", "people_leader_days_in_title <= 90", 30, days=90,
                 angle="Progressive employer")
FIRST_HIRE = sig("First People hire", "apollo_jobs, apollo_people",
                 "open_people_roles >= 1 AND people_leader_count = 0", 25, days=90, angle="Growing team")
FUNDING = sig("Recent funding", "apollo_org, clay_funding", "days_since_funding <= 540", 20, angle="Growing team")
GROWTH = sig("Hiring and growth", "apollo_org", "open_roles >= 3 OR headcount_growth_12m >= 0.10", 15, days=90,
             angle="Growing team")
VISITED = sig("Visited the US site", "site_visits", "us_visits_30d >= 1", 20, days=30)
PRICING = sig("Viewed US pricing or demo page", "site_visits", "pricing_or_demo_visits_30d >= 1", 15, days=30)
NP_BUDGET = sig("Nonprofit budget", "irs_bmf", "revenue >= 2000000 AND revenue <= 50000000", 15, days=400)
NP_FISCAL = sig("Nonprofit fiscal year ahead", "irs_bmf",
                "days_to_fiscal_year_start >= 60 AND days_to_fiscal_year_start <= 120", 20, days=1)
Q4 = sig("Q4 plan-year window", "calendar", "month in [10, 11, 12]", 10, days=1)
LAYOFFS = sig("Layoffs", "layoffs", "days_since_layoff <= 90", 0, "Suppress", days=90)

SPEC_SIGNALS = (
    MENTAL_HEALTH, EAP_NAMED, VENDOR, PROGRESSIVE, VALUES, LEADER, NEW_LEADER, FIRST_HIRE, FUNDING, GROWTH,
    VISITED, PRICING, NP_BUDGET, NP_FISCAL, Q4, LAYOFFS,
)

SPEC_ANGLES = (
    Angle("Upgrade the EAP", 1, "Your team already has an EAP ...",
          "I saw your team already has an employee assistance program.", True),
    Angle("Progressive employer", 2, "You already invest in your people ...",
          "It's clear you already invest in your people.", True),
    Angle("Growing team", 3, "Hiring fast means onboarding stress ...", "It looks like your team is growing fast.", True),
    Angle("General", 4, "Mental health support your team will use ...",
          "I wanted to share a simple way to give your team mental health support.", True),
    Angle("Switch from a competitor", 5, "About a third of the price ...",
          "I saw your team already offers a mental health app.", False),
)

ACTIVE_STATES = ("NY", "MA", "NJ", "PA", "IL", "GA", "TX")
SPEC_STATES = tuple(State(s, s in ACTIVE_STATES) for s in (*ACTIVE_STATES, "FL", "NC", "VA", "CA", "WA"))


def make_settings(
    signals: tuple[Signal, ...] = SPEC_SIGNALS,
    *,
    angles: tuple[Angle, ...] = SPEC_ANGLES,
    states: tuple[State, ...] = SPEC_STATES,
    overrides: tuple[Override, ...] = (),
    **general,
) -> Settings:
    return Settings(general=General(**general), signals=signals, angles=angles, states=states, overrides=overrides)


def account(**kw) -> dict:
    row = {
        "account_id": "a1",
        "domain": "acme.com",
        "clean_name": "Acme Creative",
        "hq_city": "Chicago",
        "hq_state": "IL",
        "industry": "Advertising agencies",
        "industry_group": "Marketing & Creative Agencies",
        "naics": "541810",
        "employees": 64,
        "size_band": "50-99",
        "founded_year": 2014,
        "status": "queued",
        "first_seen": NOW - timedelta(days=3),
    }
    row.update(kw)
    return row


_ids = iter(range(1, 1_000_000))


def fact(source: str, name: str, value, days_ago: int = 0, *, quote: str = "", url: str = "", account_id: str = "a1") -> dict:
    return {
        "event_id": f"e{next(_ids)}",
        "account_id": account_id,
        "source": source,
        "fact": name,
        "value": value,
        "quote": quote,
        "source_url": url,
        "observed_at": NOW - timedelta(days=days_ago),
    }


def benefit(item: str, days_ago: int = 0, quote: str = "", source: str = "clay_careers", **kw) -> dict:
    return fact(source, "benefit", {"item": item}, days_ago, quote=quote or f"We offer {item}.",
                url="https://acme.com/careers", **kw)


# -- weights and caps ------------------------------------------------------------------


def test_progressive_benefits_counts_each_distinct_term_up_to_max_weight():
    two = [benefit("wellness stipend"), benefit("parental leave")]
    three = [*two, benefit("sabbatical")]
    four = [*three, benefit("unlimited PTO")]
    assert match_signal(PROGRESSIVE, two, TODAY).weight_applied == 20
    assert match_signal(PROGRESSIVE, three, TODAY).weight_applied == 30
    assert match_signal(PROGRESSIVE, four, TODAY).weight_applied == 30
    assert len(match_signal(PROGRESSIVE, four, TODAY).evidence) == 4


def test_a_term_found_twice_counts_once():
    facts = [benefit("sabbatical"), benefit("sabbatical", quote="Paid sabbatical after five years."),
             fact("job_posts", "posting_text", "Perks: a sabbatical program.", 2)]
    m = match_signal(PROGRESSIVE, facts, TODAY)
    assert m.weight_applied == 10
    assert [e.term for e in m.evidence] == ["sabbatical"]


def test_signal_without_max_weight_counts_its_weight_once():
    facts = [fact("clay_careers", "mental_health_provision", {"type": "eap", "provider": "ComPsych"},
                  quote="Our EAP through ComPsych offers counseling.")]
    m = match_signal(MENTAL_HEALTH, facts, TODAY)
    assert m.weight_applied == 25
    assert {e.term for e in m.evidence} >= {"EAP", "counseling"}
    assert match_signal(EAP_NAMED, facts, TODAY).weight_applied == 10


def test_total_is_capped_at_score_cap_and_not_floored():
    big = sig("Big", "apollo_org", "open_roles >= 1", 80)
    bigger = sig("Bigger", "apollo_org", "open_roles >= 1", 70)
    negative = sig("Negative", "apollo_org", "open_roles >= 1", -30)
    facts = [fact("apollo_org", "open_roles", 4)]
    capped = score_account(account(), facts, make_settings((big, bigger)), TODAY)
    assert capped.score == 100
    assert "capped at 100" in capped.tier_reason
    below = score_account(account(), facts, make_settings((negative,)), TODAY)
    assert below.score == -30
    assert below.tier == "Control"


def test_score_sums_only_score_signals_and_explains_itself():
    facts = [
        fact("clay_careers", "read_status", "read"),
        benefit("mental health days", quote="Mental health days and an EAP."),
        fact("apollo_people", "people_leader_days_in_title", 40, 5, quote="Jane Doe, Head of People since Sep 2026"),
    ]
    r = score_account(account(), facts, make_settings(), TODAY)
    # Mental health support (+25), EAP named (+10), New People leader (+30), Q4 (+10)
    assert r.score == 75
    assert r.tier == "Priority"
    assert r.tier_reason == (
        "Priority: Mental health support listed (+25), EAP named (+10), New People leader (+30), "
        "Q4 plan-year window (+10)"
    )


# -- freshness --------------------------------------------------------------------------


def test_fact_older_than_counts_for_days_is_ignored():
    assert match_signal(MENTAL_HEALTH, [benefit("therapy", days_ago=540)], TODAY) is not None
    assert match_signal(MENTAL_HEALTH, [benefit("therapy", days_ago=541)], TODAY) is None
    assert match_signal(NEW_LEADER, [fact("apollo_people", "people_leader_days_in_title", 30, 91)], TODAY) is None


def test_freshness_uses_the_uk_date():
    one_day = dataclasses.replace(LEADER, counts_for_days=1)
    e = fact("apollo_people", "people_leader_count", 2)
    e["observed_at"] = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)  # 00:30 on 1 July in London
    assert match_signal(one_day, [e], date(2026, 7, 2)) is not None
    e["observed_at"] = "2026-06-30T22:30:00+00:00"  # 23:30 on 30 June in London, as ISO text
    assert match_signal(one_day, [e], date(2026, 7, 2)) is None


def test_condition_uses_latest_fresh_value_per_field():
    facts = [
        fact("apollo_people", "people_leader_days_in_title", 200, 30),
        fact("apollo_people", "people_leader_days_in_title", 45, 1, quote="Head of People since Sep 2026"),
    ]
    m = match_signal(NEW_LEADER, facts, TODAY)
    assert m is not None and m.evidence[0].text == "Head of People since Sep 2026"
    facts.append(fact("apollo_people", "people_leader_days_in_title", 120, 0))
    assert match_signal(NEW_LEADER, facts, TODAY) is None


def test_day_count_facts_are_aged_to_today():
    """days_since_* and days_in_title are stored as of observed_at; scoring adds the days since."""
    assert match_signal(FUNDING, [fact("clay_funding", "days_since_funding", 500, 100)], TODAY) is None  # 600 today
    m = match_signal(FUNDING, [fact("clay_funding", "days_since_funding", 500, 10)], TODAY)
    assert m is not None and m.weight_applied == 20
    assert [e.text for e in m.evidence] == ["days_since_funding = 510"]
    assert match_signal(NEW_LEADER, [fact("apollo_people", "people_leader_days_in_title", 85, 10)], TODAY) is None
    # Other fields are not aged, and a hand-set override is used as it reads.
    assert match_signal(LEADER, [fact("apollo_people", "people_leader_count", 1, 300)], TODAY) is not None
    over = match_signal(FUNDING, [fact("clay_funding", "days_since_funding", 900, 10)], TODAY,
                        overrides={"days_since_funding": 30})
    assert over is not None and over.evidence[0].text == "days_since_funding = 30"


def test_condition_across_two_sources():
    facts = [fact("apollo_jobs", "open_people_roles", 1), fact("apollo_people", "people_leader_count", 0)]
    m = match_signal(FIRST_HIRE, facts, TODAY)
    assert m.weight_applied == 25
    assert [e.text for e in m.evidence] == ["open_people_roles = 1", "people_leader_count = 0"]


# -- text matching ---------------------------------------------------------------------------


def test_context_rule_calm_without_context_is_not_matched():
    plain = [fact("clay_careers", "culture_statement", "We keep a calm, focused office.", quote="A calm office.")]
    assert match_signal(VENDOR, plain, TODAY) is None
    app = [fact("clay_careers", "benefit", {"item": "Calm app subscription"}, quote="Free Calm app subscription.")]
    m = match_signal(VENDOR, app, TODAY)
    assert m is not None and m.evidence[0].term == "Calm"


def test_whole_words_only():
    assert match_signal(EAP_NAMED, [benefit("LEAP-year bonus")], TODAY) is None
    # "mental health day" does not match "mental health days"
    assert match_signal(PROGRESSIVE, [benefit("mental health days")], TODAY) is None


def test_job_posts_feed_progressive_benefits():
    m = match_signal(PROGRESSIVE, [fact("job_posts", "posting_text", "Perks: unlimited PTO and a sabbatical.")], TODAY)
    assert m.weight_applied == 20
    # EAP named reads only clay_careers
    assert match_signal(EAP_NAMED, [fact("job_posts", "posting_text", "We offer an EAP.")], TODAY) is None


# -- not read (SPEC 8) ----------------------------------------------------------------------


def test_blocked_read_adds_nothing_and_does_not_clear_an_earlier_hold():
    earlier = [
        fact("clay_careers", "read_status", "read", 30),
        benefit("Lyra Health app", 30, quote="Therapy through the Lyra app."),
    ]
    blocked = [*earlier, fact("clay_careers", "read_status", "blocked", 0)]
    before = score_account(account(), earlier, make_settings(), TODAY)
    assert before.tier == "Held"
    assert any(m.signal is MENTAL_HEALTH for m in before.matches)

    after = score_account(account(), blocked, make_settings(), TODAY)
    assert after.tier == "Held"
    assert after.tier_reason.startswith("Held: Modern mental-health vendor named (Lyra)")
    assert not any(m.signal is MENTAL_HEALTH for m in after.matches)
    assert after.score == 10  # only the Q4 calendar signal


def test_blocked_read_leaves_other_sources_of_a_signal_in_play():
    facts = [
        benefit("sabbatical", 5),
        fact("job_posts", "posting_text", "Perks include parental leave.", 2),
        fact("clay_careers", "read_status", "error", 0),
    ]
    m = match_signal(PROGRESSIVE, facts, TODAY)
    assert m.weight_applied == 10
    assert [e.source for e in m.evidence] == ["job_posts"]


def test_no_pages_found_is_a_read():
    facts = [benefit("therapy", 3), fact("clay_careers", "read_status", "no_pages_found", 0)]
    assert match_signal(MENTAL_HEALTH, facts, TODAY) is not None


# -- calendar ----------------------------------------------------------------------------------


def test_q4_calendar_signal_on_27_october():
    m = match_signal(Q4, [], TODAY)
    assert m.weight_applied == 10
    assert m.evidence[0].text == "month = 10"
    assert match_signal(Q4, [], date(2026, 9, 29)) is None


def test_days_to_fiscal_year_start():
    assert scoring.days_to_fiscal_year_start(12, TODAY) == 66  # 1 Jan 2027
    assert scoring.days_to_fiscal_year_start(6, TODAY) == 247  # 1 Jul 2027
    assert scoring.days_to_fiscal_year_start(9, date(2026, 10, 1)) == 0  # starts today
    assert scoring.days_to_fiscal_year_start(9, date(2026, 10, 2)) == 364


def test_nonprofit_fiscal_year_ahead_is_computed_daily_from_an_old_irs_fact():
    old = [fact("irs_bmf", "fiscal_year_end_month", "12", 200)]
    m = match_signal(NP_FISCAL, old, TODAY)
    assert m is not None and m.evidence[0].text == "days_to_fiscal_year_start = 66"
    assert match_signal(NP_FISCAL, [fact("irs_bmf", "fiscal_year_end_month", 6, 200)], TODAY) is None
    # a stale stored value does not count; the computed one wins
    stale = [fact("irs_bmf", "days_to_fiscal_year_start", 90, 30), fact("irs_bmf", "fiscal_year_end_month", 6, 30)]
    assert match_signal(NP_FISCAL, stale, TODAY) is None


# -- suppress, overrides ------------------------------------------------------------------------


def test_suppress_signal_sets_suppress_days():
    r = score_account(account(), [fact("layoffs", "days_since_layoff", 20, 3)], make_settings(), TODAY)
    assert r.suppress_days == 90 and r.suppress_signal == "Layoffs"
    assert r.tier == "Control"  # suppression is not a tier rule
    none = score_account(account(), [fact("layoffs", "days_since_layoff", 200, 3)], make_settings(), TODAY)
    assert none.suppress_days is None


def test_override_wins_over_every_source():
    settings = make_settings(overrides=(Override("acme.com", "people_leader_count", "2"),))
    facts = [fact("apollo_people", "people_leader_count", 0)]
    m = [x for x in score_account(account(), facts, settings, TODAY).matches if x.signal is LEADER]
    assert m and m[0].evidence[0].source == "override"
    moved = make_settings(overrides=(Override("acme.com", "hq_state", "CA"),))
    assert score_account(account(), [], moved, TODAY).tier == "Excluded"


def test_inactive_signals_are_ignored():
    off = dataclasses.replace(Q4, active=False)
    assert score_account(account(), [], make_settings((off,)), TODAY).score == 0


# -- the score job -------------------------------------------------------------------------------


def _slack_routes(transport: FakeTransport) -> FakeTransport:
    transport.route("GET", "conversations.list", {
        "ok": True,
        "channels": [{"name": "us-outbound", "id": "C_ALERT"}, {"name": "us-outbound-dev", "id": "C_DEV"}],
    })
    transport.route("POST", "chat.postMessage", {"ok": True, "channel": "C_DEV", "ts": "1.0"})
    return transport


def _seed(store: MemoryStore) -> None:
    store.tables["accounts"] = [
        account(account_id="a1", domain="acme.com", tier="Control", score=0, angle="General"),
        account(account_id="a2", domain="peo.com", clean_name="Peo Co", naics="561330"),
        account(account_id="a3", domain="laid.com", clean_name="Laid Off Inc"),
        account(account_id="a4", domain="enrolled.com", status="enrolled", angle="General", tier="Control",
                sender="Hannah Spalding"),
        account(account_id="a5", domain="done.com", status="disqualified", tier="Control"),
    ]
    store.tables["signal_events"] = [
        fact("clay_careers", "read_status", "read", 2, account_id="a1"),
        benefit("mental health days", 2, quote="Mental health days and an EAP.", account_id="a1"),
        fact("layoffs", "days_since_layoff", 10, 1, account_id="a3"),
        fact("clay_careers", "mental_health_provision", {"type": "eap", "provider": "ComPsych"}, 2,
             quote="Our EAP is provided by ComPsych.", account_id="a4"),
        fact("apollo_people", "people_leader_days_in_title", 20, 2, account_id="a4"),
    ]


def test_rescore_end_to_end_and_a_weight_change_moves_the_score():
    settings = make_settings()
    ctx = make_context(settings, now=NOW)
    slack = _with_slack(ctx)
    store = ctx.store
    _seed(store)

    summary = rescore(ctx)
    assert summary["accounts"] == 4  # the disqualified account is not scored
    acc = {a["account_id"]: a for a in store.tables["accounts"]}
    a1 = acc["a1"]
    assert (a1["score"], a1["tier"], a1["angle"]) == (45, "Standard", "Upgrade the EAP")
    assert a1["tier_reason"] == "Standard: Mental health support listed (+25), EAP named (+10), Q4 plan-year window (+10)"
    assert a1["last_scored"] == NOW
    assert acc["a2"]["tier"] == "Excluded" and "PEO" in acc["a2"]["tier_reason"]
    # enrolled: the tier is recorded, the angle and sender never change
    assert acc["a4"]["tier"] == "Priority"
    assert acc["a4"]["angle"] == "General" and acc["a4"]["sender"] == "Hannah Spalding"
    assert acc["a5"]["tier"] == "Control" and "last_scored" not in acc["a5"]

    rows = [e for e in store.tables["signal_events"] if e["source"] == "scoring"]
    a1_rows = {r["value"]["signal"]: r for r in rows if r["account_id"] == "a1"}
    assert set(a1_rows) == {"Mental health support listed", "EAP named", "Q4 plan-year window"}
    assert a1_rows["EAP named"]["fact"] == "signal_matched"
    assert a1_rows["EAP named"]["value"] == {"signal": "EAP named", "weight": 10, "evidence": ["EAP"]}
    assert a1_rows["EAP named"]["quote"] == "Mental health days and an EAP."

    [sup] = store.tables["suppression"]
    assert sup["domain"] == "laid.com" and sup["email_sha256"] is None
    assert sup["reason"] == "signal:Layoffs" and sup["source"] == "scoring"
    assert sup["expires_at"] == NOW + timedelta(days=90)
    [partner] = store.tables["partners"]
    assert partner["domain"] == "peo.com" and partner["reason"] == "peo" and partner["naics"] == "561330"
    assert summary["tiers"]["Standard"] == 1 and summary["tiers"]["Excluded"] == 1
    assert summary["suppressed"] == 1 and summary["partners"] == 1
    assert summary["tier_share"] is None and not slack.posts  # fewer than 20 accounts: no check

    # Harry raises the EAP named weight in the sheet; the next rescore picks it up.
    heavier = tuple(dataclasses.replace(s, weight=20) if s.signal == "EAP named" else s for s in settings.signals)
    ctx2 = make_context(make_settings(heavier), now=NOW + timedelta(days=1), store=store)
    rescore(ctx2)
    a1 = next(a for a in store.tables["accounts"] if a["account_id"] == "a1")
    assert a1["score"] == 55 and a1["tier"] == "Priority"
    rows = [e for e in store.tables["signal_events"] if e["source"] == "scoring" and e["account_id"] == "a1"]
    assert len(rows) == 3  # replaced, not added to
    assert next(r for r in rows if r["value"]["signal"] == "EAP named")["value"]["weight"] == 20
    # the Layoffs fact is still fresh: its suppression is extended from today, keeping when it was added
    [sup] = store.tables["suppression"]
    assert (sup["added_at"], sup["expires_at"]) == (NOW, NOW + timedelta(days=91))
    assert len(store.tables["partners"]) == 1


def test_rescore_writes_only_changed_fields():
    ctx = make_context(make_settings((Q4,)), now=NOW)
    ctx.store.tables["accounts"] = [account(score=10, tier="Control", tier_reason="Control: Q4 plan-year window (+10)",
                                            angle="General")]
    upserts = []
    real = ctx.store.upsert
    ctx.store.upsert = lambda table, rows: (upserts.append((table, [dict(r) for r in rows])), real(table, rows))[1]
    assert rescore(ctx)["changed"] == 0
    assert upserts == [("accounts", [{"account_id": "a1", "last_scored": NOW}])]


def test_rescore_does_not_shorten_an_existing_suppression():
    ctx = make_context(make_settings(), now=NOW)
    ctx.store.tables["accounts"] = [account(domain="laid.com")]
    ctx.store.tables["signal_events"] = [fact("layoffs", "days_since_layoff", 10, 1)]
    ctx.store.tables["suppression"] = [{"email_sha256": None, "domain": "laid.com", "reason": "kill_rule",
                                        "source": "kill_rules", "added_at": NOW, "expires_at": None}]
    assert rescore(ctx)["suppressed"] == 0
    assert ctx.store.tables["suppression"][0]["expires_at"] is None


class RecordingSlack:
    """Stands in for ctx.clients.slack: records post(channel, text) per the client contract."""

    connected = True  # clients/slack.SlackLike

    def __init__(self):
        self.posts: list[tuple[str, str]] = []

    def post(self, channel, text, blocks=None, thread_ts=None):
        self.posts.append((channel, text))
        return {"channel": channel, "ts": "1.0"}


def _with_slack(ctx) -> RecordingSlack:
    slack = RecordingSlack()
    ctx.clients.__dict__["slack"] = slack  # Clients.slack is a cached_property
    return slack


def test_tier_share_alert_posts_once_to_the_alert_channel():
    ctx = make_context(make_settings((Q4,)), now=NOW)  # every account scores 10: Control
    slack = _with_slack(ctx)
    ctx.store.tables["accounts"] = [account(account_id=f"c{i}", domain=f"c{i}.com") for i in range(20)]
    summary = rescore(ctx)
    assert summary["tier_share_alert"] is True
    assert summary["tier_share"] == {"Priority": 0.0, "Standard": 0.0, "Control": 1.0}
    [(channel, text)] = slack.posts
    assert channel == "#us-outbound"
    assert text == (
        "Tier mix check: this month's queue (October 2026, 20 accounts) is Priority 0%, Standard 0%, Control 100%. "
        "Each tier should be 5–40% of the queue; Priority is under 5%, Standard is under 5%, Control is over 40%. "
        "Review the thresholds and signal weights in the settings sheet."
    )


def test_tier_share_check_skips_small_queues_and_passes_a_good_mix():
    ctx = make_context(make_settings((Q4,)), now=NOW)
    slack = _with_slack(ctx)
    ctx.store.tables["accounts"] = [account(account_id=f"c{i}", domain=f"c{i}.com") for i in range(19)]
    assert rescore(ctx)["tier_share"] is None
    assert not slack.posts

    points = sig("Points", "apollo_org", "open_roles >= 1", 1)
    big = sig("Big", "apollo_org", "open_roles >= 50", 40)
    ctx = make_context(make_settings((points, big), priority_threshold=41, standard_threshold=1), now=NOW)
    slack = _with_slack(ctx)
    rows, events = [], []
    for i in range(30):
        rows.append(account(account_id=f"m{i}", domain=f"m{i}.com"))
        roles = 60 if i < 10 else (5 if i < 20 else 0)
        events.append(fact("apollo_org", "open_roles", roles, account_id=f"m{i}"))
    # outside this month's queue: first seen last month, or not queued/verified
    rows.append(account(account_id="old", domain="old.com", first_seen=NOW - timedelta(days=40)))
    rows.append(account(account_id="new", domain="new.com", status="new"))
    ctx.store.tables["accounts"], ctx.store.tables["signal_events"] = rows, events
    summary = rescore(ctx)
    assert summary["tier_share"] == {"Priority": 0.333, "Standard": 0.333, "Control": 0.333}
    assert summary["tier_share_alert"] is False
    assert not slack.posts


def test_tier_share_alert_in_dry_run_goes_to_the_dev_channel():
    pytest.importorskip("us_outbound.clients.slack")
    transport = _slack_routes(FakeTransport())
    ctx = make_context(make_settings((Q4,)), now=NOW, transport=transport)
    ctx.store.tables["accounts"] = [account(account_id=f"c{i}", domain=f"c{i}.com") for i in range(20)]
    rescore(ctx)
    [post] = [r for r in transport.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_DEV"
    assert post.json["text"].startswith("[dry-run → #us-outbound] Tier mix check:")


def test_rescore_writes_only_to_the_us_outbound_schema():
    ctx = make_context(make_settings(), now=NOW)
    _seed(ctx.store)
    rescore(ctx)
    assert {c.system for c in ctx.guard.calls} == {"db"}
    assert all(c.target.startswith("us_outbound.") for c in ctx.guard.calls if c.system == "db")


@pytest.mark.parametrize("status", ["new", "queued", "verified", "enrolled", "engaged"])
def test_rescore_covers_every_open_status(status):
    ctx = make_context(make_settings((Q4,)), now=NOW)
    ctx.store.tables["accounts"] = [account(status=status)]
    assert rescore(ctx)["accounts"] == 1


def test_size_signals_read_the_account_s_size_band_when_apollo_sent_no_employee_count():
    """2 Oct 2026: Apollo's search rows carry no employee count, only the band searched, so the size
    signals read accounts.size_band when no employees fact exists. A fact still wins."""
    from datetime import date

    from us_outbound.scoring.score import score_account
    from us_outbound.settings.defaults import default_tabs
    from us_outbound.settings.validate import validate_all

    s, _ = validate_all(default_tabs())
    base = {"account_id": "a1", "domain": "acme.com", "hq_state": "NY", "industry": "Fintech",
            "industry_group": "Technology & Startups", "employees": None}
    today = date(2026, 10, 2)

    def signals(account, events=()):
        return {m.signal.signal for m in score_account(account, list(events), s, today).matches}

    assert "Team of 10–49" in signals({**base, "size_band": "20-49"})
    assert "Team of 50–99" in signals({**base, "size_band": "50-99"})
    assert not {"Team of 10–49", "Team of 50–99"} & signals({**base, "size_band": "100-249"})
    assert not {"Team of 10–49", "Team of 50–99"} & signals({**base, "size_band": None})
    fact = {"account_id": "a1", "source": "apollo_org", "fact": "employees", "value": 120, "observed_at": today}
    assert "Team of 10–49" not in signals({**base, "size_band": "20-49"}, [fact])  # the fact wins
