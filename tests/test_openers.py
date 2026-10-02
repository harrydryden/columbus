"""Tokenized openers (enrol/openers.py; docs/roadmap.md §4 item 2; Harry, 2 Oct 2026): every signal's line for
each copy role filled from stored facts, the fallbacks, the contact who is the new People leader, title cleaning
and numbers, the holdout, the optional "what they do" phrase, validation of the sheet's lines, the copy desk's
preview and check, and the enrol job recording which arm each account was in. Fakes only."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from tests.fakes import make_context
from tests.test_client_claude import FakeSDK
from tests.test_render import contact, make_settings
from us_outbound.enrol import copy_desk, enrol, openers, queue, render
from us_outbound.scoring.angle import choose_angle
from us_outbound.scoring.score import score_account
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all, validate_tab
from us_outbound.sources import apollo_universe

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
_ids = count()


def defaults(**general):
    settings, errors = validate_all(default_tabs())
    assert settings is not None, errors
    return dataclasses.replace(settings, general=dataclasses.replace(settings.general, **general))


DEFAULT = defaults()
S = defaults(opener_holdout_share=0.0)  # nobody held out, so each line can be read


def check(text: str) -> str:
    return render.pick_opener(text, sender_is_harry=False, demo_host="Harry Dryden", exempt=("Brightline",))[1]


def fact(source, name, value, days=2, *, quote="", account_id="acc-1") -> dict:
    return {"event_id": f"f{next(_ids)}", "account_id": account_id, "source": source, "fact": name, "value": value,
            "quote": quote, "source_url": "", "observed_at": NOW - timedelta(days=days)}


def acct(**kw) -> dict:
    return {"account_id": "acc-1", "domain": "brightline.com", "clean_name": "Brightline", "hq_city": "Austin",
            "hq_state": "TX", "industry": "Fintech", "industry_group": "Technology & Startups", "employees": 40,
            "size_band": "20-49", "tier": "Standard", **kw}


def opener(facts, role="People leader", settings=S, person=None, **kw) -> openers.Opener:
    """The account's opener; its angle as scoring sets it for its tier (Standard, unless given)."""
    a = acct(**kw)
    a.setdefault("angle", choose_angle(a["tier"], score_account(a, facts, settings, TODAY).matches, settings).angle)
    return openers.choose(a, person or {"contact_id": "con-1", "role": role}, facts, settings, TODAY, check=check)


LEADER = [fact("apollo_people", "people_leader_count", 1), fact("apollo_people", "people_leader_days_in_title", 40),
          fact("apollo_people", "people_leader_newest",
               {"apollo_person_id": "p-dana", "title": "HEAD OF PEOPLE", "days_in_title": 40})]
FIRST_HIRE = [fact("apollo_jobs", "open_people_roles", 1), fact("apollo_jobs", "open_roles", 1),
              fact("apollo_jobs", "posting_titles", ["People Operations Manager (Remote)"]),
              fact("apollo_people", "people_leader_count", 0)]
PEOPLE_ROLE = [fact("apollo_jobs", "open_people_roles", 1), fact("apollo_jobs", "open_roles", 2),
               fact("apollo_jobs", "posting_titles", ["Account Executive", "HR Generalist - Austin, TX"])]
FUNDING = [fact("apollo_org", "days_since_funding", 60), fact("apollo_org", "funding_stage", "Series A")]
POSTINGS = ["Marketing Intern", "Senior Product Designer (Remote) - Req #4412", "Account Executive, New York"]
HIRING = [fact("apollo_jobs", "open_roles", 6), fact("apollo_jobs", "open_people_roles", 0),
          fact("apollo_jobs", "posting_titles", POSTINGS)]
GROWTH = [fact("apollo_org", "headcount_growth_12m", 0.34)]
EAP = [fact("clay_careers", "mental_health_provision", {"type": "eap", "provider": "ComPsych"},
            quote="Our employee assistance program through ComPsych is free and confidential.")]
MENTAL_HEALTH = [fact("clay_careers", "benefit", {"item": "Mental health support"}, quote="We care about mental health.")]
APP = [fact("clay_careers", "benefit", {"item": "Headspace app subscription"})]
BENEFITS = [fact("clay_careers", "benefit", {"item": "Paid parental leave"})]
PEOPLE, FOUNDER, OPS = "People leader", "Founder or executive", "Operations"


