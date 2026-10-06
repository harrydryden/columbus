"""Source "apollo_org", the universe: Apollo organization search into accounts and facts (SPEC 7; SPEC 9 source_universe).

Harry, 30 Sep 2026 (docs/pipeline.md, "The funnel"): fit finds accounts and signals rank them, so the
universe is every company that fits the sheet's Industries, States and size bands.

  1. Filters, built from the sheet each run (no saved search or list in Apollo, SPEC 1.2). One search
     per industry group and HQ state:
       * the NAICS codes of the group's active Industries labels, as prefixes; a code an earlier
         group already searched is left out, so no company is paid for twice;
       * a label with no NAICS codes is searched by its apollo_keywords instead;
       * the active States, never CA or WA (SPEC 1.3); the General size range, min_employees to
         max_employees (10 to 249 by default; Harry, 6 Oct 2026), one search per size band it touches
         (Settings.size_bands), each band clipped to the range; insurers and brokers (NAICS 524) left
         out by Apollo, other partners by Python.
     A search Apollo says holds over 50,000 companies (its display limit) is split by size band (SPEC 7),
     and so is one whose first page carries no employee counts, so the band comes from the filter.
  2. Each company is mapped to exactly one Industries label (best_label: its NAICS codes, then its
     Apollo keywords) and skipped if none fits or the best fit is switched off. Its root domain and
     name are cleaned (clean/), and it comes in by the front door (accounts.admit: one account per
     root domain, through domain_aliases; suppressed and partner domains refused). Personal domains,
     companies with no website, HQs outside the active states and sizes outside the General range are
     skipped. A new account has source apollo and status new; an account still in the queue that is
     found again gets Apollo's latest columns. Overrides win; fields Clay has confirmed are kept
     (docs/pipeline.md, "Which value wins"). A row with no employee count only fills a blank size: the
     exact count apollo_enrich (or an earlier row) gave, and its size band, stay.
  3. Facts: every apollo_org field scoring reads (settings/model.py SOURCE_FIELDS) goes to
     signal_events with a quote, the Apollo page and observed_at: employees, naics, hq_state,
     headcount_growth_12m, days_since_funding, funding_stage, funding_amount_usd, founded_year,
     technologies, keywords, apollo_industry and description (Apollo's short description, which
     the opener's optional "what they do" phrase reads; enrol/openers.py). open_roles is not one of
     them: it belongs to apollo_jobs (docs/pipeline.md, change 4). Search rows carry no funding and
     no employee count (2 Oct 2026), so apollo_enrich (sources/apollo_enrich.py) writes those from
     Apollo's organization enrich, through org_facts, for the General apollo_enrich_groups.

When to stop. The queue (accounts new, queued or verified, not Excluded or Held) should hold two
weeks of the weekly target, 2 × weekly_enrol_cap (SPEC 2: "a two-week queue"). The Focus tab's groups
come first, each up to its share of that; the other active groups share the rest, a page at a time,
state by state. The run also stops when today's credits are used.

Credits. A search page costs 1 credit when it returns a company and 0 when it is empty (Apollo's
API docs; docs/phase0-facts.md), so about 0.01 to 0.02 credits per account sourced. Every page goes
into credit_ledger with its search and page in the note, so the next run carries on from the next
page; each month starts again from page 1, which refreshes the universe monthly (SPEC 7). Sourcing
may spend SOURCING_SHARE of apollo_monthly_credits, paced by the weekday, and never more than is left
of the whole Apollo budget today (sources/apollo_credits.py), so pick_contacts keeps the rest for
email reveals. Nothing is spent while Apollo's balance is below apollo_floor.

Schedule: weekdays at 03:00 UK, not SPEC 9's 1st of the month: topping the queue up to two weeks
needs a run each morning, and the month's credits are paced by the weekday (budget.py).

Dry-run: the searches are reads, so they happen, and their credits are recorded. Accounts, facts
and the ledger are database writes, which dry-run makes too (SPEC 0.3), as settings_sync does for
named accounts and enrol for HubSpot exclusions. The job writes nothing outside the database in
any mode, so --live changes nothing for it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from us_outbound import accounts, budget
from us_outbound.clean.domains import is_personal_domain, record_alias, root_domain
from us_outbound.clean.names import clean_company_name
from us_outbound.clean.people import USPS_STATES, size_band, state_code
from us_outbound.clients.apollo import MAX_PAGE, MAX_PER_PAGE, organizations_in, total_entries
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, Context
from us_outbound.enrol import focus
from us_outbound.logs import log
from us_outbound.scoring import tiers
from us_outbound.scoring.score import parse_override
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.model import Industry, Settings
from us_outbound.sources import apollo_credits as credits

JOB = "source_universe"
SOURCE = "apollo_org"
ACCOUNT_SOURCE = "apollo"
SOURCING_SHARE = 0.25  # of apollo_monthly_credits; apollo_signals 0.25, apollo_enrich 0.15, the 0.35 left for email reveals
QUEUE_WEEKS = 2  # SPEC 2: a two-week queue
OPEN_STATUSES = ("new", "queued", "verified")  # waiting to be enrolled
OUT_OF_QUEUE_TIERS = frozenset({tiers.EXCLUDED, tiers.HELD})
NEVER_STATES = frozenset({"CA", "WA"})  # SPEC 1.3
MAX_RESULTS = MAX_PAGE * MAX_PER_PAGE  # Apollo shows at most 50,000 companies per search
# PHASE0-CONFIRM: organization_naics_codes takes 2 to 5 digits (Apollo's docs), so a 6-digit code is
# sent as its 5-digit prefix and Python checks the full code. Set to 6 if Apollo takes six digits.
NAICS_DIGITS = 5
NAICS = "naics"  # a search by NAICS codes; otherwise it is a label's keyword search
MAX_PAGES_PER_RUN = 200  # well inside the 60-minute timeout
# Apollo's search rows carry no estimated_num_employees (2 Oct 2026, the first live run: none on 432
# accounts), so a search without a size filter gives accounts no size band at all. Every search is
# therefore read by size band from the start, and the band comes from the search's own filter.
# None: the bands the General size range touches (Settings.size_bands; Harry, 6 Oct 2026), each searched with
# its range clipped to min_employees and max_employees.
START_BANDS: tuple[str, ...] | None = None
BACKFILL_BATCH = 100  # accounts per size-band backfill search (one page; Apollo's per_page limit)
QUOTE_LIMIT = 300  # SPEC 6
DESCRIPTION_LIMIT = 600  # the description fact, for the opener's "what they do" phrase
APOLLO_ORG_URL = "https://app.apollo.io/#/organizations/{}"
USD = frozenset({"$", "USD", "US$"})  # funding_events[].currency read as dollars
AMOUNT = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([KMB])?", re.I)  # "8M", "$1.5B", "750K", "2,500,000"
AMOUNT_UNITS = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
US_COUNTRIES = frozenset({"united states", "united states of america", "us", "usa"})
# Account columns an Overrides row may set (SPEC 5: an override wins over every source).
OVERRIDABLE = ("clean_name", "legal_name", "hq_city", "hq_state", "industry", "industry_group", "naics",
               "employees", "size_band", "founded_year")
# Columns Clay confirms (docs/pipeline.md, "Which value wins"): Apollo no longer changes them once it has.
CLAY_OWNED = frozenset({"clean_name", "legal_name", "hq_city", "hq_state", "employees", "size_band",
                        "founded_year", "industry", "industry_group"})


def _prefixes(codes: Iterable[str]) -> list[str]:
    """NAICS codes as Apollo takes them (at most NAICS_DIGITS), dropping any another code already covers."""
    cut = sorted({c[:NAICS_DIGITS] for c in codes if c})
    return [c for c in cut if not any(p != c and c.startswith(p) for p in cut)]


PARTNER_FILTER = _prefixes(c for c in tiers.PARTNER_NAICS if len(c) <= NAICS_DIGITS)  # ["524"]


# -- the sheet: states, groups, searches ---------------------------------------------------------


def allowed_states(settings: Settings) -> tuple[str, ...]:
    """Active states on the States tab, never CA or WA."""
    return tuple(s for s in settings.active_states() if s not in NEVER_STATES)


def location(state: str) -> str:
    """Apollo's organization_locations value for an HQ state. PHASE0-CONFIRM: "<state name>, US"
    (the country keeps Georgia the state, not the country)."""
    return f"{USPS_STATES[state]}, US"


def searchable(ind: Industry) -> bool:
    return ind.active and bool(ind.naics_prefixes or ind.apollo_keywords)


def active_groups(settings: Settings) -> list[str]:
    """Groups with an active label to search: the Focus tab's first (largest share first), then by priority."""
    first: dict[str, tuple[int, int]] = {}
    for i, ind in enumerate(settings.industries):
        if searchable(ind):
            key = (ind.priority, i)
            first[ind.industry_group] = min(first.get(ind.industry_group, key), key)
    return sorted(first, key=lambda g: (focus.group_rank(g, settings), first[g]))


