"""The sheet's SPEC 5 defaults: shape, content, and that they pass validation."""

import dataclasses
import re
from datetime import time

import pytest

from us_outbound.settings.defaults import COLUMNS, US_STATES, default_tabs
from us_outbound.settings.model import TABS, General, SendWindow
from us_outbound.settings.validate import COPY_VARIABLES, validate_all


@pytest.fixture(scope="module")
def tabs():
    return default_tabs()


@pytest.fixture(scope="module")
def settings(tabs):
    s, errors = validate_all(tabs)
    assert not any(errors.values()), [str(e) for v in errors.values() for e in v]
    return s


def test_every_tab_has_its_columns_and_only_strings(tabs):
    assert list(tabs) == list(TABS) == list(COLUMNS)
    for tab, rows in tabs.items():
        for row in rows:
            assert list(row) == COLUMNS[tab]
            assert all(isinstance(v, str) for v in row.values()), (tab, row)


def test_defaults_are_fresh_copies(tabs):
    other = default_tabs()
    other["Signals"][0]["weight"] = "99"
    assert tabs["Signals"][0]["weight"] == "25"


def test_general_has_every_model_key_once(tabs, settings):
    keys = [r["key"] for r in tabs["General"]]
    assert sorted(keys) == sorted(f.name for f in dataclasses.fields(General))
    assert len(keys) == len(set(keys))
    # Every value reads back as the model default: a key missing from the tab takes the same value.
    assert settings.general == General()


def test_general_spec_values(tabs, settings):
    raw = {r["key"]: r["value"] for r in tabs["General"]}
    assert raw["live_sending"] == "no"
    assert raw["send_window"] == "Mon–Fri 09:00–16:00 America/New_York"
    assert raw["approver_slack_ids"] == raw["postal_address"] == raw["privacy_url"] == ""
    assert raw["claude_model"] == "claude-haiku-4-5"
    notes = {r["key"]: r["note"] for r in tabs["General"]}
    assert "quarter of the Clay pool" in notes["clay_monthly_credits"]
    g = settings.general
    assert g.live_sending is False
    assert g.send_window == SendWindow((0, 1, 2, 3, 4), time(9), time(16), "America/New_York")
    assert (g.daily_enrol_cap, g.control_share, g.priority_threshold, g.standard_threshold) == (30, 0.15, 50, 20)
    assert (g.apollo_monthly_credits, g.apollo_floor, g.escalation_hours) == (1500, 5000, 24)
    assert g.claude_monthly_cap_usd == 10.0


# signal, weight, max_weight, action, suggests_angle, counts_for_days (SPEC 5)
SPEC_SIGNALS = [
    ("Mental health support listed", 25, None, "Score", "Progressive employer", 540),
    ("EAP named", 10, None, "Score", "Upgrade the EAP", 540),
    ("Modern mental-health vendor named", 0, None, "Hold", "Switch from a competitor", 540),
    ("Progressive benefits", 10, 30, "Score", "Progressive employer", 540),
    ("Culture or values page", 10, None, "Score", "Progressive employer", 540),
    ("People leader in place", 10, None, "Score", "", 365),
    ("New People leader", 30, None, "Score", "Progressive employer", 90),
    ("First People hire", 25, None, "Score", "Growing team", 90),
    ("Recent funding", 20, None, "Score", "Growing team", 540),
    ("Hiring and growth", 15, None, "Score", "Growing team", 90),
    ("Visited the US site", 20, None, "Score", "", 30),
    ("Viewed US pricing or demo page", 15, None, "Score", "", 30),
    ("Nonprofit budget", 15, None, "Score", "", 400),
    ("Nonprofit fiscal year ahead", 20, None, "Score", "", 1),
    ("Q4 plan-year window", 10, None, "Score", "", 1),
    ("Layoffs", 0, None, "Suppress", "", 90),
]


def test_every_spec_signal(settings):
    got = {s.signal: s for s in settings.signals}
    assert list(got) == [row[0] for row in SPEC_SIGNALS]
    for name, weight, max_weight, action, angle, days in SPEC_SIGNALS:
        s = got[name]
        assert (s.weight, s.max_weight, s.action, s.suggests_angle, s.counts_for_days) == (
            weight, max_weight, action, angle, days,
        ), name
        assert s.active


