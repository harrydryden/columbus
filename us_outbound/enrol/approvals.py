"""Send approvals: every email waits for an approver's ✅ in Slack before it reaches Instantly (Harry, 2 Oct 2026).

Harry, 2 Oct 2026: "every single message that gets sent out comes to this channel first for
approval", showing the company with a link to its domain, the recipient linked to where we found
them, the sender, the subject, the body and the number of emails sent. A tick sends it; a cross
offers to edit the email (and approve it again), to drop the contact (and find another) or to drop
the company. "There should be a switch to turn on and off auto send": the General key auto_send.
While it is no (the default) the enrol job proposes instead of enrolling; while it is yes, enrol adds
leads straight away, as before. live_sending stays the master switch: nothing reaches Instantly while
it is no.

Proposing (enrol, weekdays 12:00 UK; propose below). Each account enrol prepares becomes one item:
a hitl_items row and a card in the alert channel (#us-outbound). Approval is per contact and covers
the whole four-email sequence: the card shows email 1 in full and its first thread reply shows
emails 2 to 4, so the exact text of every message is seen before anything is sent; once approved,
Instantly sends email 1 in the next send window and the follow-ups on days 7, 14 and 21 by itself.
The bot seeds ✅ and ❌ on the card (Slack.react), so approving is one click; its own reactions never
count. Waiting items hold their sender's slots today and their place in the week (limits.today), and
an account with one waiting is not proposed again. With auto_send = no the weekly hand-check is not
a gate (every email is approved anyway): hand_check_post records only the accounts verify_accounts
held for doubtful facts, and golive's Hand-check line passes. A second contact at an account (General
second_contact; enrol/second.py; Harry, 6 Oct 2026) waits for its own ✅ like any email: its card is headed
"Send approval · second contact" and says who the first contact was and when their email 1 went; 🚫 on it
stops any further contact with the company, not the first person's emails.

Approving (poll_approvals, every 5 minutes, after the reply desk; poll below). Only an id on
approver_slack_ids counts; anyone else, and the bot, is ignored. Thread replies are read in order,
then the reactions on the message whose ✅ counts now (the card, or the latest edited version):
  ✅, or "send"                  re-check, then add the one lead to "US Outbound – {owner}" and record
                                 the enrollment as enrol does (enrol._record_enrolled). The card says
                                 "✅ Approved by @Harry at 14:02 UK · added to US Outbound – Hannah
                                 Spalding" and the thread confirms. A compare-and-set "sending" status
                                 means it is never added twice; if Instantly fails, the thread says why
                                 and the item waits for a fresh ✅ on that note (or "send"). A lead
                                 Instantly leaves out of its add summary is looked up in the campaign
                                 (enrol.campaign_lead_ids): there, it is recorded and approved; not
                                 there, Instantly refused it (its blocklist, or a lead in another
                                 campaign), so the card closes blocked and the contact is marked
                                 suppressed, and pick_contacts finds the next person. If a run dies
                                 mid-add (or the lookup fails), a later run looks it up: there, it is
                                 approved; not there, a person approves it again; Instantly unreadable,
                                 the thread asks a person to check the campaign first. A lead left out
                                 because Instantly's plan is full is no refusal (enrol/plan.py; Harry,
                                 7 Oct 2026): the card is held, the contact kept, the approvers asked
                                 once a day to make room, and the rest of that run's ✅s held too.
                                 The re-check (recheck) tells holds from blocks. A hold is temporary:
                                 live_sending or optout_tested no, an operator stop, the stop rule, the
                                 reply pause, a blackout date on Instantly's next send day, the owner's
                                 campaign not active in Instantly, Instantly or HubSpot not answering.
                                 The card stays open, the thread gets one note per hold reason, and the
                                 ✅ stays valid: each run tries again and the lead is added once the
                                 holds clear (or the card expires). Approving at the weekend is fine:
                                 Instantly sends on its next weekday. A block is permanent: the account
                                 or contact suppressed, excluded or no longer verified, the enrol run's
                                 own eligibility check (kill rules and hand-check pulls included), the
                                 email changed, no Active mailbox for the owner, another sender, or
                                 HubSpot excluding it now (the account is excluded too). The item
                                 closes as "blocked" with the reason.
  ❌, or "skip" / "no"           state "rejected": the thread offers three choices, their reactions seeded:
    ✏️ (or 📝), or "edit"        how to edit: reply in the thread with the new email 1, an optional first
                                 line "Subject: …" and then the body; "Email 2:" (3, 4) first changes a
                                 follow-up. Each edit is rendered by the renderer that renders for sending
                                 (render.render_step: HTML or text, the signature,
                                 every copy rule; the QA hash is left out, since the approver's ✅ is the
                                 review), and one that breaks a rule is refused in the thread with the
                                 rules it breaks. A passing edit is posted as the new version with ✅ and
                                 ❌ seeded: the re-approval. ✅ on it sends the edited sequence (outcome
                                 approved_edited); ❌ offers the three choices again. The first wording is
                                 kept in payload.original. Slack cannot open an editor without an
                                 interactive endpoint, which this app deliberately has none of (SPEC 2:
                                 no public endpoint), so edits are thread replies.
    👤, or "contact"             not this person: the contact is marked suppressed ("declined in Slack
                                 by …"; not the suppression table, as they did not opt out), so
                                 pick_contacts finds the next-ranked person at 05:30 (it skips people
                                 already revealed for the account) and a later enrol proposes them. The
                                 account stays verified (outcome contact_rejected).
    🚫, or "company"             drop the company: excluded with a declined_in_slack fact, which
                                 scoring/tiers.py reads as a hard exclusion, so a rescore keeps it out
                                 (enrol.mark_excluded; outcome company_rejected).
  When more than one choice is on the choices message, the least drastic wins (✏️, then 👤, then 🚫).
The 2-hour re-post and the 24-hour escalation of the reply desk do not apply to send approvals.

Expiry (a simple rule): an item may be approved on the UK day it was posted (send_day) and through
the next send day (expires_on). At the end of that day it is closed as expired by "system", its card
says so, and the account goes back to the queue: nothing was enrolled.

The data contract (the daily report reads it). hitl_items: one row per proposed contact, kind
"send_approval", with account_id, contact_id, slack_channel, slack_ts (the card), created_at; status
"open" while it waits (in any state), "sending" only while its lead is added, "handled" once closed,
with handled_at and handled_by (a Slack user id, "cli", or "system" for an expiry). Its payload always
has state (waiting, rejected, editing, sending, done), outcome ("" while open, then approved,
approved_edited, contact_rejected, company_rejected, expired or blocked), owner, mailbox, campaign,
lead (the Instantly lead, custom variables included), copy_version, angle, test_id, opener_arm,
opener_source, subject_arm (personal or copy: email 1's subject, render.subject_arm; Harry, 5 Oct 2026),
industry, industry_group, role, tier, score, send_day (YYYY-MM-DD, UK), edited,
original (the first custom variables once edited, else {}) and reason (why blocked or expired, else
""); and the keys this module keeps for itself (expires_on, the card's facts, the steps as text and as
editable source, the render variables, approve_ts, seen_ts, choices_ts, contact_slot (2 for a second
contact) and first_contact (who the first was), ...). Each closing decision is
one events row: event_id "send-approval:{item_id}", type "send_approval", approval = the outcome,
approved_by, account_id, contact_id, step 1, mailbox, occurred_at.

Dry-run (live_sending = no, the scheduler's state until Harry signs off): enrol writes no item and
posts at most PREVIEW_CARDS cards (with their thread) to the dev channel so Harry sees what they will
look like; its summary says how many would have been posted. poll_approvals reads Slack, works out each
decision and changes nothing. Live: every card is posted; a live enrol without
US_OUTBOUND_SLACK_BOT_TOKEN refuses, as there is nowhere to approve. Items whose card could not be
posted are posted by the next poll_approvals run, and the command line does the same work without
Slack: `us-outbound approvals list`, `approvals approve ID --live` and `approvals reject ID
--contact|--company --live` take the same close path, with approved_by "cli".
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from us_outbound import budget
from us_outbound.clients.db import new_id
from us_outbound.clients.guard import CLI_APPROVER, GuardViolation
from us_outbound.clients import instantly as instantly_client
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import STEP_DAYS
from us_outbound.context import UK, ConfigError, Context
from us_outbound.enrol import capacity, copy_markup, enrol, openers, plan, queue, render, second
from us_outbound.learn import holds
from us_outbound.logs import hash_email, log
from us_outbound.replies.desk import APPROVE_REACTIONS, SKIP_REACTIONS, slack_text
from us_outbound.replies.items import ts as parse_ts
from us_outbound.scoring.tiers import DECLINED_IN_SLACK
from us_outbound.settings.model import GENERAL_COPY, CopyRow, CopyStep, Mailbox, Settings

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


# -- small helpers --------------------------------------------------------------------------------


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


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


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
    last = _date(payload.get("expires_on"))
    return last is None or today > last


# -- the items --------------------------------------------------------------------------------------


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


def slack_or_none(ctx: Context) -> Any | None:
    """The Slack client, or None without a token (logged): the command line then does the work."""
    from us_outbound.clients.slack import SlackOff

    try:
        slack = ctx.clients.slack
    except ConfigError:
        slack = None
    if slack is None or isinstance(slack, SlackOff):
        log("slack_not_configured", job=ctx.job, reason="send approvals wait for `us-outbound approvals list|approve|reject`")
        return None
    return slack


def slack_for(ctx: Context) -> tuple[Any | None, str | None]:
    """(the Slack client for enrol's cards, why a live run refuses). Dry-run without a token logs the previews."""
    try:
        return ctx.clients.slack, None
    except ConfigError:
        return None, NO_SLACK if ctx.live else None


# -- what the card says -----------------------------------------------------------------------------


def editable(body: str, values: Mapping[str, str]) -> str:
    """A Copy-tab body with the lead's values filled in and the markup kept: what an approver edits."""
    source = copy_markup.drop_empty_optional(body, values, render.OPTIONAL_VARIABLES)

    def fill(m: re.Match[str]) -> str:
        name = m.group(1)
        return " ".join(str(values.get(name) or "").split()) if name in values else m.group(0)

    return re.sub(r"\n\s*\n(?:\s*\n)+", "\n\n", copy_markup.VARIABLE.sub(fill, source)).strip()


def recipient_source(ctx: Context, account_id: str, contact: Mapping[str, Any]) -> dict:
    """Where the contact came from: the account's latest contact_pick fact for them (contacts/pick.py)."""
    cid = _text(contact.get("contact_id"))
    latest: tuple[Any, dict] | None = None
    for e in ctx.store.select("signal_events", {"account_id": account_id, "source": PICK_SOURCE, "fact": PICK_FACT}):
        v = e.get("value")
        if not isinstance(v, Mapping) or _text(v.get("contact_id")) != cid:
            continue
        t = parse_ts(e.get("observed_at"))
        if latest is None or (t is not None and (latest[0] is None or t >= latest[0])):
            latest = (t, dict(v))
    value = latest[1] if latest else {}
    person = _text(value.get("apollo_person_id"))
    return {"email_source": _text(value.get("email_source")) or _text(contact.get("email_source")),
            "apollo_person_id": person, "url": APOLLO_PERSON_URL.format(person) if person else ""}


def history(ctx: Context, account_id: str) -> dict:
    """What the company has had from us before: emails sent, and earlier contacts declined here."""
    sent = [e for e in ctx.store.select("events", {"account_id": account_id, "type": "sent"})]
    last = max((t for t in (parse_ts(e.get("occurred_at")) for e in sent) if t), default=None)
    declined = []
    for row in ctx.store.select(TABLE, {"kind": KIND, "account_id": account_id, "status": HANDLED}):
        item = Item(row)
        if item.payload.get("outcome") == CONTACT_REJECTED:
            c = item.payload.get("contact") or {}
            declined.append({"name": item.person, "title": _text(c.get("title")),
                             "at": _text((item.payload.get("decided") or {}).get("at"))[:10]})
    return {"sent": len(sent), "last_sent": last.astimezone(UK).date().isoformat() if last else "", "declined": declined}


def build_payload(ctx: Context, p: enrol.Prepared, *, slot: int, slots: int) -> dict:
    """The item's payload: the data contract's keys, and what the card and an edit need."""
    s = ctx.settings
    a, c = p.account, p.contact
    row = s.copy_row(p.copy_version)
    steps = []
    for r in sorted(p.rendered, key=lambda r: r.step):
        source = editable(row.step(r.step).body, p.values) if row else r.text
        steps.append({"step": r.step, "subject": r.subject, "text": r.text, "source": source})
    send_day = ctx.today_uk()
    group = s.industry_group_of(a)
    return {
        # The contract (the daily report reads these).
        "state": WAITING, "outcome": "", "owner": p.owner, "mailbox": p.mailbox,
        "campaign": queue.campaign_name(p.owner), "lead": dict(p.lead), "copy_version": p.copy_version,
        "angle": p.angle, "test_id": p.test_id, "opener_arm": p.opener_arm, "opener_source": p.opener_source,
        "subject_arm": p.subject_arm,
        "industry": _text(a.get("industry")), "industry_group": group, "role": _text(c.get("role")),
        "tier": _text(a.get("tier")), "score": a.get("score"), "send_day": send_day.isoformat(),
        "edited": False, "original": {}, "reason": "",
        # This module's own.
        "expires_on": expires_on(send_day, s).isoformat(),
        "company": _text(a.get("clean_name")) or _text(a.get("domain")), "domain": _text(a.get("domain")).lower(),
        "contact": {"first_name": _text(c.get("first_name")), "last_name": _text(c.get("last_name")),
                    "title": _text(c.get("title"))},
        "source": recipient_source(ctx, str(a["account_id"]), c),
        "mailboxes": [m.address for m in s.mailboxes_for(p.owner, "Active")],
        "render_mailbox": p.render_mailbox, "values": dict(p.values), "steps": steps,
        "slot": slot, "slots": slots, "before": history(ctx, str(a["account_id"])),
        "approve_ts": "", "seen_ts": "", "choices_ts": "", "edit_ts": "", "followups_ts": "",
        "decided": {}, "added": {}, "edits": [],
        # A second contact at the account (enrol/second.py; Harry, 6 Oct 2026): 2, and who the first was.
        "contact_slot": p.slot, "first_contact": dict(p.first or {}),
    }


def opener_label(arm: str, source: str) -> str:
    """What set email 1's opener, for the card's context line."""
    if arm == openers.HOLDOUT:
        return "no opener (held out)"
    if not source or arm == openers.NONE:
        return "no opener"
    if source == openers.FOCUS_SOURCE_NAME:
        return "opener: what they do"
    if source.startswith(openers.GENERIC_OPENER_KEY):
        return "opener: the generic line"
    return f"opener from the signal {source.split(' / ')[0]}"


def _quoted(text: str) -> list[str]:
    """The text as Slack quote lines, in sections of at most SECTION_CHARS characters."""
    out: list[str] = []
    cur = ""
    for line in str(text or "").split("\n"):
        q = f"> {_esc(line)}"
        if cur and len(cur) + 1 + len(q) > SECTION_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{q}" if cur else q[:SECTION_CHARS]
    if cur:
        out.append(cur)
    return out


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:SECTION_CHARS]}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:SECTION_CHARS]}]}


