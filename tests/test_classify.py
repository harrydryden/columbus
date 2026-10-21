"""Reply classification and draft rules (SPEC 11), the Claude effort setting, and what a reply costs."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tests.fakes_replies import NOW, classification
from tests.test_client_claude import FakeSDK, make
from us_outbound.clients.claude import BudgetExceeded, ClaudeError, PRICES, estimate_call_usd
from us_outbound.clients.instantly import UNSUBSCRIBE_HTML, UNSUBSCRIBE_PLAIN
from us_outbound.replies import classify, desk, draft

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


def test_C3_our_current_footer_left_unquoted_is_never_read_as_a_stop_request():
    """Probe C3 (tests-mutation-safety-measure §4): the footer since 5 Oct 2026, "Not relevant? Unsubscribe here",
    left unquoted by a mail client, is cut back to the prospect's words, so the stop rule never reads it."""
    words = "Sounds good, send me some times."
    assert classify.strip_quoted(words + UNSUBSCRIBE_PLAIN) == words
    assert not classify.asks_to_stop(classify.strip_quoted(words + UNSUBSCRIBE_PLAIN))
    assert not classify.asks_to_stop(classify.reply_text({"body": {"html": f"<div>{words}</div>{UNSUBSCRIBE_HTML}"}}))


# The warm and referral replies code-replies-crm H2 probed: the stop rule matches 6 of these 7, so each must still
# reach the model (W1-2).
WARM_WITH_A_STOP_WORD = [
    "Sounds interesting, can we talk Tuesday? Please remove me from the newsletter though.",
    "Not the right person, please remove me and contact our HR lead Sam Lee instead.",
    "We would love a demo. No more emails needed, just send a calendar link.",
    "Can you take us off the waitlist and book a demo?",
    "Happy to chat. Please unsubscribe my old address and use this one.",
    "Yes please, but stop emailing my colleague, I am the one to talk to.",
]


@pytest.mark.parametrize("text", WARM_WITH_A_STOP_WORD)
def test_the_stop_rule_catches_these_warm_replies_so_the_model_must_still_read_them(text):
    assert classify.asks_to_stop(text)


def test_reply_text_reads_html_when_there_is_no_text_part():
    email = {"body": {"html": "<div>Sounds <b>good</b>&nbsp;&amp; thanks<br>Jane</div>"}}
    assert classify.reply_text(email) == "Sounds good\xa0& thanks\nJane"
    assert classify.reply_text({"content_preview": "Preview only"}) == "Preview only"


# -- the answer, checked -------------------------------------------------------------------------------------


def test_the_classes_are_spec_11s_and_the_desks_words():
    assert set(classify.CLASSES) == set(desk.CLASS_WORDS)
    assert classify.SCHEMA["properties"]["class"]["enum"] == list(classify.CLASSES)


def test_below_point_seven_is_other():
    v = classify.from_answer(classification("positive", confidence=0.69), RECEIVED)
    assert (v.reply_class, v.model_class) == ("other", "positive")
    assert classify.from_answer(classification("positive", confidence=0.7), RECEIVED).reply_class == "positive"


def test_asking_to_stop_is_unsubscribe_at_any_confidence_but_a_warm_reply_keeps_its_class():
    assert classify.from_answer(classification("unsubscribe", confidence=0.4), RECEIVED).reply_class == "unsubscribe"
    assert classify.from_answer(classification("negative", asks_to_stop=True), RECEIVED).reply_class == "unsubscribe"
    assert classify.from_answer(classification("out_of_office", asks_to_stop=True), RECEIVED).reply_class == "unsubscribe"
    v = classify.from_answer(classification("positive", asks_to_stop=True), RECEIVED)
    assert (v.reply_class, v.asks_to_stop, v.source) == ("positive", True, "model")