def test_signal_sources_and_parsing(settings):
    got = {s.signal: s for s in settings.signals}
    assert got["Modern mental-health vendor named"].sources == ("clay_careers", "job_posts")
    assert got["Recent funding"].sources == ("apollo_org", "clay_funding")
    # people_leader_count comes from apollo_people, so that source is listed too.
    assert got["First People hire"].sources == ("apollo_jobs", "apollo_people")
    assert got["EAP named"].terms == ("EAP", "employee assistance", "ComPsych", "GuidanceResources", "Magellan")
    assert "Justworks Plus" in got["Modern mental-health vendor named"].terms
    assert got["Modern mental-health vendor named"].context == {
        "headspace": ("for Work", "app", "subscription"),
        "calm": ("app", "premium", "business", "subscription"),
    }
    assert got["Q4 plan-year window"].condition.evaluate({"month": 11})
    assert got["First People hire"].condition.evaluate({"open_people_roles": 1, "people_leader_count": 0})
    assert got["Culture or values page"].condition.evaluate({"values_page": True})
    for s in settings.signals:
        assert bool(s.terms) != s.is_condition, s.signal


def test_angles_in_order(settings):
    assert [(a.angle, a.order, a.active) for a in settings.angles] == [
        ("Upgrade the EAP", 1, True),
        ("Progressive employer", 2, True),
        ("Growing team", 3, True),
        ("General", 4, True),
        ("Switch from a competitor", 5, False),
    ]
    assert settings.angle("Upgrade the EAP").argument.startswith("Your team already has an EAP")
    assert all(a.default_opener for a in settings.angles)


def test_industries(settings):
    by_group: dict[str, list] = {}
    for i in settings.industries:
        by_group.setdefault(i.industry_group, []).append(i)
    tech = by_group["Technology & Startups"]
    assert len(tech) == 17 and all(i.active and i.priority == 1 for i in tech)
    assert all(len(i.apollo_keywords) == 1 for i in tech)
    assert tech[0].naics_prefixes == ("5112", "513210", "5415", "518210")
    marketing = by_group["Marketing & Creative Agencies"]
    assert len(marketing) == 10 and all(i.active and i.priority == 2 for i in marketing)
    assert marketing[0].naics_prefixes == ("5418", "541430", "541613", "5121", "5111")
    assert len(by_group["Nonprofits"]) == 13 and not any(i.active for i in by_group["Nonprofits"])
    assert [i.industry for i in by_group["Legal Teams"]] == ["Legal Teams"]
    prof = {i.industry: i for i in by_group["Professional Services"]}
    assert prof["CPA firms"].exclude_naics == ("541214", "541612")
    assert not any(i.active for i in prof.values())
    active_groups = {i.industry_group for i in settings.industries if i.active}
    assert active_groups == {"Technology & Startups", "Marketing & Creative Agencies"}
    assert {i.priority for g, rows in by_group.items() if g not in {
        "Technology & Startups", "Marketing & Creative Agencies", "Nonprofits", "Legal Teams", "Professional Services"
    } for i in rows} == {9}
    assert all(i.landing_page_url == "" and i.proof_point == "" for i in settings.industries)


def test_industry_notes(tabs):
    notes = {r["industry"]: r["note"] for r in tabs["Industries"]}
    assert notes["Fintech"].startswith("On from 26 Oct")
    assert "Harry to fill" in notes["Fintech"]
    assert notes["Nonprofits"].startswith("January")
    assert notes["Churches & religious organizations"] == notes["Emergency & rescue"] == "off"
    assert notes["Legal Teams"].startswith("January")
    assert notes["Management consulting"].startswith("After January")
    assert notes["Staffing agencies"] == "off"
    assert notes["HR consulting"].startswith("never")


def test_states(tabs, settings):
    assert [s.state for s in settings.states] == list(US_STATES)
    assert len(US_STATES) == 51
    assert set(settings.active_states()) == {"NY", "MA", "NJ", "PA", "IL", "GA", "TX"}
    notes = {r["state"]: r["note"] for r in tabs["States"]}
    assert notes["FL"] == "off until Harry confirms"
    assert all(notes[s] == "Wave 2" for s in ("NC", "VA", "MD", "OH", "MN"))
    assert notes["CA"] == notes["WA"] == "never (also enforced in code)"


def _contact_order(roles, employees: int) -> list[str]:
    """Who to contact first at a company size, from the Roles tab alone."""
    ranked = []
    for r in roles:
        for rng in r.first_choice_for_size:
            lo, hi = map(int, rng.split("-"))
            if lo <= employees <= hi:
                ranked.append((1, r.role))
        for rng, rank in r.fallback_order.items():
            lo, hi = map(int, rng.split("-"))
            if lo <= employees <= hi:
                ranked.append((rank, r.role))
    return [role for _, role in sorted(ranked)]


def test_roles_encode_who_to_contact_first(settings):
    assert _contact_order(settings.roles, 30) == ["Founder or executive", "Operations"]
    assert _contact_order(settings.roles, 120) == ["People leader", "Operations", "Founder or executive"]
    finance = next(r for r in settings.roles if r.role == "Finance")
    assert not finance.first_choice_for_size and not finance.fallback_order
    assert finance.titles == ("CFO", "Finance Director", "Head of Finance", "Controller")
    people = next(r for r in settings.roles if r.role == "People leader")
    assert "People & Culture" in people.titles and len(people.titles) == 8


