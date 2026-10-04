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
    assert "*Positive reply · Acme Creative (Chicago, IL) · Marketing agencies · Priority* <@U_HARRY>" in text
    assert "From: Jane Doe, Head of People → hannah@meetspill.org" in text
    assert "Draft reply (signed Hannah Spalding; hands the demo to Harry):\n> Hi Jane," in text
    assert text.endswith('✅ sends this draft · ❌ skips it (you\'ll answer yourself) · reply "edit: &lt;new text&gt;" '
                         'to change it, then ✅ the new version · "send: &lt;text&gt;" sends your text now')
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
    assert first.startswith(f"*{poll.LABELS[cls]} reply · Acme Creative")


def test_a_referral_carries_the_person_named(world):
    world.sdk.answer = classification("referral", referral_name="Ann Lee", referral_title="HR Director",
                                      referral_email="Ann.Lee@acmecreative.com")
    world.reply("E1", text="Not me, try Ann Lee, our HR Director: ann.lee@acmecreative.com")
    run(world)
    p = only_item(world)["payload"]
    assert p["referral"] == {"name": "Ann Lee", "title": "HR Director", "email": "ann.lee@acmecreative.com"}
    text = "\n".join(b["text"]["text"] for b in world.slack_posts[0]["blocks"])
    assert "Referral: Ann Lee, HR Director, ann.lee@acmecreative.com" in text


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
    assert "Claude was not sure: it read this as positive at 55%" in text


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
    assert note["text"].startswith("Opted out · Acme Creative · Jane Doe asked to stop (Claude)")


def test_hubspot_opt_out_only_when_hubspot_has_the_contact(world):
    world.reply("E1", text="Unsubscribe")
    run(world)
    assert world.blocked == [[JANE]] and world.hubspot_unsubscribed == []
    assert [e["event_id"] for e in world.events("unsubscribed")] == ["unsubscribed:E1"]


@pytest.mark.parametrize("text", ["STOP", "Please remove me from your list.", "Unsubscribe", "take me off this list",
                                  "Do not contact me again", "stop emailing me"])
def test_the_stop_rule_opts_out_without_a_model_call(world, text):
    world.reply("E1", text=text)
    run(world)
    assert world.sdk.calls == []
    assert world.events("replied")[0]["reply_class"] == "unsubscribe"
    assert world.blocked == [[JANE]]


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
    world.reply("E1", text="Unsubscribe me please")
    out = run(world)
    assert out["opted_out"] == {"pending": 1} and world.events("unsubscribed") == []
    assert world.suppressed(JANE)  # suppressed at once all the same
    world.transport.routes.pop()  # the blocklist works again
    world.at(NOW + timedelta(minutes=15))
    out = run(world)
    assert out["opted_out_on_retry"] == {"done": 1} and len(world.events("unsubscribed")) == 1
    assert any(r.url.endswith("/emails/E1") for r in world.transport.requests)  # read back by id
    assert world.sdk.calls == []  # the stop rule, both times
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
    retime = row["payload"]["retime"]
    assert retime["status"] == "noted" and "LEAD_PAUSE_CONFIRMED" in retime["note"]
    assert retime["resume_on"] == "2026-11-04"  # two send days after Monday 2 November
    assert world.ctx.store.get("accounts", account_id="acc-acme")["status"] == "enrolled"
    assert world.events("replied")[0]["reply_class"] == "out_of_office"
    assert world.lead_patches == [] and out["classified"] == {"out_of_office": 1}


def test_out_of_office_pauses_and_resumes_the_lead_once_phase0_confirms(world, monkeypatch):
    monkeypatch.setattr(instantly_client, "LEAD_PAUSE_CONFIRMED", True)
    world.sdk.answer = classification("out_of_office", ooo_return_date="2026-11-02")
    world.reply("E1", text="Out until 2 November.")
    run(world)
    assert world.lead_patches == [("L-jane", {"status": 2})]
    assert world.items("out_of_office")[0]["payload"]["retime"]["status"] == "paused"
    world.at(NOW + timedelta(days=3))  # Fri 30 Oct: not yet
    run(world)
    assert len(world.lead_patches) == 1
    world.at(NOW + timedelta(days=8))  # Wed 4 Nov
    out = run(world)
    assert world.lead_patches[-1] == ("L-jane", {"status": 1}) and out["ooo_resumed"] == 1
    assert world.items("out_of_office")[0]["payload"]["retime"]["status"] == "resumed"


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
    assert "pause the lead in Instantly" in text


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
    assert "someone else at Acme Creative (the contact is Jane Doe, Head of People)" in text


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


def test_a_run_that_stopped_after_the_item_gives_the_event_its_class(world):
    world.reply("E1")
    run(world)
    world.ctx.store.update("events", {"event_id": "E1"}, {"reply_class": None})  # as if it stopped there
    calls = len(world.sdk.calls)
    world.at(NOW + timedelta(minutes=15))
    run(world)
    assert world.ctx.store.get("events", event_id="E1")["reply_class"] == "positive"
    assert len(world.sdk.calls) == calls and len(world.items()) == 1


def test_sync_outcomes_writing_the_reply_first_does_not_stop_its_classification(world):
    from us_outbound.replies import outcomes

    world.reply("E1")
    outcomes.run(world.ctx)
    assert world.events("replied")[0].get("reply_class") is None
    run(world)
    assert world.events("replied")[0]["reply_class"] == "positive" and len(world.items()) == 1
    outcomes.run(world.ctx)  # and a later sync never clears the class
    assert world.events("replied")[0]["reply_class"] == "positive"


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
    assert "No draft" in text and "read the reply yourself" in text
    # With no draft there is nothing for ✅ to send: only ❌ is offered and seeded.
    assert "✅" not in text and text.endswith('❌ skips it (you\'ll answer yourself) · "send: &lt;text&gt;" sends your text now')
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
    run(world)
    assert world.sdk.calls == [] and world.blocked == [[JANE]]


def test_a_model_error_is_retried_then_handed_to_a_person(world):
    exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
    Exception.__init__(exc, "overloaded")
    exc.status_code = 529
    world.sdk.raises = exc
    world.reply("E1", at=NOW - timedelta(minutes=5))
    out = run(world)
    assert out["retry_later"] == 1 and world.items() == [] and world.events("replied") == []
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
    from us_outbound.ops.heartbeat import run_job

    world = make_world(default_settings)
    world.reply("E1")
    summary = run_job(world.ctx, poll.run)
    [hb] = world.ctx.store.tables["heartbeats"]
    assert hb["status"] == "ok" and hb["job"] == "poll_replies"
    assert summary["items"] == {"positive": 1}
    assert "Sounds good" not in str(hb["detail"])  # the summary carries counts, never reply text


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
    assert len(re.search(r'"(Interested[^"]*)"', text).group(1)) == poll.SLACK_EXCERPT_CHARS


def test_an_unexpected_ue_type_never_drops_a_reply(world):
    """Only the codes that mark an email as ours exclude it (PHASE0-CONFIRM: Instantly's ue_type codes)."""
    world.reply("E1", ue_type=7)
    world.reply("E2", ue_type=3)  # one of ours, sent by hand
    run(world)
    assert [i["event_id"] for i in world.items()] == ["E1"]
