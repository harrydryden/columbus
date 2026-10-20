"""Which signals go with replies (learn/signal_review.py): the companies emailed, split by the signals they showed
when enrolled, their 28-day human replies with Jeffreys intervals, and no verdict (W2-3: the z-test verdicts went)."""

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
    """companies: (account_id, sent_days_ago, signals or None for no snapshot, explore arm, reply_class or None)."""
    ctx = make_context(SETTINGS, now=NOW)
    events, contacts = [], []
    for aid, days, signals, arm, reply in companies:
        cid = f"k-{aid}"
        sent = NOW - timedelta(days=days)
        events.append({"event_id": f"s-{aid}", "type": "sent", "step": 1, "account_id": aid, "contact_id": cid,
                       "mailbox": "hannah@meetspill.org", "occurred_at": sent})
        if reply:
            events.append({"event_id": f"r-{aid}", "type": "replied", "account_id": aid, "contact_id": cid,
                           "reply_class": reply, "occurred_at": sent + timedelta(days=2)})
        row = {"contact_id": cid, "account_id": aid, "arm": arm}
        if signals is not None:
            row["signals_at_enrol"] = [{"signal": x, "weight": 10} for x in signals]
        contacts.append(row)
    ctx.store.insert("events", events)
    ctx.store.insert("contacts", contacts)
    ctx.store.insert("accounts", [{"account_id": c[0], "tier": "Queue"} for c in companies])
    return ctx


def test_each_signal_is_counts_with_intervals_and_too_few_under_15_a_side():
    """40 companies with "New People leader" (20 replied) against 41 without (4 replied). Jeffreys 95% (stats.jeffreys,
    pinned in tests/test_stats.py): 20 of 40 → 35.0–65.0%, 4 of 41 → 3.4–21.5%. "Visited the US site" has 1 with it
    and "Team of 10–49" has 0 without: under 15 a side, too few to show. The explore arm: the 40 with the signal were
    drawn at random (20 replied), the 41 ranked (4)."""
    rows = []
    for i in range(40):  # with "New People leader": half replied
        rows.append((f"a{i}", 20, ["New People leader", "Team of 10–49"], "random", "positive" if i % 2 else None))
    for i in range(40):  # without it: one in ten replied
        rows.append((f"b{i}", 20, ["Team of 10–49"], "ranked", "objection" if i % 10 == 0 else None))
    rows.append(("c0", 20, ["Visited the US site", "Team of 10–49"], "ranked", None))
    r = signal_review.review(world(rows))
    v = {x.name: x for x in r.splits}
    leader = v["New People leader"]
    assert (leader.with_n, leader.with_replied, leader.without_n, leader.without_replied) == (40, 20, 41, 4)
    assert leader.readable and not v["Visited the US site"].readable and not v["Team of 10–49"].readable
    assert (r.arms.with_n, r.arms.with_replied, r.arms.without_n, r.arms.without_replied) == (40, 20, 41, 4)
    text = signal_review.lines(r)
    assert text[0].startswith("Signal review: 81 companies emailed, the first on Tue 20 Oct; 0 of their 28-day")
    assert ("  New People leader (weight 30): 20 of 40 (50.0%; 95% 35.0–65.0%) vs 4 of 41 (9.8%; 95% 3.4–21.5%)"
            in text)
    assert text[1] == ("Explore arm (read once, at 300 matured in the random arm): random 20 of 40 (50.0%; 95% "
                       "35.0–65.0%) vs ranked 4 of 41 (9.8%; 95% 3.4–21.5%).")
    assert any(x.startswith("Too few to show (under 15 emailed companies with or without it): ") for x in text)
    assert not any(x.lstrip().startswith(("RAISE", "LOWER", "KEEP")) or "p = " in x for x in text)
    assert text[-1].startswith("Monitoring, not a verdict")
    assert not hasattr(signal_review, "p_value") and not hasattr(signal_review, "P_VALUE")


def test_out_of_office_and_late_replies_do_not_count_and_bounced_sends_are_left_out():
    ctx = world([("a", 40, [], "ranked", "out_of_office"), ("b", 40, [], "ranked", None),
                 ("c", 40, [], "ranked", None)])
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
    assert c.signals == frozenset({"Named by Harry"}) and c.arm == "ranked"  # no arm recorded reads as ranked


def subject_world(personal, copy_, before=0):
    """personal and copy_: (emailed, replied) by email 1's subject arm; before: contacts enrolled before the split."""
    rows, arms = [], {}
    for arm, (n, k) in (("personal", personal), ("copy", copy_)):
        for i in range(n):
            rows.append((f"{arm}{i}", 20, [], "ranked", "positive" if i < k else None))
            arms[f"k-{arm}{i}"] = arm
    rows += [(f"old{i}", 20, [], "ranked", "positive") for i in range(before)]
    ctx = world(rows)
    for cid, arm in arms.items():
        ctx.store.update("contacts", {"contact_id": cid}, {"subject_arm": arm})
    return ctx


def test_the_review_reads_email_1_s_subject_split_the_same_way():
    """Harry, 5 Oct 2026: the personal subject (General email1_subject) against the Copy row's s1_subject, by
    contacts.subject_arm; a contact enrolled before the split has none and is left out. 20 of 40 → 35.0–65.0%, 4 of
    40 → 3.5–22.0% (stats.jeffreys)."""
    r = signal_review.review(subject_world((40, 20), (40, 4), before=5))
    v = r.subject
    assert (v.with_n, v.with_replied, v.without_n, v.without_replied) == (40, 20, 40, 4)
    assert ("Email 1 subject (General email1_subject_share): personal 20 of 40 (50.0%; 95% 35.0–65.0%) vs the Copy "
            "row's 4 of 40 (10.0%; 95% 3.5–22.0%).") in signal_review.lines(r)
    few = signal_review.review(subject_world((14, 10), (40, 4))).subject  # MIN_CELL (15) in each arm
    assert not few.readable
    assert signal_review.pair_line("Email 1 subject", "personal", "the Copy row's", few) == (
        "Email 1 subject: too few to show (personal 14, the Copy row's 40 emailed; 15 a side needed).")


def test_nothing_emailed_says_so_and_the_command_is_read_only(capsys):
    from tests.test_cli import Harness

    assert signal_review.lines(signal_review.review(world([]))) == [
        "No company has been emailed yet, so there is nothing to review."]
    h = Harness(dataclasses.replace(SETTINGS))
    assert h.run("report", "signals") == 0  # `signals review` until W2-4
    assert "nothing to review" in capsys.readouterr().out
    assert h.store.tables.get("heartbeats", []) == []
