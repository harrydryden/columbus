"""What limits today's enrollment, in one place (Harry, 30 Sep 2026).

Today's number is the smallest of three terms (enrol/queue.py):
  * weekly_target:    what is left of weekly_enrol_cap ÷ the send days left this week;
  * sending_capacity: the senders' free slots after follow-ups already due (enrol/capacity.py),
                      each mailbox at its place on the sending ramp (registry/ramp.py);
  * ready_accounts:   verified accounts with a sendable email.
Send approvals still waiting in Slack (enrol/approvals.py; Harry, 2 Oct 2026) count as enrolled
this week for the weekly target, and each takes one of its sender's slots today.
The budgets sit behind ready_accounts: Clay credits verify accounts and Apollo credits find
emails, each within its monthly budget and today's share of it (budget.py). While the General key
clay_verification is skip (Harry, 1 Oct 2026), verify_accounts verifies accounts on Apollo data and
HubSpot (weekdays 04:30 UK) and Clay stands behind nothing, so the line names that job instead. When
ready_accounts binds, the explanation says which of them, or which earlier stage, is the reason,
including verified accounts where pick_contacts found no suitable contact (contacts/pick.py).
With General second_contact = yes (enrol/second.py; Harry, 6 Oct 2026), ready_accounts also counts the
second contacts due today: each counts against the week and its sender's slots like any contact, and a
line says how many are ready. Enrol takes them only after the first contacts of new accounts.

The enrol job records the result in its summary (number_terms, limited_by), so the
heartbeat row keeps it; `us-outbound status` prints the same picture for this week; the
daily post (phase 3) will carry the limited_by line to Slack.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from us_outbound import budget
from us_outbound.context import Context
from us_outbound.enrol import capacity, focus, queue
from us_outbound.settings.model import CLAY_REQUIRED

VERIFY_WAITING = ("new", "queued")  # what verify_accounts verifies while clay_verification = skip (verify.py)
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

    @property
    def not_sending(self) -> dict[str, str]:
        """owner -> why their campaign takes no new leads now (read only with campaigns=True)."""
        return {o: c.not_sending for o, c in self.senders.items() if c.not_sending}


def _count(ctx: Context, status: str) -> int:
    return len(ctx.store.select("accounts", {"status": status}))


def today(ctx: Context, day: date, *, ready_accounts: int, pending: Mapping[str, int] | None = None,
          campaigns: bool = False, second_ready: int = 0) -> Limits:
    """Today's number, its terms and why, for the send day `day` (a US Eastern date).

    pending: owner -> send approvals still waiting in Slack (enrol/approvals.waiting); they count as
    enrolled this week and hold their sender's slots.
    campaigns: read each owner's campaign status from Instantly (capacity.campaigns_not_sending; the enrol
    job does). In a live run an owner whose campaign is not active has no capacity today; a dry run says so.
    second_ready: how many of ready_accounts are second contacts (enrol/second.candidates).
    """
    s = ctx.settings
    pending = {o: n for o, n in (pending or {}).items() if n > 0}
    enrolled_contacts = [c for c in ctx.store.select("contacts") if c.get("enrolled_at")]
    done = budget.enrolled_this_week(enrolled_contacts, ctx.now)
    days_left = budget.send_days_left_in_week(day, s)
    target = budget.weekly_target_today(s, done + sum(pending.values()), days_left)
    senders = capacity.sending_capacity(ctx.store, s, day)
    for owner, c in senders.items():
        c.hold(pending.get(owner, 0))
    if campaigns:
        for owner, why in capacity.campaigns_not_sending(ctx, senders).items():
            senders[owner].stop(why, live=ctx.live)
    free = sum(c.free for c in senders.values())
    n, terms = queue.daily_number(weekly_target=target, sending_capacity=free, ready_accounts=ready_accounts)
    budgets = {sys: budget.monthly(ctx.store, s, sys, ctx.now) for sys in ("clay", "apollo")}
    terms.update(
        weekly_enrol_cap=s.general.weekly_enrol_cap,
        enrolled_this_week=done,
        awaiting_approval=sum(pending.values()),
        send_days_left_in_week=days_left,
        senders={o: {"cap": c.cap, "free": c.free, **({"pending": c.pending} if c.pending else {}),
                     **({"not_sending": c.not_sending} if c.not_sending else {})}
                 for o, c in senders.items()},
        budgets={sys: b.as_dict() for sys, b in budgets.items()},
    )
    if s.general.second_contact:
        terms["second_contacts_ready"] = second_ready
    explanation, detail = explain(ctx, n, terms, senders, budgets)
    if pending:
        each = ", ".join(f"{o} {k}" for o, k in sorted(pending.items()))
        detail.insert(1, f"Waiting for approval in Slack: {sum(pending.values())} emails ({each}); each counts "
                         "towards the week and holds its sender's slot until it is approved or expires.")
    q = focus.today(ctx, days_left)
    if q.active:
        detail.insert(1, q.describe())
    if s.general.second_contact:
        g = s.general
        detail.append(f"Second contacts: {second_ready} ready (companies of {g.second_contact_min_employees} or more "
                      f"staff, {g.second_contact_delay_days} days after the first person's email 1); they take what "
                      "the first contacts of new companies leave.")
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
    not_sending = list(dict.fromkeys(c.not_sending for c in senders.values() if c.not_sending))
    if not_sending:  # the campaigns Instantly is not sending (capacity.campaigns_not_sending)
        why = " ".join(f"{w[:1].upper()}{w[1:]}." for w in not_sending)
        head += f" {why}" if ctx.live else f" Counted anyway in this dry run, but a live run would not: {why}"
    detail = [
        f"Weekly target: {terms['enrolled_this_week']} of {terms['weekly_enrol_cap']} enrolled this week "
        f"(Monday to Sunday, UK time), {terms['send_days_left_in_week']} send days left.",
    ]
    if not senders:
        detail.append("Sending capacity: no Active mailbox.")
    detail += [f"Sending capacity, {c.describe()}." for c in senders.values()]
    if binding == "ready_accounts":  # accounts waiting for approval are ready too, just not today's
        first = terms["ready_accounts"] - terms.get("second_contacts_ready", 0)  # behind_ready reads verified accounts
        detail.append(behind_ready(ctx, first + terms.get("awaiting_approval", 0), budgets))
    elif binding == "sending_capacity" and senders:
        detail += add_a_mailbox(senders, terms["ready_accounts"] - terms["sending_capacity"])
    return head, detail


def _ramp_line(c: capacity.SenderCapacity) -> str:
    steps = "; ".join(f"{m.address} {m.ramp.describe()}" for m in c.ramping if m.ramp)
    why = f": {c.why_full()}" if c.full else f", so {c.cap} sends a day"
    return (f"{c.owner} is on the sending ramp ({steps}){why}. Capacity rises as the ramp does; "
            "a new mailbox would start its own ramp at 10 a day.")


def add_a_mailbox(senders: dict[str, capacity.SenderCapacity], waiting: int) -> list[str]:
    """When sending capacity is the limit: which senders are full, and what adding a mailbox needs.

    A full sender whose mailboxes are on the sending ramp is held by the ramp, not by a lack of
    mailboxes, so the line says when the ramp lifts it instead (Harry, 1 Oct 2026).
    """
    full = [c for c in senders.values() if c.full and not c.stopped]  # a stopped campaign is not a lack of mailboxes
    if not full:
        ramping = [c for c in senders.values() if c.ramping and not c.stopped]
        if ramping:
            return [_ramp_line(c) for c in ramping]
        if any(c.stopped for c in senders.values()):
            return []  # the head line already says which campaigns to start
        return ["More sends need another Active mailbox (`us-outbound mailbox add`), or higher daily caps once the inboxes are warm."]
    lines = []
    for c in full:
        if c.pending and c.slots > 0 and not c.at_limit:  # held by its own cards, not by a lack of mailboxes
            lines.append(f"{c.owner}'s slots today are held by {c.pending} emails waiting for approval in Slack: "
                         "approve or decline them there (`us-outbound approvals list`).")
            continue
        if c.ramping:
            lines.append(_ramp_line(c))
            continue
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
    if ctx.settings.general.clay_verification != CLAY_REQUIRED:
        # clay_verification = skip (Harry, 1 Oct 2026): verify_accounts verifies new and queued accounts on
        # Apollo data and HubSpot, so neither Clay nor its budget stands behind them.
        waiting = sum(_count(ctx, st) for st in VERIFY_WAITING)
        if waiting:
            return f"Behind it: {waiting} accounts are waiting for verify_accounts (weekdays 04:30 UK)."
        return ("Behind it: no accounts are waiting to be verified, so the universe or the free checks are the limit "
                "(source_universe, apollo_people).")
    if clay.budget <= 0:
        return "Behind it: Clay has no monthly budget (clay_monthly_credits is 0), so no account can be verified."
    if queued and (clay.spent or clay.left_today <= 0):
        return f"Behind it: Clay's budget is used {_until(clay)}, and {queued} accounts are waiting to be verified."
    if queued:
        return f"Behind it: {queued} accounts are waiting for Clay (verify_in_clay)."
    return ("Behind it: no accounts are waiting for Clay, so the universe or the free checks are the limit "
            "(source_universe, apollo_people).")
