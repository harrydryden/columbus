"""Stacked multiplier, corrected: haircut applied to the LIFT (1 + (total-1)*(1-h)), lever E bounded by placement
headroom, C measured against the SPEC role rule, A+B merged, D excluded from the December window."""
FULL = {  # name: (stage, low, mid, high)
 "A+B right account and moment":           ("who",  1.02, 1.10, 1.22),
 "C right person (over the SPEC role rule)": ("who", 1.05, 1.15, 1.30),
 "D second contact at 50-249":              ("who",  1.00, 1.10, 1.20),
 "J Harry-signed to 10-49 founders":        ("who",  1.00, 1.04, 1.12),
 "E reaches the inbox (placement headroom; 4-5 links per email after the 1 Oct signature)":("seen", 1.03, 1.07, 1.12),
 "F relevant message":                      ("seen", 1.05, 1.12, 1.25),
 "G easy ask (on positives)":               ("act",  1.05, 1.15, 1.35),
 "H follow-through (on this metric)":       ("act",  1.00, 1.02, 1.05),
 "I demo page fixed (demo requests)":       ("act",  1.01, 1.03, 1.08),
}
DEC = {k: v for k, v in FULL.items() if not k.startswith(("D ", "J "))}
# December: openers on about half of accounts
DEC["F relevant message"] = ("seen", 1.02, 1.05, 1.10)   # signal openers only, ~half of accounts
DEC["I demo page fixed (demo requests)"] = ("act", 1.01, 1.03, 1.06)

def stack(levers, idx, overlap, haircut):
    groups = {}
    for name, (g, *m) in levers.items():
        groups.setdefault(g, []).append(m[idx])
    parts = {g: 1 + sum(x - 1 for x in ms) * (1 - overlap) for g, ms in groups.items()}
    total = 1.0
    for v in parts.values(): total *= v
    return parts, total, 1 + (total - 1) * (1 - haircut)

for label, levers in (("FULL PROGRAMME (Feb onward)", FULL), ("DECEMBER WINDOW (what is live by 18 Dec)", DEC)):
    print(label)
    for case, idx, ov, h in (("conservative", 0, 0.30, 0.15), ("mid", 1, 0.40, 0.15), ("optimistic", 2, 0.50, 0.10)):
        parts, prod, final = stack(levers, idx, ov, h)
        print(f"  {case:13} {{{', '.join(f'{k} {v:.2f}' for k, v in parts.items())}}} product {prod:.2f} -> {final:.2f}x")
print()
print("WHAT 2x NEEDS: full programme, mid values except the named levers at high")
import itertools
names = list(FULL)
def with_high(hi):
    lv = {k: (v[0], v[1], v[3] if k in hi else v[2], v[3]) for k, v in FULL.items()}
    return stack(lv, 1, 0.40, 0.15)[2]
for hi in ([], ["G easy ask (on positives)"], ["G easy ask (on positives)", "C right person (over the SPEC role rule)"],
           ["G easy ask (on positives)", "C right person (over the SPEC role rule)", "D second contact at 50-249"],
           ["G easy ask (on positives)", "C right person (over the SPEC role rule)", "D second contact at 50-249", "F relevant message"],
           ["G easy ask (on positives)", "C right person (over the SPEC role rule)", "D second contact at 50-249", "F relevant message", "E reaches the inbox (placement headroom)"]):
    print(f"  high on {[h.split()[0] for h in hi] or 'none'}: {with_high(hi):.2f}x")
print()
print("Sensitivity of mid case to overlap:", ", ".join(f"{int(o*100)}% -> {stack(FULL,1,o,0.15)[2]:.2f}" for o in (0.3,0.4,0.5)))
