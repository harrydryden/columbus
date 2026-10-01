"""Sample sizes (two-proportion, two-sided alpha .05, power .8), power at fixed n, sequential/bandit intuition, and funnel maths."""
from math import sqrt, erf, exp, log
from statistics import NormalDist
N = NormalDist()
za = N.inv_cdf(0.975); zb = N.inv_cdf(0.80)

def n_per_arm(p1, p2):
    pbar = (p1+p2)/2
    num = (za*sqrt(2*pbar*(1-pbar)) + zb*sqrt(p1*(1-p1)+p2*(1-p2)))**2
    return num/((p2-p1)**2)

def power(p1, p2, n):
    pbar=(p1+p2)/2
    se0 = sqrt(2*pbar*(1-pbar)/n); se1 = sqrt((p1*(1-p1)+p2*(1-p2))/n)
    z = (abs(p2-p1) - za*se0)/se1
    return N.cdf(z)

print("SAMPLE SIZE PER ARM (alpha .05 two-sided, power .8)")
for p1,p2,label in [(0.01,0.02,"positive 1%->2%"),(0.01,0.015,"positive 1%->1.5%"),(0.015,0.03,"positive 1.5%->3%"),
                    (0.03,0.06,"reply 3%->6%"),(0.03,0.045,"reply 3%->4.5%"),(0.04,0.06,"reply 4%->6%"),(0.05,0.075,"reply 5%->7.5%"),
                    (0.10,0.05,"microsoft vs google reply gap 10%->5%"),(0.02,0.01,"bounce 2%->1%")]:
    n=n_per_arm(p1,p2); print(f"  {label:40} n/arm={n:7.0f}  weeks@64/arm/wk={n/64:5.1f}  weeks@128/arm/wk(2 arms all volume)={n/64:5.1f}")
print()
print("POWER at n per arm for 2x lift")
for p1 in (0.01,0.02,0.03,0.05):
    for n in (200,400,800,1600):
        print(f"  base {p1:.0%} n={n:4}: power {power(p1,2*p1,n):.2f}")
print()
# Weekly cohort at 128 non-control accounts a week (150 - 15% control = 127.5)
print("LEADING INDICATOR NOISE: weekly cohort of 128 accounts")
for p in (0.01,0.03,0.05):
    se = sqrt(p*(1-p)/128); print(f"  rate {p:.0%}: 95% CI half-width +/-{1.96*se:.1%}; expected events {128*p:.1f}")
print("  4-week rolling (512):")
for p in (0.01,0.03,0.05):
    se = sqrt(p*(1-p)/512); print(f"  rate {p:.0%}: +/-{1.96*se:.1%}; events {512*p:.0f}")
print()
# Bayesian: probability arm B > arm A after n per arm with observed counts, Beta(1,1) priors, Monte Carlo
import random
random.seed(1)
def p_b_better(a_s,a_n,b_s,b_n,draws=20000):
    import random
    wins=0
    for _ in range(draws):
        a=random.betavariate(1+a_s,1+a_n-a_s); b=random.betavariate(1+b_s,1+b_n-b_s)
        wins += b>a
    return wins/draws
print("BAYESIAN P(B better) with Beta(1,1) priors")
for a_n,a_s,b_n,b_s in [(300,9,300,18),(300,9,300,14),(500,15,500,30),(500,15,500,22),(200,2,200,4),(600,6,600,12)]:
    print(f"  A {a_s}/{a_n} ({a_s/a_n:.1%}) vs B {b_s}/{b_n} ({b_s/b_n:.1%}): P(B>A)={p_b_better(a_s,a_n,b_s,b_n):.2f}")
print()
# Funnel baseline
print("FUNNEL: 650 accounts/month")
for name,deliv,inbox,read,reply,pos,book,held in [
    ("conservative/current design",0.95,0.70,0.45,0.03,0.30,0.50,0.75),
    ("mid/current design",0.97,0.80,0.55,0.04,0.33,0.55,0.80),
    ("optimistic/current design",0.98,0.88,0.60,0.05,0.40,0.60,0.85)]:
    acc=650; d=acc*deliv; i=d*inbox; r=i*read; rep=r*reply/ (inbox*read) if False else d*reply  # reply rate quoted per delivered
    posn=rep*pos; booked=posn*book; heldn=booked*held
    print(f"  {name:28}: delivered {d:.0f}, inbox {i:.0f}, replies {rep:.1f} ({reply:.0%} of delivered), positive {posn:.1f} ({posn/d:.2%}), booked {booked:.1f}, held {heldn:.1f}")
print()
# Multi-threading capacity cost: 2 contacts at 50-249 share, 4 steps -> accounts per week with 120 sends/day
print("CAPACITY under variants (4 boxes x 30 = 120 sends/day = 600/wk)")
for label, steps, contacts_avg in [("4 steps,1 contact",4,1.0),("4 steps, 2 contacts at 50-249 (~40% of accounts)",4,1.4),("3 steps, 2 contacts at 50-249",3,1.4),("4 steps, 2 contacts everywhere",4,2.0),("3 steps,1 contact",3,1.0)]:
    print(f"  {label:48}: {600/(steps*contacts_avg):5.0f} accounts/week")
