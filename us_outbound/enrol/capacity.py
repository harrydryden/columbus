"""Sending capacity: how many new leads each sender can take today (replaces SPEC 9's "Active caps ÷ 4").

Instantly decides the moment each email goes, inside limits the jobs set and check:
  * a mailbox sends at most its daily cap: the Mailboxes tab's daily_cap, or Instantly's own
    daily limit on the account when mailbox_health last saw a lower one;
  * a campaign's daily limit is the sum of its Active mailboxes' caps (registry/mailboxes.py);
  * sends go out only in the send window (Mon–Fri) and never on blackout dates.
A lead enrolled today sends step 1 today and steps 2–4 on days 3, 8 and 15 (SPEC 10). Each
step waits its delay after the one before and, when that falls outside the send window,
goes on the next send day: Instantly counts delays in calendar days, weekends included. So
a new lead takes one slot on each of four send days, and the leads already enrolled hold
slots on the days their later steps fall. With steps on days 0, 3, 8 and 15, most later
steps of leads enrolled Wednesday to Friday fall on a weekend and move to Monday, and
those Mondays, not the average day, are what limit a sender (docs/pipeline.md).

A sender takes at most capacity ÷ 4 new leads a day (the pace that keeps a full day
steady), and fewer when, on any of the four days a lead enrolled today would send, the
follow-ups already due leave less room than that. So no step of any lead, old or new, has
to wait for a full inbox, including on the Mondays that collect steps due at the weekend.

Leads that have stopped (a reply, bounce or unsubscribe, or an account no longer enrolled)
hold nothing. Steps Instantly is behind on are not counted yet: sync_outcomes (phase 2)
reads that backlog from Instantly, and it will come off capacity then.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.budget import is_send_day
from us_outbound.clients.db import Store
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.clients.instantly import STEP_DAYS
from us_outbound.context import ET
from us_outbound.settings.model import Settings

# Days after the step before, from the campaign's own step days (0, 3, 8, 15 -> 0, 3, 5, 7),
# so the forecast and the Instantly campaign cannot drift apart.
STEP_DELAYS = (0,) + tuple(b - a for a, b in zip(STEP_DAYS, STEP_DAYS[1:]))
STOP_EVENTS = ("replied", "bounced", "unsubscribed")
ACTIVE = "Active"
MAX_SCAN_DAYS = 400  # no send day in this long means the settings allow none


def next_send_day(d: date, settings: Settings) -> date | None:
    """d if it is a send day, else the first send day after it; None if there is none within a year."""
    for i in range(MAX_SCAN_DAYS):
        day = d + timedelta(days=i)
        if is_send_day(day, settings):
            return day
    return None


def step_days(start: date, settings: Settings) -> list[date]:
    """The send days of a lead's four steps when it is enrolled on start."""
    out: list[date] = []
    day: date | None = start
    for delay in STEP_DELAYS:
        day = next_send_day(day + timedelta(days=delay), settings) if day else None
        if day is None:
            break
        out.append(day)
    return out


def owner_of(campaign: str) -> str:
    """"US Outbound – Hannah Spalding" -> "Hannah Spalding"; "" for any other name."""
    return campaign[len(US_CAMPAIGN_PREFIX):] if campaign.startswith(US_CAMPAIGN_PREFIX) else ""


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    return None


