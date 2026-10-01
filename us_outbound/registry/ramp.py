"""The sending ramp: what each mailbox may send today, new leads and follow-ups together.

Harry, 1 Oct 2026 (docs/gtm-review/README.md §4.2 D4 and §7.6 item 6: "caps at the ramp
value, not 30"): a newly Active mailbox sends at most RAMP_CAPS[0] (10) emails a day in its
first sending week and RAMP_CAPS[1] (20) in its second, then its daily_cap on the Mailboxes
tab (30 at most, MAX_CAP; SPEC 13). The lower of the ramp and the sheet's cap always wins.

When a mailbox's ramp starts (the first sending week is the 7 days from that date, US
Eastern, as the send window is):
  1. its first send: the earliest events row of type sent from it (sync_outcomes writes them);
  2. failing that, while the events table has no sends at all (sync_outcomes is not feeding
     it yet), its promotion: the day it became Active (a live mailbox_health promotion, or a
     live mailbox add of an address that was already warm), but never before its owner's first
     lead was enrolled, since nothing could go out before then. With no lead enrolled for its
     owner the ramp has not started. With no promotion on record, the owner's first enrolment;
  3. otherwise it has not sent yet, and today is its first sending week (10 a day).
So a mailbox that was Active for weeks but never sent still starts at 10, and the ramp only
ever errs towards sending less.

Who uses it (one place computes the number):
  * enrol/capacity.py: each mailbox's sends a day in the send forecast, so today's number
    and limits.py's explanation follow the ramp;
  * registry/mailboxes.py: each campaign's daily_limit (the sum of its Active mailboxes'
    ramped caps), so the daily drift check reports a campaign whose limit is behind the ramp
    and `campaigns ensure --fix --live` sets it (the existing drift fix); and each Instantly
    account's own daily limit, which mailbox_health sets every morning;
  * `us-outbound status`, `us-outbound golive` and the daily post show it.
PHASE0-CONFIRM: that an Instantly account's daily_limit counts every campaign email it sends
(step 1 and follow-ups alike) and not warmup, so a cap of 10 is 10 prospect emails a day.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.context import ET
from us_outbound.settings.model import Mailbox, Settings

RAMP_CAPS = (10, 20)  # sends a day in the first and the second sending week (Harry, 1 Oct 2026)
WEEK_DAYS = 7
RETIRED = "Retired"
FIRST_SEND, PROMOTION, FIRST_ENROLMENT, NOT_STARTED = "first send", "promotion", "first enrolment", ""


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


def _et_date(v: Any) -> date | None:
    t = _ts(v)
    return t.astimezone(ET).date() if t else None


@dataclass(frozen=True)
class Ramp:
    """One mailbox's place on the ramp today."""

    address: str
    owner: str
    sheet_cap: int  # daily_cap on the Mailboxes tab
    start: date | None  # the first day of its first sending week; None: it has not sent yet
    basis: str  # FIRST_SEND, PROMOTION, FIRST_ENROLMENT, or NOT_STARTED
    today: date

    @property
    def week(self) -> int:
        """Its sending week today, from 1; 1 until it has sent."""
        if self.start is None or self.today < self.start:
            return 1
        return (self.today - self.start).days // WEEK_DAYS + 1

    @property
    def ramp_cap(self) -> int | None:
        """The ramp's limit this week, or None once the ramp is over."""
        return RAMP_CAPS[self.week - 1] if self.week <= len(RAMP_CAPS) else None

    @property
    def cap(self) -> int:
        """What it may send today: the lower of the ramp and the sheet's cap."""
        cap = max(0, int(self.sheet_cap or 0))
        return cap if self.ramp_cap is None else min(cap, self.ramp_cap)

    @property
    def ramping(self) -> bool:
        """The ramp, not the sheet, sets today's cap."""
        return self.cap < max(0, int(self.sheet_cap or 0))

    def _cap_in_week(self, week: int) -> int:
        sheet = max(0, int(self.sheet_cap or 0))
        return min(sheet, RAMP_CAPS[week - 1]) if week <= len(RAMP_CAPS) else sheet

    def next_step(self) -> tuple[date, int] | None:
        """(the day its cap next rises, the cap from then), while it is ramping and has started."""
        if not self.ramping or self.start is None:
            return None
        return self.start + timedelta(days=self.week * WEEK_DAYS), self._cap_in_week(self.week + 1)

    def describe(self) -> str:
        """e.g. "ramp week 1 (first send Mon 05 Oct): 10 a day of its 30; 20 from Mon 12 Oct"; "" once it is over."""
        if not self.ramping:
            return ""
        since = f"{self.basis} {self.start:%a %d %b}" if self.start is not None else "no send yet"
        line = f"ramp week {self.week} ({since}): {self.cap} a day of its {self.sheet_cap}"
        step = self.next_step()
        if step is not None:
            return line + f"; {step[1]} from {step[0]:%a %d %b}"
        return line + f"; {self._cap_in_week(self.week + 1)} from a week after its first send"

    def as_dict(self) -> dict[str, Any]:
        return {"cap": self.cap, "sheet_cap": self.sheet_cap, "week": self.week, "ramping": self.ramping,
                "start": self.start.isoformat() if self.start else None, "basis": self.basis}


