"""Source "lookalike": Spill's own HubSpot customers, counted into lookalike cells.

Harry, 1 Oct 2026: "I'm happy using Spill companies from HubSpot to help inform lookalike target
lists." This replaces the Seeds tab that docs/pipeline.md deferred.

The lookalikes job (monthly, the 1st at 02:30 UK, before source_universe; ops/schedule.py). Harry, 5 Oct
2026: "The customer base for Spill is fairly static, so the whole HubSpot pull and lookalike search can
happen on a monthly cadence." It:
  1. Reads, read only, the HubSpot companies that are or were Spill customers: lifecycle stage
     customer or Churned, any subscription status, and the companies on won, onboarding or
     churned deals in the Spill 3.0 pipeline (82002613; SPEC 4). It asks for company fields only
     (COMPANY_FIELDS: domain, industry, the two employee counts, country, state, lifecycle and
     subscription status) and deal stages. It never reads a contact, a company name or anything
     about a person, and it logs counts only. HubSpot is never written (SPEC 1.2).
  2. Maps each customer to a website industry label (customer_label) and a size band
     (customer_band), and counts them into cells, industry label x size band: active and churned
     customers, the US ones apart, and a weighted strength (an active customer 1, a churned one
     CHURNED_WEIGHT, a US one US_WEIGHT times that). The cells replace lookalike_cells whole, so
     a second run over the same HubSpot data leaves the table as it was.
  3. Keeps customers out of the queue early (exclude): each customer's root domain is suppressed
     for SUPPRESS_DAYS (70: a monthly run may be missed once before an entry lapses), renewed by every
     run, so the front door (accounts.admit), v_queue and enrol refuse it before any Apollo or Clay
     spend. The daily suppression_load job reads the customers and calls exclude too (Harry, 7 Oct 2026:
     a new customer is kept out by the next night, and its leads in flight are stopped by sync_outcomes'
     sweep; suppression.load_customers), without the cells. An account already on that domain gets the HubSpot
     fact hubspot_customer (or hubspot_former_customer), which tiers it Excluded (scoring/tiers.py).
     enrol's HubSpot re-check stays as the last line, but it reads lifecyclestage only, and on
     1 Oct 2026 162 HubSpot companies at lifecycle "lead" had an Active Spill subscription.
     Former customers are kept out too (EXCLUDE_FORMER): a cold first email to a company that has
     already used Spill is the wrong message, and it is Harry's call to change.
  4. Reads 12-month headcount growth from Apollo (read_growth; Harry, 5 Oct 2026: "Sharpen up lookalike
     for scoring to include industry, size and growth rate"), in four bands: shrinking (under 0%),
     flat (0-10%), growing (10-30%) and fast (30% or more).
       * Customers: Apollo's organization search filtered by headcount growth over 12 months and by a
         list of domains, one search per band and GROWTH_CHUNK domains, fastest band first; a domain
         a band's search returns is in that band, and one no band returns is unknown. Only counts per
         industry group and band are kept (lookalike_growth, replaced whole like the cells): never a
         customer's domain or name. A search that did not reach every customer keeps the stored counts,
         so a part-read never skews them.
       * Open accounts in the queue: a fresh headcount_growth_12m fact (apollo_enrich writes it for
         apollo_enrich_groups) gives the band with no search; the same band search fills the gaps for
         accounts whose band is missing or older than GROWTH_REFRESH_DAYS. Either way the band is a
         fact (source apollo_org, headcount_growth_band; "unknown" when no band search returned the
         account, so it is not searched again for GROWTH_REFRESH_DAYS).
     Credits: a search page costs 1 when it returns a company, 0 when empty (clients/apollo.py), so a
     hundred domains cost at most 4 (one page per band). Every page goes into credit_ledger as job
     "lookalikes" (sources/apollo_credits.py). A run spends at most LOOKALIKE_GROWTH_CREDITS and never
     more than is left of the whole Apollo budget today; nothing while Apollo's balance is below
     apollo_floor. Customers come first, and only when every one can be searched within that; the
     accounts take what is left, as many as can be searched in full. Dry-run: the searches are reads,
     so they happen and their credits are recorded, as the other Apollo sources do.
  5. Writes the lookalike facts (apply): for each open account, its lookalike fit, 0 to 100 (FitModel),
     from three parts, each 0 to 1:
       * industry: the weighted strength of Spill's customers (10 to 249 staff) in the account's
         industry, over the strongest industry group's (as lookalike_priorities). The industry is the
         account's website label when Spill's own HubSpot industry field names it apart from its group
         (FIT_LABELS: Games studios, Staffing agencies, CPA firms...), else its group: HubSpot's field has
         23 values, most of them a whole group ("Tech"), so an Edtech prospect is read by its group.
       * size: the strength in the account's size band within its group, over the group's best band;
         across every group when the group has fewer than SMALL_GROUP active customers there.
       * growth: the strength in the account's growth band over the most common band, across every
         customer, or within the group once GROWTH_GROUP_MIN of its customers have a known band.
     fit = 100 x the weighted mean (INDUSTRY_WEIGHT, SIZE_WEIGHT, GROWTH_WEIGHT). A part that is not
     known for the account (its growth, or its size) is left out and the weights of the others
     renormalised, so missing data never costs points. The facts are lookalike_fit and the three parts
     (lookalike_industry_fit, lookalike_size_fit, lookalike_growth_fit, null when unknown), with a quote
     in plain words from aggregate counts only, and lookalike_active and lookalike_strength (the cells
     of its group and band) as before. The "Close match to Spill's customers" and "Some match to Spill's
     customers" signals score lookalike_fit (SPEC 7: facts into signal_events; scoring matches them).
     settings_sync runs apply() every night too, from the stored cells and growth counts with no
     outside call, so an account sourced after the 1st has its facts by the next morning.
  6. Rescores, so the exclusions and facts are in force before verify_in_clay runs.

Also here: report() for `us-outbound lookalikes show`, fit_report() for `us-outbound lookalikes fit` (the
fits, and the tier mix the build's lookalike rows would give, scored in memory: nothing is written), and
lookalike_priorities(), for the sourcing job to order industries by how many customers Spill has like them.

Not here: the "former Spill buyer now at this company" signal (design review D17). It re-purposes
personal data and waits on a legal assessment; this module reads companies only.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import budget
from us_outbound.clean.domains import is_personal_domain, root_domain
from us_outbound.clean.people import size_band
from us_outbound.clients.apollo import MAX_PER_PAGE, organizations_in, total_entries
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, Context
from us_outbound.logs import log
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.defaults import US_STATES
from us_outbound.settings.model import SIZE_BANDS, Settings, Signal, band_bounds
from us_outbound.sources import apollo_credits as credits
from us_outbound.timeparse import utc_strict_or_none

SOURCE = "lookalike"
JOB = "lookalikes"  # credit_ledger's job for the growth searches
TABLE = "lookalike_cells"
GROWTH_TABLE = "lookalike_growth"
ACTIVE_FACT, STRENGTH_FACT = "lookalike_active", "lookalike_strength"
FIT_FACT = "lookalike_fit"
INDUSTRY_FIT_FACT, SIZE_FIT_FACT, GROWTH_FIT_FACT = "lookalike_industry_fit", "lookalike_size_fit", "lookalike_growth_fit"
HUBSPOT_SOURCE = "hubspot"  # the source enrol's HubSpot re-check writes its facts under
CUSTOMER_FACT, FORMER_FACT = "hubspot_customer", "hubspot_former_customer"  # scoring/tiers.HUBSPOT_EXCLUSIONS
SUPPRESS_SOURCE = "lookalikes"
SUPPRESS_DAYS = 70  # ten weeks: the monthly job may miss one run (31 + 31 days) before a customer's entry lapses
REFRESH_DAYS = 90  # an unchanged lookalike fact is written again after this, inside the signals' 120 days

# The lookalike fit (Harry, 5 Oct 2026: "include industry, size and growth rate"). Industry leads: it says most
# about the kind of company Spill sells to, and the Focus tab is set by it; size next (05 §3.1: 44% won at 10-49
# staff, 24% at 100-249); growth least, as it is known for fewer companies and is new. A part not known for an
# account is left out and the others' weights renormalised (FitModel.fit).
INDUSTRY_WEIGHT, SIZE_WEIGHT, GROWTH_WEIGHT = 0.45, 0.35, 0.20
SMALL_GROUP = 5  # active customers at 10 to 249 staff below which a group's own size mix is too thin to read
GROWTH_GROUP_MIN = 20  # customers with a known growth band before a group's own growth mix is read
ALL = "*"  # FitModel's key for every customer, whatever the group

# 12-month headcount growth bands. The fact is source apollo_org (it is Apollo's figure), so a Signals row
# may read it too (settings/model.py SOURCE_FIELDS).
GROWTH_SOURCE, GROWTH_FACT, GROWTH_12M_FACT = "apollo_org", "headcount_growth_band", "headcount_growth_12m"
SHRINKING, FLAT, GROWING, FAST = "shrinking", "flat", "growing", "fast"
GROWTH_BANDS = (SHRINKING, FLAT, GROWING, FAST)
GROWTH_WORDS = {SHRINKING: "shrinking", FLAT: "flat (0–10% a year)", GROWING: "growing 10–30% a year",
                FAST: "growing 30% or more a year"}
# Apollo's organization search filter, in whole percent over GROWTH_MONTHS (its MCP tool's schema:
# organization_headcount_growth_range {min, max}, organization_headcount_growth_past_n_months).
# PHASE0-CONFIRM: that /mixed_companies/search takes both in the REST JSON body as they are sent here
# (clients/apollo.normalize_filters passes a {min, max} dict and a number through unchanged), whether the
# bounds are inclusive, and that "fast" needs no max and "shrinking" takes -100 as its min. The bands are
# searched fastest first, and a domain placed in one is not asked again, so with inclusive bounds a
# company at exactly 10% or 30% lands in the higher band, as the bands read (10-30% is growing).
GROWTH_RANGES: dict[str, dict[str, int]] = {
    FAST: {"min": 30}, GROWING: {"min": 10, "max": 30}, FLAT: {"min": 0, "max": 10}, SHRINKING: {"min": -100, "max": 0},
}
GROWTH_SEARCH_ORDER = (FAST, GROWING, FLAT, SHRINKING)
GROWTH_MONTHS = 12
GROWTH_CHUNK = 100  # domains per search, one page of MAX_PER_PAGE. PHASE0-CONFIRM: the most q_organization_domains_list takes
GROWTH_PAGES = 3  # pages read for one band and chunk; more means the domain filter is not doing its job
GROWTH_REFRESH_DAYS = 90  # an account's band is searched again after this
GROWTH_FACT_DAYS = 180  # how long a band (or a 12-month growth figure) counts for the fit
# Apollo credits a run may spend: at most 4 per 100 domains (a paid page per band), so up to 1,500 customer domains
# at the worst case, and the rest for queue accounts. Confirmed live 5 Oct 2026: HubSpot has 1,161 customer domains
# (588 active customers, 582 churned), under 1,500. With more than 1,500 the customers' growth is never searched at
# this cap, and each run's summary says so (growth.customers.kept).
LOOKALIKE_GROWTH_CREDITS = 60
GROWTH_MAX_ERRORS = 5  # Apollo errors before the growth searches stop
# A guard until the filter is confirmed (PHASE0-CONFIRM above): if the first band searched (30% or more a year)
# returns most of the domains asked, Apollo is taken to have ignored the growth filter, and nothing from the
# run's searches is used.
FILTER_CHECK_MIN, FILTER_CHECK_SHARE = 20, 0.6
QUEUE_STATUSES = ("new", "queued", "verified")  # the accounts whose growth is searched (apollo_universe.OPEN_STATUSES)
OUT_OF_QUEUE_TIERS = frozenset({"Excluded", "Held"})
APOLLO_ORG_URL = "https://app.apollo.io/#/organizations/{}"
EXCLUDE_FORMER = True
OPEN_STATUSES = ("new", "queued", "verified", "enrolled", "engaged")  # score.SCORED_STATUSES
CHUNK = 1000
SPILL_DOMAIN = "spill.chat"

# What the job reads from HubSpot: company fields only (Harry, 1 Oct 2026). company_industry is
# Spill's own industry dropdown; industry is HubSpot's standard one, read in case it is ever filled.
COMPANY_FIELDS = (
    "domain", "company_industry", "industry", "numberofemployees", "employees_covered", "country", "state",
    "lifecyclestage", "subscription_status",
)
CUSTOMER_STAGE = "customer"
CHURNED_STAGE = "23168024"  # portal 8481055's own "Churned" lifecycle stage (its property options, 1 Oct 2026)
SPILL3_PIPELINE = "82002613"  # used when the General tab's hubspot_pipeline_id is blank
ACTIVE_DEAL_STAGES = {"154381891": "Closed won", "258214822": "Onboarding"}  # Spill 3.0 (05 §2)
CHURNED_DEAL_STAGES = {"1287546140": "Churned"}

ACTIVE, CHURNED = "active", "churned"
ACTIVE_WEIGHT, CHURNED_WEIGHT, US_WEIGHT = 1.0, 0.25, 2.0
# Spill's evidence bands (05 §3.1). A prospect's 10-19 and 20-49 both read 10-49: HubSpot's enriched
# headcount cannot tell them apart, and the cells stay big enough to mean something.
BANDS = ("1-9", "10-49", "50-99", "100-249", "250+")
UNKNOWN = "unknown"
# A prospect's size band as the cells read it. A band outside 10 to 249 (General min_employees or max_employees
# set wider; Harry, 6 Oct 2026) reads as the nearest cell: under 10 as 10-49, 250 and over as 100-249.
PROSPECT_BANDS = {band: "10-49" if band_bounds(band)[1] < 50 else "50-99" if band_bounds(band)[1] < 100 else "100-249"
                  for band in SIZE_BANDS}
TARGET_BANDS = ("10-49", "50-99", "100-249")  # the sizes the system contacts (SPEC 2: 10 to 249)
# HubSpot's enriched numberofemployees is mostly the top of a range: 10 is 1-10, 50 is 11-50, 250 is
# 51-250, 1000 is 251-1,000 (1 Oct 2026: 413 at 50, 179 at 10, 165 at 250). 250 spans two bands: unknown.
BUCKET_BANDS: dict[int, str | None] = {
    10: "1-9", 50: "10-49", 250: None, 1000: "250+", 5000: "250+", 10000: "250+", 50000: "250+",
}

# Spill's company_industry values (portal 8481055, 1 Oct 2026) -> website industry label. None: no label
# fits. IT Services is left out on purpose: the tech rows exclude IT-services NAICS (Appendix A.4), so
# Spill's IT-services customers must not lift tech prospects.
HUBSPOT_INDUSTRY_LABELS: dict[str, str | None] = {
    "tech": "Technology & Startups",
    "computer games": "Games studios",
    "it services": None,
    "creative agency": "Marketing & Creative Agencies",
    "law": "Legal Teams",
    "ngo / charity": "Nonprofits",
    "consultancy": "Management consulting",
    "recruitment": "Staffing agencies",
    "engineering": "Engineering & design firms",
    "architecture firm": "Architecture studios",
    "accounting firm": "CPA firms",
    "finance": "Financial Services",
    "venture capital": "Private equity & VC",
    "hospitality": "Hospitality",
    "direct to consumer": "DTC brands",
    "retail": "Retail & E-commerce",
    "manufacturing": "Manufacturing & Industrial",
    "healthcare": "Healthcare",
    "school": "Education",
    "construction & utilities": "Construction & Trades",
    "mom & pop biz": "Small Businesses",
    "civil services": None,
    "other": None,
}
# Labels Spill's own industry field names apart (the values above): the lookalike fit reads an account in one
# of these by that label's customers, not its whole group's (fit_industry). The standard values below are left
# out, as they overlap Spill's own: HubSpot's "design" is not apart from Spill's "creative agency", for one.
FIT_LABELS = frozenset(v for v in HUBSPOT_INDUSTRY_LABELS.values() if v)
HUBSPOT_INDUSTRY_LABELS |= {
    # HubSpot's standard industry values (the industry property, read when company_industry is blank;
    # 1 Oct 2026's first run left these unmapped). IT services stays out, as above.
    "computer software": "Technology & Startups",
    "information technology and services": None,
    "marketing and advertising": "Marketing & Creative Agencies",
    "design": "Creative & design agencies",
    "architecture planning": "Architecture studios",
    "human resources": "HR consulting",
    "pharmaceuticals": "Pharma & medical devices",
    "renewables environment": "Energy & utilities",
    "oil energy": "Energy & utilities",
    "mechanical or industrial engineering": "Engineering & design firms",
    "chemicals": "Manufacturing & Industrial",
    "leisure travel tourism": "Hospitality",
    "fund raising": "Nonprofits",
}
US_COUNTRIES = frozenset({"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."})


class NoCustomers(RuntimeError):
    """HubSpot returned no customer companies: surely a fault, so the stored cells are kept."""


@dataclass(frozen=True)
class Customer:
    """One Spill customer company, as the cells count it. No name and nothing about a person."""

    status: str  # active or churned
    domain: str | None  # root domain, mapped through the alias table; None when HubSpot has none
    label: str  # website industry label, "" when none fits
    group: str  # the label's industry group
    band: str  # one of BANDS, or unknown
    us: bool
    hubspot_industry: str = ""

    @property
    def weight(self) -> float:
        return (ACTIVE_WEIGHT if self.status == ACTIVE else CHURNED_WEIGHT) * (US_WEIGHT if self.us else 1.0)


# -- one company ------------------------------------------------------------------------------


def _number(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def band_of(employees: float) -> str:
    n = int(round(employees))
    if n < 10:
        return "1-9"
    if n <= 49:
        return "10-49"
    if n <= 99:
        return "50-99"
    if n <= 249:
        return "100-249"
    return "250+"


def customer_band(props: Mapping[str, Any]) -> str:
    """The size band: the employees covered by Spill's contract, else HubSpot's enriched headcount.

    Covered staff come first: exact, and what Spill's own win rates by size are counted on
    (05 §3.1). An enriched headcount that is the top of a range is read as that range
    (BUCKET_BANDS), and is unknown when the range spans two bands.
    """
    covered = _number(props.get("employees_covered"))
    if covered is not None and covered > 0:
        return band_of(covered)
    n = _number(props.get("numberofemployees"))
    if n is None or n <= 0:
        return UNKNOWN
    if n.is_integer() and int(n) in BUCKET_BANDS:
        return BUCKET_BANDS[int(n)] or UNKNOWN
    return band_of(n)


def is_us(props: Mapping[str, Any]) -> bool:
    """HQ in the United States: HubSpot's country, else a USPS state code when the country is blank."""
    country = str(props.get("country") or "").strip().casefold()
    if country:
        return country in US_COUNTRIES
    return str(props.get("state") or "").strip().upper() in US_STATES


