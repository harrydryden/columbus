"""✅: the lead added to the owner's campaign in Instantly (9 Oct 2026, split from enrol/approvals.py).

send re-checks the item, moves it to sending (a compare-and-set, so it is never added twice), adds the one lead and
records the enrollment as enrol does; a lead Instantly leaves out is looked up in the campaign, then approved,
refused, or held while Instantly's plan is full.
"""

from __future__ import annotations

from typing import Any

from us_outbound import labels
from us_outbound.clients import instantly as instantly_client
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import capacity, enrol, openers, plan, render, second
from us_outbound.enrol.approvals.model import (
    ADDING,
    APPROVED,
    APPROVED_EDITED,
    BLOCKED,
    EXPIRED,
    FOLLOW_UP_DAYS,
    HOLD_PLAN,
    OPEN,
    SEED_APPROVE,
    WAITING,
    Item,
    _cas,
    _esc,
    _save,
    _text,
    _uk_time,
    _who,
    is_second,
)
from us_outbound.enrol.approvals.checks import recheck
from us_outbound.enrol.approvals.relabel import label_unfit
from us_outbound.enrol.approvals.thread import _seed, _thread
from us_outbound.enrol.approvals.transitions import _close, _hold, transition, withdraw
from us_outbound.logs import log


def _prepared(ctx: Context, item: Item) -> enrol.Prepared:
    p = item.payload
    account = ctx.store.get("accounts", account_id=item.account_id) or {"account_id": item.account_id}
    contact = ctx.store.get("contacts", contact_id=item.contact_id) or {"contact_id": item.contact_id}
    return enrol.Prepared(
        account=account, contact=contact, owner=_text(p.get("owner")), mailbox=_text(p.get("mailbox")),
        copy_version=_text(p.get("copy_version")), angle=_text(p.get("angle")), test_id=_text(p.get("test_id")),
        lead=dict(p.get("lead") or {}), opener_arm=_text(p.get("opener_arm")) or openers.NONE,
        opener_source=_text(p.get("opener_source")),
        # A card posted before the split (5 Oct 2026) was rendered with the Copy row's subject.
        subject_arm=_text(p.get("subject_arm")) or render.COPY_SUBJECT,
        # The arm the card was rendered in (Harry, 7 Oct 2026), edited or not: the contact records it at ✅.
        test_arm=_text(p.get("test_arm")),
        slot=second.SECOND if is_second(p) else second.FIRST,  # a second contact leaves its account as it is
        # A card posted before the stamp (8 Oct 2026) has none: the contact is left unstamped.
        config_version=_text(p.get("config_version")), code_sha=_text(p.get("code_sha")),
        copy_hash=_text(p.get("copy_hash")),
    )


def send(ctx: Context, item: Item, *, by: str, via: str, slack: Any = None) -> dict:
    """✅: re-check, then add the one lead to the owner's campaign and record the enrollment; close the item."""
    p = item.payload
    outcome = APPROVED_EDITED if p.get("edited") else APPROVED
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "by": by, "via": via}
    account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
    unfit = label_unfit(item, account, ctx.settings)
    if unfit is not None:  # its label was decided again after the card: withdrawn, never sent (labels.py)
        result.update(added=False, outcome=EXPIRED, withdrawn=unfit[0])
        if ctx.dry_run:
            result["dry_run"] = True
        else:
            withdraw(ctx, item, slack, unfit[0], back_to_queue=unfit[1], via=labels.JOB)
        return result
    check = recheck(ctx, item)
    if check.blocks:
        result.update(added=False, outcome=BLOCKED, why=check.blocks)
        if ctx.dry_run:
            result["dry_run"] = True
            return result
        reasons = "; ".join(check.blocks)
        closed = _close(ctx, item, BLOCKED, by, slack, reason=reasons, via=via, status=f"⛔ Not sent: {_esc(reasons)}",
                        note=f"⛔ Not added ({_who(by)}): {_esc(reasons)}. Closed: nothing was sent, and "
                             f"{_esc(item.company)} goes back to the queue if it can still be emailed.")
        if closed and check.exclude:  # HubSpot excludes it now: kept out at the next rescore too
            enrol.mark_excluded(ctx, {"account_id": item.account_id}, *check.exclude)
        return result
    if check.holds:
        result.update(added=False, held=list(check.holds.values()))
        if ctx.dry_run:
            result["dry_run"] = True
            return result
        _hold(ctx, item, check.holds, by=by, via=via, slack=slack)
        return result
    if ctx.dry_run:
        result.update(added=False, dry_run=True, outcome=outcome)
        return result
    campaign = _text(p.get("campaign"))
    previous = item.state
    p["sending"] = {"at": ctx.now.isoformat(), "by": by, "via": via}
    if not transition(ctx, item, ADDING):
        result.update(added=False, why=["another run is handling this item"])
        return result
    try:
        added = ctx.clients.instantly.add_leads(campaign, [p.get("lead") or {}])
    except GuardViolation:  # put back as it was: the one state change not made by transition() (MOVES)
        p.update(state=previous)
        p.pop("sending", None)
        _cas(ctx, item, OPEN)
        raise
    except (ApiError, LookupError, ValueError, ConfigError) as exc:
        if instantly_client.plan_full_error(exc):  # the whole add refused for want of room: held, not failed
            return {**result, **_plan_full(ctx, item, by=by, via=via, slack=slack, alert=plan.full_alert(ctx))}
        why = f"{type(exc).__name__}: {str(exc)[:200]}"
        note = _thread(slack, item, f"Not added: Instantly refused: {_esc(str(exc)[:200])}. Check {_esc(campaign)} in "
                                    f"Instantly for {_esc(item.email)} before you approve it again: ✅ this message, or "
                                    "reply \"send\".")
        _seed(slack, item.channel, note, SEED_APPROVE[:1])
        p.update(approve_ts=note, failed={**p.pop("sending", {}), "error": why})
        transition(ctx, item, WAITING)
        log("send_approval_add_failed", item_id=item.id, error=why)
        result.update(added=False, why=[why])
        return result
    lead = dict(p.get("lead") or {})
    email = _text(lead.get("email")).lower()
    room = plan.after_add(ctx, added)  # the plan-full ask, or the low-room warning (enrol/plan.py)
    ids = enrol._created_ids(added or {}, [lead])
    if 0 not in ids:  # not created: in the campaign after all, or refused (enrol.campaign_lead_ids)
        try:
            found = enrol.campaign_lead_ids(ctx, campaign, [email])
        except (ApiError, LookupError, ConfigError) as exc:  # left "sending": _stuck looks again after STUCK_AFTER
            # Instantly had the add, and did not create it (for want of room, when its plan was full: enrol/plan.py)
            p["sending"] = {**(p.get("sending") or {}), "answered": True, **({"plan_full": True} if room["full"] else {})}
            _save(ctx, item)
            why = f"Instantly did not confirm the lead and {campaign} could not be read ({str(exc)[:160]}); looked up again"
            log("send_approval_lookup_failed", item_id=item.id, error=str(exc)[:200])
            result.update(added=False, why=[why])
            return result
        if email not in found and room["full"]:  # left out for want of room, not refused: held, the contact kept
            return {**result, **_plan_full(ctx, item, by=by, via=via, slack=slack, alert=room.get("alert"))}
        if email not in found:
            return {**result, **_refused(ctx, item, by=by, via=via, slack=slack)}
        ids = {0: found[email]}
    return {**result, **_added(ctx, item, ids[0], by=by, via=via, slack=slack), "instantly_plan": room}


