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
    assert tabs["Signals"][0]["weight"] == "15"


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
    assert raw["approver_slack_ids"] == ""
    assert "postal_address" not in raw and "privacy_url" not in raw  # retired (Harry, 1 Oct 2026)
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


# signal, weight, max_weight, action, suggests_angle, counts_for_days, active: SPEC 5's rows as the design
# review's Appendix A reworked them (docs/gtm-review/README.md, 1 Oct 2026), plus the build's two.
DEFAULT_SIGNALS = [
    ("Mental health support listed", 15, None, "Score", "Progressive employer", 540, True),  # was +25
    ("EAP named", 5, None, "Score", "Upgrade the EAP", 540, True),  # was +10
    ("Modern mental-health vendor named", 0, None, "Hold", "Switch from a competitor", 540, True),  # competitors only
    ("Wellbeing app or perk named", 5, None, "Score", "Progressive employer", 540, True),  # new: the complements
    ("Progressive benefits", 5, 15, "Score", "Progressive employer", 540, True),  # was +10 each, at most +30
    ("Culture or values page", 0, None, "Score", "Progressive employer", 540, False),  # was +10
    ("People leader in place", 10, None, "Score", "", 365, True),
    ("New People leader", 30, None, "Score", "Progressive employer", 90, True),
    ("First People hire", 25, None, "Score", "Growing team", 90, True),
    ("People role open", 15, None, "Score", "Growing team", 60, True),  # new
    ("Funding in the last 6 months", 20, None, "Score", "Growing team", 180, True),  # Recent funding, split
    ("Funding 6–12 months ago", 10, None, "Score", "Growing team", 365, True),
    ("Hiring and growth", 15, None, "Score", "Growing team", 90, True),
    ("Visited the US site", 35, None, "Score", "", 30, True),  # was +20
    ("Viewed US pricing or demo page", 25, None, "Score", "", 30, True),  # was +15
    ("Nonprofit budget", 15, None, "Score", "", 400, True),
    ("Nonprofit fiscal year ahead", 20, None, "Score", "", 1, True),
    ("Q4 plan-year window", 10, None, "Score", "", 1, False),  # inactive
    ("Team of 10–49", 15, None, "Score", "", 365, True),  # new: the size signal
    ("Team of 50–99", 10, None, "Score", "", 365, True),
    ("Layoffs", 0, None, "Suppress", "", 90, True),
    ("Named by Harry", 30, None, "Score", "", 365, True),  # build addition (Harry, 30 Sep 2026)
    ("Looks like Spill's customers", 4, None, "Score", "", 120, True),  # build addition (Harry, 1 Oct 2026)
]


def test_every_default_signal(settings):
    got = {s.signal: s for s in settings.signals}
    assert list(got) == [row[0] for row in DEFAULT_SIGNALS]
    for name, weight, max_weight, action, angle, days, active in DEFAULT_SIGNALS:
        s = got[name]
        assert (s.weight, s.max_weight, s.action, s.suggests_angle, s.counts_for_days, s.active) == (
            weight, max_weight, action, angle, days, active,
        ), name
        assert s.note, name  # every row says why it is set that way


def test_signal_sources_and_parsing(settings):
    got = {s.signal: s for s in settings.signals}
    assert got["Modern mental-health vendor named"].sources == ("clay_careers", "job_posts")
    assert got["Funding in the last 6 months"].sources == ("apollo_org", "clay_funding")
    # people_leader_count comes from apollo_people, so that source is listed too.
    assert got["First People hire"].sources == ("apollo_jobs", "apollo_people")
    assert got["First People hire"].condition.evaluate({"open_people_roles": 1, "people_leader_count": 0})
    assert got["Q4 plan-year window"].condition.evaluate({"month": 11})  # kept on the sheet, inactive
    assert got["Culture or values page"].condition.evaluate({"values_page": True})
    assert got["Looks like Spill's customers"].sources == ("lookalike",)
    for s in settings.signals:
        assert bool(s.terms) != s.is_condition, s.signal


