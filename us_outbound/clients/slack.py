"""Slack Web API client with a bot token (SPEC 1.2, 3, 11): alerts, approvals, posts.

Writes go only to the alert channel (#us-outbound) and the dev channel
(#us-outbound-dev). In dry-run (SPEC 0.3) a post meant for another channel goes to the
dev channel instead, its text prefixed "[dry-run → {channel}] ". Ops carry the channel
NAME, since that is what the guard checks; request bodies carry the resolved id.
Slack answers errors with HTTP 200 and ok: false, which raises ApiError here.

Approvals (SPEC 11, poll_approvals) read thread replies and the reactions on one message.
Both callers (the reply desk and enrol/approvals.py) read a thread with replies() and then the
reactions on a message in it, so replies() keeps the reactions conversations.replies already
returned with each message (the parent too), and reactions() answers from them when Slack listed
every user of every reaction (count == len(users)), instead of one reactions.get per open item: at
a hundred or so open cards those calls alone passed Slack's per-minute limit and poll_approvals'
timeout. A message read without a reactions list, or read more than REACTIONS_FRESH seconds ago,
is asked for with reactions.get as before; the bot's own react() forgets that message's reactions.
The bot adds reactions of its own (react) only on the two US Outbound channels: it seeds ✅ and
❌ on a send approval's card (enrol/approvals.py; Harry, 2 Oct 2026) and on a reply alert
(replies/poll.py) so deciding is one click, and bot_user_id tells the approvals pass which
reactions are its own. In dry-run a reaction lands only on the dev channel.
Direct messages (dm) go only to the approvers in approver_slack_ids (the guard checks), for
escalation when the forward endpoint is missing; in dry-run they go to the dev channel instead.
"""

from __future__ import annotations

import time
from typing import Any

from us_outbound.clients.guard import Op
from us_outbound.clients.http import ApiError, HttpClient
from us_outbound.logs import log

LIST_PAGE = 200  # Slack recommends no more than 200 per page
REACTIONS_FRESH = 60.0  # seconds a thread read's reactions answer reactions() for its messages


def channel_key(name: str) -> str:
    """'#US-Outbound' -> 'us-outbound'."""
    return name.strip().lstrip("#").lower()


