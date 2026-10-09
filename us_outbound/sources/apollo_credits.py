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
The rows are written by us_outbound/ledger.py, as every paid call's are (9 Oct 2026): a request whose cost is
known only from its answer (a search page, 1 credit with results; apollo_enrich, 1 credit per company found) is
reserved before it is made, at the most it can cost, and settled after it (ledger.charge), so a request cut off
still counts, as pick_contacts does for its reveals.
"""

from __future__ import annotations

from dataclasses import dataclass

from us_outbound import budget
from us_outbound.clients.apollo import credits_left
from us_outbound.context import Context
from us_outbound.logs import log

SYSTEM = "apollo"


def floor_reason(ctx: Context) -> str | None:
    """Why no Apollo credit may be spent now (the balance is below apollo_floor), or None.

    An unknown balance (credits_left gives None; lead_credit's has been read every run since 2 Oct 2026)
    does not stop the job: the monthly budget in credit_ledger still holds it.
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

