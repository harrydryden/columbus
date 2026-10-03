"""People fields: names, states, titles to roles, size bands (SPEC 13, 5, 6)."""

from __future__ import annotations

import pytest

from us_outbound.clean.people import (
    SENIORITY,
    clean_person_name,
    company_size,
    is_junior_title,
    is_skipped_title,
    map_title_to_role,
    rank_person,
    seniority_rank,
    size_band,
    state_code,
    title_key,
)
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import SIZE_BANDS, Role
from us_outbound.settings.validate import validate_all

# The SPEC 5 Roles tab, built directly (not through the settings agent's defaults).
PEOPLE = Role(
    "People leader",
    ("Head of People", "VP People", "Chief People Officer", "People Ops Lead", "HR Director", "Director of HR",
     "HR Manager", "People & Culture"),
    order={"50-249": 1},
)
FOUNDER = Role(
    "Founder or executive",
    ("CEO", "Founder", "Co-founder", "President", "Managing Partner", "Managing Director", "Executive Director"),
    order={"10-49": 1, "50-249": 3},
)
OPERATIONS = Role(
    "Operations",
    ("COO", "Chief of Staff", "Head of Operations", "Director of Operations", "Office Manager", "Firm Administrator"),
    order={"10-49": 2, "50-249": 2},
)
FINANCE = Role("Finance", ("CFO", "Finance Director", "Head of Finance", "Controller"))
ROLES = (PEOPLE, FOUNDER, OPERATIONS, FINANCE)
# Harry's 1 Oct 2026 Roles tab, as the sheet is created with it.
DEFAULT_ROLES = validate_all(default_tabs())[0].roles
LEGAL, TECH = "Legal Teams", "Technology & Startups"


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


def test_company_size_reads_the_band_first():
    assert company_size(64, "50-99") == 50
    assert company_size(48, "50-99") == 50  # the resolver's band wins over a raw count
    assert company_size("30", None) == 30
    assert company_size(None, "") is None and company_size("many") is None


# -- Harry's 1 Oct 2026 Roles tab: real-world titles --------------------------------------------------


