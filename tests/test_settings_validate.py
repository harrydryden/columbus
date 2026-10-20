"""Validation of the settings sheet: bad rows are rejected with a precise RowError."""

import copy
from datetime import date, time
from pathlib import Path

import pytest

from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import SendWindow
from us_outbound.settings.validate import (
    FIRST_DATA_ROW,
    HEADER_ROW,
    RowError,
    general_warnings,
    natural_key,
    parse_blackout_dates,
    parse_bool,
    parse_fallback_order,
    parse_int,
    parse_send_window,
    parse_size_range,
    validate_all,
    validate_tab,
)

BASE = default_tabs()


@pytest.fixture
def tabs():
    return copy.deepcopy(BASE)


def _row(tabs, tab, **match):
    """(sheet row number, row dict) of the first row matching all of match."""
    for i, r in enumerate(tabs[tab]):
        if all(r[k] == v for k, v in match.items()):
            return i + FIRST_DATA_ROW, r
    raise KeyError(match)


def _errors(tabs, tab):
    settings, errors = validate_all(tabs)
    assert settings is None
    others = {t: e for t, e in errors.items() if e and t != tab}
    assert not others, others
    return errors[tab]


def _one(tabs, tab, column):
    errs = _errors(tabs, tab)
    assert len(errs) == 1, [str(e) for e in errs]
    assert errs[0].column == column, str(errs[0])
    return errs[0]


# -- Signals --------------------------------------------------------------------------


def test_bad_action(tabs):
    n, row = _row(tabs, "Signals", signal="Layoffs")
    row["action"] = "Maybe"
    e = _one(tabs, "Signals", "action")
    assert (e.tab, e.row, e.label) == ("Signals", n, "Layoffs")
    assert "Score, Hold, Exclude, Suppress" in e.message and "'Maybe'" in e.message
    assert str(e) == f"Signals row {n} (Layoffs), action: {e.message}"


def test_action_is_case_insensitive(tabs):
    _row(tabs, "Signals", signal="Layoffs")[1]["action"] = "suppress"
    settings, errors = validate_all(tabs)
    assert settings is not None
    assert next(s for s in settings.signals if s.signal == "Layoffs").action == "Suppress"


def test_unknown_source(tabs):
    n, row = _row(tabs, "Signals", signal="EAP named")
    row["source"] = "clay_carers"
    e = _one(tabs, "Signals", "source")
    assert e.row == n and "clay_carers" in e.message and "did you mean clay_careers" in e.message


def test_weight_not_a_number(tabs):
    n, row = _row(tabs, "Signals", signal="EAP named")
    row["weight"] = "ten"
    e = _one(tabs, "Signals", "weight")
    assert e.row == n and "whole number" in e.message


def test_weight_accepts_plus_sign(tabs):
    _row(tabs, "Signals", signal="EAP named")[1]["weight"] = "+10"
    settings, _ = validate_all(tabs)
    assert next(s for s in settings.signals if s.signal == "EAP named").weight == 10


def test_score_signal_needs_a_weight_but_suppress_does_not(tabs):
    _row(tabs, "Signals", signal="Layoffs")[1]["weight"] = ""
    assert validate_all(tabs)[0] is not None
    _row(tabs, "Signals", signal="EAP named")[1]["weight"] = ""
    _one(tabs, "Signals", "weight")


def test_condition_with_unknown_field(tabs):
    n, row = _row(tabs, "Signals", signal="People leader in place")
    row["looks_for"] = "people_leader_cnt >= 1"
    e = _one(tabs, "Signals", "looks_for")
    assert e.row == n
    assert "people_leader_cnt is not a field of apollo_people" in e.message
    assert "did you mean people_leader_count" in e.message


def test_condition_field_from_a_source_the_row_does_not_name(tabs):
    _row(tabs, "Signals", signal="First People hire (likely)")[1]["source"] = "apollo_jobs"
    errs = _errors(tabs, "Signals")
    assert {e.column for e in errs} == {"looks_for"}
    assert sorted(e.message.split(" is not")[0] for e in errs) == ["people_leader_count", "people_search_coverage"]
    assert all("is not a field of apollo_jobs" in e.message for e in errs)


def test_field_source_needs_a_condition(tabs):
    n, row = _row(tabs, "Signals", signal="Visited the US site")
    row["looks_for"] = "us_visits_30d >="
    e = _one(tabs, "Signals", "looks_for")
    assert e.row == n and "need a condition" in e.message


def test_text_source_takes_terms(tabs):
    _row(tabs, "Signals", signal="Mental health support listed")[1]["looks_for"] = "EAP; Lyra Health >= good"
    settings, _ = validate_all(tabs)
    assert next(s for s in settings.signals if s.signal == "Mental health support listed").terms == (
        "EAP", "Lyra Health >= good")


def test_a_context_rule_names_only_its_own_terms(tabs):
    """EAP named's context rule names Optum, Cigna, Carelon and Health Advocate: dropping one from looks_for fails."""
    _row(tabs, "Signals", signal="EAP named")[1]["looks_for"] = "EAP; employee assistance; ComPsych"
    e = _one(tabs, "Signals", "context_rule")
    assert "'Optum', which is not in looks_for" in e.message


def test_suggests_angle_not_on_angles(tabs):
    n, row = _row(tabs, "Signals", signal="EAP named")
    row["suggests_angle"] = "Upgrade EAP"
    e = _one(tabs, "Signals", "suggests_angle")
    assert (e.row, e.label) == (n, "EAP named")
    assert "not on the Angles tab" in e.message and "did you mean Upgrade the EAP" in e.message


def test_suggests_angle_case_is_normalized(tabs):
    _row(tabs, "Signals", signal="EAP named")[1]["suggests_angle"] = "upgrade the eap"
    settings, _ = validate_all(tabs)
    assert next(s for s in settings.signals if s.signal == "EAP named").suggests_angle == "Upgrade the EAP"


def test_bad_angles_row_does_not_also_fail_signals_naming_it(tabs):
    _row(tabs, "Angles", angle="Growing team")[1]["order"] = "third"
    errs = _errors(tabs, "Angles")
    assert [e.column for e in errs] == ["order"]


