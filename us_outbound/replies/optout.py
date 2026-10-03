"""Opt-outs: a person asks to hear nothing more from us (SPEC 11 routing, SPEC 13 CAN-SPAM).

Harry, 1 Oct 2026: the opt-out every email carries is Instantly's own unsubscribe link. There are
two ways in, and both are handled the same way, so a later re-contact can never email the person:

  * a click on that link: the lead's status in its campaign becomes unsubscribed, which
    sync_outcomes reads every 15 minutes (replies/outcomes.py);
  * a reply that asks to stop: the unsubscribe class, a negative reply asking to stop, or a reply
    the stop rule catches without a model call (replies/classify.py), found by poll_replies.

opt_out() does three things:
  1. at once, in any mode: the address's hash goes on suppression (reason unsubscribe) and the
     contact is marked suppressed. That is what stops this system (enrol, and any re-contact)
     from emailing them again;
  2. live only: the address goes on the Instantly blocklist, which is the workspace's, so the
     European campaigns honor it too; then HubSpot's opt-out is set if HubSpot already has the
     contact. HubSpot never gets a new record for it (SPEC 1.2);
  3. once both are done, an events row of type unsubscribed records it: "unsubscribed:lead:{id}"
     for a link click, "unsubscribed:{reply email id}" for a reply. Until that row exists the
     opt-out is pending, and each live run tries again (the lead's status is read every run, and a
     reply is read back from Instantly by its id), so a failed call is never the end of it.

Only hashes are logged. SPEC 13: opt-outs are honored the same day.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.logs import hash_email, log
from us_outbound import suppression

REASON = "unsubscribe"
UNSUBSCRIBED = "unsubscribed"  # the events type (SPEC 6; enrol/capacity.py STOP_EVENTS)
DONE, PENDING, DRY_RUN = "done", "pending", "dry_run"
ALREADY_BLOCKED = 409  # PHASE0-CONFIRM: what the blocklist bulk-create answers for an address already on it


def lead_marker(lead_id: str) -> str:
    return f"{UNSUBSCRIBED}:lead:{lead_id}"


def reply_marker(email_id: str) -> str:
    return f"{UNSUBSCRIBED}:{email_id}"


def done_markers(store) -> set[str]:
    """Ids of the opt-outs already recorded everywhere."""
    return {str(e["event_id"]) for e in store.select("events", {"type": UNSUBSCRIBED})}


def _clean(addresses: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(a.strip().lower() for a in addresses if a and "@" in a))


def suppress_now(ctx: Context, addresses: Iterable[str], *, contact: Mapping[str, Any] | None, source: str) -> int:
    """Step 1, in any mode: hashed suppression for each address, and the contact marked suppressed."""
    added = sum(
        suppression.add(ctx.store, email=a, reason=REASON, source=source, now=ctx.now) for a in _clean(addresses)
    )
    if contact and contact.get("contact_id") and not (contact.get("suppressed") and contact.get("suppressed_reason") == REASON):
        ctx.store.update("contacts", {"contact_id": contact["contact_id"]}, {"suppressed": True, "suppressed_reason": REASON})
    return added


def _blocklist(ctx: Context, addresses: list[str]) -> str | None:
    try:
        ctx.clients.instantly.blocklist_add(addresses)
    except ApiError as exc:
        if exc.status != ALREADY_BLOCKED:
            return f"Instantly blocklist: HTTP {exc.status}"
    except ConfigError as exc:
        return f"Instantly blocklist: {exc}"
    return None


def _hubspot(ctx: Context, addresses: list[str]) -> str | None:
    """Opt out in HubSpot each address HubSpot already has a contact for (SPEC 11), and only those."""
    try:
        hs = ctx.clients.hubspot
        for a in addresses:
            found = hs.search_contacts_by_email(a)
            if not found:
                continue
            if all(str((c.get("properties") or {}).get("hs_email_optout") or "").lower() == "true" for c in found):
                continue  # already opted out
            hs.unsubscribe(a)
    except ApiError as exc:
        return f"HubSpot opt-out: HTTP {exc.status}"
    except ConfigError as exc:
        return f"HubSpot opt-out: {exc}"
    return None


def opt_out(
    ctx: Context,
    *,
    marker: str,
    addresses: Iterable[str],
    contact: Mapping[str, Any] | None,
    account_id: str | None,
    mailbox: str | None,
    occurred_at: datetime,
    source: str,
    done: set[str] | None = None,
) -> str:
    """Record one opt-out everywhere it belongs: "done", "pending" (a call failed; the next live run retries) or "dry_run"."""
    addrs = _clean(addresses)
    if not addrs:
        raise ValueError("an opt-out needs an email address")
    suppress_now(ctx, addrs, contact=contact, source=source)
    done = done_markers(ctx.store) if done is None else done
    hashes = [hash_email(a) for a in addrs]
    if marker in done:
        return DONE
    if ctx.dry_run:
        log("opt_out_dry_run", run_id=ctx.run_id, marker=marker, email_sha256=hashes)
        return DRY_RUN
    problems = [p for p in (_blocklist(ctx, addrs), _hubspot(ctx, addrs)) if p]
    if problems:
        log("opt_out_pending", run_id=ctx.run_id, marker=marker, email_sha256=hashes, problems=problems)
        return PENDING
    ctx.store.upsert("events", [{
        "event_id": marker, "contact_id": (contact or {}).get("contact_id"), "account_id": account_id,
        "type": UNSUBSCRIBED, "mailbox": mailbox, "occurred_at": occurred_at,
    }])
    done.add(marker)
    log("opt_out_done", run_id=ctx.run_id, marker=marker, email_sha256=hashes, source=source)
    return DONE
