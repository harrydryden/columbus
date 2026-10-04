"""The reply desk: poll_approvals, every 5 minutes (SPEC 9; SPEC 11 "Approval" and "Escalation"; D11).

For each reply item not yet handled (replies/items.py; poll_replies creates them):
  1. HubSpot. A positive or referral reply not yet written to HubSpot gets the SPEC 11 writes
     (crm/hubspot_writes.record_reply) and its thread gets the record's link. SPEC 11 has
     poll_replies do this before it posts the alert; this catches anything it did not.
  2. Slack. The thread's new replies, then the reactions on the current draft, are read. Only an
     approver counts (decision D11, approved by Harry, 1 Oct 2026): a Slack id on approver_slack_ids,
     or the owner of the mailbox the prospect wrote to (Mailboxes slack_id), for that mailbox only.
     Anyone else is ignored.
       ✅ on the draft, or "send"   send the draft;
       "send: <text>"              send that text instead (SPEC 11);
       "edit: <text>"              the text replaces the draft, which is posted again in the thread;
                                   ✅ on that message, or "send", then sends it;
       ❌ on the draft, or "skip"   handled, and nothing is sent (SPEC 11).
     Thread replies count in order, then the reactions on the current draft message; ❌ beats ✅.
     A ✅ on a draft that was edited since, or whose send failed, no longer counts. The bot seeds ✅
     and ❌ on each alert (replies/poll.py) and on a changed draft, so deciding is one click; its own
     reactions and notes are neither decisions nor counted as ignored.
  3. Send, from the mailbox the prospect wrote to, in the thread, through Instantly's reply
     endpoint (clients/instantly.reply: a US Outbound campaign's thread and a registry mailbox; the
     guard checks the approver too). The text must pass the copy rules' word and line checks (SPEC
     1.9) or it is not sent, and the thread says why. Then the thread confirms ("Sent from
     hannah@meetspill.org at 14:32 UK"), an events row records the approval (approved, edited or
     skipped, with approved_by) and the item is handled. A sent reply's row is type reply_sent, not
     sent: it uses the mailbox (registry/mailboxes.last_use counts it) but is no campaign send, so
     send counts, steps and the kill rules' bounce rates leave it out. While it goes out the item is held at
     status "sending" by a compare-and-set, so it can never go twice; if a run dies mid-send the
     item is not resent by itself: the thread (and `replies list`) asks a person to check.
  4. Re-post (SPEC 11, D11). An alert unanswered for 2 hours is re-posted in its thread and to the
     channel, mentioning the approvers, between 13:00 and 23:00 UK (D11 moved the end from 21:00,
     which is 4 pm ET). Once per item.
Then escalation (SPEC 11), for every kind of human-in-the-loop item:
  5. Open longer than escalation_hours (24), except the weekly hand-check while auto_send = no
     (nothing waits for it then): a reply item is forwarded from the mailbox that
     received it to escalation_email (harry@spill.chat) through Instantly's forward endpoint, with
     the draft, the Slack link, the HubSpot link and how to act added. Anything else, or a reply
     whose forward fails (PHASE0-CONFIRM that the endpoint exists on our plan), becomes a HubSpot task
     for Harry due now, which HubSpot emails him, and a Slack DM to the approvers when Slack is set
     up. A reply item then becomes "escalated" (still open to approval), an escalated event is
     recorded and its thread says so. Other kinds keep their status, so the jobs that own them still
     find them; escalated_at marks them. Enrolment pauses while a positive reply waits longer than
     escalation_hours (enrol.reply_pause reads the same items: replies/items.positive_waiting).
Then send approvals (Harry, 2 Oct 2026; while auto_send = no every email waits for an approver's ✅
before its lead is added to Instantly): enrol/approvals.poll reads their cards' threads and
reactions and acts, with the patterns above. They are never re-posted or escalated: an item not
approved by the end of its next send day expires instead.

Without Slack (no US_OUTBOUND_SLACK_BOT_TOKEN, as may be so for the pilot), steps 2 and 4 are left
out and logged, escalation still forwards (or tasks) to escalation_email, and the command line does
the desk's work: `us-outbound replies list | approve | skip` (list_items, approve and skip below) take
the same send, HubSpot and close path, with approved_by "cli".

Dry-run: Slack and HubSpot are read and every write is worked out; the summary (and so the heartbeat)
lists each decision, send, re-post and escalation; nothing is posted, sent, forwarded or changed in
the database, so the live run that follows acts on the same items.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Any

from us_outbound.clients.guard import CLI_APPROVER, GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, ConfigError, Context
from us_outbound.crm import hubspot_writes as hw
from us_outbound.enrol.copy_rules import content_violations, structure_violations
from us_outbound.logs import clip, log
from us_outbound.replies.items import (
    ACTIONABLE,
    ESCALATED,
    HANDLED,
    OPEN,
    SENDING,
    TABLE,
    WAITING,
    ReplyItem,
    claim,
    is_reply,
    reply_items,
    save_payload,
    ts,
)
from us_outbound.replies.outcomes import REPLY_SENT
from us_outbound.settings.model import Settings

JOB = "poll_approvals"
SEND_APPROVAL_KIND = "send_approval"  # enrol/approvals.py KIND: never re-posted or escalated here
HAND_CHECK_KIND = "hand_check"  # enrol/hand_check.py KIND: escalated only while auto_send = yes
REPOST_AFTER = timedelta(hours=2)  # SPEC 11
REPOST_FROM, REPOST_UNTIL = time(13), time(23)  # UK time; D11 (Harry, 1 Oct 2026): until 23:00, not 21:00
APPROVE_REACTIONS = frozenset({"white_check_mark", "heavy_check_mark"})  # ✅ (and ✔️)
SKIP_REACTIONS = frozenset({"x"})  # ❌
SEED_REACTIONS = ("white_check_mark", "x")  # the bot's own ✅ and ❌ on an alert or a changed draft: one click decides
APPROVED, EDITED, SKIPPED = "approved", "edited", "skipped"  # events.approval (SPEC 6)
ESCALATED_EVENT = "escalated:"  # events.event_id of an escalation: the item's id, prefixed
SENT_EVENT = "reply-sent:"  # events.event_id of a sent reply when Instantly returns no id
LIST_LIMIT = 50  # per-list entries kept in the summary
EXCERPT_CHARS = 200  # SPEC 1.7: at most 200 characters of any email body leave the database


# -- Slack text -----------------------------------------------------------------------------------

_SLACK_LINK = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")
_SEND = re.compile(r"^send[\s.!]*$", re.I)
_SKIP = re.compile(r"^skip[\s.!]*$", re.I)
_SEND_TEXT = re.compile(r"^send\s*:\s*(.*)$", re.I | re.S)
_EDIT = re.compile(r"^edit\s*:\s*(.*)$", re.I | re.S)


def slack_text(text: Any) -> str:
    """A Slack message as plain text: links and mentions unwrapped, &amp; &lt; &gt; unescaped.

    Slack sends a typed link as <https://x.com|x.com> and an address as <mailto:a@b.com|a@b.com>;
    the email gets the link itself. A mention (<@U123>) is dropped.
    """

    def unwrap(m: re.Match[str]) -> str:
        target, label = m.group(1), m.group(2) or ""
        if target.startswith("#"):
            return f"#{label}" if label else ""
        if target.startswith(("@", "!")):
            return ""
        if target.startswith("mailto:"):
            return label or target[len("mailto:"):]
        bare = re.sub(r"^https?://", "", target).rstrip("/")
        if not label or label.rstrip("/") in (target.rstrip("/"), bare):
            return target
        return f"{label} ({target})"

    out = _SLACK_LINK.sub(unwrap, str(text or ""))
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


@dataclass(frozen=True)
class Command:
    kind: str  # send | send_text | edit | skip
    text: str = ""


def parse_command(text: Any) -> Command | None:
    """An approver's thread reply as a command, or None if it is not one (SPEC 11; D11)."""
    t = slack_text(text)
    if _SEND.match(t):
        return Command("send")
    if _SKIP.match(t):
        return Command("skip")
    for rx, kind in ((_SEND_TEXT, "send_text"), (_EDIT, "edit")):
        m = rx.match(t)
        if m:
            body = m.group(1).strip()
            return Command(kind, body) if body else None
    return None


