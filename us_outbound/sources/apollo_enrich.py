"""Source "apollo_org", enriched: funding and an exact headcount from Apollo's organization enrich (Harry, 2 Oct 2026).

The first live run (2 Oct 2026) showed that Apollo's organization search rows carry no funding fields
and no employee count, so the funding signals ("Funding in the last 6 months", "Funding 6–12 months
ago"; sources apollo_org and clay_funding) never fired. Harry, 2 Oct 2026: funding is a sign that a
company is in a period of change and growth; take it from Apollo's organization enrich (1 credit a
company) for the industry groups where funding is common (General apollo_enrich_groups, default
Technology & Startups).

Each weekday at 04:10 UK, after read_pages starts (03:45) and before verify_accounts (04:30), whose
rescore scores the new facts before pick_contacts (05:30), for the queue accounts (new, queued or
verified, not Excluded or Held) in those groups with a domain: never-enriched accounts first, then
those enriched more than REFRESH_DAYS ago; within each, the Focus tab's groups, then queue order
(enrol/queue.order_key), as apollo_signals takes them.
  1. Bulk Organization Enrichment, up to BULK_ENRICH_MAX domains a call (clients/apollo.py). A domain
     the bulk answer does not carry is asked once more by Organization Enrichment, whose answer
     settles it: a record (found) or none (not found), so a company Apollo files under another domain
     is not lost. The run carries on with single calls when Apollo refuses a bulk call, or its answer
     carries a record that matches no domain asked or says it found more than it carries: a single
     call costs the same credits, only more requests (the bulk answer's shape: confirmed live 5 Oct 2026).
  2. Facts, as source_universe writes them (apollo_universe.org_facts, source apollo_org):
     days_since_funding (as of today; scoring ages it), funding_stage, funding_amount_usd, employees
     (estimated_num_employees) and headcount_growth_12m; description, technologies and keywords only
     when the account has none stored yet. And an org_enrich fact for every account answered, with
     the outcome and the run, so an account is not enriched again within REFRESH_DAYS. A company Apollo
     returns nothing for is "not found", never "no funding". A call that failed writes nothing, and its
     accounts are tried again next run.
  3. The account row: an exact count supersedes the searched band, so employees and size_band
     (clean.people.size_band) are written, unless an Overrides row sets either or Clay has confirmed
     them (apollo_universe.CLAY_OWNED). verify_accounts then handles a count outside the size range as it
     already does; a verified account the count puts outside goes back to new, so verify sees it again.

Credits. 1 credit per company found and 0 for one not found (Apollo's docs; PHASE0-CONFIRM). Each call
is reserved in credit_ledger before it is made (a bulk call at its domains, a single call at 1) and
settled after it at the companies found, so a call cut off still counts. The job may spend ENRICH_SHARE
of apollo_monthly_credits, paced by the weekday, and never more than is left of the whole Apollo budget
today (sources/apollo_credits.py), so pick_contacts keeps the rest for email reveals; nothing while
Apollo's balance is below apollo_floor.

Dry-run: the enrich calls are reads, so they happen, and their credits are recorded; facts, account
columns and the ledger are database writes, which dry-run makes too (SPEC 0.3), as the other sources
do. Nothing is written outside the database in any mode.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound.clean.domains import root_domain
from us_outbound.clean.people import size_band
from us_outbound.clients.apollo import BULK_ENRICH_MAX, enriched_in
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.enrol import focus, queue
from us_outbound.logs import log
from us_outbound.scoring import tiers
from us_outbound.scoring.score import aged_value
from us_outbound.settings.model import Settings
from us_outbound.sources import apollo_credits as credits
from us_outbound.sources import apollo_universe as uni
from us_outbound.timeparse import utc

JOB = "apollo_enrich"
SOURCE = uni.SOURCE  # apollo_org: the funding signals read it
ENRICH_SHARE = 0.15  # of apollo_monthly_credits (source_universe and apollo_signals 0.25 each; the 0.35 left for reveals)
REFRESH_DAYS = 180  # SPEC 7's refresh: a company's latest round is then at least this old, or a new one shows
MARKER = "org_enrich"  # one per account answered: found or not found, with the run
FOUND, NOT_FOUND = "found", "not_found"
BULK, SINGLE = "bulk", "single"
FUNDING_FACTS = ("days_since_funding", "funding_stage", "funding_amount_usd")
FACTS = frozenset({*FUNDING_FACTS, "employees", "headcount_growth_12m"})  # written at every enrich
EXTRA_FACTS = frozenset({"description", "technologies", "keywords"})  # written only while none is stored
SIZE_FIELDS = frozenset({"employees", "size_band"})
MAX_ACCOUNTS_PER_RUN = 500  # a company not found costs no credit, only a request: well inside the timeout
MAX_ERRORS = 5  # Apollo errors before the run stops
DONE, SPENT = "every account due an enrich is enriched", "today's Apollo credits for enrichment are used"
ID_CHUNK = 1000


def _chunks(items: Sequence[str], n: int) -> list[Sequence[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


# -- which accounts ---------------------------------------------------------------------------------


def history(ctx: Context, account_ids: Sequence[str]) -> tuple[dict[str, datetime], dict[str, set[str]]]:
    """(account_id -> when it was last enriched; account_id -> the apollo_org facts it has stored)."""
    last: dict[str, datetime] = {}
    stored: dict[str, set[str]] = defaultdict(set)
    for chunk in _chunks(list(account_ids), ID_CHUNK):
        for e in ctx.store.select("signal_events", {"account_id": list(chunk), "source": SOURCE}):
            aid = e["account_id"]
            stored[aid].add(str(e.get("fact")))
            t = utc(e.get("observed_at")) if e.get("fact") == MARKER else None
            if t is not None and (aid not in last or t > last[aid]):
                last[aid] = t
    return last, stored


def candidates(ctx: Context) -> tuple[list[dict], dict[str, set[str]]]:
    """Queue accounts in apollo_enrich_groups with a domain, not enriched within REFRESH_DAYS, in enriching order;
    and the apollo_org facts each has stored."""
    s = ctx.settings
    wanted = {g.casefold() for g in s.general.apollo_enrich_groups}
    rows = [a for a in ctx.store.select("accounts", {"status": list(uni.OPEN_STATUSES)})
            if a.get("domain") and a.get("tier") not in uni.OUT_OF_QUEUE_TIERS
            and s.industry_group_of(a).casefold() in wanted]
    seen, stored = history(ctx, [a["account_id"] for a in rows])
    cutoff = ctx.now - timedelta(days=REFRESH_DAYS)
    due = [a for a in rows if a["account_id"] not in seen or seen[a["account_id"]] < cutoff]
    return sorted(due, key=lambda a: (a["account_id"] in seen, focus.group_rank(s.industry_group_of(a), s),
                                      queue.order_key(a, s))), stored


# -- one answer -------------------------------------------------------------------------------------


def org_keys(org: Mapping[str, Any]) -> set[str]:
    """The root domains an enrich record answers for: its primary domain, domain and website."""
    return {d for v in (org.get("primary_domain"), org.get("domain"), org.get("website_url"))
            if v and (d := root_domain(str(v)))}


def answer_facts(account: Mapping[str, Any], org: Mapping[str, Any] | None, stored: set[str], via: str,
                 now: datetime, run_id: str) -> list[dict]:
    """The apollo_org facts of one enrich answer (org None: Apollo has no record) and its org_enrich fact."""
    aid = str(account["account_id"])
    facts: list[dict] = []
    if org:
        keep = FACTS | (EXTRA_FACTS - stored)
        facts = [e for e in uni.org_facts(aid, org, "", now) if e["fact"] in keep]
    got = {e["fact"]: e["value"] for e in facts}
    org_id = str(org.get("organization_id") or org.get("id") or "") if org else ""
    if org:
        days, stage, n = got.get("days_since_funding"), got.get("funding_stage"), got.get("employees")
        parts = ["found"]
        if days is not None:
            parts.append(f"latest funding round {days} days ago" + (f" ({stage})" if stage else ""))
        elif stage:
            parts.append(f"latest funding round {stage}, undated")
        else:
            parts.append("no funding round listed")
        if n is not None:
            parts.append(f"about {n} employees")
        quote = "Apollo organization enrich: " + "; ".join(parts)
    else:
        quote = f"Apollo organization enrich: no record for {account.get('domain')}"
    value = {"outcome": FOUND if org else NOT_FOUND, "via": via, "run_id": run_id, "organization_id": org_id or None,
             "funding": any(f in got for f in FUNDING_FACTS) if org else None,
             "days_since_funding": got.get("days_since_funding"), "funding_stage": got.get("funding_stage"),
             "employees": got.get("employees")}
    facts.append({"event_id": new_id(), "account_id": aid, "source": SOURCE, "fact": MARKER, "value": value,
                  "quote": quote[: uni.QUOTE_LIMIT], "source_url": uni.APOLLO_ORG_URL.format(org_id) if org_id else "",
                  "observed_at": now})
    return facts


def size_update(account: Mapping[str, Any], employees: Any, settings: Settings) -> tuple[dict | None, str]:
    """(the account's employees and size_band from Apollo's exact count, ""), or (None, why the row keeps its own).

    The exact count supersedes the band searched. An Overrides row for either field wins (SPEC 5),
    and so do sizes Clay has confirmed (apollo_universe.CLAY_OWNED). A verified account the count puts
    outside the General size range goes back to new, so verify_accounts checks it as it checks every new account.
    """
    n = tiers.as_number(employees)
    if n is None:
        return None, ""
    if SIZE_FIELDS & set(settings.overrides_for(str(account.get("domain") or "").strip().lower())):
        return None, "an Overrides row sets the size"
    if account.get("clay_checked_at"):
        return None, "Clay confirmed the size"
    n, band = int(n), size_band(n)
    if account.get("employees") == n and account.get("size_band") == band:
        return None, ""
    row: dict[str, Any] = {"account_id": account["account_id"], "employees": n, "size_band": band}
    if not settings.size_in_range(n) and account.get("status") == "verified":
        row["status"] = "new"
    return row, ""


# -- the calls --------------------------------------------------------------------------------------


@dataclass
class _Run:
    bulk: bool = True  # False once a bulk call is refused or unclear: single calls for the rest of the run
    bulk_calls: int = 0
    single_calls: int = 0
    unmatched: int = 0  # companies a bulk answer carried that match no domain asked
    credits: float = 0.0
    facts: int = 0
    sized: int = 0  # accounts whose employees and size_band were written
    back_to_verify: int = 0
    kept: Counter[str] = field(default_factory=Counter)  # why an account's row kept its own size
    waiting: int = 0  # accounts a bulk call left unanswered with no credit left for a single call
    errors: list[str] = field(default_factory=list)


def _failed(ctx: Context, entry: dict, exc: ApiError, reserved: float, note: dict, what: str, r: _Run,
            room: credits.Room) -> None:
    """Settle a call Apollo refused: 0 for a 4xx (refused unprocessed), else counted in case it charged."""
    spent = 0.0 if 400 <= exc.status < 500 else reserved
    credits.settle(ctx, entry, spent, note=json.dumps({**note, "failed": exc.status}))
    room.spend(spent)
    r.credits += spent
    if exc.status in (401, 403):
        raise exc  # the key is wrong: every call would fail
    r.errors.append(f"{what}: {str(exc)[:200]}")


def ask_bulk(ctx: Context, batch: Sequence[Mapping[str, Any]], r: _Run, room: credits.Room) -> dict[str, dict] | None:
    """domain -> Apollo's record, for the batch's domains one bulk call found; None if Apollo refused the call.

    Reserved at one credit a domain and settled at the companies found: the records it carries, or
    what it reports when that is more (PHASE0-CONFIRM: unique_enriched_records, credits_consumed).
    An answer that carries a record matching no domain asked, or reports more found than it carries,
    turns bulk calls off for the rest of the run, so no company is paid for twice unseen.
    """
    domains = [str(a["domain"]) for a in batch]
    note = {"bulk_enrich": len(domains)}
    entry = credits.reserve(ctx, JOB, len(domains), note=json.dumps({**note, "reserved": True}))
    try:
        body = ctx.clients.apollo.bulk_enrich_organizations(domains)
    except ApiError as exc:
        _failed(ctx, entry, exc, float(len(domains)), note, f"bulk_enrich of {len(domains)} domains", r, room)
        return None
    orgs = enriched_in(body)
    reported = max(float(body.get("unique_enriched_records") or 0), float(body.get("credits_consumed") or 0))
    spent = max(float(len(orgs)), reported)
    credits.settle(ctx, entry, spent, note=json.dumps({**note, "found": len(orgs), "reported": reported}))
    room.spend(spent)
    r.credits += spent
    r.bulk_calls += 1
    asked, out, unmatched = set(domains), {}, 0
    for org in orgs:
        hits = org_keys(org) & asked
        unmatched += not hits
        for d in hits:
            out.setdefault(d, org)
    r.unmatched += unmatched
    if unmatched or reported > len(orgs):
        r.bulk = False
        r.errors.append(f"bulk_enrich carried {len(orgs)} records, {unmatched} matching no domain asked, and reported "
                        f"{reported:.0f} found: single calls from here")
    return out


def ask_one(ctx: Context, account: Mapping[str, Any], r: _Run, room: credits.Room) -> tuple[bool, dict | None]:
    """(answered, Apollo's record or None when it has none) for one domain (1 credit if found); (False, None) on an error."""
    domain = str(account["domain"])
    note = {"enrich": domain}
    entry = credits.reserve(ctx, JOB, 1.0, note=json.dumps({**note, "reserved": True}), account_id=account["account_id"])
    try:
        body = ctx.clients.apollo.enrich_organization(domain)
    except ApiError as exc:
        if exc.status != 404:  # PHASE0-CONFIRM: 404 is how Apollo says it has no record for the domain
            _failed(ctx, entry, exc, 1.0, note, domain, r, room)
            return False, None
        body = {}
    org = next(iter(enriched_in(body)), None)
    spent = 1.0 if org else 0.0
    credits.settle(ctx, entry, spent, note=json.dumps({**note, "found": bool(org)}))
    room.spend(spent)
    r.credits += spent
    r.single_calls += 1
    return True, org


# -- what it found (the summary and the daily post) ------------------------------------------------


@dataclass
class Tally:
    """Enrich answers: one run's, or every account's latest so far, with funding aged to today."""

    accounts: int = 0
    found: int = 0
    not_found: int = 0
    with_funding: int = 0
    within_180: int = 0
    within_365: int = 0
    with_employees: int = 0
    run_at: datetime | None = None

    def describe(self) -> str:
        return (f"{self.accounts} accounts ({self.found} found, {self.not_found} not found): {self.with_funding} "
                f"with funding ({self.within_180} in the last 180 days, {self.within_365} in the last 365); "
                f"{self.with_employees} with an employee count")


def tally(store: Any, today: date, *, run_id: str | None = None) -> Tally:
    """What the enrich found: for one run (its org_enrich facts), or for every account enriched so far."""
    latest: dict[str, dict] = {}
    for e in store.select("signal_events", {"source": SOURCE, "fact": MARKER}):
        v, t = e.get("value"), utc(e.get("observed_at"))
        if not isinstance(v, Mapping) or t is None or (run_id is not None and v.get("run_id") != run_id):
            continue
        aid = str(e.get("account_id"))
        if aid not in latest or t > latest[aid]["observed_at"]:
            latest[aid] = {**e, "observed_at": t}
    out = Tally()
    for e in latest.values():
        v = e["value"]
        out.accounts += 1
        out.run_at = e["observed_at"] if out.run_at is None or e["observed_at"] > out.run_at else out.run_at
        if v.get("outcome") != FOUND:
            out.not_found += 1
            continue
        out.found += 1
        out.with_funding += bool(v.get("funding"))
        out.with_employees += v.get("employees") is not None
        days = tiers.as_number(aged_value("days_since_funding", {**e, "value": v.get("days_since_funding")}, today))
        if days is not None:
            out.within_180 += days <= 180
            out.within_365 += days <= 365
    return out


def latest_run(store: Any) -> str | None:
    """The run_id of the newest org_enrich fact."""
    best: tuple[datetime, str] | None = None
    for e in store.select("signal_events", {"source": SOURCE, "fact": MARKER}):
        t, v = utc(e.get("observed_at")), e.get("value")
        if t is not None and isinstance(v, Mapping) and v.get("run_id") and (best is None or t > best[0]):
            best = (t, str(v["run_id"]))
    return best[1] if best else None


def post_lines(ctx: Context) -> list[str]:
    """The daily post's lines: the last run and every account enriched so far (learn/daily_post.py)."""
    groups = ", ".join(ctx.settings.general.apollo_enrich_groups) or "no group: apollo_enrich_groups is blank"
    today = ctx.today_uk()
    total = tally(ctx.store, today)
    if not total.accounts:
        return [f"Funding (Apollo enrich, {groups}): no account enriched yet (apollo_enrich, weekdays 04:10)."]
    run_id = latest_run(ctx.store)
    last = tally(ctx.store, today, run_id=run_id) if run_id else Tally()
    return [f"Funding (Apollo enrich, {groups}): last run {last.describe()}.", f"  So far {total.describe()}."]


# -- the job ----------------------------------------------------------------------------------------


def _done(ctx: Context, summary: dict) -> dict:
    log("apollo_enrich_done", run_id=ctx.run_id, **summary)
    return summary


def run(ctx: Context) -> dict:
    """The apollo_enrich job (JOB CONTRACT: run(ctx) -> summary)."""
    s = ctx.settings
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "groups": list(s.general.apollo_enrich_groups)}
    if not s.general.apollo_enrich_groups:
        summary.update(skipped=True, reason="apollo_enrich_groups is blank, so no industry group is enriched")
        return _done(ctx, summary)
    todo, stored = candidates(ctx)
    if not todo:
        summary.update(status="ok", candidates=0, stopped_by="no account in these groups is due an enrich")
        return _done(ctx, summary)
    floor = credits.floor_reason(ctx)
    if floor:
        summary.update(skipped=True, reason=floor, candidates=len(todo))
        return _done(ctx, summary)
    room = credits.room(ctx, JOB, ENRICH_SHARE)
    r = _Run()
    stopped = DONE
    if len(todo) > MAX_ACCOUNTS_PER_RUN:
        stopped = f"the run's cap of {MAX_ACCOUNTS_PER_RUN} accounts"
    pending = deque(todo[:MAX_ACCOUNTS_PER_RUN])
    while pending:
        if len(r.errors) >= MAX_ERRORS:
            stopped = f"{MAX_ERRORS} Apollo errors"
            break
        size = min(BULK_ENRICH_MAX if r.bulk else 1, room.whole_credits(), len(pending))
        if size < 1:
            stopped = SPENT
            break
        batch = [pending.popleft() for _ in range(size)]
        found = ask_bulk(ctx, batch, r, room) if r.bulk else {}
        if found is None:
            r.bulk, found = False, {}
        facts: list[dict] = []
        rows: list[dict] = []
        for account in batch:
            via, org = BULK, found.get(str(account["domain"]))
            if org is None:
                if not room.allows():
                    r.waiting += 1  # no fact written: the next run asks again
                    continue
                answered, org = ask_one(ctx, account, r, room)
                if not answered:
                    continue
                via = SINGLE
            got = answer_facts(account, org, stored.get(account["account_id"], set()), via, ctx.now, ctx.run_id)
            facts += got
            n = next((e["value"] for e in got if e["fact"] == "employees"), None)
            row, kept = size_update(account, n, s)
            if kept:
                r.kept[kept] += 1
            if row:
                rows.append(row)
                r.back_to_verify += row.get("status") == "new"
        if facts:  # each batch as it is answered, so a run stopped by its timeout keeps what it paid for
            ctx.store.insert("signal_events", facts)
            r.facts += len(facts)
        if rows:
            ctx.store.upsert("accounts", rows)  # partial rows: only employees, size_band (and status) change
            r.sized += len(rows)
    if r.waiting and stopped == DONE:
        stopped = SPENT
    t = tally(ctx.store, ctx.today_uk(), run_id=ctx.run_id)
    summary.update(
        status="ok", stopped_by=stopped, candidates=len(todo), enriched=t.accounts, found=t.found,
        not_found=t.not_found, with_funding=t.with_funding, funding_within_180=t.within_180,
        funding_within_365=t.within_365, with_employees=t.with_employees, size_written=r.sized,
        size_kept=dict(r.kept), back_to_verify=r.back_to_verify, left_for_next_run=len(todo) - t.accounts,
        bulk_calls=r.bulk_calls, single_calls=r.single_calls, bulk_unmatched=r.unmatched, credits=r.credits,
        credits_per_account=round(r.credits / max(1, t.accounts), 3), facts=r.facts, budget=room.as_dict(),
        errors=r.errors[:20],
    )
    return _done(ctx, summary)
