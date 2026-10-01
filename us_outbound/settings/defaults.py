"""The settings sheet "US Outbound – Settings" as first created: headers and SPEC 5 defaults.

COLUMNS gives each tab's headers in SPEC 5 order. A "note" column is added at the end
of General, Angles, Industries and Roles (SPEC gives one only on Signals, States and
Overrides) so every row can say why it is set that way.

default_tabs() returns the rows exactly as Sheets.read_tabs would return them: every
value a str, "" when blank. Lists inside a cell use the separators validate.py reads:
sources and approver ids with commas, terms / titles / NAICS / keywords with semicolons.
Copy rows load as draft: only Harry approves copy (SPEC 5, 10).
"""

from __future__ import annotations

import csv
from pathlib import Path

from us_outbound.settings.model import TABS

COLUMNS: dict[str, list[str]] = {
    "General": ["key", "value", "note"],
    "Signals": [
        "signal", "source", "looks_for", "context_rule", "weight", "max_weight", "action",
        "suggests_angle", "opener", "counts_for_days", "active", "note",
    ],
    "Angles": ["angle", "order", "argument", "default_opener", "landing_page_override", "active", "note"],
    "Industries": [
        "industry", "industry_group", "active", "naics_prefixes", "exclude_naics", "apollo_keywords",
        "landing_page_url", "proof_point", "priority",
        # The industry's spill.chat page, as material for its emails (Harry, 30 Sep 2026); optional columns.
        "page_blurb", "page_intro", "page_ticks", "page_challenges", "page_stats", "page_benefits",
        "page_features", "page_faqs", "page_customers", "note",
    ],
    "States": ["state", "active", "note"],
    "Roles": ["role", "titles", "first_choice_for_size", "fallback_order", "note"],
    # One row per industry (and optionally role), the four emails across (Harry, 30 Sep 2026).
    "Copy": [
        "copy_version", "industry", "role", "status", "approved_by", "qa", "qa_notes",
        "s1_subject", "s1_body", "s2_subject", "s2_body", "s3_subject", "s3_body", "s4_subject", "s4_body",
        "people_leader_line", "founder_line", "operations_line", "sources", "note",
    ],
    "Mailboxes": [
        "address", "instantly_account_id", "domain", "provider", "owner_name", "owner_role", "signature",
        "status", "daily_cap", "added_on", "retire_after",
    ],
    "Overrides": ["domain", "field", "value", "note"],
    "Tests": [
        "test_id", "hypothesis", "version_a", "version_b", "accounts_per_version", "start_date", "read_date",
        "decision_rule", "status", "result",
    ],
    # Added 30 Sep 2026 (Harry); a sheet without them reads as if they were empty.
    "Focus": ["industry_group", "share", "note"],
    "Named accounts": ["domain", "name", "note"],
}
assert tuple(COLUMNS) == TABS

# USPS codes: the 50 states and DC.
US_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
)

HARRY_TO_FILL = "Harry to fill"

# -- General -----------------------------------------------------------------

