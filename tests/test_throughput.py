"""Throughput (learn/throughput.py; loop L6; W3-3, 20 Oct 2026): the binding term per send day, lost leads by cause and
the mailboxes' use, from enrol's own run summaries, and L6's one line. The numbers are hand-computed in each test."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from tests.fakes import make_context
from us_outbound.learn import throughput as tp
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import Mailbox
from us_outbound.settings.validate import validate_all

NOW = datetime(2026, 10, 26, 7, 30, tzinfo=UTC)  # Mon 26 Oct, 07:30 UK: the readout reads the week before
BASE, _ = validate_all(default_tabs())
BOXES = (Mailbox("maria@meetspill.org", "meetspill.org", "Maria", "Active", 30),
         Mailbox("tom@spillhq.org", "spillhq.org", "Tom", "Active", 30))
SETTINGS = dataclasses.replace(BASE, mailboxes=BOXES)


def enrol_run(ctx, day: int, detail: dict, status="ok"):
    """An enrol run on Mon 19 + day Oct, 12:00 UK."""
    at = datetime(2026, 10, 19 + day, 11, 0, tzinfo=UTC)
    ctx.store.upsert("heartbeats", [{"run_id": f"enrol-{day}", "job": "enrol", "status": status, "dry_run": False,
                                     "started_at": at, "finished_at": at,
                                     "detail": {"date": f"2026-10-{19 + day}", "dry_run": False, **detail}}])


def ran(target, capacity, ready, enrolled):
    binding = min(("weekly_target", "sending_capacity", "ready_accounts"),
                  key=lambda k: {"weekly_target": target, "sending_capacity": capacity, "ready_accounts": ready}[k])
    number = min(target, capacity, ready)
    return {"number": number, "enrolled": enrolled, "prepared": enrolled,
            "number_terms": {"weekly_target": target, "sending_capacity": capacity, "ready_accounts": ready,
                             "binding": binding}}


def sends(ctx, mailbox, n, days_ago):
    ctx.store.insert("events", [{"event_id": f"s-{mailbox}-{days_ago}-{i}", "type": "sent", "mailbox": mailbox,
                                 "contact_id": f"c-{mailbox}-{i}", "occurred_at": NOW - timedelta(days=days_ago)}
                                for i in range(n)])


def week(first_send_days_ago: int):
    """W3-1's five summaries: capacity ×3 (free 14, 21, 25, enrolled equal), a gate (the hand-check held Thursday),
    supply ×1 (capacity 25, 10 ready). The mailboxes first sent first_send_days_ago, and 22 and 23 times last week."""
    ctx = make_context(SETTINGS, job="monday_readout", now=NOW)
    for day, (cap, n) in enumerate(((14, 14), (21, 21), (25, 25))):
        enrol_run(ctx, day, ran(30, cap, 40, n))
    enrol_run(ctx, 3, {"reason": "the weekly hand-check is not approved yet"}, status="skipped")
    enrol_run(ctx, 4, ran(30, 25, 10, 10))
    enrol_run(ctx, 5, {"reason": "2026-10-24 is not a send day"}, status="skipped")  # Saturday: no day lost
    sends(ctx, "maria@meetspill.org", 1, first_send_days_ago)
    sends(ctx, "tom@spillhq.org", 1, first_send_days_ago)
    sends(ctx, "maria@meetspill.org", 22, 3)
    sends(ctx, "tom@spillhq.org", 23, 4)
    return ctx


def test_lost_leads_by_cause_from_five_summaries_and_two_leads_never_sent():
    """Room = min(weekly target, sending capacity): capacity days 14, 21, 25 with as many enrolled lose 0; the gate day
    loses the day's share, ceil(150 ÷ 5) = 30; the supply day's room is 25 and 10 were ready, so 15. Two leads enrolled
    four days ago with no step 1 sent are never_sent: {capacity 0, supply 15, gate 30, never_sent 2}."""
    ctx = week(first_send_days_ago=30)
    ctx.store.insert("contacts", [{"contact_id": f"n{i}", "account_id": f"a{i}", "enrolled_at": NOW - timedelta(days=4)}
                                  for i in range(2)])
    r = tp.report(ctx)
    assert tp.day_share(ctx) == 30
    assert [d.binding for d in r.days] == ["capacity", "capacity", "capacity", "gate", "supply"]
    assert r.lost == {"capacity": 0, "supply": 15, "gate": 30, "blackout": 0, "errors": 0, "never_sent": 2}


def test_past_ramp_week_3_capacity_binding_3_of_5_days_at_15_percent_is_the_formula_or_a_gate():
    """First sends 30 days ago: week 5, so both are evaluated. Use = 22 ÷ (30 × 5) = 0.147 and 23 ÷ 150 = 0.153, a mean
    of 0.150, under 0.60, with capacity binding 3 of the 5 days."""
    r = tp.report(week(first_send_days_ago=30))
    assert {m: round(u, 3) for m, u in r.utilisation.items()} == {"maria@meetspill.org": 0.147, "tom@spillhq.org": 0.153}
    assert r.rule.startswith("capacity binding 3 of 5 days at 15% utilisation: the formula or a gate")
    assert "a gate or an error held enrolment on 1 of 5 days" in r.rule


def test_mailboxes_in_ramp_week_1_are_not_evaluated():
    r = tp.report(week(first_send_days_ago=5))  # first sends 5 days ago: both in their first sending week
    assert r.utilisation == {} and set(r.ramping) == {"maria@meetspill.org", "tom@spillhq.org"}
    assert r.rule.startswith("ramp: not evaluated")
    text = "\n".join(tp.lines(r))
    assert "Leads lost by cause: supply 15, gate 30." in text and "none past it yet" in text


def test_a_run_that_prepared_more_than_it_enrolled_loses_the_rest_to_errors_and_supply_three_days_says_so():
    ctx = make_context(SETTINGS, job="monday_readout", now=NOW)
    for day in range(3):
        enrol_run(ctx, day, ran(30, 30, 8, 8))  # supply: room 30, 8 ready → 22 a day
    enrol_run(ctx, 3, ran(30, 30, 40, 26))  # target bound, 30 prepared, 26 enrolled: 4 errors
    sends(ctx, "maria@meetspill.org", 1, 40)
    sends(ctx, "tom@spillhq.org", 1, 40)
    r = tp.report(ctx)
    assert r.lost["supply"] == 66 and r.lost["errors"] == 4
    assert r.rule == ("supply binding 3 of 4 days: sourcing and contacts (loops L1, L2, L5); a gate or an error held "
                      "enrolment on 1 of 4 days: look at it the same day")