# -- every signal × role (the default lines, filled from stored facts) ----------------------------------------


@pytest.mark.parametrize("facts, signal, role, line", [
    (LEADER, "New People leader", PEOPLE, "I saw Brightline recently named a new Head of People."),
    (LEADER, "New People leader", FOUNDER, "I saw Brightline has a new Head of People in place."),
    (LEADER, "New People leader", OPS, "I saw there's a new Head of People at Brightline."),
    (FIRST_HIRE, "First People hire", PEOPLE, "I saw Brightline is hiring a People Operations Manager."),
    (FIRST_HIRE, "First People hire", FOUNDER, "I saw Brightline is hiring its first People Operations Manager."),
    (FIRST_HIRE, "First People hire", OPS, "I saw Brightline is looking for its first People Operations Manager."),
    (PEOPLE_ROLE, "People role open", PEOPLE, "I saw Brightline is adding an HR Generalist to the team."),
    (PEOPLE_ROLE, "People role open", FOUNDER, "I saw Brightline is hiring an HR Generalist right now."),
    (PEOPLE_ROLE, "People role open", OPS, "I saw Brightline has an HR Generalist role open."),
    (FUNDING, "Funding in the last 6 months", PEOPLE, "Congratulations to everyone at Brightline on the Series A."),
    (FUNDING, "Funding in the last 6 months", FOUNDER, "Congratulations on the Series A."),
    (FUNDING, "Funding in the last 6 months", OPS, "Congratulations to the Brightline team on the Series A."),
    (HIRING, "Hiring and growth", PEOPLE, "I saw Brightline has six roles open, including a Senior Product Designer."),
    (HIRING, "Hiring and growth", FOUNDER, "I saw Brightline is hiring for six roles, including a Senior Product Designer."),
    (HIRING, "Hiring and growth", OPS, "I saw Brightline is recruiting for six roles, including a Senior Product Designer."),
    (GROWTH, "Hiring and growth", PEOPLE, "I saw the team at Brightline has grown by about a third in the last year."),
    (GROWTH, "Hiring and growth", FOUNDER, "I saw Brightline has grown by about a third in headcount over the last year."),
    (GROWTH, "Hiring and growth", OPS, "I saw the Brightline team has grown by about a third over the past year."),
    (EAP, "EAP named", PEOPLE, "I saw Brightline offers its team an employee assistance program through ComPsych."),
    (EAP, "EAP named", FOUNDER, "I saw Brightline offers an employee assistance program through ComPsych."),
    (EAP, "EAP named", OPS, "I saw Brightline provides an employee assistance program through ComPsych."),
    (MENTAL_HEALTH, "Mental health support listed", PEOPLE, "I saw mental health comes up when Brightline talks about working there."),
    (MENTAL_HEALTH, "Mental health support listed", FOUNDER, "I saw Brightline mentions mental health when it talks about working there."),
    (MENTAL_HEALTH, "Mental health support listed", OPS, "I saw mental health is part of how Brightline describes working there."),
    (APP, "Wellbeing app or perk named", PEOPLE, "I saw Brightline offers Headspace as part of its benefits."),
    (APP, "Wellbeing app or perk named", FOUNDER, "I saw Headspace is one of the perks at Brightline."),
    (APP, "Wellbeing app or perk named", OPS, "I saw Brightline includes Headspace in its benefits."),
    (BENEFITS, "Progressive benefits", PEOPLE, "I saw Brightline lists parental leave among its benefits."),
    (BENEFITS, "Progressive benefits", FOUNDER, "I saw parental leave is one of the benefits at Brightline."),
    (BENEFITS, "Progressive benefits", OPS, "I saw the benefits at Brightline include parental leave."),
])
def test_each_signal_and_role_fills_from_the_account_s_facts(facts, signal, role, line):
    col = {PEOPLE: "opener_people", FOUNDER: "opener_founder", OPS: "opener_ops"}[role]
    op = opener(facts, role)
    assert (op.text, op.arm, op.source) == (line, openers.OPENER, f"{signal} / {col}")
    assert check(op.text) == "" and len(op.text.split()) <= 20  # style.md: one sentence, under about 20 words


