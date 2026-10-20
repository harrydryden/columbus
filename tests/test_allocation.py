"""The sources' state and the week's allocation (W3-3, 20 Oct 2026): source_state (sources/source_state.py) in place of
the credit_ledger notes, the one-off `source migrate-state`, and sources/allocation.py's pages per state (loop L2).

The allocation's numbers are hand-computed in each test's docstring; the sampled ones are read over many seeds, as
the plan is a draw (RT §3: "a prior-weighted allocation with posterior noise")."""

from __future__ import annotations

import dataclasses
import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_context
from us_outbound.clients.db import new_id
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all
from us_outbound.sources import allocation as al
from us_outbound.sources import apollo_universe as uni
from us_outbound.sources import source_state

NOW = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)  # Tue 20 Oct 2026, 03:00 UK: ISO week 43
SETTINGS, _ = validate_all(default_tabs())
EQUAL = {"NY": 0, "GA": 0}  # an equal prior: w = 1 in both


def ctx_at(now=NOW, live=False):
    return make_context(SETTINGS, job=uni.JOB, now=now, live=live)


# -- the allocation, by hand ----------------------------------------------------------------------------------------


def test_two_states_with_a_cap_of_one_half_ny_takes_at_least_5_of_10_and_ga_never_fewer_than_1():
    """NY 20 of 50 verified with a contact, GA 5 of 50, equal prior (w = 1): NY ~ Beta(22, 31) (mean 0.415), GA ~
    Beta(7, 46) (mean 0.132). Two states: cap = max(0.40, 1/2) = 0.50, so 5 of 10 pages at most. Floor 1 each, then the
    8 left in proportion: NY's share is about 0.76 of them, 1 + 6.1 = 7.1, over the cap, so NY takes 5 and GA the 5
    left. NY ≥ 5 in ≥ 95% of seeds (in fact every seed: GA would need a draw near NY's); GA ≥ 1 always."""
    yields = {("Technology & Startups", "NY", "50-99"): (50, 20), ("Technology & Startups", "GA", "50-99"): (50, 5)}
    ny5 = 0
    for seed in range(1000):
        v = al.sample_values(yields, yields, random.Random(seed), EQUAL)
        got = al.allocate(v, 10)
        assert sum(got.values()) == 10 and got["GA"] >= 1 and got["NY"] <= 5
        ny5 += got["NY"] >= 5
    assert ny5 / 1000 >= 0.95


def test_without_the_cap_ny_takes_at_least_6_of_10_in_95_percent_of_seeds():
    """The same draws with no cap (cap 1.0): NY gets 1 + 8 × p, p = v_NY ÷ (v_NY + v_GA), rounded by the largest
    remainder; NY ≥ 6 needs p ≥ about 0.56, which Beta(22, 31) against Beta(7, 46) gives in about 99% of draws (99.2%
    over these 1,000 seeds; the plan's own simulation, 99.8%). A mutation that drops the floor or the proportion fails."""
    yields = {("Technology & Startups", "NY", "50-99"): (50, 20), ("Technology & Startups", "GA", "50-99"): (50, 5)}
    ny6 = sum(al.allocate(al.sample_values(yields, yields, random.Random(seed), EQUAL), 10, cap=1.0)["NY"] >= 6
              for seed in range(1000))
    assert ny6 / 1000 >= 0.95


def test_seven_states_and_ten_pages_put_no_state_over_four():
    """cap = max(0.40, 1/7) = 0.40 of 10 = 4. One state far ahead (value 100 against 1) takes its 4; the other six their
    floor of 1 each: 4 + 6 = 10."""
    values = {"NY": 100.0, "GA": 1.0, "IL": 1.0, "MA": 1.0, "NJ": 1.0, "PA": 1.0, "TX": 1.0}
    got = al.allocate(values, 10)
    assert got == {"NY": 4, "GA": 1, "IL": 1, "MA": 1, "NJ": 1, "PA": 1, "TX": 1}
    for seed in range(200):  # any values: never over 4
        rng = random.Random(seed)
        assert max(al.allocate({s: rng.random() for s in values}, 10).values()) <= 4