@dataclass(frozen=True)
class Slice:
    """One Apollo search: a group's NAICS codes (or one label's keywords) in one HQ state, maybe one size band."""

    group: str
    state: str
    what: str  # NAICS, or the label searched by keyword
    terms: tuple[str, ...]  # NAICS prefixes or keywords
    label: str  # the label for a company that comes back with no NAICS codes or keywords ("" for none)
    band: str = ""
    employees: str = ""  # Apollo's range for the band, clipped to the General size range ("250,300")

    @property
    def key(self) -> str:
        digest = hashlib.sha256("|".join(self.terms).encode()).hexdigest()[:8]  # a changed filter starts afresh
        band = self.band or "all"
        if self.band and self.employees and self.employees != self.band.replace("-", ","):
            band += f"={self.employees}"  # a band the General range clips: a new clip starts afresh
        return f"{self.group}|{self.what}|{self.state}|{band}|{digest}"

    def filters(self, settings: Settings) -> dict[str, Any]:
        if self.band:
            ranges = [self.employees or settings.employee_range(self.band)]
        else:
            ranges = [settings.employee_range(b) for b in settings.size_bands()]
        f: dict[str, Any] = {
            "organization_locations": [location(self.state)],
            "organization_num_employees_ranges": ranges,
        }
        if self.what == NAICS:
            f["organization_naics_codes"] = list(self.terms)
        else:
            f["q_organization_keyword_tags"] = list(self.terms)
        if PARTNER_FILTER:
            f["not_organization_naics_codes"] = list(PARTNER_FILTER)
        return f

    def by_band(self, settings: Settings) -> list[Slice]:
        return [replace(self, band=b, employees=settings.employee_range(b)) for b in settings.size_bands()]


