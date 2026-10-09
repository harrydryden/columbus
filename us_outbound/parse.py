"""Numbers and yes/no values from rows, sheets and API payloads, read one way everywhere (9 Oct 2026).

True and False are never numbers here, and anything that is not a finite number gives None.
"1,200" reads as 1200 only when the caller says so (commas=True).
"""

from __future__ import annotations

import math
from typing import Any


def integer(v: Any, commas: bool = False) -> int | None:
    """A whole number from an int, a float, a Decimal or text ("12", "12.0"); a fraction is dropped."""
    if isinstance(v, str):
        text = _text(v, commas)
        try:
            return int(text)
        except ValueError:
            v = number(text)
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


def number(v: Any, commas: bool = False) -> float | None:
    """A finite float from an int, a float, a Decimal or text ("12", "12.5")."""
    if isinstance(v, str):
        v = _text(v, commas)
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def truthy(v: Any) -> bool:
    """A sheet or HubSpot yes: True, a non-zero number, or "true", "yes" or "1" in any case."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1"}
    return False


def _text(v: str, commas: bool) -> str:
    return (v.replace(",", "") if commas else v).strip()