@pytest.mark.parametrize(
    "title, role",
    [
        # Founder or executive
        ("CEO", "Founder or executive"),
        ("Chief Executive Officer", "Founder or executive"),
        ("Co-Founder & CEO", "Founder or executive"),
        ("Founder and CEO", "Founder or executive"),
        ("Cofounder", "Founder or executive"),
        ("Owner", "Founder or executive"),
        ("Co-Owner", "Founder or executive"),
        ("Owner/Operator", "Founder or executive"),
        ("President", "Founder or executive"),
        ("President & CEO", "Founder or executive"),
        ("Managing Partner", "Founder or executive"),
        ("Founding Partner", "Founder or executive"),
        ("Managing Director", "Founder or executive"),
        ("Managing Principal", "Founder or executive"),
        ("Executive Director", "Founder or executive"),
        ("General Manager", "Founder or executive"),
        ("GM", "Founder or executive"),
        # People leader
        ("Chief People Officer", "People leader"),
        ("CPO", "People leader"),
        ("CHRO", "People leader"),
        ("Chief Human Resources Officer", "People leader"),
        ("Chief HR Officer", "People leader"),
        ("Chief People & Culture Officer", "People leader"),
        ("VP, People", "People leader"),
        ("Vice President of Human Resources", "People leader"),
        ("V.P. People Operations", "People leader"),
        ("SVP, People & Culture", "People leader"),
        ("Head of People", "People leader"),
        ("Head of HR", "People leader"),
        ("Head of Human Resources", "People leader"),
        ("Head of People & Culture", "People leader"),
        ("Head of People Ops", "People leader"),
        ("Director of People", "People leader"),
        ("Dir. of Human Resources", "People leader"),
        ("Sr. Director, People & Culture", "People leader"),
        ("HR Director", "People leader"),
        ("Human Resources Director", "People leader"),
        ("People Director", "People leader"),
        ("People & Culture Lead", "People leader"),
        ("People and Culture Director", "People leader"),
        ("Director of Total Rewards", "People leader"),
        # HR manager rows get the People leader copy
        ("HR Manager", "People leader"),
        ("Senior HR Manager", "People leader"),
        ("Human Resources Manager", "People leader"),
        ("People Operations Manager", "People leader"),
        ("HR Generalist", "People leader"),
        ("HR Business Partner", "People leader"),
        ("HRBP", "People leader"),
        ("Benefits Manager", "People leader"),
        # Operations
        ("COO", "Operations"),
        ("Chief Operating Officer", "Operations"),
        ("VP of Operations", "Operations"),
        ("Head of Operations", "Operations"),
        ("Director of Operations", "Operations"),
        ("Chief of Staff", "Operations"),
        ("Director of Finance & Operations", "Operations"),
        ("Office Manager", "Operations"),
        ("Firm Administrator", "Operations"),
        ("Operations Manager", "Operations"),
        ("Practice Manager", "Operations"),
        ("Studio Manager", "Operations"),
        ("Office Admin", "Operations"),
        # Finance (never contacted, but recognized)
        ("CFO", "Finance"),
        ("Chief Financial Officer", "Finance"),
        ("Controller", "Finance"),
        ("VP Finance", "Finance"),
        # Not on the tab
        ("Chief Product Officer", None),
        ("Chief Technology Officer", None),
        ("Product Owner", None),
        ("Partner Manager", None),
        ("Principal Engineer", None),
        ("Partner", None),  # only at professional firms
        ("Principal", None),
        ("Vice President of Sales", None),
        ("Head of Talent Acquisition", None),  # SPEC 5 skip: recruiters
        ("People Operations Coordinator", None),  # SPEC 5 skip: coordinators
        ("Software Engineer", None),
    ],
)
def test_default_roles_map_real_titles(title, role):
    assert map_title_to_role(title, DEFAULT_ROLES) == role


@pytest.mark.parametrize("title", ["Partner", "Senior Partner", "Equity Partner", "Principal", "Partner, Litigation"])
def test_partners_and_principals_count_only_at_professional_firms(title):
    assert map_title_to_role(title, DEFAULT_ROLES, LEGAL) == "Founder or executive"
    assert map_title_to_role(title, DEFAULT_ROLES, "Professional Services") == "Founder or executive"
    assert map_title_to_role(title, DEFAULT_ROLES, TECH) is None
    assert rank_person(title, DEFAULT_ROLES, 30, TECH) is None
    assert rank_person(title, DEFAULT_ROLES, 30, LEGAL).role.role == "Partner at a professional firm"


@pytest.mark.parametrize("title", ["Principal Engineer", "Principal Consultant", "Partner Manager", "Partner Account Manager"])
def test_a_guarded_word_before_another_job_is_not_an_owner(title):
    assert map_title_to_role(title, DEFAULT_ROLES, "Professional Services") is None


@pytest.mark.parametrize(
    "title, rank",
    [
        ("CEO", 0), ("Founder", 0), ("Owner", 0), ("President", 0), ("CHRO", 0), ("CPO", 0),
        ("Chief People Officer", 0), ("Managing Director", 0), ("Executive Director", 0), ("Managing Partner", 0),
        ("VP People", 1), ("SVP, HR", 1), ("Vice President, Operations", 1), ("Head of People", 1),
        ("General Manager", 1), ("Partner", 1), ("Chief of Staff", 1),
        ("Director of HR", 2), ("Sr. Dir. People", 2), ("HR Director", 2), ("Associate Director of HR", 2),
        ("HR Manager", 3), ("People Ops Lead", 3), ("HR Business Partner", 3), ("Office Mgr", 3),
        ("HR Generalist", 4), ("Firm Administrator", 4), ("HR Specialist", 4),
    ],
)
def test_seniority_rank(title, rank):
    assert seniority_rank(title) == rank, SENIORITY[seniority_rank(title)]


