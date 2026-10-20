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
     Every search is by size band from the start (SPEC 7's split over 50,000, and Apollo's rows carrying no
     employee count, both made it so), and the band comes from the search's own filter. (The unbanded search, its
     split and backfill_bands, the one-off for the 2 Oct 2026 accounts with no band, went with W2-1, 20 Oct 2026.)
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
     Apollo's organization enrich, through org_facts, for settings/model.APOLLO_ENRICH_GROUPS.

When to stop (W1-3a, 19 Oct 2026; code-sources B1, loop L1). The queue should hold two weeks of the
weekly target, 2 × weekly_enrol_cap (SPEC 2: "a two-week queue"), counted in companies enrol could
actually email: contactable supply (contactable_supply), not every open account. Until 19 Oct the count
was every new, queued or verified account, 305 to 338 of which had nobody to email, so sourcing created
no company for a week while ready supply fell to 1.3 days. Contactable supply is
    ready + findable × p_c + raw × p_v × p_c
where ready are the accounts enrol could take that have a sendable contact (contacts/pick.ready_accounts),
findable the verified ones pick_contacts may still find someone at (not tried in its 14-day wait, not
known to have no candidate), raw the new and queued ones not yet checked; p_v is the share of the
companies first checked over the last 28 days that passed verify, p_c the share of the accounts
pick_contacts tried over the same days that it found a contact at. Each yield is measured from 20
observations, a default (0.60, 0.46) before that, and never taken below 0.20; the summary says which.
When supply is short the run creates the raw accounts that close the gap, (target − contactable) ÷
(p_v × p_c), never more than 2 × weekly_enrol_cap in a run nor 3 × weekly_enrol_cap in a week (UK,
Monday to Sunday), so a floored yield cannot admit thousands at once (RS §2 W1-3). The Focus tab's groups
come first, each to its share of the target; the other active groups share the rest. The run also stops
when today's credits are used. A page whose companies the ceiling cuts off is read again next run (its
place stays on the page before), so no company on it is lost for the month.

Search order (code-sources B2; loop L2). The week's plan (sources/allocation.py, W3-3) gives each active state
its pages: one at least, the rest by a prior-weighted draw of each state's contactable yield per page, none above
max(0.40, 1 ÷ states) of them. A run reads the searches of the state furthest from its plan first, so pages
alternate state in the plan's proportions; ties, and the reads once the week's plan is used up, go in turn from a
different state each ISO week (week mod the number of states). Until 19 Oct they were read in the States tab's
order, alphabetical, and two Georgia pages filled the Technology pool: the queue was 85% Georgia. Each account
created is stamped with the search that found it, a sourced_by fact (source source_universe: slice, group, state,
band, page, run), so what happens to it later can be traced back to where sourcing looked (and the plan's yields
are read from them).

