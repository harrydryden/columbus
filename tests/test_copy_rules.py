"""The render-time copy check (SPEC 10 "Rules"): each rule fails and passes, and check()
returns every violation, not the first."""

from __future__ import annotations

import dataclasses

import pytest

from us_outbound.enrol import copy_rules
from us_outbound.enrol.copy_rules import check, content_violations, links
from us_outbound.settings.model import CopyRow

PRIVACY = "https://www.spill.chat/us/privacy"
ROW = CopyRow("general-v1", "General", 2, "Subject", "Body", "approved", "Harry Dryden")
FOOTER = f"\n\nHannah Spalding, Spill\n1 Example Street, London\nThis is a marketing email from Spill.\nReply STOP or use this link to opt out: {PRIVACY}"


def run(body: str, *, subject: str = "Quick question", step: int | None = 2, harry: bool = True, row=ROW, **kw) -> list[str]:
    return check(subject, body, copy_row=row, step=step, sender_is_harry=harry, privacy_url=PRIVACY, **kw)


def test_clean_copy_passes():
    body = ("Hi Jane,\n\nSpill is same-day counseling with registered counselors, booked in Slack or Teams. "
            "30% of employees use Spill. Your team already has an EAP, which says you take this seriously.")
    assert run(body) == []


@pytest.mark.parametrize(
    "bad, good",
    [
        ("Book a therapy session today.", "Book a counseling session today."),
        ("Talk to a therapist in Slack.", "Talk to a counselor in Slack."),
        ("Our therapists are on call.", "Our counselors are on call."),
        ("Sessions with licensed counselors.", "Sessions with registered counselors."),
        ("Unlimited sessions for your team.", "As many sessions as your team needs."),
    ],
)
def test_banned_words(bad, good):
    assert run(bad) and run(good) == []


def test_unlimited_pto_in_an_opener_is_flagged():
    assert any('"unlimited"' in v for v in content_violations("Saw your benefits page mentions unlimited PTO"))


@pytest.mark.parametrize(
    "bad",
    ["EAPs don't work.", "Swap that useless EAP.", "Nobody uses their EAP.", "No one uses your EAP.",
     "A bad EAP costs you.", "Your EAP is a waste.", "Replace that outdated EAP.",
     "Your current benefits aren't working."],
)
def test_disparaging_an_eap_is_blocked(bad):
    assert any("disparages" in v for v in run(bad))


def test_naming_their_eap_is_fine():
    assert run("Your team already has an EAP. Spill works alongside the benefits you already offer.") == []


@pytest.mark.parametrize("bad", ["Meet the Spill EAP.", "Spill's EAP is in Slack.", "Our EAP is different.",
                                 "Spill is an EAP your team will use."])
def test_eap_in_spills_name_is_blocked(bad):
    assert any("Spill's product name" in v for v in run(bad))


def test_eap_alternative_is_not_spills_name():
    assert run("Their EAP stays; Spill is an EAP alternative your team will use.") == []


BRITISH = [
    "counselling", "counsellor", "utilisation", "organisation", "organise", "prioritise", "recognise", "behaviour",
    "behavioural", "programme", "colour", "favour", "favourite", "labour", "centre", "travelling", "cancelled",
    "enrol", "enrolment", "licence", "practise", "analyse", "catalogue", "wellbeing", "judgement", "whilst",
    "personalised", "specialise", "honour", "theatre", "per cent",
]


@pytest.mark.parametrize("word", BRITISH)
def test_british_spellings_are_blocked(word):
    found = run(f"We {word} this.")
    assert any("British spelling" in v for v in found), word


@pytest.mark.parametrize(
    "word",
    ["counseling", "counselor", "utilization", "organization", "organize", "prioritize", "behavior", "program",
     "color", "favorite", "labor", "center", "traveling", "canceled", "enroll", "enrollment", "license", "practice",
     "analyze", "analyses", "catalog", "well-being", "specialist", "economist", "hour", "your", "contour", "genre",
     "advertise", "exercise", "enterprise", "glamour"],
)
def test_american_spellings_pass(word):
    assert run(f"We {word} this.") == [], word


def test_british_spelling_names_the_american_form():
    [v] = run("Our counselling is fast.")
    assert '"counselling" (American: "counseling")' in v


