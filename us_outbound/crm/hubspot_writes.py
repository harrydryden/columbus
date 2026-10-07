"""HubSpot writes (SPEC 1.2, 11): the six us_outbound_* properties, the ids to look up, and the reply writes.

ensure_properties() creates the property group "US Outbound" (internal name us_outbound)
on companies and contacts, then any of the six properties that is missing. It never
changes an existing property: if one of the six names already exists with a different
type, it reports the clash and creates nothing at all. Dry-run creates nothing and says
what it would create.

lookup_pipeline() reads the "Spill 3.0" pipeline, its first stage and Harry's owner id,
for Harry to paste into the General tab. It never writes settings itself.

record_reply() is SPEC 11 "HubSpot writes" for one positive or referral reply item (replies/items.py):
  1. the company by root domain: more than one → nothing is written, the account is held and a
     manual_merge item is opened (it closes itself once HubSpot has one company for the domain);
     none → created with the clean name, the domain and the four company properties;
  2. the contact by email, or created; associated with the company; the two contact properties set;
  3. the reply as a note, and a task for Harry due today;
  4. lifecyclestage = lead, hs_lead_status = CONNECTED (contacts only: companies have hs_lead_status, but with
     no CONNECTED among its options, confirmed 7 Oct 2026) and hubspot_owner_id = Harry, each only where it is empty;
  5. a deal (ensure_deal) when the reply asks for a demo; hubspot_readback does the same for bookings.
Only the six properties, the identity fields of a new record (name and domain; email, first and
last name and job title) and the three empty-only fields are ever sent; the guard refuses anything
else (clients/guard.py). Each step's result is kept on the item (payload.hubspot) as it is made,
so a re-run carries on where the last stopped rather than writing twice; found records are looked
up by the ids stored on accounts and contacts first. Dry-run reads, works out every write and
sends none (the guard skips them), and stores nothing.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

from us_outbound.clients.guard import HUBSPOT_PROPERTY_GROUP, HUBSPOT_SIX_PROPS, US_CAMPAIGN_PREFIX, WARM_REPLY_CLASSES
from us_outbound.clients.hubspot import COMPANY_PROPS, CONTACT_PROPS, DEAL_PROPS
from us_outbound.context import UK, Context
from us_outbound.logs import clip, log
from us_outbound.replies.items import ReplyItem, save_payload
from us_outbound.settings.model import TIERS

# SPEC 11 classification classes, in plain English for the dropdown.
REPLY_CLASSES: dict[str, str] = {
    "positive": "Positive",
    "referral": "Referral",
    "objection": "Objection",
    "not_now": "Not now",
    "negative": "Negative",
    "out_of_office": "Out of office",
    "wrong_person": "Wrong person",
    "unsubscribe": "Unsubscribe",
    "other": "Other",
}


def _options(values: Mapping[str, str]) -> list[dict]:
    return [{"label": label, "value": value, "displayOrder": i, "hidden": False} for i, (value, label) in enumerate(values.items())]


def _spec(name: str, label: str, description: str, type_: str, field_type: str, **extra: Any) -> dict:
    return {
        "name": name,
        "label": label,
        "description": description,
        "groupName": HUBSPOT_PROPERTY_GROUP,
        "type": type_,
        "fieldType": field_type,
        "formField": False,
        **extra,
    }


# (object type, property spec) for the six properties of SPEC 11, in the group "US Outbound".
PROPERTY_SPECS: tuple[tuple[str, dict], ...] = (
    (
        "companies",
        _spec(
            "us_outbound_account_id", "US Outbound account id",
            "The account's id in the US Outbound system (its database, us_outbound.accounts). Set by the jobs.",
            "string", "text", hasUniqueValue=True,
        ),
    ),
    (
        "companies",
        _spec(
            "us_outbound_tier", "US Outbound tier", "The account's tier when it replied. Set by the jobs.",
            "enumeration", "select", options=_options({t: t for t in TIERS}),
        ),
    ),
    (
        "companies",
        _spec(
            "us_outbound_industry_group", "US Outbound industry group",
            "The website industry group the account was found under. Set by the jobs.", "string", "text",
        ),
    ),
    (
        "companies",
        _spec(
            "us_outbound_top_signals", "US Outbound top signals",
            "The signals that scored the account, with their evidence. Set by the jobs.", "string", "textarea",
        ),
    ),
    (
        "contacts",
        _spec(
            "us_outbound_angle", "US Outbound angle",
            "The angle and copy version this contact was sent. Set by the jobs.", "string", "text",
        ),
    ),
    (
        "contacts",
        _spec(
            "us_outbound_reply_class", "US Outbound reply class",
            "How the contact's reply was classified. Set by the jobs.", "enumeration", "select",
            options=_options(REPLY_CLASSES),
        ),
    ),
)
assert {s["name"] for _, s in PROPERTY_SPECS} == HUBSPOT_SIX_PROPS
OBJECT_TYPES = ("companies", "contacts")


class PropertyClash(Exception):
    """One of the six names already exists on HubSpot with a different type. Nothing was created."""


def ensure_properties(ctx: Context) -> dict:
    """Create the group and any missing us_outbound_* property; never modify an existing one."""
    hs = ctx.clients.hubspot
    existing = {obj: {p.get("name"): p for p in hs.properties(obj)} for obj in OBJECT_TYPES}
    clashes: list[str] = []
    missing: list[tuple[str, dict]] = []
    present: list[str] = []
    notes: list[str] = []
    for obj, spec in PROPERTY_SPECS:
        have = existing[obj].get(spec["name"])
        if have is None:
            missing.append((obj, spec))
            continue
        present.append(spec["name"])
        if have.get("type") != spec["type"]:
            clashes.append(f"{obj} {spec['name']}: exists as type {have.get('type')!r}, we need {spec['type']!r}")
            continue
        if have.get("groupName") != HUBSPOT_PROPERTY_GROUP:
            notes.append(f"{obj} {spec['name']} exists in group {have.get('groupName')!r}; left as it is")
        if spec["type"] == "enumeration":
            want = {o["value"] for o in spec["options"]}
            got = {o.get("value") for o in have.get("options") or []}
            if want - got:
                notes.append(f"{obj} {spec['name']} lacks options {sorted(want - got)}; left as it is (add them by hand)")
    if clashes:
        log("hubspot_setup_clash", clashes=clashes)
        raise PropertyClash("; ".join(clashes) + ". Nothing was created; ask Harry which property to use.")

    for obj in OBJECT_TYPES:
        hs.ensure_property_group(obj)
    for obj, spec in missing:
        hs.create_property(obj, spec)
    names = [s["name"] for _, s in missing]
    summary = {
        "dry_run": ctx.dry_run,
        "group": HUBSPOT_PROPERTY_GROUP,
        ("would_create" if ctx.dry_run else "created"): names,
        "existing": present,
        "notes": notes,
    }
    log("hubspot_setup", **summary)
    return summary


def lookup_pipeline(ctx: Context) -> dict:
    """Ids for the General tab: the "Spill 3.0" pipeline, its first stage and Harry's owner id (reads only)."""
    g = ctx.settings.general
    hs = ctx.clients.hubspot
    found = hs.find_pipeline(g.hubspot_pipeline)
    owner = hs.owner_id_for_email(g.escalation_email)
    ids = {
        "hubspot_pipeline_id": found[0] if found else None,
        "hubspot_deal_stage_id": found[1] if found else None,
        "hubspot_owner_id": owner,
    }
    log("hubspot_lookup", pipeline=g.hubspot_pipeline, **ids)
    return ids


