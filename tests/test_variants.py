"""Copy variants (enrol/variants.py; Harry, 7 Oct 2026: "an A/B where in the A the opening line of the first email in
the sequence is a warm intro 'I hope you're really well. Great to be connected.' and the B version doesn't have
that"): the Tests tab's variant kind and its checks, `test start` for it, the arms enrol assigns and renders, the
card, and the reads."""

from __future__ import annotations

import copy
import dataclasses
import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_cli import Harness, _approved, _tests_tab
from tests.test_enrol import default_openers, instantly_posts, make  # noqa: F401  (default_openers: autouse)
from tests.test_looks import T0, emailed
from tests.test_looks import world as looks_world
from tests.test_registry import SETTINGS
from tests.test_send_approvals import HARRY_ID, _rejected, at, blocks_text, item_for, items, poll, proposed
from tests.test_render import (
    BODIES,
    EAP_OPENER,
    FIRST_TEST,
    GENERAL_OPENER,
    HANNAH,
    SUBJECTS,
    account,
    contact,
    copy_row,
    make_settings,
    values_for,
)
from us_outbound import config_version
from us_outbound.enrol import approvals, enrol, openers, queue, render, variants
from us_outbound.learn import cohorts
from us_outbound.learn import looks
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.model import CopyStep
from us_outbound.settings.model import Test as CopyTest  # aliased so pytest does not collect it
from us_outbound.settings.validate import validate_all, validate_tab

WARM = "I hope you're really well. Great to be connected."
WARM_TEST = CopyTest("warm-intro", "A warm intro gets more replies", "warm intro", "no intro", 400, "running",
                     start_date=date(2026, 10, 12), read_date=date(2027, 3, 29), decision_rule="reply rate",
                     kind="variant", email=1, change="first_line", text_a=WARM)


def variant_row(**kw) -> dict[str, str]:
    """Harry's first test as a Tests-tab row (docs/daily.md, "Copy tests"), every column a string."""
    row = {c: "" for c in COLUMNS["Tests"]}
    row.update(test_id="warm-intro", kind="variant", hypothesis="A warm intro gets more replies",
               version_a="warm intro", version_b="no intro", accounts_per_version="400", status="planned",
               read_date="2027-03-29", decision_rule="reply rate", email="1", change="first_line", text_a=WARM)
    row.update(kw)
    return row


@pytest.fixture
def tabs():
    t = copy.deepcopy(default_tabs())
    for c in t["Copy"]:
        c.update(status="approved", approved_by="Harry")
    return t


def errors_for(row: dict[str, str]) -> list[tuple[str, str]]:
    _, errors = validate_tab("Tests", [row])
    return [(e.column, e.message) for e in errors]


# -- the Tests tab -----------------------------------------------------------------------------------------------------


def test_the_warm_intro_row_validates(tabs):
    tabs["Tests"] = [variant_row(email="", status="running", start_date="2026-10-12", looks="200; 400")]
    settings, errors = validate_all(tabs)
    assert not any(errors.values())
    [t] = settings.tests
    assert (t.kind, t.email, t.change, t.text_a, t.text_b, t.find) == ("variant", 1, "first_line", WARM, "", "")
    assert (t.arm_name("a"), t.arm_name("b"), t.text("a"), t.text("b")) == ("warm intro", "no intro", WARM, "")
    assert settings.running_test() is t  # the copy test enrol assigns arms for


def test_the_live_tab_without_the_new_columns_still_syncs(tabs):
    """The live Tests tab (7 Oct 2026): test_id, hypothesis, version_a, version_b, accounts_per_version, status,
    start_date, read_date, decision_rule, result, kind, looks, and no rows."""
    live = ["test_id", "hypothesis", "version_a", "version_b", "accounts_per_version", "status", "start_date",
            "read_date", "decision_rule", "result", "kind", "looks"]
    tabs["Tests"] = []
    settings, errors = validate_all(tabs)
    assert not any(errors.values()) and settings.tests == ()
    tabs["Tests"] = [{c: _tests_tab(version_a="eap-v1").get(c, "") for c in live}]
    settings, errors = validate_all({**tabs, "Tests": [dict(tabs["Tests"][0], version_a="legal-teams-people-v1",
                                                            version_b="legal-teams-founder-v1")]})
    assert not any(errors.values()) and settings.tests[0].kind == "ab" and settings.tests[0].change == ""


@pytest.mark.parametrize("change,text,message", [
    ("first_line", "Book a demo with us!", "exclamation mark"),
    ("first_line", "Book a demo with us.", "email 1 asks only for a visit to the site"),
    ("first_line", "Our therapy is quick.", "use counselor or counseling"),
    ("first_line", "We organise support quickly.", 'British spelling "organise"'),
    ("first_line", "See [our page]({{site_url}}).", "a line of its own carries none"),
    ("first_line", "One line.\nAnd another.", "more than one line"),
    ("last_line", "It costs $5 a head.", "the price comes only from {{price_line}}"),
    ("last_line", "Act now, while it lasts.", "reads as spam"),
    ("first_line", "Hi {{nickname}}.", "unknown variable {{nickname}}"),
    ("first_line", "{{opener}}", "a line of its own the Copy row places"),
    ("first_line", "Hello {company}.", "variables take double braces"),
    ("subject", "A note for the whole {{company}} team about support that people actually use", "characters"),
    ("subject", "See **this**", "a subject is plain text"),
])
def test_a_text_that_breaks_a_copy_rule_is_refused(change, text, message):
    found = errors_for(variant_row(change=change, text_a=text))
    assert any(col == "text_a" and message in m for col, m in found), found


