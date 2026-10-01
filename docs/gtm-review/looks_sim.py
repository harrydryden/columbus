"""Error rates of a pre-registered 3-look Bayesian schedule for Test 1 (non-negative reply rate), vs weekly peeking.
P(B>A) via normal approximation to Beta posteriors (fine at n>=200, p~4%)."""
import random
from statistics import NormalDist
N = NormalDist(); random.seed(7)
def p_b_better(sa, na, sb, nb):
    ma, mb = (sa+1)/(na+2), (sb+1)/(nb+2)
    va, vb = ma*(1-ma)/(na+3), mb*(1-mb)/(nb+3)
    return N.cdf((mb-ma)/((va+vb)**0.5))
def run(pa, pb, looks, shift_at, shift_p, adopt_p, adopt_look, draws=20000):
    adopt = shift = harm_stop = 0
    for _ in range(draws):
        sa = sb = 0; na = nb = 0; adopted = shifted = stopped = False
        prev = 0
        for i, n in enumerate(looks):
            k = n - prev; prev = n
            sa += sum(random.random() < pa for _ in range(k)); na += k
            sb += sum(random.random() < pb for _ in range(k)); nb += k
            p = p_b_better(sa, na, sb, nb)
            if i == 0 and (sb/nb) < (sa/na) - 0.02:   # harm rule at look 1: B at least 2 points worse
                stopped = True; break
            if i == shift_at and p >= shift_p: shifted = True
            if i >= adopt_look and p >= adopt_p: adopted = True; break
        adopt += adopted; shift += shifted; harm_stop += stopped
    return adopt/draws, shift/draws, harm_stop/draws
looks3 = [200, 400, 600]
weekly = list(range(64*4, 64*10, 64))  # weekly looks from week 4 to 9 (~256..576 per arm)
print("Schedule: looks at 200/400/600 per arm; shift to 70/30 at look 2 if P>=0.90; adopt at look 3 if P>=0.975")
for pa, pb, label in ((0.04, 0.04, "null 4% vs 4%"), (0.04, 0.052, "1.3x: 4% vs 5.2%"), (0.04, 0.06, "1.5x: 4% vs 6%"), (0.04, 0.03, "harm: 4% vs 3%")):
    a, s, h = run(pa, pb, looks3, 1, 0.90, 0.975, 2, draws=6000)
    print(f"  {label:20} P(adopt B)={a:.2f}  P(shift at look 2)={s:.2f}  P(harm stop at look 1)={h:.2f}")
print("Weekly peeking from week 4 to 9, adopt at first P>=0.95 (what the first draft implied)")
for pa, pb, label in ((0.04, 0.04, "null"), (0.04, 0.052, "1.3x")):
    a, s, h = run(pa, pb, weekly, 99, 0.90, 0.95, 0, draws=6000)
    print(f"  {label:20} P(adopt B)={a:.2f}")
print("Single look at 600/arm, adopt at P>=0.975")
for pa, pb, label in ((0.04, 0.04, "null"), (0.04, 0.052, "1.3x"), (0.04, 0.06, "1.5x")):
    a, s, h = run(pa, pb, [600], 99, 0.90, 0.975, 0, draws=6000)
    print(f"  {label:20} P(adopt B)={a:.2f}")
# Calendar: when do looks land? 64/arm/week of enrolments; windows close 28 days later.
import datetime as dt
start = dt.date(2026,10,26)
def enrol_week_for(n_per_arm): return -(-n_per_arm // 64)  # weeks of enrolment needed
for n in looks3:
    wk = enrol_week_for(n)
    # skip Thanksgiving week (23 Nov) and 18 Dec - 4 Jan blackout for enrolment
    d = start; weeks = 0
    while weeks < wk:
        if not (dt.date(2026,11,23) <= d <= dt.date(2026,11,27) or dt.date(2026,12,18) <= d <= dt.date(2027,1,4)):
            weeks += 1
        d += dt.timedelta(days=7)
    enrolled_by = d - dt.timedelta(days=7)
    print(f"  look at {n}/arm: last enrolment week starts {enrolled_by}, window closes {enrolled_by + dt.timedelta(days=28+4)}")
