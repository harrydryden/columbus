"""Source "apollo_jobs": open roles and open People roles from Apollo (SPEC 7; SPEC 9 apollo_signals).

These are the facts the "Hiring and growth" (open_roles >= 3), "People role open" and "First People
hire (likely)" (open_people_roles >= 1) signals read. open_roles is owned by this source (docs/pipeline.md, change 4).

Each weekday at 03:30 UK, after source_universe and before verify_accounts, for the accounts in the
queue (new, queued or verified, not Excluded or Held) with an Apollo organization id and no
apollo_jobs facts from the last REFRESH_DAYS days: never-read accounts first, then the Focus tab's
groups, then queue order.
  1. A screen: one organization search per 100 accounts, filtered to their ids and to companies
     with at least one current posting (organization_ids, organization_num_jobs_range). An account
     it leaves out has no open postings: open_roles 0, open_people_roles 0, no titles.
  2. Job postings for each account the screen kept (Organization Job Postings), while today's
     credits last: open_roles is the number of current postings, open_people_roles the number
     with an HR or People title (people_titles: PEOPLE_TITLES and the titles of the Roles tab's
     People roles), posting_titles the titles. An account not read today waits for the next run.
SPEC 7 has these reads at 0 to 1 credits; Apollo's docs (1 Oct 2026) charge 1 credit per job
postings request and 1 per search page that returns a company. So an account costs at most 0.01
credits for the screen, and 1 more if it has open postings. The job may spend SIGNALS_SHARE of
apollo_monthly_credits, paced by the weekday, and never more than is left of the whole Apollo
budget today (sources/apollo_credits.py); nothing while Apollo's balance is below apollo_floor.

Schedule: weekdays rather than SPEC 9's Mondays, so the accounts sourced each morning are read
before verify_accounts at 04:30 and its rescore, and the credits are paced by the weekday.

Dry-run: the reads happen and their credits are recorded; facts and the ledger are database
writes, which dry-run makes too (SPEC 0.3), as in source_universe. Nothing is written outside
the database in any mode.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from us_outbound.clients.apollo import MAX_PER_PAGE, organizations_in, postings_in, total_entries
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.enrol import focus, queue
from us_outbound.logs import log
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.model import Settings
from us_outbound.sources import apollo_credits as credits
from us_outbound.sources.apollo_universe import OPEN_STATUSES, OUT_OF_QUEUE_TIERS
from us_outbound.timeparse import utc

JOB = "apollo_signals"
SOURCE = "apollo_jobs"
SIGNALS_SHARE = 0.25  # of apollo_monthly_credits (source_universe as much, apollo_enrich 0.15; the 0.35 left for reveals)
REFRESH_DAYS = 30  # read an account again after this; the hiring signals count facts for 90 days
SCREEN_BATCH = MAX_PER_PAGE  # accounts per screening search: one page holds them all
POSTINGS_PER_PAGE = 100  # one request (1 credit) per account; a larger count is taken from pagination
TITLES_KEPT = 50
QUOTE_LIMIT = 300  # SPEC 6
ID_CHUNK = 1000
# HR and People titles, matched as whole words (case-insensitive) in a posting's title. Recruiting
# titles alone are hiring, not a People function, so "Recruiter" and "Talent Acquisition" are not here.
PEOPLE_TITLES = (
    "HR", "HRBP", "CHRO", "Human Resources", "Human Resource", "People Operations", "People Ops",
    "People & Culture", "People and Culture", "People Partner", "People Business Partner", "Head of People",
    "VP People", "VP of People", "Chief People Officer", "Director of People", "People Director", "People Lead",
    "People Manager", "People Generalist", "People Specialist", "People Coordinator", "Head of Talent",
    "Total Rewards", "Benefits Manager",
)


def people_titles(settings: Settings) -> tuple[str, ...]:
    """PEOPLE_TITLES and the titles of the Roles tab's People roles ("People leader", "People manager")."""
    extra = [t for r in settings.roles if r.role.casefold().startswith("people") for t in r.titles]
    return tuple(dict.fromkeys([*PEOPLE_TITLES, *extra]))


