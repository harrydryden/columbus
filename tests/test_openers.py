"""Tokenized openers (enrol/openers.py; docs/roadmap.md §4 item 2; Harry, 2 Oct 2026): every signal's line for
each copy role filled from stored facts, the fallbacks, the contact who is the new People leader, title cleaning
and numbers, the holdout, the optional "what they do" phrase, the generic line, validation of the sheet's lines,
the copy desk's preview and check, and the enrol job recording which arm each account was in. Harry, 2 Oct 2026:
signals are context, never the line, and no opener mentions funding or money. Fakes only."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from tests.fakes import make_context
from tests.test_client_claude import FakeSDK
from tests.test_render import contact, make_settings
from us_outbound.enrol import copy_desk, copy_rules, enrol, hand_check, openers, queue, render
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


def with_lines(settings, signal, **cells):
    """settings with one Signals row's cells replaced: opener, opener_self, or a copy role's line (role=line)."""
    roles = {"people": PEOPLE, "founder": FOUNDER, "ops": OPS}
    plain = {k: v for k, v in cells.items() if k in ("opener", "opener_self")}
    lines = {roles[k]: v for k, v in cells.items() if k in roles}
    return dataclasses.replace(settings, signals=tuple(
        dataclasses.replace(x, role_openers={**x.role_openers, **lines}, **plain) if x.signal == signal else x
        for x in settings.signals))


LEADER = [fact("apollo_people", "people_leader_count", 1), fact("apollo_people", "people_leader_days_in_title", 40),
          fact("apollo_people", "people_leader_newest",
               {"apollo_person_id": "p-dana", "title": "HEAD OF PEOPLE", "days_in_title": 40})]
FIRST_HIRE = [fact("apollo_jobs", "open_people_roles", 1), fact("apollo_jobs", "open_roles", 1),
              fact("apollo_jobs", "posting_titles", ["People Operations Manager (Remote)"]),
              fact("apollo_people", "people_leader_count", 0)]
PEOPLE_ROLE = [fact("apollo_jobs", "open_people_roles", 1), fact("apollo_jobs", "open_roles", 2),
               fact("apollo_jobs", "posting_titles", ["Account Executive", "HR Generalist - Austin, TX"])]
FUNDING = [fact("apollo_org", "days_since_funding", 60), fact("apollo_org", "funding_stage", "Series A")]
FUNDING_OLDER = [fact("apollo_org", "days_since_funding", 300), fact("apollo_org", "funding_stage", "Seed")]
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

# The context signals' default lines (Harry, 2 Oct 2026: signals are context, never the line): the pressure the
# situation tends to bring, then support through it, and nothing that was observed. "" is the plain opener.
NEW_LEADER_LINES = {
    "": "Setting a team's priorities takes time, and support for people is often one of the first things to get right.",
    PEOPLE: "Setting new priorities tends to fill the first months, and support helps most when it's easy to roll out.",
    FOUNDER: "Getting support for a team right usually takes months, and it helps to have something people can use "
             "right away.",
    OPS: "A new set of people priorities usually brings new processes too, and it helps when support is the simple part.",
    "self": "Shaping how a team is supported is often a lot to carry, and it helps when one piece is simple.",
}
PEOPLE_ROLE_LINES = {  # First People hire and People role open
    "": "People issues tend to land on a few busy desks, and support helps most when it shares the load.",
    PEOPLE: "People issues tend to pile up on whoever handles them, and it helps when support doesn't rest on one desk.",
    FOUNDER: "People issues tend to find the busiest desk, and it helps when support doesn't depend on one person.",
    OPS: "People questions tend to pile up alongside everything else, and support helps most when it takes no extra "
         "admin.",
}
HIRING_LINES = {
    "": "When many people join at once, managers and culture tend to feel the stretch, and support helps most early.",
    PEOPLE: "When many people join at once, managers are often stretched thin, and support matters most in those first "
            "months.",
    FOUNDER: "When many people join at once, culture tends to come under strain, and support helps people settle in.",
    OPS: "When many people join at once, onboarding often fills the week, and it helps when support is easy to reach.",
}
FUNDING_LINES = {  # both funding rows
    "": "Times of change tend to put more on everyone's plate, and that's when support matters most.",
    PEOPLE: "The pressure of change often lands on managers first, and support helps most when it comes early.",
    FOUNDER: "Change tends to test a culture as much as a plan, and that's when taking care of people matters most.",
    OPS: "Change often brings new routines and more to coordinate, and support works best when it's already in place.",
}
# The generic line (General opener_generic_*): ever more pressure in our work and personal lives.
GENERIC = {
    PEOPLE: "Pressure at work and at home seems to keep rising, and the two rarely stay separate for long.",
    FOUNDER: "Work and life both seem to ask more of people every year, and even the strongest teams feel it.",
    OPS: "Pressure in work and life seems to keep rising, and it hardly ever waits for a convenient week.",
    "": "Pressure at work and at home seems to keep rising, and most teams feel it somewhere.",
}
GENERIC_KEY = {PEOPLE: "opener_generic_people", FOUNDER: "opener_generic_founder", OPS: "opener_generic_ops",
               "": "opener_generic"}
CONTEXT_SIGNALS = ("New People leader", "First People hire", "People role open", "Funding in the last 6 months",
                   "Funding 6–12 months ago", "Hiring and growth")
COLUMN = {PEOPLE: "opener_people", FOUNDER: "opener_founder", OPS: "opener_ops"}
MONEY = copy_rules.MONEY_ADVICE

# The token machinery stays for lines Harry writes (style.md, "Tokens"), though the context signals' defaults use
# none of it: the title, count and growth tests give these signals the tokenized lines they had before 2 Oct.
TOKEN_LINES = {
    "New People leader": {
        PEOPLE: "I saw {company} recently named a new {people_title}.",
        FOUNDER: "I saw {company} has a new {people_title} in place.",
        OPS: "I saw there's a new {people_title} at {company}.",
    },
    "First People hire": {
        PEOPLE: "I saw {company} is hiring a {people_title}.",
        FOUNDER: "I saw {company} is recruiting a {people_title}.",
        OPS: "I saw {company} is looking to hire a {people_title}.",
    },
    "People role open": {
        PEOPLE: "I saw {company} is adding a {people_title} to the team.",
        FOUNDER: "I saw {company} is hiring a {people_title} right now.",
        OPS: "I saw {company} has a {people_title} role open.",
    },
    "Hiring and growth": {
        PEOPLE: "I saw {company} has {open_roles} roles open, including a {posting_title}.\n"
                "I saw {company} has {open_roles} roles open right now.\n"
                "I saw the team at {company} has {growth} in the last year.",
        FOUNDER: "I saw {company} is hiring for {open_roles} roles, including a {posting_title}.\n"
                 "I saw {company} is hiring for {open_roles} roles right now.\n"
                 "I saw {company} has {growth} in headcount over the last year.",
        OPS: "I saw {company} is recruiting for {open_roles} roles, including a {posting_title}.\n"
             "I saw {company} is recruiting for {open_roles} roles right now.\n"
             "I saw the {company} team has {growth} over the past year.",
    },
}
T = dataclasses.replace(S, signals=tuple(
    dataclasses.replace(x, role_openers={**x.role_openers, **TOKEN_LINES[x.signal]}) if x.signal in TOKEN_LINES else x
    for x in S.signals))


# -- every signal × role (the default lines, filled from stored facts) ----------------------------------------


CONTEXT_CASES = [
    (facts, signal, role, lines[role])
    for facts, signal, lines in (
        (LEADER, "New People leader", NEW_LEADER_LINES), (FIRST_HIRE, "First People hire", PEOPLE_ROLE_LINES),
        (PEOPLE_ROLE, "People role open", PEOPLE_ROLE_LINES), (FUNDING, "Funding in the last 6 months", FUNDING_LINES),
        (FUNDING_OLDER, "Funding 6–12 months ago", FUNDING_LINES), (HIRING, "Hiring and growth", HIRING_LINES),
        (GROWTH, "Hiring and growth", HIRING_LINES),
    )
    for role in (PEOPLE, FOUNDER, OPS)
]


@pytest.mark.parametrize("facts, signal, role, line", [
    *CONTEXT_CASES,
    (EAP, "EAP named", PEOPLE, "I saw Brightline offers its team an employee assistance program through ComPsych."),
    (EAP, "EAP named", FOUNDER, "I saw Brightline offers an employee assistance program through ComPsych."),
    (EAP, "EAP named", OPS, "I saw Brightline provides an employee assistance program through ComPsych."),
    (MENTAL_HEALTH, "Mental health support listed", PEOPLE, "I saw Brightline mentions mental health on its careers page."),
    (MENTAL_HEALTH, "Mental health support listed", FOUNDER, "I saw Brightline mentions mental health when it recruits."),
    (MENTAL_HEALTH, "Mental health support listed", OPS, "I saw Brightline talks about mental health on its careers page."),
    (APP, "Wellbeing app or perk named", PEOPLE, "I saw Brightline offers Headspace as part of its benefits."),
    (APP, "Wellbeing app or perk named", FOUNDER, "I saw Headspace is one of the perks at Brightline."),
    (APP, "Wellbeing app or perk named", OPS, "I saw Brightline includes Headspace in its benefits."),
    (BENEFITS, "Progressive benefits", PEOPLE, "I saw Brightline lists parental leave among its benefits."),
    (BENEFITS, "Progressive benefits", FOUNDER, "I saw Brightline offers parental leave as part of its benefits."),
    (BENEFITS, "Progressive benefits", OPS, "I saw the benefits at Brightline include parental leave."),
])
def test_each_signal_and_role_fills_from_the_account_s_facts(facts, signal, role, line):
    op = opener(facts, role)
    assert (op.text, op.arm, op.source) == (line, openers.OPENER, f"{signal} / {COLUMN[role]}")
    assert check(op.text) == "" and len(op.text.split()) <= 20  # style.md: one sentence, under about 20 words


def test_the_default_lines_cover_every_signal_that_can_fire_and_pass_the_rules():
    with_lines = {s.signal for s in DEFAULT.signals if s.role_openers}
    assert with_lines == {"New People leader", "First People hire", "People role open", "Funding in the last 6 months",
                          "Funding 6–12 months ago", "Hiring and growth", "Mental health support listed", "EAP named",
                          "Wellbeing app or perk named", "Progressive benefits"}
    assert all(set(s.role_openers) == {PEOPLE, FOUNDER, OPS} for s in DEFAULT.signals if s.role_openers)
    assert [s.signal for s in DEFAULT.signals if s.opener_self] == ["New People leader"]
    assert copy_desk.check_openers(DEFAULT).ok
    banned = ("therap", "licensed", "unlimited", "%", "noticed you're", "!")
    lines = openers.sample_lines(DEFAULT)
    assert {(sig, col) for sig, col, _, _ in lines if sig == openers.GENERAL_TAB} == {
        ("General", k) for k in (*GENERIC_KEY.values(), "opener_focus_line")}  # the General tab's lines too
    for _, _, line, filled in lines:
        assert not any(b in line.lower() for b in banned), line
        assert filled is None or filled.count(".") == 1 and filled.endswith("."), filled  # one sentence


def test_signals_are_context_never_the_line():
    """Harry, 2 Oct 2026: the hiring, People, growth and funding signals' lines, and the generic line, speak to the
    pressure the situation tends to bring: no word naming what was observed, no title or count, no token at all."""
    observed = ("hiring", "hire ", "recruit", "growth", "growing", "grown", "scaling", "headcount", "funding", "fund",
                "money", "round", "series", "raise", "investor", "congratulat", "new role", "{", "your team is")
    by = {s.signal: s for s in DEFAULT.signals}
    for name in CONTEXT_SIGNALS:
        s = by[name]
        for line in (s.opener, *s.role_openers.values(), s.opener_self):
            assert not any(w in line.lower() for w in observed), (name, line)
            assert line == "" or line.count(".") == 1 and len(line.split()) <= 20, (name, line)
    assert by["Funding 6–12 months ago"].role_openers == by["Funding in the last 6 months"].role_openers
    assert by["First People hire"].role_openers == by["People role open"].role_openers
    for role, key in GENERIC_KEY.items():
        line = getattr(DEFAULT.general, key)
        assert line == GENERIC[role] and not any(w in line.lower() for w in observed), line


# -- fallbacks: the next line, the plain opener, the generic line, none -----------------------------------------


def test_a_line_whose_token_has_no_fact_falls_back_to_the_next_line_then_the_plain_opener():
    # Hiring and growth fired on growth alone: the open-roles line has no {open_roles}, so the growth line.
    op = opener(GROWTH, FOUNDER, settings=T)
    assert op.source == "Hiring and growth / opener_founder" and "grown by about a third" in op.text
    assert op.notes == ("Hiring and growth, opener_founder line 1: no {open_roles}, {posting_title}",
                        "Hiring and growth, opener_founder line 2: no {open_roles}")
    # Six roles but no posting that cleans to a title: the roles-only middle line keeps the count (copy QA).
    junk = [fact("apollo_jobs", "open_roles", 6), fact("apollo_jobs", "posting_titles", ["Summer Intern", "R-1234"])]
    op = opener(junk, OPS, settings=T)
    assert (op.text, op.source) == ("I saw Brightline is recruiting for six roles right now.",
                                    "Hiring and growth / opener_ops")
    assert op.notes == ("Hiring and growth, opener_ops line 1: no {posting_title}",)
    # A count past 99 is not believed, and there is no growth: the signal's plain opener.
    op = opener([fact("apollo_jobs", "open_roles", 150)], OPS, settings=T)
    assert (op.text, op.source) == (HIRING_LINES[""], "Hiring and growth / opener")
    assert op.notes == ("Hiring and growth, opener_ops line 1: no {open_roles}, {posting_title}",
                        "Hiring and growth, opener_ops line 2: no {open_roles}",
                        "Hiring and growth, opener_ops line 3: no {growth}")


def test_a_token_that_fails_its_check_is_missing():
    # The only posting is refused by the copy rules, and growth below 10% is not worth a line: the count alone.
    few = [fact("apollo_jobs", "open_roles", 3), fact("apollo_jobs", "posting_titles", ["Licensed Therapist"]),
           fact("apollo_org", "headcount_growth_12m", 0.04)]
    assert opener(few, PEOPLE, settings=T).text == "I saw Brightline has three roles open right now."
    many = [fact("apollo_jobs", "open_roles", 14), fact("apollo_jobs", "posting_titles", POSTINGS)]
    assert opener(many, PEOPLE, settings=T).text == "I saw Brightline has 14 roles open, including a Senior Product Designer."


def test_a_filled_line_that_breaks_a_copy_rule_falls_back():
    # Evidence the copy may not use ("therapy") drops every line and the plain line: the generic line.
    therapy = [fact("clay_careers", "benefit", {"item": "Free therapy sessions"})]
    op = opener(therapy, OPS)
    assert (op.text, op.arm, op.source) == (GENERIC[OPS], openers.OPENER, "opener_generic_ops")
    assert any("therapy" in n for n in op.notes)
    # A line that fills but breaks a rule is passed over for the next.
    s = with_lines(S, "Progressive benefits", ops="I saw {company} offers unlimited {evidence}.\n"
                                                  "I saw the benefits at {company} include {evidence}.")
    op = opener(BENEFITS, OPS, settings=s)
    assert op.text == "I saw the benefits at Brightline include parental leave."
    assert 'says "unlimited"' in op.notes[0]


def test_british_evidence_is_spelled_the_american_way():
    wellbeing = [fact("clay_careers", "benefit", {"item": "Wellbeing support for everyone"})]
    assert opener(wellbeing, FOUNDER).text == "I saw Brightline mentions well-being support when it recruits."


def test_no_signal_line_and_no_plain_opener_means_the_generic_line_and_with_none_of_those_none():
    s = with_lines(T, "Hiring and growth", opener="")
    op = opener([fact("apollo_jobs", "open_roles", 150)], OPS, settings=s)
    assert (op.text, op.arm, op.source) == (GENERIC[OPS], openers.OPENER, "opener_generic_ops")
    assert op.notes[-1] == "Hiring and growth, opener_ops line 3: no {growth}"  # the signal's notes are kept
    s = dataclasses.replace(s, general=dataclasses.replace(s.general, opener_generic_ops="", opener_generic=""))
    op = opener([fact("apollo_jobs", "open_roles", 150)], OPS, settings=s)
    assert (op.text, op.arm, op.source) == ("", openers.NONE, "")


def test_the_general_angle_and_control_get_the_generic_line_never_a_signal_line():
    # A Control account is signal-blind (SPEC 5): General angle, so the generic line, even with funding facts.
    op = opener(FUNDING, FOUNDER, tier="Control", angle="General")
    assert (op.text, op.arm, op.source) == (GENERIC[FOUNDER], openers.OPENER, "opener_generic_founder")
    assert opener([], FOUNDER).source == "opener_generic_founder"  # no signal at all


def test_old_facts_the_signal_no_longer_counts_are_not_quoted():
    # The funding round fires (days are aged), but the posting titles from 120 days ago are past the
    # Hiring signal's window: they never fill a line about the account today.
    stale = [*FUNDING, fact("apollo_jobs", "open_roles", 6, days=120), fact("apollo_jobs", "posting_titles", POSTINGS, days=120)]
    assert opener(stale, PEOPLE).source == "Funding in the last 6 months / opener_people"
    s = next(x for x in S.signals if x.signal == "Hiring and growth")
    assert "posting_title" not in openers.tokens(acct(), s, None, stale, S, TODAY)


def test_the_page_token_says_where_the_evidence_was_read():
    def mh(source, url=""):
        return [fact(source, "benefit", {"item": "Mental health support"}, quote="We care about mental health.")
                | {"source_url": url}]

    assert opener(mh("careers_pages", "https://brightline.com/careers"), PEOPLE).text == \
        "I saw Brightline mentions mental health on its careers page."
    assert opener(mh("careers_pages", "https://brightline.com/benefits"), OPS).text == \
        "I saw Brightline talks about mental health on its benefits page."
    assert opener(mh("job_posts"), PEOPLE).text == "I saw Brightline mentions mental health in its job postings."


# -- funding is a signal, never a line: no opener mentions funding or money (Harry, 2 Oct 2026) ---------------------


@pytest.mark.parametrize("line, word", [
    ("I saw Brightline recently raised a Series A.", "raised"),
    ("Congratulations on the Series A.", "Series A"),
    ("I saw Brightline closed a seed round.", "seed round"),
    ("I saw your company recently took on new funding.", "funding"),
    ("Congratulations on the new round.", "round"),
    ("I saw Brightline was funded last year.", "funded"),
    ("I saw Brightline is fundraising again.", "fundraising"),
    ("I saw Brightline is backed by Sequoia.", "backed"),
    ("I saw new investors joined Brightline.", "investors"),
    ("I saw the investment in Brightline.", "investment"),
    ("I saw Brightline has fresh capital.", "capital"),
    ("I saw news of the valuation.", "valuation"),
    ("Congratulations on the IPO.", "IPO"),
    ("Money tends to be tight for most teams.", "Money"),
    ("I saw Brightline closed $8M.", "$8M"),
    ("Congratulations on the {funding_stage}.", "{funding_stage}"),
])
def test_the_money_rule_names_each_mention_of_funding_or_money(line, word):
    got = copy_rules.money_violations(line)
    assert got and got[0] == f'says "{word}"; {MONEY}'
    assert render.pick_opener(line)[0] == "" and f'says "{word}"; {MONEY}' in render.pick_opener(line)[1]


def test_the_money_rule_lets_ordinary_words_through():
    for line in ("Change often brings a series of new routines.", "I saw Brightline is adding a Finance Manager.",
                 "I saw Brightline mentions seed technicians when it recruits.", "Teams around the country feel it.",
                 "I saw Brightline offers refunds.", "Support is fundamental."):
        assert copy_rules.money_violations(line) == [], line
    # A company's own name is not our wording.
    assert copy_rules.money_violations("I saw Summit Capital mentions counseling.", exempt=("Summit Capital",)) == []
    assert render.pick_opener("I saw Summit Capital mentions counseling.", exempt=("Summit Capital",))[1] == ""


def test_a_funding_line_on_the_sheet_is_refused_by_copy_check(capsys):
    from us_outbound.ops import cli

    bad = with_lines(DEFAULT, "Funding in the last 6 months", opener="I saw your company recently took on new funding.",
                     founder="Congratulations on the {funding_stage}.\nI saw {company} recently raised money.")
    problems = copy_desk.check_openers(bad).problems
    assert problems == [
        f"Funding in the last 6 months, opener: 'I saw your company recently took on new funding.' says \"funding\"; {MONEY}",
        # As written: the retired token never fills, so the line is read as it is on the sheet.
        f"Funding in the last 6 months, opener_founder: 'Congratulations on the {{funding_stage}}.' says "
        f"\"{{funding_stage}}\"; {MONEY}",
        # As filled for the sample prospect.
        f"Funding in the last 6 months, opener_founder: 'I saw Harbor & Finch recently raised money.' says \"raised\"; "
        f"{MONEY}; says \"money\"; {MONEY}",
    ]
    ctx = make_context(bad, now=NOW)
    assert cli.main(["copy", "check", "--synced", "--version", "general-founder-v1"],
                    context_factory=lambda *a, **k: ctx) == 1
    out = capsys.readouterr().out
    assert "Opener: Funding in the last 6 months, opener_founder: 'Congratulations on the {funding_stage}.'" in out
    assert "funding is a signal, never a line" in out


def test_a_sheet_that_still_has_the_old_funding_lines_loads_and_never_sends_them():
    """The Signals tab in force may still hold the 1 Oct lines: it validates, so settings stay usable, but the
    retired token never fills and a money word never passes the check, so those accounts get the plain line."""
    rows = signals_rows(**{"Funding in the last 6 months": {
        "opener": "I saw your company recently took on new funding.",
        "opener_founder": "Congratulations on the {funding_stage}.",
        "opener_people": "I saw {company} recently raised a {funding_stage}.\nI saw {company} raised money recently.",
    }})
    got, errs = validate_tab("Signals", rows)
    assert errs == []
    s = dataclasses.replace(S, signals=tuple(got))
    op = opener(FUNDING, FOUNDER, settings=s)  # no {funding_stage}, and the plain line says "funding": generic
    assert (op.text, op.source) == (GENERIC[FOUNDER], "opener_generic_founder")
    assert op.notes[0] == "Funding in the last 6 months, opener_founder line 1: no {funding_stage}"
    assert op.notes[1] == f'Funding in the last 6 months opener: says "funding"; {MONEY}'
    op = opener(FUNDING, PEOPLE, settings=s)
    assert op.source == "opener_generic_people" and f'says "raised"; {MONEY}' in op.notes[1]
    funding = next(x for x in s.signals if x.signal == "Funding in the last 6 months")
    assert "funding_stage" not in openers.tokens(acct(), funding, None, FUNDING, s, TODAY)  # retired


def test_a_rendered_opener_that_mentions_money_is_passed_over():
    s = with_lines(S, "Funding in the last 6 months",
                   founder="I saw {company} recently raised money.\nChange tends to ask a lot of a team.")
    op = opener(FUNDING, FOUNDER, settings=s)
    assert op.text == "Change tends to ask a lot of a team." and f'says "raised"; {MONEY}' in op.notes[0]
    # The enrol job's own check, as the line will be sent.
    assert render.pick_opener("Congratulations on the Series A.") == ("", f'says "Series A"; {MONEY}')
    # A posting title that mentions money is never named; the next one is.
    assert openers.best_posting(["Capital Markets Associate", "Account Executive"]) == "Account Executive"
    assert openers.usable_title("Investment Analyst") is None


def test_email_bodies_may_still_say_funding():
    """The money rule is the opener's alone: nonprofit copy says "funding cycles" and fintech copy "a long
    fundraise", and the copy check passes them."""
    rows = {c.copy_version: c for c in DEFAULT.copy}
    for version, words in (("nonprofits-ops-v1", "funding cycles"), ("fintech-founder-v1", "fundraise")):
        row = rows[version]
        assert words in row.step(1).body
        problems = copy_desk.check_row(row, DEFAULT).problems
        assert not any("funding or money" in p for p in problems), problems
    words = "Hi Dana,\n\nIn a nonprofit, caseloads grow while funding cycles shrink and money is tight.\n\nBest wishes,\nHarry"
    got = copy_rules.email_violations("Subject", words, [], step=3, demo_url="", sender_is_harry=True)
    assert not any("funding or money" in v for v in got)