def _first_sends(store: Store) -> tuple[dict[str, date], bool]:
    """(address -> the ET date of its first send, whether the events table has any send at all)."""
    first: dict[str, date] = {}
    any_send = False
    for e in store.select("events", {"type": "sent"}):
        any_send = True
        address = str(e.get("mailbox") or "").strip().lower()
        d = _et_date(e.get("occurred_at"))
        if address and d is not None and (address not in first or d < first[address]):
            first[address] = d
    return first, any_send


def _promotions(store: Store) -> dict[str, date]:
    """address -> the ET date it last became Active, from live mailbox_health and mailbox add runs."""
    out: dict[str, date] = {}
    for r in store.select("heartbeats", {"job": ["mailbox_health", "mailbox_add"], "status": "ok"}):
        detail = r.get("detail") if isinstance(r.get("detail"), Mapping) else {}
        if r.get("dry_run") is not False or detail.get("dry_run"):
            continue  # a dry run changed nothing
        if r.get("job") == "mailbox_add":
            promoted = [detail.get("address")] if detail.get("status") == "Active" else []
        else:
            promoted = list(detail.get("promoted") or ())
        d = _et_date(r.get("started_at"))
        for a in promoted:
            address = str(a or "").strip().lower()
            if address and d is not None and (address not in out or d > out[address]):
                out[address] = d
    return out


def _first_enrolments(store: Store) -> dict[str, date]:
    """owner -> the ET date of the first lead enrolled in their campaign."""
    out: dict[str, date] = {}
    for c in store.select("contacts"):
        campaign = str(c.get("instantly_campaign") or "")
        d = _et_date(c.get("enrolled_at"))
        if not campaign.startswith(US_CAMPAIGN_PREFIX) or d is None:
            continue
        owner = campaign[len(US_CAMPAIGN_PREFIX):]
        if owner not in out or d < out[owner]:
            out[owner] = d
    return out


def _start(m: Mailbox, first: Mapping[str, date], any_send: bool, promoted: Mapping[str, date],
           enrolled: Mapping[str, date]) -> tuple[date | None, str]:
    address = m.address.lower()
    if address in first:
        return first[address], FIRST_SEND
    if any_send:
        return None, NOT_STARTED  # sends are being recorded and none came from it
    owner_first = enrolled.get(m.owner_name)
    if owner_first is None:
        return None, NOT_STARTED  # nothing has been enrolled for its owner, so nothing has gone out
    if address in promoted:
        return max(promoted[address], owner_first), PROMOTION
    return owner_first, FIRST_ENROLMENT


def ramps(store: Store, settings: Settings, today: date) -> dict[str, Ramp]:
    """lower-case address -> its Ramp today (a US Eastern date), for every mailbox not Retired."""
    boxes = [m for m in settings.mailboxes if m.status != RETIRED]
    if not boxes:
        return {}
    first, any_send = _first_sends(store)
    # Promotions and enrolments matter only while no send is recorded at all (rule 2).
    promoted = {} if any_send else _promotions(store)
    enrolled = {} if any_send else _first_enrolments(store)
    out: dict[str, Ramp] = {}
    for m in boxes:
        start, basis = _start(m, first, any_send, promoted, enrolled)
        out[m.address.lower()] = Ramp(m.address.lower(), m.owner_name, int(m.daily_cap or 0), start, basis, today)
    return out


def caps(store: Store, settings: Settings, today: date) -> dict[str, int]:
    """lower-case address -> what it may send today."""
    return {a: r.cap for a, r in ramps(store, settings, today).items()}