def test_the_default_lines_cover_every_signal_that_can_fire_and_pass_the_rules():
    with_lines = {s.signal for s in DEFAULT.signals if s.role_openers}
    assert with_lines == {"New People leader", "First People hire", "People role open", "Funding in the last 6 months",
                          "Hiring and growth", "Mental health support listed", "EAP named",
                          "Wellbeing app or perk named", "Progressive benefits"}
    assert all(set(s.role_openers) == {PEOPLE, FOUNDER, OPS} for s in DEFAULT.signals if s.role_openers)
    assert [s.signal for s in DEFAULT.signals if s.opener_self] == ["New People leader"]
    assert copy_desk.check_openers(DEFAULT).ok
    banned = ("therap", "licensed", "unlimited", "%", "noticed you're", "!")
    for _, _, line, filled in openers.sample_lines(DEFAULT):
        assert not any(b in line.lower() for b in banned), line
        assert filled is None or filled.count(".") == 1 and filled.endswith("."), filled  # one sentence


# -- fallbacks: the next line, the plain opener, none -------------------------------------------------------------


def test_a_line_whose_token_has_no_fact_falls_back_to_the_next_line_then_the_plain_opener():
    # Hiring and growth fired on growth alone: the open-roles line has no {open_roles}, so the growth line.
    op = opener(GROWTH, FOUNDER)
    assert op.source == "Hiring and growth / opener_founder" and "grown by about a third" in op.text
    assert op.notes == ("Hiring and growth, opener_founder line 1: no {open_roles}, {posting_title}",)
    # Six roles but no posting that cleans to a title, and no growth: the signal's plain opener.
    junk = [fact("apollo_jobs", "open_roles", 6), fact("apollo_jobs", "posting_titles", ["Summer Intern", "R-1234"])]
    op = opener(junk, OPS)
    assert (op.text, op.source) == ("I saw the team has been growing.", "Hiring and growth / opener")
    assert op.notes == ("Hiring and growth, opener_ops line 1: no {posting_title}",
                        "Hiring and growth, opener_ops line 2: no {growth}")


def test_a_token_that_fails_its_check_is_missing():
    # A round that does not read as one ("Venture (Round not Specified)") gets the plain line.
    venture = [fact("apollo_org", "days_since_funding", 60), fact("apollo_org", "funding_stage", "Venture (Round not Specified)")]
    assert opener(venture, FOUNDER).text == "Congratulations on the recent funding round."
    # One open role never reads "has one roles open"; growth below 10% is not worth a line.
    few = [fact("apollo_jobs", "open_roles", 3), fact("apollo_jobs", "posting_titles", ["Licensed Therapist"]),
           fact("apollo_org", "headcount_growth_12m", 0.04)]
    op = opener(few, PEOPLE)
    assert op.source == "Hiring and growth / opener"  # the therapist posting is refused by the copy rules
    many = [fact("apollo_jobs", "open_roles", 14), fact("apollo_jobs", "posting_titles", POSTINGS)]
    assert opener(many, PEOPLE).text == "I saw Brightline has 14 roles open, including a Senior Product Designer."


def test_a_filled_line_that_breaks_a_copy_rule_falls_back():
    # "Call Center Agent" would read as asking for a call in email 1, so the next line, then the plain opener.
    calls = [fact("apollo_jobs", "open_roles", 5), fact("apollo_jobs", "posting_titles", ["Call Center Agent"])]
    op = opener(calls, PEOPLE)
    assert op.text == "I saw the team has been growing."
    assert "email 1 asks only for a visit" in op.notes[0]
    # Evidence the copy may not use ("therapy") drops the role line and the plain line: none.
    therapy = [fact("clay_careers", "benefit", {"item": "Free therapy sessions"})]
    op = opener(therapy, OPS)
    assert (op.text, op.arm) == ("", openers.NONE) and any("therapy" in n for n in op.notes)


def test_british_evidence_is_spelled_the_american_way():
    wellbeing = [fact("clay_careers", "benefit", {"item": "Wellbeing support for everyone"})]
    assert opener(wellbeing, FOUNDER).text == "I saw Brightline mentions well-being support when it talks about working there."


def test_no_line_and_no_plain_opener_means_none():
    s = dataclasses.replace(S, signals=tuple(dataclasses.replace(x, opener="") if x.signal == "Hiring and growth" else x
                                             for x in S.signals))
    junk = [fact("apollo_jobs", "open_roles", 6), fact("apollo_jobs", "posting_titles", ["Intern"])]
    op = opener(junk, OPS, settings=s)
    assert (op.text, op.arm, op.source) == ("", openers.NONE, "")


