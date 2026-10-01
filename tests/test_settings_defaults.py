"""The sheet's SPEC 5 defaults: shape, content, and that they pass validation."""

import dataclasses
from datetime import time

import pytest

from us_outbound.settings.defaults import COLUMNS, US_STATES, default_tabs
from us_outbound.settings.model import TABS, General, SendWindow
from us_outbound.settings.validate import validate_all


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
    assert raw["claude_model"] == "claude-opus-5-5" and raw["claude_task_model"] == "claude-sonnet-5-5"
    assert raw["email_format"] == "html" and raw["booking_page"] == "https://www.spill.chat/us/book-demo"
    assert raw["site_url"] == "https://www.spill.chat/us" and raw["price_from"] == "195"
    notes = {r["key"]: r["note"] for r in tabs["General"]}
    assert "calendar month" in notes["clay_monthly_credits"] and "0 means no Clay calls" in notes["clay_monthly_credits"]
    assert "Monday to Sunday" in notes["weekly_enrol_cap"] and "calendar month" in notes["apollo_monthly_credits"]
    g = settings.general
    assert g.live_sending is False
    assert g.send_window == SendWindow((0, 1, 2, 3, 4), time(9), time(16), "America/New_York")
    # Harry, 30 Sep 2026: a weekly enrolment target (SPEC's 30 a day is 150 a week), monthly credits.
    assert (g.weekly_enrol_cap, g.control_share, g.priority_threshold, g.standard_threshold) == (150, 0.15, 50, 20)
    assert (g.apollo_monthly_credits, g.clay_monthly_credits, g.apollo_floor, g.escalation_hours) == (2000, 2000.0, 5000, 24)
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
    assert list(got) == [row[0] for row in SPEC_SIGNALS] + ["Named by Harry"]  # the last is a build addition
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


UNREACHABLE = {"Insurance", "HR consulting", "Substance use treatment"}  # partners, never contacted: no copy


def test_industries(settings):
    """All 108 of the website's industry pages (Harry, 30 Sep 2026), grouped under its 15 hub pages."""
    assert len(settings.industries) == 108
    by_group: dict[str, list] = {}
    for i in settings.industries:
        by_group.setdefault(i.industry_group, []).append(i)
    assert len(by_group) == 15 and all(rows[0].industry == g for g, rows in by_group.items())  # each hub row first
    tech = by_group["Technology & Startups"]
    assert len(tech) == 17 and all(i.priority == 1 for i in tech)
    assert [i.industry for i in tech if not i.active] == ["Remote & hybrid teams"]  # not an industry (SPEC 5)
    assert tech[0].naics_prefixes == ("5112", "513210", "5415", "518210")
    assert all(i.apollo_keywords for i in settings.industries if i.industry != "Small Businesses")
    marketing = by_group["Marketing & Creative Agencies"]
    assert len(marketing) == 10 and all(i.active and i.priority == 2 for i in marketing)
    assert len(by_group["Nonprofits"]) == 14 and not any(i.active for i in by_group["Nonprofits"])
    assert [i.industry for i in by_group["Legal Teams"]] == ["Legal Teams"]
    prof = {i.industry: i for i in by_group["Professional Services"]}
    assert "541214" in prof["CPA firms"].exclude_naics and not any(i.active for i in prof.values())
    digital = settings.industry("Digital health")
    assert digital.industry_group == "Healthcare" and digital.active and digital.priority == 1
    active_groups = {i.industry_group for i in settings.industries if i.active}
    assert active_groups == {"Technology & Startups", "Marketing & Creative Agencies", "Healthcare"}
    assert sum(i.active for i in settings.industries) == 27  # as before: 17 tech labels (Digital health moved) and 10 agencies


def test_industry_pages_and_links(settings):
    live = [i for i in settings.industries if i.landing_page_url]
    assert len(live) == 107  # the 1 Oct export: only Small Businesses is still a draft on the site
    assert {i.industry for i in settings.industries if not i.landing_page_url} == {"Small Businesses"}
    assert all(i.landing_page_url.startswith("https://www.spill.chat/us/industry/mental-health-support-for-") for i in live)
    cpa = settings.industry("CPA firms")
    assert cpa.page.blurb == "Counseling that fits around client work and busy season"
    assert "Busy season is structural" in cpa.page.intro and cpa.page.customers.startswith("Proudly supporting")
    assert all("$" not in i.page.ticks for i in settings.industries)  # the site's price tick is left out
    assert all(i.proof_point == "" for i in settings.industries)  # Harry fills a named customer


def test_industry_notes(tabs):
    notes = {r["industry"]: r["note"] for r in tabs["Industries"]}
    assert notes["Fintech"].startswith("On from 26 Oct")
    assert notes["Nonprofits"].startswith("January")
    assert notes["Churches & religious organizations"].startswith("off")
    assert notes["Legal Teams"].startswith("January")
    assert notes["Management consulting"].startswith("After January")
    assert notes["Staffing agencies"].startswith("off")
    assert notes["HR consulting"].startswith("never")
    assert "draft on spill.chat" in notes["Small Businesses"] and "draft" not in notes["Retail & E-commerce"]
    assert all("partner" in notes[i] or "never" in notes[i] for i in UNREACHABLE)


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
    assert t.decision_rule.startswith("reply rate, human replies within 28 days of step 1")
    assert "2×" in t.decision_rule
    assert t.start_date is None and t.read_date is None
    assert settings.running_test() is None
    assert not settings.overrides


# -- copy: one sequence per industry, and a General one (Harry, 30 Sep 2026) ---------------------


def test_every_contactable_industry_has_a_draft_sequence(settings):
    industries = {c.industry for c in settings.copy}
    assert industries == ({i.industry for i in settings.industries} - UNREACHABLE) | {"General"}
    assert len(settings.copy) == len(industries) and all(c.role == "" for c in settings.copy)
    assert all(c.status == "draft" and c.approved_by == "" for c in settings.copy)  # only Harry approves copy
    # Every row passed QA in its current wording: editing data/copy.csv needs `copy qa` again.
    assert all(c.qa_current for c in settings.copy), [c.copy_version for c in settings.copy if not c.qa_current]
    assert all(c.copy_version.endswith("-v1") for c in settings.copy)


def test_every_sequence_passes_the_sheet_check(settings):
    from us_outbound.enrol import copy_desk

    bad = {c.copy_version: c.problems for c in copy_desk.check_all(settings) if not c.ok}
    assert bad == {}


def test_every_sequence_has_harry_s_shape(settings):
    for c in settings.copy:
        s1, s2, s3, s4 = (c.step(n).body for n in (1, 2, 3, 4))
        assert "{{opener}}" in s1 and "{{role_line}}" in s1, c.copy_version
        for heading in ("**What is Spill?**", "**Who is Spill for?**", "**What makes Spill unique**"):
            assert heading in s2, (c.copy_version, heading)
        assert "{{price_line}} We don't lock you in." in s2, c.copy_version
        assert "{{industry_url}}" in s1 and "{{demo_url}}" not in s1, c.copy_version  # the page, no demo ask
        assert "[trusted by over 50,000 employees]({{site_url}})" in s2, c.copy_version
        assert all(b.endswith("Best wishes,\n{{sender_first_name}}") for b in (s1, s2, s3, s4)), c.copy_version
        assert all("{{industry_url}}" not in b and "{{demo_url}}" in b for b in (s2, s3, s4)), c.copy_version
        assert len(s2) > max(len(s1), len(s3), len(s4)), c.copy_version  # the long form is email 2
        assert all(c.role_lines.get(r) for r in ("People leader", "Founder or executive", "Operations")), c.copy_version
