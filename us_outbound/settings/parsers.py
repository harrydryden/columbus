"""How one cell of the settings sheet is read: each parser takes the cell's text and returns its value, or raises
ValueError with what a person reads on the sheet (9 Oct 2026, the refactoring scan's phase 5, stage 3).

settings/spec.py names a column's parser, so a column is declared once; settings/validate.py checks what spans
columns and rows. They lived in validate.py, which spec.py cannot import (validate.py imports spec.py).
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any

from us_outbound.settings.model import US_STATES

SPILL_PAGE = re.compile(r"https://(?:www\.)?spill\.chat/\S*")
_EMAIL = re.compile(r"[a-z0-9._%+'-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
_SLACK_USER = re.compile(r"[UW][A-Z0-9_]+")
_NAICS = re.compile(r"\d{2,6}")
_FIELD = re.compile(r"[a-z_][a-z0-9_]*")


def hint(value: str, choices: Iterable[str]) -> str:
    close = difflib.get_close_matches(value, list(choices), n=1, cutoff=0.6)
    return f" (did you mean {close[0]}?)" if close else ""


def fail(message: str) -> Any:
    raise ValueError(message)


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


def parse_looks(text: str) -> tuple[int | date, ...]:
    """"200; 2026-11-16" -> (200, date(2026, 11, 16)): a test's pre-registered looks (Harry, 6 Oct 2026), each a
    whole number of accounts per arm with closed reply windows, or a date. Separated by semicolons or commas."""
    out: list[int | date] = []
    for part in split_list(text, ";,"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            look: int | date = parse_date(part)
        elif re.fullmatch(r"\d+", part) and int(part) >= 1:
            look = int(part)
        else:
            raise ValueError(f"each look is a number of accounts per version, like 200, or a date written "
                             f"YYYY-MM-DD, not {part!r}")
        if look in out:
            raise ValueError(f"{part} is listed twice")
        out.append(look)
    return tuple(out)


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


def split_list(text: str, seps: str = ";") -> tuple[str, ...]:
    parts = re.split("[" + re.escape(seps) + "]", text)
    return tuple(p for p in (" ".join(x.split()) for x in parts) if p)


def items(text: str) -> tuple[str, ...]:
    """A list written with semicolons: "payroll; hr software"."""
    return split_list(text, ";")


def one_of(allowed: Iterable[str]) -> Callable[[str], str]:
    """A parser accepting any of allowed, case-insensitively, returning its canonical spelling."""
    allowed = tuple(allowed)

    def parse(text: str) -> str:
        for a in allowed:
            if text.strip().casefold() == a.casefold():
                return a
        return fail(f"must be one of {', '.join(allowed)}, not {text!r}{hint(text.strip(), allowed)}")

    return parse


def https(text: str) -> str:
    if not re.fullmatch(r"https://\S+\.\S+", text):
        raise ValueError(f"must be a full https:// link, not {text!r}")
    return text


def spill_page(text: str) -> str:
    """An industry page: emails link it, and emails link only to spill.chat."""
    https(text)
    if not SPILL_PAGE.fullmatch(text):
        raise ValueError(f"must be a page on https://www.spill.chat (emails link only there), not {text!r}")
    return text


def email(text: str) -> str:
    t = text.strip().lower()
    if not _EMAIL.fullmatch(t):
        raise ValueError(f"{text!r} is not an email address")
    return t


def naics(text: str) -> tuple[str, ...]:
    codes = split_list(text, ";,")
    bad = [c for c in codes if not _NAICS.fullmatch(c)]
    if bad:
        raise ValueError(f"NAICS codes are 2 to 6 digits: {', '.join(bad)}")
    return codes


def usps(text: str) -> str:
    code = text.strip().upper()
    if code not in US_STATES:
        raise ValueError(f"{text!r} is not a USPS state code like NY")
    return code


def slack_user(text: str) -> str:
    if not _SLACK_USER.fullmatch(text):
        raise ValueError(f"{text!r} is not a Slack user id (they look like U01ABCDEF)")
    return text


def root_domain(text: str) -> str:
    d = text.strip().lower()
    if d.startswith("www.") or not _DOMAIN.fullmatch(d):
        raise ValueError(f"must be a root domain like acme.com, not {text!r}")
    return d


def field_name(text: str) -> str:
    if not _FIELD.fullmatch(text):
        raise ValueError(f"{text!r} is not a field name like hq_state")
    return text
