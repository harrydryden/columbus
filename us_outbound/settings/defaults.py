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

from us_outbound.settings.model import TABS, General

COLUMNS: dict[str, list[str]] = {
    "General": ["key", "value", "note"],
    "Signals": [
        "signal", "source", "looks_for", "context_rule", "weight", "max_weight", "action",
        "suggests_angle", "opener",
        # Tokenized openers by copy role (Harry, 2 Oct 2026; enrol/openers.py); optional columns.
        "opener_people", "opener_founder", "opener_ops", "opener_self",
        "counts_for_days", "active", "note",
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
    # One row per group of titles, with its order at each size (Harry, 1 Oct 2026).
    "Roles": ["role", "copy_role", "titles", "order_10_49", "order_50_249", "industry_groups", "note"],
    # One row per industry (and optionally role), the four emails across (Harry, 30 Sep 2026).
    "Copy": [
        "copy_version", "industry", "role", "status", "approved_by", "qa", "qa_notes",
        "s1_subject", "s1_body", "s2_subject", "s2_body", "s3_subject", "s3_body", "s4_subject", "s4_body",
        "people_leader_line", "founder_line", "operations_line", "sources", "note",
    ],
    "Mailboxes": [
        "address", "instantly_account_id", "domain", "provider", "owner_name", "owner_role", "signature",
        "status", "daily_cap", "added_on", "retire_after",
        # Optional (decision D11, Harry, 1 Oct 2026): the owner's Slack user id, so they can approve
        # replies to their own mailbox. A sheet without the column reads it as blank.
        "slack_id",
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
    (
        "clay_verification",
        "skip",
        "required: every account passes through Clay before it can be emailed (SPEC 2, verify_in_clay). "
        "skip: verify_accounts verifies an account on its Apollo data and HubSpot instead. "
        "Harry, 1 Oct 2026: go live on 5 Oct before the Clay functions exist; set to required once they do.",
    ),
    (
        "clay_email_fallback",
        "no",
        "yes: when Apollo has no verified email for the chosen person (a miss or a catch-all), pick_contacts asks "
        "Clay's Work Email waterfall, within clay_monthly_credits; only a valid result is used. "
        "Harry, 2 Oct 2026: no until Clay's server-callable path is confirmed.",
    ),
    ("apollo_credits_per_account", "1", "Estimate until measured."),
    ("claude_model", "claude-opus-5-5",
     "Writing: drafts copy and reply drafts (Harry, 30 Sep 2026: Opus constructs the emails)."),
    ("claude_task_model", "claude-sonnet-5-5",
     "Well-defined tasks: checks drafted copy (copy qa) and classifies replies. Both models share the cap below."),
    ("email_format", "html",
     "html: emails with embedded links and bullets; text: plain text with links written out. Tracking stays off."),
    ("claude_monthly_cap_usd", "10", "Hard cap on Claude API spend; SPEC 1.1 allows at most $10 a month."),
    (
        "opener_holdout_share",
        "0.3",
        "Share of accounts that get no opener in email 1, chosen by a hash of the account id, so replies "
        "can compare opener against none (contacts.opener_arm). Added by the build (Harry, 2 Oct 2026).",
    ),
    (
        "opener_generic",
        General.opener_generic,
        "The generic opener (Harry, 2 Oct 2026: ever more pressure in our work and personal lives) for an account "
        "with no signal line: the General angle, Control among it, or a signal whose lines all fall through. The "
        "contact's role line below comes first; this one is for a contact with no copy role, or when that line "
        "is blank. After opener_focus_line when opener_focus is yes. Tokens: {company}, {city}. Blank: none. "
        "Added by the build.",
    ),
    ("opener_generic_people", General.opener_generic_people,
     "The generic opener for a People leader; it leads into email 1's industry hook. Added by the build."),
    ("opener_generic_founder", General.opener_generic_founder,
     "The generic opener for a founder or executive. Added by the build."),
    ("opener_generic_ops", General.opener_generic_ops,
     "The generic opener for operations. Added by the build."),
    (
        "opener_focus",
        "no",
        "yes: an account with no signal line gets opener_focus_line, with a short \"what they do\" phrase the "
        "task model takes from Apollo's keywords and description (about $0.002 an account, once, in live runs "
        "only; within the monthly cap). Added by the build.",
    ),
    (
        "opener_focus_line",
        "I came across {company} and its work on {focus}.",
        "The line opener_focus uses. Tokens: {company}, {focus}, {city}. Added by the build.",
    ),
]

# -- Signals (SPEC 5, every one editable) --------------------------------------
# The weights are the design review's Appendix A.2 (docs/gtm-review/README.md, 1 Oct 2026), which
# reworked SPEC 5's defaults against Spill's own HubSpot win data (docs/gtm-review/05-spill-evidence.md).
# Each row's note says what changed and why; where these rows differ from Appendix A, the note says so.
# Openers: the page-reader signals say what the page says (Appendix A.2); the hiring, People, growth and funding
# signals are context, never the line (Harry, 2 Oct 2026; _CONTEXT_NOTE, below).

_APP_CONTEXT = "Headspace: for Work, app, subscription; Calm: app, premium, business, subscription"
# Optum, Cigna and Carelon are mainly medical carriers, "health advocate" is an everyday phrase, Magellan is an
# ordinary word and many products' name (and Magellan Health a health plan), and TELUS Health sells virtual care
# and benefits as well as the EAP: each counts only near words that make it the EAP, so the EAP opener never
# claims an EAP the page doesn't list (copy QA, 2 Oct 2026). ComPsych, GuidanceResources and Aetna Resources For
# Living name only an EAP.
_EAP_CONTEXT = (
    "Optum: EAP, employee assistance, emotional wellbeing, emotional well-being; "
    "Cigna: EAP, employee assistance, life assistance; "
    "Carelon: EAP, employee assistance, behavioral health; "
    "Health Advocate: EAP, employee assistance, work-life; "
    "Magellan: EAP, employee assistance, behavioral health; "
    "TELUS Health: EAP, employee assistance, life assistance"
)
_APPENDIX_A = "Design review Appendix A, 1 Oct 2026"

# Signals are context, never the line (Harry, 2 Oct 2026: "these are just signals"; funding is a signal, never a
# line). The openers of the hiring, People, growth and funding signals never name what was observed: no hiring,
# recruiting, growth, growing, scaling, headcount, funding or money, no job or posting titles, no counts. Each
# speaks to the pressure that situation tends to bring for people, then to support through it, hedged ("often",
# "tends to") so it is true whether or not the observation was. None uses {company}: "at Brightline" would claim
# to know their team. copy_rules.money_violations keeps funding and money out of every opener line.
# (opener, opener_people, opener_founder, opener_ops, opener_self)
_CONTEXT_NOTE = "Openers: signals are context, never the line (Harry, 2 Oct 2026)."
# A new People leader settling in: the first months, setting priorities. opener_self is warm without saying
# "congratulations on the new role", which would tell them we watched their start date.
_NEW_LEADER_LINES = (
    "Setting a team's priorities takes time, and support for people is often one of the first things to get right.",
    "Setting new priorities tends to fill the first months, and support helps most when it's easy to roll out.",
    "Getting support for a team right usually takes months, and it helps to have something people can use right away.",
    "A new set of people priorities usually brings new processes too, and it helps when support is the simple part.",
    "Shaping how a team is supported is often a lot to carry, and it helps when one piece is simple.",
)
# A first or open People role: whoever carries people issues is carrying too much. Never "one person" for the
# People leader: Startups' hook already says "the people function is often one person".
_PEOPLE_ROLE_LINES = (
    "People issues tend to land on a few busy desks, and support helps most when it shares the load.",
    "People issues tend to pile up on whoever handles them, and it helps when support doesn't rest on one desk.",
    "People issues tend to find the busiest desk, and it helps when support doesn't depend on one person.",
    "People questions tend to pile up alongside everything else, and support helps most when it takes no extra admin.",
    "",
)
# A lot of new people: onboarding, managers stretched, culture under strain.
_HIRING_LINES = (
    "When many people join at once, managers and culture tend to feel the stretch, and support helps most early.",
    "When many people join at once, managers are often stretched thin, and support matters most in those first months.",
    "When many people join at once, culture tends to come under strain, and support helps people settle in.",
    "When many people join at once, onboarding often fills the week, and it helps when support is easy to reach.",
    "",
)
# A round: a period of change, more on everyone's plate, priorities and routines shifting. The same lines at any
# age of round, since they name no round and no time.
_FUNDING_LINES = (
    "Times of change tend to put more on everyone's plate, and that's when support matters most.",
    "The pressure of change often lands on managers first, and support helps most when it comes early.",
    "Change tends to test a culture as much as a plan, and that's when taking care of people matters most.",
    "Change often brings new routines and more to coordinate, and support works best when it's already in place.",
    "",
)

# signal, source, looks_for, context_rule, weight, max_weight, action, suggests_angle, opener, counts_for_days,
# active, note: the Signals tab's columns in order, but for the opener lines by role (_OPENERS, below).
_SIGNALS: list[tuple[str, str, str, str, str, str, str, str, str, str, str, str]] = [
    (
        "Mental health support listed", "clay_careers, careers_pages, job_posts",
        "mental health; therapy; counseling; counselling; wellbeing support; well-being support",
        "", "15", "", "Score", "Progressive employer", "I noticed your careers page mentions {evidence}.", "540", "yes",
        "A budget-and-brand signal: wellbeing is part of the employer's brand, never an exclusion. "
        f"{_APPENDIX_A}: +25 to +15, and EAP and employee assistance moved out, so a carrier EAP is no "
        "longer counted twice (it scored +35 with the EAP named row); job posts read too.",
    ),
    (
        "EAP named", "clay_careers, careers_pages, job_posts",
        "EAP; employee assistance; ComPsych; GuidanceResources; Magellan; Optum; Carelon; Cigna; "
        "Aetna Resources For Living; TELUS Health; Health Advocate",
        _EAP_CONTEXT, "5", "", "Score", "Upgrade the EAP",
        "I noticed your benefits page lists an employee assistance program.", "540", "yes",
        f"Kept as the Upgrade the EAP angle trigger, alongside or instead, never disparaging. {_APPENDIX_A}: "
        "+10 to +5, because Spill won 32% of deals where an EAP was in place against 46% with nothing "
        "(05 §3.6); the carrier EAP names and job posts added. Not in Appendix A: Optum, Cigna, Carelon, "
        "Health Advocate, Magellan and TELUS Health count only near their context words, so a medical plan "
        "through Cigna or a product called Magellan is not read as an EAP and the opener stays true.",
    ),
    (
        "Modern mental-health vendor named", "clay_careers, careers_pages, job_posts",
        "Talkspace; Lyra; Modern Health; Spring Health; BetterUp; Nivati; Tava; Wellbound",
        "", "0", "", "Hold", "Switch from a competitor", "", "540", "yes",
        "Held for review: a direct competitor is already in place. "
        f"{_APPENDIX_A}: competitors only. Headspace, Calm, Wellhub and Gympass moved to the Wellbeing app "
        "or perk named row (complements, not competitors); Justworks Plus, a PEO plan rather than a "
        "counseling vendor, left the list too, as Appendix A handles PEO clients with its phase-1 On a PEO "
        "row. Teletherapy companies themselves are partners, never prospected (scoring/tiers.py).",
    ),
    (
        "Wellbeing app or perk named", "clay_careers, careers_pages, job_posts",
        "Headspace; Calm; Wellhub; Gympass",
        _APP_CONTEXT, "5", "", "Score", "Progressive employer", "I noticed {evidence} is part of your benefits.", "540", "yes",
        f"New ({_APPENDIX_A}): apps and fitness perks complement counseling, and Calm and Headspace buyers "
        "buy counseling too, so they score instead of being held. Headspace and Calm count only near the "
        "context terms.",
    ),
    (
        "Progressive benefits", "clay_careers, careers_pages, job_posts",
        "wellness stipend; wellness stipends; mental health day; mental health days; unlimited PTO; "
        "four-day week; four-day weeks; 4-day week; 4-day weeks; parental leave; sabbatical; sabbaticals; "
        "100% employer-paid",
        "", "5", "15", "Score", "Progressive employer", "I noticed your benefits include {evidence}.", "540", "yes",
        f"+5 for each benefit found, at most +15. {_APPENDIX_A}: from +10 each and at most +30, since these are "
        "standard tech perks and +30 skewed Priority toward VC-backed startups that most often already have "
        "a modern vendor; plurals added (terms match whole words).",
    ),
    (
        "Culture or values page", "clay_careers, careers_pages", "values_page = true",
        "", "0", "", "Score", "Progressive employer", "", "540", "no",
        f"{_APPENDIX_A}: 0 and inactive, because nearly every company has one.",
    ),
    (
        "People leader in place", "apollo_people", "people_leader_count >= 1",
        "", "10", "", "Score", "", "", "365", "yes", "SPEC 5 default; unchanged by Appendix A.",
    ),
    (
        "New People leader", "apollo_people", "people_leader_days_in_title <= 90",
        "", "30", "", "Score", "Progressive employer", _NEW_LEADER_LINES[0], "90", "yes",
        f"SPEC 5 weight, kept by {_APPENDIX_A}, which adds the opener. The opener reads right to anyone at the "
        "account, since pick_contacts ranks by seniority, not by the signal; opener_self is for the new People "
        f"leader themself. {_CONTEXT_NOTE}",
    ),
    (
        "First People hire", "apollo_jobs, apollo_people", "open_people_roles >= 1 AND people_leader_count = 0",
        "", "25", "", "Score", "Growing team", _PEOPLE_ROLE_LINES[0], "90", "yes",
        "SPEC 5 default. SPEC lists apollo_jobs; apollo_people is added because people_leader_count comes from it. "
        f"A missing people_leader_count never matches, so People role open below fires anyway. {_CONTEXT_NOTE}",
    ),
    (
        "People role open", "apollo_jobs", "open_people_roles >= 1",
        "", "15", "", "Score", "Growing team", _PEOPLE_ROLE_LINES[0], "60", "yes",
        f"New ({_APPENDIX_A}): a People role posted in the last 60 days, whether or not a People leader is "
        f"already known. {_CONTEXT_NOTE}",
    ),
    (
        "Funding in the last 6 months", "apollo_org, clay_funding", "days_since_funding <= 180",
        "", "20", "", "Score", "Growing team", _FUNDING_LINES[0], "180", "yes",
        f"{_APPENDIX_A}: Recent funding (+20 for 540 days) split so funding decays without code: +20 up to "
        "180 days, +10 from 181 to 365 days (the next row), nothing after. days_since_funding is aged to "
        f"today, so a round read months ago still counts its true age. {_CONTEXT_NOTE} Funding is a signal, "
        "never a line: a line about the round reads as money grabbing.",
    ),
    (
        "Funding 6–12 months ago", "apollo_org, clay_funding",
        "days_since_funding > 180 AND days_since_funding <= 365",
        "", "10", "", "Score", "Growing team", _FUNDING_LINES[0], "365", "yes",
        f"{_APPENDIX_A}: the second half of the funding split. It had no opener while the line said the round "
        "was recent; the lines now name no round and no time, and the change they speak to usually runs well "
        f"past six months, so it has the 6-month row's lines (2 Oct 2026). {_CONTEXT_NOTE}",
    ),
    (
        "Hiring and growth", "apollo_jobs, apollo_org", "open_roles >= 3 OR headcount_growth_12m >= 0.10",
        "", "15", "", "Score", "Growing team", _HIRING_LINES[0], "90", "yes",
        f"{_APPENDIX_A}: reads apollo_jobs too, which owns open_roles (docs/pipeline.md, one owning source per "
        "fact); it read apollo_org only, so 5 open roles from apollo_jobs scored 0. headcount_growth_12m is "
        f"apollo_org's. {_CONTEXT_NOTE}",
    ),
    (
        "Visited the US site", "site_visits", "us_visits_30d >= 1",
        "", "35", "", "Score", "", "", "30", "yes",
        f"{_APPENDIX_A}: +20 to +35, so a US-site visit with a pricing or demo-page view (35 + 25 = 60) is "
        "Priority. A visit alone is Standard. The angle stays General and the copy never mentions the visit.",
    ),
    (
        "Viewed US pricing or demo page", "site_visits", "pricing_or_demo_visits_30d >= 1",
        "", "25", "", "Score", "", "", "30", "yes",
        f"{_APPENDIX_A}: +15 to +25; with Visited the US site it makes Priority.",
    ),
    (
        "Nonprofit budget", "irs_bmf", "revenue >= 2000000 AND revenue <= 50000000",
        "", "15", "", "Score", "", "", "400", "yes", "SPEC 5 default.",
    ),
    (
        "Nonprofit fiscal year ahead", "irs_bmf",
        "days_to_fiscal_year_start >= 60 AND days_to_fiscal_year_start <= 120",
        "", "20", "", "Score", "", "", "1", "yes", "SPEC 5 default. Recomputed daily.",
    ),
    (
        "Q4 plan-year window", "calendar", "month in [10, 11, 12]",
        "", "10", "", "Score", "", "", "1", "no",
        f"{_APPENDIX_A}: inactive. It added 10 to every account from October to December, so it could not tell "
        "accounts apart and shifted every tier on 1 January. Seasonality is handled by reading in February.",
    ),
    (
        "Team of 10–49", "apollo_org", 'employees >= 10 AND employees <= 49 OR size_band in ["10-19", "20-49"]',
        "", "15", "", "Score", "", "", "365", "yes",
        f"New ({_APPENDIX_A}): Spill wins 44% of deals at 10-49 staff, 28% at 50-99 and 24% at 100-249 "
        "(05 §3.1). Apollo's search sends no employee count (2 Oct 2026), so the size band searched counts too.",
    ),
    (
        "Team of 50–99", "apollo_org", 'employees >= 50 AND employees <= 99 OR size_band = "50-99"',
        "", "10", "", "Score", "", "", "365", "yes",
        f"New ({_APPENDIX_A}): see Team of 10–49. 100-249 gets nothing.",
    ),
    (
        "Layoffs", "layoffs", "days_since_layoff <= 90",
        "", "0", "", "Suppress", "", "", "90", "yes",
        "Suppresses the domain for 90 days after a layoff. SPEC 5 default.",
    ),
    (
        "Named by Harry", "named", "named = true",
        "", "30", "", "Score", "", "", "365", "yes",
        "Companies on the Named accounts tab (Harry, 30 Sep 2026). They pass every other check as usual.",
    ),
    (
        "Looks like Spill's customers", "lookalike", "lookalike_active >= 5 AND lookalike_strength >= 10",
        "", "4", "", "Score", "", "", "120", "yes",
        "Spill's HubSpot customers in the account's industry group and size band (Harry, 1 Oct 2026: Spill "
        "companies from HubSpot may inform lookalike targets; sources/lookalikes.py, weekly). "
        "lookalike_strength counts an active customer 1 and a churned one 0.25, and a US one double. "
        "Threshold: at least 5 active and a strength of 10, about ten active customers. From HubSpot's "
        "aggregates on 1 Oct that is Technology & Startups at 10-49 (about 80 active), 50-99 (about 27) and "
        "100-249 (about 12), and Marketing & Creative Agencies at 10-49 (about 44); every other cell from 10 "
        "to 249 staff has fewer than 5 active, Legal Teams included. Weight +4, not more: the size rows "
        "already give +15 at 10-49, so the two "
        "firmographic rows together stay under standard_threshold (20) and an account with no observed "
        "signal stays in Control, the signal-blind holdout. `us-outbound lookalikes show` lists the cells.",
    ),
]

# Tokenized openers (docs/roadmap.md §4 item 2; Harry, 2 Oct 2026): one line per copy role, filled at enrol
# time from the account's stored facts (enrol/openers.py, which documents each token). A line whose token has
# no fact, or a fact that fails its check, falls back to the next line in the cell, then to the signal's plain
# opener, then to the generic line (General opener_generic_*), then to none. opener_self is for a contact who is
# the new People leader. Written to style.md: US English, one sentence, no statistic; the page-reader lines say
# one observed fact, the context signals' lines none (_CONTEXT_NOTE).
# signal: (opener_people, opener_founder, opener_ops, opener_self)
_OPENERS: dict[str, tuple[str, str, str, str]] = {
    # The context signals (_CONTEXT_NOTE, above): no tokens, so every line fills for every account.
    "New People leader": _NEW_LEADER_LINES[1:],
    "First People hire": _PEOPLE_ROLE_LINES[1:],
    "People role open": _PEOPLE_ROLE_LINES[1:],
    "Funding in the last 6 months": _FUNDING_LINES[1:],
    "Funding 6–12 months ago": _FUNDING_LINES[1:],
    "Hiring and growth": _HIRING_LINES[1:],
    # The page-reader signals (sources/pages.py). {page} says where the evidence was read; without it,
    # "when it recruits" is true of a careers page, a benefits page and a job board alike.
    "Mental health support listed": (
        "I saw {company} mentions {evidence} {page}.\n"
        "I saw {company} mentions {evidence} when it recruits.",
        "I saw {company} mentions {evidence} when it recruits.",
        "I saw {company} talks about {evidence} {page}.\n"
        "I saw {company} talks about {evidence} when it recruits.",
        "",
    ),
    "EAP named": (
        "I saw {company} offers its team an employee assistance program through {provider}.\n"
        "I saw {company} offers its team an employee assistance program.",
        "I saw {company} offers an employee assistance program through {provider}.\n"
        "I saw {company} offers an employee assistance program as part of its benefits.",
        "I saw {company} provides an employee assistance program through {provider}.\n"
        "I saw an employee assistance program is part of the benefits at {company}.",
        "",
    ),
    "Wellbeing app or perk named": (
        "I saw {company} offers {evidence} as part of its benefits.",
        "I saw {evidence} is one of the perks at {company}.",
        "I saw {company} includes {evidence} in its benefits.",
        "",
    ),
    # {evidence} is plural or mass here ("wellness stipends", "parental leave"; openers.BENEFIT_FORMS).
    "Progressive benefits": (
        "I saw {company} lists {evidence} among its benefits.",
        "I saw {company} offers {evidence} as part of its benefits.",
        "I saw the benefits at {company} include {evidence}.",
        "",
    ),
}
_OPENER_COLUMNS = ("opener_people", "opener_founder", "opener_ops", "opener_self")


def _signal_rows() -> list[dict[str, str]]:
    """The Signals rows: _SIGNALS in its column order, with each signal's _OPENERS lines."""
    plain = [c for c in COLUMNS["Signals"] if c not in _OPENER_COLUMNS]
    out = []
    for values in _SIGNALS:
        row = dict(zip(plain, values, strict=True))
        lines = dict(zip(_OPENER_COLUMNS, _OPENERS.get(row["signal"], ("",) * 4), strict=True))
        out.append({c: row[c] if c in row else lines[c] for c in COLUMNS["Signals"]})
    return out


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


# -- Focus (Harry, 30 Sep 2026; the shares at launch from the design review, Appendix A.4) -------------

_FOCUS: list[tuple[str, str, str]] = [
    ("Technology & Startups", "50%", "Appendix A.4, 1 Oct 2026: the launch mix. Tech won 39% of decided deals (05 §3.2)."),
    ("Marketing & Creative Agencies", "30%", "Appendix A.4: the launch mix. Agencies won 39% (05 §3.2)."),
    (
        "Legal Teams", "20%",
        "Appendix A.4: Legal on from launch as a bet: 57% won on 14 UK deals (95% CI 33-79%) and 10 of 52 "
        "historic US customers (05 §3.2, §5). The three shares make 100%, so Digital health (filed under "
        "Healthcare) is enrolled only when these groups run short of ready accounts.",
    ),
]


# -- States ----------------------------------------------------------------------

_ACTIVE_STATES = {"NY", "MA", "NJ", "PA", "IL", "GA", "TX"}
_STATE_NOTES = {
    "FL": "off until Harry confirms",
    **{s: "Wave 2" for s in ("NC", "VA", "MD", "OH", "MN")},
    "CA": "never (also enforced in code)",
    "WA": "never (also enforced in code)",
}

# -- Roles (SPEC 5, "Who to contact first"; Harry, 1 Oct 2026) -----------------------
# "Targeting the right contact is paramount... the closer to seniority and decision maker the
# better, whilst also having the time to engage in understanding Spill." Spill's HubSpot win
# rates by the main contact's title (docs/gtm-review/05-spill-evidence.md §3.5): founders and
# CEOs 55%; senior People leaders 42%, 50% at 50+ staff; Operations 35%, 25% at 50+; HR
# Managers and generalists 22-26%; Finance 30% (n = 10). Seniority matters more than function.
# Rows: (role, copy_role, titles, order_10_49, order_50_249, industry_groups, note). Within a
# rank, clean/people.py ranks by seniority, then by how well the title matches.

_ROLES: list[tuple[str, str, str, str, str, str, str]] = [
    (
        "Founder or executive", "",
        "CEO; Founder; Co-founder; Founding Partner; Owner; Co-owner; President; Managing Partner; "
        "Managing Director; Managing Principal; Managing Member; Executive Director; Founding Director; "
        "General Manager",
        "1", "2", "",
        "Founders and CEOs win 55% of Spill's deals at every size. First at 10 to 49 staff, second at 50 to 249.",
    ),
    (
        "Partner at a professional firm", "Founder or executive",
        "Partner; Senior Partner; Equity Partner; Principal",
        "1", "2", "Legal Teams; Professional Services",
        "Partners and principals own law, accounting, consulting and design firms. Elsewhere these words name "
        "other jobs (Partner Manager, Principal Engineer), so they count only in these groups.",
    ),
    (
        "People leader", "",
        "Chief People Officer; CPO; Chief Human Resources Officer; CHRO; Chief People and Culture Officer; "
        "VP of People; VP of HR; VP of People and Culture; VP of People Operations; VP of Total Rewards; "
        "Head of People; Head of HR; Head of People and Culture; Head of People Operations; "
        "Head of Total Rewards; Head of Benefits; Director of People; Director of HR; "
        "Director of People and Culture; Director of People Operations; Director of Total Rewards; "
        "Director of Benefits; People Director; HR Director; People and Culture Director; "
        "People and Culture Lead; People Operations Lead; People Lead; HR Lead",
        "2", "1", "",
        "Senior People leaders win 42% of Spill's deals, 50% at 50 or more staff. First at 50 to 249 staff, "
        "second at 10 to 49. CPO is read as Chief People Officer; Chief Product Officer written out is not.",
    ),
    (
        "Operations", "",
        "COO; VP of Operations; Head of Operations; Director of Operations; Operations Director; Chief of Staff; "
        "Head of Finance and Operations; Director of Finance and Operations; Finance and Operations Director",
        "3", "3", "",
        "Operations win 35% of Spill's deals, but 25% at 50 or more staff. Third at every size.",
    ),
    (
        "HR manager", "People leader",
        "HR Manager; People Manager; People Operations Manager; People and Culture Manager; HR Generalist; "
        "HR Business Partner; Benefits Manager; Total Rewards Manager",
        "", "4", "",
        "Often the most senior HR person at 50 to 249 staff, but HR Managers and generalists win only 22-26% of "
        "Spill's deals. Fourth at 50 to 249, after the founder; not contacted at 10 to 49.",
    ),
    (
        "Office or firm administrator", "Operations",
        "Office Manager; Firm Administrator; Office Administrator; Operations Manager; Practice Manager; "
        "Studio Manager",
        "4", "5", "",
        "A fallback only, when nobody above is found.",
    ),
    (
        "Finance", "",
        "CFO; VP of Finance; Head of Finance; Director of Finance; Finance Director; Controller",
        "", "", "",
        "Never contacted. Listed so these titles are recognized and left out.",
    ),
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
        "Signals": _signal_rows(),
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
                "status": "Warming", "daily_cap": "30", "added_on": "", "retire_after": "", "slack_id": "",
            }
            for address, domain, owner in _MAILBOXES
        ],
        "Overrides": [],
        "Tests": [dict(_FIRST_TEST)],
        "Focus": _rows("Focus", _FOCUS),
        "Named accounts": [],
    }
    for tab, rows in tabs.items():
        for row in rows:
            assert list(row) == COLUMNS[tab], (tab, list(row))
    return tabs
