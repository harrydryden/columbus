"""Slack client: channel resolution, dry-run redirect, ok:false errors, thread replies."""

import pytest

from tests.fakes import FakeTransport
from us_outbound.clients.guard import Boundaries, Guard, GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.clients.slack import Slack

CHANNELS = [
    {"id": "C_GENERAL", "name": "general"},
    {"id": "C_ALERT", "name": "us-outbound"},
    {"id": "C_DEV", "name": "us-outbound-dev"},
]


def make(live=True):
    transport = FakeTransport()
    pages = {None: {"ok": True, "channels": CHANNELS[:1], "response_metadata": {"next_cursor": "p2"}},
             "p2": {"ok": True, "channels": CHANNELS[1:], "response_metadata": {"next_cursor": ""}}}
    transport.route("GET", "conversations.list", fn=lambda req: pages[req.params.get("cursor")])
    transport.route("POST", "chat.postMessage", fn=lambda req: {"ok": True, "channel": req.json["channel"], "ts": "111.222"})
    transport.route("POST", "chat.update", fn=lambda req: {"ok": True, "channel": req.json["channel"], "ts": req.json["ts"]})
    guard = Guard(live=live, bounds=Boundaries())
    return Slack(guard, transport, "xoxb-test"), transport, guard


def test_channel_id_paginates_and_caches():
    slack, t, _ = make()
    assert slack.channel_id("#us-outbound") == "C_ALERT"
    assert slack.channel_id("US-Outbound-Dev") == "C_DEV"
    lists = [r for r in t.requests if "conversations.list" in r.url]
    assert len(lists) == 2
    assert lists[0].params["types"] == "public_channel,private_channel"
    assert lists[1].params["cursor"] == "p2"
    assert lists[0].headers["Authorization"] == "Bearer xoxb-test"
    with pytest.raises(LookupError):
        slack.channel_id("#missing")


def test_live_post_to_alert_channel():
    slack, t, guard = make(live=True)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    out = slack.post("#us-outbound", "Positive reply", blocks=blocks, thread_ts="1.0")
    assert out == {"channel": "#us-outbound", "channel_id": "C_ALERT", "ts": "111.222"}
    post = t.writes()[0]
    assert post.url == "https://slack.com/api/chat.postMessage"
    assert post.json["channel"] == "C_ALERT"
    assert (post.json["text"], post.json["blocks"], post.json["thread_ts"]) == ("Positive reply", blocks, "1.0")
    assert post.headers["Content-Type"] == "application/json; charset=utf-8"
    [rec] = guard.writes("slack")
    assert (rec.target, rec.sent) == ("#us-outbound", True)


def test_dry_run_post_redirects_to_dev_with_prefix():
    slack, t, guard = make(live=False)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    out = slack.post("#us-outbound", "Positive reply", blocks=blocks)
    assert out["channel"] == "#us-outbound-dev"
    [post] = t.writes()
    assert post.json["channel"] == "C_DEV"
    assert post.json["text"] == "[dry-run → #us-outbound] Positive reply"
    assert post.json["blocks"][0] == {"type": "context", "elements": [{"type": "mrkdwn", "text": "[dry-run → #us-outbound]"}]}
    assert post.json["blocks"][1:] == blocks
    assert [(r.target, r.sent) for r in guard.writes("slack")] == [("#us-outbound", False), ("#us-outbound-dev", True)]


def test_dry_run_post_to_dev_channel_has_no_prefix():
    slack, t, _ = make(live=False)
    slack.post("#us-outbound-dev", "Sync failed")
    [post] = t.writes()
    assert (post.json["channel"], post.json["text"]) == ("C_DEV", "Sync failed")


@pytest.mark.parametrize("live", [True, False])
def test_post_to_other_channel_refused_before_any_request(live):
    slack, t, _ = make(live=live)
    with pytest.raises(GuardViolation):
        slack.post("#general", "hello")
    with pytest.raises(GuardViolation):
        slack.update("#general", "1.0", "hello")
    assert t.requests == []


def test_ok_false_raises():
    slack, t, _ = make(live=True)
    t.route("POST", "chat.postMessage", body={"ok": False, "error": "not_in_channel"})
    with pytest.raises(ApiError) as err:
        slack.post("#us-outbound", "hi")
    assert "not_in_channel" in str(err.value)


def test_update_live_and_dry_run():
    slack, t, _ = make(live=True)
    assert slack.update("#us-outbound", "5.5", "Handled")["ts"] == "5.5"
    assert t.writes()[0].json == {"channel": "C_ALERT", "ts": "5.5", "text": "Handled"}

    slack, t, _ = make(live=False)
    assert slack.update("#us-outbound", "5.5", "Handled") is None
    assert t.writes() == []
    assert slack.update("#us-outbound-dev", "6.6", "Handled")["channel"] == "#us-outbound-dev"