# -- Phase 2: reply writes (SPEC 11 "HubSpot writes", positive or referral replies only) ----------

HUBSPOT_PORTAL_ID = "8481055"  # SPEC 4, for record links in Slack, escalation emails and tasks
LIFECYCLE_LEAD = "lead"  # docs/phase0-facts.md: a valid lifecyclestage option
LEAD_STATUS_CONNECTED = "CONNECTED"  # docs/phase0-facts.md: a valid hs_lead_status option
MANUAL_MERGE = "manual_merge"  # the hitl_items kind for a domain with more than one HubSpot company
DEAL_EVENT = "hs-deal:"  # events.event_id of a deal: the HubSpot object id, prefixed by its type
TOP_SIGNALS = 5
TOP_SIGNALS_CHARS = 1000
EVIDENCE_CHARS = 120
NOTE_CHARS = 5000
# accounts.status, in the order an account moves through it (sql/ddl/01); it never moves back.
STATUS_ORDER = ("new", "queued", "verified", "enrolled", "engaged", "demo_requested", "demo_booked")
_RECORD_TYPE = {"companies": "0-2", "contacts": "0-1", "deals": "0-3"}


def record_link(object_type: str, record_id: str) -> str:
    """The record's page in HubSpot portal 8481055."""
    return f"https://app.hubspot.com/contacts/{HUBSPOT_PORTAL_ID}/record/{_RECORD_TYPE[object_type]}/{record_id}"


