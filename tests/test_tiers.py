"""Tiers and hard exclusions (SPEC 9 "Scoring" step 2, "Hard exclusions")."""

from __future__ import annotations

from datetime import date

import pytest

from tests.test_scoring import (
    EAP_NAMED,
    PROGRESSIVE,
    SPEC_STATES,
    TODAY,
    VENDOR,
    account,
    benefit,
    fact,
    make_settings,
    sig,
)
from us_outbound.scoring import tiers
from us_outbound.scoring.score import score_account
from us_outbound.scoring.tiers import hard_exclusion, partner_category, tier
from us_outbound.settings.model import State

SETTINGS = make_settings()


def excluded(acct: dict | None = None, facts: dict | None = None, settings=SETTINGS, today: date = TODAY) -> str | None:
    return hard_exclusion(account(**(acct or {})), facts or {}, settings, today)


def test_a_clean_account_is_not_excluded():
    assert excluded() is None
    assert excluded(facts={"ca_wa_share": 0.10, "fl_share": 0.1, "us_headcount": 5, "hubspot_customer": False}) is None


# -- partners ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "naics,category",
    [
        ("561330", "peo"),
        ("541612", "hr_consultancy"),
        ("524210", "broker"),
        ("524113", "insurer"),
        ("621330", "behavioral_health"),
        ("621420", "behavioral_health"),
        ("622210", "behavioral_health"),
        ("623220", "behavioral_health"),
        (["541810", 561330], "peo"),
    ],
)
def test_partner_naics(naics, category):
    assert partner_category(account(naics=naics), {}) == category
    assert excluded({"naics": naics}).endswith("a partner (NAICS " + str(naics[-1] if isinstance(naics, list) else naics) + ")")


def test_peo_reason_reads_plainly():
    assert excluded({"naics": "561330"}) == "PEO, a partner (NAICS 561330)"


@pytest.mark.parametrize(
    "where,value,category",
    [
        ("industry", "HR software", "hr_tech"),
        ("industry", "Employee Benefits Broker", "broker"),
        ("industry", "HR consulting", "hr_consultancy"),
        ("apollo_keywords", ["saas", "payroll", "compliance"], "hr_tech"),
        ("apollo_keywords", ["professional employer organization"], "peo"),
        ("keywords", "behavioral health; telehealth", "behavioral_health"),
        ("apollo_industry", "mental health care", "behavioral_health"),
    ],
)
def test_partner_keywords(where, value, category):
    in_account = partner_category(account(naics=None, **{where: value}), {})
    in_facts = partner_category(account(naics=None, industry="Software"), {where: value})
    assert category in (in_account, in_facts)


def test_insurtech_and_mental_health_words_on_their_own_are_not_partners():
    assert partner_category(account(industry="Insurtech", naics="541511"), {}) is None
    assert partner_category(account(industry="Digital health", naics=None), {"apollo_keywords": ["mental health"]}) is None


# -- HubSpot, states, size, age -------------------------------------------------------------


@pytest.mark.parametrize("fact_name", [f for f, _ in tiers.HUBSPOT_EXCLUSIONS])
def test_any_hubspot_flag_excludes(fact_name):
    assert excluded(facts={fact_name: True}) == dict(tiers.HUBSPOT_EXCLUSIONS)[fact_name]
    assert excluded(facts={fact_name: "true"}) is not None
    assert excluded(facts={fact_name: False}) is None


def test_hubspot_customer_via_score_account():
    r = score_account(account(), [fact("hubspot", "hubspot_customer", True, 400)], SETTINGS, TODAY)
    assert (r.tier, r.tier_reason) == ("Excluded", "Excluded: a customer in HubSpot")
    assert r.partner is None


def test_ca_or_wa_share_over_20_percent():
    assert excluded(facts={"ca_wa_share": 0.25}) == "25% of US staff are in CA or WA (over 20%)"
    assert excluded(facts={"ca_wa_share": 0.2}) is None


def test_fl_share_counts_only_while_fl_is_off():
    assert excluded(facts={"fl_share": 0.3}) == "30% of US staff are in CA, WA or FL, which is not active (over 20%)"
    fl_on = make_settings(states=(*SPEC_STATES[:7], State("FL", True)))
    assert excluded({"hq_state": "FL"}, {"fl_share": 0.3}, settings=fl_on) is None


