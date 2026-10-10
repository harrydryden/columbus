"""Numbers, shares, counts and times as the posts and reports write them, one way everywhere (9 Oct 2026).

They had about twenty private copies that differed in small ways: a thousands comma or none, "-", "0%" or "n/a"
for 0 of 0, zeros kept or dropped. A difference a caller means is a parameter; the defaults are the common case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from us_outbound.context import UK
from us_outbound.timeparse import utc

WHEN = "%a %d %b %H:%M UK"  # "Fri 09 Oct 11:00 UK"
WHEN_YEAR = "%a %d %b %Y %H:%M UK"


def plural(n: int | float, one: str, many: str | None = None) -> str:
    """"1 account", "1,172 accounts"; `many` for a plural that is not one + "s"."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def pct(k: int | float, n: int | float, *, places: int = 1, empty: str = "-") -> str:
    """k of n as a percentage ("4.2%"); `empty` when n is 0."""
    return f"{k / n:.{places}%}" if n else empty


def share(x: float, *, places: int = 1) -> str:
    """A fraction already worked out, as a percentage."""
    return f"{x:.{places}%}"


def counts(c: Mapping[Any, int], *, order: Sequence[Any] | None = None, limit: int | None = None, label: str = "",
           empty: str = "") -> str:
    """"a 3, b 1": most first then by name, or the keys in `order` first and any others by name; zeros left out,
    the first `limit` only, each key after `label`; `empty` when nothing is left."""
    keys = [k for k in c if c[k]]
    if order is None:
        keys.sort(key=lambda k: (-c[k], str(k)))
    else:
        keys = [k for k in order if c.get(k)] + sorted((k for k in keys if k not in order), key=str)
    return ", ".join(f"{label}{k} {c[k]}" for k in keys[:limit]) or empty


def hours(delta: timedelta) -> str:
    """How long, in whole hours: "under an hour", "1 hour", "30 hours"."""
    n = int(delta.total_seconds() // 3600)
    return "under an hour" if n < 1 else plural(n, "hour")


def uk_time(v: Any, pattern: str = WHEN, *, missing: str = "-") -> str:
    """A stored time in UK time ("Fri 09 Oct 11:00 UK"); `missing` when there is none, and text that is not a time
    as it is, so a report never fails on one."""
    t = utc(v)
    if t is not None:
        return t.astimezone(UK).strftime(pattern)
    text = str(v if v is not None else "").strip()
    return text or missing
