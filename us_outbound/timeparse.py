"""Timestamps and days read one way everywhere (9 Oct 2026; they had ~40 private copies).

A timezone-less datetime or ISO text is UTC; "Z", an offset and a space for the "T" read alike.
utc() gives None for what it cannot read, utc_or_epoch() the 1970 epoch (for sorting),
utc_strict() raises (for values that must be there) and utc_strict_or_none() raises only for
text it cannot read (for values that may be missing but never wrong). uk_day() and et_day() give
the day of an instant in a zone; iso_date() the calendar date a stored date names.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from typing import Any

from us_outbound.context import ET, UK

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc(v: Any, *, date_tz: tzinfo = UTC) -> datetime | None:
    """An aware datetime from a datetime, ISO text or a date (midnight in date_tz); None if blank or unreadable."""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.strip())
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=date_tz).astimezone(UTC)
    return None


def utc_or_epoch(v: Any) -> datetime:
    return utc(v) or EPOCH


def utc_strict(v: Any) -> datetime:
    """utc(), raising ValueError for None, blank or unreadable text."""
    t = utc(v)
    if t is None:
        raise ValueError(f"not a timestamp: {v!r}")
    return t


def utc_strict_or_none(v: Any) -> datetime | None:
    """None for None or blank text; otherwise utc_strict(), so unreadable text still raises."""
    return None if v is None or isinstance(v, str) and not v.strip() else utc_strict(v)


def uk_day(v: Any) -> date | None:
    """The UK calendar day of a timestamp; a date is its own day."""
    return _day(v, UK)


def et_day(v: Any) -> date | None:
    """The US Eastern calendar day of a timestamp; a date is its own day."""
    return _day(v, ET)


def iso_date(v: Any) -> date | None:
    """The calendar date a value names, with no change of zone: a date, a datetime's own date, or text
    that starts YYYY-MM-DD (a stored "resume_on" or "expires_on"); None otherwise."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(v.strip()[:10]) if isinstance(v, str) else None
    except ValueError:
        return None


def uk_midnight(d: date) -> datetime:
    """The start of a UK day, in UK time, so its .date() is that day."""
    return datetime(d.year, d.month, d.day, tzinfo=UK)


def _day(v: Any, zone: tzinfo) -> date | None:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    t = utc(v)
    return t.astimezone(zone).date() if t else None
