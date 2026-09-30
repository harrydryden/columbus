"""erase --email (SPEC 6 "Retention and erasure"): an erasure request across the database, HubSpot, Instantly and Clay.

The database (in any mode; dry-run writes to the database, SPEC 0.3):
  * contacts rows with that email (by email_sha256 or email) are deleted;
  * their events keep their counts but lose reply_text; their hitl_items lose payload;
  * raw_clay_contacts rows holding the email are deleted;
  * the email's hash goes on suppression (reason "erasure"), so it is never contacted again.
HubSpot (live only): the contact is GDPR-deleted, if HubSpot has one.
Instantly (live only): every lead with that email in a "US Outbound – " campaign is deleted.
Clay: there is no API for deleting table rows, so the report lists a manual step for Harry.

The report never carries the raw email, only its hash.
"""

from __future__ import annotations

from typing import Any

from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.context import Context
from us_outbound.logs import hash_email, log, normalise_email
from us_outbound.suppression import add as suppress

ERASURE_REASON, ERASURE_SOURCE = "erasure", "erase"
CONTACTS_SQL = (
    "SELECT contact_id, instantly_campaign, instantly_lead_id FROM {schema}.contacts "
    "WHERE lower(trim(email)) = %(email)s"
)
RAW_CONTACTS_SQL = (
    'SELECT "key" FROM {schema}.raw_clay_contacts WHERE strpos(lower(payload::text), %(email)s) > 0'
)
CLAY_STEP = (
    "Clay: in the \"US Outbound\" folder, delete every row holding the address (the tables of the "
    "US Outbound – Contacts and Accounts functions) within 30 days of the request. Clay has no API for this."
)
INSTANTLY_CONFIRM = (
    "Instantly: check the Unibox holds no thread with the address once its leads are deleted "
    "(PHASE0-CONFIRM whether deleting a lead removes its emails)."
)


def _raw_contact_keys(ctx: Context, email: str) -> list[str]:
    try:
        rows = ctx.store.query(RAW_CONTACTS_SQL.format(schema=ctx.store.schema), {"email": email})
    except NotImplementedError:  # MemoryStore without a handler: scan the table
        rows = [r for r in ctx.store.select("raw_clay_contacts") if email in str(r.get("payload") or "").lower()]
    return sorted({str(r["key"]) for r in rows if r.get("key") is not None})


def _contacts_by_email(ctx: Context, email: str) -> list[dict]:
    """Contacts whose email matches however it was cased or spaced when stored."""
    try:
        rows = ctx.store.query(CONTACTS_SQL.format(schema=ctx.store.schema), {"email": email})
    except NotImplementedError:  # MemoryStore without a handler: scan the table
        rows = [c for c in ctx.store.select("contacts") if normalise_email(str(c.get("email") or "")) == email]
    return rows


def _database(ctx: Context, email: str, sha: str) -> dict:
    store = ctx.store
    found = {c["contact_id"]: c for c in store.select("contacts", {"email_sha256": sha})}
    found.update({c["contact_id"]: c for c in _contacts_by_email(ctx, email)})
    ids = sorted(found)
    events = hitl = deleted = 0
    if ids:
        events = store.update("events", {"contact_id": ids}, {"reply_text": None})
        hitl = store.update("hitl_items", {"contact_id": ids}, {"payload": None})
        deleted = store.delete("contacts", {"contact_id": ids})
    keys = _raw_contact_keys(ctx, email)
    raw = store.delete("raw_clay_contacts", {"key": keys}) if keys else 0
    added = suppress(store, email=email, reason=ERASURE_REASON, source=ERASURE_SOURCE, now=ctx.now)
    return {
        "contacts_deleted": deleted,
        "events_reply_text_cleared": events,
        "hitl_items_cleared": hitl,
        "raw_clay_contacts_deleted": raw,
        "suppression": "added" if added else "already suppressed",
        "_leads": [(c.get("instantly_campaign"), c.get("instantly_lead_id")) for c in found.values()],
    }


def _hubspot(ctx: Context, email: str) -> dict:
    hs = ctx.clients.hubspot
    found = hs.search_contacts_by_email(email)
    if not found:
        return {"contacts_found": 0, "deleted": False}
    hs.gdpr_delete_contact(email)  # the guard allows this only in the erase job (Guard.job)
    return {"contacts_found": len(found), "deleted": ctx.live}


def _instantly(ctx: Context, email: str, known: list[tuple[Any, Any]]) -> dict:
    inst = ctx.clients.instantly
    leads: dict[str, str] = {}  # lead id -> campaign name
    for c in inst.list_campaigns():
        for lead in inst.list_leads(c["name"]):
            if str(lead.get("email") or "").strip().lower() == email and lead.get("id"):
                leads[str(lead["id"])] = c["name"]
    for campaign, lead_id in known:
        if lead_id and campaign and str(campaign).startswith(US_CAMPAIGN_PREFIX):
            leads.setdefault(str(lead_id), str(campaign))
    for lead_id, campaign in sorted(leads.items()):
        inst.delete_lead(campaign, lead_id)
    return {"leads_found": len(leads), "deleted": len(leads) if ctx.live else 0, "campaigns": sorted(set(leads.values()))}


def erase(ctx: Context, email: str) -> dict:
    """Handle one erasure request; returns a per-system report (no raw email in it)."""
    e = normalise_email(email)
    if "@" not in e:
        raise ValueError("erase needs an email address")
    sha = hash_email(e)
    db = _database(ctx, e, sha)
    known = db.pop("_leads")
    report: dict[str, Any] = {
        "email_sha256": sha,
        "dry_run": ctx.dry_run,
        "database": db,
        "hubspot": _hubspot(ctx, e),
        "instantly": _instantly(ctx, e, known),
        "clay": {"deleted": False, "manual": True},
        "manual_steps": [CLAY_STEP, INSTANTLY_CONFIRM],
    }
    if ctx.dry_run:
        report["manual_steps"].insert(
            0, "Dry-run: HubSpot and Instantly were not changed. Run `us-outbound erase --email <address> --live` to finish."
        )
    log("erase", **{k: v for k, v in report.items() if k != "manual_steps"}, manual_steps=len(report["manual_steps"]))
    return report
