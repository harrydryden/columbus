"""Company-name cleaning (SPEC 13, Data cleaning)."""

from __future__ import annotations

import pytest

from us_outbound.clean.names import clean_company_name, names_disagree, strip_entity_suffix

# raw name -> (clean_name, legal_name)
CASES = [
    ("Acme Creative, LLC | Chicago's best agency", "Acme Creative", "Acme Creative, LLC"),
    ("ACME Inc.", "ACME", "ACME Inc."),
    ("Smith & Jones, P.A.", "Smith & Jones", "Smith & Jones, P.A."),
    ("Foo (formerly Bar) Inc", "Foo", "Foo Inc"),
    ("iCIMS, Inc.", "iCIMS", "iCIMS, Inc."),
    ("Official Site – Beta Corp", "Beta", "Beta Corp"),
    ("HubSpot, Inc.", "HubSpot", "HubSpot, Inc."),
    ("Acme, Inc. LLC", "Acme", "Acme, Inc. LLC"),
    ("Zeta L.L.C.", "Zeta", "Zeta L.L.C."),
    ("Gamma Partners LLP", "Gamma Partners", "Gamma Partners LLP"),
    ("Theta Law PLLC", "Theta Law", "Theta Law PLLC"),
    ("Mu Architects, PC", "Mu Architects", "Mu Architects, PC"),
    ("Nu Dental P.C.", "Nu Dental", "Nu Dental P.C."),
    ("Delta Corp.", "Delta", "Delta Corp."),
    ("Epsilon Ltd.", "Epsilon", "Epsilon Ltd."),
    ("Omicron Ltd", "Omicron", "Omicron Ltd"),
    ("Coca-Cola Bottling Co.", "Coca-Cola Bottling", "Coca-Cola Bottling Co."),
    ("Eta Holdings, Inc. - Official Website", "Eta Holdings", "Eta Holdings, Inc."),
    ("Kappa: Payroll made simple", "Kappa", "Kappa"),
    ("Lambda Studio — Design for humans", "Lambda Studio", "Lambda Studio"),
    ("Iota Group [US]", "Iota Group", "Iota Group"),
    ("Welcome to Acme Dental", "Acme Dental", "Acme Dental"),
    ("Home | Beta Labs", "Beta Labs", "Beta Labs"),
    ("Beta Labs | Home", "Beta Labs", "Beta Labs"),
    ("Sigma Homepage", "Sigma", "Sigma"),
    ("  Tau    Creative   Group  ", "Tau Creative Group", "Tau Creative Group"),
    ("Upsilon™ Corporation (USA", "Upsilon", "Upsilon Corporation"),
    ('"Phi Health" Inc.', "Phi Health", "Phi Health Inc."),
]


@pytest.mark.parametrize("raw, clean, legal", CASES)
def test_clean_company_name(raw, clean, legal):
    assert clean_company_name(raw) == (clean, legal)


# Names that must survive untouched: intentional casing, colons and dashes inside a name,
# "& Co." as part of the name, and a trailing state code that is not an entity suffix.
KEEP = [
    "Re:Build Manufacturing",
    "Smith & Co.",
    "Johnson and Co.",
    "Big Brothers Big Sisters of Southeastern PA",
    "Acme Co",  # undotted Co may be Colorado
    "Hewlett-Packard",
    "dbt Labs",
    "AT&T",
]


@pytest.mark.parametrize("raw", KEEP)
def test_names_kept(raw):
    assert clean_company_name(raw)[0] == raw


def test_entity_suffix_after_comma_is_stripped_even_when_ambiguous():
    assert clean_company_name("Acme, Co")[0] == "Acme"
    assert clean_company_name("Rho Law, PA")[0] == "Rho Law"


def test_empty_result_falls_back_to_raw_trimmed():
    assert clean_company_name("  LLC ") == ("LLC", "LLC")
    assert clean_company_name("Home") == ("Home", "Home")
    assert clean_company_name("") == ("", "")
    assert clean_company_name(None) == ("", "")


def test_clay_name_is_the_start_but_our_rules_still_apply():
    assert clean_company_name("ACME CREATIVE AGENCY, LLC", "Acme Creative Agency") == (
        "Acme Creative Agency",
        "ACME CREATIVE AGENCY, LLC",
    )
    # Clay left a suffix and a tagline on: re-applied rules remove them.
    assert clean_company_name("Acme Creative", "Acme Creative LLC | Chicago")[0] == "Acme Creative"
    # Clay's placeholder values mean "no name".
    assert clean_company_name("Acme Creative, Inc.", "N/A")[0] == "Acme Creative"
    assert clean_company_name("Acme Creative, Inc.", "  ")[0] == "Acme Creative"
    # Clay's name reduces to nothing: the raw name is used.
    assert clean_company_name("Acme Creative, Inc.", "Inc.")[0] == "Acme Creative"


def test_clay_legal_name_used_only_when_raw_has_no_suffix_and_names_agree():
    assert clean_company_name("Acme Creative", clay_legal_name="Acme Creative LLC")[1] == "Acme Creative LLC"
    assert clean_company_name("Acme Creative, Inc.", clay_legal_name="Acme Creative LLC")[1] == "Acme Creative, Inc."
    assert clean_company_name("Acme Creative", clay_legal_name="Beta Holdings LLC")[1] == "Acme Creative"
    assert clean_company_name("Acme Creative", clay_legal_name="Acme Creative")[1] == "Acme Creative"


def test_strip_entity_suffix_never_empties_the_name():
    assert strip_entity_suffix("Inc.") == "Inc."
    assert strip_entity_suffix("Acme Inc Inc") == "Acme"


@pytest.mark.parametrize(
    "a, b, disagree",
    [
        ("Acme Creative", "ACME CREATIVE, LLC", False),
        ("The Acme Group", "Acme Group Inc", False),
        ("Smith and Jones", "Smith & Jones, P.A.", False),
        ("Acme Creative", "Acme Creative Agency", False),
        ("Acme Creatve", "Acme Creative", False),
        ("AcmeCreative", "Acme Creative", False),
        ("Acme", "Beta", True),
        ("Acme Creative", "Northwind Traders", True),
        ("Acme Creative", "", True),
        ("", None, False),
    ],
)
def test_names_disagree(a, b, disagree):
    assert names_disagree(a, b) is disagree