def test_a_carrier_eap_is_counted_once(settings):
    """Appendix A: "EAP" and "employee assistance" were in both rows, so a carrier EAP alone scored 25 + 10."""
    got = {s.signal: s for s in settings.signals}
    mh, eap = got["Mental health support listed"], got["EAP named"]
    assert mh.sources == eap.sources == ("clay_careers", "job_posts")  # job posts read too
    assert not {t.casefold() for t in mh.terms} & {t.casefold() for t in eap.terms}
    assert "EAP" not in mh.terms and "employee assistance" not in mh.terms
    for vendor in ("Optum", "Carelon", "Cigna", "Aetna Resources For Living", "TELUS Health", "Health Advocate"):
        assert vendor in eap.terms, vendor
    # The medical carriers count only near words that make them the EAP, so the opener stays true.
    assert set(eap.context) == {"optum", "cigna", "carelon", "health advocate"}
    assert eap.opener == "I noticed your benefits page lists an employee assistance program."


def test_competitors_hold_and_complements_score(settings):
    got = {s.signal: s for s in settings.signals}
    hold, apps = got["Modern mental-health vendor named"], got["Wellbeing app or perk named"]
    assert hold.terms == ("Talkspace", "Lyra", "Modern Health", "Spring Health", "BetterUp", "Nivati", "Tava",
                          "Wellbound")
    assert apps.terms == ("Headspace", "Calm", "Wellhub", "Gympass")
    assert not set(hold.terms) & set(apps.terms) and hold.context == {}
    assert apps.context == {
        "headspace": ("for Work", "app", "subscription"),
        "calm": ("app", "premium", "business", "subscription"),
    }


def test_hiring_reads_apollo_jobs_and_funding_decays(settings):
    got = {s.signal: s for s in settings.signals}
    assert got["Hiring and growth"].sources == ("apollo_jobs", "apollo_org")  # apollo_jobs owns open_roles
    recent, older = got["Funding in the last 6 months"], got["Funding 6–12 months ago"]
    for days, want in ((1, (True, False)), (180, (True, False)), (181, (False, True)), (365, (False, True)),
                       (366, (False, False)), (539, (False, False))):
        values = {"days_since_funding": days}
        assert (recent.condition.evaluate(values), older.condition.evaluate(values)) == want, days
    assert "Recent funding" not in got


def test_size_rows_favor_10_to_99(settings):
    got = {s.signal: s for s in settings.signals}
    small, mid = got["Team of 10–49"], got["Team of 50–99"]
    assert small.sources == mid.sources == ("apollo_org",)
    for n, want in ((9, (False, False)), (10, (True, False)), (49, (True, False)), (50, (False, True)),
                    (99, (False, True)), (100, (False, False)), (249, (False, False))):
        assert (small.condition.evaluate({"employees": n}), mid.condition.evaluate({"employees": n})) == want, n


