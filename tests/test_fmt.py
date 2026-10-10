"""us_outbound/fmt.py: numbers, shares, counts and times written one way (9 Oct 2026)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta

from us_outbound import fmt


def test_plural_has_a_thousands_comma_and_takes_an_irregular_plural():
    assert fmt.plural(1, "account") == "1 account"
    assert fmt.plural(1172, "company", "companies") == "1,172 companies"
    assert fmt.plural(0, "reply", "replies") == "0 replies"


def test_pct_and_its_empty_value():
    assert fmt.pct(1, 24) == "4.2%"
    assert fmt.pct(1, 3, places=0) == "33%"
    assert fmt.pct(0, 0) == "-" and fmt.pct(0, 0, empty="n/a") == "n/a"
    assert fmt.share(0.125) == "12.5%" and fmt.share(0.125, places=0) == "12%"


def test_counts_most_first_or_in_an_order_with_zeros_left_out():
    c = Counter({"b": 1, "a": 3, "c": 1, "z": 0})
    assert fmt.counts(c) == "a 3, b 1, c 1"
    assert fmt.counts(c, limit=2) == "a 3, b 1"
    assert fmt.counts(c, label="step ") == "step a 3, step b 1, step c 1"
    assert fmt.counts(c, order=("c", "z", "a")) == "c 1, a 3, b 1"  # the order first, then any others by name
    assert fmt.counts(Counter(), empty="none") == "none" and fmt.counts(Counter()) == ""


def test_hours():
    assert fmt.hours(timedelta(minutes=59)) == "under an hour"
    assert fmt.hours(timedelta(hours=1, minutes=30)) == "1 hour"
    assert fmt.hours(timedelta(days=2)) == "48 hours"


def test_uk_time_reads_any_stored_form_and_never_fails():
    assert fmt.uk_time(datetime(2026, 10, 9, 10, tzinfo=UTC)) == "Fri 09 Oct 11:00 UK"
    assert fmt.uk_time("2026-12-09T10:00:00Z", fmt.WHEN_YEAR) == "Wed 09 Dec 2026 10:00 UK"  # GMT in winter
    assert fmt.uk_time(date(2026, 10, 9), "%d %b %Y") == "09 Oct 2026"
    assert fmt.uk_time(None) == "-" and fmt.uk_time("  ", missing="never") == "never"
    assert fmt.uk_time("soon") == "soon"  # text that is not a time, as it is