Credits. A search page costs 1 credit when it returns a company and 0 when it is empty (Apollo's
API docs; docs/phase0-facts.md), so about 0.01 to 0.02 credits per account sourced. Every page goes
into credit_ledger with its search and page in the note (what was paid for); each search's place is kept in
source_state (cursors:<month>; W3-3, code-sources B11: the ledger's notes were the place until then, copied in once),
so the next run carries on from the next page; each month starts again from page 1, which refreshes the universe
monthly (SPEC 7). Sourcing
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
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound import accounts, budget, ledger, parse
from us_outbound.clean.domains import is_personal_domain, record_alias, root_domain
from us_outbound.clean.names import clean_company_name
from us_outbound.clean.people import USPS_STATES, size_band, state_code
from us_outbound.clients.apollo import MAX_PAGE, MAX_PER_PAGE, organizations_in, total_entries
from us_outbound.clients.apollo import org_id as apollo_org_id
from us_outbound.clients.db import Range, new_id
from us_outbound.clients.http import ApiError, AuthError
from us_outbound import icp
from us_outbound.context import UK, Context
from us_outbound.enrol import focus
from us_outbound.industry.material import ENTITY_REASONS, RulesInput, entity, texts
from us_outbound.logs import log
from us_outbound.scoring import tiers
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.model import Industry, Settings
from us_outbound.settings.overrides import effective
from us_outbound.sources import allocation, source_state
from us_outbound.sources import apollo_credits as credits
from us_outbound.timeparse import iso_date

JOB = "source_universe"
SOURCE = "apollo_org"
ACCOUNT_SOURCE = "apollo"
SOURCING_SHARE = 0.25  # of apollo_monthly_credits; apollo_signals 0.25, apollo_enrich 0.15, the 0.35 left for email reveals
QUEUE_WEEKS = 2  # SPEC 2: a two-week queue
# Contactable supply (contactable_supply; loop L1). The ceilings are in weekly_enrol_cap: raw accounts a run, a week.
RUN_CEILING_WEEKS, WEEK_CEILING_WEEKS = 2, 3
YIELD_DAYS = 28  # the window both yields are measured over
YIELD_MIN_N = 20  # observations before a yield is measured rather than the default
YIELD_FLOOR = 0.20  # no yield is taken below this: 0.2 × 0.2 already asks for 25 raw accounts per contactable one
# The defaults before 20 observations are loop L1's: a guess to be replaced by the measure, not a measure. p_c is
# about what pick_contacts found in its first fortnight (19 Oct: 21 of 46 tried).
DEFAULT_P_VERIFY = 0.60
DEFAULT_P_CONTACT = 0.46
MEASURED, DEFAULT, FLOORED = "measured", "default", "floored"
RAW_STATUSES = ("new", "queued")  # not checked yet, or waiting to be checked again
# A first check that passed: verified, or already further on. A failed one: disqualified or parked (W1-4), or
# tiered Excluded while still new (a HubSpot exclusion: enrol.mark_excluded leaves the status as it is).
FURTHER_STATUSES = ("enrolled", "engaged", "demo_requested", "demo_booked")  # past verify: emailed
FAILED_STATUSES = ("disqualified", "parked")
NO_CANDIDATES = "no_contact_candidates"  # accounts.status_reason: nobody at Apollo to try (the contact pre-check)
SOURCED_BY = "sourced_by"  # the fact naming the search that found an account (source JOB)
OPEN_STATUSES = ("new", "queued", "verified")  # waiting to be enrolled
OUT_OF_QUEUE_TIERS = tiers.OUT_OF_QUEUE_TIERS
# organization_naics_codes takes 2 to 5 digits (Apollo's docs), so a 6-digit code is sent as its 5-digit
# prefix and Python checks the full code. Confirmed live 2 Oct 2026: the active industries' searches by
# 5-digit prefixes found the 432 accounts. Whether six digits work is untried (it would only narrow a search).
NAICS_DIGITS = 5
NAICS = "naics"  # a search by NAICS codes; otherwise it is a label's keyword search
MAX_PAGES_PER_RUN = 200  # well inside the 60-minute timeout
# Apollo's search rows carry no estimated_num_employees (2 Oct 2026, the first live run: none on 432
# accounts), so a search without a size filter gives accounts no size band at all. Every search is
# therefore read by size band, the bands the General size range touches (Settings.size_bands; Harry, 6 Oct 2026),
# each searched with its range clipped to min_employees and max_employees, and the band comes from the filter.
QUOTE_LIMIT = 300  # SPEC 6
DESCRIPTION_LIMIT = 600  # the description fact, for the opener's "what they do" phrase
APOLLO_ORG_URL = "https://app.apollo.io/#/organizations/{}"
USD = frozenset({"$", "USD", "US$"})  # funding_events[].currency read as dollars
AMOUNT = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([KMB])?", re.I)  # "8M", "$1.5B", "750K", "2,500,000"
AMOUNT_UNITS = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
# Account columns an Overrides row may set (SPEC 5: an override wins over every source).
OVERRIDABLE = ("clean_name", "legal_name", "hq_city", "hq_state", "industry", "industry_group", "naics",
               "employees", "size_band", "founded_year")
# Columns Clay confirms (docs/pipeline.md, "Which value wins"): Apollo no longer changes them once it has.
CLAY_OWNED = frozenset({"clean_name", "legal_name", "hq_city", "hq_state", "employees", "size_band",
                        "founded_year", "industry", "industry_group"})
# Columns the label check owns once it has decided (accounts.label_source set; labels.py, Harry, 7 Oct 2026): a
# company found again keeps its checked label, never the raw rules' one.
LABEL_OWNED = frozenset({"industry", "industry_group"})


def _prefixes(codes: Iterable[str]) -> list[str]:
    """NAICS codes as Apollo takes them (at most NAICS_DIGITS), dropping any another code already covers."""
    cut = sorted({c[:NAICS_DIGITS] for c in codes if c})
    return [c for c in cut if not any(p != c and c.startswith(p) for p in cut)]


PARTNER_FILTER = _prefixes(c for c in icp.PARTNER_NAICS if len(c) <= NAICS_DIGITS)  # ["524"]


# -- the sheet: states, groups, searches ---------------------------------------------------------


def allowed_states(settings: Settings) -> tuple[str, ...]:
    """Active states on the States tab, never CA or WA."""
    return tuple(s for s in settings.active_states() if s not in icp.NEVER_STATES)


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


def rotated(states: Sequence[str], week: int) -> list[str]:
    """The states in turn from index week mod their number: ISO week 43 over GA, IL, MA, NJ, NY, PA, TX starts at IL."""
    if not states:
        return []
    k = week % len(states)
    return [*states[k:], *states[:k]]


def plan(settings: Settings, groups: Sequence[str], states: Sequence[str], *, week: int = 0) -> dict[str, list[Slice]]:
    """group -> its searches, one per state and size band, in the groups' order.

    Within each search and band the states come in turn, from the state week mod their number (rotated), so the
    lane run() reads alternates state page by page (code-sources B2: read state by state in the States tab's
    alphabetical order, two Georgia pages filled the Technology pool, and the queue was 85% Georgia)."""
    order = rotated(states, week)
    covered: list[str] = []
    out: dict[str, list[Slice]] = {}
    for group in groups:
        labels = [i for i in settings.industries if searchable(i) and i.industry_group == group]
        codes = [c for c in _prefixes(c for i in labels for c in i.naics_prefixes)
                 if not any(c.startswith(p) for p in covered)]
        covered += codes
        umbrella = next((i.industry for i in labels if i.is_umbrella), "")
        searches = [(NAICS, tuple(codes), umbrella)] if codes else []
        searches += [(i.industry, i.apollo_keywords, i.industry) for i in labels if not i.naics_prefixes]
        out[group] = [Slice(group, st, what, terms, label, band, settings.employee_range(band))
                      for what, terms, label in searches for band in settings.size_bands() for st in order]
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
    """Accounts waiting to be enrolled (new, queued or verified, not Excluded or Held), per industry group.

    Kept for the summary's queue_before only: it counts accounts that will never be emailed (code-sources B1), so
    it no longer decides anything. contactable_supply does."""
    out: Counter[str] = Counter()
    for a in ctx.store.select("accounts", {"status": list(OPEN_STATUSES)}):
        if a.get("tier") not in OUT_OF_QUEUE_TIERS:
            out[ctx.settings.industry_group_of(a)] += 1
    return out


# -- contactable supply: how much the queue holds in companies enrol could email (loop L1) --------------------


@dataclass(frozen=True)
class Yield:
    """One funnel yield as the supply rule uses it: k of n, and whether it is that share, the default or the floor."""

    value: float
    basis: str  # MEASURED, DEFAULT or FLOORED
    k: int
    n: int

    def text(self, name: str, of: str) -> str:
        if self.basis == DEFAULT:
            return f"{name} {self.value:.2f} (default: {self.n} {of} so far, under {YIELD_MIN_N})"
        if self.basis == FLOORED:
            return f"{name} {self.value:.2f} (floored: measured {self.k} of {self.n})"
        return f"{name} {self.value:.2f} (measured: {self.k} of {self.n})"


def measured_yield(k: int, n: int, default: float) -> Yield:
    """k ÷ n once there are YIELD_MIN_N observations, never below YIELD_FLOOR; the default before that.

    RT §3 L1: the readout must say which is in use, or the loop looks as if it learned while it sits on its default
    or its floor."""
    if n < YIELD_MIN_N:
        return Yield(default, DEFAULT, k, n)
    share = k / n
    if share < YIELD_FLOOR:
        return Yield(YIELD_FLOOR, FLOORED, k, n)
    return Yield(share, MEASURED, k, n)


def verify_yield(ctx: Context) -> Yield:
    """p_v: of the distinct companies admitted in the last YIELD_DAYS whose first check has a decision, the share
    that passed it, from each account's status (RT §3 L1: never heartbeat sums, which count every re-check of a
    never-pass account again).

    Passed: verified, or already further on (enrolled, engaged). Failed: disqualified or parked (verify's terminal
    codes, W1-4; apollo_enrich's exact size), or tiered Excluded while still new (a HubSpot exclusion). An account
    still new or queued with no decision is not counted: it is waiting for its first check (or, before W1-4 lands,
    failed a check that left no mark, which reads p_v high until then). A company verified and then left out (its
    exact size, the hand-check) counts as failed: what the rule needs is the share of raw accounts that reach the
    verified pool and stay in it."""
    passed = failed = 0
    for a in ctx.store.select("accounts", {"first_seen": Range(ctx.now - timedelta(days=YIELD_DAYS), ctx.now)}):
        status = a.get("status")
        if status in FURTHER_STATUSES:
            passed += 1
        elif status in FAILED_STATUSES or a.get("tier") == tiers.EXCLUDED:
            failed += 1
        elif status == "verified":
            passed += 1
    return measured_yield(passed, passed + failed, DEFAULT_P_VERIFY)


def contact_yield(ctx: Context) -> Yield:
    """p_c: Σ picked ÷ Σ tried over pick_contacts' finished runs in the last YIELD_DAYS (their heartbeats)."""
    from us_outbound.contacts import pick  # here, not at the top: pick imports enrol, which imports this module

    picked = tried = 0
    for r in ctx.store.select("heartbeats", {"job": pick.JOB, "status": "ok",
                                             "started_at": Range(ctx.now - timedelta(days=YIELD_DAYS), ctx.now)}):
        d = r.get("detail")
        if isinstance(d, Mapping):
            picked += parse.integer(d.get("picked")) or 0
            tried += parse.integer(d.get("tried")) or 0
    return measured_yield(picked, tried, DEFAULT_P_CONTACT)


@dataclass(frozen=True)
class Supply:
    """Contactable supply: ready + findable × p_c + raw × p_v × p_c, in all and per industry group."""

    ready: int  # accounts enrol could take with a sendable contact (pick.ready_accounts)
    findable: int  # verified, enrol could take, no sendable contact, pick_contacts may still find one
    raw: int  # new or queued, not Excluded or Held: not checked yet
    p_v: float
    p_c: float
    contactable: float
    basis: dict[str, str]  # {"p_v": ..., "p_c": ...}: measured, default or floored
    by_group: dict[str, float] = field(default_factory=dict)  # industry group -> its contactable supply
    yields: tuple[Yield, Yield] | None = None  # (p_v, p_c) with their k and n, for the summary's words

    def of(self, groups: Iterable[str]) -> float:
        return sum(self.by_group.get(g, 0.0) for g in groups)

    def yields_text(self) -> str:
        if self.yields is None:
            return f"p_v {self.p_v:.2f} ({self.basis['p_v']}), p_c {self.p_c:.2f} ({self.basis['p_c']})"
        v, c = self.yields
        return f"{v.text('p_v', 'companies first checked')}, {c.text('p_c', 'accounts tried')}"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ready": self.ready, "findable": self.findable, "raw": self.raw,
                               "p_v": round(self.p_v, 3), "p_c": round(self.p_c, 3),
                               "contactable": round(self.contactable, 1), "basis": dict(self.basis),
                               "by_group": {g: round(v, 1) for g, v in self.by_group.items()}}
        if self.yields is not None:
            out["p_v_of"] = [self.yields[0].k, self.yields[0].n]
            out["p_c_of"] = [self.yields[1].k, self.yields[1].n]
        return out


