"""The statistics every report and test read uses, in one place (W2-3, 20 Oct 2026; redteam-stats §3, §5).

Before this, a pooled two-proportion z-test (signal_review's, now gone) decided copy tests, cohort pairs and signal
verdicts. At reply rates of 0–5% it is anti-conservative: 3 of 30 against 0 of 30 gave p = 0.076, where Fisher's exact
test gives 0.237. Every rate the system prints now carries an interval from here, and a decision is taken only at a
pre-registered look with an exact test (learn/looks.py).

  wilson(k, n)                 Wilson score 95% interval (the hand-check and label sample precision, T1 and T3).
  jeffreys(k, n)               Jeffreys 95% interval: the equal-tailed Beta(k + ½, n − k + ½) quantiles, with the lower
                               bound 0 at k = 0 and the upper 1 at k = n (Brown, Cai and DasGupta 2001). For outcome rates.
  n_per_arm(p1, p2, ...)       companies per arm to tell p1 from p2 (normal approximation, pooled variance under H0),
                               rounded to the nearest whole company.
  fisher_exact(k1, n1, k2, n2) two-sided Fisher exact p: the tables with the same margins at most as likely as the one
                               seen.
  beta_posterior_gt(...)       P(rate 1 > rate 2) with independent Beta posteriors, by numerical integration.
  sequential_warning(k, n, r0) P(rate < r0 | Beta(½ + k, ½ + n − k)): the early warning (W3-4), e.g. a meeting rate
                               under 0.25%.
  obrien_fleming(looks, alpha) the nominal two-sided p each of `looks` equally spaced looks must beat so the test's
                               total false-positive rate is alpha (Lan-DeMets O'Brien-Fleming spending).
  min_n_ok(cells, minimum)     every cell has at least `minimum` (15): a split is printed only then (RT §1.5).

Pure Python (math and statistics.NormalDist): no scipy in the image. Each function has a hand-computed fixture in
tests/test_stats.py, and a Wald interval or the z-test swapped in fails it.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Iterable, Mapping
from statistics import NormalDist

Z95 = 1.959963984540054  # NormalDist().inv_cdf(0.975)
LEVEL = 0.95
_N = NormalDist()
_EPS = 1e-12
_TINY = 1e-300


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for k of n; (0, 0) for n = 0."""
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, mid - half), min(1.0, mid + half)


# -- the Beta distribution ---------------------------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function (modified Lentz; Numerical Recipes §6.4)."""
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > _TINY else _TINY)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > _TINY else _TINY)
        c = 1 + aa / c
        c = c if abs(c) > _TINY else _TINY
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > _TINY else _TINY)
        c = 1 + aa / c
        c = c if abs(c) > _TINY else _TINY
        delta = d * c
        h *= delta
        if abs(delta - 1) < 3e-15:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """The regularized incomplete beta function I_x(a, b): P(X <= x) for X ~ Beta(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    """The q quantile of Beta(a, b), by bisection on beta_cdf (to 1e-12)."""
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < _EPS:
            break
    return (lo + hi) / 2


def jeffreys(k: int, n: int, level: float = LEVEL) -> tuple[float, float]:
    """The Jeffreys interval for k of n; (0, 1) for n = 0."""
    if n <= 0:
        return 0.0, 1.0
    tail = (1 - level) / 2
    a, b = k + 0.5, n - k + 0.5
    low = 0.0 if k <= 0 else beta_ppf(tail, a, b)
    high = 1.0 if k >= n else beta_ppf(1 - tail, a, b)
    return low, high


def beta_posterior_gt(k1: int, n1: int, k2: int, n2: int, prior: tuple[float, float] = (0.5, 0.5),
                      steps: int = 2000) -> float:
    """P(p1 > p2) for p1 ~ Beta(a + k1, b + n1 − k1) and p2 ~ Beta(a + k2, b + n2 − k2), independent.

    It integrates pdf1(x) · I_x(p2's) over x = sin²θ, θ in (0, π/2) (Simpson's rule), which takes away the
    x^(a − 1) and (1 − x)^(b − 1) singularities of a Jeffreys prior at the ends."""
    a, b = prior
    a1, b1, a2, b2 = a + k1, b + n1 - k1, a + k2, b + n2 - k2
    log_norm = math.lgamma(a1 + b1) - math.lgamma(a1) - math.lgamma(b1)
    steps += steps % 2
    h = (math.pi / 2) / steps

    def power(base: float, exponent: float) -> float | None:  # log(base^exponent), None for 0^positive
        if base > 0:
            return exponent * math.log(base)
        return 0.0 if exponent == 0 else None

    def f(theta: float) -> float:
        s, c = math.sin(theta), math.cos(theta)
        # pdf1(x) dx with x = sin²θ: 2 · sin^(2a1 − 1)θ · cos^(2b1 − 1)θ dθ, over B(a1, b1).
        ls, lc = power(s, 2 * a1 - 1), power(c, 2 * b1 - 1)
        if ls is None or lc is None:
            return 0.0
        return 2 * math.exp(log_norm + ls + lc) * beta_cdf(s * s, a2, b2)

    total = f(0.0) + f(math.pi / 2)
    for i in range(1, steps):
        total += (4 if i % 2 else 2) * f(i * h)
    return min(1.0, max(0.0, total * h / 3))


def sequential_warning(k: int, n: int, r0: float, prior: tuple[float, float] = (0.5, 0.5)) -> float:
    """P(rate < r0) under the Beta(a + k, b + n − k) posterior (the early warning: W3-4 reads it)."""
    a, b = prior
    return beta_cdf(r0, a + k, b + n - k)


# -- tests and sample sizes --------------------------------------------------------------------------------------------


def n_per_arm(p1: float, p2: float, alpha: float = 0.05, power: float = 0.80, one_sided: bool = False) -> int:
    """Companies per arm to detect p1 against p2: (z_α √(2 p̄ q̄) + z_β √(p1 q1 + p2 q2))² ÷ (p1 − p2)², rounded."""
    if p1 == p2:
        raise ValueError("p1 and p2 must differ")
    z_a = _N.inv_cdf(1 - (alpha if one_sided else alpha / 2))
    z_b = _N.inv_cdf(power)
    pbar = (p1 + p2) / 2
    root = z_a * math.sqrt(2 * pbar * (1 - pbar)) + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
    return round(root * root / (p1 - p2) ** 2)


def _log_choose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact(k1: int, n1: int, k2: int, n2: int) -> float:
    """The two-sided Fisher exact p for k1 of n1 against k2 of n2; 1.0 when either arm is empty."""
    if n1 <= 0 or n2 <= 0:
        return 1.0
    total, drawn = n1 + n2, k1 + k2
    lo, hi = max(0, drawn - n2), min(drawn, n1)

    def logp(x: int) -> float:
        return _log_choose(n1, x) + _log_choose(n2, drawn - x) - _log_choose(total, drawn)

    seen = logp(k1)
    p = sum(math.exp(lp) for x in range(lo, hi + 1) if (lp := logp(x)) <= seen + 1e-9)
    return min(1.0, p)


def _spent(t: float, alpha_side: float) -> float:
    """The one-sided O'Brien-Fleming-type spending function: 2 − 2Φ(z_(1 − α/2) / √t) (Lan and DeMets 1983)."""
    return 2 - 2 * _N.cdf(_N.inv_cdf(1 - alpha_side / 2) / math.sqrt(t))