def _num(slack_ts: Any) -> tuple[int, int]:
    """A Slack ts ("1700000000.000100") as (seconds, microseconds), compared exactly, never as a float."""
    seconds, _, micro = str(slack_ts or "0").strip().partition(".")
    try:
        return int(seconds or 0), int((micro or "0").ljust(6, "0")[:6])
    except ValueError:
        return 0, 0


# -- who may approve (D11) ---------------------------------------------------------------------------


def approvers_for(settings: Settings, mailbox: str) -> frozenset[str]:
    """approver_slack_ids, and the owner of this mailbox when their Mailboxes row has a slack_id (D11)."""
    ids = {x.strip() for x in settings.general.approver_slack_ids if x.strip()}
    for m in settings.mailboxes:
        if m.address.lower() == mailbox.lower() and m.slack_id and m.status != "Retired":
            ids.add(m.slack_id)
    return frozenset(ids)


def _mentions(ids: Iterable[str]) -> str:
    return " ".join(f"<@{u}>" for u in sorted(ids))


def slack_or_none(ctx: Context) -> Any | None:
    """The Slack client, or None (logged) when no bot token is set: the desk then works without Slack."""
    from us_outbound.clients.slack import SlackOff

    try:
        slack = ctx.clients.slack
    except ConfigError:
        slack = None
    if slack is None or isinstance(slack, SlackOff):
        log("slack_not_configured", job=ctx.job,
            reason="US_OUTBOUND_SLACK_BOT_TOKEN is not set: Slack approvals and re-posts are skipped; "
                   "use `us-outbound replies list|approve|skip`")
        return None
    return slack


