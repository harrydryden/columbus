"""The Industries tab read through Settings: one place for "the group's own label", the groups prospected, a label
from what a person typed, and the names the code holds (9 Oct 2026, the refactoring scan's phase 3)."""

from __future__ import annotations

from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import LEGAL_GROUP
from us_outbound.settings.validate import validate_all
from us_outbound.sources.lookalikes import HUBSPOT_INDUSTRY_LABELS

S, _ = validate_all(default_tabs())
MC = "Marketing & Creative Agencies"


def test_the_groups_own_label_and_its_labels():
    umbrella = S.umbrella(MC)
    assert umbrella is not None and umbrella.is_umbrella and umbrella.industry == MC
    assert not S.industry("PR agencies").is_umbrella
    labels = S.labels_in(MC)
    assert umbrella in labels and S.industry("PR agencies") in labels
    assert all(i.industry_group == MC for i in labels) and S.labels_in("No such group") == ()


def test_the_groups_prospected_are_those_with_a_label_switched_on():
    groups = S.active_groups()
    assert MC in groups and "Nonprofits" not in groups  # every Nonprofits label is off
    assert len(groups) == len(set(groups))


def test_a_label_from_what_a_person_typed():
    assert S.label("  fintech ").industry == "Fintech"
    assert S.label("games") is None  # exact only, unless asked
    assert S.label("games", fuzzy=True).industry == "Games studios"
    assert S.label("agencies", fuzzy=True) is None  # more than one label contains it
    assert S.label("") is None and S.label(None) is None


def test_the_names_the_code_holds_are_on_the_industries_tab():
    """Renaming one on the tab would silently drop what it drives (the legal overlay, a customer's lookalike)."""
    groups = {i.industry_group for i in S.industries}
    assert LEGAL_GROUP in groups
    names = {i.industry for i in S.industries} | groups
    assert {v for v in HUBSPOT_INDUSTRY_LABELS.values() if v} <= names