@pytest.mark.parametrize(
    "bad",
    ["20% of teams say so.", "50% of managers agree.", "It is 3x faster.", "2 in 3 people burn out.",
     "Get 30% off this month.", "Trusted by 500+ companies.", "Rated 4.8/5 by members.", "One in four people struggle.",
     "It costs 40 percent less."],
)
def test_statistics_other_than_30_percent_utilization_are_blocked(bad):
    assert any("statistic" in v for v in run(bad)), bad


@pytest.mark.parametrize(
    "good",
    ["30% of employees use Spill.", "About 30% of your team use it.", "Utilization is 30% across our customers.",
     "It's $350 a month, flat.", "A 20-minute walkthrough.", "50-minute sessions."],
)
def test_the_utilization_figure_prices_and_durations_pass(good):
    assert run(good) == [], good


def test_unrendered_variables_and_placeholders():
    assert any('unrendered variable "{{proof}}"' in v for v in run("Hi,\n\n{{proof}}"))
    assert any('unmatched "{{" or "}}"' in v for v in run("Hi }} there"))
    assert any('unfilled placeholder "{privacy_url}"' in v for v in run("Opt out: {privacy_url}"))
    assert any("subject" in v and "{{company}}" in v for v in run("Hi", subject="For {{company}}"))


def test_line_length_limit():
    assert run("a" * 300) == []
    [v] = run("ok\n" + "a" * 301)
    assert v == "body line 2 is 301 characters (the limit is 300)"


@pytest.mark.parametrize("status", ["draft", "retired", ""])
def test_copy_row_must_be_approved(status):
    row = dataclasses.replace(ROW, status=status)
    assert any("not approved" in v for v in run("Hi Jane.", row=row))


def test_reply_drafts_have_no_row_or_step():
    assert check("Re: hello", "Hi Jane, grab a time with me.", copy_row=None, step=None, sender_is_harry=True) == []


def test_step1_carries_exactly_one_link_the_privacy_page():
    row = dataclasses.replace(ROW, step=1)
    assert run("Hi Jane." + FOOTER, step=1, row=row) == []
    assert any("step 1 has 0 links" in v for v in run("Hi Jane.", step=1, row=row))
    two = run("Hi Jane, see spill.chat/us." + FOOTER, step=1, row=row)
    assert any("step 1 has 2 links" in v for v in two)
    wrong = check("Hi", "Hi Jane.\nhttps://example.com/x", copy_row=row, step=1, sender_is_harry=True, privacy_url=PRIVACY)
    assert any("must be the privacy and opt-out page" in v for v in wrong)
    assert any("subject is empty" in v for v in run("Hi Jane." + FOOTER, subject="", step=1, row=row))


def test_later_steps_may_have_more_links():
    assert run("Grab a time: https://meetings.hubspot.com/harry336/us-demo-link" + FOOTER) == []


def test_links_finds_urls_and_bare_domains_but_not_emails():
    text = "See https://www.spill.chat/us/privacy. Or spill.chat/us, www.example.org and Apollo.io; mail hannah@meetspill.org, e.g. U.S."
    assert links(text) == ["https://www.spill.chat/us/privacy", "spill.chat/us", "www.example.org", "Apollo.io"]


def test_demos_are_always_with_harry():
    assert run("Grab a time with me here.", harry=True) == []
    assert any("demos are always with Harry Dryden" in v for v in run("Grab a time with me here.", harry=False))
    assert run("My colleague Harry Dryden runs our US demos.", harry=False) == []
    assert any('"my colleague"' in v for v in run("My colleague Harry Dryden runs our US demos.", harry=True))


def test_same_day_forms_pass():
    assert run("Same-day counseling. Often the same day.") == []


def test_every_violation_is_returned():
    body = "Our licensed therapists offer unlimited counselling. 50% of teams agree.\n" + "x" * 301
    found = run(body, row=dataclasses.replace(ROW, status="draft"))
    for part in ("not approved", '"licensed"', '"therapists"', '"unlimited"', '"counselling"', '"50%"', "301 characters"):
        assert any(part in v for v in found), part
    assert len(found) >= 7


def test_prospect_names_are_exempt_from_word_rules():
    body = "Hi Jane, how is The Colour Agency?"
    assert any("British spelling" in v for v in run(body))
    assert run(body, exempt=("The Colour Agency",)) == []
    assert run("Hi Al, also this.", exempt=("Al",)) == []  # whole words only


def test_max_line_constant():
    assert copy_rules.MAX_LINE == 300
