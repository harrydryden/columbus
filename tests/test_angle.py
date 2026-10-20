"""Angle (SPEC 9 "Scoring" step 4; SPEC 5 Angles), and the evidence helpers the openers use.

The scoring-time opener and the Control tier's "always General" rule went with W2-2 (20 Oct 2026, Harry's D7: the
random arm uses its normal copy angle); the opener is chosen at enrol time (tests/test_openers.py)."""

from __future__ import annotations

import dataclasses

from tests.test_scoring import (
    EAP_NAMED,
    FUNDING,
    SPEC_ANGLES,
    TODAY,
    account,
    benefit,
    fact,
    make_settings,
)
from us_outbound.enrol.openers import LEGAL_OVERLAY, fill_opener, legal_overlay
from us_outbound.scoring.angle import choose_angle
from us_outbound.scoring.score import Evidence, evidence_display, match_signal, score_account

SETTINGS = make_settings()
EAP_FACTS = [benefit("EAP", quote="An EAP through ComPsych."), benefit("therapy")]


def matches(facts, settings=SETTINGS):
    return [m for s in settings.active_signals() if (m := match_signal(s, facts, TODAY))]


def test_eap_named_wins_over_progressive_employer_by_angle_order():
    ms = matches(EAP_FACTS)
    suggested = {m.signal.suggests_angle for m in ms}
    assert {"Upgrade the EAP", "Progressive employer"} <= suggested
    assert choose_angle(ms, SETTINGS) == "Upgrade the EAP"
    # Reorder the Angles tab and Progressive employer wins
    swapped = tuple(
        dataclasses.replace(a, order={"Upgrade the EAP": 2, "Progressive employer": 1}.get(a.angle, a.order))
        for a in SPEC_ANGLES
    )
    assert choose_angle(ms, make_settings(angles=swapped)) == "Progressive employer"


def test_a_low_score_gets_the_angle_its_signals_suggest():
    """EAP named alone is 10 points: under the old standard_threshold of 20, the Control tier, which always got
    General. With the tiers gone (Harry's D7: the random arm uses its normal copy angle), it gets Upgrade the EAP."""
    r = score_account(account(), [benefit("EAP")], make_settings((EAP_NAMED,)), TODAY)
    assert (r.score, r.tier, r.angle) == (10, "Queue", "Upgrade the EAP")


def test_inactive_angle_is_skipped():
    ms = matches([benefit("Lyra")])  # the vendor Hold suggests Switch from a competitor, inactive in v1
    assert {m.signal.signal for m in ms} >= {"Modern mental-health vendor named"}
    assert choose_angle(ms, SETTINGS) == "General"


def test_no_suggestion_falls_back_to_general():
    ms = matches([fact("apollo_people", "people_leader_count", 2)])
    assert ms and not any(m.signal.suggests_angle for m in ms)
    assert choose_angle(ms, SETTINGS) == "General"
    assert choose_angle([], SETTINGS) == "General"


def test_growing_team_from_funding():
    settings = make_settings((FUNDING,))
    ms = matches([fact("apollo_org", "days_since_funding", 100, quote="your Series A")], settings)
    assert choose_angle(ms, settings) == "Growing team"


def test_legal_teams_overlay():
    # Prospect-facing copy: the line is pinned as written (it moved from scoring/angle.py to enrol/openers.py).
    assert legal_overlay("Legal Teams") == LEGAL_OVERLAY
    assert LEGAL_OVERLAY == "The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?"
    assert legal_overlay("Marketing & Creative Agencies") == ""
    assert legal_overlay(None) == ""


def test_evidence_display_keeps_acronyms_and_brands():
    assert evidence_display(Evidence("Mental Health", term="mental health")) == "mental health"
    assert evidence_display(Evidence("eap", term="EAP")) == "EAP"
    assert evidence_display(Evidence("Headspace", term="Headspace")) == "Headspace"
    assert evidence_display(Evidence("BetterUp", term="")) == "BetterUp"  # not a term match: as it is
    assert evidence_display(Evidence("HRIS", term="hris")) == "HRIS"
    assert evidence_display(Evidence("100% Employer-Paid", term="100% employer-paid")) == "100% employer-paid"


def test_fill_opener_placeholders():
    ev = Evidence("EAP", quote="An EAP through ComPsych.", url="https://acme.com/benefits", term="EAP")
    assert fill_opener("Saw {evidence} on {url}: {quote}", ev) == (
        "Saw EAP on https://acme.com/benefits: An EAP through ComPsych."
    )
    assert fill_opener("Saw { evidence }.", ev) == "Saw EAP."
    assert fill_opener("Keep {{opener}} as is", ev) == "Keep {{opener}} as is"