def test_the_general_angle_and_control_have_no_signal_line():
    # A Control account is signal-blind (SPEC 5): General angle, so no line, even with funding facts.
    assert opener(FUNDING, FOUNDER, tier="Control", angle="General") == openers.Opener()
    assert opener([], FOUNDER).arm == openers.NONE  # no signal at all


def test_old_facts_the_signal_no_longer_counts_are_not_quoted():
    # The funding round fires (days are aged), but the posting titles from 120 days ago are past the
    # Hiring signal's window: they never fill a line about the account today.
    stale = [*FUNDING, fact("apollo_jobs", "open_roles", 6, days=120), fact("apollo_jobs", "posting_titles", POSTINGS, days=120)]
    assert opener(stale, PEOPLE).source == "Funding in the last 6 months / opener_people"
    s = next(x for x in S.signals if x.signal == "Hiring and growth")
    assert "posting_title" not in openers.tokens(acct(), s, None, stale, S, TODAY)


# -- the contact who is the new People leader --------------------------------------------------------------------


def picked(contact_id="con-dana", person="p-dana"):
    return fact("pick_contacts", "contact_pick", {"outcome": "picked", "contact_id": contact_id, "apollo_person_id": person})


def test_the_new_people_leader_is_congratulated_and_everyone_else_hears_of_the_hire():
    dana = {"contact_id": "con-dana", "role": PEOPLE}
    op = opener([*LEADER, picked()], person=dana)
    assert (op.text, op.source) == ("Congratulations on the new role at Brightline.", "New People leader / opener_self")
    # Another People person at the account (a different Apollo person) hears about the hire.
    op = opener([*LEADER, picked(person="p-sam")], person=dana)
    assert (op.text, op.source) == ("I saw Brightline recently named a new Head of People.",
                                    "New People leader / opener_people")
    # Without a stored person id the contact is never assumed to be the leader, whatever the title.
    assert opener(LEADER, person={**dana, "title": "Head of People"}).source == "New People leader / opener_people"
    # The id may be on the contact itself (a later Clay contact, say).
    assert opener(LEADER, person={**dana, "apollo_person_id": "p-dana"}).source == "New People leader / opener_self"


def test_without_a_self_line_the_leader_gets_the_plain_opener_not_news_of_their_own_hire():
    s = dataclasses.replace(S, signals=tuple(dataclasses.replace(x, opener_self="") if x.signal == "New People leader"
                                             else x for x in S.signals))
    op = opener([*LEADER, picked()], person={"contact_id": "con-dana", "role": PEOPLE}, settings=s)
    assert (op.text, op.source) == ("I saw the team recently added a new People leader.", "New People leader / opener")


# -- tokens: titles, numbers, growth, rounds, articles --------------------------------------------------------------


@pytest.mark.parametrize("raw, clean", [
    ("Senior Product Designer (Remote) - Req #4412", "Senior Product Designer"),
    ("Remote - Account Executive, New York", "Account Executive"),
    ("Customer Success Manager | Hybrid | Austin, TX", "Customer Success Manager"),
    ("R-12345 Head of People", "Head of People"),
    ("Head of People in New York", "Head of People"),
    ("SENIOR PRODUCT DESIGNER", "Senior Product Designer"),
    ("VP OF SALES", "VP of Sales"),
    ("hr generalist", "HR Generalist"),
    ("Software Engineer - Backend", "Software Engineer"),
    ("Office Manager [JR100234]", "Office Manager"),
    ("Licensed Therapist", None),  # the copy rules never allow it
    ("Chief Executive Officer and Founder of the Whole Wide Company", None),  # too long to read as a title
    ("2026", None),
    ("", None),
])
def test_posting_titles_are_cleaned(raw, clean):
    assert openers.clean_title(raw) == clean


def test_the_posting_named_is_the_most_senior_and_never_an_intern():
    assert openers.best_posting(["Marketing Intern", "Account Executive", "Director of Operations (Remote)"]) == \
        "Director of Operations"
    assert openers.best_posting(["Summer Internship", "Contract Bookkeeper"]) is None
    assert openers.best_posting(["Account Executive", "Sales Development Rep"]) == "Account Executive"


