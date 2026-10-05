"""Validation of the settings sheet (SPEC 5): raw tab rows in, a typed Settings out.

Each tab arrives as a list of {column: text}. validate_tab() checks one tab on its own;
validate_all() adds the checks between tabs, each attributed to the tab that holds the
reference (a signal naming a missing angle is a Signals error). Any error rejects the
whole tab: settings_sync then keeps the previous version of that tab in force.

Row numbers are sheet rows counting the header, so the first data row is row 2.
Errors are written for Harry, in American English.
"""

from __future__ import annotations

import dataclasses
import difflib
import math
import re
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from us_outbound.clean.people import title_key
from us_outbound.enrol import copy_markup
from us_outbound.enrol.copy_rules import money_violations, subject_violations
from us_outbound.settings.conditions import ConditionError, parse_condition, parse_context_rule, parse_terms, try_parse_condition
from us_outbound.settings.defaults import COLUMNS, US_STATES
from us_outbound.settings.model import (
    ACTIONS,
    CLAY_VERIFICATION_MODES,
    COPY_STATUSES,
    COPY_STEPS,
    EMAIL1_SUBJECT_VARIABLES,
    EMAIL_FORMATS,
    FOCUS_LINE_TOKENS,
    GENERAL_COPY,
    GENERIC_LINE_TOKENS,
    GENERIC_OPENER_KEY,
    GENERIC_OPENER_KEYS,
    MAILBOX_STATUSES,
    OPENER_COLUMNS,
    OPENER_SELF_COLUMN,
    OPENER_TOKENS,
    PAGE_FIELDS,
    PERSON_FIELDS,
    PLAIN_OPENER_TOKENS,
    QA_VERDICTS,
    RETIRED_OPENER_TOKENS,
    ROLE_LINE_COLUMNS,
    ROLE_ORDER_COLUMNS,
    OPTIONAL_TABS,
    SOURCE_FIELDS,
    SOURCE_KEYS,
    TABS,
    TEST_STATUSES,
    TEXT_SOURCES,
    Angle,
    CopyRow,
    CopyStep,
    DateRange,
    Focus,
    General,
    Industry,
    IndustryPage,
    Mailbox,
    NamedAccount,
    Override,
    Role,
    SendWindow,
    Settings,
    Signal,
    State,
    Test,
)

