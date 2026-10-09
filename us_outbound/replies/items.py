"""A reply item as the reply desk reads it: the hitl_items contract with poll_replies (SPEC 11).

poll_replies (replies/poll.py, built in parallel) creates one hitl_items row for each reply that
needs a human: kind "reply", status "open", and a payload with contact_id, account_id, mailbox
(the registry address the prospect wrote to), owner, instantly_email_id, the reply-to id,
reply_class, reply_excerpt, draft, referral ({name, title, email} or null), received_at, and
slack_channel and slack_ts once the alert is posted. Everything here is read defensively:
a missing key is blank, never an error, and the row's own columns (account_id, contact_id,
event_id, slack_channel, slack_ts, created_at) stand in when the payload lacks them.

Keys read beyond the contract (each optional): demo_requested (SPEC 11's classification field,
also looked for under payload.classification), subject, and reply_to_uuid / reply_to_id /
instantly_reply_to for the id Instantly's reply endpoint answers. The desk keeps its own state
under payload.desk and the HubSpot writes under payload.hubspot; nothing else in the payload is
changed, except draft when an approver edits it (the first version is kept as draft_original).

Status: open → handled (sent or skipped), or → escalated after escalation_hours (still open to
approval). "sending" is held only while a reply goes out (a compare-and-set on status, so a
scheduled run and `us-outbound replies approve` can never both send it).

Send approvals (kind send_approval; Harry, 2 Oct 2026) share the table: enrol/approvals/ holds
their contract, and nothing here reads them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.clients.guard import WARM_REPLY_CLASSES
from us_outbound.timeparse import utc

TABLE = "hitl_items"
# "reply" is the contract with poll_replies; "reply_approval" is the name sql/ddl/09 and enrol used first.
REPLY_KINDS = ("reply", "reply_approval")
OPEN, ESCALATED, SENDING, HANDLED = "open", "escalated", "sending", "handled"
ACTIONABLE = (OPEN, ESCALATED)  # an approver may still act on these
WAITING = (OPEN, ESCALATED, SENDING)  # not yet handled


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
