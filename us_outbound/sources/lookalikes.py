"""Source "lookalike": Spill's own HubSpot customers, counted into lookalike cells.

Harry, 1 Oct 2026: "I'm happy using Spill companies from HubSpot to help inform lookalike target
lists." This replaces the Seeds tab that docs/pipeline.md deferred.

The lookalikes job (weekly, Monday 02:30 UK, before source_universe; ops/schedule.py):
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
     for SUPPRESS_DAYS, renewed by every run, so the front door (accounts.admit), v_queue and enrol
     refuse it before any Apollo or Clay spend; an account already on that domain gets the HubSpot
     fact hubspot_customer (or hubspot_former_customer), which tiers it Excluded (scoring/tiers.py).
     enrol's HubSpot re-check stays as the last line, but it reads lifecyclestage only, and on
     1 Oct 2026 162 HubSpot companies at lifecycle "lead" had an Active Spill subscription.
     Former customers are kept out too (EXCLUDE_FORMER): a cold first email to a company that has
     already used Spill is the wrong message, and it is Harry's call to change.
  4. Writes the lookalike facts (apply): for each open account, the cells of its industry group in
     its size band, as lookalike_active and lookalike_strength, which the "Looks like Spill's
     customers" signal scores (SPEC 7: facts into signal_events; scoring matches them). Cells are
     read by industry group, not label: HubSpot's industry field has 23 values, most of them a
     whole group ("Tech"), so an Edtech prospect would otherwise match no customer at all.
     settings_sync runs apply() every night too, from the stored cells and with no HubSpot call,
     so an account sourced mid-week has its facts by the next morning.
  5. Rescores, so the exclusions and facts are in force before verify_in_clay runs.

Also here: report() for `us-outbound lookalikes show`, and lookalike_priorities(), for the sourcing
job to order industries by how many customers Spill has like them.

Not here: the "former Spill buyer now at this company" signal (design review D17). It re-purposes
personal data and waits on a legal assessment; this module reads companies only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from us_outbound.clean.domains import is_personal_domain, root_domain
from us_outbound.clean.people import size_band
from us_outbound.clients.db import new_id
from us_outbound.context import UK, Context
from us_outbound.logs import log
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.defaults import US_STATES
from us_outbound.settings.model import Settings, Signal

SOURCE = "lookalike"
TABLE = "lookalike_cells"
ACTIVE_FACT, STRENGTH_FACT = "lookalike_active", "lookalike_strength"
HUBSPOT_SOURCE = "hubspot"  # the source enrol's HubSpot re-check writes its facts under
CUSTOMER_FACT, FORMER_FACT = "hubspot_customer", "hubspot_former_customer"  # scoring/tiers.HUBSPOT_EXCLUSIONS
SUPPRESS_SOURCE = "lookalikes"
SUPPRESS_DAYS = 35  # five weeks: a weekly job may miss a run or two before a customer's entry lapses
REFRESH_DAYS = 90  # an unchanged lookalike fact is written again after this, inside the signal's 120 days
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
PROSPECT_BANDS = {"10-19": "10-49", "20-49": "10-49", "50-99": "50-99", "100-249": "100-249"}
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


def store_cells(ctx: Context, cells: Mapping[str, dict]) -> dict[str, int]:
    """Replace lookalike_cells with these cells: the ones gone are deleted, the rest upserted."""
    old = {r["cell_id"] for r in ctx.store.select(TABLE)}
    gone = sorted(old - set(cells))
    if gone:
        ctx.store.delete(TABLE, {"cell_id": gone})
    rows = [{**cells[k], "computed_at": ctx.now, "run_id": ctx.run_id} for k in sorted(cells)]
    if rows:
        ctx.store.upsert(TABLE, rows)
    return {"cells": len(rows), "removed": len(gone)}


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


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _latest(events: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], tuple[datetime, Any]]:
    """(account_id, fact) -> (observed_at, value) of the newest event."""
    out: dict[tuple[str, str], tuple[datetime, Any]] = {}
    floor = datetime(1970, 1, 1, tzinfo=UTC)
    for e in events:
        key = (str(e.get("account_id")), str(e.get("fact")))
        t = _ts(e.get("observed_at")) or floor
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
    rows = []
    for domain in domains:
        old = existing.get(domain)
        if old is not None:
            old_expiry = _ts(old.get("expires_at"))
            if old_expiry is None or old_expiry >= expires:
                continue  # indefinite, or already suppressed for longer
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
    return {"domains": len(domains), "suppressed": len(rows), "accounts_marked": len(facts)}


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


def apply(ctx: Context) -> dict[str, int]:
    """Write each open account's lookalike facts from the stored cells, when they change or grow old.

    Run by the lookalikes job and nightly by settings_sync before its rescore; it calls nothing outside
    the database. An account with no cell and no earlier fact gets nothing; one whose cell went gets 0s.
    """
    store, now, today = ctx.store, ctx.now, ctx.today_uk()
    rows = store.select(TABLE)
    if not rows:
        return {"cells": 0, "accounts": 0, "written": 0}
    groups = group_cells(rows)
    accounts = store.select("accounts", {"status": list(OPEN_STATUSES)})
    ids = [a["account_id"] for a in accounts]
    latest = _latest(e for chunk in _chunks(ids) for e in store.select(
        "signal_events", {"account_id": chunk, "source": SOURCE}))
    facts = []
    for a in accounts:
        aid = a["account_id"]
        group, band = ctx.settings.industry_group_of(a), prospect_band(a)
        g = groups.get((group, band)) if group and band else None
        want = {ACTIVE_FACT: int(g["active"]) if g else 0, STRENGTH_FACT: round(g["strength"], 2) if g else 0.0}
        have = {f: latest.get((aid, f)) for f in want}
        if not g and all(h is None or not h[1] for h in have.values()):
            continue  # no cell, and no earlier fact that still scores
        same = all(h is not None and h[1] == want[f] for f, h in have.items())
        fresh = all(h is not None and (today - h[0].astimezone(UK).date()).days < REFRESH_DAYS for h in have.values())
        if same and fresh:
            continue
        quote = _quote(group or "its industry", band or "its", g)
        facts += [{"event_id": new_id(), "account_id": aid, "source": SOURCE, "fact": f, "value": v,
                   "quote": quote, "source_url": "", "observed_at": now} for f, v in want.items()]
    if facts:
        store.insert("signal_events", facts)
    return {"cells": len(rows), "accounts": len(accounts), "written": len(facts) // 2}


# -- the job ------------------------------------------------------------------------------------------------


def run(ctx: Context) -> dict:
    """The lookalikes job: HubSpot customers into cells, exclusions and facts, then a rescore."""
    from us_outbound.scoring import score

    customers = read_customers(ctx)
    if not customers:
        raise NoCustomers("HubSpot returned no Spill customer companies; the lookalike cells are kept as they were")
    stored = store_cells(ctx, build_cells(customers))
    excluded = exclude(ctx, customers)
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


def lookalike_signal(settings: Settings) -> Signal | None:
    return next((s for s in settings.signals if SOURCE in s.sources), None)


def _share(us: float, total: float) -> str:
    return f"{us / total:.0%}" if total else "-"


def _when(v: Any) -> str:
    t = _ts(v)
    return t.astimezone(UK).strftime("%a %d %b %Y %H:%M UK") if t else "-"


def report(settings: Settings, store: Any, *, top: int = 20, all_bands: bool = False) -> list[str]:
    """The lines `us-outbound lookalikes show` prints: the top cells, the groups, and what the signal scores."""
    rows = store.select(TABLE)
    if not rows:
        return ["No lookalike cells yet. `us-outbound run lookalikes` reads Spill's customers from HubSpot "
                "(read only) and fills them."]
    computed = max((r.get("computed_at") for r in rows if r.get("computed_at")), key=lambda v: _ts(v), default=None)
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
    signal = lookalike_signal(settings)
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
    lines += ["", "By industry group, 10 to 249 staff (what the signal and lookalike_priorities read):",
              f"  {'Group':<30} {'Active':>6} {'Churned':>7} {'US share':>8} {'Priority':>8}  Signal scores"]
    for group, t in sorted(totals.items(), key=lambda kv: (-group_priority.get(kv[0], 0.0), kv[0])):
        fires = [band for band in TARGET_BANDS if signal and signal.condition and (group, band) in groups
                 and signal.condition.evaluate({ACTIVE_FACT: groups[(group, band)]["active"],
                                                STRENGTH_FACT: groups[(group, band)]["strength"]})]
        lines.append(f"  {group:<30.30} {int(t['active']):>6} {int(t['churned']):>7} "
                     f"{_share(t['us'], t['active'] + t['churned']):>8} {group_priority.get(group, 0.0):>8.2f}  "
                     f"{', '.join(fires) if fires else '-'}")
    if signal:
        state = "" if signal.active else " (inactive on the Signals tab)"
        lines += ["", f'Signal "{signal.signal}" ({signal.weight:+d}{state}) scores an account whose group and size '
                      f"band meet: {signal.looks_for}."]
    return lines
