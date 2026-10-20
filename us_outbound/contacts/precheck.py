"""The contact_precheck job: who Apollo has to contact at each verified company, before any credit (W3-3, 20 Oct
2026; code-contacts-enrol §7; loop L5), with the People-leader facts apollo_people wrote until then.

Why. Until W3-3 pick_contacts learnt that a company had nobody to email only by trying it: 54% of the accounts tried
yielded no contact, and contactable supply counted them as findable (code-sources B1). And apollo_people (Harry, 5 Oct
2026: "Get People data for every company, not only those already being contacted") searched every queue account at
04:20 for the People signals. The two are one free search now: weekdays at 05:15 UK, after apollo_enrich (05:00) and
before pick_contacts (05:30), whose rescore scores the facts, for the verified accounts enrol could take that have no
sendable contact (pick.waiting, in queue order) and no precheck in the last REFRESH_DAYS:
  1. Candidates: People API Search at the organization (its Apollo id, else its domain; pick.search_filters) for every
     title of the Roles-tab rows contacted at its size, in the US, with an email Apollo has verified: the search
     pick_contacts makes. The people pick_contacts could contact (pick.rank_candidates: a Roles-tab title at the size,
     not located outside the US or in CA or WA) are counted: a contactable_candidates fact (source contact_precheck,
     not a SPEC 7 source, so no signal scores it). 0 sets accounts.status_reason = no_contact_candidates: the account
     leaves contactable supply's findable count (apollo_universe.contactable_supply) and pick_contacts' queue until the
     next precheck, REFRESH_DAYS later; one with candidates again has the reason cleared. pick_contacts tries the
     accounts with the most candidates first, after its random arm is drawn (contacts/pick.py).
  2. People leaders (as apollo_people did; Harry, 5 Oct 2026), for an account whose size the Roles tab contacts a
     People leader at: the same organization for the People-leader titles with no email filter. A People leader is who
     pick_contacts would contact as one (pick.people_leaders: rank_person, never a junior title, location_block), so the
     signals mean the same whichever job wrote them.
  3. Coverage: the same organization with no other filter and one row a page, for the number of people Apollo holds
     there (total_entries). Coverage is that over the account's employees: an Overrides row's, else the employees
     column, else the latest apollo_org employees fact, else the top of its size band, which can only understate it.
  4. The newest leader's days in title, only when a leader was found. Search rows carry no employment history (Apollo's
     MCP tool, 5 Oct 2026: an id, the title and has_* flags), so it comes from the search's
     person_days_in_current_title_range: the leaders search again for at most HORIZON_DAYS in title and, when someone
     is found, with the window halved until it is PRECISION_DAYS wide. The window's upper end is written, so "New
     People leader" ends a few days early rather than late. Nobody within HORIZON_DAYS (or nobody whose start Apollo
     knows) writes HORIZON_DAYS + 1, which any window shorter than that reads as "not new". A row that does carry
     employment_history is read directly (pick.days_in_role), with no further search.
The People facts keep apollo_people's source and shapes (pick.leader_facts), so the Signals tab reads them as before:
  people_leader_count          the leaders found. 0 only when coverage is at least COVERAGE_MIN, so that 0 likely
                               means there is no People leader ("First People hire (likely)"); with lower or unknown
                               coverage and nobody found, no count is written.
  people_leader_days_in_title  as above; scoring ages it to today. With a count of 0 it is HORIZON_DAYS + 1, so a
                               leader found before who has since gone no longer counts as new.
  people_leader_newest         that leader's Apollo person id and title, for the opener (enrol/openers.py).
  people_search_coverage       people found over employees, two decimals.
  people_found                 the people Apollo holds at the organization.
  people_search                the run marker, one per account searched: what was found and from how many requests.
                               pick_contacts writes its own People-leader facts only when there is no marker that recent.
A fact is written only when its value changed or the stored one is REFRESH_DAYS old, so the signals' counts_for_days
keep seeing it; a search that fails writes nothing, and its account is tried again next run. The People signals are
read for the accounts about to be contacted now, not every queue account: apollo_people's 04:20 run over the whole
queue went with W3-3 (the queue is ordered by the same facts at pick's rescore).

Requests. People API Search costs 0 credits and returns no emails (clients/apollo.py "people.search"), so nothing goes
to credit_ledger. An account takes 1 request with no Roles-tab People row at its size, 3 with no People leader, 4 with
one not new, and up to 9 with one in title within HORIZON_DAYS. A run waits PACE_SECONDS between requests and stops
starting accounts after MAX_ACCOUNTS_PER_RUN accounts, MAX_REQUESTS_PER_RUN requests or RUN_SECONDS, so its facts are
in before pick_contacts' rescore. A 429 is resent by the transport after Retry-After (clients/http.py); one that
persists stops the run, and the rest wait for the next. REST honours person_days_in_current_title_range as the MCP
tool does (below).

Dry-run: the searches are reads, which the guard allows in dry-run, and the facts and the status reason are database
writes, which dry-run makes too (SPEC 0.3), as the other sources do. Nothing is written outside the database in any
mode.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound.clients.apollo import people_in, total_entries, while_full_page
from us_outbound.clients.db import Store, new_id
from us_outbound.clients.http import ApiError, AuthError
from us_outbound.contacts import pick
from us_outbound.context import Context
from us_outbound.facts import load, newest, newest_by
from us_outbound.logs import log
from us_outbound.scoring import tiers
from us_outbound.scoring.score import aged_value
from us_outbound.settings.model import AGED_FACTS, SIZE_BANDS, Settings
from us_outbound.sources.apollo_universe import NO_CANDIDATES
from us_outbound.timeparse import utc

JOB = "contact_precheck"
SOURCE = pick.PEOPLE_SOURCE  # apollo_people: the People signals read it (settings/model.py SOURCE_FIELDS)
CANDIDATES_FACT = "contactable_candidates"  # source JOB: the people pick_contacts could contact, counted
ORG_SOURCE = "apollo_org"  # its employees fact, when the account row has no count
MARKER = "people_search"  # one per account searched, with the run
NEWEST = pick.NEWEST_LEADER_FACT
COVERAGE, FOUND = "people_search_coverage", "people_found"
REFRESH_DAYS = 30  # search an account again after this; "First People hire (likely)" counts facts for 60 days
COVERAGE_MIN = 0.5  # Apollo holds at least half the headcount, so finding no People leader likely means none
HORIZON_DAYS = 180  # the days in title worked out exactly; beyond, HORIZON_DAYS + 1 ("not new")
PRECISION_DAYS = 7  # how narrow the window gets: "New People leader" ends at most this early
MAX_ACCOUNTS_PER_RUN = 150
MAX_REQUESTS_PER_RUN = 300
RUN_SECONDS = 12 * 60  # starts no account after 05:27, so its facts are in before pick_contacts' rescore at 05:30
PACE_SECONDS = 1.5  # between requests: at most 40 a minute
MAX_ERRORS = 5  # Apollo errors before the run stops
RATE_LIMITED = 429
QUOTE_LIMIT = 300  # SPEC 6
DONE = "every account due a precheck is checked"
NEW_SIGNAL, OLD_SIGNAL = "First People hire (likely)", "First People hire"

_clock = time.monotonic  # tests replace it
_sleep = time.sleep  # tests replace it


# -- which accounts ---------------------------------------------------------------------------------


def searched_recently(store: Store, account_id: str, now: datetime) -> bool:
    """This job searched the account less than REFRESH_DAYS ago (pick_contacts then leaves its facts alone)."""
    e = store.latest("signal_events", "observed_at", {"account_id": account_id, "source": SOURCE, "fact": MARKER})
    t = utc(e.get("observed_at")) if e else None
    return t is not None and now - t < timedelta(days=REFRESH_DAYS)


def history(ctx: Context, account_ids: Sequence[str]) -> tuple[dict[str, dict[str, dict]], dict[str, Any]]:
    """(account_id -> fact -> its newest apollo_people event, the marker among them;
    account_id -> its newest apollo_org employees value)."""
    stored: dict[str, dict[str, dict]] = defaultdict(dict)
    for aid, rows in load(ctx.store, account_ids, SOURCE).items():
        stored[aid] = newest_by(rows, lambda e: str(e.get("fact") or ""), dated=True)
    employees = {aid: e.get("value") for aid, rows in load(ctx.store, account_ids, ORG_SOURCE, "employees").items()
                 if (e := newest(rows, dated=True)) is not None}
    return stored, employees


def leader_titles(settings: Settings, account: Mapping[str, Any]) -> list[str]:
    """The titles of the Roles-tab rows contacted as the People leader at the account's size (and industry group)."""
    roles = [r for r in settings.roles if r.writes_as == pick.PEOPLE_LEADER]
    return pick.search_titles(roles, pick.account_size(account), settings.industry_group_of(account))