def customer_status(props: Mapping[str, Any], deal: str | None = None) -> str | None:
    """active, churned, or None when the company is not a Spill customer and never was.

    The subscription status (from billing) decides first; then a Spill 3.0 deal that is won or
    onboarding (a customer whose status is not set yet); then the Churned lifecycle stage or a
    churned deal; then lifecycle customer.
    """
    sub = str(props.get("subscription_status") or "").strip().casefold()
    if sub == ACTIVE:
        return ACTIVE
    if sub == CHURNED:
        return CHURNED
    if deal == ACTIVE:
        return ACTIVE
    stage = str(props.get("lifecyclestage") or "").strip().casefold()
    if stage == CHURNED_STAGE or deal == CHURNED:
        return CHURNED
    if stage == CUSTOMER_STAGE:
        return ACTIVE
    return None


def _tab_match(text: str, settings: Settings) -> str:
    """The Industries label a HubSpot industry names: the label itself, else the row whose label or Apollo
    keywords it contains as whole words (the longest match; ties to the lower priority, then tab order)."""
    t = text
    for ind in settings.industries:
        if ind.industry.casefold() == t.casefold():
            return ind.industry
    best: tuple[int, int, int] | None = None
    label = ""
    for n, ind in enumerate(settings.industries):
        found = find_terms(t, (ind.industry, *ind.apollo_keywords))
        if not found:
            continue
        key = (-max(len(m.term) for m in found), ind.priority, n)
        if best is None or key < best:
            best, label = key, ind.industry
    return label


