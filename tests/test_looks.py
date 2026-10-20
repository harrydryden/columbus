"""Tests read only at pre-registered looks (learn/looks.py; SPEC 12; Harry, 6 Oct 2026): the Tests tab's kind and
looks, the no-peek rule, what each look covers, holdout tests, and `test start` for them."""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_cli import Harness, _approved, _split, _tests_tab
from tests.test_registry import SETTINGS
from us_outbound.learn import looks
from us_outbound.settings import load
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.model import Test as CopyTest
from us_outbound.settings.validate import parse_looks, validate_all

NOW = datetime(2026, 11, 30, 12, tzinfo=UTC)
T0 = datetime(2026, 10, 1, 15, tzinfo=UTC)  # the first step 1s; their windows close on 29 Oct


def ab_test(**kw) -> CopyTest:
    base = dict(test_id="t1", hypothesis="h", version_a="eap-v1", version_b="general-v1", accounts_per_version=400,
                status="running", start_date=date(2026, 9, 28), read_date=date(2026, 12, 21), decision_rule="reply rate")
    return CopyTest(**{**base, **kw})


def world(test: CopyTest, now: datetime = NOW):
    ctx = make_context(dataclasses.replace(SETTINGS, tests=(test,)), now=now)
    return ctx


def emailed(ctx, i: int, arm: str, at: datetime, *, column: str = "copy_version", test_id: str | None = "t1",
            reply: str | None = None, reply_after: timedelta = timedelta(days=2), meeting_after: timedelta | None = None,
            bounced: bool = False, enrolled_at: datetime | None = None):
    """One account in an arm: its contact, step 1 at `at`, and what followed."""
    aid, cid = f"a{i}", f"k{i}"
    ctx.store.insert("contacts", [{"contact_id": cid, "account_id": aid, "test_id": test_id, column: arm,
                                   "enrolled_at": enrolled_at or at - timedelta(hours=2)}])
    events = [{"event_id": f"s{i}", "type": "sent", "step": 1, "account_id": aid, "contact_id": cid, "occurred_at": at}]
    if reply:
        events.append({"event_id": f"r{i}", "type": "replied", "account_id": aid, "contact_id": cid,
                       "reply_class": reply, "occurred_at": at + reply_after})
    if meeting_after is not None:
        events.append({"event_id": f"m{i}", "type": "meeting_booked", "account_id": aid, "occurred_at": at + meeting_after})
    if bounced:
        events.append({"event_id": f"b{i}", "type": "bounced", "step": 1, "account_id": aid, "contact_id": cid,
                       "occurred_at": at})
    ctx.store.insert("events", events)


# -- the Tests tab ------------------------------------------------------------------------------------------------------


@pytest.fixture
def tabs():
    t = copy.deepcopy(default_tabs())
    for c in t["Copy"]:
        c.update(status="approved", approved_by="Harry")
    return t


def set_test_row(t, **kw):
    t["Tests"][0].update(kw)
    return t


def test_kind_and_looks_are_optional_columns(tabs):
    for r in tabs["Tests"]:
        del r["kind"], r["looks"]
    settings, errors = validate_all(tabs)
    assert not any(errors.values())
    assert (settings.tests[0].kind, settings.tests[0].looks) == ("ab", ())
    assert COLUMNS["Tests"][:2] == ["test_id", "kind"] and "looks" in COLUMNS["Tests"]


def test_looks_parse_and_check_against_the_test(tabs):
    assert parse_looks("200; 2026-11-16, 300") == (200, date(2026, 11, 16), 300)
    for bad in ("200; 200", "0", "soon", "2026-13-01"):
        with pytest.raises(ValueError):
            parse_looks(bad)
    t = set_test_row(tabs, status="running", start_date="2026-10-05", read_date="2026-12-14",
                  looks="100; 2026-11-16", version_a="legal-teams-people-v1", version_b="legal-teams-founder-v1")
    settings, errors = validate_all(t)
    assert not any(errors.values()) and settings.tests[0].looks == (100, date(2026, 11, 16))
    for looks_text, message in (("500", "more than accounts_per_version (400)"),
                                ("2026-12-14", "must be before read_date"),
                                ("2026-10-01", "must be after start_date")):
        _, errors = validate_all(set_test_row(copy.deepcopy(t), looks=looks_text))
        [e] = errors["Tests"]
        assert e.column == "looks" and message in e.message