def searched_at(stored: Mapping[str, Mapping[str, Any]] | None) -> datetime | None:
    marker = (stored or {}).get(MARKER)
    return utc(marker.get("observed_at")) if marker else None


def checked_recently(store: Store, account_ids: Sequence[str], now: datetime) -> set[str]:
    """The accounts with a contactable_candidates fact less than REFRESH_DAYS old."""
    since = now - timedelta(days=REFRESH_DAYS)
    out: set[str] = set()
    for aid, rows in load(store, account_ids, JOB, CANDIDATES_FACT).items():
        e = newest(rows, dated=True)
        t = utc(e.get("observed_at")) if e else None
        if t is not None and t >= since:
            out.add(aid)
    return out


@dataclass
class Todo:
    accounts: list[dict]  # in checking order
    stored: dict[str, dict[str, dict]]
    employees: dict[str, Any]  # account_id -> its apollo_org employees fact
    left_out: Counter[str]  # accounts waiting for a contact not checked, and why


def candidates(ctx: Context) -> Todo:
    """The verified accounts enrol could take that have no sendable contact (pick.waiting, in queue order), with a
    domain and a size, not prechecked within REFRESH_DAYS."""
    s = ctx.settings
    need, _ = pick.waiting(ctx)
    rows = [a for a in need if a.get("domain")]
    fresh = checked_recently(ctx.store, [a["account_id"] for a in rows], ctx.now)
    stored, employees = history(ctx, [a["account_id"] for a in rows])
    due: list[dict] = []
    left_out: Counter[str] = Counter()
    for a in rows:
        if a["account_id"] in fresh:
            continue
        size = pick.account_size(a)
        if size is None:
            left_out["company size unknown"] += 1
        elif not pick.search_titles(s.roles, size, s.industry_group_of(a)):
            left_out["no Roles-tab row is contacted at its size"] += 1
        else:
            due.append(a)
    return Todo(due, stored, employees, left_out)


