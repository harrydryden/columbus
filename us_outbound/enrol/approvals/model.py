"""Send approvals' data (9 Oct 2026, split from enrol/approvals.py): the hitl_items row and its payload.

The names of the data contract (statuses, payload states, outcomes, the events row), the reactions and holds the
other modules read, small text helpers, and Item: one send_approval row, with its payload as a working copy that
_save writes back and _cas moves from one status to another (transitions.transition moves an item's payload state
with them). find_item and list_items are the command line's view of the items.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from us_outbound.clients.guard import CLI_APPROVER
from us_outbound.clients.instantly import STEP_DAYS
from us_outbound.context import UK, Context
from us_outbound.enrol import capacity
from us_outbound.replies.desk import SKIP_REACTIONS
from us_outbound.settings.model import Settings
from us_outbound.timeparse import iso_date

KIND = "send_approval"  # hitl_items.kind
TABLE = "hitl_items"
OPEN, SENDING, HANDLED = "open", "sending", "handled"  # hitl_items.status
# payload.state
WAITING, REJECTED, EDITING, ADDING, DONE = "waiting", "rejected", "editing", "sending", "done"
# payload.outcome, and events.approval of the closing decision
APPROVED, APPROVED_EDITED = "approved", "approved_edited"
CONTACT_REJECTED, COMPANY_REJECTED = "contact_rejected", "company_rejected"
EXPIRED, BLOCKED = "expired", "blocked"
OUTCOMES = (APPROVED, APPROVED_EDITED, CONTACT_REJECTED, COMPANY_REJECTED, EXPIRED, BLOCKED)
EVENT_TYPE = "send_approval"  # events.type
EVENT_PREFIX = "send-approval:"  # events.event_id: the item's id, prefixed
SYSTEM = "system"  # handled_by of an expiry
APPROVALS_CLI_JOB = "approvals_approve"  # `approvals approve`: a job's live (--live and live_sending = yes)

REJECT_REACTIONS = SKIP_REACTIONS  # ❌
EDIT_REACTIONS = frozenset({"pencil2", "memo"})  # ✏️ (and 📝)
CONTACT_REACTIONS = frozenset({"bust_in_silhouette"})  # 👤
COMPANY_REACTIONS = frozenset({"no_entry_sign"})  # 🚫
# A ✅'s holds (recheck): temporary, so the card stays open and the approval valid; one thread note per key.
HOLD_LIVE, HOLD_OPTOUT, HOLD_STOP, HOLD_STOP_RULE = "live_sending", "optout_tested", "operator_stop", "stop_rule"
HOLD_REPLIES, HOLD_BLACKOUT, HOLD_CAMPAIGN = "reply_pause", "blackout", "campaign"
HOLD_INSTANTLY, HOLD_HUBSPOT = "instantly", "hubspot"
HOLD_PLAN = "instantly_plan"  # Instantly's plan has no room (enrol/plan.py): found at the add, not by recheck
SEED_APPROVE = ("white_check_mark", "x")  # what the bot puts on a card or a version, so a click decides
SEED_CHOICES = ("pencil2", "bust_in_silhouette", "no_entry_sign")

PREVIEW_CARDS = 5  # dry-run: cards posted to the dev channel as a preview
SECTION_CHARS = 2900  # Slack allows 3000 characters in a section's text
STUCK_AFTER = timedelta(minutes=10)  # an add still "sending" after this was cut off
LIST_LIMIT = 50  # per-list entries kept in a summary
APOLLO_PERSON_URL = "https://app.apollo.io/#/people/{}"
PICK_SOURCE, PICK_FACT = "pick_contacts", "contact_pick"  # contacts/pick.py SOURCE, OUTCOME_FACT
FOLLOW_UP_DAYS = STEP_DAYS[1:]  # 7, 14, 21: Instantly's own step days (clients/instantly.py)
NO_SLACK = ("auto_send is no, so every email waits for approval in Slack, and US_OUTBOUND_SLACK_BOT_TOKEN "
            "is not set (a live run needs it): set the token, or set auto_send = yes")


# -- small helpers --------------------------------------------------------------------------------------------------


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _esc(text: Any) -> str:
    """Text for Slack mrkdwn: &, < and > escaped, as Slack asks."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _key(slack_ts: Any) -> tuple[int, int]:
    """A Slack ts as (seconds, microseconds), compared exactly."""
    seconds, _, micro = str(slack_ts or "0").strip().partition(".")
    try:
        return int(seconds or 0), int((micro or "0").ljust(6, "0")[:6])
    except ValueError:
        return 0, 0


def _day(d: date | None) -> str:
    return f"{d:%a} {d.day} {d:%b}" if d else "its next send day"


def _first(name: str) -> str:
    return (name.strip().split() or [""])[0]


def _who(by: str) -> str:
    return "at the command line" if by == CLI_APPROVER else f"by <@{by}>"


def _uk_time(ctx: Context) -> str:
    return ctx.now.astimezone(UK).strftime("%H:%M")


def expires_on(send_day: date, settings: Settings) -> date:
    """The last UK day an item posted on send_day may be approved: the next send day after it."""
    return capacity.next_send_day(send_day + timedelta(days=1), settings) or send_day + timedelta(days=1)