def contactable_supply(ctx: Context) -> Supply:
    """How many companies the queue holds that enrol could email, counting the unchecked ones at what they yield.

    findable leaves out the accounts pick_contacts found nobody at in its RETRY_DAYS wait (pick.History.cooling) and
    those the contact pre-check marked status_reason no_contact_candidates: neither will have a contact soon
    (code-sources B1 fix: drop from depth any account whose last pick outcome is "no contact")."""
    from us_outbound.contacts import pick  # here, not at the top: pick imports enrol, which imports this module

    s = ctx.settings
    ready = pick.ready_accounts(ctx)
    need, _ = pick.waiting(ctx)
    past = pick.history(ctx.store, ctx.now)
    findable = [a for a in need if not past.cooling(a["account_id"], ctx.now) and a.get("status_reason") != NO_CANDIDATES]
    raw = [a for a in ctx.store.select("accounts", {"status": list(RAW_STATUSES)})
           if a.get("tier") not in OUT_OF_QUEUE_TIERS]
    v, c = verify_yield(ctx), contact_yield(ctx)
    by_group: dict[str, float] = {}
    for rows, weight in ((ready, 1.0), (findable, c.value), (raw, v.value * c.value)):
        for a in rows:
            g = s.industry_group_of(a)
            by_group[g] = by_group.get(g, 0.0) + weight
    contactable = len(ready) + len(findable) * c.value + len(raw) * v.value * c.value
    return Supply(ready=len(ready), findable=len(findable), raw=len(raw), p_v=v.value, p_c=c.value,
                  contactable=contactable, basis={"p_v": v.basis, "p_c": c.basis}, by_group=by_group, yields=(v, c))


