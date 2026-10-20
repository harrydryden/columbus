"""Throughput: what held each send day's enrolment, the leads lost by cause, and the mailboxes' use (loop L6; W3-3, 20
Oct 2026; design §4.3 P1-P3).

From enrol's own run summaries (heartbeats.detail; the number's terms, limits.py), over the last `weeks` weeks:
  * the binding term per send day: capacity (sending_capacity bound the number), supply (ready_accounts), target
    (the weekly target: nothing lost), a gate (enrol did not run: the hand-check, a kill rule, the operator's pause,
    live_sending), blackout, or errors (accounts prepared and not enrolled);
  * lost leads by cause: the day's safe room less what was enrolled, put down to the term that bound. The room is
    min(weekly target, sending capacity) on a day enrol ran, and the day's share of weekly_enrol_cap (cap ÷ send days)
    on a day a gate or the blackout stopped it. A lead enrolled at least NEVER_SENT_DAYS ago whose step 1 never went
    out (no sent event) is lost too, as never_sent (RT §1.1).
  * utilisation per mailbox: its sent events over the window ÷ (its daily cap × the send days), for the mailboxes past
    ramp week RAMP_WEEKS only (registry/ramp.py), as a mailbox still ramping is held below its cap on purpose.
L6's rule is one line (rule_line), a report and never an action: mailboxes and the cap are Harry's.
  * no mailbox past ramp week 3: "ramp: not evaluated";
  * capacity bound at least BINDING_DAYS days at utilisation under LOW_USE: the formula or a gate holds the number
    below what the mailboxes can send (a bug to look at);
  * capacity bound at least BINDING_DAYS days at LOW_USE or more: the mailboxes are the limit; one more a domain is
    proposed only once supply holds 10 days and step-1 bounces are under 2% on 100 or more contacts (D14, D19);
  * supply bound at least BINDING_DAYS days: sourcing and contacts (loops L1, L2, L5);
  * a gate or an error on any day: looked at the same day.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from us_outbound.clients.db import Range
from us_outbound.context import Context
from us_outbound.registry import ramp
from us_outbound.timeparse import et_day, utc

ENROL = "enrol"
RAMP_WEEKS = 3  # evaluated on mailboxes past their third sending week
BINDING_DAYS = 3  # of the week's send days
LOW_USE = 0.60
NEVER_SENT_DAYS = 2  # a lead enrolled this long ago with no step 1 sent is lost
CAUSES = ("capacity", "supply", "gate", "blackout", "errors", "never_sent")
TERM_CAUSE = {"sending_capacity": "capacity", "ready_accounts": "supply", "weekly_target": "target"}


@dataclass
class Day:
    date: str
    binding: str  # capacity, supply, target, gate, blackout
    room: int
    number: int
    enrolled: int
    lost: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"date": self.date, "binding": self.binding, "room": self.room, "number": self.number,
                "enrolled": self.enrolled, "lost": dict(self.lost)}


@dataclass
class Report:
    days: list[Day]
    lost: dict[str, int]
    utilisation: dict[str, float]  # mailbox -> sends ÷ (cap × send days), past ramp week 3 only
    ramping: list[str]  # mailboxes not past ramp week 3
    rule: str

    def as_dict(self) -> dict[str, Any]:
        return {"days": [d.as_dict() for d in self.days], "lost": dict(self.lost),
                "utilisation": {m: round(u, 3) for m, u in self.utilisation.items()}, "ramping": list(self.ramping),
                "rule": self.rule}


def day_share(ctx: Context) -> int:
    """The day's share of the weekly cap: what a day enrol did not run could have taken."""
    g = ctx.settings.general
    return math.ceil(g.weekly_enrol_cap / max(1, len(g.send_window.days)))


def read_day(ctx: Context, row: Mapping[str, Any]) -> Day | None:
    """One enrol run's day, from its heartbeat row; None for a run that is no send day's (an error, a dry run)."""
    d = row.get("detail")
    if not isinstance(d, Mapping) or row.get("status") not in ("ok", "skipped"):
        return None
    started = utc(row.get("started_at"))
    day = str(d.get("date") or (et_day(started).isoformat() if started else ""))
    if row.get("status") == "skipped":
        cause = "blackout" if "blackout" in str(d.get("reason") or "").casefold() else "gate"
        share = day_share(ctx)
        return Day(day, cause, share, 0, 0, {cause: share})
    terms = d.get("number_terms") if isinstance(d.get("number_terms"), Mapping) else {}
    if not terms:
        return None
    room = int(min(terms.get("weekly_target") or 0, terms.get("sending_capacity") or 0))
    number = int(d.get("number") or 0)
    enrolled = int(d.get("prepared") if d.get("dry_run") else d.get("enrolled") or 0)
    binding = TERM_CAUSE.get(str(terms.get("binding") or ""), "target")
    lost: Counter[str] = Counter()
    if binding in ("capacity", "supply") and room > number:
        lost[binding] += room - number
    if number > enrolled:
        lost["errors"] += number - enrolled
    return Day(day, binding, room, number, enrolled, {k: v for k, v in lost.items() if v})


