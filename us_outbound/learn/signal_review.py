"""Which signals go with replies: the evidence for the Signals tab's weights, as counts with intervals (Harry, 5 Oct
2026; descriptive from W2-3, 20 Oct 2026).

`us-outbound report signals` is read-only. For each active Score signal on the Signals tab it takes the companies
emailed (step 1 delivered, learn/outcomes.per_company) and splits them by whether they showed the signal when their
contact was enrolled (contacts.signals_at_enrol, written by enrol._record_enrolled; for a contact enrolled before that
column existed, the account's current matches). Each side gets its human-reply count within the 28 days after step 1
(as v_account_outcomes counts it) and its Jeffreys 95% interval (learn/stats.py).

There is no verdict. Until W2-3 a pooled two-proportion z-test at p < 0.10 said raise, lower or keep; at 0–5% reply
rates and 30 a side it is anti-conservative (3 of 30 against 0 of 30 gave p = 0.076, Fisher 0.237) and, with every
signal tested at once, flagged about half of them by chance (metrics-audit H5, M2; redteam-stats §3). A weight change
is read once per 300 matured companies in a cell (L8 and L9), on the Suggestions tab; this report is monitoring. A
split is shown only when both sides have stats.MIN_CELL (15) companies; under that it is "too few". One line each for
the explore arm (random against ranked: Harry's D7, in place of the tiers) and email 1's subject split (personal,
General email1_subject, against the Copy row's; contacts.subject_arm from 5 Oct 2026), the same way.

Replies in windows that have not closed yet are counted as they stand, so an early read understates every rate a
little and both sides alike; the header says how many windows have closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from us_outbound import fmt
from us_outbound.clients.instantly import REPLY_WINDOW_DAYS
from us_outbound.context import Context
from us_outbound.enrol.render import COPY_SUBJECT, PERSONAL_SUBJECT
from us_outbound.learn import outcomes, stats
from us_outbound.scoring.score import MATCH_FACT, SCORING_SOURCE

MIN_CELL = 15  # companies on each side before a split is shown (design-final §9: "every cell has n ≥ 15")
RANDOM, RANKED = "random", "ranked"


@dataclass
class Company:
    account_id: str
    step1_at: datetime
    replied: bool = False
    positive: bool = False
    signals: frozenset[str] = frozenset()
    arm: str = RANKED  # contacts.arm at enrolment; none recorded reads as ranked (v_account_outcomes)
    subject_arm: str = ""  # personal or copy (contacts.subject_arm); "" before the split
    window_closed: bool = False


@dataclass
class Split:
    """One split: the companies with (a) against without (b), each side's replies and Jeffreys 95% interval."""

    name: str
    weight: int
    with_n: int
    with_replied: int
    without_n: int
    without_replied: int

    @property
    def readable(self) -> bool:
        return stats.min_n_ok((self.with_n, self.without_n), MIN_CELL)

    def side(self, k: int, n: int) -> str:
        low, high = stats.jeffreys(k, n)
        return f"{k} of {n} ({fmt.pct(k, n)}; 95% {low * 100:.1f}–{high * 100:.1f}%)"


@dataclass
class Review:
    companies: list[Company] = field(default_factory=list)
    closed: int = 0
    first_send: datetime | None = None
    splits: list[Split] = field(default_factory=list)
    arms: Split | None = None  # random (with) against ranked (without)
    subject: Split | None = None  # email 1's personal subject (with) against the Copy row's (without)


def emailed(ctx: Context) -> tuple[list[Company], int]:
    """Each company whose step 1 was delivered, whether a human replied in its 28 days, and how many windows closed."""
    found = [o for o in outcomes.per_company(ctx) if o.delivered]
    out = {o.account_id: Company(o.account_id, o.step1_at, o.replied_in_window, o.positive_in_window,
                                 arm=o.stamp("arm", RANKED).lower(), subject_arm=o.stamp("subject_arm").lower(),
                                 window_closed=o.window_closed_28) for o in found}
    missing: list[str] = []
    for o in found:
        snap = o.stamps.get("signals_at_enrol")
        if isinstance(snap, list):
            out[o.account_id].signals = frozenset(str(m.get("signal")) for m in snap
                                                  if isinstance(m, Mapping) and m.get("signal"))
        else:
            missing.append(o.account_id)
    if missing:  # enrolled before the snapshot existed: today's matches are the best we have
        now: dict[str, set[str]] = {aid: set() for aid in missing}
        for e in ctx.store.select("signal_events", {"account_id": missing, "source": SCORING_SOURCE, "fact": MATCH_FACT}):
            v = e.get("value") if isinstance(e.get("value"), Mapping) else {}
            if v.get("signal"):
                now[str(e["account_id"])].add(str(v["signal"]))
        for aid, names in now.items():
            out[aid].signals = frozenset(names)
    companies = sorted(out.values(), key=lambda c: (c.step1_at, c.account_id))
    return companies, sum(c.window_closed for c in companies)