def plan(settings: Settings, groups: Sequence[str], states: Sequence[str]) -> dict[str, list[Slice]]:
    """group -> its searches, one per state, in the groups' order."""
    covered: list[str] = []
    out: dict[str, list[Slice]] = {}
    for group in groups:
        labels = [i for i in settings.industries if searchable(i) and i.industry_group == group]
        codes = [c for c in _prefixes(c for i in labels for c in i.naics_prefixes)
                 if not any(c.startswith(p) for p in covered)]
        covered += codes
        umbrella = next((i.industry for i in labels if i.industry == group), "")
        searches = [(NAICS, tuple(codes), umbrella)] if codes else []
        searches += [(i.industry, i.apollo_keywords, i.industry) for i in labels if not i.naics_prefixes]
        bands = START_BANDS if START_BANDS is not None else settings.size_bands()
        out[group] = [Slice(group, st, what, terms, label, band, settings.employee_range(band) if band else "")
                      for what, terms, label in searches for st in states for band in bands]
    return out


def pools(settings: Settings, groups: Sequence[str], target: int) -> list[tuple[tuple[str, ...], int]]:
    """(groups, queue target) to fill in turn: each Focus group its share; the rest of the groups the rest."""
    shares = {f.industry_group: f.share for f in settings.focus if f.industry_group in groups}
    if not shares:
        return [(tuple(groups), target)]
    out = [((g,), round(shares[g] * target)) for g in groups if g in shares]
    rest = tuple(g for g in groups if g not in shares)
    left = 1.0 - sum(shares.values())
    if rest and left > 1e-9:
        out.append((rest, round(left * target)))
    return out


def queue_depth(ctx: Context) -> Counter[str]:
    """Accounts waiting to be enrolled (new, queued or verified, not Excluded or Held), per industry group."""
    out: Counter[str] = Counter()
    for a in ctx.store.select("accounts", {"status": list(OPEN_STATUSES)}):
        if a.get("tier") not in OUT_OF_QUEUE_TIERS:
            out[ctx.settings.industry_group_of(a)] += 1
    return out


# -- where each search got to this month (credit_ledger notes) ------------------------------------


@dataclass
class Cursor:
    page: int = 0  # the last page read this month
    pages: int | None = None  # the pages Apollo said the search has
    split: bool = False  # over 50,000 companies: searched by size band instead

    @property
    def done(self) -> bool:
        return self.split or (self.pages is not None and self.page >= min(self.pages, MAX_PAGE))


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def cursors(ctx: Context) -> dict[str, Cursor]:
    """Each search's place this month (UK), from this job's credit_ledger notes."""
    start, end = budget.month_bounds(ctx.now)
    out: dict[str, Cursor] = {}
    for r in ctx.store.select("credit_ledger", {"system": credits.SYSTEM, "job": JOB}):
        t = _ts(r.get("occurred_at"))
        if t is None or not start <= t < end:
            continue
        try:
            note = json.loads(r.get("note") or "")
        except ValueError:
            continue
        if not isinstance(note, dict) or not note.get("slice"):
            continue
        c = out.setdefault(str(note["slice"]), Cursor())
        page = int(note.get("page") or 0)
        if page >= c.page:
            c.page, c.pages = page, note.get("pages")
        c.split = c.split or bool(note.get("split"))
    return out


