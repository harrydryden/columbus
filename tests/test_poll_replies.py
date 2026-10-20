"""poll_replies (SPEC 11): routing for every class, the hitl contract, idempotency, the Claude cap,
the opt-out path, out-of-office re-timing, Slack, and dry-run."""

from __future__ import annotations

import re
from datetime import timedelta

import anthropic
import pytest

from tests.fakes_replies import BOB, HANNAH, HARRY, JANE, NOW, OPUS, SONNET, classification, make_world
from us_outbound.clients import instantly as instantly_client
from us_outbound.replies import poll

# The payload keys the reply desk codes to (replies/poll.py docstring).
CONTRACT = {
    "contact_id", "account_id", "mailbox", "owner", "instantly_email_id", "reply_to_uuid", "reply_subject",
    "instantly_thread_id", "message_id", "instantly_campaign", "instantly_lead_id", "reply_class", "model_class",
    "confidence", "classified_by", "classification_error", "reply_excerpt", "summary", "demo_requested", "objection",
    "competitor_named", "language_terms", "asks_to_stop", "draft", "draft_problems", "draft_rejected", "referral",
    "not_now_date", "follow_up_date", "follow_up_source", "received_at", "company", "place", "industry", "tier", "why",
    "contact_name", "first_name", "title", "role", "from_is_contact", "sequence_may_continue", "sequence_paused",
    "slack_channel", "slack_ts",
}


@pytest.fixture
def world(default_settings):
    return make_world(default_settings)


def run(world):
    return poll.run(world.ctx)


def only_item(world):
    [item] = world.items()
    return item


def ledger(world, usd):
    world.ctx.store.insert("credit_ledger", [{"entry_id": "spent", "system": "claude", "job": "x", "usd": usd,
                                              "credits": 0.0, "occurred_at": NOW - timedelta(days=1)}])


# -- positive: the full path ------------------------------------------------------------------------------


def test_a_positive_reply_becomes_an_item_with_a_draft_and_an_alert(world):
    world.reply("E1")
    out = run(world)
    item = only_item(world)
    p = item["payload"]
    # The row and the contract.
    assert (item["item_id"], item["kind"], item["status"]) == ("reply:E1", "reply", "open")
    assert (item["account_id"], item["contact_id"], item["event_id"]) == ("acc-acme", "k-jane", "E1")
    assert CONTRACT <= set(p)
    assert p["mailbox"] == HANNAH and p["owner"] == "Hannah Spalding"
    assert p["instantly_email_id"] == p["reply_to_uuid"] == "E1"
    assert p["reply_subject"] == "Re: Busy season support for Acme Creative" and p["instantly_thread_id"] == "t-E1"
    assert p["instantly_lead_id"] == "L-jane" and p["instantly_campaign"] == "US Outbound – Hannah Spalding"
    assert p["reply_class"] == "positive" and p["classified_by"] == "model" and p["demo_requested"] is True
    assert p["reply_excerpt"].startswith("Sounds good") and len(p["reply_excerpt"]) <= 500
    assert p["received_at"].startswith("2026-10-27T14:50")
    # The draft: signed by the sender's first name; Hannah hands the demo to Harry with his booking link.
    assert p["draft"].startswith("Hi Jane,") and p["draft"].endswith("Best wishes,\nHannah")
    assert "My colleague Harry Dryden runs our US demos" in p["draft"]
    assert "https://meetings.hubspot.com/harry336/us-demo-link" in p["draft"] and p["draft_problems"] == []
    # Slack: the alert channel, mentioning the approver, with the class, account, role, excerpt and draft.
    [post] = world.slack_posts
    assert post["channel"] == "C_ALERT"
    text = "\n".join(b["text"]["text"] for b in post["blocks"])
    # Harry, 12 Oct 2026: a newcomer sees who replied, what they said, what it is, the draft and what each
    # reaction or reply does.
    assert text.startswith(":raised_hand: *Jane Doe at Acme Creative replied: Interested* <@U_HARRY>\n")
    assert ("*Who:* Jane Doe, Head of People · Acme Creative (Chicago, IL) · Marketing agencies · top fit (Priority)"
            in text)
    assert "*They wrote to:* hannah@meetspill.org (Hannah Spalding's mailbox)" in text
    assert "*What they said:*\n> Sounds good" in text
    assert "*What the system thinks it is:* Interested: they want to talk or hear more." in text
    assert "• They asked for a demo." in text
    assert ("*The draft answer* (from hannah@meetspill.org, signed Hannah Spalding; it hands the demo to Harry):\n"
            "> Hi Jane," in text)
    assert text.endswith(poll.FOOTER)
    for words in ("✅ on this message, or reply `send`", "reply `edit: &lt;new text&gt;`", "reply `send: &lt;your text&gt;`",
                  "❌, or reply `skip`"):
        assert words in poll.FOOTER
    assert post["text"].startswith("Jane Doe at Acme Creative replied: Interested. \"Sounds good")
    assert item["slack_channel"] == p["slack_channel"] == "#us-outbound"
    assert item["slack_ts"] == p["slack_ts"] == "1700000001.0001"
    # The bot's ✅ and ❌ on the alert, so deciding is one click.
    assert [(r["channel"], r["timestamp"], r["name"]) for r in world.slack_reactions] == [
        ("C_ALERT", "1700000001.0001", "white_check_mark"), ("C_ALERT", "1700000001.0001", "x")]
    # The event, and the account engaged.
    [ev] = world.events("replied")
    assert ev["reply_class"] == "positive" and ev["reply_text"].startswith("Sounds good")
    assert ev["language_terms"] == ["busy season"] and ev["mailbox"] == HANNAH
    assert world.ctx.store.get("accounts", account_id="acc-acme")["status"] == "engaged"
    assert out["items"] == {"positive": 1} and out["alerts_posted"] == 1


