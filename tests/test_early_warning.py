"""The early warning (learn/early_warning.py; W3-4, 21 Oct 2026; design-final D13, metrics-audit M9).

Each expected number is derived in its test: 0.97^n by hand, the sequential warning from stats.sequential_warning's own
hand-computed fixtures (tests/test_stats.py: 0.843 at 0 of 400, 0.955 at 0 of 800).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_registry import SETTINGS, slack_routes
from us_outbound.base import holds
from us_outbound.learn import early_warning, kill_rules, metrics
from us_outbound.ops import health

NOW = datetime(2026, 12, 7, 8, 50, tzinfo=UTC)
TRANSPORTS: dict[int, FakeTransport] = {}  # each world's transport, to read its Slack posts


def world(n: int, *, days: int = 50, human: int = 0, positive: int = 0, meetings: int = 0, pause: bool = False):
    """n companies sent step 1 `days` ago; the first `human` replied with an objection, the next `positive` positively,
    and `meetings` of them (from the end) booked a meeting 10 days after step 1."""
    s = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, early_warning_pause=pause))
    t = slack_routes(FakeTransport())
    ctx = make_context(s, now=NOW, live=True, transport=t)
    TRANSPORTS[id(ctx)] = t
    at = NOW - timedelta(days=days)
    events = []
    for i in range(n):
        events.append({"event_id": f"s{i}", "type": "sent", "step": 1, "account_id": f"a{i}", "contact_id": f"k{i}",
                       "occurred_at": at})
        cls = "objection" if i < human else "positive" if i < human + positive else None
        if cls:
            events.append({"event_id": f"r{i}", "type": "replied", "account_id": f"a{i}", "contact_id": f"k{i}",
                           "reply_class": cls, "occurred_at": at + timedelta(days=3)})
        if i >= n - meetings:
            events.append({"event_id": f"m{i}", "type": "meeting_booked", "account_id": f"a{i}",
                           "occurred_at": at + timedelta(days=10)})
    ctx.store.insert("events", events)
    return ctx


def posts(ctx) -> list[str]:
    return [r.json["text"] for r in TRANSPORTS[id(ctx)].requests if r.url.endswith("chat.postMessage")]


def names(ctx) -> list[str]:
    return [w["name"] for w in early_warning.check(ctx)["warnings"]]


def test_no_human_reply_in_100_matured_fires_with_its_p():
    """0.97^100 = e^(100 ln 0.97) = e^(−3.0459) = 0.0476: "P = 0.048"."""
    ctx = world(100)
    out = early_warning.check(ctx)
    assert [w["name"] for w in out["warnings"]][0] == "no_human" and out["warnings"][0]["p"] == 0.048
    posted = "\n".join(posts(ctx))
    assert ("No human reply from 100 companies that have had 28 days: P = 0.048 at a 3% rate: check capture and "
            "deliverability before copy.") in posted
    assert out["held"] is None and holds.enrolment_stop(ctx.store) is None  # early_warning_pause = no: no hold
    assert "nothing is paused (General early_warning_pause = no" in posted


def test_99_matured_or_one_reply_does_not_fire():
    assert "no_human" not in names(world(99))
    assert "no_human" not in names(world(100, human=1))
    assert "no_human" not in names(world(100, days=20))  # none has had 28 days


def test_the_meeting_warning_reads_the_45_day_companies():
    """P(rate < 0.25%) under Beta(½ + k, ½ + n − k): 0 of 400 is 0.843, under 0.9; 0 of 800 is 0.955: fires; 2 of 800
    is 0.451 (stats.sequential_warning): not. Each world has human replies so only the meeting rule can fire, and
    positives above the 0.5% line: 10 of 400 and 20 of 800 positive (2.5%) leave P(positive rate < 0.5%) near 0."""
    assert "meetings" not in names(world(400, human=10, positive=10))
    assert "meetings" in names(world(800, human=10, positive=20))
    assert "meetings" not in names(world(800, human=10, positive=20, meetings=2))
    assert "meetings" not in names(world(800, days=40, human=10, positive=20))  # not yet 45 days: no n


def test_the_positive_warning_against_one_percent():
    """P(positive rate < 0.5%) > 0.9: 0 positive of 300 matured at 28 days (5 objections, so the no-human rule stays
    quiet) gives Beta(0.5, 300.5) at 0.005: 0.917, the same as stats.sequential_warning(0, 600, 0.0025)'s hand-computed
    0.917, since at small r0 Beta(½, n) at r0 depends on n × r0 alone (3 both times): it fires. 6 positive of 600 (1%)
    gives Beta(6.5, 594.5) at 0.005: 0.053: it does not."""
    assert "positive" in names(world(300, human=5))
    assert "positive" not in names(world(600, human=5, positive=6))


def test_each_warning_posts_once_a_week():
    ctx = world(100)
    early_warning.check(ctx)
    early_warning.check(ctx)
    assert len(posts(ctx)) == 1


def test_early_warning_pause_yes_holds_new_enrolment_once():
    ctx = world(100, pause=True)
    out = early_warning.check(ctx)
    assert out["held"] and "No human reply from 100 companies" in holds.enrolment_stop(ctx.store)
    item = ctx.store.get("hitl_items", item_id=out["held"])
    assert (item["kind"], item["status"], item["payload"]["action"], item["payload"]["outbox"]) == (
        "kill_rule", "open", "pause_enrolment", False)  # not re-posted as a kill rule's alert
    assert early_warning.check(ctx)["held"] is None  # held already: no second item
    assert len(ctx.store.select("hitl_items", {"kind": "kill_rule"})) == 1


def test_a_cleared_hold_does_not_come_back_the_next_day():
    """W3-4 r1: with early_warning_pause = yes the warning holds; Harry clears it (`killrules clear`); the next
    metrics_daily (a day later, the same ISO week, so its post went out already) still sees 0 of 100. It holds no
    more (like the stop rule: one hold per warning, ever), so enrolment is not paused again silently; the post says
    so the next week. The hold was recorded before its alert."""
    ctx = world(100, pause=True)
    first = early_warning.check(ctx)
    item = ctx.store.get("hitl_items", item_id=first["held"])
    assert item is not None and item["created_at"] == NOW and len(posts(ctx)) == 1
    assert "New enrolment is paused until `us-outbound killrules clear" in posts(ctx)[0]
    kill_rules.clear(ctx, first["held"], "Harry")
    assert holds.enrolment_stop(ctx.store) is None
    tomorrow = dataclasses.replace(ctx, now=NOW + timedelta(days=1))
    TRANSPORTS[id(tomorrow)] = TRANSPORTS[id(ctx)]
    again = early_warning.check(tomorrow)
    assert [w["name"] for w in again["warnings"]] == ["no_human"] and again["held"] is None
    assert holds.enrolment_stop(tomorrow.store) is None and len(posts(ctx)) == 1  # nothing new this week
    assert len(ctx.store.select("hitl_items", {"kind": "kill_rule"})) == 1
    next_week = dataclasses.replace(ctx, now=NOW + timedelta(days=7))
    TRANSPORTS[id(next_week)] = TRANSPORTS[id(ctx)]
    assert early_warning.check(next_week)["held"] is None and holds.enrolment_stop(ctx.store) is None
    assert "its hold was cleared with `us-outbound killrules clear`" in posts(ctx)[-1]


def test_the_hold_is_written_before_the_alert(monkeypatch):
    """Holds are recorded before alerts: a post that fails leaves the hold in place."""
    ctx = world(100, pause=True)

    def down(*a, **k):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(early_warning.notify, "post_once", down)
    with pytest.raises(RuntimeError):
        early_warning.check(ctx)
    assert "No human reply from 100 companies" in holds.enrolment_stop(ctx.store)


def test_it_runs_inside_metrics_daily():
    ctx = world(100)
    ctx.job = metrics.JOB
    summary = health.run_job(ctx, metrics.run)
    assert summary["early_warning"]["warnings"][0]["name"] == "no_human"