def _lane(slices: Iterable[Slice], progress: Mapping[str, Cursor], settings: Settings) -> deque[Slice]:
    """The searches still to read, in turn; a split search is read as its four size bands."""
    out: deque[Slice] = deque()
    for sl in slices:
        c = progress.get(sl.key)
        if c is not None and c.split:
            out.extend(b for b in sl.by_band(settings) if not progress.get(b.key, Cursor()).done)
        elif c is None or not c.done:
            out.append(sl)
    return out


def _pages(body: Mapping[str, Any], page: int, found: int) -> int | None:
    """How many pages the search has: Apollo's pagination, else what this page shows."""
    if found == 0:
        return page - 1
    pages = (body.get("pagination") or {}).get("total_pages")
    if pages is not None:
        try:
            return int(pages)
        except (TypeError, ValueError):
            pass
    total = total_entries(body)
    if total is not None:
        return math.ceil(total / MAX_PER_PAGE)
    return page if found < MAX_PER_PAGE else None


# -- one company ----------------------------------------------------------------------------------


def _int(v: Any) -> int | None:
    try:
        return None if v in (None, "") or isinstance(v, bool) else int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return None if v in (None, "") or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _texts(v: Any) -> list[str]:
    return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, (list, tuple)) else []


def org_domain(org: Mapping[str, Any]) -> str | None:
    """The company's root domain: Apollo's primary domain, else its website."""
    for v in (org.get("primary_domain"), org.get("domain"), org.get("website_url")):
        root = root_domain(str(v)) if v else None
        if root:
            return root
    return None


def org_naics(org: Mapping[str, Any]) -> list[str]:
    """PHASE0-CONFIRM: search rows carry naics_codes (as enrichment does)."""
    return tiers.naics_codes(org.get("naics_codes")) or tiers.naics_codes(org.get("naics_code"))


def org_keywords(org: Mapping[str, Any]) -> list[str]:
    return _texts(org.get("keywords"))


def org_technologies(org: Mapping[str, Any]) -> list[str]:
    names = _texts(org.get("technology_names"))
    if not names:
        names = _texts([t.get("name") for t in org.get("current_technologies") or () if isinstance(t, Mapping)])
    return list(dict.fromkeys(names))


def org_description(org: Mapping[str, Any]) -> str:
    """Apollo's short description of the company, whitespace folded, at most DESCRIPTION_LIMIT characters.

    The opener's optional "what they do" phrase is taken from it and the keywords (enrol/openers.py).
    PHASE0-CONFIRM: organization search rows carry short_description, as enrichment does.
    """
    text = " ".join(str(org.get("short_description") or org.get("description") or "").split())
    return text if len(text) <= DESCRIPTION_LIMIT else text[: DESCRIPTION_LIMIT - 1] + "…"


def in_us(org: Mapping[str, Any]) -> bool:
    country = str(org.get("country") or "").strip().casefold()
    return not country or country in US_COUNTRIES


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def funding_usd(event: Mapping[str, Any]) -> int | None:
    """A funding event's amount in dollars: a number, or text like "8M", "$1.5B" or "750K"; None otherwise.

    PHASE0-CONFIRM: organization enrich gives funding_events[].amount as text ("8M") with a
    currency ("$"); an amount in another currency is not taken as dollars.
    """
    if str(event.get("currency") or "$").strip().upper() not in USD:
        return None
    v = event.get("amount")
    n = _int(v)
    if n is None and isinstance(v, str):
        m = AMOUNT.fullmatch(v.strip())
        if m:
            n = int(float(m.group(1).replace(",", "")) * AMOUNT_UNITS[m.group(2).upper() if m.group(2) else ""])
    return n if n and n > 0 else None


def org_funding(org: Mapping[str, Any], today: date) -> dict[str, Any]:
    """days_since_funding (as of today), funding_stage and funding_amount_usd of the latest round.

    The amount is the latest round's own (an event on its date), never an earlier round's.
    PHASE0-CONFIRM: latest_funding_round_date, latest_funding_stage and funding_events[].amount.
    """
    events = sorted((e for e in org.get("funding_events") or () if isinstance(e, Mapping) and _date(e.get("date"))),
                    key=lambda e: _date(e.get("date")), reverse=True)
    when = _date(org.get("latest_funding_round_date")) or (_date(events[0].get("date")) if events else None)
    stage = str(org.get("latest_funding_stage") or (events[0].get("type") if events else "") or "").strip()
    amount = next((a for e in events if _date(e.get("date")) == when and (a := funding_usd(e))), None)
    out: dict[str, Any] = {}
    if when is not None and when <= today:
        out["days_since_funding"] = (today - when).days
        out["funding_date"] = when.isoformat()
    if stage:
        out["funding_stage"] = stage
    if amount:
        out["funding_amount_usd"] = amount
    return out