HEADER_ROW = 1
FIRST_DATA_ROW = 2
OPTIONAL_COLUMNS = frozenset({"note"})  # every other COLUMNS header must be present
# Columns added after the sheet was first made (Harry, 30 Sep 2026): a tab without them reads them as blank.
TAB_OPTIONAL_COLUMNS: dict[str, frozenset[str]] = {
    "Industries": frozenset(f"page_{f}" for f in PAGE_FIELDS),
    "Copy": frozenset({"qa_notes", "sources"}),
    "Roles": frozenset({"copy_role", "industry_groups"}),
    "Mailboxes": frozenset({"slack_id"}),  # decision D11 (Harry, 1 Oct 2026): owners approve their own replies
    "Signals": frozenset({*OPENER_COLUMNS.values(), OPENER_SELF_COLUMN}),  # tokenized openers (Harry, 2 Oct 2026)
}
# The Copy tab's layout before 30 Sep 2026 (one row per step): read as no copy, with a notice.
LEGACY_COPY_COLUMNS = frozenset({"step", "subject", "body"})
# The Roles tab's layout before 1 Oct 2026 (SPEC 5): still read, as the same order, until
# `us-outbound settings load --tab Roles` replaces it with the new rows.
LEGACY_ROLES_COLUMNS = frozenset({"first_choice_for_size", "fallback_order"})
MAX_ROLE_RANK = 20
MAY_BE_EMPTY = frozenset({"Overrides", "Tests", *OPTIONAL_TABS})  # an empty tab anywhere else is almost surely a mistake
NEVER_ACTIVE_STATES = frozenset({"CA", "WA"})  # SPEC 1.3
MAX_DAILY_CAP = 30  # SPEC 13: 30 sends per mailbox per day
CLAUDE_CAP_USD = 10.0  # SPEC 1.1
SPILL_DOMAIN = "spill.chat"  # SPEC 1.2: spill.chat never sends cold email
CONTROL_ANGLE = "General"  # SPEC 5: Control-tier accounts always get this angle
# Variables a copy row may use (render.VARIABLES; style.md).
COPY_VARIABLES = frozenset(
    {"first_name", "company", "place", "opener", "legal_overlay", "role_line", "price_line", "demo_url",
     "industry_url", "site_url", "sender_first_name", "proof"}
)
SPILL_PAGE = re.compile(r"https://(?:www\.)?spill\.chat/\S*")
OPENER_PLACEHOLDERS = frozenset(PLAIN_OPENER_TOKENS)  # SPEC 5: "Saw your benefits page mentions {evidence}"
# The tokens each opener column may use (enrol/openers.py fills them from stored facts).
OPENER_COLUMN_TOKENS: dict[str, tuple[str, ...]] = {
    "opener": PLAIN_OPENER_TOKENS,
    **{col: OPENER_TOKENS for col in (*OPENER_COLUMNS.values(), OPENER_SELF_COLUMN)},
}
# The sheet row whose key names each tab's rows; settings_sync versions rows by it.
KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "General": ("key",),
    "Signals": ("signal",),
    "Angles": ("angle",),
    "Industries": ("industry",),
    "States": ("state",),
    "Roles": ("role",),
    "Copy": ("copy_version",),
    "Mailboxes": ("address",),
    "Overrides": ("domain", "field"),
    "Tests": ("test_id",),
    "Focus": ("industry_group",),
    "Named accounts": ("domain",),
}
# General keys that must not be blank. Other text keys may be blank until phase 0 fills them.
_GENERAL_REQUIRED_TEXT = frozenset(
    {"escalation_email", "alert_channel", "dev_channel", "booking_link", "booking_page", "demo_host",
     "hubspot_pipeline", "claude_model", "claude_task_model", "email_format", "site_url", "clay_verification"}
)

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FULL_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_EMAIL = re.compile(r"[a-z0-9._%+'-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
_CHANNEL = re.compile(r"#[a-z0-9][a-z0-9_-]*")
_SLACK_USER = re.compile(r"[UW][A-Z0-9_]+")
_NAICS = re.compile(r"\d{2,6}")
_FIELD = re.compile(r"[a-z_][a-z0-9_]*")
_VARIABLE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_SINGLE_BRACE = re.compile(r"(?<!\{)\{([^{}]*)\}(?!\})")


@dataclass(frozen=True)
class RowError:
    tab: str
    row: int  # sheet row, header included: the first data row is 2; tab-level errors use 1
    column: str
    message: str
    label: str = ""  # the row's key, so the error still points at the right row if rows move

    def __str__(self) -> str:
        where = f"{self.tab} row {self.row}"
        if self.label:
            where += f" ({self.label})"
        if self.column:
            where += f", {self.column}"
        return f"{where}: {self.message}"


# -- cell parsers (raise ValueError with a message for Harry) ---------------------


def _hint(value: str, choices: Iterable[str]) -> str:
    close = difflib.get_close_matches(value, list(choices), n=1, cutoff=0.6)
    return f" (did you mean {close[0]}?)" if close else ""


def parse_bool(text: str) -> bool:
    t = text.strip().lower()
    if t in ("yes", "true"):
        return True
    if t in ("no", "false"):
        return False
    raise ValueError(f"must be yes or no, not {text!r}")


def parse_int(text: str) -> int:
    t = text.strip()
    if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", t):
        t = t.replace(",", "")
    if not re.fullmatch(r"[+-]?\d+", t):
        raise ValueError(f"must be a whole number, not {text!r}")
    return int(t)


def parse_float(text: str) -> float:
    try:
        v = float(text.strip())
    except ValueError:
        raise ValueError(f"must be a number, not {text!r}") from None
    if not math.isfinite(v):
        raise ValueError(f"must be a number, not {text!r}")
    return v


def parse_date(text: str) -> date:
    t = text.strip()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            raise ValueError
        return date.fromisoformat(t)
    except ValueError:
        raise ValueError(f"must be a date written YYYY-MM-DD, not {text!r}") from None


def split_list(text: str, seps: str = ";") -> tuple[str, ...]:
    parts = re.split("[" + re.escape(seps) + "]", text)
    return tuple(p for p in (" ".join(x.split()) for x in parts) if p)


def one_of(allowed: Iterable[str]) -> Callable[[str], str]:
    """A parser accepting any of allowed, case-insensitively, returning its canonical spelling."""
    allowed = tuple(allowed)

    def parse(text: str) -> str:
        for a in allowed:
            if text.strip().casefold() == a.casefold():
                return a
        return _fail(f"must be one of {', '.join(allowed)}, not {text!r}{_hint(text.strip(), allowed)}")

    return parse


def _fail(message: str) -> Any:
    raise ValueError(message)


def _day(name: str) -> int:
    n = name.strip().lower()
    if n in _DAYS:
        return _DAYS.index(n)
    if n in _FULL_DAYS:
        return _FULL_DAYS.index(n)
    raise ValueError(f"unknown day {name.strip()!r}")


def _parse_days(text: str) -> tuple[int, ...]:
    days: set[int] = set()
    for part in split_list(text, ","):
        m = re.fullmatch(r"(.+?)\s*[-–—]\s*(.+)", part)
        if m:
            lo, hi = _day(m[1]), _day(m[2])
            if lo > hi:
                raise ValueError(f"day range {part!r} runs backward")
            days.update(range(lo, hi + 1))
        else:
            days.add(_day(part))
    if not days:
        raise ValueError("names no days")
    return tuple(sorted(days))


def _parse_time(text: str) -> time:
    h, _, m = text.partition(":")
    try:
        return time(int(h), int(m))
    except ValueError:
        raise ValueError(f"{text!r} is not a time of day") from None


def parse_send_window(text: str) -> SendWindow:
    """"Mon–Fri 09:00–16:00 America/New_York"; a hyphen works as well as an en dash."""
    m = re.fullmatch(
        r"\s*(?P<days>.+?)\s+(?P<start>\d{1,2}:\d{2})\s*[-–—]\s*(?P<end>\d{1,2}:\d{2})\s+(?P<tz>\S+)\s*", text
    )
    if not m:
        raise ValueError(f"must look like 'Mon–Fri 09:00–16:00 America/New_York', not {text!r}")
    days = _parse_days(m["days"])
    start, end = _parse_time(m["start"]), _parse_time(m["end"])
    if start >= end:
        raise ValueError("the start time must be before the end time")
    try:
        ZoneInfo(m["tz"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {m['tz']!r}; use a name like America/New_York") from None
    return SendWindow(days, start, end, m["tz"])


def parse_blackout_dates(text: str) -> tuple[DateRange, ...]:
    """"2026-11-23..2026-11-27, 2026-12-18..2027-01-04"; a single date is a one-day range."""
    out = []
    for part in split_list(text, ",;"):
        a, sep, b = part.partition("..")
        start = parse_date(a)
        end = parse_date(b) if sep else start
        if end < start:
            raise ValueError(f"{part!r} ends before it starts")
        out.append(DateRange(start, end))
    return tuple(out)


def parse_size_range(text: str) -> str:
    """"10-49" (hyphen or en dash) -> "10-49"."""
    m = re.fullmatch(r"\s*(\d+)\s*[-–]\s*(\d+)\s*", text)
    if not m:
        raise ValueError(f"size ranges are written like 10-49, not {text!r}")
    lo, hi = int(m[1]), int(m[2])
    if lo > hi:
        raise ValueError(f"size range {text.strip()!r} runs backward")
    return f"{lo}-{hi}"


def parse_fallback_order(text: str) -> dict[str, int]:
    """"10-49:2; 50-249:3" -> {"10-49": 2, "50-249": 3}. Rank 1 is the first choice, so ranks start at 2."""
    out: dict[str, int] = {}
    for part in split_list(text, ";,"):
        rng, colon, rank = part.partition(":")
        if not colon:
            raise ValueError(f"write each fallback as range:rank, like 50-249:3, not {part!r}")
        key = parse_size_range(rng)
        n = parse_int(rank)
        if n < 2:
            raise ValueError(f"fallback rank for {key} must be 2 or more (1 is the first choice)")
        if key in out:
            raise ValueError(f"{key} is listed twice")
        out[key] = n
    return out


def _parse_sources(text: str) -> tuple[str, ...]:
    out: list[str] = []
    for s in split_list(text, ",;"):
        key = s.lower()
        if key not in SOURCE_KEYS:
            raise ValueError(f"unknown source {s!r}{_hint(key, SOURCE_KEYS)}; sources are {', '.join(SOURCE_KEYS)}")
        if key not in out:
            out.append(key)
    if not out:
        raise ValueError("names no source")
    return tuple(out)


def _https(text: str) -> str:
    if not re.fullmatch(r"https://\S+\.\S+", text):
        raise ValueError(f"must be a full https:// link, not {text!r}")
    return text


def _email(text: str) -> str:
    t = text.strip().lower()
    if not _EMAIL.fullmatch(t):
        raise ValueError(f"{text!r} is not an email address")
    return t


# -- rows ------------------------------------------------------------------------


def _cell(row: Mapping[str, Any], col: str) -> str:
    v = row.get(col)
    return "" if v is None else str(v).strip()


def natural_key(tab: str, row: Mapping[str, Any]) -> str:
    """The key settings_sync versions a row by: the General key, or the row's natural key."""
    return "|".join(_cell(row, c) for c in KEY_COLUMNS[tab])


def _label(tab: str, row: Mapping[str, Any]) -> str:
    return " ".join(_cell(row, c) for c in KEY_COLUMNS[tab]).strip()


class _Row:
    """One sheet row under validation. Parse failures become RowErrors against its row number."""

    def __init__(self, tab: str, number: int, raw: Mapping[str, Any], errors: list[RowError]):
        self.tab, self.number, self.raw, self.errors = tab, number, raw, errors
        self.label = _label(tab, raw)
        self.ok = True

    def text(self, col: str) -> str:
        return _cell(self.raw, col)

    def fail(self, col: str, message: str) -> None:
        self.ok = False
        self.errors.append(RowError(self.tab, self.number, col, message, self.label))

    def parse(self, col: str, fn: Callable[[str], Any], *, required: bool = True, default: Any = None) -> Any:
        t = self.text(col)
        if not t:
            if required:
                self.fail(col, "is required")
            return default
        try:
            return fn(t)
        except ValueError as exc:  # ConditionError is a ValueError
            self.fail(col, str(exc))
            return default


def is_legacy_copy(columns: Iterable[str]) -> bool:
    """True for a Copy tab still in the one-row-per-step layout (before 30 Sep 2026)."""
    cols = set(columns)
    return LEGACY_COPY_COLUMNS <= cols and "s1_body" not in cols


def is_legacy_roles(columns: Iterable[str]) -> bool:
    """True for a Roles tab still in SPEC 5's layout (first_choice_for_size, fallback_order; before 1 Oct 2026)."""
    cols = set(columns)
    return bool(LEGACY_ROLES_COLUMNS & cols) and not set(ROLE_ORDER_COLUMNS) & cols


def _prepare(tab: str, rows: Iterable[Mapping[str, Any]] | None, errors: list[RowError]) -> list[_Row] | None:
    """Tab-level checks; None when the tab cannot be read row by row."""
    if rows is None:
        if tab in OPTIONAL_TABS:
            return []  # not added to the sheet yet: the same as an empty tab
        errors.append(RowError(tab, HEADER_ROW, "", f"the sheet has no {tab} tab"))
        return None
    rows = list(rows)
    if not rows:
        if tab not in MAY_BE_EMPTY:
            errors.append(RowError(tab, HEADER_ROW, "", f"the {tab} tab has no rows"))
        return []
    present = set().union(*(set(r) for r in rows))
    if tab == "Copy" and is_legacy_copy(present):
        return []  # the old one-row-per-step layout: no copy until the new tab is loaded (sync says so)
    if tab == "Roles" and is_legacy_roles(present):
        return [_Row(tab, i + FIRST_DATA_ROW, r, errors) for i, r in enumerate(rows)]  # read by _legacy_role_order
    optional = OPTIONAL_COLUMNS | TAB_OPTIONAL_COLUMNS.get(tab, frozenset())
    missing = [c for c in COLUMNS[tab] if c not in present and c not in optional]
    if missing:
        noun = "columns are" if len(missing) > 1 else "column is"
        errors.append(RowError(tab, HEADER_ROW, ", ".join(missing), f"the {noun} missing from the header row"))
        return None
    return [_Row(tab, i + FIRST_DATA_ROW, r, errors) for i, r in enumerate(rows)]


def _unique(r: _Row, col: str, value: Any, seen: dict[Any, int], what: str = "") -> bool:
    if value is None:
        return True
    if value in seen:
        r.fail(col, f"{what or 'this'} is already on row {seen[value]}")
        return False
    seen[value] = r.number
    return True


# -- General -----------------------------------------------------------------------

_GENERAL_TYPES: dict[str, Any] = typing.get_type_hints(General)
# Keys that were renamed, and why (Harry, 30 Sep 2026): the enrolment target is weekly,
# the credit budgets monthly, like Apollo's and Clay's own.
RENAMED_GENERAL = {
    "daily_enrol_cap": ("weekly_enrol_cap", "the enrolment target is weekly, Monday to Sunday, UK time"),
    "clay_weekly_credits": ("clay_monthly_credits", "credit budgets are monthly, like Clay's own"),
    "apollo_weekly_credits": ("apollo_monthly_credits", "credit budgets are monthly, like Apollo's own"),
}
# Keys no longer used (Harry, 1 Oct 2026): emails carry no postal address and no privacy link, and
# the opt-out is Instantly's own unsubscribe link. A sheet that still has these rows stays valid;
# `us-outbound settings load` removes them.
RETIRED_GENERAL = {
    "postal_address": "emails carry no postal address (Harry, 1 Oct 2026)",
    "privacy_url": "emails carry no privacy link; the opt-out is Instantly's unsubscribe link (Harry, 1 Oct 2026)",
}


def _converter(hint: Any) -> Callable[[str], Any]:
    if hint is bool:
        return parse_bool
    if hint is int:
        return parse_int
    if hint is float:
        return parse_float
    if hint is str:
        return str
    if hint == tuple[str, ...]:
        return lambda t: split_list(t, ",")
    if hint is SendWindow:
        return parse_send_window
    if hint == tuple[DateRange, ...]:
        return parse_blackout_dates
    raise TypeError(f"no sheet converter for General type {hint!r}")


def _blank_value(hint: Any) -> Any:
    """What a blank cell means for a General key; None means the value is required."""
    if hint is str:
        return ""
    if hint in (tuple[str, ...], tuple[DateRange, ...]):
        return ()
    return None


_GENERIC_KEYS = frozenset({GENERIC_OPENER_KEY, *GENERIC_OPENER_KEYS.values()})


def _line_problems(value: str, tokens: Iterable[str]) -> list[str]:
    """A General opener line (opener_focus_line, the generic lines): its tokens, one line, no funding or money.

    Refusing money here is safe: the generic lines are new and the focus line has none, so the General tab in
    force stays valid. The Signals tab's lines may still hold the old funding lines, so `copy check` and the
    render-time check read those instead (copy_rules.money_violations), and a refusal never empties settings.
    """
    problems = opener_token_problems(value, tokens)
    if "\n" in value:
        problems.append("is one line")
    return problems + money_violations(value)


# copy_desk's sample prospect (Harbor & Finch, Dana): email1_subject is checked as it reads for them.
_SAMPLE_SUBJECT_VALUES = {"company": "Harbor & Finch", "first_name": "Dana"}
_HTML_TAG = re.compile(r"</?\s*[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")


def email1_subject_problems(value: str) -> list[str]:
    """General email1_subject (Harry, 5 Oct 2026), checked as a subject: {{company}} and {{first_name}} only, no
    markup (a subject is plain text, as render_step requires of a Copy row's), and the subject copy rules on it as
    it reads for the sample prospect (copy_rules.subject_violations), within SUBJECT_MAX characters."""
    allowed = " and ".join(f"{{{{{v}}}}}" for v in EMAIL1_SUBJECT_VARIABLES)
    out = [f"may use only {allowed}, not {{{{{name}}}}}"
           for name in _VARIABLE.findall(value) if name not in EMAIL1_SUBJECT_VARIABLES]
    out += [f"variables take double braces: write {{{{{name.strip()}}}}}, not {{{name}}}"
            for name in _SINGLE_BRACE.findall(value)]
    if copy_markup.links(value) or "**" in value or _HTML_TAG.search(value):
        out.append("has markup; a subject is plain text")
    if out:
        return list(dict.fromkeys(out))
    filled, _ = copy_markup.fill_text(value, _SAMPLE_SUBJECT_VALUES)
    return subject_violations(filled, exempt=_SAMPLE_SUBJECT_VALUES.values())


def _check_general_value(key: str, value: Any) -> None:
    hint = _GENERAL_TYPES[key]
    if hint in (int, float) and value < 0:
        raise ValueError("must not be negative")
    if key == "control_share" and not 0 <= value <= 1:
        raise ValueError("is a share: between 0 and 1, like 0.15")
    if key in ("stop_rule_bounce_rate", "stop_rule_complaint_rate") and not 0 <= value <= 1:
        raise ValueError("is a share: between 0 and 1, like 0.03 for 3%")
    if key == "opener_holdout_share" and not 0 <= value <= 1:
        raise ValueError("is a share: between 0 and 1, like 0.3 for 30% of accounts with no opener")
    if key == "email1_subject_share" and not 0 <= value <= 1:
        raise ValueError("is a share: between 0 and 1, like 0.5 for half of the email 1s with email1_subject")
    if key == "email1_subject" and value:
        problems = email1_subject_problems(value)
        if problems:
            raise ValueError("; ".join(problems))
    if key == "opener_focus_line" and value:
        problems = _line_problems(value, FOCUS_LINE_TOKENS)
        if not problems and "{focus}" not in value.replace(" ", ""):
            problems.append("must use {focus}, the phrase it exists for")
        if problems:
            raise ValueError("; ".join(problems))
    if key in _GENERIC_KEYS and value:
        problems = _line_problems(value, GENERIC_LINE_TOKENS)
        if problems:
            raise ValueError("; ".join(problems))
    if key == "escalation_hours" and value < 1:
        raise ValueError("must be at least 1")
    if key == "claude_monthly_cap_usd" and value > CLAUDE_CAP_USD:
        raise ValueError(f"may not exceed ${CLAUDE_CAP_USD:.0f} a month (SPEC 1.1)")
    if key == "escalation_email":
        _email(value)
    # A model id the cap cannot price is refused at call time (clients/claude.py), not here, so a typo
    # stops only the Claude calls, never the whole General tab. `us-outbound copy` names the ids it knows.
    if key in ("claude_model", "claude_task_model") and not re.fullmatch(r"claude-[a-z0-9.-]+", value):
        raise ValueError(f"must be a Claude model id like claude-opus-5-5 or claude-sonnet-5-5, not {value!r}")
    if key == "email_format" and value not in EMAIL_FORMATS:
        raise ValueError(f"must be one of {', '.join(EMAIL_FORMATS)}")
    if key == "clay_verification" and value not in CLAY_VERIFICATION_MODES:
        raise ValueError(f"must be one of {', '.join(CLAY_VERIFICATION_MODES)} (required: every account goes "
                         "through Clay; skip: verified on Apollo data and HubSpot)")
    if key in ("alert_channel", "dev_channel") and not _CHANNEL.fullmatch(value):
        raise ValueError(f"must be a Slack channel name like #us-outbound, not {value!r}")
    if key in ("booking_link", "booking_page") and value:
        _https(value)
    if key == "site_url":
        _page_url(value)
    if key == "price_from" and value < 1:
        raise ValueError("is the starting price in dollars a month, like 195")
    if key == "approver_slack_ids":
        bad = [v for v in value if not _SLACK_USER.fullmatch(v)]
        if bad:
            raise ValueError(f"{', '.join(bad)} is not a Slack user id (they look like U01ABCDEF)")
    # The website-visit signals (sources/site_visits.py; Harry, 5 Oct 2026).
    if key == "site_visit_domain" and value and not _DOMAIN.fullmatch(value.strip().lower()):
        raise ValueError(f"is the bare domain Apollo's tracker is on, like spill.chat (no https:// or path), not {value!r}")
    if key in ("site_visit_us_paths", "site_visit_intent_paths"):
        bad = [v for v in value if not v.startswith("/")]
        if bad:
            raise ValueError(f"{', '.join(bad)} is not a path on the site: each starts with /, like /us/pricing")


def _general(rows: list[_Row]) -> General:
    values: dict[str, Any] = {}
    where: dict[str, _Row] = {}
    for r in rows:
        key = r.text("key")
        if not key:
            if r.text("value"):
                r.fail("key", "is required")
            continue  # a note-only row
        if key in RENAMED_GENERAL:
            new, why = RENAMED_GENERAL[key]
            r.fail("key", f"{key!r} is now {new!r}: {why}")
            continue
        if key in RETIRED_GENERAL:
            continue  # no longer used; the row can be deleted
        if key not in _GENERAL_TYPES:
            r.fail("key", f"unknown key {key!r}{_hint(key, _GENERAL_TYPES)}")
            continue
        if key in where:
            r.fail("key", f"{key} is already on row {where[key].number}")
            continue
        where[key] = r
        hint = _GENERAL_TYPES[key]
        blank = _blank_value(hint)
        required = blank is None or key in _GENERAL_REQUIRED_TEXT
        v = r.parse("value", _converter(hint), required=required, default=blank)
        if not r.ok:
            continue
        try:
            _check_general_value(key, v)
        except ValueError as exc:
            r.fail("value", str(exc))
            continue
        values[key] = v

    def err(key: str, message: str) -> None:
        r = where.get(key)
        if r is not None:
            r.fail("value", message)
        elif rows:
            rows[0].errors.append(RowError("General", HEADER_ROW, "value", f"{key}: {message}"))

    g = General(**values)
    if g.standard_threshold > g.priority_threshold:
        err("standard_threshold", f"must not be above priority_threshold ({g.priority_threshold})")
    if g.dev_channel.lstrip("#").lower() == g.alert_channel.lstrip("#").lower():
        err("dev_channel", "must differ from alert_channel: dry-run posts only to the dev channel (SPEC 0.3)")
    if g.live_sending and not g.approver_slack_ids:
        err("live_sending", "cannot be yes while approver_slack_ids is blank")
    if g.email1_subject_share > 0 and not g.email1_subject.strip():
        err("email1_subject", f"cannot be blank while email1_subject_share is above 0 ({g.email1_subject_share:g}); "
                              "write a subject, or set the share to 0")
    return g


# -- the other tabs -----------------------------------------------------------------


def opener_token_problems(text: str, allowed: Iterable[str], retired: Iterable[str] = ()) -> list[str]:
    """What is wrong with the tokens of an opener line: an unknown {token} (with a "did you mean"), or {{braces}}.

    retired: tokens a line may still have without failing the tab (RETIRED_OPENER_TOKENS); they never fill.
    """
    allowed = tuple(allowed)
    listed = ", ".join(f"{{{t}}}" for t in allowed)
    out: list[str] = []
    for name in _SINGLE_BRACE.findall(text):
        token = name.strip()
        if token not in allowed and token not in retired:
            close = difflib.get_close_matches(token, list(allowed), n=1, cutoff=0.6)
            hint = f" (did you mean {{{close[0]}}}?)" if close else ""
            out.append(f"unknown token {{{name}}}{hint}; this column can use {listed}")
    if _VARIABLE.search(text):
        out.append(f"opener tokens take single braces, like {{{allowed[0]}}}; this column can use {listed}")
    return list(dict.fromkeys(out))


def _check_opener(r: _Row, col: str) -> str:
    """An opener cell: one line, or (role columns) alternatives one per line; every token one the column allows."""
    opener = r.text(col)
    retired = RETIRED_OPENER_TOKENS if col != "opener" else ()  # a sheet with the old funding lines still loads
    for problem in opener_token_problems(opener, OPENER_COLUMN_TOKENS[col], retired):
        r.fail(col, problem)
    if col == "opener" and "\n" in opener:
        r.fail(col, "the plain opener is one line; put alternatives in the opener_people, opener_founder and opener_ops columns")
    return "\n".join(line.strip() for line in opener.splitlines() if line.strip())


def _signals(rows: list[_Row]) -> list[tuple[Signal, int]]:
    out: list[tuple[Signal, int]] = []
    seen: dict[str, int] = {}
    for r in rows:
        name = r.parse("signal", str)
        _unique(r, "signal", name.casefold() if name else None, seen, f"signal {name!r}")
        sources = r.parse("source", _parse_sources)
        looks_for = r.parse("looks_for", str)
        action = r.parse("action", one_of(ACTIONS))
        weight = r.parse("weight", parse_int, required=action == "Score", default=0)
        max_weight = r.parse("max_weight", parse_int, required=False)
        if max_weight is not None and weight is not None and max_weight < weight:
            r.fail("max_weight", f"must be at least the weight ({weight})")
        days = r.parse("counts_for_days", parse_int)
        if days is not None and days < 1:
            r.fail("counts_for_days", "must be at least 1")
        active = r.parse("active", parse_bool)
        opener = _check_opener(r, "opener")
        role_openers = {role: line for role, col in OPENER_COLUMNS.items() if (line := _check_opener(r, col))}
        opener_self = _check_opener(r, OPENER_SELF_COLUMN)
        context_rule = r.text("context_rule")

        terms: tuple[str, ...] = ()
        condition = None
        context: dict[str, tuple[str, ...]] = {}
        if sources and looks_for:
            condition = try_parse_condition(looks_for)
            if condition is not None:
                known = frozenset().union(*(SOURCE_FIELDS[s] for s in sources))
                for f in sorted(condition.fields - known):
                    r.fail("looks_for", f"{f} is not a field of {', '.join(sources)}{_hint(f, known)}")
                if context_rule:
                    r.fail("context_rule", "applies only to a list of terms, not to a condition")
            elif not any(s in TEXT_SOURCES for s in sources):
                try:
                    parse_condition(looks_for)
                except ConditionError as exc:
                    r.fail("looks_for", f"{', '.join(sources)} need a condition like people_leader_count >= 1 ({exc})")
            else:
                terms = parse_terms(looks_for)
                if not terms:
                    r.fail("looks_for", "lists no terms")
                elif context_rule:
                    try:
                        context = parse_context_rule(context_rule, terms)
                    except ConditionError as exc:
                        r.fail("context_rule", str(exc))
        if opener_self and r.ok and not (condition is not None and condition.fields & PERSON_FIELDS):
            r.fail(OPENER_SELF_COLUMN, "is for a signal about one person, whose condition reads "
                   f"{', '.join(sorted(PERSON_FIELDS))}; leave it blank on this row")
        if r.ok:
            out.append((
                Signal(
                    signal=name, sources=sources, looks_for=looks_for, weight=weight, action=action,
                    counts_for_days=days, active=active, context_rule=context_rule, max_weight=max_weight,
                    suggests_angle=r.text("suggests_angle"), opener=opener, note=r.text("note"),
                    terms=terms, condition=condition, context=context, role_openers=role_openers,
                    opener_self=opener_self,
                ),
                r.number,
            ))
    return out


def _angles(rows: list[_Row]) -> list[tuple[Angle, int]]:
    out: list[tuple[Angle, int]] = []
    names: dict[str, int] = {}
    orders: dict[int, int] = {}
    for r in rows:
        name = r.parse("angle", str)
        _unique(r, "angle", name.casefold() if name else None, names, f"angle {name!r}")
        order = r.parse("order", parse_int)
        if order is not None and order < 1:
            r.fail("order", "must be 1 or more")
        _unique(r, "order", order, orders, f"order {order}")
        active = r.parse("active", parse_bool)
        argument = r.parse("argument", str)
        opener = r.parse("default_opener", str, required=bool(active), default="")
        landing = r.parse("landing_page_override", _https, required=False, default="")
        if name == CONTROL_ANGLE and active is False:
            r.fail("active", f"{CONTROL_ANGLE} must stay active: Control-tier accounts always get it")
        if r.ok:
            out.append((Angle(name, order, argument, opener, active, landing), r.number))
    if rows and CONTROL_ANGLE.casefold() not in names:
        rows[0].errors.append(
            RowError("Angles", HEADER_ROW, "angle", f"the {CONTROL_ANGLE} angle is missing; Control-tier accounts always get it")
        )
    return out


def _naics(text: str) -> tuple[str, ...]:
    codes = split_list(text, ";,")
    bad = [c for c in codes if not _NAICS.fullmatch(c)]
    if bad:
        raise ValueError(f"NAICS codes are 2 to 6 digits: {', '.join(bad)}")
    return codes


def _page_url(text: str) -> str:
    """An industry page: emails link it, and emails link only to spill.chat."""
    _https(text)
    if not SPILL_PAGE.fullmatch(text):
        raise ValueError(f"must be a page on https://www.spill.chat (emails link only there), not {text!r}")
    return text


def _industries(rows: list[_Row]) -> list[tuple[Industry, int]]:
    out: list[tuple[Industry, int]] = []
    seen: dict[str, int] = {}
    for r in rows:
        label = r.parse("industry", str)
        _unique(r, "industry", label.casefold() if label else None, seen, f"industry {label!r}")
        if label and label.casefold() == GENERAL_COPY.casefold():
            r.fail("industry", f"{GENERAL_COPY!r} is kept for the Copy tab's fallback sequence; name the industry")
        group = r.parse("industry_group", str)
        active = r.parse("active", parse_bool)
        naics = r.parse("naics_prefixes", _naics, required=False, default=())
        exclude = r.parse("exclude_naics", _naics, required=False, default=())
        keywords = split_list(r.text("apollo_keywords"), ";")
        landing = r.parse("landing_page_url", _page_url, required=False, default="")
        priority = r.parse("priority", parse_int, required=False, default=Industry.priority)
        if priority is not None and priority < 1:
            r.fail("priority", "must be 1 or more")
        page = IndustryPage(**{f: r.text(f"page_{f}") for f in PAGE_FIELDS})
        if r.ok:
            out.append((
                Industry(label, group, active, naics, exclude, keywords, landing, r.text("proof_point"), priority, page),
                r.number,
            ))
    return out


def _usps(text: str) -> str:
    code = text.strip().upper()
    if code not in US_STATES:
        raise ValueError(f"{text!r} is not a USPS state code like NY")
    return code


def _states(rows: list[_Row]) -> list[tuple[State, int]]:
    out: list[tuple[State, int]] = []
    seen: dict[str, int] = {}
    for r in rows:
        code = r.parse("state", _usps)
        _unique(r, "state", code, seen, code or "")
        active = r.parse("active", parse_bool)
        if active and code in NEVER_ACTIVE_STATES:
            r.fail("active", f"{code} is never contacted (SPEC 1.3)")
        if r.ok:
            out.append((State(code, active, r.text("note")), r.number))
    return out


def _role_rank(text: str) -> int:
    n = parse_int(text)
    if not 1 <= n <= MAX_ROLE_RANK:
        raise ValueError(f"must be a rank from 1 (contacted first) to {MAX_ROLE_RANK}, or blank (not contacted)")
    return n


def _legacy_role_order(r: _Row) -> dict[str, int]:
    """SPEC 5's first_choice_for_size and fallback_order as one {size range: rank}."""
    first = r.parse(
        "first_choice_for_size", lambda t: tuple(parse_size_range(x) for x in split_list(t, ";,")),
        required=False, default=(),
    )
    order = dict.fromkeys(first or (), 1)
    for rng, rank in (r.parse("fallback_order", parse_fallback_order, required=False, default={}) or {}).items():
        if rng in order:
            r.fail("fallback_order", f"{rng} is already this role's first choice")
        order[rng] = rank
    return order


def _role_order(r: _Row) -> dict[str, int]:
    """{size range: rank} from the order_10_49 and order_50_249 columns; a blank one is not contacted."""
    order: dict[str, int] = {}
    for col, rng in ROLE_ORDER_COLUMNS.items():
        rank = r.parse(col, _role_rank, required=False)
        if rank is not None:
            order[rng] = rank
    return order


def _roles(rows: list[_Row]) -> list[tuple[Role, int]]:
    """Roles (Harry, 1 Oct 2026): one row per group of titles, its copy role, and its rank at each size.

    Two rows may share a rank (a founder and a law firm's partner both come first at 10 to
    49 staff): seniority then decides (clean/people.rank_person). A title may not be on two
    rows, however it is spelled ("Head of HR", "Head of Human Resources"). A row that is
    contacted at some size needs a copy role with a line on the Copy tab. Every size needs at
    least one row that is contacted there. The SPEC 5 layout (first_choice_for_size,
    fallback_order) is still read, as the same order.
    """
    out: list[tuple[Role, int]] = []
    names: dict[str, int] = {}
    titles_seen: dict[str, tuple[str, int]] = {}
    legacy = bool(rows) and is_legacy_roles(set().union(*(set(r.raw) for r in rows)))
    copy_roles = {k.casefold() for k in ROLE_LINE_COLUMNS}
    for r in rows:
        role = r.parse("role", str)
        _unique(r, "role", role.casefold() if role else None, names, f"role {role!r}")
        titles = r.parse("titles", lambda t: split_list(t, ";") or _fail("lists no titles"))
        for t in titles or ():
            other = titles_seen.get(title_key(t))
            if other and other[0] != role:
                r.fail("titles", f"{t!r} is also a title of {other[0]} (row {other[1]})")
            else:
                titles_seen[title_key(t)] = (role, r.number)
        if legacy:
            order, copy_role, groups = _legacy_role_order(r), "", ()
        else:
            order = _role_order(r)
            copy_role = r.parse("copy_role", one_of(ROLE_LINE_COLUMNS), required=False, default="")
            groups = r.parse("industry_groups", lambda t: split_list(t, ";,"), required=False, default=())
        if order and role and not r.text("copy_role") and role.casefold() not in copy_roles:
            r.fail("copy_role", f"a row that is contacted gets the copy of one of {', '.join(ROLE_LINE_COLUMNS)}; "
                                "set copy_role to one of them")
        if r.ok:
            out.append((Role(role, titles, order, copy_role, groups), r.number))
    if rows and not legacy:
        contacted = {rng for role, _ in out for rng in role.order}
        for col, rng in ROLE_ORDER_COLUMNS.items():
            if rng not in contacted and all(r.ok for r in rows):
                rows[0].errors.append(RowError("Roles", HEADER_ROW, col, f"no row is contacted at {rng} staff"))
    return out


def _check_copy_text(r: _Row, col: str) -> None:
    text = r.text(col)
    for name in _VARIABLE.findall(text):
        if name not in COPY_VARIABLES:
            r.fail(col, f"unknown variable {{{{{name}}}}}{_hint(name, COPY_VARIABLES)}")
    for name in _SINGLE_BRACE.findall(text):
        r.fail(col, f"variables take double braces: write {{{{{name.strip()}}}}}, not {{{name}}}")


def _qa(text: str) -> str:
    parts = text.split()
    if parts[0].lower() not in QA_VERDICTS or len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{8}", parts[1].lower()):
        raise ValueError('is written by `us-outbound copy qa`: "pass" or "fail" and the copy\'s check code, like "pass 1a2b3c4d"')
    return f"{parts[0].lower()} {parts[1].lower()}"


def _copy(rows: list[_Row]) -> list[tuple[CopyRow, int]]:
    out: list[tuple[CopyRow, int]] = []
    seen: dict[str, int] = {}
    for r in rows:
        version = r.parse("copy_version", str)
        _unique(r, "copy_version", version.casefold() if version else None, seen, f"copy_version {version!r}")
        industry = r.parse("industry", str)
        role = r.text("role")
        status = r.parse("status", one_of(COPY_STATUSES))
        approved_by = r.text("approved_by")
        if status == "approved" and not approved_by:
            r.fail("approved_by", "is required once a row is approved")
        qa = r.parse("qa", _qa, required=False, default="")
        steps = []
        for n in COPY_STEPS:
            subject = r.parse(f"s{n}_subject", str, default="")
            body = r.parse(f"s{n}_body", str, default="")
            _check_copy_text(r, f"s{n}_subject")
            _check_copy_text(r, f"s{n}_body")
            steps.append(CopyStep(subject or "", body or ""))
        lines = {name: r.text(col) for name, col in ROLE_LINE_COLUMNS.items()}
        uses_line = any("role_line" in _VARIABLE.findall(st.body) for st in steps)
        for name, col in ROLE_LINE_COLUMNS.items():
            if uses_line and not lines[name] and (not role or role.casefold() == name.casefold()):
                r.fail(col, f"is required: the emails use {{{{role_line}}}}, and this is the {name} line")
            for v in _VARIABLE.findall(lines[name]):
                if v not in ("company",):
                    r.fail(col, f"a role line may use only {{{{company}}}}, not {{{{{v}}}}}")
        if r.ok:
            out.append((CopyRow(version, industry, status, tuple(steps), role, lines, approved_by, qa,
                                r.text("qa_notes"), r.text("sources")), r.number))
    return out


def is_spill_domain(domain: str) -> bool:
    """spill.chat or any subdomain of it: none of them sends cold email (SPEC 1.2)."""
    d = domain.strip().lower().rstrip(".")
    return d == SPILL_DOMAIN or d.endswith("." + SPILL_DOMAIN)


def _slack_user(text: str) -> str:
    if not _SLACK_USER.fullmatch(text):
        raise ValueError(f"{text!r} is not a Slack user id (they look like U01ABCDEF)")
    return text


def _mailboxes(rows: list[_Row]) -> list[tuple[Mailbox, int]]:
    out: list[tuple[Mailbox, int]] = []
    seen: dict[str, int] = {}
    ids: dict[str, int] = {}
    # slack_id (D11): one Slack id per owner and one owner per Slack id, so an approval in Slack
    # names exactly one person, and only for that person's own mailboxes.
    slack_owner: dict[str, str] = {}
    owner_slack: dict[str, str] = {}
    for r in rows:
        address = r.parse("address", _email)
        _unique(r, "address", address, seen, address or "")
        own_domain = address.partition("@")[2] if address else ""
        if is_spill_domain(own_domain):
            r.fail("address", f"{SPILL_DOMAIN} never sends cold email (SPEC 1.2)")
        domain = r.text("domain").lower() or own_domain
        if own_domain and domain != own_domain:
            r.fail("domain", f"must be the address's domain, {own_domain}")
        owner = r.parse("owner_name", str)
        status = r.parse("status", one_of(MAILBOX_STATUSES))
        cap = r.parse("daily_cap", parse_int)
        if cap is not None and not 0 <= cap <= MAX_DAILY_CAP:
            r.fail("daily_cap", f"must be between 0 and {MAX_DAILY_CAP} (SPEC 13)")
        added_on = r.parse("added_on", parse_date, required=False)
        retire_after = r.parse("retire_after", parse_date, required=False)
        account_id = r.text("instantly_account_id")
        if account_id:
            _unique(r, "instantly_account_id", account_id, ids, account_id)
        slack_id = r.parse("slack_id", _slack_user, required=False, default="")
        if slack_id and owner:
            if slack_owner.setdefault(slack_id, owner) != owner:
                r.fail("slack_id", f"{slack_id} is already {slack_owner[slack_id]}'s; one person per Slack id")
            elif owner_slack.setdefault(owner, slack_id) != slack_id:
                r.fail("slack_id", f"{owner} already has Slack id {owner_slack[owner]} on another row")
        if r.ok:
            out.append((
                Mailbox(
                    address=address, domain=domain, owner_name=owner, status=status, daily_cap=cap,
                    instantly_account_id=account_id, provider=r.text("provider"), owner_role=r.text("owner_role"),
                    signature=r.text("signature"), added_on=added_on, retire_after=retire_after,
                    slack_id=slack_id or "",
                ),
                r.number,
            ))
    return out


def _override_domain(text: str) -> str:
    d = text.strip().lower()
    if d.startswith("www.") or not _DOMAIN.fullmatch(d):
        raise ValueError(f"must be a root domain like acme.com, not {text!r}")
    return d


def _overrides(rows: list[_Row]) -> list[tuple[Override, int]]:
    out: list[tuple[Override, int]] = []
    seen: dict[tuple[str, str], int] = {}
    for r in rows:
        domain = r.parse("domain", _override_domain)
        field = r.parse("field", lambda t: t if _FIELD.fullmatch(t) else _fail(f"{t!r} is not a field name like hq_state"))
        if domain and field:
            _unique(r, "field", (domain, field), seen, f"{domain} {field}")
        if r.ok:
            out.append((Override(domain, field, r.text("value"), r.text("note")), r.number))
    return out


def parse_share(text: str) -> float:
    """"60%" or "0.6" -> 0.6. A bare number above 1 is refused: write it with a % sign."""
    t = text.strip()
    try:
        value = float(t[:-1].strip()) / 100 if t.endswith("%") else float(t)
    except ValueError:
        raise ValueError(f"must be a share like 60% or 0.6, not {text!r}") from None
    if not 0 <= value <= 1:
        raise ValueError(f"must be between 0% and 100%, not {text!r} (write 60% or 0.6)")
    return value


def _focus(rows: list[_Row]) -> list[tuple[Focus, int]]:
    out: list[tuple[Focus, int]] = []
    seen: dict[str, int] = {}
    for r in rows:
        group = r.parse("industry_group", str)
        _unique(r, "industry_group", group.casefold() if group else None, seen, f"industry group {group!r}")
        share = r.parse("share", parse_share)
        if r.ok:
            out.append((Focus(group, share, r.text("note")), r.number))
    total = sum(f.share for f, _ in out)
    if out and total > 1 + 1e-9:
        last = next(r for r in reversed(rows) if r.number == out[-1][1])
        last.fail("share", f"the shares add up to {total:.0%}; together they may not pass 100%")
        return []
    return out


def _named_accounts(rows: list[_Row]) -> list[tuple[NamedAccount, int]]:
    from us_outbound.clean.domains import is_personal_domain

    out: list[tuple[NamedAccount, int]] = []
    seen: dict[str, int] = {}
    for r in rows:
        domain = r.parse("domain", _override_domain)
        if domain and is_personal_domain(domain):
            r.fail("domain", f"{domain} is a personal email domain, not a company")
        elif domain == SPILL_DOMAIN:
            r.fail("domain", "spill.chat is Spill")
        _unique(r, "domain", domain, seen, domain or "")
        if r.ok:
            out.append((NamedAccount(domain, r.text("name"), r.text("note")), r.number))
    return out


def _tests(rows: list[_Row]) -> list[tuple[Test, int]]:
    out: list[tuple[Test, int]] = []
    seen: dict[str, int] = {}
    running: int | None = None
    for r in rows:
        test_id = r.parse("test_id", str)
        _unique(r, "test_id", test_id, seen, f"test {test_id!r}")
        hypothesis = r.parse("hypothesis", str)
        a = r.parse("version_a", str)
        b = r.parse("version_b", str)
        if a and b and a == b:
            r.fail("version_b", "must differ from version_a")
        n = r.parse("accounts_per_version", parse_int)
        if n is not None and n < 1:
            r.fail("accounts_per_version", "must be 1 or more")
        status = r.parse("status", one_of(TEST_STATUSES))
        is_running = status == "running"
        start = r.parse("start_date", parse_date, required=is_running)
        read = r.parse("read_date", parse_date, required=is_running)
        rule = r.parse("decision_rule", str, required=is_running, default="")
        if start and read and read <= start:
            r.fail("read_date", "must be after start_date")
        if is_running:
            if running is not None:
                r.fail("status", f"only one test runs at a time; row {running} is already running")
            else:
                running = r.number
        if r.ok:
            out.append((Test(test_id, hypothesis, a, b, n, status, start, read, rule, r.text("result")), r.number))
    return out


_VALIDATORS: dict[str, Callable[[list[_Row]], Any]] = {
    "General": _general,
    "Signals": _signals,
    "Angles": _angles,
    "Industries": _industries,
    "States": _states,
    "Roles": _roles,
    "Copy": _copy,
    "Mailboxes": _mailboxes,
    "Overrides": _overrides,
    "Tests": _tests,
    "Focus": _focus,
    "Named accounts": _named_accounts,
}


def _validate(tab: str, rows: Iterable[Mapping[str, Any]] | None) -> tuple[Any, list[RowError]]:
    if tab not in _VALIDATORS:
        raise ValueError(f"unknown settings tab {tab!r}")
    errors: list[RowError] = []
    prepared = _prepare(tab, rows, errors)
    if prepared is None:
        return (General() if tab == "General" else []), errors
    return _VALIDATORS[tab](prepared), errors


def validate_tab(tab: str, rows: Iterable[Mapping[str, Any]] | None) -> tuple[Any, list[RowError]]:
    """One tab on its own: (General, errors) for General, else (tuple of the tab's rows, errors)."""
    value, errors = _validate(tab, rows)
    if tab == "General":
        return value, errors
    return tuple(obj for obj, _ in value), errors


# -- all tabs --------------------------------------------------------------------


def _names(rows: Iterable[Mapping[str, Any]] | None, col: str) -> dict[str, str]:
    """casefolded name -> name as written, for every row that names one (valid or not)."""
    return {n.casefold(): n for n in (_cell(r, col) for r in rows or ()) if n}


def validate_all(tabs: Mapping[str, Iterable[Mapping[str, Any]] | None]) -> tuple[Settings | None, dict[str, list[RowError]]]:
    """Every tab, then the references between them. Settings only when there are no errors at all."""
    raw = {tab: (list(tabs[tab]) if tabs.get(tab) is not None else None) for tab in TABS}
    values: dict[str, Any] = {}
    errors: dict[str, list[RowError]] = {}
    for tab in TABS:
        values[tab], errors[tab] = _validate(tab, raw[tab])

    # References are checked against every name on the referenced tab, valid row or not, so
    # one bad Angles row does not also fail the signals that name it.
    angles = _names(raw["Angles"], "angle")
    signals = []
    for s, row in values["Signals"]:
        if s.suggests_angle:
            canonical = angles.get(s.suggests_angle.casefold())
            if canonical is None:
                errors["Signals"].append(RowError(
                    "Signals", row, "suggests_angle",
                    f"{s.suggests_angle!r} is not on the Angles tab{_hint(s.suggests_angle, angles.values())}", s.signal,
                ))
                continue
            s = dataclasses.replace(s, suggests_angle=canonical)
        signals.append((s, row))
    values["Signals"] = signals

    # A Copy row names an Industries label, an industry group, or General; and a role or none.
    labels = _names(raw["Industries"], "industry")
    groups = _names(raw["Industries"], "industry_group")
    roles = _names(raw["Roles"], "role")
    copy_rows = []
    for c, row in values["Copy"]:
        target = c.industry.casefold()
        canonical = (GENERAL_COPY if target == GENERAL_COPY.casefold() else labels.get(target) or groups.get(target))
        if canonical is None:
            errors["Copy"].append(RowError(
                "Copy", row, "industry",
                f"{c.industry!r} is not an industry or industry_group on the Industries tab, nor {GENERAL_COPY!r}"
                f"{_hint(c.industry, [*labels.values(), *groups.values()])}", c.copy_version,
            ))
            continue
        role = c.role
        if role:
            role = roles.get(role.casefold())
            if role is None:
                errors["Copy"].append(RowError("Copy", row, "role", f"{c.role!r} is not on the Roles tab"
                                               f"{_hint(c.role, roles.values())}", c.copy_version))
                continue
        copy_rows.append((dataclasses.replace(c, industry=canonical, role=role), row))
    values["Copy"] = copy_rows

    # A Roles row's industry_groups are groups on the Industries tab ("Partner" at law firms).
    role_rows = []
    for role, row in values["Roles"]:
        unknown = [g for g in role.industry_groups if g.casefold() not in groups]
        for g in unknown:
            errors["Roles"].append(RowError("Roles", row, "industry_groups", f"{g!r} is not an industry_group on the "
                                            f"Industries tab{_hint(g, groups.values())}", role.role))
        if not unknown:
            role = dataclasses.replace(role, industry_groups=tuple(groups[g.casefold()] for g in role.industry_groups))
            role_rows.append((role, row))
    values["Roles"] = role_rows

    # A test's versions may be written after it is planned; a running test needs both approved.
    versions = {_cell(r, "copy_version").casefold(): _cell(r, "status").lower() for r in raw["Copy"] or ()}
    for t, row in values["Tests"]:
        if t.status != "running":
            continue
        for col, version in (("version_a", t.version_a), ("version_b", t.version_b)):
            status = versions.get(version.casefold())
            if status is None:
                errors["Tests"].append(RowError("Tests", row, col, f"{version!r} is not a copy_version on the Copy tab", t.test_id))
            elif status != "approved":
                errors["Tests"].append(RowError("Tests", row, col, f"a running test needs {version} approved", t.test_id))

    # A Focus row names an industry group on the Industries tab that has an active industry.
    groups = _names(raw["Industries"], "industry_group")
    active_groups = {i.industry_group.casefold() for i, _ in values["Industries"] if i.active}
    focus_rows = []
    for f, row in values["Focus"]:
        canonical = groups.get(f.industry_group.casefold())
        if canonical is None:
            errors["Focus"].append(RowError("Focus", row, "industry_group",
                                            f"{f.industry_group!r} is not an industry_group on the Industries tab"
                                            f"{_hint(f.industry_group, groups.values())}", f.industry_group))
            continue
        if canonical.casefold() not in active_groups:
            errors["Focus"].append(RowError("Focus", row, "industry_group",
                                            f"{canonical!r} has no active industry on the Industries tab", f.industry_group))
            continue
        focus_rows.append((dataclasses.replace(f, industry_group=canonical), row))
    values["Focus"] = focus_rows

    # General apollo_enrich_groups names industry groups on the Industries tab (sources/apollo_enrich.py).
    general = values["General"]
    enrich_row = next((i + FIRST_DATA_ROW for i, r in enumerate(raw["General"] or ())
                       if _cell(r, "key") == "apollo_enrich_groups"), HEADER_ROW)
    unknown = [g for g in general.apollo_enrich_groups if g.casefold() not in groups]
    for g in unknown:
        errors["General"].append(RowError("General", enrich_row, "value", f"apollo_enrich_groups: {g!r} is not an "
                                          f"industry_group on the Industries tab{_hint(g, groups.values())}",
                                          "apollo_enrich_groups"))
    if not unknown and general.apollo_enrich_groups:
        values["General"] = dataclasses.replace(general, apollo_enrich_groups=tuple(
            dict.fromkeys(groups[g.casefold()] for g in general.apollo_enrich_groups)))

    if any(errors.values()):
        return None, errors
    settings = Settings(
        general=values["General"],
        **{
            field: tuple(obj for obj, _ in values[tab])
            for field, tab in (
                ("signals", "Signals"), ("angles", "Angles"), ("industries", "Industries"), ("states", "States"),
                ("roles", "Roles"), ("copy", "Copy"), ("mailboxes", "Mailboxes"), ("overrides", "Overrides"),
                ("tests", "Tests"), ("focus", "Focus"), ("named_accounts", "Named accounts"),
            )
        },
    )
    return settings, errors