def test_the_rules_are_the_email_s_own():
    assert errors_for(variant_row(email="2", change="subject", text_a="A short demo, when it suits")) == []
    assert any("email 1 asks only" in m for _, m in errors_for(variant_row(change="subject",
                                                                           text_a="A short demo, when it suits")))
    assert errors_for(variant_row(email="4", change="last_line", text_a="A free trial is there too.")) == []
    assert any("spam" in m for _, m in errors_for(variant_row(email="3", change="last_line",
                                                              text_a="A free trial is there too.")))
    assert errors_for(variant_row(text_a="Hi again, {{first_name}}: we work with teams around {{place}}.")) == []


def test_both_blank_or_the_same_compares_nothing():
    [(col, message)] = errors_for(variant_row(text_a=""))
    assert col == "text_a" and "both blank" in message
    [(col, message)] = errors_for(variant_row(text_b=WARM))
    assert col == "text_b" and "must differ" in message
    assert errors_for(variant_row(text_a="", text_b=WARM)) == []  # either arm may be the Copy row as it is


def test_replace_needs_find_and_only_replace_takes_it():
    [(col, message)] = errors_for(variant_row(change="replace", text_a="Spill gives every team quick support."))
    assert col == "find" and "required for change replace" in message
    assert errors_for(variant_row(change="replace", find="Spill is that place.",
                                  text_a="Spill can be that place.")) == []
    [(col, message)] = errors_for(variant_row(find="Spill is that place."))
    assert col == "find" and "only by change replace" in message


def test_the_email_and_change_columns():
    assert any(col == "email" for col, _ in errors_for(variant_row(email="5")))
    assert any(col == "change" for col, _ in errors_for(variant_row(change="middle_line")))
    assert any(col == "change" and "required" in m for col, m in errors_for(variant_row(change="")))
    found = errors_for(variant_row(kind="ab", version_a="eap-v1", version_b="general-v1"))
    assert ("text_a", "is for a variant test (kind variant); a ab test leaves it blank") in found


def test_a_variant_may_not_run_beside_the_ab_test_but_may_beside_a_holdout(tabs):
    ab = dict(tabs["Tests"][0], status="running", start_date="2026-10-05", read_date="2026-12-14",
              version_a="legal-teams-people-v1", version_b="legal-teams-founder-v1")
    variant = variant_row(status="running", start_date="2026-10-12")
    _, errors = validate_all({**tabs, "Tests": [ab, variant]})
    [e] = errors["Tests"]
    assert e.label == "warm-intro" and "only one copy test (ab or variant) runs at a time" in e.message
    holdout = dict(ab, test_id="t2", kind="holdout", version_a="opener", version_b="holdout")
    settings, errors = validate_all({**tabs, "Tests": [holdout, variant]})
    assert not any(errors.values()) and settings.running_test().test_id == "warm-intro"


# -- test start --------------------------------------------------------------------------------------------------------


def test_test_start_starts_a_variant(capsys):
    h = Harness(SETTINGS, sheet_tabs={"Tests": [variant_row()]})
    assert h.run("test", "start", "warm-intro", "--live") == 0
    row = h.sheet_tabs["Tests"][0]
    assert row["status"] == "running" and row["start_date"] == "2026-10-27"


def test_test_start_refuses_a_variant_whose_text_breaks_a_rule(capsys):
    h = Harness(SETTINGS, sheet_tabs={"Tests": [variant_row(text_a="Book a demo with us!")]})
    assert h.run("test", "start", "warm-intro", "--live") == 2
    err = capsys.readouterr().err
    assert "does not pass the checks settings_sync makes" in err and "exclamation mark" in err
    assert h.sheet_tabs["Tests"][0]["status"] == "planned"


def test_test_start_refuses_a_variant_beside_the_running_ab_test_and_not_beside_a_holdout(capsys):
    running = _tests_tab(test_id="t1", status="running", start_date="2026-10-20")
    h = Harness(dataclasses.replace(SETTINGS, copy=_approved()), sheet_tabs={"Tests": [running, variant_row()]})
    assert h.run("test", "start", "warm-intro", "--live") == 2
    assert "only one copy test (ab or variant) runs at a time (SPEC 9); t1 is running" in capsys.readouterr().err
    holdout = _tests_tab(test_id="t2", kind="holdout", version_a="opener", version_b="holdout", status="running",
                         start_date="2026-10-20")
    h2 = Harness(SETTINGS, sheet_tabs={"Tests": [holdout, variant_row()]})
    assert h2.run("test", "start", "warm-intro", "--live") == 0
    # And an ab test is refused beside the running variant.
    h3 = Harness(dataclasses.replace(SETTINGS, copy=_approved()),
                 sheet_tabs={"Tests": [variant_row(status="running", start_date="2026-10-20"), _tests_tab()]})
    assert h3.run("test", "start", "t1", "--live") == 2


def test_test_start_says_which_copy_rows_the_change_fits_and_refuses_one_it_fits_none(capsys):
    h = Harness(dataclasses.replace(SETTINGS, copy=_approved()), sheet_tabs={"Tests": [variant_row()]})
    assert h.run("test", "start", "warm-intro") == 0
    out = capsys.readouterr().out
    result, _ = json.JSONDecoder().raw_decode(out[out.index('{\n  "dry_run"'):])
    assert result["copy_rows"] == {"change_fits": 2, "sendable": 2, "not_in_the_test": {}}
    absent = variant_row(change="replace", find="Words no email has.", text_a="Other words.")
    h2 = Harness(dataclasses.replace(SETTINGS, copy=_approved()), sheet_tabs={"Tests": [absent]})
    assert h2.run("test", "start", "warm-intro", "--live") == 2
    assert "cannot be made in any sendable Copy row" in capsys.readouterr().err


