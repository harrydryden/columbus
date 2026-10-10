"""Every reader of "the newest fact" (signal_events), pinned: the newest row wins whatever order the rows come in, a
tie goes to the higher event_id, and each reader keeps its own rule for a row with no observed_at (9 Oct 2026; all of
them read through facts.py).

Each reader is driven through a small adapter: it is given (tag, observed_at) pairs, writes them as the rows that
reader reads (the tag is the event_id and is carried in the value), and returns the tag of the row the reader chose
(None for none). A reader whose answer is yes or no is asked once per tag, with only that tag's row saying yes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from tests.test_scoring import sig
from us_outbound import clay_cross_check, labels, verify
from us_outbound.contacts import pick
from us_outbound.enrol import approvals, openers
from us_outbound.learn import daily_report
from us_outbound.ops import relabel
from us_outbound.scoring import score
from us_outbound.sources import apollo_enrich, apollo_people, lookalikes, named, pages, site_visits

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # the ctx fixture's now
TODAY = date(2026, 10, 27)
T = NOW - timedelta(hours=1)
OLD = T - timedelta(days=1)

Specs = Sequence[tuple[str, datetime | None]]


def row(tag: str, at: datetime | None, source: str, fact: str, value: Any, *, quote: str = "", url: str = "",
        account_id: str = "a1") -> dict:
    return {"event_id": tag, "account_id": account_id, "source": source, "fact": fact, "value": value,
            "quote": quote, "source_url": url, "observed_at": at}


def stored(ctx, rows: list[dict]):
    ctx.store.insert("signal_events", rows)
    return ctx.store


def which(specs: Specs, says_yes: Callable[[Callable[[str], bool]], bool]) -> str | None:
    """The tag whose row the reader went by, for a reader that answers yes or no: says_yes(marked) runs it with only
    the rows marked(tag) says yes."""
    for tag, _ in specs:
        if says_yes(lambda t, tag=tag: t == tag):
            return tag
    return None


# -- the adapters: ctx, specs -> the chosen tag -----------------------------------------------------------------------


def labels_verdict(ctx, specs):
    v = labels.latest_verdict([row(t, at, labels.JOB, labels.VERDICT_FACT, {"tag": t}) for t, at in specs])
    return v and v["tag"]


def labels_correction(ctx, specs):
    v = labels.latest_correction([row(t, at, labels.JOB, labels.CORRECTED_FACT, {"tag": t}) for t, at in specs])
    return v and v["tag"]


def labels_material(ctx, specs):
    m = labels.Material.of({"domain": "a1.com"}, [row(t, at, labels.APOLLO_SOURCE, "description", t)
                                                  for t, at in specs])
    return m.description or None


def _codes(specs) -> dict[str, str]:
    """tag -> a NAICS code standing for it (the readers keep digits only, industry/material.naics_codes)."""
    return {t: str(541100 + i) for i, (t, _) in enumerate(specs)}


def labels_rules_material(ctx, specs):
    codes = _codes(specs)
    got = labels.rules_input([row(t, at, labels.APOLLO_SOURCE, "naics", [codes[t]]) for t, at in specs]).codes
    return {c: t for t, c in codes.items()}[got[0]] if got else None


def labels_home_page(ctx, specs):
    e = labels._home_page([row(t, at, pages.SOURCE, labels.HOME_FACT, {"title": t}) for t, at in specs])
    return e and e["value"]["title"]


def labels_tally(ctx, specs):
    stored(ctx, [row(t, at, labels.JOB, labels.VERDICT_FACT, {"asked": True, "rules": "", "model": t})
                 for t, at in specs])
    return labels.tally(ctx, None, NOW).no_rules.get("a1")


def labels_gap_codes(ctx, specs):
    codes = _codes(specs)
    stored(ctx, [row(t, at, labels.APOLLO_SOURCE, "naics", [codes[t]]) for t, at in specs])
    got, _ = labels.gap_codes(ctx, {"a1": "Dental"})
    return {c: t for t, c in codes.items()}[got[0][0]] if got else None


def labels_corrected_rows(ctx, specs):
    ctx.store.insert("accounts", [{"account_id": "a1", "domain": "a1.com", "clean_name": "A1"}])
    stored(ctx, [row("fix", NOW, labels.JOB, labels.CORRECTED_FACT, {"from": "x", "to": "Dental", "by": "Harry"})]
           + [row(t, at, labels.APOLLO_SOURCE, "description", t) for t, at in specs])
    return labels.corrected_rows(ctx)[0]["description"] or None


def score_latest_facts(ctx, specs):
    return score.latest_facts([row(t, at, "apollo_org", "employees", t) for t, at in specs]).get("employees")


def score_calendar(ctx, specs):
    months = {t: i + 1 for i, (t, _) in enumerate(specs)}
    got = score.calendar_facts([row(t, at, "irs_bmf", "fiscal_year_end_month", months[t]) for t, at in specs], TODAY)
    days = got.get("days_to_fiscal_year_start")
    return next((t for t, m in months.items() if score.days_to_fiscal_year_start(m, TODAY) == days), None)


def score_unread(ctx, specs):
    return which(specs, lambda marked: "clay_careers" in score.unread_sources(
        [row(t, at, "clay_careers", "read_status", "blocked" if marked(t) else "read") for t, at in specs]))


def score_condition(ctx, specs):
    rows = [row(t, at, "apollo_org", "employees", 100, quote=t) for t, at in specs]
    m = score._match_condition(sig("Big", "apollo_org", "employees >= 50", 10), rows, rows, TODAY, {})
    return m.evidence[0].quote if m else None


def score_terms(ctx, specs):
    rows = [row(t, at, "clay_careers", "benefit", "Therapy covered", url=t) for t, at in specs]
    m = score._match_terms(sig("MH", "clay_careers", "therapy", 10), rows)
    return m.evidence[0].url if m else None


def openers_tokens(ctx, specs):
    counts = {t: i + 2 for i, (t, _) in enumerate(specs)}  # OPEN_ROLES_MIN and up, each read as a word
    got = openers.tokens({}, sig("Hiring", "apollo_jobs", "open_roles >= 1", 10), None,
                         [row(t, at, "apollo_jobs", "open_roles", counts[t]) for t, at in specs], ctx.settings, TODAY)
    return next((t for t, n in counts.items() if openers.number_word(n) == got.get("open_roles")), None)


def openers_newest_leader(ctx, specs):
    rows = [row(t, at, "apollo_people", pick.NEWEST_LEADER_FACT, {"apollo_person_id": t}) for t, at in specs]
    return openers.newest_leader(rows, sig("New leader", "apollo_people", "people_found >= 1", 10), TODAY).get(
        "apollo_person_id")


def openers_focus_material(ctx, specs):
    return openers.focus_material([row(t, at, "apollo_org", "description", t) for t, at in specs])[1] or None


def openers_person_id(ctx, specs):
    return openers.contact_person_id({"contact_id": "c1"}, [
        row(t, at, pick.SOURCE, pick.OUTCOME_FACT, {"contact_id": "c1", "apollo_person_id": t}) for t, at in specs
    ]) or None


def openers_stored_focus(ctx, specs):
    v = openers.stored_focus([row(t, at, openers.FOCUS_SOURCE, openers.FOCUS_FACT, {"tag": t}) for t, at in specs],
                             NOW)
    return v and v["tag"]


def verify_doubts(ctx, specs):
    stored(ctx, [row(t, at, verify.DOUBT_SOURCE, verify.DOUBT_FACT, {"reasons": [t]}) for t, at in specs])
    latest, _ = verify.doubt_history(ctx, ["a1"])
    return latest["a1"]["reasons"][0] if "a1" in latest else None


def pick_outcomes(ctx, specs):
    stored(ctx, [row(t, at, pick.SOURCE, pick.OUTCOME_FACT, {"tag": t}) for t, at in specs])
    got = pick.history(ctx.store, NOW).outcomes.get("a1")
    return got and got[1]["tag"]


def clay_answers(ctx, specs):
    stored(ctx, [row(t, at, clay_cross_check.SOURCE, clay_cross_check.FACT, {"tag": t}) for t, at in specs])
    return clay_cross_check.answers(ctx.store, ["a1"]).get("a1", {}).get("tag")


def lookalikes_latest(ctx, specs):
    got = lookalikes._latest([row(t, at, lookalikes.SOURCE, "lookalike_fit", t) for t, at in specs])
    return got[("a1", "lookalike_fit")][1] if got else None


def named_latest(ctx, specs):
    return which(specs, lambda marked: named._latest(
        [row(t, at, named.SOURCE, named.FACT, marked(t)) for t, at in specs]).get("a1", False))


def site_visits_latest(ctx, specs):
    stored(ctx, [row(t, at, site_visits.SOURCE, site_visits.US_FACT, t) for t, at in specs])
    got = site_visits.latest_facts(ctx).get(("a1", site_visits.US_FACT))
    return got and got[1]


def approvals_recipient(ctx, specs):
    stored(ctx, [row(t, at, approvals.PICK_SOURCE, approvals.PICK_FACT, {"contact_id": "c1", "apollo_person_id": t})
                 for t, at in specs])
    return approvals.recipient_source(ctx, "a1", {"contact_id": "c1"})["apollo_person_id"] or None


def relabel_show(ctx, specs):
    ctx.store.insert("accounts", [{"account_id": "a1", "domain": "a1.com"}])
    stored(ctx, [row(t, at, labels.JOB, labels.VERDICT_FACT, {"asked": True, "model": t}) for t, at in specs])
    history = relabel.show(ctx, "a1.com")["history"]
    return history[0]["model"] if history else None


def pages_history(ctx, specs):
    stored(ctx, [row(t, at, pages.SOURCE, pages.SUMMARY_FACT, {"outcome": t}) for t, at in specs])
    got = pages.history(ctx, ["a1"])[0].get("a1")
    return got and got[1]


def pages_coverage(ctx, specs):
    stored(ctx, [row(t, at, pages.SOURCE, pages.SUMMARY_FACT, {"outcome": t}) for t, at in specs])
    got = pages.coverage(ctx.store, ctx.settings, TODAY).outcomes
    return next(iter(got), None)


def pages_latest_run(ctx, specs):
    stored(ctx, [row(t, at, pages.SOURCE, pages.SUMMARY_FACT, {"run_id": t}) for t, at in specs])
    return pages.latest_run(ctx.store)


def enrich_tally(ctx, specs):
    def says_yes(marked):
        ctx.store.delete("signal_events", {"source": apollo_enrich.SOURCE})
        stored(ctx, [row(t, at, apollo_enrich.SOURCE, apollo_enrich.MARKER,
                         {"outcome": apollo_enrich.FOUND, "employees": 9 if marked(t) else None}) for t, at in specs])
        return apollo_enrich.tally(ctx.store, TODAY).with_employees == 1

    return which(specs, says_yes)


def enrich_latest_run(ctx, specs):
    stored(ctx, [row(t, at, apollo_enrich.SOURCE, apollo_enrich.MARKER, {"run_id": t}) for t, at in specs])
    return apollo_enrich.latest_run(ctx.store)


def people_history(ctx, specs):
    stored(ctx, [row(t, at, apollo_people.SOURCE, "people_found", t) for t, at in specs])
    got = apollo_people.history(ctx, ["a1"])[0]["a1"].get("people_found")
    return got and got["value"]


def people_employees(ctx, specs):
    stored(ctx, [row(t, at, apollo_people.ORG_SOURCE, "employees", t) for t, at in specs])
    return apollo_people.history(ctx, ["a1"])[1].get("a1")


def daily_no_contact(ctx, specs):
    stored(ctx, [row(t, at, pick.SOURCE, pick.OUTCOME_FACT, {"outcome": pick.NO_CONTACT, "reason": t})
                 for t, at in specs])
    return daily_report.no_contact_in(ctx.store, NOW - timedelta(days=30), NOW).get("a1")


# reader -> (adapter, whether a row with no observed_at can be chosen)
READERS: dict[str, tuple[Callable[[Any, Specs], str | None], bool]] = {
    "labels.latest_verdict": (labels_verdict, True),
    "labels.latest_correction": (labels_correction, True),
    "labels.Material.of": (labels_material, True),
    "labels.rules_input": (labels_rules_material, True),
    "labels._home_page": (labels_home_page, True),
    "labels.tally": (labels_tally, True),
    "labels.gap_codes": (labels_gap_codes, True),
    "labels.corrected_rows": (labels_corrected_rows, True),
    "score.latest_facts": (score_latest_facts, True),
    "score.calendar_facts": (score_calendar, True),
    "score.unread_sources": (score_unread, True),
    "score._match_condition": (score_condition, True),
    "score._match_terms": (score_terms, True),
    "openers.tokens": (openers_tokens, False),  # fresh_facts leaves out a fact with no time
    "openers.newest_leader": (openers_newest_leader, False),
    "openers.focus_material": (openers_focus_material, True),
    "openers.contact_person_id": (openers_person_id, True),
    "openers.stored_focus": (openers_stored_focus, False),  # undated is older than FOCUS_REFRESH_DAYS
    "verify.doubt_history": (verify_doubts, True),
    "pick.history": (pick_outcomes, False),
    "clay_cross_check.answers": (clay_answers, True),
    "lookalikes._latest": (lookalikes_latest, True),
    "named._latest": (named_latest, True),
    "site_visits.latest_facts": (site_visits_latest, False),
    "approvals.recipient_source": (approvals_recipient, True),
    "relabel.show": (relabel_show, True),
    "pages.history": (pages_history, False),
    "pages.coverage": (pages_coverage, False),
    "pages.latest_run": (pages_latest_run, False),
    "apollo_enrich.tally": (enrich_tally, False),
    "apollo_enrich.latest_run": (enrich_latest_run, False),
    "apollo_people.history": (people_history, False),
    "apollo_people.history employees": (people_employees, False),
    "daily_report.no_contact_in": (daily_no_contact, False),
}


@pytest.mark.parametrize("name", READERS)
@pytest.mark.parametrize("reverse", [False, True])
def test_the_newest_row_wins_in_any_order(ctx, name, reverse):
    """"new" sorts before "old" as an event_id, so the time decides, not the id."""
    specs = [("old", OLD), ("new", T)]
    assert READERS[name][0](ctx, specs[::-1] if reverse else specs) == "new"


@pytest.mark.parametrize("name", READERS)
@pytest.mark.parametrize("reverse", [False, True])
def test_a_dated_row_beats_one_with_no_time(ctx, name, reverse):
    """"undated" sorts after "dated" as an event_id, so a reader that ignored the time would get it wrong."""
    specs = [("undated", None), ("dated", OLD)]
    assert READERS[name][0](ctx, specs[::-1] if reverse else specs) == "dated"


@pytest.mark.parametrize("name", READERS)
def test_a_row_with_no_time_is_chosen_only_where_the_reader_allows(ctx, name):
    adapter, undated_ok = READERS[name]
    assert adapter(ctx, [("undated", None)]) == ("undated" if undated_ok else None)


@pytest.mark.parametrize("name", READERS)
@pytest.mark.parametrize("reverse", [False, True])
def test_a_tie_goes_to_the_higher_event_id(ctx, name, reverse):
    """Every reader reads through facts.py, so a tie (one observed_at) has one answer. Before 9 Oct 2026 it was the
    first or the last row as they came, and Postgres returns rows in no set order."""
    specs = [("e1", T), ("e2", T)]
    assert READERS[name][0](ctx, specs[::-1] if reverse else specs) == "e2"


# -- what some readers do besides -------------------------------------------------------------------------------------


def test_score_latest_facts_takes_a_fact_from_whichever_source_wrote_it_last():
    """employees 40 from lookalike beats 180 from apollo_org when newer: latest_facts reads no source (recorded in the
    facts.py report; tiers' hard exclusions read this)."""
    rows = [row("e1", OLD, "apollo_org", "employees", 180), row("e2", T, "lookalike", "employees", 40)]
    assert score.latest_facts(rows)["employees"] == 40
    assert score.latest_facts(rows[:1])["employees"] == 180


def test_keywords_stored_as_text_read_as_a_list_by_labels():
    m = labels.Material.of({"domain": "a1.com"}, [row("e1", T, "apollo_org", "keywords", "payroll, hr software")])
    assert m.keywords == ("payroll", "hr software")


def test_keywords_stored_as_text_read_as_a_list_by_the_opener_focus():
    """facts.texts: as labels read them (before 9 Oct 2026 the opener read text keywords as none)."""
    keywords, _ = openers.focus_material([row("e1", T, "apollo_org", "keywords", "payroll, hr software")])
    assert keywords == ["payroll", "hr software"]
