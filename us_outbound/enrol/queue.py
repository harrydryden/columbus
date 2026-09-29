"""Today's enrollment number, the order accounts are taken in, test versions and senders.

SPEC 9 "Daily enrolment number": the smallest of
  * daily_enrol_cap;
  * the sum of the Active mailboxes' daily caps ÷ 4 (four steps per lead);
  * the remaining Clay budget ÷ working days left in the month ÷ credits per account;
  * the same calculation for Apollo;
  * the size of the verified queue.
Of that number, control_share comes from the Control tier; the rest from Priority, then
Standard, ordered by score, then size band (20 to 99 first), then industry priority. A
shortfall in either is filled from the other, so capacity is never left idle while verified
accounts wait.

SPEC 9 "Test assignment": the version is hash(account_id + test_id) % 2, so an account's
version never changes and both contacts at an account get the same one.

SPEC 9 "Sender continuity": a sender is assigned at first enrollment and kept for life.
New accounts go to the sender with the most free capacity that day. A paused sender's
accounts wait rather than move to someone else.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.settings.model import Settings

STEPS_PER_LEAD = 4
MIN_N_FOR_CONTROL = 4  # from 4 a day, at least one Control account when there are any
QUEUE_TIERS = ("Priority", "Standard", "Control")
CONTROL = "Control"
TIER_RANK = {"Priority": 0, "Standard": 1, "Control": 2}
# SPEC 9: size band 20 to 99 first; then 100-249, then 10-19 (as v_queue orders them).
SIZE_BAND_RANK = {"20-49": 0, "50-99": 0, "100-249": 1, "10-19": 2}
DEFAULT_INDUSTRY_PRIORITY = 99
_EPS = 1e-9


# -- the daily number ---------------------------------------------------------------------


def _budget_term(remaining: float, days: int, per_account: float) -> int | None:
    """floor(remaining ÷ days ÷ per_account); None when credits per account is unknown (0)."""
    if not per_account or per_account <= 0:
        return None
    if days <= 0:
        return 0
    return max(0, math.floor(remaining / days / per_account + _EPS))


def daily_number(
    settings: Settings,
    *,
    active_mailbox_caps: Iterable[int],
    clay_remaining: float,
    clay_per_account: float,
    apollo_remaining: float,
    apollo_per_account: float,
    working_days_left: int,
    verified_queue_size: int,
) -> tuple[int, dict[str, Any]]:
    """(today's number, the terms behind it). A budget term with credits per account 0 is left out, and said so."""
    terms: dict[str, int | None] = {
        "daily_enrol_cap": settings.general.daily_enrol_cap,
        "mailbox_capacity": sum(int(c or 0) for c in active_mailbox_caps) // STEPS_PER_LEAD,
        "clay_budget": _budget_term(clay_remaining, working_days_left, clay_per_account),
        "apollo_budget": _budget_term(apollo_remaining, working_days_left, apollo_per_account),
        "verified_queue": verified_queue_size,
    }
    known = {k: v for k, v in terms.items() if v is not None}
    n = max(0, min(known.values()))
    binding = min(known, key=lambda k: known[k])
    reasons: dict[str, Any] = {
        **terms,
        "binding": binding,
        "working_days_left": working_days_left,
        "clay_remaining": clay_remaining,
        "apollo_remaining": apollo_remaining,
        "left_out": [f"{k}: credits per account unknown" for k, v in terms.items() if v is None],
    }
    return n, reasons


def is_blackout(day: date, settings: Settings) -> bool:
    return any(day in r for r in settings.general.blackout_dates)


def working_days_left(today: date, settings: Settings) -> int:
    """Send days (Mon–Fri by send_window) from today to the month's end, inclusive, less blackout dates."""
    days = set(settings.general.send_window.days)
    first_next = date(today.year + today.month // 12, today.month % 12 + 1, 1)
    n, d = 0, today
    while d < first_next:
        if d.weekday() in days and not is_blackout(d, settings):
            n += 1
        d += timedelta(days=1)
    return n


def in_send_window(now_et: datetime, settings: Settings) -> bool:
    """True inside the send window (Mon–Fri 09:00–16:00 America/New_York by default), end exclusive."""
    w = settings.general.send_window
    local = now_et.astimezone(ZoneInfo(w.tz)) if now_et.tzinfo else now_et
    return local.weekday() in w.days and w.start <= local.time() < w.end


# -- order and selection ----------------------------------------------------------------------


def _number(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def industry_priority(row: Mapping[str, Any], settings: Settings) -> int:
    ind = settings.industry(str(row.get("industry") or ""))
    return ind.priority if ind else DEFAULT_INDUSTRY_PRIORITY


def order_key(row: Mapping[str, Any], settings: Settings) -> tuple:
    """Tier, score (high first), size band (20 to 99 first), industry priority; then first_seen, id."""
    return (
        TIER_RANK.get(str(row.get("tier")), 9),
        -_number(row.get("score")),
        SIZE_BAND_RANK.get(str(row.get("size_band") or ""), 3),
        industry_priority(row, settings),
        str(row.get("first_seen") or ""),
        str(row.get("account_id") or ""),
    )


def ordered(rows: Iterable[Mapping[str, Any]], settings: Settings) -> list[Mapping[str, Any]]:
    return sorted(rows, key=lambda r: order_key(r, settings))


def control_count(n: int, settings: Settings, control_available: int) -> int:
    """control_share of n, rounded half up; at least 1 once n >= 4; never more than there are."""
    if n <= 0 or control_available <= 0:
        return 0
    want = int((Decimal(str(settings.general.control_share)) * n).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if n >= MIN_N_FOR_CONTROL:
        want = max(want, 1)
    return min(want, control_available, n)


def select(queue_rows: Sequence[Mapping[str, Any]], n: int, settings: Settings) -> list[Mapping[str, Any]]:
    """Today's n accounts: control_share from Control, the rest from Priority then Standard.

    A shortfall in either is filled from the other, so a day never leaves capacity unused
    while verified accounts wait (the verified queue is itself one of the SPEC 9 terms).
    """
    control = ordered((r for r in queue_rows if r.get("tier") == CONTROL), settings)
    main = ordered((r for r in queue_rows if r.get("tier") in QUEUE_TIERS and r.get("tier") != CONTROL), settings)
    picked = main[: max(0, n - control_count(n, settings, len(control)))]
    return picked + control[: max(0, n - len(picked))]


# -- test versions (SPEC 9, 12) ----------------------------------------------------------------


def test_version(account_id: str, test_id: str) -> str:
    """"a" or "b", from sha256(account_id + test_id) % 2; 0 is version_a."""
    return "a" if int(hashlib.sha256((account_id + test_id).encode()).hexdigest(), 16) % 2 == 0 else "b"


test_version.__test__ = False  # not a pytest test, despite the name


# -- senders (SPEC 9 "Sender continuity") ----------------------------------------------------


def campaign_name(owner: str) -> str:
    """The owner's Instantly campaign: "US Outbound – {owner}"."""
    return US_CAMPAIGN_PREFIX + owner


def free_capacity(settings: Settings, today_load: Mapping[str, int]) -> dict[str, int]:
    """Per owner with an Active mailbox: their Active daily caps ÷ 4, less the accounts given them today."""
    caps: dict[str, int] = {}
    for m in settings.mailboxes:
        if m.status == "Active":
            caps[m.owner_name] = caps.get(m.owner_name, 0) + int(m.daily_cap or 0)
    return {owner: cap // STEPS_PER_LEAD - int(today_load.get(owner, 0)) for owner, cap in caps.items()}


def assign_sender(account: Mapping[str, Any], settings: Settings, today_load: Mapping[str, int]) -> str | None:
    """The account's sender; None means wait.

    An account with a sender keeps it; if that sender has no Active mailbox (paused, or
    gone), the account waits (SPEC 9 "Pause and retire"). A new account goes to the owner
    with the most free capacity today, ties by name. The day's total is bounded by
    daily_number, so an owner at zero free capacity can still be chosen when every owner is.
    """
    existing = str(account.get("sender") or "").strip()
    if existing:
        return existing if settings.mailboxes_for(existing, "Active") else None
    free = free_capacity(settings, today_load)
    if not free:
        return None
    return min(free, key=lambda owner: (-free[owner], owner))
