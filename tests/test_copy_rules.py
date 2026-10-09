"""The copy rules (SPEC 10 "Rules"; Harry, 30 Sep 2026): each rule fails and passes, and every
check returns every violation, not the first."""

from __future__ import annotations

import pytest

from us_outbound.copy import copy_rules
from us_outbound.copy.copy_rules import content_violations, email_violations, links, source_violations

PRIVACY = "https://www.spill.chat/us/privacy"
DEMO = "https://www.spill.chat/us/book-demo"
PAGE = "https://www.spill.chat/us/industry/mental-health-support-for-cpa-firms"


def run(body: str, *, subject: str = "Quick question", harry: bool = True, **kw) -> list[str]:
    """The word and structure rules on a subject and a body (what every email and reply draft gets)."""
    out: list[str] = []
    for part, text in (("subject", subject), ("body", body)):
        out += [f"{part} {v}" for v in content_violations(text, sender_is_harry=harry, **kw)]
        out += [f"{part} {v}" for v in copy_rules.structure_violations(text)]
    return out


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
    assert any('unfilled placeholder "{opt_out}"' in v for v in run("Opt out: {opt_out}"))
    assert any("subject" in v and "{{company}}" in v for v in run("Hi", subject="For {{company}}"))


def test_line_length_limit():
    assert run("a" * 300) == []
    [v] = run("ok\n" + "a" * 301)
    assert v == "body line 2 is 301 characters (the limit is 300)"


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
    found = run(body)
    for part in ('"licensed"', '"therapists"', '"unlimited"', '"counselling"', '"50%"', "301 characters"):
        assert any(part in v for v in found), part
    assert len(found) >= 6


def test_prospect_names_are_exempt_from_word_rules():
    body = "Hi Jane, how is The Colour Agency?"
    assert any("British spelling" in v for v in run(body))
    assert run(body, exempt=("The Colour Agency",)) == []
    assert run("Hi Al, also this.", exempt=("Al",)) == []  # whole words only


def test_max_line_constant():
    assert copy_rules.MAX_LINE == 300


# -- the copy as written (source_violations) ---------------------------------------------------

GOOD_BODY = "Hi {{first_name}},\n\n{{opener}}\n\nA short note.\n\nBest wishes,\n{{sender_first_name}}"


def test_clean_source_passes():
    assert source_violations("Busy season support for {{company}}", GOOD_BODY, step=1) == []


@pytest.mark.parametrize(
    "body, part",
    [
        ("Hello {{first_name}},\n\nA note.\n\nBest wishes,\n{{sender_first_name}}", 'start with the line "Hi {{first_name}},"'),
        ("Hi {{first_name}},\n\nA note.\n\nBest,\n{{sender_first_name}}", 'end with the line "Best wishes,"'),
        ("Hi {{first_name}},\n\nA note.\n\nHannah", 'end with the line "Best wishes,"'),
        ("Hi {{first_name}},\n\nPlans from $195 a month.\n\nBest wishes,\n{{sender_first_name}}", 'the price "$1'),
        ("Hi {{first_name}},\n\nIt's great!\n\nBest wishes,\n{{sender_first_name}}", "exclamation mark"),
        ("Hi {{first_name}},\n\n{{opener}} And more.\n\nBest wishes,\n{{sender_first_name}}", "{{opener}} must be alone"),
    ],
)
def test_source_rules(body, part):
    assert any(part in v for v in source_violations("Subject", body, step=1)), source_violations("Subject", body, step=1)


def test_subjects_are_calm_and_honest():
    assert any("Re:" in v for v in source_violations("Re: our chat", GOOD_BODY, step=1))
    assert any("emoji" in v for v in source_violations("Support 🎉", GOOD_BODY, step=1))
    assert any("exclamation" in v for v in source_violations("Act fast!", GOOD_BODY, step=1))


# -- each email as sent (email_violations) -------------------------------------------------------


def words(n: int) -> str:
    return "Hi Jane,\n\n" + " ".join(["word"] * n) + "\n\nBest,\nHannah"


def emailed(text: str = "", *, step: int = 3, found=(("book a short demo", DEMO),), subject: str = "A question", **kw):
    return email_violations(subject, text or words(50), list(found), step=step, demo_url=DEMO, industry_url=PAGE,
                            sender_is_harry=kw.pop("harry", True), **kw)


def test_a_clean_email_passes():
    assert emailed() == []


def test_every_email_has_exactly_one_demo_link():
    assert any("0 links to the demo page" in v for v in emailed(found=()))
    two = (("book a demo", DEMO), ("see a demo", DEMO))
    assert any("2 links to the demo page" in v for v in emailed(found=two))


def test_links_go_only_to_the_demo_page_the_industry_page_or_spill():
    ok = (("book a demo", DEMO), ("see how Spill works for CPA firms", PAGE), ("our privacy page", PRIVACY))
    # Each target alone is allowed; three links in one body break the two-links-an-email rule (below).
    assert [v for v in emailed(found=ok) if "links in the body" not in v] == []
    bad = emailed(found=(("book a demo", DEMO), ("our story", "https://example.com/x")))
    assert any('links to "https://example.com/x"' in v for v in bad)
    assert any("more than once" in v for v in emailed(found=(("book a demo", DEMO), ("a", PAGE), ("b", PAGE))))


def test_the_body_has_one_link_as_the_signature_adds_the_second():
    """Harry, 6 Oct 2026: two links an email at most, body and signature together."""
    assert emailed() == []
    two = (("book a short demo", DEMO), ("trusted by over 50,000 employees", "https://www.spill.chat/us"))
    assert [v for v in emailed(found=two) if "links in the body" in v] == [
        "has 2 links in the body; with the signature's link an email has 2 at most, so the body has one, its call "
        "to action: write any other as plain words"]


def test_anchor_text_says_where_the_link_goes():
    assert any('anchor text "here"' in v for v in emailed(found=(("here", DEMO),)))


def test_bare_addresses_and_spam_phrases_are_blocked():
    assert any("bare address" in v for v in emailed(words(40) + "\nSee spill.chat/us for more."))
    assert any("reads as spam" in v for v in emailed(words(40) + "\nAct now, it is risk-free."))


@pytest.mark.parametrize("step, n, ok", [(1, 39, False), (1, 40, True), (1, 120, True), (1, 121, False),
                                         (2, 149, False), (2, 300, True), (4, 25, True), (4, 91, False)])
def test_word_counts_by_email(step, n, ok):
    found = emailed(words(n), step=step)
    assert (not any("words; it should have" in v for v in found)) is ok, found


def test_a_subject_is_needed_and_the_word_rules_apply_to_it():
    assert any("subject is empty" in v for v in emailed(subject=""))
    assert any('subject says "Therapy"' in v for v in emailed(subject="Therapy for your team"))


def test_email_1_links_the_industry_page_not_the_demo():
    assert emailed(step=1, found=(("how Spill works for CPA firms", PAGE),), text=words(60)) == []
    demo = emailed(step=1, found=(("book a demo", DEMO), ("how Spill works", PAGE)), text=words(60))
    assert any("email 1 links the demo page" in v for v in demo)
    assert any("email 1 has 0 links to the industry page" in v for v in emailed(step=1, found=(), text=words(60)))