def test_context_rule_naming_a_missing_term(tabs):
    row = _row(tabs, "Signals", signal="Modern mental-health vendor named")[1]
    row["context_rule"] = "Headspaces: app"
    e = _one(tabs, "Signals", "context_rule")
    assert "Headspaces" in e.message


def test_context_rule_on_a_condition(tabs):
    _row(tabs, "Signals", signal="Culture or values page")[1]["context_rule"] = "careers"
    _one(tabs, "Signals", "context_rule")


def test_max_weight_below_weight(tabs):
    _row(tabs, "Signals", signal="Progressive benefits")[1]["max_weight"] = "4"
    e = _one(tabs, "Signals", "max_weight")
    assert "at least the weight (5)" in e.message


def test_counts_for_days_at_least_one(tabs):
    _row(tabs, "Signals", signal="Layoffs")[1]["counts_for_days"] = "0"
    _one(tabs, "Signals", "counts_for_days")
    _row(tabs, "Signals", signal="Layoffs")[1]["counts_for_days"] = "1 (recomputed daily)"
    _one(tabs, "Signals", "counts_for_days")


def test_duplicate_signal_name(tabs):
    dup = dict(_row(tabs, "Signals", signal="Layoffs")[1])
    dup["signal"] = "layoffs"
    tabs["Signals"].append(dup)
    e = _one(tabs, "Signals", "signal")
    assert e.row == len(tabs["Signals"]) + 1 and "already on row" in e.message


def test_active_must_be_yes_or_no(tabs):
    _row(tabs, "Signals", signal="Layoffs")[1]["active"] = "maybe"
    e = _one(tabs, "Signals", "active")
    assert "yes or no" in e.message
    _row(tabs, "Signals", signal="Layoffs")[1]["active"] = "TRUE"
    assert validate_all(tabs)[0] is not None


def test_opener_placeholders(tabs):
    row = _row(tabs, "Signals", signal="EAP named")[1]
    row["opener"] = "Saw your benefits page mentions {evidence}"
    settings, _ = validate_all(tabs)
    assert settings is not None
    row["opener"] = "Saw your page mentions {evidnce}"
    _one(tabs, "Signals", "opener")


def test_several_errors_on_one_row_are_all_reported(tabs):
    row = _row(tabs, "Signals", signal="EAP named")[1]
    row.update(action="Maybe", weight="ten", source="nowhere")
    errs = _errors(tabs, "Signals")
    assert {e.column for e in errs} == {"action", "weight", "source"}


# -- General ------------------------------------------------------------------------


def _no_errors(errors):
    return {t: [str(e) for e in errs] for t, errs in errors.items() if errs}


def test_an_unknown_key_warns_and_a_duplicate_key_still_fails_the_sync(tabs):
    """W1-1 (RS P0-2): an unknown key was an error, so a rollback after Harry added a new key's row (or a package
    that added one) left General unusable and stopped every job, the brakes included (11 Oct 2026). It warns now,
    with the same hint, and its row is not read; a duplicate key still fails the sheet sync (strict)."""
    tabs["General"].append({"key": "live_sendng", "value": "yes", "note": ""})
    n = len(tabs["General"]) + 1  # the typo's sheet row
    settings, errors = validate_all(tabs)
    assert _no_errors(errors) == {} and settings.general.live_sending is False  # the typo's "yes" is never read
    [w] = general_warnings(tabs["General"])
    assert w.row == n and w.column == "key" and "unknown key" in w.message and "did you mean live_sending" in w.message
    tabs["General"].append({"key": "weekly_enrol_cap", "value": "20", "note": ""})
    e = _one(tabs, "General", "key")
    assert e.row == n + 1 and "already on row" in e.message


def test_renamed_keys_warn_and_say_what_replaced_them(tabs):
    # clay_weekly_credits was renamed clay_monthly_credits, and both are retired with Clay (W2-1, Harry's D18).
    for old, new, why in (("daily_enrol_cap", "weekly_enrol_cap", "Monday to Sunday"),
                          ("apollo_weekly_credits", "apollo_monthly_credits", "monthly, like Apollo's own")):
        t = copy.deepcopy(BASE)
        t["General"].append({"key": old, "value": "30", "note": ""})
        settings, errors = validate_all(t)
        assert _no_errors(errors) == {} and settings is not None
        [w] = general_warnings(t["General"])
        assert f"'{old}' is now '{new}'" in w.message and why in w.message and "this row is not read" in w.message


@pytest.mark.parametrize("strict", [False, True])
def test_unknown_renamed_and_retired_keys_warn_in_both_modes_and_a_bad_value_fails(tabs, strict):
    """The W1-1 case as the plan gives it: a retired key (control_share, retired by W1-C with explore_share in its
    place), a renamed one (daily_enrol_cap) and an unknown one: 3 warnings and 0 errors, strict or not. The retired
    row's value is not read: General has no control_share since W2-2, and explore_share keeps its default."""
    for key, value in (("control_share", "0.4"), ("daily_enrol_cap", "30"), ("made_up_key", "x")):
        tabs["General"].append({"key": key, "value": value, "note": ""})
    settings, errors = validate_all(tabs, strict=strict)
    assert _no_errors(errors) == {} and settings is not None
    assert not hasattr(settings.general, "control_share") and settings.general.explore_share == 0.15
    warned = general_warnings(tabs["General"], strict=strict)
    assert sorted(w.label for w in warned) == ["control_share", "daily_enrol_cap", "made_up_key"]
    [retired] = [w for w in warned if w.label == "control_share"]
    assert "no longer used" in retired.message and "explore_share" in retired.message
    # A value that does not parse fails in both modes: leniency is about key names, never about values.
    _, row = _row(tabs, "General", key="weekly_enrol_cap")
    row["value"] = "abc"
    _, errors = validate_all(tabs, strict=strict)
    [e] = errors["General"]
    assert e.label == "weekly_enrol_cap" and e.column == "value" and "whole number" in e.message


def test_a_duplicate_key_in_the_stored_version_is_read_leniently(tabs):
    """strict=False (load_current re-reading the version in force) keeps the first row of a duplicated key."""
    tabs["General"].append({"key": "weekly_enrol_cap", "value": "20", "note": ""})
    settings, errors = validate_all(tabs, strict=False)
    assert _no_errors(errors) == {} and settings.general.weekly_enrol_cap == 150  # the first row's value
    [w] = general_warnings(tabs["General"], strict=False)
    assert "already on row" in w.message