def customer_label(props: Mapping[str, Any], settings: Settings, account_label: str = "") -> str:
    """The website industry label for a customer, or "" when none fits.

      1. the label the pipeline already gave an account on the same domain (from its NAICS and
         Apollo keywords against the Industries tab);
      2. HubSpot's industry (company_industry, else industry) through HUBSPOT_INDUSTRY_LABELS;
      3. that text matched against the Industries tab's labels and Apollo keywords.
    HubSpot companies carry no NAICS code in portal 8481055, so 1 is where NAICS counts.
    """
    if account_label and settings.industry(account_label):
        return account_label
    for col in ("company_industry", "industry"):
        text = " ".join(str(props.get(col) or "").replace("_", " ").split())
        if not text:
            continue
        key = text.casefold()  # "COMPUTER_GAMES" reads as "computer games"
        if key in HUBSPOT_INDUSTRY_LABELS:
            label = HUBSPOT_INDUSTRY_LABELS[key]
            if label is None:
                return ""
            if settings.industry(label):
                return label
        matched = _tab_match(text, settings)
        if matched:
            return matched
    return ""


# -- reading HubSpot ------------------------------------------------------------------------------


def _deal_statuses(ctx: Context) -> dict[str, str]:
    """company id -> active (a won or onboarding Spill 3.0 deal) or churned (only churned deals)."""
    hs = ctx.clients.hubspot
    pipeline = ctx.settings.general.hubspot_pipeline_id.strip() or SPILL3_PIPELINE
    stages = sorted({*ACTIVE_DEAL_STAGES, *CHURNED_DEAL_STAGES})
    groups = [[{"propertyName": "pipeline", "operator": "EQ", "value": pipeline},
               {"propertyName": "dealstage", "operator": "IN", "values": stages}]]
    out: dict[str, str] = {}
    for deal in hs.iter_deals(groups, ["dealstage"]):
        stage = str(deal["properties"].get("dealstage") or "")
        status = ACTIVE if stage in ACTIVE_DEAL_STAGES else CHURNED if stage in CHURNED_DEAL_STAGES else None
        if status is None:
            continue
        for company_id in hs.deal_company_ids(deal["id"]):
            if out.get(company_id) != ACTIVE:
                out[company_id] = status
    return out


