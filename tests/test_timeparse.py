"""Timestamps and days read one way everywhere (us_outbound/timeparse.py, 9 Oct 2026)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from us_outbound.context import ET, UK
from us_outbound.timeparse import (
    EPOCH,
    UTC,
    et_day,
    iso_date,
    uk_day,
    uk_midnight,
    utc,
    utc_or_epoch,
    utc_strict,
    utc_strict_or_none,
)

NOON = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
BAD = [None, "", "   ", "soon", 5, 1.5, [], object()]


@pytest.mark.parametrize("text", [
    "2026-10-09T12:00:00", "2026-10-09T12:00:00Z", "2026-10-09 12:00:00", "2026-10-09T12:00:00+00:00",
    "2026-10-09T08:00:00-04:00", "2026-10-09T13:00:00+01:00", "  2026-10-09T12:00:00Z\n",
])
def test_iso_text_in_any_form_is_the_same_instant(text):
    t = utc(text)
    assert t == NOON and t.tzinfo is not None


def test_a_naive_datetime_is_utc_and_an_aware_one_is_kept():
    assert utc(datetime(2026, 10, 9, 12)) == NOON and utc(datetime(2026, 10, 9, 12)).tzinfo is UTC
    aware = datetime(2026, 10, 9, 8, tzinfo=ET)
    assert utc(aware) is aware


def test_a_date_is_midnight_in_date_tz_given_in_utc():
    assert utc(date(2026, 10, 9)) == datetime(2026, 10, 9, tzinfo=UTC)
    uk = utc(date(2026, 10, 9), date_tz=UK)
    assert uk == datetime(2026, 10, 8, 23, tzinfo=UTC) and uk.tzinfo is UTC  # British Summer Time
    assert utc(date(2026, 12, 9), date_tz=UK) == datetime(2026, 12, 9, tzinfo=UTC)
    assert utc("2026-10-09", date_tz=UK) == datetime(2026, 10, 9, tzinfo=UTC)  # text is UTC whatever date_tz


@pytest.mark.parametrize("v", BAD)
def test_utc_gives_none_for_what_it_cannot_read(v):
    assert utc(v) is None


@pytest.mark.parametrize("v", BAD)
def test_utc_or_epoch_gives_the_1970_epoch_for_what_it_cannot_read(v):
    assert utc_or_epoch(v) == EPOCH == datetime(1970, 1, 1, tzinfo=UTC)


def test_utc_or_epoch_reads_what_utc_reads():
    assert utc_or_epoch("2026-10-09T12:00:00Z") == NOON
    assert utc_or_epoch(date(2026, 10, 9)) == datetime(2026, 10, 9, tzinfo=UTC)
    assert sorted([NOON, utc_or_epoch(None)]) == [EPOCH, NOON]


@pytest.mark.parametrize("v", BAD)
def test_utc_strict_raises_for_what_it_cannot_read(v):
    with pytest.raises(ValueError):
        utc_strict(v)


def test_utc_strict_reads_what_utc_reads():
    assert utc_strict("2026-10-09 12:00:00") == NOON
    assert utc_strict(date(2026, 10, 9)) == datetime(2026, 10, 9, tzinfo=UTC)
    assert utc_strict(datetime(2026, 10, 9, 12)) == NOON


@pytest.mark.parametrize("v", [None, "", "   "])
def test_utc_strict_or_none_lets_a_missing_value_through(v):
    assert utc_strict_or_none(v) is None


@pytest.mark.parametrize("v", ["soon", 5, []])
def test_utc_strict_or_none_raises_for_what_it_cannot_read(v):
    with pytest.raises(ValueError):
        utc_strict_or_none(v)


def test_utc_strict_or_none_reads_what_utc_reads():
    assert utc_strict_or_none("2026-10-09T12:00:00Z") == NOON
    assert utc_strict_or_none(date(2026, 10, 9)) == datetime(2026, 10, 9, tzinfo=UTC)


def test_the_uk_and_eastern_day_of_an_instant():
    late = datetime(2026, 10, 9, 23, 30, tzinfo=UTC)  # 00:30 on the 10th in London, 19:30 on the 9th in New York
    assert uk_day(late) == date(2026, 10, 10) and et_day(late) == date(2026, 10, 9)
    early = "2026-10-09T02:00:00Z"  # 03:00 in London, 22:00 on the 8th in New York
    assert uk_day(early) == date(2026, 10, 9) and et_day(early) == date(2026, 10, 8)
    assert uk_day(datetime(2026, 12, 9, 23, 30)) == date(2026, 12, 9)  # GMT: naive is UTC, the same day
    assert et_day(datetime(2026, 10, 9, 1, tzinfo=timezone(timedelta(hours=5)))) == date(2026, 10, 8)


def test_a_date_is_its_own_day():
    assert uk_day(date(2026, 10, 9)) == et_day(date(2026, 10, 9)) == date(2026, 10, 9)


@pytest.mark.parametrize("v", BAD)
def test_no_day_for_what_cannot_be_read(v):
    assert uk_day(v) is None and et_day(v) is None


@pytest.mark.parametrize("v", [
    date(2026, 10, 9), "2026-10-09", " 2026-10-09 ", "2026-10-09T23:30:00-05:00", "2026-10-09 02:00:00Z",
    datetime(2026, 10, 9, 23, 30, tzinfo=ET), datetime(2026, 10, 9, 0, 30),
])
def test_iso_date_is_the_date_a_value_names_with_no_change_of_zone(v):
    assert iso_date(v) == date(2026, 10, 9)


@pytest.mark.parametrize("v", BAD + ["2026-13-01", "09/10/2026"])
def test_no_iso_date_for_what_cannot_be_read(v):
    assert iso_date(v) is None


def test_uk_midnight_in_summer_and_winter():
    assert uk_midnight(date(2026, 10, 9)) == datetime(2026, 10, 8, 23, tzinfo=UTC)
    assert uk_midnight(date(2026, 12, 9)) == datetime(2026, 12, 9, tzinfo=UTC)
    assert uk_midnight(date(2026, 10, 9)).date() == date(2026, 10, 9)  # in UK time, so .date() is the day
    assert uk_day(uk_midnight(date(2026, 10, 25))) == date(2026, 10, 25)  # the day the clocks go back
