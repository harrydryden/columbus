"""The card's thread in Slack (9 Oct 2026, split from enrol/approvals.py): posting the card, seeding its reactions,
notes in its thread, proposing enrol's accounts as cards, and reading an approver's reply as a command.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import enrol
from us_outbound.enrol.approvals.cards import card, followups
from us_outbound.enrol.approvals.model import (
    EDITING,
    KIND,
    LIST_LIMIT,
    NO_SLACK,
    OPEN,
    PREVIEW_CARDS,
    SEED_APPROVE,
    TABLE,
    WAITING,
    Item,
    _save,
)
from us_outbound.enrol.approvals.payload import build_payload
from us_outbound.logs import log
from us_outbound.replies.desk import slack_text


def slack_or_none(ctx: Context) -> Any | None:
    """The Slack client, or None without a token (logged): the command line then does the work."""
    try:
        slack = ctx.clients.slack
    except ConfigError:
        slack = None
    if slack is None or not slack.connected:
        log("slack_not_configured", job=ctx.job, reason="send approvals wait for `us-outbound approvals list|approve|reject`")
        return None
    return slack


def slack_for(ctx: Context) -> tuple[Any | None, str | None]:
    """(the Slack client for enrol's cards, why a live run refuses). Dry-run without a token logs the previews."""
    try:
        return ctx.clients.slack, None
    except ConfigError:
        return None, NO_SLACK if ctx.live else None


# -- posting --------------------------------------------------------------------------------------------------------


def _thread(slack: Any, item: Item, text: str, blocks: list[dict] | None = None) -> str:
    """Post in the card's thread; the new message's ts (else "")."""
    if slack is None or not item.channel or not item.ts:
        return ""
    try:
        out = slack.post(item.channel, text, blocks=blocks, thread_ts=item.ts)
    except (ApiError, LookupError) as exc:
        log("send_approval_slack_failed", item_id=item.id, error=str(exc)[:200])
        return ""
    return str(out.get("ts") or "") if out and out.get("channel") == item.channel else ""


def _seed(slack: Any, channel: str, ts: str, names: Iterable[str]) -> None:
    """The bot's own reactions on a message, so the approver's decision is one click."""
    if slack is None or not channel or not ts:
        return
    for name in names:
        try:
            slack.react(channel, ts, name)
        except (ApiError, LookupError) as exc:  # the approver can still add the reaction, or reply
            log("send_approval_react_failed", ts=ts, reaction=name, error=str(exc)[:200])


def _update_card(ctx: Context, slack: Any, item: Item, status: str) -> None:
    if slack is None or not item.channel or not item.ts or ctx.dry_run:
        return
    text, blocks = card(item.payload, status=status)
    try:
        slack.update(item.channel, item.ts, text, blocks)
    except (ApiError, LookupError) as exc:
        log("send_approval_slack_failed", item_id=item.id, error=str(exc)[:200])


def post_card(ctx: Context, slack: Any, item: Item) -> bool:
    """Post the card to the alert channel (dry-run: the dev channel), seed ✅ and ❌, and post emails 2 to 4
    in its thread. Live, the row records where. True when the card was posted."""
    text, blocks = card(item.payload)
    posted = slack.post(ctx.settings.general.alert_channel, text, blocks=blocks)
    if not posted or not posted.get("ts"):
        return False
    where, ts = str(posted["channel"]), str(posted["ts"])
    _seed(slack, where, ts, SEED_APPROVE)
    if ctx.live and item.id:
        item.payload["approve_ts"] = ts
        _save(ctx, item, slack_channel=where, slack_ts=ts)
    else:
        item.row.update(slack_channel=where, slack_ts=ts)
    post_followups(ctx, slack, item)
    return True


def post_followups(ctx: Context, slack: Any, item: Item) -> bool:
    text, blocks = followups(item.payload)
    ts = _thread(slack, item, text, blocks)
    if ts and ctx.live and item.id:
        item.payload["followups_ts"] = ts
        _save(ctx, item)
    return bool(ts)


def propose(ctx: Context, prepared: Sequence[enrol.Prepared], lim: Any, slack: Any) -> dict:
    """enrol with auto_send = no: each prepared account becomes an item and a card (dry-run: a preview)."""
    out: dict[str, Any] = {"posted": 0, "would_post": 0, "previews": 0, "by_owner": Counter(),
                           "waiting_before": sum(c.pending for c in lim.senders.values()), "items": [], "errors": []}
    taken = {owner: c.pending for owner, c in lim.senders.items()}
    for p in prepared:
        taken[p.owner] = taken.get(p.owner, 0) + 1
        sender = lim.senders.get(p.owner)
        payload = build_payload(ctx, p, slot=taken[p.owner], slots=sender.slots if sender else taken[p.owner])
        out["by_owner"][p.owner] += 1
        if ctx.dry_run:
            out["would_post"] += 1
            if slack is not None and out["previews"] < PREVIEW_CARDS:
                try:
                    if post_card(ctx, slack, Item({"item_id": "", "payload": payload})):
                        out["previews"] += 1
                except (ApiError, LookupError) as exc:
                    out["errors"].append(f"preview: {str(exc)[:160]}")
            continue
        row = {"item_id": new_id(), "kind": KIND, "account_id": p.account["account_id"],
               "contact_id": p.contact["contact_id"], "slack_channel": "", "slack_ts": "", "status": OPEN,
               "created_at": ctx.now, "payload": payload}
        ctx.store.upsert(TABLE, [row])  # the item first: a card that fails to post is posted by poll_approvals
        item = Item(row)
        if len(out["items"]) < LIST_LIMIT:
            out["items"].append(item.short_id)
        try:
            if post_card(ctx, slack, item):
                out["posted"] += 1
        except (ApiError, LookupError) as exc:
            out["errors"].append(f"{item.short_id}: the card was not posted ({str(exc)[:160]}); poll_approvals tries again")
        log("send_approval_posted", item_id=item.id, account_id=item.account_id, owner=p.owner, card=bool(item.ts))
    out["by_owner"] = dict(out["by_owner"])
    return out


# -- reading the thread ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    kind: str  # send | reject | edit | contact | company | edit_text | industry
    text: str = ""


_COMMANDS = (
    (re.compile(r"^(?:send|approve)[\s.!]*$", re.I), "send"),
    (re.compile(r"^(?:skip|no|reject|don'?t send)[\s.!]*$", re.I), "reject"),
    (re.compile(r"^edit[\s.!]*$", re.I), "edit"),
    (re.compile(r"^(?:(?:new|another|change|other)\s+)?contact[\s.!]*$", re.I), "contact"),
    (re.compile(r"^(?:(?:drop|cancel)\s+(?:the\s+)?)?company[\s.!]*$", re.I), "company"),
)
# An approver's industry correction (Harry, 7 Oct 2026; labels.py): "industry: Fintech", in any state.
_INDUSTRY = re.compile(r"^(?:industry|label)\s*[:=]\s*(.+?)[\s.!]*$", re.I)
_EDIT_TEXT = re.compile(r"^edit\s*:\s*(.+)$", re.I | re.S)
_STEP = re.compile(r"^email\s*([1-4])\s*:?[ \t]*", re.I)
_SUBJECT = re.compile(r"^subject\s*:\s*(.*)$", re.I)
_GREETING = re.compile(r"(?m)^\s*Hi\b")


def parse_command(text: Any, state: str = WAITING) -> Command | None:
    """An approver's thread reply as a command, or None. While editing, a reply with a greeting is an edit."""
    t = slack_text(text)
    for rx, kind in _COMMANDS:
        if rx.match(t):
            return Command(kind)
    m = _INDUSTRY.match(t)
    if m and "\n" not in t:
        return Command("industry", m.group(1).strip())
    m = _EDIT_TEXT.match(t)
    if m:
        return Command("edit_text", m.group(1).strip())
    if _STEP.match(t) or _SUBJECT.match(t.split("\n", 1)[0].strip()):
        return Command("edit_text", t)
    if state == EDITING and _GREETING.search(t):
        return Command("edit_text", t)
    return None


def parse_edit(text: str) -> tuple[int, str | None, str | None]:
    """(the email it changes, the new subject or None, the new body or None) from an edit reply."""
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    step = 1
    if lines:
        m = _STEP.match(lines[0].strip())
        if m:
            step = int(m.group(1))
            lines[0] = lines[0].strip()[m.end():]
    while lines and not lines[0].strip():
        lines.pop(0)
    subject = None
    if lines:
        m = _SUBJECT.match(lines[0].strip())
        if m:
            subject = m.group(1).strip()
            lines.pop(0)
    body = "\n".join(lines).strip()
    return step, subject, body or None
