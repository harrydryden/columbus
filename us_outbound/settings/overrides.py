"""The Overrides tab laid over an account, in one place (9 Oct 2026, the refactoring scan's phase 3).

Five overlays used to do this, each a little differently: verify's parsed every value, the universe's also gave an
overridden label its own group and an overridden employee count its band, and render's and the openers' took the
text as it was. An `industry = Legal Teams` row then reached scoring with the old group and never reached the
account's columns at all once the account was verified, so enrol kept the old label's copy (verify now re-decides
such an account: labels.override_moves). effective() is the one overlay; the columns are what readers should read.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from us_outbound.clean.people import size_band, state_code
from us_outbound.settings.model import Settings


# Columns that are text whatever they look like: a company named "1800", a NAICS code kept as written.
TEXT_FIELDS = frozenset({"clean_name", "legal_name", "hq_city", "hq_state", "industry", "industry_group", "naics"})


def parse_value(text: Any) -> Any:
    """An Overrides-tab value as a fact value: true/false/yes/no, a number, or the text."""
    t = str(text).strip()
    low = t.lower()
    if low in {"true", "yes"}:
        return True
    if low in {"false", "no"}:
        return False
    if t.isdigit() and not (t.startswith("0") and len(t) > 1):
        return int(t)
    try:
        return float(t)
    except ValueError:
        return t


def effective(account: Mapping[str, Any], settings: Settings, *, domain: str | None = None,
              fields: Collection[str] | None = None) -> dict[str, Any]:
    """The account with its domain's Overrides rows over it (SPEC 5): names, places, labels and NAICS as text and any
    other value parsed as a fact's is, an overridden label with its own group (unless a row gives the group), an
    overridden count with its size band, and a state as its USPS code. domain: when the row has none of its own;
    fields: only these Overrides rows."""
    key = str(domain if domain is not None else account.get("domain") or "").strip().lower()
    ov = {f: v for f, v in settings.overrides_for(key).items() if fields is None or f in fields} if key else {}
    out = {**account, **{f: str(v).strip() if f in TEXT_FIELDS else parse_value(v) for f, v in ov.items()}}
    if "hq_state" in ov:
        out["hq_state"] = state_code(out["hq_state"]) or out["hq_state"].upper()
    if "industry" in ov and "industry_group" not in ov:
        ind = settings.industry(out["industry"])
        out["industry_group"] = ind.industry_group if ind else account.get("industry_group")
    if "employees" in ov and "size_band" not in ov:
        out["size_band"] = size_band(out["employees"])
    return out
