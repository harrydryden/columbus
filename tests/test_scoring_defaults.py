"""The sheet's default Signals, scored end to end: the design review's Appendix A (docs/gtm-review/README.md,
1 Oct 2026), re-run as its probe ran it (docs/gtm-review/probe_synth.py), against the real defaults.

test_scoring.py tests the scoring mechanism with its own signals; this file checks that the defaults Harry
starts from score accounts the way Appendix A meant, so a sheet edit that undoes one shows here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from us_outbound.scoring.score import score_account
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

TODAY = date(2026, 10, 15)  # inside Q4: the Q4 plan-year window would have scored here
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def settings():
    s, errors = validate_all(default_tabs())
    assert s is not None, errors
    return s


def account(**kw) -> dict:
    row = {"account_id": "a1", "domain": "acme.com", "hq_state": "NY", "industry": "Fintech",
           "industry_group": "Technology & Startups", "size_band": "100-249", "status": "queued"}
    row.update(kw)
    return row


def fact(source, name, value, *, quote="", days_ago=10):
    return {"event_id": f"{source}-{name}-{days_ago}", "account_id": "a1", "source": source, "fact": name,
            "value": value, "quote": quote, "source_url": "", "observed_at": NOW - timedelta(days=days_ago)}


def scored(settings, events, **acct):
    r = score_account(account(**acct), events, settings, TODAY)
    return r, {m.signal.signal: m.weight_applied for m in r.matches}


def test_nothing_known_scores_nothing_in_october(settings):
    r, matched = scored(settings, [])
    assert (r.score, r.tier, matched) == (0, "Control", {})  # Q4 no longer adds 10 to everyone


def test_a_carrier_eap_alone_is_a_modest_plus_not_standard(settings):
    """Appendix A: it scored 25 + 10 (+10 in Q4), so 45 and Standard; a values page made it Priority."""
    eap = fact("clay_careers", "mental_health_provision", {"type": "carrier_eap", "provider": "ComPsych"},
               quote="Employee assistance program through ComPsych")
    r, matched = scored(settings, [eap, fact("clay_careers", "values_page", True)])
    assert matched == {"EAP named": 5} and (r.score, r.tier) == (5, "Control")


def test_a_medical_carrier_is_not_read_as_an_eap(settings):
    medical = fact("clay_careers", "benefit", {"item": "Medical, dental and vision through Cigna"},
                   quote="Medical, dental and vision through Cigna.")
    _, matched = scored(settings, [medical])
    assert "EAP named" not in matched
    eap = fact("clay_careers", "benefit", {"item": "Cigna Life Assistance Program"},
               quote="Free, confidential support through the Cigna Life Assistance Program.")
    r, matched = scored(settings, [eap])
    [m] = [m for m in r.matches if m.signal.signal == "EAP named"]
    assert matched == {"EAP named": 5} and [e.term for e in m.evidence] == ["Cigna"]


def test_a_us_site_visit_with_a_pricing_view_is_priority(settings):
    visit = fact("site_visits", "us_visits_30d", 2, days_ago=1)
    pricing = fact("site_visits", "pricing_or_demo_visits_30d", 1, days_ago=1)
    r, _ = scored(settings, [visit])
    assert (r.score, r.tier) == (35, "Standard")
    r, _ = scored(settings, [visit, pricing])
    assert (r.score, r.tier, r.angle) == (60, "Priority", "General")  # the copy never mentions the visit


def test_hiring_counts_open_roles_from_apollo_jobs(settings):
    _, matched = scored(settings, [fact("apollo_jobs", "open_roles", 5)])
    assert matched == {"Hiring and growth": 15}  # it read apollo_org only, so this scored 0
    _, matched = scored(settings, [fact("apollo_jobs", "open_people_roles", 1), fact("apollo_jobs", "open_roles", 1)])
    assert matched == {"People role open": 15}  # no people_leader_count: First People hire (likely) waits, this fires


@pytest.mark.parametrize("days, want", [(30, {"Funding in the last 6 months": 20}), (300, {"Funding 6–12 months ago": 10}),
                                        (500, {})])
def test_funding_decays(settings, days, want):
    _, matched = scored(settings, [fact("apollo_org", "days_since_funding", days, quote="Series A", days_ago=0)])
    assert matched == want


def test_funding_read_long_ago_counts_its_true_age(settings):
    """days_since_funding is aged to today: a round 20 days old when read 200 days ago is 220 days old."""
    _, matched = scored(settings, [fact("apollo_org", "days_since_funding", 20, days_ago=200)])
    assert matched == {"Funding 6–12 months ago": 10}


@pytest.mark.parametrize("employees, want", [(30, 15), (75, 10), (150, 0)])
def test_the_size_signal_favors_10_to_99(settings, employees, want):
    r, _ = scored(settings, [fact("apollo_org", "employees", employees)])
    assert r.score == want


def test_complements_score_and_competitors_hold(settings):
    calm = fact("clay_careers", "benefit", {"item": "Calm app subscription"}, quote="A free Calm app subscription.")
    r, matched = scored(settings, [calm])
    assert matched == {"Wellbeing app or perk named": 5} and r.tier != "Held"
    lyra = fact("clay_careers", "benefit", {"item": "Lyra Health"}, quote="Therapy through Lyra Health.")
    r, _ = scored(settings, [lyra])
    assert r.tier == "Held" and r.tier_reason.startswith("Held: Modern mental-health vendor named (Lyra)")


@pytest.mark.parametrize("keywords", ["teletherapy", "online therapy; telehealth", "virtual therapy",
                                      "mental health platform"])
def test_teletherapy_companies_are_partners(settings, keywords):
    r = score_account(account(industry="Digital health", industry_group="Healthcare", keywords=keywords), [],
                      settings, TODAY)
    assert r.tier == "Excluded" and r.partner == "behavioral_health"
    assert "behavioral-health provider, a partner" in r.tier_reason


def test_progressive_benefits_are_worth_less_and_read_plurals(settings):
    perks = [fact("clay_careers", "benefit", {"item": b}, quote=f"We offer {b}.", days_ago=i)
             for i, b in enumerate(("unlimited PTO", "mental health days", "sabbaticals", "wellness stipends"))]
    _, matched = scored(settings, perks)
    assert matched["Progressive benefits"] == 15  # +5 each, at most +15 (was +10 each, at most +30)


def test_the_lookalike_signal_never_lifts_a_signal_less_account_out_of_control(settings):
    """Size (+15 at 10-49) and lookalike (+4) together stay under standard_threshold (20): with no observed
    signal an account stays in Control, the signal-blind holdout."""
    events = [fact("apollo_org", "employees", 30), fact("lookalike", "lookalike_active", 80),
              fact("lookalike", "lookalike_strength", 110.0)]
    r, matched = scored(settings, events, size_band="20-49")
    assert matched == {"Team of 10–49": 15, "Looks like Spill's customers": 4}
    assert r.score == 19 < settings.general.standard_threshold and r.tier == "Control"
    r, _ = scored(settings, [*events, fact("clay_careers", "benefit", {"item": "EAP"}, quote="An EAP.")])
    assert r.tier == "Standard"  # any observed signal on top makes it Standard


def test_customers_are_excluded(settings):
    for name, reason in (("hubspot_customer", "a customer in HubSpot"),
                         ("hubspot_former_customer", "a former customer in HubSpot")):
        r = score_account(account(), [fact("hubspot", name, True)], settings, TODAY)
        assert (r.tier, r.tier_reason) == ("Excluded", f"Excluded: {reason}")