@functools.lru_cache(maxsize=64)
def obrien_fleming(looks: int, alpha: float = 0.10, grid: int = 1200) -> tuple[float, ...]:
    """The nominal two-sided p each of `looks` equally spaced looks must beat, for a total two-sided alpha.

    Each side spends alpha/2 by the O'Brien-Fleming-type function; the boundary at each look is solved so the chance
    of first crossing there, under no difference, is that look's spend. The score S_k = Z_k √t_k is a Brownian motion,
    so its density on the region not yet crossed is carried from look to look by numerical integration. The two
    sides' overlap is ignored (it is of the order of 1e-6 at these alphas)."""
    if looks < 1:
        raise ValueError("looks must be 1 or more")
    side = alpha / 2
    times = [(i + 1) / looks for i in range(looks)]
    out: list[float] = []
    xs: list[float] = []
    dens: list[float] = []
    prev_t, prev_spent = 0.0, 0.0
    for t in times:
        target = _spent(t, side) - prev_spent
        dt = t - prev_t
        sd = math.sqrt(dt)
        if not xs:  # the first look: S_1 ~ N(0, t)
            c = _N.inv_cdf(1 - target) * math.sqrt(t)
        else:
            def crossing(c: float) -> float:
                return _trap([d * (1 - _N.cdf((c - x) / sd)) for x, d in zip(xs, dens)], xs)

            lo, hi = -10.0, 10.0
            for _ in range(100):
                mid = (lo + hi) / 2
                if crossing(mid) > target:
                    lo = mid
                else:
                    hi = mid
            c = (lo + hi) / 2
        out.append(2 * (1 - _N.cdf(c / math.sqrt(t))))
        # The density of S at this look, below the boundary, for the next look.
        new_x = [-8 * math.sqrt(t) + (c + 8 * math.sqrt(t)) * i / grid for i in range(grid + 1)]
        if not xs:
            new_d = [_N.pdf(x / math.sqrt(t)) / math.sqrt(t) for x in new_x]
        else:
            new_d = [_trap([d * _N.pdf((y - x) / sd) / sd for x, d in zip(xs, dens)], xs) for y in new_x]
        xs, dens = new_x, new_d
        prev_t, prev_spent = t, prev_spent + target
    return tuple(out)


def _trap(ys: list[float], xs: list[float]) -> float:
    return sum((xs[i + 1] - xs[i]) * (ys[i] + ys[i + 1]) / 2 for i in range(len(xs) - 1))


def newcombe(k1: int, n1: int, k2: int, n2: int, z: float = Z95) -> tuple[float, float]:
    """The 95% interval for p1 − p2 from the two Wilson intervals (Newcombe 1998, method 10: the hybrid score). W3-1
    reads the A/A sham arm with it: a difference between two labels that change nothing should sit inside it."""
    if n1 <= 0 or n2 <= 0:
        return -1.0, 1.0
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    return d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)


def binomial_quantile(q: float, n: int, p: float) -> int:
    """The smallest x with P(X <= x) >= q for X ~ Binomial(n, p): a forecast count's interval ends (W3-1, F1)."""
    if n <= 0 or p <= 0:
        return 0
    if p >= 1:
        return n
    total = 0.0
    for x in range(n + 1):
        total += math.exp(_log_choose(n, x) + x * math.log(p) + (n - x) * math.log1p(-p))
        if total >= q - 1e-12:
            return x
    return n


def min_n_ok(cells: Mapping[object, int] | Iterable[int], minimum: int = 15) -> bool:
    """Every cell has at least `minimum`: a split prints only then, else "pooled only" (RT §1.5). No cells: False."""
    ns = list(cells.values()) if isinstance(cells, Mapping) else list(cells)
    return bool(ns) and all(int(n) >= minimum for n in ns)
