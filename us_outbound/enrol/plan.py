"""Instantly's plan limit (Harry, 7 Oct 2026): a full plan keeps contacts and asks for room, never suppresses them.

The workspace's plan holds only so many leads. When /leads/add leaves a lead out because the plan is full
(clients/instantly.plan_full: remaining_in_plan 0, or leads left out with a status naming the limit; plan_full_error:
the whole add refused for it), the lead was not refused: it is not on Instantly's blocklist and in no other
campaign. Until 7 Oct 2026 enrol and a send approval's ✅ treated every left-out lead as refused and suppressed its
contact for good (enrol.mark_not_added), so a full plan would have suppressed every contact enrol tried. Now:
  * enrol (auto_send = yes) skips each lead left out with the reason SKIP ("Instantly plan limit"), keeps its
    contact and account as they are (verified, not suppressed), and adds no more leads that run, so the next run
    tries them again;
  * a send approval's ✅ holds the card (approvals._hold, the way a stop or a blackout holds it): it stays open,
    its ✅ stays valid and each poll_approvals run tries again until there is room or the card expires; the rest
    of that run's approvals are held without an add;
  * either posts ALERT to the alert channel with the approvers mentioned, once a UK day (notify.post_once).
And after any add that reports remaining_in_plan, a warning once an ISO week when the room left is under
LOW_WEEKS weeks of weekly_enrol_cap, so the plan is raised before it fills.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from us_outbound.clients import instantly
from us_outbound.context import Context
from us_outbound.logs import log
from us_outbound.ops import notify

SKIP = "Instantly plan limit"  # the skip reason (enrol) and the hold (a send approval)
KEPT = "Instantly's plan has no room for new leads: the contact is kept and is added when there is room"
HOLD = "Instantly's plan has no room for new leads (upgrade the plan, or delete leads that finished their sequence)"
ALERT = ("Instantly's plan has no room for new leads, so nothing new is being sent. Upgrade the Instantly plan, or "
         "delete leads that finished their sequence. Contacts are kept and will be added when there is room.")
LOW_WEEKS = 2  # warn when the plan's room is under this many weeks of weekly_enrol_cap
HEAD = "Instantly's plan:"


def full_alert(ctx: Context) -> dict[str, Any]:
    """The plan-full ask, once a UK day."""
    out = notify.post_once(ctx, [(f"instantly_plan_full:{ctx.today_uk().isoformat()}", ALERT)], head=HEAD)
    log("instantly_plan_full", job=ctx.job, posted=out.get("keys"))
    return out


def low_alert(ctx: Context, remaining: int) -> dict[str, Any] | None:
    """The plan's room under LOW_WEEKS weeks of weekly_enrol_cap: a warning once an ISO week; None when there is room."""
    cap = ctx.settings.general.weekly_enrol_cap
    if remaining >= LOW_WEEKS * cap:
        return None
    year, week, _ = ctx.today_uk().isocalendar()
    text = (f"Instantly's plan has room for {remaining:,} more leads, under {LOW_WEEKS} weeks at weekly_enrol_cap "
            f"({cap} a week). Upgrade the Instantly plan, or delete leads that finished their sequence, before it "
            "fills: then nothing new is sent.")
    return notify.post_once(ctx, [(f"instantly_plan_low:{year}-W{week:02d}", text)], head=HEAD)


def after_add(ctx: Context, answer: Mapping[str, Any] | None) -> dict[str, Any]:
    """After a live add: the plan-full ask, or the low-room warning. Returns what the summary keeps."""
    remaining = instantly.remaining_in_plan(answer)
    out: dict[str, Any] = {"remaining_in_plan": remaining, "full": instantly.plan_full(answer)}
    if out["full"]:
        out["alert"] = full_alert(ctx)
    elif remaining is not None and (low := low_alert(ctx, remaining)) is not None:
        out["alert"] = low
    return out
