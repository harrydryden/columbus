"""The ICP gate: every rule that keeps a company out, written once (W2-2, 20 Oct 2026).

The same rules were written five times (verify.check, scoring/tiers.hard_exclusion, accounts.admit,
enrol.account_block, sources/apollo_universe.take; NEVER_STATES four times), each a little differently. gate() holds
them once, in one order, each with the reason code verify writes to accounts.status_reason (verify.REASON_CODES), and
each caller names the checks it makes (its stage), so what each stage decides is what it decided before:

  domain        no domain (incomplete)
  personal      a personal email domain (suppressed)
  public_body   a public body, never prospected
  suppressed    the domain is on the suppression list (aliases' roots included, as the caller loads them)
  partner       on the partners table, or a partner by NAICS or keywords (partner_match): never prospected
  held          sampled or pulled at this week's hand-check (enrol.hand_check)
  state         HQ state unknown (state_unknown), in CA or WA (never, SPEC 1.3), or not active on the States tab.
                A website visitor (any_us_state) may be in any US state, and an unknown one once Apollo puts the HQ
                in the US (Harry, 6 Oct 2026)
  size          outside the General range, min_employees to max_employees: the employee count when known, else
                the size band (Overrides applied)
  industry      no Industries label (no_label), or one switched off (industry_off)
  hubspot       a HubSpot fact: a customer, an open deal, another owner, an opted-out or bounced contact
  declined      dropped by an approver at a send approval (a fact kept from before W2-1)
  founded       founded less than MIN_AGE_YEARS ago

  complete      not a rule but how the others read a gap: with it, a missing size is incomplete, a missing label
                no_label, and the HQ state is read from the account row alone (verify's rule, Harry, 14 Oct 2026:
                "for any company where we have incomplete information, filter out"); without it, a gap passes (an
                account verified before the rule, or with an Overrides row, is not taken out of the queue for a fact
                nobody can now supply) and a state fact stands in for a blank hq_state column (the rescore's reading)

The stages (each caller's checks):
  VERIFY  verify.check: the first check of a new account, on what is known
  SCORE   scoring/tiers.hard_exclusion: what tiers an account Excluded at every rescore
  SEND    enrol.account_block: what keeps a verified account from pick_contacts, enrol and the hand-check pool, now
  ADMIT   accounts.admit: the front door (personal and public-body domains, partners, suppression)
  SOURCE  sources/apollo_universe.take and sources/site_visits: an Apollo organization before it is admitted

Dead rules gone with W2-2 (code-scoring B5: no source ever wrote their facts): more than 20% of US staff in CA or WA
(ca_wa_share), the same with FL while FL is off (fl_share), and fewer than 5 US-located people (us_headcount).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import date, datetime
from typing import Any, NamedTuple

from us_outbound import parse
from us_outbound.clean.domains import is_personal_domain, is_public_body
from us_outbound.clean.people import state_code
from us_outbound.industry.material import naics_codes, texts
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.model import SIZE_BANDS, Settings
from us_outbound.settings.overrides import effective

NEVER_STATES = frozenset({"CA", "WA"})  # SPEC 1.3: never an HQ there, never a contact located there (SPEC 1.5)
US_COUNTRIES = frozenset({"united states", "united states of america", "us", "usa"})
VISITOR_SOURCE = "site_visit"  # accounts.source of a company that came in by visiting (accounts.SITE_VISIT)
MIN_AGE_YEARS = 2

# The rules, in the order gate() applies them (verify's order, so the first failing rule, and so the code verify
# writes, is what it was: a company in CA with an open deal is left out for good for its state, not for 30 days).
DOMAIN, PERSONAL, PUBLIC_BODY, SUPPRESSED, PARTNER, HELD = "domain", "personal", "public_body", "suppressed", \
    "partner", "held"
STATE, SIZE, INDUSTRY, HUBSPOT, DECLINED, FOUNDED = "state", "size", "industry", "hubspot", "declined", "founded"
COMPLETE = "complete"
RULES = (DOMAIN, PERSONAL, PUBLIC_BODY, SUPPRESSED, PARTNER, HELD, STATE, SIZE, INDUSTRY, HUBSPOT, DECLINED, FOUNDED)
ALL = frozenset({*RULES, COMPLETE})
VERIFY = frozenset({DOMAIN, PERSONAL, PUBLIC_BODY, SUPPRESSED, PARTNER, STATE, SIZE, INDUSTRY, HUBSPOT, COMPLETE})
SCORE = frozenset({PARTNER, HUBSPOT, DECLINED, STATE, FOUNDED})
SEND = frozenset({DOMAIN, PUBLIC_BODY, SUPPRESSED, PARTNER, HELD, SIZE, INDUSTRY})
ADMIT = frozenset({PERSONAL, PUBLIC_BODY, PARTNER, SUPPRESSED})
SOURCE = frozenset({PERSONAL, STATE, SIZE, PARTNER})  # apollo_universe.take (in its order), site_visits.verdict

HELD_TEXT = "held or pulled at this week's hand-check"
PARTNER_TEXT = "a partner, never prospected"

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

# HubSpot facts (source "hubspot"): any true excludes the account, with the code verify writes. enrol's and verify's
# HubSpot checks write them, and so does the lookalikes job for every Spill customer it reads (sources/lookalikes.py).
# A former customer is a customer; a contact in another user's sequence, or another user's activity, is another
# owner's, which can end. The stored opted-out-or-bounced fact cannot say which (verify.hubspot_check can), so it
# reads as opted out: both are for good.
HUBSPOT_EXCLUSIONS = (
    ("hubspot_customer", "a customer in HubSpot"),
    ("hubspot_former_customer", "a former customer in HubSpot"),
    ("hubspot_open_deal", "an open deal in HubSpot"),
    ("hubspot_active_sequence", "a contact in an active HubSpot sequence"),
    ("hubspot_opted_out_or_bounced", "a contact opted out or bounced in HubSpot"),
    ("hubspot_other_owner", "owned by someone else in HubSpot"),
    ("hubspot_other_activity_90d", "activity by another HubSpot user in the last 90 days"),
)
HUBSPOT_CODES: dict[str, str] = {
    "hubspot_customer": "hubspot_customer", "hubspot_former_customer": "hubspot_customer",
    "hubspot_open_deal": "hubspot_open_deal", "hubspot_active_sequence": "hubspot_owner",
    "hubspot_opted_out_or_bounced": "hubspot_opted_out", "hubspot_other_owner": "hubspot_owner",
    "hubspot_other_activity_90d": "hubspot_owner",
}
# A decision a person made about an account, kept as a fact so a rescore keeps it (enrol.mark_excluded wrote it): an
# approver dropped the company at a send approval (removed with W2-1; the facts stay).
DECLINED_IN_SLACK = "declined_in_slack"
DECLINED_TEXT = "dropped by an approver at a send approval in Slack"

# Every code gate() returns: verify.REASON_CODES files each one (tests/test_icp.py).
CODES = frozenset({"incomplete", "suppressed", "public_body", "partner", "held", "state_unknown", "state", "size",
                   "no_label", "industry_off", *HUBSPOT_CODES.values(), "declined", "founded"})


class Reason(NamedTuple):
    """Why the gate keeps a company out: the code (verify.REASON_CODES) and the reason in words. A tuple, so verify's
    callers keep reading (code, text)."""

    code: str
    text: str


# -- small readers ---------------------------------------------------------------------------------


def us_country(country: object) -> bool:
    return " ".join(str(country or "").split()).casefold() in US_COUNTRIES


def any_us_state(account: Mapping[str, Any]) -> bool:
    """Harry, 6 Oct 2026: a website visitor is never excluded on its HQ state, as long as it is in the US. Its HQ
    may be in any state, CA, WA and the States tab's inactive ones included; an unknown state is no doubt once
    Apollo puts the HQ in the US (hq_country). SPEC 1.3's rule on people stands: a contact located in CA or WA is
    never emailed (contacts/pick.py, enrol.pick_contact), whatever the company's HQ."""
    return str(account.get("source") or "") == VISITOR_SOURCE


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _first(account: Mapping[str, Any], facts: Mapping[str, Any], field: str) -> Any:
    """The account's value for the field when it has one, else the facts'."""
    for src in (account, facts):
        v = src.get(field)
        if v not in (None, "", [], ()):
            return v
    return None


