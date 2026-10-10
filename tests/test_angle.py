"""Angle and opener (SPEC 9 "Scoring" steps 4 and 5; SPEC 5 Angles)."""

from __future__ import annotations

import dataclasses

from tests.test_scoring import (
    EAP_NAMED,
    FUNDING,
    MENTAL_HEALTH,
    PROGRESSIVE,
    SPEC_ANGLES,
    TODAY,
    VENDOR,
    account,
    benefit,
    fact,
    make_settings,
    sig,
)
from us_outbound.scoring.angle import (
    LEGAL_OVERLAY,
    AngleChoice,
    choose_angle,
    evidence_display,
    fill_opener,
    legal_overlay,
)
from us_outbound.scoring.score import Evidence, match_signal, score_account

SETTINGS = make_settings()
EAP_FACTS = [benefit("EAP", quote="An EAP through ComPsych."), benefit("therapy")]
EAP_DEFAULT = "I saw your team already has an employee assistance program."
GENERAL_DEFAULT = "I wanted to share a simple way to give your team mental health support."


def matches(facts, settings=SETTINGS):
    return [m for s in settings.active_signals() if (m := match_signal(s, facts, TODAY))]


def test_control_always_gets_general():
    ms = matches(EAP_FACTS)
    assert choose_angle("Control", ms, SETTINGS, "") == AngleChoice("General", GENERAL_DEFAULT, "")
    r = score_account(account(), [benefit("EAP")], make_settings((EAP_NAMED,)), TODAY)
    assert (r.tier, r.angle, r.opener) == ("Control", "General", GENERAL_DEFAULT)


def test_eap_named_wins_over_progressive_employer_by_angle_order():
    ms = matches(EAP_FACTS)
    suggested = {m.signal.suggests_angle for m in ms}
    assert {"Upgrade the EAP", "Progressive employer"} <= suggested
    assert choose_angle("Priority", ms, SETTINGS).angle == "Upgrade the EAP"
    # Reorder the Angles tab and Progressive employer wins
    swapped = tuple(
        dataclasses.replace(a, order={"Upgrade the EAP": 2, "Progressive employer": 1}.get(a.angle, a.order))
        for a in SPEC_ANGLES
    )
    assert choose_angle("Priority", ms, make_settings(angles=swapped)).angle == "Progressive employer"


def test_no_opener_on_the_signal_uses_the_angles_default_opener():
    assert choose_angle("Standard", matches(EAP_FACTS), SETTINGS).opener == EAP_DEFAULT


def test_opener_is_filled_with_the_evidence():
    eap = dataclasses.replace(EAP_NAMED, opener="Saw your benefits page mentions {evidence}.")
    progressive = dataclasses.replace(PROGRESSIVE, opener="Saw your careers page offers {evidence}.")
    settings = make_settings((eap, progressive))
    [m] = matches([benefit("ComPsych", quote="Counseling through compsych.")], settings)
    assert choose_angle("Standard", [m], settings).opener == "Saw your benefits page mentions ComPsych."
    ms = matches([benefit("Wellness Stipend", quote="A Wellness Stipend every month.")], settings)
    assert choose_angle("Standard", ms, settings).opener == "Saw your careers page offers wellness stipend."
    ms = matches([benefit("Unlimited PTO")], settings)
    assert choose_angle("Standard", ms, settings).opener == "Saw your careers page offers unlimited PTO."


def test_the_strongest_suggesting_signal_sets_the_opener():
    mh = dataclasses.replace(MENTAL_HEALTH, opener="Saw your team offers {evidence} support.")
    progressive = dataclasses.replace(PROGRESSIVE, opener="Saw your careers page offers {evidence}.")
    settings = make_settings((mh, progressive))
    ms = matches([benefit("therapy"), benefit("sabbatical"), benefit("parental leave", 1),
                  benefit("wellness stipend", 2)], settings)
    # Progressive benefits (+30) outweighs Mental health support listed (+25); both suggest Progressive employer.
    # Its first evidence is the newest fact that matched (facts are read newest first; a tie by event_id, not by
    # the order the rows came in).
    assert choose_angle("Priority", ms, settings) == AngleChoice(
        "Progressive employer", "Saw your careers page offers sabbatical.", ""
    )


def test_inactive_angle_is_skipped():
    ms = matches([benefit("Lyra")])  # the vendor Hold suggests Switch from a competitor, inactive in v1
    assert {m.signal.signal: m.signal.suggests_angle for m in ms} == {
        VENDOR.signal: "Switch from a competitor", "Q4 plan-year window": ""
    }
    assert choose_angle("Held", ms, SETTINGS).angle == "General"


def test_no_suggestion_falls_back_to_general():
    ms = matches([fact("apollo_people", "people_leader_count", 2)])
    assert ms and not any(m.signal.suggests_angle for m in ms)
    assert choose_angle("Standard", ms, SETTINGS) == AngleChoice("General", GENERAL_DEFAULT, "")


def test_growing_team_from_funding_with_a_quote():
    funding = dataclasses.replace(FUNDING, opener="Congratulations on {evidence}.")
    settings = make_settings((funding,))
    ms = matches([fact("apollo_org", "days_since_funding", 100, quote="your Series A")], settings)
    assert choose_angle("Standard", ms, settings) == AngleChoice("Growing team", "Congratulations on your Series A.", "")


def test_a_condition_with_no_quote_never_fills_an_opener():
    """"days_since_funding = 100" must not reach the email: the angle's default opener is used."""
    funding = dataclasses.replace(FUNDING, opener="Congratulations on {evidence}.")
    settings = make_settings((funding,))
    ms = matches([fact("apollo_org", "days_since_funding", 100)], settings)
    assert choose_angle("Standard", ms, settings) == AngleChoice(
        "Growing team", "It looks like your team is growing fast.", ""
    )
    r = score_account(account(), [fact("apollo_org", "days_since_funding", 45)], settings, TODAY)
    assert "days_since_funding" not in r.opener


def test_an_opener_with_no_placeholder_is_used_as_written():
    funding = dataclasses.replace(FUNDING, opener="Congratulations on the recent raise. ")
    settings = make_settings((funding,))
    ms = matches([fact("apollo_org", "days_since_funding", 100)], settings)
    assert choose_angle("Standard", ms, settings).opener == "Congratulations on the recent raise."


def test_legal_teams_overlay():
    assert legal_overlay("Legal Teams") == LEGAL_OVERLAY
    assert LEGAL_OVERLAY == "The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?"
    assert legal_overlay("Marketing & Creative Agencies") == ""
    assert legal_overlay(None) == ""
    assert choose_angle("Control", [], SETTINGS, "Legal Teams").legal_overlay == LEGAL_OVERLAY
    r = score_account(account(industry_group="Legal Teams", industry="Legal Teams", naics="541110"), [], SETTINGS,
                      TODAY)
    assert r.legal_overlay == LEGAL_OVERLAY


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


def test_signal_without_evidence_text_uses_default():
    opener_only = sig("Opener", "apollo_org", "open_roles >= 1", 10, angle="Growing team", opener="Saw {evidence}.")
    settings = make_settings((opener_only,))
    [m] = matches([fact("apollo_org", "open_roles", 4)], settings)
    assert choose_angle("Standard", [dataclasses.replace(m, evidence=[])], settings).opener == (
        "It looks like your team is growing fast."
    )