def headcount(account: Mapping[str, Any], settings: Settings, org_employees: Any) -> tuple[int | None, str]:
    """(the employees coverage is read against, where the number came from); (None, "") when none is known.

    An Overrides row's, the employees column, the latest apollo_org employees fact; else the top of the account's
    size band, so coverage is never overstated (a 50-99 company with 40 people at Apollo is at least 0.4).
    """
    domain = str(account.get("domain") or "").strip().lower()
    for value, basis in ((settings.overrides_for(domain).get("employees"), "override"),
                         (account.get("employees"), "employees"), (org_employees, "apollo_org")):
        n = tiers.as_number(value)
        if n is not None and n > 0:
            return int(n), basis
    band = str(account.get("size_band") or "")
    if band in SIZE_BANDS:
        return int(band.partition("-")[2]), "size_band_top"
    return None, ""


# -- the searches -----------------------------------------------------------------------------------


@dataclass
class _Run:
    started: float
    requests: int = 0
    searched: int = 0
    with_leader: int = 0
    zero_written: int = 0  # people_leader_count = 0, at coverage >= COVERAGE_MIN
    no_candidates: int = 0  # accounts marked no_contact_candidates
    candidates: Counter[str] = field(default_factory=Counter)  # accounts by how many candidates: 0, 1, 2, 3 or more
    no_count: int = 0  # nobody found and coverage below COVERAGE_MIN or unknown: no count written
    new_in_title: int = 0  # the newest leader within HORIZON_DAYS
    facts: int = 0
    unchanged: int = 0  # facts not written again: the stored value stands
    coverage: Counter[str] = field(default_factory=Counter)
    by_band: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    errors: list[str] = field(default_factory=list)

    def search(self, ctx: Context, filters: Mapping[str, Any], *, page: int = 1, per_page: int = 100) -> dict:
        """One People API Search request (0 credits), PACE_SECONDS after the last."""
        if self.requests:
            _sleep(PACE_SECONDS)
        self.requests += 1
        return ctx.clients.apollo.search_people(filters, page=page, per_page=per_page)

    def why_stop(self, started_accounts: int) -> str | None:
        if started_accounts >= MAX_ACCOUNTS_PER_RUN:
            return f"the run's cap of {MAX_ACCOUNTS_PER_RUN} accounts"
        if self.requests >= MAX_REQUESTS_PER_RUN:
            return f"the run's cap of {MAX_REQUESTS_PER_RUN} requests"
        if _clock() - self.started >= RUN_SECONDS:
            return f"{RUN_SECONDS // 60} minutes, so the facts are in before verify_accounts"
        return None


