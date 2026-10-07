"""Copy variants (enrol/variants.py; Harry, 7 Oct 2026: "an A/B where in the A the opening line of the first email in
the sequence is a warm intro 'I hope you're really well. Great to be connected.' and the B version doesn't have
that"): the Tests tab's variant kind and its checks, `test start` for it, the arms enrol assigns and renders, the
card, and the reads."""

from __future__ import annotations

import copy
import dataclasses

import pytest

from tests.test_cli import Harness, _approved, _tests_tab
from tests.test_registry import SETTINGS
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.validate import validate_all, validate_tab

WARM = "I hope you're really well. Great to be connected."


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


# -- the Tests tab ------------------------------------------------------------------------------------------------------


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
    assert errors_for(variant_row(change="replace", find="Spill is that place.", text_a="Spill can be that place.")) == []
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


# -- test start ----------------------------------------------------------------------------------------------------------


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
