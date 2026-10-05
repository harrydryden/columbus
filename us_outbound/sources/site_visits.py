"""Source "site_visits": the companies that visit Spill's US site, from Apollo's website visitors (SPEC 7, 9).

Harry, 5 Oct 2026: "Switch on website visits. It's the strongest intent signal." The Signals tab's
"Visited the US site" (us_visits_30d >= 1) and "Viewed US pricing or demo page"
(pricing_or_demo_visits_30d >= 1) read these facts. Daily at 06:00 UK, after pick_contacts and before
enrol at 12:00, whose tiers the rescore at the end of the run sets.

  1. Apollo's visitor list, read only: an organization search with its website-visitor filters
     (clients/apollo.search_website_visitors) for the General site_visit_domain (spill.chat), three ways:
       * us_today: the site_visit_us_paths (/us), the last 1 day: who visited since the last run;
       * us: the same pages, the last 30 days: the us_visits_30d facts, and new companies;
       * intent: the site_visit_intent_paths (/us/pricing, /us/demo, /us/book), the last 30 days:
         the pricing_or_demo_visits_30d facts.
     Apollo matches a page whose path contains a listed path. The tracker's own settings are never
     read or changed: Apollo's tracker endpoint creates a tracker when there is none (a write).
     Every row as received goes to raw_site_visits, once per company a run (SPEC 6, raw loads).
  2. Each visiting company is matched to an account by its root domain through the alias table
     (clean/domains.canonical_domain), else by its Apollo organization id. A matched account gets
     us_visits_30d = 1 while Apollo lists it in the 30-day window, and 0 once a search read to its
     end no longer does, so the signal stops. A fact is written only when it changes, or again
     before the signals' counts_for_days would let it go stale while the company keeps visiting.
     The value is 1 for "visited in the window", not a count: a count needs a domain aggregates
     call per company, whose cost Apollo does not state (PHASE0-CONFIRM before using it).
  3. Each account in the one-day list gets one events row of type site_visit per UK day,
     event_id site-visit:{account_id}:{day}, dated at the start of Apollo's one-day window (24 hours
     before the run), so a rerun adds nothing and a visit is never dated after an email it may have
     come before. The daily post's "Warm accounts" line (enrolled accounts) and v_readout_weekly's
     site visits before and after the first email read these. Every matched account gets them,
     whatever its status, so the readout can count visits before the first email.
     PHASE0-CONFIRM: what Apollo's "last 1 day" covers. If it is yesterday and today rather than
     24 hours, one visit can give two events a day apart.
  4. New high-intent leads (SPEC 7: "An unknown visiting company ... joins the queue if it
     passes"). Visitors with no account are screened by one organization search filtered to their
     Apollo ids, the United States and 10 to 249 employees (and not insurers or brokers, as the
     universe search): Apollo's own filters decide country and size, since search rows carry no
     employee count. A company the screen keeps comes in by the front door (accounts.admit,
     source site_visit; suppressed, partner and personal domains refused) when its HQ is in an
     active state (never CA or WA) and an active Industries label fits, with the account columns
     and apollo_org facts the universe gives (sources/apollo_universe.py). It then goes through
     verify, scoring and pick_contacts like any other; source_universe's size-band backfill gives
     it its band before verify_accounts (weekdays). A company the screen leaves out or the checks
     refuse is not asked about again for SCREEN_AGAIN_DAYS (credit_ledger notes).
  5. The rescore (SPEC 9: score runs after site_visits), when the run wrote a fact or an account.

No data. When Apollo refuses the visitor filters (a plan or permission error) or ignores them (a
total beyond any real visitor list, MAX_PLAUSIBLE: its whole database), or the one-day list has
been empty on NO_DATA_RUNS runs in a row, the run still ends ok, and its summary and the daily
post's Sources section say plainly to check the tracker (TRACKER_MESSAGE). Nothing from a refused
or ignored search is used, and the run makes no further search.

Credits. Each search page that returns a company costs 1 Apollo credit and an empty one 0, so a
normal day costs 3, and 4 when there are new visitors to screen: at most MAX_CREDITS_PER_RUN a run,
about 90 to 120 a month (under 6% of apollo_monthly_credits). This is not paced like the sources'
shares: it runs after pick_contacts has taken the day's allowance, and a visit not read today is
lost from the one-day list. Every request goes into credit_ledger (job site_visits), so it counts
in the month's budget; nothing is spent while Apollo's balance is below apollo_floor or the month's
Apollo budget is used.

Dry-run: the reads happen and their credits are recorded. Accounts, facts, events, raw rows and the
ledger are database writes, which dry-run makes too (SPEC 0.3), as in source_universe. Nothing is
written outside the database in any mode, so --live changes nothing for this job.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from us_outbound import accounts, budget
from us_outbound.clean.domains import canonical_domain, is_personal_domain
from us_outbound.clean.people import state_code
from us_outbound.clients.apollo import MAX_PER_PAGE, organizations_in, total_entries
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, Context
from us_outbound.logs import log
from us_outbound.scoring import score, tiers
from us_outbound.settings.model import SIZE_BANDS, Settings
from us_outbound.sources import apollo_credits as credits
from us_outbound.sources import apollo_universe as universe

JOB = "site_visits"
SOURCE = "site_visits"  # signal_events.source (SPEC 7)
ACCOUNT_SOURCE = "site_visit"  # accounts.source of a company that came in by visiting (SPEC 6)
EVENT_TYPE = "site_visit"
US_FACT, INTENT_FACT = "us_visits_30d", "pricing_or_demo_visits_30d"
US, US_TODAY, INTENT = "us", "us_today", "intent"
WINDOW_DAYS = 30  # the facts' window: us_visits_30d, pricing_or_demo_visits_30d
MAX_PAGES_PER_SEARCH = 3  # 300 visiting companies; a longer list is read in part and clears nothing
MAX_CREDITS_PER_RUN = 5  # three searches and a screen, with a page to spare
MAX_PLAUSIBLE = 5_000  # companies in 30 days: beyond any real visitor list, so the filters were ignored
SCREEN_AGAIN_DAYS = 30  # a visitor screened out is not asked about again for this long
REFRESH_DAYS = 7  # a positive fact is written again after this, before it goes stale
NO_DATA_RUNS = 7  # runs in a row with an empty one-day list before the post says to check the tracker
REFUSED_STATUSES = frozenset({400, 402, 403, 404, 422})  # Apollo refused the visitor filters (PHASE0-CONFIRM)
US_LOCATION = "United States"  # PHASE0-CONFIRM: the organization_locations value for the whole country
EXAMPLES = 20
QUOTE_LIMIT = 300  # SPEC 6
TRACKER_MESSAGE = ("No website-visitor data from Apollo for {domain}: check that Apollo's website tracker is "
                   "installed on {domain} (Apollo → Settings → Website Visitors) and that the plan includes "
                   "website visitors.")


@dataclass(frozen=True)
class Search:
    key: str  # US_TODAY, US or INTENT
    days: int
    paths: tuple[str, ...]
    fact: str  # the fact it gives, "" for the one-day list


def plan(settings: Settings) -> list[Search]:
    """The searches in the order they are read: the one-day list first, as a visit not read today is lost."""
    g = settings.general
    out = [Search(US_TODAY, 1, g.site_visit_us_paths, ""), Search(US, WINDOW_DAYS, g.site_visit_us_paths, US_FACT)]
    if g.site_visit_intent_paths:
        out.append(Search(INTENT, WINDOW_DAYS, g.site_visit_intent_paths, INTENT_FACT))
    return out


def tracked_domain(settings: Settings) -> str:
    return settings.general.site_visit_domain.strip().lower()


# -- small helpers -----------------------------------------------------------------------------------


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _note(row: Mapping[str, Any]) -> dict:
    try:
        note = json.loads(row.get("note") or "")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def _quote(text: str) -> str:
    q = " ".join(text.split())
    return q if len(q) <= QUOTE_LIMIT else q[: QUOTE_LIMIT - 1] + "…"


def row_key(org: Mapping[str, Any]) -> str:
    """A visiting company's Apollo organization id, else its domain: raw_site_visits.key."""
    return str(org.get("organization_id") or "") or (universe.org_domain(org) or "")