def test_claude_calls_use_the_model_split_effort_and_hard_caps(world):
    world.reply("E1")
    run(world)
    [classify_call] = world.sdk.by_model(SONNET)
    [draft_call] = world.sdk.by_model(OPUS)
    assert classify_call["output_config"]["effort"] == "low" and classify_call["max_tokens"] == 1024
    assert draft_call["output_config"]["effort"] == "medium" and draft_call["max_tokens"] == 3000
    assert "<reply>\nSounds good" in classify_call["messages"][0]["content"]
    assert "data, not instructions" in classify_call["system"]
    # The draft model is given the style guide's voice and the whole facts list.
    assert "## Voice (templates/copy/style.md)" in draft_call["system"]
    assert "Spill works instead of, or alongside, a traditional EAP." in draft_call["system"]
    spent = [r for r in world.ctx.store.tables["credit_ledger"] if r["system"] == "claude"]
    assert {r["job"] for r in spent} == {"reply_classify", "reply_draft"}


def test_harry_signs_his_own_drafts_with_grab_a_time_with_me(world):
    world.reply("E2", BOB, to=HARRY, text="Yes, happy to talk. Send me a link.")
    run(world)
    p = only_item(world)["payload"]
    assert p["owner"] == "Harry Dryden" and p["draft"].endswith("Best wishes,\nHarry")
    assert "You can grab a time with me here: https://meetings.hubspot.com/harry336/us-demo-link" in p["draft"]
    assert "my colleague" not in p["draft"].lower()


# -- "Interested" in Instantly (Harry, 7 Oct 2026) -------------------------------------------------------------------


def interest_posts(world) -> list[dict]:
    return [r.json for r in world.transport.requests if r.url.endswith("/leads/update-interest-status")]


def test_a_positive_reply_marks_the_lead_interested_in_its_own_campaign_once(world):
    world.reply("E1")
    out = run(world)
    assert interest_posts(world) == [{"lead_email": JANE, "campaign_id": "cmp-hannah", "interest_value": 1}]
    [call] = [c for c in world.ctx.guard.writes("instantly") if c.action == "lead.interest"]
    assert (call.target, call.sent) == ("US Outbound – Hannah Spalding", True)  # through the guard, live
    assert only_item(world)["payload"]["instantly_interest"] == "interested" and out["marked_interested"] == 1
    assert not world.lead_patches  # nothing else about the lead changes: no pause, no status
    # A later positive reply from the same company marks nothing again.
    world.reply("E2", text="Also, Thursday works.", at=NOW - timedelta(minutes=5))
    run(world)
    assert len(interest_posts(world)) == 1
    assert {i["item_id"]: i["payload"]["instantly_interest"] for i in world.items()} == {
        "reply:E1": "interested", "reply:E2": "marked for an earlier reply"}


def test_a_colleague_s_positive_reply_marks_the_contact_s_lead(world):
    world.reply("E1", "sam.lee@acmecreative.com", text="I look after this for Jane. Happy to chat.")
    run(world)
    assert [p["lead_email"] for p in interest_posts(world)] == [JANE]


@pytest.mark.parametrize("cls, extra", [("referral", {"referral_name": "Ann Lee"}), ("objection", {}), ("not_now", {})])
def test_only_a_positive_reply_is_marked(world, cls, extra):
    world.sdk.answer = classification(cls, **extra)
    world.reply("E1", text="Some reply.")
    run(world)
    assert interest_posts(world) == [] and "instantly_interest" not in only_item(world)["payload"]


def test_a_meeting_already_booked_is_never_marked_back_to_interested(world):
    world.ctx.store.insert("events", [{"event_id": "hs-m1", "contact_id": "k-jane", "account_id": "acc-acme",
                                       "type": "meeting_booked", "occurred_at": NOW - timedelta(hours=1)}])
    world.reply("E1")
    run(world)
    assert interest_posts(world) == [] and only_item(world)["payload"]["instantly_interest"] == "a meeting is booked already"


def test_a_failed_interest_call_is_logged_and_the_reply_is_handled_all_the_same(world):
    world.transport.route("POST", "/leads/update-interest-status", {"error": "not found"}, status=404)
    world.reply("E1")
    out = run(world)
    item = only_item(world)
    assert item["status"] == "open" and item["payload"]["draft"] and world.slack_posts  # the alert still goes
    assert item["payload"]["instantly_interest"] == "not set: ApiError"
    assert out["interest_error"].startswith("ApiError: ") and out["marked_interested"] == 0
    assert out["classified"] == {"positive": 1} and world.events("replied")[0]["reply_class"] == "positive"


# -- every other class a person answers -------------------------------------------------------------------


@pytest.mark.parametrize("cls, extra, guidance", [
    ("referral", {"referral_name": "Ann Lee", "referral_title": "HR Director", "referral_email": "Ann.Lee@acmecreative.com"},
     "confirm Ann Lee is the right person, and ask whether they would loop them in"),
    ("wrong_person", {}, "ask who looks after benefits or employee well-being"),
    ("objection", {"objection": "have_eap"}, "Spill works alongside an EAP or instead of one, and covers the gaps"),
    ("objection", {"objection": "price"}, "Plans start from $195 a month for the whole team"),
    ("not_now", {}, "ask when would be a better time to come back"),
    ("negative", {}, "say you won't follow up"),
    ("other", {}, "ask one short question"),
])
def test_each_class_gets_an_item_with_its_own_draft(world, cls, extra, guidance):
    world.sdk.answer = classification(cls, **extra)
    world.reply("E1", text="Some reply.")
    run(world)
    item = only_item(world)
    p = item["payload"]
    assert p["reply_class"] == cls and item["status"] == "open" and p["draft"]
    [draft_call] = world.sdk.by_model(OPUS)
    prompt = draft_call["messages"][0]["content"]
    assert guidance in prompt
    # Only a positive reply gets Harry's booking link; the others may link the demo page.
    assert "The only link you may use: https://www.spill.chat/us/book-demo" in prompt
    assert "meetings.hubspot.com" not in p["draft"]
    # Only warm replies mention the approver (SPEC 11); the rest are still alerted until the daily post exists.
    [post] = world.slack_posts
    first = post["blocks"][0]["text"]["text"]
    assert ("<@U_HARRY>" in first) is (cls == "referral")
    assert first.startswith(f":raised_hand: *Jane Doe at Acme Creative replied: {poll.LABELS[cls]}*")