def search_rows(ctx: Context, r: _Run, filters: Mapping[str, Any]) -> list[dict]:
    """The rows of a search, each once, up to pick.SEARCH_PAGES pages."""
    rows: list[dict] = []
    seen: set[str] = set()
    pages = ctx.clients.apollo.iter_pages(
        lambda page: r.search(ctx, filters, page=page, per_page=pick.SEARCH_PER_PAGE),
        more=while_full_page, rows=people_in, per_page=pick.SEARCH_PER_PAGE, max_pages=pick.SEARCH_PAGES)
    for page in pages:
        for p in page.rows:
            pid = str(p.get("id") or "")
            if pid and pid not in seen:
                seen.add(pid)
                rows.append(p)
    return rows


@dataclass
class Found:
    leaders: list[tuple[int | None, Mapping[str, Any]]]
    total: int | None  # the people Apollo holds at the organization; None when its answer gave no total
    newest: tuple[int, Mapping[str, Any] | None] | None = None  # (days in title, the leader's row)
    candidates: list[pick.Candidate] = field(default_factory=list)  # who pick_contacts could contact
    leaders_searched: bool = True  # False: no Roles-tab People row at its size, so no People facts


def newest_in_title(ctx: Context, r: _Run, account: Mapping[str, Any], titles: Sequence[str],
                    leaders: Sequence[tuple[int | None, Mapping[str, Any]]]) -> tuple[int, Mapping[str, Any] | None]:
    """(the newest leader's days in title, their row): from employment_history when a row has it, else by narrowing
    person_days_in_current_title_range to PRECISION_DAYS (the upper end); (HORIZON_DAYS + 1, None) when nobody is
    within HORIZON_DAYS."""
    known = pick.newest_known(leaders)
    if known is not None:
        return known
    base = pick.search_filters(account, titles, verified_only=False)
    today = ctx.today_uk()

    def within(days: int) -> list[tuple[int | None, Mapping[str, Any]]]:
        # REST honours person_days_in_current_title_range as the MCP tool does: confirmed live 6 Oct 2026, when 3 of
        # 18 accounts' leaders read as new in title (an ignored filter would make every leader new).
        rows = search_rows(ctx, r, {**base, "person_days_in_current_title_range": {"max": days}})
        return pick.people_leaders(account, rows, ctx.settings, today)

    lo, hi = -1, HORIZON_DAYS  # nobody has at most lo days in title; somebody has at most hi
    found = within(hi)
    if not found:
        return HORIZON_DAYS + 1, None
    while hi - lo > PRECISION_DAYS:
        mid = (lo + hi) // 2
        got = within(mid)
        if got:
            hi, found = mid, got
        else:
            lo = mid
    return hi, min((p for _, p in found), key=lambda p: str(p.get("id") or ""))


def contactable(ctx: Context, r: _Run, account: Mapping[str, Any]) -> list[pick.Candidate]:
    """The people pick_contacts could contact at the account: its own search (every Roles-tab title at the size, US,
    a verified email) ranked as it ranks them (pick.rank_candidates; nobody left out for an earlier reveal)."""
    s = ctx.settings
    titles = pick.search_titles(s.roles, pick.account_size(account), s.industry_group_of(account))
    rows = search_rows(ctx, r, pick.search_filters(account, titles))
    cands, _ = pick.rank_candidates(rows, account, s, ctx.today_uk())
    return cands


def search_account(ctx: Context, r: _Run, account: Mapping[str, Any]) -> Found:
    """The candidates search, and for an account the Roles tab contacts a People leader at, the leaders' two searches
    (and, for a leader, the days in title). Raises ApiError, writing nothing."""
    cands = contactable(ctx, r, account)
    titles = leader_titles(ctx.settings, account)
    if not titles:
        return Found([], None, candidates=cands, leaders_searched=False)
    rows = search_rows(ctx, r, pick.search_filters(account, titles, verified_only=False))
    leaders = pick.people_leaders(account, rows, ctx.settings, ctx.today_uk())
    # An organization-only search answers {"total_entries": 812, "people": [one row]}, the people Apollo holds
    # there, wherever they are based: confirmed live over REST on 6 Oct 2026 (a count for each of 134 accounts).
    total = total_entries(r.search(ctx, pick.organization_filter(account), per_page=1))
    found = Found(leaders, total, candidates=cands)
    if leaders:
        found.newest = newest_in_title(ctx, r, account, titles, leaders)
    return found


# -- the facts --------------------------------------------------------------------------------------