def _keyword_text(org: Mapping[str, Any]) -> str:
    return " ; ".join([*org_keywords(org), str(org.get("industry") or "")]).strip(" ;")


def _naics_match(codes: Sequence[str], prefix: str) -> int:
    """How many digits of the label's prefix the company's codes match (0 for none).

    A company code shorter than the prefix ("54181" for 541810) matches on its own digits, from 5.
    """
    best = 0
    for c in codes:
        if c.startswith(prefix):
            best = max(best, len(prefix))
        elif len(c) >= 5 and prefix.startswith(c):
            best = max(best, len(c))
    return best


def best_label(codes: Sequence[str], keyword_text: str, settings: Settings) -> Industry | None:
    """The one Industries label that fits best, active or not; None if none fits.

    Candidates are the labels whose NAICS prefixes match the company's codes (none of its
    exclude_naics), or, when none does, the labels with a keyword match. Among them: the most
    keyword matches, then the longest NAICS match, then a label over its group's umbrella label,
    then the Industries priority, then the tab's order.
    """
    rows = []
    for idx, ind in enumerate(settings.industries):
        if any(_naics_match(codes, x) for x in ind.exclude_naics):
            continue
        naics = max((_naics_match(codes, p) for p in ind.naics_prefixes), default=0)
        words = len(find_terms(keyword_text, ind.apollo_keywords)) if keyword_text else 0
        rows.append((ind, naics, words, idx))
    pool = [r for r in rows if r[1]] or [r for r in rows if r[2]]
    if not pool:
        return None
    return min(pool, key=lambda r: (-r[2], -r[1], r[0].industry == r[0].industry_group, r[0].priority, r[3]))[0]


def _quote(text: str) -> str:
    q = " ".join(text.split())
    return q if len(q) <= QUOTE_LIMIT else q[: QUOTE_LIMIT - 1] + "…"


def org_facts(account_id: str, org: Mapping[str, Any], state: str, now: datetime) -> list[dict]:
    """The apollo_org facts of one organization record, each with a quote and the Apollo page."""
    org_id = str(org.get("organization_id") or org.get("id") or "")
    url = APOLLO_ORG_URL.format(org_id) if org_id else ""
    today = now.astimezone(UK).date()
    out: list[dict] = []

    def add(fact: str, value: Any, quote: str) -> None:
        if value in (None, "", [], ()):
            return
        out.append({"event_id": new_id(), "account_id": account_id, "source": SOURCE, "fact": fact, "value": value,
                    "quote": _quote(quote), "source_url": url, "observed_at": now})

    employees = _int(org.get("estimated_num_employees"))
    add("employees", employees, f"Apollo: about {employees} employees")
    codes = org_naics(org)
    add("naics", codes, "Apollo NAICS: " + ", ".join(codes))
    city = str(org.get("city") or "").strip()
    add("hq_state", state, f"Apollo: HQ in {city + ', ' if city else ''}{state}")
    growth = _float(org.get("organization_headcount_twelve_month_growth"))  # PHASE0-CONFIRM: a fraction (0.12)
    add("headcount_growth_12m", growth, f"Apollo: headcount {growth:+.0%} over 12 months" if growth is not None else "")
    funding = org_funding(org, today)
    when = funding.get("funding_date", "")
    add("days_since_funding", funding.get("days_since_funding"), f"Apollo: latest funding round on {when}")
    add("funding_stage", funding.get("funding_stage"), f"Apollo: latest round {funding.get('funding_stage')} ({when})")
    amount = funding.get("funding_amount_usd")
    add("funding_amount_usd", amount, f"Apollo: latest round ${amount:,}" if amount else "")
    founded = _int(org.get("founded_year"))
    add("founded_year", founded, f"Apollo: founded {founded}")
    techs = org_technologies(org)
    add("technologies", techs, "Apollo technologies: " + ", ".join(techs))
    words = org_keywords(org)
    add("keywords", words, "Apollo keywords: " + ", ".join(words))
    industry = str(org.get("industry") or "").strip()
    add("apollo_industry", industry, f"Apollo industry: {industry}")
    about = org_description(org)
    add("description", about, f"Apollo description: {about}")
    return out


def with_overrides(row: Mapping[str, Any], domain: str, settings: Settings) -> dict:
    """The account columns with the domain's Overrides rows applied (SPEC 5)."""
    ov = settings.overrides_for(domain)
    out = dict(row)
    for f in OVERRIDABLE:
        if f in ov:
            out[f] = parse_override(ov[f])
    if "naics" in ov:
        out["naics"] = str(out["naics"])
    if "hq_state" in ov:
        out["hq_state"] = state_code(str(out["hq_state"])) or str(out["hq_state"]).strip().upper()
    if "industry" in ov and "industry_group" not in ov:
        ind = settings.industry(str(out["industry"]))
        out["industry_group"] = ind.industry_group if ind else out.get("industry_group")
    if "employees" in ov and "size_band" not in ov:
        out["size_band"] = size_band(out["employees"])
    return out