_GENERAL: list[tuple[str, str, str]] = [
    ("live_sending", "no", "Live needs yes here and the --live flag. Stays no until Harry signs off (SPEC 0.3)."),
    ("weekly_enrol_cap", "150", "Most new accounts enrolled in a week (Monday to Sunday, UK time). Each send day takes what is left of it ÷ the send days left in the week."),
    ("control_share", "0.15", "Share of each day's enrollment taken from the Control tier."),
    ("priority_threshold", "50", "Score at or above this is Priority."),
    ("standard_threshold", "20", "Score at or above this is Standard; below it is Control."),
    ("score_cap", "100", "Cap on an account's total score."),
    (
        "clay_monthly_credits",
        "2000",
        "Clay credits the jobs may spend each calendar month (UK time), about 500 a week. "
        "Each weekday may use what is left ÷ the weekdays left. 0 means no Clay calls.",
    ),
    (
        "apollo_monthly_credits",
        "2000",
        "Apollo credits the jobs may spend each calendar month (UK time), about 500 a week. "
        "Each weekday may use what is left ÷ the weekdays left.",
    ),
    ("apollo_floor", "5000", "New verification stops when Apollo credits fall below this."),
    (
        "approver_slack_ids",
        "",
        "Slack user ids allowed to approve replies, comma-separated. Harry is the only approver; "
        "fill in his id once the Slack app is installed.",
    ),
    ("escalation_email", "harry@spill.chat", "Where human-in-the-loop items go after escalation_hours."),
    ("escalation_hours", "24", "Hours before an open item is emailed to escalation_email."),
    ("alert_channel", "#us-outbound", "Alerts, approvals, the daily post and the Monday readout."),
    ("dev_channel", "#us-outbound-dev", "The only channel dry-run posts to. Added by the build."),
    ("booking_link", "https://meetings.hubspot.com/harry336/us-demo-link", "Harry's own meeting link, for replies."),
    ("booking_page", "https://www.spill.chat/us/book-demo",
     "The demo page every email links as its call to action ({{demo_url}}; Harry, 30 Sep 2026)."),
    ("site_url", "https://www.spill.chat/us",
     "Spill's US site ({{site_url}}): the \"trusted by over 50,000 employees\" link, and the page linked when an industry has none (Harry, 1 Oct 2026)."),
    ("price_from", "195",
     "The starting price in every email: \"Plans start from $195 a month for the whole team, on a rolling 30-day contract\" (Harry, 1 Oct 2026)."),
    ("demo_host", "Harry Dryden", "Every demo is booked with this person."),
    ("send_window", "Mon–Fri 09:00–16:00 America/New_York", "Days, hours and time zone for sends."),
    (
        "blackout_dates",
        "2026-11-23..2026-11-27, 2026-12-18..2027-01-04",
        "No enrollment on these dates: comma-separated YYYY-MM-DD..YYYY-MM-DD ranges (Thanksgiving week; the holidays).",
    ),
    ("recontact_person_months", "12", "Months before a person may be contacted again."),
    ("recontact_account_months", "6", "Months before an account may be contacted again."),
    (
        "stop_rule_accounts",
        "1500",
        "Stop rule: if the first stop_rule_accounts accounts bring fewer than stop_rule_meetings meetings, "
        "enrollment pauses for a profile and copy review (SPEC 12).",
    ),
    ("stop_rule_meetings", "5", "See stop_rule_accounts."),
    (
        "stop_rule_bounce_rate",
        "0.03",
        "Stop rule: enrollment also pauses when more than this share of the accounts sent step 1 in the last "
        "30 days bounced (at least 100 accounts; kill_rules, hourly). Added by the build.",
    ),
    (
        "stop_rule_complaint_rate",
        "0.003",
        "Stop rule: and when more than this share of them made a spam complaint (Google's 0.3% line). "
        "Added by the build.",
    ),
    (
        "optout_tested",
        "no",
        "yes once a test send to a seed inbox shows Instantly's {{unsubscribe}} link renders and works in html and "
        "text (go-live blocker; `us-outbound golive` checks it). Added by the build.",
    ),
    ("hubspot_pipeline", "Spill 3.0", "Deals go in this pipeline."),
    ("hubspot_pipeline_id", "", "Looked up through the HubSpot API in phase 0."),
    ("hubspot_deal_stage", "", "SPEC: the first stage of Spill 3.0. Its label, looked up in phase 0."),
    ("hubspot_deal_stage_id", "", "Looked up through the HubSpot API in phase 0."),
    ("hubspot_owner_id", "", "Harry's HubSpot owner id, looked up in phase 0. Added by the build."),
    ("clay_accounts_function_id", "", "Clay function \"US Outbound – Accounts\", once built in phase 0."),
    ("clay_contacts_function_id", "", "Clay function \"US Outbound – Contacts\", once built in phase 0."),
    ("clay_credits_per_account", "0", "Estimate until measured on the first 100 accounts (SPEC 8)."),
    ("apollo_credits_per_account", "1", "Estimate until measured."),
    ("claude_model", "claude-opus-5-5",
     "Writing: drafts copy and reply drafts (Harry, 30 Sep 2026: Opus constructs the emails)."),
    ("claude_task_model", "claude-sonnet-5-5",
     "Well-defined tasks: checks drafted copy (copy qa) and classifies replies. Both models share the cap below."),
    ("email_format", "html",
     "html: emails with embedded links and bullets; text: plain text with links written out. Tracking stays off."),
    ("claude_monthly_cap_usd", "10", "Hard cap on Claude API spend; SPEC 1.1 allows at most $10 a month."),
]

