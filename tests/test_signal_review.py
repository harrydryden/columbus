"""Which signals predict replies (learn/signal_review.py): the companies emailed, split by the signals they showed
when enrolled, their 28-day human-reply rates compared, and a verdict per signal that never changes a weight."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from tests.fakes import make_context
from us_outbound.learn import signal_review
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)  # a Monday
SETTINGS = validate_all(default_tabs())[0]


def world(companies):
    """companies: (account_id, sent_days_ago, signals or None for no snapshot, tier, reply_class or None)."""
    ctx = make_context(SETTINGS, now=NOW)
    events, contacts = [], []
    for aid, days, signals, tier, reply in companies:
        cid = f"k-{aid}"
        sent = NOW - timedelta(days=days)
        events.append({"event_id": f"s-{aid}", "type": "sent", "step": 1, "account_id": aid, "contact_id": cid,
                       "mailbox": "hannah@meetspill.org", "occurred_at": sent})
        if reply:
            events.append({"event_id": f"r-{aid}", "type": "replied", "account_id": aid, "contact_id": cid,
                           "reply_class": reply, "occurred_at": sent + timedelta(days=2)})
        row = {"contact_id": cid, "account_id": aid, "tier_at_enrol": tier}
        if signals is not None:
            row["signals_at_enrol"] = [{"signal": x, "weight": 10} for x in signals]
        contacts.append(row)
    ctx.store.insert("events", events)
    ctx.store.insert("contacts", contacts)
    ctx.store.insert("accounts", [{"account_id": c[0], "tier": "Control"} for c in companies])
    return ctx


def test_a_signal_whose_companies_reply_more_is_marked_raise_and_one_with_too_few_is_left_alone():
    rows = []
    for i in range(40):  # with "New People leader": half replied
        rows.append((f"a{i}", 20, ["New People leader", "Team of 10–49"], "Priority", "positive" if i % 2 else None))
    for i in range(40):  # without it: one in ten replied
        rows.append((f"b{i}", 20, ["Team of 10–49"], "Control", "objection" if i % 10 == 0 else None))
    rows.append(("c0", 20, ["Named by Harry", "Team of 10–49"], "Standard", None))
    r = signal_review.review(world(rows))
    v = {x.signal: x for x in r.verdicts}
    leader = v["New People leader"]
    assert (leader.with_n, leader.with_replied, leader.without_n, leader.without_replied) == (40, 20, 41, 4)
    assert leader.verdict == "raise" and leader.p < 0.001
    assert v["Named by Harry"].verdict == "too few"
    assert v["Team of 10–49"].verdict == "too few"  # every company has it: nobody to compare with
    assert r.tiers == {"Priority": (40, 20), "Standard": (1, 0), "Control": (40, 4)}
    text = signal_review.lines(r)
    assert text[0].startswith("Signal review: 81 companies emailed, the first on Tue 20 Oct; 0 of their 28-day")
    assert "  RAISE  New People leader (weight 30): 40 companies, 50.0% vs 9.8% without · p = 0.00" in text
    assert "  Priority: 40 emailed, 50.0% replied" in text
    assert any(x.startswith("Too few to judge (under 30 emailed companies with or without it): ") for x in text)
    assert text[-1].startswith("Nothing changes by itself")


def test_out_of_office_and_late_replies_do_not_count_and_bounced_sends_are_left_out():
    ctx = world([("a", 40, [], "Control", "out_of_office"), ("b", 40, [], "Control", None),
                 ("c", 40, [], "Control", None)])
    ctx.store.insert("events", [
        {"event_id": "late", "type": "replied", "account_id": "b", "contact_id": "k-b", "reply_class": "positive",
         "occurred_at": NOW - timedelta(days=40) + timedelta(days=29)},  # after the 28-day window
        {"event_id": "bounce", "type": "bounced", "account_id": "c", "contact_id": "k-c", "step": 1,
         "occurred_at": NOW - timedelta(days=39)},
    ])
    companies, closed = signal_review.emailed(ctx)
    assert [(c.account_id, c.replied) for c in companies] == [("a", False), ("b", False)]
    assert closed == 2


def test_a_contact_enrolled_before_the_snapshot_uses_the_current_matches():
    ctx = world([("old", 10, None, "", "positive")])
    ctx.store.insert("signal_events", [{"event_id": "m", "account_id": "old", "source": "scoring",
                                        "fact": "signal_matched", "value": {"signal": "Named by Harry", "weight": 30},
                                        "observed_at": NOW}])
    [c] = signal_review.emailed(ctx)[0]
    assert c.signals == frozenset({"Named by Harry"}) and c.tier == "Control"  # tier from the account row


def test_the_monday_post_says_when_the_review_is_worth_reading():
    few = world([(f"a{i}", 20, [], "Control", None) for i in range(59)])
    assert signal_review.ready(few) == ""
    enough = world([(f"a{i}", 20, [], "Control", None) for i in range(60)])
    assert signal_review.ready(enough).startswith("  Signal review: 60 companies emailed (0 reply windows closed).")
    recent = world([(f"a{i}", 5, [], "Control", None) for i in range(80)])
    assert signal_review.ready(recent) == ""  # under two weeks since the first send


def test_nothing_emailed_says_so_and_the_command_is_read_only(capsys):
    from tests.test_cli import Harness

    assert signal_review.lines(signal_review.review(world([]))) == [
        "No company has been emailed yet, so there is nothing to review."]
    h = Harness(dataclasses.replace(SETTINGS))
    assert h.run("signals", "review") == 0
    assert "nothing to review" in capsys.readouterr().out
    assert h.store.tables.get("heartbeats", []) == []