def _chunks(items: list[str], n: int = CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _aliases(ctx: Context) -> dict[str, str]:
    return {str(r["alias"]).lower(): str(r["root_domain"]).lower()
            for r in ctx.store.select("domain_aliases") if r.get("alias") and r.get("root_domain")}


def _canonical(domain: str | None, aliases: Mapping[str, str]) -> str | None:
    """The account domain for a HubSpot domain: its root, through the alias table; None for none to match."""
    root = root_domain(domain)
    if not root or is_personal_domain(root) or root == SPILL_DOMAIN:
        return None
    seen = {root}
    while root in aliases and aliases[root] not in seen and len(seen) < 5:
        root = aliases[root]
        seen.add(root)
    return root


def read_customers(ctx: Context) -> list[Customer]:
    """Every company that is or was a Spill customer, once per root domain (an active record wins)."""
    hs = ctx.clients.hubspot
    groups = [
        [{"propertyName": "lifecyclestage", "operator": "EQ", "value": CUSTOMER_STAGE}],
        [{"propertyName": "lifecyclestage", "operator": "EQ", "value": CHURNED_STAGE}],
        [{"propertyName": "subscription_status", "operator": "HAS_PROPERTY"}],
    ]
    companies = {c["id"]: c["properties"] for c in hs.iter_companies(groups, COMPANY_FIELDS)}
    deals = _deal_statuses(ctx)
    missing = sorted(set(deals) - set(companies))
    if missing:
        companies.update({c["id"]: c["properties"] for c in hs.companies_by_id(missing, COMPANY_FIELDS)})

    aliases = _aliases(ctx)
    found = []
    for company_id, props in companies.items():
        status = customer_status(props, deals.get(company_id))
        if status is not None:
            found.append((status, _canonical(props.get("domain"), aliases), props))
    domains = sorted({d for _, d, _ in found if d})
    account_labels: dict[str, str] = {}
    for chunk in _chunks(domains):
        for a in ctx.store.select("accounts", {"domain": chunk}):
            if a.get("industry"):
                account_labels[str(a["domain"])] = str(a["industry"])

    settings = ctx.settings
    by_domain: dict[str, Customer] = {}
    without_domain: list[Customer] = []
    for status, domain, props in found:
        label = customer_label(props, settings, account_labels.get(domain or "", ""))
        ind = settings.industry(label) if label else None
        c = Customer(status, domain, label, ind.industry_group if ind else "", customer_band(props), is_us(props),
                     str(props.get("company_industry") or props.get("industry") or "").strip())
        if domain is None:
            without_domain.append(c)
        elif domain not in by_domain or (by_domain[domain].status == CHURNED and status == ACTIVE):
            by_domain[domain] = c
    return [*by_domain.values(), *without_domain]


# -- the cells -------------------------------------------------------------------------------------


def cell_id(label: str, band: str) -> str:
    return f"{label or '-'}|{band}"


def build_cells(customers: Iterable[Customer]) -> dict[str, dict]:
    """cell_id -> the lookalike_cells row (without computed_at and run_id)."""
    cells: dict[str, dict] = {}
    for c in customers:
        key = cell_id(c.label, c.band)
        row = cells.setdefault(key, {
            "cell_id": key, "industry_label": c.label or None, "industry_group": c.group or None,
            "size_band": c.band, "active_customers": 0, "churned_customers": 0, "us_active": 0,
            "us_churned": 0, "strength": 0.0,
        })
        prefix = "active" if c.status == ACTIVE else "churned"
        row[f"{prefix}_customers"] += 1
        row[f"us_{prefix}"] += int(c.us)
        row["strength"] += c.weight
    for row in cells.values():
        row["strength"] = round(row["strength"], 2)
    return cells


def store_cells(ctx: Context, cells: Mapping[str, dict], table: str = TABLE) -> dict[str, int]:
    """Replace lookalike_cells (or lookalike_growth) with these rows: the ones gone are deleted, the rest upserted."""
    old = {r["cell_id"] for r in ctx.store.select(table)}
    gone = sorted(old - set(cells))
    if gone:
        ctx.store.delete(table, {"cell_id": gone})
    rows = [{**cells[k], "computed_at": ctx.now, "run_id": ctx.run_id} for k in sorted(cells)]
    if rows:
        ctx.store.upsert(table, rows)
    return {"cells": len(rows), "removed": len(gone)}


def build_growth(customers: Iterable[Customer], bands: Mapping[str, str]) -> dict[str, dict]:
    """cell_id -> the lookalike_growth row: customers counted by industry group and growth band (unknown when
    no band search returned them). Counts only: the domains in `bands` are never stored."""
    rows: dict[str, dict] = {}
    for c in customers:
        band = (bands.get(c.domain) if c.domain else None) or UNKNOWN
        key = f"{c.group or '-'}|{band}"
        row = rows.setdefault(key, {"cell_id": key, "industry_group": c.group or None, "growth_band": band,
                                    "active_customers": 0, "churned_customers": 0, "strength": 0.0})
        row["active_customers" if c.status == ACTIVE else "churned_customers"] += 1
        row["strength"] += c.weight
    for row in rows.values():
        row["strength"] = round(row["strength"], 2)
    return rows


def group_cells(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """(industry group, size band) -> the summed counts and strength of that group's cells."""
    out: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"active": 0, "churned": 0, "us_active": 0, "us_churned": 0, "strength": 0.0})
    for r in rows:
        group, band = r.get("industry_group"), r.get("size_band")
        if not group or not band:
            continue
        g = out[(str(group), str(band))]
        g["active"] += int(r.get("active_customers") or 0)
        g["churned"] += int(r.get("churned_customers") or 0)
        g["us_active"] += int(r.get("us_active") or 0)
        g["us_churned"] += int(r.get("us_churned") or 0)
        g["strength"] += float(r.get("strength") or 0.0)
    return dict(out)


# -- keeping customers out ---------------------------------------------------------------------------


def _latest(events: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], tuple[datetime, Any]]:
    """(account_id, fact) -> (observed_at, value) of the newest event."""
    out: dict[tuple[str, str], tuple[datetime, Any]] = {}
    floor = datetime(1970, 1, 1, tzinfo=UTC)
    for e in events:
        key = (str(e.get("account_id")), str(e.get("fact")))
        t = utc_strict_or_none(e.get("observed_at")) or floor
        if key not in out or t >= out[key][0]:
            out[key] = (t, e.get("value"))
    return out


def exclude(ctx: Context, customers: Iterable[Customer]) -> dict[str, int]:
    """Suppress customer domains, and mark accounts already on them, so no customer is ever queued."""
    store, now = ctx.store, ctx.now
    reasons: dict[str, str] = {}
    for c in customers:
        if c.domain and (c.status == ACTIVE or EXCLUDE_FORMER) and reasons.get(c.domain) != CUSTOMER_FACT:
            reasons[c.domain] = CUSTOMER_FACT if c.status == ACTIVE else FORMER_FACT
    domains = sorted(reasons)

    expires = now + timedelta(days=SUPPRESS_DAYS)
    existing: dict[str, dict] = {}
    for chunk in _chunks(domains):
        for r in store.select("suppression", {"domain": chunk, "email_sha256": None}):
            existing[r["domain"]] = r
    rows, new = [], 0
    for domain in domains:
        old = existing.get(domain)
        old_expiry = utc_strict_or_none(old.get("expires_at")) if old is not None else None
        if old is not None and (old_expiry is None or old_expiry >= expires):
            continue  # indefinite, or already suppressed for longer
        if old is None or (old_expiry is not None and old_expiry <= now):
            new += 1  # kept out from now: a customer since the last run (suppression_load reads them daily)
        rows.append({
            "email_sha256": None, "domain": domain, "reason": reasons[domain], "source": SUPPRESS_SOURCE,
            "added_at": old.get("added_at") if old and old.get("source") == SUPPRESS_SOURCE else now,
            "expires_at": expires,
        })
    if rows:
        store.upsert("suppression", rows)

    accounts = [a for chunk in _chunks(domains) for a in store.select("accounts", {"domain": chunk})]
    ids = [a["account_id"] for a in accounts]
    latest = _latest(e for chunk in _chunks(ids) for e in store.select(
        "signal_events", {"account_id": chunk, "source": HUBSPOT_SOURCE}))
    facts = []
    for a in accounts:
        aid, fact = a["account_id"], reasons[str(a["domain"])]
        if latest.get((aid, fact), (None, None))[1] is True or latest.get((aid, CUSTOMER_FACT), (None, None))[1] is True:
            continue
        quote = ("A Spill customer in HubSpot (lookalikes job)" if fact == CUSTOMER_FACT
                 else "A former Spill customer in HubSpot (lookalikes job)")
        facts.append({"event_id": new_id(), "account_id": aid, "source": HUBSPOT_SOURCE, "fact": fact,
                      "value": True, "quote": quote, "source_url": "", "observed_at": now})
    if facts:
        store.insert("signal_events", facts)
    return {"domains": len(domains), "suppressed": len(rows), "new": new, "accounts_marked": len(facts)}


# -- 12-month headcount growth (Apollo) -------------------------------------------------------------------


def growth_band_of(growth: Any) -> str | None:
    """The band of a headcount_growth_12m figure, a fraction (0.12 is 12%; PHASE0-CONFIRM in apollo_universe)."""
    g = _number(growth)
    if g is None:
        return None
    pct = round(g * 100, 6)
    return SHRINKING if pct < 0 else FLAT if pct < 10 else GROWING if pct < 30 else FAST


def _age(t: datetime, today: date) -> int:
    return (today - t.astimezone(UK).date()).days


def account_growth(latest: Mapping[tuple[str, str], tuple[datetime, Any]], account_id: str, today: date) -> str | None:
    """An account's growth band for the fit: the newer known one of its headcount_growth_band and its
    headcount_growth_12m, within GROWTH_FACT_DAYS; None when neither says (an "unknown" band says nothing)."""
    known = []
    band = latest.get((account_id, GROWTH_FACT))
    if band is not None and band[1] in GROWTH_BANDS:
        known.append((band[0], str(band[1])))
    figure = latest.get((account_id, GROWTH_12M_FACT))
    if figure is not None and (b := growth_band_of(figure[1])):
        known.append((figure[0], b))
    known = [(t, b) for t, b in known if _age(t, today) <= GROWTH_FACT_DAYS]
    return max(known, key=lambda tb: tb[0])[1] if known else None


def growth_facts(store: Any, account_ids: Sequence[str]) -> dict[tuple[str, str], tuple[datetime, Any]]:
    """(account_id, fact) -> the newest headcount_growth_band and headcount_growth_12m facts (source apollo_org)."""
    return _latest(e for chunk in _chunks(list(account_ids)) for e in store.select(
        "signal_events", {"account_id": chunk, "source": GROWTH_SOURCE, "fact": [GROWTH_FACT, GROWTH_12M_FACT]}))


def _band_fact(account_id: str, band: str, quote: str, now: datetime, org_id: str = "") -> dict:
    return {"event_id": new_id(), "account_id": account_id, "source": GROWTH_SOURCE, "fact": GROWTH_FACT,
            "value": band, "quote": quote, "source_url": APOLLO_ORG_URL.format(org_id) if org_id else "",
            "observed_at": now}