# -- Signals (SPEC 5, every one editable) --------------------------------------

_VENDOR_CONTEXT = "Headspace: for Work, app, subscription; Calm: app, premium, business, subscription"

# signal, source, looks_for, context_rule, weight, max_weight, action, suggests_angle, counts_for_days, note
_SIGNALS: list[tuple[str, str, str, str, str, str, str, str, str, str]] = [
    (
        "Mental health support listed", "clay_careers",
        "mental health; EAP; employee assistance; therapy; counseling; counselling; wellbeing support; "
        "well-being support",
        "", "25", "", "Score", "Progressive employer", "540",
        "Listed mental-health support is a positive sign: wellbeing is part of the employer's brand. "
        "Never an exclusion. SPEC 5 default.",
    ),
    (
        "EAP named", "clay_careers",
        "EAP; employee assistance; ComPsych; GuidanceResources; Magellan",
        "", "10", "", "Score", "Upgrade the EAP", "540",
        "A named EAP suggests the Upgrade the EAP angle. SPEC 5 default.",
    ),
    (
        "Modern mental-health vendor named", "clay_careers, job_posts",
        "Talkspace; Lyra; Modern Health; Spring Health; Headspace; Calm; BetterUp; Nivati; Tava; Wellhub; "
        "Gympass; Wellbound; Justworks Plus",
        _VENDOR_CONTEXT, "0", "", "Hold", "Switch from a competitor", "540",
        "Held for review: they already have a modern vendor. Headspace and Calm count only near the "
        "context terms. SPEC 5 default.",
    ),
    (
        "Progressive benefits", "clay_careers, job_posts",
        "wellness stipend; mental health day; unlimited PTO; four-day week; 4-day week; parental leave; "
        "sabbatical; 100% employer-paid",
        "", "10", "30", "Score", "Progressive employer", "540",
        "+10 for each benefit found, at most +30. SPEC 5 default.",
    ),
    (
        "Culture or values page", "clay_careers", "values_page = true",
        "", "10", "", "Score", "Progressive employer", "540", "SPEC 5 default.",
    ),
    (
        "People leader in place", "apollo_people", "people_leader_count >= 1",
        "", "10", "", "Score", "", "365", "SPEC 5 default.",
    ),
    (
        "New People leader", "apollo_people", "people_leader_days_in_title <= 90",
        "", "30", "", "Score", "Progressive employer", "90", "SPEC 5 default.",
    ),
    (
        "First People hire", "apollo_jobs, apollo_people", "open_people_roles >= 1 AND people_leader_count = 0",
        "", "25", "", "Score", "Growing team", "90",
        "SPEC 5 default. SPEC lists apollo_jobs; apollo_people is added because people_leader_count comes from it.",
    ),
    (
        "Recent funding", "apollo_org, clay_funding", "days_since_funding <= 540",
        "", "20", "", "Score", "Growing team", "540", "SPEC 5 default.",
    ),
    (
        "Hiring and growth", "apollo_org", "open_roles >= 3 OR headcount_growth_12m >= 0.10",
        "", "15", "", "Score", "Growing team", "90", "SPEC 5 default.",
    ),
    (
        "Visited the US site", "site_visits", "us_visits_30d >= 1",
        "", "20", "", "Score", "", "30", "SPEC 5 default.",
    ),
    (
        "Viewed US pricing or demo page", "site_visits", "pricing_or_demo_visits_30d >= 1",
        "", "15", "", "Score", "", "30", "SPEC 5 default.",
    ),
    (
        "Nonprofit budget", "irs_bmf", "revenue >= 2000000 AND revenue <= 50000000",
        "", "15", "", "Score", "", "400", "SPEC 5 default.",
    ),
    (
        "Nonprofit fiscal year ahead", "irs_bmf",
        "days_to_fiscal_year_start >= 60 AND days_to_fiscal_year_start <= 120",
        "", "20", "", "Score", "", "1", "SPEC 5 default. Recomputed daily.",
    ),
    (
        "Q4 plan-year window", "calendar", "month in [10, 11, 12]",
        "", "10", "", "Score", "", "1", "SPEC 5 default. Recomputed daily.",
    ),
    (
        "Layoffs", "layoffs", "days_since_layoff <= 90",
        "", "0", "", "Suppress", "", "90",
        "Suppresses the domain for 90 days after a layoff. SPEC 5 default.",
    ),
    (
        "Named by Harry", "named", "named = true",
        "", "30", "", "Score", "", "365",
        "Companies on the Named accounts tab (Harry, 30 Sep 2026). They pass every other check as usual.",
    ),
]