SIZE_COLUMNS = ("employees", "size_band")


def _keep_size(cols: Mapping[str, Any], account: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict:
    """A search row with no employee count, for an account already in the queue: its size columns only fill
    blanks. An exact count from apollo_enrich or an earlier row, and the band that came with it, are never
    overwritten by None or by the band of the search that found it again. An Overrides row still wins."""
    out = dict(cols)
    overridden = set(overrides) | ({"size_band"} if "employees" in overrides else set())  # its band follows (with_overrides)
    for f in SIZE_COLUMNS:
        if f in overridden:
            continue
        if account.get(f) not in (None, "") or out.get(f) is None:
            out.pop(f, None)
    return out


def columns(org: Mapping[str, Any], label: Industry | None, state: str, band: str) -> dict:
    """The accounts columns Apollo gives (SPEC 6). No label leaves the industry blank: a site visitor held for
    the weekly hand-check (sources/site_visits.py) until an Overrides row gives one."""
    employees = _int(org.get("estimated_num_employees"))
    codes = org_naics(org)
    return {
        "apollo_org_id": str(org.get("organization_id") or org.get("id") or "") or None,
        "hq_city": str(org.get("city") or "").strip() or None,
        "hq_state": state,
        "hq_country": "United States" if in_us(org) and str(org.get("country") or "").strip() else None,
        "industry": label.industry if label else None,
        "industry_group": label.industry_group if label else None,
        "naics": ", ".join(codes) or None,
        "employees": employees,
        "size_band": size_band(employees) if employees is not None else (band or None),
        "founded_year": _int(org.get("founded_year")),
    }


# -- the run --------------------------------------------------------------------------------------


@dataclass
class _Run:
    pages: int = 0
    credits: float = 0.0
    created: Counter[str] = field(default_factory=Counter)  # per industry group
    updated: int = 0
    facts: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    seen: dict[str, str] = field(default_factory=dict)  # domain -> the size band of the search that found it
    errors: list[str] = field(default_factory=list)
    states: frozenset[str] = frozenset()  # allowed_states, once per run


def take(ctx: Context, org: Mapping[str, Any], sl: Slice, run: _Run, depth: Counter[str],
         rows: list[dict], events: list[dict], partners: dict[str, dict], *,
         source: str = ACCOUNT_SOURCE) -> accounts.Admitted | None:
    """One organization from a search page: into accounts and facts, or skipped with the reason.

    Returns the front door's answer, or None for a company skipped before it, so the lookalike leads
    (sources/lookalike_leads.py), which read Apollo's search under their own account source, can tell
    the accounts they created from the ones found again.
    """
    s, store, now = ctx.settings, ctx.store, ctx.now
    domain = org_domain(org)
    if not domain:
        run.skipped["no website"] += 1
        return
    if is_personal_domain(domain):
        run.skipped["a personal email domain"] += 1
        return
    again = domain in run.seen
    if again and (run.seen[domain] or not sl.band):
        return  # found by two searches this run; a size-band search still gives a size band
    run.seen[domain] = sl.band
    state = state_code(str(org.get("state") or "")) or sl.state
    if not in_us(org) or state not in run.states:
        run.skipped["HQ outside the active states"] += 1
        return
    employees = _int(org.get("estimated_num_employees"))
    if employees is not None and not s.size_in_range(employees):
        run.skipped[f"outside {s.size_range_text()} employees"] += 1
        return
    codes, text = org_naics(org), _keyword_text(org)
    label = best_label(codes, text, s) if codes or text else s.industry(sl.label) if sl.label else None
    if label is None:
        run.skipped["no Industries label fits"] += 1
        return
    if not label.active:
        run.skipped["its best Industries label is switched off"] += 1
        return
    partner = tiers.partner_match({"naics": codes, "industry": label.industry, "keywords": org_keywords(org),
                                   "apollo_industry": org.get("industry")}, {})
    if partner:
        run.skipped["a partner, never prospected"] += 1
        if not store.get("partners", domain=domain):
            partners[domain] = {"domain": domain, "name": str(org.get("name") or domain), "reason": partner[0],
                                "naics": ", ".join(codes) or None, "added_at": now}
        return
    site = root_domain(str(org.get("website_url") or "")) if org.get("website_url") else None
    if site and site != domain and not is_personal_domain(site) and not store.get("accounts", domain=site):
        record_alias(store, site, domain, ACCOUNT_SOURCE, now)  # one account per root domain (SPEC 13)
    got = accounts.admit(store, domain, source=source, now=now, name=str(org.get("name") or ""))
    if not got.ok:
        run.skipped[got.outcome] += 1
        return got
    account = store.get("accounts", account_id=got.account_id) or {}
    cols = with_overrides(columns(org, label, state, sl.band), got.domain or domain, s)
    if got.outcome == "created":
        rows.append({"account_id": got.account_id, **cols})
        run.created[cols.get("industry_group") or label.industry_group] += 1
        depth[cols.get("industry_group") or label.industry_group] += 1
    elif account.get("status") in OPEN_STATUSES:
        if account.get("clay_checked_at"):
            cols = {k: v for k, v in cols.items() if k not in CLAY_OWNED}
        if employees is None:
            cols = _keep_size(cols, account, s.overrides_for(got.domain or domain))
        clean, legal = clean_company_name(str(org.get("name") or ""))
        if not account.get("clean_name") and clean:
            cols["clean_name"] = clean
        if not account.get("legal_name") and legal:
            cols["legal_name"] = legal
        rows.append({"account_id": got.account_id, **cols})
        run.updated += 1
    elif account.get("status") == "disqualified":
        run.skipped["disqualified earlier"] += 1
        return got
    if not again:
        events.extend(org_facts(got.account_id, org, state, now))  # Apollo's view; Overrides apply at scoring
    return got


def _write(ctx: Context, rows: list[dict], events: list[dict], partners: dict[str, dict], run: _Run) -> None:
    if rows:
        ctx.store.upsert("accounts", rows)  # partial rows: upsert leaves the other columns alone
    if events:
        ctx.store.insert("signal_events", events)
        run.facts += len(events)
    if partners:
        ctx.store.upsert("partners", list(partners.values()))


def read_page(ctx: Context, sl: Slice, cur: Cursor, run: _Run, room: credits.Room, depth: Counter[str]) -> bool:
    """Read the search's next page into accounts and facts; False if Apollo refused it."""
    page = cur.page + 1
    try:
        body = ctx.clients.apollo.search_organizations(sl.filters(ctx.settings), page=page, per_page=MAX_PER_PAGE)
    except ApiError as exc:
        if exc.status in (401, 403):
            raise  # the key is wrong: every search would fail
        run.errors.append(f"{sl.key} page {page}: {str(exc)[:200]}")
        return False
    orgs = organizations_in(body)
    total = total_entries(body)
    split = page == 1 and not sl.band and bool(split_reason(orgs, total))
    spent = 1.0 if orgs else 0.0
    cur.page, cur.pages, cur.split = page, _pages(body, page, len(orgs)), split
    credits.record(ctx, JOB, spent, note=json.dumps(
        {"slice": sl.key, "page": page, "pages": cur.pages, "results": len(orgs), "total": total, "split": split,
         "why": split_reason(orgs, total) if split else ""}))
    room.spend(spent)
    run.pages += 1
    run.credits += spent
    if split and split_reason(orgs, total) == "no employee counts":
        return True  # no size on these rows: the size-band searches that follow take the same companies
    rows: list[dict] = []
    events: list[dict] = []
    partners: dict[str, dict] = {}
    for org in orgs:
        take(ctx, org, sl, run, depth, rows, events, partners)
    _write(ctx, rows, events, partners, run)
    return True


def backfill_bands(ctx: Context, run: _Run, room: credits.Room) -> int:
    """Give open accounts with no size band one, from size-band searches filtered to their Apollo ids.

    Accounts made before START_BANDS (the 2 Oct 2026 run) came from searches with no size filter and
    Apollo sent no employee count, so they have no band and verify_accounts cannot place them. Per
    BACKFILL_BATCH accounts it runs one search per band, filtered to their organization ids; a
    company a band's search returns is in that band. A page with results costs 1 credit, so about 4
    per 100 accounts. Accounts no band search returns keep no band and are tried next run.
    PHASE0-CONFIRM: organization_ids with organization_num_employees_ranges on mixed_companies/search
    (apollo_jobs.screen pairs organization_ids with another filter the same way).
    """
    todo = [a for a in ctx.store.select("accounts", {"status": list(OPEN_STATUSES)})
            if not a.get("size_band") and a.get("apollo_org_id") and a.get("employees") is None]
    banded = 0
    for i in range(0, len(todo), BACKFILL_BATCH):
        batch = {str(a["apollo_org_id"]): a for a in todo[i : i + BACKFILL_BATCH]}
        for band in ctx.settings.size_bands():
            if not batch or not room.allows():
                return banded
            filters = {"organization_ids": list(batch),
                       "organization_num_employees_ranges": [ctx.settings.employee_range(band)]}
            try:
                body = ctx.clients.apollo.search_organizations(filters, page=1, per_page=MAX_PER_PAGE)
            except ApiError as exc:
                if exc.status in (401, 403):
                    raise
                run.errors.append(f"size-band backfill {band}: {str(exc)[:200]}")
                return banded
            found = [str(o.get("organization_id") or o.get("id") or "") for o in organizations_in(body)]
            rows = [{"account_id": batch.pop(oid)["account_id"], "size_band": band} for oid in found if oid in batch]
            spent = 1.0 if found else 0.0
            credits.record(ctx, JOB, spent, note=json.dumps({"backfill": band, "asked": len(batch) + len(rows),
                                                              "banded": len(rows)}))
            room.spend(spent)
            run.pages += 1
            run.credits += spent
            if rows:
                ctx.store.upsert("accounts", rows)  # partial rows: only size_band changes
                banded += len(rows)
    return banded


def split_reason(orgs: Sequence[Mapping[str, Any]], total: int | None) -> str:
    """Why a search is read again by size band, or "": over Apollo's 50,000 (SPEC 7), or no employee counts.

    PHASE0-CONFIRM: search rows carry estimated_num_employees. If none on a page does, the size band
    comes from the search's own size filter instead, so accounts can still pass the 10-to-249 check.
    """
    if total is not None and total > MAX_RESULTS:
        return "over 50,000 companies"
    if orgs and all(_int(o.get("estimated_num_employees")) is None for o in orgs):
        return "no employee counts"
    return ""


def _depth(depth: Counter[str], groups: Iterable[str]) -> int:
    return sum(depth[g] for g in groups)


def run(ctx: Context) -> dict:
    """The source_universe job (JOB CONTRACT: run(ctx) -> summary)."""
    s = ctx.settings
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run}
    states, groups = allowed_states(s), active_groups(s)
    if not states or not groups:
        why = "no active state outside CA and WA" if not states else "no active industry with NAICS codes or keywords"
        summary.update(skipped=True, reason=why)
        log("source_universe_done", run_id=ctx.run_id, **summary)
        return summary
    target = QUEUE_WEEKS * s.general.weekly_enrol_cap
    depth = queue_depth(ctx)
    todo = pools(s, groups, target)
    summary.update(target=target, queue_before=dict(depth))
    floor = credits.floor_reason(ctx)
    if floor:
        summary.update(skipped=True, reason=floor)
        log("source_universe_done", run_id=ctx.run_id, **summary)
        return summary

    room = credits.room(ctx, JOB, SOURCING_SHARE)
    r = _Run(states=frozenset(states))
    # Before the queue check: accounts already queued with no size band are the ones it fills.
    summary["size_bands_backfilled"] = backfill_bands(ctx, r, room)
    if all(_depth(depth, g) >= t for g, t in todo):
        summary.update(status="ok", stopped_by="the queue already holds two weeks", pages=r.pages,
                       credits=r.credits, created=0, budget=room.as_dict(), errors=r.errors[:20])
        log("source_universe_done", run_id=ctx.run_id, **summary)
        return summary
    progress = cursors(ctx)
    searches = plan(s, groups, states)
    halt, short = "", []
    for pool, pool_target in todo:
        lane = _lane((sl for g in pool for sl in searches.get(g, ())), progress, s)
        while not halt and _depth(depth, pool) < pool_target:
            if r.pages >= MAX_PAGES_PER_RUN:
                halt = f"the limit of {MAX_PAGES_PER_RUN} pages a run"
            elif not room.allows():
                halt = "today's Apollo credits for sourcing are used"
            elif not lane:
                short.extend(pool)
                break
            else:
                sl = lane.popleft()
                cur = progress.setdefault(sl.key, Cursor())
                if not read_page(ctx, sl, cur, r, room, depth):
                    continue  # refused: that search waits for the next run
                if cur.split:
                    lane.extendleft(reversed([b for b in sl.by_band(s) if not progress.get(b.key, Cursor()).done]))
                elif not cur.done:
                    lane.append(sl)
        if halt:
            break
    stopped = halt or (f"every search is read to its end this month for {', '.join(short)}" if short
                       else "the queue holds two weeks")

    summary.update(
        status="ok", stopped_by=stopped, pages=r.pages, credits=r.credits,
        created=sum(r.created.values()), created_by_group=dict(r.created), updated=r.updated, facts=r.facts,
        skipped=dict(r.skipped), queue_after=dict(depth),
        pools=[{"groups": list(g), "target": t, "in_queue": _depth(depth, g)} for g, t in todo],
        credits_per_account=round(r.credits / max(1, sum(r.created.values())), 3),
        budget=room.as_dict(), errors=r.errors[:20],
    )
    log("source_universe_done", run_id=ctx.run_id, **summary)
    return summary