def test_a_referral_carries_the_person_named(world):
    world.sdk.answer = classification("referral", referral_name="Ann Lee", referral_title="HR Director",
                                      referral_email="Ann.Lee@acmecreative.com")
    world.reply("E1", text="Not me, try Ann Lee, our HR Director: ann.lee@acmecreative.com")
    run(world)
    p = only_item(world)["payload"]
    assert p["referral"] == {"name": "Ann Lee", "title": "HR Director", "email": "ann.lee@acmecreative.com"}
    text = "\n".join(b["text"]["text"] for b in world.slack_posts[0]["blocks"])
    assert "• They pointed us to: Ann Lee, HR Director, ann.lee@acmecreative.com" in text


def test_not_now_records_the_date_given_or_a_default(world):
    world.sdk.answer = classification("not_now", not_now_date="2027-01-11")
    world.reply("E1", text="Try me again after the holidays, around the 11th of January.")
    run(world)
    p = only_item(world)["payload"]
    assert (p["not_now_date"], p["follow_up_date"], p["follow_up_source"]) == ("2027-01-11", "2027-01-11", "reply")
    assert "confirm you will check back around January 11, 2027" in world.sdk.by_model(OPUS)[0]["messages"][0]["content"]

    world.sdk.answer = classification("not_now")
    world.reply("E2", text="Not now, maybe later.")
    run(world)
    p2 = world.ctx.store.get("hitl_items", item_id="reply:E2")["payload"]
    assert p2["not_now_date"] is None and p2["follow_up_source"] == "default"
    assert p2["follow_up_date"] == (NOW.date() + poll.NOT_NOW_DEFAULT).isoformat()


def test_a_low_confidence_answer_is_other(world):
    world.sdk.answer = classification("positive", confidence=0.55)
    world.reply("E1")
    run(world)
    p = only_item(world)["payload"]
    assert (p["reply_class"], p["model_class"]) == ("other", "positive")
    text = "\n".join(b["text"]["text"] for b in world.slack_posts[0]["blocks"])
    assert "replied: Unclear*" in text
    assert "• Claude was not sure: it read this as Interested at 55%, too unsure to count, so it is marked Unclear." in text


# -- opt-outs ------------------------------------------------------------------------------------------------


def test_an_unsubscribe_reply_is_opted_out_everywhere(world):
    world.hubspot_contacts[JANE] = {"hs_email_optout": "false"}
    world.sdk.answer = classification("unsubscribe", asks_to_stop=True)
    world.reply("E1", text="I'd rather this was the last I hear about it, thanks.")
    out = run(world)
    assert world.items() == []  # no item: nothing for a person to approve
    assert world.suppressed(JANE)[0]["reason"] == "unsubscribe"
    assert world.ctx.store.get("contacts", contact_id="k-jane")["suppressed"] is True
    assert world.blocked == [[JANE]]
    assert world.hubspot_unsubscribed == ["jane.doe%40acmecreative.com"]
    [marker] = world.events("unsubscribed")
    assert marker["event_id"] == "unsubscribed:E1" and marker["contact_id"] == "k-jane"
    assert world.events("replied")[0]["reply_class"] == "unsubscribe"
    assert out["opted_out"] == {"done": 1}
    [note] = world.slack_posts
    assert note["text"].startswith(":white_check_mark: *Jane Doe at Acme Creative asked us to stop emailing, so they "
                                   "are opted out*\nNo more emails: on our do-not-contact list, Instantly's blocklist")
    assert "Read as a stop request by Claude; they wrote to hannah@meetspill.org. Nothing to do." in note["text"]


def test_hubspot_opt_out_only_when_hubspot_has_the_contact(world):
    world.reply("E1", text="Unsubscribe")
    run(world)
    assert world.blocked == [[JANE]] and world.hubspot_unsubscribed == []
    assert [e["event_id"] for e in world.events("unsubscribed")] == ["unsubscribed:E1"]


def opted_out_at_each_call(world) -> list[tuple[bool, bool]]:
    """(suppressed, blocklisted) as each model request arrives: what the stop rule had done before the model."""
    seen: list[tuple[bool, bool]] = []
    world.sdk.before = lambda: seen.append((bool(world.suppressed(JANE)), world.blocked == [[JANE]]))
    return seen


@pytest.mark.parametrize("text", ["STOP", "Please remove me from your list.", "Unsubscribe", "take me off this list",
                                  "Do not contact me again", "stop emailing me"])
def test_the_stop_rule_opts_out_before_the_model_is_asked(world, text):
    """W1-2 (code-replies-crm H2; RS §2 W1-2): a stop phrasing is suppressed and blocklisted at once, and only then is
    the model asked (it may be a warm reply that also says "remove me")."""
    world.sdk.answer = classification("unsubscribe", asks_to_stop=True)
    seen = opted_out_at_each_call(world)
    world.reply("E1", text=text)
    out = run(world)
    assert seen == [(True, True)]  # one classification call, made after the suppression and the blocklist
    assert world.events("replied")[0]["reply_class"] == "unsubscribe" and world.items() == []
    assert world.blocked == [[JANE]] and out["opted_out"] == {"done": 1}
    [note] = world.slack_posts
    assert "Read as a stop request by the stop-word check" in note["text"]


WARM_AND_STOP = "Sounds interesting, can we talk Tuesday? Please remove me from the newsletter though."


