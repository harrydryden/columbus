"""HubSpot writes (SPEC 1.2, 11): the ids to look up, and the reply writes.

lookup_pipeline() reads the "Spill 3.0" pipeline, its first stage and Harry's owner id,
for Harry to paste into the General tab. It never writes settings itself.

record_reply() is SPEC 11 "HubSpot writes" for one positive or referral reply item (replies/items.py):
  1. the company by root domain: more than one → nothing is written, the account is held and a
     manual_merge item is opened (it closes itself once HubSpot has one company for the domain);
     none → created with the clean name and the domain;
  2. the person who replied, by email, or created; associated with the company. That is the emailed
     contact, or the colleague who answered for them (SPEC 2: anyone at the account; W1-2, 19 Oct 2026,
     code-replies-crm M9: until then HubSpot recorded the person we emailed, and the task named them);
  3. the reply as a note, and a task for Harry due today, both naming who replied and, for a colleague,
     who was emailed;
  4. lifecyclestage = lead, hs_lead_status = CONNECTED (contacts only: companies have hs_lead_status, but with
     no CONNECTED among its options, confirmed 7 Oct 2026) and hubspot_owner_id = Harry, each only where it is empty;
  5. a deal (ensure_deal) when the reply asks for a demo; hubspot_readback does the same for bookings.
A warm reply that also asked to be taken off the list (replies/poll.py also_asked_to_stop) is opted out in
HubSpot too once its contact exists there, so a contact created here is never left subscribed; a contact HubSpot
already has opted out (hs_email_optout, as replies/optout.py checks) is not unsubscribed again, so HubSpot answering
an address it has already unsubscribed can never fail the write every run. Its task says to answer by hand: the desk
will not send to a suppressed address, so it does not offer the approval.
Only standard properties are sent: the identity fields of a new record (name and domain; email, first and
last name and job title) and the three empty-only fields; the guard refuses anything else
(clients/guard.py). SPEC 11's six us_outbound_* custom properties are not written (Harry's D9, W1-2): they
were never created in the portal, so every company and contact write failed with a 400, and nothing read
them. Each step's result is kept on the item (payload.hubspot) as it is made, so a re-run carries on where
the last stopped rather than writing twice; found records are looked up by the ids stored on accounts and
contacts first. Dry-run reads, works out every write and sends none (the guard skips them), and stores nothing.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

from us_outbound import fmt
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX, WARM_REPLY_CLASSES
from us_outbound.clients.hubspot import COMPANY_PROPS, CONTACT_PROPS, DEAL_PROPS
from us_outbound.context import Context
from us_outbound.logs import clip, log
from us_outbound.replies.items import ReplyItem, save_payload


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
                 **{k: v for k, v in empty_only.items() if v}}
        new = _id(hs.create_company(props, reply_class=reply_class))
        out["company_created"] = True
        return new
    have = found.get("properties") or {}
    update = _where_empty(have, empty_only)
    if update:
        hs.update_company(str(found["id"]), update, current=have)
    return str(found["id"])


def replier(item: Any, contact: Mapping[str, Any]) -> tuple[dict, bool]:
    """(the person who wrote the reply, True when that is not the emailed contact).

    A colleague's reply (poll_replies' from_is_contact false) carries their address and the name on its From line
    (from_address, from_name); an item from before those were kept, or with no address, falls back to the contact.
    """
    p = item.payload if isinstance(item, ReplyItem) else {}
    address = str(p.get("from_address") or "").strip().lower()
    if p.get("from_is_contact") is not False or "@" not in address or address == str(contact.get("email") or "").lower():
        return dict(contact), False
    first, _, last = " ".join(str(p.get("from_name") or "").split()).partition(" ")
    return {"email": address, "first_name": first, "last_name": last, "title": ""}, True


def _contact(ctx: Context, person: Mapping[str, Any], reply_class: str, out: dict, *, stored: str = "") -> str | None:
    """The replier's HubSpot contact id: the one on the item, the one stored on our contact (stored, only when the
    replier is that contact), else found by email, else created (SPEC 11 step 2)."""
    hs, g = ctx.clients.hubspot, ctx.settings.general
    empty_only = {"hubspot_owner_id": g.hubspot_owner_id.strip(), "lifecyclestage": LIFECYCLE_LEAD,
                  "hs_lead_status": LEAD_STATUS_CONNECTED}
    email = str(person.get("email") or "").strip().lower()
    found = _known(ctx, "contacts", (out.get("contact_id"), stored), CONTACT_PROPS)
    if found is None:
        matches = hs.search_contacts_by_email(email)
        found = matches[0] if matches else None
    if found is None:
        identity = {"email": email, "firstname": person.get("first_name"), "lastname": person.get("last_name"),
                    "jobtitle": person.get("title")}
        props = {**{k: str(v).strip() for k, v in identity.items() if not _blank(v)},
                 **{k: v for k, v in empty_only.items() if v}}
        new = _id(hs.create_contact(props, reply_class=reply_class))
        out["contact_created"] = True
        return new
    have = found.get("properties") or {}
    update = _where_empty(have, empty_only)
    if update:
        hs.update_contact(str(found["id"]), update, current=have)
    return str(found["id"])


def _person(contact: Mapping[str, Any], default: str = "the contact") -> str:
    name = " ".join(str(contact.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    return name or str(contact.get("email") or "").strip() or default


def _titled(contact: Mapping[str, Any]) -> str:
    return _person(contact) + (f", {contact['title']}" if contact.get("title") else "")


def note_html(ctx: Context, item: Any, account: Mapping[str, Any], person: Mapping[str, Any],
              emailed: Mapping[str, Any] | None = None) -> str:
    """The reply as a HubSpot note (hs_note_body is HTML): who, when, to which mailbox, and the text. emailed: the
    contact we emailed, when someone else at the company replied (M9)."""
    reply = ctx.store.get("events", event_id=item.email_id) if item.email_id else None
    text = str((reply or {}).get("reply_text") or item.excerpt or "").strip()
    when = fmt.uk_time(item.received_at, "%d %b %Y %H:%M UK", missing="recently")
    lines = [
        f"<p><strong>US Outbound: {html.escape(item.reply_class.replace('_', ' '))} reply</strong> from "
        f"{html.escape(_titled(person))} at {html.escape(str(account.get('clean_name') or account.get('domain')))}, "
        f"to {html.escape(item.mailbox)}, {when}.</p>",
        f"<p>We emailed {html.escape(_titled(emailed))}; {html.escape(_person(person))} answered for them.</p>"
        if emailed else "",
        "<p>" + html.escape(clip(text, NOTE_CHARS)).replace("\n", "<br>") + "</p>" if text else "",
    ]
    ref = item.referral
    if ref.get("name") or ref.get("email"):
        lines.append("<p>Referred us to: " + html.escape(", ".join(v for v in (ref.get("name"), ref.get("title"),
                                                                               ref.get("email")) if v)) + "</p>")
    return "".join(x for x in lines if x)


def task_text(item: Any, account: Mapping[str, Any], person: Mapping[str, Any], escalation_email: str,
              emailed: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """(subject, body) of Harry's task: who replied (and, for a colleague, who we emailed), and how to act (SPEC 11)."""
    company = str(account.get("clean_name") or account.get("domain") or "the account")
    subject = f"US Outbound: {item.reply_class.replace('_', ' ')} reply from {_person(person)} at {company}"
    body = ""
    if emailed:
        body = f"{_titled(person)} replied for {_titled(emailed)}, the person we emailed. "
    if isinstance(item, ReplyItem) and item.payload.get("also_asked_to_stop"):  # nothing to approve (W1-2 review)
        body += ("They also asked to be taken off the list, so they are suppressed: the desk will not send to them. "
                 f"Answer them by hand from {item.mailbox}, or lift the suppression knowingly. ")
    else:
        body += (
            f"Approve the answer in the #us-outbound thread (react ✅ or reply \"send\"; \"edit: <text>\" changes "
            f"it), or run `us-outbound replies approve {item.short_id} --live`. It goes from {item.mailbox}, signed by "
            f"{item.owner or 'its owner'}. "
        )
    body += f"Never reply to the prospect from {escalation_email}."
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

    person, colleague = replier(item, contact)
    emailed = contact if colleague else None
    contact_id = _contact(ctx, person, item.reply_class, out,
                          stored="" if colleague else str(contact.get("hubspot_contact_id") or ""))
    if contact_id is None:
        return {**out, "status": "dry_run", "why": "would create the contact, the note and the task"}
    out["contact_id"] = contact_id
    if colleague:
        out["replier"] = person["email"]
    elif ctx.live and contact.get("hubspot_contact_id") != contact_id:
        store.upsert("contacts", [{"contact_id": contact["contact_id"], "hubspot_contact_id": contact_id}])
    if not out.get("associated"):
        hs.associate("contacts", contact_id, "companies", company_id)
        out["associated"] = True
    if item.payload.get("also_asked_to_stop") and not out.get("opted_out"):
        # They asked to stop: the contact HubSpot now has is opted out too, unless HubSpot has it opted out already.
        have = hs.get_record("contacts", contact_id, ("hs_email_optout",)) or {}
        if str((have.get("properties") or {}).get("hs_email_optout") or "").lower() != "true":
            hs.unsubscribe(person["email"])
        out["opted_out"] = True
    save()

    records = [("companies", company_id), ("contacts", contact_id)]
    if not out.get("note_id"):
        note = hs.create_note(note_html(ctx, item, account, person, emailed), records, at=item.received_at or ctx.now)
        out["note_id"] = _id(note) or ("dry-run" if ctx.dry_run else None)
        save()
    if not out.get("task_id"):
        if _blank(g.hubspot_owner_id):
            out["why"] = "no task: hubspot_owner_id is not set on the General tab"
        else:
            subject, body = task_text(item, account, person, g.escalation_email, emailed)
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