def test_an_ab_test_takes_the_copy_rows_own_spelling(tabs):
    """9 Oct 2026: a version written in other capitals validated, then enrol (which matches exactly) found no row."""
    t = set_test_row(tabs, status="running", start_date="2026-10-05", read_date="2026-12-14",
                     version_a="LEGAL-TEAMS-PEOPLE-V1", version_b="Legal-Teams-Founder-V1")
    settings, errors = validate_all(t)
    assert not any(errors.values())
    test = settings.running_test()
    assert (test.version_a, test.version_b) == ("legal-teams-people-v1", "legal-teams-founder-v1")
    assert settings.copy_row(test.version_a) is not None


def test_a_holdout_compares_the_two_arms_of_one_split(tabs):
    t = set_test_row(tabs, kind="holdout", version_a="Opener", version_b="holdout", status="running",
                  start_date="2026-10-05", read_date="2026-12-14")
    settings, errors = validate_all(t)
    assert not any(errors.values())  # no Copy row needed: the arms are what enrol records
    held = settings.tests[0]
    assert (held.kind, held.version_a, held.version_b) == ("holdout", "opener", "holdout")
    assert settings.running_test() is None  # enrol assigns copy for the ab test only
    _, errors = validate_all(set_test_row(copy.deepcopy(t), version_b="copy"))
    assert "two arms of one split" in errors["Tests"][0].message


def test_a_holdout_may_run_beside_the_copy_test_but_two_copy_tests_may_not(tabs):
    tabs["Tests"][0].update(status="running", start_date="2026-10-05", read_date="2026-12-14",
                            version_a="legal-teams-people-v1", version_b="legal-teams-founder-v1")
    holdout = dict(tabs["Tests"][0], test_id="t2", kind="holdout", version_a="personal", version_b="copy")
    settings, errors = validate_all({**tabs, "Tests": [*tabs["Tests"], holdout]})
    assert not any(errors.values()) and settings.running_test().test_id == "t1-eap-opener"
    second = dict(tabs["Tests"][0], test_id="t3")
    _, errors = validate_all({**tabs, "Tests": [*tabs["Tests"], holdout, second]})
    assert "only one copy test (ab or variant) runs at a time" in errors["Tests"][0].message


def test_settings_load_brings_the_new_columns_and_keeps_harrys_values():
    sheet = [{k: v for k, v in _tests_tab(test_id="t1-eap-opener", status="running", start_date="2026-10-05").items()},
             _tests_tab(test_id="harrys-own")]
    plan = load.plan_tab("Tests", sheet, default_tabs()["Tests"])
    assert plan.new_columns == ["kind", "looks", "email", "change", "text_a", "text_b", "find", "share_a"]
    rows = {r["test_id"]: r for r in plan.rows}
    assert rows["t1-eap-opener"]["status"] == "running" and rows["t1-eap-opener"]["kind"] == "ab"
    assert rows["harrys-own"]["kind"] == "" and plan.extra == ["harrys-own"]
    assert "Tests" in load.LOADABLE


# -- the no-peek rule -----------------------------------------------------------------------------------------------------


def test_before_the_first_look_the_read_refuses_and_shows_no_reply():
    ctx = world(ab_test(looks=(2,)), now=datetime(2026, 11, 1, 12, tzinfo=UTC))
    emailed(ctx, 0, "eap-v1", T0, reply="positive")
    emailed(ctx, 1, "eap-v1", T0 + timedelta(days=10), reply="positive")
    emailed(ctx, 2, "general-v1", T0)
    with pytest.raises(looks.NotYet) as exc:
        looks.read(ctx, "t1")
    message = str(exc.value)
    assert "reached no pre-registered look" in message and "the first is look 1: 2 accounts per arm" in message
    assert "eap-v1 2 emailed (1 window closed), general-v1 1 emailed (1 window closed)" in message
    assert "replied" not in message and "positive" not in message and "%" not in message