def _blank(v: Any) -> bool:
    return v is None or str(v).strip() == ""


def _id(result: Any) -> str | None:
    """The id of a created record; None when dry-run skipped the write."""
    if not isinstance(result, Mapping) or result.get("dry_run") or _blank(result.get("id")):
        return None
    return str(result["id"])


def advance_status(ctx: Context, account_id: str, status: str) -> bool:
    """Move the account on to demo_requested or demo_booked, never back; True if it moved."""
    if not account_id or status not in STATUS_ORDER:
        return False
    account = ctx.store.get("accounts", account_id=account_id)
    if account is None:
        return False
    have = str(account.get("status") or "")
    if have in STATUS_ORDER and STATUS_ORDER.index(have) >= STATUS_ORDER.index(status):
        return False
    ctx.store.upsert("accounts", [{"account_id": account_id, "status": status}])
    log("account_status", account_id=account_id, status=status, was=have)
    return True


def top_signals(ctx: Context, account: Mapping[str, Any]) -> str:
    """us_outbound_top_signals: the account's strongest fresh signals, each with its evidence (SPEC 11)."""
    from us_outbound.scoring.score import score_account

    events = ctx.store.select("signal_events", {"account_id": account["account_id"]})
    result = score_account(account, events, ctx.settings, ctx.today_uk())
    parts = []
    for m in sorted(result.matches, key=lambda m: -m.weight_applied)[:TOP_SIGNALS]:
        ev = m.evidence[0] if m.evidence else None
        what = clip(" ".join((ev.quote or ev.text).split()), EVIDENCE_CHARS) if ev else ""
        parts.append(f"{m.signal.signal} ({m.weight_applied:+d})" + (f": {what}" if what else ""))
    return clip("; ".join(parts), TOP_SIGNALS_CHARS)


def company_properties(ctx: Context, account: Mapping[str, Any]) -> dict[str, str]:
    """The four company properties (SPEC 11), the blank ones left out."""
    props = {
        "us_outbound_account_id": str(account["account_id"]),
        "us_outbound_tier": str(account.get("tier") or "") if account.get("tier") in TIERS else "",
        "us_outbound_industry_group": ctx.settings.industry_group_of(account),
        "us_outbound_top_signals": top_signals(ctx, account),
    }
    return {k: v for k, v in props.items() if not _blank(v)}


def contact_properties(contact: Mapping[str, Any], reply_class: str) -> dict[str, str]:
    """The two contact properties (SPEC 11): the angle with its copy version, and the reply class."""
    angle, version = str(contact.get("angle") or "").strip(), str(contact.get("copy_version") or "").strip()
    props = {
        "us_outbound_angle": f"{angle} ({version})" if angle and version else angle or version,
        "us_outbound_reply_class": reply_class,
    }
    return {k: v for k, v in props.items() if not _blank(v)}


def _changed(have: Mapping[str, Any], want: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in want.items() if str(have.get(k) or "").strip() != v}


def _where_empty(have: Mapping[str, Any], want: Mapping[str, str]) -> dict[str, str]:
    """The empty-only fields (owner, lifecycle, lead status) that are empty now (SPEC 1.2, 11 step 4)."""
    return {k: v for k, v in want.items() if not _blank(v) and _blank(have.get(k))}


def _known(ctx: Context, object_type: str, ids: tuple[Any, ...], props: tuple[str, ...]) -> dict | None:
    hs = ctx.clients.hubspot
    for known in dict.fromkeys(str(i).strip() for i in ids if not _blank(i)):
        found = hs.get_record(object_type, known, props)
        if found is not None:
            return found
    return None


