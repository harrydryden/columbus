"""Source "lookalike_lead": new US accounts like Spill's active customers, found once a month in Apollo.

Harry, 5 Oct 2026: "We should also be utilising Clay or Apollo for this lookalike algorithm if either have
this functionality ... The customer base for Spill is fairly static, so the whole HubSpot pull and lookalike
search can happen on a monthly cadence." Then, the same day: Apollo's lookalikes, as they fit the system,
and UK customers may inspire US lookalikes (most of Spill's customers are in the UK).

The lookalike_leads job (the 1st of each month, 02:50 UK, after lookalikes at 02:30; ops/schedule.py):
  1. Seeds. Spill's customers are read from HubSpot as the lookalikes job reads them
     (lookalikes.read_customers: read only, company fields only). A seed is an active customer with a
     domain, 10 to 249 staff (lookalikes.customer_band's 10-49, 50-99 and 100-249: covered staff, else
     HubSpot's headcount) and an active Industries label, in any country. When the Focus tab's shares
     fill the whole week, only its groups seed, as only they are sourced (apollo_universe.pools);
     otherwise its groups come first. Seeds are taken in turn from each (industry group, size band)
     cell, up to SEEDS_PER_CELL a cell and MAX_SEEDS a run, so every cell gets one before any gets two.
     Within a cell US customers come first (the closest match for a US market), then the others, each
     in a month's rotation (a hash of the month and the domain): the same seeds if the job runs again
     this month, others next month, so a static customer base still brings new leads. Seeds live only
     in memory: no customer's domain or name goes into a table, a log, a ledger note or the summary,
     which counts them by cell and by country (US or non-US).
  2. Apollo. The seeds' organization ids are found by domain; then for each cell, its US seeds and its
     other seeds apart (so each search says which kind of seed it used), Apollo's organization search
     ranks companies by likeness to up to 5 seeds (lookalike_organization_ids), one search per size band
     of the cell: search rows carry no headcount, so the band must come from the filter (as source_universe
     reads it), and companies of the seeds' own size are the lookalikes wanted. Across the cells the
     searches cover the General size range (10 to 249 by default; cell_bands). The results are US only,
     three times over:
       a. in the request: HQ in the United States, never California or Washington (the wording
          source_universe's filters use), the size band, and insurers and brokers left out, as there;
       b. on each row before the door: Apollo's HQ country must be the US (a row without one is counted
          "not US"), and source_universe's take() then wants an HQ state active on the States tab (never
          CA or WA; FL only when switched on), a size inside the General range when the row gives one, an active
          Industries label, and no partner;
       c. after the door: verify_accounts checks state, size, HubSpot, partners and suppression as for
          any account. There is no shortcut.
     A search that returns nothing is counted and passed over: Apollo returns nothing when it holds no
     lookalike data for a seed, and for the whole search when one of its seeds has none, so a group of
     seeds that comes back empty is tried one seed at a time, with the same filters (an empty page
     costs nothing). There is never a search without the filters.
  3. Each company comes in by the front door through apollo_universe.take (accounts.admit: one account
     per root domain, suppressed and partner domains refused; the account columns and the apollo_org
     facts as source_universe writes them), with account source "lookalike". A Spill customer, current or
     former, is never admitted, even before the lookalikes job has suppressed its domain.
  4. Each account it creates gets two lookalike_lead facts: lookalike_lead, {provider, seed_group,
     seed_band, seed_country} (US or non-US, never the customer), and found_as_lookalike = true, which
     the "Found as a lookalike of a customer" signal scores. A company that is an account already is
     counted as refused and left as it is.

Yield watch: Apollo's likeness may favour companies in the seed's own country, so UK seeds filtered to the
US may find few. The summary counts, for US seeds and for the others, the searches (and the empty ones),
the rows returned, and the companies admitted, refused and not US, so Harry can see whether UK seeds work.

Credits, per run: at most APOLLO_CREDITS_PER_RUN, never while Apollo's balance is below apollo_floor, and
never more than is left of the month's apollo_monthly_credits. A search page with results costs 1, an
empty one 0: about 1 for the seeds' ids and 1 or 2 for each cell and seed country (10-49 is two Apollo
bands), recorded in credit_ledger with job lookalike_leads (sources/apollo_credits.py). A monthly job is
not paced by the weekday: its spend comes out of the month's budget, so the other Apollo jobs' allowance
that day is that much smaller.

Once a month: a run that already finished this month is not repeated (the seeds would be the same, so
the credits would buy the same companies again).

Dry-run: the HubSpot and Apollo calls are reads, so they happen and their credits are recorded; accounts,
facts and the ledger are database writes, which dry-run makes too, as source_universe does. The job writes
nothing outside the database in any mode, so --live changes nothing for it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from us_outbound import budget, ledger
from us_outbound.clean.domains import root_domain
from us_outbound.clean.people import state_code
from us_outbound.clients.apollo import LOOKALIKE_SEEDS_MAX, MAX_PER_PAGE, organizations_in
from us_outbound.clients.apollo import org_id as apollo_org_id
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError, AuthError
from us_outbound.context import UK, Context
from us_outbound.enrol import focus
from us_outbound.logs import log
from us_outbound.settings.model import Settings
from us_outbound.sources import apollo_credits as credits
from us_outbound.sources import apollo_universe as uni
from us_outbound.sources import lookalikes
from us_outbound.timeparse import utc

JOB = "lookalike_leads"
SOURCE = "lookalike_lead"  # signal_events.source (settings/model.py SOURCE_KEYS)
FACT, FLAG_FACT = "lookalike_lead", "found_as_lookalike"  # the dict for people; the flag the signal reads
ACCOUNT_SOURCE = "lookalike"  # accounts.source
PROVIDER = "apollo"
US_SEED, OTHER_SEED = "US", "non-US"  # a seed's country, the only thing the run keeps about the customer
SEED_COUNTRIES = (US_SEED, OTHER_SEED)

SEEDS_PER_CELL = LOOKALIKE_SEEDS_MAX  # 5: a few per cell, and the most one Apollo lookalike search takes
MAX_SEEDS = 40  # a run's seeds across every cell
APOLLO_CREDITS_PER_RUN = 60
PAGES_PER_SEARCH = 1  # Apollo ranks by likeness: the first 100 in a band are the closest
MAX_NEW_PER_RUN = 1000  # what verify_accounts checks in one run (verify.MAX_ACCOUNTS_PER_RUN)
DOMAIN_CHUNK = MAX_PER_PAGE  # seeds looked up by domain 100 at a time: one page answers them all
POST_HOURS = 36  # the daily post mentions a run this recent (the 1st's post)
# PHASE0-CONFIRM: organization_locations takes a country ("United States"), and organization_not_locations
# takes states written as source_universe writes them ("California, US"), on the lookalike search too.
US_LOCATION = "United States"
NOT_LOCATIONS = tuple(uni.location(s) for s in sorted(uni.NEVER_STATES))  # never CA or WA (SPEC 1.3)
def cell_bands(settings: Settings, cell: str) -> tuple[str, ...]:
    """A cell's size bands (lookalikes.TARGET_BANDS) as Apollo's search bands within the General size range: the
    bands the range touches that read as this cell (lookalikes.PROSPECT_BANDS), so 5-9 searches with 10-49's seeds
    and 250-499 with 100-249's when the range is set wider (Harry, 6 Oct 2026)."""
    return tuple(b for b in settings.size_bands() if lookalikes.PROSPECT_BANDS.get(b) == cell)