def raw_need(short: float, supply: Supply) -> int:
    """Raw accounts that close a shortfall of contactable supply: ceil(short ÷ (p_v × p_c)); 0 when nothing is short.

    Rounded to 9 places first, so a shortfall that is a whole multiple of the product in exact arithmetic (140 ÷
    0.25) is not pushed up one by the float error of the product."""
    if short <= 0:
        return 0
    return math.ceil(round(short / (supply.p_v * supply.p_c), 9))


def created_this_week(ctx: Context) -> int:
    """Accounts source_universe's searches created this week (UK, Monday to Sunday): the weekly ceiling's count."""
    start, end = budget.week_bounds(ctx.now)
    return len(ctx.store.select("accounts", {"source": ACCOUNT_SOURCE, "first_seen": Range(start, end)}))


# -- where each search got to this month (source_state; W3-3) ----------------------------------------


@dataclass
class Cursor:
    page: int = 0  # the last page read this month
    pages: int | None = None  # the pages Apollo said the search has

    @property
    def done(self) -> bool:
        return self.pages is not None and self.page >= min(self.pages, MAX_PAGE)


def cursors(ctx: Context) -> dict[str, Cursor]:
    """Each search's place this month (UK), from source_state (W3-3, code-sources B11: until then from this job's
    credit_ledger notes, which now record money only). The month's ledger-note cursors are copied in once, by
    `us-outbound source migrate-state` or the first run that finds no copy, so the month does not restart at page 1."""
    source_state.migrate_cursors(ctx.store, credits.SYSTEM, JOB, ctx.now)
    raw = source_state.get(ctx.store, source_state.cursors_key(ctx.now)) or {}
    out: dict[str, Cursor] = {}
    for key, c in raw.items():
        if isinstance(c, Mapping):
            pages = parse.integer(c.get("pages"))
            out[str(key)] = Cursor(parse.integer(c.get("page")) or 0, pages)
    return out


