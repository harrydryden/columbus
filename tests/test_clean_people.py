"""People fields: names, states, titles to roles, size bands (SPEC 13, 5, 6)."""

from __future__ import annotations

import pytest

from us_outbound.clean.people import (
    clean_person_name,
    is_skipped_title,
    map_title_to_role,
    size_band,
    state_code,
)
from us_outbound.settings.model import SIZE_BANDS, Role

# The SPEC 5 Roles tab, built directly (not through the settings agent's defaults).
PEOPLE = Role(
    "People leader",
    ("Head of People", "VP People", "Chief People Officer", "People Ops Lead", "HR Director", "Director of HR",
     "HR Manager", "People & Culture"),
    first_choice_for_size=("50-249",),
)
FOUNDER = Role(
    "Founder or executive",
    ("CEO", "Founder", "Co-founder", "President", "Managing Partner", "Managing Director", "Executive Director"),
    first_choice_for_size=("10-49",),
    fallback_order={"50-249": 3},
)
OPERATIONS = Role(
    "Operations",
    ("COO", "Chief of Staff", "Head of Operations", "Director of Operations", "Office Manager", "Firm Administrator"),
    fallback_order={"10-49": 2, "50-249": 2},
)
FINANCE = Role("Finance", ("CFO", "Finance Director", "Head of Finance", "Controller"))
ROLES = (PEOPLE, FOUNDER, OPERATIONS, FINANCE)


@pytest.mark.parametrize(
    "first, last, want",
    [
        ("JANE", "DOE, MBA", ("Jane", "Doe")),
        ("JANE DOE, MBA", "", ("Jane Doe", "")),
        ("sean", "o'brien", ("Sean", "O'Brien")),
        ("SEAN", "O’BRIEN", ("Sean", "O'Brien")),
        ("Sarah (she/her)", "Connor", ("Sarah", "Connor")),
        ("Dr. Amy", "Lee PhD", ("Amy", "Lee")),
        ("Dr. Amy Lee PhD", "", ("Amy Lee", "")),
        ("mary-kate", "SMITH-JONES", ("Mary-Kate", "Smith-Jones")),
        ("jim", "mcdonald", ("Jim", "McDonald")),
        ("Angus", "MacArthur", ("Angus", "MacArthur")),  # intentional casing kept
        ("DeShawn", "LaRue", ("DeShawn", "LaRue")),
        ("Tom", "Ma", ("Tom", "Ma")),  # a surname, not a Master of Arts
        ("Anh", "DO", ("Anh", "Do")),
        ("Lee", "Smith MA", ("Lee", "Smith")),
        ("🌟 Priya", "Patel 🚀", ("Priya", "Patel")),
        ("Alex", "Kim he/him", ("Alex", "Kim")),
        ("Sam", "Rivera [they/them]", ("Sam", "Rivera")),
        ("Jane", "Doe (pronouns: she/her)", ("Jane", "Doe")),
        ("Robert \"Bob\"", "Smith Jr.", ("Robert", "Smith Jr.")),
        ("JOHN", "SMITH III", ("John", "Smith III")),
        ("Jane", "Doe, SHRM-CP, SPHR", ("Jane", "Doe")),
        ("Jane", "Doe, SHRM-SCP", ("Jane", "Doe")),
        ("Mr. John", "Smith, Esq.", ("John", "Smith")),
        ("Chris", "Evans, M.S.", ("Chris", "Evans")),
        ("Kate", "Lin MBA/PhD", ("Kate", "Lin")),
        ("Pat", "Nguyen, CPA, CFP", ("Pat", "Nguyen")),
        ("Morgan", "Ellis PMP", ("Morgan", "Ellis")),
        ("josé", "garcía", ("José", "García")),
        ("  Jane  ", "  Doe  ", ("Jane", "Doe")),
        (None, None, ("", "")),
    ],
)
def test_clean_person_name(first, last, want):
    assert clean_person_name(first, last) == want