def test_a_state_with_no_admitted_accounts_gets_its_floor_plus_its_prior_share_of_the_remainder():
    """NJ has n = 0: its value is its prior's, Beta(1 + w, 1) with w = 0 + 1 = 1, mean 2/3. NY (n 50, k 20, w 17) has
    mean (1 + 17 + 20) ÷ (2 + 17 + 50) = 38/69 = 0.551; GA (n 50, k 5, w 2) 8/55 = 0.145. 100 pages, 3 states: cap =
    max(0.40, 1/3) = 40. Floor 1 each; the 97 left in proportion: NJ 1 + 97 × 0.667 ÷ 1.363 = 48.4 and NY 1 + 97 ×
    0.551 ÷ 1.363 = 40.2, both over 40, so both take the cap and GA the 20 left (1 + 19)."""
    means = {"NY": 38 / 69, "GA": 8 / 55, "NJ": 2 / 3}
    t = al.targets(means, 100)
    assert t == {"NY": 40.0, "GA": 20.0, "NJ": 40.0}
    # With more pages than the caps bind (cap 1.0): NJ = 1 + 97 × (2/3) ÷ (38/69 + 8/55 + 2/3).
    total = sum(means.values())
    assert al.targets(means, 100, cap=1.0)["NJ"] == pytest.approx(1 + 97 * (2 / 3) / total)
    assert al.prior("NJ") == 1 and al.prior("NY") == 17 and al.prior("WY") == 1


def test_the_yields_come_from_the_sourced_by_facts_of_the_last_eight_weeks():
    """Three NY accounts admitted: one verified with a contact (k), one verified without, one enrolled with a contact
    (k); one GA account nine weeks ago (out of the window). NY: n 3, k 2; GA: nothing."""
    ctx = ctx_at()
    sl = ("Technology & Startups", "NY", "50-99")

    def admitted(aid, state, status, *, contact, weeks_ago=1):
        ctx.store.insert("accounts", [{"account_id": aid, "domain": f"{aid}.com", "status": status}])
        ctx.store.insert("signal_events", [{
            "event_id": new_id(), "account_id": aid, "source": uni.JOB, "fact": uni.SOURCED_BY,
            "value": {"slice": "k", "group": sl[0], "state": state, "band": "50-99", "page": 1, "run_id": "r"},
            "observed_at": NOW - timedelta(weeks=weeks_ago)}])
        if contact:
            ctx.store.insert("contacts", [{"contact_id": f"c-{aid}", "account_id": aid}])

    admitted("a1", "NY", "verified", contact=True)
    admitted("a2", "NY", "verified", contact=False)
    admitted("a3", "NY", "enrolled", contact=True)
    admitted("a4", "GA", "verified", contact=True, weeks_ago=9)
    assert al.slice_yields(ctx) == {sl: (3, 2)}


# -- the plan in the run --------------------------------------------------------------------------------------------


def test_the_week_s_plan_is_kept_and_a_second_run_that_week_reads_the_same_one():
    ctx = ctx_at()
    slices = [("Technology & Startups", st, "50-99") for st in ("NY", "IL")]
    first = al.this_week(ctx, slices, ["NY", "IL"])
    assert ctx.store.get("source_state", key="plan:2026-W43")["value"]["pages"] == first.pages
    first.spend("NY")
    al.save(ctx, first)
    again = al.this_week(dataclasses.replace(ctx, now=NOW + timedelta(days=1)), slices, ["NY", "IL"])
    assert again.left["NY"] == first.pages["NY"] - 1  # the same week: what is left carries on
    nxt = al.this_week(dataclasses.replace(ctx, now=NOW + timedelta(days=7)), slices, ["NY", "IL"])
    assert nxt.week == "2026-W44" and nxt.left == nxt.pages  # a new week: a new plan


def test_the_lane_reads_the_state_furthest_from_its_plan_first_then_in_turn():
    """Plan NY 3, IL 1. Reads: NY (both all left: the larger plan first), IL (1/1 left against NY's 2/3), then NY, NY;
    every page read, the states in turn from the week's order."""
    plan = al.Plan(week="2026-W43", pages={"NY": 3, "IL": 1}, left={"NY": 3, "IL": 1})

    @dataclasses.dataclass(frozen=True)
    class S:
        state: str
        n: int

    lane = al.Lane([S("NY", i) for i in range(10)] + [S("IL", i) for i in range(10)], plan, ["IL", "NY"])
    order = []
    for _ in range(6):
        sl = lane.popleft()
        order.append(sl.state)
        plan.spend(sl.state)
        lane.append(sl)
    assert order == ["NY", "IL", "NY", "NY", "IL", "NY"]