# -- the arms ----------------------------------------------------------------------------------------------------------


def test_the_arm_is_the_account_s_own_hash_about_half_and_half():
    ids = [f"acct-{i}" for i in range(4000)]
    arms = [variants.arm_for(WARM_TEST, i) for i in ids]
    assert arms == [variants.arm_for(WARM_TEST, i) for i in ids]  # deterministic
    assert arms == [queue.test_version(i, "warm-intro") for i in ids]  # SPEC 9's hash, as an ab test splits
    assert 0.47 < arms.count("a") / len(ids) < 0.53
    other = dataclasses.replace(WARM_TEST, test_id="another-test")
    assert sum(variants.arm_for(other, i) != a for i, a in zip(ids, arms)) > 1800  # each test splits afresh


def test_the_arm_is_independent_of_the_opener_holdout_and_the_subject_split():
    """Each split hashes the account id with its own salt, so the designs are factorial: within each opener arm and
    each subject arm, about half the accounts are in each variant arm."""
    s = make_settings(email1_subject_share=0.5)
    ids = [f"acct-{i}" for i in range(4000)]
    by_holdout: dict[bool, Counter] = {True: Counter(), False: Counter()}
    by_subject: dict[str, Counter] = {render.PERSONAL_SUBJECT: Counter(), render.COPY_SUBJECT: Counter()}
    for i in ids:
        arm = variants.arm_for(WARM_TEST, i)
        by_holdout[openers.in_holdout(i, 0.3)][arm] += 1
        by_subject[render.subject_arm(i, s)][arm] += 1
    for counts in (*by_holdout.values(), *by_subject.values()):
        assert 0.45 < counts["a"] / sum(counts.values()) < 0.55, counts


# -- the change, rendered ----------------------------------------------------------------------------------------------


def rendered(test, *, arm="a", settings=None, row=None, **kw):
    s = settings or make_settings()
    row = row or s.copy[0]
    st = variants.apply(row.step(test.email), test.change, test.text(arm), test.find)
    return render.render_sequence(row, values_for(settings=s, row=row, **kw), mailbox=HANNAH, settings=s,
                                  written={test.email: st})


def test_first_line_lands_after_the_greeting_and_before_the_opener_in_html_and_text():
    one = rendered(WARM_TEST)[0]
    assert one.ok, one.violations
    assert one.html.startswith(f"<p>Hi Jane,</p><p>{WARM}</p><p>{EAP_OPENER}</p><p>In most agencies")
    assert one.text.startswith(f"Hi Jane,\n\n{WARM}\n\n{EAP_OPENER}\n\nIn most agencies")
    held_out = rendered(WARM_TEST, opener="")[0]  # the opener holdout: the line goes, the intro stays
    assert held_out.text.startswith(f"Hi Jane,\n\n{WARM}\n\nIn most agencies")
    assert rendered(WARM_TEST, arm="b")[0].text.startswith(f"Hi Jane,\n\n{EAP_OPENER}\n\n")  # no intro


def test_last_line_lands_just_before_the_sign_off():
    last = dataclasses.replace(WARM_TEST, email=3, change="last_line", text_a="Either way, I hope the week goes well.")
    three = rendered(last)[2]
    assert three.ok, three.violations
    assert "</p><p>Either way, I hope the week goes well.</p><p>Best wishes,<br>Hannah</p>" in three.html
    assert "\n\nEither way, I hope the week goes well.\n\nBest wishes,\nHannah" in three.text


def test_replace_changes_the_exact_text_and_subject_changes_the_subject():
    swap = dataclasses.replace(WARM_TEST, change="replace", find="the strain stays hidden until someone good leaves",
                               text_a="the strain shows up late, as sick days or a resignation")
    one = rendered(swap)[0]
    assert one.ok and "the strain shows up late, as sick days or a resignation." in one.text
    assert "stays hidden" not in one.html and "stays hidden" in rendered(swap, arm="b")[0].html
    subject = dataclasses.replace(WARM_TEST, email=2, change="subject", text_a="Spill for {{company}}, in brief")
    two = rendered(subject)[1]
    assert two.ok and two.subject == "Spill for Acme Creative, in brief"
    assert rendered(subject, arm="b")[1].subject == "How Spill works for agencies"


def test_apply_says_when_the_change_cannot_be_made():
    st = CopyStep(SUBJECTS[1], BODIES[1])
    assert variants.apply(st, "replace", "x", "no such words") is None
    assert variants.apply(st, "first_line", "", "") is st  # a blank text: the Copy row's email
    assert variants.apply(CopyStep("s", "Hello,\n\nBody.\n\nBest wishes,\n{{sender_first_name}}"), "first_line",
                          WARM) is None
    assert variants.apply(CopyStep("s", "Hi {{first_name}},\n\nBody.\n\nThanks"), "last_line", WARM) is None


# -- enrol -------------------------------------------------------------------------------------------------------------