def test_a_warm_reply_that_also_asks_to_stop_is_suppressed_first_and_still_reaches_a_person(world):
    world.sdk.answer = classification("positive", confidence=0.9)
    seen = opted_out_at_each_call(world)
    world.reply("E1", text=WARM_AND_STOP)
    out = run(world)
    assert seen[0] == (True, True)  # the suppression row and the blocklist came before the model call
    assert world.suppressed(JANE)[0]["reason"] == "unsubscribe" and world.blocked == [[JANE]]
    [marker] = world.events("unsubscribed")
    assert marker["event_id"] == "unsubscribed:E1"
    item = only_item(world)
    p = item["payload"]
    assert (item["status"], p["reply_class"], p["classified_by"]) == ("open", "positive", "model+rule")
    assert p["also_asked_to_stop"] is True and p["opt_out"] == "done" and p["stop_note"] == poll.ALSO_STOP
    assert world.events("replied")[0]["reply_class"] == "positive"
    assert out["items"] == {"positive": 1} and out["also_asked_to_stop"] == 1 and out["opted_out"] == {"done": 1}
    # The alert says so, first among what is worth knowing.
    [alert] = [x for x in world.slack_posts if x.get("blocks")]
    text = "\n".join(b["text"]["text"] for b in alert["blocks"])
    assert f"*Worth knowing:*\n• *{poll.ALSO_STOP}*" in text
    assert "Claude was not sure" not in text  # 0.9 is at or over MIN_CONFIDENCE (0.7)


def test_a_kept_class_on_a_weak_read_says_the_model_was_unsure(world):
    """W1-2 review: a class kept because they asked to stop is kept at any confidence (0.3 here, under 0.7), and then
    counts as an engaged company; the alert says the read was weak."""
    world.sdk.answer = classification("positive", confidence=0.3)
    world.reply("E1", text=WARM_AND_STOP)
    run(world)
    p = only_item(world)["payload"]
    assert (p["reply_class"], p["confidence"], p["also_asked_to_stop"]) == ("positive", 0.3, True)
    [alert] = [x for x in world.slack_posts if x.get("blocks")]
    text = "\n".join(b["text"]["text"] for b in alert["blocks"])
    assert "Claude was not sure: it read this as " in text and "at only 30%, kept only because they also" in text


def test_the_same_reply_with_the_model_timing_out_is_an_unsubscribe_and_no_item(world):
    world.sdk.raises = TimeoutError("ReadTimeout")  # anything at all: an OSError from the transport here
    world.reply("E1", text=WARM_AND_STOP)
    out = run(world)
    assert world.items() == [] and world.events("replied")[0]["reply_class"] == "unsubscribe"
    assert world.suppressed(JANE) and world.blocked == [[JANE]] and len(world.sdk.calls) == 1
    assert out["retry_later"] == 0 and out["claude_error"].startswith("TimeoutError")


def test_a_wrong_person_who_asks_to_be_removed_keeps_the_class_and_is_suppressed(world):
    world.sdk.answer = classification("wrong_person", confidence=0.8)
    world.reply("E1", text="Not the right person, please remove me and contact our HR lead instead")
    run(world)
    p = only_item(world)["payload"]
    assert (p["reply_class"], p["also_asked_to_stop"]) == ("wrong_person", True)
    assert world.suppressed(JANE) and world.blocked == [[JANE]]


def test_plain_unsubscribe_with_the_model_saying_negative_is_an_unsubscribe(world):
    world.sdk.answer = classification("negative", confidence=0.9)
    world.reply("E1", text="unsubscribe")
    run(world)
    assert world.items() == [] and world.events("replied")[0]["reply_class"] == "unsubscribe"
    assert world.blocked == [[JANE]]


@pytest.mark.parametrize("cls", ["negative", "objection", "not_now", "other", "wrong_person"])
def test_C1_the_models_stop_flag_at_low_confidence_always_suppresses(world, cls):
    """Probe C1 at the job (the classify test has the class rule): the regex sees nothing, the model says
    asks_to_stop at 0.3. Every one of the five is suppressed and blocklisted; only wrong_person keeps an item."""
    world.sdk.answer = classification(cls, confidence=0.3, asks_to_stop=True)
    world.reply("E1", text="I would rather you did not keep sending these.")
    run(world)
    assert world.suppressed(JANE) and world.blocked == [[JANE]]
    assert [i["payload"]["reply_class"] for i in world.items()] == (["wrong_person"] if cls == "wrong_person" else [])


def test_a_negative_reply_asking_to_stop_is_an_unsubscribe(world):
    world.sdk.answer = classification("negative", asks_to_stop=True)
    world.reply("E1", text="Not interested, and I'd rather not hear from you again.")
    run(world)
    assert world.items() == [] and world.events("replied")[0]["reply_class"] == "unsubscribe"
    assert world.blocked == [[JANE]]


def test_a_colleague_asking_to_stop_suppresses_them_and_the_contact(world):
    world.reply("E1", "ops@acmecreative.com", text="Please remove us from your list.")
    run(world)
    assert world.blocked == [["ops@acmecreative.com", JANE]]
    assert world.suppressed("ops@acmecreative.com") and world.suppressed(JANE)


def test_an_opt_out_that_fails_is_retried_on_the_next_live_run(world):
    world.fail_blocklist(500)
    world.sdk.answer = classification("unsubscribe", asks_to_stop=True)
    world.reply("E1", text="Unsubscribe me please")
    out = run(world)
    assert out["opted_out"] == {"pending": 1} and world.events("unsubscribed") == []
    assert world.suppressed(JANE)  # suppressed at once all the same
    world.transport.routes.pop()  # the blocklist works again
    world.at(NOW + timedelta(minutes=15))
    out = run(world)
    assert out["opted_out_on_retry"] == {"done": 1} and len(world.events("unsubscribed")) == 1
    assert any(r.url.endswith("/emails/E1") for r in world.transport.requests)  # read back by id
    assert len(world.sdk.calls) == 1  # classified once, after the opt-out; the retry reads the reply back only
    world.at(NOW + timedelta(minutes=30))
    before = len(world.instantly_writes())
    run(world)
    assert len(world.instantly_writes()) == before  # done: nothing more


# -- out of office ------------------------------------------------------------------------------------------


def test_out_of_office_records_the_return_date_and_engages_nobody(world):
    world.sdk.answer = classification("out_of_office", ooo_return_date="2026-11-02")
    world.reply("E1", text="I'm out of the office until 2 November with limited access to email.", is_auto_reply=1)
    out = run(world)
    assert world.items() == [] and world.slack_posts == [] and world.sdk.by_model(OPUS) == []
    [row] = world.items("out_of_office")
    assert row["status"] == "handled" and row["handled_by"] == "poll_replies"
    assert row["payload"]["ooo_return_date"] == "2026-11-02"
    assert "retime" not in row["payload"]  # the re-timing, never confirmed, went with W2-1
    assert world.ctx.store.get("accounts", account_id="acc-acme")["status"] == "enrolled"
    assert world.events("replied")[0]["reply_class"] == "out_of_office"
    assert world.lead_patches == [] and out["classified"] == {"out_of_office": 1}


