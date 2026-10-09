"""The newest fact, read one way everywhere (us_outbound/facts.py, 9 Oct 2026)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from us_outbound import facts
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Guard

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def row(event_id: str, fact: str = "employees", value=None, at=NOW, source: str = "apollo_org",
        account_id: str = "a1") -> dict:
    return {"event_id": event_id, "account_id": account_id, "source": source, "fact": fact,
            "value": event_id if value is None else value, "quote": "", "source_url": "", "observed_at": at}


def both_orders(rows: list[dict]) -> list[list[dict]]:
    return [list(rows), list(reversed(rows))]


def test_the_latest_observed_at_wins_in_any_order():
    old, new = row("e9", at=NOW - timedelta(days=1)), row("e1", at=NOW)
    for rows in both_orders([old, new]):
        assert facts.newest(rows, "employees") is new


def test_a_tie_goes_to_the_higher_event_id_in_any_order():
    a, b = row("e1"), row("e2")
    for rows in both_orders([a, b]):
        assert facts.newest(rows, "employees") is b
        assert facts.history(rows) == [b, a]


def test_time_is_read_in_any_stored_form():
    """ISO text, a naive datetime (UTC) and an aware one compare as instants."""
    text = row("e1", at="2026-10-09T13:00:00+00:00")
    naive = row("e2", at=datetime(2026, 10, 9, 12, 30))
    aware = row("e3", at=datetime(2026, 10, 9, 8, 0, tzinfo=UTC))
    for rows in both_orders([text, naive, aware]):
        assert facts.newest(rows) is text
    assert facts.at(naive) == datetime(2026, 10, 9, 12, 30, tzinfo=UTC)


def test_an_undated_row_is_oldest_but_still_chosen_unless_dated():
    undated, unreadable, dated = row("e9", at=None), row("e8", at="soon"), row("e1", at=NOW - timedelta(days=400))
    for rows in both_orders([undated, dated]):
        assert facts.newest(rows) is dated
    assert facts.newest([undated]) is undated
    assert facts.newest([undated, unreadable]) is undated  # both undated: the higher event_id
    assert facts.newest([undated, unreadable], dated=True) is None
    assert facts.newest([undated, dated], dated=True) is dated
    assert facts.history([undated, dated]) == [dated, undated]


def test_fact_source_and_where_filter_before_choosing():
    org, jobs = row("e1", "open_roles", 12, source="apollo_org"), row("e2", "open_roles", 4, source="apollo_jobs")
    posts = row("e3", "open_roles", 7, source="job_posts", at=NOW - timedelta(days=1))
    rows = [org, jobs, posts, row("e4", "employees", 180)]
    assert facts.newest(rows, "open_roles") is jobs
    assert facts.newest(rows, "open_roles", "apollo_org") is org
    assert facts.newest(rows, "open_roles", {"apollo_org", "job_posts"}) is org
    assert facts.newest(rows, "open_roles", ["job_posts"]) is posts
    assert facts.newest(rows, "open_roles", where=lambda r: r["value"] > 5) is org
    assert facts.newest(rows, "headcount") is None and facts.newest([], "open_roles") is None


def test_where_sees_only_dated_rows_when_dated():
    seen = []
    facts.newest([row("e1", at=None), row("e2")], dated=True, where=lambda r: seen.append(r["event_id"]) or True)
    assert seen == ["e2"]


def test_value_is_the_newest_rows_value_or_the_default():
    rows = [row("e1", value=40, at=NOW - timedelta(days=1)), row("e2", value=None, at=NOW)]
    rows[1]["value"] = None
    assert facts.value(rows, "employees") is None  # the newest says nothing: no older value shows through
    assert facts.value(rows[:1], "employees") == 40
    assert facts.value(rows, "naics", default=()) == ()


def test_newest_by_picks_one_row_per_key():
    rows = [row("e1", account_id="a1", at=NOW - timedelta(days=2)), row("e2", account_id="a1"),
            row("e3", account_id="a2", at=None), row("e4", account_id="a2", at=NOW - timedelta(days=9)),
            row("e5", "naics", account_id="a2")]
    for rs in both_orders(rows):
        got = facts.newest_by(rs, lambda r: r["account_id"], "employees")
        assert {k: r["event_id"] for k, r in got.items()} == {"a1": "e2", "a2": "e4"}
        got = facts.newest_by(rs, lambda r: (r["account_id"], r["fact"]), dated=True)
        assert {k: r["event_id"] for k, r in got.items()} == {("a1", "employees"): "e2", ("a2", "employees"): "e4",
                                                               ("a2", "naics"): "e5"}


def test_latest_by_fact_takes_each_fact_from_whichever_source_wrote_it_last():
    rows = [row("e1", "employees", 180, source="apollo_org", at=NOW - timedelta(days=1)),
            row("e2", "employees", 40, source="lookalike"), row("e3", "naics", ["5415"]),
            row("e4", "", "no fact"), row("e5", "posting_text", "We are hiring")]
    got = facts.latest_by_fact(rows, skip_facts={"posting_text"})
    assert {f: r["value"] for f, r in got.items()} == {"employees": 40, "naics": ["5415"]}
    got = facts.latest_by_fact(rows, sources="apollo_org")
    assert {f: r["value"] for f, r in got.items()} == {"employees": 180, "naics": ["5415"], "posting_text": "We are hiring"}


@pytest.mark.parametrize("value, want", [
    (["payroll", " hr  software ", "", None, "payroll"], ["payroll", "hr software", "payroll"]),
    (("saas",), ["saas"]),
    ("payroll, hr software,, ", ["payroll", "hr software"]),
    ("", []), (None, []), ([], []),
    (541511, ["541511"]),  # a bare value reads as one item, as the labels' rules always read it
])
def test_texts_reads_a_list_or_comma_separated_text(value, want):
    assert facts.texts(value) == want


def test_load_reads_each_accounts_rows_in_chunks(monkeypatch):
    store = MemoryStore(Guard())
    store.insert("signal_events", [row("e1", account_id="a1"), row("e2", "naics", account_id="a1"),
                                   row("e3", account_id="a2", source="lookalike"), row("e4", account_id="a3")])
    calls = []
    select = store.select
    monkeypatch.setattr(store, "select", lambda table, where=None: calls.append(where) or select(table, where))
    monkeypatch.setattr(facts, "ID_CHUNK", 2)
    got = facts.load(store, ["a1", "a2", "a3", "a4"], sources="apollo_org", facts=["employees"])
    assert {k: [r["event_id"] for r in v] for k, v in got.items()} == {"a1": ["e1"], "a3": ["e4"]}
    assert got["a4"] == []
    assert calls == [{"source": ["apollo_org"], "fact": ["employees"], "account_id": ["a1", "a2"]},
                     {"source": ["apollo_org"], "fact": ["employees"], "account_id": ["a3", "a4"]}]
    assert set(facts.load(store, ["a2"])) == {"a2"}
