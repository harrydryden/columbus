"""What limits today's enrollment, in one place (Harry, 30 Sep 2026).

Today's number is the smallest of three terms (enrol/queue.py):
  * weekly_target:    what is left of weekly_enrol_cap ÷ the send days left this week;
  * sending_capacity: the senders' free slots after follow-ups already due (enrol/capacity.py);
  * ready_accounts:   verified accounts with a sendable email.
The budgets sit behind ready_accounts: Clay credits verify accounts and Apollo credits find
emails, each within its monthly budget and today's share of it (budget.py). When
ready_accounts binds, the explanation says which of them, or which earlier stage, is the reason,
including verified accounts where pick_contacts found no suitable contact (contacts/pick.py).

The enrol job records the result in its summary (number_terms, limited_by), so the
heartbeat row keeps it; `us-outbound status` prints the same picture for this week; the
daily post (phase 3) will carry the limited_by line to Slack.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from us_outbound import budget
from us_outbound.context import Context
from us_outbound.enrol import capacity, focus, queue

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
    detail: list[str] = field(default_factory=list)  # the weekly target, each sender, and what stands behind
    budget_lines: list[str] = field(default_factory=list)  # each credit budget this month

    @property
    def lines(self) -> list[str]:
        """Everything, for the enrol summary and the log."""
        return [*self.detail, *self.budget_lines]


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
    budgets = {sys: budget.monthly(ctx.store, s, sys, ctx.now) for sys in ("clay", "apollo")}
    terms.update(
        weekly_enrol_cap=s.general.weekly_enrol_cap,
        enrolled_this_week=done,
        send_days_left_in_week=days_left,
        senders={o: {"cap": c.cap, "free": c.free} for o, c in senders.items()},
        budgets={sys: b.as_dict() for sys, b in budgets.items()},
    )
    explanation, detail = explain(ctx, n, terms, senders, budgets)
    q = focus.today(ctx, days_left)
    if q.active:
        detail.insert(1, q.describe())
    return Limits(n, terms, senders, budgets, explanation, detail, [b.describe() + "." for b in budgets.values()])


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
    if binding == "ready_accounts":
        detail.append(behind_ready(ctx, terms["ready_accounts"], budgets))
    elif binding == "sending_capacity" and senders:
        detail += add_a_mailbox(senders, terms["ready_accounts"] - terms["sending_capacity"])
    return head, detail


def add_a_mailbox(senders: dict[str, capacity.SenderCapacity], waiting: int) -> list[str]:
    """When sending capacity is the limit: which senders are full, and what adding a mailbox needs."""
    full = [c for c in senders.values() if c.full]
    if not full:
        return ["More sends need another Active mailbox (`us-outbound mailbox add`), or higher daily caps once the inboxes are warm."]
    lines = []
    for c in full:
        lines.append(f"Add a mailbox for {c.owner}: {c.why_full()}. "
                     f"`us-outbound mailbox add <address> --owner \"{c.owner}\" --live`, then it warms for 21 days.")
    if waiting > 0:
        lines.append(f"{waiting} ready accounts are waiting for inbox space.")
    return lines


def _until(b: budget.Budget) -> str:
    """When a spent budget next allows spending: tomorrow (today's share is used) or the 1st."""
    if b.spent:
        nxt = b.month_end + timedelta(days=1)
        return f"until {nxt:%-d %b}"
    return "until tomorrow (today's share of the month is used)"


def no_suitable_contact(ctx: Context) -> str:
    """How many verified accounts pick_contacts found nobody suitable at, and why; "" for none."""
    from us_outbound.contacts import pick  # pick imports enrol, which imports this module

    verified = {a["account_id"] for a in ctx.store.select("accounts", {"status": "verified"})}
    reasons = Counter(r for aid, r in pick.no_contact(ctx.store, ctx.now).items() if aid in verified)
    if not reasons:
        return ""
    return (f"At {sum(reasons.values())} of them pick_contacts found no suitable contact "
            f"(mostly: {reasons.most_common(1)[0][0]}); it tries again after {pick.RETRY_DAYS} days.")


def behind_ready(ctx: Context, ready: int, budgets: dict[str, budget.Budget]) -> str:
    """Why there are only `ready` accounts: the first of the stages before enrollment that is short."""
    verified = _count(ctx, "verified")
    queued = _count(ctx, "queued")
    clay, apollo = budgets["clay"], budgets["apollo"]
    no_email = max(0, verified - ready)
    apollo_used = apollo.spent or apollo.left_today <= 0
    stuck = no_suitable_contact(ctx) if no_email else ""
    if stuck:
        return (f"Behind it: {no_email} verified accounts have no email yet. {stuck}"
                + (f" Apollo's budget is used {_until(apollo)}." if apollo_used else ""))
    if no_email and apollo_used:
        return (f"Behind it: {no_email} verified accounts have no email yet and Apollo's budget is used "
                f"{_until(apollo)}, so no more emails are looked up.")
    if no_email:
        return f"Behind it: {no_email} verified accounts are waiting for an email (pick_contacts)."
    if clay.budget <= 0:
        return "Behind it: Clay has no monthly budget (clay_monthly_credits is 0), so no account can be verified."
    if queued and (clay.spent or clay.left_today <= 0):
        return f"Behind it: Clay's budget is used {_until(clay)}, and {queued} accounts are waiting to be verified."
    if queued:
        return f"Behind it: {queued} accounts are waiting for Clay (verify_in_clay)."
    return ("Behind it: no accounts are waiting for Clay, so the universe or the free checks are the limit "
            "(source_universe, apollo_people).")