def growth_todo(ctx: Context, skip: Iterable[str] = ()) -> tuple[list[dict], list[dict]]:
    """(band facts from fresh 12-month growth figures, the queue accounts whose band is due a search).

    The queue accounts (new, queued or verified, not Excluded or Held) with a domain, other than the
    domains in skip (Spill's customers, searched as customers). An account with a headcount_growth_12m
    fact younger than GROWTH_REFRESH_DAYS takes its band from it, at no credit: the figure wins and the
    band search only fills gaps. The rest are due a search when their band is missing or older than
    GROWTH_REFRESH_DAYS, never-searched first, then the oldest, then the Focus tab's groups and queue order.
    """
    from us_outbound.enrol import focus, queue

    s, today, skipped = ctx.settings, ctx.today_uk(), set(skip)
    rows = [a for a in ctx.store.select("accounts", {"status": list(QUEUE_STATUSES)})
            if a.get("domain") and a.get("tier") not in OUT_OF_QUEUE_TIERS and a["domain"] not in skipped]
    latest = growth_facts(ctx.store, [a["account_id"] for a in rows])
    facts: list[dict] = []
    due: list[tuple[tuple, dict]] = []
    for a in rows:
        aid = a["account_id"]
        band, figure = latest.get((aid, GROWTH_FACT)), latest.get((aid, GROWTH_12M_FACT))
        fresh_band = band is not None and _age(band[0], today) < GROWTH_REFRESH_DAYS
        from_figure = growth_band_of(figure[1]) if figure and _age(figure[0], today) < GROWTH_REFRESH_DAYS else None
        if from_figure:
            if not (fresh_band and band[1] == from_figure):
                pct = float(_number(figure[1]) or 0.0)
                facts.append(_band_fact(aid, from_figure, f"Apollo: headcount {pct:+.0%} over 12 months, so "
                                                          f"{GROWTH_WORDS[from_figure]}", ctx.now))
            continue
        if fresh_band:
            continue
        searched = band[0] if band else None
        due.append(((searched is not None, searched or datetime.min.replace(tzinfo=UTC),
                     focus.group_rank(s.industry_group_of(a), s), queue.order_key(a, s)), a))
    due.sort(key=lambda kv: kv[0])
    return facts, [a for _, a in due]


