"""Slack Web API client with a bot token (SPEC 1.2, 3, 11): alerts, approvals, posts.

Writes go only to the alert channel (#us-outbound) and the dev channel
(#us-outbound-dev). In dry-run (SPEC 0.3) a post meant for another channel goes to the
dev channel instead, its text prefixed "[dry-run → {channel}] ". Ops carry the channel
NAME, since that is what the guard checks; request bodies carry the resolved id.
Slack answers errors with HTTP 200 and ok: false, which raises ApiError here.
"""

from __future__ import annotations

from typing import Any

from us_outbound.clients.guard import Op
from us_outbound.clients.http import ApiError, HttpClient
from us_outbound.logs import log

LIST_PAGE = 200  # Slack recommends no more than 200 per page


def channel_key(name: str) -> str:
    """'#US-Outbound' -> 'us-outbound'."""
    return name.strip().lstrip("#").lower()


class Slack(HttpClient):
    system = "slack"
    base_url = "https://slack.com/api/"

    def __init__(self, guard, transport, token: str = ""):
        super().__init__(guard, transport, token)
        self._ids: dict[str, str] = {}

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=utf-8"}

    def _api(self, http_method: str, method: str, op: Op, *, json: Any = None, params: dict | None = None) -> dict | None:
        """Call a Web API method; None when dry-run skipped the write."""
        body = self.request(http_method, method, op, json=json, params=params)
        if body is None:
            return None
        if not isinstance(body, dict) or not body.get("ok"):
            raise ApiError(self.system, 200, body, self.base_url + method)
        return body

    def _preflight(self, op: Op) -> None:
        """Let the guard reject a channel outside the US Outbound pair before any request (even the id lookup)."""
        b = self.guard.bounds
        if op.target not in {b.alert_channel, b.dev_channel}:
            self.guard.authorize(self.system, op)

    # -- reads -----------------------------------------------------------------

    def channel_id(self, name: str) -> str:
        """Resolve "#name" to its id via conversations.list (public and private), cached."""
        key = channel_key(name)
        if key not in self._ids:
            cursor = ""
            while True:
                params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": LIST_PAGE}
                if cursor:
                    params["cursor"] = cursor
                body = self._api("GET", "conversations.list", Op("conversations.list"), params=params) or {}
                for c in body.get("channels", []):
                    self._ids.setdefault(channel_key(c.get("name", "")), c["id"])
                cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
                if key in self._ids or not cursor:
                    break
        if key not in self._ids:
            raise LookupError(f"Slack channel #{key} not found; is the bot a member of it?")
        return self._ids[key]

    def replies(self, channel: str, ts: str) -> list[dict]:
        """Replies in the thread under message ts, oldest first; the parent message is left out."""
        cid = self.channel_id(channel)
        out: list[dict] = []
        cursor = ""
        while True:
            params = {"channel": cid, "ts": ts, "limit": LIST_PAGE}
            if cursor:
                params["cursor"] = cursor
            body = self._api("GET", "conversations.replies", Op("conversations.replies", target=channel), params=params) or {}
            out.extend(m for m in body.get("messages", []) if m.get("ts") != ts)
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return out

    # -- writes ----------------------------------------------------------------

    def post(self, channel: str, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None) -> dict | None:
        """chat.postMessage. Returns {"channel": name posted to, "channel_id", "ts"}; None if skipped."""
        op = Op("chat.postMessage", target=channel, write=True, detail={"thread_ts": thread_ts} if thread_ts else {})
        dev = self.guard.bounds.dev_channel
        if not self.guard.live and channel != dev:
            # Records the post as not sent, or raises for a channel outside the pair.
            self.guard.authorize(self.system, op)
            note = f"[dry-run → {channel}]"
            text = f"{note} {text}"
            if blocks:
                blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}, *blocks]
            channel = dev
            op = Op("chat.postMessage", target=dev, write=True, detail={**op.detail, "redirected": True})
        else:
            self._preflight(op)
        payload: dict[str, Any] = {
            "channel": self.channel_id(channel),
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        body = self._api("POST", "chat.postMessage", op, json=payload)
        if body is None:
            return None
        return {"channel": channel, "channel_id": body.get("channel"), "ts": body.get("ts")}

    def update(self, channel: str, ts: str, text: str, blocks: list[dict] | None = None) -> dict | None:
        """chat.update on a message this bot posted. Returns {"channel", "channel_id", "ts"}; None if skipped."""
        op = Op("chat.update", target=channel, write=True, detail={"ts": ts})
        self._preflight(op)
        payload: dict[str, Any] = {"channel": self.channel_id(channel), "ts": ts, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        body = self._api("POST", "chat.update", op, json=payload)
        if body is None:
            return None
        return {"channel": channel, "channel_id": body.get("channel"), "ts": body.get("ts")}


class SlackOff:
    """Stands in for Slack while US_OUTBOUND_SLACK_BOT_TOKEN is not set, in dry-run only.

    Harry (30 Sep 2026): leave Slack out while the build starts. Posts are logged instead of
    sent, and nothing is read. Live runs still need the token (Clients.slack), because
    approvals come through Slack (SPEC 1.1, 11). It makes no network call, so it holds no guard.
    """

    system = "slack"

    def post(self, channel: str, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None) -> None:
        log("slack_off", channel=channel, message=text, thread_ts=thread_ts)
        return None

    def update(self, channel: str, ts: str, text: str, blocks: list[dict] | None = None) -> None:
        log("slack_off", channel=channel, message=text, ts=ts)
        return None

    def replies(self, channel: str, ts: str) -> list[dict]:
        return []
