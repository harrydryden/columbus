"""Mailbox capacity three weeks ahead (Harry, 7 Oct 2026): ask for mailboxes while a new one still has time to warm.

A new mailbox warms for about 21 days before it may send (registry/mailboxes.py promotes it then), so limits.py's
same-day line ("Add a mailbox for ...", when sending capacity is already today's limit) comes about three weeks
late. That line stays; this one looks ahead. On Mondays the daily post (learn/daily_post.py) compares:
  * capacity at full ramp: each Active mailbox's daily cap once its sending ramp is over (ramp.Ramp.full_cap), each
    sender's steady pace of new companies a day (capacity.steady_pace: the cap ÷ the 4 emails each new company
    receives), and the send days in a week (the send window's weekdays): X new companies a week;
  * what is needed: weekly_enrol_cap (Y), as far as there are companies to fill it in the WARMUP_WEEKS a new
    mailbox takes: the Z ready now (verified, with a sendable contact, as the daily post counts them) spread over
    those weeks, plus the recent supply: the accounts verify_accounts verified a week over the last SUPPLY_DAYS
    (learn/daily_report.verified_in).
When the need is above X, it posts once that ISO week, with the approvers mentioned (notify.post_once): "Add N
mailboxes now: a new mailbox takes about 3 weeks to warm up. At full ramp your M Active mailboxes take about X new
companies a week; weekly_enrol_cap is Y and Z companies are ready (W weeks)." N is how many mailboxes at the
largest Active cap (MAX_CAP when none is Active) close the gap, less the ones already Warming; W is how long the
ready companies last at X a week. Nothing is posted when no mailbox is needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from us_outbound.clients.guard import GuardViolation
from us_outbound.context import Context
from us_outbound.enrol import capacity
from us_outbound.learn import daily_report
from us_outbound.logs import log
from us_outbound.ops import notify
from us_outbound.registry import ramp
from us_outbound.registry.mailboxes import MAX_CAP

WARMUP_WEEKS = 3  # a new mailbox's warmup (registry/mailboxes.py: Active after 21 days of warmup)
SUPPLY_DAYS = 14  # the recent supply: accounts verified over these days, as a weekly rate
ACTIVE, WARMING = "Active", "Warming"
HEAD = "Mailboxes, three weeks ahead:"


@dataclass(frozen=True)
class Ahead:
    active: int  # Active mailboxes (a mailbox a kill rule holds counts as Paused: ops/bootstrap.py)
    warming: int  # Warming mailboxes: Active within the warmup, so counted against what is asked for
    weekly: int  # X: new companies a week at full ramp
    cap: int  # Y: weekly_enrol_cap
    ready: int  # Z: companies ready to send now
    supply: float  # accounts verified a week, recently
    per_mailbox: int  # new companies a week one more mailbox takes, at full ramp
    need: float  # what the companies on hand can fill a week, up to Y
    add: int  # N: mailboxes to add now

    @property
    def weeks_ready(self) -> float | None:
        """How long the ready companies last at full-ramp capacity."""
        return self.ready / self.weekly if self.weekly > 0 else None

    def as_dict(self) -> dict[str, Any]:
        return {"active": self.active, "warming": self.warming, "weekly_at_full_ramp": self.weekly,
                "weekly_enrol_cap": self.cap, "ready": self.ready, "supply_per_week": round(self.supply, 1),
                "per_mailbox": self.per_mailbox, "need_per_week": round(self.need, 1), "add": self.add}


def plan(ctx: Context, ready: int) -> Ahead:
    """The comparison, from the ramp and capacity code's own arithmetic."""
    s = ctx.settings
    ramps = ramp.ramps(ctx.store, s, ctx.now_et().date())
    full: dict[str, int] = {}  # owner -> sends a day at full ramp
    caps: list[int] = []
    for m in s.mailboxes:
        if m.status != ACTIVE:
            continue
        r = ramps.get(m.address.lower())
        cap = r.full_cap if r is not None else max(0, int(m.daily_cap or 0))
        full[m.owner_name] = full.get(m.owner_name, 0) + cap
        caps.append(cap)
    days = len(s.general.send_window.days)
    weekly = sum(capacity.steady_pace(c) for c in full.values()) * days
    per_mailbox = capacity.steady_pace(max(caps, default=MAX_CAP)) * days
    verified = daily_report.verified_in(ctx.store, ctx.now - timedelta(days=SUPPLY_DAYS), ctx.now) or 0
    supply = verified * 7 / SUPPLY_DAYS
    want = s.general.weekly_enrol_cap
    need = min(float(want), ready / WARMUP_WEEKS + supply)
    warming = sum(m.status == WARMING for m in s.mailboxes)
    add = 0
    if need > weekly and per_mailbox > 0:
        add = max(0, math.ceil((need - weekly) / per_mailbox) - warming)
    return Ahead(active=len(caps), warming=warming, weekly=weekly, cap=want, ready=ready, supply=supply,
                 per_mailbox=per_mailbox, need=need, add=add)


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def line(a: Ahead) -> str:
    """The ask, as Harry reads it."""
    takes = "takes" if a.active == 1 else "take"
    text = (f"Add {_n(a.add, 'mailbox', 'mailboxes')} now: a new mailbox takes about {WARMUP_WEEKS} weeks to warm up. "
            f"At full ramp your {_n(a.active, 'Active mailbox', 'Active mailboxes')} {takes} about {a.weekly} new "
            f"companies a week; weekly_enrol_cap is {a.cap} and {a.ready} companies are ready")
    weeks = a.weeks_ready
    text += f" ({weeks:.1f} weeks)." if weeks is not None else "."
    if a.warming:
        text += f" {_n(a.warming, 'mailbox is', 'mailboxes are')} warming already, counted in."
    return text + (' `us-outbound mailbox add ADDRESS --owner "NAME" --live` adds one; it warms for 21 days, then '
                   "starts its sending ramp.")


def check(ctx: Context, ready: int) -> dict[str, Any]:
    """Mondays (UK): post the ask when a mailbox is needed, once that ISO week. Never raises (but a GuardViolation)."""
    if ctx.today_uk().weekday() != 0:
        return {"checked": False}
    try:
        a = plan(ctx, ready)
        out: dict[str, Any] = {"checked": True, **a.as_dict()}
        if a.add > 0:
            year, week, _ = ctx.today_uk().isocalendar()
            out["alert"] = notify.post_once(ctx, [(f"mailboxes:{year}-W{week:02d}", line(a))], head=HEAD)
    except GuardViolation:
        raise
    except Exception as exc:  # the daily post still goes
        log("capacity_ahead_failed", error=str(exc)[:200])
        return {"checked": False, "capacity_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    log("capacity_ahead", **out)
    return out