def _fact(account_id: str, fact: str, value: Any, now: datetime, quote: str = "") -> dict:
    return {"event_id": new_id(), "account_id": account_id, "source": SOURCE, "fact": fact, "value": value,
            "quote": quote[:QUOTE_LIMIT], "source_url": "", "observed_at": now}


def unchanged(new: Mapping[str, Any], old: Mapping[str, Any] | None, today: date, now: datetime) -> bool:
    """The stored fact says the same and is less than REFRESH_DAYS old, so it need not be written again.

    A day count is compared as of today (scoring ages it), and the newest leader by person and title.
    """
    t = utc(old.get("observed_at")) if old else None
    if old is None or t is None or now - t >= timedelta(days=REFRESH_DAYS):
        return False
    fact, value, before = new["fact"], new["value"], old.get("value")
    if fact in AGED_FACTS:
        return aged_value(fact, old, today) == value
    if fact == NEWEST and isinstance(before, Mapping):
        return all(before.get(k) == value.get(k) for k in ("apollo_person_id", "title"))
    return before == value


def coverage_of(total: int | None, employees: int | None) -> float | None:
    return round(total / employees, 2) if total is not None and employees else None


def candidates_fact(account: Mapping[str, Any], found: Found, ctx: Context) -> dict:
    """The contactable_candidates fact (source contact_precheck): how many, and the first few titles in pick's order."""
    n = len(found.candidates)
    titles = list(dict.fromkeys(" ".join(str(c.person.get("title") or "").split()) for c in found.candidates))
    return {"event_id": new_id(), "account_id": str(account["account_id"]), "source": JOB, "fact": CANDIDATES_FACT,
            "value": {"count": n, "titles": titles[:5], "run_id": ctx.run_id},
            "quote": f"Apollo people search: {n} {'person' if n == 1 else 'people'} with a Roles-tab title for its size, "
                     "in the US, with a verified email", "source_url": "", "observed_at": ctx.now}


def mark_candidates(ctx: Context, account: Mapping[str, Any], n: int) -> str | None:
    """accounts.status_reason from the count: no_contact_candidates at 0, cleared once there is someone again. Only
    a reason this job writes is changed. Returns the reason written, or None."""
    reason = account.get("status_reason")
    if n == 0 and reason in (None, "", NO_CANDIDATES):
        ctx.store.upsert("accounts", [{"account_id": account["account_id"], "status_reason": NO_CANDIDATES}])
        return NO_CANDIDATES
    if n and reason == NO_CANDIDATES:
        ctx.store.upsert("accounts", [{"account_id": account["account_id"], "status_reason": None}])
    return None


def account_facts(account: Mapping[str, Any], found: Found, employees: int | None, basis: str,
                  stored: Mapping[str, Mapping[str, Any]], ctx: Context) -> tuple[list[dict], list[dict], dict]:
    """(the facts to write, the marker among them; those left unwritten because the stored value stands; the marker's
    value). With no People leader search (no Roles-tab People row at its size), only the candidates fact."""
    aid, now = str(account["account_id"]), ctx.now
    if not found.leaders_searched:
        return [candidates_fact(account, found, ctx)], [], {"leaders": None, "found": None, "coverage": None,
                                                            "newest_days_in_title": None, "count_written": False}
    count = len(found.leaders)
    coverage = coverage_of(found.total, employees)
    zero_means_none = coverage is not None and coverage >= COVERAGE_MIN
    newest = found.newest if count else (HORIZON_DAYS + 1, None) if zero_means_none else None
    rows = pick.leader_facts(aid, count if count or zero_means_none else None, newest, now)
    if coverage is not None:
        rows.append(_fact(aid, COVERAGE, coverage, now))
    if found.total is not None:
        rows.append(_fact(aid, FOUND, found.total, now))
    write = [e for e in rows if not unchanged(e, stored.get(e["fact"]), ctx.today_uk(), now)]
    kept = [e for e in rows if e not in write]
    days = found.newest[0] if found.newest else None
    titles = sorted({" ".join(str(p.get("title") or "").split()) for _, p in found.leaders})
    value = {"run_id": ctx.run_id, "leaders": count, "leader_titles": titles[:10], "newest_days_in_title": days,
             "found": found.total, "employees": employees, "employees_from": basis or None, "coverage": coverage,
             "count_written": bool(count or zero_means_none)}
    quote = f"Apollo people search: {count} People leader{'' if count == 1 else 's'} in the US"
    if titles:
        quote += f" ({', '.join(titles[:3])})"
    if found.total is not None:
        quote += f"; {found.total} people at the company"
    if coverage is not None:
        quote += f", {coverage} of {employees} employees"
    return [*write, _fact(aid, MARKER, value, now, quote), candidates_fact(account, found, ctx)], kept, value