def test_a_count_look_reads_the_first_n_per_arm_once_their_windows_close():
    ctx = world(ab_test(looks=(2,)))
    for i, (arm, day, reply) in enumerate([("eap-v1", 0, "positive"), ("eap-v1", 1, None), ("eap-v1", 2, "objection"),
                                           ("general-v1", 0, None), ("general-v1", 3, "out_of_office")]):
        emailed(ctx, i, arm, T0 + timedelta(days=day), reply=reply)
    emailed(ctx, 5, "general-v1", T0 + timedelta(days=4), reply="positive", reply_after=timedelta(days=30))  # too late
    emailed(ctx, 6, "general-v1", T0 + timedelta(days=5), bounced=True)  # not delivered
    result = looks.read(ctx, "t1")
    assert result["look"]["label"] == "look 1: 2 accounts per arm with closed reply windows"
    # The slower arm's second account (general-v1's, step 1 on 4 Oct) closed its window on 1 Nov.
    assert result["look"]["reached_at"] == (T0 + timedelta(days=3, weeks=4)).isoformat()
    eap, general = result["versions"]["eap-v1"], result["versions"]["general-v1"]
    assert (eap["accounts"], eap["delivered"], eap["replied"], eap["positive"]) == (3, 2, 1, 1)  # the third is not read
    assert (general["accounts"], general["delivered"], general["replied"]) == (4, 2, 0)  # out-of-office is no reply
    assert result["next_look"] == "look 2 (the read date): Mon 21 Dec 2026"


def test_an_uneven_test_s_count_look_reads_each_arm_in_its_share():
    """At 70/30 a look at 3 is 3 accounts in the smaller arm and 7 in the larger (Harry, 8 Oct 2026)."""
    test = ab_test(looks=(3,), share_a=0.7)
    ctx = world(test)
    for i in range(6):
        emailed(ctx, i, "eap-v1", T0 + timedelta(hours=i), reply="positive" if i < 2 else None)
    for i in range(6, 9):
        emailed(ctx, i, "general-v1", T0)
    [first, _] = looks.looks(test, looks.arms(ctx, test))
    assert first.reached_at is None and first.label == (
        "look 1: eap-v1 7 and general-v1 3 accounts with closed reply windows")
    emailed(ctx, 9, "eap-v1", T0 + timedelta(days=1))
    emailed(ctx, 10, "eap-v1", T0 + timedelta(days=2), reply="positive")  # the eighth: not read
    result = looks.read(ctx, "t1")
    assert result["look"]["reached_at"] == (T0 + timedelta(days=1, weeks=4)).isoformat()
    eap, general = result["versions"]["eap-v1"], result["versions"]["general-v1"]
    assert (eap["delivered"], eap["positive"], general["delivered"]) == (7, 2, 3)


def test_a_date_look_reads_the_windows_closed_by_its_date_and_meetings_until_then():
    ctx = world(ab_test(looks=(date(2026, 11, 2),)))
    emailed(ctx, 0, "eap-v1", T0, reply="positive", meeting_after=timedelta(days=10))
    emailed(ctx, 1, "eap-v1", T0 + timedelta(days=1), meeting_after=timedelta(days=40))  # booked after the look
    emailed(ctx, 2, "eap-v1", T0 + timedelta(days=20), reply="positive")  # window open on 2 Nov: not read
    emailed(ctx, 3, "general-v1", T0, reply="objection")
    result = looks.read(ctx, "t1")
    eap = result["versions"]["eap-v1"]
    assert (eap["delivered"], eap["replied"], eap["meetings"], eap["meeting_rate"]) == (2, 1, 1, 0.5)
    assert result["versions"]["general-v1"]["reply_rate"] == 1.0
    assert [lk["reached"] for lk in result["looks"]] == [True, False]


def test_the_latest_look_reached_is_read():
    ctx = world(ab_test(looks=(1, date(2026, 11, 20))), now=datetime(2026, 11, 25, tzinfo=UTC))
    emailed(ctx, 0, "eap-v1", T0)
    emailed(ctx, 1, "general-v1", T0)
    emailed(ctx, 2, "eap-v1", T0 + timedelta(days=10), reply="positive")
    emailed(ctx, 3, "general-v1", T0 + timedelta(days=10))
    result = looks.read(ctx, "t1")
    assert result["look"]["number"] == 2 and result["versions"]["eap-v1"]["delivered"] == 2