# -- reading a thread --------------------------------------------------------------------------------


@dataclass
class Decision:
    """What the thread and reactions say, read in order; nothing is changed while reading."""

    action: str = ""  # send | skip | "" (nothing decided)
    by: str = ""
    via: str = ""  # ✅, ❌, send, send:, skip
    text: str = ""  # what send sends
    draft: str = ""  # the draft after any edits
    edited: bool = False
    edits: list[tuple[str, str]] = field(default_factory=list)  # (user, new draft), in order
    approve_ts: str = ""  # the message whose ✅ / ❌ count
    seen_ts: str = ""  # the newest thread reply read
    ignored: list[str] = field(default_factory=list)  # users who are not approvers for this item


def read_decision(slack: Any, item: ReplyItem, approvers: frozenset[str], bot: str = "") -> Decision:
    """bot: the bot's own Slack user id; its notes and the ✅ and ❌ it seeds are neither decisions nor ignored."""
    desk = item.desk
    approve_ts = desk["approve_ts"] if "approve_ts" in desk else item.slack_ts  # "" after an edit or a failed send
    d = Decision(draft=item.draft, edited=item.draft_edited, seen_ts=str(desk.get("seen_ts") or ""),
                 approve_ts=str(approve_ts or ""))
    replies = sorted(slack.replies(item.slack_channel, item.slack_ts), key=lambda m: _num(m.get("ts")))
    for m in replies:
        if _num(m.get("ts")) <= _num(d.seen_ts):
            continue
        d.seen_ts = str(m.get("ts"))
        if m.get("bot_id") or m.get("subtype") == "bot_message" or (bot and m.get("user") == bot):
            continue  # the desk's own notes
        user = str(m.get("user") or "")
        cmd = parse_command(m.get("text"))
        if cmd is None:
            continue
        if user not in approvers:
            d.ignored.append(user)
            continue
        if cmd.kind == "edit":
            d.edits.append((user, cmd.text))
            d.draft, d.edited, d.approve_ts = cmd.text, True, ""  # a ✅ on the old draft no longer counts
        elif cmd.kind == "skip":
            d.action, d.by, d.via = "skip", user, "skip"
            break
        elif cmd.kind == "send":
            d.action, d.by, d.via, d.text = "send", user, "send", d.draft
            break
        else:
            d.action, d.by, d.via, d.text = "send", user, "send:", cmd.text
            d.edited = d.edited or cmd.text.strip() != item.draft.strip()
            break
    if not d.action and d.approve_ts:
        reactions = slack.reactions(item.slack_channel, d.approve_ts)

        def first(names: frozenset[str]) -> str:
            for r in reactions:
                if r.get("name") in names:
                    for u in r.get("users") or ():
                        if bot and u == bot:
                            continue  # the ✅ and ❌ the bot seeds on the alert
                        if u in approvers:
                            return str(u)
                        d.ignored.append(str(u))
            return ""

        skip_by, send_by = first(SKIP_REACTIONS), first(APPROVE_REACTIONS)
        if skip_by:
            d.action, d.by, d.via = "skip", skip_by, "❌"
        elif send_by:
            d.action, d.by, d.via, d.text = "send", send_by, "✅", d.draft
    return d


# -- the shared close path (Slack and the command line) ---------------------------------------------


def _reload(ctx: Context, item: ReplyItem) -> ReplyItem:
    row = ctx.store.get(TABLE, item_id=item.id)
    return ReplyItem(row) if row is not None else item


def _thread(slack: Any, item: ReplyItem, text: str, *, broadcast: bool = False) -> str:
    """Post in the item's thread; the new message's ts when it landed there (else "")."""
    if slack is None or not item.slack_channel or not item.slack_ts:
        return ""
    try:
        out = slack.post(item.slack_channel, text, thread_ts=item.slack_ts, broadcast=broadcast)
    except (ApiError, LookupError) as exc:
        log("desk_slack_failed", item_id=item.id, error=str(exc)[:200])
        return ""
    return str(out.get("ts") or "") if out and out.get("channel") == item.slack_channel else ""


def seed(slack: Any, channel: str, slack_ts: str, names: Iterable[str] = SEED_REACTIONS) -> None:
    """The bot's own reactions on a message (Slack.react), so deciding is one click. A failure is logged, never fatal."""
    if slack is None or not channel or not slack_ts:
        return
    for name in names:
        try:
            slack.react(channel, slack_ts, name)
        except GuardViolation:
            raise
        except Exception as exc:  # the approver can still add the reaction, or reply in the thread
            log("desk_react_failed", ts=slack_ts, reaction=name, error=f"{type(exc).__name__}: {str(exc)[:200]}")


