"""Weekly budgets and the weekly target (budget.py; Harry, 30 Sep 2026): weeks run Monday to Sunday, UK time."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tests.fakes import make_context
from tests.test_render import make_settings
from us_outbound import budget
from us_outbound.context import UK

S = make_settings()


def test_the_week_starts_monday_midnight_uk_time():
    wed = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)
    assert budget.week_start(wed) == datetime(2026, 9, 28, 0, 0, tzinfo=UK)  # 27 Sep 23:00 UTC in summer time
    sunday_late = datetime(2026, 10, 4, 22, 30, tzinfo=UTC)  # Sunday 23:30 UK
    assert budget.week_start(sunday_late) == budget.week_start(wed)
    monday = datetime(2026, 10, 4, 23, 0, tzinfo=UTC)  # Monday 00:00 UK
    assert budget.week_start(monday) == datetime(2026, 10, 5, tzinfo=UK)


def test_a_week_across_the_clock_change_is_169_hours():
    # Aware datetimes in the same zone subtract by the clock, so measure in UTC.
    start, end = budget.week_bounds(datetime(2026, 10, 21, 12, tzinfo=UTC))  # clocks go back on Sun 25 Oct
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=169)
    start, end = budget.week_bounds(datetime(2026, 11, 4, 12, tzinfo=UTC))
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=168)
    # Sunday 23:30 UK after the change is still that week; Monday 00:00 UK is the next.
    assert start == budget.week_start(datetime(2026, 11, 8, 23, 30, tzinfo=UTC))


def test_spend_counts_only_this_week():
    now = datetime(2026, 10, 28, 10, tzinfo=UTC)  # Wednesday
    ctx = make_context(S, now=now)
    ctx.store.insert("credit_ledger", [
        {"entry_id": "a", "system": "apollo", "credits": 100.0, "occurred_at": datetime(2026, 10, 26, 0, 30, tzinfo=UK)},
        {"entry_id": "b", "system": "apollo", "credits": 50.0, "occurred_at": datetime(2026, 10, 25, 23, 59, tzinfo=UK)},  # Sunday
        {"entry_id": "c", "system": "apollo", "credits": 7.0, "occurred_at": now - timedelta(hours=1)},
        {"entry_id": "d", "system": "clay", "credits": 9.0, "occurred_at": now},
    ])
    b = budget.weekly(ctx.store, S, "apollo", now)
    assert (b.budget, b.used, b.remaining, b.spent) == (500.0, 107.0, 393.0, False)
    assert b.describe() == "Apollo: 107 of 500 credits used this week, 393 left"
    assert budget.weekly(ctx.store, S, "clay", now).used == 9.0


def test_a_zero_budget_means_the_system_is_not_called():
    ctx = make_context(make_settings(clay_weekly_credits=0.0))
    b = budget.weekly(ctx.store, ctx.settings, "clay", ctx.now)
    assert b.spent and "no weekly budget set (clay_weekly_credits is 0)" in b.describe()


def test_send_days_left_in_the_week():
    assert budget.send_days_left_in_week(date(2026, 10, 26), S) == 5  # Monday
    assert budget.send_days_left_in_week(date(2026, 10, 27), S) == 4  # Tuesday counts itself
    assert budget.send_days_left_in_week(date(2026, 10, 31), S) == 0  # Saturday
    assert budget.send_days_left_in_week(date(2026, 11, 23), S) == 0  # the Thanksgiving blackout week
    assert budget.send_days_left_in_week(date(2026, 11, 30), S) == 5


def test_the_weekly_target_is_shared_over_the_send_days_left():
    assert budget.weekly_target_today(S, 0, 5) == 30  # 150 a week
    assert budget.weekly_target_today(S, 10, 4) == 35  # a short Monday is made up later in the week
    assert budget.weekly_target_today(S, 150, 3) == 0
    assert budget.weekly_target_today(S, 0, 0) == 0


def test_rounding_up_never_takes_a_week_past_its_target():
    s = make_settings(weekly_enrol_cap=17)
    done = 0
    for days_left in (5, 4, 3, 2, 1):
        done += budget.weekly_target_today(s, done, days_left)
    assert done == 17


def test_enrolled_this_week_counts_accounts():
    now = datetime(2026, 10, 28, 10, tzinfo=UTC)
    rows = [
        {"contact_id": "1", "account_id": "a", "enrolled_at": now - timedelta(days=1)},
        {"contact_id": "2", "account_id": "a", "enrolled_at": now - timedelta(hours=1)},  # the same account
        {"contact_id": "3", "account_id": "b", "enrolled_at": now - timedelta(days=5)},  # last week
        {"contact_id": "4", "account_id": "c", "enrolled_at": None},
    ]
    assert budget.enrolled_this_week(rows, now) == 1