@dataclass
class _Growth:
    """One run's growth searches."""

    left: float  # the most the run may spend: LOOKALIKE_GROWTH_CREDITS, within today's Apollo budget
    spent: float = 0.0
    pages: int = 0
    errors: list[str] = field(default_factory=list)
    stopped: str = ""

    def allows(self) -> bool:
        return self.left - self.spent >= 1.0

    def domains_affordable(self) -> int:
        """The most domains that can still be searched in full: a page per band and GROWTH_CHUNK domains."""
        return int((self.left - self.spent + 1e-9) // len(GROWTH_SEARCH_ORDER)) * GROWTH_CHUNK


def worst_cost(domains: int) -> int:
    """Credits a full search of this many domains can cost at most: one paid page per band and chunk."""
    return len(GROWTH_SEARCH_ORDER) * math.ceil(domains / GROWTH_CHUNK)


def _org_domains(org: Mapping[str, Any]) -> set[str]:
    """The root domains a search row answers for: its primary domain (organizations), domain (accounts), website."""
    return {d for v in (org.get("primary_domain"), org.get("domain"), org.get("website_url"))
            if v and (d := root_domain(str(v)))}


def _search_band(ctx: Context, chunk: Sequence[str], band: str, g: _Growth, who: str) -> dict[str, str] | None:
    """domain -> Apollo organization id, for the chunk's domains Apollo puts in this growth band; None when not
    every page could be read (the chunk's domains then stay unsearched for this band)."""
    asked, out, page = set(chunk), {}, 1
    filters = {"q_organization_domains_list": list(chunk), "organization_headcount_growth_range": dict(GROWTH_RANGES[band]),
               "organization_headcount_growth_past_n_months": GROWTH_MONTHS}
    while True:
        if not g.allows():
            g.stopped = g.stopped or "the run's Apollo credits are used"
            return None
        try:
            body = ctx.clients.apollo.search_organizations(filters, page=page, per_page=MAX_PER_PAGE)
        except ApiError as exc:
            if exc.status in (401, 403):
                raise  # the key is wrong: every search would fail
            g.errors.append(f"growth {band} search for {who}, page {page}: {str(exc)[:200]}")
            if len(g.errors) >= GROWTH_MAX_ERRORS:
                g.stopped = f"{GROWTH_MAX_ERRORS} Apollo errors"
            return None
        orgs, total = organizations_in(body), total_entries(body)
        spent = 1.0 if orgs else 0.0
        # The note carries counts only: never a customer's domain.
        credits.record(ctx, JOB, spent, note=json.dumps({"growth": band, "for": who, "page": page, "asked": len(chunk),
                                                         "results": len(orgs), "total": total}))
        g.spent += spent
        g.pages += 1
        for org in orgs:
            oid = str(org.get("organization_id") or org.get("id") or "")
            for d in _org_domains(org) & asked:
                out.setdefault(d, oid)
        if not orgs or total is None or total <= page * MAX_PER_PAGE:
            return out
        if page >= GROWTH_PAGES:
            g.errors.append(f"growth {band} search for {who}: over {GROWTH_PAGES} pages for {len(chunk)} domains")
            return None
        page += 1


def search_growth(ctx: Context, domains: Sequence[str], g: _Growth, who: str) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """(domain -> (growth band, Apollo organization id), the domains searched in full).

    Band by band, fastest first, over the domains no earlier band placed, GROWTH_CHUNK at a time. A domain
    is searched in full when it was placed, or every band's search that asked for it was read to the end;
    only then does "no band" mean unknown. Nothing is returned when the first band looks unfiltered
    (FILTER_CHECK_SHARE).
    """
    found: dict[str, tuple[str, str]] = {}
    unread: set[str] = set()
    for band in GROWTH_SEARCH_ORDER:
        for chunk in _chunks([d for d in domains if d not in found], GROWTH_CHUNK):
            got = None if g.stopped else _search_band(ctx, chunk, band, g, who)
            if got is None:
                unread.update(chunk)
                continue
            for d, oid in got.items():
                found[d] = (band, oid)
        if band == GROWTH_SEARCH_ORDER[0] and len(domains) >= FILTER_CHECK_MIN \
                and len(found) >= FILTER_CHECK_SHARE * len(domains):
            g.stopped = (f"{len(found)} of {len(domains)} {who} came back as growing {GROWTH_RANGES[band]['min']}% or "
                         "more a year: Apollo seems to ignore the growth filter, so nothing from it is used")
            g.errors.append(g.stopped)
            return {}, set()
    return found, set(found) | (set(domains) - unread)


def read_growth(ctx: Context, customers: Sequence[Customer]) -> dict[str, Any]:
    """Customers' and queue accounts' growth bands from Apollo (see the module docstring, step 4)."""
    store, now = ctx.store, ctx.now
    customer_domains = sorted({c.domain for c in customers if c.domain})
    facts, todo = growth_todo(ctx, customer_domains)
    out: dict[str, Any] = {"customers": {"domains": len(customer_domains), "stored": False},
                           "accounts": {"from_growth_figures": len(facts), "due_a_search": len(todo)}}

    def done(**extra: Any) -> dict[str, Any]:
        if facts:
            store.insert("signal_events", facts)
        out["accounts"]["band_facts"] = len(facts)
        out.update(extra)
        return out

    floor = credits.floor_reason(ctx)
    if floor:
        return done(skipped=floor, credits=0.0)
    whole = budget.monthly(store, ctx.settings, credits.SYSTEM, now)
    g = _Growth(left=min(float(LOOKALIKE_GROWTH_CREDITS), whole.left_today))

    # Customers: all of them or none, so a part-read never skews the stored counts.
    c = out["customers"]
    if customer_domains and worst_cost(len(customer_domains)) > g.left:
        c["kept"] = (f"a full search could cost {worst_cost(len(customer_domains))} credits and the run has "
                     f"{g.left:.0f}; the stored growth counts are kept")
    elif customer_domains:
        found, searched = search_growth(ctx, customer_domains, g, "customers")
        c.update(placed=len(found), bands=dict(Counter(b for b, _ in found.values())))
        if g.stopped or len(searched) < len(customer_domains):
            c["kept"] = "not every customer was searched; the stored growth counts are kept"
        else:
            c["unknown"] = len(customer_domains) - len(found)
            c.update(store_cells(ctx, build_growth(customers, {d: b for d, (b, _) in found.items()}), GROWTH_TABLE))
            c["stored"] = True

    # Accounts: as many as the credits left can search in full.
    a = out["accounts"]
    take = [] if g.stopped else todo[: g.domains_affordable()]
    if take:
        by_domain = {str(acct["domain"]): acct for acct in take}
        found, searched = search_growth(ctx, list(by_domain), g, "accounts")
        for d in sorted(searched):
            aid = by_domain[d]["account_id"]
            if d in found:
                band, oid = found[d]
                facts.append(_band_fact(aid, band, f"Apollo search by 12-month headcount growth: {GROWTH_WORDS[band]}",
                                        now, oid))
            else:
                facts.append(_band_fact(aid, UNKNOWN, "Apollo search by 12-month headcount growth: in no band", now))
        a.update(searched=len(searched), placed=len(found), unknown=len(searched) - len(found))
    a["left_for_next_run"] = len(todo) - a.get("searched", 0)
    return done(credits=g.spent, pages=g.pages, budget_today=round(g.left, 2), stopped_by=g.stopped or None,
                errors=g.errors[:10])


# -- the lookalike facts ----------------------------------------------------------------------------------


def prospect_band(account: Mapping[str, Any]) -> str | None:
    """The account's size band as the cells count it (10-49, 50-99 or 100-249), or None."""
    band = str(account.get("size_band") or "").strip()
    if band not in PROSPECT_BANDS:
        band = size_band(account.get("employees")) or ""
    return PROSPECT_BANDS.get(band)


def _quote(group: str, band: str, g: Mapping[str, float] | None) -> str:
    if not g:
        return f"No Spill customers counted in {group} at {band} staff (HubSpot)."
    return (f"Spill's HubSpot customers in {group} at {band} staff: {int(g['active'])} active and "
            f"{int(g['churned'])} churned; {int(g['us_active'])} of the active ones are in the US.")


def fit_industry(label: str, group: str) -> str:
    """The industry the fit reads an account (or a cell) by: its label when Spill's HubSpot field names it apart
    from its group (FIT_LABELS), else its group. A group's own umbrella label reads as the group."""
    return label if label in FIT_LABELS and label != group else group


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _rank_words(rank: int, noun: str) -> str:
    """"the strongest industry", "the 2nd strongest industry"."""
    return f"the {noun}" if rank == 1 else f"the {_ordinal(rank)} {noun}"


def _staff(band: str) -> str:
    return f"{band.replace('-', '–')} staff"


@dataclass(frozen=True)
class Fit:
    """One account's lookalike fit: the parts (0 to 1, None when not known) and the fit (0 to 100)."""

    fit: int
    industry: float
    size: float | None
    growth: float | None
    quote: str


class FitModel:
    """What the fit reads: the stored cells (industry and size) and the customers' growth counts, in aggregate."""

    def __init__(self, cells: Iterable[Mapping[str, Any]], growth: Iterable[Mapping[str, Any]] = ()):
        cells = list(cells)
        self.cells = len(cells)
        self.groups = group_cells(cells)  # (group, band) -> counts: lookalike_active and lookalike_strength
        self.group_strength: dict[str, float] = defaultdict(float)  # 10 to 249 staff, as lookalike_priorities
        self.label_strength: dict[str, float] = defaultdict(float)
        self.sizes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))  # group or ALL -> band
        self.size_active: dict[str, int] = defaultdict(int)
        for r in cells:
            band = r.get("size_band")
            if band not in TARGET_BANDS:
                continue
            group, label = str(r.get("industry_group") or ""), str(r.get("industry_label") or "")
            strength = float(r.get("strength") or 0.0)
            self.sizes[ALL][band] += strength
            if group:
                self.group_strength[group] += strength
                self.sizes[group][band] += strength
                self.size_active[group] += int(r.get("active_customers") or 0)
            if label:
                self.label_strength[label] += strength
        # group or ALL -> growth band -> [customers, strength]
        self.growth: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
        for r in growth:
            band = str(r.get("growth_band") or "")
            n = int(r.get("active_customers") or 0) + int(r.get("churned_customers") or 0)
            for key in {ALL, str(r.get("industry_group") or "") or ALL}:
                self.growth[key][band][0] += n
                self.growth[key][band][1] += float(r.get("strength") or 0.0)

    def _industry(self, label: str, group: str) -> tuple[float, str]:
        key = fit_industry(label, group)
        strength = self.group_strength.get(key, 0.0) if key == group else self.label_strength.get(key, 0.0)
        top = max(self.group_strength.values(), default=0.0)
        value = round(min(1.0, strength / top), 2) if top else 0.0
        if strength <= 0:
            return value, f"none in {key} ({value:.2f})"
        rank = 1 + sum(s > strength for s in self.group_strength.values())
        return value, f"{key} is {_rank_words(rank, 'strongest industry')} ({value:.2f})"

    def _size(self, group: str, label: str, band: str | None) -> tuple[float | None, str]:
        if band is None:
            return None, "size unknown, so left out"
        thin = self.size_active.get(group, 0) < SMALL_GROUP
        dist = self.sizes.get(ALL if thin else group, {})
        best = max(dist.values(), default=0.0)
        mine = dist.get(band, 0.0)
        value = round(mine / best, 2) if best else 0.0
        if mine <= 0:
            text = f"none at {_staff(band)}"
        else:
            rank = 1 + sum(s > mine for b, s in dist.items() if b in TARGET_BANDS)
            common = "most common size" if rank == 1 else f"{_ordinal(rank)} most common size"
            if thin:
                text = f"{_staff(band)} is their {common}"
            elif fit_industry(label, group) == group:
                text = f"{_staff(band)} is its {common}"
            else:  # the industry read is a label: the size mix is its group's
                text = f"{_staff(band)} is the {common} in {group}"
        if thin:
            text += f" overall, as {group} has under {SMALL_GROUP} active"
        return value, f"{text} ({value:.2f})"

    def _growth(self, group: str, band: str | None) -> tuple[float | None, str]:
        if band not in GROWTH_BANDS:
            return None, "its growth unknown, so left out"
        own = self.growth.get(group, {})
        in_group = sum(own[b][0] for b in GROWTH_BANDS if b in own) >= GROWTH_GROUP_MIN
        dist = own if in_group else self.growth.get(ALL, {})
        known = {b: dist[b] for b in GROWTH_BANDS if b in dist and dist[b][0]}
        customers = sum(n for n, _ in known.values())
        if not customers:
            return None, "customers' growth not known yet, so left out"
        best = max(s for _, s in known.values())
        n, s = known.get(band, (0, 0.0))
        value = round(s / best, 2) if best else 0.0
        whom = "its customers" if in_group else "them"
        return value, f"{GROWTH_WORDS[band]} like {n / customers:.0%} of {whom} ({value:.2f})"

    def fit(self, label: str, group: str, band: str | None, growth: str | None) -> Fit:
        """The fit of an account in this industry label and group, size band (10-49, 50-99, 100-249) and growth band."""
        industry, i_text = self._industry(label, group)
        size, s_text = self._size(group, label, band)
        grown, g_text = self._growth(group, growth)
        parts = [(w, v) for w, v in ((INDUSTRY_WEIGHT, industry), (SIZE_WEIGHT, size), (GROWTH_WEIGHT, grown))
                 if v is not None]
        fit = round(100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts))
        return Fit(fit, industry, size, grown, f"Spill's customers: {i_text}; {s_text}; {g_text}. Fit {fit}.")

    def wanted(self, settings: Settings, account: Mapping[str, Any], growth: str | None) -> dict[str, tuple[Any, str]]:
        """fact -> (value, quote): the lookalike facts an account should have now."""
        group, band = settings.industry_group_of(account), prospect_band(account)
        g = self.groups.get((group, band)) if group and band else None
        cell = (int(g["active"]) if g else 0, round(g["strength"], 2) if g else 0.0,
                _quote(group or "its industry", band or "its", g))
        if not group:
            none = "No industry group known for this company, so no lookalike fit."
            return {FIT_FACT: (0, none), INDUSTRY_FIT_FACT: (0.0, none), SIZE_FIT_FACT: (None, none),
                    GROWTH_FIT_FACT: (None, none), ACTIVE_FACT: (cell[0], cell[2]), STRENGTH_FACT: (cell[1], cell[2])}
        f = self.fit(str(account.get("industry") or ""), group, band, growth)
        return {FIT_FACT: (f.fit, f.quote), INDUSTRY_FIT_FACT: (f.industry, f.quote), SIZE_FIT_FACT: (f.size, f.quote),
                GROWTH_FIT_FACT: (f.growth, f.quote), ACTIVE_FACT: (cell[0], cell[2]), STRENGTH_FACT: (cell[1], cell[2])}