def test_numbers_growth_and_rounds_read_as_words():
    assert [openers.number_word(n) for n in (2, 6, 9, 10, 14)] == ["two", "six", "nine", "10", "14"]
    assert openers.growth_words(0.09) is None and openers.growth_words(7) is None  # too small; bad data
    assert [openers.growth_words(g) for g in (0.1, 0.2, 0.25, 0.34, 0.5, 0.9, 1.0, 1.6)] == [
        "grown by about a tenth", "grown by about a fifth", "grown by about a quarter", "grown by about a third",
        "grown by about half", "nearly doubled", "roughly doubled", "more than doubled"]
    assert [openers.funding_phrase(x) for x in ("Series A", "series_b", "Seed", "Pre-Seed", "Angel", "Debt Financing")] == [
        "Series A", "Series B", "seed round", "pre-seed round", "angel round", None]


def test_articles_and_possessives_follow_the_value():
    assert openers.fill("I saw {company} has a {t} role open.", {"company": "Acme", "t": "HR Generalist"})[0] == \
        "I saw Acme has an HR Generalist role open."
    assert openers.fill("A {t} joined.", {"t": "Office Manager"})[0] == "An Office Manager joined."
    assert openers.fill("a {t}", {"t": "UX Designer"})[0] == "a UX Designer"
    assert openers.fill("a {t}", {"t": "User Researcher"})[0] == "a User Researcher"
    assert openers.fill("Congratulations on {company}'s {r}.", {"company": "Acme Labs", "r": "Series A"})[0] == \
        "Congratulations on Acme Labs' Series A."
    assert openers.fill("{company} and {x}", {"company": "Acme"}) == (None, ["x"])


# -- the holdout ------------------------------------------------------------------------------------------------------


def test_the_holdout_is_deterministic_about_30_percent_and_independent_of_the_test_split():
    ids = [f"acc-{i}" for i in range(20000)]
    held = [openers.in_holdout(a, 0.3) for a in ids]
    assert held == [openers.in_holdout(a, 0.3) for a in ids]  # the same every run
    assert 0.29 < sum(held) / len(ids) < 0.31
    assert not any(openers.in_holdout(a, 0.0) for a in ids[:500]) and all(openers.in_holdout(a, 1.0) for a in ids[:500])
    # The opener holdout does not line up with the copy test's split (queue.test_version).
    a_side = [h for a, h in zip(ids, held, strict=True) if queue.test_version(a, "t1") == "a"]
    b_side = [h for a, h in zip(ids, held, strict=True) if queue.test_version(a, "t1") == "b"]
    assert abs(sum(a_side) / len(a_side) - sum(b_side) / len(b_side)) < 0.02
    assert DEFAULT.general.opener_holdout_share == 0.3


def test_a_held_out_account_gets_none_and_records_the_line_it_would_have_had():
    op = opener(FUNDING, FOUNDER, settings=defaults(opener_holdout_share=1.0))
    assert (op.text, op.arm, op.source, op.would_be) == (
        "", openers.HOLDOUT, "Funding in the last 6 months / opener_founder", "Congratulations on the Series A.")


# -- the "what they do" phrase (General opener_focus) ---------------------------------------------------------------

ABOUT = [fact("apollo_org", "keywords", ["payroll software", "restaurants", "hospitality", "saas"]),
         fact("apollo_org", "description", "Brightline makes payroll software for restaurants and bars.")]


def focus_ctx(phrase="payroll software for restaurants", live=True, **general):
    s = defaults(opener_holdout_share=0.0, **general)
    sdk = FakeSDK(text=json.dumps({"phrase": phrase}), input_tokens=400, output_tokens=60)
    ctx = make_context(s, now=NOW, claude_sdk=sdk, live=live)
    return ctx, sdk


def test_a_dry_run_asks_no_model_and_says_what_a_live_one_would_spend():
    ctx, sdk = focus_ctx(live=False, opener_focus=True)
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, ABOUT)
    assert op.arm == openers.NONE and sdk.calls == [] and ctx.store.select("credit_ledger") == []
    [note] = op.notes
    assert note.startswith("opener_focus: dry-run: not asked; a live run asks claude-sonnet-5-5, at most $0.00")


def test_the_focus_line_is_off_by_default_and_costs_nothing():
    ctx, sdk = focus_ctx()
    assert DEFAULT.general.opener_focus is False
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, ABOUT)
    assert op.arm == openers.NONE and sdk.calls == []