def test_openers_are_one_observed_fact_and_pass_the_copy_rules(settings):
    from us_outbound.enrol import render

    openers = {s.signal: s.opener for s in settings.signals if s.opener}
    # Harry, 1 Oct 2026: email 1 opens on a personal, relevant fact, so every signal with evidence
    # about the account has an opener, worded to read right to any of the three roles.
    assert openers == {
        "Mental health support listed": "I noticed your careers page mentions {evidence}.",
        "EAP named": "I noticed your benefits page lists an employee assistance program.",
        "Wellbeing app or perk named": "I noticed {evidence} is part of your benefits.",
        "Progressive benefits": "I noticed your benefits include {evidence}.",
        "New People leader": "I saw the team recently added a new People leader.",
        "First People hire": "I saw you're hiring your first People role.",
        "People role open": "I saw you're hiring for a People role right now.",
        "Funding in the last 6 months": "Congratulations on the recent funding round.",
        "Hiring and growth": "I saw the team has been growing.",
    }
    for opener in openers.values():
        filled = opener.replace("{evidence}", "parental leave")
        assert render.pick_opener(filled, sender_is_harry=False, demo_host="Harry Dryden") == (filled, "")


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
    # Legal Teams on from launch at priority 2, beside the agencies (design review Appendix A.4, 1 Oct 2026).
    [legal] = by_group["Legal Teams"]
    assert (legal.industry, legal.active, legal.priority) == ("Legal Teams", True, 2)
    prof = {i.industry: i for i in by_group["Professional Services"]}
    assert "541214" in prof["CPA firms"].exclude_naics and not any(i.active for i in prof.values())
    digital = settings.industry("Digital health")
    assert digital.industry_group == "Healthcare" and digital.active and digital.priority == 1
    active_groups = {i.industry_group for i in settings.industries if i.active}
    assert active_groups == {"Technology & Startups", "Marketing & Creative Agencies", "Legal Teams", "Healthcare"}
    assert sum(i.active for i in settings.industries) == 28  # 17 tech labels (Digital health moved), 10 agencies, Legal


IT_SERVICES = ("541512", "541513", "541519")  # computer systems design, facilities management, other IT services


def test_tech_rows_exclude_it_services(settings):
    """Appendix A.4: the tech labels share NAICS 5415, which brings in IT services, MSPs and IT staffing."""
    tech = [i for i in settings.industries if i.naics_prefixes and (
        i.industry_group == "Technology & Startups" or i.industry == "Digital health")]
    assert len(tech) == 17
    for i in tech:
        assert i.exclude_naics == IT_SERVICES, i.industry
    others = [i for i in settings.industries if i not in tech]
    assert not any(set(IT_SERVICES) & set(i.exclude_naics) for i in others)


def test_focus_at_launch(settings, tabs):
    """Appendix A.4: Tech 50%, Agencies 30%, Legal 20%."""
    assert [(f.industry_group, f.share) for f in settings.focus] == [
        ("Technology & Startups", 0.5), ("Marketing & Creative Agencies", 0.3), ("Legal Teams", 0.2)]
    assert all(r["note"] for r in tabs["Focus"])


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
    assert notes["Legal Teams"].startswith("On from launch") and "Appendix A.4" in notes["Legal Teams"]
    assert "541512" in notes["Fintech"] and "541512" not in notes["Advertising agencies"]
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


def _contact_order(roles, employees: int) -> list[tuple[int, str, str]]:
    """Who to contact first at a company size, from the Roles tab alone: (rank, row, the copy it gets)."""
    return sorted((r.order_at(employees), r.role, r.writes_as) for r in roles if r.order_at(employees) is not None)


def test_roles_encode_who_to_contact_first(settings):
    """Harry, 1 Oct 2026: the size-band order, led by seniority; Finance never contacted."""
    assert _contact_order(settings.roles, 30) == [
        (1, "Founder or executive", "Founder or executive"),
        (1, "Partner at a professional firm", "Founder or executive"),
        (2, "People leader", "People leader"),
        (3, "Operations", "Operations"),
        (4, "Office or firm administrator", "Operations"),
    ]
    assert _contact_order(settings.roles, 120) == [
        (1, "People leader", "People leader"),
        (2, "Founder or executive", "Founder or executive"),
        (2, "Partner at a professional firm", "Founder or executive"),
        (3, "Operations", "Operations"),
        (4, "HR manager", "People leader"),
        (5, "Office or firm administrator", "Operations"),
    ]
    assert _contact_order(settings.roles, 49) == _contact_order(settings.roles, 10)
    assert _contact_order(settings.roles, 249) == _contact_order(settings.roles, 50)
    finance = next(r for r in settings.roles if r.role == "Finance")
    assert not finance.order and not finance.first_choice_for_size and not finance.fallback_order
    assert "CFO" in finance.titles and "Controller" in finance.titles
    by_role = {r.role: r for r in settings.roles}
    assert {r.writes_as for r in settings.roles if r.order} == {"People leader", "Founder or executive", "Operations"}
    people = by_role["People leader"]
    assert {"CHRO", "Chief People Officer", "VP of People", "Head of HR", "Director of People", "HR Director",
            "People and Culture Lead", "People and Culture Director"} <= set(people.titles)
    assert "HR Manager" not in people.titles and "HR Manager" in by_role["HR manager"].titles
    assert {"Owner", "Principal", "Partner", "General Manager"} <= set(by_role["Founder or executive"].titles
                                                                       + by_role["Partner at a professional firm"].titles)
    assert by_role["Partner at a professional firm"].industry_groups == ("Legal Teams", "Professional Services")
    assert {"Office Manager", "Firm Administrator"} <= set(by_role["Office or firm administrator"].titles)
    assert not {"Office Manager", "Firm Administrator"} & set(by_role["Operations"].titles)