def test_replies_paginate_and_skip_parent():
    slack, t, _ = make()
    pages = {
        None: {"ok": True, "messages": [{"ts": "1.0", "text": "alert"}, {"ts": "1.1", "user": "U_HARRY", "text": "send"}],
               "has_more": True, "response_metadata": {"next_cursor": "c2"}},
        "c2": {"ok": True, "messages": [{"ts": "1.2", "user": "U_SAM", "text": "skip"}], "has_more": False},
    }
    t.route("GET", "conversations.replies", fn=lambda req: pages[req.params.get("cursor")])
    got = slack.replies("#us-outbound", "1.0")
    assert [m["ts"] for m in got] == ["1.1", "1.2"]
    calls = [r for r in t.requests if "conversations.replies" in r.url]
    assert calls[0].params["channel"] == "C_ALERT" and calls[0].params["ts"] == "1.0"
    assert calls[1].params["cursor"] == "c2"


def _calls(t, method):
    return [r for r in t.requests if r.url.endswith(method)]


def test_reactions_after_replies_come_from_the_thread_read():
    """poll_approvals reads each open item's thread, then the reactions on its card: one Slack read per item, not two."""
    slack, t, _ = make()
    tick = {"name": "white_check_mark", "users": ["U_BOT", "U_HARRY"], "count": 2}
    t.route("GET", "conversations.replies", fn=lambda req: {"ok": True, "messages": [
        {"ts": req.params["ts"], "text": "card", "reactions": [tick, {"name": "x", "users": ["U_BOT"], "count": 1}]},
        {"ts": req.params["ts"] + "1", "text": "edited version", "reactions": [tick]},
    ]})
    t.route("GET", "reactions.get", body={"ok": True, "message": {"reactions": []}})
    for i in range(120):
        parent = f"{1000 + i}.000100"
        assert [m["ts"] for m in slack.replies("#us-outbound", parent)] == [parent + "1"]  # the parent is left out
        assert slack.reactions("#us-outbound", parent)[0] == tick
        assert slack.reactions("#us-outbound", parent + "1") == [tick]  # a reply in the thread too
    assert len(_calls(t, "conversations.replies")) == 120 and _calls(t, "reactions.get") == []


@pytest.mark.parametrize("message", [
    {"ts": "1.0", "text": "card"},  # Slack sent no reactions list: asked, never assumed empty
    {"ts": "1.0", "reactions": [{"name": "white_check_mark", "users": ["U_BOT"], "count": 3}]},  # not every user listed
])
def test_reactions_are_asked_for_when_the_thread_read_does_not_list_them_all(message):
    slack, t, _ = make()
    t.route("GET", "conversations.replies", body={"ok": True, "messages": [message]})
    full = [{"name": "white_check_mark", "users": ["U_BOT", "U_HARRY", "U_SAM"], "count": 3}]
    t.route("GET", "reactions.get", body={"ok": True, "message": {"reactions": full}})
    slack.replies("#us-outbound", "1.0")
    assert slack.reactions("#us-outbound", "1.0") == full
    assert len(_calls(t, "reactions.get")) == 1


def test_kept_reactions_go_stale_and_the_bots_own_reaction_drops_them():
    slack, t, _ = make()
    clock = [100.0]
    slack._clock = lambda: clock[0]
    t.route("GET", "conversations.replies", body={"ok": True, "messages": [
        {"ts": "1.0", "reactions": [{"name": "x", "users": ["U_HARRY"], "count": 1}]}]})
    t.route("GET", "reactions.get", body={"ok": True, "message": {"reactions": []}})
    t.route("POST", "reactions.add", body={"ok": True})
    slack.replies("#us-outbound", "1.0")
    clock[0] += 61  # older than REACTIONS_FRESH
    assert slack.reactions("#us-outbound", "1.0") == [] and len(_calls(t, "reactions.get")) == 1
    slack.replies("#us-outbound", "1.0")
    slack.react("#us-outbound", "1.0", "white_check_mark")
    assert slack.reactions("#us-outbound", "1.0") == [] and len(_calls(t, "reactions.get")) == 2
    slack.replies("#us-outbound", "1.0")
    assert slack.reactions("#us-outbound", "1.0")[0]["name"] == "x" and len(_calls(t, "reactions.get")) == 2


# -- approvals and escalation (SPEC 11; D11) ---------------------------------------------------------------


