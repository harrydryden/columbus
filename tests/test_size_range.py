"""The company size range from the General tab, and website visitors in any US state (Harry, 6 Oct 2026).

"We also need to include lower and upper bound company size in the settings sheet. It is currently set to 10-249
but I want to be able to update that to say 5-500 easily and have the system read that." And: "I never want to
exclude a website visitor on their state, as long as they're in the US."

General min_employees and max_employees (10 and 249 by default) drive every search's size bands, verify_accounts'
range check and the hand-check's size edges; the Roles tab's two orders cover sizes past them. A site visitor's HQ
may be in any US state; a contact located in CA or WA is still never emailed (SPEC 1.3)."""

from __future__ import annotations

import dataclasses

import pytest

from us_outbound import verify
from us_outbound.scoring import tiers
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import SIZE_BANDS, Role
from us_outbound.settings.validate import validate_all, validate_tab
from us_outbound.sources import apollo_universe as uni
from us_outbound.sources import lookalike_leads

BASE, _ = validate_all(default_tabs())


def sized(lo: int, hi: int):
    return dataclasses.replace(BASE, general=dataclasses.replace(BASE.general, min_employees=lo, max_employees=hi))


WIDE = sized(5, 500)


def general(**values):
    rows = [dict(r) for r in default_tabs()["General"]]
    for key, value in values.items():
        next(r for r in rows if r["key"] == key)["value"] = value
    return validate_tab("General", rows)


def account(**kw) -> dict:
    return {"account_id": "a1", "domain": "a1co.com", "hq_state": "NY", "employees": 64, "size_band": "50-99",
            "industry": "Fintech", "industry_group": "Technology & Startups", "status": "new", "source": "apollo", **kw}


# -- the General keys ------------------------------------------------------------------------------------


def test_the_defaults_are_spec_2_and_search_its_four_bands():
    assert (BASE.general.min_employees, BASE.general.max_employees) == (10, 249)
    assert BASE.size_bands() == ("10-19", "20-49", "50-99", "100-249")
    assert [BASE.employee_range(b) for b in BASE.size_bands()] == ["10,19", "20,49", "50,99", "100,249"]
    assert BASE.size_range_text() == "10 to 249"


def test_a_wider_range_adds_bands_each_clipped_to_it():
    assert WIDE.size_bands() == ("5-9", "10-19", "20-49", "50-99", "100-249", "250-499", "500-999")
    assert (WIDE.employee_range("5-9"), WIDE.employee_range("250-499"), WIDE.employee_range("500-999")) == (
        "5,9", "250,499", "500,500")
    assert sized(10, 300).employee_range("250-499") == "250,300"
    assert set(WIDE.size_bands()) <= set(SIZE_BANDS)


def test_the_sheet_takes_the_range_and_refuses_a_backwards_or_impossible_one():
    g, errors = general(min_employees="5", max_employees="500")
    assert errors == [] and (g.min_employees, g.max_employees) == (5, 500)
    _, errors = general(min_employees="300", max_employees="250")
    assert any("must not be above max_employees" in e.message for e in errors)
    _, errors = general(min_employees="0")
    assert any("headcount from 1" in e.message for e in errors)


# -- verify_accounts and the hand-check ---------------------------------------------------------------------


@pytest.mark.parametrize("change, wide, default", [
    ({"employees": 7, "size_band": "5-9"}, None, "outside 10 to 249 employees"),
    ({"employees": 300, "size_band": "250-499"}, None, "outside 10 to 249 employees"),
    ({"employees": 600, "size_band": "500-999"}, "outside 5 to 500 employees", "outside 10 to 249 employees"),
    ({"employees": None, "size_band": "250-499"}, None, "outside 10 to 249 employees"),  # a band from the search
    ({"employees": None, "size_band": "1000-2499"}, "outside 5 to 500 employees", "outside 10 to 249 employees"),
])
def test_verify_holds_accounts_to_the_general_range(change, wide, default):
    a = account(**change)
    assert verify.check(a, {}, WIDE, set(), set()) == wide
    assert verify.check(a, {}, BASE, set(), set()) == default


