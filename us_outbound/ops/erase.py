"""erase --email (SPEC 6 "Retention and erasure"): an erasure request across the database, HubSpot, Instantly and Clay.

The same command honours an Apollo deletion notice (SPEC 2 #10: "Apollo deletion notices honoured within 30 days").
Apollo has no API that lists them, and the jobs only read Apollo (SPEC 1.2), so Harry runs
`us-outbound erase --email <address> --live` within 30 days of each notice (docs/daily.md).

The database (in any mode; dry-run writes to the database, SPEC 0.3):
  * contacts rows with that email (by email_sha256 or email) are deleted;
  * their events keep their counts but lose reply_text; their hitl_items lose payload;
  * anywhere else the address is written, it goes too (Harry, 7 Oct 2026): a reply card of someone else that names
    them (a colleague's reply, a referral) loses its reply text (ops/retention.REPLY_TEXT_KEYS), and its whole
    payload if the address is still in it; a replied event whose text holds the address loses reply_text;
  * raw_clay_contacts rows holding the email are deleted;
  * the email's hash goes on suppression (reason "erasure"), so it is never contacted again.
HubSpot (live only): the contact is GDPR-deleted, if HubSpot has one.
Instantly (live only): every lead with that email in a "US Outbound – " campaign is deleted.
Clay: there is no API for deleting table rows, so the report lists a manual step for Harry.
The manual steps also name what the jobs cannot reach, when there is any: the Slack cards that showed them (the
jobs never delete a Slack message), the escalation email of their reply in escalation_email's inbox, and the note
and task the reply desk added in HubSpot, which GDPR delete leaves on the company.

The report never carries the raw email, only its hash.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.context import UK, Context
from us_outbound.logs import hash_email, log, normalise_email
from us_outbound.ops.retention import REPLY_TEXT_KEYS
from us_outbound.suppression import add as suppress

ERASURE_REASON, ERASURE_SOURCE = "erasure", "erase"
CONTACTS_SQL = (
    "SELECT contact_id, instantly_campaign, instantly_lead_id FROM {schema}.contacts "
    "WHERE lower(trim(email)) = %(email)s"
)
RAW_CONTACTS_SQL = (
    'SELECT "key" FROM {schema}.raw_clay_contacts WHERE strpos(lower(payload::text), %(email)s) > 0'
)
MENTION_ITEMS_SQL = (
    "SELECT item_id, payload, contact_id, slack_ts, created_at, escalated_at FROM {schema}.hitl_items"
    " WHERE strpos(lower(payload::text), %(email)s) > 0"
)
MENTION_EVENTS_SQL = "SELECT event_id, contact_id FROM {schema}.events WHERE strpos(lower(reply_text), %(email)s) > 0"
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


def _mentions(ctx: Context, email: str) -> tuple[list[dict], list[dict]]:
    """(hitl_items whose payload holds the address, events whose reply_text does), whoever they are about."""
    store = ctx.store
    try:
        items = store.query(MENTION_ITEMS_SQL.format(schema=store.schema), {"email": email})
        events = store.query(MENTION_EVENTS_SQL.format(schema=store.schema), {"email": email})
    except NotImplementedError:  # MemoryStore without a handler: scan the tables
        items = [i for i in store.select("hitl_items") if i.get("payload") is not None and email in _text(i["payload"])]
        events = [e for e in store.select("events") if email in str(e.get("reply_text") or "").lower()]
    return items, events


def _text(payload: Any) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False).lower()


def _scrubbed(payload: Mapping[str, Any], email: str) -> dict | None:
    """Someone else's card that names them: its reply text goes, and the whole payload if the address is still there."""
    p = {**payload, **{k: None for k in REPLY_TEXT_KEYS if k in payload}}
    return None if email in _text(p) else p


def _uk(v: Any) -> str:
    t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v)) if v else None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UK).strftime("%a %d %b %Y %H:%M UK") if t else "?"


