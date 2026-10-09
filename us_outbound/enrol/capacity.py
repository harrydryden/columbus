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
slots on the days their later steps fall. Steps a week apart (0, 7, 14, 21) always fall on
the weekday of the first; with SPEC 10's days 0, 3, 8 and 15, most later steps of leads
enrolled Wednesday to Friday fell at the weekend and piled onto Mondays (docs/pipeline.md).

A sender takes at most capacity ÷ 4 new leads a day (the pace that keeps a full day
steady), and fewer when, on any of the four days a lead enrolled today would send, the
follow-ups already due leave less room than that. So no step of any lead, old or new, has
to wait for a full inbox, including on the Mondays that collect steps due at the weekend.

Leads that have stopped (a reply, bounce or unsubscribe, the account-level stop that ends a
second contact's lead with the first's, or an account no longer enrolled)
hold nothing. A send approval still waiting in Slack (enrol/approvals.py; Harry, 2 Oct 2026)
holds one of today's slots for its sender until it is approved or expires (limits.today), so
the next enrol run never proposes more than the senders can send.

Each mailbox's sends a day are the lowest of:
  * the sending ramp (registry/ramp.py; Harry, 1 Oct 2026): 10 a day in its first sending
    week, 20 in its second, then its cap;
  * the Mailboxes tab's daily_cap;
  * Instantly's own daily limit on the account, when mailbox_health last saw one.
A mailbox a kill rule holds (learn/holds.py) has no capacity, even before the sheet syncs.

What Instantly reports back, from the latest mailbox_health run (registry/mailboxes.py):
  * each mailbox's own daily limit (the lower of it and the sheet's cap is used);
  * each mailbox's campaign sends per day: when a sender's inboxes sent fewer emails on the
    last send day than the forecast had due, Instantly is behind, and the shortfall goes out
    today, so it comes off today's room;
  * each campaign's sending status: Instantly saying a campaign or all its inboxes hit their
    daily limit marks the sender as full, which limits.py turns into "add a mailbox" when
    ready accounts are waiting.

Each campaign's own status, read from Instantly when enrol runs (campaigns_not_sending; limits.today
with campaigns=True). Campaigns are created paused (draft) and only `us-outbound start --live`
activates them, so a lead added to one that is not active (Instantly status 1) would wait there
unsent. Instantly marks an active campaign "completed" (3) once no lead is left to email, as it does
straight after `start` when the campaign has none yet; that one still takes leads, and adding them
resumes it (resume_if_completed, after each add). In a live run an owner whose campaign takes no
leads (draft or paused), is missing, or cannot be read (the safe direction) has no capacity today, so
enrol proposes no card and adds no lead for them, and the limiter line says why. A dry run counts them as usual, so previews still work, and only says so.
A campaign the blackout job paused (registry/blackout.py; Harry, 7 Oct 2026) takes no leads either, and the line says
"paused for the blackout until <day>" rather than asking for `start`: the job starts it again after the blackout. Enrol
does not run on a blackout date anyway (enrol.gate), and the steps that fall due on one go on the next send day, as
step_days has always assumed.
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
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import CAMPAIGN_STATUS, STEP_DAYS
from us_outbound.context import ET, ConfigError, Context
from us_outbound.learn import holds
from us_outbound.registry import blackout
from us_outbound.registry import ramp as ramps_
from us_outbound.settings.model import Settings
from us_outbound.timeparse import utc

# Days after the step before, from the campaign's own step days (0, 7, 14, 21 -> 0, 7, 7, 7),
# so the forecast and the Instantly campaign cannot drift apart.
STEP_DELAYS = (0,) + tuple(b - a for a, b in zip(STEP_DAYS, STEP_DAYS[1:]))
# lead_stopped: the account-level stop ended this lead because someone else at the account replied, bounced,
# unsubscribed or complained, or an address or domain at the account was suppressed while it was in flight
# (replies/account_stop.py; written only once Instantly has stopped it).
STOP_EVENTS = ("replied", "bounced", "unsubscribed", "lead_stopped")
OUT_OF_OFFICE = "out_of_office"  # a replied event of this class holds its slots
ACTIVE = "Active"
MAX_SCAN_DAYS = 400  # no send day in this long means the settings allow none
CAMPAIGN_ACTIVE = 1  # Instantly's campaign status for active (clients/instantly.CAMPAIGN_STATUS)
CAMPAIGN_COMPLETED = 3  # active with no lead left to email; the next lead added resumes it (resume_if_completed)
TAKES_LEADS = frozenset({CAMPAIGN_ACTIVE, CAMPAIGN_COMPLETED})


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


