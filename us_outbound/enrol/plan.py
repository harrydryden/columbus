"""Instantly's plan limit (Harry, 7 Oct 2026): a full plan keeps contacts and asks for room, never suppresses them.

The workspace's plan holds only so many leads. When /leads/add leaves a lead out because the plan is full
(clients/instantly.plan_full: remaining_in_plan 0, or leads left out with a status naming the limit; plan_full_error:
the whole add refused for it), the lead was not refused: it is not on Instantly's blocklist and in no other
campaign. Until 7 Oct 2026 enrol treated every left-out lead as refused and suppressed its contact for good
(enrol.mark_not_added), so a full plan would have suppressed every contact enrol tried. Now:
  * enrol skips each lead left out with the reason SKIP ("Instantly plan limit"), keeps its contact and account as
    they are (verified, not suppressed), and adds no more leads that run, so the next run tries them again;
  * it posts the plan-full alert (full_text) to the alert channel with the approvers mentioned, once a UK day (notify.post_once).
And after any add that reports remaining_in_plan, a warning once an ISO week when the room left is under
LOW_WEEKS weeks of weekly_enrol_cap, so the plan is raised before it fills.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from us_outbound.base import notify, say
from us_outbound.clients import instantly
from us_outbound.context import Context
from us_outbound.logs import log

SKIP = "Instantly plan limit"  # enrol's skip reason
KEPT = "Instantly's plan has no room for new leads: the contact is kept and is added when there is room"
LOW_WEEKS = 2  # warn when the plan's room is under this many weeks of weekly_enrol_cap
MAKE_ROOM = "upgrade the plan in Instantly, or delete leads there that have finished all their emails"


def _post(ctx: Context, key: str, text: str) -> dict[str, Any]:
    """The say.compose headline is post_once's head (the approvers mentioned in front of it); the rest goes under it."""
    head, _, body = text.partition("\n")
    return notify.post_once(ctx, [(key, body)], head=head)


def full_text() -> str:
    """The plan-full alert: nothing new goes out until there is room."""
    return say.compose(
        say.URGENT, "Instantly's plan is full: nobody new is being emailed",
        ["Instantly's plan holds only so many people (leads) at once, and it has no room left.",
         "People already in the campaign still get their follow-ups. The new ones are kept, not dropped, and wait "
         "for room."],
        do=f"Make room: {MAKE_ROOM}. Nothing else is needed: {say.job_name('enrol')} tries again by itself.",
    )


def low_text(ctx: Context, remaining: int, cap: int) -> str:
    """The low-room warning: under LOW_WEEKS weeks of weekly_enrol_cap left."""
    return say.compose(
        say.ACTION, f"Instantly's plan is nearly full: room for {remaining:,} more people",
        [f"That is under {LOW_WEEKS} weeks at the current pace: `weekly_enrol_cap` (people added a week) is {cap} "
         f"on the {say.sheet_link(ctx, 'General')}."],
        do=f"Before it fills, {MAKE_ROOM}. Once it is full, nobody new is emailed until there is room.",
    )


def full_alert(ctx: Context) -> dict[str, Any]:
    """The plan-full ask, once a UK day."""
    out = _post(ctx, notify.once_key(ctx, "instantly_plan_full"), full_text())
    log("instantly_plan_full", job=ctx.job, posted=out.get("keys"))
    return out


def low_alert(ctx: Context, remaining: int) -> dict[str, Any] | None:
    """The plan's room under LOW_WEEKS weeks of weekly_enrol_cap: a warning once an ISO week; None when there is room."""
    cap = ctx.settings.general.weekly_enrol_cap
    if remaining >= LOW_WEEKS * cap:
        return None
    return _post(ctx, notify.once_key(ctx, "instantly_plan_low", per="week"), low_text(ctx, remaining, cap))


def after_add(ctx: Context, answer: Mapping[str, Any] | None) -> dict[str, Any]:
    """After a live add: the plan-full ask, or the low-room warning. Returns what the summary keeps."""
    remaining = instantly.remaining_in_plan(answer)
    out: dict[str, Any] = {"remaining_in_plan": remaining, "full": instantly.plan_full(answer)}
    if out["full"]:
        out["alert"] = full_alert(ctx)
    elif remaining is not None and (low := low_alert(ctx, remaining)) is not None:
        out["alert"] = low
    return out
