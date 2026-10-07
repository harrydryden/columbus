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
        "page_features", "page_faqs", "page_customers",
        # What a company under the label is and is not, for the model's label check (labels.py; Harry, 7 Oct 2026);
        # optional: a blank cell takes the build's line from data/industries.csv.
        "definition", "note",
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
    # kind and looks (Harry, 6 Oct 2026: tests read only at pre-registered looks) are optional columns, and so are
    # a variant test's email, change, text_a, text_b and find (Harry, 7 Oct 2026; enrol/variants.py), at the end
    # as they were added to the sheet.
    "Tests": [
        "test_id", "kind", "hypothesis", "version_a", "version_b", "accounts_per_version", "start_date", "looks",
        "read_date", "decision_rule", "status", "result", "email", "change", "text_a", "text_b", "find",
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
    # The notes are what Harry reads on the sheet (`settings load --tab General --take note` refreshes them):
    # plain words, no SPEC numbers (4 Oct 2026).
    (
        "live_sending",
        "no",
        "The master switch and Harry's sign-off. no: nothing reaches a prospect or Instantly. yes: emails go out "
        "(each still waits for your ✅ while auto_send is no). Sheet edits apply at the next sync (02:00 and 11:30 "
        "UK on weekdays), or now with `us-outbound sync`. To stop sending that is already under way, run "
        "`us-outbound stop --live`.",
    ),
    (
        "auto_send",
        "no",
        "no (the pilot): every email waits for an approver's ✅ on its card in #us-outbound. yes: emails go straight "
        "to Instantly once the weekly hand-check is approved. Nothing goes out while live_sending is no.",
    ),
    ("weekly_enrol_cap", "150", "Most new contacts enrolled in a week (Monday to Sunday, UK time): one a company, and a second contact (second_contact) counts too. Each send day takes what is left of it ÷ the send days left in the week."),
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
    ("apollo_floor", "5000",
     "Safety floor: no job spends Apollo credits once Apollo's own balance falls below this."),
    (
        "approver_slack_ids",
        "",
        "Slack member ids (U…) whose ✅ counts on send cards and reply drafts, comma-separated. Harry: U098X453UAG. "
        "A mailbox owner's slack_id (Mailboxes tab) can also approve replies to their own mailbox.",
    ),
    ("escalation_email", "harry@spill.chat", "Where human-in-the-loop items go after escalation_hours."),
    ("escalation_hours", "24", "Hours before an open item is emailed to escalation_email."),
    ("alert_channel", "#us-outbound", "Alerts, approvals, the daily post and the Monday readout."),
    ("dev_channel", "#us-outbound-dev", "The only channel dry-run posts to. Added by the build."),
    ("booking_link", "https://meetings.hubspot.com/harry336/us-demo-link",
     "Harry's HubSpot meetings link: the signature's 'Book a call here', and reply drafts."),
    ("booking_page", "https://www.spill.chat/us/book-demo",
     "The demo page each email's call to action links to."),
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
    ("recontact_person_months", "12", "Not used yet; leave as it is."),
    ("recontact_account_months", "6", "Not used yet; leave as it is."),
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
        "yes once a test email to a seed inbox shows Instantly's unsubscribe link working in HTML and plain text, "
        "and the click marks the lead unsubscribed. Live sending waits for it.",
    ),
    ("hubspot_pipeline", "Spill 3.0", "Deals go in this pipeline."),
    ("hubspot_pipeline_id", "",
     "The pipeline's id. From `us-outbound hubspot ids`. Without it, a positive reply creates no HubSpot deal."),
    ("hubspot_deal_stage", "", "Not used yet; leave as it is."),
    ("hubspot_deal_stage_id", "",
     "Its first stage's id. From `us-outbound hubspot ids`. Without it, a positive reply creates no HubSpot deal."),
    ("hubspot_owner_id", "",
     "Harry's HubSpot owner id. From `us-outbound hubspot ids`. Without it, a positive reply creates no HubSpot deal."),
    ("clay_accounts_function_id", "", "Clay function \"US Outbound – Accounts\", once built in phase 0."),
    ("clay_contacts_function_id", "", "Clay function \"US Outbound – Contacts\", once built in phase 0."),
    ("clay_credits_per_account", "0", "Not used yet; leave as it is."),
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
        "Harry, 2 Oct 2026: no until Clay's server-callable path is confirmed: run "
        "`us-outbound clay check-email --first YOUR_FIRST_NAME --last YOUR_LAST_NAME --domain spill.chat --live` "
        "first; it says when to set this to yes.",
    ),
    (
        "clay_cross_check",
        "no",
        "yes: before an account whose HQ state or size Apollo leaves in doubt goes to the weekly hand-check, "
        "verify_accounts asks Clay's \"US Outbound – Accounts\" function (clay_accounts_function_id) once, within "
        "clay_monthly_credits. A doubt Clay settles is dropped; where Clay disagrees, the hand-check shows both counts or "
        "states. A missing HQ state or size is filled in from Clay. Needs clay_accounts_function_id. Harry, 6 Oct 2026: "
        "no until the function is built in Clay (docs/pipeline.md has its spec).",
    ),
    (
        "second_contact",
        "no",
        "yes: at companies of second_contact_min_employees or more staff, a second person of another role (the "
        "founder, say, when the first was the People leader) gets their own emails, from the same sender, "
        "second_contact_delay_days after the first person's email 1. Second contacts take only the room the first "
        "contacts of new companies leave, and count towards the week and each sender's day like anyone. When anyone "
        "at the company replies, bounces, unsubscribes or complains, both people's emails stop. no: one person per "
        "company. Harry, 6 Oct 2026: no while sending capacity is the limit.",
    ),
    (
        "second_contact_min_employees",
        "50",
        "The smallest company, by staff, that gets a second contact. Read as the Roles tab reads size, so a company "
        "in the 50-99 band counts as 50.",
    ),
    (
        "second_contact_delay_days",
        "3",
        "Days after the first person's email 1 before the second person's, so the two never arrive the same day "
        "(at least 1). A multiple of 7 would put the second's emails on the days of the first's follow-ups.",
    ),
    ("apollo_credits_per_account", "1", "Not used yet; leave as it is."),
    (
        "apollo_enrich_groups",
        "Technology & Startups",
        "Industry groups whose queued accounts apollo_enrich enriches through Apollo for funding and an exact "
        "employee count, comma-separated (1 credit per company found, within 15% of apollo_monthly_credits; again "
        "after 180 days). Blank enriches none. Harry, 2 Oct 2026: funding as a sign of change and growth, where "
        "funding is common. Added by the build.",
    ),
    (
        "site_visit_domain",
        "spill.chat",
        "The website Apollo's visitor tracker is installed on. Each morning at 06:00 UK, site_visits reads the "
        "companies that visited it in the last 30 days for the website-visit signals (a few Apollo credits a "
        "day). Blank: nothing is read. Added by the build.",
    ),
    (
        "site_visit_us_paths",
        "/us",
        "Pages that count as a visit to the US site, comma-separated. A page counts when its path contains one "
        "of these, so /us also counts /us/pricing. Added by the build.",
    ),
    (
        "site_visit_intent_paths",
        "/us/pricing, /us/demo, /us/book",
        "Pages that count as a pricing or demo visit, comma-separated, matched the same way, so /us/book counts "
        "/us/book-demo. Blank: no pricing or demo signal. Added by the build.",
    ),
    (
        "min_employees",
        "10",
        "The smallest company we contact, in employees (SPEC 2: 10). Every source searches from here, "
        "verify_accounts holds accounts to it and the hand-check's size edges follow it. Harry, 6 Oct 2026. "
        "Added by the build.",
    ),
    (
        "max_employees",
        "249",
        "The largest company we contact, in employees, this number included (SPEC 2: 249). Set 500 to search "
        "and contact companies up to 500. The Roles tab's 50-249 order covers everyone from 50 up. "
        "Added by the build.",
    ),
    ("claude_model", "claude-opus-5-5",
     "Writing: drafts copy and reply drafts (Harry, 30 Sep 2026: Opus constructs the emails)."),
    ("claude_task_model", "claude-sonnet-5-5",
     "Well-defined tasks: checks drafted copy (copy qa) and classifies replies. Both models share the cap below."),
    ("email_format", "html",
     "html: emails with embedded links and bullets; text: plain text with links written out. Tracking stays off."),
    # -- The learning loop (Harry, 6 Oct 2026: "push ahead with building") --
    (
        "utm_links",
        "no",
        "yes: the links to spill.chat and your booking link carry UTM tags (utm_source us_outbound, utm_medium "
        "email, utm_campaign the Copy row's copy_version, utm_content the email's number), so website visits and "
        "bookings can be traced to the emails. The words of each link don't change, and the Trustpilot and "
        "unsubscribe links are never tagged. no (the default): plain links. Tagged links can read as marketing to "
        "inbox filters, so try yes on a seed send first. Added by the build.",
    ),
    ("claude_monthly_cap_usd", "10", "Hard cap on Claude API spend each month (UTC), in dollars; at most $100. SPEC 1.1 set $10."),
    (
        "label_check",
        "required",
        "required: before a new company can get a send-approval card, verify_accounts has the task model check its "
        "industry label against its Apollo facts (about $0.01 a company, once): agreement earns the label's own copy, "
        "a doubt the group's or General copy, and a public body, society or company no label fits is held for the "
        "hand-check or left out. While the model cannot be asked (the Claude cap, an error), new companies wait "
        "unverified and the daily post asks; companies already verified keep going with their group's copy. "
        "skip: the rules alone, with the group's copy (for a Claude outage). Harry, 7 Oct 2026. Added by the build.",
    ),
    (
        "opener_holdout_share",
        "0.3",
        "Share of accounts that get no opener in email 1, chosen by a hash of the account id, so replies "
        "can compare opener against none (contacts.opener_arm). Added by the build (Harry, 2 Oct 2026).",
    ),
    (
        "opener_generic",
        General.opener_generic,
        "The opener for a company with no signal line, when the contact's role has no line of its own below. "
        "Tokens: {company}, {city}. Blank: no opener.",
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
    (
        "email1_subject",
        General.email1_subject,
        "Email 1's personal subject, for the email1_subject_share of companies: short, lower case and about the "
        "reader, so mail filters and people read it as personal mail. Variables: {{company}}, {{first_name}}. "
        "Emails 2 to 4 keep the Copy row's subjects. Added by the build (Harry, 5 Oct 2026).",
    ),
    (
        "email1_subject_share",
        "0.5",
        "Share of companies whose email 1 uses email1_subject instead of the Copy row's s1_subject, chosen by a "
        "hash of the account id (independent of the opener holdout), so replies can compare the two "
        "(contacts.subject_arm; `us-outbound signals review`). 0: every email 1 keeps the Copy row's subject. "
        "Added by the build (Harry, 5 Oct 2026).",
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
# recruiting, growth, growing, scaling, headcount, funding or money, no job or posting titles, no roles, no counts.
# Each takes the pressure that situation tends to bring, at work and at home (Harry: "ever more pressure in our work
# and personal lives"), and turns to having somewhere to turn, hedged ("often", "tends to") so it is true whether or
# not the observation was. Each line has its own shape, so a batch doesn't read as one template (copy QA, 2 Oct
# 2026). None uses {company}: "at Brightline" would claim to know their team. No booking words either: the ask
# belongs to the body. copy_rules.money_violations keeps funding and money out of every opener line.
# (opener, opener_people, opener_founder, opener_ops, opener_self)
_CONTEXT_NOTE = "Openers: signals are context, never the line (Harry, 2 Oct 2026)."
# A new People leader settling in: priorities shifting, new routines. opener_self is warm without saying
# "congratulations on the new role", which would tell them we watched their start date.
_NEW_LEADER_LINES = (
    "When priorities shift at work, people often feel it at home too, and someone to talk to helps.",
    "While things settle, pressure from work and home tends to build, and people lean on help that's easy to reach.",
    "Settling into new ways of working takes energy, and people cope better with somewhere private to turn.",
    "New routines can follow people home, and the help that sticks is the kind that adds no admin.",
    "It's easy to put your own well-being last when you spend the day looking after everyone else's.",
)
# A first or open People role: whoever carries people issues is carrying a lot. Never "one person" for the
# People leader: Startups' hook already says "the people function is often one person".
_PEOPLE_ROLE_LINES = (
    "Work and home both ask a lot of people, and the best help is the kind nobody has to chase.",
    "Plenty of people carry worries from home into work, and they tend to use help that feels private and easy.",
    "Being the person everyone looks to can weigh heavily, and it eases when people have somewhere else to turn.",
    "Questions about people pile up alongside everything else, so help that runs itself is worth a lot.",
    "",
)
# A busy stretch: onboarding, managers stretched, little slack at work or at home.
_HIRING_LINES = (
    "Busy stretches tend to follow people home, and a sounding board outside the team can take the edge off.",
    "When managers are stretched thin, a private place for their teams to turn can take some of the weight.",
    "A busy stretch asks a lot at work and at home, and people hold up better knowing where to turn.",
    "When the week is packed, there's little slack at work or home, so help has to fit in without fuss.",
    "",
)
# A round: a period of change, more on everyone's plate, priorities and routines shifting. The same lines at any
# age of round, since they name no round and no time.
_FUNDING_LINES = (
    "Times of change tend to put more on everyone's plate, and that's when support matters most.",
    "The pressure of change often lands on managers first, and it helps when teams have a second place to go.",
    "When a lot is changing, even steady people can feel stretched, and it helps to know where to turn.",
    "Change often brings new routines and more to coordinate, and support works best when it's already in place.",
    "",
)
# The page-reader signals (sources/pages.py) follow the same rule (Harry, 2 Oct 2026: "rework the benefits-page
# lines the same way"). The page tells us they already look after their people; the line speaks to the pressure
# that still finds people, at work and at home, and to help that's there when it does. It never echoes the page:
# no mental health, counseling, EAP, employee assistance, app, perk, benefit, stipend, time off, leave or
# sabbatical, no provider and no token. Never a word against what they offer: the Upgrade the EAP angle is
# "alongside or instead, never disparaging", and an app or perk complements counseling.
# Mental health support listed: they already speak up for well-being; the hard weeks are where it counts.
_MENTAL_HEALTH_LINES = (
    "Most teams know pressure from work and home adds up, and the hard part is making help easy to use.",
    "Looking after a team well is a long game, and the hard weeks at home are where it counts most.",
    "Few people switch off fully when they get home, and it's good to have someone in their corner.",
    "Days fill up fast, so people tend to value support that's simple to find when they need it.",
    "",
)
# EAP named: support is in place. A choice of who to talk to, being listened to, knowing support is there: never
# quicker, more personal or better used than theirs, which a reader with an EAP hears as a comparison (copy QA).
_EAP_LINES = (
    "Pressure lands differently on everyone, so it helps to have a choice of who to talk to.",
    "Worries from home often show up at work first, and people open up more when they feel listened to.",
    "Good people tend to carry more than they show, and knowing support is there goes a long way.",
    "A workplace tends to run best when people feel supported on the days life at home gets complicated.",
    "",
)
# Wellbeing app or perk named: weeks when work and life land at once, and a listener. Never "the everyday",
# "self-care" or "a real person", which name the app and say it falls short (copy QA).
_APP_LINES = (
    "Life and work have a way of landing all at once, and a good listener can ease those weeks.",
    "Managers can't be everything to everyone, and a second trusted voice often makes a difference.",
    "Leaders tend to learn that heavy weeks go easier when no one has to face them alone.",
    "Deadlines and life at home tend to collide sooner or later, and someone who gets it helps.",
    "",
)
# Progressive benefits: life at home doesn't keep office hours. Never "good balance" or "breathing room",
# which echo the time off they offer.
_BENEFITS_LINES = (
    "Stress rarely stays in one place, and talking it through tends to help wherever it started.",
    "The moments that weigh on people rarely keep office hours, so it matters when help is quick to reach.",
    "A rough night at home can shape a whole week at work, and having someone to confide in helps.",
    "Big moments at home tend to land mid-week, and people cope better with someone to talk to.",
    "",
)

# signal, source, looks_for, context_rule, weight, max_weight, action, suggests_angle, opener, counts_for_days,
# active, note: the Signals tab's columns in order, but for the opener lines by role (_OPENERS, below).
_SIGNALS: list[tuple[str, str, str, str, str, str, str, str, str, str, str, str]] = [
    (
        "Mental health support listed", "clay_careers, careers_pages, job_posts",
        "mental health; therapy; counseling; counselling; wellbeing support; well-being support",
        "", "15", "", "Score", "Progressive employer", _MENTAL_HEALTH_LINES[0], "540", "yes",
        "A budget-and-brand signal: wellbeing is part of the employer's brand, never an exclusion. "
        f"{_APPENDIX_A}: +25 to +15, and EAP and employee assistance moved out, so a carrier EAP is no "
        "longer counted twice (it scored +35 with the EAP named row); job posts read too.",
    ),
    (
        "EAP named", "clay_careers, careers_pages, job_posts",
        "EAP; employee assistance; ComPsych; GuidanceResources; Magellan; Optum; Carelon; Cigna; "
        "Aetna Resources For Living; TELUS Health; Health Advocate",
        _EAP_CONTEXT, "5", "", "Score", "Upgrade the EAP",
        _EAP_LINES[0], "540", "yes",
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
        _APP_CONTEXT, "5", "", "Score", "Progressive employer", _APP_LINES[0], "540", "yes",
        f"New ({_APPENDIX_A}): apps and fitness perks complement counseling, and Calm and Headspace buyers "
        "buy counseling too, so they score instead of being held. Headspace and Calm count only near the "
        "context terms.",
    ),
    (
        "Progressive benefits", "clay_careers, careers_pages, job_posts",
        "wellness stipend; wellness stipends; mental health day; mental health days; unlimited PTO; "
        "four-day week; four-day weeks; 4-day week; 4-day weeks; parental leave; sabbatical; sabbaticals; "
        "100% employer-paid",
        "", "5", "15", "Score", "Progressive employer", _BENEFITS_LINES[0], "540", "yes",
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
        "First People hire (likely)", "apollo_jobs, apollo_people",
        "open_people_roles >= 1 AND people_leader_count = 0 AND people_search_coverage >= 0.5",
        "", "20", "", "Score", "Growing team", _PEOPLE_ROLE_LINES[0], "60", "yes",
        "Harry, 5 Oct 2026: replaces First People hire, which could never fire, as nothing wrote a count of 0. "
        "apollo_people (weekdays 04:20) writes people_leader_count = 0 only where Apollo holds at least half the "
        "company's headcount (people_search_coverage), so finding no People leader there likely means there is none; "
        "the coverage is in the condition too, so a later thin search turns it off. +20, not +25: it is an inference. "
        f"People role open fires with it. {_CONTEXT_NOTE}",
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
        "Close match to Spill's customers", "lookalike", "lookalike_fit >= 70",
        "", "10", "", "Score", "", "", "120", "yes",
        "Harry, 5 Oct 2026: the lookalike graded by industry, size and growth rate. lookalike_fit (0 to 100; "
        "sources/lookalikes.py, refreshed nightly from the monthly lookalikes job) weighs how strong Spill's "
        "HubSpot customers are in the account's industry (0.45), how common its size band is among them (0.35) and "
        "how common its 12-month headcount growth is (0.20; left out, never counted against it, when unknown). "
        "+10 with Team of 10–49 makes 25, Standard: the old +4 left most 10-49 accounts at 19, one point under "
        "standard_threshold, and on 5 Oct Control held 300 of 432 open accounts. So a close lookalike with no "
        "observed signal is Standard now, and Control holds the accounts that fit less well. On 5 Oct any weight "
        "from +5 to +15 gave the same tiers (Standard 163 of 421, 39%), and +10 leaves room for observed signals "
        "to order them. `us-outbound lookalikes fit` shows the fits and the tier mix these rows give.",
    ),
    (
        "Some match to Spill's customers", "lookalike", "lookalike_fit >= 45 AND lookalike_fit < 70",
        "", "3", "", "Score", "", "", "120", "yes",
        "Harry, 5 Oct 2026: the middle of lookalike_fit (see Close match to Spill's customers). It orders accounts "
        "within a tier and lifts none on its own: +3 with Team of 10–49 makes 18, Control. At +8 it put 256 of 421 "
        "open accounts in Standard (61%, 5 Oct), outside the tier-mix check's 5-40%.",
    ),
    (
        "Looks like Spill's customers", "lookalike", "lookalike_active >= 5 AND lookalike_strength >= 10",
        "", "4", "", "Score", "", "", "120", "no",
        "Replaced by Close match and Some match to Spill's customers (Harry, 5 Oct 2026), which grade the fit; "
        "kept here switched off, so a settings load switches the sheet's row off. It scored +4 when the account's "
        "industry group and size band held at least 5 active customers and a strength of 10 (Harry, 1 Oct "
        "2026), which fired on 268 of 432 accounts on 5 Oct and so barely told them apart.",
    ),
    (
        "Found as a lookalike of a customer", "lookalike_lead", "found_as_lookalike = true",
        "", "10", "", "Score", "", "", "120", "yes",
        "Companies the monthly lookalike_leads job found through Apollo's search for US companies like Spill's "
        "active customers, UK ones included, in an active industry group and size band (Harry, 5 Oct 2026; "
        "sources/lookalike_leads.py). Only accounts it brought in get it; the fact keeps whether the customer "
        "was in the US, so US- and UK-seeded leads can be compared. Counts for 120 days, about four monthly runs. "
        "Unlike Looks like Spill's customers, which scores the account's cell, this was measured on the company "
        "itself, so it may lift an account out of Control: with the size row (+15 at 10-49) and the cell signal "
        "(+4) a lookalike lead scores 29, Standard. Added by the build.",
    ),
]

# Tokenized openers (docs/roadmap.md §4 item 2; Harry, 2 Oct 2026): one line per copy role, filled at enrol
# time from the account's stored facts (enrol/openers.py, which documents each token). A line whose token has
# no fact, or a fact that fails its check, falls back to the next line in the cell, then to the signal's plain
# opener, then to the generic line (General opener_generic_*), then to none. opener_self is for a contact who is
# the new People leader. Written to style.md: US English, one sentence, no statistic, and no default line names
# what was observed (_CONTEXT_NOTE, _MENTAL_HEALTH_LINES); the tokens are for lines Harry writes on the sheet.
# signal: (opener_people, opener_founder, opener_ops, opener_self)
_OPENERS: dict[str, tuple[str, str, str, str]] = {
    # The context signals (_CONTEXT_NOTE, above): no tokens, so every line fills for every account.
    "New People leader": _NEW_LEADER_LINES[1:],
    "First People hire (likely)": _PEOPLE_ROLE_LINES[1:],
    "People role open": _PEOPLE_ROLE_LINES[1:],
    "Funding in the last 6 months": _FUNDING_LINES[1:],
    "Funding 6–12 months ago": _FUNDING_LINES[1:],
    "Hiring and growth": _HIRING_LINES[1:],
    # The page-reader signals (_MENTAL_HEALTH_LINES, above): no tokens either.
    "Mental health support listed": _MENTAL_HEALTH_LINES[1:],
    "EAP named": _EAP_LINES[1:],
    "Wellbeing app or perk named": _APP_LINES[1:],
    "Progressive benefits": _BENEFITS_LINES[1:],
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


_DEFINITIONS: dict[str, str] | None = None


def bundled_definitions() -> dict[str, str]:
    """Industries label (casefolded) -> the build's one-line definition from data/industries.csv (Harry, 7 Oct 2026).

    validate.py gives a label with a blank definition cell, or a sheet without the column, the build's line, so the
    model's label check (labels.py) works before the column reaches the sheet, and loading it changes nothing the
    check reads unless Harry has edited a line."""
    global _DEFINITIONS
    if _DEFINITIONS is None:
        _DEFINITIONS = {r["industry"].strip().casefold(): " ".join(r["definition"].split()) for r in _industries()
                        if r["definition"].strip()}  # spaces folded, as validate.py folds a sheet cell
    return _DEFINITIONS


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
# The signature is added at render time, and Instantly's unsubscribe line by the campaign's step
# template; neither is stored here. No email carries a data notice (Harry, 5 Oct 2026).


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
    "kind": "ab",
    "hypothesis": (
        "On accounts where an EAP is named, an Upgrade the EAP opener gets a higher reply rate than a General "
        "opener."
    ),
    "version_a": "eap-v1",
    "version_b": "general-v1",
    "accounts_per_version": "400",
    "start_date": "",
    "looks": "",
    "read_date": "",
    "decision_rule": (
        "reply rate, human replies within 28 days of step 1 ÷ accounts with step 1 delivered; "
        "detects a 2× difference"
    ),
    "status": "planned",
    "result": "",
    "email": "",
    "change": "",
    "text_a": "",
    "text_b": "",
    "find": "",
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