# -- the generic line (General opener_generic_*; Harry, 2 Oct 2026) ------------------------------------------------


def test_an_account_with_no_signal_line_gets_the_generic_line_for_the_contact_s_role():
    for role in (PEOPLE, FOUNDER, OPS):
        op = opener([], role)
        assert (op.text, op.arm, op.source) == (GENERIC[role], openers.OPENER, GENERIC_KEY[role])
        assert check(op.text) == "" and len(op.text.split()) <= 20
    # A contact with no copy role, or a role line left blank, gets the plain generic line.
    assert (opener([], "").text, opener([], "").source) == (GENERIC[""], "opener_generic")
    assert opener([], OPS, settings=defaults(opener_holdout_share=0.0, opener_generic_ops="")).text == GENERIC[""]
    # Tokens fill from the account; a line that breaks a rule is passed over for the plain one.
    s = defaults(opener_holdout_share=0.0, opener_generic_founder="Pressure seems to keep rising in {city} too.",
                 opener_generic_people="Pressure keeps rising, so let's book a call.")
    assert opener([], FOUNDER, settings=s).text == "Pressure seems to keep rising in Austin too."
    op = opener([], PEOPLE, settings=s)
    assert op.text == GENERIC[""] and op.notes[0].startswith('opener_generic_people: says "book"')


