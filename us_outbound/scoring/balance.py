"""Self-balancing tier thresholds (Harry, 19 Oct 2026: "implement self-balancing logic (re thresholds) to ensure that
the best and right leads are coming through and are sufficient for the capacity in the rest of the system").

While General auto_thresholds is yes (the default), priority_threshold and standard_threshold come from the queue, not
the sheet. Once a UK day, the first rescore (score.rescore) reads the scores of the open queue (new, queued and
verified accounts, not Held or Excluded) and sets:
  * priority_threshold: top fit is the best PRIORITY_SHARE (a fifth) of the queue, but a company needs PRIORITY_FLOOR
    points at least (one strong signal), so a weak queue has few top-fit companies rather than weak ones called top;
  * standard_threshold: the comparison group (Control, below it) aims at CONTROL_SHARE of the queue, with two checks
    on capacity: the good-fit pool (Priority and Standard) must hold COVER_WEEKS of the sends that are not comparison
    (weekly_enrol_cap × (1 − control_share) a week), and the comparison group COVER_WEEKS of its own share. When both
    cannot hold, the good-fit pool comes first (the best companies are sent), and the day's record says the queue is
    short: more companies are needed, which no threshold can supply. A company needs STANDARD_FLOOR points (a signal)
    to be good fit.
Enrol takes the best first whatever the thresholds (enrol/queue.order_key: by tier, then score), so the thresholds
decide what counts as good fit and how big the comparison group is, not the order. The tier of a company already
emailed is kept as tier_at_enrol on its contact, so the cohort reports do not move with the thresholds.

The day's thresholds are a signal_events row (source SOURCE, fact FACT, no account), so every job that day reads the
same ones: bootstrap.build_context lays the latest over the sheet's (applied), as base/holds.py lays kill-rule holds.
With auto_thresholds = no, the sheet's two values are used as typed, and nothing here runs.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.settings.model import General, Settings

SOURCE, FACT = "score", "auto_thresholds"
PRIORITY_SHARE = 0.20  # top fit: the best fifth of the queue
CONTROL_SHARE = 0.30  # the comparison group's aim
CONTROL_MIN_SHARE = 0.10  # never smaller than this, so the comparison still means something
COVER_WEEKS = 2  # the good-fit pool should last two weeks at weekly_enrol_cap
PRIORITY_FLOOR = 30  # one strong signal (a new People leader is 30)
STANDARD_FLOOR = 5  # at least one signal
MIN_POOL = 20  # with fewer companies than this, the sheet's thresholds stand


@dataclasses.dataclass(frozen=True)
class Thresholds:
    priority: int
    standard: int
    pool: int  # open queue companies the thresholds were set from
    good_fit: int  # of them, Priority and Standard
    top_fit: int  # of them, Priority
    need: int  # good-fit companies COVER_WEEKS of sending needs
    short: bool  # the queue cannot hold both the good-fit pool and the comparison group
    day: str = ""

    @property
    def weeks(self) -> float:
        """How many weeks of non-comparison sends the good-fit pool holds."""
        per_week = self.need / COVER_WEEKS if self.need else 0
        return round(self.good_fit / per_week, 1) if per_week else 0.0

    def value(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "weeks": self.weeks}


def _at_least(scores: Sequence[int], t: int) -> int:
    return sum(1 for s in scores if s >= t)


def balance(scores: Sequence[int], g: General) -> Thresholds | None:
    """The thresholds for a queue with these scores; None below MIN_POOL companies (the sheet's then stand)."""
    n = len(scores)
    if n < MIN_POOL:
        return None
    top = max(scores)
    # Top fit: the threshold whose share of the queue is nearest PRIORITY_SHARE (the higher one on a tie), never
    # below PRIORITY_FLOOR. With no company that high, nobody is top fit.
    options = sorted({s for s in scores if s >= PRIORITY_FLOOR})
    if options:
        priority = min(options, key=lambda t: (abs(_at_least(scores, t) / n - PRIORITY_SHARE), -t))
    else:
        priority = max(PRIORITY_FLOOR, top + 1)
    # The comparison group: below standard. Candidates are the scores themselves (and the floor), up to priority.
    weekly = g.weekly_enrol_cap
    need_main = math.ceil(COVER_WEEKS * weekly * (1 - g.control_share))
    need_control = max(math.ceil(COVER_WEEKS * weekly * g.control_share), math.ceil(CONTROL_MIN_SHARE * n))
    candidates = sorted({t for t in [STANDARD_FLOOR, *scores] if STANDARD_FLOOR <= t <= priority})

    def aim(t: int) -> tuple[float, int]:
        return abs((n - _at_least(scores, t)) / n - CONTROL_SHARE), -t

    both = [t for t in candidates if _at_least(scores, t) >= need_main and n - _at_least(scores, t) >= need_control]
    main_ok = [t for t in candidates if _at_least(scores, t) >= need_main]
    if both:
        standard, short = min(both, key=aim), False
    elif main_ok:  # the good-fit pool first: the comparison group is smaller than it should be
        standard, short = min(main_ok, key=aim), True
    else:  # even everyone with a signal is too few: all of them are good fit
        standard, short = STANDARD_FLOOR, True
    return Thresholds(priority=priority, standard=standard, pool=n, good_fit=_at_least(scores, standard),
                      top_fit=_at_least(scores, priority), need=need_main, short=short)


def with_thresholds(settings: Settings, t: Thresholds | Mapping[str, Any] | None) -> Settings:
    """The settings with these thresholds in General (as they are when t is None)."""
    if t is None:
        return settings
    p, s = (t.priority, t.standard) if isinstance(t, Thresholds) else (t.get("priority"), t.get("standard"))
    if not isinstance(p, int) or not isinstance(s, int):
        return settings
    return dataclasses.replace(settings, general=dataclasses.replace(settings.general, priority_threshold=p,
                                                                     standard_threshold=s))


def stored(store: Store, day: date | None = None) -> dict[str, Any] | None:
    """The latest stored thresholds (that day's only, when day is given)."""
    rows = [r for r in store.select("signal_events", {"source": SOURCE, "fact": FACT})
            if isinstance(r.get("value"), Mapping)]
    if day is not None:
        rows = [r for r in rows if r["value"].get("day") == day.isoformat()]
    if not rows:
        return None
    return dict(max(rows, key=lambda r: (str(r["value"].get("day") or ""), str(r.get("observed_at") or "")))["value"])


def record(store: Store, t: Thresholds, day: date, now: datetime) -> Thresholds:
    """Keep the day's thresholds (one row a day: a later rescore that day reads it rather than moving them)."""
    t = dataclasses.replace(t, day=day.isoformat())
    store.upsert("signal_events", [{"event_id": f"{FACT}:{t.day}", "account_id": None, "source": SOURCE, "fact": FACT,
                                    "value": t.value(), "quote": "", "source_url": "", "observed_at": now}])
    return t


def applied(store: Store, settings: Settings) -> Settings:
    """bootstrap: the sheet's settings with the latest automatic thresholds laid over them, while auto_thresholds is
    yes."""
    if not settings.general.auto_thresholds:
        return settings
    return with_thresholds(settings, stored(store))


def describe(value: Mapping[str, Any] | None) -> str:
    """One line for the daily post: what counts as top and good fit today, and how long the good-fit pool lasts."""
    if not value:
        return ""
    line = (f"Fit today (set automatically): top fit {value['priority']}+ points ({value['top_fit']:,} companies), "
            f"good fit {value['standard']}+ ({value['good_fit']:,}), about {value.get('weeks', 0)} weeks of sending")
    if value.get("short"):
        line += "; too few companies to fill the comparison group as well"
    return line