def test_mailboxes(settings):
    assert [(m.address, m.domain, m.owner_name, m.status, m.daily_cap) for m in settings.mailboxes] == [
        ("hannah@meetspill.org", "meetspill.org", "Hannah Spalding", "Warming", 30),
        ("harry@meetspill.org", "meetspill.org", "Harry Dryden", "Warming", 30),
        ("sam@meetspill.org", "meetspill.org", "Sam Jackson", "Warming", 30),
        ("harry@tryspill.org", "tryspill.org", "Harry Dryden", "Warming", 30),
    ]
    assert settings.mailboxes[0].signature == "Hannah Spalding\nSpill\nspill.chat/us"
    assert settings.owners() == ("Hannah Spalding", "Harry Dryden", "Sam Jackson")


def test_first_test_is_planned(settings):
    (t,) = settings.tests
    assert (t.test_id, t.version_a, t.version_b, t.accounts_per_version, t.status) == (
        "t1-eap-opener", "eap-v1", "general-v1", 400, "planned",
    )
    assert t.decision_rule.startswith("reply rate, human replies within 21 days of step 1")
    assert "2×" in t.decision_rule
    assert t.start_date is None and t.read_date is None
    assert settings.running_test() is None
    assert not settings.overrides


# -- copy obeys SPEC 10 ------------------------------------------------------------

FORBIDDEN = re.compile(r"\b(therapy|therapies|therapist|therapists|licensed|licence|unlimited)\b", re.IGNORECASE)
BRITISH = re.compile(
    r"\b(counsell\w*|organis\w*|utilis\w*|recognis\w*|prioritis\w*|colour\w*|favour\w*|behaviour\w*|programme\w*|"
    r"centre\w*|labour\w*|enrol|enrolment|personalis\w*|analys(e|es|ed|ing)|cheque|wellbeing)\b",
    re.IGNORECASE,
)
FOOTER_WORDS = re.compile(r"\b(STOP|opt out|opt-out|unsubscribe|privacy)\b", re.IGNORECASE)


def _copy_text(row) -> str:
    return row["subject"] + "\n" + row["body"]


def test_copy_rows_are_drafts_for_both_versions(settings):
    assert {(c.copy_version, c.angle) for c in settings.copy} == {("general-v1", "General"), ("eap-v1", "Upgrade the EAP")}
    for version in ("general-v1", "eap-v1"):
        assert sorted(c.step for c in settings.copy if c.copy_version == version) == [1, 2, 3, 4]
    assert all(c.status == "draft" and c.approved_by == "" for c in settings.copy)
    assert settings.approved_copy("eap-v1", 1) is None


def test_copy_obeys_the_rules(tabs):
    for row in tabs["Copy"]:
        text = _copy_text(row)
        label = f"{row['copy_version']} step {row['step']}"
        assert not FORBIDDEN.search(text), label
        assert not BRITISH.search(text), (label, BRITISH.search(text))
        assert not FOOTER_WORDS.search(text), label  # the footer is added at render time
        assert not re.search(r"https?://|www\.", text), label  # step 1's only link is the footer's privacy link
        assert "Spill EAP" not in text and "our EAP" not in text.lower(), label
        for line in text.splitlines():
            assert len(line) <= 300, label
        # 30% is the only statistic; other digits appear only inside variables.
        stripped = re.sub(r"\{\{[^}]*\}\}", "", text).replace("30%", "")
        assert not re.search(r"\d", stripped), (label, stripped)
        assert set(re.findall(r"\{\{\s*(\w+)\s*\}\}", text)) <= COPY_VARIABLES, label
        if "counsel" in text.lower():
            assert re.search(r"\bcounsel(or|ors|ing)\b", text), label


def test_copy_sequence_shape(tabs):
    for version in ("general-v1", "eap-v1"):
        steps = {int(r["step"]): r for r in tabs["Copy"] if r["copy_version"] == version}
        s1, s2, s3, s4 = (steps[i]["body"] for i in (1, 2, 3, 4))
        assert "{{opener}}" in s1 and "{{demo_line}}" not in s1 and "{{proof}}" not in s1
        assert "same-day" in s1 and "registered counselors" in s1
        assert "{{proof}}" in s2 and "{{demo_line}}" in s2
        assert s3.count("?") == 1 and len(s3) < 200
        assert "one-pager" in s4
    eap1 = next(r for r in tabs["Copy"] if r["copy_version"] == "eap-v1" and r["step"] == "1")
    assert "EAP" in eap1["body"]