def test_the_holdout_records_the_generic_line_it_would_have_had():
    op = opener([], FOUNDER, settings=defaults(opener_holdout_share=1.0))
    assert (op.text, op.arm, op.source, op.would_be) == ("", openers.HOLDOUT, "opener_generic_founder", GENERIC[FOUNDER])


def test_the_general_tab_validates_the_generic_lines():
    def general(key, value):
        rows = [dict(r) for r in default_tabs()["General"]]
        row = next((r for r in rows if r["key"] == key), None)
        if row is None:
            rows.append(row := {"key": key, "value": "", "note": ""})
        row["value"] = value
        return validate_tab("General", rows)

    g, errs = general("opener_generic_ops", GENERIC[OPS])
    assert errs == [] and (g.opener_generic_people, g.opener_generic) == (GENERIC[PEOPLE], GENERIC[""])
    assert "did you mean {company}?" in str(general("opener_generic_ops", "Pressure is rising at {compny}.")[1][0])
    assert "is one line" in str(general("opener_generic", "One.\nTwo.")[1][0])
    err = str(general("opener_generic_founder", "Congratulations on the new round.")[1][0])
    assert f'says "round"; {MONEY}' in err
    assert general("opener_generic", "")[0].opener_generic == ""  # blank: no plain generic line
    # A sheet without the new keys reads the build's lines.
    rows = [r for r in default_tabs()["General"] if not r["key"].startswith("opener_generic")]
    assert validate_tab("General", rows)[0].opener_generic_ops == GENERIC[OPS]