def agencies(n: int, **kw) -> tuple[list[dict], list[dict]]:
    """n agency accounts, every fourth in Control, each with one contact."""
    accts = [account(account_id=f"ag-{i}", domain=f"ag{i}.com", clean_name=f"Agency {i}",
                     **({"tier": "Control", "score": 5, "angle": "General"} if i % 4 == 0 else {}), **kw)
             for i in range(n)]
    cons = [contact(contact_id=f"c-{i}", account_id=f"ag-{i}", email=f"p{i}@ag{i}.com") for i in range(n)]
    return accts, cons


def test_every_account_gets_its_arm_control_included_and_email_1_carries_it():
    accts, cons = agencies(12)
    s = make_settings(live_sending=True, tests=(WARM_TEST,))
    ctx, t = make(live=True, settings=s, accounts=accts, contacts=cons)
    out = enrol.run(ctx)
    leads = {lead["email"]: lead for r in instantly_posts(t) for lead in r.json["leads"]}
    seen = Counter()
    for i in range(12):
        con = ctx.store.get("contacts", contact_id=f"c-{i}")
        arm = variants.arm_for(WARM_TEST, f"ag-{i}")
        assert (con["test_id"], con["test_arm"], con["copy_version"]) == ("warm-intro", arm, "agencies-v1")
        opener = GENERAL_OPENER if i % 4 == 0 else EAP_OPENER  # Control accounts are in the test too
        body = leads[f"p{i}@ag{i}.com"]["custom_variables"]["s1_body"]
        assert body.startswith(f"<p>Hi Jane,</p><p>{WARM}</p><p>{opener}</p>" if arm == "a"
                               else f"<p>Hi Jane,</p><p>{opener}</p>")
        assert con["copy_hash"] == s.copy[0].content_hash()  # the Copy row's wording, as before
        seen[arm] += 1
    assert set(seen) == {"a", "b"}
    assert out["copy_test"] == {"test_id": "warm-intro", "kind": "variant",
                                "arms": {"warm intro": seen["a"], "no intro": seen["b"]}, "not_in_test": {}}


def test_an_ab_test_records_its_arm_too():
    accts = [account(account_id=f"ag-{i}", domain=f"ag{i}.com", clean_name=f"Agency {i}") for i in range(6)]
    cons = [contact(contact_id=f"c-{i}", account_id=f"ag-{i}", email=f"p{i}@ag{i}.com") for i in range(6)]
    rows = make_settings().copy + (copy_row("agencies-v2", "Marketing & Creative Agencies"),)
    ctx, _ = make(live=True, settings=make_settings(live_sending=True, tests=(FIRST_TEST,), copy=rows),
                  accounts=accts, contacts=cons)
    enrol.run(ctx)
    for i in range(6):
        con = ctx.store.get("contacts", contact_id=f"c-{i}")
        arm = queue.test_version(f"ag-{i}", FIRST_TEST.test_id)
        assert con["test_arm"] == arm and con["copy_version"] == FIRST_TEST.arm_name(arm)


def test_an_account_whose_copy_row_lacks_find_is_not_in_the_test_and_its_email_is_unchanged():
    swap = dataclasses.replace(WARM_TEST, change="replace", find="the strain stays hidden",
                               text_a="the strain shows up late")
    general = copy_row("general-v1", "General", bodies={**BODIES, 1: BODIES[1].replace("stays hidden", "hides")})
    s = make_settings(live_sending=True, tests=(swap,), copy=(make_settings().copy[0], general))
    ctx, t = make(live=True, settings=s)
    out = enrol.run(ctx)
    brightfin = ctx.store.get("contacts", contact_id="con-2")  # Fintech: the General row
    assert brightfin["copy_version"] == "general-v1" and brightfin["test_id"] is None and brightfin["test_arm"] is None
    assert ctx.store.get("contacts", contact_id="con-1")["test_id"] == "warm-intro"
    why = "its email 1 (general-v1) does not have the text the test replaces"
    assert out["copy_test"]["not_in_test"] == {why: 1}
    [lead] = [x for r in instantly_posts(t) for x in r.json["leads"] if x["email"] == "omar@brightfin.com"]
    assert "the strain hides" in lead["custom_variables"]["s1_body"]


def test_a_change_that_breaks_a_rule_for_one_contact_leaves_that_account_out_under_both_arms():
    """A last line that brings email 1 to 118 words with a two-word company name: a five-word name takes it over
    the 120-word limit, so that account is not in the test, whichever arm it hashes to (and it is still sent)."""
    words = "people here can talk to someone the same day".split()
    filler = " ".join((words * 6)[:44])
    long_line = dataclasses.replace(WARM_TEST, change="last_line", text_a=f"At {{{{company}}}}, {filler}.")
    accts = [account(account_id=f"ag-{i}", domain=f"ag{i}.com",
                     clean_name="Agency Two" if i % 2 else "North Shore Creative Group Partners") for i in range(8)]
    cons = [contact(contact_id=f"c-{i}", account_id=f"ag-{i}", email=f"p{i}@ag{i}.com") for i in range(8)]
    s = make_settings(live_sending=True, tests=(long_line,))
    ctx, _ = make(live=True, settings=s, accounts=accts, contacts=cons)
    out = enrol.run(ctx)
    for i in range(8):
        con = ctx.store.get("contacts", contact_id=f"c-{i}")
        assert con["instantly_lead_id"]  # sent either way
        assert con["test_id"] == ("warm-intro" if i % 2 else None)
    [(why, n)] = out["copy_test"]["not_in_test"].items()
    assert n == 4 and why.startswith("warm intro would break a copy rule in its email 1: email 1 has 121 words")


