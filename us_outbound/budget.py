"""Monthly credit budgets, how the month is going against them, and the weekly enrolment target.

Harry, 30 Sep 2026:
  * apollo_monthly_credits and clay_monthly_credits are what the jobs may spend in a calendar
    month (UK time), as Apollo and Clay themselves count credits. Spend is what credit_ledger
    records. Each weekday may spend what is left of the month ÷ the weekdays left, including
    today, so the budget lasts the month instead of going in the first week. The jobs that
    spend (source_universe, verify_in_clay, pick_contacts) check this before every batch and
    stop at zero (SPEC 1.6). Enrolment spends no credits, so budgets never hold it back directly.
  * weekly_enrol_cap is the most new accounts enrolled in a week, Monday to Sunday, UK time (a second
    contact at an account counts as one more; enrol/second.py, 6 Oct 2026).
    Each send day takes what is left of it ÷ the send days left in the week, so a short day
    is made up later in the same week.
The Claude cap is monthly too, a UTC month as the Anthropic Console counts it (clients/claude.py).
Unspent credits do not carry over to the next month.

A share for some jobs (share_for_jobs; build, 1 Oct 2026, for the 5 Oct pilot): source_universe
runs at 03:00, apollo_signals at 03:30 and apollo_enrich at 04:10 (2 Oct 2026), before pick_contacts
at 05:30, so each may spend only its share of apollo_monthly_credits (sources/apollo_credits.py),
paced by the weekday like the whole budget. Today's room is the smaller of what is left of the
whole budget today and of the share today.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clients.db import Range, Store
from us_outbound.context import UK
from us_outbound.settings.model import Settings

MONTHLY_KEYS = {"apollo": "apollo_monthly_credits", "clay": "clay_monthly_credits"}
AHEAD = 1.10  # spend more than 10% above the month's pace is "ahead of budget"
PACE_WORDS = {"ahead": "ahead of pace", "on": "on pace", "behind": "behind pace"}


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


def _uk_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UK)


# -- the month --------------------------------------------------------------------------------


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[the 1st 00:00, the next 1st 00:00), UK time."""
    d = now.astimezone(UK).date()
    first = d.replace(day=1)
    nxt = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return _uk_midnight(first), _uk_midnight(nxt)


def weekdays(start: date, end: date) -> int:
    """Mondays to Fridays in [start, end)."""
    return sum((start + timedelta(days=i)).weekday() < 5 for i in range((end - start).days))


@dataclass(frozen=True)
class Budget:
    """One month's budget for apollo or clay, and how the month is going."""

    system: str
    budget: float
    used: float  # this month, including today
    used_today: float
    weekdays_in_month: int
    weekdays_before_today: int
    month_end: date  # the last day of the month
    today_is_weekday: bool = True

    @property
    def remaining(self) -> float:
        return self.budget - self.used

    @property
    def spent(self) -> bool:
        """Nothing left this month (a budget of 0 means the system is not called at all)."""
        return self.remaining <= 0

    @property
    def weekdays_left(self) -> int:
        """Weekdays from today to the month's end, today included."""
        return max(0, self.weekdays_in_month - self.weekdays_before_today)

    @property
    def allowance_today(self) -> float:
        """What today may spend: what was left this morning ÷ the weekdays left, today included."""
        if self.weekdays_left <= 0:
            return max(0.0, self.remaining)
        return max(0.0, (self.budget - (self.used - self.used_today)) / self.weekdays_left)

    @property
    def left_today(self) -> float:
        return max(0.0, min(self.allowance_today - self.used_today, self.remaining))

    @property
    def weekdays_through_today(self) -> int:
        return min(self.weekdays_in_month, self.weekdays_before_today + (1 if self.today_is_weekday else 0))

    @property
    def expected_by_now(self) -> float:
        """The month's pace: the budget spread evenly over its weekdays, up to the end of today."""
        if self.weekdays_in_month <= 0:
            return self.budget
        return self.budget * self.weekdays_through_today / self.weekdays_in_month

    @property
    def projected(self) -> float | None:
        """What the month will use at the pace so far, counting today as done (the jobs spend early)."""
        if self.weekdays_through_today <= 0:
            return None
        return self.used / self.weekdays_through_today * self.weekdays_in_month

    @property
    def pace(self) -> str:
        """"ahead", "on" or "behind" the month's pace."""
        if self.expected_by_now <= 0:
            return "on"
        share = self.used / self.expected_by_now
        return "ahead" if share > AHEAD else "behind" if share < 1 / AHEAD else "on"

    def describe(self) -> str:
        name = self.system.capitalize()
        if self.budget <= 0:
            return f"{name}: no monthly budget set ({MONTHLY_KEYS[self.system]} is 0), so it is not called"
        pct = self.used / self.budget
        line = (f"{name}: {_n(self.used)} of {_n(self.budget)} credits used this month ({pct:.0%}); "
                f"pace to date {_n(self.expected_by_now)}, so {PACE_WORDS[self.pace]}")
        if self.projected is not None:
            line += f"; heading for {_n(self.projected)} by {self.month_end:%d %b}"
        if self.spent:
            return line + "; none left this month"
        return line + f"; up to {_n(self.left_today)} more today ({self.weekdays_left} weekdays left)"

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget, "used": self.used, "remaining": self.remaining, "used_today": self.used_today,
            "allowance_today": round(self.allowance_today, 2), "expected_by_now": round(self.expected_by_now, 2),
            "projected": None if self.projected is None else round(self.projected, 2), "pace": self.pace,
            "weekdays_left": self.weekdays_left,
        }


