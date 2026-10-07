"""Tests read only at pre-registered looks (learn/looks.py; SPEC 12; Harry, 6 Oct 2026): the Tests tab's kind and
looks, the no-peek rule, what each look covers, holdout tests, and `test start` for them."""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_cli import Harness, _approved, _tests_tab
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
    assert plan.new_columns == ["kind", "looks", "email", "change", "text_a", "text_b", "find"]
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
    assert at_look == ["t1"] and lines[0].startswith("  t1 (ab) reached look 1: 1 accounts per arm")
    assert "eap-v1 1 of 1 replied (100.0%); general-v1 0 of 1 replied (0.0%)" in lines[0]
    lines, at_look = looks.readout_lines(ctx, ctx.now)  # nothing new since
    assert at_look == [] and "no look reached this week" in lines[0] and "next look 2 (the read date)" in lines[0]
    assert "replied" not in lines[0] and "%" not in lines[0]


# -- test start ------------------------------------------------------------------------------------------------------------


def test_a_holdout_starts_without_copy_and_beside_the_running_copy_test(capsys):
    running = _tests_tab(test_id="t1", status="running", start_date="2026-10-20")
    holdout = _tests_tab(test_id="t2", kind="holdout", version_a="opener", version_b="holdout", looks="100")
    h = Harness(dataclasses.replace(SETTINGS, copy=_approved()), sheet_tabs={"Tests": [running, holdout]})
    assert h.run("test", "start", "t2", "--live") == 0
    assert h.sheet_tabs["Tests"][1]["status"] == "running"
    third = _tests_tab(test_id="t3")
    h2 = Harness(dataclasses.replace(SETTINGS, copy=_approved()), sheet_tabs={"Tests": [running, third]})
    assert h2.run("test", "start", "t3", "--live") == 2
    assert "only one copy test (ab or variant) runs at a time" in capsys.readouterr().err
    h3 = Harness(dataclasses.replace(SETTINGS, copy=_approved()), sheet_tabs={"Tests": [_tests_tab(looks="soon")]})
    assert h3.run("test", "start", "t1", "--live") == 2
    assert "looks on the Tests tab" in capsys.readouterr().err