def is_people_title(title: str, terms: Sequence[str]) -> bool:
    return bool(find_terms(title or "", tuple(terms)))


def _chunks(items: Sequence[str], n: int) -> list[Sequence[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def last_read(ctx: Context, account_ids: Sequence[str]) -> dict[str, datetime]:
    """account_id -> when its apollo_jobs facts were last observed."""
    out: dict[str, datetime] = {}
    for chunk in _chunks(list(account_ids), ID_CHUNK):
        for e in ctx.store.select("signal_events", {"account_id": list(chunk), "source": SOURCE}):
            t = utc(e.get("observed_at"))
            if t is not None and (e["account_id"] not in out or t > out[e["account_id"]]):
                out[e["account_id"]] = t
    return out


def candidates(ctx: Context) -> list[dict]:
    """Queue accounts with an Apollo id and no apollo_jobs facts from the last REFRESH_DAYS, in reading order."""
    s = ctx.settings
    rows = [a for a in ctx.store.select("accounts", {"status": list(OPEN_STATUSES)})
            if a.get("apollo_org_id") and a.get("tier") not in OUT_OF_QUEUE_TIERS]
    seen = last_read(ctx, [a["account_id"] for a in rows])
    cutoff = ctx.now - timedelta(days=REFRESH_DAYS)
    stale = [a for a in rows if a["account_id"] not in seen or seen[a["account_id"]] < cutoff]
    return sorted(stale, key=lambda a: (a["account_id"] in seen, focus.group_rank(s.industry_group_of(a), s),
                                        queue.order_key(a, s)))


def _quote(text: str) -> str:
    q = " ".join(text.split())
    return q if len(q) <= QUOTE_LIMIT else q[: QUOTE_LIMIT - 1] + "…"


def _fact(account: Mapping[str, Any], fact: str, value: Any, quote: str, url: str, now: datetime) -> dict:
    return {"event_id": new_id(), "account_id": account["account_id"], "source": SOURCE, "fact": fact,
            "value": value, "quote": _quote(quote), "source_url": url, "observed_at": now}


def no_postings_facts(account: Mapping[str, Any], now: datetime) -> list[dict]:
    q = "Apollo lists no open job postings"
    return [_fact(account, "open_roles", 0, q, "", now), _fact(account, "open_people_roles", 0, q, "", now),
            _fact(account, "posting_titles", [], q, "", now)]


def postings_facts(account: Mapping[str, Any], body: Mapping[str, Any], terms: Sequence[str], now: datetime) -> list[dict]:
    """open_roles, open_people_roles and posting_titles from one job postings page."""
    posts = postings_in(body)
    titles = [str(p.get("title")).strip() for p in posts if str(p.get("title") or "").strip()]
    people = [p for p in posts if is_people_title(str(p.get("title") or ""), terms)]
    open_roles = max(len(posts), total_entries(body) or 0)
    url = str((people or posts or [{}])[0].get("url") or "")
    people_seen = [str(p.get("title")).strip() for p in people]
    return [
        _fact(account, "open_roles", open_roles, f"Apollo lists {open_roles} open job postings: " + "; ".join(titles), url, now),
        _fact(account, "open_people_roles", len(people),
              ("HR or People postings: " + "; ".join(people_seen)) if people else "No HR or People postings", url, now),
        _fact(account, "posting_titles", titles[:TITLES_KEPT], "; ".join(titles), url, now),
    ]


@dataclass
class _Run:
    screened: int = 0
    without_postings: int = 0
    read: int = 0
    waiting: int = 0  # screened in, not read today
    credits: float = 0.0
    facts: int = 0
    errors: list[str] = field(default_factory=list)


def screen(ctx: Context, batch: Sequence[dict], run: _Run, room: credits.Room) -> set[str] | None:
    """The Apollo ids in batch with at least one current posting; None if Apollo refused the search.

    Confirmed live 2 to 7 Oct 2026: organization_ids and organization_num_jobs_range together on
    mixed_companies/search (each screen of 100 accounts returned 50 to 66 as hiring).
    """
    ids = [str(a["apollo_org_id"]) for a in batch]
    try:
        body = ctx.clients.apollo.search_organizations(
            {"organization_ids": ids, "organization_num_jobs_range[min]": 1}, per_page=SCREEN_BATCH)
    except ApiError as exc:
        if exc.status in (401, 403):
            raise
        run.errors.append(f"screen: {str(exc)[:200]}")
        return None
    hiring = {str(o.get("organization_id")) for o in organizations_in(body) if o.get("organization_id")}
    spent = 1.0 if hiring else 0.0
    credits.record(ctx, JOB, spent, note=json.dumps({"screen": len(ids), "with_postings": len(hiring)}))
    room.spend(spent)
    run.credits += spent
    run.screened += len(ids)
    return hiring


def read_postings(ctx: Context, account: Mapping[str, Any], terms: Sequence[str], run: _Run,
                  room: credits.Room) -> list[dict]:
    """The account's facts from its job postings (1 credit); [] if Apollo refused the request."""
    org = str(account["apollo_org_id"])
    try:
        body = ctx.clients.apollo.job_postings(org, per_page=POSTINGS_PER_PAGE)
    except ApiError as exc:
        if exc.status in (401, 403):
            raise
        run.errors.append(f"{account['account_id']}: {str(exc)[:200]}")
        return []
    credits.record(ctx, JOB, 1.0, note=json.dumps({"postings": org}), account_id=account["account_id"])
    room.spend(1.0)
    run.credits += 1.0
    run.read += 1
    return postings_facts(account, body, terms, ctx.now)


def run(ctx: Context) -> dict:
    """The apollo_signals job (JOB CONTRACT: run(ctx) -> summary)."""
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run}
    todo = candidates(ctx)
    if not todo:
        summary.update(status="ok", candidates=0, stopped_by="no account needs its job postings read")
        log("apollo_signals_done", run_id=ctx.run_id, **summary)
        return summary
    floor = credits.floor_reason(ctx)
    if floor:
        summary.update(skipped=True, reason=floor, candidates=len(todo))
        log("apollo_signals_done", run_id=ctx.run_id, **summary)
        return summary
    room = credits.room(ctx, JOB, SIGNALS_SHARE)
    terms = people_titles(ctx.settings)
    r = _Run()
    stopped = "every account in the queue is read"
    for batch in _chunks(todo, SCREEN_BATCH):
        if not room.allows():
            stopped = "today's Apollo credits for job postings are used"
            break
        hiring = screen(ctx, batch, r, room)
        if hiring is None:
            continue
        facts: list[dict] = []
        for account in batch:
            if str(account["apollo_org_id"]) not in hiring:
                r.without_postings += 1
                facts += no_postings_facts(account, ctx.now)
            elif room.allows():
                facts += read_postings(ctx, account, terms, r, room)
            else:
                r.waiting += 1
        if facts:  # each batch as it is read, so a run stopped by its timeout keeps what it paid for
            ctx.store.insert("signal_events", facts)
            r.facts += len(facts)
    if r.waiting:
        stopped = "today's Apollo credits for job postings are used"
    summary.update(
        status="ok", stopped_by=stopped, candidates=len(todo), screened=r.screened, without_postings=r.without_postings,
        postings_read=r.read, waiting=r.waiting, credits=r.credits, facts=r.facts,
        credits_per_account=round(r.credits / max(1, r.screened), 3), budget=room.as_dict(), errors=r.errors[:20],
    )
    log("apollo_signals_done", run_id=ctx.run_id, **summary)
    return summary