def test_out_of_office_never_pauses_or_resumes_the_lead(world):
    """The re-timing (pause the lead, set it going two send days after the return date) waited for phase 0 to confirm
    Instantly's lead pause, which never came, and went with W2-1: an out-of-office reply changes no lead, and a later
    run sets none going."""
    world.sdk.answer = classification("out_of_office", ooo_return_date="2026-11-02")
    world.reply("E1", text="Out until 2 November.")
    out = run(world)
    world.at(NOW + timedelta(days=8))  # Wed 4 Nov, past the return date
    later = run(world)
    assert world.lead_patches == [] and "ooo_paused" not in out and "ooo_resumed" not in later


def test_out_of_office_does_not_hold_back_capacity(world):
    from us_outbound.enrol.capacity import stopped_contacts

    world.sdk.answer = classification("out_of_office", ooo_return_date="2026-11-02")
    world.reply("E1", text="Away until 2 November.")
    world.sdk.answer = classification("out_of_office")
    run(world)
    assert "k-jane" not in stopped_contacts(world.ctx.store)


# -- stopping the sequence ---------------------------------------------------------------------------------------


def test_a_human_reply_instantly_took_for_an_auto_reply_is_flagged(world):
    world.reply("E1", text="Out today, but yes, interested. Send times.", is_auto_reply=1)
    run(world)
    p = only_item(world)["payload"]
    assert p["sequence_may_continue"] is True and p["sequence_paused"] is False
    text = "\n".join(b["text"]["text"] for b in world.slack_posts[0]["blocks"])
    assert "Instantly may still send them the rest of the email sequence" in text
    assert "*pause this person in their Instantly campaign* so the follow-ups stop." in text


def test_a_flagged_lead_is_paused_once_phase0_confirms(world, monkeypatch):
    monkeypatch.setattr(instantly_client, "LEAD_PAUSE_CONFIRMED", True)
    world.reply("E1", text="Interested.", is_auto_reply=1)
    run(world)
    assert world.lead_patches == [("L-jane", {"status": 2})]
    assert only_item(world)["payload"]["sequence_paused"] is True


def test_an_ordinary_reply_is_not_flagged(world):
    world.reply("E1")
    run(world)
    assert only_item(world)["payload"]["sequence_may_continue"] is False


# -- who it is from ----------------------------------------------------------------------------------------------


def test_a_colleague_at_the_account_counts_as_the_account(world):
    world.reply("E1", "sam.lee@acmecreative.com", text="I look after this for Jane. Happy to chat.",
                from_address_json=[{"address": "sam.lee@acmecreative.com", "name": "Sam Lee"}])
    run(world)
    p = only_item(world)["payload"]
    assert p["contact_id"] == "k-jane" and p["from_is_contact"] is False and p["first_name"] == "Sam"
    assert p["draft"].startswith("Hi Sam,")
    text = world.slack_posts[0]["blocks"][0]["text"]["text"]
    assert text.startswith(":raised_hand: *Someone else at Acme Creative replied: Interested*")
    assert "*Who:* someone else at Acme Creative, not the person we emailed (Jane Doe, Head of People)" in text


@pytest.mark.parametrize("frm", ["random@elsewhere.com", "mailer-daemon@acmecreative.com", HARRY,
                                 "harry@spill.chat", "cy@cold.com"])
def test_mail_from_anyone_else_is_ignored(world, frm):
    world.reply("E1", frm)
    out = run(world)
    assert world.items() == [] and world.sdk.calls == [] and world.events() == []
    assert out["unmatched"] + out["already_handled"] <= 1


def test_a_list_item_without_a_body_is_read_by_id(world):
    e = world.reply("E1")
    e["body"] = None
    e["content_preview"] = "Sounds good"
    world.transport.route("GET", "/emails/E1", {"id": "E1", "eaccount": HANNAH, "from_address_email": JANE,
                                               "body": {"text": "Sounds good, full text here."}})
    run(world)
    assert only_item(world)["payload"]["reply_excerpt"] == "Sounds good, full text here."


def test_the_quoted_thread_and_our_footer_are_not_read(world):
    world.reply("E1", text="Sounds good.\n\nOn Tue, Oct 20, 2026 at 9:00 AM Hannah Spalding <hannah@meetspill.org> wrote:\n"
                           "> To stop hearing from us, unsubscribe here\n> Hi Jane, ...")
    run(world)
    assert only_item(world)["payload"]["reply_excerpt"] == "Sounds good."
    assert world.events("unsubscribed") == []  # our own unsubscribe line in the quote is not a stop


# -- idempotency --------------------------------------------------------------------------------------------------


def test_each_reply_is_handled_once(world):
    world.reply("E1")
    world.reply("E2", BOB, to=HARRY, text="Yes please.")
    run(world)
    calls, posts = len(world.sdk.calls), len(world.slack_posts)
    for minutes in (15, 30):
        world.at(NOW + timedelta(minutes=minutes))
        out = run(world)
        assert out["already_handled"] == 2
    assert len(world.items()) == 2 and len(world.events("replied")) == 2
    assert len(world.sdk.calls) == calls and len(world.slack_posts) == posts


def test_a_run_that_stopped_after_the_class_and_before_the_item_is_finished_by_the_next(world):
    """W2-4: the class is written first and the item after it, so a run that stopped between the two left the reply
    with a class and no item; the next run handles it again and writes the item (no repair write from the item, as
    before: an item is never on record without its class). One event, one item, the class kept."""
    world.reply("E1")
    run(world)
    world.ctx.store.delete("hitl_items", {"item_id": "reply:E1"})  # as if it stopped there
    world.at(NOW + timedelta(minutes=15))
    out = run(world)
    assert out["already_handled"] == 0 and out["classified"] == {"positive": 1}
    assert len(world.items()) == 1 and len(world.events("replied")) == 1
    assert world.ctx.store.get("events", event_id="E1")["reply_class"] == "positive"
    again = run(world)
    assert again["already_handled"] == 1 and len(world.items()) == 1