def test_the_size_edges_follow_the_range():
    assert verify.size_edges(BASE) == (10, 50, 250)
    assert verify.size_edges(WIDE) == (5, 50, 501)
    assert verify.doubts(account(employees=6, size_band="5-9"), WIDE) == [
        "Apollo's estimate of 6 staff is within 2 of the 5-staff edge"]
    assert verify.doubts(account(employees=500, size_band="500-999"), WIDE) == [
        "Apollo's estimate of 500 staff is within 2 of the 501-staff edge"]
    assert verify.doubts(account(employees=250, size_band="250-499"), WIDE) == []  # no longer an edge


# -- the searches ---------------------------------------------------------------------------------------------


def test_the_universe_searches_every_band_the_range_touches():
    groups = uni.active_groups(WIDE)
    slices = [sl for g in uni.plan(WIDE, groups, ["NY"]).values() for sl in g]
    assert {sl.band for sl in slices} == set(WIDE.size_bands())
    top = next(sl for sl in slices if sl.band == "500-999")
    assert top.filters(WIDE)["organization_num_employees_ranges"] == ["500,500"]
    assert "|500-999=500,500|" in top.key  # a clipped band: a new clip starts afresh
    ten = next(sl for sl in slices if sl.band == "10-19")
    assert "|10-19|" in ten.key  # unclipped bands keep the keys their cursors were saved under


def test_lookalike_leads_search_the_range_through_the_nearest_cell():
    assert lookalike_leads.cell_bands(BASE, "10-49") == ("10-19", "20-49")
    assert lookalike_leads.cell_bands(WIDE, "10-49") == ("5-9", "10-19", "20-49")
    assert lookalike_leads.cell_bands(WIDE, "100-249") == ("100-249", "250-499", "500-999")
    assert lookalike_leads.us_filters("250-499", WIDE)["organization_num_employees_ranges"] == ["250,499"]


def test_the_roles_order_covers_sizes_past_its_two_ranges():
    founder = Role("Founder", ("CEO",), {"10-49": 1, "50-249": 2})
    hr = Role("HR manager", ("HR Manager",), {"50-249": 4})
    assert (founder.order_at(7), founder.order_at(30), founder.order_at(400)) == (1, 1, 2)
    assert (hr.order_at(7), hr.order_at(400), hr.order_at(None)) == (None, 4, None)


# -- website visitors: any US state (Harry, 6 Oct 2026) ----------------------------------------------------------


@pytest.mark.parametrize("state", ["CA", "WA", "OH", "FL"])
def test_a_visitor_in_any_us_state_is_verified_and_tiered_but_an_account_from_a_search_is_not(state):
    visitor = account(hq_state=state, source="site_visit", hq_country="United States")
    assert verify.check(visitor, {}, BASE, set(), set()) is None
    assert "HQ" not in (tiers.hard_exclusion(visitor, {}, BASE) or "")
    searched = account(hq_state=state)
    assert verify.check(searched, {}, BASE, set(), set()) in ("HQ in CA or WA", "HQ state not active")
    assert "HQ" in (tiers.hard_exclusion(searched, {}, BASE) or "")


def test_a_us_visitor_with_no_state_is_no_doubt_but_one_with_no_country_either_is():
    us = account(hq_state=None, source="site_visit", hq_country="United States")
    assert verify.check(us, {}, BASE, set(), set()) is None and verify.doubts(us, BASE) == []
    assert tiers.hard_exclusion(us, {}, BASE) is None
    unknown = account(hq_state=None, source="site_visit", hq_country=None)
    assert verify.check(unknown, {}, BASE, set(), set()) == "HQ state unknown"
    assert verify.doubts(unknown, BASE) == [verify.NO_STATE]  # to the hand-check, where an Overrides row settles it
