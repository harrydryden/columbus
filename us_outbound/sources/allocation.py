"""Where sourcing looks: the week's Apollo search pages per state (loop L2; W3-3, 20 Oct 2026; code-sources §6 L2).

Until W3-3 source_universe read the active states in turn from a rotating start (W1-3a), so each state got about the
same pages whatever came of them. This plans the week's pages instead, once a week, and source_universe's run reads
the slices in the plan's order and spends it down page by page.

It is a prior-weighted allocation with posterior noise, named as such (RT §3), not a bandit with a reward: in 2026 it
optimises contactable yield per page (a cost), not value, since no state has a matured outcome to learn from. Per
slice (industry group × HQ state × size band), over the last WINDOW_WEEKS weeks:
    n  the accounts it admitted (their sourced_by facts: source_universe stamps each account it creates)
    k  of those, the ones verified (or further on) with a contact found
    w  the state's prior weight, customers_in_state + 1 (CUSTOMERS_BY_STATE: Spill's US customers by HQ state)
    value = a draw from Beta(1 + w + k, 1 + n − k)
A state's value is the mean of its slices' draws. The week's PAGES_PER_WEEK pages go one to each active state first
(the floor, so no state goes unread for a week), the rest in proportion to the states' values, no state above
cap = max(0.40, 1 ÷ active states) of them, what a capped state cannot take shared among the others in proportion
(allocate). With two states the cap is 0.50, with seven 0.40: 4 of 10 pages at most.

The plan is kept in source_state (plan:<ISO week>) with what is left of each state's pages; source_universe reads the
slices of the state furthest from its plan first (Lane: the largest share of its pages left), and once every state's
pages are read that week it goes on in turn, so the plan steers where the run looks and the run's need and ceilings
still decide how much. The Monday readout prints the plan (readout_line). Only the States tab's active states, never
CA or WA, and only the Industries tab's active groups are ever planned: the plan moves pages between what Harry
switched on, nothing else.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from us_outbound.clients.db import Range
from us_outbound.context import UK, Context
from us_outbound.sources import source_state

WINDOW_WEEKS = 8  # the slices' yields: accounts admitted in the last 8 weeks
PAGES_PER_WEEK = 100  # the week's planned pages (a run reads at most MAX_PAGES_PER_RUN; a week about 15 to 60)
FLOOR = 1  # pages a week for every active state
CAP_SHARE = 0.40  # no state above max(CAP_SHARE, 1 ÷ active states) of the week's pages
# Spill's US customers by HQ state (data-icp §8.5; HubSpot, 19 Oct 2026): the prior, w = customers + 1. A state not
# named has none (w = 1). Refreshed by hand when the customer base moves (it is fairly static: Harry, 5 Oct 2026).
CUSTOMERS_BY_STATE: dict[str, int] = {"NY": 16, "TX": 4, "IL": 4, "PA": 2, "MA": 1, "GA": 1, "NJ": 0}
PASSED = ("verified", "enrolled", "engaged", "demo_requested", "demo_booked")
NAME = ("a prior-weighted allocation; in 2026 it optimises contactable yield per page, not value")


def week_key(now: datetime) -> str:
    year, week, _ = now.astimezone(UK).date().isocalendar()
    return f"{year}-W{week:02d}"


def plan_key(now: datetime) -> str:
    return f"plan:{week_key(now)}"


def prior(state: str, customers: Mapping[str, int] | None = None) -> int:
    """w_state = customers in the state + 1."""
    return int((CUSTOMERS_BY_STATE if customers is None else customers).get(state, 0)) + 1


def cap_share(states: int) -> float:
    return max(CAP_SHARE, 1 / states) if states else 1.0


# -- the yields ------------------------------------------------------------------------------------------------------


def slice_yields(ctx: Context) -> dict[tuple[str, str, str], tuple[int, int]]:
    """(group, state, band) -> (n admitted, k verified with a contact) over WINDOW_WEEKS, from the sourced_by facts."""
    from us_outbound.sources.apollo_universe import JOB, SOURCED_BY  # apollo_universe imports this module

    since = ctx.now - timedelta(weeks=WINDOW_WEEKS)
    facts = ctx.store.select("signal_events", {"source": JOB, "fact": SOURCED_BY, "observed_at": Range(since, None)})
    where: dict[str, tuple[str, str, str]] = {}
    for e in facts:
        v = e.get("value")
        if e.get("account_id") and isinstance(v, Mapping) and v.get("state"):
            where[str(e["account_id"])] = (str(v.get("group") or ""), str(v["state"]), str(v.get("band") or ""))
    ids = sorted(where)
    status: dict[str, str] = {}
    with_contact: set[str] = set()
    for i in range(0, len(ids), 1000):
        chunk = ids[i : i + 1000]
        status.update({str(a["account_id"]): str(a.get("status") or "")
                       for a in ctx.store.select("accounts", {"account_id": chunk})})
        with_contact |= {str(c["account_id"]) for c in ctx.store.select("contacts", {"account_id": chunk})}
    out: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for aid, key in where.items():
        if aid not in status:  # gone (retention, a merge): not counted either way
            continue
        out[key][0] += 1
        out[key][1] += status[aid] in PASSED and aid in with_contact
    return {k: (n, k_) for k, (n, k_) in out.items()}


# -- the allocation --------------------------------------------------------------------------------------------------


def targets(values: Mapping[str, float], pages: int, cap: float | None = None) -> dict[str, float]:
    """The pages each state should get, before rounding: FLOOR each, the rest in proportion to value, none above the
    cap (cap × pages), what a capped state cannot take shared among the uncapped ones in proportion."""
    states = list(values)
    if not states or pages <= 0:
        return {s: 0.0 for s in states}
    cap_pages = (cap if cap is not None else cap_share(len(states))) * pages
    out = {s: float(min(FLOOR, cap_pages)) for s in states}
    left = pages - sum(out.values())
    free = [s for s in states if out[s] < cap_pages - 1e-9]
    while left > 1e-9 and free:
        total = sum(max(values[s], 0.0) for s in free)
        share = {s: (max(values[s], 0.0) / total if total > 0 else 1 / len(free)) for s in free}
        over = [s for s in free if out[s] + left * share[s] > cap_pages + 1e-9]
        if not over:
            for s in free:
                out[s] += left * share[s]
            break
        for s in over:  # these take the cap; the rest is shared again among the others
            left -= cap_pages - out[s]
            out[s] = cap_pages
        free = [s for s in free if s not in over]
    return out


def allocate(values: Mapping[str, float], pages: int, cap: float | None = None) -> dict[str, int]:
    """Whole pages per state (targets rounded by the largest remainder, never above the cap)."""
    want = targets(values, pages, cap)
    if not want:
        return {}
    cap_pages = math.floor((cap if cap is not None else cap_share(len(want))) * pages + 1e-9)
    out = {s: math.floor(t + 1e-9) for s, t in want.items()}
    order = sorted(want, key=lambda s: (-(want[s] - out[s]), -values[s], s))
    left = pages - sum(out.values())
    for s in order * 2:
        if left <= 0:
            break
        if out[s] < max(cap_pages, FLOOR):
            out[s] += 1
            left -= 1
    return out


def sample_values(slices: Iterable[tuple[str, str, str]], yields: Mapping[tuple[str, str, str], tuple[int, int]],
                  rng: random.Random, customers: Mapping[str, int] | None = None) -> dict[str, float]:
    """state -> the mean of its slices' draws from Beta(1 + w + k, 1 + n − k)."""
    draws: dict[str, list[float]] = defaultdict(list)
    for key in slices:
        n, k = yields.get(key, (0, 0))
        k = min(k, n)
        draws[key[1]].append(rng.betavariate(1 + prior(key[1], customers) + k, 1 + n - k))
    return {st: sum(v) / len(v) for st, v in draws.items()}