def save_cursors(ctx: Context, progress: Mapping[str, Cursor]) -> None:
    """This month's places into source_state (a search not read yet this month has none)."""
    source_state.put(ctx.store, source_state.cursors_key(ctx.now),
                     {k: {"page": c.page, "pages": c.pages} for k, c in progress.items() if c.page or c.pages is not None},
                     ctx.now)


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


def org_domain(org: Mapping[str, Any]) -> str | None:
    """The company's root domain: Apollo's primary domain, else its website."""
    for v in (org.get("primary_domain"), org.get("domain"), org.get("website_url")):
        root = root_domain(str(v)) if v else None
        if root:
            return root
    return None


def org_naics(org: Mapping[str, Any]) -> list[str]:
    """PHASE0-CONFIRM: search rows carry naics_codes (as enrichment does)."""
    return list(RulesInput.from_org(org).codes)


def org_keywords(org: Mapping[str, Any]) -> list[str]:
    return list(RulesInput.from_org(org).tags)


def org_technologies(org: Mapping[str, Any]) -> list[str]:
    names = texts(org.get("technology_names"))
    if not names:
        names = texts([t.get("name") for t in org.get("current_technologies") or () if isinstance(t, Mapping)])
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
    return not country or country in icp.US_COUNTRIES


def funding_usd(event: Mapping[str, Any]) -> int | None:
    """A funding event's amount in dollars: a number, or text like "8M", "$1.5B" or "750K"; None otherwise.

    PHASE0-CONFIRM: organization enrich gives funding_events[].amount as text ("8M") with a
    currency ("$"); an amount in another currency is not taken as dollars.
    """
    if str(event.get("currency") or "$").strip().upper() not in USD:
        return None
    v = event.get("amount")
    n = parse.integer(v)
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
    events = sorted((e for e in org.get("funding_events") or () if isinstance(e, Mapping) and iso_date(e.get("date"))),
                    key=lambda e: iso_date(e.get("date")), reverse=True)
    when = iso_date(org.get("latest_funding_round_date")) or (iso_date(events[0].get("date")) if events else None)
    stage = str(org.get("latest_funding_stage") or (events[0].get("type") if events else "") or "").strip()
    amount = next((a for e in events if iso_date(e.get("date")) == when and (a := funding_usd(e))), None)
    out: dict[str, Any] = {}
    if when is not None and when <= today:
        out["days_since_funding"] = (today - when).days
        out["funding_date"] = when.isoformat()
    if stage:
        out["funding_stage"] = stage
    if amount:
        out["funding_amount_usd"] = amount
    return out


def _naics_match(codes: Sequence[str], prefix: str) -> int:
    """How many digits of the label's prefix the company's codes match (0 for none): codes as _read_codes gives them."""
    return max((len(prefix) for c in codes if c.startswith(prefix)), default=0)


def _read_codes(codes: Sequence[str], settings: Settings) -> list[str]:
    """The company's codes as the Industries tab can place them.

    Apollo often gives five digits ("54161") for a six-digit code. That stands for the one six-digit code the tab
    names under it when a row places that code (an exclusion alone says only what the code is not), and otherwise,
    when the tab names any under it, for its first four digits.

    The crosswalk, 10 Oct 2026: a five-digit code used to match every longer prefix that starts with it, so 54161
    (consulting: 23 companies, 9% marketing) took Marketing's 541613, and 54151 (computer systems design: 38
    companies, 84% tech) hit Technology & Startups' exclusions 541512/3/9 and got no label.
    """
    placed = {p for ind in settings.industries for p in ind.naics_prefixes if len(p) == 6}
    named = placed | {p for ind in settings.industries for p in ind.exclude_naics if len(p) == 6}
    out = []
    for c in codes:
        if len(c) == 5 and (under := [p for p in named if p.startswith(c)]):
            c = under[0] if len(under) == 1 and under[0] in placed else c[:4]
        out.append(c)
    return out