class Slack(HttpClient):
    system = "slack"
    base_url = "https://slack.com/api/"

    def __init__(self, guard, transport, token: str = ""):
        super().__init__(guard, transport, token)
        self._ids: dict[str, str] = {}
        self._bot_user: str | None = None
        # (channel id, message ts) -> (when read, its reactions), kept by replies() for reactions().
        self._reactions: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._clock = time.monotonic  # tests replace it

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
        """Replies in the thread under message ts, oldest first; the parent message is left out.

        The reactions on every message read, the parent's included, are kept for reactions().
        """
        cid = self.channel_id(channel)
        out: list[dict] = []
        cursor = ""
        while True:
            params = {"channel": cid, "ts": ts, "limit": LIST_PAGE}
            if cursor:
                params["cursor"] = cursor
            body = self._api("GET", "conversations.replies", Op("conversations.replies", target=channel), params=params) or {}
            messages = body.get("messages", [])
            self._keep_reactions(cid, messages)
            out.extend(m for m in messages if m.get("ts") != ts)
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return out

    def _keep_reactions(self, cid: str, messages: list[dict]) -> None:
        """Keep each message's reactions when Slack listed every user of each (a long list may carry only some)."""
        now = self._clock()
        for m in messages:
            if not isinstance(m, dict) or not m.get("ts"):
                continue
            key = (cid, str(m["ts"]))
            reactions = m.get("reactions")
            complete = isinstance(reactions, list) and all(
                isinstance(r, dict) and isinstance(r.get("users"), list) and r.get("count") == len(r["users"])
                for r in reactions
            )
            if complete:
                self._reactions[key] = (now, [{**r, "users": list(r["users"])} for r in reactions])
            else:  # no list (Slack leaves it out when there is none, but nothing is assumed) or a cut one
                self._reactions.pop(key, None)

    def reactions(self, channel: str, ts: str) -> list[dict]:
        """The reactions on message ts: [{"name": "white_check_mark", "users": [...], "count": n}].

        From the thread replies() has just read, when Slack listed them all there; otherwise
        reactions.get with full=true, so every user is listed. Needs the reactions:read scope.
        PHASE0-CONFIRM: the scope is on the installed app (deploy/slack-app-manifest.yaml).
        """
        cid = self.channel_id(channel)
        kept = self._reactions.get((cid, str(ts)))
        if kept is not None and self._clock() - kept[0] <= REACTIONS_FRESH:
            return [{**r, "users": list(r["users"])} for r in kept[1]]
        body = self._api(
            "GET", "reactions.get", Op("reactions.get", target=channel),
            params={"channel": cid, "timestamp": ts, "full": "true"},
        ) or {}
        return list((body.get("message") or {}).get("reactions") or [])

    def permalink(self, channel: str, ts: str) -> str:
        """A link to message ts (chat.getPermalink), for escalation emails and DMs."""
        cid = self.channel_id(channel)
        body = self._api(
            "GET", "chat.getPermalink", Op("chat.getPermalink", target=channel), params={"channel": cid, "message_ts": ts}
        ) or {}
        return str(body.get("permalink") or "")

    def auth_test(self) -> dict:
        """auth.test (no scope needed): Slack's answer for this token. Raises ApiError when Slack rejects it
        (invalid_auth, not_authed, token_revoked, account_inactive): the outside watchdog's check that alerts can
        still be posted (ops/watchdog.py)."""
        body = self._api("GET", "auth.test", Op("auth.test")) or {}
        if body.get("user_id"):
            self._bot_user = str(body["user_id"])
        return body

    def bot_user_id(self) -> str:
        """The bot's own Slack user id (auth.test, no scope needed), cached; "" when Slack will not say.

        The approvals pass leaves the bot's own reactions out (the ✅ and ❌ it seeds on a card).
        """
        if self._bot_user is None:
            try:
                body = self.auth_test()
            except ApiError as exc:
                log("slack_auth_test_failed", error=str(exc)[:200])
                return ""
            self._bot_user = str(body.get("user_id") or "")
        return self._bot_user

    # -- writes ----------------------------------------------------------------

    def post(
        self, channel: str, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None,
        *, broadcast: bool = False,
    ) -> dict | None:
        """chat.postMessage. Returns {"channel": name posted to, "channel_id", "ts"}; None if skipped.

        broadcast: a thread reply also shown in the channel (reply_broadcast), as SPEC 11's re-post is.
        """
        op = Op("chat.postMessage", target=channel, write=True, detail={"thread_ts": thread_ts} if thread_ts else {})
        dev = self.guard.bounds.dev_channel
        # The guard refuses a channel outside the pair before any request (even the id lookup).
        if not self.guard.refuse_unless_allowed(self.system, op):  # dry-run: a post for the alert channel
            self.guard.authorize(self.system, op)  # recorded as not sent; it goes to the dev channel instead
            note = f"[dry-run → {channel}]"
            text = f"{note} {text}"
            if blocks:
                blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}, *blocks]
            channel = dev
            op = Op("chat.postMessage", target=dev, write=True, detail={**op.detail, "redirected": True})
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
            if broadcast:
                payload["reply_broadcast"] = True
        body = self._api("POST", "chat.postMessage", op, json=payload)
        if body is None:
            return None
        return {"channel": channel, "channel_id": body.get("channel"), "ts": body.get("ts")}

    def dm(self, user_id: str, text: str) -> dict | None:
        """A direct message to an approver (the guard allows only approver_slack_ids).

        chat.postMessage with the user id as channel, which posts in the app's Messages tab.
        PHASE0-CONFIRM: that the installed app has that tab on and may post there (im:write).
        Dry-run posts it to the dev channel instead, prefixed "[dry-run → DM @user]".
        """
        user = str(user_id or "").strip()
        op = Op("chat.postMessage", target=f"@{user}", write=True, detail={"dm": True})
        if not self.guard.refuse_unless_allowed(self.system, op):  # a non-approver is refused before any request
            self.guard.authorize(self.system, op)  # dry-run: recorded as not sent, and posted to the dev channel
            dev = self.guard.bounds.dev_channel
            return self.post(dev, f"[dry-run → DM @{user}] {text}")
        payload = {"channel": user, "text": text, "unfurl_links": False, "unfurl_media": False}
        body = self._api("POST", "chat.postMessage", op, json=payload)
        if body is None:
            return None
        return {"channel": f"@{user}", "channel_id": body.get("channel"), "ts": body.get("ts")}

    def update(self, channel: str, ts: str, text: str, blocks: list[dict] | None = None) -> dict | None:
        """chat.update on a message this bot posted. Returns {"channel", "channel_id", "ts"}; None if skipped."""
        op = Op("chat.update", target=channel, write=True, detail={"ts": ts})
        self.guard.refuse_unless_allowed(self.system, op)  # a channel outside the pair: before the id lookup
        payload: dict[str, Any] = {"channel": self.channel_id(channel), "ts": ts, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        body = self._api("POST", "chat.update", op, json=payload)
        if body is None:
            return None
        return {"channel": channel, "channel_id": body.get("channel"), "ts": body.get("ts")}

    def react(self, channel: str, ts: str, name: str) -> dict | None:
        """reactions.add: the bot's reaction `name` ("white_check_mark") on message ts. None if dry-run skipped it.

        Needs the reactions:write scope (deploy/slack-app-manifest.yaml). The guard allows it on the
        alert and dev channels only, and in dry-run on the dev channel only. A reaction the bot has
        already added is not an error.
        """
        op = Op("reactions.add", target=channel, write=True, detail={"ts": ts, "name": name})
        self.guard.refuse_unless_allowed(self.system, op)  # a channel outside the pair: before the id lookup
        payload = {"channel": self.channel_id(channel), "timestamp": ts, "name": name}
        self._reactions.pop((payload["channel"], str(ts)), None)  # changed now: asked again next time
        body = self.request("POST", "reactions.add", op, json=payload)
        if body is None:
            return None
        if isinstance(body, dict) and not body.get("ok") and body.get("error") != "already_reacted":
            raise ApiError(self.system, 200, body, self.base_url + "reactions.add")
        return {"channel": channel, "ts": ts, "name": name}


class SlackOff:
    """Stands in for Slack while US_OUTBOUND_SLACK_BOT_TOKEN is not set, in dry-run only.

    Harry (30 Sep 2026): leave Slack out while the build starts. Posts are logged instead of
    sent, and nothing is read. Live runs still need the token (Clients.slack), because
    approvals come through Slack (SPEC 1.1, 11). It makes no network call, so it holds no guard.
    """

    system = "slack"

    def post(
        self, channel: str, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None,
        *, broadcast: bool = False,
    ) -> None:
        log("slack_off", channel=channel, message=text, thread_ts=thread_ts)
        return None

    def dm(self, user_id: str, text: str) -> None:
        log("slack_off", channel=f"@{user_id}", message=text)
        return None

    def update(self, channel: str, ts: str, text: str, blocks: list[dict] | None = None) -> None:
        log("slack_off", channel=channel, message=text, ts=ts)
        return None

    def react(self, channel: str, ts: str, name: str) -> None:
        log("slack_off", channel=channel, reaction=name, ts=ts)
        return None

    def replies(self, channel: str, ts: str) -> list[dict]:
        return []

    def reactions(self, channel: str, ts: str) -> list[dict]:
        return []

    def permalink(self, channel: str, ts: str) -> str:
        return ""

    def auth_test(self) -> dict:
        return {"ok": True, "off": True}  # no token, by design in dry-run: nothing to test

    def bot_user_id(self) -> str:
        return ""