# -- reading Apollo -----------------------------------------------------------------------------------


@dataclass
class Read:
    search: Search
    rows: list[dict] = field(default_factory=list)
    pages: int = 0
    total: int | None = None
    complete: bool = False  # read to its last page: only then does a company it leaves out lose its fact
    stopped: str = ""  # why it was not read to its end (the run's credit cap, an error); what was read is used
    problem: str = ""  # why none of it is used: Apollo refused the filters, or a total no visitor list reaches

    @property
    def usable(self) -> bool:
        return not self.problem

    def describe(self) -> dict:
        return {"days": self.search.days, "paths": list(self.search.paths), "companies": len(self.rows),
                "pages": self.pages, "total": self.total, "complete": self.complete, "stopped": self.stopped,
                "problem": self.problem}


@dataclass
class _Run:
    cap: float
    credits: float = 0.0
    refused: str = ""
    errors: list[str] = field(default_factory=list)
    not_admitted: Counter[str] = field(default_factory=Counter)
    examples: list[dict] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    screened: int = 0

    def allows(self) -> bool:
        return self.credits + 1 <= self.cap

    def refuse(self, org: Mapping[str, Any], why: str) -> None:
        self.not_admitted[why] += 1
        if len(self.examples) < EXAMPLES:
            self.examples.append({"domain": universe.org_domain(org) or row_key(org), "reason": why})


