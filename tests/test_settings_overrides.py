"""settings/overrides.py: the Overrides tab laid over an account in one place (9 Oct 2026)."""

from __future__ import annotations

import dataclasses

from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import Override
from us_outbound.settings.overrides import effective, parse_value
from us_outbound.settings.validate import validate_all

BASE, _ = validate_all(default_tabs())
ACCOUNT = {"domain": "acme.com", "industry": "Fintech", "industry_group": "Technology & Startups", "employees": 64,
           "size_band": "50-99", "hq_state": "NY", "clean_name": "Acme"}


def with_rows(*rows):
    return dataclasses.replace(BASE, overrides=tuple(Override("acme.com", f, v) for f, v in rows))


def test_an_overridden_label_brings_its_group_and_a_count_its_band():
    s = with_rows(("industry", "Advertising agencies"), ("employees", "180"), ("hq_state", "new jersey"))
    out = effective(ACCOUNT, s)
    assert (out["industry"], out["industry_group"]) == ("Advertising agencies", "Marketing & Creative Agencies")
    assert (out["employees"], out["size_band"], out["hq_state"]) == (180, "100-249", "NJ")


def test_names_places_labels_and_codes_stay_text():
    s = with_rows(("clean_name", "1800"), ("naics", "541613"), ("open_roles", "4"), ("hiring", "yes"))
    out = effective(ACCOUNT, s)
    assert (out["clean_name"], out["naics"]) == ("1800", "541613")
    assert (out["open_roles"], out["hiring"]) == (4, True)  # any other value as a fact's
    assert parse_value("2.5") == 2.5 and parse_value("No") is False and parse_value("07") == 7.0


def test_a_row_giving_the_group_keeps_it_and_fields_limits_the_rows():
    s = with_rows(("industry", "Fintech"), ("industry_group", "Financial Services"), ("open_roles", "4"))
    assert effective(ACCOUNT, s)["industry_group"] == "Financial Services"
    assert "open_roles" not in effective({"industry": "Fintech"}, s, domain="acme.com", fields=("industry",))
    assert effective({**ACCOUNT, "domain": "other.com"}, s) == {**ACCOUNT, "domain": "other.com"}