def test_C1_a_low_confidence_reply_asking_to_stop_is_unsubscribe_unless_a_person_must_answer_it():
    """Probe C1, changed knowingly (W1-2; RS §2 W1-2): at confidence 0.3 with asks_to_stop, negative, objection,
    not_now and other are unsubscribe (CAN-SPAM: a missed opt-out at any confidence); wrong_person keeps its class,
    as positive and referral do, so a person answers it, and poll_replies suppresses the address either way
    (test_poll_replies: every one of the five is suppressed and blocklisted)."""
    for cls in ("negative", "objection", "not_now", "other"):
        v = classify.from_answer(classification(cls, confidence=0.3, asks_to_stop=True), RECEIVED)
        assert (v.reply_class, v.asks_to_stop) == ("unsubscribe", True), cls
    v = classify.from_answer(classification("wrong_person", confidence=0.3, asks_to_stop=True), RECEIVED)
    assert (v.reply_class, v.asks_to_stop) == ("wrong_person", True)
    # Without a stop request, 0.3 is below SPEC 11's 0.7: "other", not wrong_person.
    assert classify.from_answer(classification("wrong_person", confidence=0.3), RECEIVED).reply_class == "other"
    assert classify.KEEP_ON_STOP == {"positive", "referral", "wrong_person"}


@pytest.mark.parametrize("cls, kept", [("positive", "positive"), ("referral", "referral"),
                                        ("wrong_person", "wrong_person"), ("negative", "unsubscribe"),
                                        ("not_now", "unsubscribe"), ("other", "unsubscribe"),
                                        ("out_of_office", "unsubscribe"), ("unsubscribe", "unsubscribe")])
def test_the_stop_rule_kept_apart_from_the_models_flag(cls, kept):
    """stop= is the regex's verdict: with it, the model's own asks_to_stop may be false and the rule still holds;
    the source says both looked (model+rule)."""
    v = classify.from_answer(classification(cls, confidence=0.9, asks_to_stop=False), RECEIVED, stop=True)
    assert (v.reply_class, v.asks_to_stop, v.source, v.model_class) == (kept, True, "model+rule", cls)
    assert v.by_rule


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
    # A stop phrasing is still asked about, and the cap's refusal is the stop rule's own unsubscribe.
    v = classify.classify(Ctx(claude), "unsubscribe", received=NOW)
    assert (v.reply_class, v.source) == ("unsubscribe", "rule") and v.error.startswith("BudgetExceeded")
    assert sdk.calls == []


class Raises:
    """A task model that fails the way a live call can: anything, a timeout included."""

    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def json(self, *a, **kw):
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize("exc", [ClaudeError("Claude API error 529"), BudgetExceeded("cap"), OSError("reset"),
                                 TimeoutError("ReadTimeout"), RuntimeError("anything")])
def test_with_a_stop_phrasing_any_model_failure_is_the_stop_rules_unsubscribe(exc):
    model = Raises(exc)
    text = "Sounds interesting, can we talk Tuesday? Please remove me from the newsletter though."
    v = classify.classify(Ctx(model), text, received=NOW)
    assert model.calls == 1 and (v.reply_class, v.source, v.asks_to_stop) == ("unsubscribe", "rule", True)
    assert v.error.startswith(type(exc).__name__)


def test_without_a_stop_phrasing_a_model_failure_is_raised_as_before():
    with pytest.raises(ClaudeError):
        classify.classify(Ctx(Raises(ClaudeError("Claude API error 529"))), "Sounds good.", received=NOW)
    with pytest.raises(OSError):
        classify.classify(Ctx(Raises(OSError("reset"))), "Sounds good.", received=NOW)


def test_a_warm_reply_with_a_stop_word_is_classified_and_keeps_its_class():
    claude, _, sdk, _ = make(sdk=FakeSDK(text='{"class": "positive", "confidence": 0.9, "asks_to_stop": false}'),
                             model="claude-sonnet-5-5")
    v = classify.classify(Ctx(claude), WARM_WITH_A_STOP_WORD[0], received=NOW)
    assert len(sdk.calls) == 1
    assert (v.reply_class, v.asks_to_stop, v.source) == ("positive", True, "model+rule")


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
