"""The reply desk's items and their one close path (SPEC 11 "Approval"; D11): the hitl_items rows poll_replies opens
for each reply a human must answer, sending the approved text, skipping, and the command line's `replies list | send
| skip`.

W2-4 (20 Oct 2026) split the old replies/desk.py three ways and folded replies/items.py in here:
  * this module: the item as the desk reads it (ReplyItem, the contract with poll_replies below), the send and skip
    path that Slack and the command line share (send_reply, skip_item, refused), the HubSpot writes for a warm
    reply (write_hubspot), and the command line (list_items, approve, skip);
  * replies/poll_desk.py: the poll_approvals job, every 5 minutes: each item's Slack thread read for an approver's
    decision, re-posts, then escalation and the weekly hand-check tab;
  * replies/escalation.py: an item open longer than ESCALATION_HOURS (24) goes to escalation_email (SPEC 11).

THE ITEM (the hitl_items contract with poll_replies, replies/poll.py). One row for each reply that needs a human:
kind "reply", status "open", and a payload with contact_id, account_id, mailbox (the registry address the prospect
wrote to), owner, instantly_email_id, the reply-to id, reply_class, reply_excerpt, draft, referral ({name, title,
email} or null), received_at, and slack_channel and slack_ts once the alert is posted. Everything is read
defensively: a missing key is blank, never an error, and the row's own columns (account_id, contact_id, event_id,
slack_channel, slack_ts, created_at) stand in when the payload lacks them. Keys read beyond the contract (each
optional): demo_requested (also under payload.classification), subject, and reply_to_uuid / reply_to_id /
instantly_reply_to for the id Instantly's reply endpoint answers. The desk keeps its own state under payload.desk
and the HubSpot writes under payload.hubspot; nothing else in the payload is changed, except draft when an approver
edits it (the first version is kept as draft_original).

Status: open → handled (sent or skipped), or → escalated after ESCALATION_HOURS (still open to approval). "sending"
is held only while a reply goes out (a compare-and-set on status, so a scheduled run and `us-outbound replies send`
can never both send it).

SENDING (send_reply). From the mailbox the prospect wrote to, in the thread, through Instantly's reply endpoint
(clients/instantly.reply: a US Outbound campaign's thread and a registry mailbox; the guard checks the approver
too). The text must pass the copy rules' word and line checks (SPEC 1.9) or it is not sent, and the thread says why.
Then the thread confirms ("Sent from hannah@meetspill.org at 14:32 UK"), an events row records the approval
(approved, edited or skipped, with approved_by) and the item is handled. A sent reply's row is type reply_sent, not
sent: it uses the mailbox but is no campaign send. If a run dies mid-send the item is not resent by itself: the
thread (and `replies list`) asks a person to check. Never to a suppressed address (W1-2, 19 Oct 2026;
code-replies-crm M1): if the person who wrote, or the contact, is suppressed now, the send is refused (why
suppressed_since), and the thread says to answer by hand. A send the guard refuses (a colleague's thread with no US
Outbound campaign id, M2) closes the item "by hand" with one alert (refused).

Without Slack (no US_OUTBOUND_SLACK_BOT_TOKEN) the command line does the desk's work: `us-outbound replies list |
send | skip` take the same send, HubSpot and close path, with approved_by "cli".

Dry-run: every write is worked out and reported; nothing is posted, sent or changed in the database.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from us_outbound import suppression
from us_outbound.base import notify, say
from us_outbound.clients.db import Store
from us_outbound.clients.guard import CLI_APPROVER, WARM_REPLY_CLASSES, GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, ConfigError, Context
from us_outbound.copy.copy_rules import content_violations, structure_violations
from us_outbound.logs import clip, log
from us_outbound.settings.model import Settings
from us_outbound.timeparse import utc

TABLE = "hitl_items"
# "reply" is the contract with poll_replies; "reply_approval" is the name sql/ddl/09 and enrol used first.
REPLY_KINDS = ("reply", "reply_approval")
OPEN, ESCALATED, SENDING, HANDLED = "open", "escalated", "sending", "handled"
ACTIONABLE = (OPEN, ESCALATED)  # an approver may still act on these
WAITING = (OPEN, ESCALATED, SENDING)  # not yet handled
SEED_REACTIONS = ("white_check_mark", "x")  # the bot's own ✅ and ❌ on an alert or a changed draft: one click decides
APPROVED, EDITED, SKIPPED = "approved", "edited", "skipped"  # events.approval (SPEC 6)
SENT_EVENT = "reply-sent:"  # events.event_id of a sent reply when Instantly returns no id
# events.type of a reply the desk sent: outcomes.REPLY_SENT, written out here as outcomes imports optout, which
# imports this module.
REPLY_SENT = "reply_sent"
LIST_LIMIT = 50  # per-list entries kept in a summary
SUPPRESSED_SINCE = "suppressed_since"  # send_reply's why: the address is suppressed now (W1-2, code-replies-crm M1)
BY_HAND = "by hand"  # events.approval when a guardrail refused the desk's send: a person answers (M2)
EXCERPT_CHARS = 200  # SPEC 1.7: at most 200 characters of any email body leave the database


# Each reply class in plain words, for a reader new to the system (Harry, 12 Oct 2026): (label, what it means).
# The alert (replies/poll.py), the thread notes and the escalation DM all use it.
CLASS_WORDS: dict[str, tuple[str, str]] = {
    "positive": ("Interested", "they want to talk or hear more"),
    "referral": ("Referral", "they point us to someone else"),
    "objection": ("Objection", "they gave a reason not to go ahead"),
    "not_now": ("Not now", "maybe later, but not now"),
    "negative": ("Not interested", "they said no"),
    "wrong_person": ("Wrong person", "they are not the one to talk to"),
    "out_of_office": ("Out of office", "an automatic away message"),
    "unsubscribe": ("Asked to stop", "they want no more emails"),
    "other": ("Unclear", "the system could not tell what they want: read it yourself"),
}




def class_label(reply_class: Any) -> str:
    """The class as a reader says it: "Interested", "Not now", "Wrong person"."""
    cls = str(reply_class or "").strip().lower()
    return CLASS_WORDS.get(cls, (cls.replace("_", " ").capitalize() or "Unclear", ""))[0]


def class_meaning(reply_class: Any) -> str:
    return CLASS_WORDS.get(str(reply_class or "").strip().lower(), ("", ""))[1]


def esc(text: Any) -> str:
    """Slack mrkdwn escaping, for text a prospect or an approver wrote."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def quoted(text: Any) -> str:
    """Text as a Slack quote, line by line."""
    return "\n".join(f"> {esc(line)}" for line in str(text or "").split("\n"))