NOT_US = "not US"
CUSTOMER = "a Spill customer"
EXISTS = "already an account"
PARTNER = "a partner, never prospected"
SUPPRESSED = "suppressed"
REFUSALS = (EXISTS, SUPPRESSED, PARTNER, CUSTOMER)


@dataclass(frozen=True)
class Seed:
    """One active customer, in memory for the run only: never stored, logged or summarised by domain."""

    domain: str
    group: str
    band: str  # one of lookalikes.TARGET_BANDS
    country: str  # US_SEED or OTHER_SEED

    @property
    def cell(self) -> str:
        return f"{self.group}|{self.band}"


# -- seeds -------------------------------------------------------------------------------------------


def seed_groups(settings: Settings) -> list[str]:
    """Industry groups that may seed, the Focus tab's first (largest share first).

    A group needs an active Industries label. When the Focus tab's shares make up the whole week, only
    its groups seed: source_universe sources only those then (apollo_universe.pools), and leads in other
    groups would wait for a day the focus groups run short.
    """
    active: dict[str, int] = {}
    for i, ind in enumerate(settings.industries):
        if ind.active:
            active.setdefault(ind.industry_group, i)
    shares = {f.industry_group: f.share for f in settings.focus if f.industry_group in active}
    groups = list(active)
    if shares and sum(shares.values()) >= 1.0 - 1e-9:
        groups = [g for g in groups if g in shares]
    return sorted(groups, key=lambda g: (focus.group_rank(g, settings), active[g]))


