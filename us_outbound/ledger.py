"""credit_ledger, written one way: what each paid call to Apollo, Clay or Claude cost (9 Oct 2026, defect 9).

budget.py sums these rows against the monthly budgets, and clients/claude.py against the Claude cap. Every paid
call goes in as it is made, in dry-run too: the read happened, so its credits were spent (SPEC 1.6). Two kinds:

  * record(): a call whose cost is known when the row is written (Claude's usage; a 0-credit row a job keeps its
    place with).
  * charge(): a call whose cost is known only from its answer (a search page is 1 credit with results and 0
    without; a reveal or an enrichment is charged per record found). The row is reserved before the call at the
    most it can cost, and the caller settles it in the block at what the call cost:

        with ledger.charge(ctx, "apollo", JOB, 1.0, note=ledger.reserved_note(what)) as paid:
            body = ctx.clients.apollo.search_organizations(...)
            paid.settle(1.0 if organizations_in(body) else 0.0, note=json.dumps(...))

    A call that fails, is cut off or gives no answer keeps its reservation, so the budget never under-counts.
    Charge.fail() settles a failed call at 0 only when the server refused it unprocessed (an HTTP 4xx answer).

Some jobs read their own rows' notes back as their place in a search (apollo_universe.cursors,
site_visits.quiet_runs and judged_recently). A reservation's note (reserved_note) keeps what it was for under one
key, "reserved", so a call that was never settled moves no cursor.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError

if TYPE_CHECKING:
    from us_outbound.context import Context

TABLE = "credit_ledger"


def entry(system: str, job: str, credits: float, *, run_id: str | None, now: datetime, note: str,
          account_id: str | None = None, usd: float | None = None) -> dict:
    """One credit_ledger row (not written)."""
    return {"entry_id": new_id(), "system": system, "job": job, "run_id": run_id, "account_id": account_id,
            "credits": float(credits), "usd": usd, "occurred_at": now, "note": note}


def record(ctx: Context, system: str, job: str, credits: float, *, note: str, account_id: str | None = None,
           usd: float | None = None) -> dict:
    """A call whose cost is known, written as one row (a 0-credit row too)."""
    row = entry(system, job, credits, run_id=ctx.run_id, now=ctx.now, note=note, account_id=account_id, usd=usd)
    ctx.store.insert(TABLE, [row])
    return row


def reserve(ctx: Context, system: str, job: str, credits: float, *, note: str,
            account_id: str | None = None) -> dict:
    """A paid call's row, written before it is made at the most it can cost; settle() it after."""
    return record(ctx, system, job, credits, note=note, account_id=account_id)


def reserve_each(ctx: Context, system: str, job: str, credits: float, account_ids: Iterable[str], *,
                 note: str) -> dict[str, dict]:
    """account_id -> its reserved row, for one call made for several accounts (one row each, one write)."""
    rows = {aid: entry(system, job, credits, run_id=ctx.run_id, now=ctx.now, note=note, account_id=aid)
            for aid in account_ids}
    ctx.store.insert(TABLE, list(rows.values()))
    return rows


def settle(ctx: Context, reserved: Mapping[str, Any], credits: float, *, note: str) -> dict:
    """The reserved row at what the call cost."""
    return settle_each(ctx, [(reserved, credits, note)])[0]


def settle_each(ctx: Context, settled: Iterable[tuple[Mapping[str, Any], float, str]]) -> list[dict]:
    """Several reserved rows, each at (its cost, its note), in one write."""
    rows = [{**dict(r), "credits": float(credits), "note": note} for r, credits, note in settled]
    if rows:
        ctx.store.upsert(TABLE, rows)
    return rows


def refused(exc: BaseException) -> bool:
    """Whether a failed call surely cost nothing: an HTTP 4xx answer, refused unprocessed. A 5xx, a timeout or a
    dropped connection may come after the work was done, and charged."""
    return isinstance(exc, ApiError) and 400 <= exc.status < 500


def reserved_note(what: Mapping[str, Any]) -> str:
    """A reservation's note: what the call is for, under "reserved", so no reader takes it for a settled call."""
    return json.dumps({"reserved": dict(what)})


def failed_note(what: Mapping[str, Any], exc: BaseException) -> str:
    """A failed call's note: what it was for, still under "reserved", and its HTTP status (0: no answer)."""
    return json.dumps({"reserved": dict(what), "failed": getattr(exc, "status", 0)})


@dataclass
class Charge:
    """One reserved call (charge()). settle() it at what it cost; left unsettled, it keeps its reservation."""

    ctx: Any
    row: dict
    cost: float | None = None  # what it was settled at; None while it is only reserved

    @property
    def reserved(self) -> float:
        return float(self.row["credits"])

    def settle(self, credits: float, *, note: str) -> float:
        settle(self.ctx, self.row, credits, note=note)
        self.cost = float(credits)
        return self.cost

    def keep(self, *, note: str) -> float:
        """Settled at the reservation: a call that may have been charged, with no answer to price it by."""
        return self.settle(self.reserved, note=note)

    def fail(self, exc: BaseException, *, note: str) -> float:
        """A failed call: 0 when it was refused unprocessed (refused()), else the reservation is kept."""
        return self.settle(0.0 if refused(exc) else self.reserved, note=note)


@contextmanager
def charge(ctx: Context, system: str, job: str, max_cost: float, *, note: str, account_id: str | None = None,
           failed_note: str | None = None) -> Iterator[Charge]:
    """Reserve max_cost before a paid call, for the block to settle (Charge). An error out of the block, or a block
    that ends without settling, leaves the reservation standing, noted failed_note when one is given."""
    paid = Charge(ctx, reserve(ctx, system, job, max_cost, note=note, account_id=account_id))
    try:
        yield paid
    except Exception:
        if paid.cost is None and failed_note is not None:
            paid.keep(note=failed_note)
        raise