# -- Angles (SPEC 5, in order) -------------------------------------------------

# angle, argument, default_opener, active, note
_ANGLES: list[tuple[str, str, str, str, str]] = [
    (
        "Upgrade the EAP",
        "Your team already has an EAP, which says you take this seriously. This is the version they'll use: "
        "same-day counseling in Slack or Teams, and 30% of employees use Spill.",
        "I saw your team already has an employee assistance program.",
        "yes", "",
    ),
    (
        "Progressive employer",
        "You already invest in your people. This is the benefit they'll actually use.",
        "It's clear you already invest in your people.",
        "yes", "",
    ),
    (
        "Growing team",
        "Hiring fast means onboarding stress. Keep the people you just hired, for a flat fee from $195 a month.",
        "It looks like your team is growing fast.",
        "yes", "",
    ),
    (
        "General",
        "Mental health support your team will use: same-day counseling for one flat monthly fee.",
        "I wanted to share a simple way to give your team mental health support.",
        "yes", "Control-tier accounts always get this angle.",
    ),
    (
        "Switch from a competitor",
        "About a third of the price, 50-minute sessions rather than 30, and no billable maximums that run out "
        "in August.",
        "I saw your team already offers a mental health app.",
        "no", "Inactive in v1 (phase 4).",
    ),
]

# -- Industries (SPEC 5; Harry, 30 Sep 2026: all 108 of the website's industry pages) ----------
# data/industries.csv holds the tab as first loaded: each page's label, its group (the website's
# 15 hub pages), NAICS and Apollo keywords, the page link and the page's copy. The build makes
# it from the website's export (docs/pipeline.md, "Industries"); the sheet is then Harry's.

DATA_DIR = Path(__file__).resolve().parent / "data"
INDUSTRIES_FILE = DATA_DIR / "industries.csv"
COPY_FILE = DATA_DIR / "copy.csv"


def _csv_rows(path: Path, tab: str) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    cols = COLUMNS[tab]
    return [{c: str(r.get(c) or "") for c in cols} for r in rows]


def _industries() -> list[dict[str, str]]:
    return _csv_rows(INDUSTRIES_FILE, "Industries")


# -- States ----------------------------------------------------------------------

_ACTIVE_STATES = {"NY", "MA", "NJ", "PA", "IL", "GA", "TX"}
_STATE_NOTES = {
    "FL": "off until Harry confirms",
    **{s: "Wave 2" for s in ("NC", "VA", "MD", "OH", "MN")},
    "CA": "never (also enforced in code)",
    "WA": "never (also enforced in code)",
}

# -- Roles (SPEC 5, "Who to contact first") ---------------------------------------

_ROLES: list[tuple[str, str, str, str, str]] = [
    (
        "People leader",
        "Head of People; VP People; Chief People Officer; People Ops Lead; HR Director; Director of HR; "
        "HR Manager; People & Culture",
        "50-249", "", "First choice at 50 to 249 staff.",
    ),
    (
        "Founder or executive",
        "CEO; Founder; Co-founder; President; Managing Partner; Managing Director; Executive Director",
        "10-49", "50-249:3", "First choice at 10 to 49 staff; third at 50 to 249.",
    ),
    (
        "Operations",
        "COO; Chief of Staff; Head of Operations; Director of Operations; Office Manager; Firm Administrator",
        "", "10-49:2; 50-249:2", "Second choice at every size.",
    ),
    ("Finance", "CFO; Finance Director; Head of Finance; Controller", "", "", "not contacted"),
]