def _step(p: Mapping[str, Any], n: int) -> dict:
    steps = p.get("steps") or []
    return next((s for s in steps if s.get("step") == n), {"step": n, "subject": "", "text": "", "source": ""})


def _source_line(p: Mapping[str, Any]) -> str:
    src = p.get("source") or {}
    clay = _text(src.get("email_source")).lower() == "clay"
    if src.get("url"):
        return f"<{src['url']}|Apollo>" + (" · email from Clay" if clay else "")
    if clay:
        return "email from Clay"
    name = _text(src.get("email_source"))
    return {"apollo": "Apollo"}.get(name.lower(), name) or "source not recorded"


def is_second(p: Mapping[str, Any]) -> bool:
    """The card is for the account's second contact (enrol/second.py)."""
    return str(p.get("contact_slot") or "1") == "2"


def _second_line(p: Mapping[str, Any]) -> str:
    """"*Second contact* at Acme: the first was Jane Doe, Head of People (People leader), email 1 on Tue 27 Oct. ...",
    or "" for a first contact's card."""
    if not is_second(p):
        return ""
    f = p.get("first_contact") or {}
    name = " ".join(x for x in (_text(f.get("first_name")), _text(f.get("last_name"))) if x) or "the first contact"
    title, role = _text(f.get("title")), _text(f.get("role"))
    who = ", ".join(x for x in (_esc(name), _esc(title)) if x) + (f" ({_esc(role)})" if role and role != title else "")
    sent = _date(f.get("email_1"))
    when = f", email 1 on {_day(sent)}" if sent else ""
    return (f"*Second contact* at {_esc(p.get('company') or 'this company')}: the first was {who}{when}. Same sender; "
            "both people's emails stop when either replies.")


