"""Daily enrollment number, working days, selection order, test versions and senders (SPEC 9)."""

from __future__ import annotations

import dataclasses
import random
import uuid
from datetime import UTC, date, datetime

from us_outbound.enrol import queue
from tests.test_render import HANNAH, HARRY_M, HARRY_T, SAM, make_settings

S = make_settings()


def number(**kw):
    args = dict(
        active_mailbox_caps=[30, 30, 30, 30], clay_remaining=100_000.0, clay_per_account=5.0,
        apollo_remaining=1500.0, apollo_per_account=1.0, working_days_left=20, verified_queue_size=500,
    )
    args.update(kw)
    return queue.daily_number(S, **args)


# -- daily number --------------------------------------------------------------------------


def test_four_mailboxes_at_30_give_30_a_day():
    n, why = number()
    assert n == 30
    assert why["mailbox_capacity"] == 30 and why["daily_enrol_cap"] == 30
    assert why["clay_budget"] == 1000 and why["apollo_budget"] == 75


def test_clay_budget_binds():
    n, why = number(clay_remaining=2000.0, clay_per_account=5.0, working_days_left=20)
    assert n == 20 and why["binding"] == "clay_budget"


def test_apollo_budget_binds_with_floor_division():
    n, why = number(apollo_remaining=299.0, apollo_per_account=1.0, working_days_left=20)
    assert n == 14 and why["binding"] == "apollo_budget"


def test_verified_queue_binds():
    n, why = number(verified_queue_size=7)
    assert n == 7 and why["binding"] == "verified_queue"


def test_unknown_credits_per_account_is_left_out_and_recorded():
    n, why = number(clay_remaining=0.0, clay_per_account=0.0)
    assert n == 30 and why["clay_budget"] is None
    assert why["left_out"] == ["clay_budget: credits per account unknown"]


def test_never_negative():
    n, _ = number(clay_remaining=-500.0)
    assert n == 0
    n, _ = number(active_mailbox_caps=[])
    assert n == 0
    n, _ = number(working_days_left=0)
    assert n == 0


def test_capacity_counts_only_the_caps_given():
    n, why = number(active_mailbox_caps=[30, 30])
    assert n == 15 and why["binding"] == "mailbox_capacity"


# -- calendar -------------------------------------------------------------------------------


def test_working_days_exclude_weekends_and_the_thanksgiving_blackout():
    assert queue.working_days_left(date(2026, 11, 2), S) == 16  # 21 weekdays less 23–27 Nov
    no_blackout = make_settings(blackout_dates=())
    assert queue.working_days_left(date(2026, 11, 2), no_blackout) == 21
    assert queue.working_days_left(date(2026, 11, 30), S) == 1  # the last day counts
    assert queue.working_days_left(date(2026, 11, 28), S) == 1  # Saturday: only Monday 30th is left
    assert queue.working_days_left(date(2026, 12, 1), S) == 13  # 1–17 Dec, then the holiday blackout


def test_is_blackout():
    assert queue.is_blackout(date(2026, 11, 23), S) and queue.is_blackout(date(2026, 11, 27), S)
    assert not queue.is_blackout(date(2026, 11, 28), S) and not queue.is_blackout(date(2026, 11, 20), S)
    assert queue.is_blackout(date(2027, 1, 4), S)


def test_in_send_window_is_eastern_time():
    assert queue.in_send_window(datetime(2026, 10, 27, 13, 0, tzinfo=UTC), S)  # 09:00 EDT
    assert not queue.in_send_window(datetime(2026, 10, 27, 12, 59, tzinfo=UTC), S)
    assert not queue.in_send_window(datetime(2026, 10, 27, 20, 0, tzinfo=UTC), S)  # 16:00 EDT, end exclusive
    assert not queue.in_send_window(datetime(2026, 10, 31, 15, 0, tzinfo=UTC), S)  # Saturday


# -- selection ------------------------------------------------------------------------------


def row(i: int, tier: str, score: int = 30, band: str = "50-99", industry: str = "Advertising agencies") -> dict:
    return {"account_id": f"a{i:03d}", "tier": tier, "score": score, "size_band": band, "industry": industry,
            "first_seen": f"2026-10-{1 + i % 28:02d}"}


def test_control_share_rounds_half_up():
    assert queue.control_count(30, S, 100) == 5  # 15% of 30 is 4.5
    assert queue.control_count(20, S, 100) == 3
    assert queue.control_count(4, S, 100) == 1  # 0.6 rounds to 1
    assert queue.control_count(5, S, 100) == 1
    assert queue.control_count(3, S, 100) == 0  # 0.45 rounds to 0, and 3 is under 4
    assert queue.control_count(30, S, 2) == 2  # never more than there are
    assert queue.control_count(30, S, 0) == 0
    one_pct = make_settings(control_share=0.01)
    assert queue.control_count(10, one_pct, 5) == 1  # at least one from 4 a day