@dataclass
class Plan:
    """The week's pages per state, and what is left of them (kept in source_state as plan:<ISO week>)."""

    week: str
    pages: dict[str, int]
    left: dict[str, int]
    values: dict[str, float] = field(default_factory=dict)
    yields: dict[str, list[int]] = field(default_factory=dict)  # state -> [k, n] over its slices
    made_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"week": self.week, "pages": dict(self.pages), "left": dict(self.left),
                "values": {s: round(v, 4) for s, v in self.values.items()}, "yields": dict(self.yields),
                "made_at": self.made_at}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Plan:
        return cls(week=str(d.get("week") or ""), pages={str(k): int(v) for k, v in (d.get("pages") or {}).items()},
                   left={str(k): int(v) for k, v in (d.get("left") or {}).items()},
                   values={str(k): float(v) for k, v in (d.get("values") or {}).items()},
                   yields={str(k): list(v) for k, v in (d.get("yields") or {}).items()},
                   made_at=str(d.get("made_at") or ""))

    def spend(self, state: str) -> None:
        if self.left.get(state, 0) > 0:
            self.left[state] -= 1

    def describe(self) -> str:
        parts = [f"{s} {self.pages[s] - self.left.get(s, 0)} of {self.pages[s]}"
                 for s in sorted(self.pages, key=lambda s: (-self.pages[s], s))]
        return f"pages read this week by HQ state: {', '.join(parts)}"


