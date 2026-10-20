"""Escalation (SPEC 11), run by the poll_approvals job (replies/poll_desk.py) after the reply desk: every
human-in-the-loop item open longer than ESCALATION_HOURS (24, settings/model.py) goes to escalation_email.

Except the weekly hand-check while it gates no day's enrolment (hand_check_gate no: nothing but its own sample waits
for it, and it reminds once on Wednesday itself, enrol/hand_check.remind), and the removed send approvals (closed by
replies/poll_desk.close_send_approvals). A reply item is forwarded from the mailbox that received it to
escalation_email (harry@spill.chat) through Instantly's forward endpoint, with the draft, the Slack link, the HubSpot
link and how to act added. Anything else, or a reply whose forward fails (PHASE0-CONFIRM that the endpoint exists on
our plan), becomes a HubSpot task for Harry due now, which HubSpot emails him, and a Slack DM to the approvers when
Slack is set up. A reply item then becomes "escalated" (still open to approval), an escalated event is recorded and
its thread says so. Other kinds keep their status, so the jobs that own them still find them; escalated_at marks
them. Enrolment pauses while a positive reply waits longer than escalation_hours (enrol.reply_pause reads the same
items: replies/desk.positive_waiting). Split from replies/desk.py in W2-4 (20 Oct 2026).

Dry-run: what would be escalated is listed; nothing is forwarded, tasked, posted or changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from us_outbound import fmt
from us_outbound.base import say
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.logs import clip, log
from us_outbound.replies.desk import (
    ESCALATED,
    EXCERPT_CHARS,
    OPEN,
    TABLE,
    ReplyItem,
    class_label,
    class_meaning,
    describe_item,
    esc,
    is_reply,
    parties,
    post_in_thread,
    quoted,
)
from us_outbound.settings.model import ESCALATION_HOURS
from us_outbound.timeparse import utc

SEND_APPROVAL_KIND = "send_approval"  # the removed send approvals' kind: closed, never escalated
HAND_CHECK_KIND = "hand_check"  # enrol/hand_check.py KIND: escalated only while it gates the day (gates())
ESCALATED_EVENT = "escalated:"  # events.event_id of an escalation: the item's id, prefixed


# How an item reached escalation_email, in words (escalate's `how`: "forwarded", "task", "dm", joined by "+").
HOW_WORDS = {"forwarded": "email (the reply forwarded to them)", "task": "a HubSpot task (HubSpot emails it)",
             "dm": "a Slack message to the approvers"}



# The other kinds of item escalation covers, in words, and what to do about each (the DM; {id} is the item id).
KIND_WORDS: dict[str, tuple[str, str]] = {
    HAND_CHECK_KIND: ("this week's hand-check (a sample of companies to check before they are emailed)",
                      "check the companies listed (" + say.command("handcheck show") + " shows them), then "
                      "approve with " + say.command("handcheck approve --live") + "."),
    "kill_rule": ("a safety stop (a bounce or complaint limit was hit, so some sending is held back)",
                  "find the cause, then lift it with " + say.command("killrules clear {id} --live")
                  + " (" + say.command("killrules show") + " lists the stops)."),
    "manual_merge": ("a HubSpot duplicate (more than one company for one website)",
                     "merge the companies into one in HubSpot; the system carries on by itself after."),
}




def _permalink(slack: Any, channel: str, slack_ts: str) -> str:
    if slack is None or not channel or not slack_ts:
        return ""
    try:
        return slack.permalink(channel, slack_ts)
    except (ApiError, LookupError):
        return ""


def escalation_note(ctx: Context, item: ReplyItem, slack: Any, *, for_slack: bool = False) -> str:
    """What escalation_email gets with the forwarded reply (plain text), or the approvers' DM (for_slack): who
    replied, what they said, the draft, the links and how to act (SPEC 11). It stands alone: the reader may not
    have seen the alert."""
    g = ctx.settings.general
    link = _permalink(slack, item.slack_channel, item.slack_ts)
    hubspot = item.hubspot.get("link") or ""
    received = fmt.uk_time(item.received_at or item.created_at, missing="")
    meaning = class_meaning(item.reply_class)
    approve = say.command(f"replies approve {item.short_id} --live")
    said = clip(" ".join(item.excerpt.split()), EXCERPT_CHARS)
    if not for_slack:
        return (
            f"This is {describe_item(ctx, item)}, sent to {item.mailbox}"
            + (f" on {received}" if received else "") + f". It has waited over {ESCALATION_HOURS} hours for "
            "someone to approve the answer.\n\n"
            f"Draft answer (signed {item.owner or 'by the mailbox owner'}):\n{item.draft or '(no draft: write your own)'}"
            f"\n\nSlack: {link or 'the alert in #us-outbound'}\n"
            f"HubSpot: {hubspot or ('not yet in HubSpot' if item.is_warm else 'not in HubSpot (only Interested and Referral replies go there)')}"
            "\n\nTo send the draft: react ✅ to it in the Slack thread, or reply \"send\" there (\"edit: <new text>\" "
            f"changes it first). Or in a terminal: {approve}. Or answer the prospect yourself from {item.mailbox}, "
            f"then reply \"skip\" in the thread. Never reply to the prospect from {g.escalation_email}."
        )
    thread = f"<{link}|open the alert in Slack>" if link else "the alert in #us-outbound"
    account, contact = parties(ctx, item)
    who = " ".join(str(contact.get(k) or "") for k in ("first_name", "last_name")).strip() or "A contact"
    company = account.get("clean_name") or account.get("domain") or ""
    lines = [
        f"{say.URGENT} *A prospect's reply has waited over {ESCALATION_HOURS} hours for an answer*",
        f"*Who:* {esc(who)}" + (f" at {esc(company)}" if company else "")
        + f", writing to {item.mailbox}" + (f" on {received}" if received else "") + ".",
        f"*What the system thinks it is:* {class_label(item.reply_class)}" + (f": {meaning}." if meaning else "."),
    ]
    if said:
        lines += ["*What they said:*", quoted(said)]
    lines += ([f"*Draft answer* (signed {esc(item.owner or 'by the mailbox owner')}):", quoted(item.draft)]
              if item.draft else ["*No draft:* the answer has to be written by hand."])
    if hubspot:
        lines.append(f"*HubSpot:* <{hubspot}|the company record>")
    lines += ["*What to do* (any one of these):",
              f"1. In the Slack thread ({thread}): ✅ on the draft or reply `send` sends it; `edit: &lt;new text&gt;` "
              "changes it first; `send: &lt;your text&gt;` sends your own text instead.",
              f"2. In a terminal: {approve} sends the draft.",
              f"3. Answer them yourself from {item.mailbox}, then reply `skip` in the thread to close it. "
              f"Never answer from {g.escalation_email}."]
    return "\n".join(lines)


def _task_now(ctx: Context, subject: str, body: str, records: list[tuple[str, str]]) -> bool:
    """A HubSpot task for Harry due now; HubSpot emails him its notification (SPEC 11)."""
    owner = ctx.settings.general.hubspot_owner_id.strip()
    if not owner:
        return False
    try:
        ctx.clients.hubspot.create_task(subject, body, owner, ctx.now, records)
    except (ApiError, ConfigError) as exc:
        log("escalation_task_failed", error=str(exc)[:200])
        return False
    return True


def _dm(ctx: Context, slack: Any, text: str) -> bool:
    if slack is None:
        return False
    done = False
    for user in sorted(ctx.guard.bounds.approver_slack_ids):
        try:
            done = bool(slack.dm(user, text)) or done
        except ApiError as exc:
            log("escalation_dm_failed", error=str(exc)[:200])
    return done


def _escalate_reply(ctx: Context, item: ReplyItem, slack: Any) -> str:
    """How the item reached escalation_email ("forwarded", "task", "dm"), or "" if nothing worked."""
    g = ctx.settings.general
    note = escalation_note(ctx, item, slack)
    if item.mailbox in ctx.guard.bounds.registry_addresses and item.email_id:
        try:
            ctx.clients.instantly.forward(item.mailbox, item.email_id, g.escalation_email, note)
            return "forwarded"
        except (ApiError, LookupError, ConfigError) as exc:  # PHASE0-CONFIRM the endpoint; any failure falls back
            log("escalation_forward_failed", item_id=item.id, error=str(exc)[:200])
    hs = item.hubspot
    if item.is_warm and hs.get("company_id"):
        records = [("companies", hs["company_id"])] + ([("contacts", hs["contact_id"])] if hs.get("contact_id") else [])
        subject, body = f"US Outbound: waiting {ESCALATION_HOURS} hours, {describe_item(ctx, item)}", note
    else:  # nothing about a prospect who is not a warm lead goes into HubSpot (SPEC 1.2)
        records = []
        subject = f"US Outbound: a {item.reply_class or 'reply'} item has waited {ESCALATION_HOURS} hours"
        body = (f"Reply item {item.short_id} needs you: see the #us-outbound thread, or run "
                f"{say.command('replies list')} and {say.command(f'replies approve {item.short_id} --live')}.")
    dm = escalation_note(ctx, item, slack, for_slack=True)
    how = [x for x, ok in (("task", _task_now(ctx, subject, body, records)), ("dm", _dm(ctx, slack, dm))) if ok]
    return "+".join(how)


def _escalate_other(ctx: Context, row: Mapping[str, Any], slack: Any) -> str:
    """A hand-check, manual merge or kill-rule item: a HubSpot task for Harry due now, and a DM (SPEC 11)."""
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    kind = str(row.get("kind") or "item").replace("_", " ")
    link = _permalink(slack, str(row.get("slack_channel") or ""), str(row.get("slack_ts") or ""))
    summary = clip(str(payload.get('summary') or ''), 300)
    body = (f"{summary} It has waited over {ESCALATION_HOURS} hours. "
            + (f"Slack: {link}" if link else f"See #us-outbound or {say.command('status')}.")).strip()
    records = [("companies", str(c)) for c in payload.get("hubspot_company_ids") or ()][:5]
    subject = f"US Outbound: a {kind} item has waited {ESCALATION_HOURS} hours"
    what, todo = KIND_WORDS.get(str(row.get("kind") or ""),
                                (f"a {kind} item", "see what it needs with " + say.command("status") + "."))
    dm = say.compose(say.URGENT, f"Waiting over {ESCALATION_HOURS} hours for someone: {what}",
                     [esc(summary), f"<{link}|Open it in Slack>" if link else "It is in #us-outbound."],
                     do=todo.replace("{id}", str(row.get("item_id") or "ITEM_ID")))
    how = [x for x, ok in (("task", _task_now(ctx, subject, body, records)), ("dm", _dm(ctx, slack, dm))) if ok]
    return "+".join(how)


def escalate(ctx: Context, slack: Any, run: Any) -> None:
    """Every human-in-the-loop item open longer than escalation_hours goes to escalation_email (SPEC 11). run: the
    poll_approvals run (replies/poll_desk._Run), which keeps what was escalated and the errors.

    Send approvals are left out: they were removed, and close_send_approvals closes any still open. So is the weekly
    hand-check while it gates no day's enrolment (enrol/hand_check.gates): with hand_check_gate = no (D5) only its
    sampled companies wait, for 7 days at most, and it posts its own reminder on Wednesday (enrol/hand_check.remind),
    so a task and a DM every week would ask for nothing more.
    """
    g = ctx.settings.general
    cutoff = ctx.now - timedelta(hours=ESCALATION_HOURS)
    for row in ctx.store.select(TABLE, {"status": OPEN}):
        if row.get("escalated_at") or row.get("kind") == SEND_APPROVAL_KIND:
            continue
        if row.get("kind") == HAND_CHECK_KIND and not g.hand_check_gate:
            continue
        reply = is_reply(row)
        item = ReplyItem(row)
        started = item.created_at if reply else utc(row.get("created_at"))
        if started is None or started > cutoff:
            continue
        if ctx.dry_run:
            run.add("would", {"item": str(row.get("item_id"))[:8], "action": "escalate", "kind": row.get("kind")})
            continue
        how = _escalate_reply(ctx, item, slack) if reply else _escalate_other(ctx, row, slack)
        if not how:
            run.errors.append(f"{str(row.get('item_id'))[:8]}: could not escalate (no forward, no HubSpot owner id, "
                              "no Slack); tried again next run")
            continue
        values: dict[str, Any] = {"escalated_at": ctx.now}
        if reply:
            values["status"] = ESCALATED
        if not ctx.store.update(TABLE, {"item_id": row["item_id"], "status": OPEN}, values):
            continue  # handled in the meantime
        ctx.store.upsert("events", [{
            "event_id": f"{ESCALATED_EVENT}{row['item_id']}", "type": "escalated",
            "account_id": row.get("account_id") or item.account_id or None,
            "contact_id": row.get("contact_id") or item.contact_id or None,
            "mailbox": item.mailbox or None, "reply_class": item.reply_class or None, "occurred_at": ctx.now,
        }])
        if reply:
            ways = " and ".join(HOW_WORDS.get(x, x) for x in how.split("+"))
            post_in_thread(slack, item, f":outbox_tray: *Escalated to {g.escalation_email}* after {ESCALATION_HOURS} hours "
                                 f"with no answer, by {ways}. You can still send or skip it here.")
        log("escalated", item_id=row["item_id"], kind=row.get("kind"), how=how)
        run.add("escalated", {"item": str(row["item_id"])[:8], "kind": row.get("kind"), "how": how})
