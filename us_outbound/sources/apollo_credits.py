"""Apollo credits for the source jobs (source_universe, apollo_signals, apollo_enrich): SPEC 1.6 and 12, budget.py.

Before a batch each job:
  * checks Apollo's own balance (credit_usage, 0 credits) against apollo_floor: below it, no
    credit is spent (SPEC 12 kill rules: "Apollo credits fall below apollo_floor");
  * works out today's room: its share of apollo_monthly_credits, paced by the weekday, and never
    more than what is left of the whole Apollo budget today (budget.room_today), so pick_contacts
    at 05:30 still has the rest of the day's credits for email reveals.
The shares (2 Oct 2026): source_universe 25% (SOURCING_SHARE), apollo_signals 25% (SIGNALS_SHARE),
apollo_enrich 15% (ENRICH_SHARE), 65% in all. The 35% left is for pick_contacts' email reveals, which
spend from the whole budget after the sources have run, so they keep at least that much of each day's
allowance even when every source spends its full share.
Every paid request goes into credit_ledger as it is made, with the job, run and a note, in dry-run
too: the read happened, so its credits were spent (SPEC 1.6; docs/pipeline.md "Budgets and targets").
A request whose cost is known only from its answer (apollo_enrich: 1 credit per company found) is
reserved before it is made, at the most it can cost, and settled after it (reserve, settle), so a
request cut off still counts, as pick_contacts does for its reveals.
"""

from __future__ import annotations

from dataclasses import dataclass

from us_outbound import budget
from us_outbound.clients.apollo import credits_left
from us_outbound.clients.db import new_id
from us_outbound.context import Context
from us_outbound.logs import log

SYSTEM = "apollo"


def floor_reason(ctx: Context) -> str | None:
    """Why no Apollo credit may be spent now (the balance is below apollo_floor), or None.

    An unknown balance (credits_left gives None: PHASE0-CONFIRM the credit type) does not stop
    the job: the monthly budget in credit_ledger still holds it.
    """
    left = credits_left(ctx.clients.apollo.credit_usage())
    floor = ctx.settings.general.apollo_floor
    if left is None:
        log("apollo_balance_unknown")
        return None
    if left < floor:
        return f"Apollo has {left:,.0f} credits left, below apollo_floor ({floor:,})"
    return None


@dataclass
class Room:
    """What a job may still spend today; spend() takes each request's credits off."""

    left: float
    whole: budget.Budget
    part: budget.Budget
    spent: float = 0.0

    def allows(self, credits: float = 1.0) -> bool:
        return self.left - self.spent >= credits

    def whole_credits(self) -> int:
        """Whole credits still left today: the most a request charged per company may ask for."""
        return max(0, int(self.left - self.spent + 1e-9))

    def spend(self, credits: float) -> None:
        self.spent += credits

    def as_dict(self) -> dict:
        return {"today": round(self.left, 2), "spent": self.spent, "share": self.part.as_dict(),
                "apollo_month": self.whole.describe()}


def room(ctx: Context, job: str, share: float) -> Room:
    left, whole, part = budget.room_today(ctx.store, ctx.settings, SYSTEM, ctx.now, jobs=(job,), share=share)
    return Room(left, whole, part)


def _entry(ctx: Context, job: str, credits: float, note: str, account_id: str | None) -> dict:
    return {"entry_id": new_id(), "system": SYSTEM, "job": job, "run_id": ctx.run_id, "account_id": account_id,
            "credits": float(credits), "usd": 0.0, "occurred_at": ctx.now, "note": note}


def record(ctx: Context, job: str, credits: float, *, note: str, account_id: str | None = None) -> None:
    """One Apollo request in credit_ledger (a 0-credit row too: it carries the job's place in a search)."""
    ctx.store.insert("credit_ledger", [_entry(ctx, job, credits, note, account_id)])


def reserve(ctx: Context, job: str, credits: float, *, note: str, account_id: str | None = None) -> dict:
    """A paid request's credit_ledger row, written before it is made at the most it can cost; settle() it after."""
    entry = _entry(ctx, job, credits, note, account_id)
    ctx.store.insert("credit_ledger", [entry])
    return entry


def settle(ctx: Context, entry: dict, credits: float, *, note: str) -> None:
    """The reserved row at what the request cost."""
    ctx.store.upsert("credit_ledger", [{**entry, "credits": float(credits), "note": note}])