def fit_model(store: Any) -> FitModel | None:
    """The model from the stored cells and growth counts; None before the first lookalikes run."""
    cells = store.select(TABLE)
    return FitModel(cells, store.select(GROWTH_TABLE)) if cells else None


def _fact_rows(account_id: str, want: Mapping[str, tuple[Any, str]], now: datetime) -> list[dict]:
    return [{"event_id": new_id(), "account_id": account_id, "source": SOURCE, "fact": f, "value": v, "quote": q,
             "source_url": "", "observed_at": now} for f, (v, q) in want.items()]


def _open_accounts(store: Any) -> tuple[list[dict], dict[tuple[str, str], tuple[datetime, Any]],
                                        dict[tuple[str, str], tuple[datetime, Any]]]:
    """The open accounts, their newest lookalike facts and their newest growth facts."""
    accounts = store.select("accounts", {"status": list(OPEN_STATUSES)})
    ids = [a["account_id"] for a in accounts]
    latest = _latest(e for chunk in _chunks(ids) for e in store.select(
        "signal_events", {"account_id": chunk, "source": SOURCE}))
    return accounts, latest, growth_facts(store, ids)


def apply(ctx: Context) -> dict[str, int]:
    """Write each open account's lookalike facts from the stored cells and growth counts, when they change or
    grow old (REFRESH_DAYS).

    Run by the lookalikes job and nightly by settings_sync before its rescore; it calls nothing outside the
    database. An account whose facts would all be 0 or unknown, with no earlier fact that still scores, gets
    nothing; one whose fit fell to 0 gets its 0s once.
    """
    store, now, today = ctx.store, ctx.now, ctx.today_uk()
    model = fit_model(store)
    if model is None:
        return {"cells": 0, "accounts": 0, "written": 0}
    accounts, latest, growth = _open_accounts(store)
    facts, written = [], 0
    for a in accounts:
        aid = a["account_id"]
        want = model.wanted(ctx.settings, a, account_growth(growth, aid, today))
        have = {f: latest.get((aid, f)) for f in want}
        if not any(v for v, _ in want.values()) and all(h is None or not h[1] for h in have.values()):
            continue  # nothing that scores, and no earlier fact that still does
        same = all(h is not None and h[1] == want[f][0] for f, h in have.items())
        fresh = all(h is not None and _age(h[0], today) < REFRESH_DAYS for h in have.values())
        if same and fresh:
            continue
        written += 1
        facts += _fact_rows(aid, want, now)
    if facts:
        store.insert("signal_events", facts)
    return {"cells": model.cells, "accounts": len(accounts), "written": written}


# -- the job ------------------------------------------------------------------------------------------------


def run(ctx: Context) -> dict:
    """The lookalikes job: HubSpot customers into cells, exclusions and facts, then a rescore."""
    from us_outbound.scoring import score

    customers = read_customers(ctx)
    if not customers:
        raise NoCustomers("HubSpot returned no Spill customer companies; the lookalike cells are kept as they were")
    stored = store_cells(ctx, build_cells(customers))
    excluded = exclude(ctx, customers)
    growth = read_growth(ctx, customers)
    facts = apply(ctx)
    rescored = bool(excluded["accounts_marked"] or facts["written"])
    if rescored:
        score.rescore(ctx)
    by_status = Counter(c.status for c in customers)
    summary = {
        "customers": {
            "active": by_status[ACTIVE], "churned": by_status[CHURNED],
            "us_active": sum(c.us and c.status == ACTIVE for c in customers),
            "us_churned": sum(c.us and c.status == CHURNED for c in customers),
            "without_domain": sum(c.domain is None for c in customers),
        },
        "bands": dict(sorted(Counter(c.band for c in customers).items())),
        "unmapped_industries": dict(sorted(Counter(c.hubspot_industry or "(blank)" for c in customers
                                                   if not c.label).items())),
        **stored,
        "excluded": excluded,
        "growth": growth,
        "lookalike_facts": facts,
        "rescored": rescored,
        "dry_run": ctx.dry_run,
    }
    log("lookalikes", run_id=ctx.run_id, **summary)
    return summary


# -- for sourcing and for Harry --------------------------------------------------------------------------


def lookalike_priorities(settings: Settings, store: Any) -> dict[str, float]:
    """Industries label -> how much Spill's customer base looks like it, 0 to 1 (the best group is 1).

    The weighted strength of the label's industry group across the sizes the system contacts (10 to
    249 staff), divided by the strongest group's. Every label on the Industries tab is listed; all are
    0 before the first lookalikes run. For the sourcing job to order industries; it changes nothing itself.
    """
    strength: dict[str, float] = defaultdict(float)
    for (group, band), g in group_cells(store.select(TABLE)).items():
        if band in TARGET_BANDS:
            strength[group] += g["strength"]
    top = max(strength.values(), default=0.0)
    return {i.industry: round(strength.get(i.industry_group, 0.0) / top, 3) if top else 0.0
            for i in settings.industries}


def lookalike_signals(settings: Settings) -> list[Signal]:
    """The Signals rows that read the lookalike facts, active ones first."""
    return sorted((s for s in settings.signals if SOURCE in s.sources), key=lambda s: not s.active)


def _share(us: float, total: float) -> str:
    return f"{us / total:.0%}" if total else "-"


def _when(v: Any) -> str:
    t = utc_strict_or_none(v)
    return t.astimezone(UK).strftime("%a %d %b %Y %H:%M UK") if t else "-"


def report(settings: Settings, store: Any, *, top: int = 20, all_bands: bool = False) -> list[str]:
    """The lines `us-outbound lookalikes show` prints: the top cells, the groups, and what the signal scores."""
    rows = store.select(TABLE)
    if not rows:
        return ["No lookalike cells yet. `us-outbound run lookalikes` reads Spill's customers from HubSpot "
                "(read only) and fills them."]
    computed = max((r.get("computed_at") for r in rows if r.get("computed_at")), key=utc_strict_or_none, default=None)
    shown = [r for r in rows if all_bands or r.get("size_band") in TARGET_BANDS]
    shown.sort(key=lambda r: (-float(r.get("strength") or 0), -int(r.get("active_customers") or 0),
                              str(r.get("industry_label") or "~"), BANDS.index(r["size_band"])
                              if r.get("size_band") in BANDS else len(BANDS)))
    where = "at every size" if all_bands else "at 10 to 249 staff"
    lines = [
        f"Lookalike cells from Spill's HubSpot customers, computed {_when(computed)}.",
        f"Top {min(top, len(shown))} of {len(shown)} cells {where}, by strength (an active customer 1, a churned "
        f"one {CHURNED_WEIGHT:g}, a US one double):",
        f"  {'Industry label':<32} {'Group':<30} {'Size':<8} {'Active':>6} {'Churned':>7} {'US share':>8} {'Strength':>8}",
    ]
    for r in shown[:top]:
        active, churned = int(r.get("active_customers") or 0), int(r.get("churned_customers") or 0)
        us = int(r.get("us_active") or 0) + int(r.get("us_churned") or 0)
        lines.append(
            f"  {str(r.get('industry_label') or '(no website label)'):<32.32} {str(r.get('industry_group') or '-'):<30.30} "
            f"{r.get('size_band') or '-':<8} {active:>6} {churned:>7} {_share(us, active + churned):>8} "
            f"{float(r.get('strength') or 0):>8.2f}"
        )
    signals = lookalike_signals(settings)
    # A row that reads only the cell counts (the old "Looks like Spill's customers") can be judged per cell.
    by_cell = [s for s in signals if s.active and s.condition and s.condition.fields <= {ACTIVE_FACT, STRENGTH_FACT}]
    groups = group_cells(rows)
    priorities = lookalike_priorities(settings, store)
    group_priority = {settings.industry(lbl).industry_group: p for lbl, p in priorities.items() if settings.industry(lbl)}
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"active": 0, "churned": 0, "us": 0})
    for (group, band), g in groups.items():
        if band in TARGET_BANDS:
            t = totals[group]
            t["active"] += g["active"]
            t["churned"] += g["churned"]
            t["us"] += g["us_active"] + g["us_churned"]
    lines += ["", "By industry group, 10 to 249 staff (what the lookalike fit's industry part and lookalike_priorities "
                  "read):",
              f"  {'Group':<30} {'Active':>6} {'Churned':>7} {'US share':>8} {'Priority':>8}"
              + ("  Signal scores" if by_cell else "")]
    for group, t in sorted(totals.items(), key=lambda kv: (-group_priority.get(kv[0], 0.0), kv[0])):
        line = (f"  {group:<30.30} {int(t['active']):>6} {int(t['churned']):>7} "
                f"{_share(t['us'], t['active'] + t['churned']):>8} {group_priority.get(group, 0.0):>8.2f}")
        if by_cell:
            fires = [band for band in TARGET_BANDS if (group, band) in groups and any(
                s.condition.evaluate({ACTIVE_FACT: groups[(group, band)]["active"],
                                      STRENGTH_FACT: groups[(group, band)]["strength"]}) for s in by_cell)]
            line += f"  {', '.join(fires) if fires else '-'}"
        lines.append(line)
    lines += ["", growth_line(store)]
    for s in signals:
        state = "" if s.active else ", inactive on the Signals tab"
        lines.append(f'Signal "{s.signal}" ({s.weight:+d}{state}): {s.looks_for}.')
    if signals:
        lines.append("`us-outbound lookalikes fit` shows the accounts' fits and the tier mix the build's rows give.")
    return lines