@pytest.mark.parametrize(
    "value, code",
    [
        ("Illinois", "IL"),
        ("il", "IL"),
        ("NY", "NY"),
        ("New York", "NY"),
        ("new york state", "NY"),
        ("Calif.", "CA"),
        ("Mass.", "MA"),
        ("N.Y.", "NY"),
        ("Penn", "PA"),
        ("Penna.", "PA"),
        ("Tex.", "TX"),
        ("W. Va.", "WV"),
        ("DC", "DC"),
        ("D.C.", "DC"),
        ("District of Columbia", "DC"),
        ("Washington, D.C.", "DC"),
        ("Washington", "WA"),
        ("Chicago, IL, USA", "IL"),
        ("New York, NY 10001", "NY"),
        ("Seattle, Washington, United States", "WA"),
        ("Indiana, PA", "PA"),
        ("Puerto Rico", "PR"),
        ("Ontario", None),
        ("London, England", None),
        ("", None),
        (None, None),
    ],
)
def test_state_code(value, code):
    assert state_code(value) == code


@pytest.mark.parametrize(
    "title, role",
    [
        ("Head of People & Culture", "People leader"),
        ("Head of People and Culture", "People leader"),
        ("VP, People", "People leader"),
        ("Vice President, People", "People leader"),
        ("SVP People", "People leader"),
        ("Director of Human Resources", "People leader"),
        ("Sr. Dir. of Human Resources", "People leader"),
        ("Senior HR Manager", "People leader"),
        ("People Ops Lead", "People leader"),
        ("Head of People Operations", "People leader"),
        ("Director, People & Culture (US)", "People leader"),
        ("Office Manager", "Operations"),
        ("Chief Operating Officer", "Operations"),
        ("Chief of Staff to the CEO", "Operations"),
        ("Dir. of Operations", "Operations"),
        ("Firm Administrator", "Operations"),
        ("Co-Founder & CEO", "Founder or executive"),
        ("Cofounder", "Founder or executive"),
        ("Chief Executive Officer", "Founder or executive"),
        ("President & COO", "Founder or executive"),
        ("Managing Partner", "Founder or executive"),
        ("Executive Director", "Founder or executive"),
        ("Chief Financial Officer", "Finance"),
        ("Vice President of Sales", None),  # "president" inside "vice president" does not count
        ("Executive Assistant to the CEO", None),
        ("Software Engineer", None),
        ("Internal Communications Manager", None),
        ("", None),
        (None, None),
    ],
)
def test_map_title_to_role(title, role):
    assert map_title_to_role(title, ROLES) == role


def test_finance_titles_map_only_when_finance_is_on_the_tab():
    assert map_title_to_role("Chief Financial Officer", (PEOPLE, FOUNDER, OPERATIONS)) is None


def test_roles_come_from_the_tab_not_from_code():
    assert map_title_to_role("Chief Human Resources Officer", ROLES) is None
    extra = Role("People leader", PEOPLE.titles + ("CHRO",))
    assert map_title_to_role("Chief Human Resources Officer", (extra, FOUNDER)) == "People leader"


def test_longest_match_wins():
    roles = (Role("A", ("Director",)), Role("B", ("Director of Operations",)))
    assert map_title_to_role("Director of Operations", roles) == "B"


@pytest.mark.parametrize(
    "title",
    [
        "People Operations Coordinator",
        "Talent Acquisition Manager",
        "HR Director, EMEA",
        "Managing Director, APAC",
        "Head of People, Asia-Pacific",
        "VP People (Europe, Middle East & Africa)",
        "HR Intern",
        "Summer Internship - People Team",
        "Senior Technical Recruiter",
        "Recruiting Lead",
        "Head of Recruitment",
        "Sales Ops Manager",
        "Sales Operations Director",
        "Customer Success Ops Manager",
        "CS Operations Lead",
        "Director of Revenue Operations",
    ],
)
def test_skipped_titles(title):
    assert is_skipped_title(title) is True
    assert map_title_to_role(title, ROLES) is None


@pytest.mark.parametrize(
    "title",
    ["Interim HR Director", "Head of People & Talent", "International Operations Manager", "CEO", "Office Manager"],
)
def test_titles_not_skipped(title):
    assert is_skipped_title(title) is False


@pytest.mark.parametrize(
    "employees, band",
    [
        (None, None),
        (9, None),
        (10, "10-19"),
        (19, "10-19"),
        (20, "20-49"),
        (49, "20-49"),
        (50, "50-99"),
        (99, "50-99"),
        (100, "100-249"),
        (249, "100-249"),
        (250, None),
        ("64", "50-99"),
        (" 120 ", "100-249"),
        ("1,200", None),
        (10.7, "10-19"),
        ("many", None),
        (True, None),
        (float("inf"), None),
    ],
)
def test_size_band(employees, band):
    assert size_band(employees) == band
    assert band is None or band in SIZE_BANDS