def _who(by: str) -> str:
    return "at the command line" if by == CLI_APPROVER else f"by <@{by}>"


def _names(ctx: Context, item: ReplyItem) -> tuple[dict, dict]:
    account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
    contact = ctx.store.get("contacts", contact_id=item.contact_id) if item.contact_id else None
    return account or {}, contact or {}


def copy_problems(ctx: Context, item: ReplyItem, text: str) -> list[str]:
    """SPEC 1.9: the copy rules' word and line checks on the text about to be sent."""
    if not text.strip():
        return ["there is no draft to send; reply \"send: <text>\" (or `replies approve --edit`)"]
    g = ctx.settings.general
    account, contact = _names(ctx, item)
    owner = item.owner or next((m.owner_name for m in ctx.settings.mailboxes if m.address.lower() == item.mailbox), "")
    exempt = [str(x) for x in (account.get("clean_name"), contact.get("first_name"), contact.get("last_name")) if x]
    return [*content_violations(text, sender_is_harry=owner == g.demo_host, demo_host=g.demo_host, exempt=exempt),
            *structure_violations(text)]


def _record_skip_event(ctx: Context, item: ReplyItem, by: str) -> None:
    """approval = skipped on the prospect's replied event (SPEC 6), added if poll_replies has not made it."""
    if not item.email_id:
        return
    values = {"approval": SKIPPED, "approved_by": by}
    if not ctx.store.update("events", {"event_id": item.email_id}, values):
        ctx.store.upsert("events", [{
            "event_id": item.email_id, "type": "replied", "account_id": item.account_id or None,
            "contact_id": item.contact_id or None, "mailbox": item.mailbox or None,
            "reply_class": item.reply_class or None, "occurred_at": item.received_at or item.created_at or ctx.now,
            **values,
        }])


def send_reply(ctx: Context, item: ReplyItem, text: str, *, by: str, edited: bool, via: str,
               slack: Any = None) -> dict:
    """Send the approved text from the mailbox the prospect wrote to, then close the item (SPEC 11 Approval)."""
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "via": via, "by": by}
    problems = copy_problems(ctx, item, text)
    if not item.mailbox or not item.reply_to:
        problems.append("the item names no mailbox or Instantly email to reply to")
    elif item.mailbox not in ctx.guard.bounds.registry_addresses:
        problems.append(f"{item.mailbox} is not in the mailbox registry, so it cannot send")
    if problems:
        result.update(sent=False, why=problems)
        if ctx.live:
            note = _thread(slack, item, "Not sent: " + "; ".join(problems)
                           + ". Fix it with \"edit: <text>\" and then \"send\", or \"send: <text>\".")
            save_payload(ctx.store, item.id, desk={**item.desk, "approve_ts": note, "blocked": problems[:5]})
        log("desk_blocked", item_id=item.id, problems=problems[:5])
        return result
    approval = EDITED if edited else APPROVED
    if ctx.dry_run:
        result.update(sent=False, dry_run=True, approval=approval, mailbox=item.mailbox)
        return result
    if not claim(ctx.store, item, SENDING):
        result.update(sent=False, why=["another run is handling this item"])
        return result
    started = {"at": ctx.now.isoformat(), "by": by, "approval": approval, "via": via}
    save_payload(ctx.store, item.id, desk={**item.desk, "sending": started})
    try:
        sent = ctx.clients.instantly.reply(item.mailbox, item.reply_to, item.subject or None, text, approved_by=by)
    except GuardViolation as exc:
        # Refused (SPEC 1): back to its status for a person, and this approval spent, so the next run
        # does not try it again (the violation surfaces once, in this run's heartbeat).
        note = _thread(slack, item, f"Not sent: blocked by a guardrail ({str(exc)[:160]}). Answer it from "
                                    f"{item.mailbox} yourself, or \"skip\".")
        save_payload(ctx.store, item.id, {"status": item.status},
                     desk={**_reload(ctx, item).desk, "approve_ts": note, "sending": None,
                           "failed": {**started, "error": "guardrail"}})
        raise
    except Exception as exc:  # the reply did not go, or Instantly did not say: never resent without a person
        why = f"{type(exc).__name__}: {str(exc)[:200]}"
        note = _thread(slack, item, f"Not sent: Instantly answered {why}. Check the Sent folder of {item.mailbox} "
                                    "before you approve it again (\"send\").")
        desk = {**_reload(ctx, item).desk, "approve_ts": note, "failed": {**started, "error": why}}
        desk.pop("sending", None)
        save_payload(ctx.store, item.id, {"status": item.status}, desk=desk)
        log("desk_send_failed", item_id=item.id, error=why)
        result.update(sent=False, why=[why])
        return result
    email_id = str((sent or {}).get("id") or "")
    uk_time = ctx.now.astimezone(UK).strftime("%H:%M")
    desk = {**_reload(ctx, item).desk, "sent": {**started, "email_id": email_id, "mailbox": item.mailbox}}
    desk.pop("sending", None)
    save_payload(ctx.store, item.id, {"status": HANDLED, "handled_at": ctx.now, "handled_by": by},
                 desk=desk, sent_text=text)
    ctx.store.upsert("events", [{  # reply_sent, not sent: never counted as a campaign send
        "event_id": email_id or f"{SENT_EVENT}{item.id}", "type": REPLY_SENT, "step": None, "mailbox": item.mailbox,
        "account_id": item.account_id or None, "contact_id": item.contact_id or None,
        "approval": approval, "approved_by": by, "occurred_at": ctx.now,
    }])
    _thread(slack, item, f"Sent from {item.mailbox} at {uk_time} UK{' (edited)' if edited else ''}, approved {_who(by)}.")
    log("desk_sent", item_id=item.id, account_id=item.account_id, mailbox=item.mailbox, approval=approval, by=by)
    result.update(sent=True, approval=approval, mailbox=item.mailbox, at=f"{uk_time} UK")
    return result