def test_a_subject_test_on_email_1_leaves_out_the_personal_subject_arm():
    subject = dataclasses.replace(WARM_TEST, change="subject", text_a="A note for the {{company}} team")
    s = make_settings(tests=(subject,), email1_subject_share=1.0)  # every account has the personal subject
    ctx, _ = make(settings=s)
    cand = enrol.Candidate(account(), contact())
    p = enrol.prepare(ctx, cand, Counter({"Hannah Spalding": 5}), Counter(), enrol.sendable_copy(s))
    assert p.subject_arm == "personal" and p.test_id == "" and p.test_arm == ""
    assert "personal subject" in p.test_note


def test_before_its_start_date_and_once_an_arm_is_full_no_account_is_added():
    later = dataclasses.replace(WARM_TEST, start_date=date(2026, 11, 2))
    s = make_settings(tests=(later,))
    ctx, _ = make(settings=s)
    free, rows = Counter({"Hannah Spalding": 5}), enrol.sendable_copy(s)
    p = enrol.prepare(ctx, enrol.Candidate(account(), contact()), free, Counter(), rows)
    assert (p.test_id, p.test_arm, p.test_note, p.written) == ("", "", "", {})
    s = make_settings(tests=(dataclasses.replace(WARM_TEST, accounts_per_version=3),))
    ctx, _ = make(settings=s)
    arm = variants.arm_for(WARM_TEST, "acc-1")
    p = enrol.prepare(ctx, enrol.Candidate(account(), contact()), free, Counter({arm: 3}), rows)
    assert p.test_id == "" and p.test_note == f"{WARM_TEST.arm_name(arm)} has its 3 accounts"


def test_a_second_contact_gets_its_account_s_arm_and_is_not_a_new_account():
    s = make_settings(tests=(WARM_TEST,), copy=make_settings().copy + (copy_row("agencies-ops-v1",
                      "Marketing & Creative Agencies", role="Operations"),))
    ctx, _ = make(settings=s)
    free, rows = Counter({"Hannah Spalding": 5}), enrol.sendable_copy(s)
    arm = variants.arm_for(WARM_TEST, "acc-1")
    ops = contact(contact_id="con-9", role="Operations", first_name="Lee")
    first = {"contact_id": "con-1", "copy_version": "agencies-v1", "test_id": "warm-intro"}
    p = enrol.prepare(ctx, enrol.Candidate(account(sender="Hannah Spalding"), ops, 2, first), free,
                      Counter({"a": 400, "b": 400}), rows)  # the arms are full: a second contact still follows
    assert (p.test_id, p.test_arm, p.copy_version) == ("warm-intro", arm, "agencies-ops-v1")
    assert (WARM in p.rendered[0].text) == (arm == "a")
    before = dict(first, test_id="")  # the first contact was enrolled before the test: the account is not in it
    p = enrol.prepare(ctx, enrol.Candidate(account(sender="Hannah Spalding"), ops, 2, before), free, Counter(), rows)
    assert (p.test_id, p.test_arm, p.test_note) == ("", "", "") and WARM not in p.rendered[0].text


# -- the card ----------------------------------------------------------------------------------------------------------
# For "warm-intro", acc-1 and acc-2 hash to "b" (no intro) and acc-3, the Control account, to "a" (warm intro).


def card_for(ctx, sl, account_id: str) -> str:
    row = item_for(ctx, account_id)
    [post] = [x for x in sl.posts if x["ts"] == row["slack_ts"]]
    return blocks_text(post)


def test_the_card_names_the_test_and_the_arm_and_shows_the_arm_s_email():
    ctx, t, sl, _ = proposed(tests=(WARM_TEST,))
    assert [variants.arm_for(WARM_TEST, a) for a in ("acc-1", "acc-2", "acc-3")] == ["b", "b", "a"]
    loop = item_for(ctx, "acc-3")["payload"]
    assert (loop["test_id"], loop["test_arm"], loop["test_name"], loop["test_note"]) == ("warm-intro", "a",
                                                                                        "warm intro", "")
    text = card_for(ctx, sl, "acc-3")
    facts = text.split("\n")[1]  # the context line under the head
    assert facts.startswith("Control · score 5 · General · ")
    assert facts.endswith(" · Test: warm-intro · warm intro")
    assert f"> Hi Lee,\n> \n> {WARM}\n> \n> {GENERAL_OPENER}" in text
    assert loop["steps"][0]["source"].startswith(f"Hi Lee,\n\n{WARM}\n\n{GENERAL_OPENER}")  # an edit starts here
    assert WARM in loop["lead"]["custom_variables"]["s1_body"]
    acme = item_for(ctx, "acc-1")["payload"]
    assert "Test: warm-intro · no intro" in card_for(ctx, sl, "acc-1") and WARM not in acme["steps"][0]["source"]
    assert acme["test_arm"] == "b" and acme["test_name"] == "no intro"


def test_the_card_says_when_the_account_is_not_in_the_test():
    swap = dataclasses.replace(WARM_TEST, change="replace", find="the strain stays hidden",
                               text_a="the strain shows up late")
    general = copy_row("general-v1", "General", bodies={**BODIES, 1: BODIES[1].replace("stays hidden", "hides")})
    ctx, t, sl, _ = proposed(tests=(swap,), copy=(make_settings().copy[0], general))
    p = item_for(ctx, "acc-2")["payload"]
    assert (p["test_id"], p["test_arm"]) == ("", "")
    assert ("Test: warm-intro · not in the test (its email 1 (general-v1) does not have the text the test replaces)"
            in card_for(ctx, sl, "acc-2"))
    assert approvals.test_label({"test_id": "", "test_note": ""}) == ""  # no copy test running
    assert approvals.test_label({"test_id": "t1-agencies", "copy_version": "agencies-v2"}) == "Test: t1-agencies · " \
                                                                                              "agencies-v2"