def eligible(customers: Iterable[lookalikes.Customer], settings: Settings) -> list[lookalikes.Customer]:
    """Active customers, in any country, with a domain, 10 to 249 staff and an active label in a seed group."""
    groups = set(seed_groups(settings))
    out = []
    for c in customers:
        if c.status != lookalikes.ACTIVE or not c.domain or c.band not in lookalikes.TARGET_BANDS:
            continue
        ind = settings.industry(c.label) if c.label else None
        if ind is None or not ind.active or ind.industry_group not in groups:
            continue
        out.append(c)
    return out


def _rotation(c: lookalikes.Customer, month: str) -> str:
    """A month's stable order: the same seeds on a second run this month, others next month."""
    return hashlib.sha256(f"{month}|{c.domain}".encode()).hexdigest()


def select_seeds(customers: Iterable[lookalikes.Customer], settings: Settings, now: datetime,
                 limit: int = MAX_SEEDS) -> list[Seed]:
    """The run's seeds in the order they are used: one from each cell in turn, up to SEEDS_PER_CELL a cell.

    Cells go in the groups' order (seed_groups), then by size band, 10-49 first; within a cell US customers
    come first, then the others, each in the month's rotation.
    """
    month = now.astimezone(UK).strftime("%Y-%m")
    rank = {g: i for i, g in enumerate(seed_groups(settings))}
    cells: dict[tuple[str, str], list[lookalikes.Customer]] = defaultdict(list)
    for c in eligible(customers, settings):
        cells[(c.group, c.band)].append(c)
    order = sorted(cells, key=lambda k: (rank.get(k[0], len(rank)), lookalikes.TARGET_BANDS.index(k[1]), k[0]))
    queues = [sorted(cells[k], key=lambda c: (not c.us, _rotation(c, month)))[:SEEDS_PER_CELL] for k in order]
    out: list[Seed] = []
    for turn in range(SEEDS_PER_CELL):
        for q in queues:
            if turn < len(q) and len(out) < limit:
                c = q[turn]
                out.append(Seed(str(c.domain), c.group, c.band, US_SEED if c.us else OTHER_SEED))
    return out


def searches(seeds: Iterable[Seed]) -> list[list[Seed]]:
    """The seeds as Apollo searches, in the seeds' order: each cell's US seeds and its other seeds apart, at most
    5 a search, so every search's seeds are alike and its yield can be put down to one kind of seed."""
    groups: dict[tuple[str, str], list[Seed]] = {}
    for s in seeds:
        groups.setdefault((s.cell, s.country), []).append(s)
    return [g[i : i + LOOKALIKE_SEEDS_MAX] for g in groups.values() for i in range(0, len(g), LOOKALIKE_SEEDS_MAX)]


# -- the run ---------------------------------------------------------------------------------------------


def _yield() -> Counter[str]:
    """One seed country's yield: seeds, searches, rows returned, and what became of the rows."""
    return Counter({k: 0 for k in ("seeds", "unknown_to_apollo", "searches", "empty_searches", "results",
                                   "admitted", "refused", "not_US", "other_skips")})


