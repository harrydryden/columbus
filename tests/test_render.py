"""Copy rendering (SPEC 10): variables, footer, Article 14 notice, and every template against
empty and maximum-length values. Settings are built here directly from the model; the
builders are shared with test_copy_rules, test_queue and test_enrol."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from us_outbound.enrol import copy_rules, render
from us_outbound.settings.model import (
    Angle,
    CopyRow,
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
BOOKING = "https://meetings.hubspot.com/harry336/us-demo-link"
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
INDUSTRIES = (
    Industry("Marketing & Creative Agencies", "Marketing & Creative Agencies", True,
             proof_point="UK example: the London agency Mother gives its whole team Spill.", priority=2),
    Industry("Advertising agencies", "Marketing & Creative Agencies", True, priority=2),
    Industry("Technology & Startups", "Technology & Startups", True, proof_point="UK example: Monzo's teams use Spill.", priority=1),
    Industry("Fintech", "Technology & Startups", True, priority=1),
    Industry("Legal Teams", "Legal Teams", True, proof_point="UK example: a London law firm gives its staff Spill.", priority=4),
    Industry("Staffing agencies", "Professional Services", False, priority=5),
)
LEGAL_OVERLAY = "The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?"

SUBJECTS = {1: "Mental health support for the {{company}} team", 2: "Same-day counseling for {{company}}",
            3: "Quick question", 4: "Closing the loop"}
BODIES = {
    1: "Hi {{first_name}},\n\n{{opener}}\n\n{{legal_overlay}}\n\nSpill is mental health support your team will use: "
       "same-day counseling with registered counselors, booked right in Slack or Teams, for one flat monthly fee. "
       "30% of employees use Spill.\n\n{{ask}}",
    2: "Hi {{first_name}},\n\nFollowing up on my note about mental health support for the {{company}} team.\n\n"
       "{{proof}}\n\n{{price_line}}\n\nIf you'd like to see how it works, {{demo_line}}\n\n{{signature}}",
    3: "Hi {{first_name}},\n\nIs mental health support for your team in {{place}} on your list right now?",
    4: "Hi {{first_name}},\n\nI'll leave it here. If it would help to have something to share with the team later, "
       "reply \"one-pager\" and I'll send over our one-page summary of Spill.",
}


def copy_rows(version: str, angle: str, status: str = "approved") -> tuple[CopyRow, ...]:
    return tuple(
        CopyRow(version, angle, step, SUBJECTS[step], BODIES[step], status, "Harry Dryden" if status == "approved" else "")
        for step in (1, 2, 3, 4)
    )


COPY = copy_rows("general-v1", "General") + copy_rows("eap-v1", "Upgrade the EAP")
FIRST_TEST = ABTest("t1-eap-opener", "EAP opener beats General", "eap-v1", "general-v1", 400, "running")
BLACKOUTS = (DateRange(date(2026, 11, 23), date(2026, 11, 27)), DateRange(date(2026, 12, 18), date(2027, 1, 4)))


def make_settings(*, mailboxes=MAILBOXES, copy=COPY, tests=(), overrides=(), focus=(), named_accounts=(), **general) -> Settings:
    g = General(
        postal_address=POSTAL, privacy_url=PRIVACY, hubspot_owner_id="owner-harry",
        clay_monthly_credits=2000.0, clay_credits_per_account=5.0, blackout_dates=BLACKOUTS,
    )
    return Settings(
        general=dataclasses.replace(g, **general), angles=ANGLES, industries=INDUSTRIES, copy=tuple(copy),
        mailboxes=tuple(mailboxes), tests=tuple(tests), overrides=tuple(overrides), focus=tuple(focus),
        named_accounts=tuple(named_accounts),
    )


def account(**kw) -> dict:
    return {
        "account_id": "acc-1", "domain": "acmecreative.com", "clean_name": "Acme Creative", "hq_city": "Chicago",
        "hq_state": "IL", "industry": "Advertising agencies", "industry_group": "Marketing & Creative Agencies",
        "employees": 64, "size_band": "50-99", "tier": "Priority", "score": 60, "angle": "Upgrade the EAP",
        "status": "verified", **kw,
    }


def contact(**kw) -> dict:
    return {
        "contact_id": "con-1", "account_id": "acc-1", "first_name": "Jane", "last_name": "Doe", "role": "People leader",
        "title": "Head of People", "email": "jane@acmecreative.com", "email_status": "verified", "person_state": "IL",
        **kw,
    }


def values_for(mb=HANNAH, settings=None, acct=None, con=None, opener=EAP_OPENER, overlay="") -> dict[str, str]:
    s = settings or make_settings()
    return render.variables(acct or account(), con or contact(), mb, s, opener=opener, legal_overlay=overlay)


# -- variables --------------------------------------------------------------------------


def test_variables_follow_the_spec_table():
    v = values_for()
    assert v["first_name"] == "Jane" and v["company"] == "Acme Creative"
    assert v["opener"] == EAP_OPENER
    assert v["place"] == "Chicago, IL"
    assert v["proof"] == INDUSTRIES[0].proof_point  # the label has none, so the group's row
    assert v["price_line"] == "For a team your size it's $495 a month, flat."
    assert v["signature"] == "Hannah Spalding\nSpill\nspill.chat/us"
    assert v["legal_overlay"] == ""
    assert set(v) == set(render.VARIABLES)


def test_place_proof_and_signature_fallbacks():
    s = make_settings()
    assert render.place_for({"hq_state": "NY"}) == "NY"
    assert render.place_for({}) == ""
    assert render.proof_for({"industry": "Fintech", "industry_group": "Technology & Startups"}, s).startswith("UK example: Monzo")
    assert render.proof_for({"industry": "Unknown label", "industry_group": ""}, s) == ""
    bare = dataclasses.replace(HANNAH, signature="")
    assert render.signature_for(bare) == "Hannah Spalding\nSpill\nspill.chat/us"


@pytest.mark.parametrize(
    "employees, dollars",
    [(1, "$195"), (10, "$195"), (11, "$250"), (25, "$250"), (26, "$350"), (50, "$350"), (51, "$495"),
     (100, "$495"), (101, "$995"), (200, "$995"), ("64", "$495")],
)
def test_price_line_by_team_size(employees, dollars):
    assert render.price_line(employees) == f"For a team your size it's {dollars} a month, flat."


def test_price_line_over_200_and_unknown():
    assert render.price_line(201) == "For a team your size it's $5 per employee a month."
    assert render.price_line(None) == "" and render.price_line("n/a") == "" and render.price_line(0) == ""


def test_ask_by_role_and_host():
    assert render.ask_for("People leader", sender_is_host=True, host="Harry Dryden") == "Would a 20-minute walkthrough be useful?"
    hosted = render.ask_for("People leader", sender_is_host=False, host="Harry Dryden")
    assert "20-minute walkthrough" in hosted and "Harry Dryden" in hosted
    assert render.ask_for("Founder or executive", sender_is_host=False, host="Harry Dryden") == "Worth a look for the team?"
    assert "happy to send the one-pager" in render.ask_for("Operations", sender_is_host=True, host="Harry Dryden").lower()
    assert render.ask_for("Finance", sender_is_host=True, host="Harry Dryden") == ""


def test_demo_line_names_harry_for_other_senders():
    s = make_settings()
    hannah = render.demo_line_for(HANNAH, s)
    assert hannah == f"my colleague Harry Dryden runs our US demos; you can grab a time with him here: {BOOKING}"
    harry = render.demo_line_for(HARRY_M, s)
    assert harry == f"grab a time with me: {BOOKING}"
    assert render.is_demo_host(HARRY_T, s) and not render.is_demo_host(SAM, s)


def test_legal_overlay_only_for_legal_teams():
    legal = account(industry="Legal Teams", industry_group="Legal Teams")
    assert values_for(acct=legal, overlay=LEGAL_OVERLAY)["legal_overlay"] == LEGAL_OVERLAY
    assert values_for(overlay=LEGAL_OVERLAY)["legal_overlay"] == ""


def test_overrides_win_over_the_account_row():
    s = make_settings(overrides=(Override("acmecreative.com", "clean_name", "Acme"), Override("acmecreative.com", "employees", "12")))
    v = values_for(settings=s)
    assert v["company"] == "Acme" and "$250" in v["price_line"]


def test_pick_opener_falls_back_when_evidence_breaks_a_rule():
    opener, why = render.pick_opener("Saw your benefits page mentions unlimited PTO", EAP_OPENER)
    assert opener == EAP_OPENER and "unlimited" in why
    opener, why = render.pick_opener("Saw your benefits page mentions 100% employer-paid", EAP_OPENER)
    assert opener == EAP_OPENER and "100%" in why
    opener, why = render.pick_opener("Saw your benefits page mentions mental health days", EAP_OPENER)
    assert opener == "Saw your benefits page mentions mental health days" and why == ""
    assert render.pick_opener("", EAP_OPENER) == (EAP_OPENER, "")


# -- rendering ----------------------------------------------------------------------------


def test_clean_sequence_renders_without_violations():
    s = make_settings()
    for mb in MAILBOXES:
        for version in ("general-v1", "eap-v1"):
            seq = render.render_sequence(version, values_for(mb, s), mailbox=mb, settings=s)
            assert [r.step for r in seq] == [1, 2, 3, 4]
            assert all(r.ok for r in seq), render.violations(seq)
            cv = render.custom_variables(seq)
            assert list(cv) == ["s1_subject", "s1_body", "s2_subject", "s2_body", "s3_subject", "s3_body", "s4_subject", "s4_body"]
            assert cv["s1_subject"] == "Mental health support for the Acme Creative team"
            assert "{{" not in "".join(cv.values())


def test_footer_on_every_step():
    s = make_settings()
    for r in render.render_sequence("general-v1", values_for(HANNAH, s), mailbox=HANNAH, settings=s):
        lines = r.body.splitlines()
        assert lines[-4:] == ["Hannah Spalding, Spill", POSTAL, AD_LINE, STOP_LINE], r.step
        assert PRIVACY in r.body


def test_footer_role_shown_only_when_set():
    s = make_settings()
    with_role = dataclasses.replace(HANNAH, owner_role="Head of Partnerships")
    foot, missing = render.footer(with_role, s)
    assert foot.splitlines()[0] == "Hannah Spalding, Head of Partnerships, Spill" and not missing
    foot, _ = render.footer(HANNAH, s)
    assert foot.splitlines()[0] == "Hannah Spalding, Spill"


def test_step1_has_article14_notice_and_exactly_one_link():
    s = make_settings()
    seq = render.render_sequence("eap-v1", values_for(HANNAH, s), mailbox=HANNAH, settings=s)
    step1 = seq[0]
    assert step1.ok, step1.violations
    assert "Where we got your details" in step1.body and "Apollo" in step1.body and "Acme Creative" in step1.body
    assert "legitimate interests" in step1.body
    assert copy_rules.links(step1.body) == [PRIVACY]
    assert step1.body.index("Where we got your details") < step1.body.index(AD_LINE)
    # later steps have no notice, and step 2 may carry the booking link too
    assert all("Where we got your details" not in r.body for r in seq[1:])
    assert BOOKING in seq[1].body


def test_step1_with_a_second_link_is_blocked():
    s = make_settings()
    row = dataclasses.replace(COPY[0], body=COPY[0].body + "\n\nIf you'd like, {{demo_line}}")
    r = render.render_step(row, values_for(HARRY_M, s), step=1, mailbox=HARRY_M, settings=s)
    assert any("step 1 has 2 links" in v for v in r.violations)


def test_signature_link_counts_in_step1():
    s = make_settings()
    row = dataclasses.replace(COPY[0], body=COPY[0].body + "\n\n{{signature}}")
    r = render.render_step(row, values_for(HANNAH, s), step=1, mailbox=HANNAH, settings=s)
    assert any("step 1 has 2 links" in v for v in r.violations)


def test_blank_legal_overlay_leaves_no_gap_and_legal_accounts_get_it():
    s = make_settings()
    step1 = render.render_sequence("general-v1", values_for(HANNAH, s), mailbox=HANNAH, settings=s)[0]
    assert "\n\n\n" not in step1.body
    legal = account(industry="Legal Teams", industry_group="Legal Teams")
    step1 = render.render_sequence("general-v1", values_for(HANNAH, s, acct=legal, overlay=LEGAL_OVERLAY),
                                   mailbox=HANNAH, settings=s)[0]
    assert step1.ok and LEGAL_OVERLAY in step1.body


def test_unknown_and_empty_variables_stay_visible_and_block():
    s = make_settings()
    row = dataclasses.replace(COPY[2], body="Hi {{first_name}}, about {{nickname}} in {{place}}")
    v = {**values_for(HANNAH, s), "place": ""}
    r = render.render_step(row, v, step=3, mailbox=HANNAH, settings=s)
    assert "{{nickname}}" in r.body and "{{place}}" in r.body
    assert "body has the unknown variable {{nickname}}" in r.violations
    assert "body has the empty variable {{place}}" in r.violations


def test_empty_proof_blocks_step2():
    s = make_settings()
    acct = account(industry="Staffing agencies", industry_group="Professional Services")
    seq = render.render_sequence("general-v1", values_for(HANNAH, s, acct=acct), mailbox=HANNAH, settings=s)
    assert seq[0].ok and not seq[1].ok
    assert "body has the empty variable {{proof}}" in seq[1].violations


def test_blank_postal_address_or_privacy_url_blocks_every_step():
    s = make_settings(postal_address="", privacy_url="")
    seq = render.render_sequence("general-v1", values_for(HANNAH, s), mailbox=HANNAH, settings=s)
    for r in seq:
        assert any("postal_address is blank" in v for v in r.violations)
        assert any("privacy_url is blank" in v for v in r.violations)


def test_draft_copy_blocks():
    s = make_settings(copy=copy_rows("general-v1", "General", status="draft"))
    seq = render.render_sequence("general-v1", values_for(HANNAH, s), mailbox=HANNAH, settings=s)
    assert all(any("is draft, not approved" in v for v in r.violations) for r in seq)


def test_missing_step_is_a_violation():
    s = make_settings(copy=copy_rows("general-v1", "General")[:3])
    seq = render.render_sequence("general-v1", values_for(HANNAH, s), mailbox=HANNAH, settings=s)
    assert seq[3].violations == ("copy general-v1 has no step 4 row",)


def test_copy_rows_prefers_the_approved_row():
    draft = dataclasses.replace(COPY[0], status="draft", body="old")
    s = make_settings(copy=(draft,) + COPY)
    assert render.copy_rows(s, "general-v1")[1].status == "approved"


# -- every template against empty and maximum-length values (SPEC 10) -------------------------


def _every_template(s: Settings):
    for row in s.copy:
        for mb in s.mailboxes:
            yield row, mb


def test_every_template_against_empty_values():
    s = make_settings()
    for row, mb in _every_template(s):
        r = render.render_step(row, render.empty_variables(), step=row.step, mailbox=mb, settings=s)
        assert r.violations, (row.copy_version, row.step)
        assert any("empty variable" in v for v in r.violations)
        assert r.body.splitlines()[-2:] == [AD_LINE, STOP_LINE]  # the footer is still there


def test_every_template_against_maximum_length_values():
    s = make_settings()
    for row, mb in _every_template(s):
        values = render.max_length_variables(mb, s)
        r = render.render_step(row, values, step=row.step, mailbox=mb, settings=s)
        long_lines = [line for line in r.body.splitlines() if len(line) > copy_rules.MAX_LINE]
        flagged = [v for v in r.violations if "characters (the limit is 300)" in v]
        assert len(flagged) == len(long_lines), (row.copy_version, row.step, r.violations)
        if row.step in (1, 2):  # opener and proof run past 300 characters at their longest
            assert flagged
        assert not any("empty variable" in v for v in r.violations)


def test_max_rendered_lengths_probe():
    s = make_settings()
    lengths = render.max_rendered_lengths(s)
    assert set(lengths) == {f"s{i}_{p}" for i in range(1, 5) for p in ("subject", "body")}
    assert lengths["s1_body"] > 300 and lengths["s1_subject"] >= 100
    seq = render.render_sequence("eap-v1", render.max_length_variables(HANNAH, s), mailbox=HANNAH, settings=s)
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