# What each reaction or reply does on a reply alert, for the notes that repeat it (the words are desk's commands).
HOW_TO_ANSWER = ("✅ on the draft (or reply `send`) sends it · `edit: &lt;new text&gt;` changes it first · "
                 "❌ or `skip` sends nothing (you answer yourself)")




# -- the item (the contract with poll_replies) ------------------------------------------------------------



def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


@dataclass(frozen=True)
class ReplyItem:
    """Read-only view of one hitl_items row of a reply kind."""

    row: Mapping[str, Any]

    @property
    def payload(self) -> dict:
        p = self.row.get("payload")
        return dict(p) if isinstance(p, Mapping) else {}

    def _get(self, key: str) -> str:
        """The payload's value, else the row's column of the same name."""
        return _text(self.payload.get(key)) or _text(self.row.get(key))

    @property
    def id(self) -> str:
        return _text(self.row.get("item_id"))

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def status(self) -> str:
        return _text(self.row.get("status")).lower()

    @property
    def account_id(self) -> str:
        return self._get("account_id")

    @property
    def contact_id(self) -> str:
        return self._get("contact_id")

    @property
    def mailbox(self) -> str:
        return _text(self.payload.get("mailbox")).lower()

    @property
    def owner(self) -> str:
        return _text(self.payload.get("owner"))

    @property
    def reply_class(self) -> str:
        return _text(self.payload.get("reply_class")).lower()

    @property
    def is_warm(self) -> bool:
        return self.reply_class in WARM_REPLY_CLASSES

    @property
    def excerpt(self) -> str:
        return _text(self.payload.get("reply_excerpt"))

    @property
    def draft(self) -> str:
        p = self.payload.get("draft")
        return "" if p is None else str(p).strip("\n")

    @property
    def draft_edited(self) -> bool:
        return "draft_original" in self.payload

    @property
    def referral(self) -> dict:
        r = self.payload.get("referral")
        return {k: _text(r.get(k)) for k in ("name", "title", "email")} if isinstance(r, Mapping) else {}

    @property
    def subject(self) -> str:
        return _text(self.payload.get("subject"))

    @property
    def email_id(self) -> str:
        """The prospect's email in Instantly (the replied event's id, SPEC 6)."""
        return _text(self.payload.get("instantly_email_id")) or _text(self.row.get("event_id"))

    @property
    def reply_to(self) -> str:
        """The id Instantly's reply endpoint answers (PHASE0-CONFIRM): an explicit reply-to id, else the email's."""
        for key in ("reply_to_uuid", "reply_to_id", "instantly_reply_to"):
            if _text(self.payload.get(key)):
                return _text(self.payload.get(key))
        return self.email_id

    @property
    def demo_requested(self) -> bool:
        p = self.payload
        c = p.get("classification") if isinstance(p.get("classification"), Mapping) else {}
        return bool(p.get("demo_requested") or c.get("demo_requested"))

    @property
    def received_at(self) -> datetime | None:
        return utc(self.payload.get("received_at"))

    @property
    def created_at(self) -> datetime | None:
        """When the item started waiting: its created_at, else the reply's received_at."""
        return utc(self.row.get("created_at")) or self.received_at

    @property
    def slack_channel(self) -> str:
        return self._get("slack_channel")

    @property
    def slack_ts(self) -> str:
        return self._get("slack_ts")

    @property
    def desk(self) -> dict:
        d = self.payload.get("desk")
        return dict(d) if isinstance(d, Mapping) else {}

    @property
    def hubspot(self) -> dict:
        h = self.payload.get("hubspot")
        return dict(h) if isinstance(h, Mapping) else {}