def test_a_reply_is_on_record_before_it_is_classified(world):
    """RS §2 W2-4: the first pass writes each new reply's replied row (no class), with the step it answers and its
    mailbox, before the model is asked; the account is engaged once the class is known. An email Instantly flags as an
    automatic reply waits for its class (it would free the contact's later send slots)."""
    world.sent("S1", at=NOW - timedelta(days=10))
    world.sent("S2", at=NOW - timedelta(days=3))
    world.reply("E1")
    world.reply("E2", BOB, text="I am out of the office.", is_auto_reply=1)
    from collections import Counter

    from us_outbound.replies import outcomes

    outcomes.run(world.ctx)  # the two sends on record, so the reply's step is known
    out = {"dropped": Counter(), "unmatched": 0, "auto_replies_left_to_classify": 0}
    received = [e for e in world.emails if e["_type"] == "received"]
    kept = poll.record(world.ctx, outcomes.Directory(world.ctx), received, out)
    assert sorted(e["id"] for e, _ in kept) == ["E1", "E2"]
    [ev] = world.events("replied")
    assert (ev["event_id"], ev.get("reply_class"), ev["step"], ev["mailbox"]) == ("E1", None, 2, HANNAH)
    assert out["recorded"] == 1 and out["auto_replies_left_to_classify"] == 1
    assert world.ctx.store.get("accounts", account_id="acc-acme")["status"] == "enrolled"  # not yet engaged
    run(world)
    assert world.ctx.store.get("events", event_id="E1")["reply_class"] == "positive"
    assert world.ctx.store.get("accounts", account_id="acc-acme")["status"] == "engaged"


# -- the Claude cap and model errors -------------------------------------------------------------------------------


def test_the_cap_refuses_the_call_and_the_reply_still_reaches_a_person(world):
    ledger(world, 9.95)
    world.reply("E1")
    out = run(world)
    assert world.sdk.calls == []  # refused before any request
    item = only_item(world)
    p = item["payload"]
    assert (p["reply_class"], p["classified_by"]) == ("other", "fallback") and "cap" in p["classification_error"]
    assert p["draft"] == "" and "cap" in p["draft_problems"][0]
    assert item["status"] == "open" and out["claude_cap_reached"] == 1
    text = "\n".join(b["text"]["text"] for b in world.slack_posts[0]["blocks"])
    assert text.startswith(":warning: *Jane Doe at Acme Creative replied: Unclear*")
    assert "• Not sorted automatically (" in text and "read the reply yourself." in text
    assert "*No draft* (Claude's monthly cap is used up): the answer has to be written by hand." in text
    # With no draft there is nothing for ✅ to send: only ❌ is offered and seeded.
    assert "✅" not in text and text.endswith(poll.NO_DRAFT_FOOTER)
    assert "reply `send: &lt;your text&gt;` in the thread" in text and "❌, or reply `skip`" in text
    assert [r["name"] for r in world.slack_reactions] == ["x"]


def test_a_reaction_the_bot_cannot_add_never_stops_the_alert(world, capsys):
    world.transport.route("POST", "reactions.add", body={"ok": False, "error": "missing_scope"})
    world.reply("E1")
    out = run(world)
    assert out["alerts_posted"] == 1 and only_item(world)["slack_ts"]
    assert '"event": "desk_react_failed"' in capsys.readouterr().out


def test_the_cap_never_blocks_an_opt_out(world):
    ledger(world, 10.0)
    world.reply("E1", text="Please unsubscribe me.")
    out = run(world)
    assert world.sdk.calls == [] and world.blocked == [[JANE]]  # the cap refuses before any request
    assert world.events("replied")[0]["reply_class"] == "unsubscribe" and out["claude_cap_reached"] == 1


def test_a_model_error_is_retried_then_handed_to_a_person(world):
    exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
    Exception.__init__(exc, "overloaded")
    exc.status_code = 529
    world.sdk.raises = exc
    world.reply("E1", at=NOW - timedelta(minutes=5))
    out = run(world)
    # Recorded before it was classified (RS §2 W2-4): the replied event exists, unclassified; no item yet; one
    # error for heartbeat_check (ops/health.py: a key ending "_error").
    assert out["retry_later"] == 1 and world.items() == []
    [ev] = world.events("replied")
    assert ev["event_id"] == "E1" and ev.get("reply_class") is None
    assert out["claude_error"] == "Claude API error 529"
    from us_outbound.ops import health

    assert [f.kind for f in health.findings("poll_replies", {"status": "ok", "detail": out})] == ["claude_error"]
    world.at(NOW + timedelta(minutes=40))
    run(world)
    p = only_item(world)["payload"]
    assert p["classified_by"] == "fallback" and "529" in p["classification_error"]


def test_a_draft_that_breaks_a_rule_is_tried_again_then_withheld(world):
    world.sdk.draft = lambda prompt: "Hi Jane,\n\nOur therapy sessions are great!\n\nBest wishes,\nHannah"
    world.reply("E1")
    run(world)
    p = only_item(world)["payload"]
    assert len(world.sdk.by_model(OPUS)) == 2  # one retry, with the problems
    assert "Your last draft broke these rules" in world.sdk.by_model(OPUS)[1]["messages"][0]["content"]
    assert p["draft"] == "" and "therapy" in p["draft_rejected"]
    assert any('"therapy"' in x for x in p["draft_problems"]) and any("exclamation" in x for x in p["draft_problems"])


# -- Slack -----------------------------------------------------------------------------------------------------------


def test_without_a_slack_token_the_item_stays_open_for_the_escalation_email(world):
    world.without_slack_token()
    world.reply("E1")
    out = run(world)
    item = only_item(world)
    assert item["status"] == "open" and not item.get("slack_ts") and out["waiting_for_slack"] == 1
    world.at(NOW + timedelta(minutes=15))
    world.ctx.clients.__dict__.pop("slack", None)
    from us_outbound.context import Secrets

    world.ctx.clients.secrets = Secrets(world.ctx.guard, fetch=lambda n: f"test-{n}")
    run(world)
    assert world.ctx.store.get("hitl_items", item_id="reply:E1")["slack_ts"]