def _before_line(p: Mapping[str, Any]) -> str:
    b = p.get("before") or {}
    company = _esc(p.get("company") or "this company")
    n = int(b.get("sent") or 0)
    if n:
        last = _date(b.get("last_sent"))
        line = f"{n} email{'s' if n != 1 else ''} to {company} from us before" + (f", the last on {_day(last)}" if last else "")
    else:
        line = f"no email to {company} from us before"
    declined = b.get("declined") or []
    if declined:
        who = "; ".join(", ".join(x for x in (_esc(d.get("name")), _esc(d.get("title"))) if x) for d in declined)
        line += f"; the earlier contact ({who}) was declined here, so this is the next one"
    return line


def card(p: Mapping[str, Any], status: str = "") -> tuple[str, list[dict]]:
    """The card: (plain-text fallback, Block Kit blocks). status, once decided, replaces the footer."""
    c = p.get("contact") or {}
    name = " ".join(x for x in (_text(c.get("first_name")), _text(c.get("last_name"))) if x) or "the contact"
    company, domain = _text(p.get("company")), _text(p.get("domain"))
    first = _step(p, 1)
    owner = _text(p.get("owner"))
    head = f"*{_esc(company)}* · <https://{domain}|{_esc(domain)}>" if domain else f"*{_esc(company)}*"
    industry = _text(p.get("industry"))
    group = _text(p.get("industry_group"))
    where = f"{industry} ({group})" if group and group.casefold() != industry.casefold() else (industry or group)
    score = p.get("score")
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    facts = [f"{_text(p.get('tier'))} · score {score}" if p.get("tier") else "",
             _text(p.get("angle")), opener_label(_text(p.get("opener_arm")), _text(p.get("opener_source"))), where]
    title = _text(c.get("title"))
    role = _text(p.get("role"))
    to = ", ".join(x for x in (_esc(name), _esc(title)) if x) + (f" ({_esc(role)})" if role and role != title else "")
    boxes = p.get("mailboxes") or ([p["mailbox"]] if p.get("mailbox") else [])
    sender = " or ".join(_esc(b) for b in boxes) + (" (Instantly picks)" if len(boxes) > 1 else "")
    people = "\n".join(x for x in [
        f"*To:* {to} · {_esc(_text((p.get('lead') or {}).get('email')))} · {_source_line(p)}",
        _second_line(p),
        f"*From:* {_esc(owner)}" + (f" · {sender}" if sender else ""),
        f"*Subject:* {_esc(first.get('subject'))}",
    ] if x)
    days = ", ".join(str(d) for d in FOLLOW_UP_DAYS[:-1]) + f" and {FOLLOW_UP_DAYS[-1]}"
    counts = "\n".join([
        f"*Emails:* Email 1 of {len(render.STEPS)} · follow-ups on days {days} (in the thread)",
        f"*Before:* {_before_line(p)}",
        f"*{_esc(_first(owner)) or 'The sender'} today:* card {p.get('slot', '?')} of {p.get('slots', '?')}",
    ])
    blocks = []
    if status:
        blocks.append(_section(status))
    kind = "Send approval · second contact" if is_second(p) else "Send approval"
    blocks += [_section(f"{kind} · {head}"), _context(" · ".join(_esc(f) for f in facts if f)), _section(people)]
    blocks += [_section(q) for q in _quoted(first.get("text") or "")]
    blocks.append(_section(counts))
    if status:
        blocks.append(_context(status))
    else:
        blocks.append(_context("✅ send · ❌ don't send. Or reply \"send\" or \"skip\" in the thread."))
    text = _esc(f"{'Send approval (second contact)' if is_second(p) else 'Send approval'}: {company} · {name} · "
                f"from {owner}: {first.get('subject') or ''}")
    return (f"{status} {text}" if status else text), blocks  # status is mrkdwn already (it may mention)


def followups(p: Mapping[str, Any]) -> tuple[str, list[dict]]:
    """The card's first thread reply: emails 2 to 4 in full."""
    c = p.get("contact") or {}
    first = _text(c.get("first_name")) or "they"
    days = ", ".join(str(d) for d in FOLLOW_UP_DAYS[:-1]) + f" and {FOLLOW_UP_DAYS[-1]}"
    blocks = [_section(f"Emails 2 to {len(render.STEPS)}, sent on days {days} unless {_esc(first)} replies. "
                       "The ✅ on the card approves all four.")]
    for n, day in zip(render.STEPS[1:], FOLLOW_UP_DAYS):
        st = _step(p, n)
        blocks.append(_section(f"*Email {n} · day {day} · Subject:* {_esc(st.get('subject'))}"))
        blocks += [_section(q) for q in _quoted(st.get("text") or "")]
    return _esc(f"Follow-ups for {p.get('company') or 'this card'}: emails 2 to {len(render.STEPS)}"), blocks


def choices_text(p: Mapping[str, Any], by: str) -> str:
    company = _esc(p.get("company") or "the company")
    c = p.get("contact") or {}
    name = _esc(" ".join(x for x in (_text(c.get("first_name")), _text(c.get("last_name"))) if x) or "this person")
    # A second contact's company is enrolled already: dropping it stops any further contact, not the first's emails.
    drop = (f"drop {company}: no second contact there (the first person's emails carry on)" if is_second(p)
            else f"drop {company}")
    return (f"❌ Not sent ({_who(by)}). What next? React to this message, or reply with the word:\n"
            "✏️ *edit*: change the email (or a follow-up), then approve it again\n"
            f"👤 *contact*: not {name}; you'll get a card for the next person at {company}\n"
            f"🚫 *company*: {drop}\n"
            f"If nothing is chosen, it expires at the end of {_day(_date(p.get('expires_on')))} (UK) and "
            f"{company} goes back to the queue.")


def edit_help(ctx: Context, p: Mapping[str, Any], by: str) -> str:
    values = p.get("values") or {}
    first, sender = _esc(values.get("first_name") or "them"), _esc(values.get("sender_first_name") or "the sender")
    st = _step(p, 1)
    copy = f"Subject: {st.get('subject') or ''}\n{st.get('source') or ''}"
    return (f"✏️ Editing ({_who(by)}). Reply in this thread with the new email 1: an optional first line "
            "`Subject: …`, then the body. To change a follow-up, start with `Email 2:` (or `Email 3:`, `Email 4:`). "
            f"Keep the greeting \"Hi {first},\" and the sign-off \"Best wishes,\" then \"{sender}\"; write links as "
            "[anchor text](https://…). The signature is added as before. Each version "
            "is checked against the copy rules and posted back here for a fresh ✅. (Slack can't open an editor "
            "here: this app has no interactive endpoint.)\nEmail 1 as it stands, to copy:\n"
            f"```{_esc(copy)}```")


def version_message(p: Mapping[str, Any], step: int, by: str) -> tuple[str, list[dict]]:
    st = _step(p, step)
    head = (f"✏️ New version of email {step} ({_who(by)}). ✅ this message to send the sequence with it, "
            "❌ if not. The other emails are as before.")
    blocks = [_section(head), _section(f"*Subject:* {_esc(st.get('subject'))}")]
    blocks += [_section(q) for q in _quoted(st.get("text") or "")]
    return _esc(f"New version of email {step} for {p.get('company') or 'this card'}"), blocks


# -- posting ------------------------------------------------------------------------------------------


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


# -- reading the thread -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    kind: str  # send | reject | edit | contact | company | edit_text
    text: str = ""