def read_search(ctx: Context, search: Search, run: _Run) -> Read:
    """Every page of one visitor search, within the run's credits; each page into credit_ledger."""
    out = Read(search)
    domain = tracked_domain(ctx.settings)
    for page in range(1, MAX_PAGES_PER_SEARCH + 1):
        if not run.allows():
            out.stopped = f"the run's cap of {run.cap:g} credits"
            return out
        try:
            body = ctx.clients.apollo.search_website_visitors(
                [domain], days=search.days, pages=search.paths, page=page, per_page=MAX_PER_PAGE)
        except ApiError as exc:
            if exc.status == 401:
                raise  # the key is wrong: every Apollo job fails the same way
            if exc.status in REFUSED_STATUSES and page == 1:
                run.refused = f"Apollo refused the website-visitor search (HTTP {exc.status}): {str(exc.body)[:200]}"
                out.problem = "Apollo refused the visitor filters"
            else:
                run.errors.append(f"{search.key} page {page}: {str(exc)[:200]}")
                out.stopped = f"an error on page {page}"
            return out
        orgs = organizations_in(body)
        total = total_entries(body)
        spent = 1.0 if orgs else 0.0
        credits.record(ctx, JOB, spent, note=json.dumps(
            {"search": search.key, "days": search.days, "page": page, "results": len(orgs), "total": total}))
        run.credits += spent
        out.pages, out.total = page, total
        if total is not None and total > MAX_PLAUSIBLE:
            out.problem = (f"implausible: Apollo says {total:,} companies visited, so the visitor filters were "
                           "probably ignored; nothing from this search is used")
            run.refused = f"Apollo answered the website-visitor search with {total:,} companies: the filters were ignored"
            out.rows = []
            return out
        out.rows.extend(orgs)
        if len(orgs) < MAX_PER_PAGE or (total is not None and page * MAX_PER_PAGE >= total):
            out.complete = True
            return out
    return out


def quiet_runs(ctx: Context) -> int:
    """Runs in a row, newest first, whose one-day list was empty (this job's credit_ledger notes)."""
    runs: dict[str, tuple[datetime, int]] = {}
    for r in ctx.store.select("credit_ledger", {"system": credits.SYSTEM, "job": JOB}):
        note, t = _note(r), _ts(r.get("occurred_at"))
        if t is None or note.get("search") != US_TODAY or note.get("page") != 1:
            continue
        rid = str(r.get("run_id") or r.get("entry_id"))
        runs[rid] = (t, int(note.get("results") or 0))
    n = 0
    for _, results in sorted(runs.values(), key=lambda x: x[0], reverse=True):
        if results:
            break
        n += 1
    return n