def test_the_alert_escapes_what_the_prospect_wrote(world):
    world.reply("E1", text="<!channel> interested <https://evil.example|click>")
    run(world)
    text = "\n".join(b["text"]["text"] for b in world.slack_posts[0]["blocks"])
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text and "<https://evil" not in text


# -- dry-run ---------------------------------------------------------------------------------------------------------


def test_dry_run_calls_no_model_writes_no_item_and_sends_nothing(default_settings):
    world = make_world(default_settings, live=False)
    world.sdk.answer = lambda prompt: classification("unsubscribe" if "remove me" in prompt else "positive")
    world.hubspot_contacts[JANE] = {}
    world.reply("E1")
    world.reply("E2", BOB, to=HARRY, text="Please remove me from your list.")
    out = poll.run(world.ctx)
    assert world.sdk.calls == [] and world.items() == [] and world.slack_posts == []
    assert [e for e in world.events() if e.get("reply_class")] == []
    assert world.instantly_writes() == [] and world.hubspot_writes() == []
    assert out["would_classify"] == 1 and out["would_opt_out"] == 1 and 0 < out["would_spend_usd_at_most"] < 0.2
    assert world.suppressed(BOB)  # the stop rule's suppression protects even in dry-run
    # A later live run does all of it: nothing was used up by the dry run.
    world.at(NOW + timedelta(minutes=15), live=True)
    out = poll.run(world.ctx)
    assert len(world.items()) == 1 and len(world.slack_posts) == 2  # the alert and the opt-out note
    assert world.blocked == [[BOB]] and out["opted_out"] == {"done": 1}


def test_the_job_runs_from_the_cli_with_a_heartbeat(default_settings):
    from us_outbound.ops.health import run_job

    world = make_world(default_settings)
    world.reply("E1")
    summary = run_job(world.ctx, poll.run)
    [hb] = world.ctx.store.tables["heartbeats"]
    assert hb["status"] == "ok" and hb["job"] == "poll_replies"
    assert summary["items"] == {"positive": 1}
    assert "Sounds good" not in str(hb["detail"])  # the summary carries counts, never reply text


def test_a_backlog_stops_at_the_time_budget_and_the_next_run_takes_the_rest(world, monkeypatch):
    """Each draft can take a minute: past RUN_SECONDS no new reply is started, and none is lost."""
    import itertools

    from us_outbound.ops.health import run_job

    world.reply("E1", at=NOW - timedelta(days=5))
    world.reply("E2", at=NOW - timedelta(days=4))  # older than the two days every run re-reads
    world.reply("E3", at=NOW - timedelta(minutes=10))
    ticks = itertools.chain([0.0, 0.0], itertools.repeat(poll.RUN_SECONDS + 1.0))  # the start, E1, then past it
    monkeypatch.setattr(poll, "_clock", lambda: next(ticks))
    first = run_job(world.ctx, poll.run)
    assert [i["item_id"] for i in world.items()] == ["reply:E1"] and first["alerts_posted"] == 1
    assert first["left_for_next_run"] == 2 and first["resume_from"] == (NOW - timedelta(days=4)).isoformat()
    monkeypatch.setattr(poll, "_clock", lambda: 0.0)
    world.at(NOW + timedelta(minutes=15))
    second = run_job(world.ctx, poll.run)
    assert second["since"] == (NOW - timedelta(days=4, hours=1)).isoformat()
    assert sorted(i["item_id"] for i in world.items()) == ["reply:E1", "reply:E2", "reply:E3"]
    assert second["left_for_next_run"] == 0 and "resume_from" not in second and second["received"] == 2


def test_the_contract_is_written_in_the_module_docstring():
    doc = poll.__doc__
    assert "THE CONTRACT WITH THE REPLY DESK" in doc
    for key in CONTRACT - {"model_class", "confidence", "classification_error", "classified_by", "draft_problems",
                           "draft_rejected", "asks_to_stop", "language_terms", "competitor_named", "summary",
                           "demo_requested", "objection", "first_name", "sequence_paused", "reply_to_uuid",
                           "not_now_date", "follow_up_source", "place", "industry", "tier", "why", "title", "role",
                           "contact_name", "message_id", "instantly_campaign", "instantly_lead_id",
                           "instantly_thread_id", "reply_subject"}:
        assert re.search(rf"\b{key}\b", doc), key
    assert 'kind           "reply"' in doc and 'status         "open"' in doc


def test_excerpts_are_capped_at_500_characters(world):
    world.reply("E1", text="Interested. " + "x" * 2000)
    run(world)
    assert len(only_item(world)["payload"]["reply_excerpt"]) == 500
    text = "\n".join(b["text"]["text"] for b in world.slack_posts[0]["blocks"])
    assert len(re.search(r"\n> (Interested[^\n]*)\n", text).group(1)) == poll.SLACK_EXCERPT_CHARS


def test_an_unexpected_ue_type_never_drops_a_reply(world):
    """Only the codes that mark an email as ours exclude it (PHASE0-CONFIRM: Instantly's ue_type codes)."""
    world.reply("E1", ue_type=7)
    world.reply("E2", ue_type=3)  # one of ours, sent by hand
    run(world)
    assert [i["event_id"] for i in world.items()] == ["E1"]


# -- the alert's words (Harry, 12 Oct 2026: clear to someone new) ------------------------------------------------


def _alert_text(settings, **payload):
    base = {"reply_class": "not_now", "company": "Acme Creative", "place": "Chicago, IL", "industry": "Marketing agencies",
            "tier": "Standard", "contact_name": "Jane Doe", "title": "Head of People", "from_is_contact": True,
            "mailbox": HANNAH, "owner": "Hannah Spalding", "reply_excerpt": "Busy now.\nTry me in January?",
            "draft": "Hi Jane,\n\nWill do.\n\nBest wishes,\nHannah"}
    text, blocks = poll.alert({**base, **payload}, settings)
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks)
    return text, "\n".join(b["text"]["text"] for b in blocks)