def _company(ctx: Context, account: Mapping[str, Any], reply_class: str, out: dict) -> str | None:
    """The account's HubSpot company id: the stored one, else found by root domain, else created.

    More than one company for the domain sets out["merge_ids"] and returns None (nothing is written).
    """
    hs, g = ctx.clients.hubspot, ctx.settings.general
    ours = company_properties(ctx, account)
    empty_only = {"hubspot_owner_id": g.hubspot_owner_id.strip(), "lifecyclestage": LIFECYCLE_LEAD}
    found = _known(ctx, "companies", (out.get("company_id"), account.get("hubspot_company_id")), COMPANY_PROPS)
    if found is None:
        matches = hs.search_companies_by_domain(str(account["domain"]))
        if len(matches) > 1:
            out["merge_ids"] = sorted(str(m["id"]) for m in matches)
            return None
        found = matches[0] if matches else None
    if found is None:
        props = {"name": str(account.get("clean_name") or account["domain"]), "domain": str(account["domain"]),
                 **ours, **{k: v for k, v in empty_only.items() if v}}
        new = _id(hs.create_company(props, reply_class=reply_class))
        out["company_created"] = True
        return new
    have = found.get("properties") or {}
    update = {**_changed(have, ours), **_where_empty(have, empty_only)}
    if update:
        hs.update_company(str(found["id"]), update, current=have)
    return str(found["id"])


def _contact(ctx: Context, contact: Mapping[str, Any], reply_class: str, out: dict) -> str | None:
    """The contact's HubSpot id: the stored one, else found by email, else created (SPEC 11 step 2)."""
    hs, g = ctx.clients.hubspot, ctx.settings.general
    ours = contact_properties(contact, reply_class)
    empty_only = {"hubspot_owner_id": g.hubspot_owner_id.strip(), "lifecyclestage": LIFECYCLE_LEAD,
                  "hs_lead_status": LEAD_STATUS_CONNECTED}
    email = str(contact.get("email") or "").strip().lower()
    found = _known(ctx, "contacts", (out.get("contact_id"), contact.get("hubspot_contact_id")), CONTACT_PROPS)
    if found is None:
        matches = hs.search_contacts_by_email(email)
        found = matches[0] if matches else None
    if found is None:
        identity = {"email": email, "firstname": contact.get("first_name"), "lastname": contact.get("last_name"),
                    "jobtitle": contact.get("title")}
        props = {**{k: str(v).strip() for k, v in identity.items() if not _blank(v)}, **ours,
                 **{k: v for k, v in empty_only.items() if v}}
        new = _id(hs.create_contact(props, reply_class=reply_class))
        out["contact_created"] = True
        return new
    have = found.get("properties") or {}
    update = {**_changed(have, ours), **_where_empty(have, empty_only)}
    if update:
        hs.update_contact(str(found["id"]), update, current=have)
    return str(found["id"])