def test_the_hand_check_marks_the_generic_line():
    a = {"opener": GENERIC[OPS], "opener_arm": openers.OPENER, "opener_source": "opener_generic_ops"}
    assert hand_check._opener(a) == f"{GENERIC[OPS]} (the generic line)"
    assert hand_check._opener({**a, "opener_arm": openers.HOLDOUT}) == f"none (held out; would be: {GENERIC[OPS]} (the generic line))"
    assert hand_check._opener({**a, "opener_source": "EAP named / opener_ops"}) == GENERIC[OPS]


# -- the copy QA's code findings (2 Oct 2026), each with the QA's samples ------------------------------------------


@pytest.mark.parametrize("raw, clean", [
    ("VP, People Operations", "VP of People Operations"),
    ("Director, Human Resources", "Director of Human Resources"),
    ("SVP, Engineering", "SVP of Engineering"),
    ("Head, People", "Head of People"),
    ("Senior Manager, People & Culture", "Senior Manager of People & Culture"),
    ("Account Executive, Mid-Market", "Account Executive"),  # the first part is a whole title: kept as it was
    ("Product Manager, Growth", "Product Manager"),
    ("VP, New York", None),  # a seniority and a place: no function to name
    ("VP", None),
    ("Senior", None),
])
def test_bug1_a_seniority_takes_its_function_with_of_or_the_title_is_dropped(raw, clean):
    assert openers.clean_title(raw) == clean