def _traces(ctx: Context, items: Iterable[Mapping[str, Any]]) -> list[str]:
    """Manual steps for what the jobs cannot reach, from the items about them, read before their payloads go."""
    items = list(items)
    steps = []
    cards = sorted(_uk(i.get("created_at")) for i in items if i.get("slack_ts"))
    if cards:
        steps.append(f"Slack: {len(cards)} card{'s' if len(cards) != 1 else ''} in #us-outbound showed them (posted "
                     f"{', '.join(cards)}): delete each, its thread included, and any daily post quoting their reply. "
                     "The jobs never delete a Slack message.")
    escalated = sorted(_uk(i.get("escalated_at")) for i in items if i.get("escalated_at"))
    if escalated:
        steps.append(f"Inbox: their reply was escalated to {ctx.settings.general.escalation_email} "
                     f"({', '.join(escalated)}): delete that email.")
    hubspot = [(i.get("payload") or {}).get("hubspot") or {} for i in items]
    ids = sorted({f"{kind} {h[key]}" for h in hubspot if isinstance(h, Mapping)
                  for kind, key in (("note", "note_id"), ("task", "task_id")) if h.get(key)})
    if ids:
        steps.append(f"HubSpot: GDPR delete removes the contact, not what the reply desk added to the company: delete "
                     f"{', '.join(ids)} by hand.")
    return steps


def _database(ctx: Context, email: str, sha: str) -> dict:
    store = ctx.store
    found = {c["contact_id"]: c for c in store.select("contacts", {"email_sha256": sha})}
    found.update({c["contact_id"]: c for c in _contacts_by_email(ctx, email)})
    ids = sorted(found)
    own = store.select("hitl_items", {"contact_id": ids}) if ids else []
    items, events_named = _mentions(ctx, email)
    theirs = {str(x) for x in ids}
    others = [i for i in items if str(i.get("contact_id") or "") not in theirs]  # someone else's, naming them
    texts = sorted(str(e["event_id"]) for e in events_named if str(e.get("contact_id") or "") not in theirs)
    traces = _traces(ctx, [*own, *others])
    events = hitl = deleted = 0
    if ids:
        events = store.update("events", {"contact_id": ids}, {"reply_text": None})
        hitl = store.update("hitl_items", {"contact_id": ids}, {"payload": None})
        deleted = store.delete("contacts", {"contact_id": ids})
    for i in others:
        store.update("hitl_items", {"item_id": i["item_id"]}, {"payload": _scrubbed(i["payload"], email)})
    if texts:
        store.update("events", {"event_id": texts}, {"reply_text": None})
    keys = _raw_contact_keys(ctx, email)
    raw = store.delete("raw_clay_contacts", {"key": keys}) if keys else 0
    added = suppress(store, email=email, reason=ERASURE_REASON, source=ERASURE_SOURCE, now=ctx.now)
    return {
        "contacts_deleted": deleted,
        "events_reply_text_cleared": events,
        "hitl_items_cleared": hitl,
        "mentions_cleared": {"hitl_items": len(others), "events_reply_text": len(texts)},
        "raw_clay_contacts_deleted": raw,
        "suppression": "added" if added else "already suppressed",
        "_leads": [(c.get("instantly_campaign"), c.get("instantly_lead_id")) for c in found.values()],
        "_steps": traces,
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
    known, traces = db.pop("_leads"), db.pop("_steps")
    report: dict[str, Any] = {
        "email_sha256": sha,
        "dry_run": ctx.dry_run,
        "database": db,
        "hubspot": _hubspot(ctx, e),
        "instantly": _instantly(ctx, e, known),
        "clay": {"deleted": False, "manual": True},
        "manual_steps": [CLAY_STEP, INSTANTLY_CONFIRM, *traces],
    }
    if ctx.dry_run:
        report["manual_steps"].insert(
            0, "Dry-run: HubSpot and Instantly were not changed. Run `us-outbound erase --email <address> --live` to finish."
        )
    log("erase", **{k: v for k, v in report.items() if k != "manual_steps"}, manual_steps=len(report["manual_steps"]))
    return report