def is_expired(payload: Mapping[str, Any], today: date) -> bool:
    """Past the end of its expires_on day (UK). An item with none (its payload erased by `erase`) has expired."""
    last = iso_date(payload.get("expires_on"))
    return last is None or today > last


# -- the items ------------------------------------------------------------------------------------------------------


class Item:
    """One send_approval row, its payload held as a working copy that _save writes back."""

    def __init__(self, row: Mapping[str, Any]):
        self.row = dict(row)
        p = row.get("payload")
        self.payload: dict[str, Any] = dict(p) if isinstance(p, Mapping) else {}

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
    def state(self) -> str:
        return _text(self.payload.get("state")) or WAITING

    @property
    def channel(self) -> str:
        return _text(self.row.get("slack_channel"))

    @property
    def ts(self) -> str:
        return _text(self.row.get("slack_ts"))

    @property
    def account_id(self) -> str:
        return _text(self.row.get("account_id"))

    @property
    def contact_id(self) -> str:
        return _text(self.row.get("contact_id"))

    @property
    def company(self) -> str:
        return _text(self.payload.get("company")) or _text(self.payload.get("domain")) or "the company"

    @property
    def person(self) -> str:
        c = self.payload.get("contact") or {}
        return " ".join(x for x in (_text(c.get("first_name")), _text(c.get("last_name"))) if x) or "the contact"

    @property
    def email(self) -> str:
        return _text((self.payload.get("lead") or {}).get("email"))


def items(store: Any, statuses: Sequence[str] = (OPEN, SENDING)) -> list[Item]:
    """Send approvals in these statuses, oldest first."""
    rows = store.select(TABLE, {"kind": KIND, "status": list(statuses)})
    return [Item(r) for r in sorted(rows, key=lambda r: (str(r.get("created_at") or ""), str(r.get("item_id"))))]


@dataclass
class Waiting:
    """Send approvals still waiting: their accounts, and how many each sender has."""

    accounts: frozenset[str] = frozenset()
    by_owner: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.by_owner.values())


def waiting(ctx: Context) -> Waiting:
    """The items not handled and not past their expiry (UK): what holds slots and accounts today."""
    today = ctx.today_uk()
    accounts: set[str] = set()
    owners: Counter[str] = Counter()
    for item in items(ctx.store):
        if item.status == OPEN and is_expired(item.payload, today):
            continue  # poll_approvals closes it; it holds nothing now
        accounts.add(item.account_id)
        owners[_text(item.payload.get("owner"))] += 1
    return Waiting(frozenset(accounts), dict(owners))


def _save(ctx: Context, item: Item, **values: Any) -> None:
    """Write the payload (and these columns) while the item is in the status it was read in; never over a closed one."""
    ctx.store.update(TABLE, {"item_id": item.id, "status": item.row.get("status")}, {"payload": item.payload, **values})
    item.row.update(values)


def _cas(ctx: Context, item: Item, status: str, **values: Any) -> bool:
    """Move the item from the status it was read in to `status`, with its payload; False if a run moved it first."""
    n = ctx.store.update(TABLE, {"item_id": item.id, "status": item.row.get("status")},
                         {"status": status, "payload": item.payload, **values})
    if n == 1:
        item.row.update(status=status, **values)
    return n == 1


def _step(p: Mapping[str, Any], n: int) -> dict:
    steps = p.get("steps") or []
    return next((s for s in steps if s.get("step") == n), {"step": n, "subject": "", "text": "", "source": ""})


def is_second(p: Mapping[str, Any]) -> bool:
    """The card is for the account's second contact (enrol/second.py)."""
    return str(p.get("contact_slot") or "1") == "2"


# -- the command line's view (`us-outbound approvals list`) ---------------------------------------------------------


def find_item(ctx: Context, ref: str) -> Item:
    """The send approval with this id, or the only one whose id starts with it (at least 4 characters)."""
    ref = str(ref or "").strip()
    if len(ref) < 4:
        raise ValueError("give the item id from `us-outbound approvals list` (at least its first 4 characters)")
    rows = [r for r in ctx.store.select(TABLE, {"kind": KIND}) if str(r.get("item_id") or "").startswith(ref)]
    exact = [r for r in rows if r.get("item_id") == ref]
    if exact or len(rows) == 1:
        return Item((exact or rows)[0])
    if not rows:
        raise LookupError(f"no send approval {ref!r}; `us-outbound approvals list` shows the waiting ones")
    raise ValueError(f"{ref!r} matches {len(rows)} items; give more of the id")


def list_items(ctx: Context) -> list[dict]:
    """The send approvals not yet handled, oldest first, as `approvals list` prints them."""
    out = []
    for item in items(ctx.store):
        p = item.payload
        c = p.get("contact") or {}
        out.append({
            "id": item.short_id, "item_id": item.id, "status": item.status, "state": item.state,
            "company": item.company, "domain": _text(p.get("domain")), "person": item.person,
            "title": _text(c.get("title")), "owner": _text(p.get("owner")),
            "mailbox": _text(p.get("mailbox")) or " or ".join(p.get("mailboxes") or []),
            "subject": _text(_step(p, 1).get("subject")), "edited": bool(p.get("edited")),
            "send_day": _text(p.get("send_day")), "expires_on": _text(p.get("expires_on")),
            "expired": is_expired(p, ctx.today_uk()), "in_slack": bool(item.ts),
        })
    return out