def test_bug1_the_rendered_lines_name_the_function():
    leader = [*LEADER[:2], fact("apollo_people", "people_leader_newest",
                                {"apollo_person_id": "p-dana", "title": "Director, Human Resources", "days_in_title": 40})]
    assert opener(leader, OPS, settings=T).text == "I saw there's a new Director of Human Resources at Brightline."
    hiring = [*FIRST_HIRE[:2], fact("apollo_jobs", "posting_titles", ["VP, People Operations"]), FIRST_HIRE[3]]
    assert opener(hiring, PEOPLE, settings=T).text == "I saw Brightline is hiring a VP of People Operations."


def test_bug2_organisational_is_british_and_never_reaches_email_1():
    assert copy_rules.content_violations("Organisational Development Lead") == [
        'has the British spelling "Organisational" (American: "organizational")']
    assert copy_rules.content_violations("organizational development") == []
    assert openers.clean_title("Organisational Development Lead") is None
    six = [fact("apollo_jobs", "open_roles", 6), fact("apollo_jobs", "posting_titles", ["Organisational Development Lead"])]
    assert opener(six, PEOPLE, settings=T).text == "I saw Brightline has six roles open right now."


@pytest.mark.parametrize("raw, clean", [
    ("Sr. Software Engineer II - Payments", "Senior Software Engineer"),
    ("Software Engineer III", "Software Engineer"),
    ("Jr. Designer", "Junior Designer"),
    ("Office Mgr", "Office Manager"),
    ("Exec Asst", "Exec Assistant"),
    ("Data Analyst Level 2", "Data Analyst"),
    ("Software Engineer L5", "Software Engineer"),
    ("Account Executive 2", "Account Executive"),
])
def test_bug3_abbreviations_are_written_out_and_levels_dropped(raw, clean):
    assert openers.clean_title(raw) == clean