_COMMANDS = (
    (re.compile(r"^(?:send|approve)[\s.!]*$", re.I), "send"),
    (re.compile(r"^(?:skip|no|reject|don'?t send)[\s.!]*$", re.I), "reject"),
    (re.compile(r"^edit[\s.!]*$", re.I), "edit"),
    (re.compile(r"^(?:(?:new|another|change|other)\s+)?contact[\s.!]*$", re.I), "contact"),
    (re.compile(r"^(?:(?:drop|cancel)\s+(?:the\s+)?)?company[\s.!]*$", re.I), "company"),
)
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


def _mailbox(settings: Settings, p: Mapping[str, Any]) -> Mailbox:
    address = _text(p.get("render_mailbox")).lower()
    found = next((m for m in settings.mailboxes if m.address.lower() == address), None)
    if found is not None:
        return found
    owner = _text(p.get("owner"))
    return Mailbox(address=address, domain=address.split("@")[-1], owner_name=owner, status="Active", daily_cap=0)


def _templated(body: str, values: Mapping[str, str], settings: Settings, sender_name: str = "") -> str:
    """The approver's body as copy: the greeting and sign-off back to their variables, a pasted signature
    taken off (render_step adds it), so the copy rules read it as they read the sheet. The signature shows
    one of its lines (Harry, 5 Oct 2026), and any of them, or the sender's name, is taken off."""
    fixed = render.signature_texts(settings, sender_name)
    lines = body.replace("\r\n", "\n").split("\n")
    while lines and (not lines[-1].strip() or lines[-1].strip() in fixed):
        lines.pop()
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    first, sender = str(values.get("first_name") or ""), str(values.get("sender_first_name") or "")
    if nonblank:
        i = nonblank[0]
        if first and re.fullmatch(rf"Hi\s+{re.escape(first)}\s*,", lines[i].strip(), re.I):
            lines[i] = "Hi {{first_name}},"
        j = nonblank[-1]
        if sender and lines[j].strip().casefold() == sender.casefold():
            lines[j] = "{{sender_first_name}}"
    return "\n".join(lines).strip()


def render_edit(ctx: Context, p: Mapping[str, Any], step: int, subject: str | None,
                body: str | None) -> tuple[render.Rendered, list[str], str]:
    """(the edited email rendered for sending, the copy rules it breaks, its copy source)."""
    s = ctx.settings
    values = dict(p.get("values") or {})
    cur = _step(p, step)
    subject = (cur.get("subject") or "") if subject is None else subject
    mailbox = _mailbox(s, p)
    source = _templated(body if body is not None else str(cur.get("source") or ""), values, s, mailbox.owner_name)
    steps = tuple(CopyStep(subject if n == step else "", source if n == step else "") for n in render.STEPS)
    row = CopyRow(str(p.get("copy_version") or "edited"), _text(p.get("industry")) or GENERAL_COPY, "approved", steps)
    r = render.render_step(row, values, step=step, mailbox=mailbox, settings=s, for_send=False)
    names = {"{{first_name}}": str(values.get("first_name") or "the first name"),
             "{{sender_first_name}}": str(values.get("sender_first_name") or "the sender's first name")}
    problems = []
    for v in render.violations([r]):
        for var, value in names.items():
            v = v.replace(var, value)
        problems.append(v)
    return r, problems, source


# -- the decisions --------------------------------------------------------------------------------------------


def _event(ctx: Context, item: Item, outcome: str, by: str) -> None:
    ctx.store.upsert("events", [{
        "event_id": f"{EVENT_PREFIX}{item.id}", "type": EVENT_TYPE, "approval": outcome, "approved_by": by,
        "account_id": item.account_id or None, "contact_id": item.contact_id or None, "step": 1,
        "mailbox": _text(item.payload.get("mailbox")) or None, "occurred_at": ctx.now,
    }])


def _close(ctx: Context, item: Item, outcome: str, by: str, slack: Any, *, status: str, note: str,
           reason: str = "", via: str = "") -> bool:
    """Handled, with its outcome, the events row, the card and a thread note. False if another run closed it."""
    item.payload.update(state=DONE, outcome=outcome, reason=reason)
    item.payload["decided"] = {"by": by, "at": ctx.now.isoformat(), "via": via, "outcome": outcome}
    item.payload.pop("sending", None)
    if not _cas(ctx, item, HANDLED, handled_at=ctx.now, handled_by=by):
        return False
    _event(ctx, item, outcome, by)
    _update_card(ctx, slack, item, status)
    _thread(slack, item, note)
    log("send_approval_closed", item_id=item.id, account_id=item.account_id, outcome=outcome, by=by)
    return True


def eligibility(ctx: Context, item: Item, account: Mapping[str, Any], contact: Mapping[str, Any]) -> str:
    """Why the card's account or contact may no longer be emailed, or "": the enrol run's own check
    (enrol.eligible: suppression, partners, the hand-check's pulls, industries switched off, kill-rule holds
    on industry groups and email sources, the recipient rules), with suppression looked up for this domain
    and email only (enrol.suppressed_for), not the whole table."""
    email = _text(contact.get("email")).lower()
    domains = [_text(account.get("domain")), email.rpartition("@")[2]]
    hashes = [hash_email(email) if "@" in email else "", _text(contact.get("email_sha256"))]
    _, pulled = enrol.hand_check(ctx, ctx.now_et().date())  # the pulls enrol leaves out, as it reads them
    g = enrol.gates(ctx, pulled, enrol.suppressed_for(ctx, domains, hashes))
    why = enrol.account_reason(account, g)
    if why:
        return f"{item.company}: {why}"
    chosen, why = enrol.eligible(account, [contact], g)
    return f"{item.person}: {why}" if chosen is None else ""


@dataclass
class Recheck:
    """What a ✅ finds just before the lead is added (recheck).

    holds: key -> why, for what is temporary. The card stays open and its ✅ stays valid: each
      poll_approvals run tries again, and the lead is added once every hold clears (or the card expires).
    blocks: what is permanent. The card closes as blocked and the account goes back to the queue.
    exclude: (fact, reason) when HubSpot now excludes the account, which is excluded as the card closes.
    """

    holds: dict[str, str] = field(default_factory=dict)
    blocks: list[str] = field(default_factory=list)
    exclude: tuple[str, str] | None = None


def instantly_send_day(ctx: Context) -> date | None:
    """The day Instantly would send email 1 if the lead were added now: its schedule's next weekday (the send
    window's days, in the window's time zone), today if the window has not closed yet. Instantly knows the
    weekdays, not our blackout dates."""
    w = ctx.settings.general.send_window
    now = ctx.now.astimezone(ZoneInfo(w.tz))
    day = now.date() + timedelta(days=1 if now.time() >= w.end else 0)
    for i in range(14):
        if (day + timedelta(days=i)).weekday() in w.days:
            return day + timedelta(days=i)
    return None