@pytest.mark.parametrize(
    "title, junior",
    [
        ("Executive Assistant", True), ("Administrative Asst.", True), ("Associate Director of HR", True),
        ("HR Coordinator", True), ("Summer Intern", True), ("Benefits Specialist", True), ("Jr. Office Manager", True),
        ("Head of People", False), ("Office Manager", False), ("Founder", False),
    ],
)
def test_junior_titles(title, junior):
    assert is_junior_title(title) is junior


def test_title_key_spells_a_title_one_way():
    assert title_key("Head of Human Resources") == title_key("head of HR") == "head hr"
    assert title_key("VP, People & Culture") == title_key("Vice President of People and Culture")
    assert title_key("Chief People Officer") == title_key("CPO")


def _order(titles, employees, group=""):
    ranked = [(rank_person(t, DEFAULT_ROLES, employees, group), t) for t in titles]
    return [t for r, t in sorted((x for x in ranked if x[0]), key=lambda x: x[0].key)]


def test_rank_at_10_to_49_founder_then_people_leader_then_operations_then_the_office():
    titles = ["Office Manager", "COO", "Head of People", "HR Manager", "CEO", "CFO", "Executive Assistant to the CEO"]
    assert _order(titles, 30) == ["CEO", "Head of People", "COO", "Office Manager"]  # no HR Manager, CFO or assistant


def test_rank_at_50_to_249_people_leader_then_founder_then_operations_then_hr_manager():
    titles = ["Office Manager", "HR Manager", "COO", "CEO", "VP People", "CFO", "HR Generalist"]
    assert _order(titles, 120) == ["VP People", "CEO", "COO", "HR Manager", "HR Generalist", "Office Manager"]


def test_seniority_decides_within_a_rank_then_the_title_match():
    assert _order(["Director of People", "People Lead", "Head of People", "CHRO"], 120) == [
        "CHRO", "Head of People", "Director of People", "People Lead"]
    assert _order(["People Operations Manager", "Senior HR Manager"], 120) == ["People Operations Manager", "Senior HR Manager"]
    assert _order(["Partner", "Managing Partner"], 30, LEGAL) == ["Managing Partner", "Partner"]


def test_the_newest_in_role_breaks_a_tie():
    a = rank_person("Head of People", DEFAULT_ROLES, 120, days_in_role=400)
    b = rank_person("Head of People", DEFAULT_ROLES, 120, days_in_role=40)
    c = rank_person("Head of People", DEFAULT_ROLES, 120)
    assert sorted([a, b, c], key=lambda r: r.key) == [b, a, c]


def test_a_title_on_two_rows_counts_as_the_better_one_at_the_size():
    assert rank_person("Founder & Head of People", DEFAULT_ROLES, 30).role.writes_as == "Founder or executive"
    assert rank_person("Founder & Head of People", DEFAULT_ROLES, 120).role.writes_as == "People leader"
    assert rank_person("CFO & COO", DEFAULT_ROLES, 120).role.role == "Operations"


def test_juniors_come_last_and_never_as_people_leaders_or_founders():
    assert rank_person("Associate Director of People", DEFAULT_ROLES, 120) is None
    assert rank_person("Assistant HR Manager", DEFAULT_ROLES, 120) is None
    assert rank_person("Executive Assistant, Office of the CEO", DEFAULT_ROLES, 30) is None
    junior = rank_person("Assistant Office Manager", DEFAULT_ROLES, 30)
    assert junior.junior and junior.role.role == "Office or firm administrator"
    assert _order(["Assistant Office Manager", "Office Manager"], 30) == ["Office Manager", "Assistant Office Manager"]


def test_finance_and_skipped_titles_are_never_ranked():
    for title in ("CFO", "Controller", "Recruiting Lead", "HR Intern", "Head of People, EMEA"):
        assert rank_person(title, DEFAULT_ROLES, 30) is None and rank_person(title, DEFAULT_ROLES, 120) is None
