"""The early warning (W3-4, 21 Oct 2026; design-final §1 "Stop rule", D13; metrics-audit M9; redteam-stats §2).

The stop rule SPEC 12 set (fewer than stop_rule_meetings meetings from the first stop_rule_accounts companies, 5 in
1,500) first reads about 23 weeks in and lands in the blackout. This says much sooner, in a sequential form, that the
outcomes are far below what the pilot was built for, so the first thing checked is the plumbing (is every reply read,
is the mail delivered) and not the copy. Each warning reads companies once, intent to treat (learn/outcomes.py):

  no_human     at least MIN_MATURED_28 (100) companies have had 28 days and none of them replied at all (out-of-office
               aside): at a true 3% that happens with P = 0.97^n (0.048 at 100; 0.13 at 2%, 0.37 at 1%: it speaks
               against the 3-5% the pilot planned for, not for "the copy is weak");
  meetings     P(meeting rate < 0.25%) > 0.9 on the companies that have had 45 days, from Beta(½ + k, ½ + n − k)
               (stats.sequential_warning): 0.843 at 0 of 400, 0.955 at 0 of 800, so about 560 matured with none;
  positive     P(positive rate < 0.5%) > 0.9 on the companies that have had 28 days, against the 1% SPEC 12 expects
               (the n it took is printed).

check(ctx) runs inside metrics_daily (08:50 UK), after the series is written. Each warning is posted once an ISO week
(notify.post_once) with its n and P, to the alert channel. It is an alert: it pauses new enrolment only when General
early_warning_pause = yes (Harry's D13), and then as the stop rule pauses it, a kill_rule hold of action
pause_enrolment (base/holds.py) that `us-outbound killrules clear <item> --live` lifts. Each warning holds once, as the
stop rule fires once: after a clear it goes on alerting weekly and does not hold again (W3-4 r1); another warning that
fires later holds. The hold is recorded before its alert. The 5-in-1,500 stop rule stays as the brake until D13.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from us_outbound.base import notify, say
from us_outbound.base.holds import PAUSE_ENROLMENT
from us_outbound.context import Context
from us_outbound.learn import kill_rules, outcomes, stats
from us_outbound.learn.outcomes import Outcome
from us_outbound.logs import log

MIN_MATURED_28 = 100
EXPECTED_HUMAN = 0.03  # the human reply rate the pilot planned for (design-final §2.3)
MEETING_FLOOR, POSITIVE_FLOOR = 0.0025, 0.005  # half of SPEC 12's 0.5% meetings and 1% positive replies
WARN_AT = 0.9
SCOPE, TARGET = "enrolment", "early_warning"
CHECK_FIRST = "check capture and deliverability before copy"


@dataclass(frozen=True)
class Warning:
    name: str  # no_human, meetings, positive
    n: int
    k: int
    p: float
    line: str


def warnings(found: Sequence[Outcome]) -> list[Warning]:
    """The warnings that fire on these companies (the module docstring)."""
    m28 = [o for o in found if o.window_closed_28]
    m45 = [o for o in found if o.window_closed_45]
    out: list[Warning] = []
    human = sum(o.replied_in_window for o in m28)
    if len(m28) >= MIN_MATURED_28 and human == 0:
        p = (1 - EXPECTED_HUMAN) ** len(m28)
        out.append(Warning("no_human", len(m28), 0, p,
                           f"No human reply from {len(m28):,} companies that have had 28 days: P = {p:.3f} at a "
                           f"{EXPECTED_HUMAN:.0%} rate: {CHECK_FIRST}."))
    meetings = sum(o.meeting_in_45 for o in m45)
    p = stats.sequential_warning(meetings, len(m45), MEETING_FLOOR)
    if m45 and p > WARN_AT:
        out.append(Warning("meetings", len(m45), meetings, p,
                           f"{meetings:,} meetings from {len(m45):,} companies that have had 45 days: P(meeting rate "
                           f"< {MEETING_FLOOR:.2%}) = {p:.3f}: {CHECK_FIRST}."))
    positive = sum(o.positive_in_window for o in m28)
    p = stats.sequential_warning(positive, len(m28), POSITIVE_FLOOR)
    if m28 and p > WARN_AT:
        out.append(Warning("positive", len(m28), positive, p,
                           f"{positive:,} positive replies from {len(m28):,} companies that have had 28 days (the n "
                           f"it took): P(positive rate < {POSITIVE_FLOOR:.1%}) = {p:.3f}: {CHECK_FIRST}."))
    return out


def check(ctx: Context, found: Sequence[Outcome] | None = None) -> dict[str, Any]:
    """Post this week's warnings once, and hold new enrolment when General early_warning_pause = yes (the module
    docstring). A summary for metrics_daily's heartbeat."""
    found = outcomes.per_company(ctx) if found is None else found
    fired = warnings(found)
    summary: dict[str, Any] = {"warnings": [{"name": w.name, "n": w.n, "k": w.k, "p": round(w.p, 3)} for w in fired],
                               "posted": [], "held": None}
    if not fired:
        return summary
    pause = ctx.settings.general.early_warning_pause
    # The hold first, then the alert (holds are recorded before alerts). A warning holds once: like the stop rule
    # (kill_rules: fired.ever), a warning whose hold Harry cleared (`killrules clear`) never holds again, or the next
    # metrics_daily would pause enrolment again, silently, as its post went out that week (W3-4 r1).
    # Read whatever the key says now (W3-4 r2): a hold made while it was yes stays until cleared, and the footer says so.
    holding = kill_rules.holding(ctx, SCOPE, TARGET)
    fired_before = kill_rules.ever_fired(ctx)
    new = next((w for w in fired if f"early_warning_{w.name}" not in fired_before), None)
    if pause and not holding and new is not None:
        f = kill_rules.Firing(f"early_warning_{new.name}", SCOPE, TARGET, PAUSE_ENROLMENT, new.line,
                              numbers={"n": new.n, "k": new.k, "p": round(new.p, 4)})
        kill_rules.hold(ctx, f)
        summary["held"] = f.item_id
        holding = True
    lines = [(notify.once_key(ctx, "early_warning", w.name, per="week"), f"• {w.line}") for w in fired]
    if holding and pause:
        foot = ("New enrolment is paused until `us-outbound killrules clear <item> --live` (General "
                "early_warning_pause = yes).")
    elif holding:
        foot = ("New enrolment is still paused by an earlier early warning until `us-outbound killrules clear <item> "
                "--live` (General early_warning_pause is now no, so no new warning holds; the hold made stays).")
    elif pause:
        foot = ("This is an alert only: its hold was cleared with `us-outbound killrules clear`, and a warning holds "
                "once (General early_warning_pause = yes).")
    else:
        foot = ("This is an alert only: nothing is paused (General early_warning_pause = no; the stop rule of 5 "
                "meetings in the first stop_rule_accounts companies stays the brake).")
    sent = notify.post_once(ctx, lines, head=f"{say.URGENT} *Early warning: outcomes far below plan*", foot=foot)
    summary["posted"] = sent.get("keys") or []
    log("early_warning", run_id=ctx.run_id, **summary)
    return summary