def recheck(ctx: Context, item: Item) -> Recheck:
    """Why the lead may not be added now, just before adding it: the holds (temporary: the ✅ stays valid) and
    the blocks (permanent: the card closes). Nothing in either when it may be added.

    Holds: live_sending no, optout_tested no, an operator stop, the stop rule, the reply pause (a positive reply
    waiting over escalation_hours), Instantly's next send day being a blackout date, the owner's campaign not
    active in Instantly, and Instantly or HubSpot not answering. Approving at the weekend is fine: Instantly
    sends on its next weekday. Blocks: the account or contact gone, no longer verified or in a queue tier, the
    enrol run's own eligibility check (eligibility), the email changed, no Active mailbox for the owner, another
    sender, and a HubSpot exclusion. A second contact's card (enrol/second.py) needs its account enrolled, not
    verified, and the second-contact rules still met: second_contact yes, nobody at the account who replied,
    bounced, unsubscribed or complained, and another role than the first's. Instantly and HubSpot are asked only
    when nothing else holds or blocks.
    """
    s = holds.with_holds(ctx.store, ctx.settings)
    p = item.payload
    out = Recheck()
    if not s.general.live_sending:
        out.holds[HOLD_LIVE] = "live_sending is no"
    if not s.general.optout_tested:
        out.holds[HOLD_OPTOUT] = enrol.OPTOUT_UNTESTED
    for key, why in ((HOLD_STOP, enrol.operator_pause(ctx)), (HOLD_STOP_RULE, holds.enrolment_stop(ctx.store)),
                     (HOLD_REPLIES, enrol.reply_pause(ctx))):
        if why:
            out.holds[key] = why if key != HOLD_REPLIES else f"new emails wait while {why}"
    day = instantly_send_day(ctx)
    if day is not None and budget.is_blackout(day, s):
        out.holds[HOLD_BLACKOUT] = (f"{_day(day)} is a blackout date, and Instantly (which does not know our blackout "
                                    "dates) would send email 1 then")
    account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
    contact = ctx.store.get("contacts", contact_id=item.contact_id) if item.contact_id else None
    if account is None or contact is None:
        out.blocks.append("the account or its contact is no longer on file")
        return out
    company = item.company
    in_queue = account.get("tier") in queue.QUEUE_TIERS
    if is_second(p):  # its account is enrolled: the second-contact rules, as of now (enrol/second.py)
        why = second.recheck(ctx, account, contact) if in_queue else None
        if why:
            out.blocks.append(f"{company}: no second contact now: {why}")
    elif account.get("status") != "verified":
        out.blocks.append(f"{company} is {account.get('status') or 'without a status'} now, not verified")
    if not in_queue:
        reason = _text(account.get("tier_reason"))
        out.blocks.append(f"{company} is {account.get('tier') or 'untiered'} now" + (f" ({reason})" if reason else ""))
    why = eligibility(ctx, item, account, contact)
    if why:
        out.blocks.append(why)
    elif _text(contact.get("email")).lower() != item.email.lower():
        out.blocks.append(f"{item.person}'s email has changed since the card was posted")
    owner = _text(p.get("owner"))
    if not s.mailboxes_for(owner, "Active"):
        out.blocks.append(f"{owner} has no Active mailbox now")
    sender = _text(account.get("sender"))
    if sender and sender != owner:
        out.blocks.append(f"{company}'s sender is {sender} now, not {owner}")
    if out.holds or out.blocks:
        return out
    try:
        why = capacity.campaign_problem(owner, ctx.clients.instantly.list_campaigns())
    except (ApiError, ConfigError, LookupError) as exc:
        out.holds[HOLD_INSTANTLY] = f"Instantly could not be read ({type(exc).__name__}: {str(exc)[:160]})"
        return out
    if why:
        out.holds[HOLD_CAMPAIGN] = why
        return out
    try:
        excluded = enrol.hubspot_block(ctx, account, contact)
    except (ApiError, ConfigError) as exc:
        out.holds[HOLD_HUBSPOT] = f"HubSpot could not be read for the re-check ({str(exc)[:160]})"
        return out
    if excluded:
        out.exclude = excluded
        out.blocks.append(f"{company}: {excluded[1]}")
    return out


def _hold(ctx: Context, item: Item, why: Mapping[str, str], *, by: str, via: str, slack: Any) -> None:
    """A ✅ that something temporary stops for now: the card stays open and the approval stays valid, so each
    poll_approvals run tries again and adds the lead once it clears, until the card expires. The thread gets
    one note per hold reason (payload.held.noted), never the same one again every 5 minutes."""
    p = item.payload
    held = dict(p.get("held") or {})
    noted = [str(k) for k in held.get("noted") or ()]
    new = [k for k in why if k not in noted]
    p["held"] = {"by": by, "via": via, "since": held.get("since") or ctx.now.isoformat(), "at": ctx.now.isoformat(),
                 "reasons": dict(why), "noted": noted + new}
    _save(ctx, item)
    if new:
        _thread(slack, item, f"⏸ Approved {_who(by)}, but not added yet: {_esc('; '.join(why[k] for k in new))}. "
                             "It goes through by itself once that clears, until the card expires at the end of "
                             f"{_day(_date(p.get('expires_on')))} (UK). ❌ still stops it.")
    log("send_approval_held", item_id=item.id, holds=list(why), noted=new)


def _held_approval(item: Item) -> tuple[str, str] | None:
    """(who approved, how) of an approval a hold stopped, while the card still waits; else None."""
    held = item.payload.get("held") or {}
    if item.status != OPEN or item.state != WAITING or not held.get("by"):
        return None
    return _text(held.get("by")), _text(held.get("via")) or "held"


def _retry(item: Item, approvers: frozenset[str]) -> tuple[Command | None, str]:
    """(send, how it was approved) for an approval a hold stopped, by someone who may still approve; else (None, "")."""
    held = _held_approval(item)
    if held is None or (held[0] not in approvers and held[0] != CLI_APPROVER):
        return None, ""
    return Command("send", held[0]), held[1]


def _prepared(ctx: Context, item: Item) -> enrol.Prepared:
    p = item.payload
    account = ctx.store.get("accounts", account_id=item.account_id) or {"account_id": item.account_id}
    contact = ctx.store.get("contacts", contact_id=item.contact_id) or {"contact_id": item.contact_id}
    return enrol.Prepared(
        account=account, contact=contact, owner=_text(p.get("owner")), mailbox=_text(p.get("mailbox")),
        copy_version=_text(p.get("copy_version")), angle=_text(p.get("angle")), test_id=_text(p.get("test_id")),
        lead=dict(p.get("lead") or {}), opener_arm=_text(p.get("opener_arm")) or openers.NONE,
        opener_source=_text(p.get("opener_source")),
        # A card posted before the split (5 Oct 2026) was rendered with the Copy row's subject.
        subject_arm=_text(p.get("subject_arm")) or render.COPY_SUBJECT,
        slot=second.SECOND if is_second(p) else second.FIRST,  # a second contact leaves its account as it is
    )


def send(ctx: Context, item: Item, *, by: str, via: str, slack: Any = None) -> dict:
    """✅: re-check, then add the one lead to the owner's campaign and record the enrollment; close the item."""
    p = item.payload
    outcome = APPROVED_EDITED if p.get("edited") else APPROVED
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "by": by, "via": via}
    check = recheck(ctx, item)
    if check.blocks:
        result.update(added=False, outcome=BLOCKED, why=check.blocks)
        if ctx.dry_run:
            result["dry_run"] = True
            return result
        reasons = "; ".join(check.blocks)
        closed = _close(ctx, item, BLOCKED, by, slack, reason=reasons, via=via, status=f"⛔ Not sent: {_esc(reasons)}",
                        note=f"⛔ Not added ({_who(by)}): {_esc(reasons)}. Closed: nothing was sent, and "
                             f"{_esc(item.company)} goes back to the queue if it can still be emailed.")
        if closed and check.exclude:  # HubSpot excludes it now: kept out at the next rescore too
            enrol.mark_excluded(ctx, {"account_id": item.account_id}, *check.exclude)
        return result
    if check.holds:
        result.update(added=False, held=list(check.holds.values()))
        if ctx.dry_run:
            result["dry_run"] = True
            return result
        _hold(ctx, item, check.holds, by=by, via=via, slack=slack)
        return result
    if ctx.dry_run:
        result.update(added=False, dry_run=True, outcome=outcome)
        return result
    campaign = _text(p.get("campaign"))
    previous = item.state
    p.update(state=ADDING, sending={"at": ctx.now.isoformat(), "by": by, "via": via})
    if not _cas(ctx, item, SENDING):
        result.update(added=False, why=["another run is handling this item"])
        return result
    try:
        added = ctx.clients.instantly.add_leads(campaign, [p.get("lead") or {}])
    except GuardViolation:
        p.update(state=previous)
        p.pop("sending", None)
        _cas(ctx, item, OPEN)
        raise
    except (ApiError, LookupError, ValueError, ConfigError) as exc:
        if instantly_client.plan_full_error(exc):  # the whole add refused for want of room: held, not failed
            return {**result, **_plan_full(ctx, item, by=by, via=via, slack=slack, alert=plan.full_alert(ctx))}
        why = f"{type(exc).__name__}: {str(exc)[:200]}"
        note = _thread(slack, item, f"Not added: Instantly refused: {_esc(str(exc)[:200])}. Check {_esc(campaign)} in "
                                    f"Instantly for {_esc(item.email)} before you approve it again: ✅ this message, or "
                                    "reply \"send\".")
        _seed(slack, item.channel, note, SEED_APPROVE[:1])
        p.update(state=WAITING, approve_ts=note, failed={**p.pop("sending", {}), "error": why})
        _cas(ctx, item, OPEN)
        log("send_approval_add_failed", item_id=item.id, error=why)
        result.update(added=False, why=[why])
        return result
    lead = dict(p.get("lead") or {})
    email = _text(lead.get("email")).lower()
    room = plan.after_add(ctx, added)  # the plan-full ask, or the low-room warning (enrol/plan.py)
    ids = enrol._created_ids(added or {}, [lead])
    if 0 not in ids:  # not created: in the campaign after all, or refused (enrol.campaign_lead_ids)
        try:
            found = enrol.campaign_lead_ids(ctx, campaign, [email])
        except (ApiError, LookupError, ConfigError) as exc:  # left "sending": _stuck looks again after STUCK_AFTER
            # Instantly had the add, and did not create it (for want of room, when its plan was full: enrol/plan.py)
            p["sending"] = {**(p.get("sending") or {}), "answered": True, **({"plan_full": True} if room["full"] else {})}
            _save(ctx, item)
            why = f"Instantly did not confirm the lead and {campaign} could not be read ({str(exc)[:160]}); looked up again"
            log("send_approval_lookup_failed", item_id=item.id, error=str(exc)[:200])
            result.update(added=False, why=[why])
            return result
        if email not in found and room["full"]:  # left out for want of room, not refused: held, the contact kept
            return {**result, **_plan_full(ctx, item, by=by, via=via, slack=slack, alert=room.get("alert"))}
        if email not in found:
            return {**result, **_refused(ctx, item, by=by, via=via, slack=slack)}
        ids = {0: found[email]}
    return {**result, **_added(ctx, item, ids[0], by=by, via=via, slack=slack), "instantly_plan": room}


