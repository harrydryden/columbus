"""The account-level stop (Harry, 6 Oct 2026): when anyone at an account replies, bounces, unsubscribes or
complains, every sequence at the account stops, not only theirs.

With one contact per account this was the contact's own stop. With a second contact (enrol/second.py) an account
can have two leads in its sender's campaign, and today:
  * a reply: Instantly stops the replying lead, and its campaigns stop "for the company" too (stop_for_company,
    clients/instantly.CAMPAIGN_SETTINGS), which covers the colleague's lead. Not covered: a reply Instantly ties
    to no lead, or takes for an auto-reply (stop_on_auto_reply is off). poll_replies flags those on the reply
    item (sequence_may_continue), and the account becomes engaged, so no second contact is proposed after it;
  * a bounce: sync_outcomes suppresses that address and marks that contact; Instantly stops only that lead;
  * an unsubscribe (Instantly's link, or a reply asking to stop): replies/optout.py suppresses that address, puts
    it on the blocklist and opts it out in HubSpot; Instantly stops only that lead;
  * a spam complaint (an events row of type complained; learn/kill_rules.py): nothing stops the colleague.
So sync_outcomes runs this sweep after it has read Instantly, every 15 minutes: for each account with a stop,
each other contact whose lead is in a US Outbound campaign, and that has no stop of its own and was not stopped
here before, is stopped through the guarded Instantly calls: paused (Instantly.set_lead_paused) once phase 0
confirms Instantly's lead pause (clients/instantly.LEAD_PAUSE_CONFIRMED), and until then deleted from its
campaign (Instantly.delete_lead, which first checks the lead is that campaign's), the stop that is certain.
The guard allows either only on a "US Outbound –" campaign and only live. PHASE0-CONFIRM: a deleted lead
leaves Instantly before SPEC 13's 31 days, so sync_outcomes can no longer read a later click on the unsubscribe
link in an email the colleague already has; Instantly's own unsubscribe list should still hold it.

A stop is a replied event with a class other than out_of_office (positive, referral, objection, not_now,
negative, wrong_person, unsubscribe, other), a bounced, unsubscribed or complained event. A reply not yet
classified waits for poll_replies' class, as it may be an out-of-office one. Each stop made is an events row of
type lead_stopped (event_id "lead_stopped:{contact_id}"), so it is made once and the send forecast frees the
lead's later steps (enrol/capacity.STOP_EVENTS); a failed call is tried again by the next run.

Per address (Harry, 6 Oct 2026): an unsubscribe or bounce suppresses only the address it came from. The
colleague's emails stop, but they are not suppressed, not blocklisted and not opted out in HubSpot. The
contact whose own event it was is never touched here: Instantly has stopped their lead already.

With second_contact = no every account has one enrolled contact, so there is never a colleague's lead to stop
and the sweep calls nothing; it still runs, for accounts given a second contact before the switch went off.
Dry-run: it counts what it would stop, calls nothing and writes nothing.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from us_outbound.clients import instantly as instantly_client
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import second
from us_outbound.logs import log

STOPPED = "lead_stopped"  # events.type (enrol/capacity.STOP_EVENTS reads it)
ID_CHUNK = 1000
LIST_LIMIT = 50


def marker(contact_id: str) -> str:
    return f"{STOPPED}:{contact_id}"


def _stop(ctx: Context, campaign: str, lead_id: str) -> str:
    """Stop one lead through the guarded calls: "paused" or "deleted"."""
    inst = ctx.clients.instantly
    if instantly_client.LEAD_PAUSE_CONFIRMED:
        inst.set_lead_paused(campaign, lead_id, True)
        return "paused"
    inst.delete_lead(campaign, lead_id)
    return "deleted"


def to_stop(ctx: Context) -> list[tuple[dict, dict]]:
    """[(contact, the stop that ends their lead)]: other contacts at an account with a stop, whose lead is in a US
    Outbound campaign, with no stop of their own and not stopped here before."""
    store = ctx.store
    events = store.select("events", {"type": list(second.STOP_TYPES)})
    stops = [e for e in events if second.is_stop(e, classified_only=True)]
    if not stops:
        return []
    done = {str(e["event_id"]) for e in store.select("events", {"type": STOPPED})}
    # Whoever replied, bounced, unsubscribed or complained themselves (a reply not classified yet too): Instantly
    # stopped their own lead, which is left as it is.
    own = {str(e.get("contact_id")) for e in events if e.get("contact_id") and second.is_stop(e)}
    by_contact: dict[str, dict] = {}
    contact_ids = sorted(own)
    for i in range(0, len(contact_ids), ID_CHUNK):
        for c in store.select("contacts", {"contact_id": contact_ids[i : i + ID_CHUNK]}):
            by_contact[str(c["contact_id"])] = c
    first_stop: dict[str, dict] = {}
    for e in sorted(stops, key=lambda e: (str(e.get("occurred_at") or ""), str(e.get("event_id")))):
        who = by_contact.get(str(e.get("contact_id"))) or {}
        aid = str(e.get("account_id") or who.get("account_id") or "")
        if aid:
            first_stop.setdefault(aid, e)
    out: list[tuple[dict, dict]] = []
    ids = sorted(first_stop)
    for i in range(0, len(ids), ID_CHUNK):
        for c in store.select("contacts", {"account_id": ids[i : i + ID_CHUNK]}):
            cid = str(c["contact_id"])
            campaign = str(c.get("instantly_campaign") or "")
            if (cid in own or marker(cid) in done or not c.get("instantly_lead_id")
                    or not campaign.startswith(US_CAMPAIGN_PREFIX)):
                continue
            out.append((c, first_stop[str(c["account_id"])]))
    return out


def sweep(ctx: Context) -> dict:
    """Stop the leads of everyone else at an account where someone replied, bounced, unsubscribed or complained."""
    pending = to_stop(ctx)
    out: dict[str, Any] = {"to_stop": len(pending), "stopped": Counter(), "errors": []}
    if ctx.dry_run:
        out["would_stop"] = [{"account_id": c.get("account_id"), "contact_id": c["contact_id"],
                              "because": e.get("type")} for c, e in pending[:LIST_LIMIT]]
        out["stopped"] = {}
        return out
    for c, e in pending:
        cid, campaign, lead_id = str(c["contact_id"]), str(c["instantly_campaign"]), str(c["instantly_lead_id"])
        try:
            how = _stop(ctx, campaign, lead_id)
        except (ApiError, LookupError, ConfigError) as exc:  # tried again by the next run
            if len(out["errors"]) < LIST_LIMIT:
                out["errors"].append(f"{cid}: {type(exc).__name__}: {str(exc)[:160]}")
            log("account_stop_failed", run_id=ctx.run_id, contact_id=cid, error=str(exc)[:200])
            continue
        ctx.store.upsert("events", [{
            "event_id": marker(cid), "contact_id": cid, "account_id": c.get("account_id"), "type": STOPPED,
            "mailbox": c.get("mailbox"), "occurred_at": ctx.now,
        }])
        out["stopped"][how] += 1
        log("account_stop", run_id=ctx.run_id, contact_id=cid, account_id=c.get("account_id"), how=how,
            because=e.get("type"), because_event=e.get("event_id"))
    out["stopped"] = dict(out["stopped"])
    return out

