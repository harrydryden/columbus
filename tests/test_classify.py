"""Reply classification and draft rules (SPEC 11), the Claude effort setting, and what a reply costs."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tests.fakes_replies import NOW, classification
from tests.test_client_claude import FakeSDK, make
from us_outbound.clients.claude import BudgetExceeded, PRICES, estimate_call_usd
from us_outbound.crm.hubspot_writes import REPLY_CLASSES
from us_outbound.replies import classify, draft

RECEIVED = date(2026, 10, 27)


# -- the stop rule and the prospect's own words -------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "STOP", "stop.", "Unsubscribe", "Please unsubscribe me", "Remove me", "please remove me from your list",
    "Take me off your list.", "Stop emailing me", "Do not contact me again.", "Don't email us", "opt out",
    "Please opt me out", "No more emails please",
])
def test_the_stop_rule_catches_plain_requests(text):
    assert classify.asks_to_stop(text)


@pytest.mark.parametrize("text", [
    "Sounds good, send times.", "We already have an EAP.", "Can you remove the setup fee?", "Not now, try in Q1.",
    "I'm out of the office until Monday.", "Stop by our booth at the conference", "Who should I talk to?",
])
def test_the_stop_rule_leaves_everything_else_to_the_model(text):
    assert not classify.asks_to_stop(text)


def test_reply_text_cuts_the_thread_and_our_own_lines():
    gmail = ("Yes, interested.\n\nOn Tue, Oct 20, 2026 at 9:00 AM Hannah Spalding <hannah@meetspill.org>\nwrote:\n"
             "> Hi Jane,\n> To stop hearing from us, unsubscribe here")
    assert classify.strip_quoted(gmail) == "Yes, interested."
    outlook = "Happy to talk.\n\nFrom: Hannah Spalding\nSent: Tuesday, October 20, 2026 9:00 AM\nTo: Jane\nSubject: Hi"
    assert classify.strip_quoted(outlook) == "Happy to talk."
    unquoted_footer = "Not for us.\nTo stop hearing from us, unsubscribe here: https://x\nRead our Trustpilot reviews"
    assert classify.strip_quoted(unquoted_footer) == "Not for us."
    assert not classify.asks_to_stop(classify.strip_quoted(gmail))


def test_reply_text_reads_html_when_there_is_no_text_part():
    email = {"body": {"html": "<div>Sounds <b>good</b>&nbsp;&amp; thanks<br>Jane</div>"}}
    assert classify.reply_text(email) == "Sounds good\xa0& thanks\nJane"
    assert classify.reply_text({"content_preview": "Preview only"}) == "Preview only"


# -- the answer, checked -------------------------------------------------------------------------------------


def test_the_classes_are_spec_11s_and_hubspots():
    assert set(classify.CLASSES) == set(REPLY_CLASSES)
    assert classify.SCHEMA["properties"]["class"]["enum"] == list(classify.CLASSES)


def test_below_point_seven_is_other():
    v = classify.from_answer(classification("positive", confidence=0.69), RECEIVED)
    assert (v.reply_class, v.model_class) == ("other", "positive")
    assert classify.from_answer(classification("positive", confidence=0.7), RECEIVED).reply_class == "positive"


def test_asking_to_stop_is_unsubscribe_at_any_confidence_but_a_warm_reply_keeps_its_class():
    assert classify.from_answer(classification("unsubscribe", confidence=0.4), RECEIVED).reply_class == "unsubscribe"
    assert classify.from_answer(classification("negative", asks_to_stop=True), RECEIVED).reply_class == "unsubscribe"
    assert classify.from_answer(classification("out_of_office", asks_to_stop=True), RECEIVED).reply_class == "unsubscribe"
    assert classify.from_answer(classification("positive", asks_to_stop=True), RECEIVED).reply_class == "positive"


def test_dates_must_parse_and_not_be_in_the_past():
    v = classify.from_answer(classification("not_now", not_now_date="2027-01-11", ooo_return_date="2026-10-01"), RECEIVED)
    assert v.not_now_date == date(2027, 1, 11) and v.ooo_return_date is None
    assert classify.from_answer(classification("not_now", not_now_date="next spring"), RECEIVED).not_now_date is None
    assert classify.from_answer(classification("not_now", not_now_date="2031-01-01"), RECEIVED).not_now_date is None


def test_the_referral_is_kept_as_given_and_a_bad_email_dropped():
    v = classify.from_answer(classification("referral", referral_name=" Ann  Lee ", referral_email="Ann@Acme.com"), RECEIVED)
    assert v.referral == {"name": "Ann Lee", "title": "", "email": "ann@acme.com"}
    v = classify.from_answer(classification("referral", referral_name="Ann", referral_email="ann at acme"), RECEIVED)
    assert v.referral == {"name": "Ann", "title": "", "email": ""}
    assert classify.from_answer(classification("positive"), RECEIVED).referral is None


def test_an_answer_out_of_bounds_is_tamed():
    v = classify.from_answer({"class": "spam", "confidence": "high", "objection": "weird",
                              "language_terms": [" a ", "", "b", "c", "d", "e", "f"]}, RECEIVED)
    assert v.reply_class == "other" and v.confidence == 0.0 and v.objection == "other"
    assert v.language_terms == ["a", "b", "c", "d", "e"]


# -- the calls -------------------------------------------------------------------------------------------------


class Ctx:
    """Just enough context for classify.classify and draft.write."""

    def __init__(self, claude, settings=None):
        from types import SimpleNamespace

        self.clients = SimpleNamespace(claude_task=claude, claude=claude)
        self.now = NOW
        self.settings = settings


def test_classification_is_one_low_effort_call_with_a_hard_cap():
    claude, _, sdk, _ = make(sdk=FakeSDK(text='{"class": "positive", "confidence": 0.9}'), model="claude-sonnet-5-5")
    v = classify.classify(Ctx(claude), "Sounds good.", received=NOW, company="Acme Creative", title="Head of People")
    assert v.reply_class == "positive"
    [call] = sdk.calls
    assert call["model"] == "claude-sonnet-5-5" and call["max_tokens"] == classify.MAX_TOKENS == 1024
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["schema"] is classify.SCHEMA
    assert "Received: 2026-10-27 (Tuesday)" in call["messages"][0]["content"]


def test_the_cap_refuses_classification_before_any_request():
    claude, store, sdk, _ = make(model="claude-sonnet-5-5", cap=10.0)
    store.insert("credit_ledger", [{"entry_id": "e", "system": "claude", "usd": 9.98, "occurred_at": NOW}])
    with pytest.raises(BudgetExceeded):
        classify.classify(Ctx(claude), "Sounds good.", received=NOW)
    assert sdk.calls == []
    # The stop rule needs no model, so it is never refused.
    assert classify.classify(Ctx(claude), "unsubscribe", received=NOW).reply_class == "unsubscribe"


def test_effort_goes_in_output_config_and_must_be_a_known_level():
    claude, _, sdk, _ = make(model="claude-opus-5-5")
    schema = {"type": "object", "properties": {"class": {"type": "string"}}, "required": ["class"],
              "additionalProperties": False}
    claude.json("s", "p", schema, effort="medium", now=NOW)
    assert sdk.calls[-1]["output_config"] == {"format": {"type": "json_schema", "schema": schema}, "effort": "medium"}
    claude.json("s", "p", schema, now=NOW)
    assert "effort" not in sdk.calls[-1]["output_config"]
    with pytest.raises(ValueError):
        claude.json("s", "p", schema, effort="extreme", now=NOW)


# -- the draft rules ----------------------------------------------------------------------------------------------


@pytest.fixture
def req():
    return draft.Request("positive", "Sounds good.", "Jane", "Hannah Spalding", False, company="Acme Creative")


def body(middle, sender="Hannah", greeting="Hi Jane,"):
    return f"{greeting}\n\n{middle}\n\nBest wishes,\n{sender}"


DEMO = ("My colleague Harry Dryden runs our US demos; you can grab a time with him here: "
        "https://meetings.hubspot.com/harry336/us-demo-link")


def test_a_good_positive_draft_passes(default_settings, req):
    text = body("Great to hear, thanks for coming back to me so quickly.\n\n" + DEMO)
    assert draft.check(text, req, default_settings) == []


def test_drafts_may_mention_the_free_trial(default_settings, req):
    text = body("Thanks. We also offer a free trial, so your team can see how it works first.\n\n" + DEMO)
    assert draft.check(text, req, default_settings) == []


@pytest.mark.parametrize("middle, problem", [
    ("Our therapists are great.\n\n" + DEMO, '"therapists"'),
    ("Thanks so much!\n\n" + DEMO, "exclamation"),
    ("Our licensed counselors help with burnout and much more besides.\n\n" + DEMO, '"licensed"'),
    ("Your EAP doesn't work, honestly, and this one will help your people.\n\n" + DEMO, "disparages"),
    ("It costs $5 per head, which is a good deal for teams.\n\n" + DEMO, "dollar figure"),
    ("Click here to see our organisation's approach to it.\n\n" + DEMO, "British spelling"),
    ("Book with me: https://calendly.com/hannah and thanks for replying so quickly.", "calendly.com"),
    ("Thanks for replying, it is good to hear from you about this.", "booking link"),
    ("You can grab a time with me here: https://meetings.hubspot.com/harry336/us-demo-link today.", "demos are always"),
])
def test_a_draft_that_breaks_a_rule_is_caught(default_settings, req, middle, problem):
    problems = draft.check(body(middle), req, default_settings)
    assert any(problem in p for p in problems), problems


def test_the_greeting_and_sign_off_are_checked(default_settings, req):
    assert any("open with" in p for p in draft.check(body(DEMO, greeting="Dear Jane"), req, default_settings))
    assert any("end with" in p for p in draft.check(body("Thanks for the reply.\n\n" + DEMO, sender="Harry"), req,
                                                     default_settings))


def test_only_a_positive_draft_links_the_booking_link(default_settings):
    other = draft.Request("objection", "We have an EAP.", "Jane", "Hannah Spalding", False)
    page = body("Spill works alongside an EAP, with same-day sessions. Here is a short demo: "
                "https://www.spill.chat/us/book-demo")
    assert draft.check(page, other, default_settings) == []
    assert any("only a positive reply" in p for p in draft.check(body("Thanks.\n\n" + DEMO), other, default_settings))


def test_the_prospects_own_names_are_not_spelling_errors(default_settings):
    r = draft.Request("referral", "Talk to Colour Labour.", "Jane", "Hannah Spalding", False, company="The Colour Agency")
    text = body("Thanks for pointing me to the right person at The Colour Agency, I appreciate it.")
    assert draft.check(text, r, default_settings, exempt=["The Colour Agency"]) == []


# -- what a reply costs -------------------------------------------------------------------------------------------------


def test_what_a_reply_costs_at_most(default_settings):
    """The figures behind the cap: a 600-character reply, at the full max_tokens of every call."""
    reply = "x" * 600
    c = estimate_call_usd("claude-sonnet-5-5", classify.SYSTEM,
                          classify.prompt(reply, received=datetime(2026, 10, 27, tzinfo=UTC)), classify.SCHEMA,
                          classify.MAX_TOKENS)
    r = draft.Request("positive", reply, "Jane", "Hannah Spalding", False)
    d = draft.estimate_usd(r, default_settings)
    assert c < 0.02  # classification: Sonnet, at most 1,024 tokens out
    assert d < 0.20  # the draft: Opus, at most 3,000 tokens out, twice
    assert {"claude-sonnet-5-5", "claude-opus-5-5"} <= set(PRICES)