def partner_match(account: Mapping[str, Any], facts: Mapping[str, Any]) -> tuple[str, str] | None:
    """(category, what matched) for a partner organization, else None."""
    codes = naics_codes(account.get("naics")) + naics_codes(facts.get("naics"))
    prefixes = sorted(PARTNER_NAICS, key=len, reverse=True)
    for code in codes:
        for p in prefixes:
            if code.startswith(p):
                return PARTNER_NAICS[p], f"NAICS {code}"
    text = " ; ".join(t for f in KEYWORD_FIELDS for src in (account, facts) for t in texts(src.get(f)))
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


# -- the gate ----------------------------------------------------------------------------------------


def gate(account: Mapping[str, Any], facts: Mapping[str, Any], settings: Settings | None, *,
         suppressed: Collection[str] = (), partners: Collection[str] = (), today: date | None = None,
         held: Collection[str] = (), checks: Collection[str] = ALL) -> Reason | None:
    """Why the company is kept out, by the first of the checks it fails (in RULES order), or None: it passes.

    account: its row with its Overrides applied (the size rule applies the size rows itself); facts: the latest value
    of each field fact (score.latest_facts); suppressed, partners: the domains in force (lower case); held: the
    account ids the hand-check holds. settings may be None only for checks that read no setting (ADMIT)."""
    complete = COMPLETE in checks
    domain = _lower(account.get("domain"))
    if DOMAIN in checks and not domain:
        return Reason("incomplete", "no domain")
    if domain:
        if PERSONAL in checks and is_personal_domain(domain):
            return Reason("suppressed", "a personal email domain")
        if PUBLIC_BODY in checks and is_public_body(domain):
            return Reason("public_body", "a public body, never prospected")
        if SUPPRESSED in checks and domain in suppressed:
            return Reason("suppressed", "domain suppressed")
    if PARTNER in checks:
        if domain and domain in partners:
            return Reason("partner", PARTNER_TEXT)
        match = partner_match(account, facts)
        if match:
            category, what = match
            return Reason("partner", f"{PARTNER_LABELS[category]}, a partner ({what})")
    if HELD in checks and str(account.get("account_id")) in held:
        return Reason("held", HELD_TEXT)
    if STATE in checks:
        assert settings is not None, "the state rule reads the States tab"
        why = _state(account, facts, settings, complete)
        if why:
            return why
    if SIZE in checks:
        assert settings is not None, "the size rule reads the General range"
        why = _size(account, settings, complete)
        if why:
            return why
    if INDUSTRY in checks:
        assert settings is not None, "the industry rule reads the Industries tab"
        ind = settings.industry(str(account.get("industry") or ""))
        if ind is None and complete:
            return Reason("no_label", "no Industries label")
        if ind is not None and not ind.active:
            return Reason("industry_off", "industry switched off")
    if HUBSPOT in checks:
        for fact, text in HUBSPOT_EXCLUSIONS:
            if parse.truthy(facts.get(fact)):
                return Reason(HUBSPOT_CODES[fact], text)
    if DECLINED in checks and parse.truthy(facts.get(DECLINED_IN_SLACK)):
        return Reason("declined", DECLINED_TEXT)
    if FOUNDED in checks:
        founded = parse.number(_first(account, facts, "founded_year"))
        day = today or datetime.now().date()
        # Only the year is known: take the earliest date it allows (1 January).
        if founded is not None and day < date(int(founded) + MIN_AGE_YEARS, 1, 1):
            return Reason("founded", f"founded less than {MIN_AGE_YEARS} years ago ({int(founded)})")
    return None


