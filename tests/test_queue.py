"""Today's number, selection order, test versions and senders (SPEC 9; weekly targets, Harry 30 Sep 2026)."""

from __future__ import annotations

import dataclasses
import random
import uuid

from us_outbound.enrol import queue
from us_outbound.settings.model import Focus
from tests.test_render import AGENCIES, HANNAH, HARRY_M, HARRY_T, SAM, TECH, make_settings

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


def explore(share: float):
    return make_settings(explore_share=share)


def test_explore_share_0_means_no_random_arm_and_the_default_is_harry_s_0_15():
    """explore_share 0 on the sheet draws nobody at random, however many are wanted or waiting. Its code default is
    0.15 from W2-2 (Harry's D7, 19 Oct 2026: "Rank + 15% random arm"), which retired the tiers and the Control tier's
    0.15, so a sheet without the row keeps a comparison arm: 0.15 × 25 = 3.75 → 4."""
    assert S.general.explore_share == 0.15 and queue.explore_count(25, S, 60) == 4
    off = explore(0.0)
    assert queue.explore_count(25, off, 60) == 0
    assert queue.explore_count(4, off, 60) == 0  # not even the at-least-one from 4 a day
    assert queue.split([row(i, "Queue", 90 - i) for i in range(10)], off, "pick:2026-10-27", 0)[0] == []


def test_explore_count_rounds_half_up_with_one_from_four_and_never_more_than_available():
    # 0.15 × 25 = 3.75 → 4; 0.01 × 4 = 0.04 → 0, raised to 1 as n ≥ 4; 0.01 × 3 = 0.03 → 0 and 3 < 4 (probe M19);
    # 0.15 × 25 = 3.75 → 4 but only 2 are available → 2; 0.15 × 30 = 4.5 → 5 (half up, not banker's 4).
    assert queue.explore_count(25, explore(0.15), 5) == 4
    assert queue.explore_count(4, explore(0.01), 5) == 1
    assert queue.explore_count(3, explore(0.01), 5) == 0
    assert queue.explore_count(25, explore(0.15), 2) == 2
    assert queue.explore_count(30, explore(0.15), 100) == 5
    assert queue.explore_count(0, explore(0.15), 100) == 0
    assert queue.explore_count(30, explore(0.15), 0) == 0


def test_apportion_gives_the_largest_remainders_the_places_left():
    # k = 9 over 50/30/20: exact 4.5, 2.7, 1.8; whole parts 4, 2, 1 (7); the 2 left go to the largest fractions,
    # Legal's 0.8 and Creative's 0.7, so 4, 3, 2 (sums to 9). (The plan's "5, 3, 1" sums to 9 but is neither
    # rounding, 5 + 3 + 2 = 10, nor largest remainder.)
    assert queue.apportion(9, {"Tech": 0.5, "Creative": 0.3, "Legal": 0.2}) == {"Tech": 4, "Creative": 3, "Legal": 2}
    # k = 4 over 50/30/20: exact 2.0, 1.2, 0.8; whole 2, 1, 0; the 1 left to Legal's 0.8: 2, 1, 1.
    assert queue.apportion(4, {"Tech": 0.5, "Creative": 0.3, "Legal": 0.2}) == {"Tech": 2, "Creative": 1, "Legal": 1}
    # Equal fractions go to the larger share: k = 1 over 50/50 by order given, 1 over 0.6/0.4 to the 0.6.
    assert queue.apportion(1, {"A": 0.5, "B": 0.5}) == {"A": 1, "B": 0}
    assert queue.apportion(1, {"A": 0.4, "B": 0.6}) == {"A": 0, "B": 1}
    assert queue.apportion(0, {"A": 1.0}) == {"A": 0}


def p1_world(n_tech=160, n_creative=96, n_legal=64) -> tuple[list[dict], dict[str, str]]:
    """Probe P1's world (code-contacts-enrol H2; code-scoring B1): 320 verified accounts needing a contact, the groups
    in the Focus mix (160/96/64 = 50/30/20) and interleaved through the score order, so each group spans every rank."""
    rows, group = [], {}
    names = ["T"] * n_tech + ["C"] * n_creative + ["L"] * n_legal
    order = sorted(range(len(names)), key=lambda i: (i % 5, i))  # deal the groups across the ranks
    ind = {"T": "Fintech", "C": "Advertising agencies", "L": "Legal Teams"}
    for rank, i in enumerate(order):
        r = row(i, "Standard", 1000 - rank, "20-49", ind[names[i]])
        rows.append(r)
        group[r["account_id"]] = names[i]
    return rows, group


FOCUS_50_30_20 = (Focus(TECH, 0.5), Focus(AGENCIES, 0.3), Focus("Legal Teams", 0.2))


