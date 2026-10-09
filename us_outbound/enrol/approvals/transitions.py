"""How a send approval moves (9 Oct 2026, split from enrol/approvals.py): the one transition() a payload state
changes through, with its table of allowed moves, and the decisions that close or hold an item.

Each payload state has one hitl_items status (STATUS): waiting, rejected and editing are open, sending is sending,
done is handled. transition() writes the new state with its status: a move that changes the status is a
compare-and-set (False when another run moved the item first), one that keeps it a plain write.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from us_outbound.clients.guard import CLI_APPROVER
from us_outbound.context import Context
from us_outbound.enrol import enrol
from us_outbound.enrol.approvals.cards import choices_text
from us_outbound.enrol.approvals.model import (
    ADDING,
    COMPANY_REJECTED,
    CONTACT_REJECTED,
    DONE,
    EDITING,
    EVENT_PREFIX,
    EVENT_TYPE,
    EXPIRED,
    HANDLED,
    KIND,
    OPEN,
    REJECTED,
    SEED_CHOICES,
    SENDING,
    SYSTEM,
    WAITING,
    Item,
    _cas,
    _day,
    _esc,
    _save,
    _text,
    _who,
)
from us_outbound.enrol.approvals.thread import _seed, _thread, _update_card
from us_outbound.logs import log
from us_outbound.scoring.tiers import DECLINED_IN_SLACK
from us_outbound.timeparse import iso_date

# Each payload state's hitl_items status.
STATUS: dict[str, str] = {WAITING: OPEN, REJECTED: OPEN, EDITING: OPEN, ADDING: SENDING, DONE: HANDLED}
# The moves transition() makes: from a payload state, the states it may go to.
#   waiting   -> rejected (❌), editing (✏️), waiting (an edit reply, which needs a fresh ✅), sending (✅), done
#   rejected  -> editing (✏️), waiting (an edit reply), sending (✅, or "send"), done (👤, 🚫, expiry, withdrawal)
#   editing   -> waiting (an accepted edit), sending, done
#   sending   -> waiting (Instantly refused the add, its plan was full, or the add was cut off), done (added, or
#                refused for good)
#   done      -> nothing: a handled item is never moved again
# One move is not made here: a guard refusal during the add puts back whatever state the item had (add.send).
MOVES: dict[str, frozenset[str]] = {
    WAITING: frozenset({WAITING, REJECTED, EDITING, ADDING, DONE}),
    REJECTED: frozenset({WAITING, EDITING, ADDING, DONE}),
    EDITING: frozenset({WAITING, ADDING, DONE}),
    ADDING: frozenset({WAITING, DONE}),
    DONE: frozenset(),
}


def transition(ctx: Context, item: Item, state: str, **values: Any) -> bool:
    """Move the item to payload state `state`, its status with it (STATUS), writing the payload and these columns
    while the item is in the status it was read in. A move off the MOVES table is logged and still made, so a live
    run never stops on one."""
    if state not in MOVES.get(item.state, frozenset()):
        log("send_approval_unexpected_move", item_id=item.id, state=item.state, to=state)
    item.payload["state"] = state
    status = STATUS[state]
    if status == item.status:
        _save(ctx, item, **values)
        return True
    return _cas(ctx, item, status, **values)


# -- the decisions that close or hold an item -----------------------------------------------------------------------


def _event(ctx: Context, item: Item, outcome: str, by: str) -> None:
    ctx.store.upsert("events", [{
        "event_id": f"{EVENT_PREFIX}{item.id}", "type": EVENT_TYPE, "approval": outcome, "approved_by": by,
        "account_id": item.account_id or None, "contact_id": item.contact_id or None, "step": 1,
        "mailbox": _text(item.payload.get("mailbox")) or None, "occurred_at": ctx.now,
    }])


def _close(ctx: Context, item: Item, outcome: str, by: str, slack: Any, *, status: str, note: str,
           reason: str = "", via: str = "") -> bool:
    """Handled, with its outcome, the events row, the card and a thread note. False if another run closed it."""
    item.payload.update(outcome=outcome, reason=reason)
    item.payload["decided"] = {"by": by, "at": ctx.now.isoformat(), "via": via, "outcome": outcome}
    item.payload.pop("sending", None)
    if not transition(ctx, item, DONE, handled_at=ctx.now, handled_by=by):
        return False
    _event(ctx, item, outcome, by)
    _update_card(ctx, slack, item, status)
    _thread(slack, item, note)
    log("send_approval_closed", item_id=item.id, account_id=item.account_id, outcome=outcome, by=by)
    return True


def _hold(ctx: Context, item: Item, why: Mapping[str, str], *, by: str, via: str, slack: Any) -> None:
    """A ✅ that something temporary stops for now: the card stays open and the approval stays valid, so each
    poll_approvals run tries again and adds the lead once it clears, until the card expires. The thread gets
    one note per hold reason (payload.held.noted), never the same one again every 5 minutes."""
    p = item.payload
    held = dict(p.get("held") or {})
    noted = [str(k) for k in held.get("noted") or ()]
    new = [k for k in why if k not in noted]
    p["held"] = {"by": by, "via": via, "since": held.get("since") or ctx.now.isoformat(), "at": ctx.now.isoformat(),
                 "reasons": dict(why), "noted": noted + new}
    _save(ctx, item)
    if new:
        _thread(slack, item, f"⏸ Approved {_who(by)}, but not added yet: {_esc('; '.join(why[k] for k in new))}. "
                             "It goes through by itself once that clears, until the card expires at the end of "
                             f"{_day(iso_date(p.get('expires_on')))} (UK). ❌ still stops it.")
    log("send_approval_held", item_id=item.id, holds=list(why), noted=new)


def reject(ctx: Context, item: Item, *, by: str, via: str, slack: Any) -> None:
    """❌: not sent; the thread offers the three choices, their reactions seeded."""
    p = item.payload
    p.pop("held", None)  # ❌ stops an approval a hold kept
    p.update(approve_ts="", rejected={"by": by, "at": ctx.now.isoformat(), "via": via})
    note = _thread(slack, item, choices_text(p, by))
    _seed(slack, item.channel, note, SEED_CHOICES)
    p["choices_ts"] = note
    transition(ctx, item, REJECTED)
    _update_card(ctx, slack, item, f"❌ Not sent ({_who(by)}): ✏️ edit, 👤 another contact or 🚫 drop the company, in the thread")


def drop_contact(ctx: Context, item: Item, *, by: str, via: str, slack: Any = None) -> dict:
    """👤: not this person. The contact is marked suppressed so pick_contacts finds the next; the account stays."""
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "by": by, "via": via,
                              "outcome": CONTACT_REJECTED}
    if ctx.dry_run:
        return {**result, "dry_run": True, "done": False}
    where = "at the command line" if by == CLI_APPROVER else f"in Slack by {by}"
    reason = f"declined {where} at a send approval ({ctx.today_uk():%-d %b %Y})"
    company = _esc(item.company)
    if not _close(ctx, item, CONTACT_REJECTED, by, slack, via=via,
                  status=f"👤 {_esc(item.person)} declined ({_who(by)}): {company} stays in the queue",
                  note=f"👤 Declined ({_who(by)}): {_esc(item.person)} won't be emailed. {company} stays in the queue: "
                       "pick_contacts looks for the next-ranked person at 05:30 on a send day, and the next enrol "
                       "after that proposes them here."):
        return {**result, "done": False, "why": ["another run is handling this item"]}
    if item.contact_id:
        ctx.store.update("contacts", {"contact_id": item.contact_id}, {"suppressed": True, "suppressed_reason": reason})
    return {**result, "done": True}


def drop_company(ctx: Context, item: Item, *, by: str, via: str, slack: Any = None) -> dict:
    """🚫: drop the company. Excluded with a declined_in_slack fact, so a rescore keeps it out."""
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "by": by, "via": via,
                              "outcome": COMPANY_REJECTED}
    if ctx.dry_run:
        return {**result, "dry_run": True, "done": False}
    where = "at the command line" if by == CLI_APPROVER else f"in Slack by {by}"
    company = _esc(item.company)
    if not _close(ctx, item, COMPANY_REJECTED, by, slack, via=via,
                  status=f"🚫 {company} dropped ({_who(by)}): excluded",
                  note=f"🚫 Dropped ({_who(by)}): {company} is excluded and won't be proposed again."):
        return {**result, "done": False, "why": ["another run is handling this item"]}
    enrol.mark_excluded(ctx, {"account_id": item.account_id}, DECLINED_IN_SLACK,
                        f"dropped {where} at a send approval ({ctx.today_uk():%-d %b %Y})", source=KIND)
    return {**result, "done": True}


def expire(ctx: Context, item: Item, slack: Any) -> bool:
    """Not approved by the end of its next send day: closed by "system", and the account goes back to the queue."""
    last = iso_date(item.payload.get("expires_on"))
    reason = f"not approved by the end of {_day(last)} (UK)"
    held = (item.payload.get("held") or {}).get("reasons") or {}
    if held:  # approved, but a hold never cleared
        reason = f"approved, but still held at the end of {_day(last)} (UK): {'; '.join(held.values())}"
    company = _esc(item.company)
    return _close(ctx, item, EXPIRED, SYSTEM, slack, reason=reason, via="expiry",
                  status=f"⌛ Expired: {reason}; nothing was added and {company} goes back to the queue",
                  note=f"⌛ Expired: {reason}. Nothing was added; {company} goes back to the queue.")


def withdraw(ctx: Context, item: Item, slack: Any, reason: str, *, back_to_queue: bool = True,
             via: str = "relabel", note: str = "") -> bool:
    """Closed by "system" before anyone decides, as an expiry is (`us-outbound relabel`, ops/relabel.py; Harry,
    7 Oct 2026): the card was rendered under a label that turned out wrong. Nothing was added; the company goes
    back to the queue for a card with the right copy, or, when it may not be emailed at all, does not. note: the
    thread's words, when not the plain ones (an approver's correction says what happens next)."""
    company = _esc(item.company)
    after = f"{company} goes back to the queue for a new card" if back_to_queue else f"{company} will not be emailed"
    return _close(ctx, item, EXPIRED, SYSTEM, slack, reason=f"withdrawn: {reason}", via=via,
                  status=f"↩️ Withdrawn: {_esc(reason)}; nothing was added and {after}",
                  note=note or f"↩️ Withdrawn: {_esc(reason)}. Nothing was added; {after}.")