def test_the_focus_line_with_the_task_model_is_asked_once_and_logs_its_spend():
    ctx, sdk = focus_ctx(opener_focus=True)
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, ABOUT)
    assert (op.text, op.arm, op.source) == ("I came across Brightline and its work on payroll software for restaurants.",
                                            openers.OPENER, openers.FOCUS_SOURCE_NAME)
    [call] = sdk.calls
    assert call["model"] == "claude-sonnet-5-5" and call["max_tokens"] == openers.FOCUS_MAX_TOKENS
    assert call["output_config"]["effort"] == "low"
    assert "<keywords>\npayroll software, restaurants" in call["messages"][0]["content"]
    [spend] = ctx.store.select("credit_ledger", {"system": "claude"})
    assert spend["job"] == "opener_focus" and 0 < spend["usd"] < 0.005  # a fraction of a cent an account
    [stored] = ctx.store.select("signal_events", {"source": openers.FOCUS_SOURCE})
    assert stored["value"]["phrase"] == "payroll software for restaurants"
    # The stored phrase is used next time: no second call.
    again = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, [*ABOUT, stored])
    assert again.text == op.text and len(sdk.calls) == 1
    # A firing signal never uses it; nor does a held-out account, which costs no call.
    assert openers.for_account(ctx, acct(angle="Growing team"), {"role": FOUNDER}, [*ABOUT, *FUNDING]).source.startswith("Funding")
    ctx.settings = defaults(opener_focus=True, opener_holdout_share=1.0)
    held = openers.for_account(ctx, acct(account_id="acc-9", angle="General"), {"role": FOUNDER}, ABOUT)
    assert (held.arm, held.source) == (openers.HOLDOUT, openers.FOCUS_SOURCE_NAME) and len(sdk.calls) == 1


def test_the_monthly_cap_refuses_the_call_and_the_account_gets_none():
    ctx, sdk = focus_ctx(opener_focus=True)
    ctx.store.insert("credit_ledger", [{"entry_id": "e1", "system": "claude", "job": "copy_draft", "usd": 9.99,
                                        "credits": 0.0, "occurred_at": NOW}])
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, ABOUT)
    assert (op.text, op.arm) == ("", openers.NONE) and sdk.calls == []
    assert any("monthly Claude cap" in n for n in op.notes)
    assert ctx.store.select("signal_events", {"source": openers.FOCUS_SOURCE}) == []  # asked again next time


def test_a_phrase_that_fails_validation_is_stored_as_refused_and_never_used():
    ctx, sdk = focus_ctx("the leading payroll platform", opener_focus=True)
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, ABOUT)
    assert op.arm == openers.NONE and any("makes a claim (leading)" in n for n in op.notes)
    [stored] = ctx.store.select("signal_events", {"source": openers.FOCUS_SOURCE})
    assert stored["value"]["phrase"] == "" and stored["value"]["answer"] == "the leading payroll platform"
    openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, [*ABOUT, stored])
    assert len(sdk.calls) == 1  # not asked again while the refusal is fresh


def test_no_keywords_or_description_means_no_call():
    ctx, sdk = focus_ctx(opener_focus=True)
    assert openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, []).arm == openers.NONE and sdk.calls == []


@pytest.mark.parametrize("raw, why", [
    ("payroll and scheduling software for restaurants and bars and cafes", "10 words; at most 8"),
    ("award-winning payroll software", "makes a claim"),
    ("Toast payroll software", "not lower case"),
    ("payroll software for 500 restaurants", "digits or punctuation"),
    ("therapy for restaurant staff", 'says "therapy"'),
    ("brightline payroll", "names the company"),
    ("accounting software for dentists", "keywords and description do not (accounting, dentists)"),
    ("", "no phrase"),
])
def test_validation_drops_bad_phrases(raw, why):
    keywords = ["payroll software", "restaurants", "staff scheduling"]
    phrase, reason = openers.validate_focus(raw, keywords, "Payroll software for restaurants and bars.", "Brightline")
    assert phrase == "" and why in reason


def test_validation_keeps_a_grounded_lower_case_phrase():
    assert openers.validate_focus("A payroll software for restaurants.", ["payroll software", "restaurant"], "") == (
        "payroll software for restaurants", "")
    assert openers.validate_focus("HR software for restaurants", ["hr software", "restaurants"], "")[0] == \
        "HR software for restaurants"