def _plan_full(ctx: Context, item: Item, *, by: str, via: str, slack: Any, alert: Any) -> dict:
    """Instantly's plan has no room (enrol/plan.py): the add did not happen, so the item goes back to waiting, held
    as a stop or a blackout holds it: the card stays open and the ✅ stays valid, each poll_approvals run tries
    again until there is room or the card expires, and the contact is kept (never enrol.mark_not_added)."""
    p = item.payload
    p.update(state=WAITING)
    p.pop("sending", None)
    _cas(ctx, item, OPEN)
    _hold(ctx, item, {HOLD_PLAN: plan.HOLD}, by=by, via=via, slack=slack)
    return {"added": False, "held": [plan.HOLD], "plan_full": True, "alert": alert}


def _added(ctx: Context, item: Item, lead_id: str, *, by: str, via: str, slack: Any) -> dict:
    """The lead is in the owner's campaign: record the enrollment as enrol does, and close the item approved."""
    p = item.payload
    edited = bool(p.get("edited"))
    outcome = APPROVED_EDITED if edited else APPROVED
    campaign = _text(p.get("campaign"))
    enrol._record_enrolled(ctx, [_prepared(ctx, item)], {0: lead_id}, campaign, ctx.now_et().date().strftime("%Y-%m"))
    resume = ""
    try:
        if capacity.resume_if_completed(ctx, campaign):
            log("campaign_resumed", campaign=campaign, item_id=item.id)
    except (ApiError, LookupError, ConfigError) as exc:
        log("campaign_resume_failed", campaign=campaign, item_id=item.id, error=str(exc)[:200])
        resume = (f" ⚠️ {_esc(campaign)} is completed in Instantly and could not be resumed, so it won't send until "
                  "someone runs `us-outbound start --live`.")
    at = _uk_time(ctx)
    p["added"] = {"lead_id": lead_id, "at": ctx.now.isoformat(), "by": by, "campaign": campaign}
    first = _esc((p.get("contact") or {}).get("first_name") or "they")
    days = ", ".join(str(d) for d in FOLLOW_UP_DAYS[:-1]) + f" and {FOLLOW_UP_DAYS[-1]}"
    _close(ctx, item, outcome, by, slack, via=via,
           status=f"✅ Approved{' (edited)' if edited else ''} {_who(by)} at {at} UK · added to {_esc(campaign)}",
           note=f"Added to {_esc(campaign)} at {at} UK{' (edited)' if edited else ''}, approved {_who(by)}. Email 1 goes "
                f"out in the next send window, and the follow-ups on days {days} unless {first} replies.{resume}")
    return {"added": True, "outcome": outcome, "campaign": campaign, "at": f"{at} UK"}


def _refused(ctx: Context, item: Item, *, by: str, via: str, slack: Any) -> dict:
    """Instantly did not create the lead and the campaign does not have it: its blocklist, or a lead in another
    campaign of the workspace. Closed as blocked, and the contact marked suppressed (enrol.mark_not_added) so
    pick_contacts finds the next person and a later enrol proposes them."""
    why = enrol.NOT_ADDED
    company = _esc(item.company)
    if _close(ctx, item, BLOCKED, by, slack, reason=why, via=via, status=f"⛔ Not sent: {why}",
              note=f"⛔ Instantly did not add {_esc(item.email)} (in its blocklist, or already in another campaign). "
                   f"Closed: nothing was sent. {_esc(item.person)} won't be proposed again; you'll get a card for the "
                   f"next person at {company}."):
        enrol.mark_not_added(ctx, item.contact_id)
    return {"added": False, "outcome": BLOCKED, "why": [why]}


def reject(ctx: Context, item: Item, *, by: str, via: str, slack: Any) -> None:
    """❌: not sent; the thread offers the three choices, their reactions seeded."""
    p = item.payload
    p.pop("held", None)  # ❌ stops an approval a hold kept
    p.update(state=REJECTED, approve_ts="", rejected={"by": by, "at": ctx.now.isoformat(), "via": via})
    note = _thread(slack, item, choices_text(p, by))
    _seed(slack, item.channel, note, SEED_CHOICES)
    p["choices_ts"] = note
    _save(ctx, item)
    _update_card(ctx, slack, item, f"❌ Not sent ({_who(by)}): ✏️ edit, 👤 another contact or 🚫 drop the company, in the thread")


def start_edit(ctx: Context, item: Item, *, by: str, slack: Any) -> None:
    """✏️: how to edit, with email 1 as it stands, ready to copy."""
    item.payload.pop("held", None)  # an edited version needs its own ✅
    item.payload["state"] = EDITING
    item.payload["edit_ts"] = _thread(slack, item, edit_help(ctx, item.payload, by))
    _save(ctx, item)


def apply_edit(ctx: Context, item: Item, text: str, *, by: str, slack: Any) -> bool:
    """An edit reply: rendered and checked; refused in the thread, or posted as the new version for a fresh ✅."""
    p = item.payload
    step, subject, body = parse_edit(text)
    edits = list(p.get("edits") or [])
    if subject is None and body is None:
        _thread(slack, item, f"I couldn't find a new email {step} in that reply: give `Subject: …` and/or the body.")
        return False
    r, problems, source = render_edit(ctx, p, step, subject, body)
    if problems:
        edits.append({"step": step, "by": by, "at": ctx.now.isoformat(), "accepted": False, "problems": problems[:5]})
        p["edits"] = edits
        _save(ctx, item)
        _thread(slack, item, f"That version of email {step} breaks the copy rules, so it is not used: "
                             + _esc("; ".join(problems[:8])) + ". Reply with another version.")
        return False
    lead = dict(p.get("lead") or {})
    variables = dict(lead.get("custom_variables") or {})
    if not p.get("edited"):
        p["original"] = dict(variables)
    variables[f"s{step}_subject"], variables[f"s{step}_body"] = r.subject, r.body
    lead["custom_variables"] = variables
    steps = [dict(s) for s in p.get("steps") or []]
    steps = [s for s in steps if s.get("step") != step] + [{"step": step, "subject": r.subject, "text": r.text,
                                                           "source": source}]
    edits.append({"step": step, "by": by, "at": ctx.now.isoformat(), "accepted": True})
    p.pop("held", None)  # an approval of the earlier version does not carry over
    p.update(lead=lead, steps=sorted(steps, key=lambda s: s.get("step") or 0), edited=True, state=WAITING, edits=edits)
    text_, blocks = version_message(p, step, by)
    note = _thread(slack, item, text_, blocks)
    _seed(slack, item.channel, note, SEED_APPROVE)
    p["approve_ts"] = note
    _save(ctx, item)
    _update_card(ctx, slack, item, f"✏️ Edited ({_who(by)}): approve the new version in the thread")
    return True


