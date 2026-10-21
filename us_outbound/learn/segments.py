"""segment_prior_monthly, the 1st of each month at 02:20 UK (W3-4, 21 Oct 2026; design-final §5 L9; data-icp §3, §8.3).

Which industry groups and sizes are worth emailing cannot be learned from Columbus's own meetings at this volume
(about 2,300 a segment would be needed: design-final §2.3), so the segment value starts from Spill's own HubSpot
history and lets Columbus's replies move it as they mature:

  refresh(ctx)  read only: the Spill 3.0 pipeline's decided deals (won: Closed won or Churned, a won deal that
                churned since; lost: Closed lost; open and disqualified deals are left out), read in one paged search
                for the five deal properties DEAL_FIELDS, never a contact or a company name. Counted per industry group
                (the deal's HubSpot industry through lookalikes' HUBSPOT_INDUSTRY_LABELS) and per size band (employees
                covered, lookalikes.band_of), into the metrics table as the month's prior (day: the 1st), one row per
                cell and measure (sources/lookalikes.PRIOR_METRICS): segment_prior_won (k won, n decided, value the
                win rate shrunk toward PRIOR_MEAN with weight PRIOR_WEIGHT: (won + 7.6) ÷ (decided + 20), which is
                the mean of Beta(won + 7.6, lost + 12.4)), segment_prior_median_gbp (the median won deal, £ a month),
                segment_prior_days (the median days to close a won deal) and segment_prior_retained (won deals not
                churned: (k + 1) ÷ (n + 2)). Its own definition_version (PRIOR_VERSION). Post-demo rates, mostly UK
                inbound: what converts once a buyer raises a hand, not who replies to a cold email (data-icp §3).
  value(ctx)    per group and per size band (not their cross: 12 cells cannot reach 300 each), Columbus's positive
                rate within 28 days (learn/outcomes.py, matured, intent to treat) from a Beta(2, 98) prior (2%) updated
                by its matured companies, times P(win | demo) SAMPLED from Beta(won + 7.6, lost + 12.4), times the
                median £ and the retained share: the expected £ a month per company contacted, by Monte Carlo
                (DRAWS, seeded so a month's report is repeatable), and each Focus group's Thompson share (how often
                its draw is the best of the Focus groups').
  propose(ctx)  Suggestions-tab rows (settings/suggestions.py), never a change: a Focus-share row for a Focus group
                only once it has PROPOSE_AT (300) matured, its Thompson share with a FLOOR_SHARE (10%) floor; and a
                switch-off row only when P(the cell's rate < half the pooled Focus rate) > OFF_AT (0.9), read once per
                300 matured in that cell (the row's id carries the 300 it was read at) and never under 300. None will
                be made for months; the job's summary and the Monday readout print the n each cell has.
lookalikes.FitModel reads the prior's win rates for the industry part of the lookalike fit (the shrunk win rate ÷
PRIOR_MEAN, clipped to 0.5 to 1.5) in place of the customers' volume, which rewarded a group for being common.
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from us_outbound.clients.guard import GuardViolation
from us_outbound.context import Context
from us_outbound.learn import outcomes, stats
from us_outbound.learn.outcomes import Outcome
from us_outbound.logs import log
from us_outbound.parse import number
from us_outbound.sources import lookalikes as lk

JOB = "segment_prior_monthly"
PRIOR_VERSION = lk.PRIOR_VERSION
WON_STAGES = {"154381891": "Closed won", "1287546140": "Churned"}  # Spill 3.0 (data-icp §3)
CHURNED_STAGES = frozenset({"1287546140"})
LOST_STAGES = {"154381892": "Closed lost"}
DEAL_FIELDS = ("industry", "dealstage", "days_to_close", "amount_in_home_currency", "employees_covered__cloned_")
PRIOR_MEAN, PRIOR_WEIGHT = lk.PRIOR_MEAN, lk.PRIOR_WEIGHT  # 0.38 and 20: Beta(7.6, 12.4)
RATE_PRIOR = (2.0, 98.0)  # Columbus's positive rate before its own outcomes: 2%, worth 100 companies
DRAWS = 2000
SEED = 20261021
PROPOSE_AT = 300
OFF_AT = 0.9
FLOOR_SHARE = 0.10
GROUP, BAND = "group", "size_band"
# Columbus's size bands (accounts.size_band, SIZE_BANDS) as the prior's (lookalikes.TARGET_BANDS).
PRIOR_BAND = {"10-19": "10-49", "20-49": "10-49", "10-49": "10-49", "50-99": "50-99", "100-249": "100-249"}


@dataclass
class Prior:
    """One cell of the HubSpot prior."""

    won: int = 0
    lost: int = 0
    churned: int = 0
    amounts: list[float] = field(default_factory=list)
    days: list[float] = field(default_factory=list)

    @property
    def decided(self) -> int:
        return self.won + self.lost

    @property
    def win_rate(self) -> float:
        """The mean of Beta(won + 7.6, lost + 12.4): the win rate shrunk toward 0.38 with weight 20."""
        return (self.won + PRIOR_MEAN * PRIOR_WEIGHT) / (self.decided + PRIOR_WEIGHT)

    @property
    def retained(self) -> float:
        return (self.won - self.churned + 1) / (self.won + 2)


def group_of(settings: Any, hubspot_industry: str) -> str:
    """The industry group a deal's HubSpot industry names ("" for none: IT services, Other)."""
    label = lk.customer_label({"company_industry": hubspot_industry}, settings)
    ind = settings.industry(label) if label else None
    return ind.industry_group if ind else ""


