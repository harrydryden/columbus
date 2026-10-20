"""The rescore that cannot lose rows (W2-2, 20 Oct 2026; code-scoring B4, RS P0-8), on the MemoryStore.

The score job runs in one transaction under an advisory lock: a second rescore waits for the first (or skips after
LOCK_WAIT_SECONDS), the new signal_matched rows go in before the old ones go, and a rescore that fails part-way
rolls back. enrol.signals_now reads the latest run's rows, and scores an account itself when it finds none.
tests/test_db.py runs the two-process case against Postgres (US_OUTBOUND_TEST_DSN).
"""

from __future__ import annotations

import dataclasses
import threading
from collections import Counter
from datetime import UTC, datetime

import pytest

from tests.fakes import make_context
from tests.test_enrol import contacts3, make
from tests.test_enrol import accounts3 as enrol_accounts
from us_outbound.clients.db import LockTimeout, MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.enrol import enrol
from us_outbound.ops import heartbeat
from us_outbound.scoring import score
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import State
from us_outbound.settings.validate import validate_all

S, _ = validate_all(default_tabs())
NOW = datetime(2026, 10, 27, 5, 30, tzinfo=UTC)


def world(n: int = 20, store: MemoryStore | None = None):
    """n open accounts in NY, verified in the queue: the first 12 with 25 staff (Team of 10–49 fires), the rest 64
    (Team of 50–99). No other fact, so each fires exactly its size signal on the default Signals tab."""
    ctx = make_context(S, now=NOW, store=store)
    if store is None:
        ctx.store.insert("accounts", [
            {"account_id": f"a{i:02d}", "domain": f"a{i:02d}.com", "status": "verified", "tier": "Queue",
             "hq_state": "NY", "industry": "Fintech", "employees": 25 if i < 12 else 64,
             "size_band": "20-49" if i < 12 else "50-99"} for i in range(n)])
    return ctx


def match_rows(store) -> list[dict]:
    return [e for e in store.select("signal_events", {"source": score.SCORING_SOURCE, "fact": score.MATCH_FACT})]


def per_account(store) -> Counter:
    return Counter((r["account_id"], r["value"]["signal"]) for r in match_rows(store))


def test_score_done_says_how_often_each_signal_fired():
    """20 accounts: 12 at 25 staff and 8 at 64, so Team of 10–49 fires 12 times and Team of 50–99 8; nothing else
    fires (no fact but the size), so 20 match rows."""
    out = score.rescore(world())
    assert out["fired"] == {"Team of 10–49": 12, "Team of 50–99": 8}
    assert (out["accounts"], out["signal_matches"], out["replaced"]) == (20, 20, 0)
    assert out["tiers"] == {"Queue": 20, "Held": 0, "Excluded": 0}


def test_a_second_rescore_replaces_the_rows_by_id_and_stamps_its_run():
    """The second run reads the first's 20 rows and deletes them by id after inserting its own 20: one row per
    matched signal, all of the second run, observed at its start."""
    first = world()
    score.rescore(first)
    second = make_context(S, now=NOW.replace(hour=11), store=first.store)
    out = score.rescore(second)
    assert (out["signal_matches"], out["replaced"]) == (20, 20)
    assert set(per_account(second.store).values()) == {1} and len(match_rows(second.store)) == 20
    assert {r["value"]["run_id"] for r in match_rows(second.store)} == {second.run_id}
    assert {r["observed_at"] for r in match_rows(second.store)} == {NOW.replace(hour=11)}


def test_two_rescores_in_one_job_run_leave_one_row_per_signal():
    """settings_sync runs the lookalikes' rescore and then its own with one run_id: the second still replaces the
    first's rows (they were read under the lock), so no signal is counted twice."""
    ctx = world()
    score.rescore(ctx)
    out = score.rescore(ctx)
    assert (out["signal_matches"], out["replaced"]) == (20, 20)
    assert set(per_account(ctx.store).values()) == {1}


def _pause_after_insert(store, inserted: threading.Event, go: threading.Event):
    """store.insert of signal_events signals `inserted` and waits for `go`: the rescore holding the lock stops between
    its insert and its delete, the window B4 lost rows in."""
    real = store.insert

    def insert(table, rows):
        n = real(table, rows)
        if table == "signal_events" and not inserted.is_set():
            inserted.set()
            assert go.wait(10)
        return n

    store.insert = insert