@dataclass
class _Leads:
    """What the run did, in counts. Domains stay in memory (handled, customers) and never reach the summary."""

    customers: frozenset[str]  # every customer root domain, active or former: never admitted
    universe: uni._Run  # source_universe's own run state, so take() skips and counts as it does there
    cap: float = 0.0
    spent: float = 0.0
    pages: int = 0
    seeds_used: list[Seed] = field(default_factory=list)
    handled: set[str] = field(default_factory=set)  # rows already taken this run
    created: Counter[str] = field(default_factory=Counter)  # per industry group
    refused: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    by_country: dict[str, Counter[str]] = field(default_factory=lambda: {c: _yield() for c in SEED_COUNTRIES})
    errors: list[str] = field(default_factory=list)
    stopped_by: str = ""

    def allows(self) -> bool:
        return self.spent + 1.0 <= self.cap + 1e-9

    @property
    def admitted(self) -> int:
        return sum(self.created.values())


def us_filters(band: str, settings: Settings) -> dict[str, Any]:
    """source_universe's filters for one size band (clipped to the General size range), over the whole US at
    once, never CA or WA."""
    f: dict[str, Any] = {
        "organization_locations": [US_LOCATION],
        "organization_not_locations": list(NOT_LOCATIONS),
        "organization_num_employees_ranges": [settings.employee_range(band)],
    }
    if uni.PARTNER_FILTER:
        f["not_organization_naics_codes"] = list(uni.PARTNER_FILTER)
    return f


def us_hq(org: Mapping[str, Any]) -> bool:
    """Apollo puts the HQ in the US: its country, or, with no country, a US state.

    Search rows carry country and state (source_universe reads both): confirmed live 6 Oct 2026, when
    site_visits' rows put 11 companies outside the US by country and gave a state to 52 of 72.
    """
    country = " ".join(str(org.get("country") or "").split()).casefold()
    if country:
        return country in uni.US_COUNTRIES
    return bool(state_code(str(org.get("state") or "")))


def _roots(org: Mapping[str, Any]) -> set[str]:
    return {r for v in (org.get("primary_domain"), org.get("domain"), org.get("website_url"))
            if v and (r := root_domain(str(v)))}


def lead_facts(account_id: str, seed: Seed, now: datetime) -> list[dict]:
    """The two lookalike_lead facts of an account the run created. They name the seed's cell and country only."""
    where = "in the US" if seed.country == US_SEED else "outside the US"
    quote = (f"Found by Apollo's search by likeness as like an active Spill customer {where}, in {seed.group} at "
             f"{seed.band} staff (lookalike_leads, {now.astimezone(UK):%b %Y}).")
    value = {"provider": PROVIDER, "seed_group": seed.group, "seed_band": seed.band, "seed_country": seed.country}
    return [{"event_id": new_id(), "account_id": account_id, "source": SOURCE, "fact": f, "value": v,
             "quote": quote, "source_url": "", "observed_at": now} for f, v in ((FACT, value), (FLAG_FACT, True))]


def _search(ctx: Context, leads: _Leads, filters: Mapping[str, Any], note: Mapping[str, Any],
            seed_ids: Sequence[str] = ()) -> list[dict] | None:
    """One Apollo search page (a lookalike search when seed_ids are given), recorded in credit_ledger; None if
    Apollo refused it. The note carries counts, the cell and the seeds' country, never a domain."""
    apollo = ctx.clients.apollo
    what = {**note, "page": 1}
    with ledger.charge(ctx, credits.SYSTEM, JOB, 1.0, note=ledger.reserved_note(what)) as paid:
        try:
            if seed_ids:
                body = apollo.search_lookalike_organizations(seed_ids, filters, page=1, per_page=MAX_PER_PAGE)
            else:
                body = apollo.search_organizations(filters, page=1, per_page=MAX_PER_PAGE)
        except ApiError as exc:
            leads.spent += paid.fail(exc, note=ledger.failed_note(what, exc))  # kept unless refused (9 Oct 2026)
            if isinstance(exc, AuthError):
                raise  # the key is wrong: every search would fail
            leads.errors.append(f"Apollo {note.get('step')}: HTTP {exc.status}")  # never the body: it may echo a seed
            return None
        orgs = organizations_in(body)
        spent = paid.settle(1.0 if orgs else 0.0, note=json.dumps({**what, "results": len(orgs)}))
    leads.spent += spent
    leads.pages += 1
    return orgs


