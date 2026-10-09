"""Hard exclusions and tiers (SPEC 9 "Scoring" step 2, "Hard exclusions").

Tier: the first rule that applies.
  1. Excluded: a hard exclusion (below) or an Exclude signal applies.
  2. Held: a Hold signal applies.
  3. Priority: score >= priority_threshold.
  4. Standard: score >= standard_threshold.
  5. Control: everything else.

Hard exclusions are fixed in code, not in the settings sheet:
  * partners, never prospected and written to the partners table: brokers, insurers,
    HR-tech vendors, PEOs (NAICS 561330), HR consultancies (541612) and behavioral-health
    providers, found by NAICS or by keywords in the industry label and Apollo keywords;
  * anything HubSpot marks: a customer (or a former one), an open deal, an active sequence, an
    opted-out or bounced contact, an owner other than Harry, or another user's activity in 90 days;
  * a company an approver dropped at a send approval (🚫 or "company" in Slack, or `approvals reject
    --company`; enrol/approvals.py, Harry, 2 Oct 2026), kept as a declined_in_slack fact;
  * more than 20% of US-located staff in CA or WA (or in FL, until FL is switched on);
  * fewer than 5 US-located people found;
  * founded less than 2 years ago;
  * HQ in CA or WA (never, SPEC 1.3), or in a state not active on the States tab. Not a website
    visitor (Harry, 6 Oct 2026; accounts.any_us_state): its HQ may be in any US state.

`facts` here is the latest value of each field across all sources, whatever its age
(score.latest_facts), with the account's Overrides applied.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from us_outbound import fmt, parse
from us_outbound.accounts import any_us_state, us_country
from us_outbound.scoring.angle import evidence_display
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.model import Settings

if TYPE_CHECKING:
    from us_outbound.scoring.score import Match

PRIORITY, STANDARD, CONTROL, HELD, EXCLUDED = "Priority", "Standard", "Control", "Held", "Excluded"

NEVER_STATES = frozenset({"CA", "WA"})  # SPEC 1.3
STATE_SHARE_LIMIT = 0.20
MIN_US_PEOPLE = 5
MIN_AGE_YEARS = 2

# Partner categories: the reason codes stored in partners.reason, and how they read.
PARTNER_LABELS = {
    "insurer": "insurer",
    "broker": "insurance or benefits broker",
    "hr_tech": "HR-tech vendor",
    "peo": "PEO",
    "hr_consultancy": "HR consultancy",
    "behavioral_health": "behavioral-health provider",
}
# NAICS prefix -> category; the longest matching prefix wins.
PARTNER_NAICS = {
    "524": "insurer",  # insurance carriers and related activities
    "5241": "insurer",  # insurance carriers
    "5242": "broker",  # insurance agencies and brokerages
    "561330": "peo",  # professional employer organizations
    "541612": "hr_consultancy",  # human resources consulting services
    "621330": "behavioral_health",  # offices of mental health practitioners
    "621420": "behavioral_health",  # outpatient mental health and substance abuse centers
    "622210": "behavioral_health",  # psychiatric and substance abuse hospitals
    "623220": "behavioral_health",  # residential mental health and substance abuse facilities
}
# Whole-word, case-insensitive phrases in the industry label or Apollo keywords.
PARTNER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "broker": (
        "insurance broker", "insurance brokers", "insurance brokerage", "insurance agency",
        "employee benefits broker", "benefits broker", "benefits brokerage",
    ),
    "insurer": ("insurance carrier", "insurance company", "insurer"),
    "hr_tech": (
        "HR software", "HR tech", "HR-tech", "HR technology", "HRIS", "HCM software", "human resources software",
        "payroll", "benefits administration", "benefits platform",
    ),
    "peo": ("PEO", "professional employer organization", "employer of record"),
    "hr_consultancy": ("HR consulting", "HR consultancy", "human resources consulting"),
    "behavioral_health": (
        "behavioral health", "behavioural health", "mental health services", "mental health care",
        "mental healthcare", "therapy practice", "counseling practice", "counselling practice",
        "psychotherapy", "EAP provider", "employee assistance program",
        # Teletherapy companies are competitors. Apollo rarely gives them a behavioral-health NAICS, so
        # inside Digital health or Healthtech they were let in (design review Appendix A.4, 1 Oct 2026).
        "teletherapy", "online therapy", "virtual therapy", "mental health platform",
    ),
}
# Where industry text lives: the account row, and apollo_org facts.
KEYWORD_FIELDS = ("industry", "apollo_industry", "keywords", "apollo_keywords")

# HubSpot facts (source "hubspot"): any true excludes the account. enrol's HubSpot re-check writes
# them, and so does the lookalikes job for every Spill customer it reads (sources/lookalikes.py).
HUBSPOT_EXCLUSIONS = (
    ("hubspot_customer", "a customer in HubSpot"),
    ("hubspot_former_customer", "a former customer in HubSpot"),
    ("hubspot_open_deal", "an open deal in HubSpot"),
    ("hubspot_active_sequence", "a contact in an active HubSpot sequence"),
    ("hubspot_opted_out_or_bounced", "a contact opted out or bounced in HubSpot"),
    ("hubspot_other_owner", "owned by someone else in HubSpot"),
    ("hubspot_other_activity_90d", "activity by another HubSpot user in the last 90 days"),
)
# Decisions people made about an account, kept as facts so a rescore keeps them (enrol.mark_excluded writes
# them). declined_in_slack: an approver dropped the company at a send approval (enrol/approvals.py).
DECLINED_IN_SLACK = "declined_in_slack"
DECISION_EXCLUSIONS = ((DECLINED_IN_SLACK, "dropped by an approver at a send approval in Slack"),)


# -- small parsers -------------------------------------------------------------


def as_number(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def _first(account: Mapping[str, Any], facts: Mapping[str, Any], *fields: str) -> Any:
    """The account's value for the first field it has, else the facts'."""
    for src in (account, facts):
        for f in fields:
            v = src.get(f)
            if v not in (None, "", [], ()):
                return v
    return None


def naics_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [c for v in value for c in naics_codes(v)]
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.findall(r"\d{2,6}", str(value))


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v) for v in value if isinstance(v, str) and v.strip()]
    return []


# -- partners ------------------------------------------------------------------


def partner_match(account: Mapping[str, Any], facts: Mapping[str, Any]) -> tuple[str, str] | None:
    """(category, what matched) for a partner organization, else None."""
    codes = naics_codes(account.get("naics")) + naics_codes(facts.get("naics"))
    prefixes = sorted(PARTNER_NAICS, key=len, reverse=True)
    for code in codes:
        for p in prefixes:
            if code.startswith(p):
                return PARTNER_NAICS[p], f"NAICS {code}"
    text = " ; ".join(t for f in KEYWORD_FIELDS for src in (account, facts) for t in _texts(src.get(f)))
    if text:
        for category, words in PARTNER_KEYWORDS.items():
            found = find_terms(text, words)
            if found:
                return category, found[0].matched
    return None


def partner_category(account: Mapping[str, Any], facts: Mapping[str, Any]) -> str | None:
    """The partners.reason code (broker, insurer, hr_tech, peo, hr_consultancy, behavioral_health)."""
    m = partner_match(account, facts)
    return m[0] if m else None


# -- hard exclusions ---------------------------------------------------------------


def hard_exclusion(
    account: Mapping[str, Any], facts: Mapping[str, Any], settings: Settings, today: date | None = None
) -> str | None:
    """The reason an account is excluded whatever its signals, in plain English; None if it is not."""
    today = today or datetime.now().date()

    partner = partner_match(account, facts)
    if partner:
        category, what = partner
        return f"{PARTNER_LABELS[category]}, a partner ({what})"

    for fact, reason in HUBSPOT_EXCLUSIONS + DECISION_EXCLUSIONS:
        if parse.truthy(facts.get(fact)):
            return reason

    active = {s.upper() for s in settings.active_states()}
    state = str(_first(account, facts, "hq_state") or "").strip().upper()
    visitor = any_us_state(account)
    if not state and not (visitor and us_country(account.get("hq_country"))):
        return "HQ state unknown"
    if state and not visitor:
        if state in NEVER_STATES:
            return f"HQ in {state}, which is never contacted"
        if state not in active:
            return f"HQ state {state} is not active"

    ca_wa = as_number(facts.get("ca_wa_share"))
    if ca_wa is not None and ca_wa > STATE_SHARE_LIMIT:
        return f"{fmt.share(ca_wa, places=0)} of US staff are in CA or WA (over 20%)"
    # SPEC 9: "in CA or WA (or in FL, until it is switched on)": while FL is off it joins
    # the same restricted share.
    fl = as_number(facts.get("fl_share"))
    if fl is not None and "FL" not in active:
        combined = round((ca_wa or 0.0) + fl, 9)
        if combined > STATE_SHARE_LIMIT:
            return f"{fmt.share(combined, places=0)} of US staff are in CA, WA or FL, which is not active (over 20%)"

    us_people = as_number(facts.get("us_headcount"))
    if us_people is None:
        us_people = as_number(account.get("us_employees"))
    if us_people is not None and us_people < MIN_US_PEOPLE:
        return f"fewer than {MIN_US_PEOPLE} US-located people found ({int(us_people)})"

    founded = as_number(_first(account, facts, "founded_year"))
    # Only the year is known: take the earliest date it allows (1 January).
    if founded is not None and today < date(int(founded) + MIN_AGE_YEARS, 1, 1):
        return f"founded less than {MIN_AGE_YEARS} years ago ({int(founded)})"
    return None


# -- tiers -------------------------------------------------------------------------


def _evidence_summary(m: Match, limit: int = 3) -> str:
    shown: list[str] = []
    for ev in m.evidence:
        t = evidence_display(ev)
        if t and t not in shown:
            shown.append(t)
    text = "; ".join(shown[:limit])
    return f"{m.signal.signal} ({text})" if text else m.signal.signal


def score_parts(matches: Iterable[Match], score: int) -> str:
    """ "Mental health support listed (+25), New People leader (+30)", noting a cap."""
    scored = [m for m in matches if m.signal.action == "Score"]
    parts = [f"{m.signal.signal} ({m.weight_applied:+d})" for m in scored if m.weight_applied]
    text = ", ".join(parts) or "no scoring signals found"
    raw = sum(m.weight_applied for m in scored)
    if raw > score:
        text += f"; capped at {score}"
    return text


def tier(
    score: int,
    matches: list[Match],
    exclusion: str | None,
    settings: Settings,
) -> tuple[str, str]:
    """(tier, tier_reason) by SPEC 9's first-rule-wins order; below standard_threshold is Control."""
    if exclusion:
        return EXCLUDED, f"{EXCLUDED}: {exclusion}"
    excludes = [m for m in matches if m.signal.action == "Exclude"]
    if excludes:
        return EXCLUDED, f"{EXCLUDED}: " + ", ".join(_evidence_summary(m) for m in excludes)
    holds = [m for m in matches if m.signal.action == "Hold"]
    if holds:
        return HELD, f"{HELD}: " + ", ".join(_evidence_summary(m) for m in holds)
    g = settings.general
    if score >= g.priority_threshold:
        name = PRIORITY
    elif score >= g.standard_threshold:
        name = STANDARD
    else:
        name = CONTROL
    return name, f"{name}: {score_parts(matches, score)}"