def is_reply(row: Mapping[str, Any]) -> bool:
    return _text(row.get("kind")).lower() in REPLY_KINDS


def reply_items(store: Store, statuses: tuple[str, ...] = ACTIONABLE) -> list[ReplyItem]:
    """Reply items in these statuses, oldest first."""
    rows = [r for r in store.select(TABLE, {"status": list(statuses)}) if is_reply(r)]
    items = [ReplyItem(r) for r in rows]
    return sorted(items, key=lambda i: (i.created_at or datetime.max.replace(tzinfo=UTC), i.id))


def save_payload(store: Store, item_id: str, values: Mapping[str, Any] | None = None, **payload: Any) -> dict:
    """Merge these top-level payload keys into the row as it is now (re-read first, so poll_replies' keys stay).

    values: other columns to set on the row (status, handled_at, ...). Returns the new row.
    """
    row = store.get(TABLE, item_id=item_id) or {"item_id": item_id}
    merged = {**(row.get("payload") if isinstance(row.get("payload"), Mapping) else {}), **payload}
    store.update(TABLE, {"item_id": item_id}, {"payload": merged, **dict(values or {})})
    return {**row, **dict(values or {}), "payload": merged}


def claim(store: Store, item: ReplyItem, status: str) -> bool:
    """Move the item from the status it was read in to `status`; False if another run changed it first.

    UPDATE ... WHERE item_id = x AND status = y touches the row only if nobody moved it since:
    the lock that keeps a reply from going out twice.
    """
    return store.update(TABLE, {"item_id": item.id, "status": item.row.get("status")}, {"status": status}) == 1


def positive_waiting(store: Store, now: datetime, hours: int) -> list[ReplyItem]:
    """Positive or referral replies not yet handled that have waited more than `hours` (SPEC 11 enrolment pause).

    An item without a reply_class counts: the first kind name (reply_approval) was used only for them.
    """
    cutoff = now - timedelta(hours=hours)
    out = []
    for item in reply_items(store, WAITING):
        if item.reply_class and not item.is_warm:
            continue
        if item.created_at is not None and item.created_at <= cutoff:
            out.append(item)
    return out



# -- who may approve (D11), and Slack -------------------------------------------------------------------



def approvers_for(settings: Settings, mailbox: str) -> frozenset[str]:
    """approver_slack_ids, and the owner of this mailbox when their Mailboxes row has a slack_id (D11)."""
    ids = {x.strip() for x in settings.general.approver_slack_ids if x.strip()}
    for m in settings.mailboxes:
        if m.address.lower() == mailbox.lower() and m.slack_id and m.status != "Retired":
            ids.add(m.slack_id)
    return frozenset(ids)


