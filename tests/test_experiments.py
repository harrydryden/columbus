"""The experiment registry and the explore-arm table (learn/experiments.py; W3-4, 21 Oct 2026; design-final §5 L8, L10).

Each expected number is derived in its test from the definitions, not read off the code's output.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

from tests.fakes import make_context
from tests.test_registry import SETTINGS
from us_outbound.learn import experiments, readout, stats
from us_outbound.settings.model import Test as CopyTest  # aliased so pytest does not collect it

NOW = datetime(2026, 12, 7, 9, tzinfo=UTC)  # a Monday
OLD = NOW - timedelta(days=60)  # matured at 28 and 45 days


def ctx_with(**general):
    s = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, **general))
    return make_context(s, now=NOW)


def company(ctx, i: int, *, at: datetime = OLD, reply: str | None = None, **contact):
    aid, cid = f"a{i}", f"k{i}"
    ctx.store.insert("contacts", [{"contact_id": cid, "account_id": aid, "enrolled_at": at - timedelta(hours=2),
                                   **contact}])
    events = [{"event_id": f"s{i}", "type": "sent", "step": 1, "account_id": aid, "contact_id": cid, "occurred_at": at}]
    if reply:
        events.append({"event_id": f"r{i}", "type": "replied", "account_id": aid, "contact_id": cid,
                       "reply_class": reply, "occurred_at": at + timedelta(days=2)})
    ctx.store.insert("events", events)


# -- the registry ------------------------------------------------------------------------------------------------------


def test_planned_n_and_the_projected_date():
    """3% → 6% at two-sided alpha 0.10 and 80% power: (z_0.95 √(2 p̄ q̄) + z_0.80 √(p1 q1 + p2 q2))² ÷ 0.03², with
    p̄ = 0.045: √(2 × 0.045 × 0.955) = 0.293172 and √(0.03 × 0.97 + 0.06 × 0.94) = 0.292404, so (1.644854 × 0.293172 +
    0.841621 × 0.292404)² ÷ 0.0009 = (0.482225 + 0.246094)² ÷ 0.0009 = 0.530449 ÷ 0.0009 = 589.4: 589. At 125 a week with 200 reached of the
    1,178 needed (589 a side at 50/50): ceil((1,178 − 200) ÷ 125) = ceil(7.82) = 8 weeks, plus 4 to mature: 12."""
    planned = experiments.planned_per_arm(CopyTest("t", "", "a", "b", 0, "running", metric_id="O4", alpha=0.10))
    assert abs(planned - 589) <= 0.02 * 589 and planned == stats.n_per_arm(0.03, 0.06, alpha=0.10, power=0.80)
    today = date(2026, 10, 26)
    assert experiments.projected(today, 589, 0.5, 200, 125) == today + timedelta(weeks=12)
    assert experiments.projected(today, 589, 0.5, 1178, 0) == today + timedelta(weeks=4)  # reached: maturity only
    assert experiments.projected(today, 589, 0.5, 200, 0) is None  # nothing coming in
    # An uneven 70/30 split needs 589 in the smaller arm: 589 ÷ 0.3 = 1,963.3, so 1,964 in all.
    assert experiments.projected(today, 589, 0.3, 1964, 10) == today + timedelta(weeks=4)
    assert experiments.projected(today, 589, 0.3, 1963, 10) == today + timedelta(weeks=5)


def test_live_split_both_lists_the_three_splits_drawing_and_a_paused_test_stays_in_sight():
    """both (the default): the opener holdout, the subject split and the running copy test all draw. opener: the
    holdout alone, and warm-intro-70, still running, is listed as paused."""
    warm = CopyTest("warm-intro-70", "", "warm intro", "no intro", 290, "running", start_date=date(2026, 10, 12),
                    read_date=date(2027, 4, 26), kind="variant", looks=(145, 290), share_a=0.7)
    ctx = ctx_with()
    ctx.settings = dataclasses.replace(ctx.settings, tests=(warm,))
    got = [(e.split, e.name, e.drawing) for e in experiments.registry(ctx)]
    assert got == [("opener", "the opener holdout", True), ("subject", "email 1's subject split", True),
                   ("test", "warm-intro-70", True)]
    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general,
                                                                                 live_split="opener"))
    got = [(e.split, e.name, e.drawing) for e in experiments.registry(ctx)]
    assert got == [("opener", "the opener holdout", True), ("test", "warm-intro-70", False)]
    lines = experiments.registry_lines(ctx)
    assert lines[0] == "  General live_split = opener: only the opener holdout draws arms."
    assert "warm-intro-70 (one-change variant test; paused: General live_split is opener, not test)" in lines[2]
    assert ("not pre-registered on the Tests tab: read as O4 (any human reply within 28 days), fisher_exact at alpha "
            "0.10, obrien_fleming over looks 145; 290; read 2027-04-26") in lines[2]
    ctx.settings = dataclasses.replace(ctx.settings, tests=(), general=dataclasses.replace(ctx.settings.general,
                                                                                         live_split="none"))
    assert experiments.registry_lines(ctx) == ["  General live_split = none: no split draws arms.",
                                               "  No split is drawing arms and no test is running."]


def test_a_test_started_from_21_oct_under_another_split_is_shown_paused_or_not_alone():
    """W3-4 r1: a mismatch is no validation error, so the registry says it. A pre-registered ab test started 26 Oct
    under live_split = both draws no arms (Settings.drawn_test): paused. A pre-registered opener holdout under both
    still has its arms drawn (the opener holdout draws under both), beside the subject split: not alone. Under
    live_split = opener, the holdout is the one split: drawing arms, nothing more."""
    prereg = {"metric_id": "O4", "definition_version": "2026-10-20", "test": "fisher_exact", "alpha": 0.10,
              "look_thresholds": "one_look"}
    ab = CopyTest("short-email-1", "", "agencies-v1", "agencies-v2", 589, "running", start_date=date(2026, 10, 26),
                  read_date=date(2027, 4, 26), **prereg)
    ctx = ctx_with()
    ctx.settings = dataclasses.replace(ctx.settings, tests=(ab,))
    assert ctx.settings.drawn_test() is None
    got = [(e.split, e.name, e.drawing) for e in experiments.registry(ctx)]
    assert got == [("opener", "the opener holdout", True), ("subject", "email 1's subject split", True),
                   ("test", "short-email-1", False)]
    assert "short-email-1 (A/B copy test; paused: General live_split is both, not test)" in \
        experiments.registry_lines(ctx)[3]
    holdout = CopyTest("opener-holdout", "", "opener", "holdout", 589, "running", start_date=date(2026, 10, 26),
                       read_date=date(2027, 4, 26), kind="holdout", **prereg)
    ctx.settings = dataclasses.replace(ctx.settings, tests=(holdout,))
    lines = experiments.registry_lines(ctx)
    assert ("opener-holdout (holdout; drawing arms, but not alone: General live_split is both, not opener)"
            in lines[1])
    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general,
                                                                                 live_split="opener"))
    assert "opener-holdout (holdout; drawing arms)" in experiments.registry_lines(ctx)[1]
    # W3-4 r2: the ab test under its own split, test, but without its pre-registration (the version in force read
    # leniently): no arms, and the line says it is the row, not the split.
    bare = dataclasses.replace(ab, **{k: "" for k in prereg if k != "alpha"}, alpha=None)
    ctx.settings = dataclasses.replace(ctx.settings, tests=(bare,), general=dataclasses.replace(
        ctx.settings.general, live_split="test"))
    assert ctx.settings.drawn_test() is None
    assert [(e.name, e.drawing) for e in experiments.registry(ctx)] == [("short-email-1", False)]
    assert ("short-email-1 (A/B copy test; paused: its row needs its pre-registration fields on the Tests tab): not "
            "pre-registered on the Tests tab") in experiments.registry_lines(ctx)[1]


def test_a_registered_test_prints_its_fields_reached_matured_and_a_date():
    """A pre-registered ab test, live_split = test. 60 companies in it: 20 a side matured (step 1 60 days ago) and 10
    a side sent in the last 14 days, so the rate into it is 20 companies in the last 28 days over min(28, 60) days =
    20 ÷ 4 weeks = 5 a week. Reached 30 a side (60), needed 589 ÷ 0.5 = 1,178: ceil(1,118 ÷ 5) = 224 weeks + 4 = 228
    weeks from Mon 7 Dec 2026: Mon 21 Apr 2031 (which says what this volume means)."""
    test = CopyTest("t1", "h", "eap-v1", "general-v1", 400, "running", start_date=date(2026, 10, 26),
                    read_date=date(2027, 3, 1), metric_id="O4", definition_version="2026-10-20", test="fisher_exact",
                    alpha=0.10, look_thresholds="one_look")
    ctx = ctx_with(live_split="test")
    ctx.settings = dataclasses.replace(ctx.settings, tests=(test,))
    for i in range(60):
        version = "eap-v1" if i % 2 == 0 else "general-v1"
        company(ctx, i, at=OLD if i < 40 else NOW - timedelta(days=14), test_id="t1", copy_version=version)
    (e,) = experiments.registry(ctx)
    assert (e.reached, e.matured, e.weekly) == ({"eap-v1": 30, "general-v1": 30}, {"eap-v1": 20, "general-v1": 20},
                                                5.0)
    assert e.projected == date(2026, 12, 7) + timedelta(weeks=228) == date(2031, 4, 21)
    line = experiments.registry_lines(ctx)[1]
    assert line == ("  t1 (A/B copy test; drawing arms): O4 (any human reply within 28 days) · fisher_exact at alpha "
                    "0.10 · looks read 2027-03-01 (one_look) · definition 2026-10-20. Planned 589 a side to tell 3% "
                    "from 6%; reached eap-v1 30, general-v1 30; matured at 28 days eap-v1 20, general-v1 20; readable "
                    "about Mon 21 Apr 2031 at 5 a week.")


# -- the explore arm against ranked -----------------------------------------------------------------------------------


def test_the_arms_table_before_the_read():
    """Random 20 matured with 1 positive, ranked 150 with 3. Jeffreys 95%: Beta(1.5, 19.5) → 0.5–21.1%; Beta(3.5,
    147.5) → 0.6–5.2% (stats.jeffreys(1, 20) = (0.0054, 0.2108), (3, 150) = (0.0057, 0.0524)). Realised share 20 of
    170 = 11.8%. Under 300 random matured: no read."""
    ctx = ctx_with()
    for i in range(170):
        arm = "random" if i < 20 else "ranked"
        positive = i == 0 or 20 <= i < 23
        company(ctx, i, arm=arm, reply="positive" if positive else None)
    lines, nums = experiments.arms_table(ctx)
    assert "    O3 positive within 28 days: random 5.0% (1 of 20; 0.5–21.1%), ranked 2.0% (3 of 150; 0.6–5.2%)." in lines
    assert any(x.startswith("    Realised explore share: 20 of 170 companies contacted (11.8%;") for x in lines)
    assert lines[-1] == ("    No read until the random arm has 300 companies matured at 28 days (it has 20): "
                         "monitoring only.")
    assert nums["read"] is None and nums["realised_share"] == 0.1176 and nums["intervals"] == 6
    # The readout prints it in the splits with the sham arm under it.
    splits = readout.split_lines(ctx, readout.outcomes.per_company(ctx))
    assert any("O3 positive within 28 days: random 5.0%" in x for x in splits)
    assert any("A/A check (the sham arm" in x for x in splits)


def test_the_one_pre_registered_read_at_300_random_matured():
    """Random 300 and ranked 1,700 matured, human replies 9 (3%) and 102 (6%). P(ranked > random) from Jeffreys
    posteriors Beta(102.5, 1,598.5) against Beta(9.5, 291.5): 0.987 by numerical integration (stats.beta_posterior_gt),
    printed once against 0.95; O3 is printed above it and not decided on.
    W3-4 r1: the read is made once, by metrics_daily the first day it is due, stored in the metrics table, and printed
    from there: before that the readout says it is due and prints no P; after it, 300 more random companies with
    replies change nothing printed as the read, and metrics_daily stores no second read."""
    from us_outbound.learn import metrics

    ctx = ctx_with()
    for i in range(2000):
        random_ = i < 300
        human = (i < 9) if random_ else (300 <= i < 402)
        company(ctx, i, arm="random" if random_ else "ranked", reply="objection" if human else None)
    lines, nums = experiments.arms_table(ctx)
    assert nums["read"] is None and not any("P(ranked > random)" in x for x in lines)
    assert lines[-1].startswith("    The random arm has 300 companies matured at 28 days: the one read is made by "
                                "metrics_daily")
    ctx.job = metrics.JOB
    metrics.run(ctx)
    stored = [r for r in ctx.store.select("metrics") if r["metric"] == "explore_read"]
    assert sorted((r["segment_key"], r["k"], r["n"], r["value"]) for r in stored) == [
        ("arm=random", 9, 300, 0.9868), ("arm=ranked", 102, 1700, 0.9868)]
    lines, nums = experiments.arms_table(ctx)
    assert nums["read"] == 0.987
    assert lines[-1] == ("    The pre-registered read (O4, any human reply within 28 days; made once, on 06 Dec 2026, at "
                         "300 random-arm companies matured): P(ranked > random) = 0.987, against 0.95 (ranked 102 of "
                         "1,700, random 9 of 300); the numbers above are monitoring since, and O3 is not decided on.")
    assert sum("P(ranked > random)" in x for x in lines) == 1
    for i in range(2000, 2300):  # 300 more random companies, a third of them replying
        company(ctx, i, arm="random", reply="objection" if i % 3 == 0 else None)
    metrics.run(dataclasses.replace(ctx, now=NOW + timedelta(days=1)))
    assert len([r for r in ctx.store.select("metrics") if r["metric"] == "explore_read"]) == 2  # no second read
    assert experiments.arms_table(ctx)[1]["read"] == 0.987


def test_the_arms_compare_only_the_step_1_weeks_with_random_companies():
    """W3-4 r1: 20 random and 30 ranked companies in one week, and 100 ranked a month earlier (before the arm, as
    the companies contacted before W1-6 were) with 10 replies. The table compares 20 against 30, the same weeks; the
    earlier 100 are in the realised share (20 of 150, 13.3%) but not in the comparison."""
    ctx = ctx_with()
    for i in range(50):
        company(ctx, i, arm="random" if i < 20 else "ranked", reply="objection" if i in (0, 20) else None)
    for i in range(50, 150):
        company(ctx, i, at=OLD - timedelta(days=28), arm="ranked", reply="objection" if i < 60 else None)
    lines, _ = experiments.arms_table(ctx)
    assert ("    O4 any human reply within 28 days: random 5.0% (1 of 20; 0.5–21.1%), ranked 3.3% (1 of 30; "
            f"{stats.jeffreys(1, 30)[0] * 100:.1f}–{stats.jeffreys(1, 30)[1] * 100:.1f}%).") in lines
    assert any(x.startswith("    Realised explore share: 20 of 150 companies contacted (13.3%;") for x in lines)
    assert len(experiments.concurrent(experiments.outcomes.per_company(ctx))) == 50


def test_the_readout_prints_the_registry_with_a_date_and_no_n_of_1500_advice():
    """Acceptance (W3-4): the Monday readout's Tests section carries the registry line with its pre-registration
    fields and a projected date, and no line reads "N of 1,500" as advice (the stop rule is printed as a brake)."""
    import re

    test = CopyTest("t1", "h", "eap-v1", "general-v1", 400, "running", start_date=date(2026, 10, 26),
                    read_date=date(2027, 3, 1), metric_id="O4", definition_version="2026-10-20", test="fisher_exact",
                    alpha=0.10, look_thresholds="one_look")
    ctx = ctx_with(live_split="test")
    ctx.settings = dataclasses.replace(ctx.settings, tests=(test,))
    for i in range(40):
        company(ctx, i, at=NOW - timedelta(days=10), test_id="t1", copy_version="eap-v1" if i % 2 else "general-v1")
    lines = readout.build(ctx)[0]
    tests = lines[lines.index(next(x for x in lines if x.startswith("*Tests*"))):]
    assert tests[1] == "  General live_split = test: only the Tests tab test draws arms."
    assert tests[2].startswith("  t1 (A/B copy test; drawing arms): O4 (any human reply within 28 days) · fisher_exact")
    assert "readable about Mon " in tests[2]
    assert not [x for x in lines if re.search(r"\d of 1,500", x)]


def test_acceptance_the_decisions_log_and_no_p_value():
    """W3-4's acceptance: docs/decisions.md exists with at least 12 dated entries (the settled CLAUDE.md decisions,
    the 14 Oct EAP/absenteeism note and the 6 Oct bookings ruling among them), and `grep p_value us_outbound` finds
    nothing (the pooled z-test is gone; reads are exact)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    log = (root / "docs" / "decisions.md").read_text()
    dated = re.findall(r"^\| (\d{1,2} [A-Z][a-z]{2} 2026) \|", log, re.M)
    assert len(dated) >= 12
    for words in ("Productivity/absenteeism", "Bookings count", "No postal address", "Rank + 15% random arm"):
        assert words in log, words
    hits = [p for p in (root / "us_outbound").rglob("*.py") if "p_value" in p.read_text()]
    assert hits == []