def test_sourcing_stores_apollo_s_description_as_a_fact():
    org = {"organization_id": "o1", "estimated_num_employees": 40, "keywords": ["payroll"],
           "short_description": "  Brightline makes payroll\nsoftware for restaurants. " + "x" * 700}
    facts = {f["fact"]: f for f in apollo_universe.org_facts("acc-1", org, "TX", NOW)}
    about = facts["description"]["value"]
    assert about.startswith("Brightline makes payroll software for restaurants.") and len(about) <= 600
    assert facts["description"]["source"] == "apollo_org"


# -- the sheet: validation of the lines and the General keys -------------------------------------------------------


def signals_rows(**edits):
    rows = [dict(r) for r in default_tabs()["Signals"]]
    for r in rows:
        r.update(edits.get(r["signal"], {}))
    return rows


def errors_of(tab, rows):
    return [str(e) for e in validate_tab(tab, rows)[1]]


def test_an_unknown_token_is_an_error_with_a_did_you_mean():
    errs = errors_of("Signals", signals_rows(**{"Hiring and growth": {"opener_ops": "I saw {company} hired a {posting}."}}))
    assert len(errs) == 1 and "Signals row" in errs[0] and "opener_ops" in errs[0]
    assert "unknown token {posting} (did you mean {posting_title}?)" in errs[0]
    errs = errors_of("Signals", signals_rows(**{"EAP named": {"opener": "Saw your page mentions {company}."}}))
    assert "unknown token {company}" in errs[0] and "this column can use {evidence}" in errs[0]  # the plain opener
    errs = errors_of("Signals", signals_rows(**{"EAP named": {"opener_people": "I saw {{company}} offers an EAP."}}))
    assert "single braces" in errs[0]


def test_opener_self_is_only_for_a_signal_about_one_person():
    errs = errors_of("Signals", signals_rows(**{"Funding in the last 6 months": {"opener_self": "Congratulations."}}))
    assert len(errs) == 1 and "opener_self" in errs[0] and "people_leader_days_in_title" in errs[0]


def test_the_plain_opener_is_one_line_and_role_cells_may_hold_alternatives():
    errs = errors_of("Signals", signals_rows(**{"EAP named": {"opener": "One line.\nTwo lines."}}))
    assert "the plain opener is one line" in errs[0]
    got, errs = validate_tab("Signals", signals_rows())
    assert errs == []
    hiring = next(s for s in got if s.signal == "Hiring and growth")
    assert len(openers.lines_of(hiring.role_openers[OPS])) == 2


def test_a_sheet_without_the_new_columns_reads_them_as_blank():
    new = ("opener_people", "opener_founder", "opener_ops", "opener_self")
    rows = [{c: v for c, v in r.items() if c not in new} for r in default_tabs()["Signals"]]
    got, errs = validate_tab("Signals", rows)
    assert errs == [] and not any(s.role_openers or s.opener_self for s in got)


def test_the_general_keys():
    def general(key, value):
        rows = [dict(r) for r in default_tabs()["General"]]
        next(r for r in rows if r["key"] == key)["value"] = value
        return validate_tab("General", rows)

    g, errs = general("opener_holdout_share", "0.3")
    assert errs == [] and (g.opener_holdout_share, g.opener_focus) == (0.3, False)
    assert g.opener_focus_line == "I came across {company} and its work on {focus}."
    assert "is a share" in str(general("opener_holdout_share", "1.5")[1][0])
    assert "did you mean {focus}?" in str(general("opener_focus_line", "I saw {company} works on {foc}.")[1][0])
    assert "must use {focus}" in str(general("opener_focus_line", "I came across {company}.")[1][0])
    assert general("opener_focus", "yes")[0].opener_focus is True


# -- the copy desk: preview with a real line, the sheet check -----------------------------------------------------------


def test_preview_shows_a_real_filled_opener_for_the_sample_prospect(capsys):
    from us_outbound.ops import cli

    ctx = make_context(DEFAULT, now=NOW)
    argv = ["copy", "preview", "--synced", "--version", "general-founder-v1", "--opener", "--signal",
            "Funding in the last 6 months"]
    assert cli.main(argv, context_factory=lambda *a, **k: ctx) == 0
    out = capsys.readouterr().out
    assert "Opener: Congratulations on the Series A. (Funding in the last 6 months / opener_founder)" in out
    assert "Hi Dana,\n\nCongratulations on the Series A." in out
    text, source = copy_desk.sample_opener(DEFAULT, PEOPLE, "New People leader", leader=True)
    assert (text, source) == ("Congratulations on the new role at Harbor & Finch.", "New People leader / opener_self")
    with pytest.raises(ValueError, match="no Signals row"):
        copy_desk.sample_opener(DEFAULT, PEOPLE, "Nonsense")


