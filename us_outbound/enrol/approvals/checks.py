"""What a ✅ finds just before the lead is added (9 Oct 2026, split from enrol/approvals.py): the holds, which
keep the card open and its approval valid, and the blocks, which close it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from us_outbound import budget
from us_outbound.base import holds
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import capacity, enrol, queue, second
from us_outbound.enrol.approvals.model import (
    HOLD_BLACKOUT,
    HOLD_CAMPAIGN,
    HOLD_HUBSPOT,
    HOLD_INSTANTLY,
    HOLD_LIVE,
    HOLD_OPTOUT,
    HOLD_REPLIES,
    HOLD_STOP,
    HOLD_STOP_RULE,
    Item,
    _day,
    _text,
    is_second,
)
from us_outbound.logs import hash_email
from us_outbound.registry import blackout


def eligibility(ctx: Context, item: Item, account: Mapping[str, Any], contact: Mapping[str, Any]) -> str:
    """Why the card's account or contact may no longer be emailed, or "": the enrol run's own check
    (enrol.eligible: suppression, partners, the hand-check's pulls, industries switched off, kill-rule holds
    on industry groups and email sources, the recipient rules), with suppression looked up for this domain
    and email only (enrol.suppressed_for), not the whole table."""
    email = _text(contact.get("email")).lower()
    domains = [_text(account.get("domain")), email.rpartition("@")[2]]
    hashes = [hash_email(email) if "@" in email else "", _text(contact.get("email_sha256"))]
    _, pulled = enrol.hand_check(ctx, ctx.now_et().date())  # the pulls enrol leaves out, as it reads them
    g = enrol.gates(ctx, pulled, enrol.suppressed_for(ctx, domains, hashes))
    why = enrol.account_reason(account, g)
    if why:
        return f"{item.company}: {why}"
    chosen, why = enrol.eligible(account, [contact], g)
    return f"{item.person}: {why}" if chosen is None else ""


@dataclass
class Recheck:
    """What a ✅ finds just before the lead is added (recheck).

    holds: key -> why, for what is temporary. The card stays open and its ✅ stays valid: each
      poll_approvals run tries again, and the lead is added once every hold clears (or the card expires).
    blocks: what is permanent. The card closes as blocked and the account goes back to the queue.
    exclude: (fact, reason) when HubSpot now excludes the account, which is excluded as the card closes.
    """

    holds: dict[str, str] = field(default_factory=dict)
    blocks: list[str] = field(default_factory=list)
    exclude: tuple[str, str] | None = None


def instantly_send_day(ctx: Context) -> date | None:
    """The day Instantly would send email 1 if the lead were added now: its schedule's next weekday (the send
    window's days, in the window's time zone), today if the window has not closed yet. Instantly knows the
    weekdays, not our blackout dates (registry/blackout.instantly_day, which the blackout pause reads too)."""
    return blackout.instantly_day(ctx.settings, ctx.now)


def recheck(ctx: Context, item: Item) -> Recheck:
    """Why the lead may not be added now, just before adding it: the holds (temporary: the ✅ stays valid) and
    the blocks (permanent: the card closes). Nothing in either when it may be added.

    Holds: live_sending no, optout_tested no, an operator stop, the stop rule, the reply pause (a positive reply
    waiting over escalation_hours), Instantly's next send day being a blackout date, the owner's campaign not
    active in Instantly, and Instantly or HubSpot not answering. Approving at the weekend is fine: Instantly
    sends on its next weekday. Blocks: the account or contact gone, no longer verified or in a queue tier, the
    enrol run's own eligibility check (eligibility), the email changed, no Active mailbox for the owner, another
    sender, and a HubSpot exclusion. A second contact's card (enrol/second.py) needs its account enrolled, not
    verified, and the second-contact rules still met: second_contact yes, nobody at the account who replied,
    bounced, unsubscribed or complained, and another role than the first's. Instantly and HubSpot are asked only
    when nothing else holds or blocks.
    """
    s = holds.with_holds(ctx.store, ctx.settings)
    p = item.payload
    out = Recheck()
    if not s.general.live_sending:
        out.holds[HOLD_LIVE] = "live_sending is no"
    if not s.general.optout_tested:
        out.holds[HOLD_OPTOUT] = enrol.OPTOUT_UNTESTED
    for key, why in ((HOLD_STOP, enrol.operator_pause(ctx)), (HOLD_STOP_RULE, holds.enrolment_stop(ctx.store)),
                     (HOLD_REPLIES, enrol.reply_pause(ctx))):
        if why:
            out.holds[key] = why if key != HOLD_REPLIES else f"new emails wait while {why}"
    day = instantly_send_day(ctx)
    if day is not None and budget.is_blackout(day, s):
        out.holds[HOLD_BLACKOUT] = (f"{_day(day)} is a blackout date, and Instantly (which does not know our blackout "
                                    "dates) would send email 1 then")
    account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
    contact = ctx.store.get("contacts", contact_id=item.contact_id) if item.contact_id else None
    if account is None or contact is None:
        out.blocks.append("the account or its contact is no longer on file")
        return out
    company = item.company
    in_queue = account.get("tier") in queue.QUEUE_TIERS
    if is_second(p):  # its account is enrolled: the second-contact rules, as of now (enrol/second.py)
        why = second.recheck(ctx, account, contact) if in_queue else None
        if why:
            out.blocks.append(f"{company}: no second contact now: {why}")
    elif account.get("status") != "verified":
        out.blocks.append(f"{company} is {account.get('status') or 'without a status'} now, not verified")
    if not in_queue:
        reason = _text(account.get("tier_reason"))
        out.blocks.append(f"{company} is {account.get('tier') or 'untiered'} now" + (f" ({reason})" if reason else ""))
    why = eligibility(ctx, item, account, contact)
    if why:
        out.blocks.append(why)
    elif _text(contact.get("email")).lower() != item.email.lower():
        out.blocks.append(f"{item.person}'s email has changed since the card was posted")
    owner = _text(p.get("owner"))
    if not s.mailboxes_for(owner, "Active"):
        out.blocks.append(f"{owner} has no Active mailbox now")
    sender = _text(account.get("sender"))
    if sender and sender != owner:
        out.blocks.append(f"{company}'s sender is {sender} now, not {owner}")
    if out.holds or out.blocks:
        return out
    try:
        why = capacity.campaign_problem(owner, ctx.clients.instantly.list_campaigns(),
                                        blackout.words(ctx.store, ctx.settings, ctx.now))
    except (ApiError, ConfigError, LookupError) as exc:
        out.holds[HOLD_INSTANTLY] = f"Instantly could not be read ({type(exc).__name__}: {str(exc)[:160]})"
        return out
    if why:
        out.holds[HOLD_CAMPAIGN] = why
        return out
    try:
        excluded = enrol.hubspot_block(ctx, account, contact)
    except (ApiError, ConfigError) as exc:
        out.holds[HOLD_HUBSPOT] = f"HubSpot could not be read for the re-check ({str(exc)[:160]})"
        return out
    if excluded:
        out.exclude = excluded
        out.blocks.append(f"{company}: {excluded[1]}")
    return out
