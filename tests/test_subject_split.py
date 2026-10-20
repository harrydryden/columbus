"""Email 1's personal subject, as a measured split (Harry, 5 Oct 2026; render.subject_arm).

The General email1_subject_share of accounts get General email1_subject ("support for the {{company}} team") as
email 1's subject, the rest the Copy row's s1_subject. The tests: the keys are validated as a subject; the split
is stable per account, about half, and independent of the opener holdout; only email 1's subject changes; enrol
sends the arm's subject and records contacts.subject_arm;
`copy preview` shows it. `signals review` (tests/test_signal_review.py), the daily post
(tests/test_daily_post_funnel.py) and `seed send --subject` (tests/test_seed.py) are tested with their own worlds.
"""

from __future__ import annotations

import dataclasses

import pytest

from tests.fakes import make_context
from tests.test_render import HANNAH, account, make_settings, values_for
from us_outbound.enrol import copy_desk, enrol, openers, render
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import General
from us_outbound.settings.validate import validate_all, validate_tab

PERSONAL, COPY = render.PERSONAL_SUBJECT, render.COPY_SUBJECT
NAMED = "{{first_name}}, support for the {{company}} team"  # a subject that shows both variables


def general(**values):
    rows = [dict(r) for r in default_tabs()["General"]]
    for key, value in values.items():
        next(r for r in rows if r["key"] == key)["value"] = value
    return validate_tab("General", rows)


def split(share=0.5, subject=General.email1_subject, **general_):
    return make_settings(email1_subject_share=share, email1_subject=subject, **general_)


# -- the General keys ---------------------------------------------------------------------------------------------


def test_the_general_keys_default_to_half_with_a_short_lower_case_subject():
    assert (General().email1_subject, General().email1_subject_share) == ("support for the {{company}} team", 0.5)
    rows = {r["key"]: r for r in default_tabs()["General"]}
    assert (rows["email1_subject"]["value"], rows["email1_subject_share"]["value"]) == (
        "support for the {{company}} team", "0.5")
    assert "Variables: {{company}}, {{first_name}}" in rows["email1_subject"]["note"]
    assert "contacts.subject_arm" in rows["email1_subject_share"]["note"]
    g, errs = general(email1_subject=NAMED, email1_subject_share="0.25")
    assert errs == [] and (g.email1_subject, g.email1_subject_share) == (NAMED, 0.25)


@pytest.mark.parametrize("subject, fragment", [
    ("a note for {{nickname}}", "may use only {{company}} and {{first_name}}, not {{nickname}}"),
    ("support for the {company} team", "variables take double braces: write {{company}}"),
    ("[support](https://www.spill.chat/us) for {{company}}", "has markup; a subject is plain text"),
    ("**support** for the {{company}} team", "has markup"),
    ("<b>support</b> for the {{company}} team", "has markup"),
    ("therapy for the {{company}} team", 'says "therapy": use counselor or counseling'),
    ("support for the {{company}} team!", "exclamation mark"),
    ("Re: support for the {{company}} team", 'starts with "Re:"'),
    ("support from $195 for {{company}}", 'has the price "$1'),
    ("a free trial for the {{company}} team", 'says "free trial", which reads as spam'),
    ("a quick call with {{first_name}}", "email 1 asks only for a visit to the site"),
    ("organisational support for {{company}}", 'British spelling "organisational"'),
    ("support for every single person on the {{company}} team, wherever they are", "the limit is 60"),
])
def test_email1_subject_is_checked_as_a_subject(subject, fragment):
    _, errs = general(email1_subject=subject)
    assert len(errs) == 1 and errs[0].column == "value" and errs[0].label == "email1_subject", errs
    assert fragment in errs[0].message, errs[0].message


def test_a_blank_subject_is_refused_while_the_share_is_above_0_and_the_share_is_a_share():
    _, errs = general(email1_subject="")
    assert [e.message for e in errs] == [
        "cannot be blank while email1_subject_share is above 0 (0.5); write a subject, or set the share to 0"]
    g, errs = general(email1_subject="", email1_subject_share="0")
    assert errs == [] and g.email1_subject == ""
    _, errs = general(email1_subject_share="1.5")
    assert "is a share" in errs[0].message and errs[0].label == "email1_subject_share"
    tabs = default_tabs()  # the whole sheet: a General error refuses it, as for any key
    next(r for r in tabs["General"] if r["key"] == "email1_subject")["value"] = "<i>hello</i> {{company}}"
    settings, errors = validate_all(tabs)
    assert settings is None and errors["General"]