def _plan_full(ctx: Context, item: Item, *, by: str, via: str, slack: Any, alert: Any) -> dict:
    """Instantly's plan has no room (enrol/plan.py): the add did not happen, so the item goes back to waiting, held
    as a stop or a blackout holds it: the card stays open and the ✅ stays valid, each poll_approvals run tries
    again until there is room or the card expires, and the contact is kept (never enrol.mark_not_added)."""
    item.payload.pop("sending", None)
    transition(ctx, item, WAITING)
    _hold(ctx, item, {HOLD_PLAN: plan.HOLD}, by=by, via=via, slack=slack)
    return {"added": False, "held": [plan.HOLD], "plan_full": True, "alert": alert}


def _added(ctx: Context, item: Item, lead_id: str, *, by: str, via: str, slack: Any) -> dict:
    """The lead is in the owner's campaign: record the enrollment as enrol does, and close the item approved."""
    p = item.payload
    edited = bool(p.get("edited"))
    outcome = APPROVED_EDITED if edited else APPROVED
    campaign = _text(p.get("campaign"))
    enrol._record_enrolled(ctx, [_prepared(ctx, item)], {0: lead_id}, campaign, ctx.now_et().date().strftime("%Y-%m"))
    resume = ""
    try:
        if capacity.resume_if_completed(ctx, campaign):
            log("campaign_resumed", campaign=campaign, item_id=item.id)
    except (ApiError, LookupError, ConfigError) as exc:
        log("campaign_resume_failed", campaign=campaign, item_id=item.id, error=str(exc)[:200])
        resume = (f" ⚠️ {_esc(campaign)} is completed in Instantly and could not be resumed, so it won't send until "
                  "someone runs `us-outbound start --live`.")
    at = _uk_time(ctx)
    p["added"] = {"lead_id": lead_id, "at": ctx.now.isoformat(), "by": by, "campaign": campaign}
    first = _esc((p.get("contact") or {}).get("first_name") or "they")
    days = ", ".join(str(d) for d in FOLLOW_UP_DAYS[:-1]) + f" and {FOLLOW_UP_DAYS[-1]}"
    _close(ctx, item, outcome, by, slack, via=via,
           status=f"✅ Approved{' (edited)' if edited else ''} {_who(by)} at {at} UK · added to {_esc(campaign)}",
           note=f"Added to {_esc(campaign)} at {at} UK{' (edited)' if edited else ''}, approved {_who(by)}. Email 1 goes "
                f"out in the next send window, and the follow-ups on days {days} unless {first} replies.{resume}")
    return {"added": True, "outcome": outcome, "campaign": campaign, "at": f"{at} UK"}


def _refused(ctx: Context, item: Item, *, by: str, via: str, slack: Any) -> dict:
    """Instantly did not create the lead and the campaign does not have it: its blocklist, or a lead in another
    campaign of the workspace. Closed as blocked, and the contact marked suppressed (enrol.mark_not_added) so
    pick_contacts finds the next person and a later enrol proposes them."""
    why = enrol.NOT_ADDED
    company = _esc(item.company)
    if _close(ctx, item, BLOCKED, by, slack, reason=why, via=via, status=f"⛔ Not sent: {why}",
              note=f"⛔ Instantly did not add {_esc(item.email)} (in its blocklist, or already in another campaign). "
                   f"Closed: nothing was sent. {_esc(item.person)} won't be proposed again; you'll get a card for the "
                   f"next person at {company}."):
        enrol.mark_not_added(ctx, item.contact_id)
    return {"added": False, "outcome": BLOCKED, "why": [why]}