def test_a_holdout_reads_the_split_enrol_recorded_since_its_start():
    test = ab_test(kind="holdout", version_a="opener", version_b="holdout", start_date=date(2026, 9, 30), looks=(1,))
    ctx = world(test)
    emailed(ctx, 0, "opener", T0, column="opener_arm", test_id=None, reply="positive")
    emailed(ctx, 1, "holdout", T0, column="opener_arm", test_id=None)
    emailed(ctx, 2, "none", T0, column="opener_arm", test_id=None, reply="positive")  # neither arm
    emailed(ctx, 3, "opener", T0, column="opener_arm", test_id=None,
            enrolled_at=datetime(2026, 9, 20, tzinfo=UTC))  # before the test started
    result = looks.read(ctx, "t1")
    assert {k: (v["accounts"], v["replied"]) for k, v in result["versions"].items()} == {"opener": (1, 1),
                                                                                         "holdout": (1, 0)}


def test_the_readout_lists_a_test_at_a_look_and_otherwise_its_progress():
    ctx = world(ab_test(looks=(1,)), now=T0 + timedelta(weeks=4, days=3))
    emailed(ctx, 0, "eap-v1", T0, reply="positive")
    emailed(ctx, 1, "general-v1", T0)
    lines, at_look = looks.readout_lines(ctx, ctx.now - timedelta(days=7))
    assert at_look == ["t1"] and lines[0].startswith("  t1 (A/B copy test) reached look 1: 1 accounts per arm")
    assert "eap-v1 1 of 1 replied (100.0%); general-v1 0 of 1 replied (0.0%)" in lines[0]
    assert lines[1].startswith("    *What to do:* apply its decision rule")
    assert "Tests tab>; if it does not decide yet, the test runs on to look 2 (the read date)" in lines[1]
    assert lines[1].endswith("`railway ssh -- us-outbound test read t1` has the full read.")
    lines, at_look = looks.readout_lines(ctx, ctx.now)  # nothing new since
    assert at_look == [] and "no look reached this week" in lines[0] and "next look 2 (the read date)" in lines[0]
    assert "replied" not in lines[0] and "%" not in lines[0]


# -- test start ------------------------------------------------------------------------------------------------------------


def test_a_holdout_starts_without_copy_but_never_beside_another_running_test(capsys):
    """W3-4 (21 Oct 2026; design-final §5 L10): one test runs at a time, so a holdout started from 21 Oct is refused
    beside the running copy test (it ran beside one before); alone, with General live_split = opener (its arms are
    contacts.opener_arm), it starts with no copy at all."""
    running = _tests_tab(test_id="t1", status="running", start_date="2026-10-20")

    def holdout() -> dict:
        return _tests_tab(test_id="t2", kind="holdout", version_a="opener", version_b="holdout", looks="100",
                          look_thresholds="obrien_fleming")

    h = Harness(_split(SETTINGS, "opener"), sheet_tabs={"Tests": [running, holdout()]})
    assert h.run("test", "start", "t2", "--live") == 2
    assert "only one test runs at a time: t1 is running" in capsys.readouterr().err
    h1 = Harness(_split(SETTINGS, "opener"), sheet_tabs={"Tests": [holdout()]})
    assert h1.run("test", "start", "t2", "--live") == 0
    assert h1.sheet_tabs["Tests"][0]["status"] == "running"
    h1b = Harness(_split(SETTINGS, "test"), sheet_tabs={"Tests": [holdout()]})  # the copy test's split, not its own
    assert h1b.run("test", "start", "t2", "--live") == 2
    assert "set General live_split to opener first (it is test)" in capsys.readouterr().err
    third = _tests_tab(test_id="t3")
    h2 = Harness(_split(dataclasses.replace(SETTINGS, copy=_approved())), sheet_tabs={"Tests": [running, third]})
    assert h2.run("test", "start", "t3", "--live") == 2
    assert "only one copy test (ab or variant) runs at a time" in capsys.readouterr().err
    h3 = Harness(dataclasses.replace(SETTINGS, copy=_approved()), sheet_tabs={"Tests": [_tests_tab(looks="soon")]})
    assert h3.run("test", "start", "t1", "--live") == 2
    assert "looks on the Tests tab" in capsys.readouterr().err


