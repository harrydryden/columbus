"""Tiers and the hard exclusions the rescore applies (SPEC 9 "Scoring" step 2, "Hard exclusions"; icp.SCORE)."""

from __future__ import annotations

from datetime import date

import pytest

from tests.test_scoring import (
    EAP_NAMED,
    PROGRESSIVE,
    TODAY,
    VENDOR,
    account,
    benefit,
    fact,
    make_settings,
    sig,
)
from us_outbound import icp
from us_outbound.scoring import tiers
from us_outbound.scoring.score import score_account, tier
from us_outbound.scoring.tiers import hard_exclusion
from us_outbound.settings.model import State

SETTINGS = make_settings()


def excluded(acct: dict | None = None, facts: dict | None = None, settings=SETTINGS, today: date = TODAY) -> str | None:
    return hard_exclusion(account(**(acct or {})), facts or {}, settings, today)


def test_a_clean_account_is_not_excluded():
    assert excluded() is None
    assert excluded(facts={"hubspot_customer": False}) is None


def test_the_dead_exclusions_are_gone():
    """W2-2 (code-scoring B5): no source writes ca_wa_share, fl_share or us_headcount, so their rules went. Each of
    these excluded the account before."""
    assert excluded(facts={"ca_wa_share": 0.25}) is None
    assert excluded(facts={"ca_wa_share": 0.15, "fl_share": 0.10}) is None
    assert excluded(facts={"us_headcount": 3}) is None
    assert excluded({"us_employees": 4}) is None


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
    assert icp.partner_category(account(naics=naics), {}) == category
    assert excluded({"naics": naics}).endswith("a partner (NAICS " + str(naics[-1] if isinstance(naics, list) else naics) + ")")


def test_peo_reason_reads_plainly():
    assert excluded({"naics": "561330"}) == "PEO, a partner (NAICS 561330)"


@pytest.mark.parametrize(
    "where,value,category",
    [
        ("industry", "HR software", "hr_tech"),
        ("industry", "Employee Benefits Broker", "broker"),
        ("industry", "HR consulting", "hr_consultancy"),
        ("apollo_keywords", ["professional employer organization"], "peo"),
        ("keywords", "behavioral health; telehealth", "behavioral_health"),
        ("apollo_industry", "mental health care", "behavioral_health"),
    ],
)
def test_partner_keywords(where, value, category):
    in_account = icp.partner_category(account(naics=None, **{where: value}), {})
    in_facts = icp.partner_category(account(naics=None, industry="Software"), {where: value})
    assert category in (in_account, in_facts)


def test_insurtech_and_mental_health_words_on_their_own_are_not_partners():
    assert icp.partner_category(account(industry="Insurtech", naics="541511"), {}) is None
    assert icp.partner_category(account(industry="Digital health", naics=None), {"apollo_keywords": ["mental health"]}) is None


# -- HubSpot, states, age -----------------------------------------------------------------


@pytest.mark.parametrize("fact_name", [f for f, _ in icp.HUBSPOT_EXCLUSIONS])
def test_any_hubspot_flag_excludes(fact_name):
    assert excluded(facts={fact_name: True}) == dict(icp.HUBSPOT_EXCLUSIONS)[fact_name]
    assert excluded(facts={fact_name: "true"}) is not None
    assert excluded(facts={fact_name: False}) is None


def test_an_approver_s_decision_excludes():
    assert excluded(facts={icp.DECLINED_IN_SLACK: True}) == icp.DECLINED_TEXT


def test_hubspot_customer_via_score_account():
    r = score_account(account(), [fact("hubspot", "hubspot_customer", True, 400)], SETTINGS, TODAY)
    assert (r.tier, r.tier_reason) == ("Excluded", "Excluded: a customer in HubSpot")
    assert r.partner is None


def test_hq_state_rules():
    assert excluded({"hq_state": "CA"}) == "HQ in CA, which is never contacted"
    assert excluded({"hq_state": "wa"}) == "HQ in WA, which is never contacted"
    assert excluded({"hq_state": "NC"}) == "HQ state NC is not active"
    assert excluded({"hq_state": "FL"}) == "HQ state FL is not active"
    assert excluded({"hq_state": None}) == "HQ state unknown"
    # A state written out is read as its code (it was "HQ state NEW YORK is not active" before the one gate).
    assert excluded({"hq_state": "New York"}) is None
    # CA stays excluded even if someone switches it on in the sheet (SPEC 1.3)
    ca_on = make_settings(states=(State("CA", True), State("NY", True)))
    assert excluded({"hq_state": "CA"}, settings=ca_on) == "HQ in CA, which is never contacted"


def test_founded_less_than_two_years_ago():
    assert excluded({"founded_year": 2025}) == "founded less than 2 years ago (2025)"
    assert excluded({"founded_year": 2026}) == "founded less than 2 years ago (2026)"
    assert excluded({"founded_year": 2024}) is None  # at least 2 years since 1 Jan 2024
    assert excluded({"founded_year": None}, {"founded_year": "2025"}) == "founded less than 2 years ago (2025)"
    assert excluded({"founded_year": 2025}, today=date(2027, 1, 1)) is None


# -- tier order ------------------------------------------------------------------------------


def test_every_score_is_the_queue():
    """No thresholds (Harry's D7): 0, 19, 20, 49 and 50 points, Control, Standard and Priority before, are all Queue,
    taken by score (enrol/queue.order_key)."""
    for score in (0, 19, 20, 49, 50, 100):
        assert tier(score, [], None)[0] == tiers.QUEUE
    assert tier(0, [], None) == ("Queue", "Queue: no scoring signals found")


def test_the_retired_tiers_read_as_the_queue_until_a_rescore_writes_it():
    assert tiers.QUEUE_TIERS == ("Queue", "Priority", "Standard", "Control")
    assert tiers.OUT_OF_QUEUE_TIERS == {"Held", "Excluded"}


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
    assert (r.score, r.tier) == (30, "Queue")
    assert r.tier_reason == "Queue: EAP named (+10), Progressive benefits (+20)"