def test_an_edited_card_keeps_its_arm_and_is_recorded_as_edited():
    ctx, t, sl, _ = proposed(tests=(WARM_TEST,))
    row = _rejected(ctx, sl, "acc-3")
    sl.react("pencil2", HARRY_ID, ts=row["payload"]["choices_ts"])
    poll(ctx)
    source = item_for(ctx, "acc-3")["payload"]["steps"][0]["source"]
    new = source.replace(WARM, "I hope this week is treating you well.").replace(
        "(https://www.spill.chat/us/industry/advertising)", "(<https://www.spill.chat/us/industry/advertising>)")
    sl.say(HARRY_ID, new, row["slack_ts"])
    assert poll(ctx)["edits"] == {"accepted": 1}
    p = item_for(ctx, "acc-3")["payload"]
    assert (p["edited"], p["test_id"], p["test_arm"]) == (True, "warm-intro", "a")
    assert "I hope this week is treating you well." in p["lead"]["custom_variables"]["s1_body"]
    assert WARM in p["original"]["s1_body"]
    sl.react("white_check_mark", HARRY_ID, ts=p["approve_ts"])
    assert poll(ctx)["outcomes"] == {"approved_edited": 1}
    con = ctx.store.get("contacts", contact_id="con-3")
    assert (con["test_id"], con["test_arm"]) == ("warm-intro", "a")  # the arm as assigned, edited or not
    item = item_for(ctx, "acc-3")
    assert ctx.store.get("events", event_id=f"send-approval:{item['item_id']}")["approval"] == "approved_edited"


def test_redo_and_reprepare_give_the_same_arm():
    ctx, t, sl, _ = proposed(tests=(WARM_TEST,))
    before = {r["account_id"]: r["payload"] for r in items(ctx, "open")}
    again = approvals.reprepare(ctx, approvals.Item(item_for(ctx, "acc-3")))
    assert (again.test_id, again.test_arm) == ("warm-intro", "a") and WARM in again.rendered[0].text
    at(ctx, ctx.now, job="approvals_redo")
    out = approvals.redo(ctx, "all")
    assert len(out["posted_again"]) == 3
    for r in items(ctx, "open"):
        old = before[r["account_id"]]
        p = r["payload"]
        assert (p["test_id"], p["test_arm"], p["test_name"]) == (old["test_id"], old["test_arm"], old["test_name"])
        assert p["lead"]["custom_variables"]["s1_body"] == old["lead"]["custom_variables"]["s1_body"]


# -- the reads ---------------------------------------------------------------------------------------------------------


def edited(ctx, i: int) -> None:
    """Account i's step-1 contact's card was edited before it was approved (enrol/approvals/)."""
    ctx.store.insert("events", [{"event_id": f"send-approval:e{i}", "type": "send_approval",
                                 "approval": "approved_edited", "account_id": f"a{i}", "contact_id": f"k{i}",
                                 "step": 1, "occurred_at": T0 - timedelta(hours=1)}])


def test_a_variant_test_is_read_from_its_arms_since_its_start():
    test = dataclasses.replace(WARM_TEST, start_date=date(2026, 9, 28), looks=(2,))
    ctx = looks_world(test)
    arm = {"column": "test_arm", "test_id": "warm-intro"}
    emailed(ctx, 0, "a", T0, reply="positive", **arm)
    emailed(ctx, 1, "a", T0 + timedelta(days=1), **arm)
    emailed(ctx, 2, "b", T0, reply="objection", **arm)
    emailed(ctx, 3, "b", T0 + timedelta(days=1), **arm)
    emailed(ctx, 4, "a", T0, reply="positive", enrolled_at=datetime(2026, 9, 20, tzinfo=UTC), **arm)  # before start
    emailed(ctx, 5, "a", T0, reply="positive", column="test_arm", test_id=None)  # in no test
    emailed(ctx, 6, None, T0, reply="positive", column="test_arm", test_id="warm-intro")  # not in the test: no arm
    result = looks.read(ctx, "warm-intro")
    assert (result["kind"], list(result["versions"])) == ("variant", ["warm intro", "no intro"])
    warm, none = result["versions"]["warm intro"], result["versions"]["no intro"]
    assert (warm["accounts"], warm["delivered"], warm["replied"], warm["positive"]) == (2, 2, 1, 1)
    assert (none["accounts"], none["delivered"], none["replied"], none["positive"]) == (2, 2, 1, 0)
    assert looks.summary_line(result) == ("warm intro 1 of 2 replied (50.0%); no intro 1 of 2 replied (50.0%) "
                                          "· p = 1.00")
    lines, at_look = looks.readout_lines(ctx, ctx.now - timedelta(days=60))
    assert at_look == ["warm-intro"] and lines[0].startswith("  warm-intro (variant) reached look 1: 2 accounts")


def test_before_its_first_look_a_variant_test_shows_no_reply():
    test = dataclasses.replace(WARM_TEST, start_date=date(2026, 9, 28), looks=(5,))
    ctx = looks_world(test)
    emailed(ctx, 0, "a", T0, reply="positive", column="test_arm", test_id="warm-intro")
    with pytest.raises(looks.NotYet) as exc:
        looks.read(ctx, "warm-intro")
    assert "warm intro 1 emailed (1 window closed), no intro 0 emailed" in str(exc.value)
    assert "replied" not in str(exc.value)