def never_sent(ctx: Context, since, until) -> int:
    """Contacts enrolled between since and NEVER_SENT_DAYS ago whose step 1 has no sent event."""
    cutoff = min(until, ctx.now - timedelta(days=NEVER_SENT_DAYS))
    if cutoff <= since:
        return 0
    enrolled = [c for c in ctx.store.select("contacts", {"enrolled_at": Range(since, cutoff)})
                if not c.get("lead_deleted_at")]
    if not enrolled:
        return 0
    ids = [str(c["contact_id"]) for c in enrolled]
    sent = {str(e.get("contact_id")) for i in range(0, len(ids), 1000)
            for e in ctx.store.select("events", {"type": "sent", "contact_id": ids[i : i + 1000]})}
    return sum(1 for i in ids if i not in sent)


def utilisation(ctx: Context, since, days: int) -> tuple[dict[str, float], list[str]]:
    """(mailbox -> its use past ramp week RAMP_WEEKS; the mailboxes still within it)."""
    today = et_day(ctx.now)
    boxes = ramp.ramps(ctx.store, ctx.settings, today)
    sends: Counter[str] = Counter(str(e.get("mailbox") or "").lower()
                                  for e in ctx.store.select("events", {"type": "sent", "occurred_at": Range(since, ctx.now)}))
    use, ramping = {}, []
    for address, r in sorted(boxes.items()):
        if r.week <= RAMP_WEEKS:
            ramping.append(address)
            continue
        cap = max(0, int(r.sheet_cap or 0)) * days
        if cap:
            use[address] = sends[address] / cap
    return use, ramping


def rule_line(days: list[Day], use: Mapping[str, float], ramping: list[str]) -> str:
    """Loop L6's line (the module docstring)."""
    if not use:
        return "ramp: not evaluated (no mailbox is past its third sending week)" if ramping else \
            "not evaluated: no mailbox past its third sending week"
    n = len(days)
    by = Counter(d.binding for d in days)
    overall = sum(use.values()) / len(use)
    stuck = [d for d in days if d.binding in ("gate", "errors") or d.lost.get("errors")]
    if by["capacity"] >= BINDING_DAYS and overall < LOW_USE:
        line = f"capacity binding {by['capacity']} of {n} days at {overall:.0%} utilisation: the formula or a gate"
    elif by["capacity"] >= BINDING_DAYS:
        line = (f"capacity binding {by['capacity']} of {n} days at {overall:.0%} utilisation: the mailboxes are the "
                "limit; one more a domain once supply holds 10 days and step-1 bounces are under 2% on 100+ contacts")
    elif by["supply"] >= BINDING_DAYS:
        line = f"supply binding {by['supply']} of {n} days: sourcing and contacts (loops L1, L2, L5)"
    elif not stuck:
        return f"nothing bound {BINDING_DAYS} or more of {n} days at {overall:.0%} utilisation"
    else:
        line = ""
    held = f"a gate or an error held enrolment on {len(stuck)} of {n} days: look at it the same day" if stuck else ""
    return "; ".join(x for x in (line, held) if x)


def report(ctx: Context, weeks: int = 1) -> Report:
    """The last `weeks` weeks of enrol runs (the newest run of each send day), lost leads and the mailboxes' use."""
    since = ctx.now - timedelta(weeks=weeks)
    rows = ctx.store.select("heartbeats", {"job": ENROL, "started_at": Range(since, ctx.now)})
    by_day: dict[str, tuple[Any, Day]] = {}
    for r in rows:
        day = read_day(ctx, r)
        if day is None:
            continue
        t = utc(r.get("started_at"))
        if day.date not in by_day or (t is not None and t > by_day[day.date][0]):
            by_day[day.date] = (t, day)
    days = [d for _, d in sorted(by_day.values(), key=lambda x: x[1].date)]
    lost: Counter[str] = Counter()
    for d in days:
        lost.update(d.lost)
    lost["never_sent"] += never_sent(ctx, since, ctx.now)
    use, ramping = utilisation(ctx, since, max(1, len(days)))
    return Report(days, {c: lost.get(c, 0) for c in CAUSES}, use, ramping, rule_line(days, use, ramping))


def lines(r: Report) -> list[str]:
    """The readout's lines (learn/readout.py)."""
    if not r.days:
        return ["  Throughput: no enrol run in the last week."]
    binding = ", ".join(f"{d.date} {d.binding}" for d in r.days)
    lost = ", ".join(f"{c} {n}" for c, n in r.lost.items() if n) or "none"
    use = ", ".join(f"{m} {u:.0%}" for m, u in r.utilisation.items())
    return [f"  Throughput: what bound each send day: {binding}. Leads lost by cause: {lost}.",
            f"  Mailbox use (sends ÷ cap, past ramp week {RAMP_WEEKS}): {use or 'none past it yet'}. {r.rule[:1].upper()}"
            f"{r.rule[1:]}."]
