"""Weekly budgets and the weekly target (Harry, 30 Sep 2026).

A week runs from Monday 00:00 to the next Monday 00:00, UK time (Europe/London):
  * apollo_weekly_credits and clay_weekly_credits are what the jobs may spend in a week.
    Spend is what credit_ledger records; a job that spends checks what is left of the week
    before every batch and stops at zero (SPEC 1.6). Budgets are applied where credits are
    spent (source_universe, verify_in_clay, pick_contacts), not in the enrol job, which
    spends none.
  * weekly_enrol_cap is the most new accounts enrolled in a week. Each send day takes what
    is left of it ÷ the send days left in the week, so a short day is made up later in the
    same week, never in the next.
The Claude cap stays monthly: SPEC 1.1 sets it at $10 a month, the same period as the
Anthropic Console's spend limit (clients/claude.py). Unspent credits do not carry over.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.context import UK
from us_outbound.settings.model import Settings

WEEKLY_KEYS = {"apollo": "apollo_weekly_credits", "clay": "clay_weekly_credits"}


def week_start(now: datetime) -> datetime:
    """Monday 00:00 UK time of the week now falls in."""
    d = now.astimezone(UK).date()
    monday = d - timedelta(days=d.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=UK)


def week_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[Monday 00:00, next Monday 00:00), UK time; 167 or 169 hours across a clock change."""
    start = week_start(now)
    nxt = start.date() + timedelta(days=7)
    return start, datetime(nxt.year, nxt.month, nxt.day, tzinfo=UK)


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


def in_week(v: Any, now: datetime) -> bool:
    t = _ts(v)
    if t is None:
        return False
    start, end = week_bounds(now)
    return start <= t < end


def spent_this_week(store: Store, system: str, now: datetime) -> float:
    """Credits credit_ledger records for system in now's week."""
    return sum(float(r.get("credits") or 0) for r in store.select("credit_ledger", {"system": system}) if in_week(r.get("occurred_at"), now))


@dataclass(frozen=True)
class Budget:
    system: str
    budget: float
    used: float

    @property
    def remaining(self) -> float:
        return self.budget - self.used

    @property
    def spent(self) -> bool:
        """Nothing left this week (a budget of 0 means the system is not called at all)."""
        return self.remaining <= 0

    def describe(self) -> str:
        if self.budget <= 0:
            return f"{self.system.capitalize()}: no weekly budget set ({WEEKLY_KEYS[self.system]} is 0), so it is not called"
        return f"{self.system.capitalize()}: {_n(self.used)} of {_n(self.budget)} credits used this week, {_n(max(0.0, self.remaining))} left"


def _n(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.1f}"


def weekly(store: Store, settings: Settings, system: str, now: datetime) -> Budget:
    """This week's budget for apollo or clay, and what credit_ledger says is spent."""
    budget = float(getattr(settings.general, WEEKLY_KEYS[system]))
    return Budget(system, budget, spent_this_week(store, system, now))


# -- send days and the weekly target -----------------------------------------------------------


def is_blackout(day: date, settings: Settings) -> bool:
    return any(day in r for r in settings.general.blackout_dates)


def is_send_day(day: date, settings: Settings) -> bool:
    """A day in the send window (Mon–Fri) that is not a blackout date."""
    return day.weekday() in settings.general.send_window.days and not is_blackout(day, settings)


def send_days_left_in_week(today: date, settings: Settings) -> int:
    """Send days from today to Sunday, inclusive of today."""
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
    """Accounts first enrolled this week (one contact per account in v1), from contacts.enrolled_at."""
    return len({c.get("account_id") or c.get("contact_id") for c in contacts if in_week(c.get("enrolled_at"), now)})
