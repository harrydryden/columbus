"""The reply desk (replies/desk.py): poll_approvals, the CLI path, re-posts and escalation (SPEC 11; D11)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_hubspot_writes import HANNAH, NOW, Transport, reply_row, world
from us_outbound.clients.http import Response
from us_outbound.context import Secrets
from us_outbound.ops import bootstrap, cli
from us_outbound.replies import desk
from us_outbound.replies.items import ReplyItem, claim

ITEM = "item-0001-aaaa"
ALERT_TS = "100.000100"
SAM = "sam@meetspill.org"
HARRY_ID, HANNAH_ID, SAM_ID = "U_HARRY", "U_HANNAH", "U_SAM"
BOT_ID = "U_BOT"


@pytest.fixture
def settings(default_settings):
    """Harry approves everything; Hannah and Sam have Slack ids on their Mailboxes rows (D11)."""
    ids = {"hannah@meetspill.org": HANNAH_ID, SAM: SAM_ID, "harry@meetspill.org": HARRY_ID, "harry@tryspill.org": HARRY_ID}
    boxes = tuple(dataclasses.replace(m, slack_id=ids[m.address]) for m in default_settings.mailboxes)
    return dataclasses.replace(default_settings, mailboxes=boxes)


class Slack:
    """A Slack workspace behind the transport: one thread per alert, reactions per message, posts recorded."""

    def __init__(self, t: Transport):
        self.thread: dict[str, list[dict]] = {ALERT_TS: []}
        self.reactions: dict[str, list[dict]] = {}
        self.posts: list[dict] = []
        self._n = 0
        t.route("GET", "conversations.list", {"ok": True, "channels": [{"id": "C_ALERT", "name": "us-outbound"},
                                                                      {"id": "C_DEV", "name": "us-outbound-dev"}]})
        t.route("GET", "conversations.replies", fn=lambda r: {
            "ok": True, "messages": [{"ts": r.params["ts"], "text": "alert"}] + self.thread.get(r.params["ts"], [])})
        t.route("GET", "reactions.get", fn=lambda r: {
            "ok": True, "message": {"reactions": self.reactions.get(r.params["timestamp"], [])}})
        t.route("GET", "chat.getPermalink", fn=lambda r: {
            "ok": True, "permalink": f"https://spill.slack.com/archives/{r.params['channel']}/p{r.params['message_ts']}"})
        t.route("POST", "chat.postMessage", fn=self._post)
        t.route("GET", "auth.test", {"ok": True, "user_id": BOT_ID})
        t.route("POST", "reactions.add", fn=lambda r: self.react(r.json["name"], BOT_ID, ts=r.json["timestamp"]) or {"ok": True})

    def _ts(self) -> str:
        """One clock for every message, as Slack's own ts: seconds and six digits of microseconds."""
        self._n += 1
        return f"{1000 + self._n}.000100"

    def _post(self, req) -> dict:
        ts = self._ts()
        self.posts.append({**req.json, "ts": ts})
        if req.json.get("thread_ts"):
            self.thread.setdefault(req.json["thread_ts"], []).append(
                {"ts": ts, "bot_id": "B1", "text": req.json["text"]})
        return {"ok": True, "channel": req.json["channel"], "ts": ts}

    def say(self, user: str, text: str, parent: str = ALERT_TS) -> None:
        self.thread.setdefault(parent, []).append({"ts": self._ts(), "user": user, "text": text})

    def react(self, name: str, *users: str, ts: str = ALERT_TS) -> None:
        self.reactions.setdefault(ts, []).append({"name": name, "users": list(users), "count": len(users)})

    def texts(self) -> list[str]:
        return [p["text"] for p in self.posts]


class Instantly:
    """Instantly's campaigns, emails, reply and forward endpoints."""

    def __init__(self, t: Transport):
        self.replies: list[dict] = []
        self.forwards: list[dict] = []
        self.reply_status = 200
        self.forward_status = 200
        self.campaign_of = {"em-1": "c-hannah"}
        t.route("GET", "api.instantly.ai/api/v2/campaigns", {"items": [
            {"id": "c-hannah", "name": "US Outbound – Hannah Spalding"}, {"id": "c-eu", "name": "EU Outbound – Anna"}]})
        t.route("GET", "api.instantly.ai/api/v2/emails/", fn=lambda r: {
            "id": r.url.rsplit("/", 1)[1], "eaccount": HANNAH, "subject": "A simpler way to support your team",
            "campaign_id": self.campaign_of.get(r.url.rsplit("/", 1)[1])})
        t.route("POST", "/emails/reply", fn=self._reply)
        t.route("POST", "/emails/forward", fn=self._forward)
        t.route("POST", "/leads/update-interest-status", {"status": "ok"})

    def _reply(self, req):
        if self.reply_status != 200:
            return Response(self.reply_status, {"error": "busy"})
        self.replies.append(req.json)
        return {"id": f"sent-{len(self.replies)}"}

    def _forward(self, req):
        if self.forward_status != 200:
            return Response(self.forward_status, {"error": "Not Found"})
        self.forwards.append(req.json)
        return {"id": f"fwd-{len(self.forwards)}"}


