"""One outcomes definition, per company (W2-3, 20 Oct 2026; design-final §2.1): the Python twin of v_account_outcomes.

"First step 1 per company, delivered, human reply within 28 days" was written five times in Python (kill_rules,
daily_report, signal_review, cohorts, looks) besides the view (code-learn-reporting §1.5). It is written here once,
and the view (sql/views/00_v_account_outcomes.sql) once; tests/test_learn_outcomes.py holds them to each other on
(account_id, delivered, replied_in_window, positive_in_window, meeting_any, meeting_in_45).

  step 1         the account's first `sent` event with step 1 (by occurred_at, then event_id): its contact, mailbox and
                 time. The company is the unit, once (SPEC 2); a company never sent step 1 is in no outcome.
  delivered      that contact has no `bounced` event at step 1 or with no step. Intent to treat: a bounced company
                 stays in every denominator that counts contacted companies (design-final §2.1).
  a reply's class  its latest `reply_class_corrected` event (the person's correction: events.source is the corrected
                 reply's event_id, reply_class the class it should have; the latest occurred_at wins), else the class
                 poll_replies gave it (RT §1.1). Human: every class but out_of_office; not yet classified counts.
  replied_in_window   a human reply at the account from step 1 to 28 days after it (instantly.REPLY_WINDOW_DAYS).
  positive_in_window  a positive or referral reply in the same 28 days; first_positive_at the first at any time after
                 step 1.
  meeting_any    a `meeting_booked` event at the account at or after step 1, any time (Harry, 6 Oct 2026: a demo
                 booked in week six counts); meeting_in_45 the first within 45 days (the north star's clock).
  window_closed_28 / window_closed_45   28 and 45 days have passed since step 1 (matured, design-final §2.1).
  engaged        positive_in_window or meeting_in_45; engaged_matured is None until the 45-day window has closed.
  bounced_any    any bounce at the account, at any step (the stop rule's account-level count).
  steps_sent     the distinct steps sent to the step-1 contact (exposure, X1); mailbox_paused: a kill rule paused its
                 mailbox in the 28 days after step 1.
  stamps         what the step-1 contact was enrolled as (contacts: arm, sham_arm, group, size band, score, signals,
                 tier, angle, copy version, test, opener and subject arms, config version), the gate version (the
                 labels' hash of that config version) and the search that sourced the account (its sourced_by fact).

Read-only, Python over the tables, so it runs on a MemoryStore as on Postgres.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from us_outbound.base.holds import KIND as KILL_RULE_KIND
from us_outbound.base.holds import PAUSE_MAILBOX
from us_outbound.clients.instantly import REPLY_WINDOW_DAYS
from us_outbound.context import Context
from us_outbound.replies import kinds
from us_outbound.timeparse import utc

REPLY_DAYS = REPLY_WINDOW_DAYS  # 28
MEETING_DAYS = 45
MIN_MATURED = 30  # a rate is shown only from this many matured companies; under it, counts (design-final §2.1)
CORRECTED = "reply_class_corrected"  # a person's correction of a reply's class (the module docstring)
EVENT_TYPES = ("sent", "bounced", "replied", CORRECTED, "meeting_booked", "unsubscribed")
ID_CHUNK = 1000
_NEVER = datetime.max.replace(tzinfo=UTC)


@dataclass
class Outcome:
    """One company sent step 1 (the module docstring)."""

    account_id: str
    contact_id: str
    mailbox: str
    step1_event_id: str
    step1_at: datetime
    now: datetime
    delivered: bool = True
    bounced_any: bool = False
    first_reply_at: datetime | None = None  # the first human reply in the 28 days
    replied_in_window: bool = False
    positive_in_window: bool = False
    negative_in_window: bool = False
    first_positive_at: datetime | None = None
    first_unsubscribed_at: datetime | None = None
    first_meeting_at: datetime | None = None
    meeting_source: str = ""
    steps_sent: int = 0
    mailbox_paused: bool = False
    stamps: dict[str, Any] = field(default_factory=dict)

    @property
    def window_closed_28(self) -> bool:
        return self.now >= self.step1_at + timedelta(days=REPLY_DAYS)

    @property
    def window_closed_45(self) -> bool:
        return self.now >= self.step1_at + timedelta(days=MEETING_DAYS)

    @property
    def meeting_any(self) -> bool:
        return self.first_meeting_at is not None

    @property
    def meeting_in_45(self) -> bool:
        return self.first_meeting_at is not None and self.first_meeting_at < self.step1_at + timedelta(days=MEETING_DAYS)

    @property
    def engaged(self) -> bool:
        return self.positive_in_window or self.meeting_in_45

    @property
    def engaged_matured(self) -> bool | None:
        """engaged, once the 45-day window has closed; None before (the north star counts only matured companies)."""
        return self.engaged if self.window_closed_45 else None

    def stamp(self, key: str, default: str = "") -> str:
        return str(self.stamps.get(key) or "").strip() or default

    def as_row(self) -> dict[str, Any]:
        """The columns the parity test compares with v_account_outcomes."""
        return {"account_id": self.account_id, "delivered": self.delivered, "replied_in_window": self.replied_in_window,
                "positive_in_window": self.positive_in_window, "meeting_any": self.meeting_any,
                "meeting_in_45": self.meeting_in_45}


def _chunks(ids: Sequence[str]) -> Iterable[list[str]]:
    for i in range(0, len(ids), ID_CHUNK):
        yield list(ids[i : i + ID_CHUNK])


def _when(e: Mapping[str, Any]) -> tuple[datetime, str]:
    return utc(e.get("occurred_at")) or _NEVER, str(e.get("event_id"))


def classes(events: Iterable[Mapping[str, Any]]) -> dict[str, str | None]:
    """replied event_id -> its class after the latest correction (the module docstring)."""
    rows = sorted(events, key=_when)
    out: dict[str, str | None] = {str(e["event_id"]): e.get("reply_class") for e in rows if e.get("type") == "replied"}
    for e in rows:
        if e.get("type") == CORRECTED and str(e.get("source") or "") in out:
            out[str(e["source"])] = e.get("reply_class")
    return out


def _clean(v: Any) -> str:
    return str(v or "").strip().lower()


def per_company(ctx: Context, events: Sequence[Mapping[str, Any]] | None = None) -> list[Outcome]:
    """Every company sent step 1, oldest step 1 first (the module docstring). events: the rows of EVENT_TYPES, when
    the caller has read them already."""
    store, now = ctx.store, ctx.now
    rows = [e for e in (events if events is not None else store.select("events", {"type": list(EVENT_TYPES)}))
            if utc(e.get("occurred_at")) is not None]
    rows.sort(key=_when)
    cls = classes(rows)
    out: dict[str, Outcome] = {}
    for e in rows:
        aid = str(e.get("account_id") or "")
        if e.get("type") == "sent" and e.get("step") == 1 and aid and aid not in out:
            out[aid] = Outcome(aid, str(e.get("contact_id") or ""), str(e.get("mailbox") or "").lower(),
                               str(e.get("event_id")), utc(e["occurred_at"]), now)
    if not out:
        return []
    account_of: dict[str, str] = {}  # contact -> the account its first send went to (a reply with no account_id)
    for e in rows:
        if e.get("type") == "sent" and e.get("contact_id") and e.get("account_id"):
            account_of.setdefault(str(e["contact_id"]), str(e["account_id"]))
    step1_contact = {o.contact_id for o in out.values() if o.contact_id}
    steps: dict[str, set] = defaultdict(set)
    reply_end = timedelta(days=REPLY_DAYS)
    for e in rows:
        t = utc(e["occurred_at"])
        kind = e.get("type")
        cid = str(e.get("contact_id") or "")
        own = str(e.get("account_id") or "")
        o = out.get(own if kind == "meeting_booked" else own or account_of.get(cid, ""))  # a booking names its account
        if o is None:
            continue
        if kind == "sent":
            if cid == o.contact_id and e.get("step") is not None:
                steps[o.account_id].add(e.get("step"))
            continue
        if kind == "bounced":
            o.bounced_any = True
            if cid and cid == o.contact_id and e.get("step") in (1, None):
                o.delivered = False
            continue
        if t < o.step1_at:
            continue
        if kind == "replied":
            c = cls.get(str(e["event_id"]))
            if not kinds.is_human(c):
                continue
            if kinds.is_warm(c) and o.first_positive_at is None:
                o.first_positive_at = t
            if t < o.step1_at + reply_end:
                if o.first_reply_at is None:
                    o.first_reply_at = t
                o.replied_in_window = True
                o.positive_in_window = o.positive_in_window or kinds.is_warm(c)
                o.negative_in_window = o.negative_in_window or _clean(c) == "negative"
        elif kind == "meeting_booked" and o.first_meeting_at is None:
            o.first_meeting_at, o.meeting_source = t, str(e.get("source") or "")
        elif kind == "unsubscribed" and o.first_unsubscribed_at is None:
            o.first_unsubscribed_at = t
    for aid, s in steps.items():
        out[aid].steps_sent = len(s)
    _stamp(ctx, out, step1_contact)
    _paused(ctx, out)
    return sorted(out.values(), key=lambda o: (o.step1_at, o.account_id))


STAMPS = ("arm", "sham_arm", "industry_group_at_enrol", "size_band_at_enrol", "score_at_enrol", "signals_at_enrol",
          "tier_at_enrol", "angle", "copy_version", "test_id", "test_arm", "opener_arm", "subject_arm", "config_version",
          "copy_level", "email_source")


def _stamp(ctx: Context, out: Mapping[str, Outcome], contact_ids: set[str]) -> None:
    """What each step-1 contact was enrolled as, the gate version it passed and the search that sourced it."""
    store = ctx.store
    contacts = {str(c["contact_id"]): c for chunk in _chunks(sorted(contact_ids))
                for c in store.select("contacts", {"contact_id": chunk})}
    versions = {str(v.get("config_version")): v for v in store.select("config_versions")} if contacts else {}
    sourced: dict[str, dict] = {}
    for chunk in _chunks(sorted(out)):
        for f in store.select("signal_events", {"account_id": chunk, "fact": "sourced_by"}):
            v = f.get("value") if isinstance(f.get("value"), Mapping) else {}
            sourced.setdefault(str(f["account_id"]), dict(v))
    for o in out.values():
        c = contacts.get(o.contact_id, {})
        o.stamps = {k: c.get(k) for k in STAMPS}
        version = versions.get(str(c.get("config_version") or ""), {})
        o.stamps["gate_version"] = version.get("labels_hash")
        o.stamps["sourced_by"] = sourced.get(o.account_id, {}).get("slice")


def _paused(ctx: Context, out: Mapping[str, Outcome]) -> None:
    """mailbox_paused: a kill rule paused the step-1 mailbox in the 28 days after step 1 (exposure, X1)."""
    pauses: dict[str, list[datetime]] = defaultdict(list)
    for item in ctx.store.select("hitl_items", {"kind": KILL_RULE_KIND}):
        p = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        t = utc(item.get("created_at"))
        if p.get("action") == PAUSE_MAILBOX and t is not None and not p.get("dry_run"):
            for a in p.get("mailboxes") or ():
                pauses[str(a).lower()].append(t)
    if not pauses:
        return
    window = timedelta(days=REPLY_DAYS)
    for o in out.values():
        o.mailbox_paused = any(o.step1_at <= t < o.step1_at + window for t in pauses.get(o.mailbox, ()))


@dataclass(frozen=True)
class Tally:
    """Counts over companies: the readout's and the reports' numbers (design-final §2.2)."""

    contacted: int = 0
    delivered: int = 0
    human: int = 0
    positive: int = 0
    negative: int = 0
    meeting_in_45: int = 0
    meeting_any: int = 0
    engaged: int = 0
    matured_28: int = 0  # delivered companies whose 28-day window has closed
    human_28: int = 0
    positive_28: int = 0
    matured_45: int = 0  # contacted companies (ITT) whose 45-day window has closed
    engaged_45: int = 0
    meetings_45: int = 0


def tally(found: Iterable[Outcome]) -> Tally:
    rows = list(found)
    delivered = [o for o in rows if o.delivered]
    at_28 = [o for o in delivered if o.window_closed_28]
    at_45 = [o for o in rows if o.window_closed_45]
    return Tally(
        contacted=len(rows), delivered=len(delivered), human=sum(o.replied_in_window for o in delivered),
        positive=sum(o.positive_in_window for o in delivered), negative=sum(o.negative_in_window for o in delivered),
        meeting_in_45=sum(o.meeting_in_45 for o in rows), meeting_any=sum(o.meeting_any for o in rows),
        engaged=sum(o.engaged for o in rows), matured_28=len(at_28), human_28=sum(o.replied_in_window for o in at_28),
        positive_28=sum(o.positive_in_window for o in at_28), matured_45=len(at_45),
        engaged_45=sum(o.engaged for o in at_45), meetings_45=sum(o.meeting_in_45 for o in at_45))