def test_preview_of_a_stored_account_shows_the_opener_enrol_would_give_it(capsys):
    from us_outbound.ops import cli

    ctx = make_context(S, now=NOW)
    ctx.store.insert("accounts", [acct(angle="Growing team", status="verified")])
    ctx.store.insert("contacts", [contact(contact_id="con-1", account_id="acc-1", role=FOUNDER, title="CEO",
                                          email="omar@brightline.com", first_name="Omar")])
    ctx.store.insert("signal_events", FUNDING)
    assert cli.main(["copy", "preview", "--synced", "--account", "brightline.com"],
                    context_factory=lambda *a, **k: ctx) == 0
    out = capsys.readouterr().out
    assert "Opener: Congratulations on the Series A. (opener; Funding in the last 6 months / opener_founder)" in out
    assert "Hi Omar,\n\nCongratulations on the Series A." in out


def test_the_sheet_check_flags_a_line_that_breaks_the_rules(capsys):
    from us_outbound.ops import cli

    bad = dataclasses.replace(DEFAULT, signals=tuple(
        dataclasses.replace(s, role_openers={**s.role_openers, OPS: "I saw {company} offers unlimited PTO."})
        if s.signal == "Progressive benefits" else s for s in DEFAULT.signals))
    problems = copy_desk.check_openers(bad).problems
    assert problems == ["Progressive benefits, opener_ops: 'I saw Harbor & Finch offers unlimited PTO.' says "
                        "\"unlimited\": the copy rules never allow it"]
    ctx = make_context(bad, now=NOW)
    assert cli.main(["copy", "check", "--synced", "--version", "general-founder-v1"],
                    context_factory=lambda *a, **k: ctx) == 1
    assert "Signals opener: Progressive benefits, opener_ops" in capsys.readouterr().out


# -- the enrol job: the opener at enrol time, and the arm on the contact -----------------------------------------------


def test_enrol_fills_the_opener_for_the_contact_and_records_the_arm():
    from tests.test_enrol import instantly_posts, make

    signals = tuple(s for s in DEFAULT.signals if s.signal == "Funding in the last 6 months")
    s = dataclasses.replace(make_settings(live_sending=True), signals=signals)
    # acc-1 gets its opener; acc-3 is in the 30% holdout by its hash, so its email 1 has none.
    accounts = [acct(account_id=a, domain=f"{a}.com", clean_name=f"Bright {a[-1]}", tier="Standard", score=30,
                     angle="Growing team", status="verified", industry="Advertising agencies",
                     industry_group="Marketing & Creative Agencies") for a in ("acc-1", "acc-3")]
    people = [contact(contact_id=f"con-{a[-1]}", account_id=a, role=FOUNDER, title="CEO", email=f"omar@{a}.com",
                      first_name="Omar") for a in ("acc-1", "acc-3")]
    assert not openers.in_holdout("acc-1", 0.3) and openers.in_holdout("acc-3", 0.3)
    ctx, t = make(live=True, settings=s, accounts=accounts, contacts=people)
    ctx.store.insert("signal_events", [{**f, "account_id": a, "event_id": f"{f['event_id']}-{a}"}
                                       for a in ("acc-1", "acc-3") for f in FUNDING])
    out = enrol.run(ctx)
    assert out["enrolled"] == 2
    assert out["openers"] == {"arms": {"opener": 1, "holdout": 1},
                              "sources": {"Funding in the last 6 months / opener_founder": 2}}
    bodies = {lead["email"]: lead["custom_variables"]["s1_body"] for p in instantly_posts(t) for lead in p.json["leads"]}
    assert "<p>Hi Omar,</p><p>Congratulations on the Series A.</p>" in bodies["omar@acc-1.com"]
    assert "Series A" not in bodies["omar@acc-3.com"] and "<p>Hi Omar,</p><p>In most agencies" in bodies["omar@acc-3.com"]
    got = {c["contact_id"]: (c["opener_arm"], c["opener_source"]) for c in ctx.store.select("contacts")}
    assert got == {"con-1": ("opener", "Funding in the last 6 months / opener_founder"),
                   "con-3": ("holdout", "Funding in the last 6 months / opener_founder")}