def desk_world(settings, *, live=True, now=NOW, row=None, slack=True, job="poll_approvals"):
    ctx, t, crm = world(settings, live=live, now=now, row=row)
    ctx.job = job
    ctx.guard.configure(job=job)
    sl, inst = Slack(t), Instantly(t)
    if not slack:  # no US_OUTBOUND_SLACK_BOT_TOKEN, as may be so for the pilot
        ctx.clients.secrets = Secrets(ctx.guard, fetch=lambda name: "" if "SLACK" in name else f"test-{name}")
    return ctx, t, crm, sl, inst


def row_of(ctx, item_id=ITEM) -> dict:
    return ctx.store.get("hitl_items", item_id=item_id)


def poll(ctx) -> dict:
    return desk.poll_approvals(ctx)


# -- approving in Slack ------------------------------------------------------------------------------


def test_send_in_the_thread_sends_the_draft_from_the_mailbox_written_to(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    sl.say(HARRY_ID, "Send")
    out = poll(ctx)
    [sent] = inst.replies
    draft = reply_row()["payload"]["draft"]
    assert (sent["eaccount"], sent["reply_to_uuid"], sent["body"]["text"]) == (HANNAH, "em-1", draft)
    assert sent["subject"] == "Re: A simpler way to support your team"
    row = row_of(ctx)
    assert (row["status"], row["handled_by"], row["handled_at"]) == ("handled", HARRY_ID, NOW)
    assert row["payload"]["desk"]["sent"]["email_id"] == "sent-1" and row["payload"]["sent_text"] == draft
    event = ctx.store.get("events", event_id="sent-1")
    assert (event["type"], event["approval"], event["approved_by"], event["mailbox"]) == (
        "reply_sent", "approved", HARRY_ID, HANNAH)
    assert event["step"] is None and ctx.store.select("events", {"type": "sent"}) == []  # no campaign send
    assert any(t_.startswith(":white_check_mark: *Sent* from hannah@meetspill.org at 16:00 UK, approved by <@U_HARRY>. "
                             "Nothing more to do.") for t_ in sl.texts())  # SPEC 11's words
    assert any(t_.startswith(":link: *Added to HubSpot* (the company, the contact and this reply): "
                             "<https://app.hubspot.com/contacts/8481055/record/0-2/") and
               t_.endswith("|open the company in HubSpot>") for t_ in sl.texts())
    assert crm.objects["companies"] and out["sent"][0]["approval"] == "approved"
    # Idempotent: the next run sends nothing more.
    poll(ctx)
    assert len(inst.replies) == 1


def test_a_tick_from_the_owner_approves_her_own_mailbox_d11(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    sl.react("white_check_mark", HANNAH_ID)
    poll(ctx)
    assert len(inst.replies) == 1
    assert row_of(ctx)["handled_by"] == HANNAH_ID
    assert ctx.store.get("events", event_id="sent-1")["approved_by"] == HANNAH_ID


def test_only_approvers_count(settings):
    """Sam is an approver only for his own mailbox; someone else's ✅ or "send" is ignored."""
    ctx, t, crm, sl, inst = desk_world(settings)
    sl.say(SAM_ID, "send")
    sl.say("U_SOMEONE", "send")
    sl.react("white_check_mark", SAM_ID, "U_SOMEONE")
    out = poll(ctx)
    assert inst.replies == [] and row_of(ctx)["status"] == "open"
    assert out["ignored_non_approvers"] == 4
    sl.say(HARRY_ID, "send")  # Harry is an approver for every mailbox
    poll(ctx)
    assert len(inst.replies) == 1


def test_edit_replaces_the_draft_and_needs_a_fresh_approval(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    original = reply_row()["payload"]["draft"]
    new = "Hi Jane,\n\nThanks for getting back to me. Harry runs our US demos: https://meetings.hubspot.com/harry336/us-demo-link\n\nBest wishes,\nHannah"
    sl.react("white_check_mark", HARRY_ID)  # on the old draft
    sl.say(HANNAH_ID, f"edit: {new.replace('https://meetings.hubspot.com/harry336/us-demo-link', '<https://meetings.hubspot.com/harry336/us-demo-link>')}")
    poll(ctx)
    assert inst.replies == []  # the ✅ was on the draft before the edit
    row = row_of(ctx)
    assert row["payload"]["draft"] == new and row["payload"]["draft_original"] == original
    repost = next(p for p in sl.posts if p["text"].startswith(":pencil2: *Draft changed by <@U_HANNAH>.* To send this version: ✅ on this message, or "
                                                        "reply `send`. To send nothing: ❌ or `skip`."))
    assert row["payload"]["desk"]["approve_ts"] == repost["ts"]
    sl.react("white_check_mark", HARRY_ID, ts=repost["ts"])
    poll(ctx)
    [sent] = inst.replies
    assert sent["body"]["text"] == new
    assert ctx.store.get("events", event_id="sent-1")["approval"] == "edited"
    assert any("(the edited version)" in x for x in sl.texts())


def test_send_colon_sends_that_text(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    sl.say(HARRY_ID, "send: Hi Jane,\n\nWould Tuesday at 11am ET work? &amp; <mailto:harry@spill.chat|harry@spill.chat>\n\nBest wishes,\nHannah")
    poll(ctx)
    [sent] = inst.replies
    assert sent["body"]["text"] == "Hi Jane,\n\nWould Tuesday at 11am ET work? & harry@spill.chat\n\nBest wishes,\nHannah"
    assert ctx.store.get("events", event_id="sent-1")["approval"] == "edited"


@pytest.mark.parametrize("how", ["cross", "word", "cross beats tick"])
def test_skip_marks_it_handled_and_sends_nothing(settings, how):
    ctx, t, crm, sl, inst = desk_world(settings)
    if how == "word":
        sl.say(HARRY_ID, "skip")
    else:
        sl.react("x", HARRY_ID)
        if how == "cross beats tick":
            sl.react("white_check_mark", HANNAH_ID)
    out = poll(ctx)
    assert inst.replies == [] and out["skipped"][0]["by"] == HARRY_ID
    assert row_of(ctx)["status"] == "handled"
    replied = ctx.store.get("events", event_id="em-1")
    assert (replied["approval"], replied["approved_by"]) == ("skipped", HARRY_ID)
    assert any(x == ":white_check_mark: *Closed without sending* (skipped by <@U_HARRY>). If they need an answer, "
                    "reply to them yourself from hannah@meetspill.org." for x in sl.texts())


def test_the_copy_rules_block_a_send(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    sl.say(HARRY_ID, "send: Hi Jane, our licensed therapists offer unlimited sessions.")
    out = poll(ctx)
    assert inst.replies == [] and row_of(ctx)["status"] == "open"
    assert out["not_sent"] and any("therap" in w for w in out["not_sent"][0]["why"])
    [note] = [x for x in sl.texts() if x.startswith(":warning: *Not sent:* ")]
    assert "therap" in note and note.endswith("\n*What to do:* reply `edit: &lt;fixed text&gt;` and then `send`, or "
                                              "`send: &lt;fixed text&gt;` to send your version straight away.")
    poll(ctx)  # not retried: the "send" was read, and no ✅ counts any more
    assert inst.replies == [] and sum(x.startswith(":warning: *Not sent:*") for x in sl.texts()) == 1


def test_a_failed_send_is_never_retried_without_a_person(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    inst.reply_status = 500
    sl.react("white_check_mark", HARRY_ID)
    poll(ctx)
    row = row_of(ctx)
    assert row["status"] == "open" and "500" in row["payload"]["desk"]["failed"]["error"]
    assert "sending" not in row["payload"]["desk"]
    [note] = [x for x in sl.texts() if "Instantly answered" in x]
    assert note.startswith(":warning: *Probably not sent: Instantly answered with an error* (")
    assert note.endswith("*What to do:* look in the Sent folder of hannah@meetspill.org. Not there: reply `send` to try "
                         "again. There: reply `skip` to close this.")
    inst.reply_status = 200
    poll(ctx)  # the ✅ is still on the alert, but it no longer counts
    assert inst.replies == []
    sl.say(HARRY_ID, "send")
    poll(ctx)
    assert len(inst.replies) == 1


def test_a_reply_outside_a_us_outbound_thread_is_closed_for_a_person_and_the_run_goes_on(settings):
    """M2 (code-replies-crm; RS §2 W1-2): a colleague's reply whose thread carries no campaign id is refused by the
    guard (Instantly.reply answers only in a US Outbound thread). Until 19 Oct 2026 the GuardViolation crashed
    poll_approvals (this test asserted the crash), so the escalation, the send approvals and the hand-check tab were
    not processed either. Now: that item is handled "by hand" with why, one alert says so, the other item is sent and
    the run finishes."""
    ctx, t, crm, sl, inst = desk_world(settings)
    inst.campaign_of["em-1"] = None  # Instantly's campaign_id "can be null" (OpenAPI)
    second = reply_row(instantly_email_id="em-2", slack_ts="200.000200")
    second.update(item_id="item-0002-bbbb", event_id="em-2", created_at=NOW - timedelta(minutes=10))
    ctx.store.upsert("hitl_items", [second])
    inst.campaign_of["em-2"] = "c-hannah"
    sl.thread["200.000200"] = []
    sl.say(HARRY_ID, "send")
    sl.say(HARRY_ID, "send", parent="200.000200")
    out = poll(ctx)  # no exception: exit 0
    row = row_of(ctx)
    assert (row["status"], row["handled_by"]) == ("handled", "by hand")
    assert "US Outbound" in row["payload"]["refused"] and out["by_hand"][0]["item"] == ITEM[:8]
    replied = ctx.store.get("events", event_id="em-1")
    assert (replied["approval"], replied["approved_by"]) == ("by hand", "guard")
    # The other item went, from its own thread, and the rest of the desk ran.
    [sent] = inst.replies
    assert sent["reply_to_uuid"] == "em-2" and row_of(ctx, "item-0002-bbbb")["status"] == "handled"
    assert "hand_check" in out and "send_approvals" in out
    [alert] = [x for x in sl.texts() if x.startswith(":warning: *Answer ")]
    assert "by hand*" in alert.splitlines()[0]
    assert any("refused by a guardrail" in e for e in out["errors"])  # heartbeat_check tells Harry too
    poll(ctx)  # handled: neither retried nor alerted again
    assert len(inst.replies) == 1 and len([x for x in sl.texts() if x.startswith(":warning: *Answer ")]) == 1


def test_an_opt_out_closes_the_contacts_open_reply_items(settings):
    """M1: an item open since 15:30 UK; the contact opts out at 16:00 (a later reply, or Instantly's link). The item is
    closed without sending (approval skipped by opt_out), so a ✅ given after it sends nothing."""
    from us_outbound.replies import optout

    ctx, t, crm, sl, inst = desk_world(settings)
    contact = ctx.store.get("contacts", contact_id="con-1")
    result = optout.opt_out(ctx, marker="unsubscribed:em-9", addresses=[contact["email"]], contact=contact,
                            account_id="acc-1", mailbox=HANNAH, occurred_at=NOW, source="reply")
    assert result == "done"
    row = row_of(ctx)
    assert (row["status"], row["handled_by"]) == ("handled", "opt_out")
    assert row["payload"]["closed_by_opt_out"]["marker"] == "unsubscribed:em-9"
    replied = ctx.store.get("events", event_id="em-1")
    assert (replied["approval"], replied["approved_by"]) == ("skipped", "opt_out")
    sl.react("white_check_mark", HARRY_ID)
    poll(ctx)
    assert inst.replies == []


def test_an_opt_out_leaves_the_item_for_the_email_it_came_in(settings):
    """keep_item: a warm reply that also asked to stop keeps its own item open for a person."""
    from us_outbound.replies import optout

    ctx, *_ = desk_world(settings)
    contact = ctx.store.get("contacts", contact_id="con-1")
    optout.opt_out(ctx, marker="unsubscribed:em-1", addresses=[contact["email"]], contact=contact, account_id="acc-1",
                   mailbox=HANNAH, occurred_at=NOW, source="reply", keep_item=ITEM)
    assert row_of(ctx)["status"] == "open"


def test_send_reply_refuses_an_address_suppressed_since(settings):
    """M1: the item is still open (the warm reply that also asked to stop, or a suppression from HubSpot's opt-out
    list since), and an approver ticks it: nothing goes, why suppressed_since, one thread note, the tick is spent."""
    from us_outbound import suppression

    row = reply_row(also_asked_to_stop=True)
    ctx, t, crm, sl, inst = desk_world(settings, row=row)
    suppression.add(ctx.store, email=ctx.store.get("contacts", contact_id="con-1")["email"], reason="unsubscribe",
                    source="reply", now=NOW)
    sl.react("white_check_mark", HARRY_ID)
    out = poll(ctx)
    assert inst.replies == [] and out["not_sent"][0]["why"] == desk.SUPPRESSED_SINCE == "suppressed_since"
    assert row_of(ctx)["status"] == "open"  # for Harry to answer by hand, then skip
    [note] = [x for x in sl.texts() if x.startswith(":warning: *Not sent: they are on our do-not-contact list now*")]
    assert "reply to them yourself from hannah@meetspill.org" in note
    poll(ctx)
    assert inst.replies == [] and len([x for x in sl.texts() if "do-not-contact list now" in x]) == 1
    # The command line takes the same path.
    res = desk.approve(ctx, ITEM)
    assert res["sent"] is False and res["why"] == "suppressed_since" and inst.replies == []


def test_a_contact_marked_suppressed_is_refused_too(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    ctx.store.update("contacts", {"contact_id": "con-1"}, {"suppressed": True, "suppressed_reason": "bounce"})
    sl.say(HARRY_ID, "send")
    out = poll(ctx)
    assert inst.replies == [] and out["not_sent"][0]["why"] == "suppressed_since"


def test_claim_lets_only_one_run_send(settings):
    ctx, *_ = desk_world(settings)
    item = ReplyItem(row_of(ctx))
    assert claim(ctx.store, item, "sending") is True
    assert claim(ctx.store, item, "sending") is False  # read as open, but another run moved it


def test_an_item_left_sending_goes_back_to_a_person(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    row = reply_row()
    row.update(status="sending")
    row["payload"]["desk"] = {"sending": {"at": (NOW - timedelta(minutes=20)).isoformat(), "by": HARRY_ID}}
    ctx.store.upsert("hitl_items", [row])
    sl.react("white_check_mark", HARRY_ID)
    out = poll(ctx)
    assert out["unsure"] == [ITEM[:8]] and inst.replies == []
    assert row_of(ctx)["status"] == "open" and row_of(ctx)["payload"]["desk"]["unsure"]["by"] == HARRY_ID
    assert (":warning: *Not sure the answer went out:* the run sending it stopped part-way.\n*What to do:* look in the "
            "Sent folder of hannah@meetspill.org. Not there: reply `send` to send it. There: reply `skip` to close this."
            in sl.texts())
    poll(ctx)
    assert inst.replies == []  # the old ✅ does not resend it


def test_dry_run_reports_and_changes_nothing(settings):
    ctx, t, crm, sl, inst = desk_world(settings, live=False)
    sl.react("white_check_mark", HARRY_ID)
    before = row_of(ctx)
    out = poll(ctx)
    assert out["dry_run"] and out["would"] == [{"item": ITEM[:8], "action": "send", "by": HARRY_ID, "via": "✅",
                                                "edited": False}]
    assert inst.replies == [] and sl.posts == [] and crm.objects["companies"] == {}
    assert row_of(ctx) == before and ctx.store.select("events", {"type": ["sent", "reply_sent"]}) == []


# -- re-posting (SPEC 11; D11: until 23:00 UK) ---------------------------------------------------------


@pytest.mark.parametrize("uk_hour, minutes_old, reposted", [
    (22, 150, True),   # 22:00 UK: D11's window runs to 23:00 (SPEC 11 had 21:00, 4 pm ET)
    (13, 125, True),
    (23, 150, False),  # the window has closed
    (12, 300, False),  # not open yet
    (16, 60, False),   # under 2 hours
])
def test_repost_after_two_hours_between_13_and_23_uk(settings, uk_hour, minutes_old, reposted):
    now = datetime(2026, 10, 5, uk_hour - 1, 30, tzinfo=UTC)  # BST: UK = UTC + 1
    row = reply_row()
    row["created_at"] = now - timedelta(minutes=minutes_old)
    ctx, t, crm, sl, inst = desk_world(settings, now=now, row=row)
    poll(ctx)
    posts = [p for p in sl.posts if p.get("reply_broadcast")]
    assert bool(posts) is reposted
    if reposted:
        text = posts[0]["text"]
        hours = minutes_old // 60
        assert posts[0]["thread_ts"] == ALERT_TS and text.startswith(
            f"<@U_HANNAH> <@U_HARRY> :raised_hand: *Still waiting after {hours} hours: a reply from Jane Doe at Acme "
            "Creative (Interested)*\nThey wrote to hannah@meetspill.org. Nobody has sent or skipped the answer yet.")
        assert "*What to do:* in this thread, ✅ on the draft (or reply `send`) sends it" in text
        assert "`railway ssh -- us-outbound replies approve item-000 --live`" in text
        assert text.endswith("If nobody acts, it goes to harry@spill.chat once it has waited 24 hours.")
        assert row_of(ctx)["reposted_at"] == now
        poll(ctx)
        assert len([p for p in sl.posts if p.get("reply_broadcast")]) == 1  # once


# -- escalation (SPEC 11) ------------------------------------------------------------------------------


def _old(**payload) -> dict:
    row = reply_row(**payload)
    row["created_at"] = NOW - timedelta(hours=25)
    return row


def test_after_24_hours_a_reply_is_forwarded_to_harry(settings):
    ctx, t, crm, sl, inst = desk_world(settings, row=_old())
    out = poll(ctx)
    [fwd] = inst.forwards
    assert (fwd["eaccount"], fwd["reply_to_uuid"], fwd["to_address_email_list"]) == (HANNAH, "em-1", "harry@spill.chat")
    note = fwd["body"]["text"]
    assert reply_row()["payload"]["draft"] in note
    assert "https://spill.slack.com/archives/C_ALERT/p100.000100" in note and "https://app.hubspot.com/contacts/8481055" in note
    assert "replies approve item-000" in note and "Never reply to the prospect from harry@spill.chat" in note
    row = row_of(ctx)
    assert (row["status"], row["escalated_at"]) == ("escalated", NOW) and out["escalated"][0]["how"] == "forwarded"
    assert ctx.store.get("events", event_id=f"escalated:{ITEM}")["type"] == "escalated"
    assert (":outbox_tray: *Escalated to harry@spill.chat* after 24 hours with no answer, by email (the reply "
            "forwarded to them). You can still send or skip it here." in sl.texts())
    poll(ctx)
    assert len(inst.forwards) == 1  # once
    sl.say(HARRY_ID, "send")  # an escalated item can still be approved
    poll(ctx)
    assert len(inst.replies) == 1 and row_of(ctx)["status"] == "handled"


def test_without_the_forward_endpoint_a_hubspot_task_and_a_dm(settings):
    ctx, t, crm, sl, inst = desk_world(settings, row=_old())
    inst.forward_status = 404
    out = poll(ctx)
    assert out["escalated"][0]["how"] == "task+dm"
    due_now = [task for task in crm.objects["tasks"].values() if task["hs_timestamp"].startswith("2026-10-05T15:00")]
    assert len(due_now) == 1 and due_now[0]["hubspot_owner_id"] == "owner-harry"
    [dm] = [p for p in sl.posts if p["channel"] == HARRY_ID]
    text = dm["text"]
    # It stands alone for someone who has not seen the thread: who, what, the draft, the link and every way to act.
    assert text.startswith(":rotating_light: *A prospect's reply has waited over 24 hours for an answer*\n"
                           "*Who:* Jane Doe at Acme Creative, writing to hannah@meetspill.org on ")
    assert "*What the system thinks it is:* Interested: they want to talk or hear more." in text
    assert "*What they said:*\n> " in text
    assert "*Draft answer* (signed Hannah Spalding):\n> Hi Jane," in text
    assert "1. In the Slack thread (<https://spill.slack.com/archives/C_ALERT/p100.000100|open the alert in Slack>)" in text
    assert "2. In a terminal: `railway ssh -- us-outbound replies approve item-000 --live` sends the draft." in text
    assert text.endswith("3. Answer them yourself from hannah@meetspill.org, then reply `skip` in the thread to close "
                         "it. Never answer from harry@spill.chat.")
    assert any(x.startswith(":outbox_tray: *Escalated to harry@spill.chat* after 24 hours with no answer, by a HubSpot "
                            "task (HubSpot emails it) and a Slack message to the approvers.") for x in sl.texts())
    assert row_of(ctx)["status"] == "escalated"


def test_a_non_warm_reply_task_carries_nothing_about_the_prospect(settings):
    ctx, t, crm, sl, inst = desk_world(settings, row=_old(reply_class="objection"), slack=False)
    inst.forward_status = 405
    poll(ctx)
    [task] = crm.objects["tasks"].values()
    assert "Jane" not in task["hs_task_body"] and "acmecreative" not in task["hs_task_body"]
    assert crm.objects["companies"] == {} and crm.objects["contacts"] == {}
    assert crm.linked("tasks", next(iter(crm.objects["tasks"])), "companies") == []


def _auto_send(settings, on: bool):
    return dataclasses.replace(settings, general=dataclasses.replace(settings.general, auto_send=on))


def _hand_check(ctx) -> None:
    ctx.store.upsert("hitl_items", [{"item_id": "hc-2026-W41", "kind": "hand_check", "status": "open",
                                     "created_at": NOW - timedelta(hours=30), "payload": {"summary": "This week's hand-check."}}])


def test_other_items_escalate_by_task_and_keep_their_status(settings):
    ctx, t, crm, sl, inst = desk_world(_auto_send(settings, True))  # enrol waits for the hand-check then
    _hand_check(ctx)
    poll(ctx)
    hc = row_of(ctx, "hc-2026-W41")
    assert hc["status"] == "open" and hc["escalated_at"] == NOW  # the hand-check's own job still finds it
    [task] = [x for x in crm.objects["tasks"].values() if "hand check" in x["hs_task_subject"]]
    assert task["hs_task_subject"] == "US Outbound: a hand check item has waited 24 hours"
    [dm] = [p["text"] for p in sl.posts if p["channel"] == HARRY_ID]
    assert dm == (":rotating_light: *Waiting over 24 hours for someone: this week's hand-check (a sample of companies "
                  "to check before they are emailed)*\nThis week's hand-check.\nIt is in #us-outbound.\n"
                  "*What to do:* check the companies listed (`railway ssh -- us-outbound handcheck show` shows them), "
                  "then approve with `railway ssh -- us-outbound handcheck approve --live`.")
    assert task["hs_timestamp"].startswith("2026-10-05T15:00")  # due now
    assert inst.forwards == []


def test_the_hand_check_is_not_escalated_while_every_email_is_approved_in_slack(settings):
    """auto_send = no: nothing waits for the weekly hand-check, so no task and no DM every week."""
    ctx, t, crm, sl, inst = desk_world(_auto_send(settings, False))
    _hand_check(ctx)
    out = poll(ctx)
    hc = row_of(ctx, "hc-2026-W41")
    assert hc["status"] == "open" and not hc.get("escalated_at") and out["escalated"] == []
    assert not [x for x in crm.objects["tasks"].values() if "hand check" in x["hs_task_subject"]]
    assert not [p for p in sl.posts if p["channel"] == HARRY_ID]
    assert ctx.store.get("events", event_id="escalated:hc-2026-W41") is None


def test_the_bots_own_reactions_neither_decide_nor_count_as_ignored(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    sl.react("white_check_mark", BOT_ID)  # seeded by poll_replies on the alert
    sl.react("x", BOT_ID)
    out = poll(ctx)
    assert inst.replies == [] and row_of(ctx)["status"] == "open" and out["ignored_non_approvers"] == 0
    sl.react("white_check_mark", HARRY_ID)  # the bot's ❌ does not beat Harry's ✅
    poll(ctx)
    assert len(inst.replies) == 1 and row_of(ctx)["handled_by"] == HARRY_ID
    assert len([r for r in t.requests if r.url.endswith("auth.test")]) == 1  # asked once a run


def test_two_hubspot_companies_for_the_website_are_flagged_in_the_thread_once(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    a = crm.add("companies", {"name": "Acme", "domain": "acmecreative.com"})
    b = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    poll(ctx)
    poll(ctx)
    [note] = [x for x in sl.texts() if "Not added to HubSpot" in x]
    assert note.startswith(":warning: *Not added to HubSpot: it has more than one company for this website* (<https://")
    assert f"|company {a}>" in note and f"|company {b}>" in note
    assert note.endswith("*What to do:* merge them into one in HubSpot; the reply is added within 5 minutes after. "
                         "Answering the reply does not wait for this.")


def test_a_changed_draft_gets_the_bots_tick_and_cross(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    sl.say(HARRY_ID, "edit: Hi Jane,\n\nWould Tuesday work?\n\nBest wishes,\nHannah")
    poll(ctx)
    repost = next(p for p in sl.posts if p["text"].startswith(":pencil2: *Draft changed by <@U_HARRY>.*"))
    assert [(r["name"], r["users"]) for r in sl.reactions[repost["ts"]]] == [("white_check_mark", [BOT_ID]),
                                                                            ("x", [BOT_ID])]
    assert poll(ctx)["ignored_non_approvers"] == 0 and inst.replies == []


def test_without_slack_approvals_wait_and_escalation_still_reaches_harry(settings, capsys):
    ctx, t, crm, sl, inst = desk_world(settings, row=_old(), slack=False)
    sl.say(HARRY_ID, "send")
    out = poll(ctx)
    assert out["slack"] is False and inst.replies == []
    assert [r for r in t.requests if "slack.com" in r.url] == []
    assert len(inst.forwards) == 1 and "Slack: the alert in #us-outbound" in inst.forwards[0]["body"]["text"]
    assert '"event": "slack_not_configured"' in capsys.readouterr().out
    assert crm.objects["companies"]  # the HubSpot writes do not need Slack


# -- enrolment pause (SPEC 11) -------------------------------------------------------------------------


def test_enrol_pauses_while_a_positive_reply_waits(settings):
    from us_outbound.enrol.enrol import reply_pause

    ctx, *_ = desk_world(settings, row=_old())
    assert "1 positive reply has waited over 24 hours" in reply_pause(ctx)
    ctx.store.update("hitl_items", {"item_id": ITEM}, {"status": "escalated"})
    assert reply_pause(ctx)
    ctx.store.update("hitl_items", {"item_id": ITEM}, {"status": "handled"})
    assert reply_pause(ctx) is None
    ctx.store.upsert("hitl_items", [_old(reply_class="objection") | {"item_id": "item-2"}])
    assert reply_pause(ctx) is None  # only positive (and referral) replies pause enrolment


# -- the command line ----------------------------------------------------------------------------------


class Harness:
    def __init__(self, settings, *, live_sending=True, row=None):
        g = dataclasses.replace(settings.general, live_sending=live_sending)
        self.settings = dataclasses.replace(settings, general=g)
        self.ctx, self.t, self.crm, self.sl, self.inst = desk_world(self.settings, row=row, slack=False)
        self.calls = []

    def __call__(self, job, live_flag, operator=False):
        ctx = make_context(self.settings, job=job, transport=self.t, store=self.ctx.store, now=NOW)
        ctx.guard.configure(live=bootstrap.resolve_live(live_flag, self.settings, operator=operator))
        ctx.live_flag = live_flag
        ctx.clients.secrets = self.ctx.clients.secrets  # no Slack token
        self.calls.append((job, ctx.live))
        return ctx

    def run(self, *argv):
        return cli.main(list(argv), context_factory=self)


def test_cli_list_shows_what_harry_needs(settings, capsys):
    h = Harness(settings)
    assert h.run("replies", "list") == 0
    out = capsys.readouterr().out
    assert "1 reply item waiting" in out and "item-000  positive  Acme Creative · Jane Doe, Head of People (People leader)" in out
    assert "Sounds interesting" in out and "My colleague Harry Dryden runs our US demos" in out
    assert "us-outbound replies approve item-000 --live" in out and "jane.doe@" not in out


def test_cli_approve_sends_through_the_same_path(settings, capsys):
    h = Harness(settings)
    assert h.run("replies", "approve", "item-0001", "--live") == 0
    assert h.calls[-1] == ("replies_approve", True)
    [sent] = h.inst.replies
    assert sent["eaccount"] == HANNAH and sent["body"]["text"] == reply_row()["payload"]["draft"]
    row = row_of(h.ctx)
    assert (row["status"], row["handled_by"]) == ("handled", "cli")
    assert h.ctx.store.get("events", event_id="sent-1")["approved_by"] == "cli"
    assert h.crm.objects["companies"] and h.crm.objects["tasks"]  # HubSpot written on the way
    assert h.run("replies", "approve", "item-0001", "--live") == 2  # handled: nothing to approve
    assert "nothing to act on" in capsys.readouterr().err and len(h.inst.replies) == 1


def test_cli_approve_with_edit_sends_that_text(settings):
    h = Harness(settings)
    text = "Hi Jane,\n\nHarry can do Tuesday at 11am ET: https://meetings.hubspot.com/harry336/us-demo-link\n\nBest wishes,\nHannah"
    assert h.run("replies", "approve", ITEM, "--edit", text, "--live") == 0
    assert h.inst.replies[0]["body"]["text"] == text
    assert row_of(h.ctx)["payload"]["draft"] == text and row_of(h.ctx)["payload"]["draft_original"]
    assert h.ctx.store.get("events", event_id="sent-1")["approval"] == "edited"


def test_cli_approve_needs_live_sending_too(settings, capsys):
    h = Harness(settings, live_sending=False)
    assert h.run("replies", "approve", ITEM, "--live") == 0
    assert h.inst.replies == [] and row_of(h.ctx)["status"] == "open"
    out = capsys.readouterr().out
    assert "Running dry" in out and "Dry-run: the reply was not sent" in out


def test_cli_skip_is_an_operator_command(settings):
    h = Harness(settings, live_sending=False)
    assert h.run("replies", "skip", "item-0001") == 0  # dry-run without --live
    assert row_of(h.ctx)["status"] == "open"
    assert h.run("replies", "skip", "item-0001", "--live") == 0
    assert row_of(h.ctx)["status"] == "handled" and row_of(h.ctx)["handled_by"] == "cli"
    assert h.ctx.store.get("events", event_id="em-1")["approval"] == "skipped" and h.inst.replies == []


def test_cli_refuses_an_unknown_or_ambiguous_id(settings, capsys):
    h = Harness(settings)
    h.ctx.store.upsert("hitl_items", [reply_row() | {"item_id": "item-0002-bbbb"}])
    assert h.run("replies", "approve", "item-", "--live") == 2
    assert "matches 2 items" in capsys.readouterr().err
    assert h.run("replies", "skip", "nope-123", "--live") == 2
    assert h.run("replies", "approve") == 2
    assert h.inst.replies == []


# -- reading Slack text ----------------------------------------------------------------------------------


@pytest.mark.parametrize("text, kind, body", [
    ("send", "send", ""), ("Send.", "send", ""), ("<@U_HARRY> send", "send", ""), ("SKIP!", "skip", ""),
    ("send: Hi Jane", "send_text", "Hi Jane"), ("Edit:  New text\nline two", "edit", "New text\nline two"),
    ("sending it now", None, None), ("send:", None, None), ("looks good", None, None),
    # The card's words too (9 Oct 2026: "approve" in a reply thread was dropped without a word).
    ("Approve", "send", ""), ("no", "skip", ""), ("reject.", "skip", ""), ("Don't send", "skip", ""),
    ("approved by me", None, None), ("no thanks", None, None),
])
def test_parse_command(text, kind, body):
    cmd = desk.parse_command(text)
    assert (cmd.kind if cmd else None, cmd.text if cmd else None) == (kind, body)


def test_slack_text_unwraps_links():
    assert desk.slack_text("<https://meetings.hubspot.com/x|meetings.hubspot.com/x> and <https://a.com|our page>") == \
        "https://meetings.hubspot.com/x and our page (https://a.com)"
    assert desk.slack_text("<mailto:a@b.com|a@b.com> &lt;3 &amp; <#C1|us-outbound>") == "a@b.com <3 & #us-outbound"


def test_items_without_a_kind_we_know_are_left_alone(settings):
    ctx, t, crm, sl, inst = desk_world(settings)
    other = reply_row() | {"item_id": "x-1", "kind": "kill_rule"}
    ctx.store.upsert("hitl_items", [other])
    sl.say(HARRY_ID, "send")
    out = poll(ctx)
    assert out["items"] == 1 and len(inst.replies) == 1
