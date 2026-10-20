"""What enrol could send today, worked out once (9 Oct 2026, the refactoring scan's phase 6).

enrol.run, the daily post, `status` and the accounts view each worked this out themselves, and had drifted.
read() is the one way; every count of "ready to send" is Today.ready_count.

The order is enrol.run's: this week's hand-check pulls keep their accounts out of the candidates, and limits.today
sets the number from what is ready. (Send approvals waiting in Slack and second contacts were part of it until both
were removed: W2-1, 20 Oct 2026.)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

from us_outbound import limits
from us_outbound.context import Context
from us_outbound.enrol import enrol


@dataclass(frozen=True)
class Today:
    day: date  # the send day, US Eastern
    waits: str | None  # why enrollment waits for this week's hand-check, or None (enrol.hand_check)
    pulled: frozenset[str]  # the accounts Harry pulled at this week's hand-check
    ready: list[enrol.Candidate]  # in queue order
    skipped: Counter[str]  # why the other accounts are not ready
    limits: limits.Limits

    @property
    def ready_count(self) -> int:
        """Every contact enrol could add today."""
        return len(self.ready)


def read(ctx: Context, day: date | None = None, *, pulled: frozenset[str] | None = None,
         campaigns: bool = True) -> Today:
    """Today as enrol.run sees it. pulled: the hand-check's pulls when the caller has read them (enrol.run, so the
    week's items are read once); campaigns: read each owner's campaign status from Instantly (limits.today)."""
    day = day or ctx.now_et().date()
    waits = None
    if pulled is None:
        waits, pulled = enrol.hand_check(ctx, day)
    ready, skipped = enrol.candidates(ctx, pulled)
    lim = limits.today(ctx, day, ready_accounts=len(ready), campaigns=campaigns)
    return Today(day, waits, pulled, ready, skipped, lim)
