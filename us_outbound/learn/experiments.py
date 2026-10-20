"""The experiment registry and the explore-arm table (W3-4, 21 Oct 2026; design-final §5 L8, L10; metrics-audit M6, M7).

Three randomisations shared about 25 enrolments a day and none could be read (the opener holdout, email 1's subject
split and the Tests tab's copy test). General live_split now names the one that draws arms (settings/model.LIVE_SPLITS;
both, the default, keeps today's three until Harry's D8), and the Tests tab is each test's pre-registration
(model.PRE_REGISTRATION). This module says, for the Monday readout, where each split stands; it reads and decides
nothing:

  registry(ctx)   one Entry per split drawing arms (and per running Tests row that is not drawing, so a paused test
                  is seen): its pre-registration (the Tests row's, or the defaults for a split with no row), the planned
                  companies per arm (stats.n_per_arm at the metric's EFFECTS and the test's alpha, two-sided, 80%
                  power: 589 a side for 3% → 6% at 0.10), how many each arm has reached (step 1 delivered: the
                  companies a read counts, learn/looks.arms) and how many have matured at 28 days, the trailing 4-week
                  rate into the split, and the date it is projected readable: today + the weeks to reach the planned
                  total at that rate + the 4 weeks the last company needs to mature (projected()).
  arms_table(ctx) O2-O4 by explore arm (random against ranked: the scoring lift, L8) with Jeffreys intervals and n,
                  the realised explore share by step-1 week (the share drawn is a target; the realised one is what the
                  arm compares against), and no verdict until the random arm has ARMS_READ_AT (300) companies matured
                  at 28 days; then the one pre-registered read: P(ranked > random) on O4 from Beta posteriors
                  (stats.beta_posterior_gt), beside READ_AT (0.95), with O3 printed and not decided on. The readout
                  prints the A/A sham arm under it (learn/readout.sham_line).

The pipeline cannot read a 3% against 6% difference before about 589 matured a side (design-final §2.3), so every
line here carries its n, and the dates are projections, not promises.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from us_outbound import fmt
from us_outbound.context import Context
from us_outbound.learn import looks, outcomes, stats
from us_outbound.learn.outcomes import Outcome
from us_outbound.settings.model import (
    HOLDOUT_TEST,
    SPLIT_BOTH,
    SPLIT_NONE,
    SPLIT_OPENER,
    SPLIT_SUBJECT,
    SPLIT_TEST,
    Test,
)

# The effect each metric is powered for, (base rate, rate to detect): O4 3% → 6% (design-final §2.3: 589 a side at
# two-sided 0.10); O3 1% → 2% (positive replies are a third of human ones in SPEC 12's terms: 1,826 a side).
EFFECTS = {"O4": (0.03, 0.06), "O3": (0.01, 0.02)}
POWER = 0.80
MATURE_WEEKS = 4  # the 28-day reply window after the last company's step 1
RATE_DAYS = 28  # the trailing window of the rate into the split
ARMS_READ_AT = 300  # random-arm companies matured at 28 days before the explore arm is read once (L8)
READ_AT = 0.95  # the pre-registered read: P(ranked > random) on O4 above this
MIN_CELL = 15  # an arm's interval prints once each arm has this many matured (RT §1.5)
METRIC_WORDS = {"O4": "any human reply within 28 days", "O3": "a positive or referral reply within 28 days"}
SPLIT_WORDS = {SPLIT_OPENER: "the opener holdout", SPLIT_SUBJECT: "email 1's subject split"}
KIND_WORDS = {"ab": "A/B copy test", "variant": "one-change variant test", HOLDOUT_TEST: "holdout"}
SHARE_WEEKS = 4  # step-1 weeks shown in the realised-share line


@dataclass(frozen=True)
class Entry:
    split: str  # the live_split that draws it (opener, subject, test)
    name: str  # the Tests row's test_id, or the split's words when there is no row
    test: Test
    drawing: bool  # its arms draw for new contacts now
    planned: int  # companies per arm (the smaller arm, when uneven)
    min_share: float  # the smaller arm's share
    reached: dict[str, int] = field(default_factory=dict)  # arm -> step 1 delivered
    matured: dict[str, int] = field(default_factory=dict)  # arm -> 28-day window closed
    weekly: float = 0.0  # companies a week into the split, the last RATE_DAYS
    projected: date | None = None

    @property
    def row(self) -> bool:
        """Pre-registered on the Tests tab (a row with every field)."""
        return self.test.pre_registered


def planned_per_arm(test: Test) -> int:
    """stats.n_per_arm at the metric's EFFECTS and the test's total two-sided alpha, 80% power."""
    p1, p2 = EFFECTS[test.metric]
    return stats.n_per_arm(p1, p2, alpha=test.total_alpha, power=POWER)


def projected(today: date, planned: int, min_share: float, reached: int, weekly: float) -> date | None:
    """When the split is readable: the weeks to reach planned ÷ min_share companies in all (589 a side is 1,178 at
    50/50) at `weekly`, rounded up, plus MATURE_WEEKS for the last to mature. None when nothing is coming in."""
    needed = math.ceil(planned / min_share)
    if reached >= needed:
        return today + timedelta(weeks=MATURE_WEEKS)
    if weekly <= 0:
        return None
    return today + timedelta(weeks=math.ceil((needed - reached) / weekly) + MATURE_WEEKS)


def _split_test(ctx: Context, split: str) -> Test:
    """The opener holdout or the subject split as a holdout Test with the default pre-registration, for a split with no
    running Tests row (looks.arms reads its arms from contacts.opener_arm or subject_arm)."""
    g = ctx.settings.general
    if split == SPLIT_OPENER:  # version_a opener takes 1 − opener_holdout_share
        return Test(SPLIT_WORDS[split], "", "opener", "holdout", 0, "running", kind=HOLDOUT_TEST,
                    share_a=1 - g.opener_holdout_share)
    return Test(SPLIT_WORDS[split], "", "personal", "copy", 0, "running", kind=HOLDOUT_TEST,
                share_a=g.email1_subject_share)


def _min_share(ctx: Context, test: Test) -> float:
    share = test.share_a
    if test.kind == HOLDOUT_TEST and test.version_a.casefold() in ("opener", "holdout"):
        held = ctx.settings.general.opener_holdout_share
        share = held if test.version_a.casefold() == "holdout" else 1 - held
    elif test.kind == HOLDOUT_TEST:
        personal = ctx.settings.general.email1_subject_share
        share = personal if test.version_a.casefold() == "personal" else 1 - personal
    return max(min(share, 1 - share), 0.01)


def entry(ctx: Context, split: str, test: Test, drawing: bool, name: str = "") -> Entry:
    by_arm = looks.arms(ctx, test)
    now = ctx.now
    reached = {a: len(arm.emailed) for a, arm in by_arm.items()}
    matured = {a: len(arm.closed(now)) for a, arm in by_arm.items()}
    step1s = sorted(o.step1_at for arm in by_arm.values() for o in arm.emailed)
    weekly = 0.0
    if step1s:
        days = min(RATE_DAYS, max(1.0, (now - step1s[0]).total_seconds() / 86400))
        recent = sum(t >= now - timedelta(days=RATE_DAYS) for t in step1s)
        weekly = recent / (days / 7)
    planned, min_share = planned_per_arm(test), _min_share(ctx, test)
    return Entry(split, name or test.test_id, test, drawing, planned, min_share, reached, matured, round(weekly, 1),
                 projected(ctx.today_uk(), planned, min_share, sum(reached.values()), weekly))


def registry(ctx: Context) -> list[Entry]:
    """The splits drawing arms now (General live_split), each with where it stands; then any running Tests row that
    is not drawing (its split is not named), so a paused test stays in sight."""
    s = ctx.settings
    split = s.general.live_split
    running = [t for t in s.tests if t.status == "running"]
    out: list[Entry] = []
    used: set[str] = set()
    for name in (SPLIT_OPENER, SPLIT_SUBJECT):
        if split not in (SPLIT_BOTH, name):
            continue
        row = next((t for t in running if t.kind == HOLDOUT_TEST and t.split() == name), None)
        if row is not None:
            used.add(row.test_id)
        out.append(entry(ctx, name, row or _split_test(ctx, name), True, row.test_id if row else SPLIT_WORDS[name]))
    copy_test = s.drawn_test()
    if copy_test is not None:
        used.add(copy_test.test_id)
        out.append(entry(ctx, SPLIT_TEST, copy_test, True))
    for t in running:
        if t.test_id not in used:
            out.append(entry(ctx, t.split(), t, False))
    return out


def _looks_text(t: Test) -> str:
    points = [str(x) for x in t.looks] + ([f"read {t.read_date.isoformat()}"] if t.read_date else [])
    return "; ".join(points) or "no looks set"


def registry_lines(ctx: Context) -> list[str]:
    """The readout's registry block: the split in force, then one line per Entry, with its pre-registration fields,
    planned n, reached and matured per arm, rate and projected date."""
    split = ctx.settings.general.live_split
    entries = registry(ctx)
    head = (f"  General live_split = {split}: " + {
        SPLIT_BOTH: "the opener holdout, the subject split and the running copy test all draw arms (as before; "
                    "one experiment once D8 names it)",
        SPLIT_NONE: "no split draws arms",
    }.get(split, f"only {SPLIT_WORDS.get(split, 'the Tests tab test')} draws arms") + ".")
    out = [head]
    if not entries:
        out.append("  No split is drawing arms and no test is running.")
    for e in entries:
        t = e.test
        kind = KIND_WORDS.get(t.kind, t.kind) if t.test_id else SPLIT_WORDS.get(e.split, e.split)
        state = "drawing arms" if e.drawing else f"paused: General live_split is {split}, not {e.split}"
        reg = (f"{t.metric} ({METRIC_WORDS[t.metric]}) · {t.method} at alpha {t.total_alpha:.2f} · looks "
               f"{_looks_text(t)} ({t.look_rule}) · definition {t.definition_version}") if e.row else (
            f"not pre-registered on the Tests tab: read as {t.metric} ({METRIC_WORDS[t.metric]}), {t.method} at alpha "
            f"{t.total_alpha:.2f}, {t.look_rule} over looks {_looks_text(t)}")
        p1, p2 = EFFECTS[t.metric]
        arms = ", ".join(f"{a} {n:,}" for a, n in e.reached.items())
        ripe = ", ".join(f"{a} {n:,}" for a, n in e.matured.items())
        when = (f"readable about {e.projected:%a %d %b %Y} at {e.weekly:g} a week" if e.projected
                else "no company is coming in, so no date")
        out.append(f"  {e.name} ({kind}; {state}): {reg}. Planned {e.planned:,} a side to tell {p1:.0%} from "
                   f"{p2:.0%}; reached {arms or 'none'}; matured at 28 days {ripe or 'none'}; {when}.")
    return out


# -- the explore arm against ranked (L8) -------------------------------------------------------------------------------


def _cell(k: int, n: int) -> str:
    """"5.0% (1 of 20; 0.5–21.1%)": the rate, its counts and its Jeffreys 95% interval."""
    low, high = stats.jeffreys(k, n)
    return f"{k / n:.1%} ({k:,} of {n:,}; {low * 100:.1f}–{high * 100:.1f}%)"


def arm_of(o: Outcome) -> str:
    return o.stamp("arm", "ranked")  # NULL reads as ranked (enrol/queue.py)


def arms_table(ctx: Context, found: Sequence[Outcome] | None = None) -> tuple[list[str], dict[str, Any]]:
    """(the readout lines, the numbers): O2-O4 by arm, the realised share, and the one read once it is due (the module
    docstring). Every interval is a Jeffreys 95% one."""
    found = list(outcomes.per_company(ctx) if found is None else found)
    by_arm: dict[str, list[Outcome]] = {"random": [], "ranked": []}
    for o in found:
        by_arm.setdefault(arm_of(o), []).append(o)
    m28 = {a: [o for o in g if o.window_closed_28] for a, g in by_arm.items()}
    m45 = {a: [o for o in g if o.window_closed_45] for a, g in by_arm.items()}
    measures = (("O2 engaged within 45 days", m45, lambda o: o.engaged),
                ("O3 positive within 28 days", m28, lambda o: o.positive_in_window),
                ("O4 any human reply within 28 days", m28, lambda o: o.replied_in_window))
    out = ["  Explore arm against ranked (each company once, intent to treat; Jeffreys intervals at 95%, rate (k of n; "
           "interval)):"]
    intervals = 0
    for label, matured, hit in measures:
        ns = {a: len(matured[a]) for a in ("random", "ranked")}
        ks = {a: sum(hit(o) for o in matured[a]) for a in ("random", "ranked")}
        if min(ns.values()) >= MIN_CELL:
            out.append(f"    {label}: random {_cell(ks['random'], ns['random'])}, ranked "
                       f"{_cell(ks['ranked'], ns['ranked'])}.")
            intervals += 2
        else:
            out.append(f"    {label}: random {ks['random']} of {ns['random']}, ranked {ks['ranked']} of "
                       f"{ns['ranked']} (intervals once each arm has {MIN_CELL} matured).")
    random_n, total = len(by_arm["random"]), len(found)
    weeks = Counter()
    drawn = Counter()
    from us_outbound.learn import metrics  # metrics imports learn modules; here only when used

    for o in found:
        w = metrics.week_of(o.step1_at)
        weeks[w] += 1
        drawn[w] += arm_of(o) == "random"
    recent = sorted(weeks)[-SHARE_WEEKS:]
    by_week = "; ".join(f"{w} {drawn[w]} of {weeks[w]} ({fmt.pct(drawn[w], weeks[w])})" for w in recent)
    out.append(f"    Realised explore share: {random_n:,} of {fmt.plural(total, 'company', 'companies')} contacted "
               f"({fmt.pct(random_n, total)}; the target is General explore_share "
               f"{ctx.settings.general.explore_share:g})" + (f"; by step-1 week {by_week}." if by_week else "."))
    ripe = len(m28["random"])
    nums: dict[str, Any] = {"random_matured_28": ripe, "intervals": intervals, "read": None,
                            "realised_share": round(random_n / total, 4) if total else None}
    if ripe < ARMS_READ_AT:
        out.append(f"    No read until the random arm has {ARMS_READ_AT} companies matured at 28 days (it has "
                   f"{ripe:,}): monitoring only.")
        return out, nums
    k_ranked = sum(o.replied_in_window for o in m28["ranked"])
    k_random = sum(o.replied_in_window for o in m28["random"])
    p = stats.beta_posterior_gt(k_ranked, len(m28["ranked"]), k_random, ripe)
    nums["read"] = round(p, 3)
    out.append(f"    The pre-registered read (O4, any human reply within 28 days; once): P(ranked > random) = {p:.3f}, "
               f"against {READ_AT} (ranked {k_ranked:,} of {len(m28['ranked']):,}, random {k_random:,} of {ripe:,}); "
               "O3 is printed above and not decided on.")
    return out, nums