def instantly_limits(store: Store) -> dict[str, int]:
    """address -> Instantly's daily limit on the account, from the latest mailbox_health run that saw one."""
    runs = [r for r in store.select("heartbeats", {"job": "mailbox_health", "status": "ok"})
            if isinstance(r.get("detail"), Mapping) and r["detail"].get("instantly_daily_limits")]
    if not runs:
        return {}
    latest = max(runs, key=lambda r: _ts(r.get("started_at")) or datetime.min.replace(tzinfo=UTC))
    out: dict[str, int] = {}
    for addr, limit in latest["detail"]["instantly_daily_limits"].items():
        try:
            out[str(addr).lower()] = int(limit)
        except (TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class MailboxCap:
    address: str
    owner: str
    sheet_cap: int
    instantly_limit: int | None

    @property
    def cap(self) -> int:
        """The lower of the sheet's cap and Instantly's own limit, when Instantly reports one."""
        if self.instantly_limit is None:
            return self.sheet_cap
        return max(0, min(self.sheet_cap, self.instantly_limit))


def mailbox_caps(settings: Settings, limits: Mapping[str, int]) -> list[MailboxCap]:
    return [
        MailboxCap(m.address, m.owner_name, int(m.daily_cap or 0), limits.get(m.address.lower()))
        for m in settings.mailboxes
        if m.status == ACTIVE
    ]


def committed_steps(
    contacts: Iterable[Mapping[str, Any]], stopped: set[str], settings: Settings, today: date
) -> Counter[tuple[str, date]]:
    """(owner, send day) -> steps of leads already enrolled that fall on that day, today onwards."""
    out: Counter[tuple[str, date]] = Counter()
    for c in contacts:
        owner = owner_of(str(c.get("instantly_campaign") or ""))
        start = _ts(c.get("enrolled_at"))
        if not owner or start is None or str(c.get("contact_id")) in stopped:
            continue
        for day in step_days(start.astimezone(ET).date(), settings):
            if day >= today:
                out[(owner, day)] += 1
    return out


@dataclass
class SenderCapacity:
    owner: str
    cap: int  # sends a day across the owner's Active mailboxes
    free: int  # new leads the owner can take today: the smaller of pace and room
    tightest_day: date | None  # the step day with the least room
    held_that_day: int  # steps of earlier leads already due on the tightest day
    pace: int = 0  # the steady rate: cap ÷ 4, rounded up
    room: int = 0  # cap less the steps already due, on the tightest day
    mailboxes: list[MailboxCap] = field(default_factory=list)

    def describe(self) -> str:
        lowered = [m for m in self.mailboxes if m.instantly_limit is not None and m.instantly_limit < m.sheet_cap]
        note = "".join(f"; Instantly limits {m.address} to {m.instantly_limit} a day (sheet: {m.sheet_cap})" for m in lowered)
        if self.cap <= 0:
            return f"{self.owner}: no sending capacity{note}"
        if self.room < self.pace and self.tightest_day:
            why = f"{self.held_that_day} follow-ups already due on {self.tightest_day:%a %d %b}"
        else:
            why = f"a new lead sends 4 emails, so {self.pace} new a day keeps {self.cap} sends a day steady"
        return f"{self.owner}: {self.free} new leads today, {self.cap} sends a day ({why}){note}"


def free_slots(
    settings: Settings, committed: Counter[tuple[str, date]], caps: list[MailboxCap], today: date
) -> dict[str, SenderCapacity]:
    """owner -> today's capacity for new leads, for each owner with an Active mailbox.

    Two bounds, and the smaller wins:
      * pace: cap ÷ 4 (rounded up), the rate that fills a sender's day in a steady state.
        Filling every free slot at once instead would crowd the days those leads' follow-ups
        land on and leave later days idle (tests/test_capacity.py measures it);
      * room: on each of the four days a lead enrolled today would send, the cap less the
        steps already due that day; the tightest of them. It keeps any day from being
        overfilled, including the Mondays that collect steps falling on a weekend.
    """
    by_owner: dict[str, list[MailboxCap]] = {}
    for m in caps:
        by_owner.setdefault(m.owner, []).append(m)
    days = step_days(today, settings)
    out: dict[str, SenderCapacity] = {}
    for owner, boxes in by_owner.items():
        cap = sum(m.cap for m in boxes)
        if not days or cap <= 0:
            out[owner] = SenderCapacity(owner, cap, 0, None, 0, 0, 0, boxes)
            continue
        tight = min(days, key=lambda d: (cap - committed[(owner, d)], d))
        room = max(0, cap - committed[(owner, tight)])
        pace = math.ceil(cap / len(STEP_DELAYS))
        out[owner] = SenderCapacity(owner, cap, min(pace, room), tight, committed[(owner, tight)], pace, room, boxes)
    return out


def stopped_contacts(store: Store) -> set[str]:
    """Contacts whose sequence has stopped: a reply, bounce or unsubscribe, or an account no longer enrolled."""
    out = {str(e.get("contact_id")) for t in STOP_EVENTS for e in store.select("events", {"type": t}) if e.get("contact_id")}
    not_enrolled = {str(a.get("account_id")) for a in store.select("accounts") if a.get("status") != "enrolled"}
    for c in store.select("contacts"):
        if c.get("enrolled_at") and str(c.get("account_id")) in not_enrolled:
            out.add(str(c.get("contact_id")))
    return out


def sending_capacity(store: Store, settings: Settings, today: date) -> dict[str, SenderCapacity]:
    """Today's free slots per sender, from the enrolled leads, the stop events and Instantly's limits."""
    contacts = [c for c in store.select("contacts") if c.get("enrolled_at")]
    committed = committed_steps(contacts, stopped_contacts(store), settings, today)
    return free_slots(settings, committed, mailbox_caps(settings, instantly_limits(store)), today)