def tally(settings: Any, deals: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Prior]:
    """(group or size_band, its value) -> Prior, over the decided deals (the module docstring)."""
    out: dict[tuple[str, str], Prior] = defaultdict(Prior)
    for deal in deals:
        p = deal.get("properties") if isinstance(deal.get("properties"), Mapping) else deal
        stage = str(p.get("dealstage") or "")
        if stage not in WON_STAGES and stage not in LOST_STAGES:
            continue
        cells = []
        group = group_of(settings, str(p.get("industry") or ""))
        if group:
            cells.append((GROUP, group))
        covered = number(p.get("employees_covered__cloned_"), commas=True)
        if covered is not None and covered > 0:
            cells.append((BAND, lk.band_of(covered)))
        for key in cells:
            c = out[key]
            if stage in WON_STAGES:
                c.won += 1
                c.churned += stage in CHURNED_STAGES
                amount = number(p.get("amount_in_home_currency"), commas=True)
                if amount is not None and amount > 0:
                    c.amounts.append(amount)
                days = number(p.get("days_to_close"), commas=True)
                if days is not None and days >= 0:
                    c.days.append(days)
            else:
                c.lost += 1
    return dict(out)


def prior_rows(day: Any, cells: Mapping[tuple[str, str], Prior], now: Any) -> list[dict]:
    from us_outbound.learn import metrics

    rows = []
    for (kind, value), c in sorted(cells.items()):
        seg = f"{kind}={value}"
        rows += [
            {**metrics.row(day, seg, lk.PRIOR_WIN, k=c.won, n=c.decided, value=c.win_rate)},
            {**metrics.row(day, seg, lk.PRIOR_GBP, n=len(c.amounts),
                           value=statistics.median(c.amounts) if c.amounts else None)},
            {**metrics.row(day, seg, lk.PRIOR_DAYS, n=len(c.days), value=statistics.median(c.days) if c.days else None)},
            {**metrics.row(day, seg, lk.PRIOR_RETAINED, k=c.won - c.churned, n=c.won, value=c.retained)},
        ]
    for r in rows:
        r["definition_version"] = PRIOR_VERSION
        r["computed_at"] = now
    return rows


def refresh(ctx: Context) -> dict[str, Any]:
    """Read the decided deals (read only) and store this month's prior (the module docstring)."""
    pipeline = ctx.settings.general.hubspot_pipeline_id.strip() or lk.SPILL3_PIPELINE
    stages = sorted({*WON_STAGES, *LOST_STAGES})
    groups = [[{"propertyName": "pipeline", "operator": "EQ", "value": pipeline},
               {"propertyName": "dealstage", "operator": "IN", "values": stages}]]
    deals = list(ctx.clients.hubspot.iter_deals(groups, list(DEAL_FIELDS)))
    cells = tally(ctx.settings, deals)
    if not cells:  # no decided deal at all is surely a fault: keep last month's prior
        log("segment_prior_empty", run_id=ctx.run_id, deals=len(deals))
        return {"deals": len(deals), "cells": 0, "stored": False}
    day = ctx.today_uk().replace(day=1)
    ctx.store.upsert("metrics", prior_rows(day, cells, ctx.now))
    return {"deals": len(deals), "cells": len(cells), "stored": True, "day": day.isoformat(),
            "decided": {f"{k}={v}": c.decided for (k, v), c in sorted(cells.items())}}


