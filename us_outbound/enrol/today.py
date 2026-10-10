"""What enrol could send today, worked out once (9 Oct 2026, the refactoring scan's phase 6).

enrol.run, the daily post, `status`, `golive` and the accounts view each worked this out themselves, and had drifted:
the post's headline counted first contacts only while its limits line counted second contacts too, and the accounts
view left the second contacts out. read() is the one way; every count of "ready to send" is Today.ready_count.

The order is enrol.run's: this week's hand-check pulls and the send approvals still waiting keep their accounts out
of the candidates, the waiting cards hold their senders' slots, and limits.today sets the number from what is ready.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

from us_outbound import limits
from us_outbound.context import Context
from us_outbound.enrol import approvals, enrol, second


@dataclass(frozen=True)
class Today:
    day: date  # the send day, US Eastern
    waits: str | None  # why enrollment waits for this week's hand-check, or None (enrol.hand_check)
    pulled: frozenset[str]  # the accounts Harry pulled at this week's hand-check
    held: approvals.Waiting  # the send approvals still waiting: their accounts and their senders' slots
    ready: list[enrol.Candidate]  # first contacts, in queue order
    skipped: Counter[str]  # why the other accounts are not ready
    seconds: list[enrol.Candidate]  # second contacts (none while General second_contact is no)
    not_second: Counter[str]
    limits: limits.Limits

    @property
    def ready_count(self) -> int:
        """Every contact enrol could propose today: first contacts and second ones."""
        return len(self.ready) + len(self.seconds)


def read(ctx: Context, day: date | None = None, *, pulled: frozenset[str] | None = None,
         campaigns: bool = True) -> Today:
    """Today as enrol.run sees it. pulled: the hand-check's pulls when the caller has read them (enrol.run, so the
    week's items are read once); campaigns: read each owner's campaign status from Instantly (limits.today)."""
    day = day or ctx.now_et().date()
    waits = None
    if pulled is None:
        waits, pulled = enrol.hand_check(ctx, day)
    held = approvals.waiting(ctx)
    ready, skipped = enrol.candidates(ctx, pulled, held.accounts)
    seconds, not_second = second.candidates(ctx, pulled, held.accounts)
    lim = limits.today(ctx, day, ready_accounts=len(ready) + len(seconds), pending=held.by_owner,
                       campaigns=campaigns, second_ready=len(seconds))
    return Today(day, waits, pulled, held, ready, skipped, seconds, not_second, lim)
