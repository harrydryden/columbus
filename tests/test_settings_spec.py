"""settings/spec.py (9 Oct 2026): each tab's columns declared once; the column lists, optional columns, keys and the
dependencies between tabs are derived from it."""

from __future__ import annotations

import copy

from us_outbound.settings import spec
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.model import TABS
from us_outbound.settings.sync import DEPENDS_ON
from us_outbound.settings.validate import KEY_COLUMNS, TAB_OPTIONAL_COLUMNS, validate_all


def test_every_derived_list_comes_from_the_spec():
    for tab in TABS:
        assert COLUMNS[tab] == spec.columns(tab) and KEY_COLUMNS[tab] == spec.keys(tab)
        assert TAB_OPTIONAL_COLUMNS[tab] == spec.optional(tab)
    assert DEPENDS_ON["Copy"] == ("Industries", "Roles") and DEPENDS_ON["Tests"] == ("Copy",)
    assert DEPENDS_ON["General"] == DEPENDS_ON["Overrides"] == ("Industries",)


def test_an_optional_column_added_later_says_when():
    """Rows stored under older code are validated again by newer code, so a later column stays optional for good."""
    for tab, cols in spec.SPEC.items():
        for c in cols:
            assert not (c.optional and c.name != "note" and not c.since), f"{tab}.{c.name} needs since="
            assert not (c.key and c.optional), f"{tab}.{c.name}: a key is never optional"


def test_test_ids_are_unique_whatever_their_case():
    """settings load keys a Tests row case-blind, so two ids differing only in case kept one of the two."""
    tabs = copy.deepcopy(default_tabs())
    tabs["Tests"].append(dict(tabs["Tests"][0], test_id=tabs["Tests"][0]["test_id"].upper()))
    _, errors = validate_all(tabs)
    assert [(e.column, e.message.split(" is ")[1]) for e in errors["Tests"]] == [("test_id", "already on row 2")]