def test_every_role_row_explains_itself(tabs):
    assert all(r["note"] for r in tabs["Roles"])


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


# -- copy: one sequence per industry and role, and General ones (Harry, 30 Sep and 1 Oct 2026) ----

COPY_ROLES = {"People leader": "people", "Founder or executive": "founder", "Operations": "ops"}


def test_every_contactable_industry_has_a_draft_sequence_for_each_role(settings):
    industries = {c.industry for c in settings.copy}
    assert industries == ({i.industry for i in settings.industries} - UNREACHABLE) | {"General"}
    by_industry = {i: sorted(c.role for c in settings.copy if c.industry == i) for i in industries}
    assert all(roles == sorted(COPY_ROLES) for roles in by_industry.values()), by_industry
    assert len(settings.copy) == 3 * len(industries)
    assert all(c.copy_version.endswith(f"-{COPY_ROLES[c.role]}-v1") for c in settings.copy)
    assert all(c.status == "draft" and c.approved_by == "" for c in settings.copy)  # only Harry approves copy
    # Every row passed QA in its current wording: editing data/copy.csv needs `copy qa` again.
    assert all(c.qa_current for c in settings.copy), [c.copy_version for c in settings.copy if not c.qa_current]


def test_every_sequence_passes_the_sheet_check(settings):
    from us_outbound.enrol import copy_desk

    bad = {c.copy_version: c.problems for c in copy_desk.check_all(settings) if not c.ok}
    assert bad == {}


def test_every_sequence_has_harry_s_shape(settings):
    for c in settings.copy:
        s1, s2, s3, s4 = (c.step(n).body for n in (1, 2, 3, 4))
        # Written for its role, so no role line; email 1 informs and asks only for a visit to the page.
        assert "{{opener}}" in s1 and "{{role_line}}" not in s1, c.copy_version
        for heading in ("**What is Spill?**", "**Who is Spill for?**", "**What makes Spill unique**"):
            assert heading in s2, (c.copy_version, heading)
        assert "{{price_line}} We don't lock you in." in s2, c.copy_version
        assert "{{industry_url}}" in s1 and "{{demo_url}}" not in s1, c.copy_version  # the page, no demo ask
        assert "[trusted by over 50,000 employees]({{site_url}})" in s2, c.copy_version
        assert all(b.endswith("Best wishes,\n{{sender_first_name}}") for b in (s1, s2, s3, s4)), c.copy_version
        assert all("{{industry_url}}" not in b and "{{demo_url}}" in b for b in (s2, s3, s4)), c.copy_version
        assert len(s2) > max(len(s1), len(s3), len(s4)), c.copy_version  # the long form is email 2
        assert "free trial" in s4.lower() and all("free trial" not in b.lower() for b in (s1, s2, s3)), c.copy_version
        assert not any(c.role_lines.values()), c.copy_version
