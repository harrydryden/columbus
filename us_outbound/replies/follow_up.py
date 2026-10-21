"""follow_up, daily at 08:45 UK (W2-4, 20 Oct 2026; code-replies-crm M5): a not-now reply's date kept.

poll_replies puts a follow_up_date on each not-now reply's item: the date they gave, or NOT_NOW_DEFAULT (90 days)
after the reply when they gave none (the draft then asks them when). Until W2-4 nothing read it, so "try me in
January" was answered and then forgotten. sweep() takes each reply item whose follow_up_date has come (today, UK, or
earlier when a run was missed), whatever its status (the reply itself was answered long ago), and:
  * posts one Slack line for it in the alert channel (one post a run, the approvers mentioned: notify.post_once,
    keyed on the item, so it is never posted twice);
  * makes a HubSpot task for Harry due today (hubspot_owner_id), which HubSpot emails him. Only a warm reply's
    company and contact, already in HubSpot, are linked; for any other the task names the item, not the person
    (SPEC 1.2: nothing about a prospect who is not a warm lead goes into HubSpot);
  * records it once, an events row of type follow_up, event_id "follow_up:{item_id}", so the next run passes it by.
    A run whose post and task both failed records nothing, and the next run tries again.
Never for someone who has asked to stop since (they, or the contact, are suppressed now: desk.suppressed_since): the
row is recorded with approval "suppressed" and nothing is posted or tasked. An item dated tomorrow waits.

Live only, as it posts and writes HubSpot: a dry run lists what it would follow up and writes nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from us_outbound.base import notify, say
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.logs import log
from us_outbound.replies.desk import TABLE, ReplyItem, esc, is_reply, parties, suppressed_since
from us_outbound.timeparse import iso_date

JOB = "follow_up"
EVENT = "follow_up"  # events.type, and the prefix of its event_id
SUPPRESSED = "suppressed"  # events.approval on a follow-up passed by because they asked to stop
HEAD = f"{say.ACTION} *Replies that said “not now”: today is the day to follow up*"
LIST_LIMIT = 50


def event_id(item_id: str) -> str:
    return f"{EVENT}:{item_id}"


def due(ctx: Context, today: date) -> list[ReplyItem]:
    """The reply items whose follow_up_date is today or earlier, not followed up yet, oldest date first."""
    done = {str(e["event_id"]) for e in ctx.store.select("events", {"type": EVENT})}
    out = []
    for row in ctx.store.select(TABLE):
        if not is_reply(row) or event_id(str(row.get("item_id"))) in done:
            continue
        item = ReplyItem(row)
        when = iso_date(item.payload.get("follow_up_date"))
        if when is not None and when <= today:
            out.append(item)
    return sorted(out, key=lambda i: (str(i.payload.get("follow_up_date")), i.id))


def _line(ctx: Context, item: ReplyItem) -> str:
    account, contact = parties(ctx, item)
    who = " ".join(str(contact.get(k) or "") for k in ("first_name", "last_name")).strip() or "a contact"
    company = account.get("clean_name") or account.get("domain") or "a company"
    gave = "the date they gave" if item.payload.get("follow_up_source") == "reply" else "they gave no date"
    return (f"• {esc(who)} at {esc(company)} (they wrote to {esc(item.mailbox or 'us')}; {gave}): write to them again "
            f"from {esc(item.mailbox or 'the mailbox they wrote to')}.")


def _task(ctx: Context, item: ReplyItem, today: date) -> bool:
    """Harry's HubSpot task, due today (the module docstring). False when it could not be made."""
    owner = ctx.settings.general.hubspot_owner_id.strip()
    if not owner:
        return False
    hs = item.hubspot
    records = ([("companies", str(hs["company_id"]))] if item.is_warm and hs.get("company_id") else []) + (
        [("contacts", str(hs["contact_id"]))] if item.is_warm and hs.get("contact_id") else [])
    subject = f"US Outbound: follow up a “not now” reply today (item {item.short_id})"
    body = (f"Reply item {item.short_id} said not now; {today:%a %d %b %Y} is the day to write again, from "
            f"{item.mailbox or 'the mailbox they wrote to'}. The #us-outbound post today names them.")
    try:
        ctx.clients.hubspot.create_task(subject, body, owner, today, records)
    except (ApiError, ConfigError) as exc:
        log("follow_up_task_failed", item_id=item.id, error=str(exc)[:200])
        return False
    return True


def sweep(ctx: Context) -> dict[str, Any]:
    """The follow_up job (JOB CONTRACT: run(ctx) -> summary)."""
    today = ctx.today_uk()
    items = due(ctx, today)
    out: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "today": today.isoformat(), "due": len(items),
                           "followed_up": [], "suppressed": [], "errors": []}
    if ctx.dry_run:
        out["would_follow_up"] = [i.short_id for i in items][:LIST_LIMIT]
        return out
    to_tell = []
    for item in items:
        if suppressed_since(ctx, item):
            ctx.store.upsert("events", [{"event_id": event_id(item.id), "type": EVENT, "approval": SUPPRESSED,
                                         "account_id": item.account_id or None, "contact_id": item.contact_id or None,
                                         "occurred_at": ctx.now}])
            out["suppressed"].append(item.short_id)
            continue
        to_tell.append(item)
    posted = notify.post_once(ctx, [(event_id(i.id), _line(ctx, i)) for i in to_tell], head=HEAD) if to_tell else {}
    told = set(posted.get("keys") or ())
    for item in to_tell:
        tasked = _task(ctx, item, today)
        if event_id(item.id) not in told and not tasked:
            out["errors"].append(f"{item.short_id}: neither the Slack line nor the HubSpot task went; tried again "
                                 "at the next run")
            continue
        ctx.store.upsert("events", [{"event_id": event_id(item.id), "type": EVENT, "account_id": item.account_id or None,
                                     "contact_id": item.contact_id or None, "occurred_at": ctx.now}])
        out["followed_up"].append({"item": item.short_id, "slack": event_id(item.id) in told, "task": tasked})
    log("follow_up_done", run_id=ctx.run_id, due=len(items), followed_up=len(out["followed_up"]),
        suppressed=len(out["suppressed"]), errors=len(out["errors"]))
    return out