def test_fl_joins_the_ca_wa_share_while_it_is_off():
    """SPEC 9: "more than 20% ... in CA or WA (or in FL, until it is switched on)"."""
    both = {"ca_wa_share": 0.15, "fl_share": 0.10}
    assert excluded(facts=both) == "25% of US staff are in CA, WA or FL, which is not active (over 20%)"
    assert excluded(facts={"ca_wa_share": 0.10, "fl_share": 0.10}) is None  # exactly 20%
    fl_on = make_settings(states=(*SPEC_STATES[:7], State("FL", True)))
    assert excluded({"hq_state": "FL"}, both, settings=fl_on) is None


def test_hq_state_rules():
    assert excluded({"hq_state": "CA"}) == "HQ in CA, which is never contacted"
    assert excluded({"hq_state": "wa"}) == "HQ in WA, which is never contacted"
    assert excluded({"hq_state": "NC"}) == "HQ state NC is not active"
    assert excluded({"hq_state": "FL"}) == "HQ state FL is not active"
    assert excluded({"hq_state": None}) == "HQ state unknown"
    # CA stays excluded even if someone switches it on in the sheet (SPEC 1.3)
    ca_on = make_settings(states=(State("CA", True), State("NY", True)))
    assert excluded({"hq_state": "CA"}, settings=ca_on) == "HQ in CA, which is never contacted"


def test_fewer_than_five_us_people():
    assert excluded(facts={"us_headcount": 3}) == "fewer than 5 US-located people found (3)"
    assert excluded({"us_employees": 4}) == "fewer than 5 US-located people found (4)"
    assert excluded({"us_employees": 4}, {"us_headcount": 12}) is None  # Apollo's people count wins
    assert excluded({"us_employees": None}) is None  # unknown is not "fewer than 5"


def test_founded_less_than_two_years_ago():
    assert excluded({"founded_year": 2025}) == "founded less than 2 years ago (2025)"
    assert excluded({"founded_year": 2026}) == "founded less than 2 years ago (2026)"
    assert excluded({"founded_year": 2024}) is None  # at least 2 years since 1 Jan 2024
    assert excluded({"founded_year": None}, {"founded_year": "2025"}) == "founded less than 2 years ago (2025)"
    assert excluded({"founded_year": 2025}, today=date(2027, 1, 1)) is None


# -- tier order ------------------------------------------------------------------------------


def test_thresholds():
    assert tier(50, [], None, SETTINGS)[0] == "Priority"
    assert tier(49, [], None, SETTINGS)[0] == "Standard"
    assert tier(20, [], None, SETTINGS)[0] == "Standard"
    assert tier(19, [], None, SETTINGS) == ("Control", "Control: no scoring signals found")


def test_hold_via_vendor_named_beats_a_high_score():
    facts = [
        benefit("Lyra", quote="Therapy with Lyra."),
        benefit("wellness stipend"),
        benefit("sabbatical"),
        benefit("parental leave"),
        benefit("EAP", quote="An EAP through ComPsych."),
        fact("apollo_people", "people_leader_days_in_title", 10),
    ]
    r = score_account(account(), facts, SETTINGS, TODAY)
    assert r.score >= 50
    assert r.tier == "Held"
    assert r.tier_reason == "Held: Modern mental-health vendor named (Lyra)"


def test_exclude_signal():
    rival = sig("Competitor", "clay_careers", "Acme Wellbeing Inc", 0, "Exclude")
    settings = make_settings((EAP_NAMED, VENDOR, rival))
    facts = [benefit("Acme Wellbeing Inc", quote="Acme Wellbeing Inc is our partner."), benefit("Lyra")]
    r = score_account(account(), facts, settings, TODAY)
    assert (r.tier, r.tier_reason) == ("Excluded", "Excluded: Competitor (Acme Wellbeing Inc)")


def test_hard_exclusion_comes_before_an_exclude_signal_and_a_hold():
    r = score_account(account(naics="561330"), [benefit("Lyra")], SETTINGS, TODAY)
    assert r.tier_reason == "Excluded: PEO, a partner (NAICS 561330)"
    assert r.partner == "peo"


def test_reason_lists_weights_in_sheet_order():
    settings = make_settings((EAP_NAMED, PROGRESSIVE))
    facts = [benefit("sabbatical"), benefit("parental leave"), benefit("EAP")]
    r = score_account(account(), facts, settings, TODAY)
    assert (r.score, r.tier) == (30, "Standard")
    assert r.tier_reason == "Standard: EAP named (+10), Progressive benefits (+20)"
