"""Today's enrollment number, the order accounts are taken in, test versions and senders.

Today's number is the smallest of (weekly budgets and targets, Harry 30 Sep 2026):
  * the weekly target: what is left of weekly_enrol_cap ÷ the send days left in the week
    (budget.weekly_target_today; weeks run Monday to Sunday, UK time);
  * sending capacity: each sender's free slots today, from the follow-ups already due
    (enrol/capacity.py), summed over senders;
  * ready accounts: verified accounts with a sendable contact.
SPEC 9 also listed the Clay and Apollo budgets here. The enrol job spends neither, so they
are applied where credits are spent (source_universe, verify_in_clay, pick_contacts); when
they bind, it shows up here as too few ready accounts, and ops/limits.py says so.
Of that number, control_share comes from the Control tier; the rest from Priority, then
Standard, ordered by score, then size band (20 to 99 first), then industry priority. A
shortfall in either is filled from the other, so capacity is never left idle while verified
accounts wait.

SPEC 9 "Test assignment": the version is hash(account_id + test_id) % 2, so an account's
version never changes and both contacts at an account get the same one.

SPEC 9 "Sender continuity": a sender is assigned at first enrollment and kept for life.
New accounts go to the sender with the most free capacity that day. A paused or full
sender's accounts wait rather than move to someone else.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.settings.model import Settings

MIN_N_FOR_CONTROL = 4  # from 4 a day, at least one Control account when there are any
QUEUE_TIERS = ("Priority", "Standard", "Control")
CONTROL = "Control"
TIER_RANK = {"Priority": 0, "Standard": 1, "Control": 2}
# SPEC 9: size band 20 to 99 first; then 100-249, then 10-19 (as v_queue orders them).
SIZE_BAND_RANK = {"20-49": 0, "50-99": 0, "100-249": 1, "10-19": 2}
DEFAULT_INDUSTRY_PRIORITY = 99
# The terms of today's number, in the order ties are reported.
TERMS = ("weekly_target", "sending_capacity", "ready_accounts")


# -- the daily number ---------------------------------------------------------------------


def daily_number(*, weekly_target: int, sending_capacity: int, ready_accounts: int) -> tuple[int, dict[str, Any]]:
    """(today's number, its terms and the one that binds)."""
    terms = {"weekly_target": weekly_target, "sending_capacity": sending_capacity, "ready_accounts": ready_accounts}
    n = max(0, min(terms.values()))
    binding = min(TERMS, key=lambda k: (terms[k], TERMS.index(k)))
    return n, {**terms, "binding": binding}


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


def control_count(n: int, settings: Settings, control_available: int) -> int:
    """control_share of n, rounded half up; at least 1 once n >= 4; never more than there are."""
    if n <= 0 or control_available <= 0:
        return 0
    want = int((Decimal(str(settings.general.control_share)) * n).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if n >= MIN_N_FOR_CONTROL:
        want = max(want, 1)
    return min(want, control_available, n)


# -- test versions (SPEC 9, 12) ----------------------------------------------------------------


def test_version(account_id: str, test_id: str) -> str:
    """"a" or "b", from sha256(account_id + test_id) % 2; 0 is version_a."""
    return "a" if int(hashlib.sha256((account_id + test_id).encode()).hexdigest(), 16) % 2 == 0 else "b"


test_version.__test__ = False  # not a pytest test, despite the name


# -- senders (SPEC 9 "Sender continuity") ----------------------------------------------------


def campaign_name(owner: str) -> str:
    """The owner's Instantly campaign: "US Outbound – {owner}"."""
    return US_CAMPAIGN_PREFIX + owner


def assign_sender(
    account: Mapping[str, Any], settings: Settings, free: Mapping[str, int], pace: Mapping[str, int] | None = None
) -> str | None:
    """The account's sender; None means wait.

    free is each owner's new leads left today (enrol/capacity.py, less those given out this
    run), pace each owner's steady daily rate. An account with a sender keeps it; if that
    sender has no Active mailbox (paused, or gone) or no free slot today, the account waits
    (SPEC 9 "Pause and retire"). A new account goes to the owner with the largest share of
    today's pace left, so owners fill in proportion (Harry's two mailboxes take twice
    Hannah's share); ties go to the most free, then by name. It waits when every owner is full.
    """
    existing = str(account.get("sender") or "").strip()
    if existing:
        if not settings.mailboxes_for(existing, "Active") or free.get(existing, 0) <= 0:
            return None
        return existing
    open_ = {o: k for o, k in free.items() if k > 0 and settings.mailboxes_for(o, "Active")}
    if not open_:
        return None
    pace = pace or {}

    def share(owner: str) -> float:
        p = pace.get(owner) or 0
        return open_[owner] / p if p > 0 else float(open_[owner])

    return min(open_, key=lambda owner: (-share(owner), -open_[owner], owner))
