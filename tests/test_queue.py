"""Today's number, selection order, test versions and senders (SPEC 9; weekly targets, Harry 30 Sep 2026)."""

from __future__ import annotations

import dataclasses
import random
import uuid

from us_outbound.enrol import queue
from tests.test_render import HANNAH, HARRY_M, HARRY_T, SAM, make_settings

S = make_settings()


def number(**kw):
    args = dict(weekly_target=30, sending_capacity=30, ready_accounts=500)
    args.update(kw)
    return queue.daily_number(**args)


# -- daily number --------------------------------------------------------------------------


def test_the_smallest_term_is_the_number():
    n, why = number()
    assert n == 30 and why["binding"] == "weekly_target"  # ties go to the first term
    assert (why["weekly_target"], why["sending_capacity"], why["ready_accounts"]) == (30, 30, 500)


def test_sending_capacity_binds():
    n, why = number(sending_capacity=12)
    assert n == 12 and why["binding"] == "sending_capacity"


def test_ready_accounts_bind():
    n, why = number(ready_accounts=7)
    assert n == 7 and why["binding"] == "ready_accounts"


def test_never_negative():
    n, _ = number(sending_capacity=-3)
    assert n == 0


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
    ids = [r["account_id"] for r in sorted(rows, key=lambda r: queue.order_key(r, S))]
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


FREE = {"Hannah Spalding": 7, "Harry Dryden": 15, "Sam Jackson": 7}
CAPS = {"Hannah Spalding": 30, "Harry Dryden": 60, "Sam Jackson": 30}  # proportional, like each owner's pace


def test_new_accounts_go_to_the_most_free_capacity_as_a_share():
    assert queue.assign_sender({"account_id": "x"}, S, FREE, CAPS) == "Harry Dryden"  # 15/60 beats 7/30
    assert queue.assign_sender({"account_id": "x"}, S, {**FREE, "Harry Dryden": 5}, CAPS) == "Hannah Spalding"  # ties by name
    assert queue.assign_sender({"account_id": "x"}, S, {**FREE, "Harry Dryden": 5, "Hannah Spalding": 3}, CAPS) == "Sam Jackson"
    assert queue.assign_sender({"account_id": "x"}, S, {"Harry Dryden": 20, "Sam Jackson": 11}, CAPS) == "Sam Jackson"  # 11/30 > 20/60


def test_owners_fill_in_proportion_to_their_capacity():
    free, got = dict(CAPS), {o: 0 for o in CAPS}
    for _ in range(40):
        o = queue.assign_sender({"account_id": "x"}, S, free, CAPS)
        free[o] -= 1
        got[o] += 1
    assert got == {"Harry Dryden": 20, "Hannah Spalding": 10, "Sam Jackson": 10}


def test_every_owner_full_means_wait():
    assert queue.assign_sender({"account_id": "x"}, S, {o: 0 for o in FREE}) is None


def test_existing_sender_is_kept_while_they_have_a_free_slot():
    assert queue.assign_sender({"sender": "Sam Jackson"}, S, {"Sam Jackson": 1}) == "Sam Jackson"
    assert queue.assign_sender({"sender": "Sam Jackson"}, S, {"Sam Jackson": 0, "Harry Dryden": 9}) is None  # waits, never moves


def test_paused_sender_means_wait():
    paused = make_settings(mailboxes=(HANNAH, HARRY_M, dataclasses.replace(SAM, status="Paused"), HARRY_T))
    assert queue.assign_sender({"sender": "Sam Jackson"}, paused, FREE) is None
    assert queue.assign_sender({"account_id": "x"}, paused, {"Sam Jackson": 50, "Hannah Spalding": 1}) == "Hannah Spalding"
    assert queue.assign_sender({"sender": "Someone Gone"}, paused, FREE) is None


def test_harry_keeps_his_account_with_one_mailbox_paused():
    one = make_settings(mailboxes=(HANNAH, HARRY_M, SAM, dataclasses.replace(HARRY_T, status="Paused")))
    assert queue.assign_sender({"sender": "Harry Dryden"}, one, FREE) == "Harry Dryden"


def test_no_active_mailbox_means_no_sender():
    none = make_settings(mailboxes=tuple(dataclasses.replace(m, status="Warming") for m in (HANNAH, SAM)))
    assert queue.assign_sender({"account_id": "x"}, none, {}) is None