def _state(account: Mapping[str, Any], facts: Mapping[str, Any], settings: Settings, complete: bool) -> Reason | None:
    # complete (verify's reading): the HQ state must be on the account row, where the hand-check and verify's doubts
    # read it; otherwise (the rescore's) a state fact stands in for a blank column.
    raw = account.get("hq_state") if complete else _first(account, facts, "hq_state")
    state = state_code(str(raw)) if raw is not None else None
    visitor = any_us_state(account)
    if not state and not (visitor and us_country(account.get("hq_country"))):
        return Reason("state_unknown", "HQ state unknown")
    if state and not visitor:
        if state in NEVER_STATES:
            return Reason("state", f"HQ in {state}, which is never contacted")
        if state not in settings.active_states():
            return Reason("state", f"HQ state {state} is not active")
    return None


SIZE_FIELDS = ("employees", "size_band")  # the Overrides rows the size rule reads


def _size(account: Mapping[str, Any], settings: Settings, complete: bool) -> Reason | None:
    eff = effective(account, settings, fields=SIZE_FIELDS)
    employees = parse.number(eff.get("employees"))
    band = str(eff.get("size_band") or "")
    if employees is None and band not in SIZE_BANDS:
        return Reason("incomplete", "employee count unknown") if complete else None
    inside = settings.size_in_range(employees) if employees is not None else settings.band_in_range(band)
    return None if inside else Reason("size", f"outside {settings.size_range_text()} employees")


def rules_hash(settings: Settings | None = None) -> str:
    """Which rules the gate applied, as 12 hex characters (the gate_version the hand-check stamps beside the labels'
    hash, so ICP precision is read per version of the gate: T1). The code's rules, and with settings the parts of the
    sheet they read other than the labels (labels_hash has those): the active states and the size range."""
    body: dict[str, Any] = {
        "rules": RULES, "stages": {name: sorted(s) for name, s in (("verify", VERIFY), ("score", SCORE),
                                                                   ("send", SEND), ("admit", ADMIT),
                                                                   ("source", SOURCE))},
        "never_states": sorted(NEVER_STATES), "min_age_years": MIN_AGE_YEARS, "partner_naics": PARTNER_NAICS,
        "partner_keywords": PARTNER_KEYWORDS, "hubspot": HUBSPOT_EXCLUSIONS, "declined": DECLINED_IN_SLACK,
    }
    if settings is not None:
        body["states"] = sorted(settings.active_states())
        body["size"] = [settings.general.min_employees, settings.general.max_employees]
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=list).encode()).hexdigest()[:12]
