"""The reply desk's job: poll_approvals, every 5 minutes (SPEC 9; SPEC 11 "Approval" and "Escalation"; D11).

The job keeps its name (heartbeat history, the overlap lock, health.EXPECTED, docs/daily.md); its function is
poll_desk, which runs the reply desk (steps 1 to 4 below), escalation (replies/escalation.py), and then the weekly
hand-check tab (enrol/hand_check_tab.apply), each in its own guard, so one failing never stops the other (W2-1).
Split from replies/desk.py in W2-4 (20 Oct 2026): the items and their close path are replies/desk.py.

For each reply item not yet handled:
  1. HubSpot. A positive or referral reply not yet written to HubSpot gets the SPEC 11 writes
     (desk.write_hubspot) and its thread gets the record's link.
  2. Slack. The thread's new replies, then the reactions on the current draft, are read. Only an approver counts
     (decision D11, Harry, 1 Oct 2026): a Slack id on approver_slack_ids, or the owner of the mailbox the prospect
     wrote to (Mailboxes slack_id), for that mailbox only. Anyone else is ignored.
       ✅ on the draft, or "send"   send the draft ("approve" too);
       "send: <text>"              send that text instead (SPEC 11);
       "edit: <text>"              the text replaces the draft, which is posted again in the thread;
                                   ✅ on that message, or "send", then sends it;
       ❌ on the draft, or "skip"   handled, and nothing is sent (SPEC 11; "no", "reject" or "don't send" too);
       "class: <class>"            the reply's class corrected (W3-4, 21 Oct 2026; design-final §5 L11): one
                                   reply_class_corrected event (desk.correct_class), which every outcome and report
                                   takes; it decides nothing about the answer and the thread reading goes on.
     Thread replies count in order, then the reactions on the current draft message; ❌ beats ✅. A ✅ on a draft
     that was edited since, or whose send failed, no longer counts. The bot seeds ✅ and ❌ on each alert
     (replies/poll.py) and on a changed draft; its own reactions and notes are neither decisions nor ignored.
  3. Send (desk.send_reply), or skip (desk.skip_item).
  4. Re-post (SPEC 11, D11). An alert unanswered for 2 hours is re-posted in its thread and to the channel,
     mentioning the approvers, between 13:00 and 23:00 UK. Once per item.
An item left "sending" by a run that died goes back to open with a note (never resent alone).
Send approvals were removed with Harry's D6 (W2-1, 20 Oct 2026). A send_approval item still open is closed as expired
by a live run (close_send_approvals); one caught "sending" is listed under sending_errors, which heartbeat_check
posts. The pass costs one select a run once none is left (kept in W2-4 until production shows none open: one count
of hitl_items kind send_approval with status open or sending).

Dry-run: Slack and HubSpot are read and every write is worked out; the summary (and so the heartbeat) lists each
decision, send, re-post and escalation; nothing is posted, sent, forwarded or changed in the database.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Any

from us_outbound.base import say
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.clients.slack import APPROVE_REACTIONS, SKIP_REACTIONS
from us_outbound.context import UK, Context
from us_outbound.logs import log
from us_outbound.replies.desk import (
    ESCALATED,
    HANDLED,
    HOW_TO_ANSWER,
    LIST_LIMIT,
    OPEN,
    SENDING,
    TABLE,
    WAITING,
    ReplyItem,
    approvers_for,
    class_label,
    correct_class,
    describe_item,
    esc,
    mentions,
    post_in_thread,
    refused,
    reload_item,
    reply_items,
    save_payload,
    seed,
    send_reply,
    skip_item,
    slack_or_none,
    write_hubspot,
)
from us_outbound.replies.escalation import SEND_APPROVAL_KIND, escalate
from us_outbound.settings.model import ESCALATION_HOURS
from us_outbound.timeparse import utc

JOB = "poll_approvals"
SEND_APPROVAL_EVENT, SEND_APPROVAL_PREFIX = "send_approval", "send-approval:"  # events.type, events.event_id prefix

SEND_APPROVALS_REMOVED = "send approvals were removed (W2-1, 20 Oct 2026); nothing was sent"

# A send approval caught "sending" when they were removed: its lead may or may not be in Instantly. Posted by
# heartbeat_check (a key ending "_errors"), once a UK day, until a person closes the row.
SEND_APPROVAL_STUCK = ("send approval {item_id} was caught sending when send approvals were removed: look for the lead "
                       "in the owner's Instantly campaign, then set the hitl_items row's status to handled")

REPOST_AFTER = timedelta(hours=2)  # SPEC 11

REPOST_FROM, REPOST_UNTIL = time(13), time(23)  # UK time; D11 (Harry, 1 Oct 2026): until 23:00, not 21:00



# -- Slack text -----------------------------------------------------------------------------------

_SLACK_LINK = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")

# The words a draft takes (9 Oct 2026: "approve" in a reply thread was dropped without a word).
_SEND = re.compile(r"^(?:send|approve)[\s.!]*$", re.I)

_SKIP = re.compile(r"^(?:skip|no|reject|don'?t send)[\s.!]*$", re.I)

_SEND_TEXT = re.compile(r"^send\s*:\s*(.*)$", re.I | re.S)

_EDIT = re.compile(r"^edit\s*:\s*(.*)$", re.I | re.S)
# "class: referral", "class: not now" (W3-4): one of replies/classify.CLASSES, spaces read as underscores.
_CLASS = re.compile(r"^class\s*:\s*([a-z_ -]+?)[\s.!]*$", re.I)



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
    kind: str  # send | send_text | edit | skip | class
    text: str = ""


def parse_command(text: Any) -> Command | None:
    """An approver's thread reply as a command, or None if it is not one (SPEC 11; D11)."""
    t = slack_text(text)
    if _SEND.match(t):
        return Command("send")
    if _SKIP.match(t):
        return Command("skip")
    m = _CLASS.match(t)
    if m:
        from us_outbound.replies.classify import CLASSES

        cls = re.sub(r"[\s-]+", "_", m.group(1).strip().lower())
        return Command("class", cls) if cls in CLASSES else None
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
    classes: list[tuple[str, str, str]] = field(default_factory=list)  # (user, class, message ts): class: X
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
        if cmd.kind == "class":  # a correction, not a decision: read on
            d.classes.append((user, cmd.text, d.seen_ts))
        elif cmd.kind == "edit":
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
            desk["approve_ts"] = post_in_thread(slack, item, f":pencil2: *Draft changed by <@{user}>.* To send this version: "
                                                      "✅ on this message, or reply `send`. To send nothing: ❌ or "
                                                      "`skip`. To change it again: `edit: &lt;new text&gt;`.\n"
                                                      f">>> {esc(d.draft)}")
            seed(slack, item.slack_channel, desk["approve_ts"])
    save_payload(ctx.store, item.id, desk=desk, **extra)
    return reload_item(ctx, item)


