import pytest

from us_outbound.settings.conditions import (
    ConditionError,
    find_terms,
    parse_condition,
    parse_context_rule,
    parse_terms,
    try_parse_condition,
)


@pytest.mark.parametrize(
    "text,facts,expected",
    [
        ("people_leader_count >= 1", {"people_leader_count": 2}, True),
        ("people_leader_count >= 1", {"people_leader_count": 0}, False),
        ("people_leader_count >= 1", {}, False),
        ("open_people_roles >= 1 AND people_leader_count = 0", {"open_people_roles": 1, "people_leader_count": 0}, True),
        ("open_people_roles >= 1 AND people_leader_count = 0", {"open_people_roles": 1, "people_leader_count": 1}, False),
        ("open_roles >= 3 OR headcount_growth_12m >= 0.10", {"open_roles": 1, "headcount_growth_12m": 0.12}, True),
        ("open_roles >= 3 OR headcount_growth_12m >= 0.10", {"open_roles": 1, "headcount_growth_12m": 0.05}, False),
        ("month in [10, 11, 12]", {"month": 11}, True),
        ("month in [10, 11, 12]", {"month": 9}, False),
        ("values_page = true", {"values_page": True}, True),
        ("values_page = true", {"values_page": False}, False),
        ("revenue >= 2000000 AND revenue <= 50000000", {"revenue": 3_500_000}, True),
        ("hq_state = 'ny'", {"hq_state": "NY"}, True),
        ("hq_state != \"CA\"", {"hq_state": "NY"}, True),
        ("top_paths contains \"/us/pricing\"", {"top_paths": ["/us", "/us/pricing"]}, True),
        ("a = 1 OR b = 1 AND c = 1", {"a": 0, "b": 1, "c": 0}, False),  # AND binds tighter
        ("(a = 1 OR b = 1) AND c = 1", {"a": 1, "b": 0, "c": 1}, True),
    ],
)
def test_conditions(text, facts, expected):
    assert parse_condition(text).evaluate(facts) is expected


@pytest.mark.parametrize(
    "text",
    ["", "people_leader_count >=", "month in 10", ">= 3", "x = y", "x >= 'a'", "x = 1 AND", "__import__('os')", "a = 1; b = 2"],
)
def test_bad_conditions(text):
    with pytest.raises(ConditionError):
        parse_condition(text)


def test_term_lists_are_not_conditions():
    assert try_parse_condition("mental health; EAP; employee assistance") is None
    assert try_parse_condition("counseling in Slack") is None
    assert parse_terms("EAP; employee assistance;  ; eap") == ("EAP", "employee assistance")


def test_whole_word_case_insensitive():
    text = "We offer an Employee Assistance Program and 4-day week. Heap of perks."
    found = {m.term for m in find_terms(text, ("EAP", "employee assistance", "4-day week"))}
    assert found == {"employee assistance", "4-day week"}  # "Heap" is not "EAP"


def test_context_rules():
    rules = parse_context_rule("Headspace: for Work, app, subscription; Calm: app, premium, business, subscription", ("Headspace", "Calm", "Lyra"))
    assert find_terms("We stay calm under pressure.", ("Calm",), rules) == []
    assert [m.term for m in find_terms("Free Calm app subscription for all staff", ("Calm",), rules)] == ["Calm"]
    assert [m.term for m in find_terms("Headspace for Work", ("Headspace",), rules)] == ["Headspace"]
    assert [m.term for m in find_terms("Lyra Health", ("Lyra",), rules)] == ["Lyra"]
    with pytest.raises(ConditionError):
        parse_context_rule("Nope: app", ("Calm",))