def plan_week(ctx: Context, slices: Sequence[tuple[str, str, str]], states: Sequence[str], *,
              rng: random.Random | None = None, pages: int = PAGES_PER_WEEK) -> Plan:
    """The week's plan: each active state's sampled value from its slices, then allocate (the module docstring).

    slices: (group, state, band) of every search the run could read; a state with none is planned on its prior alone.
    rng: a seeded one in tests; else one seeded by the week, so a plan made twice in a week draws the same."""
    rng = rng or random.Random(f"allocation:{week_key(ctx.now)}")
    yields = slice_yields(ctx)
    keys = sorted({k for k in slices if k[1] in states})
    keys += [("", st, "") for st in states if not any(k[1] == st for k in keys)]
    values = sample_values(keys, yields, rng)
    per_state: dict[str, list[int]] = {st: [0, 0] for st in states}
    for (g, st, b), (n, k) in yields.items():
        if st in per_state and (g, st, b) in set(keys):
            per_state[st][0] += k
            per_state[st][1] += n
    got = allocate({st: values[st] for st in states}, pages)
    return Plan(week=week_key(ctx.now), pages=got, left=dict(got), values=values, yields=per_state,
                made_at=ctx.now.isoformat())


def this_week(ctx: Context, slices: Sequence[tuple[str, str, str]], states: Sequence[str]) -> Plan:
    """The week's plan from source_state, made (and kept) when the week has none or its states changed."""
    kept = source_state.get(ctx.store, plan_key(ctx.now))
    if isinstance(kept, Mapping):
        plan = Plan.from_dict(kept)
        if set(plan.pages) == set(states):
            return plan
    plan = plan_week(ctx, slices, states)
    save(ctx, plan)
    return plan


def save(ctx: Context, plan: Plan) -> None:
    source_state.put(ctx.store, plan_key(ctx.now), plan.as_dict(), ctx.now)


# -- reading in plan order -------------------------------------------------------------------------------------------


class Lane:
    """The searches still to read for one pool, a queue per state: the next comes from the state with the largest
    share of its planned pages left (ties: the larger plan, then the states' turn); once every state's pages are
    read, the states in turn. A search read and not finished goes to the back of its state's queue."""

    def __init__(self, slices: Iterable[Any], plan: Plan, order: Sequence[str]):
        self.plan = plan
        self.order = list(order)
        self.queues: dict[str, deque[Any]] = {}
        for sl in slices:
            self.queues.setdefault(sl.state, deque()).append(sl)
        self._turn = 0

    def __bool__(self) -> bool:
        return any(self.queues.values())

    def _rank(self, st: str) -> int:
        return self.order.index(st) if st in self.order else len(self.order)

    def popleft(self) -> Any:
        have = [st for st, q in self.queues.items() if q]
        planned = [st for st in have if self.plan.left.get(st, 0) > 0]
        if planned:
            st = min(planned, key=lambda s: (-self.plan.left[s] / max(1, self.plan.pages.get(s, 1)),
                                             -self.plan.pages.get(s, 0), self._rank(s)))
        else:
            turn = sorted(have, key=self._rank)
            st = turn[self._turn % len(turn)]
            self._turn += 1
        return self.queues[st].popleft()

    def append(self, sl: Any) -> None:
        self.queues.setdefault(sl.state, deque()).append(sl)


# -- the readout -----------------------------------------------------------------------------------------------------


def readout_line(ctx: Context) -> str:
    """The Monday readout's line on where sourcing looks this week."""
    kept = source_state.get(ctx.store, plan_key(ctx.now))
    if not isinstance(kept, Mapping):
        return f"  Sourcing plan ({NAME}): none made yet this week; source_universe makes it on its first run."
    plan = Plan.from_dict(kept)
    total = sum(plan.pages.values())
    yields = ", ".join(f"{s} {k} of {n}" for s, (k, n) in sorted(plan.yields.items()) if n)
    return (f"  Sourcing plan for {plan.week} ({NAME}): {total} pages, at least {FLOOR} a state and at most "
            f"{cap_share(len(plan.pages)):.0%} to one; {plan.describe()}."
            + (f" Admitted accounts verified with a contact, last {WINDOW_WEEKS} weeks: {yields}." if yields else ""))