# -- the split ----------------------------------------------------------------------------------------------------


def test_the_split_is_stable_about_half_and_independent_of_the_opener_holdout():
    s = split()
    ids = [f"acc-{i}" for i in range(4000)]
    arms = [render.subject_arm(a, s) for a in ids]
    assert arms == [render.subject_arm(a, s) for a in ids]  # the same every run
    assert set(arms) == {PERSONAL, COPY} and 0.47 < arms.count(PERSONAL) / len(ids) < 0.53
    # Its own salt: within the opener holdout and outside it, still about half are personal.
    held = [arm for a, arm in zip(ids, arms) if openers.in_holdout(a, 0.3)]
    kept = [arm for a, arm in zip(ids, arms) if not openers.in_holdout(a, 0.3)]
    assert 0.45 < held.count(PERSONAL) / len(held) < 0.55 and 0.45 < kept.count(PERSONAL) / len(kept) < 0.55
    assert render.SUBJECT_SALT != openers.HOLDOUT_SALT
    assert {render.subject_arm(a, split(share=0.0)) for a in ids[:500]} == {COPY}
    assert {render.subject_arm(a, split(share=1.0)) for a in ids[:500]} == {PERSONAL}
    assert {render.subject_arm(a, split(share=1.0, subject="")) for a in ids[:500]} == {COPY}  # nothing to send


def test_only_email_1_s_subject_changes():
    s = split(share=1.0, subject=NAMED)
    row = s.copy[0]
    values = values_for(settings=s, row=row)
    personal = render.render_sequence(row, values, mailbox=HANNAH, settings=s, subject_arm=PERSONAL)
    copy_ = render.render_sequence(row, values, mailbox=HANNAH, settings=s, subject_arm=COPY)
    assert render.violations(personal) == [] and render.violations(copy_) == []
    assert (personal[0].subject, copy_[0].subject) == ("Jane, support for the Acme Creative team",
                                                       "Support for the Acme Creative team")
    assert [r.subject for r in personal[1:]] == [r.subject for r in copy_[1:]]
    assert [r.body for r in personal] == [r.body for r in copy_]
    cv = render.custom_variables(personal)
    assert cv["s1_subject"] == "Jane, support for the Acme Creative team"
    assert cv["s2_subject"] == render.custom_variables(copy_)["s2_subject"] == "How Spill works for agencies"


def test_the_personal_subject_goes_through_the_render_time_rules_too():
    s = split(share=1.0, subject="therapy for the {{company}} team")  # past the sheet's check, say by hand
    out = render.render_sequence(s.copy[0], values_for(settings=s), mailbox=HANNAH, settings=s, subject_arm=PERSONAL)
    assert 'email 1: subject says "therapy": use counselor or counseling' in render.violations(out)


# -- enrol: what is sent, and the arm on the contact ----------------------------------------------------------------


def test_enrol_sends_each_account_its_arm_s_subject_and_records_the_arm():
    from tests.test_enrol import instantly_posts, make

    s = split(subject=NAMED, live_sending=True)
    want = {a: render.subject_arm(a, s) for a in ("acc-1", "acc-2", "acc-3")}
    assert want == {"acc-1": PERSONAL, "acc-2": PERSONAL, "acc-3": COPY}  # by the hash of each account id
    ctx, t = make(live=True, settings=s)
    out = enrol.run(ctx)
    assert out["enrolled"] == 3 and out["subjects"] == {"arms": {PERSONAL: 2, COPY: 1}}
    sent = {lead["email"]: lead["custom_variables"] for p in instantly_posts(t) for lead in p.json["leads"]}
    assert sent["jane@acmecreative.com"]["s1_subject"] == "Jane, support for the Acme Creative team"
    assert sent["omar@brightfin.com"]["s1_subject"] == "Omar, support for the Brightfin team"
    assert sent["lee@loopstudio.com"]["s1_subject"] == "Support for the Loop Studio team"  # the Copy row's
    assert {cv["s2_subject"] for cv in sent.values()} == {"How Spill works for agencies"}
    got = {c["contact_id"]: c["subject_arm"] for c in ctx.store.select("contacts")}
    assert got == {"con-1": PERSONAL, "con-2": PERSONAL, "con-3": COPY}