def test_the_alert_says_what_each_note_means(default_settings):
    _, text = _alert_text(default_settings, why="3 open HR roles", reply_class="objection", objection="has_eap",
                          competitor_named="Lyra", follow_up_date="2027-01-11", follow_up_source="reply",
                          sequence_may_continue=True, sequence_paused=True)
    assert text.startswith(":raised_hand: *Jane Doe at Acme Creative replied: Objection*\n")  # no mention: not warm
    assert "· good fit (Standard)" in text
    assert "*What they said:*\n> Busy now.\n> Try me in January?" in text
    assert "*What the system thinks it is:* Objection: they gave a reason not to go ahead." in text
    assert "*Worth knowing:*\n• Why we emailed them: 3 open HR roles\n• Their objection: has eap" in text
    assert "• They named a competitor: Lyra" in text
    assert "• Date to follow up: Mon 11 Jan 2027 (the date they gave)." in text
    assert "(it took this for an automatic reply, or could not match it to the lead): the system has paused them there." in text
    assert "*The draft answer* (from hannah@meetspill.org, signed Hannah Spalding):\n> Hi Jane," in text


def test_the_alert_without_a_draft_says_why_and_offers_only_what_works(default_settings):
    note, text = _alert_text(default_settings, reply_class="wrong_person", draft="", follow_up_date="2027-01-11",
                             draft_problems=["the draft named a price"])
    assert text.startswith(":warning: *Jane Doe at Acme Creative replied: Wrong person*")
    assert "*What the system thinks it is:* Wrong person: they are not the one to talk to." in text
    assert "*No draft* (the draft named a price): the answer has to be written by hand." in text
    assert "(they gave none; the draft asks them when)" in text
    assert text.endswith(poll.NO_DRAFT_FOOTER) and "✅" not in text
    assert note == "Jane Doe at Acme Creative replied: Wrong person. \"Busy now.\nTry me in January?\""


def test_an_opt_out_still_waiting_on_instantly_or_hubspot_says_it_is_retried():
    info = {"company": "Acme Creative", "contact_name": "Jane Doe", "mailbox": HANNAH}
    text = poll.opt_out_note(info, from_is_contact=False, by_rule=True, result="pending")
    assert text == (":bar_chart: *Someone else at Acme Creative asked us to stop emailing, so they are opted out*\n"
                    "On our do-not-contact list now. Instantly's blocklist or HubSpot's opt-out did not take it yet; the "
                    "next check for replies (every 15 minutes) tries again. Read as a stop request by the stop-word "
                    "check; they wrote to hannah@meetspill.org. Nothing to do.")


# -- what the read drops, counted (W1-2, code-replies-crm M3) ---------------------------------------------------------


def test_each_email_dropped_from_the_read_is_counted_by_why(world):
    """Seven received emails: a campaign send, one from the mailbox itself, a bounce notice at the prospect's domain,
    one from a Spill address, one from another of our mailboxes, one with no sender and a prospect's reply. The first
    six are dropped, each counted by its reason (the M3 counts sync_outcomes kept too until W2-4); one is recorded and
    processed."""
    world.reply("D1", HANNAH, ue_type=1, text="A campaign step listed as received")
    world.reply("D2", HANNAH, text="From the mailbox itself")
    world.reply("D3", "mailer-daemon@acmecreative.com", text="Delivery failed")
    world.reply("D4", "hello@spill.chat", text="Internal")
    world.reply("D5", "harry@meetspill.org", text="One of our own mailboxes")
    world.reply("D6", "", text="No sender at all")
    world.reply("E1")
    out = run(world)
    assert out["dropped"] == {"campaign_send": 1, "from_self": 1, "system_sender": 1, "spill_domain": 1,
                              "registry_sender": 1, "no_sender": 1}
    assert out["classified"] == {"positive": 1} and out["received"] == 7 and out["recorded"] == 1
    assert [e["event_id"] for e in world.events("replied")] == ["E1"]


def test_a_warm_reply_that_asked_to_stop_retries_its_opt_out_and_keeps_its_item(world):
    world.fail_blocklist(500)
    world.sdk.answer = classification("positive", confidence=0.9)
    world.reply("E1", text=WARM_AND_STOP)
    out = run(world)
    assert out["opted_out"] == {"pending": 1} and only_item(world)["payload"]["opt_out"] == "pending"
    world.transport.routes.pop()  # the blocklist works again
    world.at(NOW + timedelta(minutes=15))
    out = run(world)
    assert out["opted_out_on_retry"] == {"done": 1} and world.blocked == [[JANE]]
    assert only_item(world)["status"] == "open"  # still for a person to answer by hand


def test_the_first_warm_reply_that_asked_to_stop_end_to_end(world):
    """W1-2's acceptance: the reply ends with HubSpot records (standard properties only, the strict fake refusing any
    other), an open item, a Slack alert, and the address suppressed, blocklisted and opted out in HubSpot."""
    from tests.test_hubspot_writes import FakeHubSpot
    from us_outbound.crm import hubspot_writes as hw

    crm = FakeHubSpot(world.transport)  # the strict portal (a 400 for any property it lacks)
    world.sdk.answer = classification("positive", confidence=0.9, demo_requested=True)
    world.reply("E1", text=WARM_AND_STOP)
    run(world)
    item = only_item(world)
    assert item["status"] == "open" and item["payload"]["also_asked_to_stop"] is True
    assert [p for p in world.slack_posts if p.get("blocks")]  # the alert
    assert world.suppressed(JANE) and world.blocked == [[JANE]]
    out = hw.record_reply(world.ctx, item)  # what the reply desk does next (replies/desk.py)
    assert out["status"] == "written" and crm.rejected == []
    assert len(crm.objects["companies"]) == len(crm.objects["contacts"]) == len(crm.objects["notes"]) == 1
    assert len(crm.objects["tasks"]) == 1 and len(crm.objects["deals"]) == 1
    assert crm.unsubscribed == ["jane.doe%40acmecreative.com"]