def drop_contact(ctx: Context, item: Item, *, by: str, via: str, slack: Any = None) -> dict:
    """👤: not this person. The contact is marked suppressed so pick_contacts finds the next; the account stays."""
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "by": by, "via": via,
                              "outcome": CONTACT_REJECTED}
    if ctx.dry_run:
        return {**result, "dry_run": True, "done": False}
    where = "at the command line" if by == CLI_APPROVER else f"in Slack by {by}"
    reason = f"declined {where} at a send approval ({ctx.today_uk():%-d %b %Y})"
    company = _esc(item.company)
    if not _close(ctx, item, CONTACT_REJECTED, by, slack, via=via,
                  status=f"👤 {_esc(item.person)} declined ({_who(by)}): {company} stays in the queue",
                  note=f"👤 Declined ({_who(by)}): {_esc(item.person)} won't be emailed. {company} stays in the queue: "
                       "pick_contacts looks for the next-ranked person at 05:30 on a send day, and the next enrol "
                       "after that proposes them here."):
        return {**result, "done": False, "why": ["another run is handling this item"]}
    if item.contact_id:
        ctx.store.update("contacts", {"contact_id": item.contact_id}, {"suppressed": True, "suppressed_reason": reason})
    return {**result, "done": True}


def drop_company(ctx: Context, item: Item, *, by: str, via: str, slack: Any = None) -> dict:
    """🚫: drop the company. Excluded with a declined_in_slack fact, so a rescore keeps it out."""
    result: dict[str, Any] = {"item": item.short_id, "account_id": item.account_id, "by": by, "via": via,
                              "outcome": COMPANY_REJECTED}
    if ctx.dry_run:
        return {**result, "dry_run": True, "done": False}
    where = "at the command line" if by == CLI_APPROVER else f"in Slack by {by}"
    company = _esc(item.company)
    if not _close(ctx, item, COMPANY_REJECTED, by, slack, via=via,
                  status=f"🚫 {company} dropped ({_who(by)}): excluded",
                  note=f"🚫 Dropped ({_who(by)}): {company} is excluded and won't be proposed again."):
        return {**result, "done": False, "why": ["another run is handling this item"]}
    enrol.mark_excluded(ctx, {"account_id": item.account_id}, DECLINED_IN_SLACK,
                        f"dropped {where} at a send approval ({ctx.today_uk():%-d %b %Y})", source=KIND)
    return {**result, "done": True}


def expire(ctx: Context, item: Item, slack: Any) -> bool:
    """Not approved by the end of its next send day: closed by "system", and the account goes back to the queue."""
    last = _date(item.payload.get("expires_on"))
    reason = f"not approved by the end of {_day(last)} (UK)"
    held = (item.payload.get("held") or {}).get("reasons") or {}
    if held:  # approved, but a hold never cleared
        reason = f"approved, but still held at the end of {_day(last)} (UK): {'; '.join(held.values())}"
    company = _esc(item.company)
    return _close(ctx, item, EXPIRED, SYSTEM, slack, reason=reason, via="expiry",
                  status=f"⌛ Expired: {reason}; nothing was added and {company} goes back to the queue",
                  note=f"⌛ Expired: {reason}. Nothing was added; {company} goes back to the queue.")


# -- the poll_approvals pass ------------------------------------------------------------------------------------


@dataclass
class _Run:
    outcomes: Counter[str] = field(default_factory=Counter)
    rejected: list[str] = field(default_factory=list)
    editing: list[str] = field(default_factory=list)
    edits: Counter[str] = field(default_factory=Counter)  # accepted, refused
    not_added: list[dict] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)  # approvals a hold stopped this run; they stay valid
    cards_posted: int = 0
    unsure: list[str] = field(default_factory=list)
    would: list[dict] = field(default_factory=list)
    ignored: int = 0
    errors: list[str] = field(default_factory=list)
    plan_full: bool = False  # Instantly's plan had no room at an add this run: the rest are held without one

    def add(self, name: str, entry: Any) -> None:
        bucket = getattr(self, name)
        if len(bucket) < LIST_LIMIT:
            bucket.append(entry)


def _act(ctx: Context, item: Item, cmd: Command, by: str, via: str, slack: Any, run: _Run) -> bool:
    """Carry out one decision (live). True when nothing more is read for the item this run."""
    state = item.state
    if cmd.kind == "send":
        if run.plan_full:  # Instantly's plan was full earlier this run: held without another add (enrol/plan.py)
            _hold(ctx, item, {HOLD_PLAN: plan.HOLD}, by=by, via=via, slack=slack)
            run.add("held", {"item": item.short_id, "why": [plan.HOLD]})
            return True
        res = send(ctx, item, by=by, via=via, slack=slack)
        run.plan_full = run.plan_full or bool(res.get("plan_full"))
        if res.get("added"):
            run.outcomes[res["outcome"]] += 1
        elif res.get("outcome") == BLOCKED:
            run.outcomes[BLOCKED] += 1
        elif res.get("held"):
            run.add("held", {"item": item.short_id, "why": res["held"]})
        else:
            run.add("not_added", res)
        return True
    if cmd.kind == "reject":
        if state == WAITING:
            reject(ctx, item, by=by, via=via, slack=slack)
            run.add("rejected", item.short_id)
        return False
    if cmd.kind == "edit":
        if state != EDITING:
            start_edit(ctx, item, by=by, slack=slack)
            run.add("editing", item.short_id)
        return False
    if cmd.kind == "edit_text":
        run.edits["accepted" if apply_edit(ctx, item, cmd.text, by=by, slack=slack) else "refused"] += 1
        return False
    fn = drop_contact if cmd.kind == "contact" else drop_company
    res = fn(ctx, item, by=by, via=via, slack=slack)
    if res.get("done"):
        run.outcomes[res["outcome"]] += 1
    return True


def _first_by(reactions: Sequence[Mapping[str, Any]], names: frozenset[str], approvers: frozenset[str],
              bot: str, run: _Run) -> str:
    """The first approver with one of these reactions, else ""; others (not the bot) are counted as ignored."""
    for r in reactions:
        if r.get("name") not in names:
            continue
        for u in r.get("users") or ():
            if bot and u == bot:
                continue  # its own seeded ✅ and ❌, even if its id were on approver_slack_ids
            if u in approvers:
                return str(u)
            run.ignored += 1
    return ""


def _reaction_decision(item: Item, slack: Any, approvers: frozenset[str], bot: str, run: _Run) -> Command | None:
    """What the reactions say now: ❌ / ✅ on the message that counts, or a choice on the choices message."""
    p = item.payload
    if item.state == WAITING and p.get("approve_ts"):
        rs = slack.reactions(item.channel, p["approve_ts"])
        no, yes = _first_by(rs, REJECT_REACTIONS, approvers, bot, run), _first_by(rs, APPROVE_REACTIONS, approvers, bot, run)
        if no:  # ❌ beats ✅
            return Command("reject", no)
        if yes:
            return Command("send", yes)
    elif item.state == REJECTED and p.get("choices_ts"):
        rs = slack.reactions(item.channel, p["choices_ts"])
        for names, kind in ((EDIT_REACTIONS, "edit"), (CONTACT_REACTIONS, "contact"), (COMPANY_REACTIONS, "company")):
            by = _first_by(rs, names, approvers, bot, run)
            if by:  # the least drastic choice wins
                return Command(kind, by)
    return None