def test_two_interleaved_rescores_run_one_after_the_other_and_leave_one_row_per_signal():
    """A inserts and stops; B starts and must wait for the lock (it reads nothing until A commits); A deletes and
    commits; B then reads A's rows, inserts, deletes them. Every account ends with exactly one row per matched
    signal (20 rows, all B's), and signals_now, read while A held the lock, was never empty."""
    a = world()
    score.rescore(a)  # rows from an earlier run, so A has rows to replace
    store = a.store
    inserted, go = threading.Event(), threading.Event()
    _pause_after_insert(store, inserted, go)
    order: list[str] = []
    real_rescore = score._rescore

    def traced(ctx):
        order.append(f"start {ctx.job}")
        out = real_rescore(ctx)
        order.append(f"end {ctx.job}")
        return out

    score_rescore = pytest.MonkeyPatch()
    score_rescore.setattr(score, "_rescore", traced)
    try:
        ctx_a = make_context(S, now=NOW.replace(hour=6), store=store, job="A")
        ctx_b = make_context(S, now=NOW.replace(hour=7), store=store, job="B")
        results: dict[str, dict] = {}
        ta = threading.Thread(target=lambda: results.update(A=score.rescore(ctx_a)))
        ta.start()
        assert inserted.wait(10)
        tb = threading.Thread(target=lambda: results.update(B=score.rescore(ctx_b)))
        tb.start()
        tb.join(0.3)
        assert tb.is_alive() and order == ["start A"]  # B waits at the lock
        snapshot = enrol.signals_now(make_context(S, now=NOW, store=store), [f"a{i:02d}" for i in range(20)])
        assert all(snapshot.values())  # every account has its signals while A is between insert and delete
        go.set()
        ta.join(10)
        tb.join(10)
    finally:
        score_rescore.undo()
    assert order == ["start A", "end A", "start B", "end B"]
    assert (results["A"]["replaced"], results["B"]["replaced"]) == (20, 20)
    assert set(per_account(store).values()) == {1} and len(match_rows(store)) == 20
    assert {r["value"]["run_id"] for r in match_rows(store)} == {ctx_b.run_id}


def test_a_rescore_that_finds_the_lock_held_past_the_wait_skips(monkeypatch):
    """The second rescore waits LOCK_WAIT_SECONDS (cut to 0.2 s here), then skips with the job contract's skip, which
    the heartbeat records as skipped; it wrote nothing."""
    a = world()
    store = a.store
    inserted, go = threading.Event(), threading.Event()
    _pause_after_insert(store, inserted, go)
    monkeypatch.setattr(score, "LOCK_WAIT_SECONDS", 0.2)
    ta = threading.Thread(target=lambda: score.rescore(a))
    ta.start()
    try:
        assert inserted.wait(10)
        b = make_context(S, now=NOW.replace(hour=7), store=store, job="B")
        out = score.rescore(b)
        assert out == {"skipped": True, "reason": "rescore already running"}
        assert heartbeat.summary_status(out) == heartbeat.SKIPPED
    finally:
        go.set()
        ta.join(10)
    assert {r["value"]["run_id"] for r in match_rows(store)} == {a.run_id}  # A's rows only, once each
    assert set(per_account(store).values()) == {1}


def test_a_rescore_that_fails_after_its_insert_rolls_back_and_the_old_rows_stand(monkeypatch):
    """The delete of the old rows raises (a worker killed part-way, say): the transaction rolls back, so the 20 rows
    of the earlier run are there, as they were, the new ones are not, the accounts keep their scores, and
    signals_now finds every account's signals."""
    ctx = world()
    score.rescore(ctx)
    before = sorted(r["event_id"] for r in match_rows(ctx.store))
    scores = {a["account_id"]: (a["score"], a["tier"]) for a in ctx.store.select("accounts")}
    # A heavier Team of 10–49 (15 -> 40) would change twelve scores; the rollback keeps the old ones.
    heavier = dataclasses.replace(S, signals=tuple(
        dataclasses.replace(sg, weight=40) if sg.signal == "Team of 10–49" else sg for sg in S.signals))
    later = make_context(heavier, now=NOW.replace(hour=11), store=ctx.store)
    real = ctx.store.delete

    def delete(table, where):
        if table == "signal_events":
            raise RuntimeError("killed between insert and delete")
        return real(table, where)

    monkeypatch.setattr(ctx.store, "delete", delete)
    with pytest.raises(RuntimeError, match="killed"):
        score.rescore(later)
    assert sorted(r["event_id"] for r in match_rows(ctx.store)) == before
    assert {a["account_id"]: (a["score"], a["tier"]) for a in ctx.store.select("accounts")} == scores
    assert scores["a00"] == (15, "Queue")  # not 40
    now = enrol.signals_now(later, [f"a{i:02d}" for i in range(20)])
    assert now["a00"] == [{"signal": "Team of 10–49", "weight": 15}] and all(now.values())