def test_an_edited_email_counts_in_its_arm_and_the_read_says_how_many():
    test = dataclasses.replace(WARM_TEST, start_date=date(2026, 9, 28), looks=(2,))
    ctx = looks_world(test)
    arm = {"column": "test_arm", "test_id": "warm-intro"}
    for i, (a, reply) in enumerate([("a", "positive"), ("a", None), ("b", None), ("b", None)]):
        emailed(ctx, i, a, T0 + timedelta(days=i), reply=reply, **arm)
    edited(ctx, 0)
    result = looks.read(ctx, "warm-intro")
    warm = result["versions"]["warm intro"]
    assert (warm["delivered"], warm["replied"], warm["edited"]) == (2, 1, 1)  # counted, as assigned
    assert result["versions"]["no intro"]["edited"] == 0
    assert looks.edited_line(result) == ("Edited by an approver before sending, and counted in the arm they were "
                                         "given: warm intro 1 of 2, no intro 0 of 2.")


def test_test_read_prints_a_variant_test_with_its_edits(capsys):
    test = dataclasses.replace(WARM_TEST, start_date=date(2026, 8, 31), looks=(1,), read_date=date(2027, 3, 29))
    h = Harness(dataclasses.replace(SETTINGS, tests=(test,)))
    ctx = h("seed", False)
    sent = datetime(2026, 9, 7, 15, tzinfo=UTC)
    emailed(ctx, 0, "a", sent, reply="positive", column="test_arm", test_id="warm-intro")
    emailed(ctx, 1, "b", sent, column="test_arm", test_id="warm-intro")
    ctx.store.insert("events", [{"event_id": "send-approval:e1", "type": "send_approval", "approval": "approved_edited",
                                 "account_id": "a1", "contact_id": "k1", "step": 1, "occurred_at": sent}])
    assert h.run("test", "read", "warm-intro") == 0
    out = capsys.readouterr().out
    assert "warm intro 1 of 1 replied (100.0%); no intro 0 of 1 replied (0.0%)" in out
    assert "counted in the arm they were given: warm intro 0 of 1, no intro 1 of 1." in out


# -- the config version ------------------------------------------------------------------------------------------------