def skip_item(ctx: Context, item: ReplyItem, *, by: str, via: str, slack: Any = None) -> dict:
    """Handled, and nothing sent: the approver will deal with it themselves (SPEC 11 "skip")."""
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "via": via, "by": by}
    if ctx.dry_run:
        result.update(skipped=False, dry_run=True)
        return result
    if not claim(ctx.store, item, HANDLED):
        result.update(skipped=False, why=["another run is handling this item"])
        return result
    save_payload(ctx.store, item.id, {"handled_at": ctx.now, "handled_by": by},
                 desk={**item.desk, "skipped": {"at": ctx.now.isoformat(), "by": by, "via": via}})
    _record_skip_event(ctx, item, by)
    _thread(slack, item, f"Skipped {_who(by)}: marked handled, and nothing was sent. "
                         f"Reply to the prospect from {item.mailbox or 'its mailbox'} if it needs an answer.")
    log("desk_skipped", item_id=item.id, account_id=item.account_id, by=by)
    result.update(skipped=True)
    return result


def _hubspot(ctx: Context, item: ReplyItem, slack: Any, out: Counter[str], errors: list[str]) -> ReplyItem:
    """SPEC 11 HubSpot writes for a warm reply not yet written; its thread gets the link once."""
    if not item.is_warm or item.hubspot.get("status") == "written":
        return item
    try:
        res = hw.record_reply(ctx, item)
    except (ApiError, ConfigError) as exc:
        errors.append(f"{item.short_id}: HubSpot: {str(exc)[:160]}")
        return item
    out[res.get("status", "?")] += 1
    item = _reload(ctx, item)
    if ctx.live and res.get("status") == "written" and res.get("link") and not item.desk.get("hubspot_posted"):
        _thread(slack, item, f"HubSpot: {res['link']}")
        save_payload(ctx.store, item.id, desk={**item.desk, "hubspot_posted": True})
        item = _reload(ctx, item)
    elif ctx.live and res.get("status") == "merge_needed" and not item.desk.get("merge_posted"):
        _thread(slack, item, "HubSpot has more than one company for this domain, so nothing was written there. "
                             "Merge them in HubSpot; the next run carries on.")
        save_payload(ctx.store, item.id, desk={**item.desk, "merge_posted": True})
        item = _reload(ctx, item)
    return item


def _apply_reading(ctx: Context, item: ReplyItem, d: Decision, slack: Any) -> ReplyItem:
    """Keep what the thread said (live): the newest reply read, edits to the draft, the message ✅ counts on."""
    desk = {**item.desk, "seen_ts": d.seen_ts, "approve_ts": d.approve_ts}
    if not d.edits and desk == item.desk:
        return item  # nothing new in the thread: no write every five minutes
    extra: dict[str, Any] = {}
    if d.edits:
        user, _ = d.edits[-1]
        extra = {"draft": d.draft, **({} if item.draft_edited else {"draft_original": item.draft})}
        desk["edited_by"] = user
        if not d.action:  # posted again so the new draft can get its ✅
            desk["approve_ts"] = _thread(slack, item, f"Draft changed by <@{user}>. React ✅ to this message or reply "
                                                      f"\"send\" to send it; ❌ or \"skip\" to leave it:\n>>> {d.draft}")
            seed(slack, item.slack_channel, desk["approve_ts"])
    save_payload(ctx.store, item.id, desk=desk, **extra)
    return _reload(ctx, item)