# -- Columbus's own outcomes on the prior ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    kind: str  # group or size_band
    key: str
    n: int  # Columbus's companies matured at 28 days
    k: int  # positive or referral within 28 days
    rate: float  # the posterior mean, from Beta(2, 98)
    p_win: float  # the prior's shrunk win rate (its Beta's mean)
    median_gbp: float | None
    retained: float
    value: float  # the mean draw of rate × p_win × median £ × retained: £ a month per company contacted
    share: float | None = None  # a Focus group's Thompson share (with the floor), else None


def stored_prior(ctx: Context) -> dict[tuple[str, str], dict[str, dict]]:
    """(kind, key) -> {metric: its row}, the latest month's prior (lookalikes.segment_prior's rows)."""
    out: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for r in lk.prior_rows_latest(ctx.store):
        kind, _, key = str(r["segment_key"]).partition("=")
        out[(kind, key)][str(r["metric"])] = r
    return dict(out)


def _cut(o: Outcome, kind: str) -> str:
    if kind == GROUP:
        return o.stamp("industry_group_at_enrol")
    return PRIOR_BAND.get(o.stamp("size_band_at_enrol"), "")


def value(ctx: Context, found: Sequence[Outcome] | None = None, *, draws: int = DRAWS, seed: int = SEED) -> list[Cell]:
    """Each cell of the prior with Columbus's matured outcomes on it (the module docstring), groups first."""
    found = outcomes.per_company(ctx) if found is None else found
    m28 = [o for o in found if o.window_closed_28]
    prior = stored_prior(ctx)
    rng = random.Random(seed)
    focus = {f.industry_group for f in ctx.settings.focus}
    samples: dict[tuple[str, str], list[float]] = {}
    out: list[Cell] = []
    for (kind, key), rows in sorted(prior.items()):
        won_row = rows.get(lk.PRIOR_WIN) or {}
        won, decided = int(won_row.get("k") or 0), int(won_row.get("n") or 0)
        gbp = (rows.get(lk.PRIOR_GBP) or {}).get("value")
        kept = (rows.get(lk.PRIOR_RETAINED) or {}).get("value")
        retained = float(kept) if kept is not None else 1.0
        mine = [o for o in m28 if _cut(o, kind) == key]
        k, n = sum(o.positive_in_window for o in mine), len(mine)
        a, b = RATE_PRIOR[0] + k, RATE_PRIOR[1] + n - k
        wa, wb = won + PRIOR_MEAN * PRIOR_WEIGHT, decided - won + (1 - PRIOR_MEAN) * PRIOR_WEIGHT
        draws_ = [rng.betavariate(a, b) * rng.betavariate(wa, wb) * float(gbp or 0) * retained for _ in range(draws)]
        samples[(kind, key)] = draws_
        out.append(Cell(kind, key, n, k, a / (a + b), wa / (wa + wb), float(gbp) if gbp is not None else None,
                        retained, statistics.fmean(draws_)))
    groups = [c for c in out if c.kind == GROUP and c.key in focus]
    # Shares only when every Focus group has a median £ (W3-4 r1): a group without one draws 0 every time, so max()
    # would hand the first group every draw (100/0/0 before the floor), which says nothing about value.
    if groups and all(c.median_gbp is not None for c in groups):
        best = [max(groups, key=lambda c: samples[(GROUP, c.key)][i]).key for i in range(draws)]
        raw = {c.key: best.count(c.key) / draws for c in groups}
        shares = floor_shares(raw, FLOOR_SHARE)
        out = [Cell(**{**c.__dict__, "share": shares[c.key]}) if c.kind == GROUP and c.key in shares else c
               for c in out]
    return out


def floor_shares(raw: Mapping[str, float], floor: float) -> dict[str, float]:
    """Shares summing to 1 with every one at least `floor`: those under it raised to it, the rest scaled down to the
    remainder in proportion."""
    low = {k for k, v in raw.items() if v < floor}
    rest = {k: v for k, v in raw.items() if k not in low}
    left = 1 - floor * len(low)
    total = sum(rest.values())
    out = {k: floor for k in low}
    out |= {k: (v / total * left if total else left / len(rest)) for k, v in rest.items()}
    return out


def off_probability(k: int, n: int, focus_rate: float) -> float:
    """P(the cell's positive rate < half the Focus rate) under Beta(2 + k, 98 + n − k)."""
    return stats.beta_cdf(focus_rate / 2, RATE_PRIOR[0] + k, RATE_PRIOR[1] + n - k)