def mentions(ids: Iterable[str]) -> str:
    return " ".join(f"<@{u}>" for u in sorted(ids))


def slack_or_none(ctx: Context) -> Any | None:
    """The Slack client, or None (logged) when no bot token is set: the desk then works without Slack."""
    try:
        slack = ctx.clients.slack
    except ConfigError:
        slack = None
    if slack is None or not slack.connected:
        log("slack_not_configured", job=ctx.job,
            reason="US_OUTBOUND_SLACK_BOT_TOKEN is not set: Slack approvals and re-posts are skipped; "
                   "use `us-outbound replies list|approve|skip`")
        return None
    return slack


def reload_item(ctx: Context, item: ReplyItem) -> ReplyItem:
    row = ctx.store.get(TABLE, item_id=item.id)
    return ReplyItem(row) if row is not None else item


def post_in_thread(slack: Any, item: ReplyItem, text: str, *, broadcast: bool = False) -> str:
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


def parties(ctx: Context, item: ReplyItem) -> tuple[dict, dict]:
    account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
    contact = ctx.store.get("contacts", contact_id=item.contact_id) if item.contact_id else None
    return account or {}, contact or {}



# -- the shared close path (Slack and the command line) -------------------------------------------------



def copy_problems(ctx: Context, item: ReplyItem, text: str) -> list[str]:
    """SPEC 1.9: the copy rules' word and line checks on the text about to be sent."""
    if not text.strip():
        return ["there is no draft to send: write one with \"send: <text>\" in the Slack thread, or "
                + say.command(f'replies approve {item.short_id} --text "<text>" --live')]
    g = ctx.settings.general
    account, contact = parties(ctx, item)
    owner = item.owner or next((m.owner_name for m in ctx.settings.mailboxes if m.address.lower() == item.mailbox), "")
    exempt = [str(x) for x in (account.get("clean_name"), contact.get("first_name"), contact.get("last_name")) if x]
    return [*content_violations(text, sender_is_harry=owner == g.demo_host, demo_host=g.demo_host, exempt=exempt),
            *structure_violations(text)]


def _record_skip_event(ctx: Context, item: ReplyItem, by: str, approval: str = SKIPPED) -> None:
    """approval (skipped, or by hand) on the prospect's replied event (SPEC 6), added if poll_replies has not made it."""
    if not item.email_id:
        return
    values = {"approval": approval, "approved_by": by}
    if not ctx.store.update("events", {"event_id": item.email_id}, values):
        ctx.store.upsert("events", [{
            "event_id": item.email_id, "type": "replied", "account_id": item.account_id or None,
            "contact_id": item.contact_id or None, "mailbox": item.mailbox or None,
            "reply_class": item.reply_class or None, "occurred_at": item.received_at or item.created_at or ctx.now,
            **values,
        }])


def suppressed_since(ctx: Context, item: ReplyItem) -> bool:
    """The person who wrote, or the contact we emailed, is suppressed now: they asked to stop (or bounced, or their
    company was kept out) after the reply came in, or in the reply itself (replies/poll.py also_asked_to_stop)."""
    _, contact = parties(ctx, item)
    if contact.get("suppressed"):
        return True
    addresses = {str(a).strip().lower() for a in (contact.get("email"), item.payload.get("from_address")) if a}
    return any(suppression.is_suppressed(ctx.store, email=a, now=ctx.now) for a in addresses if "@" in a)