def test_the_random_arm_is_stratified_by_focus_group_and_reaches_past_the_first_60():
    """Probe P1: with explore_share 0.15 and a lookahead of 60, k = round(0.15 × 60 = 9.0) = 9, apportioned over Focus
    50/30/20 to 4 Tech, 3 Creative, 2 Legal (test_apportion...). Before W1-6 pick revealed only the first 60 in order,
    so a Control account at rank 201 was never reached; the draw is uniform within each group over all 320, so the
    chance that all 9 land in the top 60 is about (60/320)^9 ≈ 3 × 10⁻⁷ per seed: every one of 200 seeds reaches past
    rank 60."""
    s = make_settings(explore_share=0.15, focus=FOCUS_50_30_20)
    rows, group = p1_world()
    k = queue.explore_count(60, s, len(rows))
    assert k == 9
    rank = {r["account_id"]: i + 1 for i, r in enumerate(sorted(rows, key=lambda r: queue.order_key(r, s)))}
    for seed in range(200):
        drawn, rest = queue.split(rows, s, f"pick:seed-{seed}", k)
        assert len(drawn) == 9 and len(rest) == 311
        assert sorted(group[r["account_id"]] for r in drawn) == ["C"] * 3 + ["L"] * 2 + ["T"] * 4
        assert max(rank[r["account_id"]] for r in drawn) > 60
        assert [r["account_id"] for r in rest] == [a for a in sorted(rank, key=rank.get)
                                                   if a not in {d["account_id"] for d in drawn}]
    # The same seed draws the same accounts (a rerun on the same day); another seed, others.
    one = [r["account_id"] for r in queue.split(rows, s, "pick:2026-10-27", k)[0]]
    assert one == [r["account_id"] for r in queue.split(rows, s, "pick:2026-10-27", k)[0]]
    assert one != [r["account_id"] for r in queue.split(rows, s, "pick:2026-10-28", k)[0]]


def test_a_group_short_of_its_places_gives_what_it_has_and_never_borrows():
    # 9 places, 4/3/2, but only 1 Legal account: 4 Tech + 3 Creative + 1 Legal = 8 drawn; the 9th place goes to the
    # ranked rest, not to another group, so the arm keeps the Focus mix (RT §4.1).
    s = make_settings(explore_share=0.15, focus=FOCUS_50_30_20)
    rows, group = p1_world(n_legal=1)
    drawn, rest = queue.split(rows, s, "pick:x", 9)
    assert sorted(group[r["account_id"]] for r in drawn) == ["C"] * 3 + ["L"] + ["T"] * 4
    assert len(rest) == len(rows) - 8


def test_strata_give_each_group_its_places_and_its_whole_pool_in_a_random_order():
    """pick_contacts tries each stratum's pool in order until its places have a kept contact, so each pool is the
    whole group, not only its places: 9 places over 50/30/20 (4/3/2), and the 160 Tech, 96 Creative and 64 Legal
    accounts of probe P1's world, each in an order the seed sets. split's draw is each pool's first places."""
    s = make_settings(explore_share=0.15, focus=FOCUS_50_30_20)
    rows, group = p1_world()
    st = queue.strata(rows, s, "pick:x", 9)
    assert [(places, len(pool), {group[r["account_id"]] for r in pool}) for _, places, pool in st] == [
        (4, 160, {"T"}), (3, 96, {"C"}), (2, 64, {"L"})]
    assert [r["account_id"] for _, p, pool in st for r in pool[:p]] == \
        [r["account_id"] for r in queue.split(rows, s, "pick:x", 9)[0]]
    again = queue.strata(rows, s, "pick:x", 9)
    assert [[r["account_id"] for r in pool] for _, _, pool in st] == [[r["account_id"] for r in pool] for _, _, pool in again]
    other = queue.strata(rows, s, "pick:y", 9)
    assert [r["account_id"] for r in st[0][2]] != [r["account_id"] for r in other[0][2]]
    assert queue.strata(rows, s, "pick:x", 0) == []
    no_focus = queue.strata(rows, make_settings(explore_share=0.15), "pick:x", 9)
    assert [(g, p, len(pool)) for g, p, pool in no_focus] == [("", 9, 320)]


def test_without_focus_rows_the_draw_is_uniform_over_all():
    s = make_settings(explore_share=0.15)
    rows, _ = p1_world()
    drawn, rest = queue.split(rows, s, "pick:x", 9)
    assert len(drawn) == 9 and len(rest) == 311 and not {r["account_id"] for r in drawn} & {r["account_id"] for r in rest}


def test_the_sham_arm_is_the_parity_of_its_own_hash_and_about_half_each():
    import hashlib

    for aid in ("acc-1", "acc-2", "acc-3"):
        h = int(hashlib.sha256(f"sham:{aid}2026-10-19".encode()).hexdigest(), 16)
        assert queue.sham_arm(aid) == ("a" if h % 2 == 0 else "b")
    # 200 accounts: a binomial(200, 0.5) lies in 80..120 with probability above 99.5%, so a fixed draw of ids does.
    rng = random.Random(20261019)
    ids = [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(200)]
    n_a = sum(queue.sham_arm(i) == "a" for i in ids)
    assert 80 <= n_a <= 120
    # It cuts across the copy test's split: not the same label under another name.
    assert any(queue.sham_arm(i) != queue.test_version(i, "t1-agencies") for i in ids)


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


def test_the_order_has_no_tier():
    """W2-2's plan case: scores 45, 40 and 40 (bands 20-49 and 10-19) give the 45, then the 20-49 one, then the 10-19
    one; and a tier never moves an account (a Control 45 before a Priority 40, as the tiers are retired)."""
    rows = [row(1, "Priority", 40, "10-19"), row(2, "Priority", 40, "20-49"), row(3, "Control", 45, "10-19")]
    assert [r["account_id"] for r in sorted(rows, key=lambda r: queue.order_key(r, S))] == ["a003", "a002", "a001"]
    assert queue.order_key(row(1, "Priority", 40), S) == queue.order_key(row(1, "Control", 40), S) \
        == queue.order_key(row(1, "Queue", 40), S)


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
