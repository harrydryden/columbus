"""learn/stats.py: every function against a hand-computed fixture (W2-3; redteam-stats §5).

Each number below carries its derivation. The named mutations the plan asks for fail here: Wald in place of Wilson
(test_wilson_is_not_wald), the pooled z-test in place of Fisher (test_fisher_is_not_the_z_test).
"""

from __future__ import annotations

import math

import pytest

from us_outbound.learn import stats


def r3(t):
    return tuple(round(x, 3) for x in t)


# -- Wilson -----------------------------------------------------------------------------------------------------------


def test_wilson_38_of_48():
    # p = 38/48 = 0.79167, z = 1.96, z² = 3.8415, n = 48.
    # centre = (p + z²/2n) / (1 + z²/n) = (0.79167 + 0.04002) / 1.08003 = 0.77006
    # half = z·√(p(1−p)/n + z²/4n²) / (1 + z²/n) = 1.96·√(0.003436 + 0.000417) / 1.08003 = 1.96·0.06207/1.08003 = 0.11265
    # → (0.6574, 0.8827)
    assert r3(stats.wilson(38, 48)) == (0.657, 0.883)


def test_wilson_is_not_wald():
    # The Wald interval for 38 of 48: 0.79167 ± 1.96·√(0.79167·0.20833/48) = 0.79167 ± 0.11489 = (0.677, 0.907).
    wald = (38 / 48 - 1.96 * math.sqrt(38 / 48 * 10 / 48 / 48), 38 / 48 + 1.96 * math.sqrt(38 / 48 * 10 / 48 / 48))
    assert r3(wald) == (0.677, 0.907)
    assert r3(stats.wilson(38, 48)) != r3(wald)
    # At 0 of 47 Wald collapses to (0, 0); Wilson's upper bound is z²/(n + z²) = 3.8415/50.8415 = 0.0756.
    assert round(stats.wilson(0, 47)[1], 4) == 0.0756
    assert stats.wilson(0, 0) == (0.0, 0.0)


def test_label_sample_and_readout_use_the_one_wilson():
    from us_outbound.learn import readout
    from us_outbound.ops import label_sample

    assert label_sample.interval is stats.wilson
    assert readout.wilson is stats.wilson


# -- Jeffreys ---------------------------------------------------------------------------------------------------------


def test_jeffreys_0_of_47_reaches_past_5_percent():
    # k = 0: the lower bound is 0; the upper is the 0.975 quantile of Beta(0.5, 47.5). For small x,
    # I_x(½, b) ≈ 2·√x·Γ(b + ½)/(Γ(½)·Γ(b + 1)) ... solved numerically: 0.0520. So "< 0.05" would be wrong (RT §5).
    low, high = stats.jeffreys(0, 47)
    assert low == 0.0
    assert round(high, 3) == 0.052
    assert high > 0.05


def test_jeffreys_fixtures():
    # Beta(1.5, 19.5) quantiles 0.025 and 0.975; Beta(3.5, 147.5) likewise (Brown, Cai and DasGupta 2001, table 5
    # gives (0.005, 0.212) to 3 dp for 1 of 20 by their rounding; the 0.975 quantile is 0.2106).
    assert r3(stats.jeffreys(1, 20)) == (0.005, 0.211)
    assert r3(stats.jeffreys(3, 150)) == (0.006, 0.052)
    assert stats.jeffreys(5, 5)[1] == 1.0  # k = n: the upper bound is 1
    assert stats.jeffreys(0, 0) == (0.0, 1.0)


def test_beta_cdf_closed_forms():
    # Beta(1, 1) is uniform: I_x = x. Beta(2, 1): I_x = x². Beta(1, 2): I_x = 1 − (1 − x)².
    assert stats.beta_cdf(0.3, 1, 1) == pytest.approx(0.3)
    assert stats.beta_cdf(0.3, 2, 1) == pytest.approx(0.09)
    assert stats.beta_cdf(0.3, 1, 2) == pytest.approx(0.51)
    assert stats.beta_ppf(0.09, 2, 1) == pytest.approx(0.3, abs=1e-9)


# -- sample size ------------------------------------------------------------------------------------------------------


def test_n_per_arm_reply_3_against_6_percent():
    # p̄ = 0.045; √(2·0.045·0.955) = 0.29317; √(0.03·0.97 + 0.06·0.94) = √0.0855 = 0.29240; (Δ)² = 0.0009.
    # one-sided α 0.10 (z 1.2816), power 0.80 (z 0.8416): (1.2816·0.29317 + 0.8416·0.29240)² / 0.0009 = 0.38666/0.0009
    #   = 429.6 → 430
    # two-sided 0.10 (z 1.6449): (0.48223 + 0.24609)² / 0.0009 = 0.53045/0.0009 = 589.4 → 589
    # two-sided 0.05 (z 1.9600): (0.57461 + 0.24609)² / 0.0009 = 0.67355/0.0009 = 748.4 → 748
    n = stats.n_per_arm(0.03, 0.06, alpha=0.10, power=0.80, one_sided=True)
    assert abs(n - 430) <= 0.02 * 430
    assert stats.n_per_arm(0.03, 0.06, alpha=0.10, power=0.80) == 589
    assert stats.n_per_arm(0.03, 0.06, alpha=0.05, power=0.80) == 748


def test_n_per_arm_2_against_4_percent():
    # p̄ = 0.03; √(2·0.03·0.97) = 0.24125; √(0.0196 + 0.0384) = 0.24083; (1.96·0.24125 + 0.8416·0.24083)² / 0.0004
    #   = 0.67553² / 0.0004 = 1140.8 → 1141
    n = stats.n_per_arm(0.02, 0.04, 0.05, 0.80)
    assert abs(n - 1141) <= 0.02 * 1141
    with pytest.raises(ValueError):
        stats.n_per_arm(0.03, 0.03)


