"""What limits today's enrollment, in one place (Harry, 30 Sep 2026).

Today's number is the smallest of three terms (enrol/queue.py):
  * weekly_target:    what is left of weekly_enrol_cap ÷ the send days left this week;
  * sending_capacity: the senders' free slots after follow-ups already due (enrol/capacity.py);
  * ready_accounts:   verified accounts with a sendable email.
The budgets sit behind ready_accounts: Clay credits verify accounts and Apollo credits find
emails, each within its weekly budget (budget.py). When ready_accounts binds, the
explanation says which of them, or which earlier stage, is the reason.

The enrol job records the result in its summary (number_terms, limited_by), so the
heartbeat row keeps it; `us-outbound status` prints the same picture for this week; the
daily post (phase 3) will carry the limited_by line to Slack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from us_outbound import budget
from us_outbound.context import Context
from us_outbound.enrol import capacity, queue

LABELS = {
    "weekly_target": "the weekly target",
    "sending_capacity": "sending capacity",
    "ready_accounts": "ready accounts",
}


@dataclass
class Limits:
    number: int
    terms: dict[str, Any]
    senders: dict[str, capacity.SenderCapacity]
    budgets: dict[str, budget.Budget]
    explanation: str
    detail: list[str] = field(default_factory=list)


def _count(ctx: Context, status: str) -> int:
    return len(ctx.store.select("accounts", {"status": status}))


def today(ctx: Context, day: date, *, ready_accounts: int) -> Limits:
    """Today's number, its terms and why, for the send day `day` (a US Eastern date)."""
    s = ctx.settings
    enrolled_contacts = [c for c in ctx.store.select("contacts") if c.get("enrolled_at")]
    done = budget.enrolled_this_week(enrolled_contacts, ctx.now)
    days_left = budget.send_days_left_in_week(day, s)
    target = budget.weekly_target_today(s, done, days_left)
    senders = capacity.sending_capacity(ctx.store, s, day)
    free = sum(c.free for c in senders.values())
    n, terms = queue.daily_number(weekly_target=target, sending_capacity=free, ready_accounts=ready_accounts)
    budgets = {sys: budget.weekly(ctx.store, s, sys, ctx.now) for sys in ("clay", "apollo")}
    terms.update(
        weekly_enrol_cap=s.general.weekly_enrol_cap,
        enrolled_this_week=done,
        send_days_left_in_week=days_left,
        senders={o: {"cap": c.cap, "free": c.free} for o, c in senders.items()},
        budgets={sys: {"budget": b.budget, "used": b.used, "remaining": b.remaining} for sys, b in budgets.items()},
    )
    explanation, detail = explain(ctx, n, terms, senders, budgets)
    return Limits(n, terms, senders, budgets, explanation, detail)


def explain(
    ctx: Context, n: int, terms: dict[str, Any], senders: dict[str, capacity.SenderCapacity],
    budgets: dict[str, budget.Budget],
) -> tuple[str, list[str]]:
    """(one line naming what holds today's number, the lines behind it)."""
    binding = terms["binding"]
    head = (f"Today: {n}, limited by {LABELS[binding]} "
            f"(weekly target {terms['weekly_target']}, sending capacity {terms['sending_capacity']}, "
            f"ready accounts {terms['ready_accounts']}).")
    detail = [
        f"Weekly target: {terms['enrolled_this_week']} of {terms['weekly_enrol_cap']} enrolled this week "
        f"(Monday to Sunday, UK time), {terms['send_days_left_in_week']} send days left.",
    ]
    if not senders:
        detail.append("Sending capacity: no Active mailbox.")
    detail += [f"Sending capacity, {c.describe()}." for c in senders.values()]
    detail += [b.describe() + "." for b in budgets.values()]
    if binding == "ready_accounts":
        detail.append(behind_ready(ctx, terms["ready_accounts"], budgets))
    elif binding == "sending_capacity" and senders:
        detail.append("More sends need another Active mailbox (`us-outbound mailbox add`), or higher daily caps once the inboxes are warm.")
    return head, detail


def behind_ready(ctx: Context, ready: int, budgets: dict[str, budget.Budget]) -> str:
    """Why there are only `ready` accounts: the first of the stages before enrollment that is short."""
    verified = _count(ctx, "verified")
    queued = _count(ctx, "queued")
    clay, apollo = budgets["clay"], budgets["apollo"]
    no_email = max(0, verified - ready)
    if no_email and apollo.spent:
        return (f"Behind it: {no_email} verified accounts have no email yet and Apollo's weekly budget is used, "
                "so no more emails are looked up until Monday.")
    if no_email:
        return f"Behind it: {no_email} verified accounts are waiting for an email (pick_contacts)."
    if clay.budget <= 0:
        return "Behind it: Clay has no weekly budget (clay_weekly_credits is 0), so no account can be verified."
    if clay.spent and queued:
        return f"Behind it: Clay's weekly budget is used and {queued} accounts are waiting to be verified until Monday."
    if queued:
        return f"Behind it: {queued} accounts are waiting for Clay (verify_in_clay)."
    return ("Behind it: no accounts are waiting for Clay, so the universe or the free checks are the limit "
            "(source_universe, apollo_people).")