def sheet_notice(settings: Settings) -> str | None:
    """Why the new First People hire row is not scoring yet: a Signals tab loaded before 5 Oct 2026."""
    names = {s.signal for s in settings.signals}
    if NEW_SIGNAL in names:
        return None
    old = next((s for s in settings.active_signals() if s.signal == OLD_SIGNAL), None)
    return (f"the Signals tab has no {NEW_SIGNAL!r} row" + (f" and {OLD_SIGNAL!r} is still on" if old else "")
            + "; run `us-outbound settings load --tab Signals --live`, then `us-outbound sync`")


# -- the job ----------------------------------------------------------------------------------------


def _done(ctx: Context, summary: dict) -> dict:
    log("contact_precheck_done", run_id=ctx.run_id, **summary)
    return summary


def run(ctx: Context) -> dict:
    """The contact_precheck job (JOB CONTRACT: run(ctx) -> summary)."""
    s = ctx.settings
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run}
    if notice := sheet_notice(s):
        summary["signals_notice"] = notice
    todo = candidates(ctx)
    summary["left_out"] = dict(todo.left_out)
    if not todo.accounts:
        summary.update(status="ok", due=0, stopped_by="no account is due a precheck")
        return _done(ctx, summary)
    r = _Run(started=_clock())
    stopped = DONE
    for started, account in enumerate(todo.accounts):
        if why := r.why_stop(started):
            stopped = why
            break
        aid = str(account["account_id"])
        try:
            found = search_account(ctx, r, account)
        except AuthError:
            raise  # the key is wrong: every request would fail
        except ApiError as exc:
            r.errors.append(f"{account.get('domain')}: {str(exc)[:200]}")
            if exc.status == RATE_LIMITED:
                stopped = "Apollo's rate limit (HTTP 429 after the transport's retries)"
                break
            if len(r.errors) >= MAX_ERRORS:
                stopped = f"{MAX_ERRORS} Apollo errors"
                break
            continue
        employees, basis = headcount(account, s, todo.employees.get(aid))
        facts, kept, value = account_facts(account, found, employees, basis, todo.stored.get(aid, {}), ctx)
        ctx.store.insert("signal_events", facts)  # each account as it is searched, so a run cut short keeps them
        n = len(found.candidates)
        r.no_candidates += mark_candidates(ctx, account, n) is not None
        r.candidates[str(min(n, 3)) if n < 3 else "3 or more"] += 1
        r.searched += 1
        if not found.leaders_searched:
            continue
        r.facts += len(facts)
        r.unchanged += len(kept)
        r.with_leader += bool(found.leaders)
        r.zero_written += value["count_written"] and not found.leaders
        r.no_count += not value["count_written"]
        r.new_in_title += bool(found.newest and found.newest[0] <= HORIZON_DAYS)
        cov = value["coverage"]
        bucket = "unknown" if cov is None else f"at least {COVERAGE_MIN}" if cov >= COVERAGE_MIN else f"below {COVERAGE_MIN}"
        r.coverage[bucket] += 1
        band = r.by_band[str(account.get("size_band") or "unknown")]
        band["searched"] += 1
        band[f"coverage at least {COVERAGE_MIN}"] += cov is not None and cov >= COVERAGE_MIN
        log("contact_precheck_account", account_id=aid, candidates=n, leaders=value["leaders"], found=value["found"],
            coverage=cov, newest_days_in_title=value["newest_days_in_title"])
    summary.update(
        status="ok", stopped_by=stopped, due=len(todo.accounts), searched=r.searched,
        left_for_next_run=len(todo.accounts) - r.searched, no_contact_candidates=r.no_candidates,
        candidates=dict(r.candidates), with_people_leader=r.with_leader,
        leader_new_in_title=r.new_in_title, new_within_days=HORIZON_DAYS, count_zero_written=r.zero_written,
        no_count_written=r.no_count, coverage=dict(r.coverage),
        coverage_by_band={b: dict(c) for b, c in sorted(r.by_band.items())}, facts=r.facts, unchanged=r.unchanged,
        requests=r.requests, requests_per_account=round(r.requests / max(1, r.searched), 2), credits=0,
        seconds=round(_clock() - r.started, 1), errors=r.errors[:20],
    )
    return _done(ctx, summary)
