"""UTM tags on our links (enrol/utm.py; Harry, 6 Oct 2026): the HTML's links to spill.chat and Harry's booking link
carry them, the words never change, and Instantly's unsubscribe link and Trustpilot never get them."""

from __future__ import annotations

import dataclasses
import html
import re

import pytest

from tests.test_render import BOOKING, DEMO, HANNAH, PAGE, TRUSTPILOT, contact, make_settings, sequence, values_for
from us_outbound.clients.instantly import UNSUBSCRIBE_HTML, UNSUBSCRIBE_PLAIN, UNSUBSCRIBE_TAG
from us_outbound.enrol import copy_desk, render, utm
from us_outbound.registry import mailboxes
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all, validate_tab

ON, OFF = make_settings(utm_links=True), make_settings()  # off by default until a seed send says otherwise
TAGS = "utm_source=us_outbound&utm_medium=email&utm_campaign=agencies-v1&utm_content=step2"


def hrefs(text: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]*)"', text)]


# -- which addresses ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url, tagged", [
    (DEMO, f"{DEMO}?{TAGS}"),
    (f"{PAGE}agencies", f"{PAGE}agencies?{TAGS}"),
    ("https://www.spill.chat/us", f"https://www.spill.chat/us?{TAGS}"),
    ("https://spill.chat/us/pricing#plans", f"https://spill.chat/us/pricing?{TAGS}#plans"),  # the fragment stays last
    ("https://www.spill.chat/us?ref=a", f"https://www.spill.chat/us?ref=a&{TAGS}"),
    (BOOKING, f"{BOOKING}?{TAGS}"),
])
def test_our_site_and_the_booking_link_are_tagged(url, tagged):
    assert utm.tag(url, ON, campaign="agencies-v1", content="step2") == tagged


@pytest.mark.parametrize("url", [
    UNSUBSCRIBE_TAG, "https://unsubscribe_instantly.ai/x",  # Instantly swaps it per lead: never touched
    TRUSTPILOT, "https://www.trustpilot.com/review/spill.chat",
    "https://meetings.hubspot.com/someone-else",  # not Harry's booking link
    "https://spill.chat.example.com/us", "https://example.com/?u=https://www.spill.chat/us",
    "http://www.spill.chat/us",  # only https
    "https://www.spill.chat/us?utm_source=partner",  # already tagged
    "{{demo_url}}", "",
])
def test_everything_else_is_left_alone(url):
    assert utm.tag(url, ON, campaign="agencies-v1", content="step2") == url


def test_the_switch_and_the_values():
    assert utm.tag(DEMO, OFF, campaign="agencies-v1", content="step2") == DEMO
    assert utm.tagger(OFF, copy_version="agencies-v1", step=1) is None
    assert utm.campaign_for("proptech-people-v1") == "proptech-people-v1"
    assert utm.campaign_for("Edited & v2") == "edited-and-v2" and utm.campaign_for("") == "general"
    assert utm.content_for(3) == "step3" and utm.content_for(3, signature=True) == "step3-signature"


# -- rendering ------------------------------------------------------------------------------------------------------


def test_the_html_is_tagged_and_the_words_never_change():
    on, off = sequence(settings=ON), sequence(settings=OFF)
    for a, b in zip(on, off, strict=True):
        assert a.ok and b.ok, render.violations(on + off)
        assert (a.subject, a.text, a.signature) == (b.subject, b.text, b.signature)  # plain text keeps bare links
        # The HTML differs only inside href="...": the words of every link are the same.
        assert re.sub(r'href="[^"]*"', "", a.html) == re.sub(r'href="[^"]*"', "", b.html)
        assert a.violations == b.violations
        for h in hrefs(a.html):
            if h.startswith(TRUSTPILOT):
                assert "utm_" not in h
            else:
                assert f"utm_campaign=agencies-v1&utm_content=step{a.step}" in h, h
        assert all("utm_" not in h for h in hrefs(b.html))
    sig = on[1].html.split(f'<p style="{render.SIGNATURE_STYLE}">')[1]
    assert hrefs(sig) == [f"{BOOKING}?utm_source=us_outbound&utm_medium=email&utm_campaign=agencies-v1&"
                          "utm_content=step2-signature"]


def test_email_format_text_keeps_the_addresses_bare():
    seq = sequence(settings=make_settings(email_format="text"))
    assert all(r.ok for r in seq) and all("utm_" not in r.body for r in seq)


