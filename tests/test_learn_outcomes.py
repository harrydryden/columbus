"""learn/outcomes.py: the one outcomes definition, on matured worlds, and its parity with v_account_outcomes (W2-3).

Fixtures follow production's shape (RT §5): several messages from one company, a bounced company kept in the
denominator, an out-of-office reply, a meeting with no reply, worlds matured past 45 days. Every count below is worked
out by hand from the world in its docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_db import db, dsn, store  # noqa: F401 (the Postgres fixtures, for the parity test)
from us_outbound.learn import outcomes

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)  # every company's step 1


def _sent(aid: str, *, day: float = 0, step: int = 1, contact: str | None = None) -> dict:
    cid = contact or f"c-{aid}"
    return {"event_id": f"s{step}-{cid}", "type": "sent", "account_id": f"a-{aid}", "contact_id": cid, "step": step,
            "mailbox": "hannah@meetspill.org", "occurred_at": T0 + timedelta(days=day)}


def _reply(aid: str, eid: str, cls: str, day: float, *, account: bool = True) -> dict:
    return {"event_id": eid, "type": "replied", "account_id": f"a-{aid}" if account else None, "contact_id": f"c-{aid}",
            "reply_class": cls, "mailbox": "hannah@meetspill.org", "occurred_at": T0 + timedelta(days=day)}


def world(now_day: int) -> list[dict]:
    """Ten companies, each sent step 1 at T0; only the events that have happened by day `now_day`.

    A bounced at step 1; B one out-of-office reply (day 2); C three positive messages (days 1, 3, 5); D one negative
    reply (day 4); E a meeting booked at day 40 and no reply; F–J nothing."""
    rows = [_sent(x) for x in "ABCDEFGHIJ"]
    rows += [{"event_id": "b-A", "type": "bounced", "account_id": "a-A", "contact_id": "c-A", "step": 1,
              "occurred_at": T0 + timedelta(minutes=5)},
             _reply("B", "r-B", "out_of_office", 2),
             _reply("C", "r-C1", "positive", 1), _reply("C", "r-C2", "positive", 3), _reply("C", "r-C3", "positive", 5),
             _reply("D", "r-D", "negative", 4),
             {"event_id": "m-E", "type": "meeting_booked", "account_id": "a-E", "occurred_at": T0 + timedelta(days=40),
              "source": "calendar"}]
    return [r for r in rows if r["occurred_at"] <= T0 + timedelta(days=now_day)]


def companies(default_settings, rows: list[dict], now_day: int, contacts: list[dict] | None = None):
    ctx = make_context(default_settings, now=T0 + timedelta(days=now_day))
    ctx.store.insert("events", rows)
    if contacts:
        ctx.store.insert("contacts", contacts)
    return ctx, outcomes.per_company(ctx)


def test_a_world_matured_by_50_days(default_settings):
    # delivered 9 (all but A); human 2 (C, D: B's out-of-office is not human); positive 1 (C, once though it wrote
    # three times); negative 1 (D); meeting_in_45 1 (E, day 40 < 45); engaged 2 (C by its reply, E by its meeting).
    _, found = companies(default_settings, world(50), 50)
    assert [o.account_id for o in found] == [f"a-{x}" for x in "ABCDEFGHIJ"]
    t = outcomes.tally(found)
    assert (t.contacted, t.delivered, t.human, t.positive, t.negative) == (10, 9, 2, 1, 1)
    assert (t.meeting_in_45, t.meeting_any, t.engaged) == (1, 1, 2)
    # All matured at 50 days (≥ 28 and ≥ 45): the matured counts are the same; ITT keeps A in the 45-day denominator.
    assert (t.matured_28, t.human_28, t.positive_28, t.matured_45, t.engaged_45) == (9, 2, 1, 10, 2)
    by = {o.account_id: o for o in found}
    assert by["a-A"].delivered is False and by["a-A"].bounced_any is True
    assert by["a-C"].first_positive_at == T0 + timedelta(days=1) and by["a-C"].first_reply_at == T0 + timedelta(days=1)
    assert by["a-E"].engaged_matured is True and by["a-E"].meeting_source == "calendar"
    assert by["a-F"].engaged_matured is False
    assert by["a-B"].replied_in_window is False


def test_a_world_matured_by_30_days_has_no_north_star_yet(default_settings):
    # At day 30 the reply windows (28 days) have closed but the 45-day ones have not: E's meeting (day 40) has not
    # happened, and engaged_matured is None for every company.
    _, found = companies(default_settings, world(30), 30)
    by = {o.account_id: o for o in found}
    assert by["a-E"].window_closed_28 and not by["a-E"].window_closed_45 and by["a-E"].engaged_matured is None
    assert all(o.engaged_matured is None for o in found)
    t = outcomes.tally(found)
    assert (t.matured_28, t.human_28, t.matured_45, t.engaged) == (9, 2, 0, 1)  # C engaged by its reply


def test_a_corrected_class_wins_and_the_latest_correction_wins(default_settings):
    # D's negative is corrected to positive (day 6): positive 2 (C, D), negative 0. A second correction of C's first
    # reply to other leaves C positive on its later messages.
    rows = world(50) + [
        {"event_id": "fix-1", "type": "reply_class_corrected", "source": "r-D", "reply_class": "objection",
         "occurred_at": T0 + timedelta(days=6)},
        {"event_id": "fix-2", "type": "reply_class_corrected", "source": "r-D", "reply_class": "positive",
         "occurred_at": T0 + timedelta(days=7)},
        {"event_id": "fix-3", "type": "reply_class_corrected", "source": "r-C1", "reply_class": "other",
         "occurred_at": T0 + timedelta(days=8)},
    ]
    _, found = companies(default_settings, rows, 50)
    t = outcomes.tally(found)
    assert (t.positive, t.negative, t.human, t.engaged) == (2, 0, 2, 3)  # engaged: C, D, E
    by = {o.account_id: o for o in found}
    assert by["a-C"].first_positive_at == T0 + timedelta(days=3)  # r-C1 is "other" now
    # A correction to out_of_office takes a reply out of the human count.
    rows.append({"event_id": "fix-4", "type": "reply_class_corrected", "source": "r-D", "reply_class": "out_of_office",
                 "occurred_at": T0 + timedelta(days=9)})
    _, found = companies(default_settings, rows, 50)
    assert outcomes.tally(found).human == 1


def test_exposure_and_stamps(default_settings):
    # C was sent all four steps; its mailbox was paused by a kill rule at day 10 (inside its 28 days).
    rows = world(50) + [_sent("C", day=7 * k, step=k + 1) for k in range(1, 4)]
    ctx = make_context(default_settings, now=T0 + timedelta(days=50))
    ctx.store.insert("events", rows)
    ctx.store.insert("contacts", [{"contact_id": "c-C", "account_id": "a-C", "arm": "random", "sham_arm": "b",
                                   "industry_group_at_enrol": "Legal Teams", "size_band_at_enrol": "50-99",
                                   "score_at_enrol": 40, "config_version": "v1"}])
    ctx.store.insert("config_versions", [{"config_version": "v1", "labels_hash": "abc123"}])
    # Two sourced_by facts: the account was found first by the Legal Teams search (day -20), and again later by
    # another (day -5, stored first). The stamp is the earliest, whatever order the store returns them in.
    ctx.store.insert("signal_events", [
        {"event_id": "f2", "account_id": "a-C", "source": "source_universe", "fact": "sourced_by",
         "value": {"slice": "Fintech|CA|50-99"}, "observed_at": T0 - timedelta(days=5)},
        {"event_id": "f1", "account_id": "a-C", "source": "source_universe", "fact": "sourced_by",
         "value": {"slice": "Legal Teams|NY|50-99"}, "observed_at": T0 - timedelta(days=20)}])
    ctx.store.insert("hitl_items", [{"item_id": "k1", "kind": "kill_rule", "status": "open",
                                     "created_at": T0 + timedelta(days=10),
                                     "payload": {"action": "pause_mailbox", "mailboxes": ["hannah@meetspill.org"],
                                                 "dry_run": False}}])
    by = {o.account_id: o for o in outcomes.per_company(ctx)}
    c = by["a-C"]
    assert c.steps_sent == 4 and c.mailbox_paused is True
    assert (c.stamp("arm"), c.stamp("sham_arm"), c.stamp("industry_group_at_enrol")) == ("random", "b", "Legal Teams")
    assert (c.stamp("gate_version"), c.stamp("sourced_by")) == ("abc123", "Legal Teams|NY|50-99")
    assert by["a-F"].steps_sent == 1 and by["a-F"].stamp("arm", "ranked") == "ranked"


def test_a_company_never_sent_step_1_is_in_no_outcome(default_settings):
    rows = [_sent("X", step=2), {"event_id": "r-X", "type": "replied", "account_id": "a-X", "reply_class": "positive",
                                 "occurred_at": T0 + timedelta(days=1)}]
    _, found = companies(default_settings, rows, 50)
    assert found == []


# -- parity with the view --------------------------------------------------------------------------------------------


def parity_world() -> list[dict]:
    """The 50-day world, with what tells the definitions apart if either drifts: K a meeting at day 50 (any = 1,
    in_45 = 0); L a reply with no account_id (its contact's send names the account); M a reply 29 days after step 1
    (outside the window); N a reply a day before its step 1 (not counted); D's negative corrected to positive; P's
    step 1 sent twice (the earlier one counts: an objection at day 0.5, between the two, is in its window) and bounced
    at step 2 (still delivered); R an objection 25 days after step 1 (inside the 28-day window: replies to step 4, sent
    day 21, arrive from day 21 to 28, so a window read as 21 days loses them)."""
    rows = world(50)
    rows += [_sent("K"), {"event_id": "m-K", "type": "meeting_booked", "account_id": "a-K",
                          "occurred_at": T0 + timedelta(days=50)}]
    rows += [_sent("L"), _reply("L", "r-L", "positive", 2, account=False)]
    rows += [_sent("M"), _reply("M", "r-M", "positive", 29)]
    rows += [_sent("N", day=2), _reply("N", "r-N", "positive", 1)]
    rows += [{"event_id": "fix-D", "type": "reply_class_corrected", "source": "r-D", "reply_class": "positive",
              "occurred_at": T0 + timedelta(days=6)}]
    rows += [_sent("P"), _reply("P", "r-P", "objection", 0.5), {**_sent("P", day=1), "event_id": "s1-again-P"},
             _sent("P", day=7, step=2),
             {"event_id": "b-P", "type": "bounced", "account_id": "a-P", "contact_id": "c-P", "step": 2,
              "occurred_at": T0 + timedelta(days=7, minutes=5)}]
    rows += [_sent("R"), _reply("R", "r-R", "objection", 25)]
    return rows


PARITY_EXPECTED = {
    # account: (delivered, replied_in_window, positive_in_window, meeting_any, meeting_in_45), by hand.
    "a-A": (False, False, False, False, False), "a-B": (True, False, False, False, False),
    "a-C": (True, True, True, False, False), "a-D": (True, True, True, False, False),
    "a-E": (True, False, False, True, True), "a-F": (True, False, False, False, False),
    "a-G": (True, False, False, False, False), "a-H": (True, False, False, False, False),
    "a-I": (True, False, False, False, False), "a-J": (True, False, False, False, False),
    "a-K": (True, False, False, True, False), "a-L": (True, True, True, False, False),
    "a-M": (True, False, False, False, False), "a-N": (True, False, False, False, False),
    "a-P": (True, True, False, False, False), "a-R": (True, True, False, False, False),
}
COLUMNS = ("delivered", "replied_in_window", "positive_in_window", "meeting_any", "meeting_in_45")


def test_python_matches_the_hand_count(default_settings):
    _, found = companies(default_settings, parity_world(), 55)
    assert {o.account_id: tuple(o.as_row()[c] for c in COLUMNS) for o in found} == PARITY_EXPECTED
    by = {o.account_id: o for o in found}
    # N's positive reply came a day before its step 1: no first_positive_at; C's first was day 1.
    assert by["a-N"].first_positive_at is None and by["a-C"].first_positive_at == T0 + timedelta(days=1)


def test_the_view_and_per_company_agree(store, default_settings):  # noqa: F811
    store.insert("events", parity_world())
    view = {r["account_id"]: (r["delivered"], r["replied_in_window"], r["positive_in_window"], r["meeting_booked"],
                              r["meeting_in_45"])
            for r in store.query(f"SELECT * FROM {store.schema}.v_account_outcomes")}
    assert view == PARITY_EXPECTED
    ctx = make_context(default_settings, now=T0 + timedelta(days=55), store=store)
    python = {o.account_id: tuple(o.as_row()[c] for c in COLUMNS) for o in outcomes.per_company(ctx)}
    assert python == view
    rows = {r["account_id"]: r for r in store.query(f"SELECT * FROM {store.schema}.v_account_outcomes")}
    assert rows["a-C"]["first_positive_at"] == T0 + timedelta(days=1) and rows["a-C"]["window_closed_45"] is True
    # N's positive reply came a day before its step 1, so it has no first_positive_at (the >= step1_at filter).
    assert rows["a-N"]["first_positive_at"] is None
    # The view's windows run on the database's clock (now()): Q, sent step 1 30 days before it, has its 28-day window
    # closed and its 45-day one open (a 45-day column read as 28 days would say closed).
    store.insert("events", [{**_sent("Q"), "occurred_at": datetime.now(UTC) - timedelta(days=30)}])
    [q] = [r for r in store.query(f"SELECT * FROM {store.schema}.v_account_outcomes") if r["account_id"] == "a-Q"]
    assert q["window_closed"] is True and q["window_closed_45"] is False


@pytest.mark.parametrize("mutation", ["drop_correction", "meeting_any_as_45", "reply_window_21", "last_step_1"])
def test_a_drift_in_either_definition_fails_parity(default_settings, monkeypatch, mutation):
    """The parity's teeth without a database: a Python twin that ignored corrections, read meeting_in_45 as any
    booking, read the reply window as 21 days (R's day-25 objection lost) or let a later step-1 send replace the first
    (P's day-0.5 objection then comes before its step 1) would no longer match the hand count the view is held to."""
    if mutation == "drop_correction":
        monkeypatch.setattr(outcomes, "CORRECTED", "no_such_type")
    elif mutation == "meeting_any_as_45":
        monkeypatch.setattr(outcomes.Outcome, "meeting_in_45", property(lambda self: self.first_meeting_at is not None))
    elif mutation == "reply_window_21":
        monkeypatch.setattr(outcomes, "REPLY_DAYS", 21)
    else:  # per_company with its first-send-wins test removed, compiled in a copy of the module's namespace
        import inspect

        src = inspect.getsource(outcomes.per_company)
        mutated = src.replace(" and aid not in out:", ":")
        assert mutated != src
        space = dict(vars(outcomes))
        exec(compile(mutated, outcomes.__file__, "exec"), space)  # noqa: S102
        monkeypatch.setattr(outcomes, "per_company", space["per_company"])
    _, found = companies(default_settings, parity_world(), 55)
    assert {o.account_id: tuple(o.as_row()[c] for c in COLUMNS) for o in found} != PARITY_EXPECTED