def send_reply(ctx: Context, item: ReplyItem, text: str, *, by: str, edited: bool, via: str,
               slack: Any = None) -> dict:
    """Send the approved text from the mailbox the prospect wrote to, then close the item (SPEC 11 Approval).

    Never to a suppressed address (W1-2, 19 Oct 2026; code-replies-crm M1): an approval given after the person
    asked to stop is refused (why SUPPRESSED_SINCE), the approval is spent, and the thread says to answer by hand
    or lift the suppression knowingly."""
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "via": via, "by": by}
    if suppressed_since(ctx, item):
        result.update(sent=False, why=SUPPRESSED_SINCE,
                      problems=["they are on the do-not-contact list now (they asked to stop, or bounced)"])
        if ctx.live:
            note = post_in_thread(slack, item, f"{say.ACTION} *Not sent: they are on our do-not-contact list now* (they "
                                        "asked to be taken off the list, or their address bounced).\n*What to do:* "
                                        f"if they still want an answer, reply to them yourself from {item.mailbox}, "
                                        "then reply `skip` here; or lift the suppression knowingly first.")
            save_payload(ctx.store, item.id, desk={**item.desk, "approve_ts": note, "blocked": [SUPPRESSED_SINCE]})
        log("desk_blocked", item_id=item.id, problems=[SUPPRESSED_SINCE])
        return result
    problems = copy_problems(ctx, item, text)
    if not item.mailbox or not item.reply_to:
        problems.append("the item names no mailbox or Instantly email to reply to")
    elif item.mailbox not in ctx.guard.bounds.registry_addresses:
        problems.append(f"{item.mailbox} is not in the mailbox registry, so it cannot send")
    if problems:
        result.update(sent=False, why=problems)
        if ctx.live:
            note = post_in_thread(slack, item, f"{say.ACTION} *Not sent:* {esc('; '.join(problems))}.\n"
                                        "*What to do:* reply `edit: &lt;fixed text&gt;` and then `send`, or "
                                        "`send: &lt;fixed text&gt;` to send your version straight away.")
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
        note = post_in_thread(slack, item, f"{say.ACTION} *Not sent: a safety rule stopped it* ({esc(str(exc)[:160])}).\n"
                                    f"*What to do:* answer them yourself from {item.mailbox}, then reply `skip` here "
                                    "to close this.")
        save_payload(ctx.store, item.id, {"status": item.status},
                     desk={**reload_item(ctx, item).desk, "approve_ts": note, "sending": None,
                           "failed": {**started, "error": "guardrail"}})
        raise
    except Exception as exc:  # the reply did not go, or Instantly did not say: never resent without a person
        why = f"{type(exc).__name__}: {str(exc)[:200]}"
        note = post_in_thread(slack, item, f"{say.ACTION} *Probably not sent: Instantly answered with an error* "
                                    f"({esc(why)}).\n*What to do:* look in the Sent folder of {item.mailbox}. "
                                    "Not there: reply `send` to try again. There: reply `skip` to close this.")
        desk = {**reload_item(ctx, item).desk, "approve_ts": note, "failed": {**started, "error": why}}
        desk.pop("sending", None)
        save_payload(ctx.store, item.id, {"status": item.status}, desk=desk)
        log("desk_send_failed", item_id=item.id, error=why)
        result.update(sent=False, why=[why])
        return result
    email_id = str((sent or {}).get("id") or "")
    uk_time = ctx.now.astimezone(UK).strftime("%H:%M")
    desk = {**reload_item(ctx, item).desk, "sent": {**started, "email_id": email_id, "mailbox": item.mailbox}}
    desk.pop("sending", None)
    save_payload(ctx.store, item.id, {"status": HANDLED, "handled_at": ctx.now, "handled_by": by},
                 desk=desk, sent_text=text)
    ctx.store.upsert("events", [{  # reply_sent, not sent: never counted as a campaign send
        "event_id": email_id or f"{SENT_EVENT}{item.id}", "type": REPLY_SENT, "step": None, "mailbox": item.mailbox,
        "account_id": item.account_id or None, "contact_id": item.contact_id or None,
        "approval": approval, "approved_by": by, "occurred_at": ctx.now,
    }])
    post_in_thread(slack, item, f"{say.DONE} *Sent* from {item.mailbox} at {uk_time} UK"
                         f"{' (the edited version)' if edited else ''}, approved {_who(by)}. Nothing more to do.")
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
    post_in_thread(slack, item, f"{say.DONE} *Closed without sending* (skipped {_who(by)}). If they need an answer, "
                         f"reply to them yourself from {item.mailbox or 'the mailbox they wrote to'}.")
    log("desk_skipped", item_id=item.id, account_id=item.account_id, by=by)
    result.update(skipped=True)
    return result