# -- the exact test at a look (W2-3; redteam-stats §3 L10) ------------------------------------------------------------


def test_a_read_uses_fisher_s_exact_test_against_the_look_s_threshold():
    """3 of 30 replied with eap-v1, 0 of 30 with general-v1, all matured, read at the read date with no interim look.
    Fisher's two-sided p: margins 3 successes in 60, 30 a side; P(x) = C(30, x)·C(30, 3 − x)/C(60, 3) with C(60, 3) =
    34,220: x = 0 and x = 3 are 4,060/34,220 = 0.11864 each, as likely as the table seen; p = 0.237. The pooled z-test
    this replaced read 0.076 (tests/test_stats.py), below 0.10: a swap back fails here. One look: its threshold is the
    whole alpha, 0.10, so the read does not pass it."""
    ctx = world(ab_test(read_date=date(2026, 11, 20)))
    for i in range(60):
        arm = "eap-v1" if i < 30 else "general-v1"
        emailed(ctx, i, arm, T0, reply="objection" if i < 3 else None)
    result = looks.read(ctx, "t1")
    assert result["reply_test"] == "fisher_exact" and result["reply_p"] == 0.2373
    assert result["look_threshold"] == 0.1 and result["below_threshold"] is False
    assert result["p_b_better"] < 0.05  # general-v1 replied less: P(general-v1 better) is small
    assert "Fisher p = 0.237 (this look's threshold 0.100)" in looks.summary_line(result)


def test_two_looks_use_the_o_brien_fleming_thresholds():
    """A count look at 30 a side and the read date: two looks at alpha 0.10. The first's threshold is 0.0111 (each side
    spends 2 − 2Φ(1.96/√½) = 0.0056; tests/test_stats.py), the read date's 0.0965.

    5 of 30 replied with eap-v1, 0 of 30 with general-v1. Fisher's two-sided p: 5 successes in 60, 30 a side;
    P(x) = C(30, x)·C(30, 5 − x)/C(60, 5), C(60, 5) = 5,461,512; x = 5 and x = 0 are C(30, 5) = 142,506 each, 0.026093,
    and every other table is likelier (x = 1: 30·27,405 = 822,150, 0.1505), so p = 0.0522. That is above the first
    look's 0.0111 (not below its threshold, though below the whole alpha, 0.10: a read that compared p with ALPHA at an
    interim look would stop the test early) and below the read date's 0.0965."""
    ctx = world(ab_test(looks=(30,), read_date=date(2026, 12, 21)))
    for i in range(60):
        emailed(ctx, i, "eap-v1" if i < 30 else "general-v1", T0, reply="objection" if i < 5 else None)
    first = looks.read(ctx, "t1")
    assert first["look"]["number"] == 1 and first["look_threshold"] == 0.0111
    assert first["reply_p"] == 0.0522 and first["below_threshold"] is False
    later = looks.read(dataclasses.replace(ctx, now=datetime(2026, 12, 22, tzinfo=UTC)), "t1")
    assert later["look"]["number"] == 2 and later["look_threshold"] == 0.0965
    assert later["reply_p"] == 0.0522 and later["below_threshold"] is True


# -- the pre-registration read (W3-4, 21 Oct 2026; settings/model.PRE_REGISTRATION) -----------------------------------


def _registered(**kw) -> CopyTest:
    return ab_test(**{"metric_id": "O3", "definition_version": "2026-10-20", "test": "fisher_exact", "alpha": 0.10,
                      "look_thresholds": "one_look", "read_date": date(2026, 11, 20), **kw})