@pytest.mark.parametrize("live_split, held, subjects", [
    # both (the default, today's draws): the holdout (share 1.0: every account) and the subject split, as above.
    ("both", {"holdout"}, {PERSONAL: 2, COPY: 1}),
    ("opener", {"holdout"}, {render.NO_SUBJECT_ARM: 3}),  # the holdout alone; no subject arm, the Copy row's subject
    ("subject", {"opener", "none"}, {PERSONAL: 2, COPY: 1}),  # the subject split alone; every opener
    ("test", {"opener", "none"}, {render.NO_SUBJECT_ARM: 3}),  # neither: the Tests tab's test alone
    ("none", {"opener", "none"}, {render.NO_SUBJECT_ARM: 3}),  # no split at all
])
def test_live_split_decides_which_split_draws_arms(live_split, held, subjects):
    """W3-4 (21 Oct 2026; design-final §5 L10, D8): General live_split names the one split that draws arms. With the
    opener holdout at a share of 1.0, a drawn holdout holds every account out; the subject split's arms by hash are
    acc-1 and acc-2 personal, acc-3 copy (the test above)."""
    from tests.test_enrol import instantly_posts, make

    s = split(subject=NAMED, live_sending=True, opener_holdout_share=1.0, live_split=live_split)
    ctx, t = make(live=True, settings=s)
    out = enrol.run(ctx)
    assert out["enrolled"] == 3 and out["subjects"] == {"arms": subjects}
    contacts = ctx.store.select("contacts")
    arms = {c["opener_arm"] for c in contacts}
    assert arms == {"holdout"} if "holdout" in held else arms <= held
    sent = {lead["email"]: lead["custom_variables"] for p in instantly_posts(t) for lead in p.json["leads"]}
    if render.NO_SUBJECT_ARM in subjects:  # every email 1 has the Copy row's own subject
        assert sent["jane@acmecreative.com"]["s1_subject"] == "Support for the Acme Creative team"
        assert {c["subject_arm"] for c in contacts} == {render.NO_SUBJECT_ARM}


# -- copy preview ----------------------------------------------------------------------------------------------------


def test_copy_preview_shows_the_arm_s_subject(capsys):
    from us_outbound.ops import cli

    settings, _ = validate_all(default_tabs())
    ctx = make_context(settings)
    argv = ["copy", "preview", "--synced", "--version", "general-founder-v1"]
    assert cli.main([*argv, "--subject", "personal"], context_factory=lambda *a, **k: ctx) == 0
    out = capsys.readouterr().out
    assert "Email 1 subject: the personal subject (General email1_subject)" in out
    assert "--- Email 1 (day 0) ---\nSubject: support for the Harbor & Finch team\n" in out
    assert cli.main(argv, context_factory=lambda *a, **k: ctx) == 0  # the sample prospect: the Copy row's
    out = capsys.readouterr().out
    assert "Email 1 subject:" not in out and "Subject: support for the Harbor & Finch team" not in out
    # A stored account: its own arm, as enrol gives it.
    row = settings.copy_row("general-founder-v1")
    acme = account()
    assert render.subject_arm(acme["account_id"], settings) == PERSONAL
    p = copy_desk.preview(row, settings, account=acme, contact={"first_name": "Jane"})
    assert (p.subject_arm, p.emails[0].subject) == (PERSONAL, "support for the Acme Creative team")
    blank = dataclasses.replace(settings, general=dataclasses.replace(settings.general, email1_subject=""))
    with pytest.raises(ValueError, match="email1_subject is blank"):
        copy_desk.preview(row, blank, subject_arm=PERSONAL)
    assert cli.main([*argv, "--subject", "personal"], context_factory=lambda *a, **k: make_context(blank)) != 0
