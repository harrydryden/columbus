"""source_state: the sources' own state, one JSON value per key (W3-3, 20 Oct 2026; code-sources B11, C9).

Until W3-3 three readers parsed credit_ledger notes as job state: source_universe's place in each search
(apollo_universe.cursors), site_visits' run of empty days (quiet_runs) and the visitors it had judged
(judged_recently). A note's shape moved a cursor, and a reservation had to be written under its own key so that no
reader took an unanswered page for a read one. The ledger now records money only; the state lives here:

  cursors:<YYYY-MM>       source_universe: {search key: {"page": the last page read this UK month, "pages": the
                          pages Apollo says the search has, or null}}. A new month has no row, so every search starts
                          again from page 1 (SPEC 7's monthly refresh), as before.
  plan:<YYYY>-W<ww>       sources/allocation.py: the week's pages per state and what is left of them.
  site_visits:list        site_visits: the visitors (row keys) on the last 30-day list read, and when.
  site_visits:quiet_runs  site_visits: {"n": runs in a row with no new visitor, "last_run": when the last ran}.
  site_visits:judged      site_visits: {Apollo organization id: when it was decided on}, pruned to LOOK_AGAIN_DAYS.
  migrated:<what>         the one-off copy of the ledger notes' state (migrate_cursors, migrate_judged), so it runs
                          once: `us-outbound source migrate-state` (dry-run prints what it would copy), or the first
                          run that finds no copy.

Every write is a database write, which dry-run makes too (SPEC 0.3), as the sources' other writes are.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from us_outbound import budget
from us_outbound.clients.db import Range, Store
from us_outbound.context import UK
from us_outbound.timeparse import utc

TABLE = "source_state"
MIGRATED = "migrated:{}"


def get(store: Store, key: str) -> Any:
    """The key's value, or None when it has none."""
    row = store.get(TABLE, key=key)
    return row.get("value") if row else None


def put(store: Store, key: str, value: Any, now: datetime) -> None:
    store.upsert(TABLE, [{"key": key, "value": value, "updated_at": now}])


def cursors_key(now: datetime) -> str:
    """cursors:<YYYY-MM>, the UK month (budget.month_bounds' month)."""
    return f"cursors:{now.astimezone(UK).strftime('%Y-%m')}"


def _note(row: Mapping[str, Any]) -> dict:
    try:
        note = json.loads(row.get("note") or "")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def ledger_cursors(store: Store, system: str, job: str, now: datetime) -> dict[str, dict[str, Any]]:
    """Each search's place this UK month as source_universe's credit_ledger notes gave it before W3-3: a settled
    page's top-level "slice" (a reservation's note keeps what it was for under "reserved", so a page never answered
    moves nothing). A page the ceiling cut short ("cut") counts as the page before it, so it is read again."""
    start, end = budget.month_bounds(now)
    out: dict[str, dict[str, Any]] = {}
    for r in store.select("credit_ledger", {"system": system, "job": job, "occurred_at": Range(start, end)}):
        note = _note(r)
        if not note.get("slice"):
            continue
        c = out.setdefault(str(note["slice"]), {"page": 0, "pages": None})
        page = int(note.get("page") or 0) - (1 if note.get("cut") else 0)
        if page >= c["page"]:
            c["page"], c["pages"] = page, note.get("pages")
    return out


def migrate_cursors(store: Store, system: str, job: str, now: datetime, *, write: bool = True) -> dict[str, Any]:
    """Copy this month's ledger-note cursors into source_state once (RS §2 W3-3: so the month does not restart at
    page 1). A place already in source_state is kept when it is further on. write=False: what it would copy."""
    done_key = MIGRATED.format("cursors:" + cursors_key(now).partition(":")[2])
    found = ledger_cursors(store, system, job, now)
    if get(store, done_key):
        return {"already": True, "cursors": len(found)}
    if write:
        have = dict(get(store, cursors_key(now)) or {})
        for key, c in found.items():
            if int((have.get(key) or {}).get("page") or 0) < c["page"]:
                have[key] = c
        put(store, cursors_key(now), have, now)
        put(store, done_key, {"at": now.isoformat(), "cursors": len(found)}, now)
    return {"already": False, "cursors": len(found), "copied": found}


UNIVERSE_JOB, VISITS_JOB, SYSTEM = "source_universe", "site_visits", "apollo"  # the jobs whose notes held state
JUDGED_DAYS = 30  # site_visits.LOOK_AGAIN_DAYS


def migrate(ctx: Any) -> dict[str, Any]:
    """`us-outbound source migrate-state [--live]` (W3-3; RS §2 W3-3): this month's ledger-note cursors and site_visits'
    judged visitors of the last 30 days copied into source_state, once. Dry-run (the default) prints what it would
    copy and writes nothing; a database write that reaches no prospect, so --live alone. source_universe and
    site_visits make the same copy on their first run if nobody has run this."""
    write = bool(ctx.live)
    cursors = migrate_cursors(ctx.store, SYSTEM, UNIVERSE_JOB, ctx.now, write=write)
    judged = migrate_judged(ctx.store, SYSTEM, VISITS_JOB, ctx.now, JUDGED_DAYS, write=write)
    return {"dry_run": not write, "month": cursors_key(ctx.now).partition(":")[2], "cursors": cursors,
            "judged": judged}


def ledger_judged(store: Store, system: str, job: str, now: datetime, days: int) -> dict[str, str]:
    """{Apollo organization id: when} decided on by site_visits in the last `days`, from its ledger notes ("judged")."""
    out: dict[str, str] = {}
    for r in store.select("credit_ledger", {"system": system, "job": job,
                                            "occurred_at": Range(now - timedelta(days=days), None)}):
        ids = _note(r).get("judged")
        t = utc(r.get("occurred_at"))
        if isinstance(ids, list) and t is not None:
            for i in ids:
                out[str(i)] = max(out.get(str(i), ""), t.isoformat())
    return out


def migrate_judged(store: Store, system: str, job: str, now: datetime, days: int, *,
                   write: bool = True) -> dict[str, Any]:
    """Copy site_visits' judged visitors of the last `days` from the ledger notes once, so none is paid for again."""
    done_key = MIGRATED.format("site_visits:judged")
    found = ledger_judged(store, system, job, now, days)
    if get(store, done_key):
        return {"already": True, "judged": len(found)}
    if write:
        have = dict(get(store, "site_visits:judged") or {})
        for oid, when in found.items():
            have[oid] = max(str(have.get(oid) or ""), when)
        put(store, "site_visits:judged", have, now)
        put(store, done_key, {"at": now.isoformat(), "judged": len(found)}, now)
    return {"already": False, "judged": len(found), "copied": found}