def test_bug3_the_rendered_line_has_no_mid_sentence_full_stop():
    six = [fact("apollo_jobs", "open_roles", 6), fact("apollo_jobs", "posting_titles", ["Sr. Software Engineer II - Payments"])]
    assert opener(six, PEOPLE, settings=T).text == "I saw Brightline has six roles open, including a Senior Software Engineer."


def test_bug4_the_posting_named_is_one_email_1_can_carry():
    assert openers.best_posting(["Call Center Agent", "Account Executive"]) == "Account Executive"
    assert openers.best_posting(["Demo Engineer", "Meeting Planner", "Booking Coordinator"]) is None
    calls = [fact("apollo_jobs", "open_roles", 5), fact("apollo_jobs", "posting_titles", ["Call Center Agent", "Account Executive"])]
    assert opener(calls, PEOPLE, settings=T).text == "I saw Brightline has five roles open, including an Account Executive."
    # A People posting the email-1 rules block is passed over the same way.
    people = [*PEOPLE_ROLE[:2], fact("apollo_jobs", "posting_titles", ["HR Call Center Lead", "HR Generalist"])]
    assert opener(people, OPS, settings=T).text == "I saw Brightline has an HR Generalist role open."


def test_bug5_evidence_skips_a_term_the_copy_rules_block():
    therapy = [fact("careers_pages", "benefit", {"item": "Therapy and counseling covered"})]
    assert opener(therapy, PEOPLE).text == "I saw Brightline mentions counseling on its careers page."
    pto = [fact("careers_pages", "benefit", {"item": "Unlimited PTO and parental leave"})]
    assert opener(pto, PEOPLE).text == "I saw Brightline lists parental leave among its benefits."
    # The plain opener (a contact with no copy role) takes the same evidence.
    assert opener(pto, "").text == "I noticed your benefits include parental leave."
    # {provider} skips blocked terms too: a provider whose name breaks a rule is never named.
    signal = next(x for x in S.signals if x.signal == "EAP named")
    from us_outbound.scoring.score import Evidence, Match

    match = Match(signal, 5, [Evidence("Unlimited Care", term="Unlimited Care"), Evidence("ComPsych", term="ComPsych")])
    got = openers.tokens(acct(), signal, match, [], S, TODAY)
    assert (got["evidence"], got["provider"]) == ("ComPsych", "ComPsych")


@pytest.mark.parametrize("item, shown", [
    ("A wellness stipend every month", "wellness stipends"),
    ("Wellness stipends", "wellness stipends"),
    ("A paid sabbatical after five years", "sabbaticals"),
    ("We work a four-day week", "four-day weeks"),
    ("Two mental health days a quarter", "mental health days"),
    ("Paid parental leave", "parental leave"),
])
def test_bug6_benefit_evidence_is_plural_or_mass(item, shown):
    facts = [fact("careers_pages", "benefit", {"item": item})]
    op = opener(facts, OPS)
    if shown == "mental health days":
        assert op.source.startswith("Mental health support listed")  # the broader signal wins, as the QA noted
        return
    assert op.text == f"I saw the benefits at Brightline include {shown}."
    assert opener(facts, FOUNDER).text == f"I saw Brightline offers {shown} as part of its benefits."
    assert opener(facts, "").text == f"I noticed your benefits include {shown}."


def test_bug7_ambiguous_eap_provider_names_count_only_near_eap_words():
    from us_outbound.scoring.score import match_signal

    eap = next(x for x in S.signals if x.signal == "EAP named")
    assert {"magellan", "telus health"} <= set(eap.context)

    def page(text):
        return [fact("careers_pages", "benefit", {"item": text})]

    assert match_signal(eap, page("Our Magellan platform team ships weekly."), TODAY) is None
    assert match_signal(eap, page("We partner with TELUS Health for virtual care."), TODAY) is None
    assert opener(page("Our Magellan platform team ships weekly."), PEOPLE).source == "opener_generic_people"
    assert opener(page("Our employee assistance program is run by Magellan."), PEOPLE).text == \
        "I saw Brightline offers its team an employee assistance program through Magellan."
    assert match_signal(eap, page("TELUS Health runs our EAP for every employee."), TODAY) is not None