def _repost_due(ctx: Context, item: ReplyItem) -> bool:
    if item.status != OPEN or item.row.get("reposted_at") or not item.slack_ts or item.created_at is None:
        return False
    if ctx.now - item.created_at < REPOST_AFTER:
        return False
    return REPOST_FROM <= ctx.now.astimezone(UK).time() < REPOST_UNTIL


def _describe(ctx: Context, item: ReplyItem) -> str:
    account, contact = _names(ctx, item)
    who = " ".join(str(contact.get(k) or "") for k in ("first_name", "last_name")).strip() or "a contact"
    company = account.get("clean_name") or account.get("domain") or "an account"
    return f"{item.reply_class.replace('_', ' ') or 'a'} reply from {who} at {company} → {item.mailbox}"


def _repost(ctx: Context, item: ReplyItem, slack: Any) -> bool:
    hours = int((ctx.now - item.created_at).total_seconds() // 3600) if item.created_at else 2
    text = (f"{_mentions(approvers_for(ctx.settings, item.mailbox))} Still waiting after {hours} hours: "
            f"{_describe(ctx, item)}. React ✅ to the draft or reply \"send\"; \"edit: <text>\" changes it; "
            f"❌ or \"skip\" if you will answer it yourself. Or `us-outbound replies approve {item.short_id} --live`.")
    if not _thread(slack, item, text, broadcast=True):
        return False
    ctx.store.update(TABLE, {"item_id": item.id}, {"reposted_at": ctx.now})
    return True


@dataclass
class _Run:
    sent: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    reposted: list[str] = field(default_factory=list)
    escalated: list[dict] = field(default_factory=list)
    would: list[dict] = field(default_factory=list)
    stuck: list[str] = field(default_factory=list)
    ignored: Counter[str] = field(default_factory=Counter)
    hubspot: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    bot: str | None = None  # the bot's own Slack user id, asked once when a thread is first read

    def add(self, name: str, entry: Any) -> None:
        bucket = getattr(self, name)
        if len(bucket) < LIST_LIMIT:
            bucket.append(entry)


def _stuck(ctx: Context, item: ReplyItem, slack: Any, run: _Run) -> None:
    """An item left at "sending" by a run that died: back to open for a person, never resent alone."""
    started = ts((item.desk.get("sending") or {}).get("at"))
    if started is not None and ctx.now - started < timedelta(minutes=10):
        return  # a send may still be going
    run.add("stuck", item.short_id)
    if ctx.dry_run:
        return
    note = _thread(slack, item, f"I can't tell whether the reply went out (the run sending it stopped). Check the Sent "
                                f"folder of {item.mailbox}, then reply \"send\" to send it, or \"skip\".")
    desk = {**item.desk, "approve_ts": note, "unsure": item.desk.get("sending")}
    desk.pop("sending", None)
    previous = ESCALATED if item.row.get("escalated_at") else OPEN
    save_payload(ctx.store, item.id, {"status": previous}, desk=desk)
    log("desk_unsure", item_id=item.id)


def _work(ctx: Context, item: ReplyItem, slack: Any, run: _Run) -> None:
    item = _hubspot(ctx, item, slack, run.hubspot, run.errors)
    if slack is None or not item.slack_channel or not item.slack_ts:
        return
    approvers = approvers_for(ctx.settings, item.mailbox)
    if run.bot is None:
        run.bot = slack.bot_user_id()
    d = read_decision(slack, item, approvers, run.bot)
    run.ignored.update(u for u in d.ignored if u)
    if ctx.dry_run:
        if d.action or d.edits:
            run.add("would", {"item": item.short_id, "action": d.action or "edit", "by": d.by, "via": d.via,
                              "edited": d.edited})
        elif _repost_due(ctx, item):
            run.add("would", {"item": item.short_id, "action": "repost"})
        return
    if d.edits:
        run.add("edited", item.short_id)
    item = _apply_reading(ctx, item, d, slack)
    if d.action == "send":
        res = send_reply(ctx, item, d.text, by=d.by, edited=d.edited, via=d.via, slack=slack)
        run.add("sent" if res.get("sent") else "blocked", res)
    elif d.action == "skip":
        run.add("skipped", skip_item(ctx, item, by=d.by, via=d.via, slack=slack))
    elif _repost_due(ctx, item) and _repost(ctx, item, slack):
        run.add("reposted", item.short_id)


# -- escalation (SPEC 11) ---------------------------------------------------------------------------


def _permalink(slack: Any, channel: str, slack_ts: str) -> str:
    if slack is None or not channel or not slack_ts:
        return ""
    try:
        return slack.permalink(channel, slack_ts)
    except (ApiError, LookupError):
        return ""


def escalation_note(ctx: Context, item: ReplyItem, slack: Any) -> str:
    """What harry@spill.chat gets with the forwarded thread: the draft, the links and how to act (SPEC 11)."""
    g = ctx.settings.general
    link = _permalink(slack, item.slack_channel, item.slack_ts) or "the alert's thread in #us-outbound"
    hubspot = item.hubspot.get("link") or ("not yet in HubSpot" if item.is_warm else
                                           "not in HubSpot (only positive and referral replies go there)")
    draft = item.draft or "(no draft: write your own)"
    return (
        f"This {_describe(ctx, item)} has waited over {g.escalation_hours} hours for approval.\n\n"
        f"Draft reply (signed {item.owner or 'by its owner'}):\n{draft}\n\n"
        f"Slack: {link}\nHubSpot: {hubspot}\n\n"
        f"To send the draft, react ✅ to it or reply \"send\" in the Slack thread (\"edit: <text>\" changes it), "
        f"or run `us-outbound replies approve {item.short_id} --live`. Or reply to the prospect yourself from "
        f"{item.mailbox}. Never reply to the prospect from {g.escalation_email}."
    )


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
        subject, body = f"US Outbound: waiting {g.escalation_hours} hours, {_describe(ctx, item)}", note
    else:  # nothing about a prospect who is not a warm lead goes into HubSpot (SPEC 1.2)
        records = []
        subject = f"US Outbound: a {item.reply_class or 'reply'} item has waited {g.escalation_hours} hours"
        body = (f"Reply item {item.short_id} needs you: see the #us-outbound thread, or run `us-outbound replies list` "
                f"and `us-outbound replies approve {item.short_id} --live`.")
    how = [x for x, ok in (("task", _task_now(ctx, subject, body, records)), ("dm", _dm(ctx, slack, note))) if ok]
    return "+".join(how)


def _escalate_other(ctx: Context, row: Mapping[str, Any], slack: Any) -> str:
    """A hand-check, manual merge or kill-rule item: a HubSpot task for Harry due now, and a DM (SPEC 11)."""
    g = ctx.settings.general
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    kind = str(row.get("kind") or "item").replace("_", " ")
    link = _permalink(slack, str(row.get("slack_channel") or ""), str(row.get("slack_ts") or ""))
    body = (f"{clip(str(payload.get('summary') or ''), 300)} It has waited over {g.escalation_hours} hours. "
            + (f"Slack: {link}" if link else "See #us-outbound or `us-outbound status`.")).strip()
    records = [("companies", str(c)) for c in payload.get("hubspot_company_ids") or ()][:5]
    subject = f"US Outbound: a {kind} item has waited {g.escalation_hours} hours"
    how = [x for x, ok in (("task", _task_now(ctx, subject, body, records)), ("dm", _dm(ctx, slack, f"{subject}. {body}")))
           if ok]
    return "+".join(how)


def escalate(ctx: Context, slack: Any, run: _Run) -> None:
    """Every human-in-the-loop item open longer than escalation_hours goes to escalation_email (SPEC 11).

    Send approvals are left out: they expire at the end of their next send day instead (enrol/approvals.py).
    So is the weekly hand-check while auto_send = no: every email is approved in Slack then, and nothing
    waits for the hand-check (enrol/hand_check.py), so a task and a DM every week would ask for nothing.
    """
    g = ctx.settings.general
    cutoff = ctx.now - timedelta(hours=g.escalation_hours)
    for row in ctx.store.select(TABLE, {"status": OPEN}):
        if row.get("escalated_at") or row.get("kind") == SEND_APPROVAL_KIND:
            continue
        if row.get("kind") == HAND_CHECK_KIND and not g.auto_send:
            continue
        reply = is_reply(row)
        item = ReplyItem(row)
        started = item.created_at if reply else ts(row.get("created_at"))
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
            _thread(slack, item, f"Escalated to {g.escalation_email} after {g.escalation_hours} hours ({how}). "
                                 "It can still be approved here.")
        log("escalated", item_id=row["item_id"], kind=row.get("kind"), how=how)
        run.add("escalated", {"item": str(row["item_id"])[:8], "kind": row.get("kind"), "how": how})


# -- the job ----------------------------------------------------------------------------------------


def poll_approvals(ctx: Context) -> dict:
    """The poll_approvals job (JOB CONTRACT: run(ctx) -> summary)."""
    from us_outbound.enrol import approvals  # send approvals (Harry, 2 Oct 2026): their own module

    slack = slack_or_none(ctx)
    run = _Run()
    items = reply_items(ctx.store, WAITING)
    for item in items:
        try:
            if item.status == SENDING:
                _stuck(ctx, item, slack, run)
            else:
                _work(ctx, item, slack, run)
        except (ApiError, LookupError) as exc:  # one item's trouble never stops the others
            run.errors.append(f"{item.short_id}: {type(exc).__name__}: {str(exc)[:160]}")
    escalate(ctx, slack, run)
    sends = approvals.poll(ctx, slack)
    summary = {
        "job": JOB, "dry_run": ctx.dry_run, "slack": slack is not None, "items": len(items),
        "sent": run.sent, "skipped": run.skipped, "not_sent": run.blocked, "edited": run.edited,
        "reposted": run.reposted, "escalated": run.escalated, "unsure": run.stuck,
        "hubspot": dict(run.hubspot), "ignored_non_approvers": sum(run.ignored.values()), "errors": run.errors,
        "send_approvals": sends,
    }
    if ctx.dry_run:
        summary["would"] = run.would
    log("poll_approvals_done", run_id=ctx.run_id,
        **{k: v for k, v in summary.items() if k not in ("sent", "would", "send_approvals")},
        sent=len(run.sent), send_approvals={k: v for k, v in sends.items() if k != "would"})
    return summary


# -- the command line (`us-outbound replies list | approve | skip`) -----------------------------------


def find_item(ctx: Context, ref: str) -> ReplyItem:
    """The reply item with this id, or the only one whose id starts with it (at least 4 characters)."""
    ref = str(ref or "").strip()
    if len(ref) < 4:
        raise ValueError("give the item id from `us-outbound replies list` (at least its first 4 characters)")
    rows = [r for r in ctx.store.select(TABLE) if is_reply(r) and str(r.get("item_id") or "").startswith(ref)]
    exact = [r for r in rows if r.get("item_id") == ref]
    if exact or len(rows) == 1:
        return ReplyItem((exact or rows)[0])
    if not rows:
        raise LookupError(f"no reply item {ref!r}; `us-outbound replies list` shows the open ones")
    raise ValueError(f"{ref!r} matches {len(rows)} items; give more of the id")


def list_items(ctx: Context) -> list[dict]:
    """The reply items not yet handled, oldest first, as `replies list` prints them."""
    out = []
    for item in reply_items(ctx.store, WAITING):
        account, contact = _names(ctx, item)
        waited = (ctx.now - item.created_at).total_seconds() / 3600 if item.created_at else None
        out.append({
            "id": item.short_id, "item_id": item.id, "status": item.status, "reply_class": item.reply_class,
            "account": account.get("clean_name") or account.get("domain") or "",
            "person": " ".join(str(contact.get(k) or "") for k in ("first_name", "last_name")).strip(),
            "role": contact.get("role") or "", "title": contact.get("title") or "",
            "mailbox": item.mailbox, "owner": item.owner, "waited_hours": round(waited, 1) if waited is not None else None,
            "excerpt": clip(" ".join(item.excerpt.split()), EXCERPT_CHARS), "draft": item.draft,
            "draft_edited": item.draft_edited, "referral": item.referral or None,
            "hubspot": item.hubspot.get("link") or "", "in_slack": bool(item.slack_ts),
            "escalated": bool(item.row.get("escalated_at")),
            "note": ("a send may not have finished: check the mailbox's Sent folder" if item.status == SENDING
                     else "the last send failed: check the mailbox's Sent folder" if item.desk.get("failed")
                     else "a run stopped while sending: check the mailbox's Sent folder" if item.desk.get("unsure")
                     else ""),
        })
    return out


def _actionable(item: ReplyItem) -> None:
    if item.status not in ACTIONABLE:
        raise ValueError(f"item {item.short_id} is {item.status or 'without a status'}; there is nothing to act on")


def approve(ctx: Context, ref: str, *, text: str | None = None) -> dict:
    """`replies approve`: the same HubSpot, send and close path as a Slack approval, approved_by "cli"."""
    item = find_item(ctx, ref)
    _actionable(item)
    slack = slack_or_none(ctx)
    hubspot: Counter[str] = Counter()
    errors: list[str] = []
    item = _hubspot(ctx, item, slack, hubspot, errors)
    final = item.draft if text is None else text.strip()
    edited = item.draft_edited or (text is not None and final != item.draft.strip())
    if text is not None and ctx.live and final != item.draft.strip():
        save_payload(ctx.store, item.id, draft=final, **({} if item.draft_edited else {"draft_original": item.draft}))
        item = _reload(ctx, item)
    result = send_reply(ctx, item, final, by=CLI_APPROVER, edited=edited, via="cli", slack=slack)
    return {"dry_run": ctx.dry_run, **result, "hubspot": dict(hubspot), "errors": errors}


def skip(ctx: Context, ref: str) -> dict:
    """`replies skip`: handled, and nothing sent; approved_by "cli"."""
    item = find_item(ctx, ref)
    _actionable(item)
    return {"dry_run": ctx.dry_run, **skip_item(ctx, item, by=CLI_APPROVER, via="cli", slack=slack_or_none(ctx))}