def _person(contact: Mapping[str, Any]) -> str:
    name = " ".join(str(contact.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    return name or "the contact"


def note_html(ctx: Context, item: Any, account: Mapping[str, Any], contact: Mapping[str, Any]) -> str:
    """The reply as a HubSpot note (hs_note_body is HTML): who, when, to which mailbox, and the text."""
    reply = ctx.store.get("events", event_id=item.email_id) if item.email_id else None
    text = str((reply or {}).get("reply_text") or item.excerpt or "").strip()
    when = item.received_at.astimezone(UK).strftime("%d %b %Y %H:%M UK") if item.received_at else "recently"
    who = _person(contact) + (f", {contact['title']}" if contact.get("title") else "")
    lines = [
        f"<p><strong>US Outbound: {html.escape(item.reply_class.replace('_', ' '))} reply</strong> from "
        f"{html.escape(who)} at {html.escape(str(account.get('clean_name') or account.get('domain')))}, "
        f"to {html.escape(item.mailbox)}, {when}.</p>",
        "<p>" + html.escape(clip(text, NOTE_CHARS)).replace("\n", "<br>") + "</p>" if text else "",
    ]
    ref = item.referral
    if ref.get("name") or ref.get("email"):
        lines.append("<p>Referred us to: " + html.escape(", ".join(v for v in (ref.get("name"), ref.get("title"),
                                                                               ref.get("email")) if v)) + "</p>")
    return "".join(x for x in lines if x)


def task_text(item: Any, account: Mapping[str, Any], contact: Mapping[str, Any], escalation_email: str) -> tuple[str, str]:
    """(subject, body) of Harry's task: who replied, and how to act (SPEC 11)."""
    company = str(account.get("clean_name") or account.get("domain") or "the account")
    subject = f"US Outbound: {item.reply_class.replace('_', ' ')} reply from {_person(contact)} at {company}"
    body = (
        f"Approve the answer in the #us-outbound thread (react ✅ or reply \"send\"; \"edit: <text>\" changes it), "
        f"or run `us-outbound replies approve {item.short_id} --live`. It goes from {item.mailbox}, signed by "
        f"{item.owner or 'its owner'}. Never reply to the prospect from {escalation_email}."
    )
    if item.referral.get("name") or item.referral.get("email"):
        body += " They referred us to " + ", ".join(v for v in item.referral.values() if v) + "."
    return subject, body


def _open_deal(ctx: Context, account_id: str, company_id: str) -> str | None:
    """An open deal the company already has: ours (by the deal_created events, read by id, so a deal
    created seconds ago counts before search sees it), then any HubSpot finds on the company."""
    hs = ctx.clients.hubspot
    for e in ctx.store.select("events", {"account_id": account_id, "type": "deal_created"}):
        deal_id = str(e.get("event_id") or "").removeprefix(DEAL_EVENT)
        deal = hs.get_record("deals", deal_id, DEAL_PROPS) if deal_id else None
        if deal is not None and str((deal.get("properties") or {}).get("hs_is_closed", "")).lower() != "true":
            return deal_id
    open_deals = hs.open_deals_for_company(company_id)
    return str(open_deals[0]["id"]) if open_deals else None


def record_deal(ctx: Context, account_id: str, contact_id: str | None, deal_id: str, occurred_at: Any = None) -> None:
    """A deal_created event (idempotent on the deal's id)."""
    ctx.store.upsert("events", [{
        "event_id": f"{DEAL_EVENT}{deal_id}", "type": "deal_created", "account_id": account_id,
        "contact_id": contact_id or None, "occurred_at": occurred_at or ctx.now,
    }])


def ensure_deal(ctx: Context, account: Mapping[str, Any], company_id: str, *, contact_id: str | None = None) -> dict:
    """SPEC 11 step 5: one deal per company, "US Outbound – {clean_name}" in Spill 3.0 at its first stage
    ("Demo requested"), owned by Harry, only if the company has no open deal. {deal_id, created, why}."""
    g = ctx.settings.general
    aid = str(account["account_id"])
    existing = _open_deal(ctx, aid, company_id)
    if existing:
        return {"deal_id": existing, "created": False, "why": "the company already has an open deal"}
    missing = [k for k in ("hubspot_pipeline_id", "hubspot_deal_stage_id", "hubspot_owner_id") if _blank(getattr(g, k))]
    if missing:
        return {"deal_id": None, "created": False, "why": f"{', '.join(missing)} not set on the General tab"}
    name = f"{US_CAMPAIGN_PREFIX}{account.get('clean_name') or account.get('domain')}"
    deal_id = _id(ctx.clients.hubspot.create_deal(name, g.hubspot_pipeline_id, g.hubspot_deal_stage_id,
                                                  g.hubspot_owner_id, company_id))
    if deal_id is None:
        return {"deal_id": None, "created": False, "why": "dry-run"}
    record_deal(ctx, aid, contact_id, deal_id)
    log("hubspot_deal", account_id=aid, deal_id=deal_id)
    return {"deal_id": deal_id, "created": True, "why": ""}


def _flag_merge(ctx: Context, account: Mapping[str, Any], company_ids: list[str]) -> None:
    """Open (or re-open) the account's manual_merge item: HubSpot has more than one company for its domain."""
    item_id = f"{MANUAL_MERGE}:{account['account_id']}"
    have = ctx.store.get("hitl_items", item_id=item_id)
    if have is not None and have.get("status") in ("open", "escalated"):
        return
    ctx.store.upsert("hitl_items", [{
        "item_id": item_id, "kind": MANUAL_MERGE, "account_id": str(account["account_id"]), "status": "open",
        "created_at": ctx.now, "handled_at": None, "handled_by": None, "escalated_at": None,
        "payload": {"domain": account.get("domain"), "clean_name": account.get("clean_name"),
                    "hubspot_company_ids": company_ids,
                    "summary": f"HubSpot has {len(company_ids)} companies for {account.get('domain')}; merge them"},
    }])
    log("manual_merge", account_id=account["account_id"], companies=len(company_ids))


def _close_merge(ctx: Context, account_id: str) -> None:
    item_id = f"{MANUAL_MERGE}:{account_id}"
    have = ctx.store.get("hitl_items", item_id=item_id)
    if have is not None and have.get("status") in ("open", "escalated"):
        ctx.store.update("hitl_items", {"item_id": item_id},
                         {"status": "handled", "handled_at": ctx.now, "handled_by": "hubspot"})


def record_reply(ctx: Context, item: Any) -> dict:
    """SPEC 11 HubSpot writes for one reply item (a replies.items.ReplyItem or its hitl_items row).

    Returns the item's hubspot block, also kept on the item (payload.hubspot) as each step is made:
    {status, company_id, contact_id, note_id, task_id, deal_id, link, why}. status is "written",
    "merge_needed" (more than one company for the domain), "skipped" (why: not a warm reply, no
    account or contact) or "dry_run". Calling it again on a written item writes nothing.
    """
    item = item if isinstance(item, ReplyItem) else ReplyItem(item)
    out: dict[str, Any] = dict(item.hubspot)
    if out.get("status") == "written":
        return out
    if item.reply_class not in WARM_REPLY_CLASSES:
        return {**out, "status": "skipped", "why": "only positive or referral replies go to HubSpot (SPEC 1.2)"}
    store = ctx.store
    account = store.get("accounts", account_id=item.account_id) if item.account_id else None
    contact = store.get("contacts", contact_id=item.contact_id) if item.contact_id else None
    if account is None or _blank(account.get("domain")):
        return {**out, "status": "skipped", "why": "the item names no account with a domain"}
    if contact is None or "@" not in str(contact.get("email") or ""):
        return {**out, "status": "skipped", "why": "the item names no contact with an email"}
    hs, g = ctx.clients.hubspot, ctx.settings.general

    def save(status: str | None = None) -> dict:
        if status:
            out["status"] = status
        if ctx.live and item.id:
            save_payload(store, item.id, hubspot=dict(out))
        return out

    out.pop("merge_ids", None)  # worked out again each run: a merge done since lets it carry on
    company_id = _company(ctx, account, item.reply_class, out)
    if out.get("merge_ids"):
        if ctx.live:
            _flag_merge(ctx, account, out["merge_ids"])
        return save("merge_needed")
    if company_id is None:  # dry-run: it would be created
        return {**out, "status": "dry_run", "why": "would create the company, the contact, the note and the task"}
    out.update(company_id=company_id, link=record_link("companies", company_id))
    out.pop("merge_ids", None)
    if ctx.live:
        _close_merge(ctx, str(account["account_id"]))
        if account.get("hubspot_company_id") != company_id:
            store.upsert("accounts", [{"account_id": account["account_id"], "hubspot_company_id": company_id}])
        advance_status(ctx, str(account["account_id"]), "engaged")  # a warm reply (never moves an account back)
    save()

    contact_id = _contact(ctx, contact, item.reply_class, out)
    if contact_id is None:
        return {**out, "status": "dry_run", "why": "would create the contact, the note and the task"}
    out["contact_id"] = contact_id
    if ctx.live and contact.get("hubspot_contact_id") != contact_id:
        store.upsert("contacts", [{"contact_id": contact["contact_id"], "hubspot_contact_id": contact_id}])
    if not out.get("associated"):
        hs.associate("contacts", contact_id, "companies", company_id)
        out["associated"] = True
    save()

    records = [("companies", company_id), ("contacts", contact_id)]
    if not out.get("note_id"):
        note = hs.create_note(note_html(ctx, item, account, contact), records, at=item.received_at or ctx.now)
        out["note_id"] = _id(note) or ("dry-run" if ctx.dry_run else None)
        save()
    if not out.get("task_id"):
        if _blank(g.hubspot_owner_id):
            out["why"] = "no task: hubspot_owner_id is not set on the General tab"
        else:
            subject, body = task_text(item, account, contact, g.escalation_email)
            task = hs.create_task(subject, body, g.hubspot_owner_id, ctx.today_uk(), records)
            out["task_id"] = _id(task) or ("dry-run" if ctx.dry_run else None)
            save()
    if item.demo_requested and not out.get("deal_id"):
        deal = ensure_deal(ctx, account, company_id, contact_id=item.contact_id)
        out["deal_id"] = deal["deal_id"]
        if deal["why"] and not deal["deal_id"]:
            out["why"] = f"no deal: {deal['why']}"
        if ctx.live:
            advance_status(ctx, str(account["account_id"]), "demo_requested")
    if ctx.dry_run:
        return {**out, "status": "dry_run"}
    log("hubspot_reply", item_id=item.id, account_id=item.account_id, company_id=company_id, contact_id=contact_id,
        created_company=bool(out.get("company_created")), created_contact=bool(out.get("contact_created")),
        deal_id=out.get("deal_id"))
    return save("written")