def test_the_signature_wording_is_fixed_and_trustpilot_untagged():
    """CLAUDE.md: the signature's wording is fixed; only its address may gain tags, and never Trustpilot's."""
    for name in ("Jane", "Ana", "Bo", "Cy", "Dee", "Eli", "Flo", "Gus"):
        for a, b in zip(sequence(settings=ON, con=contact(first_name=name)),
                        sequence(settings=OFF, con=contact(first_name=name)), strict=True):
            assert a.text.split("\n\n")[-1] == b.text.split("\n\n")[-1]
            if a.signature == render.REVIEWS:
                assert hrefs(a.html.split(f'<p style="{render.SIGNATURE_STYLE}">')[1]) == [TRUSTPILOT]


def test_the_unsubscribe_link_never_gets_tags():
    """The campaign template adds Instantly's unsubscribe link after the rendered body; nothing tags it."""
    for text_only in (False, True):
        steps = mailboxes.campaign_steps(text_only)
        assert all(UNSUBSCRIBE_TAG in s["body"] and "utm_" not in s["body"] for s in steps)
    assert hrefs(UNSUBSCRIBE_HTML) == [UNSUBSCRIBE_TAG] and "utm_" not in UNSUBSCRIBE_PLAIN
    body = sequence(settings=ON)[0].body
    sent = f"<div>{body}</div>{UNSUBSCRIBE_HTML}"  # what Instantly sends: the template around the variable
    assert hrefs(sent)[-1] == UNSUBSCRIBE_TAG and sent.count("utm_source") == len(hrefs(body)) - (
        1 if TRUSTPILOT in body else 0)


def test_campaign_drift_does_not_see_the_tags():
    """registry/mailboxes.campaign_drift compares the step templates' links: the tags live in the lead's variables."""
    want = mailboxes.campaign_steps(False)
    campaign = {"sequences": [{"steps": [{"variants": [dict(s)]} for s in want]}]}
    drift = mailboxes.campaign_drift(campaign, ON, HANNAH.owner_name)
    assert not [k for k in drift if re.fullmatch(r"steps\.\d+", k)]  # each step's subject, body and links


def test_a_slack_edit_tags_like_the_card_it_replaces():
    """The edit path renders a CopyRow named after the card's copy_version, so its tags match the original's."""
    s = ON
    values = values_for(settings=s)
    original = render.render_step(s.copy[0], values, step=3, mailbox=HANNAH, settings=s)
    edited_row = dataclasses.replace(s.copy[0], industry="Advertising agencies")  # the edit path's industry
    edited = render.render_step(edited_row, values, step=3, mailbox=HANNAH, settings=s, for_send=False)
    assert hrefs(original.html) == hrefs(edited.html)


def test_copy_rules_read_the_bare_addresses_across_the_library():
    """The sheet check finds the same problems, every email the same words, with the tags on or off (a sample of the
    build's Copy rows; `us-outbound copy check` renders them all)."""
    off, errors = validate_all(default_tabs())
    assert not any(errors.values()) and off.general.utm_links is False
    settings = dataclasses.replace(off, general=dataclasses.replace(off.general, utm_links=True))
    rows = settings.copy[::30]  # every industry family's row would take ten seconds; these cover the shapes
    assert len(rows) >= 10
    for row in rows:
        assert copy_desk.check_row(row, settings).problems == copy_desk.check_row(row, off).problems
        mb = copy_desk.sample_mailboxes(settings)[0]
        role = copy_desk.roles_for(row)[0]
        values = copy_desk._values(row, settings, mb, role, opener=copy_desk.SAMPLE_OPENER)
        on_seq = render.render_sequence(row, values, mailbox=mb, settings=settings, for_send=False)
        off_seq = render.render_sequence(row, values, mailbox=mb, settings=off, for_send=False)
        assert [r.text for r in on_seq] == [r.text for r in off_seq]
        assert any("utm_source=us_outbound" in r.html for r in on_seq)


def test_the_general_switch():
    general, errors = validate_tab("General", [{"key": "utm_links", "value": "yes"}])
    assert not errors and general.utm_links is True
    rows = {r["key"]: r for r in default_tabs()["General"]}
    assert rows["utm_links"]["value"] == "no" and "Trustpilot and unsubscribe links are never tagged" in \
        rows["utm_links"]["note"]
    _, errors = validate_tab("General", [{"key": "utm_links", "value": "maybe"}])
    assert errors
