"""What a ✅ finds just before the lead is added (9 Oct 2026, split from enrol/approvals.py): the holds, which
keep the card open and its approval valid, and the blocks, which close it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from us_outbound import budget, fmt
from us_outbound.base import holds, say
from us_outbound.base.heartbeats import enrolment_paused
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
    HOLD_PLAN,
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


def stop_rule_items(ctx: Context) -> list[tuple[str, str]]:
    """(item id, why) of each stop-rule hold pausing new emails (base/holds.py: pause_enrolment)."""
    out = []
    for r in holds.in_force(ctx.store):
        p = r.get("payload") if isinstance(r.get("payload"), Mapping) else {}
        if p.get("action") == holds.PAUSE_ENROLMENT:
            out.append((_text(r.get("item_id")), _text(p.get("reason") or p.get("rule")) or "a kill rule"))
    return out


def hold_reason(key: str, why: str) -> str:
    """A hold's reason as the thread shows it: its fix is said apart (hold_help), so a reason that carries one
    (Instantly's plan: enrol/plan.py) is cut to the reason."""
    return why.split(" (upgrade the plan", 1)[0] if key == HOLD_PLAN else why


def hold_help(ctx: Context, key: str, why: str = "") -> str:
    """What to do about a hold, or that nothing is needed, for the thread's note (base/say.py: where to do it)."""
    general = say.sheet_link(ctx, "General")
    if key == HOLD_LIVE:
        return (f"nothing, if sending is meant to be off. It goes through once Harry sets `live_sending` to yes on the "
                f"{general}.")
    if key == HOLD_OPTOUT:
        return (f"once the seed-inbox test of Instantly's unsubscribe link is done, set `optout_tested` to yes on the "
                f"{general}.")
    if key == HOLD_STOP:
        return f"when it is safe to send again, run {say.command('start --live')}."
    if key == HOLD_STOP_RULE:
        ids = [i for i, _ in stop_rule_items(ctx)] or ["<item>"]
        return ("after reviewing the contacts and the copy, Harry lifts the pause with "
                + " and ".join(say.command(f"killrules clear {i} --live") for i in ids) + ".")
    if key == HOLD_REPLIES:
        return "approve or skip the positive replies waiting in Slack; this one then goes through."
    if key == HOLD_BLACKOUT:
        return (f"nothing: it goes through once Instantly's next send day is not a blackout date (`blackout_dates` on "
                f"the {general}).")
    if key == HOLD_CAMPAIGN:
        if "blackout" in why:
            return "nothing: the campaign starts again after the blackout, and this goes through then."
        return "fix the campaign as above; this then goes through."
    if key in (HOLD_INSTANTLY, HOLD_HUBSPOT):
        return f"nothing for now: it is tried again every 5 minutes. If this lasts, see {say.logs_link()}."
    if key == HOLD_PLAN:
        return "upgrade the Instantly plan, or delete leads in Instantly that finished their sequence."
    return "nothing: it goes through once this clears."


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
        out.holds[HOLD_LIVE] = "sending is switched off on the General tab (`live_sending` = no)"
    if not s.general.optout_tested:
        out.holds[HOLD_OPTOUT] = ("the test of Instantly's unsubscribe link is not done yet (`optout_tested` = no on "
                                  "the General tab)")
    if enrol.operator_pause(ctx):
        stop = enrolment_paused(ctx.store) or {}
        when = f" ({fmt.uk_time(stop.get('started_at'))})" if stop.get("started_at") else ""
        out.holds[HOLD_STOP] = f"new emails are stopped: someone ran the emergency stop{when}"
    if holds.enrolment_stop(ctx.store):
        out.holds[HOLD_STOP_RULE] = ("new emails are paused because a bounce or complaint limit was hit ("
                                     + "; ".join(r for _, r in stop_rule_items(ctx)) + ")")
    replies = enrol.reply_pause(ctx)
    if replies:
        out.holds[HOLD_REPLIES] = f"new emails wait while {replies}"
    day = instantly_send_day(ctx)
    if day is not None and budget.is_blackout(day, s):
        out.holds[HOLD_BLACKOUT] = (f"Instantly would send email 1 on {_day(day)}, one of our blackout dates (Instantly "
                                    "does not know them)")
    account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
    contact = ctx.store.get("contacts", contact_id=item.contact_id) if item.contact_id else None
    if account is None or contact is None:
        out.blocks.append("the company or the person is no longer in our records")
        return out
    company = item.company
    in_queue = account.get("tier") in queue.QUEUE_TIERS
    if is_second(p):  # its account is enrolled: the second-contact rules, as of now (enrol/second.py)
        why = second.recheck(ctx, account, contact) if in_queue else None
        if why:
            out.blocks.append(f"{company}: no second contact now: {why}")
    elif account.get("status") != "verified":
        out.blocks.append(f"{company} is no longer cleared to email (its status is now "
                          f"{account.get('status') or 'blank'}, not verified)")
    if not in_queue:
        reason = _text(account.get("tier_reason"))
        tier = _text(account.get("tier")) or "without a tier"
        now = reason if reason.startswith(tier) else (f"{tier}: {reason}" if reason else tier)
        out.blocks.append(f"{company} is no longer in the queue to email (it is now {now})")
    why = eligibility(ctx, item, account, contact)
    if why:
        out.blocks.append(why)
    elif _text(contact.get("email")).lower() != item.email.lower():
        out.blocks.append(f"{item.person}'s email address has changed since the card was posted")
    owner = _text(p.get("owner"))
    if not s.mailboxes_for(owner, "Active"):
        out.blocks.append(f"{owner} has no active sending mailbox now (Mailboxes tab)")
    sender = _text(account.get("sender"))
    if sender and sender != owner:
        out.blocks.append(f"{company} is now {sender}'s to email, not {owner}'s")
    if out.holds or out.blocks:
        return out
    try:
        why = capacity.campaign_problem(owner, ctx.clients.instantly.list_campaigns(),
                                        blackout.words(ctx.store, ctx.settings, ctx.now))
    except (ApiError, ConfigError, LookupError) as exc:
        out.holds[HOLD_INSTANTLY] = f"Instantly did not answer ({type(exc).__name__}: {str(exc)[:160]})"
        return out
    if why:
        out.holds[HOLD_CAMPAIGN] = why.replace("`us-outbound ", "`railway ssh -- us-outbound ")
        return out
    try:
        excluded = enrol.hubspot_block(ctx, account, contact)
    except (ApiError, ConfigError) as exc:
        out.holds[HOLD_HUBSPOT] = f"HubSpot did not answer for the last check ({str(exc)[:160]})"
        return out
    if excluded:
        out.exclude = excluded
        out.blocks.append(f"{company}: {excluded[1]}")
    return out