# -- the contact who is the new People leader --------------------------------------------------------------------


def picked(contact_id="con-dana", person="p-dana"):
    return fact("pick_contacts", "contact_pick", {"outcome": "picked", "contact_id": contact_id, "apollo_person_id": person})


def test_the_new_people_leader_gets_their_own_line_and_everyone_else_the_role_s():
    dana = {"contact_id": "con-dana", "role": PEOPLE}
    op = opener([*LEADER, picked()], person=dana)
    assert (op.text, op.source) == (NEW_LEADER_LINES["self"], "New People leader / opener_self")
    assert "congratulat" not in op.text.lower()  # warm, without telling them we watched their start date
    # Another People person at the account (a different Apollo person) gets the People leader's line.
    op = opener([*LEADER, picked(person="p-sam")], person=dana)
    assert (op.text, op.source) == (NEW_LEADER_LINES[PEOPLE], "New People leader / opener_people")
    # Without a stored person id the contact is never assumed to be the leader, whatever the title.
    assert opener(LEADER, person={**dana, "title": "Head of People"}).source == "New People leader / opener_people"
    # The id may be on the contact itself (a later Clay contact, say).
    assert opener(LEADER, person={**dana, "apollo_person_id": "p-dana"}).source == "New People leader / opener_self"


def test_without_a_self_line_the_leader_gets_the_plain_opener():
    s = with_lines(S, "New People leader", opener_self="")
    op = opener([*LEADER, picked()], person={"contact_id": "con-dana", "role": PEOPLE}, settings=s)
    assert (op.text, op.source) == (NEW_LEADER_LINES[""], "New People leader / opener")


# -- tokens: titles, numbers, growth, articles --------------------------------------------------------------------


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


def test_numbers_and_growth_read_as_words():
    assert [openers.number_word(n) for n in (2, 6, 9, 10, 14)] == ["two", "six", "nine", "10", "14"]
    assert openers.growth_words(0.09) is None and openers.growth_words(7) is None  # too small; bad data
    assert [openers.growth_words(g) for g in (0.1, 0.2, 0.25, 0.34, 0.5, 0.9, 1.0, 1.6)] == [
        "grown by about a tenth", "grown by about a fifth", "grown by about a quarter", "grown by about a third",
        "grown by about half", "nearly doubled", "roughly doubled", "more than doubled"]


def test_articles_and_possessives_follow_the_value():
    assert openers.fill("I saw {company} has a {t} role open.", {"company": "Acme", "t": "HR Generalist"})[0] == \
        "I saw Acme has an HR Generalist role open."
    assert openers.fill("A {t} joined.", {"t": "Office Manager"})[0] == "An Office Manager joined."
    assert openers.fill("a {t}", {"t": "UX Designer"})[0] == "a UX Designer"
    assert openers.fill("a {t}", {"t": "User Researcher"})[0] == "a User Researcher"
    assert openers.fill("I saw {company}'s {p}.", {"company": "Acme Labs", "p": "careers page"})[0] == \
        "I saw Acme Labs' careers page."
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
        "", openers.HOLDOUT, "Funding in the last 6 months / opener_founder", FUNDING_LINES[FOUNDER])


# -- the "what they do" phrase (General opener_focus), then the generic line ----------------------------------------

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
    assert (op.text, op.source) == (GENERIC[FOUNDER], "opener_generic_founder")  # no phrase: the generic line
    assert sdk.calls == [] and ctx.store.select("credit_ledger") == []
    [note] = op.notes
    assert note.startswith("opener_focus: dry-run: not asked; a live run asks claude-sonnet-5-5, at most $0.00")


def test_the_focus_line_is_off_by_default_and_costs_nothing():
    ctx, sdk = focus_ctx()
    assert DEFAULT.general.opener_focus is False
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, ABOUT)
    assert op.source == "opener_generic_founder" and sdk.calls == []


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


def test_the_monthly_cap_refuses_the_call_and_the_account_gets_the_generic_line():
    ctx, sdk = focus_ctx(opener_focus=True)
    ctx.store.insert("credit_ledger", [{"entry_id": "e1", "system": "claude", "job": "copy_draft", "usd": 9.99,
                                        "credits": 0.0, "occurred_at": NOW}])
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, ABOUT)
    assert (op.text, op.source) == (GENERIC[FOUNDER], "opener_generic_founder") and sdk.calls == []
    assert any("monthly Claude cap" in n for n in op.notes)
    assert ctx.store.select("signal_events", {"source": openers.FOCUS_SOURCE}) == []  # asked again next time


def test_a_phrase_that_fails_validation_is_stored_as_refused_and_never_used():
    ctx, sdk = focus_ctx("the leading payroll platform", opener_focus=True)
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, ABOUT)
    assert op.source == "opener_generic_founder" and any("makes a claim (leading)" in n for n in op.notes)
    [stored] = ctx.store.select("signal_events", {"source": openers.FOCUS_SOURCE})
    assert stored["value"]["phrase"] == "" and stored["value"]["answer"] == "the leading payroll platform"
    openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, [*ABOUT, stored])
    assert len(sdk.calls) == 1  # not asked again while the refusal is fresh


def test_no_keywords_or_description_means_no_call():
    ctx, sdk = focus_ctx(opener_focus=True)
    op = openers.for_account(ctx, acct(angle="General"), {"role": FOUNDER}, [])
    assert op.source == "opener_generic_founder" and sdk.calls == []


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
    assert "{funding_stage}" not in errs[0]  # a retired token is never offered
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
    eap = next(s for s in got if s.signal == "EAP named")
    assert len(openers.lines_of(eap.role_openers[OPS])) == 2


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
    assert "funding or money" in str(general("opener_focus_line", "I saw {company} raised a round for {focus}.")[1][0])
    assert general("opener_focus", "yes")[0].opener_focus is True