def _work(ctx: Context, item: Item, slack: Any, bot: str, run: _Run) -> None:
    approvers = frozenset(x.strip() for x in ctx.settings.general.approver_slack_ids if x.strip())
    p = item.payload
    seen = before = str(p.get("seen_ts") or "")
    replies = sorted(slack.replies(item.channel, item.ts), key=lambda m: _key(m.get("ts")))
    for m in replies:
        if _key(m.get("ts")) <= _key(seen):
            continue
        seen = str(m.get("ts"))
        user = str(m.get("user") or "")
        if m.get("bot_id") or m.get("subtype") == "bot_message" or (bot and user == bot):
            continue  # the bot's own notes
        cmd = parse_command(m.get("text"), item.state)
        if cmd is None:
            continue
        if user not in approvers:
            run.ignored += 1
            continue
        if ctx.dry_run:
            run.add("would", {"item": item.short_id, "action": cmd.kind, "by": user, "via": "thread"})
            return
        item.payload["seen_ts"] = seen
        if _act(ctx, item, cmd, user, "thread", slack, run):
            return
        if item.status != OPEN:
            return
    if ctx.live and seen != before:  # the newest reply read, so it is not read again
        item.payload["seen_ts"] = seen
        _save(ctx, item)
    if item.status != OPEN:
        return
    decision = _reaction_decision(item, slack, approvers, bot, run)
    if decision is not None:
        via = {"send": "✅", "reject": "❌", "edit": "✏️", "contact": "👤", "company": "🚫"}[decision.kind]
    else:  # an approval a hold stopped (a "send" reply, or the command line) is tried again
        decision, via = _retry(item, approvers)
        if decision is None:
            return
    if ctx.dry_run:
        run.add("would", {"item": item.short_id, "action": decision.kind, "by": decision.text, "via": via})
        return
    _act(ctx, item, Command(decision.kind), decision.text, via, slack, run)


def _stuck(ctx: Context, item: Item, slack: Any, run: _Run) -> None:
    """An item left "sending" by a run that stopped, or whose lead Instantly did not confirm: the campaign is
    looked up (enrol.campaign_lead_ids). A lead found there is recorded and the item closed approved, by whoever
    approved it. One not there, after Instantly answered the add (sending.answered), was refused (_refused);
    otherwise it goes back to waiting for a person's fresh ✅: it is never added again alone, as the add may
    never have reached Instantly. If the campaign cannot be read, a person is asked to check."""
    p = item.payload
    sending = dict(p.get("sending") or {})
    started = parse_ts(sending.get("at"))
    if started is not None and ctx.now - started < STUCK_AFTER:
        return  # an add may still be going
    if ctx.dry_run:
        run.add("unsure", item.short_id)
        return
    campaign, email = _text(p.get("campaign")), item.email.lower()
    try:
        found: dict[str, str] | None = enrol.campaign_lead_ids(ctx, campaign, [email]) if campaign and email else {}
    except (ApiError, LookupError, ConfigError) as exc:
        found = None
        log("send_approval_lookup_failed", item_id=item.id, error=str(exc)[:200])
    by, via = _text(sending.get("by")) or SYSTEM, _text(sending.get("via"))
    if found and found.get(email):
        res = _added(ctx, item, found[email], by=by, via=via, slack=slack)
        run.outcomes[res["outcome"]] += 1
        log("send_approval_found", item_id=item.id)
        return
    if found is not None and sending.get("answered") and sending.get("plan_full"):  # no room then: held, kept
        _plan_full(ctx, item, by=by, via=via, slack=slack, alert=None)
        run.add("held", {"item": item.short_id, "why": [plan.HOLD]})
        return
    if found is not None and sending.get("answered"):  # Instantly had the add and did not take it: refused
        run.outcomes[_refused(ctx, item, by=by, via=via, slack=slack)["outcome"]] += 1
        return
    if found is None:
        run.add("unsure", item.short_id)
        text = (f"I can't tell whether {_esc(item.email)} was added to {_esc(campaign)} (the run adding it stopped, "
                "and Instantly could not be read). Check the campaign in Instantly; if the lead is not there, ✅ this "
                "message or reply \"send\" to add it.")
    else:
        run.add("not_added", {"item": item.short_id, "why": ["the run adding it stopped before Instantly had it"]})
        text = (f"{_esc(item.email)} is not in {_esc(campaign)}: the run adding it stopped before Instantly had it, so "
                "nothing was sent. ✅ this message or reply \"send\" to add it.")
    note = _thread(slack, item, text)
    _seed(slack, item.channel, note, SEED_APPROVE[:1])
    p.update(state=WAITING, approve_ts=note, unsure=p.pop("sending", None))
    _cas(ctx, item, OPEN)
    log("send_approval_unsure", item_id=item.id, looked_up=found is not None)


def _post_missing(ctx: Context, slack: Any, run: _Run) -> None:
    """Live: a card that could not be posted when enrol ran, or its follow-ups thread, is posted now."""
    for item in items(ctx.store, (OPEN,)):
        if is_expired(item.payload, ctx.today_uk()):
            continue
        try:
            if not item.ts:
                if post_card(ctx, slack, item):
                    run.cards_posted += 1
            elif not item.payload.get("followups_ts"):
                post_followups(ctx, slack, item)
        except (ApiError, LookupError) as exc:
            run.errors.append(f"{item.short_id}: the card was not posted ({str(exc)[:160]})")


def poll(ctx: Context, slack: Any | None) -> dict:
    """The send-approval pass of poll_approvals (replies/desk.py calls it): decisions, expiry, stuck adds."""
    run = _Run()
    if ctx.live and slack is not None:
        _post_missing(ctx, slack, run)
    today = ctx.today_uk()
    todo = items(ctx.store)
    bot: str | None = None  # the bot's own user id, asked once when an item is read
    for item in todo:
        try:
            if item.status == SENDING:
                _stuck(ctx, item, slack, run)
            elif is_expired(item.payload, today):
                if ctx.dry_run:
                    run.add("would", {"item": item.short_id, "action": "expire"})
                elif expire(ctx, item, slack):
                    run.outcomes[EXPIRED] += 1
            elif slack is not None and item.ts:
                if bot is None:
                    bot = slack.bot_user_id()
                _work(ctx, item, slack, bot, run)
            else:  # no Slack: an approval a hold stopped still goes through once it clears
                approvers = frozenset(x.strip() for x in ctx.settings.general.approver_slack_ids if x.strip())
                cmd, via = _retry(item, approvers)
                if cmd is not None and ctx.dry_run:
                    run.add("would", {"item": item.short_id, "action": cmd.kind, "by": cmd.text, "via": via})
                elif cmd is not None:
                    _act(ctx, item, Command(cmd.kind), cmd.text, via, slack, run)
        except (ApiError, LookupError) as exc:  # one item's trouble never stops the others
            run.errors.append(f"{item.short_id}: {type(exc).__name__}: {str(exc)[:160]}")
    out: dict[str, Any] = {
        "items": len(todo), "outcomes": dict(run.outcomes), "rejected": run.rejected, "editing": run.editing,
        "edits": dict(run.edits), "not_added": run.not_added, "held": run.held, "cards_posted": run.cards_posted,
        "unsure": run.unsure, "instantly_plan_full": run.plan_full,
        "ignored_non_approvers": run.ignored, "errors": run.errors,
    }
    if ctx.dry_run:
        out["would"] = run.would
    return out


# -- the command line (`us-outbound approvals list | approve | reject`) ----------------------------------------


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


def _actionable(item: Item) -> None:
    if item.status != OPEN:
        raise ValueError(f"send approval {item.short_id} is {item.status or 'without a status'}; there is nothing to act on")


def approve(ctx: Context, ref: str) -> dict:
    """`approvals approve`: the same re-check, add and close path as a ✅, approved_by "cli"."""
    item = find_item(ctx, ref)
    _actionable(item)
    if is_expired(item.payload, ctx.today_uk()):
        raise ValueError(f"send approval {item.short_id} expired at the end of {item.payload.get('expires_on')}; "
                         "the account goes back to the queue")
    return {"dry_run": ctx.dry_run, **send(ctx, item, by=CLI_APPROVER, via="cli", slack=slack_or_none(ctx))}


def reject_item(ctx: Context, ref: str, what: str) -> dict:
    """`approvals reject --contact | --company`: the same close path as 👤 or 🚫, approved_by "cli"."""
    item = find_item(ctx, ref)
    _actionable(item)
    fn = {"contact": drop_contact, "company": drop_company}[what]
    return {"dry_run": ctx.dry_run, **fn(ctx, item, by=CLI_APPROVER, via="cli", slack=slack_or_none(ctx))}
