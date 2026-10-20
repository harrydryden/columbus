"""Today's enrollment number, the order accounts are taken in, test versions and senders.

Today's number is the smallest of (Harry, 30 Sep 2026):
  * the weekly target: what is left of weekly_enrol_cap ÷ the send days left in the week
    (budget.weekly_target_today; weeks run Monday to Sunday, UK time);
  * sending capacity: each sender's free slots today, from the follow-ups already due
    (enrol/capacity.py), summed over senders;
  * ready accounts: verified accounts with a sendable contact.
SPEC 9 also listed the credit budgets here. The enrol job spends none, so they are applied where credits are spent
(source_universe, pick_contacts); when they bind, it shows up here as too few ready accounts, and limits.py says so.
Of that number, explore_share (0.15, Harry's D7, 19 Oct 2026) comes from the random explore arm: the accounts
pick_contacts drew at random from the eligible queue, within the Focus shares (strata), and stamped accounts.arm =
random. The rest are taken in order: score, size band (20 to 99 first), then industry priority (order_key). A
shortfall in either is filled from the other, so capacity is never left idle while verified accounts wait. The
tiers that came first in the order (Priority, Standard, Control) and the Control tier's share of the number went
with W2-2 (20 Oct 2026): they were score ranges, so the order by score is the same, and the random arm is the
comparison the Control tier was meant to be. explore_share 0 on the General tab: no random arm, all in order.

SPEC 9 "Test assignment": the version is hash(account_id + test_id) % 2, so an account's
version never changes and both contacts at an account get the same one.

SPEC 9 "Sender continuity": a sender is assigned at first enrollment and kept for life.
New accounts go to the sender with the most free capacity that day. A paused or full
sender's accounts wait rather than move to someone else.
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from us_outbound import parse
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.enrol import focus
from us_outbound.settings.model import Settings

MIN_N_FOR_EXPLORE = 4  # from 4 a day, at least one random-arm account while the share is above 0 and there are any
# accounts.arm and contacts.arm (the W1-C columns): drawn at random from the eligible queue, or taken in score order.
# NULL (picked or enrolled before the arm existed) reads as ranked.
RANDOM, RANKED = "random", "ranked"
# The A/A label's key (RT §4.3): fresh, and hashed with a prefix no other split uses (test_version, the opener holdout
# and the subject arm each hash the account id with their own), so the sham arms cut across every real split.
SHAM_PREFIX, SHAM_KEY = "sham:", "2026-10-19"
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


def industry_priority(row: Mapping[str, Any], settings: Settings) -> int:
    ind = settings.industry(str(row.get("industry") or ""))
    return ind.priority if ind else DEFAULT_INDUSTRY_PRIORITY


def order_key(row: Mapping[str, Any], settings: Settings) -> tuple:
    """Score (high first), size band (20 to 99 first), industry priority; then first_seen, id. (No tier: W2-2.)"""
    return (
        -(parse.number(row.get("score")) or 0.0),
        SIZE_BAND_RANK.get(str(row.get("size_band") or ""), 3),
        industry_priority(row, settings),
        str(row.get("first_seen") or ""),
        str(row.get("account_id") or ""),
    )


def explore_count(n: int, settings: Settings, available: int) -> int:
    """explore_share of n, rounded half up; at least 1 once n >= 4 while the share is above 0; never more than are
    available, nor more than n; 0 while the share is 0 (no random arm).

    The Control tier's count (control_count, on control_share), on the new key (RS P0-2)."""
    share = Decimal(str(settings.general.explore_share))
    if n <= 0 or available <= 0 or share <= 0:
        return 0
    want = int((share * n).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if n >= MIN_N_FOR_EXPLORE:
        want = max(want, 1)
    return min(want, available, n)


def lanes(rows: Sequence[Any], settings: Settings,
          account: Callable[[Any], Mapping[str, Any]] = lambda r: r) -> tuple[list[Any], list[Any]]:
    """(the random lane, the ranked lane), each in order_key order: the accounts pick_contacts drew at random
    (accounts.arm = random) while explore_share is above 0, and every other. With the share at 0 there is no random
    lane: an account drawn while it was above 0 is ranked. account: the account row of each item (enrol walks its
    candidates, the hand-check the account rows)."""
    exploring = settings.general.explore_share > 0
    ordered = sorted(rows, key=lambda r: order_key(account(r), settings))
    drawn = [r for r in ordered if exploring and account(r).get("arm") == RANDOM]
    ids = {id(r) for r in drawn}
    return drawn, [r for r in ordered if id(r) not in ids]


def day_order(ready: Sequence[Mapping[str, Any]], settings: Settings, n: int,
              quota: focus.Quota | None = None) -> tuple[list[dict], list[dict]]:
    """(the accounts a send day of n takes, the rest in order_key order), as enrol.run walks its lanes when every
    account it reaches can be prepared: explore_count(n) from the random lane first, whatever the Focus quota; then the
    ranked lane, an account whose Focus share is full today held; then the random lane's rest under the quota; then the
    held ranked accounts and the held random ones, each without it. The hand-check draws from the rest (enrol/
    hand_check.window): random-arm accounts mostly have low scores, so in plain order_key order they sat after
    today's number while enrol took them first (the Waves 2-3 review: 3 of Monday's 5 held, and 6 of the 9 drawn
    sampled against 14 of 51 ranked). quota: today's (focus.today), copied, so the caller's is not used up."""
    rnd, ranked = lanes([dict(r) for r in ready], settings)
    q = dataclasses.replace(quota, left=dict(quota.left), done=Counter(quota.done)) if quota is not None else None
    today: list[dict] = []

    def walk(lane: Iterator[dict], target: int, bound: bool, held: list[dict] | None = None) -> None:
        taken = 0
        while taken < target:
            row = next(lane, None)
            if row is None:
                return
            if bound and q is not None and not q.allows(row):
                if held is not None:
                    held.append(row)
                continue
            today.append(row)
            taken += 1
            if q is not None:
                q.take(row)

    n = max(0, n)
    random_lane, ranked_lane = iter(rnd), iter(ranked)
    held_ranked: list[dict] = []
    held_random: list[dict] = []
    walk(random_lane, explore_count(n, settings, len(rnd)), bound=False)
    walk(ranked_lane, n - len(today), bound=True, held=held_ranked)
    walk(random_lane, n - len(today), bound=True, held=held_random)
    for held in (held_ranked, held_random):
        walk(iter(held), n - len(today), bound=False)
    taken = {str(r.get("account_id")) for r in today}
    rest = sorted((r for r in rnd + ranked if str(r.get("account_id")) not in taken),
                  key=lambda r: order_key(r, settings))
    return today, rest


def apportion(k: int, shares: Mapping[str, float]) -> dict[str, int]:
    """k places shared out in proportion to shares by largest remainder: each group the whole part of k × its share,
    then one more each to the largest fractions until k are given (ties to the larger share, then the order given).

    Focus 50/30/20 and k = 9: 4.5, 2.7 and 1.8 give 4, 2 and 1 (7 places), and the 2 left go to the two largest
    fractions, 0.8 and 0.7: 4, 3 and 2. The shares are normalised first, so they need not sum to 1."""
    total = sum(v for v in shares.values() if v > 0)
    if k <= 0 or total <= 0:
        return {g: 0 for g in shares}
    order = list(shares)
    exact = {g: k * max(0.0, shares[g]) / total for g in order}
    out = {g: int(exact[g]) for g in order}
    by_fraction = sorted(order, key=lambda g: (-(exact[g] - out[g]), -shares[g], order.index(g)))
    for g in by_fraction[: k - sum(out.values())]:
        out[g] += 1
    return out


def strata(rows: Sequence[Mapping[str, Any]], settings: Settings, seed: str, k: int) -> list[tuple[str, int, list[dict]]]:
    """The random arm's strata: (group, its places, all of the group's rows in a random order), for each group with a
    place. With Focus rows, each group's places are apportion(k, its share) (focus.shares: a group not on the Focus tab
    is in "other", which holds what the listed groups leave); with none, one stratum of all the rows with k places.

    pick_contacts tries each stratum's rows in this order until its places each have a kept contact, or the group
    runs out (the whole-wave review, 20 Oct 2026: a drawn account with nobody to contact was not replaced, so the arm
    kept about p_contact × explore_share). The order is random and when to stop depends only on the rows already
    tried, so the accounts kept are a uniform random draw from the group's accounts that have a contact. A group with
    fewer such accounts than places gives what it has, and the places it cannot fill go to the ranked rest, never to
    another group, so the random arm's mix is the Focus mix and a difference between the arms is the ranking, not the
    mix (RT §4.1). random.Random(seed): a rerun on the same day draws the same order."""
    ordered = sorted((dict(r) for r in rows), key=lambda r: order_key(r, settings))
    k = max(0, min(k, len(ordered)))
    rng = random.Random(seed)
    if not k:
        return []
    if not settings.focus:
        return [("", k, rng.sample(ordered, len(ordered)))]
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in ordered:
        by_group[focus.key_for(r, settings)].append(r)
    out = []
    for group, places in apportion(k, focus.shares(settings)).items():
        pool = by_group.get(group, [])
        if places and pool:
            out.append((group, places, rng.sample(pool, len(pool))))
    return out


def split(rows: Sequence[Mapping[str, Any]], settings: Settings, seed: str, k: int) -> tuple[list[dict], list[dict]]:
    """(the random arm, the rest in order): the first places of each stratum (strata, the same seed), and the others
    sorted by order_key: the draw pick_contacts makes when every account drawn has a contact. The random arm keeps the
    order it was drawn in."""
    picked = [r for _, places, pool in strata(rows, settings, seed, k) for r in pool[:places]]
    drawn = {str(r.get("account_id")) for r in picked}
    ordered = sorted((dict(r) for r in rows), key=lambda r: order_key(r, settings))
    return picked, [r for r in ordered if str(r.get("account_id")) not in drawn]


def sham_arm(account_id: Any) -> str:
    """"a" or "b", the parity of sha256("sham:" + account_id + SHAM_KEY): an A/A label that changes nothing about the
    contact (RT §4.3), so a report split on it shows how far the pipeline alone moves a rate."""
    h = int(hashlib.sha256((SHAM_PREFIX + str(account_id) + SHAM_KEY).encode()).hexdigest(), 16)
    return "a" if h % 2 == 0 else "b"


# -- test versions (SPEC 9, 12) ----------------------------------------------------------------


def test_version(account_id: str, test_id: str, share_a: float = 0.5) -> str:
    """"a" or "b", from sha256(account_id + test_id): % 2 when the arms are even (0 is version_a), else version_a
    for the accounts whose hash falls in its share (Test.share_a; Harry, 8 Oct 2026), in steps of 0.01%."""
    h = int(hashlib.sha256((account_id + test_id).encode()).hexdigest(), 16)
    if share_a == 0.5:
        return "a" if h % 2 == 0 else "b"
    return "a" if h % 10000 < round(share_a * 10000) else "b"


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