def _take(ctx: Context, leads: _Leads, orgs: Iterable[Mapping[str, Any]], band: str, seed: Seed) -> None:
    """Search rows into accounts and facts through source_universe's take(), each counted once for the run
    and for its seeds' country.

    A row without a US HQ is skipped here, before the door; take() then applies source_universe's checks
    (active state, size, label, partner) and the front door's.
    """
    rows: list[dict] = []
    events: list[dict] = []
    partners: dict[str, dict] = {}
    tally = leads.by_country[seed.country]
    for org in orgs:
        roots = _roots(org)
        key = uni.org_domain(org) or min(roots, default="")
        if key in leads.handled:
            continue  # another search of this run returned it first
        if key:
            leads.handled.add(key)
        reason = outcome = None
        if not key:
            reason = "no website"
        elif roots & leads.customers:
            outcome = CUSTOMER
        elif not us_hq(org):
            reason = NOT_US
        else:
            before = Counter(leads.universe.skipped)
            sl = uni.Slice(group=seed.group, state="", what=JOB, terms=(), label="", band=band)
            got = uni.take(ctx, org, sl, leads.universe, Counter(), rows, events, partners, source=ACCOUNT_SOURCE)
            why = next(iter(leads.universe.skipped - before), None)  # take()'s own reason, when it skipped the row
            if got is not None and got.outcome == "created":
                group = ctx.settings.industry_group_of(next((r for r in rows if r["account_id"] == got.account_id), {}))
                leads.created[group or seed.group] += 1
                tally["admitted"] += 1
                events.extend(lead_facts(str(got.account_id), seed, ctx.now))
                continue
            if got is not None and got.ok:
                outcome = EXISTS  # found again (a disqualified one too)
            elif why in (SUPPRESSED, PARTNER):
                outcome = why
            else:
                reason = why or (got.outcome if got is not None else "skipped")
        if outcome:
            leads.refused[outcome] += 1
            tally["refused"] += 1
        else:
            leads.skipped[str(reason)] += 1
            tally["not_US" if reason == NOT_US else "other_skips"] += 1
    uni._write(ctx, rows, events, partners, leads.universe)


def _room(ctx: Context, leads: _Leads) -> str | None:
    """Why no Apollo credit may be spent (below apollo_floor, or the month's budget spent), or None; sets the cap."""
    floor = credits.floor_reason(ctx)
    if floor:
        return floor
    month = budget.monthly(ctx.store, ctx.settings, credits.SYSTEM, ctx.now)
    leads.cap = max(0.0, min(float(APOLLO_CREDITS_PER_RUN), month.remaining))
    if leads.cap < 1.0:
        return "this month's Apollo credits are used (apollo_monthly_credits)"
    return None


def _may_search(leads: _Leads) -> bool:
    if leads.admitted >= MAX_NEW_PER_RUN:
        leads.stopped_by = f"the run's cap of {MAX_NEW_PER_RUN} new accounts"
    elif not leads.allows():
        leads.stopped_by = f"the run's Apollo credits are used ({leads.cap:g})"
    return not leads.stopped_by


def apollo_ids(ctx: Context, leads: _Leads, seeds: Sequence[Seed]) -> dict[str, str]:
    """seed domain -> its Apollo organization id, by a search on the domains (1 credit a page with results)."""
    out: dict[str, str] = {}
    domains = sorted({s.domain for s in seeds})
    for i in range(0, len(domains), DOMAIN_CHUNK):
        if not _may_search(leads):
            break
        chunk = domains[i : i + DOMAIN_CHUNK]
        orgs = _search(ctx, leads, {"q_organization_domains_list": chunk}, {"step": "seed ids", "asked": len(chunk)})
        wanted = set(chunk)
        for org in orgs or ():
            oid = apollo_org_id(org)
            for root in sorted(_roots(org) & wanted):
                if oid and root not in out:
                    out[root] = oid
    return out


