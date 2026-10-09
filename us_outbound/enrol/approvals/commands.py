"""The command line's send approvals (9 Oct 2026, split from enrol/approvals.py): `us-outbound approvals approve |
reject | industry | redo` take the same paths as Slack, approved_by "cli".
"""

from __future__ import annotations

from typing import Any

from us_outbound import labels
from us_outbound.clients.guard import CLI_APPROVER
from us_outbound.context import Context
from us_outbound.enrol.approvals.add import send
from us_outbound.enrol.approvals.model import ADDING, OPEN, Item, _text, find_item, is_expired, items
from us_outbound.enrol.approvals.checks import eligibility
from us_outbound.enrol.approvals.relabel import reprepare, repropose, set_industry
from us_outbound.enrol.approvals.thread import slack_or_none
from us_outbound.enrol.approvals.transitions import drop_company, drop_contact, withdraw
from us_outbound.logs import log


def _actionable(item: Item) -> None:
    if item.status != OPEN:
        raise ValueError(f"send approval {item.short_id} is {item.status or 'without a status'}; there is nothing to act on")


def approve(ctx: Context, ref: str) -> dict:
    """`approvals approve`: the same re-check, add and close path as a ✅, approved_by "cli"."""
    item = find_item(ctx, ref)
    _actionable(item)
    if is_expired(item.payload, ctx.today_uk()):
        raise ValueError(f"send approval {item.short_id} expired at the end of {item.payload.get('expires_on')}; "
                         "the account goes back to the queue")
    return {"dry_run": ctx.dry_run, **send(ctx, item, by=CLI_APPROVER, via="cli", slack=slack_or_none(ctx))}


def industry_item(ctx: Context, ref: str, text: str) -> dict:
    """`approvals industry ID LABEL`: the same correction as an approver's "industry: LABEL" reply, by "cli". A dry
    run says what would change and changes nothing."""
    item = find_item(ctx, ref)
    _actionable(item)
    ind = labels.resolve(text, ctx.settings)
    if ind is None:
        raise ValueError(f"no Industries label called {text!r}; the labels are:\n{labels.active_list(ctx.settings)}")
    if ctx.dry_run:
        return {"dry_run": True, "item": item.short_id, "company": item.company,
                "from": _text(item.payload.get("industry")), "to": ind.industry,
                "would": "set the label, record it on the Overrides tab and withdraw the card"}
    return {"dry_run": False, "item": item.short_id, "company": item.company,
            **set_industry(ctx, item, ind.industry, by=CLI_APPROVER, via="cli", slack=slack_or_none(ctx))}


REDO_WHY = "posted again with the company's industry label and emails as they are now"


def redo(ctx: Context, ref: str) -> dict:
    """`approvals redo ID|all`: withdraw waiting cards and post each again, made ready under the company's label and
    copy as they are now (Harry, 7 Oct 2026: "bulk reject all of those contacts from today and send them round
    again", after the label check). Each card is made ready first (reprepare: the same contact, sender and slot);
    then the old card is withdrawn (nothing was sent) and the new one posted in its place, its thread naming the card
    it replaces. A company that cannot be made ready now goes back to the queue for the next enrol, or, when it may
    not be emailed at all (disqualified by the label check, suppressed, excluded), is not emailed. A card whose lead
    is being added is left alone. A dry run lists the cards and changes nothing."""
    every = str(ref).strip().lower() == "all"
    todo = items(ctx.store, (OPEN,)) if every else [find_item(ctx, ref)]
    if not every:  # "ALL" too (9 Oct 2026: with no open cards it read todo[0] and failed)
        _actionable(todo[0])
    out: dict[str, Any] = {"dry_run": ctx.dry_run, "cards": len(todo), "posted_again": [], "back_to_queue": [],
                           "not_emailed": [], "left_alone": []}
    if ctx.dry_run:
        out["would"] = [f"{i.short_id} {i.company}" for i in todo]
        return out
    slack = slack_or_none(ctx)
    for item in todo:
        if item.status != OPEN or item.state == ADDING:
            out["left_alone"].append(item.company)
            continue
        p = reprepare(ctx, item)
        why = ""
        if p is None:
            account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
            contact = ctx.store.get("contacts", contact_id=item.contact_id) if item.contact_id else None
            why = eligibility(ctx, item, account, contact) if account and contact else "no longer on file"
        reason = REDO_WHY if p is not None else (why or "to be proposed again with its label and emails as they are now")
        if not withdraw(ctx, item, slack, reason, back_to_queue=not why, via="redo"):
            out["left_alone"].append(item.company)
            continue
        if p is not None:
            new = repropose(ctx, item, p, slack, REDO_WHY)
            out["posted_again"].append(f"{new.short_id} {item.company}")
        else:
            out["not_emailed" if why else "back_to_queue"].append(item.company)
    log("send_approval_redo", run_id=ctx.run_id, cards=len(todo), posted_again=len(out["posted_again"]),
        back_to_queue=len(out["back_to_queue"]), not_emailed=len(out["not_emailed"]))
    return out


def reject_item(ctx: Context, ref: str, what: str) -> dict:
    """`approvals reject --contact | --company`: the same close path as 👤 or 🚫, approved_by "cli"."""
    item = find_item(ctx, ref)
    _actionable(item)
    fn = {"contact": drop_contact, "company": drop_company}[what]
    return {"dry_run": ctx.dry_run, **fn(ctx, item, by=CLI_APPROVER, via="cli", slack=slack_or_none(ctx))}