def test_select_takes_control_share_then_priority_then_standard():
    rows = [row(i, "Control") for i in range(10)] + [row(100 + i, "Priority", 60) for i in range(10)]
    rows += [row(200 + i, "Standard", 30) for i in range(40)] + [row(300, "Held", 90), row(301, "Excluded", 90)]
    picked = queue.select(rows, 30, S)
    tiers = [r["tier"] for r in picked]
    assert len(picked) == 30 and tiers.count("Control") == 5
    assert tiers[:10] == ["Priority"] * 10 and tiers[10:25] == ["Standard"] * 15
    assert "Held" not in tiers and "Excluded" not in tiers


def test_short_control_is_filled_from_priority_and_standard():
    rows = [row(1, "Control")] + [row(100 + i, "Standard") for i in range(40)]
    picked = queue.select(rows, 30, S)
    assert len(picked) == 30 and [r["tier"] for r in picked].count("Control") == 1


def test_short_priority_and_standard_are_filled_from_control():
    rows = [row(i, "Control") for i in range(10)] + [row(100 + i, "Priority", 60) for i in range(2)]
    picked = queue.select(rows, 3, S)  # 15% of 3 rounds to no Control, but only 2 others wait
    assert [r["tier"] for r in picked] == ["Priority", "Priority", "Control"]
    assert len(queue.select(rows, 30, S)) == 12


def test_order_is_score_then_size_band_then_industry_priority():
    rows = [
        row(1, "Standard", 30, "10-19"),
        row(2, "Standard", 30, "100-249"),
        row(3, "Standard", 30, "20-49", "Advertising agencies"),  # industry priority 2
        row(4, "Standard", 30, "50-99", "Fintech"),  # industry priority 1
        row(5, "Standard", 45, "10-19"),
        row(6, "Priority", 50, "10-19"),
        row(7, "Standard", 30, "20-49", "Not on the tab"),  # priority 99
    ]
    ids = [r["account_id"] for r in queue.ordered(rows, S)]
    assert ids == ["a006", "a005", "a004", "a003", "a007", "a002", "a001"]


# -- test versions ----------------------------------------------------------------------------


def test_test_version_is_deterministic_and_even():
    assert queue.test_version("acc-1", "t1") == queue.test_version("acc-1", "t1")
    rng = random.Random(20261027)
    ids = [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(10_000)]
    share_a = sum(queue.test_version(i, "t1-eap-opener") == "a" for i in ids) / len(ids)
    assert 0.48 <= share_a <= 0.52
    assert {queue.test_version(i, "t1-eap-opener") for i in ids[:50]} == {"a", "b"}


def test_test_version_is_the_spec_hash():
    import hashlib

    for aid in ("acc-1", "acc-2", "acc-3", "acc-4"):
        want = "a" if int(hashlib.sha256((aid + "t9").encode()).hexdigest(), 16) % 2 == 0 else "b"
        assert queue.test_version(aid, "t9") == want


# -- senders ------------------------------------------------------------------------------------


def test_campaign_name():
    assert queue.campaign_name("Hannah Spalding") == "US Outbound – Hannah Spalding"


def test_new_accounts_go_to_the_most_free_capacity():
    assert queue.free_capacity(S, {}) == {"Hannah Spalding": 7, "Harry Dryden": 15, "Sam Jackson": 7}
    assert queue.assign_sender({"account_id": "x"}, S, {}) == "Harry Dryden"
    assert queue.assign_sender({"account_id": "x"}, S, {"Harry Dryden": 10}) == "Hannah Spalding"  # 7 each, by name
    assert queue.assign_sender({"account_id": "x"}, S, {"Harry Dryden": 10, "Hannah Spalding": 3}) == "Sam Jackson"


def test_every_owner_full_still_assigns_the_least_loaded():
    full = {"Harry Dryden": 15, "Hannah Spalding": 7, "Sam Jackson": 7}
    assert queue.assign_sender({"account_id": "x"}, S, full) == "Hannah Spalding"


def test_existing_sender_is_kept():
    assert queue.assign_sender({"sender": "Sam Jackson"}, S, {"Sam Jackson": 50}) == "Sam Jackson"


def test_paused_sender_means_wait():
    paused = make_settings(mailboxes=(HANNAH, HARRY_M, dataclasses.replace(SAM, status="Paused"), HARRY_T))
    assert queue.assign_sender({"sender": "Sam Jackson"}, paused, {}) is None
    assert "Sam Jackson" not in queue.free_capacity(paused, {})
    assert queue.assign_sender({"sender": "Someone Gone"}, paused, {}) is None


def test_harry_keeps_his_account_with_one_mailbox_paused():
    one = make_settings(mailboxes=(HANNAH, HARRY_M, SAM, dataclasses.replace(HARRY_T, status="Paused")))
    assert queue.assign_sender({"sender": "Harry Dryden"}, one, {}) == "Harry Dryden"
    assert queue.free_capacity(one, {})["Harry Dryden"] == 7


def test_no_active_mailbox_means_no_sender():
    none = make_settings(mailboxes=tuple(dataclasses.replace(m, status="Warming") for m in (HANNAH, SAM)))
    assert queue.assign_sender({"account_id": "x"}, none, {}) is None