def test_the_readout_names_the_plan_for_what_it_is():
    ctx = ctx_at()
    assert "none made yet this week" in al.readout_line(ctx)
    al.save(ctx, al.Plan(week="2026-W43", pages={"NY": 40, "TX": 30, "GA": 30}, left={"NY": 38, "TX": 30, "GA": 29},
                         yields={"NY": [2, 3], "TX": [0, 0], "GA": [0, 1]}))
    line = al.readout_line(ctx)
    assert "a prior-weighted allocation; in 2026 it optimises contactable yield per page, not value" in line
    assert "NY 2 of 40, GA 1 of 30, TX 0 of 30" in line and "NY 2 of 3" in line


# -- source_state in place of the ledger notes ----------------------------------------------------------------------


def ledger_row(ctx, note, *, days_ago=1, job=uni.JOB):
    ctx.store.insert("credit_ledger", [{"entry_id": new_id(), "system": "apollo", "job": job, "run_id": "r",
                                        "credits": 1.0, "occurred_at": NOW - timedelta(days=days_ago),
                                        "note": json.dumps(note)}])


def test_migrate_state_copies_the_ledger_cursors_and_the_next_run_resumes_at_the_page_the_note_said():
    """This month's notes: search A read to page 3 of 9, search B's page 2 cut by the ceiling (so its place is page 1:
    the cut page is read again), and a reservation of A's page 4 with an extra "slice" key under "reserved": a page
    never answered moves no cursor. Last month's note for A (page 7) is not this month's."""
    ctx = ctx_at()
    ledger_row(ctx, {"slice": "A", "page": 2, "pages": 9, "results": 100})
    ledger_row(ctx, {"slice": "A", "page": 3, "pages": 9, "results": 100})
    ledger_row(ctx, {"slice": "B", "page": 2, "pages": 5, "results": 100, "cut": 40})
    ledger_row(ctx, {"reserved": {"search": "A", "page": 4, "slice": "A"}})
    ledger_row(ctx, {"slice": "A", "page": 7, "pages": 9, "results": 100}, days_ago=30)  # September
    dry = source_state.migrate(ctx)  # dry-run: prints, writes nothing
    assert dry["dry_run"] and dry["cursors"]["copied"] == {"A": {"page": 3, "pages": 9}, "B": {"page": 1, "pages": 5}}
    assert ctx.store.select("source_state") == []
    live = source_state.migrate(make_context(SETTINGS, job="x", now=NOW, live=True, store=ctx.store))
    assert not live["dry_run"] and live["cursors"]["cursors"] == 2
    got = uni.cursors(ctx)
    assert (got["A"].page, got["A"].pages, got["B"].page) == (3, 9, 1)  # the next run reads A's page 4, B's page 2
    again = source_state.migrate(make_context(SETTINGS, job="x", now=NOW, live=True, store=ctx.store))
    assert again["cursors"]["already"]  # once


def test_the_first_run_copies_the_notes_itself_when_nobody_ran_migrate_state():
    ctx = ctx_at()
    ledger_row(ctx, {"slice": "A", "page": 3, "pages": 9, "results": 100})
    assert uni.cursors(ctx)["A"].page == 3
    ledger_row(ctx, {"slice": "A", "page": 5, "pages": 9, "results": 100})  # a note written after the copy
    assert uni.cursors(ctx)["A"].page == 3  # the ledger is never read as a place again


def test_judged_visitors_are_copied_from_the_ledger_once():
    ctx = ctx_at()
    ledger_row(ctx, {"judged": ["o1", "o2"]}, days_ago=3, job="site_visits")
    ledger_row(ctx, {"judged": ["o3"]}, days_ago=40, job="site_visits")  # past the 30 days
    out = source_state.migrate(make_context(SETTINGS, job="x", now=NOW, live=True, store=ctx.store))
    assert out["judged"]["judged"] == 2
    assert set(source_state.get(ctx.store, "site_visits:judged")) == {"o1", "o2"}
