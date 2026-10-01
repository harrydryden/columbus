"""Copy rendering (SPEC 10; Harry, 30 Sep 2026): variables, the markup, the footer, the Article 14
notice, and every row against empty and maximum-length values. Settings are built here directly
from the model; the builders are shared with test_copy_rules, test_queue, test_enrol and others."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from us_outbound.enrol import copy_markup, copy_rules, render
from us_outbound.settings.model import (
    Angle,
    CopyRow,
    CopyStep,
    DateRange,
    General,
    Industry,
    Mailbox,
    Override,
    Settings,
)
from us_outbound.settings.model import Test as ABTest  # aliased so pytest does not collect it

POSTAL = "Spill Group Ltd, 1 Example Street, London, EC1A 1AA, UK"
PRIVACY = "https://www.spill.chat/us/privacy"
DEMO = "https://www.spill.chat/us/book-demo"
PAGE = "https://www.spill.chat/us/industry/"
AD_LINE = "This is a marketing email from Spill."
STOP_LINE = f"Reply STOP or use this link to opt out: {PRIVACY}"


def mailbox(address: str, owner: str, status: str = "Active", cap: int = 30, **kw) -> Mailbox:
    return Mailbox(
        address=address, domain=address.split("@")[1], owner_name=owner, status=status, daily_cap=cap,
        instantly_account_id=address, signature=kw.pop("signature", f"{owner}\nSpill\nspill.chat/us"), **kw,
    )


HANNAH = mailbox("hannah@meetspill.org", "Hannah Spalding")
SAM = mailbox("sam@meetspill.org", "Sam Jackson")
HARRY_M = mailbox("harry@meetspill.org", "Harry Dryden")
HARRY_T = mailbox("harry@tryspill.org", "Harry Dryden")
MAILBOXES = (HANNAH, HARRY_M, SAM, HARRY_T)

EAP_OPENER = "I saw your team already has an employee assistance program."
GENERAL_OPENER = "I wanted to share a simple way to give your team mental health support."
ANGLES = (
    Angle("Upgrade the EAP", 1, "Your team already has an EAP.", EAP_OPENER, True),
    Angle("Progressive employer", 2, "You already invest in your people.", "It's clear you already invest in your people.", True),
    Angle("Growing team", 3, "Hiring fast means onboarding stress.", "It looks like your team is growing fast.", True),
    Angle("General", 4, "Mental health support your team will use.", GENERAL_OPENER, True),
)
AGENCIES, TECH = "Marketing & Creative Agencies", "Technology & Startups"
INDUSTRIES = (
    Industry(AGENCIES, AGENCIES, True, landing_page_url=PAGE + "agencies",
             proof_point="UK example: the London agency Mother gives its whole team Spill.", priority=2),
    Industry("Advertising agencies", AGENCIES, True, landing_page_url=PAGE + "advertising", priority=2),
    Industry(TECH, TECH, True, landing_page_url=PAGE + "tech", proof_point="UK example: Monzo's teams use Spill.", priority=1),
    Industry("Fintech", TECH, True, priority=1),  # no page of its own
    Industry("Legal Teams", "Legal Teams", True, landing_page_url=PAGE + "legal",
             proof_point="UK example: a London law firm gives its staff Spill.", priority=4),
    Industry("Staffing agencies", "Professional Services", False, priority=5),
)
LEGAL_OVERLAY = "The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?"

SUBJECTS = {1: "Support for the {{company}} team", 2: "How Spill works for agencies",
            3: "Will anyone know who's using it?", 4: "One last note"}
BODIES = {
    1: "Hi {{first_name}},\n\n{{opener}}\n\n{{legal_overlay}}\n\nIn most agencies, the work runs on deadlines and client "
       "moods, and the strain stays hidden until someone good leaves.\n\n{{role_line}}\n\nSpill gives your team private "
       "counseling, often the same day, booked from any phone or from Slack, with evening sessions that fit around "
       "launches. If it's worth a look, you can [see a quick demo]({{demo_url}}).\n\nBest wishes,\n{{sender_first_name}}",
    2: "Hi {{first_name}},\n\nIn case it's useful, here's a short overview of Spill for agencies.\n\n"
       "**What is Spill?**\nSpill is an on-demand counseling service, [trusted by over 100,000 employees]({{site_url}}). We help "
       "agencies increase productivity, reduce absenteeism and free up HR time by addressing the issues that most often "
       "derail performance at work.\nWith Spill, employees get fast, easy access to professional counseling, and managers "
       "get the tools they need to support anyone on their team who's struggling.\n\n"
       "**Who is Spill for?**\nAnyone on your team who's struggling with personal or professional issues that affect "
       "their well-being and job performance.\nThat could be work-related challenges (like stress or burnout), mental "
       "health conditions (like anxiety, depression or ADHD) or life events (like having a baby or losing someone close).\n\n"
       "**What makes Spill unique**\n- Employees can get support the same day, in just a couple of clicks. No waiting "
       "lists or callbacks.\n- Sessions run early mornings, evenings and weekends, so support fits around pitches and "
       "launches.\n- We integrate with the tools you already use, like email, Slack and Microsoft Teams.\n"
       "- {{price_line}} We don't lock you in.\n\n"
       "If you'd like more detail, you can [see how Spill works for agencies]({{industry_url}}).\n\n"
       "To hear more and get a quote for your team, [book a short demo]({{demo_url}}).\n\nBest wishes,\n{{sender_first_name}}",
    3: "Hi {{first_name}},\n\nOne question agencies often ask: will anyone know who's using it?\n\nNo. People book "
       "directly and privately, and you see only anonymized, aggregate data. That privacy is often what makes people "
       "willing to use it at all.\n\nSetup takes hours, so it can be ready before your next busy stretch. If that's "
       "useful, [book 20 minutes for a demo]({{demo_url}}).\n\nBest wishes,\n{{sender_first_name}}",
    4: "Hi {{first_name}},\n\nI'll leave it here for now.\n\nIf support for your team in {{place}} moves up the list, "
       "Spill can be set up in hours, with counseling that fits around client work.\n\nWhenever the timing suits, "
       "[book a demo]({{demo_url}}) and we'll walk you through it.\n\nBest wishes,\n{{sender_first_name}}",
}
ROLE_LINES = {
    "People leader": "If you're the one people come to when a launch goes sideways, you want support they'll actually use.",
    "Founder or executive": "Keeping your best people through a hard quarter matters more than any single pitch.",
    "Operations": "Sick days and scrambled cover in launch weeks add up faster than most people expect.",
}


def copy_row(version: str, industry: str, *, status: str = "approved", role: str = "", qa: bool = True,
             bodies=None, subjects=None, role_lines=None) -> CopyRow:
    bodies, subjects = bodies or BODIES, subjects or SUBJECTS
    row = CopyRow(version, industry, status, tuple(CopyStep(subjects[n], bodies[n]) for n in (1, 2, 3, 4)), role,
                  dict(ROLE_LINES if role_lines is None else role_lines),
                  "Harry Dryden" if status == "approved" else "")
    return dataclasses.replace(row, qa=f"pass {row.content_hash()}") if qa else row


COPY = (copy_row("agencies-v1", AGENCIES), copy_row("general-v1", "General"))
FIRST_TEST = ABTest("t1-agencies", "A shorter email 1 beats the first", "agencies-v1", "agencies-v2", 400, "running")
BLACKOUTS = (DateRange(date(2026, 11, 23), date(2026, 11, 27)), DateRange(date(2026, 12, 18), date(2027, 1, 4)))


def make_settings(*, mailboxes=MAILBOXES, copy=COPY, tests=(), overrides=(), focus=(), named_accounts=(),
                  industries=INDUSTRIES, **general) -> Settings:
    g = General(
        postal_address=POSTAL, privacy_url=PRIVACY, hubspot_owner_id="owner-harry",
        clay_monthly_credits=2000.0, clay_credits_per_account=5.0, blackout_dates=BLACKOUTS,
    )
    return Settings(
        general=dataclasses.replace(g, **general), angles=ANGLES, industries=tuple(industries), copy=tuple(copy),
        mailboxes=tuple(mailboxes), tests=tuple(tests), overrides=tuple(overrides), focus=tuple(focus),
        named_accounts=tuple(named_accounts),
    )


def account(**kw) -> dict:
    return {
        "account_id": "acc-1", "domain": "acmecreative.com", "clean_name": "Acme Creative", "hq_city": "Chicago",
        "hq_state": "IL", "industry": "Advertising agencies", "industry_group": AGENCIES,
        "employees": 64, "size_band": "50-99", "tier": "Priority", "score": 60, "angle": "Upgrade the EAP",
        "status": "verified", **kw,
    }


def contact(**kw) -> dict:
    return {
        "contact_id": "con-1", "account_id": "acc-1", "first_name": "Jane", "last_name": "Doe", "role": "People leader",
        "title": "Head of People", "email": "jane@acmecreative.com", "email_status": "verified", "person_state": "IL",
        **kw,
    }


def values_for(mb=HANNAH, settings=None, acct=None, con=None, opener=EAP_OPENER, overlay="", row=None) -> dict[str, str]:
    s = settings or make_settings()
    return render.variables(acct or account(), con or contact(), mb, s, copy_row=row or s.copy[0], opener=opener,
                            legal_overlay=overlay)


def sequence(row=None, mb=HANNAH, settings=None, **kw) -> list[render.Rendered]:
    s = settings or make_settings()
    row = row or s.copy[0]
    return render.render_sequence(row, values_for(mb, s, row=row, **kw), mailbox=mb, settings=s)


# -- variables --------------------------------------------------------------------------


def test_variables_for_one_lead():
    v = values_for()
    assert v["first_name"] == "Jane" and v["company"] == "Acme Creative" and v["place"] == "Chicago, IL"
    assert v["opener"] == EAP_OPENER
    assert v["role_line"] == ROLE_LINES["People leader"]
    assert v["price_line"] == "Plans start from $250 a month, on a rolling 30-day contract."
    assert v["demo_url"] == DEMO and v["site_url"] == "https://www.spill.chat/us"
    assert v["industry_url"] == PAGE + "agencies"  # the Copy row's industry (the group) and its page
    assert v["sender_first_name"] == "Hannah"
    assert v["proof"] == INDUSTRIES[0].proof_point  # the label has none, so the group's row
    assert v["legal_overlay"] == ""
    assert set(v) == set(render.VARIABLES)


def test_role_line_follows_the_contact_s_role():
    assert values_for(con=contact(role="Operations"))["role_line"] == ROLE_LINES["Operations"]
    assert values_for(con=contact(role="Finance"))["role_line"] == ""


def test_the_general_row_links_the_account_s_own_page():
    s = make_settings()
    general = s.copy_row("general-v1")
    assert values_for(settings=s, row=general)["industry_url"] == PAGE + "advertising"
    fintech = account(industry="Fintech", industry_group=TECH)  # no page: its group's
    assert values_for(settings=s, row=general, acct=fintech)["industry_url"] == PAGE + "tech"


def test_one_starting_price_whatever_the_team_size():
    """Harry, 1 Oct 2026: "Plans start from $250 a month", as on the website, for every team (General price_from)."""
    for employees in (12, 64, 240):
        assert values_for(acct=account(employees=employees))["price_line"] == (
            "Plans start from $250 a month, on a rolling 30-day contract.")
    assert render.price_line(make_settings(price_from=195)) == "Plans start from $195 a month, on a rolling 30-day contract."


def test_an_industry_with_no_page_links_the_site():
    no_page = (Industry("Retail & E-commerce", "Retail & E-commerce", True),)
    s = make_settings(industries=INDUSTRIES + no_page, copy=(copy_row("retail-v1", "Retail & E-commerce"),))
    retail = account(industry="Retail & E-commerce", industry_group="Retail & E-commerce")
    assert values_for(settings=s, acct=retail)["industry_url"] == "https://www.spill.chat/us"
    seq = sequence(settings=s, acct=retail)
    assert all(r.ok for r in seq), render.violations(seq)  # the site, linked twice in email 2, is fine


def test_place_and_proof_fallbacks():
    s = make_settings()
    assert render.place_for({"hq_state": "NY"}) == "NY"
    assert render.place_for({}) == ""
    assert render.proof_for({"industry": "Fintech", "industry_group": TECH}, s).startswith("UK example: Monzo")
    assert render.proof_for({"industry": "Unknown label", "industry_group": ""}, s) == ""
    assert render.is_demo_host(HARRY_T, s) and not render.is_demo_host(SAM, s)


def test_legal_overlay_only_for_legal_teams():
    legal = account(industry="Legal Teams", industry_group="Legal Teams")
    assert values_for(acct=legal, overlay=LEGAL_OVERLAY)["legal_overlay"] == LEGAL_OVERLAY
    assert values_for(overlay=LEGAL_OVERLAY)["legal_overlay"] == ""


def test_overrides_win_over_the_account_row():
    s = make_settings(overrides=(Override("acmecreative.com", "clean_name", "Acme"), Override("acmecreative.com", "employees", "12")))
    v = values_for(settings=s)
    assert v["company"] == "Acme"


def test_pick_opener_drops_evidence_that_breaks_a_rule():
    opener, why = render.pick_opener("Saw your benefits page mentions unlimited PTO")
    assert opener == "" and "unlimited" in why
    opener, why = render.pick_opener("Saw your benefits page mentions 100% employer-paid")
    assert opener == "" and "100%" in why
    assert render.pick_opener("Saw your benefits page mentions mental health days") == (
        "Saw your benefits page mentions mental health days", "")
    assert render.pick_opener("") == ("", "")


# -- the markup ----------------------------------------------------------------------------------


def test_markup_to_html_and_text():
    src = "Hi {{first_name}},\n\n**Why**\n- one\n- two\n\nSee [the page]({{demo_url}}).\nNext line."
    r = copy_markup.render(src, {"first_name": "Jane <b>", "demo_url": DEMO})
    assert r.html == ('<p>Hi Jane &lt;b&gt;,</p><p><strong>Why</strong></p><ul><li>one</li><li>two</li></ul>'
                      f'<p>See <a href="{DEMO}">the page</a>.<br>Next line.</p>')
    assert r.text == f"Hi Jane <b>,\n\nWhy\n\n• one\n• two\n\nSee the page ({DEMO}).\nNext line."
    assert r.words == "Hi Jane <b>,\n\nWhy\n\n• one\n• two\n\nSee the page.\nNext line."
    assert r.links == [("the page", DEMO)] and r.problems == []


def test_values_never_become_markup():
    r = copy_markup.render("Hi {{company}}.", {"company": "[Acme](https://evil.example) **Co**"})
    assert "<a " not in r.html and "<strong>" not in r.html and r.links == []


def test_an_empty_optional_line_disappears():
    r = copy_markup.render("Hi.\n\n{{opener}}\n\nMore.", {"opener": ""}, optional={"opener"})
    assert r.html == "<p>Hi.</p><p>More.</p>" and r.problems == []
    r = copy_markup.render("Hi.\n\n{{opener}}\n\nMore.", {"opener": ""})
    assert any("empty variable {{opener}}" in p for p in r.problems)


@pytest.mark.parametrize(
    "src, part",
    [
        ("Hi <b>there</b>.", "has HTML"),
        ("See [the page](https://www.spill.chat.", "broken link"),
        ("Some **bold.", "unmatched **"),
        ("- only one bullet", "one bullet"),
        ("Go to https://www.spill.chat/us now.", "bare address"),
        ("See [](https://www.spill.chat/us).", "no anchor text"),
        ("See [www.spill.chat](https://www.spill.chat/us).", "anchor text is an address"),
    ],
)
def test_markup_problems(src, part):
    _, problems = copy_markup.parse(src)
    assert any(part in p for p in problems), problems


# -- rendering ------------------------------------------------------------------------------------


def test_clean_sequence_renders_without_violations():
    for mb in MAILBOXES:
        seq = sequence(mb=mb)
        assert [r.step for r in seq] == [1, 2, 3, 4]
        assert all(r.ok for r in seq), render.violations(seq)
    cv = render.custom_variables(sequence())
    assert set(cv) == {f"s{i}_{p}" for i in range(1, 5) for p in ("subject", "body")}
    assert cv["s1_subject"] == "Support for the Acme Creative team"
    assert f'<a href="{DEMO}">see a quick demo</a>' in cv["s1_body"]
    assert f'<a href="{PAGE}agencies">see how Spill works for agencies</a>' in cv["s2_body"]
    assert "<ul><li>Employees can get support the same day" in cv["s2_body"]


def test_email_format_text_sends_plain_text_with_links_written_out():
    s = make_settings(email_format="text")
    seq = sequence(settings=s)
    assert all(r.ok for r in seq)
    assert f"see a quick demo ({DEMO})" in seq[0].body and "<p>" not in seq[0].body


def test_the_role_line_and_opener_land_in_email_1():
    first = sequence()[0].text
    assert EAP_OPENER in first and ROLE_LINES["People leader"] in first
    no_opener = sequence(opener="")[0]
    assert no_opener.ok and EAP_OPENER not in no_opener.text
    assert "Hi Jane,\n\nIn most agencies" in no_opener.text  # the opener's line went with it


def test_footer_on_every_email_and_the_notice_on_email_1():
    seq = sequence()
    for r in seq:
        assert r.text.splitlines()[-2:] == [AD_LINE, STOP_LINE]
        assert f'<a href="{PRIVACY}">{PRIVACY}</a>' in r.html
    assert "Where we got your details" in seq[0].text
    assert all("Where we got your details" not in r.text for r in seq[1:])


def test_footer_role_shown_only_when_set():
    s = make_settings()
    with_role = dataclasses.replace(HANNAH, owner_role="Partnerships")
    assert "Hannah Spalding, Partnerships" in render.footer(with_role, s)[0]
    assert render.footer(HANNAH, s)[0].splitlines()[0] == "Hannah Spalding, Spill"


def test_unknown_and_empty_variables_stay_visible_and_block():
    bodies = dict(BODIES)
    bodies[3] = BODIES[3].replace("One question", "{{nickname}} One question")
    s = make_settings(copy=(copy_row("agencies-v1", AGENCIES, bodies=bodies),))
    seq = sequence(settings=s)
    assert any("unknown variable {{nickname}}" in v for v in seq[2].violations)
    assert "{{nickname}}" in seq[2].text
    blank = sequence(con=contact(first_name=""))
    assert all(any("empty variable {{first_name}}" in v for v in r.violations) for r in blank)


def test_blank_postal_address_or_privacy_url_blocks_every_email():
    s = make_settings(postal_address="", privacy_url="")
    for r in sequence(settings=s):
        assert any("postal_address is blank" in v for v in r.violations)
        assert any("privacy_url is blank" in v for v in r.violations)


def test_draft_or_unchecked_copy_blocks():
    s = make_settings(copy=(copy_row("agencies-v1", AGENCIES, status="draft"),))
    assert all(any("is draft, not approved" in v for v in r.violations) for r in sequence(settings=s))
    s = make_settings(copy=(copy_row("agencies-v1", AGENCIES, qa=False),))
    assert all(any("has not passed QA" in v for v in r.violations) for r in sequence(settings=s))


def test_an_edit_after_qa_needs_qa_again():
    row = COPY[0]
    edited = dataclasses.replace(row, steps=(CopyStep(row.step(1).subject, row.step(1).body + " "),) + row.steps[1:])
    assert edited.qa_current  # trailing spaces do not change the wording
    edited = dataclasses.replace(row, steps=(CopyStep("A new subject", row.step(1).body),) + row.steps[1:])
    assert not edited.qa_current and edited.qa_verdict == "pass"


def test_the_sequence_must_link_the_industry_page():
    bodies = dict(BODIES)
    bodies[2] = BODIES[2].replace("If you'd like more detail, you can [see how Spill works for agencies]({{industry_url}}).\n\n", "")
    s = make_settings(copy=(copy_row("agencies-v1", AGENCIES, bodies=bodies),))
    assert any("never links the industry page" in v for v in sequence(settings=s)[1].violations)
    # An industry with no page still links one: the site (Harry, 1 Oct 2026: a link in every sequence).
    fintech_row = copy_row("fintech-v1", "Fintech", bodies=bodies)
    s = make_settings(copy=(fintech_row,))
    fintech = account(industry="Fintech", industry_group=TECH)
    assert any("never links the industry page" in v for v in sequence(row=fintech_row, settings=s, acct=fintech)[1].violations)


def test_a_sender_who_is_not_harry_cannot_offer_a_time_with_me():
    bodies = dict(BODIES)
    bodies[4] = BODIES[4].replace("[book a demo]({{demo_url}})", "[grab a time with me]({{demo_url}})")
    s = make_settings(copy=(copy_row("agencies-v1", AGENCIES, bodies=bodies),))
    assert any("demos are always with Harry Dryden" in v for v in sequence(settings=s, mb=HANNAH)[3].violations)
    assert sequence(settings=s, mb=HARRY_M)[3].ok


# -- every row against empty and maximum-length values (SPEC 10) -------------------------


def test_every_row_against_empty_values():
    s = make_settings()
    for row in s.copy:
        for mb in s.mailboxes:
            for r in render.render_sequence(row, render.empty_variables(), mailbox=mb, settings=s):
                assert any("empty variable" in v for v in r.violations), (row.copy_version, r.step)
                assert r.text.splitlines()[-2:] == [AD_LINE, STOP_LINE]  # the footer is still there


def test_every_row_against_maximum_length_values():
    s = make_settings()
    for row in s.copy:
        values = render.max_length_variables(row, s)
        for mb in s.mailboxes:
            for r in render.render_sequence(row, values, mailbox=mb, settings=s):
                assert not any("empty variable" in v for v in r.violations)
                long_lines = [line for line in r.text.split("\n") if len(line) > copy_rules.MAX_LINE]
                if r.step == 1:  # the opener runs past 300 characters at its longest
                    assert long_lines and any("characters (the limit is 300)" in v for v in r.violations)


def test_max_rendered_lengths_probe():
    s = make_settings()
    lengths = render.max_rendered_lengths(s)
    assert set(lengths) == {f"s{i}_{p}" for i in range(1, 5) for p in ("subject", "body")}
    assert lengths["s2_body"] > lengths["s3_body"] > 500 and lengths["s1_subject"] >= 100
    seq = render.render_sequence(COPY[0], render.max_length_variables(COPY[0], s), mailbox=HANNAH, settings=s)
    for k, v in render.custom_variables(seq).items():
        assert len(v) <= lengths[k]


def test_templates_are_drafts_and_comments_are_stripped():
    for name in (render.FOOTER_TEMPLATE, render.ARTICLE14_TEMPLATE):
        raw = (render.TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert raw.splitlines()[0].startswith("# DRAFT — for Harry to approve")
        text = render.load_template(name)
        assert "#" not in text and "DRAFT" not in text
        assert all(len(line) <= copy_rules.MAX_LINE for line in text.splitlines())
        assert copy_rules.content_violations(text) == []


def test_a_long_opener_never_pushes_email_1_over_its_word_limit():
    long_opener = "Saw your careers page: " + " ".join(["words"] * 60) + "."
    first = sequence(opener=long_opener)[0]
    assert long_opener in first.text and not any("words; it should have" in v for v in first.violations)
