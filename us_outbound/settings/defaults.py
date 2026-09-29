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
        "landing_page_url", "proof_point", "priority", "note",
    ],
    "States": ["state", "active", "note"],
    "Roles": ["role", "titles", "first_choice_for_size", "fallback_order", "note"],
    "Copy": ["copy_version", "angle", "step", "subject", "body", "status", "approved_by", "sources"],
    "Mailboxes": [
        "address", "instantly_account_id", "domain", "provider", "owner_name", "owner_role", "signature",
        "status", "daily_cap", "added_on", "retire_after",
    ],
    "Overrides": ["domain", "field", "value", "note"],
    "Tests": [
        "test_id", "hypothesis", "version_a", "version_b", "accounts_per_version", "start_date", "read_date",
        "decision_rule", "status", "result",
    ],
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
    ("daily_enrol_cap", "30", "Most new accounts enrolled in a working day (SPEC 9)."),
    ("control_share", "0.15", "Share of each day's enrollment taken from the Control tier."),
    ("priority_threshold", "50", "Score at or above this is Priority."),
    ("standard_threshold", "20", "Score at or above this is Standard; below it is Control."),
    ("score_cap", "100", "Cap on an account's total score."),
    (
        "clay_monthly_credits",
        "0",
        "SPEC: a quarter of the Clay pool, until credits per account are measured. "
        "0 (no Clay spend) until Harry confirms the pool in phase 0.",
    ),
    ("apollo_monthly_credits", "1500", "Monthly Apollo credit budget."),
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
    ("booking_link", "https://meetings.hubspot.com/harry336/us-demo-link", "Demo link in emails and replies."),
    ("booking_page", "https://www.spill.chat/us/book-demo", "Demo booking page on the website."),
    ("demo_host", "Harry Dryden", "Every demo is booked with this person."),
    (
        "postal_address",
        "",
        "Spill's UK registered address, for the email footer. " + HARRY_TO_FILL + "; sends are blocked while blank.",
    ),
    (
        "privacy_url",
        "",
        "The US privacy and opt-out page. " + HARRY_TO_FILL + "; sends are blocked while blank.",
    ),
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
    ("hubspot_pipeline", "Spill 3.0", "Deals go in this pipeline."),
    ("hubspot_pipeline_id", "", "Looked up through the HubSpot API in phase 0."),
    ("hubspot_deal_stage", "", "SPEC: the first stage of Spill 3.0. Its label, looked up in phase 0."),
    ("hubspot_deal_stage_id", "", "Looked up through the HubSpot API in phase 0."),
    ("hubspot_owner_id", "", "Harry's HubSpot owner id, looked up in phase 0. Added by the build."),
    ("clay_accounts_function_id", "", "Clay function \"US Outbound – Accounts\", once built in phase 0."),
    ("clay_contacts_function_id", "", "Clay function \"US Outbound – Contacts\", once built in phase 0."),
    ("clay_credits_per_account", "0", "Estimate until measured on the first 100 accounts (SPEC 8)."),
    ("apollo_credits_per_account", "1", "Estimate until measured."),
    ("claude_model", "claude-haiku-4-5", "SPEC: a current fast Claude model."),
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

# -- Industries (SPEC 5: website labels grouped under the industry pages) -------

_TECH = "Technology & Startups"
_MARKETING = "Marketing & Creative Agencies"
_NONPROFITS = "Nonprofits"
_LEGAL = "Legal Teams"
_PROFESSIONAL = "Professional Services"

# label -> one Apollo keyword ("plus a keyword per label").
_TECH_LABELS: list[tuple[str, str]] = [
    ("Technology & Startups", "software"),
    ("Startups", "startup"),
    ("Fintech", "fintech"),
    ("Healthtech", "healthtech"),
    ("Insurtech", "insurtech"),
    ("Edtech", "edtech"),
    ("Proptech", "proptech"),
    ("Legaltech", "legal tech"),
    ("Adtech & martech", "adtech"),
    ("Digital health", "digital health"),
    ("AI & deep tech", "artificial intelligence"),
    ("Cybersecurity", "cybersecurity"),
    ("Agritech", "agtech"),
    ("Gametech", "gaming technology"),
    ("Cleantech", "cleantech"),
    ("Traveltech", "travel technology"),
    ("Games studios", "video games"),
]
_MARKETING_LABELS = [
    "Marketing & Creative Agencies", "Marketing agencies", "Advertising agencies", "Creative & design agencies",
    "PR agencies", "Content agencies", "UX & product design agencies", "Events & experiential agencies",
    "Production studios", "Publishers",
]
_NONPROFIT_LABELS = [
    "Nonprofits", "Human rights", "Disability organizations", "Animal welfare", "Social welfare",
    "Youth development", "Community development", "International aid & relief", "Environmental nonprofits",
    "Health & medical nonprofits", "Arts & culture", "Churches & religious organizations", "Emergency & rescue",
]
_NONPROFITS_OFF = {"Churches & religious organizations", "Emergency & rescue"}
_PROFESSIONAL_LABELS = [
    "Professional Services", "CPA firms", "Management consulting", "Architecture studios",
    "Engineering & design firms", "Research & market intelligence", "Staffing agencies", "HR consulting",
]
# Groups that are off in v1. SPEC names only the groups ("all their website labels"),
# so each starts as one row named after its group; Harry adds the labels.
_OFF_GROUPS = [
    "Financial Services", "Healthcare", "Senior Care & Home Care", "Education", "Hospitality",
    "Retail & E-commerce", "Construction & Trades", "Manufacturing & Industrial", "Fitness & Recreation",
]