# -- matching visitors to accounts --------------------------------------------------------------------


def match(ctx: Context, rows: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """row_key -> the account: by root domain through the alias table, else by Apollo organization id."""
    rows = list(rows)
    ids = sorted({str(r.get("organization_id")) for r in rows if r.get("organization_id")})
    by_org: dict[str, dict] = {}
    for i in range(0, len(ids), 1000):
        for a in ctx.store.select("accounts", {"apollo_org_id": ids[i : i + 1000]}):
            by_org.setdefault(str(a["apollo_org_id"]), a)
    out: dict[str, dict] = {}
    for org in rows:
        key = row_key(org)
        if not key or key in out:
            continue
        domain = universe.org_domain(org)
        root = canonical_domain(ctx.store, domain) if domain else None
        account = ctx.store.get("accounts", domain=root) if root else None
        account = account or by_org.get(str(org.get("organization_id") or ""))
        if account:
            out[key] = account
    return out


# -- new visitors: the screen and the front door ------------------------------------------------------


def screened_recently(ctx: Context) -> set[str]:
    """Apollo ids this job asked the screen about in the last SCREEN_AGAIN_DAYS."""
    since = ctx.now - timedelta(days=SCREEN_AGAIN_DAYS)
    out: set[str] = set()
    for r in ctx.store.select("credit_ledger", {"system": credits.SYSTEM, "job": JOB}):
        t = _ts(r.get("occurred_at"))
        note = _note(r)
        if t is not None and t >= since and isinstance(note.get("asked"), list):
            out |= {str(i) for i in note["asked"]}
    return out


def screen(ctx: Context, ids: Sequence[str], run: _Run) -> list[dict] | None:
    """The companies among ids that are in the US with 10 to 249 employees (Apollo's filters); None if refused.

    PHASE0-CONFIRM: organization_ids with organization_locations ["United States"] and the size ranges on
    mixed_companies/search (source_universe's size-band backfill pairs organization_ids with a size filter).
    """
    filters: dict[str, Any] = {
        "organization_ids": list(ids),
        "organization_locations": [US_LOCATION],
        "organization_num_employees_ranges": [universe.EMPLOYEE_RANGES[b] for b in SIZE_BANDS],
    }
    if universe.PARTNER_FILTER:
        filters["not_organization_naics_codes"] = list(universe.PARTNER_FILTER)
    try:
        body = ctx.clients.apollo.search_organizations(filters, page=1, per_page=MAX_PER_PAGE)
    except ApiError as exc:
        if exc.status == 401:
            raise
        run.errors.append(f"screen: {str(exc)[:200]}")
        return None
    asked = set(ids)
    rows = organizations_in(body)
    kept = [r for r in rows if str(r.get("organization_id") or "") in asked]  # never a company we did not ask about
    spent = 1.0 if rows else 0.0
    credits.record(ctx, JOB, spent, note=json.dumps(
        {"screen": len(ids), "asked": list(ids), "kept": [str(r["organization_id"]) for r in kept]}))
    run.credits += spent
    run.screened += len(ids)
    return kept


def _keyword_text(org: Mapping[str, Any]) -> str:
    return " ; ".join([*universe.org_keywords(org), str(org.get("industry") or "")]).strip(" ;")


def admit(ctx: Context, org: Mapping[str, Any], run: _Run, states: frozenset[str]) -> dict | None:
    """A screened visitor into accounts by the front door, with the universe's columns and facts; None if refused."""
    s, store, now = ctx.settings, ctx.store, ctx.now
    domain = universe.org_domain(org)
    if not domain:
        run.refuse(org, "no website")
        return None
    if is_personal_domain(domain):
        run.refuse(org, "a personal email domain")
        return None
    state = state_code(str(org.get("state") or ""))
    if not universe.in_us(org):
        run.refuse(org, "outside the US")
        return None
    if state not in states:
        run.refuse(org, "HQ outside the active states" if state else "HQ state unknown")
        return None
    codes, text = universe.org_naics(org), _keyword_text(org)
    label = universe.best_label(codes, text, s) if codes or text else None
    if label is None:
        run.refuse(org, "no Industries label fits")
        return None
    if not label.active:
        run.refuse(org, "its best Industries label is switched off")
        return None
    if tiers.partner_match({"naics": codes, "industry": label.industry, "keywords": universe.org_keywords(org),
                            "apollo_industry": org.get("industry")}, {}):
        run.refuse(org, "a partner, never prospected")
        return None
    got = accounts.admit(store, domain, source=ACCOUNT_SOURCE, now=now, name=str(org.get("name") or ""))
    if not got.ok:
        run.refuse(org, got.outcome)
        return None
    if got.outcome != "created":
        return store.get("accounts", account_id=got.account_id)  # found after all, through an alias
    cols = universe.with_overrides(universe.columns(org, label, state, ""), got.domain or domain, s)
    row = {"account_id": got.account_id, **cols}
    store.upsert("accounts", [row])  # partial row: admit wrote the rest
    store.insert("signal_events", universe.org_facts(got.account_id, org, state, now))
    run.created.append(got.domain or domain)
    return {**row, "domain": got.domain or domain}


def admit_new(ctx: Context, reads: Sequence[Read], matched: dict[str, dict], run: _Run) -> None:
    """Screen the visitors with no account (one search, if the run's credits allow) and admit the ones that pass."""
    usable = {r.search.key: r for r in reads if r.usable}
    if US not in usable:
        return  # the 30-day list is not usable (refused, or the filters ignored): no company comes in on it
    asked = screened_recently(ctx)
    todo: dict[str, dict] = {}
    for r in usable.values():
        for org in r.rows:
            oid = str(org.get("organization_id") or "")
            if row_key(org) not in matched and oid and oid not in asked:
                todo.setdefault(oid, org)
    if not todo or not run.allows():
        return
    ids = list(todo)[:MAX_PER_PAGE]
    kept = screen(ctx, ids, run)
    if kept is None:
        return
    kept_ids = {str(o["organization_id"]) for o in kept}
    for oid in ids:
        if oid not in kept_ids:
            run.refuse(todo[oid], "screened out: not a US company of 10 to 249 employees, or an insurer or broker")
    states = frozenset(universe.allowed_states(ctx.settings))
    for org in kept:
        account = admit(ctx, org, run, states)
        if account is not None:
            for key in {row_key(org), row_key(todo[str(org["organization_id"])])}:
                matched[key] = account


# -- facts and events ---------------------------------------------------------------------------------


def refresh_days(settings: Settings) -> int:
    """Write a positive fact again after this many days: before the shortest active visit signal lets it go stale."""
    windows = [sig.counts_for_days for sig in settings.active_signals() if SOURCE in sig.sources]
    return max(1, min([REFRESH_DAYS, *(w - 1 for w in windows)]))


def latest_facts(ctx: Context) -> dict[tuple[str, str], tuple[datetime, Any]]:
    """(account_id, fact) -> (observed_at, value) of the newest site_visits fact."""
    out: dict[tuple[str, str], tuple[datetime, Any]] = {}
    for e in ctx.store.select("signal_events", {"source": SOURCE, "fact": [US_FACT, INTENT_FACT]}):
        t = _ts(e.get("observed_at"))
        key = (str(e.get("account_id")), str(e.get("fact")))
        if t is not None and (key not in out or t >= out[key][0]):
            out[key] = (t, e.get("value"))
    return out


def _positive(v: Any) -> bool:
    n = tiers.as_number(v)
    return n is not None and n > 0


def _fact(ctx: Context, account_id: str, search: Search, value: int, org: Mapping[str, Any] | None) -> dict:
    domain = tracked_domain(ctx.settings)
    pages = ", ".join(search.paths)
    what = "pricing or demo pages" if search.key == INTENT else "the US site"
    if value:
        quote = f"Apollo: visited {what} on {domain} (pages containing {pages}) in the last {search.days} days"
    else:
        quote = f"Apollo: no visit to {what} on {domain} (pages containing {pages}) in the last {search.days} days"
    oid = str((org or {}).get("organization_id") or "")
    return {"event_id": new_id(), "account_id": account_id, "source": SOURCE, "fact": search.fact, "value": value,
            "quote": _quote(quote), "source_url": universe.APOLLO_ORG_URL.format(oid) if oid else "",
            "observed_at": ctx.now}


def visit_facts(ctx: Context, read: Read, matched: Mapping[str, dict],
                latest: Mapping[tuple[str, str], tuple[datetime, Any]], refresh: int) -> tuple[list[dict], int]:
    """(the facts to write, how many of them are 0s): 1 for each visiting account whose fact changed or is
    due a refresh, 0 for an account that had 1 and is no longer listed (only after a search read to its end)."""
    fact, today = read.search.fact, ctx.today_uk()
    visiting: dict[str, Mapping[str, Any]] = {}
    for org in read.rows:
        account = matched.get(row_key(org))
        if account:
            visiting.setdefault(str(account["account_id"]), org)
    rows: list[dict] = []
    for aid, org in visiting.items():
        prev = latest.get((aid, fact))
        stale = prev is not None and (today - prev[0].astimezone(UK).date()).days >= refresh
        if prev is None or not _positive(prev[1]) or stale:
            rows.append(_fact(ctx, aid, read.search, 1, org))
    cleared = 0
    if read.complete:
        for (aid, f), (_, value) in latest.items():
            if f == fact and _positive(value) and aid not in visiting:
                rows.append(_fact(ctx, aid, read.search, 0, None))
                cleared += 1
    return rows, cleared


def visit_events(ctx: Context, account_ids: Iterable[str]) -> int:
    """One site_visit event per account for the day the one-day window started; a rerun adds none."""
    when = ctx.now - timedelta(days=1)
    day = when.astimezone(UK).date().isoformat()
    wanted = {f"site-visit:{aid}:{day}": aid for aid in sorted(set(account_ids))}
    if not wanted:
        return 0
    have = {str(e["event_id"]) for e in ctx.store.select("events", {"event_id": list(wanted)})}
    rows = [{"event_id": eid, "account_id": aid, "contact_id": None, "type": EVENT_TYPE, "occurred_at": when}
            for eid, aid in wanted.items() if eid not in have]
    if rows:
        ctx.store.insert("events", rows)
    return len(rows)


def store_raw(ctx: Context, reads: Sequence[Read]) -> int:
    """Each visiting company once a run, as Apollo sent it, with the searches that listed it (raw_site_visits)."""
    seen: dict[str, dict] = {}
    for r in reads:
        if not r.usable:
            continue
        for org in r.rows:
            key = row_key(org)
            if not key:
                continue
            entry = seen.setdefault(key, {"searches": [], "record": org})
            if r.search.key not in entry["searches"]:
                entry["searches"].append(r.search.key)
    rows = [{"loaded_at": ctx.now, "run_id": ctx.run_id, "key": k, "payload": v} for k, v in seen.items()]
    if rows:
        ctx.store.insert("raw_site_visits", rows)
    return len(rows)


# -- the daily post -----------------------------------------------------------------------------------


def last_summary(store: Any) -> dict | None:
    """The newest site_visits run's summary (heartbeats.detail), ok or skipped."""
    best: tuple[datetime, dict] | None = None
    for r in store.select("heartbeats", {"job": JOB, "status": ["ok", "skipped"]}):
        t, d = _ts(r.get("started_at")), r.get("detail")
        if t is not None and isinstance(d, Mapping) and (best is None or t > best[0]):
            best = (t, dict(d))
    return best[1] if best else None


def post_line(ctx: Context) -> str:
    """The daily post's one line on website visits (learn/daily_post.py, Sources)."""
    domain = tracked_domain(ctx.settings) or "the tracked site"
    last = last_summary(ctx.store)
    if last is None:
        return f"Site visits ({domain}): not read yet (site_visits, daily 06:00)."
    if last.get("tracker_check"):
        return str(last["tracker_check"])
    if last.get("skipped"):
        return f"Site visits ({domain}): the last run read nothing: {last.get('reason')}."
    v, m = last.get("visitors") or {}, last.get("matched") or {}
    created = len(last.get("created") or ())
    return (f"Site visits ({domain}, Apollo): {v.get(US, 0)} companies on the US pages in the last 30 days "
            f"({m.get(US, 0)} of them ours, {v.get(INTENT, 0)} on pricing or demo pages); {v.get(US_TODAY, 0)} in "
            f"the last day; {created} new {'account' if created == 1 else 'accounts'}.")


# -- the run ------------------------------------------------------------------------------------------


def _done(ctx: Context, summary: dict) -> dict:
    log("site_visits_done", run_id=ctx.run_id, **summary)
    return summary


def run(ctx: Context) -> dict:
    """The site_visits job (JOB CONTRACT: run(ctx) -> summary)."""
    s = ctx.settings
    domain = tracked_domain(s)
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "domain": domain}
    if not domain or not s.general.site_visit_us_paths:
        why = "site_visit_domain is blank" if not domain else "site_visit_us_paths is blank"
        summary.update(skipped=True, reason=f"{why}, so no website visit is read")
        return _done(ctx, summary)
    floor = credits.floor_reason(ctx)
    if floor:
        summary.update(skipped=True, reason=floor)
        return _done(ctx, summary)
    month = budget.monthly(ctx.store, s, credits.SYSTEM, ctx.now)
    if month.remaining < 1:
        summary.update(skipped=True, reason="this month's Apollo credits (apollo_monthly_credits) are used")
        return _done(ctx, summary)

    run_ = _Run(cap=min(float(MAX_CREDITS_PER_RUN), float(int(month.remaining))))
    reads: list[Read] = []
    for search in plan(s):
        if run_.refused:  # the same filters again would be refused (or ignored) the same way
            reads.append(Read(search, problem="not read: Apollo refused or ignored the visitor filters"))
            continue
        reads.append(read_search(ctx, search, run_))
    by_key = {r.search.key: r for r in reads}
    raw = store_raw(ctx, reads)
    usable_rows = [org for r in reads if r.usable for org in r.rows]
    matched = match(ctx, usable_rows)
    admit_new(ctx, reads, matched, run_)

    latest, refresh = latest_facts(ctx), refresh_days(s)
    facts: list[dict] = []
    cleared = 0
    for r in reads:
        if r.search.fact and r.usable:
            rows, zeros = visit_facts(ctx, r, matched, latest, refresh)
            facts += rows
            cleared += zeros
    if facts:
        ctx.store.insert("signal_events", facts)
    today = by_key.get(US_TODAY)
    today_ids = {str(matched[row_key(o)]["account_id"]) for o in today.rows if row_key(o) in matched} \
        if today is not None and today.usable else set()
    events = visit_events(ctx, today_ids)

    quiet = quiet_runs(ctx)
    tracker = TRACKER_MESSAGE.format(domain=domain) if run_.refused or quiet >= NO_DATA_RUNS else ""
    if tracker:
        log("site_visits_no_data", run_id=ctx.run_id, refused=run_.refused, quiet_runs=quiet)
    changed = bool(facts or run_.created)
    summary.update(
        status="ok",
        searches={r.search.key: r.describe() for r in reads},
        visitors={r.search.key: len({row_key(o) for o in r.rows}) for r in reads if r.usable},
        matched={r.search.key: len({row_key(o) for o in r.rows if row_key(o) in matched}) for r in reads if r.usable},
        facts_written=len(facts) - cleared, facts_cleared=cleared, events=events, raw_rows=raw,
        screened=run_.screened, created=run_.created, not_admitted=dict(run_.not_admitted),
        not_admitted_examples=run_.examples, credits=run_.credits, credit_cap=run_.cap, quiet_runs=quiet,
        tracker_check=tracker, refused=run_.refused, errors=run_.errors[:20],
        rescore=score.rescore(ctx) if changed else "nothing changed",
    )
    return _done(ctx, summary)