def test_the_running_copy_test_is_in_the_config_version(monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    ctx = make_context(make_settings())

    def current(*tests):
        ctx.settings = make_settings(tests=tests)
        return config_version.current(ctx)

    none = current()
    assert none.snapshot["copy_test"] is None
    holdout = dataclasses.replace(WARM_TEST, test_id="t2", kind="holdout", version_a="opener", version_b="holdout",
                                  change="", text_a="")
    assert current(holdout).id == none.id  # a holdout changes nothing a contact is sent
    warm = current(WARM_TEST)
    assert warm.id != none.id and warm.snapshot["copy_test"] == {
        "test_id": "warm-intro", "kind": "variant", "version_a": "warm intro", "version_b": "no intro",
        "accounts_per_version": 400, "start_date": "2026-10-12", "email": 1, "change": "first_line", "text_a": WARM,
        "text_b": "", "find": ""}
    assert current(dataclasses.replace(WARM_TEST, text_a="Great to be connected.")).id != warm.id
    assert current(dataclasses.replace(WARM_TEST, status="read")).id == none.id  # stopped: as before it started
    assert current(FIRST_TEST).snapshot["copy_test"]["kind"] == "ab"
    config_version.record(ctx, warm)
    assert ctx.store.get("config_versions", config_version=warm.id)["copy_test"]["text_a"] == WARM


def test_cohorts_changes_say_when_a_copy_test_started_changed_or_stopped():
    ctx = make_context(make_settings())
    base = {"code_sha": "dev", "campaign_fingerprint": "f1", "signature_hash": "s1", "step_days": [0, 7, 14, 21],
            "settings_versions": {}, "copy_hashes": {}, "general": {}}
    snap = config_version.copy_test(make_settings(tests=(WARM_TEST,)))
    rows = {"v1": None, "v2": snap, "v3": {**snap, "text_a": "Great to be connected.", "accounts_per_version": 500},
            "v4": None, "v5": None}
    for i, (vid, test) in enumerate(rows.items()):
        ctx.store.insert("config_versions", [{"config_version": vid, "run_id": "r", **base, "copy_test": test,
                                              "first_seen": datetime(2026, 10, 12 + i, tzinfo=UTC)}])
    assert cohorts.changes(ctx, "v1", "v2") == [
        "Copy test warm-intro (variant: email 1, first_line; warm intro against no intro) started"]
    assert cohorts.changes(ctx, "v2", "v3") == ["Copy test warm-intro: accounts_per_version 400 → 500, text_a changed"]
    assert cohorts.changes(ctx, "v3", "v4") == ["Copy test warm-intro stopped"]
    assert cohorts.changes(ctx, "v4", "v5") == ["No change."]


# -- an uneven split (Harry, 8 Oct 2026: "a warm greeting on most but not all of the email 1s") ------------------------


WARM_70 = dataclasses.replace(WARM_TEST, share_a=0.7)


def test_share_a_sends_about_that_share_to_version_a_and_blank_is_half_and_half(tabs):
    ids = [f"acct-{i}" for i in range(4000)]
    arms = [variants.arm_for(WARM_70, i) for i in ids]
    assert arms == [variants.arm_for(WARM_70, i) for i in ids]  # deterministic
    assert 0.67 < arms.count("a") / len(ids) < 0.73
    assert [variants.arm_for(WARM_TEST, i) for i in ids] == [queue.test_version(i, "warm-intro") for i in ids]
    for text, share in (("70%", 0.7), ("0.7", 0.7), ("", 0.5)):
        tabs["Tests"] = [variant_row(share_a=text)]
        settings, errors = validate_all(tabs)
        assert not any(errors.values()) and settings.tests[0].share_a == share


def test_share_a_is_checked():
    assert errors_for(variant_row(share_a="95%")) == [
        ("share_a", "must be between 10% and 90%: the smaller arm decides when the test can be read")]
    assert errors_for(variant_row(share_a="70")) == [
        ("share_a", "must be between 0% and 100%, not '70' (write 60% or 0.6)")]
    holdout = _tests_tab(kind="holdout", version_a="opener", version_b="holdout", share_a="70%")
    assert errors_for(holdout) == [
        ("share_a", "applies to ab and variant tests: a holdout reads a split enrol already makes")]
    assert errors_for(_tests_tab(share_a="70%")) == []  # an ab test may be uneven too


def test_the_smaller_arm_takes_accounts_per_version_and_the_larger_its_share():
    assert (WARM_70.cap("a"), WARM_70.cap("b")) == (933, 400)
    assert (WARM_70.scaled(200, "a"), WARM_70.scaled(200, "b")) == (467, 200)
    flipped = dataclasses.replace(WARM_TEST, share_a=0.3)
    assert (flipped.cap("a"), flipped.cap("b")) == (400, 933)
    assert (WARM_TEST.cap("a"), WARM_TEST.cap("b")) == (400, 400)
    assert dataclasses.replace(WARM_70, accounts_per_version=0).cap("a") == 0  # no cap


def test_an_uneven_arm_fills_at_its_own_cap():
    small = dataclasses.replace(WARM_70, accounts_per_version=3)  # 7 accounts in version_a, 3 in version_b
    s = make_settings(tests=(small,))
    ctx, _ = make(settings=s)
    free, rows = Counter({"Hannah Spalding": 5}), enrol.sendable_copy(s)
    arm = variants.arm_for(small, "acc-1")
    cap = small.cap(arm)
    p = enrol.prepare(ctx, enrol.Candidate(account(), contact()), free, Counter({arm: cap - 1}), rows)
    assert (p.test_id, p.test_arm) == ("warm-intro", arm)
    p = enrol.prepare(ctx, enrol.Candidate(account(), contact()), free, Counter({arm: cap}), rows)
    assert p.test_id == "" and p.test_note == f"{small.arm_name(arm)} has its {cap} accounts"


def test_an_uneven_ab_test_splits_by_its_share():
    accts = [account(account_id=f"ag-{i}", domain=f"ag{i}.com", clean_name=f"Agency {i}") for i in range(40)]
    cons = [contact(contact_id=f"c-{i}", account_id=f"ag-{i}", email=f"p{i}@ag{i}.com") for i in range(40)]
    rows = make_settings().copy + (copy_row("agencies-v2", "Marketing & Creative Agencies"),)
    uneven = dataclasses.replace(FIRST_TEST, share_a=0.8)
    ctx, _ = make(live=True, settings=make_settings(live_sending=True, tests=(uneven,), copy=rows),
                  accounts=accts, contacts=cons)
    enrol.run(ctx)
    arms = {i: ctx.store.get("contacts", contact_id=f"c-{i}").get("test_arm") for i in range(40)}
    in_test = {i: arm for i, arm in arms.items() if arm}
    assert len(in_test) >= 6
    assert in_test == {i: queue.test_version(f"ag-{i}", uneven.test_id, 0.8) for i in in_test}
    assert list(in_test.values()).count("a") > list(in_test.values()).count("b")


def test_test_start_says_the_split(capsys):
    h = Harness(SETTINGS, sheet_tabs={"Tests": [variant_row(share_a="70%")]})
    assert h.run("test", "start", "warm-intro") == 0
    out = capsys.readouterr().out
    result, _ = json.JSONDecoder().raw_decode(out[out.index('{\n  "dry_run"'):])
    assert result["split"] == "warm intro 70%, no intro 30%"
    h2 = Harness(SETTINGS, sheet_tabs={"Tests": [variant_row()]})
    assert h2.run("test", "start", "warm-intro") == 0
    out = capsys.readouterr().out
    assert "split" not in json.JSONDecoder().raw_decode(out[out.index('{\n  "dry_run"'):])[0]


def test_an_even_test_keeps_its_config_version_and_an_uneven_one_says_its_split():
    even = config_version.copy_test(make_settings(tests=(WARM_TEST,)))
    uneven = config_version.copy_test(make_settings(tests=(WARM_70,)))
    assert "share_a" not in even and uneven == {**even, "share_a": 0.7}
    ctx = make_context(make_settings())
    base = {"code_sha": "dev", "campaign_fingerprint": "f1", "signature_hash": "s1", "step_days": [0, 7, 14, 21],
            "settings_versions": {}, "copy_hashes": {}, "general": {}}
    for i, (vid, test) in enumerate({"v1": None, "v2": uneven}.items()):
        ctx.store.insert("config_versions", [{"config_version": vid, "run_id": "r", **base, "copy_test": test,
                                              "first_seen": datetime(2026, 10, 12 + i, tzinfo=UTC)}])
    assert cohorts.changes(ctx, "v1", "v2") == [
        "Copy test warm-intro (variant: email 1, first_line; warm intro against no intro, 70/30) started"]