def _status(campaign: Mapping[str, Any]) -> int | None:
    try:
        return int(campaign.get("status"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def campaign_problem(owner: str, found: Iterable[Mapping[str, Any]],
                     blackout_words: Mapping[str, str] | None = None) -> str:
    """Why the owner's "US Outbound – {owner}" campaign, among those Instantly listed, takes no new leads; ""
    when it is active. blackout_words: campaign -> "paused for the blackout until …" for the campaigns the blackout
    job paused (registry/blackout.words), which wait for it rather than for `start`."""
    name = US_CAMPAIGN_PREFIX + owner
    mine = [c for c in found if str(c.get("name") or "") == name]
    if not mine:
        return (f"{owner}'s campaign ({name}) is not in Instantly: `us-outbound campaigns ensure --live` creates it, "
                "then run `us-outbound start --live`")
    if len(mine) > 1:
        return f"more than one Instantly campaign is named {name}: fix it by hand in Instantly"
    status = _status(mine[0])
    if status not in TAKES_LEADS and name in (blackout_words or {}):
        return f"{owner}'s campaign is {blackout_words[name]}"  # the blackout job starts it again then
    if status not in TAKES_LEADS:
        what = CAMPAIGN_STATUS.get(status, f"status {mine[0].get('status')}") if status is not None else "status unknown"
        return f"{owner}'s campaign is not active in Instantly ({what}): run `us-outbound start --live`"
    return ""


def resume_if_completed(ctx: Context, campaign: str) -> bool:
    """Activate the campaign again when Instantly has marked it completed; True when it did.

    Instantly marks an active campaign "completed" once it has no lead left to email, as it did straight after
    `start` for the campaigns with no leads yet (6 Oct 2026: Harry Dryden's and Hannah MacIntosh's), and a lead
    added after that waits unsent until the campaign is activated again. So every lead add (enrol, a send approval,
    a seed) calls this once the lead is in. A paused campaign (a stop, a kill rule) is left paused.
    """
    found = [c for c in ctx.clients.instantly.list_campaigns() if str(c.get("name") or "") == campaign]
    if len(found) != 1 or _status(found[0]) != CAMPAIGN_COMPLETED:
        return False
    ctx.clients.instantly.activate_campaign(campaign)
    return True


def unreadable(exc: Exception) -> str:
    return (f"Instantly could not be read ({type(exc).__name__}: {str(exc)[:120]}), so no campaign is known to be "
            "active: no new leads until it can be read")


def campaigns_not_sending(ctx: Context, owners: Iterable[str]) -> dict[str, str]:
    """owner -> why their campaign takes no new leads now (campaign_problem), for these owners; {} when every one
    is active. One read of Instantly's campaign list (list_campaigns, which also caches the ids add_leads uses).
    When Instantly cannot be read, every owner gets that reason: the safe direction."""
    owners = sorted(set(owners))
    if not owners:
        return {}
    try:
        found = ctx.clients.instantly.list_campaigns()
    except (ApiError, ConfigError, LookupError) as exc:
        return {o: unreadable(exc) for o in owners}
    paused = blackout.words(ctx.store, ctx.settings, ctx.now)
    return {o: why for o in owners if (why := campaign_problem(o, found, paused))}


def instantly_report(store: Store) -> Mapping[str, Any]:
    """The latest ok mailbox_health summary: what Instantly last reported ({} if it never ran)."""
    runs = [r for r in store.select("heartbeats", {"job": "mailbox_health", "status": "ok"})
            if isinstance(r.get("detail"), Mapping) and not r["detail"].get("skipped")]
    if not runs:
        return {}
    return max(runs, key=lambda r: utc(r.get("started_at")) or datetime.min.replace(tzinfo=UTC))["detail"]


def instantly_limits(store: Store, report: Mapping[str, Any] | None = None) -> dict[str, int]:
    """address -> Instantly's daily limit on the account, from the latest mailbox_health run."""
    report = instantly_report(store) if report is None else report
    out: dict[str, int] = {}
    for addr, limit in (report.get("instantly_daily_limits") or {}).items():
        try:
            out[str(addr).lower()] = int(limit)
        except (TypeError, ValueError):
            continue
    return out


def previous_send_day(d: date, settings: Settings) -> date | None:
    for i in range(1, 15):
        day = d - timedelta(days=i)
        if is_send_day(day, settings):
            return day
    return None


@dataclass(frozen=True)
class MailboxCap:
    address: str
    owner: str
    sheet_cap: int
    instantly_limit: int | None
    ramp: ramps_.Ramp | None = None  # its place on the sending ramp; None: not ramped

    @property
    def allowed(self) -> int:
        """What the jobs allow it today: the sheet's cap, or the ramp's when that is lower."""
        return self.ramp.cap if self.ramp is not None else max(0, self.sheet_cap)

    @property
    def cap(self) -> int:
        """The lower of what the jobs allow and Instantly's own limit, when Instantly reports one."""
        if self.instantly_limit is None:
            return self.allowed
        return max(0, min(self.allowed, self.instantly_limit))


def mailbox_caps(
    settings: Settings, limits: Mapping[str, int], ramps: Mapping[str, ramps_.Ramp] | None = None
) -> list[MailboxCap]:
    ramps = ramps or {}
    return [
        MailboxCap(m.address, m.owner_name, int(m.daily_cap or 0), limits.get(m.address.lower()), ramps.get(m.address.lower()))
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
        start = utc(c.get("enrolled_at"))
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
    backlog: int = 0  # emails due on the last send day that Instantly did not send; they go today
    last_day: date | None = None  # the last send day Instantly reported sends for
    sent_last_day: int | None = None  # campaign emails this sender's inboxes sent that day
    instantly_says: str = ""  # why Instantly says the campaign is not sending, when it says so
    at_limit: bool = False  # Instantly says the campaign or all its inboxes hit their daily limit
    # Send approvals still waiting in Slack (enrol/approvals.py; Harry, 2 Oct 2026): each holds one of
    # today's slots, so free is what is left after them (limits.today takes them off).
    pending: int = 0
    held_from: int | None = None  # free before the waiting send approvals took their slots
    not_sending: str = ""  # why the owner's campaign takes no new leads now (campaigns_not_sending)
    stopped: bool = False  # a live run: not_sending took every slot today

    def hold(self, n: int) -> None:
        """Take n slots for send approvals still waiting (limits.today)."""
        self.held_from = self.free
        self.pending = max(0, n)
        self.free = max(0, self.free - self.pending)

    def stop(self, why: str, *, live: bool) -> None:
        """The owner's campaign takes no new leads now: a live run gives the owner none today; a dry run only notes it."""
        self.not_sending = why
        if live:
            self.held_from = self.slots
            self.free = 0
            self.stopped = True

    @property
    def slots(self) -> int:
        """Today's new-lead slots before the waiting send approvals took theirs."""
        return self.free if self.held_from is None else self.held_from

    @property
    def full(self) -> bool:
        """At capacity: no room today, Instantly says it hit its limit, or its inboxes sent 95% of the cap."""
        near_cap = self.sent_last_day is not None and self.cap > 0 and self.sent_last_day >= 0.95 * self.cap
        return self.cap > 0 and (self.free <= 0 or self.at_limit or near_cap)

    def why_full(self) -> str:
        if self.stopped:
            return self.not_sending
        if self.at_limit:
            return f"Instantly says {self.instantly_says}"
        if self.sent_last_day is not None and self.last_day and self.sent_last_day >= 0.95 * self.cap:
            return f"its inboxes sent {self.sent_last_day} of {self.cap} on {self.last_day:%a %d %b}"
        if self.pending and self.free <= 0:
            return f"{self.pending} emails waiting for approval in Slack hold today's slots"
        return f"follow-ups already fill {self.tightest_day:%a %d %b}" if self.tightest_day else "no room today"

    @property
    def ramping(self) -> list[MailboxCap]:
        """Its mailboxes that the sending ramp holds below their cap today."""
        return [m for m in self.mailboxes if m.ramp is not None and m.ramp.ramping]

    def describe(self) -> str:
        lowered = [m for m in self.mailboxes if m.instantly_limit is not None and m.instantly_limit < m.allowed]
        note = "".join(
            f"; Instantly limits {m.address} to {m.instantly_limit} a day "
            f"({'ramp' if m.ramp is not None and m.ramp.ramping else 'sheet'}: {m.allowed})"
            for m in lowered
        )
        note += "".join(f"; {m.address} is on its {m.ramp.describe()}" for m in self.ramping if m.ramp)
        if self.stopped:
            return f"{self.owner}: no new leads today: {self.not_sending}"
        if self.not_sending:  # a dry run counts them, so previews work
            note += f"; a live run would give none: {self.not_sending}"
        if self.cap <= 0:
            return f"{self.owner}: no sending capacity{note}"
        if self.room < self.pace and self.tightest_day:
            why = f"{self.held_that_day} follow-ups already due on {self.tightest_day:%a %d %b}"
        else:
            why = f"a new lead sends 4 emails, so {self.pace} new a day keeps {self.cap} sends a day steady"
        extra = f"; Instantly is {self.backlog} emails behind from {self.last_day:%a %d %b}, sent today first" if self.backlog and self.last_day else ""
        if self.pending:
            extra += f"; {self.pending} more wait for approval in Slack"
        return f"{self.owner}: {self.free} new leads today, {self.cap} sends a day ({why}){extra}{note}"


def steady_pace(cap: int) -> int:
    """New leads a day that keep `cap` sends a day steady: each lead sends 4 emails (STEP_DELAYS), so cap ÷ 4,
    rounded up. free_slots' pace, and learn/capacity_ahead.py's capacity at full ramp."""
    return math.ceil(max(0, cap) / len(STEP_DELAYS))


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
        pace = steady_pace(cap)
        out[owner] = SenderCapacity(owner, cap, min(pace, room), tight, committed[(owner, tight)], pace, room, boxes)
    return out


def stopped_contacts(store: Store) -> set[str]:
    """Contacts whose sequence has stopped: a reply, bounce or unsubscribe, the account-level stop of a colleague's
    lead (lead_stopped), or an account no longer enrolled.

    An out-of-office reply stops nothing: stop_on_auto_reply is off (SPEC 9), so Instantly keeps sending.
    """
    out = {
        str(e.get("contact_id"))
        for t in STOP_EVENTS
        for e in store.select("events", {"type": t})
        if e.get("contact_id") and not (t == "replied" and e.get("reply_class") == OUT_OF_OFFICE)
    }
    not_enrolled = {str(a.get("account_id")) for a in store.select("accounts") if a.get("status") != "enrolled"}
    for c in store.select("contacts"):
        if c.get("enrolled_at") and str(c.get("account_id")) in not_enrolled:
            out.add(str(c.get("contact_id")))
    return out


def in_flight(store: Store, settings: Settings, today: date) -> list[dict]:
    """Contacts whose sequence is still running (Harry, 7 Oct 2026): enrolled, with a lead in a US Outbound campaign,
    not stopped (stopped_contacts), and with a step due today or later (step_days from enrolled_at, US Eastern).

    The campaign-drift hold reads it (registry/mailboxes.IN_FLIGHT_KEYS), as do `us-outbound cohorts in-flight` and
    the stop of a lead whose address or domain was suppressed after it was enrolled (replies/account_stop.py).
    """
    stopped = stopped_contacts(store)
    out: list[dict] = []
    for c in store.select("contacts"):
        start = utc(c.get("enrolled_at"))
        if (start is None or not c.get("instantly_lead_id") or not owner_of(str(c.get("instantly_campaign") or ""))
                or str(c.get("contact_id")) in stopped):
            continue
        days = step_days(start.astimezone(ET).date(), settings)
        if days and days[-1] >= today:
            out.append(c)
    return out


def sending_capacity(store: Store, settings: Settings, today: date) -> dict[str, SenderCapacity]:
    """Today's new leads per sender, from the enrolled leads, the stop events, the ramp and what Instantly reported."""
    settings = holds.with_holds(store, settings)
    report = instantly_report(store)
    contacts = [c for c in store.select("contacts") if c.get("enrolled_at")]
    stopped = stopped_contacts(store)
    committed = committed_steps(contacts, stopped, settings, today)

    # Instantly's backlog: steps due on the last send day that were not sent go out today first.
    sent_by_day = report.get("sent_by_day") or {}
    last = previous_send_day(today, settings)
    sent_last: dict[str, int] = {}
    backlog: dict[str, int] = {}
    if last is not None and sent_by_day:
        due_last = committed_steps(contacts, stopped, settings, last)
        for owner in settings.owners():
            days = [sent_by_day.get(m.address.lower(), {}) for m in settings.mailboxes if m.owner_name == owner]
            if not any(last.isoformat() in d for d in days):
                continue  # Instantly reported nothing for that day
            sent_last[owner] = sum(int(d.get(last.isoformat()) or 0) for d in days)
            backlog[owner] = max(0, due_last[(owner, last)] - sent_last[owner])
            committed[(owner, today)] += backlog[owner]

    caps = mailbox_caps(settings, instantly_limits(store, report), ramps_.ramps(store, settings, today))
    out = free_slots(settings, committed, caps, today)
    status = report.get("campaign_status") or {}
    for owner, c in out.items():
        c.backlog = backlog.get(owner, 0)
        c.last_day = last if owner in sent_last else None
        c.sent_last_day = sent_last.get(owner)
        st = status.get(owner) or {}
        c.at_limit = bool(st.get("at_limit"))
        c.instantly_says = str(st.get("meaning") or "")
    return out
