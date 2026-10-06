"""Monthly credit budgets and the weekly enrolment target (budget.py; Harry, 30 Sep 2026), in UK time."""

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


def test_the_month_runs_in_uk_time():
    start, end = budget.month_bounds(datetime(2026, 10, 15, 12, tzinfo=UTC))
    assert start == datetime(2026, 10, 1, tzinfo=UK) and start.astimezone(UTC) == datetime(2026, 9, 30, 23, tzinfo=UTC)
    assert end == datetime(2026, 11, 1, tzinfo=UK)
    assert budget.month_bounds(datetime(2026, 12, 31, 23, 30, tzinfo=UTC))[1] == datetime(2027, 1, 1, tzinfo=UK)
    assert budget.weekdays(date(2026, 10, 1), date(2026, 11, 1)) == 22


def test_how_the_month_is_going():
    now = datetime(2026, 10, 28, 10, tzinfo=UTC)  # Wednesday: 19 weekdays gone, 3 left with today, of 22
    ctx = make_context(S, now=now)
    ctx.store.insert("credit_ledger", [
        {"entry_id": "a", "system": "apollo", "credits": 1000.0, "occurred_at": datetime(2026, 10, 5, 9, tzinfo=UTC)},
        {"entry_id": "b", "system": "apollo", "credits": 50.0, "occurred_at": datetime(2026, 9, 30, 22, 59, tzinfo=UTC)},  # September, UK time
        {"entry_id": "c", "system": "apollo", "credits": 7.0, "occurred_at": now - timedelta(hours=1)},
        {"entry_id": "d", "system": "clay", "credits": 9.0, "occurred_at": now},
    ])
    b = budget.monthly(ctx.store, S, "apollo", now)
    assert (b.budget, b.used, b.used_today, b.remaining) == (2000.0, 1007.0, 7.0, 993.0)
    assert (b.weekdays_in_month, b.weekdays_before_today, b.weekdays_left) == (22, 19, 3)
    assert round(b.allowance_today, 2) == 333.33  # what was left this morning ÷ 3 weekdays
    assert round(b.expected_by_now, 1) == 1818.2 and b.pace == "behind"
    assert round(b.projected) == 1108  # 1,007 over the 20 weekdays through today, for 22
    assert b.describe() == ("Apollo: 1,007 of 2,000 credits used this month (50%); pace to date 1,818, so behind pace; "
                            "heading for 1,108 by 31 Oct; up to 326 more today (3 weekdays left)")
    assert budget.monthly(ctx.store, S, "clay", now).used == 9.0


def test_pace_and_a_spent_month():
    base = dict(system="clay", budget=2000.0, used_today=0.0, weekdays_in_month=20, weekdays_before_today=9,
                month_end=date(2026, 11, 30))
    assert budget.Budget(used=1000.0, **base).pace == "on"  # 10 of 20 weekdays through today: 1,000 is on pace
    assert budget.Budget(used=1200.0, **base).pace == "ahead"
    assert budget.Budget(used=800.0, **base).pace == "behind"
    spent = budget.Budget(used=2000.0, **base)
    assert spent.spent and spent.left_today == 0 and spent.describe().endswith("none left this month")


def test_a_zero_budget_means_the_system_is_not_called():
    ctx = make_context(make_settings(clay_monthly_credits=0.0))
    b = budget.monthly(ctx.store, ctx.settings, "clay", ctx.now)
    assert b.spent and "no monthly budget set (clay_monthly_credits is 0)" in b.describe()


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


def test_enrolled_this_week_counts_contacts():
    """One contact a company; a second contact at the same company counts as one more (Harry, 6 Oct 2026)."""
    now = datetime(2026, 10, 28, 10, tzinfo=UTC)
    rows = [
        {"contact_id": "1", "account_id": "a", "enrolled_at": now - timedelta(days=1)},
        {"contact_id": "2", "account_id": "a", "enrolled_at": now - timedelta(hours=1)},  # the account's second
        {"contact_id": "3", "account_id": "b", "enrolled_at": now - timedelta(days=5)},  # last week
        {"contact_id": "4", "account_id": "c", "enrolled_at": None},
        {"contact_id": "1", "account_id": "a", "enrolled_at": now - timedelta(days=1)},  # the same row twice
    ]
    assert budget.enrolled_this_week(rows, now) == 2
