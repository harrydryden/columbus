"""The one credit_ledger writer (us_outbound/ledger.py; 9 Oct 2026, defect 9): a paid call is reserved before it is
made and settled after it; a call that fails or gives no answer keeps its reservation, free only when refused
unprocessed; the store's date filter (Range) the ledger's readers use, in MemoryStore (test_db.py has Postgres)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_context
from us_outbound import budget, ledger
from us_outbound.clients.claude import month_spend_usd
from us_outbound.clients.db import MemoryStore, Range
from us_outbound.clients.guard import Guard
from us_outbound.clients.http import ApiError, TransportError
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

BASE, _ = validate_all(default_tabs())
NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)


class CountingStore(MemoryStore):
    """A MemoryStore that keeps each write and each select's filter."""

    def __init__(self, guard):
        super().__init__(guard)
        self.writes: list[tuple[str, int]] = []
        self.wheres: list[dict] = []

    def insert(self, table, rows):
        rows = list(rows)
        self.writes.append(("insert", len(rows)))
        return super().insert(table, rows)

    def upsert(self, table, rows):
        rows = list(rows)
        self.writes.append(("upsert", len(rows)))
        return super().upsert(table, rows)

    def select(self, table, where=None):
        self.wheres.append(dict(where or {}))
        return super().select(table, where)


def ctx_with(store=None):
    return make_context(BASE, job="a_job", now=NOW, store=store)


def rows(ctx) -> list[dict]:
    return ctx.store.select("credit_ledger")


def test_a_charge_is_written_before_the_call_and_settled_after_it():
    ctx = ctx_with()
    with ledger.charge(ctx, "apollo", "a_job", 3.0, note="reserving", account_id="a1") as paid:
        [row] = rows(ctx)  # written before the call
        assert (row["system"], row["job"], row["credits"], row["note"], row["account_id"], row["run_id"],
                row["occurred_at"]) == ("apollo", "a_job", 3.0, "reserving", "a1", ctx.run_id, NOW)
        assert paid.settle(1.0, note="found 1") == 1.0
    [row] = rows(ctx)
    assert (row["credits"], row["note"], row["entry_id"]) == (1.0, "found 1", paid.row["entry_id"])


def test_a_call_that_fails_or_never_answers_keeps_its_reservation():
    ctx = ctx_with()
    with pytest.raises(TimeoutError):
        with ledger.charge(ctx, "clay", "a_job", 2.0, note="reserved", failed_note="failed; counted in case"):
            raise TimeoutError("cut off")
    with ledger.charge(ctx, "clay", "a_job", 2.0, note="no answer"):
        pass  # the block never settled it
    with pytest.raises(KeyError):
        with ledger.charge(ctx, "clay", "a_job", 2.0, note="reserved", failed_note="unused") as paid:
            paid.settle(0.5, note="settled, then the caller failed")
            raise KeyError("later")
    assert sorted((r["credits"], r["note"]) for r in rows(ctx)) == [
        (0.5, "settled, then the caller failed"), (2.0, "failed; counted in case"), (2.0, "no answer")]
    assert budget.spent_in(ctx.store, "clay", NOW - timedelta(hours=1), NOW + timedelta(hours=1)) == 4.5


@pytest.mark.parametrize("exc, cost", [
    (ApiError("apollo", 422, "bad filter"), 0.0),
    (ApiError("apollo", 429, "slow down"), 0.0),
    (ApiError("apollo", 503, "unavailable"), 1.0),
    (TransportError("apollo", TimeoutError("read timed out")), 1.0),
])
def test_a_failed_call_is_free_only_when_it_was_refused_unprocessed(exc, cost):
    ctx = ctx_with()
    what = {"slice": "NY fintech", "page": 2}
    with ledger.charge(ctx, "apollo", "a_job", 1.0, note=ledger.reserved_note(what)) as paid:
        assert paid.fail(exc, note=ledger.failed_note(what, exc)) == cost
    [row] = rows(ctx)
    assert row["credits"] == cost
    # A reader of "slice" (apollo_universe.cursors) never takes a reserved or failed call for a page read.
    assert json.loads(row["note"]) == {"reserved": what, "failed": exc.status}


def test_a_call_for_several_accounts_reserves_and_settles_in_one_write_each():
    ctx = ctx_with(CountingStore(Guard()))
    held = ledger.reserve_each(ctx, "clay", "a_job", 3.0, ["a1", "a2"], note="reserved")
    assert sorted(held) == ["a1", "a2"] and {r["account_id"] for r in rows(ctx)} == {"a1", "a2"}
    ledger.settle_each(ctx, [(held["a1"], 1.5, "answered"), (held["a2"], 3.0, "failed; counted")])
    assert ctx.store.writes == [("insert", 2), ("upsert", 2)]
    assert sorted((r["account_id"], r["credits"], r["note"]) for r in rows(ctx)) == [
        ("a1", 1.5, "answered"), ("a2", 3.0, "failed; counted")]


def test_the_readers_ask_the_store_for_the_period_only():
    """budget.spent_in and the Claude cap read every row a system ever had before 9 Oct 2026."""
    store = CountingStore(Guard())
    month = datetime(2026, 10, 1, tzinfo=UTC)
    store.insert("credit_ledger", [
        {"entry_id": "c1", "system": "clay", "job": "a", "credits": 10.0, "occurred_at": NOW},
        {"entry_id": "c2", "system": "clay", "job": "b", "credits": 5.0, "occurred_at": NOW.isoformat()},  # text
        {"entry_id": "c3", "system": "clay", "job": "a", "credits": 100.0, "occurred_at": month - timedelta(days=3)},
        {"entry_id": "c4", "system": "clay", "job": "a", "credits": 7.0, "occurred_at": None},
        {"entry_id": "c5", "system": "claude", "job": "x", "usd": 0.25, "occurred_at": NOW.replace(tzinfo=None)},  # naive
        {"entry_id": "c6", "system": "claude", "job": "x", "usd": 3.0, "occurred_at": month - timedelta(minutes=1)},
    ])
    start, end = budget.month_bounds(NOW)
    assert budget.spent_in(store, "clay", start, end) == 15.0
    assert budget.spent_in(store, "clay", start, end, jobs=("a",)) == 10.0
    assert month_spend_usd(store, NOW) == 0.25
    ranged = [w for w in store.wheres if "system" in w]
    assert ranged and all(isinstance(w.get("occurred_at"), Range) for w in ranged)


def test_range_bounds_in_memory():
    store = MemoryStore(Guard())
    t = datetime(2026, 10, 9, tzinfo=UTC)
    store.insert("credit_ledger", [{"entry_id": f"e{i}", "occurred_at": t + timedelta(hours=i)} for i in range(3)]
                 + [{"entry_id": "none", "occurred_at": None}])
    ids = lambda where: sorted(r["entry_id"] for r in store.select("credit_ledger", where))  # noqa: E731
    assert ids({"occurred_at": Range(t, t + timedelta(hours=2))}) == ["e0", "e1"]  # lo included, hi left out
    assert ids({"occurred_at": Range(t + timedelta(hours=1), None)}) == ["e1", "e2"]
    assert ids({"occurred_at": Range(None, t + timedelta(hours=1))}) == ["e0"]
    assert ids({"occurred_at": Range()}) == ["e0", "e1", "e2"]  # no bound: any value, never NULL