def _n(x: float) -> str:
    """Whole credits, except a fraction under 100 (Clay spends in halves)."""
    return f"{x:,.0f}" if abs(x) >= 100 or abs(x - round(x)) < 0.05 else f"{x:,.1f}"


def spent_in(store: Store, system: str, start: datetime, end: datetime, jobs: Collection[str] | None = None) -> float:
    """Credits credit_ledger records for system in [start, end); only those jobs' rows when jobs is given.

    The database reads only the period's rows (Range; 9 Oct 2026: it read every row the system ever had)."""
    where: dict[str, Any] = {"system": system, "occurred_at": Range(start, end)}
    if jobs is not None:
        where["job"] = list(jobs)
    return sum(float(r.get("credits") or 0) for r in store.select("credit_ledger", where))


def monthly(store: Store, settings: Settings, system: str, now: datetime) -> Budget:
    """This month's budget for apollo or clay, with what credit_ledger says is spent."""
    start, end = month_bounds(now)
    today = now.astimezone(UK).date()
    today_start = _uk_midnight(today)
    return Budget(
        system=system,
        budget=float(getattr(settings.general, MONTHLY_KEYS[system])),
        used=spent_in(store, system, start, end),
        used_today=spent_in(store, system, today_start, _uk_midnight(today + timedelta(days=1))),
        weekdays_in_month=weekdays(start.date(), end.date()),
        weekdays_before_today=weekdays(start.date(), today),
        month_end=end.date() - timedelta(days=1),
        today_is_weekday=today.weekday() < 5,
    )


def share_for_jobs(store: Store, settings: Settings, system: str, now: datetime, *,
                   jobs: Collection[str], share: float) -> Budget:
    """`share` of the system's monthly budget, kept for `jobs`, with what they spent; paced the same way."""
    whole = monthly(store, settings, system, now)
    start, end = month_bounds(now)
    today = _uk_midnight(now.astimezone(UK).date())
    return replace(
        whole,
        budget=whole.budget * share,
        used=spent_in(store, system, start, end, jobs),
        used_today=spent_in(store, system, today, today + timedelta(days=1), jobs),
    )


def room_today(store: Store, settings: Settings, system: str, now: datetime, *,
               jobs: Collection[str], share: float) -> tuple[float, Budget, Budget]:
    """(credits jobs may still spend today, the whole budget, their share): the smaller of the two's left_today."""
    whole = monthly(store, settings, system, now)
    part = share_for_jobs(store, settings, system, now, jobs=jobs, share=share)
    return min(whole.left_today, part.left_today), whole, part


# -- the week (the enrolment target) ------------------------------------------------------------


def week_start(now: datetime) -> datetime:
    """Monday 00:00 UK time of the week now falls in."""
    d = now.astimezone(UK).date()
    return _uk_midnight(d - timedelta(days=d.weekday()))


def week_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[Monday 00:00, next Monday 00:00), UK time; 167 or 169 hours across a clock change."""
    start = week_start(now)
    return start, _uk_midnight(start.date() + timedelta(days=7))


def in_week(v: Any, now: datetime) -> bool:
    t = _ts(v)
    if t is None:
        return False
    start, end = week_bounds(now)
    return start <= t < end


def is_blackout(day: date, settings: Settings) -> bool:
    return any(day in r for r in settings.general.blackout_dates)


def is_send_day(day: date, settings: Settings) -> bool:
    """A day in the send window (Mon–Fri) that is not a blackout date."""
    return day.weekday() in settings.general.send_window.days and not is_blackout(day, settings)


def send_days_left_in_week(today: date, settings: Settings) -> int:
    """Send days from today to Sunday, today included."""
    sunday = today + timedelta(days=6 - today.weekday())
    return sum(is_send_day(today + timedelta(days=i), settings) for i in range((sunday - today).days + 1))


def weekly_target_today(settings: Settings, enrolled_this_week: int, days_left: int) -> int:
    """What is left of weekly_enrol_cap ÷ the send days left, rounded up; 0 with no send day left.

    Rounding up never takes the week past the cap: each day takes at most what is left.
    """
    left = max(0, settings.general.weekly_enrol_cap - enrolled_this_week)
    if days_left <= 0:
        return 0
    return math.ceil(left / days_left)


def enrolled_this_week(contacts: list[Mapping[str, Any]], now: datetime) -> int:
    """Contacts enrolled this week, from contacts.enrolled_at: one a company, and a second contact at a company
    counts as one more (enrol/second.py; Harry, 6 Oct 2026: it "counts against the day's enrolment number")."""
    return len({c.get("contact_id") or c.get("account_id") for c in contacts if in_week(c.get("enrolled_at"), now)})
