"""Validation of the settings sheet: bad rows are rejected with a precise RowError."""

import copy
from datetime import date, time

import pytest

from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import SendWindow
from us_outbound.settings.validate import (
    FIRST_DATA_ROW,
    HEADER_ROW,
    RowError,
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
    _row(tabs, "Signals", signal="First People hire")[1]["source"] = "apollo_jobs"
    e = _one(tabs, "Signals", "looks_for")
    assert "people_leader_count is not a field of apollo_jobs" in e.message


def test_field_source_needs_a_condition(tabs):
    n, row = _row(tabs, "Signals", signal="Visited the US site")
    row["looks_for"] = "us_visits_30d >="
    e = _one(tabs, "Signals", "looks_for")
    assert e.row == n and "need a condition" in e.message


def test_text_source_takes_terms(tabs):
    _row(tabs, "Signals", signal="EAP named")[1]["looks_for"] = "EAP; Lyra Health >= good"
    settings, _ = validate_all(tabs)
    assert next(s for s in settings.signals if s.signal == "EAP named").terms == ("EAP", "Lyra Health >= good")


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
    _row(tabs, "Signals", signal="Progressive benefits")[1]["max_weight"] = "5"
    e = _one(tabs, "Signals", "max_weight")
    assert "at least the weight (10)" in e.message


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


def test_general_unknown_and_duplicate_keys(tabs):
    tabs["General"].append({"key": "live_sendng", "value": "no", "note": ""})
    tabs["General"].append({"key": "weekly_enrol_cap", "value": "20", "note": ""})
    errs = _errors(tabs, "General")
    by_row = {e.row: e for e in errs}
    n = len(tabs["General"]) + 1
    assert "unknown key" in by_row[n - 1].message and "did you mean live_sending" in by_row[n - 1].message
    assert "already on row" in by_row[n].message
    assert all(e.column == "key" for e in errs)


def test_renamed_keys_say_what_replaced_them(tabs):
    for old, new, why in (("daily_enrol_cap", "weekly_enrol_cap", "Monday to Sunday"),
                          ("clay_weekly_credits", "clay_monthly_credits", "monthly, like Clay's own"),
                          ("apollo_weekly_credits", "apollo_monthly_credits", "monthly, like Apollo's own")):
        t = copy.deepcopy(BASE)
        t["General"].append({"key": old, "value": "30", "note": ""})
        e = _one(t, "General", "key")
        assert f"'{old}' is now '{new}'" in e.message and why in e.message


def test_general_types(tabs):
    for key, value, fragment in [
        ("weekly_enrol_cap", "thirty", "whole number"),
        ("control_share", "15%", "number"),
        ("control_share", "1.5", "between 0 and 1"),
        ("live_sending", "maybe", "yes or no"),
        ("send_window", "Mon–Fri 09:00–16:00 America/Nowhere", "time zone"),
        ("send_window", "weekdays", "must look like"),
        ("blackout_dates", "2026-11-27..2026-11-23", "ends before it starts"),
        ("blackout_dates", "23/11/2026", "YYYY-MM-DD"),
        ("claude_monthly_cap_usd", "50", "$10"),
        ("alert_channel", "us-outbound", "channel"),
        ("escalation_email", "harry", "email"),
        ("approver_slack_ids", "harry", "Slack user id"),
        ("booking_link", "meetings.hubspot.com/harry", "https://"),
        ("apollo_floor", "-1", "negative"),
        ("escalation_email", "", "required"),
        ("claude_model", "Haiku", "Claude model id"),
        ("claude_task_model", "Sonnet", "Claude model id"),
        ("email_format", "rich", "html, text"),
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
    tabs["General"] = [r for r in tabs["General"] if r["key"] != "score_cap"]
    settings, _ = validate_all(tabs)
    assert settings.general.score_cap == 100


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


def test_thresholds_in_order(tabs):
    _row(tabs, "General", key="standard_threshold")[1]["value"] = "60"
    e = _one(tabs, "General", "value")
    assert e.label == "standard_threshold"


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
    _row(tabs, "Roles", role="Operations")[1]["fallback_order"] = "10-49"
    e = _one(tabs, "Roles", "fallback_order")
    assert "range:rank" in e.message
    t = copy.deepcopy(BASE)
    _row(t, "Roles", role="Founder or executive")[1]["first_choice_for_size"] = "10-99"
    e = _one(t, "Roles", "first_choice_for_size")
    assert "overlaps People leader" in e.message
    t = copy.deepcopy(BASE)
    _row(t, "Roles", role="Finance")[1]["titles"] = "CFO; COO"
    e = _one(t, "Roles", "titles")
    assert "also a title of Operations" in e.message


def test_copy(tabs):
    n, row = _row(tabs, "Copy", copy_version="cpa-firms-v1")
    row["s2_body"] += " {{frist_name}}"
    e = _one(tabs, "Copy", "s2_body")
    assert (e.row, e.label) == (n, "cpa-firms-v1") and "did you mean first_name" in e.message
    for col, value, fragment in [
        ("s3_body", "Hi {company}", "double braces"),
        ("s1_subject", "", "required"),
        ("status", "live", "draft, approved, retired"),
        ("qa", "looks good", "written by `us-outbound copy qa`"),
        ("founder_line", "", "is required: the emails use {{role_line}}"),
        ("operations_line", "Hi {{first_name}}", "only {{company}}"),
        ("industry", "Accountants", "not an industry or industry_group"),
        ("role", "Intern", "not on the Roles tab"),
    ]:
        t = copy.deepcopy(BASE)
        _row(t, "Copy", copy_version="cpa-firms-v1")[1][col] = value
        e = _one(t, "Copy", col)
        assert fragment in e.message, (col, e.message)
    t = copy.deepcopy(BASE)
    _row(t, "Copy", copy_version="cpa-firms-v1")[1]["status"] = "approved"
    assert _one(t, "Copy", "approved_by").message.startswith("is required")
    t = copy.deepcopy(BASE)
    _row(t, "Copy", copy_version="legal-teams-v1")[1]["copy_version"] = "CPA-firms-v1"
    assert "already on row" in _one(t, "Copy", "copy_version").message
    t = copy.deepcopy(BASE)  # a group, General and a role-only row are all fine
    _row(t, "Copy", copy_version="cpa-firms-v1")[1].update(industry="professional services", role="operations")
    settings, errors = validate_all(t)
    assert not any(errors.values())
    row = settings.copy_row("cpa-firms-v1")
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
    code = s.copy_row("cpa-firms-v1").content_hash()
    _row(t, "Copy", copy_version="cpa-firms-v1")[1]["qa"] = f"PASS {code.upper()}"
    s, _ = validate_all(t)
    assert s.copy_row("cpa-firms-v1").qa_current
    _row(t, "Copy", copy_version="cpa-firms-v1")[1]["s4_subject"] = "A different subject"
    s, _ = validate_all(t)
    assert not s.copy_row("cpa-firms-v1").qa_current


def test_new_industries_columns_are_optional():
    t = copy.deepcopy(BASE)
    for r in t["Industries"]:
        for c in [c for c in r if c.startswith("page_")]:
            del r[c]
    settings, errors = validate_all(t)
    assert not any(errors.values()) and settings.industry("CPA firms").page.intro == ""


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
                                                         version_a="cpa-firms-v1", version_b="cpa-firms-v9")
    errs = _errors(t, "Tests")
    assert [(e.column, "not a copy_version" in e.message) for e in errs] == [("version_a", False), ("version_b", True)]
    assert "needs cpa-firms-v1 approved" in errs[0].message
    _row(t, "Tests", test_id="t1-eap-opener")[1]["version_b"] = "legal-teams-v1"
    for c in t["Copy"]:
        c.update(status="approved", approved_by="Harry")
    settings, _ = validate_all(t)
    assert settings.running_test().start_date == date(2026, 11, 2)
    second = dict(_row(t, "Tests", test_id="t1-eap-opener")[1], test_id="t2")
    t["Tests"].append(second)
    assert "only one test runs at a time" in _one(t, "Tests", "status").message


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
    assert not errors and len(signals) == 17
    bad = [dict(BASE["States"][0], active="sometimes")]
    states, errors = validate_tab("States", bad)
    assert states == () and errors == [RowError("States", 2, "active", "must be yes or no, not 'sometimes'", "AL")]
    with pytest.raises(ValueError):
        validate_tab("Nope", [])


def test_natural_keys():
    assert natural_key("General", {"key": " live_sending ", "value": "no"}) == "live_sending"
    assert natural_key("Copy", {"copy_version": "cpa-firms-v1", "industry": "CPA firms"}) == "cpa-firms-v1"
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