def growth_line(store: Any) -> str:
    """One line: Spill's customers by 12-month headcount growth band, from the last growth search."""
    rows = store.select(GROWTH_TABLE)
    if not rows:
        return "Customers' 12-month headcount growth: not searched yet (the lookalikes job reads it from Apollo)."
    n: Counter[str] = Counter()
    for r in rows:
        n[str(r.get("growth_band"))] += int(r.get("active_customers") or 0) + int(r.get("churned_customers") or 0)
    computed = max((r.get("computed_at") for r in rows if r.get("computed_at")), key=utc_strict_or_none, default=None)
    known = sum(n[b] for b in GROWTH_BANDS)
    parts = ", ".join(f"{b} {n[b]}" for b in GROWTH_BANDS)
    return (f"Customers' 12-month headcount growth (Apollo, {_when(computed)}): {parts}; unknown {n[UNKNOWN]} "
            f"({known} of {known + n[UNKNOWN]} known).")


# -- `us-outbound lookalikes fit`: judging the weights before the sheet changes -----------------------------

FIT_BANDS = tuple((lo, lo + 9 if lo < 90 else 100) for lo in range(0, 100, 10))
TIER_ORDER = ("Priority", "Standard", "Control", "Held", "Excluded")
QUEUE_TIERS = ("Priority", "Standard", "Control")


CLOSE_ROW = "Close match to Spill's customers"
SOME_ROW = "Some match to Spill's customers"


def with_build_lookalike_rows(settings: Settings, close: tuple[int, int] | None = None,
                              some: tuple[int, int] | None | bool = None) -> Settings:
    """These settings with the build's lookalike rows (settings/defaults.py) in place of the sheet's.

    close and some, (lowest fit, weight), try other cut-offs and weights in memory (`lookalikes fit --close 90:10
    --some 60:4`): Some match then runs from its own lowest fit up to Close match's. some=False leaves Some match
    out. Nothing here touches the sheet."""
    from us_outbound.settings.conditions import parse_condition
    from us_outbound.settings.defaults import default_tabs
    from us_outbound.settings.validate import validate_tab

    build, errors = validate_tab("Signals", default_tabs()["Signals"])
    if errors:  # the build's own rows always validate (tests/test_settings_defaults.py)
        raise ValueError(f"the build's Signals rows do not validate: {errors}")
    rows = [s for s in build if SOURCE in s.sources]
    if close is not None or some is not None:
        top = close[0] if close else 70
        out = []
        for s in rows:
            if s.signal == CLOSE_ROW and close:
                looks = f"{FIT_FACT} >= {close[0]}"
                s = dataclasses.replace(s, looks_for=looks, condition=parse_condition(looks), weight=close[1])
            elif s.signal == SOME_ROW:
                if some is False:
                    continue
                low, weight = some if isinstance(some, tuple) else (45, s.weight)
                if low >= top:
                    raise ValueError(f"Some match must start below Close match's {top}, not at {low}")
                looks = f"{FIT_FACT} >= {low} AND {FIT_FACT} < {top}"
                s = dataclasses.replace(s, looks_for=looks, condition=parse_condition(looks), weight=weight)
            out.append(s)
        rows = out
    return dataclasses.replace(settings, signals=(*(s for s in settings.signals if SOURCE not in s.sources), *rows))


def _mix(counts: Counter[str], tier: str) -> str:
    queue = sum(counts[t] for t in QUEUE_TIERS)
    if tier not in QUEUE_TIERS or not queue:
        return f"{counts[tier]}"
    share = counts[tier] / queue
    flag = "" if 0.05 <= share <= 0.40 else " !"
    return f"{counts[tier]} ({share:.0%}){flag}"


def fit_report(ctx: Context, close: tuple[int, int] | None = None,
               some: tuple[int, int] | None | bool = None) -> list[str]:
    """The lines `us-outbound lookalikes fit` prints. Read-only: the database, no outside call, nothing written.

    Each open account's fit is worked out in memory from the stored cells and growth counts (as apply() would
    write it), then scored twice in memory with score_account: with the Signals rows in force, and with the
    build's lookalike rows in place of the sheet's. Harry's check that the size-plus-lookalike cliff is gone
    before the Signals tab is loaded. close and some try other cut-offs and weights for the two graded rows
    (with_build_lookalike_rows), still in memory.
    """
    from us_outbound.scoring.score import score_account

    store, settings, now, today = ctx.store, ctx.settings, ctx.now, ctx.today_uk()
    model = fit_model(store)
    if model is None:
        return ["No lookalike cells yet. `us-outbound run lookalikes` reads Spill's customers from HubSpot "
                "(read only) and fills them."]
    accounts, _, growth = _open_accounts(store)
    events: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks([a["account_id"] for a in accounts]):
        for e in store.select("signal_events", {"account_id": chunk}):
            if e.get("source") != SOURCE:
                events[e["account_id"]].append(e)
    new = with_build_lookalike_rows(settings, close, some)
    rows = [s for s in new.signals if SOURCE in s.sources and s.active]
    names = {s.signal for s in rows}
    fits: Counter[tuple[int, int]] = Counter()
    no_fit = with_growth = 0
    fired: Counter[str] = Counter()
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    for a in accounts:
        aid = a["account_id"]
        band = account_growth(growth, aid, today)
        want = model.wanted(settings, a, band)
        if not settings.industry_group_of(a):
            no_fit += 1
        else:
            fit = int(want[FIT_FACT][0])
            fits[next(b for b in FIT_BANDS if b[0] <= fit <= b[1])] += 1
            with_growth += want[GROWTH_FIT_FACT][0] is not None
        evs = [*events.get(aid, []), *_fact_rows(aid, want, now)]
        before[score_account(a, evs, settings, today).tier] += 1
        r = score_account(a, evs, new, today)
        after[r.tier] += 1
        fired.update(m.signal.signal for m in r.matches if m.signal.signal in names)
    fitted = len(accounts) - no_fit
    cells = [c.get("computed_at") for c in store.select(TABLE) if c.get("computed_at")]
    lines = [f"Lookalike fit of {len(accounts)} open accounts, worked out now from the cells of "
             f"{_when(max(cells, key=utc_strict_or_none, default=None))}.",
             growth_line(store),
             f"  {'Fit':<8} {'Accounts':>8}"]
    lines += [f"  {f'{lo}-{hi}':<8} {fits[(lo, hi)]:>8}" for lo, hi in FIT_BANDS]
    if no_fit:
        lines.append(f"  {'none':<8} {no_fit:>8}  (no industry group)")
    lines.append(f"Growth known for {with_growth} of {fitted}; the rest are fitted on industry and size alone.")
    lines += [f'"{s.signal}" ({s.looks_for}, {s.weight:+d}): {fired[s.signal]} '
              f'{"account" if fired[s.signal] == 1 else "accounts"}.' for s in rows]
    lines += ["", f"  {'Tier':<10} {'Now':>12} {'With the new rows':>18}"]
    lines += [f"  {t:<10} {_mix(before, t):>12} {_mix(after, t):>18}" for t in TIER_ORDER]
    lines += ["Shares are of Priority, Standard and Control together; ! marks one outside 5-40%, the tier-mix check's "
              "range (it reads the month's queue).",
              "Nothing was written. " + (
                  "These rows were tried in memory only: to use them, set their looks_for and weight on the Signals "
                  "tab, then `us-outbound sync`." if close is not None or some is not None else
                  "`us-outbound settings load --tab Signals --live` puts the build's rows on the sheet.")]
    return lines
