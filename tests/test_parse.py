"""Numbers and yes/no values read one way everywhere (us_outbound/parse.py, 9 Oct 2026)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from us_outbound import parse

NOT_NUMBERS = [None, "", "  ", "soon", "12 people", True, False, [], {}, object(), "nan", "inf", float("nan"),
               float("-inf"), Decimal("NaN")]


@pytest.mark.parametrize("v, want", [
    (12, 12), ("12", 12), (" 12 ", 12), ("+12", 12), ("-3", -3), ("12.0", 12), (12.0, 12), (Decimal("12"), 12),
    (Decimal("12.00"), 12), ("12.7", 12), (12.7, 12), (-12.7, -12), ("1e3", 1000), (0, 0), ("0", 0),
    ("123456789012345678901234567890", 123456789012345678901234567890),
])
def test_integer_reads(v, want):
    got = parse.integer(v)
    assert got == want and type(got) is int


@pytest.mark.parametrize("v", NOT_NUMBERS + ["1,200"])
def test_integer_refuses(v):
    assert parse.integer(v) is None


def test_integer_with_commas():
    assert parse.integer("1,200", commas=True) == 1200
    assert parse.integer(" 1,200.0 ", True) == 1200
    assert parse.integer("1,2,0,0", commas=True) == 1200
    assert parse.integer(1200, commas=True) == 1200
    assert parse.integer("soon", commas=True) is None and parse.integer(True, commas=True) is None


def test_a_failure_can_be_taken_as_zero_at_the_call_site():
    assert (parse.integer("soon") or 0) == 0 and (parse.integer(None) or 0) == 0


@pytest.mark.parametrize("v, want", [
    (12, 12.0), ("12", 12.0), (" 12.5 ", 12.5), ("12.0", 12.0), (12.5, 12.5), (Decimal("0.12"), 0.12),
    ("-0.5", -0.5), ("1e3", 1000.0), (0, 0.0),
])
def test_number_reads(v, want):
    got = parse.number(v)
    assert got == want and type(got) is float


@pytest.mark.parametrize("v", NOT_NUMBERS + ["1,200"])
def test_number_refuses(v):
    assert parse.number(v) is None


def test_number_with_commas():
    assert parse.number("1,200", commas=True) == 1200.0
    assert parse.number("1,200.5", True) == 1200.5
    assert parse.number(True, commas=True) is None and parse.number("", commas=True) is None


@pytest.mark.parametrize("v, want", [
    (True, True), (False, False), (1, True), (0, False), (2, True), (-1, True), (0.0, False), (0.5, True),
    ("true", True), (" TRUE ", True), ("Yes", True), ("1", True), ("y", False), ("no", False), ("0", False),
    ("false", False), ("", False), (None, False), ([1], False), (Decimal("1"), False),
])
def test_truthy(v, want):
    assert parse.truthy(v) is want