# -- Fisher -----------------------------------------------------------------------------------------------------------


def test_fisher_3_of_30_against_0_of_30():
    # Margins: 3 successes in 60, 30 a side. P(X = x) = C(30, x)·C(30, 3 − x)/C(60, 3), C(60, 3) = 34,220.
    # x = 0: 4,060/34,220 = 0.11864; x = 1: 30·435/34,220 = 0.38135; x = 2: the same; x = 3: 0.11864.
    # Two-sided: the tables at most as likely as x = 3 → x = 0 and x = 3 → 0.23729.
    assert round(stats.fisher_exact(3, 30, 0, 30), 3) == 0.237
    assert stats.fisher_exact(0, 30, 3, 30) == pytest.approx(stats.fisher_exact(3, 30, 0, 30))
    assert stats.fisher_exact(2, 20, 2, 20) == pytest.approx(1.0)
    assert stats.fisher_exact(1, 0, 0, 10) == 1.0


def test_fisher_is_not_the_z_test():
    # The pooled z-test signal_review.p_value used: p̄ = 3/60 = 0.05; se = √(0.05·0.95·(2/30)) = 0.05627;
    # z = 0.1/0.05627 = 1.777; p = erfc(1.777/√2) = 0.0756. A swap to it fails the 0.237 above.
    pooled = 3 / 60
    z = (3 / 30) / math.sqrt(pooled * (1 - pooled) * (2 / 30))
    assert round(math.erfc(z / math.sqrt(2)), 3) == 0.076
    assert round(stats.fisher_exact(3, 30, 0, 30), 3) != 0.076


# -- Beta posteriors --------------------------------------------------------------------------------------------------


def test_beta_posterior_gt_closed_form():
    # Uniform prior, 1 of 1 against 0 of 1: p1 ~ Beta(2, 1) (pdf 2x), p2 ~ Beta(1, 2) (cdf 1 − (1 − x)² = 2x − x²).
    # P(p1 > p2) = ∫ 2x·(2x − x²) dx = 4/3 − 2/4 = 5/6 = 0.8333.
    assert stats.beta_posterior_gt(1, 1, 0, 1, prior=(1, 1)) == pytest.approx(5 / 6, abs=1e-6)
    # Equal data: 0.5 by symmetry, with the Jeffreys prior's end singularities too.
    assert stats.beta_posterior_gt(5, 40, 5, 40) == pytest.approx(0.5, abs=1e-6)
    assert stats.beta_posterior_gt(0, 0, 0, 0) == pytest.approx(0.5, abs=1e-6)
    # The other way round is the complement.
    a = stats.beta_posterior_gt(3, 30, 0, 30)
    assert a + stats.beta_posterior_gt(0, 30, 3, 30) == pytest.approx(1.0, abs=1e-6)
    assert a > 0.95


def test_sequential_warning_fixtures():
    # P(rate < 0.0025 | Beta(½ + k, ½ + n − k)) = I_0.0025(½ + k, ½ + n − k), Jeffreys prior. For k = 0 it is
    # ≈ erf(√(n·r0)) (the Beta(½, n) tail is a χ² with one degree of freedom): erf(√1) = 0.843, erf(√1.5) = 0.917,
    # erf(√2) = 0.955. For k = 1, n = 800 the Beta(1.5, 799.5) cdf at 0.0025 is 0.739.
    assert round(stats.sequential_warning(0, 400, 0.0025), 3) == 0.843
    assert round(stats.sequential_warning(0, 600, 0.0025), 3) == 0.917
    assert round(stats.sequential_warning(0, 800, 0.0025), 3) == 0.955
    assert round(stats.sequential_warning(1, 800, 0.0025), 3) == 0.739
    assert round(math.erf(1.0), 3) == 0.843  # the approximation the comment uses


# -- O'Brien-Fleming --------------------------------------------------------------------------------------------------


def test_obrien_fleming_one_look_is_alpha():
    assert stats.obrien_fleming(1, 0.10) == pytest.approx((0.10,))


def test_obrien_fleming_two_looks_at_010():
    # Look 1 (t = ½): each side spends 2 − 2Φ(z_0.975/√½) = 2 − 2Φ(2.7718) = 2·0.002788 = 0.005575, so the nominal
    # two-sided p is 0.01115 (z 2.538). Look 2 spends the rest of the 0.05 a side; with the correlation √½ between the
    # looks the final boundary is z 1.662, a nominal two-sided p of 0.0965 (Lan-DeMets tables: 2.538, 1.662; at
    # α 0.05 they give 2.963, 1.969, checked below). The plan's "≈ (0.005, 0.095)" is the first look's one-sided
    # spend and the last look's two-sided p.
    first, last = stats.obrien_fleming(2, 0.10)
    assert round(first / 2, 4) == 0.0056
    assert round(first, 4) == 0.0111
    assert round(last, 3) == 0.096
    from statistics import NormalDist

    z = [NormalDist().inv_cdf(1 - p / 2) for p in stats.obrien_fleming(2, 0.05)]
    assert [round(x, 2) for x in z] == [2.96, 1.97]
    with pytest.raises(ValueError):
        stats.obrien_fleming(0)


def test_min_n_ok():
    assert stats.min_n_ok({"GA": 15, "NY": 22}, 15)
    assert not stats.min_n_ok({"GA": 14, "NY": 22}, 15)
    assert stats.min_n_ok([15, 15])
    assert not stats.min_n_ok({})