def best_label(codes: Sequence[str], keyword_text: str, settings: Settings) -> Industry | None:
    """The one Industries label that fits best, active or not; None if none fits.

    Candidates are the labels whose NAICS prefixes match the company's codes (none of its
    exclude_naics), or, when none does, the labels with a keyword match. A label within a group
    (Games studios, Fintech) is a candidate on its NAICS codes only with a keyword match of its own;
    the group's umbrella label (Technology & Startups) needs none. Among them: the most keyword
    matches, then the longest NAICS match, then a label over its group's umbrella label, then the
    Industries priority, then the tab's order.

    Harry, 7 Oct 2026: the first cards went out as games studios, AI and adtech to a fulfilment
    consultancy, a surgeons' society and a data-centre firm. The labels within Technology & Startups
    share the umbrella's NAICS prefixes (5415 and the rest) and a few carry a longer one that says
    little (Games studios 541511, custom programming; AI & deep tech 541715, R&D), so with no keyword
    to tell them apart the tie-break picked the first, or the longest code. Codes alone now place a
    company in its group, whose umbrella copy fits any company in it; a label's own copy needs its words.
    """
    codes = _read_codes(codes, settings)
    rows = []
    for idx, ind in enumerate(settings.industries):
        if any(_naics_match(codes, x) for x in ind.exclude_naics):
            continue
        naics = max((_naics_match(codes, p) for p in ind.naics_prefixes), default=0)
        words = len(find_terms(keyword_text, ind.apollo_keywords)) if keyword_text else 0
        rows.append((ind, naics, words, idx))
    pool = [r for r in rows if r[1] and (r[2] or r[0].is_umbrella)] or [r for r in rows if r[2]]
    if not pool:
        return None
    return min(pool, key=lambda r: (-r[2], -r[1], r[0].is_umbrella, r[0].priority, r[3]))[0]


def _quote(text: str) -> str:
    q = " ".join(text.split())
    return q if len(q) <= QUOTE_LIMIT else q[: QUOTE_LIMIT - 1] + "…"