def find(ctx: Context, leads: _Leads, seeds: Sequence[Seed]) -> None:
    """For each cell and seed country, Apollo's US companies most like the seeds, a search per size band."""
    ids = apollo_ids(ctx, leads, seeds)
    resolved = [s for s in seeds if s.domain in ids]
    leads.seeds_used = resolved
    for s in seeds:
        leads.by_country[s.country]["seeds" if s.domain in ids else "unknown_to_apollo"] += 1
    for group in searches(resolved):
        first = group[0]
        seed_ids = [ids[s.domain] for s in group]
        for band in cell_bands(ctx.settings, first.band):
            if not _may_search(leads):
                return
            note = {"step": "lookalikes", "cell": first.cell, "band": band, "seed_country": first.country,
                    "seeds": len(seed_ids)}
            _lookalike(ctx, leads, seed_ids, band, first, note)


def _lookalike(ctx: Context, leads: _Leads, seed_ids: Sequence[str], band: str, seed: Seed,
               note: Mapping[str, Any]) -> None:
    """One lookalike search; when it comes back empty with several seeds, each seed alone, same filters."""
    filters = us_filters(band, ctx.settings)
    tally = leads.by_country[seed.country]
    tries: list[tuple[list[str], Mapping[str, Any]]] = [(list(seed_ids), note)]
    while tries:
        ids, n = tries.pop(0)
        if not _may_search(leads):
            return
        orgs = _search(ctx, leads, filters, n, ids)
        if orgs is None:
            continue
        tally["searches"] += 1
        tally["results"] += len(orgs)
        if not orgs:
            tally["empty_searches"] += 1
            if len(ids) > 1:  # one seed with no lookalike data empties the whole search
                tries += [([one], {**n, "seeds": 1, "retry": "one seed"}) for one in ids]
            continue
        _take(ctx, leads, orgs, band, seed)


# -- the job ---------------------------------------------------------------------------------------------


def done_this_month(ctx: Context) -> datetime | None:
    """When a run of this job last finished ok this month (UK), or None. A skipped run does not count."""
    start, end = budget.month_bounds(ctx.now)
    when = None
    for r in ctx.store.select("heartbeats", {"job": JOB, "status": "ok"}):
        detail = r.get("detail")
        t = utc(r.get("started_at"))
        if (r.get("run_id") != ctx.run_id and t is not None and start <= t < end and isinstance(detail, Mapping)
                and detail.get("status") == "ok"):
            when = max(when, t) if when else t
    return when


def _counts(c: Mapping[str, int]) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])) if n) or "none"


def _n(x: Any) -> str:
    v = float(x or 0)
    return f"{v:,.0f}" if v.is_integer() else f"{v:,.1f}"


def report(summary: Mapping[str, Any]) -> list[str]:
    """What the run did, in plain words: the summary's report, and the daily post's lines."""
    if summary.get("skipped"):
        return [f"Lookalike leads: nothing done this month: {summary.get('reason')}."]
    seeds = summary.get("seeds") or {}
    cells = "; ".join(f"{c.replace('|', ' ')}: {n}" for c, n in (seeds.get("by_cell") or {}).items()) or "none"
    country = seeds.get("by_country") or {}
    refused = summary.get("refused") or {}
    skipped = summary.get("skipped_companies") or {}
    lines = [
        f"Lookalike leads ({summary.get('month')}): Apollo's US companies like {seeds.get('used', 0)} of Spill's "
        f"active customers ({country.get(US_SEED, 0)} in the US, {country.get(OTHER_SEED, 0)} elsewhere), from "
        f"{seeds.get('eligible', 0)} that qualify of {seeds.get('customers', 0)} read.",
        f"  Seeds by cell: {cells}.",
        f"  Found {summary.get('found', 0)} companies; {summary.get('admitted', 0)} new accounts; refused "
        f"{sum(refused.values())} ({_counts(refused)}); not admitted {sum(skipped.values())} ({_counts(skipped)}).",
    ]
    for kind, y in (summary.get("by_seed_country") or {}).items():
        if y.get("seeds") or y.get("unknown_to_apollo"):
            lines.append(
                f"  {kind} seeds: {y.get('seeds', 0)} used ({y.get('unknown_to_apollo', 0)} unknown to Apollo), "
                f"{y.get('searches', 0)} searches ({y.get('empty_searches', 0)} empty), {y.get('results', 0)} results, "
                f"{y.get('admitted', 0)} new accounts, {y.get('refused', 0)} refused, {y.get('not_US', 0)} not US.")
    lines.append(f"  Credits: {_n(summary.get('credits'))} Apollo ({summary.get('apollo_searches', 0)} searches).")
    if summary.get("stopped_by"):
        lines.append(f"  Stopped: {summary['stopped_by']}.")
    if summary.get("errors"):
        lines.append(f"  Errors: {'; '.join(summary['errors'][:3])}.")
    return lines


