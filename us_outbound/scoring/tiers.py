"""Tiers (SPEC 9 "Scoring" step 2) and the hard exclusions the rescore applies.

Tier: the first rule that applies.
  1. Excluded: a hard exclusion (below) or an Exclude signal applies.
  2. Held: a Hold signal applies.
  3. Queue: everything else, in the queue, taken by score (enrol/queue.order_key).
Harry's D7 (19 Oct 2026; W2-2, 20 Oct 2026): the Priority, Standard and Control tiers and their thresholds
(priority_threshold, standard_threshold, auto_thresholds and scoring/balance.py, which re-cut them daily) are gone.
They were score ranges, and enrol took accounts by tier then score, so they changed no decision; the Control tier
was the comparison group, and the random explore arm drawn at pick_contacts (General explore_share) replaced it.
Queue is an explicit value: a NULL tier is not scored yet, so not in the queue. Rows a rescore has not reached since
carry a retired name (LEGACY_QUEUE_TIERS), which reads as Queue until the next rescore rewrites it; a contact keeps
the tier it was enrolled under as contacts.tier_at_enrol.

The hard exclusions are the ICP gate's SCORE checks (icp.py): a partner (NAICS or keywords), a HubSpot exclusion or an
approver's decision kept as a fact, the HQ state (never CA or WA, else active on the States tab, unless a website
visitor), and founded less than two years ago. `facts` here is the latest value of each field across all sources,
whatever its age (score.latest_facts), with the account's Overrides applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from us_outbound import icp
from us_outbound.settings.model import Settings

QUEUE, HELD, EXCLUDED = "Queue", "Held", "Excluded"
# The tiers before W2-2: in the queue until the next rescore writes Queue over them (they are never written again).
LEGACY_QUEUE_TIERS = ("Priority", "Standard", "Control")
QUEUE_TIERS = (QUEUE, *LEGACY_QUEUE_TIERS)  # what readers take as in the queue
OUT_OF_QUEUE_TIERS = frozenset({EXCLUDED, HELD})


def as_number(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def hard_exclusion(
    account: Mapping[str, Any], facts: Mapping[str, Any], settings: Settings, today: date | None = None
) -> str | None:
    """The reason an account is excluded whatever its signals, in plain English; None if it is not (icp.SCORE)."""
    why = icp.gate(account, facts, settings, today=today, checks=icp.SCORE)
    return why.text if why else None