def test_signals_now_scores_an_account_with_no_rows_and_reads_only_the_latest_run():
    """Belt and braces (code-scoring B4 option 3): an account no rescore has written rows for is scored from its
    facts; one with rows from two runs (as a rescore before W2-2 could leave) reads only the latest run's."""
    ctx = world(2)
    score.rescore(ctx)
    ctx.store.delete("signal_events", {"account_id": "a00"})
    ctx.store.insert("signal_events", [
        {"event_id": "old", "account_id": "a01", "source": score.SCORING_SOURCE, "fact": score.MATCH_FACT,
         "value": {"signal": "Recent funding", "weight": 20, "run_id": "an-older-run"},
         "observed_at": datetime(2026, 10, 1, tzinfo=UTC)}])
    got = enrol.signals_now(ctx, ["a00", "a01"])
    assert got == {"a00": [{"signal": "Team of 10–49", "weight": 15}],
                   "a01": [{"signal": "Team of 10–49", "weight": 15}]}


def test_a_queue_account_with_no_tier_is_not_eligible_until_it_is_rescored():
    """A NULL tier is not scored yet, never NULL-means-eligible (RS §2 W2-2): enrol's candidates leave acc-2 out
    until a rescore writes its tier; acc-1 (Priority) and acc-3 (Control), rows a rescore has not reached since
    W2-2, read as the queue."""
    accounts = enrol_accounts()
    accounts[1]["tier"] = None
    ctx, _ = make(accounts=accounts, contacts=contacts3())
    # The three are in IL and NY, active here, so the rescore's checks pass them.
    ctx.settings = dataclasses.replace(ctx.settings, states=(State("IL", True), State("NY", True)))
    cands, _ = enrol.candidates(ctx, frozenset())
    assert sorted(c.account["account_id"] for c in cands) == ["acc-1", "acc-3"]
    score.rescore(ctx)
    assert ctx.store.get("accounts", account_id="acc-2")["tier"] == "Queue"
    cands, _ = enrol.candidates(ctx, frozenset())
    assert sorted(c.account["account_id"] for c in cands) == ["acc-1", "acc-2", "acc-3"]


# -- the MemoryStore's transaction and lock -------------------------------------------------------------------------


def test_the_memory_store_rolls_a_transaction_back_and_keeps_a_committed_one():
    store = MemoryStore(Guard())
    store.insert("accounts", [{"account_id": "x", "score": 1}])
    with pytest.raises(ValueError):
        with store.transaction():
            store.update("accounts", {"account_id": "x"}, {"score": 2})
            store.insert("accounts", [{"account_id": "y"}])
            raise ValueError("stop")
    assert [(a["account_id"], a["score"]) for a in store.select("accounts")] == [("x", 1)]
    with store.transaction():
        store.update("accounts", {"account_id": "x"}, {"score": 3})
        with pytest.raises(ValueError):
            with store.transaction():  # a savepoint: only its own writes go
                store.update("accounts", {"account_id": "x"}, {"score": 4})
                raise ValueError("inner")
    assert store.get("accounts", account_id="x")["score"] == 3


def test_the_memory_store_lock_is_taken_in_a_transaction_and_released_at_its_end():
    store, other = MemoryStore(Guard()), MemoryStore(Guard())
    with pytest.raises(RuntimeError, match="inside store.transaction"):
        store.advisory_lock(7)
    with store.transaction():
        store.advisory_lock(7)
        failed: list[Exception] = []

        def take():
            try:
                with other.transaction():
                    other.advisory_lock(7, wait_seconds=0.1)  # process-wide, like Postgres: another store waits too
            except LockTimeout as exc:
                failed.append(exc)

        t = threading.Thread(target=take)
        t.start()
        t.join(5)
        assert len(failed) == 1
    with other.transaction():
        other.advisory_lock(7, wait_seconds=0.1)  # free once the first transaction ended
