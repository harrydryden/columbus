"""One post to the alert channel, or the log when there is no Slack token (SPEC 11, 12; Harry, 30 Sep 2026).

The kill rules, the hand-check and the daily post all post here. In dry-run the Slack
client sends the post to the dev channel instead (clients/slack.py). Without
US_OUTBOUND_SLACK_BOT_TOKEN, Clients.slack logs posts in dry-run and refuses in live; a
live job that must still act (a kill rule pausing a mailbox) uses `alert`, which logs the
post instead, so the action never waits on Slack. The log gets the post line by line, so a
long post is not clipped to one email body's 200 characters (logs.py); emails in it are
hashed as in every log line. A Slack error is reported, not raised: the caller has done its
work and says in its summary that the post failed.
"""

from __future__ import annotations

from typing import Any

from us_outbound.clients.http import ApiError
from us_outbound.clients.slack import SlackOff
from us_outbound.context import ConfigError, Context
from us_outbound.logs import log

NO_TOKEN = "no Slack token: posted to the log"


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