def refused(ctx: Context, item: ReplyItem, exc: GuardViolation) -> dict:
    """A guardrail refused something the desk did for this item (most likely the send: a colleague's reply whose
    thread carries no US Outbound campaign id, which Instantly.reply refuses; code-replies-crm M2). Until 19 Oct 2026
    the violation crashed poll_approvals, and with it the escalation and the hand-check tab, every 5 minutes. Now
    (live) the item is closed for a person to answer by hand: payload.refused keeps why, the replied event's approval
    is "by hand", and one alert says so. The rest of the run goes on."""
    reason = str(exc)[:300]
    out = {"item": item.short_id, "account_id": item.account_id, "refused": reason}
    if ctx.dry_run:
        return {**out, "dry_run": True}
    item = reload_item(ctx, item)
    save_payload(ctx.store, item.id, {"status": HANDLED, "handled_at": ctx.now, "handled_by": BY_HAND},
                 refused=reason, desk={**item.desk, "by_hand": {"at": ctx.now.isoformat(), "why": reason}})
    _record_skip_event(ctx, item, "guard", BY_HAND)
    who = describe_item(ctx, item)
    posted = notify.alert(ctx, say.compose(
        say.ACTION, f"Answer {esc(who)} by hand",
        [f"A safety rule stopped the reply desk sending it ({esc(reason[:160])}), so it is closed here."],
        do=f"reply to them yourself from {item.mailbox or 'the mailbox they wrote to'}."))
    log("desk_refused", item_id=item.id, error=reason)
    return {**out, "alert": posted.get("posted"), **({"alert_error": posted["error"]} if posted.get("error") else {})}


def write_hubspot(ctx: Context, item: ReplyItem, slack: Any, out: Counter[str], errors: list[str]) -> ReplyItem:
    """SPEC 11 HubSpot writes for a warm reply not yet written; its thread gets the link once.

    A guardrail refusing a HubSpot write is said like an API error, and the item stays open for its answer: the write
    is tried again next run. Before (W1-2 review, round 2) it reached poll_approvals' per-item catch, which closed the
    item "by hand", perhaps before anyone had approved it, with an alert saying a safety rule stopped the send."""
    from us_outbound.crm import hubspot_writes as hw  # it imports this module (ReplyItem, save_payload)

    if not item.is_warm or item.hubspot.get("status") == "written":
        return item
    try:
        res = hw.record_reply(ctx, item)
    except (ApiError, ConfigError) as exc:
        errors.append(f"{item.short_id}: HubSpot: {str(exc)[:160]}")
        return item
    except GuardViolation as exc:
        errors.append(f"{item.short_id}: HubSpot: refused by a guardrail: {str(exc)[:160]}")
        log("desk_hubspot_refused", item_id=item.id, error=str(exc)[:300])
        return item
    out[res.get("status", "?")] += 1
    item = reload_item(ctx, item)
    if ctx.live and res.get("status") == "written" and res.get("link") and not item.desk.get("hubspot_posted"):
        post_in_thread(slack, item, f":link: *Added to HubSpot* (the company, the contact and this reply): "
                             f"<{res['link']}|open the company in HubSpot>")
        save_payload(ctx.store, item.id, desk={**item.desk, "hubspot_posted": True})
        item = reload_item(ctx, item)
    elif ctx.live and res.get("status") == "merge_needed" and not item.desk.get("merge_posted"):
        dupes = [f"<{hw.record_link('companies', str(c))}|company {c}>" for c in (res.get("merge_ids") or ())][:5]
        post_in_thread(slack, item, f"{say.ACTION} *Not added to HubSpot: it has more than one company for this website*"
                             + (f" ({', '.join(dupes)})" if dupes else "") + ".\n"
                             "*What to do:* merge them into one in HubSpot; the reply is added within 5 minutes "
                             "after. Answering the reply does not wait for this.")
        save_payload(ctx.store, item.id, desk={**item.desk, "merge_posted": True})
        item = reload_item(ctx, item)
    return item


def describe_item(ctx: Context, item: ReplyItem) -> str:
    """"a reply from Jane Doe at Acme Creative (Interested)": the prospect is not named in a link or mention."""
    account, contact = parties(ctx, item)
    who = " ".join(str(contact.get(k) or "") for k in ("first_name", "last_name")).strip() or "a contact"
    company = account.get("clean_name") or account.get("domain") or "a company"
    return f"a reply from {who} at {company} ({class_label(item.reply_class)})"


# -- a person's correction of a reply's class (W3-4, 21 Oct 2026; design-final §5 L11) ---------------------------------


CLASS_CORRECTED = "reply_class_corrected"  # events.type (learn/outcomes.CORRECTED: the latest one counts)
CORRECTION_TABLE_FROM = 10  # corrections before the agreement and calibration table prints (L11)
CONFIDENCE_BANDS = ((0.0, 0.7, "under 0.70"), (0.7, 0.85, "0.70-0.84"), (0.85, 1.01, "0.85 and over"))