def _done(ctx: Context, summary: dict) -> dict:
    summary["report"] = report(summary)
    log("lookalike_leads_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "report"})
    return summary


def run(ctx: Context) -> dict:
    """The lookalike_leads job (JOB CONTRACT: run(ctx) -> summary)."""
    s = ctx.settings
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "month": ctx.now.astimezone(UK).strftime("%b %Y")}
    states = uni.allowed_states(s)
    if not states:
        summary.update(skipped=True, reason="no active state outside CA and WA")
        return _done(ctx, summary)
    when = done_this_month(ctx)
    if when is not None:
        summary.update(skipped=True, reason=f"it ran on {when.astimezone(UK):%d %b} already; the customers are much "
                                            "the same, so once a month is enough")
        return _done(ctx, summary)

    leads = _Leads(customers=frozenset(), universe=uni._Run(states=frozenset(states)))
    why = _room(ctx, leads)  # before HubSpot is read: below apollo_floor nothing else happens
    if why:
        summary.update(skipped=True, reason=why)
        return _done(ctx, summary)

    customers = lookalikes.read_customers(ctx)  # read only; company fields only; never stored
    if not customers:
        summary.update(skipped=True, reason="HubSpot returned no Spill customers")
        return _done(ctx, summary)
    pool = eligible(customers, s)
    seeds = select_seeds(customers, s, ctx.now)
    summary["seeds"] = {"customers": len(customers), "eligible": len(pool)}
    if not seeds:
        summary.update(skipped=True, reason="no active customer at 10 to 249 staff in an active industry group")
        return _done(ctx, summary)
    leads.customers = frozenset(c.domain for c in customers if c.domain)
    find(ctx, leads, seeds)

    used = leads.seeds_used
    summary["seeds"].update(used=len(used), by_cell=dict(Counter(x.cell for x in used)),
                            by_country={c: sum(x.country == c for x in used) for c in SEED_COUNTRIES},
                            unknown_to_apollo=len(seeds) - len(used))
    summary.update(
        status="ok", provider=PROVIDER, found=len(leads.handled), admitted=leads.admitted,
        admitted_by_group=dict(leads.created), refused={k: leads.refused[k] for k in REFUSALS if leads.refused[k]},
        skipped_companies=dict(leads.skipped), by_seed_country={c: dict(y) for c, y in leads.by_country.items()},
        credits=leads.spent, apollo_searches=leads.pages, stopped_by=leads.stopped_by, errors=leads.errors[:20],
    )
    return _done(ctx, summary)


# -- for the daily post ------------------------------------------------------------------------------------


def latest(store: Any) -> dict | None:
    """The newest heartbeat of this job that finished (ok or skipped)."""
    rows = [r for r in store.select("heartbeats", {"job": JOB}) if r.get("status") in ("ok", "skipped")]
    return max(rows, key=lambda r: utc(r.get("started_at")) or datetime.min.replace(tzinfo=UTC), default=None)


def post_lines(ctx: Context) -> list[str]:
    """The daily post's lines on the last run, for POST_HOURS after it (a monthly job: nothing otherwise)."""
    row = latest(ctx.store)
    started = utc(row.get("started_at")) if row else None
    if not row or started is None or ctx.now - started > timedelta(hours=POST_HOURS):
        return []
    detail = row.get("detail") if isinstance(row.get("detail"), Mapping) else {}
    lines = list(detail.get("report") or []) or report(detail)
    return [f"{lines[0]} (ran {started.astimezone(UK):%a %d %b %H:%M} UK)", *lines[1:]]
