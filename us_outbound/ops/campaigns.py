"""What the brake and the resume do to the campaigns, and a month's leads removed (9 Oct 2026, the refactoring scan's
phase 6: these lived in the command line, ops/cli.py, which now only parses, runs and prints).

stop() pauses every US Outbound campaign that is sending; start() activates each sender's campaign (over a blackout it
stays paused and the blackout job starts it after), refusing while a campaign's settings have drifted; unenrol()
removes a month's leads. Each is one job run (health.run_job) under the operator's name. A refusal is a ValueError,
which the command line reports in its own words (exit 2).

unenrol (W2-4, 20 Oct 2026; code-platform-ops B6, RS P0-9) is a brake: it took the leads out of Instantly and left
the contacts as if enrolled, so capacity, cohorts and the account stop went on counting leads that were gone. Now each
lead deleted is recorded as the retention job records its deletions: the contact's instantly_lead_id cleared and
lead_deleted_at set, and an events row lead_stopped:{contact_id} with source "unenrolled" (the send forecast frees its
later steps; the account stop leaves it alone). The account keeps status enrolled, with status_reason "unenrolled",
so it is never eligible again without a hand decision (setting it back to verified would have re-enrolled the people
the operator just pulled: RS P0-9). The contact is not suppressed: being pulled is no opt-out. A lead the guard
refuses to delete (not in its stored campaign) is listed under refused and the rest go on; a lead already gone from
Instantly counts as removed. Dry-run counts what it would remove and records nothing.
"""

from __future__ import annotations

import os

from us_outbound.clients.guard import GuardViolation
from us_outbound.context import Context

UNENROLLED = "unenrolled"  # accounts.status_reason, and events.source on the lead_stopped row (unenrol)
STOPPED = "lead_stopped"  # events.type (replies/account_stop.STOPPED; enrol/capacity.STOP_EVENTS)


def operator() -> str:
    """Who ran the command: the shell's user, else the Railway service."""
    return os.environ.get("USER") or os.environ.get("RAILWAY_SERVICE_NAME") or "unknown"


def stop(ctx: Context) -> dict:
    """Pause every US Outbound campaign that is sending (`us-outbound stop`, the brake)."""
    inst = ctx.clients.instantly
    paused, left = [], []
    for c in inst.list_campaigns():
        # 0 draft, 2 paused, 3 completed: nothing is sending.
        if c.get("status") in (0, 2, 3):
            left.append(c["name"])
            continue
        inst.pause_campaign(c["name"])
        paused.append(c["name"])
    return {"dry_run": ctx.dry_run, "enrollment": "stopped", "by": operator(),
            "campaigns_paused": paused, "already_not_sending": left}


def start(ctx: Context) -> dict:
    """Activate each sender's campaign (`us-outbound start`); refused while a campaign's settings have drifted."""
    from us_outbound.registry import blackout
    from us_outbound.registry.mailboxes import campaign_name, ensure_campaigns, held_words, sending_list

    check = ensure_campaigns(ctx, create=False)
    # Drift held while leads are in flight does not refuse: the campaign is consistent for them (Harry, 7 Oct 2026).
    held = check.get("held") or {}
    drift = {n: sorted(set(d) - set((held.get(n) or {}).get("keys") or ())) for n, d in check["drift"].items()}
    if any(drift.values()):
        raise ValueError(
            "campaign settings have drifted: "
            + "; ".join(f"{n}: {', '.join(keys)}" for n, keys in drift.items() if keys)
            + ". Fix with `us-outbound campaigns ensure --fix --live` first."
        )
    inst = ctx.clients.instantly
    found = {c["name"]: c for c in inst.list_campaigns()}
    # Over a blackout the campaigns stay paused, recorded as the blackout's, and the blackout job starts them after
    # it (registry/blackout.py; Harry, 7 Oct 2026): enrollment still resumes.
    over = blackout.hold(ctx.settings, ctx.now)
    started, skipped, waiting = [], [], []
    for owner in ctx.settings.owners():
        name = campaign_name(owner)
        if name not in found or not sending_list(ctx.settings, owner):
            skipped.append(name)
            continue
        if found[name].get("status") != 1 and over is not None:
            blackout.defer(ctx, name, found[name].get("status"), over)
            waiting.append(name)
            continue
        if found[name].get("status") != 1:
            inst.activate_campaign(name)
        started.append(name)
    out = {"dry_run": ctx.dry_run, "enrollment": "resumed" if ctx.live else "still stopped (dry-run)",
           "by": operator(), "campaigns_started": started, "skipped_no_active_mailbox": skipped}
    if waiting:
        out["paused_for_blackout"] = waiting
        out["blackout"] = f"{over.words()}: the blackout job starts them then" if over else ""
    if held:
        out["drift_held"] = [held_words(n, h) for n, h in held.items()]
    return out


def unenrol(ctx: Context, month: str) -> dict:
    """Remove a month's leads from their campaigns (`us-outbound unenrol`), each recorded (the module docstring)."""
    inst = ctx.clients.instantly
    contacts = ctx.store.select("contacts", {"enrolment_month": month})
    removed, refused, without_lead = [], [], 0
    for c in contacts:
        lead_id, campaign = c.get("instantly_lead_id"), c.get("instantly_campaign")
        if not lead_id or not campaign:
            without_lead += 1
            continue
        try:
            inst.delete_lead(str(campaign), str(lead_id))  # the client refuses a non-US Outbound campaign
        except GuardViolation as exc:  # not in its stored campaign: listed, and the rest go on
            refused.append({"contact_id": c["contact_id"], "lead_id": str(lead_id), "why": str(exc)[:160]})
            continue
        if ctx.live:
            record_unenrolled(ctx, c)
        removed.append(str(lead_id))
    return {"dry_run": ctx.dry_run, "month": month, "contacts": len(contacts),
            "leads_removed": len(removed) if ctx.live else 0, "leads_found": len(removed) + len(refused),
            "contacts_without_lead": without_lead, "refused": refused}


def record_unenrolled(ctx: Context, contact: dict) -> None:
    """The contact's lead is gone, recorded (the module docstring): the lead id cleared, lead_deleted_at, a
    lead_stopped row (unless one is there), and the account's status_reason, its status left as it is."""
    cid = contact["contact_id"]
    ctx.store.update("contacts", {"contact_id": cid}, {"instantly_lead_id": None, "lead_deleted_at": ctx.now})
    if ctx.store.get("events", event_id=f"{STOPPED}:{cid}") is None:
        ctx.store.upsert("events", [{"event_id": f"{STOPPED}:{cid}", "type": STOPPED, "contact_id": cid,
                                     "account_id": contact.get("account_id"), "mailbox": contact.get("mailbox"),
                                     "occurred_at": ctx.now, "source": UNENROLLED}])
    if contact.get("account_id"):
        ctx.store.update("accounts", {"account_id": contact["account_id"]}, {"status_reason": UNENROLLED})