def propose(ctx: Context, cells: Sequence[Cell] | None = None, found: Sequence[Outcome] | None = None
            ) -> list[dict[str, str]]:
    """The Suggestions-tab rows (the module docstring): none until a cell has PROPOSE_AT matured."""
    import json

    from us_outbound.settings import suggestions as tab

    found = outcomes.per_company(ctx) if found is None else found
    cells = value(ctx, found) if cells is None else cells
    focus = {f.industry_group for f in ctx.settings.focus}
    m28 = [o for o in found if o.window_closed_28]
    in_focus = [o for o in m28 if o.stamp("industry_group_at_enrol") in focus]
    focus_rate = sum(o.positive_in_window for o in in_focus) / len(in_focus) if in_focus else 0.0
    rows: list[dict[str, str]] = []
    for c in cells:
        if c.n < PROPOSE_AT:
            continue
        read_at = c.n // PROPOSE_AT * PROPOSE_AT
        snap = json.dumps({"n": c.n, "k": c.k, "rate": round(c.rate, 4), "p_win": round(c.p_win, 3),
                           "focus_rate": round(focus_rate, 4), "read_at": read_at, "prior": PRIOR_VERSION})
        if c.kind == GROUP and c.share is not None:
            rows.append({"id": f"{tab.FOCUS_SHARE}:{c.key}:{read_at}", "kind": tab.FOCUS_SHARE, "target": c.key,
                         "text": f"{c.share:.0%}", "evidence": f"{c.k} positive of {c.n} matured; Thompson share "
                         f"{c.share:.0%} (floor {FLOOR_SHARE:.0%}) on £{c.value:.2f} a month per company contacted",
                         "snapshot": snap, "status": tab.PROPOSED, "applied": ""})
        p_off = off_probability(c.k, c.n, focus_rate)
        if c.kind == GROUP and focus_rate > 0 and p_off > OFF_AT:
            rows.append({"id": f"{tab.GROUP_OFF}:{c.key}:{read_at}", "kind": tab.GROUP_OFF, "target": c.key,
                         "text": "off", "evidence": f"P(positive rate < half the Focus rate {focus_rate:.1%}) = "
                         f"{p_off:.2f} on {c.k} of {c.n} matured", "snapshot": snap, "status": tab.PROPOSED,
                         "applied": ""})
    return rows


def run(ctx: Context) -> dict[str, Any]:
    """The segment_prior_monthly job (JOB CONTRACT: run(ctx) -> summary): refresh the prior, then propose."""
    from us_outbound.settings import suggestions as tab

    refreshed = refresh(ctx)
    try:
        found = outcomes.per_company(ctx)
        cells = value(ctx, found)
        rows = propose(ctx, cells, found)
        wrote = tab.write(ctx, rows)
    except GuardViolation:
        raise
    except Exception as exc:
        log("segment_value_failed", run_id=ctx.run_id, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return {"job": JOB, "dry_run": ctx.dry_run, **refreshed, "errors": [f"{type(exc).__name__}: {str(exc)[:160]}"]}
    summary = {"job": JOB, "dry_run": ctx.dry_run, **refreshed,
               "matured": {f"{c.kind}={c.key}": c.n for c in cells}, "proposals": len(rows), "suggestions": wrote}
    log("segment_prior_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "decided"})
    return summary


def readout_line(ctx: Context, found: Sequence[Outcome] | None = None) -> str:
    """The Monday readout's segment line: the prior's month and size, and the n each Focus group has toward 300."""
    prior = stored_prior(ctx)
    if not prior:
        return "  Segment value: no HubSpot prior stored yet (segment_prior_monthly runs on the 1st)."
    decided = sum(int((rows.get(lk.PRIOR_WIN) or {}).get("n") or 0) for (kind, _), rows in prior.items()
                  if kind == BAND)
    day = next(iter(next(iter(prior.values())).values()))["day"]
    found = outcomes.per_company(ctx) if found is None else found
    m28 = [o for o in found if o.window_closed_28]
    focus = [f.industry_group for f in ctx.settings.focus]
    ns = ", ".join(f"{g} {sum(_cut(o, GROUP) == g for o in m28)}" for g in focus) or "no Focus group"
    return (f"  Segment value: the HubSpot prior of {day} ({decided:,} decided deals with a size); matured at 28 days "
            f"per Focus group: {ns}. A share or switch-off proposal waits for {PROPOSE_AT} in a group.")