# -- the copy desk: preview with a real line, the sheet check -----------------------------------------------------------


def test_preview_shows_a_real_filled_opener_for_the_sample_prospect(capsys):
    from us_outbound.ops import cli

    ctx = make_context(DEFAULT, now=NOW)
    argv = ["copy", "preview", "--synced", "--version", "general-founder-v1", "--opener", "--signal",
            "Funding in the last 6 months"]
    assert cli.main(argv, context_factory=lambda *a, **k: ctx) == 0
    out = capsys.readouterr().out
    assert f"Opener: {FUNDING_LINES[FOUNDER]} (Funding in the last 6 months / opener_founder)" in out
    assert f"Hi Dana,\n\n{FUNDING_LINES[FOUNDER]}" in out
    text, source = copy_desk.sample_opener(DEFAULT, PEOPLE, "New People leader", leader=True)
    assert (text, source) == (NEW_LEADER_LINES["self"], "New People leader / opener_self")
    with pytest.raises(ValueError, match="no Signals row"):
        copy_desk.sample_opener(DEFAULT, PEOPLE, "Nonsense")
    # --generic: the line an account with no signal line gets.
    argv = ["copy", "preview", "--synced", "--version", "technology-startups-ops-v1", "--opener", "--generic"]
    assert cli.main(argv, context_factory=lambda *a, **k: ctx) == 0
    out = capsys.readouterr().out
    assert f"Opener: {GENERIC[OPS]} (opener_generic_ops)" in out
    assert f"Hi Dana,\n\n{GENERIC[OPS]}\n\nOperations often sees burnout in a tech company" in out
    assert cli.main([*argv, "--signal", "EAP named"], context_factory=lambda *a, **k: ctx) != 0


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
    assert f"Opener: {FUNDING_LINES[FOUNDER]} (opener; Funding in the last 6 months / opener_founder)" in out
    assert f"Hi Omar,\n\n{FUNDING_LINES[FOUNDER]}" in out


def test_the_sheet_check_flags_a_line_that_breaks_the_rules(capsys):
    from us_outbound.ops import cli

    bad = with_lines(DEFAULT, "Progressive benefits", ops="I saw {company} offers unlimited PTO.")
    problems = copy_desk.check_openers(bad).problems
    assert problems == ["Progressive benefits, opener_ops: 'I saw Harbor & Finch offers unlimited PTO.' says "
                        "\"unlimited\": the copy rules never allow it"]
    ctx = make_context(bad, now=NOW)
    assert cli.main(["copy", "check", "--synced", "--version", "general-founder-v1"],
                    context_factory=lambda *a, **k: ctx) == 1
    assert "Opener: Progressive benefits, opener_ops" in capsys.readouterr().out
    # A generic line is checked the same way.
    bad = dataclasses.replace(DEFAULT, general=dataclasses.replace(DEFAULT.general,
                                                                   opener_generic_ops="Pressure is up, so book a call."))
    [problem] = copy_desk.check_openers(bad).problems
    assert problem.startswith("General, opener_generic_ops: 'Pressure is up, so book a call.' says \"book\"")


# -- the enrol job: the opener at enrol time, and the arm on the contact -----------------------------------------------


def test_enrol_fills_the_opener_for_the_contact_and_records_the_arm():
    from tests.test_enrol import instantly_posts, make

    signals = tuple(s for s in DEFAULT.signals if s.signal == "Funding in the last 6 months")
    s = dataclasses.replace(make_settings(live_sending=True), signals=signals)
    # acc-1 gets its signal's line; acc-2 has no signal, so the generic line; acc-3 is in the 30% holdout by
    # its hash, so its email 1 has none.
    accounts = [acct(account_id=a, domain=f"{a}.com", clean_name=f"Bright {a[-1]}", tier="Standard", score=30,
                     angle="Growing team" if a != "acc-2" else "General", status="verified",
                     industry="Advertising agencies", industry_group="Marketing & Creative Agencies")
                for a in ("acc-1", "acc-2", "acc-3")]
    people = [contact(contact_id=f"con-{a[-1]}", account_id=a, role=FOUNDER, title="CEO", email=f"omar@{a}.com",
                      first_name="Omar") for a in ("acc-1", "acc-2", "acc-3")]
    assert not openers.in_holdout("acc-1", 0.3) and not openers.in_holdout("acc-2", 0.3)
    assert openers.in_holdout("acc-3", 0.3)
    ctx, t = make(live=True, settings=s, accounts=accounts, contacts=people)
    ctx.store.insert("signal_events", [{**f, "account_id": a, "event_id": f"{f['event_id']}-{a}"}
                                       for a in ("acc-1", "acc-3") for f in FUNDING])
    out = enrol.run(ctx)
    assert out["enrolled"] == 3
    assert out["openers"] == {"arms": {"opener": 2, "holdout": 1},
                              "sources": {"Funding in the last 6 months / opener_founder": 2,
                                          "opener_generic_founder": 1}}
    bodies = {lead["email"]: lead["custom_variables"]["s1_body"] for p in instantly_posts(t) for lead in p.json["leads"]}
    assert f"<p>Hi Omar,</p><p>{FUNDING_LINES[FOUNDER]}</p>" in bodies["omar@acc-1.com"]
    assert f"<p>Hi Omar,</p><p>{GENERIC[FOUNDER]}</p>" in bodies["omar@acc-2.com"]
    assert "a plan" not in bodies["omar@acc-3.com"] and "<p>Hi Omar,</p><p>In most agencies" in bodies["omar@acc-3.com"]
    got = {c["contact_id"]: (c["opener_arm"], c["opener_source"]) for c in ctx.store.select("contacts")}
    assert got == {"con-1": ("opener", "Funding in the last 6 months / opener_founder"),
                   "con-2": ("opener", "opener_generic_founder"),
                   "con-3": ("holdout", "Funding in the last 6 months / opener_founder")}