def _repost_due(ctx: Context, item: ReplyItem) -> bool:
    if item.status != OPEN or item.row.get("reposted_at") or not item.slack_ts or item.created_at is None:
        return False
    if ctx.now - item.created_at < REPOST_AFTER:
        return False
    return REPOST_FROM <= ctx.now.astimezone(UK).time() < REPOST_UNTIL


def _repost(ctx: Context, item: ReplyItem, slack: Any) -> bool:
    hours = int((ctx.now - item.created_at).total_seconds() // 3600) if item.created_at else 2
    g = ctx.settings.general
    text = "\n".join([
        f"{mentions(approvers_for(ctx.settings, item.mailbox))} {say.DECIDE} *Still waiting after {hours} hours: "
        f"{esc(describe_item(ctx, item))}*",
        f"They wrote to {item.mailbox}. Nobody has sent or skipped the answer yet.",
        f"*What to do:* in this thread, {HOW_TO_ANSWER}. Or in a terminal: "
        + say.command(f"replies approve {item.short_id} --live") + ".",
        f"If nobody acts, it goes to {g.escalation_email} once it has waited {ESCALATION_HOURS} hours.",
    ])
    if not post_in_thread(slack, item, text, broadcast=True):
        return False
    ctx.store.update(TABLE, {"item_id": item.id}, {"reposted_at": ctx.now})
    return True


@dataclass
class _Run:
    sent: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    corrected: list[dict] = field(default_factory=list)  # class: X (W3-4)
    reposted: list[str] = field(default_factory=list)
    escalated: list[dict] = field(default_factory=list)
    by_hand: list[dict] = field(default_factory=list)
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
    started = utc((item.desk.get("sending") or {}).get("at"))
    if started is not None and ctx.now - started < timedelta(minutes=10):
        return  # a send may still be going
    run.add("stuck", item.short_id)
    if ctx.dry_run:
        return
    note = post_in_thread(slack, item, f"{say.ACTION} *Not sure the answer went out:* the run sending it stopped part-way.\n"
                                f"*What to do:* look in the Sent folder of {item.mailbox}. Not there: reply `send` to "
                                "send it. There: reply `skip` to close this.")
    desk = {**item.desk, "approve_ts": note, "unsure": item.desk.get("sending")}
    desk.pop("sending", None)
    previous = ESCALATED if item.row.get("escalated_at") else OPEN
    save_payload(ctx.store, item.id, {"status": previous}, desk=desk)
    log("desk_unsure", item_id=item.id)


def _work(ctx: Context, item: ReplyItem, slack: Any, run: _Run) -> None:
    item = write_hubspot(ctx, item, slack, run.hubspot, run.errors)
    if slack is None or not item.slack_channel or not item.slack_ts:
        return
    approvers = approvers_for(ctx.settings, item.mailbox)
    if run.bot is None:
        run.bot = slack.bot_user_id()
    d = read_decision(slack, item, approvers, run.bot)
    run.ignored.update(u for u in d.ignored if u)
    if ctx.dry_run:
        for user, cls, ts in d.classes:
            run.add("would", correct_class(ctx, item, cls, by=user, ref=ts))
        if d.action or d.edits:
            run.add("would", {"item": item.short_id, "action": d.action or "edit", "by": d.by, "via": d.via,
                              "edited": d.edited})
        elif _repost_due(ctx, item):
            run.add("would", {"item": item.short_id, "action": "repost"})
        return
    if d.edits:
        run.add("edited", item.short_id)
    for user, cls, ts in d.classes:
        run.add("corrected", correct_class(ctx, item, cls, by=user, ref=ts))
        post_in_thread(slack, item, f"{say.INFO} Class recorded as *{esc(class_label(cls))}* (it was "
                                    f"{esc(class_label(item.reply_class))}), by <@{user}>: the reports count it so. "
                                    "Nothing was sent.")
    item = _apply_reading(ctx, item, d, slack)
    if d.action == "send":
        res = send_reply(ctx, item, d.text, by=d.by, edited=d.edited, via=d.via, slack=slack)
        run.add("sent" if res.get("sent") else "blocked", res)
    elif d.action == "skip":
        run.add("skipped", skip_item(ctx, item, by=d.by, via=d.via, slack=slack))
    elif _repost_due(ctx, item) and _repost(ctx, item, slack):
        run.add("reposted", item.short_id)



# -- the job ----------------------------------------------------------------------------------------



def close_send_approvals(ctx: Context) -> dict:
    """Close the send approvals still open, as expired by "system" (W2-1, 20 Oct 2026): the code that answered them is
    gone, and the escalation loop must never meet a kind nothing handles. Live only; idempotent, so it costs one
    select once none is left. An item caught "sending" (an add that may have reached Instantly) is not closed: it is
    listed under sending_errors (SEND_APPROVAL_STUCK), which heartbeat_check posts, for a person to check the
    campaign by hand. Remove this pass in W2-4 (design-final W2-1 review)."""
    rows = ctx.store.select(TABLE, {"kind": SEND_APPROVAL_KIND, "status": [OPEN, SENDING]})
    stuck = sorted(str(r.get("item_id")) for r in rows if r.get("status") == SENDING)
    todo = [r for r in rows if r.get("status") == OPEN]
    closed: list[str] = []
    for r in todo if ctx.live else ():
        payload = dict(r.get("payload") or {}) if isinstance(r.get("payload"), Mapping) else {}
        payload.update(outcome="expired", reason=SEND_APPROVALS_REMOVED, state="done",
                       decided={"by": "system", "at": ctx.now.isoformat(), "via": "removed", "outcome": "expired"})
        if not ctx.store.update(TABLE, {"item_id": r["item_id"], "status": OPEN},
                                {"status": HANDLED, "handled_at": ctx.now, "handled_by": "system", "payload": payload}):
            continue  # closed in the meantime
        ctx.store.upsert("events", [{
            "event_id": f"{SEND_APPROVAL_PREFIX}{r['item_id']}", "type": SEND_APPROVAL_EVENT, "approval": "expired",
            "approved_by": "system", "account_id": r.get("account_id") or None,
            "contact_id": r.get("contact_id") or None, "step": 1, "occurred_at": ctx.now,
        }])
        closed.append(str(r["item_id"]))
    out = {"closed": closed, "would_close": [] if ctx.live else sorted(str(r["item_id"]) for r in todo)}
    if stuck:
        out["sending_errors"] = [SEND_APPROVAL_STUCK.format(item_id=i) for i in stuck]  # heartbeat_check posts these
    if closed or stuck:
        log("send_approvals_closed", run_id=ctx.run_id, closed=len(closed), sending=len(stuck))
    return out


def poll_desk(ctx: Context) -> dict:
    """The poll_approvals job (JOB CONTRACT: run(ctx) -> summary): the reply desk, then the weekly hand-check tab, each
    in its own guard, so one failing never stops the other. The hand-check tab's failure is listed
    (hand_check_errors: heartbeat_check reads it) and the run is ok. The reply desk's own failure (escalate, reply_items,
    a store error) and a guardrail's refusal of either part are raised once the hand-check tab has run, so the
    heartbeat records status error with its text and heartbeat_check's failure_rate and missed rules see it, as
    before W2-1 (run truth, W1-1). One item's trouble is still listed under errors and the other items go on."""
    from us_outbound.enrol import hand_check_tab  # the weekly hand-check's decisions on the sheet (12 Oct 2026)

    slack = slack_or_none(ctx)
    run = _Run()
    items: list[ReplyItem] = []
    refused_by: GuardViolation | None = None
    failed_by: Exception | None = None  # the reply desk's own failure, raised once the hand-check tab has run
    try:
        items = reply_items(ctx.store, WAITING)
        for item in items:
            try:
                if item.status == SENDING:
                    _stuck(ctx, item, slack, run)
                else:
                    _work(ctx, item, slack, run)
            except (ApiError, LookupError) as exc:  # one item's trouble never stops the others
                run.errors.append(f"{item.short_id}: {type(exc).__name__}: {str(exc)[:160]}")
            except GuardViolation as exc:  # a guardrail refused it: a person answers by hand; the rest go on (M2)
                run.errors.append(f"{item.short_id}: refused by a guardrail: {str(exc)[:160]}")
                run.add("by_hand", refused(ctx, item, exc))
        escalate(ctx, slack, run)
        sends = close_send_approvals(ctx)
    except GuardViolation as exc:
        refused_by, sends = exc, {}
        run.errors.append(f"the reply desk was refused by a guardrail: {str(exc)[:160]}")
    except Exception as exc:  # the hand-check tab still runs; then it is raised (failed_by)
        failed_by, sends = exc, {}
        run.errors.append(f"the reply desk failed: {type(exc).__name__}: {str(exc)[:160]}")
        log("poll_desk_failed", run_id=ctx.run_id, part="desk", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    hand_check_errors: list[str] = []
    try:
        checked = hand_check_tab.apply(ctx)
    except GuardViolation as exc:
        checked, refused_by = None, refused_by or exc
        hand_check_errors.append(f"refused by a guardrail: {str(exc)[:160]}")
    except Exception as exc:  # the reply desk has run; heartbeat_check reads the line
        checked = None
        hand_check_errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
        log("poll_desk_failed", run_id=ctx.run_id, part="hand_check", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    summary = {
        "job": JOB, "dry_run": ctx.dry_run, "slack": slack is not None, "items": len(items),
        "sent": run.sent, "skipped": run.skipped, "not_sent": run.blocked, "edited": run.edited, "corrected": run.corrected,
        "reposted": run.reposted, "escalated": run.escalated, "unsure": run.stuck, "by_hand": run.by_hand,
        "hubspot": dict(run.hubspot), "ignored_non_approvers": sum(run.ignored.values()), "errors": run.errors,
        "send_approvals": sends, "hand_check": checked, "hand_check_errors": hand_check_errors,
    }
    if ctx.dry_run:
        summary["would"] = run.would
    log("poll_approvals_done", run_id=ctx.run_id,
        **{k: v for k, v in summary.items() if k not in ("sent", "would")}, sent=len(run.sent))
    if refused_by is not None:
        raise refused_by
    if failed_by is not None:
        raise failed_by
    return summary