LIVE_KEYS = [line.strip() for line in
             (Path(__file__).parent / "fixtures" / "general_keys_live.txt").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
# The live General values that differ from the code defaults (data-crm-settings, 19 Oct 2026); ids are stand-ins.
# second_contact stays yes here although Harry sets it to no before W2-1 deploys (D15): either way it is retired.
LIVE_VALUES = {"live_sending": "yes", "auto_send": "yes", "priority_threshold": "40", "approver_slack_ids": "U0LIVE01",
               "optout_tested": "yes", "hubspot_pipeline_id": "1001", "hubspot_deal_stage": "Demo requested",
               "hubspot_deal_stage_id": "2002", "hubspot_owner_id": "3003", "second_contact": "yes",
               "max_employees": "350", "claude_monthly_cap_usd": "50"}


def test_every_live_general_key_is_known_retired_or_renamed():
    from us_outbound.settings.validate import RENAMED_GENERAL, RETIRED_GENERAL, _GENERAL_TYPES

    assert len(LIVE_KEYS) == 66 == len(set(LIVE_KEYS))
    unknown = [k for k in LIVE_KEYS if k not in _GENERAL_TYPES and k not in RETIRED_GENERAL and k not in RENAMED_GENERAL]
    assert unknown == []


# The live values of the keys W2-1 retired (no longer among the build's rows): their old build defaults, as the live
# sheet has them (data-crm-settings: at default), and LIVE_VALUES over them.
W21_RETIRED_LIVE = {
    "auto_send": "no", "clay_monthly_credits": "2000", "recontact_person_months": "12", "recontact_account_months": "6",
    "hubspot_deal_stage": "", "clay_accounts_function_id": "", "clay_contacts_function_id": "",
    "clay_credits_per_account": "0", "clay_verification": "skip", "clay_email_fallback": "no", "clay_cross_check": "no",
    "second_contact": "no", "second_contact_min_employees": "50", "second_contact_delay_days": "3",
    "apollo_credits_per_account": "1", "opener_focus": "no",
    "opener_focus_line": "I came across {company} and its work on {focus}.",
}
# The live values of the six constants W2-4 moved to code (settings/model.py; 20 Oct 2026): the live sheet has each
# at its default (data-crm-settings: score_cap 100; the others are among the 54 at default).
W24_RETIRED_LIVE = {
    "score_cap": "100", "escalation_hours": "24", "apollo_enrich_groups": "Technology & Startups",
    "site_visit_domain": "spill.chat", "site_visit_us_paths": "/us", "site_visit_intent_paths": "/us/pricing, /us/demo, /us/book",
}
# The 17 live keys W2-1 retired, W1-C's control_share, W2-2's three tier keys, W2-4's six constants and W2-3's
# stop_rule_complaint_rate (no input: nothing reports a complaint): the live sheet's 18 + 3 + 6 + 1 = 28 warnings, in
# its row order (the fixture's).
_RETIRED_BY = {"auto_send", "control_share", "priority_threshold", "standard_threshold", "auto_thresholds",
               "clay_monthly_credits", "recontact_person_months",
               "recontact_account_months", "hubspot_deal_stage", "clay_accounts_function_id", "clay_contacts_function_id",
               "clay_credits_per_account", "clay_verification", "clay_email_fallback", "clay_cross_check",
               "second_contact", "second_contact_min_employees", "second_contact_delay_days",
               "apollo_credits_per_account", "opener_focus", "opener_focus_line", *W24_RETIRED_LIVE,
               "stop_rule_complaint_rate"}
LIVE_RETIRED = [k for k in LIVE_KEYS if k in _RETIRED_BY]


def _live_general() -> list[dict]:
    """The live sheet's General tab: its 66 key names (the fixture) with the 12 changed values, every other value the
    build's default; control_share (retired by W1-C) at its live 0.15, the keys W2-1 retired at their live values, and
    the tier keys W2-2 retired at theirs (auto_thresholds yes, standard_threshold 20, priority_threshold 40)."""
    build = {r["key"]: r["value"] for r in BASE["General"] if r.get("key")}
    values = {**W21_RETIRED_LIVE, **W24_RETIRED_LIVE, **build, "control_share": "0.15", "standard_threshold": "20",
              "auto_thresholds": "yes", "stop_rule_complaint_rate": "0.003", **LIVE_VALUES}
    return [{"key": k, "value": values[k], "note": ""} for k in LIVE_KEYS]


def test_the_live_general_keys_less_the_retired_are_38_and_the_build_has_them_all():
    """W2-1's acceptance (General has at most 48 keys), with W2-2's tier keys, W2-4's six constants in code and W2-3's
    stop_rule_complaint_rate: 66 live keys less the 28 retired ones (W2-1's 18, W2-2's priority_threshold,
    standard_threshold and auto_thresholds, W2-4's 6 and W2-3's 1) = 38, every one of them a build key; the build's others are the ones Wave 1 added (explore_share,
    hand_check_sample, early_warning_pause, hand_check_gate), which the sheet has no row for yet, so they take their
    code defaults."""
    from us_outbound.settings.validate import RETIRED_GENERAL

    build = [r["key"] for r in BASE["General"] if r.get("key")]
    assert set(LIVE_RETIRED) <= set(RETIRED_GENERAL) and len(LIVE_RETIRED) == 18 + 3 + 6 + 1 == len(set(LIVE_RETIRED))
    kept = [k for k in LIVE_KEYS if k not in RETIRED_GENERAL]
    assert len(kept) == 66 - 28 == 38 and set(kept) <= set(build)
    assert not set(build) & set(RETIRED_GENERAL)
    # W2-4: the constants are in code at the live sheet's values, and the General model no longer has them.
    from us_outbound.settings import model

    assert (model.SCORE_CAP, model.ESCALATION_HOURS, model.APOLLO_ENRICH_GROUPS) == (100, 24, ("Technology & Startups",))
    assert (model.SITE_VISIT_DOMAIN, model.SITE_VISIT_US_PATHS, model.SITE_VISIT_INTENT_PATHS) == (
        "spill.chat", ("/us",), ("/us/pricing", "/us/demo", "/us/book"))
    assert not any(hasattr(model.General(), k) for k in W24_RETIRED_LIVE)


def test_the_live_general_rows_validate_with_no_errors(tabs):
    """The live sheet's General tab as it is (auto_send = yes, second_contact, clay_verification = skip and
    control_share included), strict and lenient: no errors; one warning for each retired key, each once (control_share
    by W1-C; auto_send, the Clay, second-contact and never-read keys by W2-1: their rows can be deleted, and none is
    read; W2-2's tier keys too); the changed values read as the sheet has them; and the keys W1-C adds take their
    code defaults, since the sheet has no row for them: explore_share 0.15 (Harry's D7: the random arm, from W2-2,
    which retired the Control tier's 0.15), hand_check_sample 10, early_warning_pause no."""
    tabs["General"] = _live_general()
    assert {r["key"]: r["value"] for r in tabs["General"] if r["key"] in ("auto_send", "clay_verification")} == {
        "auto_send": "yes", "clay_verification": "skip"}
    for strict in (True, False):
        settings, errors = validate_all(tabs, strict=strict)
        assert _no_errors(errors) == {}
        warned = [w.label for w in general_warnings(tabs["General"], strict=strict)]
        assert warned == LIVE_RETIRED  # each retired key once, in the sheet's order
        g = settings.general
        assert not any(hasattr(g, k) for k in LIVE_RETIRED)
        assert (g.live_sending, g.max_employees) == (True, 350)
        assert (g.explore_share, g.hand_check_sample, g.early_warning_pause) == (0.15, 10, False)


def test_the_live_general_rows_with_harry_s_new_values_validate(tabs):
    """The W1-C acceptance: the live rows plus explore_share = 0.15 (Harry's D7) and hand_check_sample = 20 (his D5)
    validate, strict and lenient, and read as typed; the retired keys are still the only warnings."""
    tabs["General"] = _live_general() + [{"key": "explore_share", "value": "0.15", "note": ""},
                                         {"key": "hand_check_sample", "value": "20", "note": ""},
                                         {"key": "early_warning_pause", "value": "yes", "note": ""}]
    for strict in (True, False):
        settings, errors = validate_all(tabs, strict=strict)
        assert _no_errors(errors) == {}
        assert [w.label for w in general_warnings(tabs["General"], strict=strict)] == LIVE_RETIRED
        g = settings.general
        assert (g.explore_share, g.hand_check_sample, g.early_warning_pause) == (0.15, 20, True)


@pytest.mark.parametrize("key, value, ok", [
    # explore_share is a share from 0 to 0.25 (MAX_EXPLORE_SHARE), both ends allowed.
    ("explore_share", "0", True), ("explore_share", "0.25", True), ("explore_share", "0.26", False),
    ("explore_share", "-0.01", False), ("explore_share", "15%", False),
    # hand_check_sample is a whole number of rows from 1 to 50.
    ("hand_check_sample", "1", True), ("hand_check_sample", "50", True), ("hand_check_sample", "0", False),
    ("hand_check_sample", "51", False), ("hand_check_sample", "12.5", False),
    ("early_warning_pause", "yes", True), ("early_warning_pause", "maybe", False),
])
def test_the_contracts_commit_keys_hold_to_their_ranges(tabs, key, value, ok):
    """The edges of W1-C's three new keys: a value outside them fails the row (strict or not: leniency is about key
    names), so a typo can never put a quarter-plus of the day's sends outside the score order."""
    _row(tabs, "General", key=key)[1]["value"] = value  # the build's row, as `settings load` puts it on the sheet
    for strict in (True, False):
        settings, errors = validate_all(tabs, strict=strict)
        if ok:
            assert _no_errors(errors) == {}, (key, value)
        else:
            [e] = errors["General"]
            assert (e.label, e.column) == (key, "value"), (key, value, str(e))


def test_general_types(tabs):
    for key, value, fragment in [
        ("weekly_enrol_cap", "thirty", "whole number"),
        ("explore_share", "15%", "number"),
        ("explore_share", "0.5", "between 0 and 0.25"),
        ("hand_check_sample", "100", "1 to 50"),
        ("live_sending", "maybe", "yes or no"),
        ("send_window", "Mon–Fri 09:00–16:00 America/Nowhere", "time zone"),
        ("send_window", "weekdays", "must look like"),
        ("blackout_dates", "2026-11-27..2026-11-23", "ends before it starts"),
        ("blackout_dates", "23/11/2026", "YYYY-MM-DD"),
        ("claude_monthly_cap_usd", "150", "$100"),  # Harry, 7 Oct 2026: $50 is allowed; past $100 is a typo
        ("alert_channel", "us-outbound", "channel"),
        ("escalation_email", "harry", "email"),
        ("approver_slack_ids", "harry", "Slack user id"),
        ("booking_link", "meetings.hubspot.com/harry", "https://"),
        ("apollo_floor", "-1", "negative"),
        ("escalation_email", "", "required"),
        ("claude_model", "Haiku", "Claude model id"),
        ("claude_task_model", "Sonnet", "Claude model id"),
        ("email_format", "rich", "html, text"),
        ("label_check", "maybe", "required, skip"),  # Harry, 7 Oct 2026: the label check (labels.py)
        ("label_check", "", "required"),
    ]:
        t = copy.deepcopy(BASE)
        n, row = _row(t, "General", key=key)
        row["value"] = value
        e = _one(t, "General", "value")
        assert e.row == n and e.label == key and fragment in e.message, (key, value, e.message)


def test_general_conversions(tabs):
    _row(tabs, "General", key="approver_slack_ids")[1]["value"] = "U01HARRY, U02OTHER"
    _row(tabs, "General", key="live_sending")[1]["value"] = "No"
    _row(tabs, "General", key="send_window")[1]["value"] = "Mon-Thu 08:30-15:00 America/Chicago"
    _row(tabs, "General", key="apollo_monthly_credits")[1]["value"] = "1,500"
    settings, _ = validate_all(tabs)
    g = settings.general
    assert g.approver_slack_ids == ("U01HARRY", "U02OTHER")
    assert g.live_sending is False
    assert g.send_window == SendWindow((0, 1, 2, 3), time(8, 30), time(15), "America/Chicago")
    assert g.apollo_monthly_credits == 1500


def test_missing_general_keys_take_model_defaults(tabs):
    tabs["General"] = [r for r in tabs["General"] if r["key"] != "apollo_floor"]
    settings, _ = validate_all(tabs)
    assert settings.general.apollo_floor == 5000


def test_a_missing_blackout_dates_row_keeps_the_spec_blackouts(tabs):
    tabs["General"] = [r for r in tabs["General"] if r["key"] != "blackout_dates"]
    settings, _ = validate_all(tabs)
    assert [(d.start, d.end) for d in settings.general.blackout_dates] == [
        (date(2026, 11, 23), date(2026, 11, 27)), (date(2026, 12, 18), date(2027, 1, 4)),
    ]


def test_live_sending_needs_an_approver(tabs):
    n, row = _row(tabs, "General", key="live_sending")
    row["value"] = "yes"
    e = _one(tabs, "General", "value")
    assert e.row == n and "approver_slack_ids" in e.message
    _row(tabs, "General", key="approver_slack_ids")[1]["value"] = "U01HARRY"
    settings, _ = validate_all(tabs)
    assert settings.general.live_sending is True


def test_blank_sending_values_are_allowed_in_dry_run(tabs):
    settings, errors = validate_all(tabs)
    assert settings.general.approver_slack_ids == () and not errors["General"]


def test_retired_general_keys_are_skipped_not_errors(tabs):
    # Harry, 1 Oct 2026: no postal address and no privacy link. A sheet that still has the rows stays valid.
    tabs["General"].append({"key": "postal_address", "value": "", "note": ""})
    tabs["General"].append({"key": "privacy_url", "value": "https://www.spill.chat/us/privacy", "note": ""})
    settings, errors = validate_all(tabs)
    assert settings is not None and not errors["General"]
    assert not hasattr(settings.general, "postal_address")


def test_dev_channel_must_differ_from_alert_channel(tabs):
    n, row = _row(tabs, "General", key="dev_channel")
    row["value"] = "#us-outbound"
    e = _one(tabs, "General", "value")
    assert e.row == n and "must differ from alert_channel" in e.message


# -- other tabs ---------------------------------------------------------------------


def test_angles_need_general(tabs):
    tabs["Angles"] = [r for r in tabs["Angles"] if r["angle"] != "General"]
    e = _one(tabs, "Angles", "angle")
    assert e.row == HEADER_ROW
    _row(tabs, "Angles", angle="Growing team")  # still there


def test_duplicate_angle_order(tabs):
    n, row = _row(tabs, "Angles", angle="Growing team")
    row["order"] = "1"
    e = _one(tabs, "Angles", "order")
    assert e.row == n and "already on row 2" in e.message


def test_states(tabs):
    _row(tabs, "States", state="CA")[1]["active"] = "yes"
    e = _one(tabs, "States", "active")
    assert "never" in e.message
    t = copy.deepcopy(BASE)
    _row(t, "States", state="FL")[1]["active"] = "yes"
    assert validate_all(t)[0] is not None  # Florida is Harry's call
    t["States"].append({"state": "XX", "active": "no", "note": ""})
    _one(t, "States", "state")


def test_roles(tabs):
    _row(tabs, "Roles", role="Operations")[1]["order_10_49"] = "first"
    e = _one(tabs, "Roles", "order_10_49")
    assert "whole number" in e.message
    t = copy.deepcopy(BASE)
    _row(t, "Roles", role="Operations")[1]["order_50_249"] = "0"
    assert "rank from 1" in _one(t, "Roles", "order_50_249").message
    t = copy.deepcopy(BASE)
    _row(t, "Roles", role="Finance")[1]["titles"] = "CFO; COO"
    e = _one(t, "Roles", "titles")
    assert "also a title of Operations" in e.message
    t = copy.deepcopy(BASE)  # however it is spelled
    _row(t, "Roles", role="HR manager")[1]["titles"] += "; Head of Human Resources"
    assert "also a title of People leader" in _one(t, "Roles", "titles").message


def test_roles_copy_role_and_who_is_contacted(tabs):
    _row(tabs, "Roles", role="HR manager")[1]["copy_role"] = "HR"
    assert "must be one of People leader" in _one(tabs, "Roles", "copy_role").message
    t = copy.deepcopy(BASE)
    _row(t, "Roles", role="Finance")[1]["order_50_249"] = "6"  # contacted, but Finance has no copy line
    assert "set copy_role" in _one(t, "Roles", "copy_role").message
    t = copy.deepcopy(BASE)
    for r in t["Roles"]:
        r["order_10_49"] = ""
    e = _one(t, "Roles", "order_10_49")
    assert (e.row, e.message) == (HEADER_ROW, "no row is contacted at 10-49 staff")
    t = copy.deepcopy(BASE)  # two rows may share a rank, and a copy role may have several rows
    _row(t, "Roles", role="Operations")[1]["order_10_49"] = "1"
    settings, errors = validate_all(t)
    assert settings is not None, errors
    assert next(r for r in settings.roles if r.role == "HR manager").writes_as == "People leader"


def test_roles_industry_groups_are_on_the_industries_tab(tabs):
    _row(tabs, "Roles", role="Partner at a professional firm")[1]["industry_groups"] = "legal teams; Law firms"
    e = _one(tabs, "Roles", "industry_groups")
    assert "'Law firms' is not an industry_group" in e.message
    t = copy.deepcopy(BASE)
    _row(t, "Roles", role="Partner at a professional firm")[1]["industry_groups"] = "legal teams"
    settings, _ = validate_all(t)
    assert next(r for r in settings.roles if r.role == "Partner at a professional firm").industry_groups == ("Legal Teams",)


# The Roles tab as SPEC 5 laid it out, before 1 Oct 2026; a sheet made then still has it.
LEGACY_ROLES = [
    {"role": "People leader", "titles": "Head of People; HR Manager", "first_choice_for_size": "50-249",
     "fallback_order": "", "note": ""},
    {"role": "Founder or executive", "titles": "CEO; Founder", "first_choice_for_size": "10-49",
     "fallback_order": "50-249:3", "note": ""},
    {"role": "Operations", "titles": "COO; Office Manager", "first_choice_for_size": "",
     "fallback_order": "10-49:2; 50-249:2", "note": ""},
    {"role": "Finance", "titles": "CFO", "first_choice_for_size": "", "fallback_order": "", "note": "not contacted"},
]


def test_the_spec5_roles_layout_is_still_read(tabs):
    tabs["Roles"] = copy.deepcopy(LEGACY_ROLES)
    settings, errors = validate_all(tabs)
    assert settings is not None, {t: [str(x) for x in e] for t, e in errors.items() if e}
    order = {r.role: dict(r.order) for r in settings.roles}
    assert order == {"People leader": {"50-249": 1}, "Founder or executive": {"10-49": 1, "50-249": 3},
                     "Operations": {"10-49": 2, "50-249": 2}, "Finance": {}}
    founder = settings.roles[1]
    assert founder.first_choice_for_size == ("10-49",) and founder.fallback_order == {"50-249": 3}
    tabs["Roles"][2]["fallback_order"] = "10-49"
    assert "range:rank" in _one(tabs, "Roles", "fallback_order").message


def test_settings_load_replaces_the_spec5_roles_layout_and_keeps_a_new_one():
    from us_outbound.settings import load as loader

    assert "Roles" in loader.LOADABLE
    build = BASE["Roles"]
    plan = loader.plan_tab("Roles", copy.deepcopy(LEGACY_ROLES), build)
    assert plan.replaced_layout and plan.rows == build
    edited = copy.deepcopy(build[:-1])  # Harry's edit, and no Finance row
    edited[0]["titles"] += "; Chief Executive"
    plan = loader.plan_tab("Roles", edited, build)
    assert not plan.replaced_layout and plan.added == ["Finance"]
    assert plan.rows[0]["titles"].endswith("; Chief Executive") and plan.rows[-1] == build[-1]


def test_copy(tabs):
    n, row = _row(tabs, "Copy", copy_version="cpa-firms-people-v1")
    row["s2_body"] += " {{frist_name}}"
    e = _one(tabs, "Copy", "s2_body")
    assert (e.row, e.label) == (n, "cpa-firms-people-v1") and "did you mean first_name" in e.message
    for col, value, fragment in [
        ("s3_body", "Hi {company}", "double braces"),
        ("s1_subject", "", "required"),
        ("status", "live", "draft, approved, retired"),
        ("qa", "looks good", "written by `us-outbound copy qa`"),
        ("operations_line", "Hi {{first_name}}", "only {{company}}"),
        ("industry", "Accountants", "not an industry or industry_group"),
        ("role", "Intern", "not on the Roles tab"),
    ]:
        t = copy.deepcopy(BASE)
        _row(t, "Copy", copy_version="cpa-firms-people-v1")[1][col] = value
        e = _one(t, "Copy", col)
        assert fragment in e.message, (col, e.message)
    # A row written for no role that uses {{role_line}} needs every role's line.
    t = copy.deepcopy(BASE)
    _row(t, "Copy", copy_version="cpa-firms-people-v1")[1].update(
        role="", s1_body="{{role_line}}", people_leader_line="x", founder_line="", operations_line="y")
    assert "is required: the emails use {{role_line}}" in _one(t, "Copy", "founder_line").message
    t = copy.deepcopy(BASE)  # one written for a role needs only that role's line
    _row(t, "Copy", copy_version="cpa-firms-people-v1")[1].update(
        s1_body="{{role_line}}", people_leader_line="x", founder_line="")
    assert not any(validate_all(t)[1].values())
    t = copy.deepcopy(BASE)
    _row(t, "Copy", copy_version="cpa-firms-people-v1")[1]["status"] = "approved"
    assert _one(t, "Copy", "approved_by").message.startswith("is required")
    t = copy.deepcopy(BASE)
    _row(t, "Copy", copy_version="legal-teams-people-v1")[1]["copy_version"] = "CPA-firms-people-v1"
    assert "already on row" in _one(t, "Copy", "copy_version").message
    t = copy.deepcopy(BASE)  # a group, General and a role-only row are all fine
    _row(t, "Copy", copy_version="cpa-firms-people-v1")[1].update(industry="professional services", role="operations")
    settings, errors = validate_all(t)
    assert not any(errors.values())
    row = settings.copy_row("cpa-firms-people-v1")
    assert (row.industry, row.role) == ("Professional Services", "Operations")  # as the other tabs spell them


def test_the_old_copy_layout_reads_as_no_copy():
    t = copy.deepcopy(BASE)
    t["Copy"] = [{"copy_version": "general-v1", "angle": "General", "step": "1", "subject": "Hi", "body": "Hi",
                  "status": "approved", "approved_by": "Harry", "sources": ""}]
    settings, errors = validate_all(t)
    assert not any(errors.values()) and settings.copy == ()


def test_qa_code_follows_the_wording():
    t = copy.deepcopy(BASE)
    s, _ = validate_all(t)
    code = s.copy_row("cpa-firms-people-v1").content_hash()
    _row(t, "Copy", copy_version="cpa-firms-people-v1")[1]["qa"] = f"PASS {code.upper()}"
    s, _ = validate_all(t)
    assert s.copy_row("cpa-firms-people-v1").qa_current
    _row(t, "Copy", copy_version="cpa-firms-people-v1")[1]["s4_subject"] = "A different subject"
    s, _ = validate_all(t)
    assert not s.copy_row("cpa-firms-people-v1").qa_current


def test_new_industries_columns_are_optional():
    t = copy.deepcopy(BASE)
    for r in t["Industries"]:
        for c in [c for c in r if c.startswith("page_")]:
            del r[c]
    settings, errors = validate_all(t)
    assert not any(errors.values()) and settings.industry("CPA firms").page.intro == ""


def test_every_label_needs_its_definition_on_the_sheet():
    """Harry, 7 Oct 2026: the label check (labels.py) reads each label's definition. 11 Oct 2026: the sheet is their
    one source, so a blank cell is an error, as a missing column is; a line Harry writes is read with its spaces
    folded."""
    t = copy.deepcopy(BASE)
    _row(t, "Industries", industry="Games studios")[1]["definition"] = ""
    _row(t, "Industries", industry="Fintech")[1]["definition"] = "  Payments   software,\n not banks. "
    settings, errors = validate_all(t)
    assert settings is None
    assert [(e.label, e.column) for e in errors["Industries"]] == [("Games studios", "definition")]
    _row(t, "Industries", industry="Games studios")[1]["definition"] = "Makes its own video games."
    settings, _ = validate_all(t)
    assert settings.industry("Fintech").definition == "Payments software, not banks."  # Harry's, spaces folded
    assert settings.industry("CPA firms").definition.startswith("An accounting, audit, tax or bookkeeping firm")
    t = copy.deepcopy(BASE)
    for r in t["Industries"]:
        del r["definition"]
    assert validate_all(t)[0] is None


def test_label_check_defaults_to_required_and_takes_skip(tabs):
    assert validate_all(tabs)[0].general.label_check == "required"
    _row(tabs, "General", key="label_check")[1]["value"] = "skip"
    assert validate_all(tabs)[0].general.label_check == "skip"
    tabs["General"] = [r for r in tabs["General"] if r["key"] != "label_check"]
    assert validate_all(tabs)[0].general.label_check == "required"  # a sheet without the row: the code default


def test_industry_pages_are_on_spill_chat():
    t = copy.deepcopy(BASE)
    _row(t, "Industries", industry="CPA firms")[1]["landing_page_url"] = "https://example.com/cpa"
    assert "must be a page on https://www.spill.chat" in _one(t, "Industries", "landing_page_url").message


def test_mailboxes(tabs):
    for field, value, fragment in [
        ("domain", "tryspill.org", "must be the address's domain"),
        ("status", "Live", "Warming, Active, Paused, Retired"),
        ("owner_name", "", "required"),
        ("daily_cap", "300", "between 0 and 30"),
        ("added_on", "yesterday", "YYYY-MM-DD"),
    ]:
        t = copy.deepcopy(BASE)
        n, row = _row(t, "Mailboxes", address="sam@meetspill.org")
        row[field] = value
        e = _one(t, "Mailboxes", field)
        assert e.row == n and fragment in e.message, (field, e.message)
    _row(tabs, "Mailboxes", address="sam@meetspill.org")[1].update(address="sam@spill.chat", domain="spill.chat")
    assert "never sends cold email" in _one(tabs, "Mailboxes", "address").message
    t = copy.deepcopy(BASE)
    _row(t, "Mailboxes", address="sam@meetspill.org")[1].update(address="sam@us.spill.chat", domain="us.spill.chat")
    assert "never sends cold email" in _one(t, "Mailboxes", "address").message


def test_mailbox_slack_id_d11(tabs):
    """Optional (D11, Harry, 1 Oct 2026): the owner's Slack id; one id per person, one person per id."""
    settings, errors = validate_all(tabs)
    assert not any(errors.values()) and all(m.slack_id == "" for m in settings.mailboxes)
    for row in tabs["Mailboxes"]:  # a sheet made before the column existed still validates
        del row["slack_id"]
    assert not any(validate_all(tabs)[1].values())
    t = copy.deepcopy(BASE)
    _row(t, "Mailboxes", address="harry@meetspill.org")[1]["slack_id"] = "U098X453UAG"
    _row(t, "Mailboxes", address="harry@tryspill.org")[1]["slack_id"] = "U098X453UAG"  # Harry's two mailboxes
    _row(t, "Mailboxes", address="hannah@meetspill.org")[1]["slack_id"] = "U0HANNAH1"
    settings, errors = validate_all(t)
    assert not any(errors.values())
    assert {m.address: m.slack_id for m in settings.mailboxes}["harry@tryspill.org"] == "U098X453UAG"
    for address, value, fragment in [
        ("sam@meetspill.org", "hannah", "not a Slack user id"),
        ("sam@meetspill.org", "U0HANNAH1", "already Hannah Spalding's"),
        ("harry@tryspill.org", "U0OTHER22", "already has Slack id U098X453UAG"),
    ]:
        bad = copy.deepcopy(t)
        _row(bad, "Mailboxes", address=address)[1]["slack_id"] = value
        assert fragment in _one(bad, "Mailboxes", "slack_id").message, (address, value)


def test_mailbox_address_is_lower_cased_and_domain_derived(tabs):
    _row(tabs, "Mailboxes", address="sam@meetspill.org")[1].update(address="Sam@MeetSpill.org", domain="")
    settings, _ = validate_all(tabs)
    sam = next(m for m in settings.mailboxes if m.owner_name == "Sam Jackson")
    assert (sam.address, sam.domain) == ("sam@meetspill.org", "meetspill.org")


def test_overrides(tabs):
    tabs["Overrides"] = [
        {"domain": "acme.com", "field": "hq_state", "value": "NY", "note": "HQ moved"},
        {"domain": "www.acme.com", "field": "hq_state", "value": "NY", "note": ""},
        {"domain": "acme.com", "field": "hq_state", "value": "NJ", "note": ""},
        {"domain": "beta.io", "field": "HQ State", "value": "NJ", "note": ""},
    ]
    errs = _errors(tabs, "Overrides")
    assert [(e.row, e.column) for e in errs] == [(3, "domain"), (4, "field"), (5, "field")]


def test_an_overrides_industry_is_one_on_the_industries_tab(tabs):
    """9 Oct 2026: "fintech" was taken as written; the label check then failed it on every run."""
    tabs["Overrides"] = [
        {"domain": "acme.com", "field": "industry", "value": "fintech", "note": ""},
        {"domain": "beta.io", "field": "industry_group", "value": "technology & startups", "note": ""},
    ]
    settings, errors = validate_all(tabs)
    assert not any(errors.values())
    assert [(o.field, o.value) for o in settings.overrides] == [("industry", "Fintech"),
                                                                ("industry_group", "Technology & Startups")]
    tabs["Overrides"].append({"domain": "gamma.io", "field": "industry", "value": "Fintek", "note": ""})
    [e] = _errors(tabs, "Overrides")
    assert (e.row, e.column) == (4, "value") and "'Fintek' is not an industry on the Industries tab" in e.message


def test_a_group_has_one_spelling(tabs):
    """9 Oct 2026: one row's "Technology & startups" validated and split the group in two."""
    n, row = next((i + 2, r) for i, r in enumerate(tabs["Industries"]) if r["industry"] == "Fintech")
    row["industry_group"] = "Technology & startups"
    [e] = _errors(tabs, "Industries")
    assert (e.row, e.column) == (n, "industry_group")
    assert e.message.startswith("'Technology & startups' is spelled 'Technology & Startups' on row 2")


def test_a_mistyped_header_is_refused_by_name(tabs):
    """9 Oct 2026: a "Share_A" header holding 70% read as no column, so the test ran 50/50 without a word."""
    for r in tabs["Tests"]:
        r["Share_A"] = r.pop("share_a", "") or "70%"
    [e] = _errors(tabs, "Tests")
    assert (e.row, e.column) == (1, "Share_A")
    assert e.message == "the header 'Share_A' looks like the column 'share_a': rename it, or its values are not read"


def test_general_lists_split_on_semicolons_too(tabs):
    """9 Oct 2026: "/us/pricing; /us/demo" was read as one path that never matches (site_visit_intent_paths, a
    constant in code since W2-4); every General list splits the same way."""
    n, row = _row(tabs, "General", key="approver_slack_ids")
    row["value"] = "U01HARRY; U02OTHER"
    settings, errors = validate_all(tabs)
    assert not any(errors.values()) and settings.general.approver_slack_ids == ("U01HARRY", "U02OTHER")


def test_tests(tabs):
    # A planned test may name copy not written yet; a running one needs both versions approved.
    n, row = _row(tabs, "Tests", test_id="t1-eap-opener")
    settings, errors = validate_all(tabs)
    assert not any(errors.values())
    t = copy.deepcopy(BASE)
    _row(t, "Tests", test_id="t1-eap-opener")[1]["status"] = "running"
    errs = _errors(t, "Tests")
    assert {e.column for e in errs} == {"start_date", "read_date"}
    _row(t, "Tests", test_id="t1-eap-opener")[1].update(start_date="2026-11-02", read_date="2026-12-14",
                                                         version_a="cpa-firms-people-v1", version_b="cpa-firms-people-v9")
    errs = _errors(t, "Tests")
    assert [(e.column, "not a copy_version" in e.message) for e in errs] == [("version_a", False), ("version_b", True)]
    assert "needs cpa-firms-people-v1 approved" in errs[0].message
    _row(t, "Tests", test_id="t1-eap-opener")[1]["version_b"] = "legal-teams-people-v1"
    for c in t["Copy"]:
        c.update(status="approved", approved_by="Harry")
    settings, _ = validate_all(t)
    assert settings.running_test().start_date == date(2026, 11, 2)
    second = dict(_row(t, "Tests", test_id="t1-eap-opener")[1], test_id="t2")
    t["Tests"].append(second)
    assert "only one copy test (ab or variant) runs at a time" in _one(t, "Tests", "status").message


# -- tab-level ---------------------------------------------------------------------


def test_missing_column(tabs):
    for r in tabs["Signals"]:
        del r["counts_for_days"]
    e = _one(tabs, "Signals", "counts_for_days")
    assert e.row == HEADER_ROW and "missing" in e.message


def test_note_columns_are_optional(tabs):
    for r in tabs["Angles"]:
        del r["note"]
    assert validate_all(tabs)[0] is not None


def test_missing_and_empty_tabs(tabs):
    del tabs["States"]
    _, errors = validate_all(tabs)
    assert [e.message for e in errors["States"]] == ["the sheet has no States tab"]
    t = copy.deepcopy(BASE)
    t["Signals"] = []
    assert "has no rows" in _one(t, "Signals", "").message
    t = copy.deepcopy(BASE)
    t["Tests"] = []
    assert validate_all(t)[0] is not None


def test_validate_tab_alone():
    general, errors = validate_tab("General", BASE["General"])
    assert not errors and general.weekly_enrol_cap == 150
    signals, errors = validate_tab("Signals", BASE["Signals"])
    assert not errors and len(signals) == len(BASE["Signals"]) == 23  # 26 less W2-2's three dead rows
    bad = [dict(BASE["States"][0], active="sometimes")]
    states, errors = validate_tab("States", bad)
    assert states == () and errors == [RowError("States", 2, "active", "must be yes or no, not 'sometimes'", "AL")]
    with pytest.raises(ValueError):
        validate_tab("Nope", [])


def test_natural_keys():
    assert natural_key("General", {"key": " live_sending ", "value": "no"}) == "live_sending"
    assert natural_key("Copy", {"copy_version": "cpa-firms-people-v1", "industry": "CPA firms"}) == "cpa-firms-people-v1"
    assert natural_key("Overrides", {"domain": "acme.com", "field": "hq_state"}) == "acme.com|hq_state"


# -- parsers -----------------------------------------------------------------------


def test_parsers():
    assert parse_bool("Yes") is True and parse_bool("false") is False
    assert parse_int("+25") == 25 and parse_int("-10") == -10 and parse_int("1,500") == 1500
    for bad in ("ten", "2.5", "1,50", ""):
        with pytest.raises(ValueError):
            parse_int(bad)
    assert parse_send_window("Mon–Fri 09:00–16:00 America/New_York") == SendWindow(
        (0, 1, 2, 3, 4), time(9), time(16), "America/New_York"
    )
    assert parse_send_window("Mon, Wed, Fri 9:00 - 12:00 Europe/London").days == (0, 2, 4)
    with pytest.raises(ValueError):
        parse_send_window("Fri–Mon 09:00–16:00 America/New_York")
    with pytest.raises(ValueError):
        parse_send_window("Mon–Fri 16:00–09:00 America/New_York")
    ranges = parse_blackout_dates("2026-11-23..2026-11-27, 2026-12-25")
    assert date(2026, 11, 25) in ranges[0] and ranges[1].start == ranges[1].end == date(2026, 12, 25)
    assert parse_size_range("50–249") == "50-249"
    with pytest.raises(ValueError):
        parse_size_range("249-50")
    assert parse_fallback_order("10-49:2; 50-249:3") == {"10-49": 2, "50-249": 3}
    with pytest.raises(ValueError):
        parse_fallback_order("10-49:1")


def test_the_claude_cap_takes_harrys_50_a_month(tabs):
    """Harry, 7 Oct 2026: the cap went from $10 (SPEC 1.1) to $50; the guard refuses only a typo past $100."""
    t = copy.deepcopy(BASE)
    next(r for r in t["General"] if r["key"] == "claude_monthly_cap_usd")["value"] = "50"
    settings, errors = validate_all(t)
    assert not errors["General"] and settings.general.claude_monthly_cap_usd == 50.0