def split(name: str, weight: int, with_: list[Company], without: list[Company]) -> Split:
    return Split(name, weight, len(with_), sum(c.replied for c in with_), len(without), sum(c.replied for c in without))


def review(ctx: Context) -> Review:
    companies, closed = emailed(ctx)
    out = Review(companies, closed, companies[0].step1_at if companies else None)
    for sig in ctx.settings.signals:
        if not sig.active or sig.action != "Score":
            continue
        out.splits.append(split(sig.signal, sig.weight, [c for c in companies if sig.signal in c.signals],
                                [c for c in companies if sig.signal not in c.signals]))
    out.arms = split("explore arm", 0, [c for c in companies if c.arm == RANDOM],
                     [c for c in companies if c.arm != RANDOM])
    out.subject = split("email 1 subject", 0, [c for c in companies if c.subject_arm == PERSONAL_SUBJECT],
                        [c for c in companies if c.subject_arm == COPY_SUBJECT])
    return out


def pair_line(label: str, a: str, b: str, s: Split) -> str:
    """"Explore arm: random 3 of 40 (7.5%; 95% 2.1–19.0%) vs ranked 9 of 200 (...)", or why it is too few."""
    if not s.readable:
        return (f"{label}: too few to show ({a} {s.with_n}, {b} {s.without_n} emailed; {MIN_CELL} a side "
                "needed).")
    return f"{label}: {a} {s.side(s.with_replied, s.with_n)} vs {b} {s.side(s.without_replied, s.without_n)}."


def lines(r: Review, limit: int | None = None) -> list[str]:
    """The review in plain words: counts and intervals, the signals with the most companies first; no verdict.
    limit: at most this many signal lines (the Monday readout's)."""
    n = len(r.companies)
    if not n:
        return ["No company has been emailed yet, so there is nothing to review."]
    replied = sum(c.replied for c in r.companies)
    positive = sum(c.positive for c in r.companies)
    first = f", the first on {r.first_send:%a %d %b}" if r.first_send else ""
    out = [f"Signal review: {n} companies emailed{first}; {r.closed} of their {REPLY_WINDOW_DAYS}-day reply windows "
           f"have closed. Replied: {replied} ({fmt.pct(replied, n)}), positive: {positive} ({fmt.pct(positive, n)})."]
    if r.arms is not None:
        out.append(pair_line("Explore arm (read once, at 300 matured in the random arm)", RANDOM, RANKED, r.arms))
    if r.subject is not None:
        out.append(pair_line("Email 1 subject (General email1_subject_share)", "personal", "the Copy row's", r.subject))
    shown = sorted((s for s in r.splits if s.readable), key=lambda s: (-min(s.with_n, s.without_n), s.name))
    if shown:
        out.append("Signals (replies with the signal at enrolment vs without it):")
    for s in shown[:limit] if limit else shown:
        out.append(f"  {s.name} (weight {s.weight}): {s.side(s.with_replied, s.with_n)} vs "
                   f"{s.side(s.without_replied, s.without_n)}")
    if limit and len(shown) > limit:
        out.append(f"  and {len(shown) - limit} more: `us-outbound report signals`")
    few = [s for s in r.splits if not s.readable]
    if few:
        names = ", ".join(f"{s.name} ({s.with_n})" for s in sorted(few, key=lambda s: (-s.with_n, s.name)))
        out.append(f"Too few to show (under {MIN_CELL} emailed companies with or without it): {names}.")
    out.append("Monitoring, not a verdict: with every signal shown at once, some intervals miss each other by chance. "
               "A weight changes only on the Signals tab, after a read at 300 matured companies a side.")
    return out