def correct_class(ctx: Context, item: ReplyItem, reply_class: str, *, by: str, ref: str = "") -> dict:
    """`class: <class>` in a reply item's thread (replies/poll_desk.py): one reply_class_corrected event, source the
    reply's events row (its Instantly email id), reply_class the class it should have. Every outcome and report
    then takes it (learn/outcomes.classes); the item's own class and the draft stay as they were, and nothing is
    sent. ref (the Slack message's ts) makes the event once per message. Dry-run: worked out, nothing written."""
    event = {"event_id": f"{CLASS_CORRECTED}:{item.email_id}:{ref or ctx.now.isoformat()}", "type": CLASS_CORRECTED,
             "source": item.email_id, "reply_class": reply_class, "approved_by": by, "occurred_at": ctx.now,
             "account_id": item.account_id or None, "contact_id": item.contact_id or None}
    out = {"item": item.short_id, "from": item.reply_class, "to": reply_class, "by": by}
    if ctx.dry_run:
        return {**out, "dry_run": True}
    ctx.store.upsert("events", [event])
    save_payload(ctx.store, item.id, class_corrected={"to": reply_class, "by": by, "at": ctx.now.isoformat()})
    log("reply_class_corrected", item_id=item.id, reply_class=reply_class, was=item.reply_class)
    return out


def correction_table(ctx: Context) -> list[str]:
    """The monthly agreement and confidence-calibration table (L11), once CORRECTION_TABLE_FROM corrections exist:
    of the reply items a person has handled, the share whose class nobody corrected, by the model's confidence."""
    corrected: dict[str, str] = {}
    for e in sorted(ctx.store.select("events", {"type": CLASS_CORRECTED}), key=lambda e: utc(e.get("occurred_at"))
                    or datetime.min.replace(tzinfo=UTC)):
        corrected[str(e.get("source") or "")] = str(e.get("reply_class") or "")
    if len(corrected) < CORRECTION_TABLE_FROM:
        return []
    handled = [ReplyItem(r) for r in ctx.store.select(TABLE, {"kind": list(REPLY_KINDS)})
               if r.get("status") == HANDLED or ReplyItem(r).email_id in corrected]
    rows = []
    for lo, hi, label in CONFIDENCE_BANDS:
        band = [i for i in handled if lo <= float(i.payload.get("confidence") or 0) < hi]
        changed = sum(corrected.get(i.email_id, i.reply_class) != i.reply_class for i in band)
        if band:
            rows.append(f"    confidence {label}: {len(band) - changed} of {len(band)} kept their class "
                        f"({(len(band) - changed) / len(band):.0%})")
    moves = Counter((i.reply_class, corrected[i.email_id]) for i in handled
                    if i.email_id in corrected and corrected[i.email_id] != i.reply_class)
    head = (f"  Reply classes a person read: {len(handled)}; corrected with `class:` {sum(moves.values())}. "
            "By the model's confidence:")
    top = ", ".join(f"{class_label(a)} → {class_label(b)} {n}" for (a, b), n in moves.most_common(5))
    return [head, *rows, *([f"    Most often: {top}."] if top else [])]



# -- the command line (`us-outbound replies list | send | skip`) -----------------------------------------



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
        account, contact = parties(ctx, item)
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
    item = write_hubspot(ctx, item, slack, hubspot, errors)
    final = item.draft if text is None else text.strip()
    edited = item.draft_edited or (text is not None and final != item.draft.strip())
    if text is not None and ctx.live and final != item.draft.strip():
        save_payload(ctx.store, item.id, draft=final, **({} if item.draft_edited else {"draft_original": item.draft}))
        item = reload_item(ctx, item)
    result = send_reply(ctx, item, final, by=CLI_APPROVER, edited=edited, via="cli", slack=slack)
    return {"dry_run": ctx.dry_run, **result, "hubspot": dict(hubspot), "errors": errors}


def skip(ctx: Context, ref: str) -> dict:
    """`replies skip`: handled, and nothing sent; approved_by "cli"."""
    item = find_item(ctx, ref)
    _actionable(item)
    return {"dry_run": ctx.dry_run, **skip_item(ctx, item, by=CLI_APPROVER, via="cli", slack=slack_or_none(ctx))}
