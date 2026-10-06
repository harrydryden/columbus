"""Industry focus: the Focus tab's share of each week's enrolment per industry group (Harry, 30 Sep 2026).

A Focus row gives an industry group a share of weekly_enrol_cap, for example Legal Teams 40%.
The groups not listed share whatever is left of 100% ("other"). Each send day, a group may
take what is left of its week's share ÷ the send days left, rounded up, the same way the
weekly target is shared out (budget.weekly_target_today).

The enrol job fills the day in queue order within those shares first. If a group has too
few ready accounts to fill its share, the rest of the day goes to the next accounts in queue
order, whatever their group, so the inboxes are never left idle for want of one group. To
contact only some groups, switch the others off on the Industries tab (active = no).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from us_outbound import budget
from us_outbound.context import Context
from us_outbound.settings.model import Settings

OTHER = "other"


def key_for(account: Mapping[str, Any], settings: Settings) -> str:
    """The account's share: its industry group if the Focus tab lists it, else "other"."""
    group = settings.industry_group_of(account)
    return group if group in {f.industry_group for f in settings.focus} else OTHER


def shares(settings: Settings) -> dict[str, float]:
    """group -> share of the week; "other" holds what the listed groups leave."""
    out = {f.industry_group: f.share for f in settings.focus}
    rest = 1.0 - sum(out.values())
    if rest > 1e-9:
        out[OTHER] = rest
    return out


def group_rank(group: str, settings: Settings) -> int:
    """0, 1, ... for the Focus tab's groups, largest share first; len(focus) for every other group.

    The source jobs and verify_accounts work in this order, so the Focus tab's industries come first.
    """
    ranked = sorted(settings.focus, key=lambda f: -f.share)  # stable: equal shares keep sheet order
    return next((i for i, f in enumerate(ranked) if f.industry_group == group), len(ranked))


def done_this_week(ctx: Context) -> Counter[str]:
    """Accounts enrolled this week per share (by the account's group now). A second contact (contact_slot 2;
    enrol/second.py) is no new account, and the focus shares out new accounts."""
    ids = {c.get("account_id") for c in ctx.store.select("contacts")
           if budget.in_week(c.get("enrolled_at"), ctx.now) and c.get("contact_slot") != 2}
    out: Counter[str] = Counter()
    for a in ctx.store.select("accounts"):
        if a.get("account_id") in ids:
            out[key_for(a, ctx.settings)] += 1
    return out


@dataclass
class Quota:
    """Today's places per share; allows() and take() as the enrol job walks the queue."""

    left: dict[str, int] = field(default_factory=dict)
    targets: dict[str, int] = field(default_factory=dict)  # the week's number per share
    done: Counter[str] = field(default_factory=Counter)
    settings: Settings | None = None

    @property
    def active(self) -> bool:
        return bool(self.left)

    def allows(self, account: Mapping[str, Any]) -> bool:
        if not self.active or self.settings is None:
            return True
        return self.left.get(key_for(account, self.settings), 0) > 0

    def take(self, account: Mapping[str, Any]) -> None:
        if self.active and self.settings is not None:
            k = key_for(account, self.settings)
            self.left[k] = self.left.get(k, 0) - 1
            self.done[k] += 1

    def describe(self) -> str:
        parts = [f"{k} {self.done[k]} of {self.targets[k]}" for k in self.targets]
        return "Focus this week: " + ", ".join(parts) + "."


def today(ctx: Context, days_left: int) -> Quota:
    """Today's quota per share; an inactive Quota (no limits) when the Focus tab is empty."""
    s = ctx.settings
    if not s.focus:
        return Quota()
    cap = s.general.weekly_enrol_cap
    done = done_this_week(ctx)
    targets = {k: round(share * cap) for k, share in shares(s).items()}
    left = {k: (math.ceil(max(0, t - done[k]) / days_left) if days_left > 0 else 0) for k, t in targets.items()}
    return Quota(left=left, targets=targets, done=Counter(done), settings=s)