def org_facts(account_id: str, org: Mapping[str, Any], state: str, now: datetime) -> list[dict]:
    """The apollo_org facts of one organization record, each with a quote and the Apollo page."""
    org_id = apollo_org_id(org)
    url = APOLLO_ORG_URL.format(org_id) if org_id else ""
    today = now.astimezone(UK).date()
    out: list[dict] = []

    def add(fact: str, value: Any, quote: str) -> None:
        if value in (None, "", [], ()):
            return
        out.append({"event_id": new_id(), "account_id": account_id, "source": SOURCE, "fact": fact, "value": value,
                    "quote": _quote(quote), "source_url": url, "observed_at": now})

    employees = parse.integer(org.get("estimated_num_employees"))
    add("employees", employees, f"Apollo: about {employees} employees")
    codes = org_naics(org)
    add("naics", codes, "Apollo NAICS: " + ", ".join(codes))
    city = str(org.get("city") or "").strip()
    add("hq_state", state, f"Apollo: HQ in {city + ', ' if city else ''}{state}")
    growth = parse.number(org.get("organization_headcount_twelve_month_growth"))  # PHASE0-CONFIRM: a fraction (0.12)
    add("headcount_growth_12m", growth, f"Apollo: headcount {growth:+.0%} over 12 months" if growth is not None else "")
    funding = org_funding(org, today)
    when = funding.get("funding_date", "")
    add("days_since_funding", funding.get("days_since_funding"), f"Apollo: latest funding round on {when}")
    add("funding_stage", funding.get("funding_stage"), f"Apollo: latest round {funding.get('funding_stage')} ({when})")
    amount = funding.get("funding_amount_usd")
    add("funding_amount_usd", amount, f"Apollo: latest round ${amount:,}" if amount else "")
    founded = parse.integer(org.get("founded_year"))
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
    """The account columns with the domain's Overrides rows applied (SPEC 5; settings/overrides.py)."""
    return effective(row, settings, domain=domain, fields=OVERRIDABLE)


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
    employees = parse.integer(org.get("estimated_num_employees"))
    codes = org_naics(org)
    return {
        "apollo_org_id": apollo_org_id(org) or None,
        "hq_city": str(org.get("city") or "").strip() or None,
        "hq_state": state,
        "hq_country": "United States" if in_us(org) and str(org.get("country") or "").strip() else None,
        "industry": label.industry if label else None,
        "industry_group": label.industry_group if label else None,
        "naics": ", ".join(codes) or None,
        "employees": employees,
        "size_band": size_band(employees) if employees is not None else (band or None),
        "founded_year": parse.integer(org.get("founded_year")),
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
    cut: int = 0  # companies left on a page the ceiling cut short, read again next run


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
    personal = icp.gate({"domain": domain}, {}, s, checks={icp.PERSONAL})
    if personal:
        run.skipped[personal.text] += 1
        return
    again = domain in run.seen
    if again and (run.seen[domain] or not sl.band):
        return  # found by two searches this run; a size-band search still gives a size band
    run.seen[domain] = sl.band
    state = state_code(str(org.get("state") or "")) or sl.state
    employees = parse.integer(org.get("estimated_num_employees"))
    # The ICP gate's state and size rules (icp.SOURCE), on what the search row says; a size it does not give passes,
    # as the band searched is in the range. The size rule reads the domain's Overrides rows, as verify does (W2-2).
    why = icp.gate({"domain": domain, "hq_state": state, "employees": employees}, {}, s,
                   checks={icp.STATE, icp.SIZE})
    if not in_us(org) or state not in run.states or (why and why.code in ("state", "state_unknown")):
        run.skipped["HQ outside the active states"] += 1
        return
    if why:
        run.skipped[why.text] += 1
        return
    inp = RulesInput.from_org(org)
    kind = entity(inp, s)
    if kind:
        run.skipped[ENTITY_REASONS[kind]] += 1  # before any label is tried (industry/material.py)
        return
    codes = list(inp.codes)
    label = best_label(inp.codes, inp.keyword_text, s) if inp else s.industry(sl.label) if sl.label else None
    if label is None:
        run.skipped["no Industries label fits"] += 1
        return
    if s.placeable(label) is None:
        run.skipped["its best Industries label is switched off"] += 1
        return
    label = s.placeable(label)
    partner = icp.partner_match({"naics": codes, "industry": label.industry, "keywords": list(inp.tags),
                                 "apollo_industry": org.get("industry")}, {})
    if partner:
        run.skipped[icp.PARTNER_TEXT] += 1
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
        if account.get("label_source"):
            cols = {k: v for k, v in cols.items() if k not in LABEL_OWNED}
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


def sourced_by(account_id: str, sl: Slice, page: int, run_id: str, now: datetime) -> dict:
    """The fact naming the search that created an account (code-sources §6: accounts.source says only "apollo"), so
    what happens to it later (verified, contacted, replied) can be traced back to where sourcing looked. Its source
    is the job, not a SPEC 7 source key, so no Signals row scores it."""
    band = f", {sl.band} staff" if sl.band else ""
    return {"event_id": new_id(), "account_id": account_id, "source": JOB, "fact": SOURCED_BY,
            "value": {"slice": sl.key, "group": sl.group, "state": sl.state, "band": sl.band or None, "page": page,
                      "run_id": run_id},
            "quote": _quote(f"Found by Apollo's search of {sl.group} in {sl.state}{band}, page {page}"),
            "source_url": "", "observed_at": now}


def read_page(ctx: Context, sl: Slice, cur: Cursor, run: _Run, room: credits.Room, depth: Counter[str],
              limit: int | None = None, progress: Mapping[str, Cursor] | None = None) -> bool:
    """Read the search's next page into accounts and facts; False if Apollo refused it.

    limit: the most accounts the page may create (the run's and the week's ceilings). The companies after the one
    that reaches it are left for the next run, which reads the page again (its cursor stays on the page before), so
    none of them is lost for the month; the ones already accounts are only found again then. progress: the month's
    cursors, saved to source_state once the page is read (save_cursors). The ledger row records what was paid for
    (the search, the page, its results), never a place: nothing reads it back (W3-3)."""
    page = cur.page + 1
    what = {"search": sl.key, "page": page}
    with ledger.charge(ctx, credits.SYSTEM, JOB, 1.0, note=ledger.reserved_note(what)) as paid:
        try:
            body = ctx.clients.apollo.search_organizations(sl.filters(ctx.settings), page=page, per_page=MAX_PER_PAGE)
        except ApiError as exc:
            spent = paid.fail(exc, note=ledger.failed_note(what, exc))  # kept unless refused (9 Oct 2026)
            room.spend(spent)
            run.credits += spent
            if isinstance(exc, AuthError):
                raise  # the key is wrong: every search would fail
            run.errors.append(f"{sl.key} page {page}: {str(exc)[:200]}")
            return False
        orgs = organizations_in(body)
        note = {"search": sl.key, "page": page, "results": len(orgs), "total": total_entries(body)}
        spent = paid.settle(1.0 if orgs else 0.0, note=json.dumps(note))
    cur.page, cur.pages = page, _pages(body, page, len(orgs))
    room.spend(spent)
    run.pages += 1
    run.credits += spent
    rows: list[dict] = []
    events: list[dict] = []
    partners: dict[str, dict] = {}
    made = 0
    for i, org in enumerate(orgs):
        if limit is not None and made >= limit:
            # The ceiling is reached: the rest of the page waits for the next run, which reads the page again.
            run.cut += len(orgs) - i
            cur.page = page - 1
            break
        got = take(ctx, org, sl, run, depth, rows, events, partners)
        if got is not None and got.outcome == "created":
            made += 1
            events.append(sourced_by(str(got.account_id), sl, page, ctx.run_id, ctx.now))
    _write(ctx, rows, events, partners, run)
    if progress is not None:
        save_cursors(ctx, progress)
    return True


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
    cap = s.general.weekly_enrol_cap
    target = QUEUE_WEEKS * cap
    supply = contactable_supply(ctx)
    todo = pools(s, groups, target)
    summary.update(target=target, queue_before=dict(queue_depth(ctx)), supply=supply.as_dict())
    floor = credits.floor_reason(ctx)
    if floor:
        summary.update(skipped=True, reason=floor)
        log("source_universe_done", run_id=ctx.run_id, **summary)
        return summary

    room = credits.room(ctx, JOB, SOURCING_SHARE)
    r = _Run(states=frozenset(states))
    # Each pool's raw need: its share of the target less its contactable supply, at the yields (loop L1).
    needs = [(pool, pool_target, raw_need(pool_target - supply.of(pool), supply)) for pool, pool_target in todo]
    week_before = created_this_week(ctx)
    run_ceiling, week_left = RUN_CEILING_WEEKS * cap, max(0, WEEK_CEILING_WEEKS * cap - week_before)
    allowance = min(run_ceiling, week_left)
    wanted = sum(n for _, _, n in needs)
    summary.update(raw_need=wanted, raw_allowance=allowance, created_this_week_before=week_before)
    held = (f"{supply.contactable:.0f} of {target} (two weeks of weekly_enrol_cap); "
            f"{supply.yields_text()}")
    if not wanted:
        summary.update(status="ok", stopped_by=f"contactable supply holds two weeks: {held}", pages=r.pages,
                       credits=r.credits, created=0, budget=room.as_dict(), errors=r.errors[:20])
        log("source_universe_done", run_id=ctx.run_id, **summary)
        return summary
    if week_left < run_ceiling:
        ceiling = (f"the ceiling of {WEEK_CEILING_WEEKS} × weekly_enrol_cap ({WEEK_CEILING_WEEKS * cap}) raw accounts "
                   f"a week, {week_before} created already this week")
    else:
        ceiling = f"the ceiling of {RUN_CEILING_WEEKS} × weekly_enrol_cap ({run_ceiling}) raw accounts a run"
    progress = cursors(ctx)
    week = ctx.today_uk().isocalendar()[1]
    searches = plan(s, groups, states, week=week)
    # The week's pages per state (sources/allocation.py, loop L2; W3-3): the lanes read in its order and spend it.
    week_plan = allocation.this_week(ctx, [(sl.group, sl.state, sl.band) for g in groups for sl in searches.get(g, ())],
                                     states)
    order = rotated(states, week)
    depth: Counter[str] = Counter()  # accounts this run created, per industry group
    halt, short = "", []

    def room_left(pool: Sequence[str], need: int) -> int:
        """What this pool may still create: its need, and what the ceilings leave of the run (any group's counts)."""
        return min(need - _depth(depth, pool), allowance - sum(depth.values()))

    for pool, _, need in needs:
        todo_here = [sl for g in pool for sl in searches.get(g, ()) if not progress.get(sl.key, Cursor()).done]
        lane = allocation.Lane(todo_here, week_plan, order) if room_left(pool, need) > 0 else None
        while lane is not None and not halt and room_left(pool, need) > 0:
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
                if not read_page(ctx, sl, cur, r, room, depth, limit=room_left(pool, need), progress=progress):
                    continue  # refused: that search waits for the next run
                week_plan.spend(sl.state)
                allocation.save(ctx, week_plan)
                if not cur.done:
                    lane.append(sl)
        if halt:
            break
    created = sum(r.created.values())
    if halt:
        stopped = halt
    elif short:
        stopped = f"every search is read to its end this month for {', '.join(short)}"
    elif wanted > allowance:
        stopped = f"{ceiling}; contactable supply was {held}"
    else:
        stopped = f"created the {wanted} raw accounts contactable supply needed; it was {held}"

    summary.update(
        status="ok", stopped_by=stopped, pages=r.pages, credits=r.credits,
        created=created, created_by_group=dict(r.created), updated=r.updated, facts=r.facts,
        skipped=dict(r.skipped), left_on_cut_pages=r.cut,
        pools=[{"groups": list(g), "target": t, "contactable": round(supply.of(g), 1), "raw_need": n,
                "created": _depth(depth, g)} for g, t, n in needs],
        credits_per_account=round(r.credits / max(1, created), 3),
        allocation={"week": week_plan.week, "pages": week_plan.pages, "left": week_plan.left},
        budget=room.as_dict(), errors=r.errors[:20],
    )
    log("source_universe_done", run_id=ctx.run_id, **summary)
    return summary