# -- Copy (drafts for Harry to approve; SPEC 10; Harry, 30 Sep 2026) --------------------------
# data/copy.csv: one four-email sequence per industry and a General one, drafted by Claude from
# templates/copy/style.md, facts.md and each industry's page, then checked by a second model
# (docs/pipeline.md, "Copy"). Every row loads as draft: only Harry approves copy (SPEC 5, 10).
# The signature and (email 1) Article 14 notice are added at render time, and Instantly's unsubscribe
# line by the campaign's step template; none is stored here.


def _copy() -> list[dict[str, str]]:
    rows = _csv_rows(COPY_FILE, "Copy")
    for r in rows:
        r["status"], r["approved_by"] = "draft", ""
    return rows


# -- Mailboxes (SPEC 5) --------------------------------------------------------------

_MAILBOXES = [
    ("hannah@meetspill.org", "meetspill.org", "Hannah Spalding"),
    ("harry@meetspill.org", "meetspill.org", "Harry Dryden"),
    ("sam@meetspill.org", "meetspill.org", "Sam Jackson"),
    ("harry@tryspill.org", "tryspill.org", "Harry Dryden"),
]

# -- Tests (SPEC 12, the first test) ------------------------------------------------

_FIRST_TEST = {
    "test_id": "t1-eap-opener",
    "hypothesis": (
        "On accounts where an EAP is named, an Upgrade the EAP opener gets a higher reply rate than a General "
        "opener."
    ),
    "version_a": "eap-v1",
    "version_b": "general-v1",
    "accounts_per_version": "400",
    "start_date": "",
    "read_date": "",
    "decision_rule": (
        "reply rate, human replies within 28 days of step 1 ÷ accounts with step 1 delivered; "
        "detects a 2× difference"
    ),
    "status": "planned",
    "result": "",
}


def _rows(tab: str, values: list[tuple[str, ...]]) -> list[dict[str, str]]:
    cols = COLUMNS[tab]
    return [dict(zip(cols, v, strict=True)) for v in values]


def default_tabs() -> dict[str, list[dict[str, str]]]:
    """Every tab's rows as the sheet is created with them (SPEC 5 defaults), all strings."""
    tabs: dict[str, list[dict[str, str]]] = {
        "General": _rows("General", _GENERAL),
        "Signals": [
            {
                "signal": s, "source": src, "looks_for": looks, "context_rule": ctx, "weight": w,
                "max_weight": mx, "action": action, "suggests_angle": angle, "opener": "",
                "counts_for_days": days, "active": "yes", "note": note,
            }
            for s, src, looks, ctx, w, mx, action, angle, days, note in _SIGNALS
        ],
        "Angles": [
            {
                "angle": a, "order": str(i), "argument": arg, "default_opener": opener,
                "landing_page_override": "", "active": active, "note": note,
            }
            for i, (a, arg, opener, active, note) in enumerate(_ANGLES, start=1)
        ],
        "Industries": _industries(),
        "States": [
            {"state": s, "active": "yes" if s in _ACTIVE_STATES else "no", "note": _STATE_NOTES.get(s, "")}
            for s in US_STATES
        ],
        "Roles": _rows("Roles", _ROLES),
        "Copy": _copy(),
        "Mailboxes": [
            {
                "address": address, "instantly_account_id": "", "domain": domain, "provider": "",
                "owner_name": owner, "owner_role": "", "signature": f"{owner}\nSpill\nspill.chat/us",
                "status": "Warming", "daily_cap": "30", "added_on": "", "retire_after": "",
            }
            for address, domain, owner in _MAILBOXES
        ],
        "Overrides": [],
        "Tests": [dict(_FIRST_TEST)],
        "Focus": [],
        "Named accounts": [],
    }
    for tab, rows in tabs.items():
        for row in rows:
            assert list(row) == COLUMNS[tab], (tab, list(row))
    return tabs
