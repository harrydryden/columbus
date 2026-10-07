"""One post to the alert channel, or the log when there is no Slack token (SPEC 11, 12; Harry, 30 Sep 2026).

The kill rules, the hand-check and the daily post all post here. In dry-run the Slack
client sends the post to the dev channel instead (clients/slack.py). Without
US_OUTBOUND_SLACK_BOT_TOKEN, Clients.slack logs posts in dry-run and refuses in live; a
live job that must still act (a kill rule pausing a mailbox) uses `alert`, which logs the
post instead, so the action never waits on Slack. The log gets the post line by line, so a
long post is not clipped to one email body's 200 characters (logs.py); emails in it are
hashed as in every log line. A Slack error is reported, not raised: the caller has done its
work and says in its summary that the post failed.

post_once (Harry, 7 Oct 2026) is for the alerts that ask for a decision or a purchase (learn/spend.py,
learn/capacity_ahead.py, enrol/plan.py, ops/job_errors.py): one message with the approvers mentioned,
holding only the lines not posted before. Each line has a key that carries its own period
("apollo_floor:2026-10-07" once a day, "claude_cap_80:2026-10" once a month), and each key posted is an
events row of type alert (event_id "alert:{key}"), so a restart or a rerun never repeats it. A key is
recorded only once the post went (or went to the log, with no token), so a Slack failure is tried again
on the next run. Dry-run keeps its own keys ("alert:dry-run:{key}"): a preview in the dev channel never
stops the live post.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from us_outbound.clients.http import ApiError
from us_outbound.clients.slack import SlackOff
from us_outbound.context import ConfigError, Context
from us_outbound.logs import log

NO_TOKEN = "no Slack token: posted to the log"
ALERT_EVENT = "alert"  # events.type of a posted alert key (post_once)


def _to_log(channel: str, text: str, reason: str) -> dict[str, Any]:
    log("slack_off", channel=channel, reason=reason, lines=text.splitlines())
    return {"posted": False, "channel": channel, "ts": None, "error": NO_TOKEN}


def alert(ctx: Context, text: str, *, channel: str | None = None) -> dict[str, Any]:
    """Post text; returns {"posted", "channel", "ts", "error"} ("posted" False when only logged)."""
    channel = channel or ctx.settings.general.alert_channel
    try:
        slack = ctx.clients.slack
    except ConfigError as exc:
        return _to_log(channel, text, str(exc))
    if isinstance(slack, SlackOff):
        return _to_log(channel, text, "dry-run without a Slack token")
    try:
        sent = slack.post(channel, text)
    except (ApiError, LookupError) as exc:
        log("slack_post_failed", channel=channel, error=str(exc)[:300], lines=text.splitlines())
        return {"posted": False, "channel": channel, "ts": None, "error": str(exc)[:300]}
    if not sent:
        return {"posted": False, "channel": channel, "ts": None, "error": None}
    return {"posted": True, "channel": sent.get("channel"), "ts": sent.get("ts"), "error": None}


def mention(ctx: Context) -> str:
    """The approvers as Slack mentions, e.g. "<@U098X453UAG> ", or ""."""
    ids = ctx.settings.general.approver_slack_ids
    return "".join(f"<@{i}> " for i in ids)


def _alert_event_id(ctx: Context, key: str) -> str:
    return f"{ALERT_EVENT}:{'dry-run:' if ctx.dry_run else ''}{key}"


def sent_before(ctx: Context, keys: Iterable[str]) -> set[str]:
    """The keys among these that post_once has already posted (in this mode)."""
    ids = {_alert_event_id(ctx, k): k for k in keys}
    if not ids:
        return set()
    return {ids[str(r["event_id"])] for r in ctx.store.select("events", {"event_id": sorted(ids)})}


def post_once(ctx: Context, lines: Sequence[tuple[str, str]], *, head: str, foot: str = "") -> dict[str, Any]:
    """One post of the (key, line) pairs not posted before, under head with the approvers mentioned; returns
    alert()'s answer, the keys it recorded as posted, and the keys a failed post left for the next run."""
    done = sent_before(ctx, [k for k, _ in lines])
    new = [(k, line) for k, line in dict(lines).items() if k not in done]
    if not new:
        return {"posted": False, "channel": None, "ts": None, "error": None, "keys": [], "unsent": []}
    text = "\n".join([f"{mention(ctx)}{head}", *(line for _, line in new), *([foot] if foot else [])])
    sent = alert(ctx, text)
    if not (sent["posted"] or sent["error"] == NO_TOKEN):
        return {**sent, "keys": [], "unsent": [k for k, _ in new]}
    ctx.store.upsert("events", [{"event_id": _alert_event_id(ctx, k), "type": ALERT_EVENT, "occurred_at": ctx.now}
                                for k, _ in new])
    return {**sent, "keys": [k for k, _ in new], "unsent": []}