_FILL_NOTE = HARRY_TO_FILL + ": landing_page_url and proof_point (a named US customer, or a UK analogue labeled UK)."


def _industry(label: str, group: str, active: bool, naics: str, exclude: str, keywords: str,
              priority: int, note: str) -> dict[str, str]:
    return {
        "industry": label,
        "industry_group": group,
        "active": "yes" if active else "no",
        "naics_prefixes": naics,
        "exclude_naics": exclude,
        "apollo_keywords": keywords,
        "landing_page_url": "",
        "proof_point": "",
        "priority": str(priority),
        "note": note,
    }


def _industries() -> list[dict[str, str]]:
    rows = []
    for label, keyword in _TECH_LABELS:
        rows.append(_industry(label, _TECH, True, "5112; 513210; 5415; 518210", "", keyword, 1,
                              "On from 26 Oct. " + _FILL_NOTE))
    for label in _MARKETING_LABELS:
        rows.append(_industry(label, _MARKETING, True, "5418; 541430; 541613; 5121; 5111", "", "", 2,
                              "On from 26 Oct. " + _FILL_NOTE))
    for label in _NONPROFIT_LABELS:
        if label in _NONPROFITS_OFF:
            note = "off"
        else:
            note = "January. Found through the IRS Business Master File (501(c)(3) by NTEE), matched to Apollo. " + _FILL_NOTE
        rows.append(_industry(label, _NONPROFITS, False, "813; 624", "", "", 3, note))
    rows.append(_industry(_LEGAL, _LEGAL, False, "541110", "", "", 4, "January. " + _FILL_NOTE))
    for label in _PROFESSIONAL_LABELS:
        note = {"Staffing agencies": "off", "HR consulting": "never (HR consultancies are a hard exclusion)"}.get(
            label, "After January. " + _FILL_NOTE
        )
        rows.append(_industry(label, _PROFESSIONAL, False, "5412; 5416; 54131; 54133; 5419", "541214; 541612",
                              "", 5, note))
    for group in _OFF_GROUPS:
        rows.append(_industry(group, group, False, "", "", "", 9, "Off. Harry to add the website labels."))
    return rows


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

# -- Copy (drafts for Harry to approve; SPEC 10) -----------------------------------
# The footer (sender, postal address, advertisement line, opt-out, privacy link and, on
# step 1, where the data came from) is added at render time, never stored here.

_SUBJECTS = {
    1: "Mental health support for the {{company}} team",
    2: "Same-day counseling for {{company}}",
    3: "Quick question",
    4: "Closing the loop",
}

_STEP1_ARGUMENT = {
    "general-v1": (
        "Spill is mental health support your team will use: same-day counseling with registered counselors, "
        "booked right in Slack or Teams, for one flat monthly fee. 30% of employees use Spill."
    ),
    "eap-v1": (
        "Having an EAP says you take this seriously. Spill is the version your team will use: same-day "
        "counseling with registered counselors, booked right in Slack or Teams, and 30% of employees use Spill."
    ),
}
_STEP2_LEAD = {
    "general-v1": "Following up on my note about mental health support for the {{company}} team.",
    "eap-v1": "Following up on my note about Spill and the benefits your team already has.",
}
_STEP2_ARGUMENT = {
    "general-v1": (
        "Your team books a registered counselor in a couple of clicks, often for the same day, without leaving "
        "Slack or Teams. {{price_line}}"
    ),
    "eap-v1": (
        "Spill works alongside the benefits you already offer: your team books a registered counselor in Slack "
        "or Teams, often for the same day. {{price_line}}"
    ),
}


def _body(*paragraphs: str) -> str:
    return "\n\n".join(paragraphs)


def _copy_rows(version: str, angle: str) -> list[dict[str, str]]:
    bodies = {
        1: _body("Hi {{first_name}},", "{{opener}}", "{{legal_overlay}}", _STEP1_ARGUMENT[version], "{{ask}}"),
        2: _body(
            "Hi {{first_name}},",
            _STEP2_LEAD[version],
            "{{proof}}",
            _STEP2_ARGUMENT[version],
            "If you'd like to see how it works, {{demo_line}}",
        ),
        3: _body("Hi {{first_name}},", "Is mental health support for your team in {{place}} on your list right now?"),
        4: _body(
            "Hi {{first_name}},",
            "I haven't heard back, so I'll leave it here. If the timing is wrong, no problem at all.",
            "If it would help to have something to share with the team later, reply \"one-pager\" and I'll send "
            "over our one-page summary of Spill.",
        ),
    }
    return [
        {
            "copy_version": version,
            "angle": angle,
            "step": str(step),
            "subject": _SUBJECTS[step],
            "body": bodies[step],
            "status": "draft",
            "approved_by": "",
            "sources": "",
        }
        for step in (1, 2, 3, 4)
    ]


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
        "reply rate, human replies within 21 days of step 1 ÷ accounts with step 1 delivered; "
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
        "Copy": _copy_rows("general-v1", "General") + _copy_rows("eap-v1", "Upgrade the EAP"),
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
    }
    for tab, rows in tabs.items():
        for row in rows:
            assert list(row) == COLUMNS[tab], (tab, list(row))
    return tabs