def test_reactions_and_permalink_are_reads():
    slack, t, guard = make(live=False)
    t.route("GET", "reactions.get", body={"ok": True, "message": {"reactions": [
        {"name": "white_check_mark", "users": ["U_HARRY"], "count": 1}]}})
    t.route("GET", "chat.getPermalink", body={"ok": True, "permalink": "https://spill.slack.com/archives/C_ALERT/p1"})
    assert slack.reactions("#us-outbound", "1.0") == [{"name": "white_check_mark", "users": ["U_HARRY"], "count": 1}]
    assert slack.permalink("#us-outbound", "1.0") == "https://spill.slack.com/archives/C_ALERT/p1"
    got = [r for r in t.requests if "reactions.get" in r.url][0]
    assert got.params == {"channel": "C_ALERT", "timestamp": "1.0", "full": "true"}
    assert t.writes() == [] and guard.writes("slack") == []


def test_a_broadcast_thread_reply():
    slack, t, _ = make(live=True)
    slack.post("#us-outbound", "Still waiting", thread_ts="1.0", broadcast=True)
    slack.post("#us-outbound", "Sent", thread_ts="1.0")
    first, second = t.writes()
    assert first.json["reply_broadcast"] is True and "reply_broadcast" not in second.json


def approver_slack():
    slack, t, guard = make(live=True)
    guard.configure(bounds=Boundaries(approver_slack_ids=frozenset({"U_HARRY"})))
    return slack, t, guard


def test_dm_goes_only_to_an_approver():
    slack, t, guard = approver_slack()
    assert slack.dm("U_HARRY", "Waiting 24 hours")["channel"] == "@U_HARRY"
    [post] = t.writes()
    assert (post.json["channel"], post.json["text"]) == ("U_HARRY", "Waiting 24 hours")
    with pytest.raises(GuardViolation):
        slack.dm("U_SAM", "hello")
    assert len(t.requests) == 1


def test_react_seeds_a_reaction_on_the_alert_channel_and_in_dry_run_only_on_dev():
    """Send approvals (Harry, 2 Oct 2026): the bot puts ✅ and ❌ on each card, so approving is one click."""
    slack, t, guard = make(live=True)
    t.route("POST", "reactions.add", body={"ok": True})
    assert slack.react("#us-outbound", "1.0", "white_check_mark") == {"channel": "#us-outbound", "ts": "1.0",
                                                                       "name": "white_check_mark"}
    [req] = t.writes()
    assert req.url == "https://slack.com/api/reactions.add"
    assert req.json == {"channel": "C_ALERT", "timestamp": "1.0", "name": "white_check_mark"}
    t.route("POST", "reactions.add", body={"ok": False, "error": "already_reacted"})
    assert slack.react("#us-outbound", "1.0", "white_check_mark")  # already there: not an error
    t.route("POST", "reactions.add", body={"ok": False, "error": "message_not_found"})
    with pytest.raises(ApiError, match="message_not_found"):
        slack.react("#us-outbound", "1.0", "x")
    with pytest.raises(GuardViolation):
        slack.react("#general", "1.0", "x")

    slack, t, guard = make(live=False)
    t.route("POST", "reactions.add", body={"ok": True})
    assert slack.react("#us-outbound", "1.0", "x") is None  # dry-run: never on the alert channel
    assert slack.react("#us-outbound-dev", "2.0", "x")["channel"] == "#us-outbound-dev"
    assert [r.json["channel"] for r in t.writes()] == ["C_DEV"]
    assert [(r.target, r.sent) for r in guard.writes("slack")] == [("#us-outbound", False), ("#us-outbound-dev", True)]


def test_bot_user_id_is_asked_once_and_never_raises():
    slack, t, _ = make()
    t.route("GET", "auth.test", body={"ok": True, "user_id": "U_BOT"})
    assert slack.bot_user_id() == "U_BOT" and slack.bot_user_id() == "U_BOT"
    assert len([r for r in t.requests if "auth.test" in r.url]) == 1
    slack, t, _ = make()
    t.route("GET", "auth.test", body={"ok": False, "error": "invalid_auth"})
    assert slack.bot_user_id() == ""


def test_slack_off_reacts_to_the_log():
    from us_outbound.clients.slack import SlackOff

    off = SlackOff()
    assert off.react("#us-outbound", "1.0", "x") is None and off.bot_user_id() == ""


def test_dm_in_dry_run_goes_to_the_dev_channel():
    slack, t, guard = approver_slack()
    guard.configure(live=False)
    slack.dm("U_HARRY", "Waiting 24 hours")
    [post] = t.writes()
    assert (post.json["channel"], post.json["text"]) == ("C_DEV", "[dry-run → DM @U_HARRY] Waiting 24 hours")
    assert [(r.target, r.sent) for r in guard.writes("slack")] == [("@U_HARRY", False), ("#us-outbound-dev", True)]
    with pytest.raises(GuardViolation):
        slack.dm("U_SAM", "hello")