def test_a_pre_registered_test_is_decided_on_its_own_metric():
    """eap-v1: 3 positive and 2 objections of 30; general-v1: none of 30. On O3 (positive), Fisher's p for 3 of 30
    against 0 of 30 is 0.237 (C(30, 3)/C(60, 3) = 4,060/34,220 = 0.1186 at each end); on O4 (any human reply) it would
    be 5 of 30 against 0, 0.0522 (test_two_looks_use_the_o_brien_fleming_thresholds). The read decides on O3, against
    the one look's whole alpha 0.10: not crossed. The same world under the old defaults (O4) crosses: a mutation that
    ignores metric_id fails here."""
    def run(test: CopyTest) -> dict:
        ctx = world(test)
        for i in range(60):
            arm = "eap-v1" if i < 30 else "general-v1"
            emailed(ctx, i, arm, T0, reply="positive" if i < 3 else "objection" if i < 5 else None)
        return looks.read(ctx, "t1")

    o3 = run(_registered())
    assert (o3["metric_id"], o3["measure"], o3["reply_p"], o3["look_threshold"]) == ("O3", "positive", 0.2373, 0.1)
    assert o3["crossed"] is False and o3["pre_registered"] is True
    old = run(ab_test(read_date=date(2026, 11, 20)))  # started 28 Sep, no pre-registration: O4, Fisher, 0.10, O'Brien-Fleming over one look
    assert (old["metric_id"], old["reply_p"], old["look_threshold"], old["crossed"]) == ("O4", 0.0522, 0.1, True)
    assert old["pre_registered"] is False


def test_a_beta_posterior_test_crosses_on_p_b_better_beyond_half_the_threshold_each_side():
    """5 of 30 replied with eap-v1 (a), 0 of 30 with general-v1 (b): P(b > a) from Jeffreys posteriors Beta(0.5, 30.5)
    against Beta(5.5, 25.5) is 0.0053, below 0.10 / 2 = 0.05, so a beta_posterior test on O4 at alpha 0.10 crosses."""
    ctx = world(_registered(metric_id="O4", test="beta_posterior"))
    for i in range(60):
        emailed(ctx, i, "eap-v1" if i < 30 else "general-v1", T0, reply="objection" if i < 5 else None)
    result = looks.read(ctx, "t1")
    assert result["reply_test"] == "beta_posterior" and result["p_b_better"] == 0.005 and result["crossed"] is True


def test_the_first_look_at_145_calls_nothing_at_6_against_2():
    """warm-intro-70's first look: 145 a side. 6 of 145 against 2 of 145: Fisher's two-sided p = 0.282 (computed:
    stats.fisher_exact), above the whole alpha 0.10 and far above the O'Brien-Fleming half-way threshold 0.0111."""
    ctx = world(ab_test(looks=(145,), read_date=date(2027, 4, 26)))
    for i in range(290):
        arm = "eap-v1" if i < 145 else "general-v1"
        emailed(ctx, i, arm, T0, reply="objection" if i < 6 or 145 <= i < 147 else None)
    result = looks.read(ctx, "t1")
    assert result["look"]["number"] == 1 and result["look_threshold"] == 0.0111
    assert result["reply_p"] == 0.2822 and result["crossed"] is False


def _false_positives(first: float, last: float, test, seeds: int = 2000) -> float:
    """Two looks (145 and 290 a side) at a true 3% in both arms: the share of 2,000 seeded worlds that call a
    difference at either look."""
    import random

    hits = 0
    for seed in range(seeds):
        r = random.Random(seed)
        a = [r.random() < 0.03 for _ in range(290)]
        b = [r.random() < 0.03 for _ in range(290)]
        if test(sum(a[:145]), 145, sum(b[:145]), 145) < first or test(sum(a), 290, sum(b), 290) < last:
            hits += 1
    return hits / seeds


def test_under_no_difference_two_corrected_looks_call_at_most_10_5_percent():
    """RT §0.2: the pooled z-test at p < 0.10 at both looks called a difference that is not there in 15.5% of these
    worlds (17.5% in the review's run); Fisher's test against the O'Brien-Fleming thresholds (0.0111, 0.0965) in 6.0%
    (both counted here over the same 2,000 seeds), inside the 10.5% bound. Swapping the z-test back, uncorrected,
    breaks the bound."""
    import math
    from statistics import NormalDist

    from us_outbound.learn import stats

    def z_test(k1, n1, k2, n2):  # the pooled two-proportion z-test W2-3 removed, as a mutation probe
        p = (k1 + k2) / (n1 + n2)
        if p in (0, 1):
            return 1.0
        return 2 * (1 - NormalDist().cdf(abs(k1 / n1 - k2 / n2) / math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))))

    first, last = stats.obrien_fleming(2, 0.10)
    corrected = _false_positives(first, last, stats.fisher_exact)
    assert corrected <= 0.105 and corrected == 0.06
    assert _false_positives(0.10, 0.10, z_test) == 0.1545 > 0.105
