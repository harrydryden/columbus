"""metrics_daily (learn/metrics.py; W3-1, 20 Oct 2026; design-final §4): the persisted series. Each expected number is
worked out by hand from the world in its test's docstring or comment, not read off the code's output."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_db import db, dsn, store  # noqa: F401  (the Postgres fixtures, for the end-to-end run)
from tests.test_learn_outcomes import world as ten_companies
from tests.test_precision import judgement
from tests.test_throughput import NOW as THROUGHPUT_NOW
from tests.test_throughput import week as five_summaries
from us_outbound.enrol import queue
from us_outbound.learn import metrics
from us_outbound.ops import health
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

BASE, _ = validate_all(default_tabs())


def one(st, metric: str, segment: str = "all") -> dict:
    [r] = [r for r in metrics.latest(st, metric, segment)]
    return r


def run(ctx) -> dict:
    out = health.run_job(ctx, metrics.run)
    assert out["errors"] == [], out["errors"]
    return out


def sent(aid: str, at: datetime, *, step: int = 1, **kw) -> dict:
    return {"event_id": f"s{step}-{aid}", "type": "sent", "account_id": aid, "contact_id": f"c-{aid}", "step": step,
            "mailbox": "hannah@meetspill.org", "occurred_at": at, **kw}


# -- the outcomes ----------------------------------------------------------------------------------------------------


def test_the_ten_company_world_matured_by_50_days_and_a_second_run_upserts():
    """W2-3's ten companies, all sent step 1 on 1 Sep, read on day 50 (21 Oct): A bounced; B out-of-office; C three
    positive messages; D one negative; E a meeting on day 40; F-J nothing. Every company has had 45 days, so the north
    star counts all ten (intent to treat: A stays): engaged C (reply) and E (meeting within 45 days) = 2 of 10 = 0.2;
    any booking 1 (E), within 45 days 1 (E). Human replies at 28 days: C, D = 2 of 10; positive C = 1 of 10; negative
    D = 1 of 10; delivered 9 of 10."""
    from tests.test_learn_outcomes import T0

    ctx = make_context(BASE, now=T0 + timedelta(days=50), job=metrics.JOB)
    ctx.store.insert("events", ten_companies(50))
    out = run(ctx)
    day = date(2026, 10, 20)  # yesterday, UK
    assert out["day"] == day.isoformat()
    r = one(ctx.store, "engaged_rate_45")
    assert (r["day"], r["k"], r["n"], r["value"], r["definition_version"]) == (day, 2, 10, 0.2, metrics.DEFINITION_VERSION)
    assert (one(ctx.store, "meeting_any")["k"], one(ctx.store, "meeting_in_45")["k"]) == (1, 1)
    assert (one(ctx.store, "human_rate_28")["k"], one(ctx.store, "human_rate_28")["n"]) == (2, 10)
    assert (one(ctx.store, "positive_rate_28")["k"], one(ctx.store, "negative_rate_28")["k"]) == (1, 1)
    assert (one(ctx.store, "delivered")["k"], one(ctx.store, "delivered")["n"]) == (9, 10)
    # The cohort week of 1 Sep (Tuesday of 2026-W36, whose Monday is 31 Aug) is still open on 21 Oct: 31 Aug + 53 days
    # (metrics.OPEN_DAYS) = 23 Oct. Its row sits on its Monday.
    assert out["cohorts"] == ["2026-W36"]
    c = one(ctx.store, "engaged_rate_45", "cohort=2026-W36")
    assert (c["day"], c["k"], c["n"]) == (date(2026, 8, 31), 2, 10)
    before = len(ctx.store.select("metrics"))
    run(ctx)  # the same day again: the same keys, upserted
    assert len(ctx.store.select("metrics")) == before
    assert one(ctx.store, "engaged_rate_45")["k"] == 2


def test_matured_rates_count_only_the_companies_whose_window_has_closed():
    """metrics-audit H2 (a rate read on open windows), on a staggered world read Wed 21 Oct 10:00 UTC:
      g0-g9 sent step 1 50 days before (Tue 1 Sep, cohort W36): g0 a positive reply on day 3; g1 a meeting on day
        20; g2 unsubscribed on day 5; g3 unsubscribed on day 35 (after the 28 days); g4 a negative reply on day 4;
        g5 sampled by the hand-check.
      m0-m4 sent 29 days before (Tue 22 Sep, W39): m0 a positive reply on day 2.
      y0-y4 sent 10 days before (Sun 11 Oct, W41): y0 a positive reply on day 1 and a meeting on day 3; y1 a negative
        reply on day 2; y2 sent step 2 on day 7, which bounced; y3 bounced at step 1.
    Matured at 45 days: the 10 g's; at 28 days: the 10 g's and the 5 m's = 15.
      engaged_rate_45 (O2): g0, g1 = 2 of 10 (over all 20 it would be g0, g1, m0, y0 = 4 of 20).
      positive_rate_28 (O3): g0, m0 = 2 of 15 (all: + y0 = 3 of 20); positive_all 3 of 20.
      human_rate_28 (O4): g0, g4, m0 = 3 of 15 (all: + y0, y1 = 5 of 20).
      negative_rate_28 (O6): g2 (unsubscribed inside 28 days) and g4 = 2 of 15; g3's unsubscribe is after the 28 days
        and y1 has not matured.
      engaged (O1, all ages): g0, g1, m0, y0 = 4 of 20. hand_check_held (X1): g5 = 1 of 20.
      D1: the step-1 sends of the last 30 days are the m's and y's, 10 contacts; bounced at step 1 y3 = 1 of 10 (y2's
        bounce is at step 2).
    The cohort rows: W39 has none at 45 days (0 of 0) and all 5 at 28 days (m0: 1 of 5 positive, 1 of 5 human); W41
    has none matured at either (0 of 0)."""
    now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    old, mid, young = now - timedelta(days=50), now - timedelta(days=29), now - timedelta(days=10)
    ev = ([sent(f"g{i}", old) for i in range(10)] + [sent(f"m{i}", mid) for i in range(5)]
          + [sent(f"y{i}", young) for i in range(5)] + [sent("y2", young + timedelta(days=7), step=2)])

    def reply(aid: str, at: datetime, cls: str) -> dict:
        return {"event_id": f"r-{aid}", "type": "replied", "account_id": aid, "contact_id": f"c-{aid}",
                "reply_class": cls, "occurred_at": at}

    def other(kind: str, aid: str, at: datetime, **kw) -> dict:
        return {"event_id": f"{kind}-{aid}", "type": kind, "account_id": aid, "contact_id": f"c-{aid}",
                "occurred_at": at, **kw}

    ev += [reply("g0", old + timedelta(days=3), "positive"), other("meeting_booked", "g1", old + timedelta(days=20)),
           other("unsubscribed", "g2", old + timedelta(days=5)), other("unsubscribed", "g3", old + timedelta(days=35)),
           reply("g4", old + timedelta(days=4), "negative"), reply("m0", mid + timedelta(days=2), "positive"),
           reply("y0", young + timedelta(days=1), "positive"), other("meeting_booked", "y0", young + timedelta(days=3)),
           reply("y1", young + timedelta(days=2), "negative"),
           other("bounced", "y2", young + timedelta(days=7, minutes=5), step=2),
           other("bounced", "y3", young + timedelta(minutes=5), step=1)]
    ctx = make_context(BASE, now=now, job=metrics.JOB)
    ctx.store.insert("events", ev)
    ctx.store.insert("signal_events", [{"event_id": "hand_check:2026-W36:g5", "account_id": "g5",
                                        "source": "hand_check", "fact": "judgement", "observed_at": old,
                                        "value": {"account_id": "g5", "decision": "fine"}}])
    run(ctx)

    def kn(metric: str, segment: str = "all") -> tuple[int, int]:
        r = one(ctx.store, metric, segment)
        return r["k"], r["n"]

    assert kn("engaged_rate_45") == (2, 10) and one(ctx.store, "engaged_rate_45")["value"] == 0.2
    assert kn("positive_rate_28") == (2, 15) and kn("positive_all") == (3, 20)
    assert kn("human_rate_28") == (3, 15)
    assert kn("negative_rate_28") == (2, 15)
    assert kn("engaged") == (4, 20) and kn("hand_check_held") == (1, 20)
    assert (kn("matured_45"), kn("matured_28")) == ((10, 20), (15, 20))
    assert kn("step1_bounce_contact") == (1, 10)
    w39, w41 = "cohort=2026-W39", "cohort=2026-W41"
    assert [kn(m, w39) for m in ("engaged_rate_45", "positive_rate_28", "human_rate_28")] == [(0, 0), (1, 5), (1, 5)]
    assert [kn(m, w41) for m in ("engaged_rate_45", "positive_rate_28", "human_rate_28")] == [(0, 0), (0, 0), (0, 0)]
    assert one(ctx.store, "engaged_rate_45", w39)["value"] is None  # no rate on no matured company
    # No contact here carries a sham_arm stamp (enrolled before it existed): the cut falls back to the same hash.
    ids = [f"{g}{i}" for g, size in (("g", 10), ("m", 5), ("y", 5)) for i in range(size)]
    in_a = sum(queue.sham_arm(x) == "a" for x in ids)
    assert (kn("contacted", "sham_arm=a")[0], kn("contacted", "sham_arm=b")[0]) == (in_a, 20 - in_a)


def test_the_reply_curve_o1b_and_exposure():
    """Four companies sent step 1 30 days before now (and one 10 days before):
      p1: a human reply on day 3 (objection); p2: a positive on day 10 and a booking on day 12 recorded as PAID_SEARCH;
      p3: a positive on day 20 and a booking on day 25 from ORGANIC_SEARCH, all four emails sent; p4: nothing, its
      mailbox paused by a kill rule on day 5; p5 (10 days old) a booking on day 2 with no recorded source.
    By day 7: 5 companies reached it; human 1 (p1), positive 0. By day 14: 4 reached (p5 has not); human 2 (p1, p2),
    positive 1. By day 21 and 28: 4 reached; human 3, positive 2.
    O1a any booking 3 of 5 (p2, p3, p5); O1b outbound-consistent 2 (p3 organic, p5 unknown: nothing says an ad).
    X1: full sequence among the 4 past day 22: 1 (p3); mailbox paused 1 of 5 (p4)."""
    now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    t = now - timedelta(days=30)
    ev = [sent(a, t, mailbox=f"{a}@meetspill.org") for a in ("p1", "p2", "p3", "p4")]
    ev += [sent("p5", now - timedelta(days=10))]
    ev += [sent("p3", t + timedelta(days=7 * k), step=k + 1) for k in range(1, 4)]
    ev += [{"event_id": "r1", "type": "replied", "account_id": "p1", "contact_id": "c-p1", "reply_class": "objection",
            "occurred_at": t + timedelta(days=3)},
           {"event_id": "r2", "type": "replied", "account_id": "p2", "contact_id": "c-p2", "reply_class": "positive",
            "occurred_at": t + timedelta(days=10)},
           {"event_id": "r3", "type": "replied", "account_id": "p3", "contact_id": "c-p3", "reply_class": "referral",
            "occurred_at": t + timedelta(days=20)},
           {"event_id": "m2", "type": "meeting_booked", "account_id": "p2", "analytics_source": "PAID_SEARCH",
            "occurred_at": t + timedelta(days=12)},
           {"event_id": "m3", "type": "meeting_booked", "account_id": "p3", "analytics_source": "ORGANIC_SEARCH",
            "occurred_at": t + timedelta(days=25)},
           {"event_id": "m5", "type": "meeting_booked", "account_id": "p5", "occurred_at": now - timedelta(days=8)}]
    ctx = make_context(BASE, now=now, job=metrics.JOB)
    ctx.store.insert("events", ev)
    ctx.store.insert("hitl_items", [{"item_id": "k1", "kind": "kill_rule", "status": "open",
                                     "created_at": t + timedelta(days=5),
                                     "payload": {"action": "pause_mailbox", "mailboxes": ["p4@meetspill.org"]}}])
    run(ctx)
    curve = {d: (one(ctx.store, f"human_by_day_{d}")["k"], one(ctx.store, f"positive_by_day_{d}")["k"],
                 one(ctx.store, f"human_by_day_{d}")["n"]) for d in metrics.CURVE_DAYS}
    assert curve == {7: (1, 0, 5), 14: (2, 1, 4), 21: (3, 2, 4), 28: (3, 2, 4)}
    assert (one(ctx.store, "meeting_any")["k"], one(ctx.store, "meeting_outbound")["k"]) == (3, 2)
    assert (one(ctx.store, "full_sequence")["k"], one(ctx.store, "full_sequence")["n"]) == (1, 4)
    assert (one(ctx.store, "mailbox_paused")["k"], one(ctx.store, "mailbox_paused")["n"]) == (1, 5)
    # The mailbox cut: p1's own mailbox holds one company, which replied (human) within 28 days.
    m = one(ctx.store, "human_rate_28", "mailbox=p1@meetspill.org")
    assert (m["k"], m["n"]) == (1, 1)


def test_lost_leads_from_five_enrol_summaries_and_two_leads_never_sent():
    """W3-1's five summaries (tests/test_throughput.week): capacity three days with free 14, 21, 25 and as many
    enrolled lose 0; the gate day (the hand-check held Thursday) loses the day's share, ceil(150 ÷ 5) = 30; the supply
    day's room is 25 with 10 ready, so 15; two leads enrolled four days ago with no step 1 sent are never_sent 2."""
    ctx = five_summaries(first_send_days_ago=30)
    ctx.job = metrics.JOB
    ctx.store.insert("contacts", [{"contact_id": f"n{i}", "account_id": f"na{i}",
                                   "enrolled_at": THROUGHPUT_NOW - timedelta(days=4)} for i in range(2)])
    run(ctx)
    lost = {r["metric"].split(":")[1]: r["k"] for r in metrics.latest(ctx.store) if r["metric"].startswith("lost_leads:")}
    assert lost == {"capacity": 0, "supply": 15, "gate": 30, "blackout": 0, "errors": 0, "never_sent": 2}
    # P3: maria sent 22 + 1 of a 30 cap over the 5 send days (the sends 3 and 30 days ago: only the 22 fall in the last
    # 7 days), 22 ÷ 150 = 0.147.
    m = one(ctx.store, "mailbox_utilisation", "mailbox=maria@meetspill.org")
    assert (m["k"], m["n"], round(m["value"], 3)) == (22, 150, 0.147)


# -- definition versions ---------------------------------------------------------------------------------------------


def test_a_definition_version_bump_recomputes_the_open_cohorts_and_the_8_closed_weeks_before_them(monkeypatch):
    """One company sent step 1 on the Monday of each of the 18 weeks W26 to W43, read on Wed 21 Oct (W43).
    Open on 21 Oct (Monday + 53 days still ahead): W36 (31 Aug + 53 = 23 Oct) to W43; W35 (24 Aug + 53 = 16 Oct) is
    closed. The 8 calendar weeks before W36 are W28 to W35.
    A run a week earlier (14 Oct) under version 1: open then W35 (closing 16 Oct) to W42, closed and recomputed W27
    (W34 closed 9 Oct) to W34.
    Then version 2 on 21 Oct: open W36 to W43 and closed W28 to W35 are written under version 2; W27, the ninth
    closed week, keeps version 1 only. The reader takes version 2 wherever both exist."""
    now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    events = [sent(f"w{n}", datetime.combine(date.fromisocalendar(2026, n, 1), datetime.min.time(), UTC)
                   + timedelta(hours=14)) for n in range(26, 44)]
    monkeypatch.setattr(metrics, "DEFINITION_VERSION", "2026-10-14")
    ctx = make_context(BASE, now=now - timedelta(days=7), job=metrics.JOB)
    ctx.store.insert("events", events[:-1])  # W43's company is sent on 19 Oct, after this run
    first = run(ctx)
    assert first["cohorts"] == [f"2026-W{n}" for n in range(27, 43)]

    ctx.store.insert("events", events[-1:])
    monkeypatch.setattr(metrics, "DEFINITION_VERSION", "2026-10-21")
    ctx2 = make_context(BASE, now=now, job=metrics.JOB, store=ctx.store)
    second = run(ctx2)
    assert second["cohorts"] == [f"2026-W{n}" for n in range(28, 44)]

    def versions(week: str) -> set[str]:
        return {r["definition_version"] for r in ctx.store.select(
            "metrics", {"segment_key": f"cohort={week}", "metric": "contacted"})}

    assert versions("2026-W27") == {"2026-10-14"}
    assert versions("2026-W28") == versions("2026-W35") == {"2026-10-14", "2026-10-21"}
    assert versions("2026-W43") == {"2026-10-21"} and versions("2026-W26") == set()
    assert one(ctx.store, "contacted", "cohort=2026-W30")["definition_version"] == "2026-10-21"
    assert one(ctx.store, "contacted", "cohort=2026-W27")["definition_version"] == "2026-10-14"
    # A third run the same day under version 2 recomputes the open weeks only: the closed ones have version-2 rows
    # computed after they closed.
    assert run(ctx2)["cohorts"] == [f"2026-W{n}" for n in range(36, 44)]


def test_a_cohort_stays_open_until_its_sunday_company_has_had_45_days_and_is_recomputed_once_after_it_closes():
    """W36 (Monday 31 Aug): A sent Mon 31 Aug 14:00 UTC, B Sun 6 Sep 20:00 UTC (21:00 UK, still W36). B has had 45
    days at 21 Oct 20:00 UTC. W36 closes at 31 Aug 00:00 UK + 53 days = 23 Oct 00:00 UK = 22 Oct 23:00 UTC.
      Wed 21 Oct 08:50 UK: open, B not matured: matured_45 1 of 2.
      Thu 22 Oct 08:50 UK: still open (with 52 days it would have closed at the start of the day), B matured: 2 of 2.
      Then a booking for A on day 30 is read back late. Fri 23 Oct: W36 has closed; its rows were computed before it
      closed, so it is recomputed once: engaged 1 of 2. Sat 24 Oct: its row was computed after it closed: not again."""
    a = sent("A", datetime(2026, 8, 31, 14, tzinfo=UTC))
    b = sent("B", datetime(2026, 9, 6, 20, tzinfo=UTC))
    assert metrics.closes("2026-W36") == datetime(2026, 10, 22, 23, tzinfo=UTC)
    st = make_context(BASE).store
    st.insert("events", [a, b])

    def run_on(day: int) -> dict:
        return run(make_context(BASE, now=datetime(2026, 10, day, 7, 50, tzinfo=UTC), job=metrics.JOB, store=st))

    def kn(metric: str) -> tuple[int, int]:
        r = one(st, metric, "cohort=2026-W36")
        return r["k"], r["n"]

    assert run_on(21)["cohorts"] == ["2026-W36"] and kn("matured_45") == (1, 2)
    assert run_on(22)["cohorts"] == ["2026-W36"] and kn("matured_45") == (2, 2)
    st.insert("events", [{"event_id": "m-A", "type": "meeting_booked", "account_id": "A",
                             "occurred_at": a["occurred_at"] + timedelta(days=30)}])
    assert run_on(23)["cohorts"] == ["2026-W36"] and kn("engaged_rate_45") == (1, 2)
    assert run_on(24)["cohorts"] == []


def test_o9_counts_the_month_of_the_rows_day():
    """Sun 1 Nov 2026 08:50 UK (GMT): the row is for Sat 31 Oct, so October's spend over October's companies. October:
    4 companies sent step 1 (20 Oct), 100 Apollo credits (15 Oct) and $2.00 of Claude (20 Oct): 25 credits and $0.50
    per company. November so far (1 company, 7 credits, $0.50 before the run) is not in it."""
    now = datetime(2026, 11, 1, 8, 50, tzinfo=UTC)
    ctx = make_context(BASE, now=now, job=metrics.JOB)
    ctx.store.insert("events", [sent(f"o{i}", datetime(2026, 10, 20, 15, tzinfo=UTC)) for i in range(4)]
                     + [sent("n0", datetime(2026, 11, 1, 7, tzinfo=UTC))])
    ctx.store.insert("credit_ledger", [
        {"entry_id": "a1", "system": "apollo", "job": "pick_contacts", "credits": 100,
         "occurred_at": datetime(2026, 10, 15, 12, tzinfo=UTC)},
        {"entry_id": "a2", "system": "apollo", "job": "pick_contacts", "credits": 7,
         "occurred_at": datetime(2026, 11, 1, 7, tzinfo=UTC)},
        {"entry_id": "c1", "system": "claude", "job": "poll_replies", "usd": 2.0,
         "occurred_at": datetime(2026, 10, 20, 12, tzinfo=UTC)},
        {"entry_id": "c2", "system": "claude", "job": "poll_replies", "usd": 0.5,
         "occurred_at": datetime(2026, 11, 1, 6, tzinfo=UTC)}])
    run(ctx)
    apollo, claude = one(ctx.store, "apollo_credits_per_contacted"), one(ctx.store, "claude_usd_per_contacted")
    assert (apollo["day"], apollo["k"], apollo["n"], apollo["value"]) == (date(2026, 10, 31), 100, 4, 25.0)
    assert (claude["n"], claude["value"]) == (4, 0.5)


# -- forecasts -------------------------------------------------------------------------------------------------------


def _week(n: int, companies: int) -> list[dict]:
    """`companies` companies sent step 1 on the Tuesday of ISO week n, 2026, at 15:00 UTC."""
    at = datetime.combine(date.fromisocalendar(2026, n, 2), datetime.min.time(), UTC) + timedelta(hours=15)
    return [sent(f"f{n}-{i}", at) for i in range(companies)]


def _monday(n: int) -> datetime:
    return datetime.combine(date.fromisocalendar(2026, n, 1), datetime.min.time(), UTC) + timedelta(hours=8)


def test_a_forecast_written_on_monday_is_scored_the_next_and_three_weeks_outside_are_flagged():
    """Weeks W39 to W42 contacted 120, 130, 125, 125: on Mon 19 Oct (W43) forecast:contacted = their mean, 125, with
    ±20%: 100 to 150. W43 then contacts 118: on Mon 26 Oct it is scored inside (k 1, value 118).
    W44, W45 and W46 contact 50 each. The forecasts: W44 from W40-W43 = (130 + 125 + 125 + 118) ÷ 4 = 124.5,
    interval round(99.6) = 100 to round(149.4) = 149; W45 from W41-W44 = 104.5: 84 to 125; W46 from W42-W45 = 85.75:
    69 to 103. 50 is outside each, so on Mon 16 Nov the scorecard has W46 outside for the third week running (W44 and
    W45 scored outside before it): the flag."""
    ev = _week(39, 120) + _week(40, 130) + _week(41, 125) + _week(42, 125)
    ctx = make_context(BASE, now=_monday(43), job=metrics.JOB)
    ctx.store.insert("events", ev)
    assert "contacted" in run(ctx)["forecasts"]
    f = {r["metric"]: r["value"] for r in metrics.latest(ctx.store, ["forecast:contacted", "forecast:contacted:low",
                                                                     "forecast:contacted:high"])}
    assert f == {"forecast:contacted": 125.0, "forecast:contacted:low": 100.0, "forecast:contacted:high": 150.0}
    assert one(ctx.store, "forecast:contacted")["day"] == date(2026, 10, 19)
    assert "contacted" not in run(ctx)["forecasts"]  # once a week

    ctx.store.insert("events", _week(43, 118))
    for n in (44, 45, 46):
        c = make_context(BASE, now=_monday(n), job=metrics.JOB, store=ctx.store)
        out = run(c)
        assert f"contacted 2026-W{n - 1}" in out["scored"]
        ctx.store.insert("events", _week(n, 50))
    scores = {r["day"]: (r["k"], r["value"]) for r in ctx.store.select("metrics", {"metric": "forecast_score:contacted"})}
    assert scores == {date(2026, 10, 19): (1, 118.0), date(2026, 10, 26): (0, 50.0), date(2026, 11, 2): (0, 50.0)}
    w45 = {r["metric"]: r["value"] for r in ctx.store.select("metrics", {"day": date(2026, 11, 2)})
           if r["metric"].startswith("forecast:contacted")}
    assert w45 == {"forecast:contacted": 104.5, "forecast:contacted:low": 84.0, "forecast:contacted:high": 125.0}

    readout_day = make_context(BASE, now=_monday(47), job="monday_readout", store=ctx.store)
    card = metrics.scorecard(readout_day)
    [c] = [x for x in card if x["name"] == "contacted"]
    assert (c["point"], c["low"], c["high"], c["actual"], c["inside"]) == (85.75, 69.0, 103.0, 50.0, False)
    assert c["outside_weeks"] == 3 and metrics.flags(card) == ["contacted"]
    # A week earlier only two weeks were outside running: no flag.
    card = metrics.scorecard(make_context(BASE, now=_monday(46), job="monday_readout", store=ctx.store))
    assert [x["outside_weeks"] for x in card if x["name"] == "contacted"] == [2] and metrics.flags(card) == []


def test_the_positive_maturing_forecast_uses_the_prior_under_30_matured():
    """On Mon 19 Oct the companies whose 28-day window closes that week were sent step 1 from 21 Sep to 27 Sep: 40
    of them, and none had matured before (fewer than 30), so p = SPEC 12's 1%: point 0.4, the 5% and 95% binomial
    quantiles of Binomial(40, 0.01): P(X = 0) = 0.99^40 = 0.669 ≥ 0.05, so 0; P(X ≤ 1) = 0.669 + 40 × 0.01 × 0.99^39
    = 0.939 < 0.95 and P(X ≤ 2) = 0.939 + 780 × 0.0001 × 0.99^38 = 0.993, so 2."""
    ctx = make_context(BASE, now=_monday(43), job=metrics.JOB)
    ctx.store.insert("events", _week(39, 40))
    run(ctx)
    got = {r["metric"]: r["value"] for r in metrics.latest(ctx.store) if r["metric"].startswith("forecast:positive")}
    assert got == {"forecast:positive_maturing": pytest.approx(0.4), "forecast:positive_maturing:low": 0.0,
                   "forecast:positive_maturing:high": 2.0}


def _positive(companies: list[dict], replies: int) -> list[dict]:
    """A positive reply a day after step 1 from the first `replies` of these companies' step-1 sends."""
    return [{"event_id": f"r-{e['account_id']}", "type": "replied", "account_id": e["account_id"],
             "contact_id": e["contact_id"], "reply_class": "positive", "occurred_at": e["occurred_at"] + timedelta(days=1)}
            for e in companies[:replies]]


def test_the_positive_maturing_forecast_switches_to_the_observed_rate_at_30_matured():
    """The same 40 maturing in W43 (sent in W39). 29 companies sent in W36 (1 Sep: 28 days had passed by Mon 19 Oct),
    3 of them positive: under 30 matured, so still the 1% prior: point 0.4, 0 to 2 (the test above).
    With a 30th W36 company: 30 matured, 3 positive, p = 0.1: point 40 × 0.1 = 4.0; Binomial(40, 0.1):
    P(X = 0) = 0.9^40 = 0.0148, P(X ≤ 1) = 0.0148 + 40 × 0.1 × 0.9^39 = 0.0805 ≥ 0.05, so low 1;
    P(X ≤ 6) = 0.9005 < 0.95 and P(X ≤ 7) = 0.9005 + C(40, 7) × 0.1^7 × 0.9^33 = 0.9581, so high 7."""
    def forecast(matured: int) -> dict:
        w36 = _week(36, matured)
        ctx = make_context(BASE, now=_monday(43), job=metrics.JOB)
        ctx.store.insert("events", _week(39, 40) + w36 + _positive(w36, 3))
        run(ctx)
        return {r["metric"]: r["value"] for r in metrics.latest(ctx.store) if r["metric"].startswith("forecast:pos")}

    assert forecast(29) == {"forecast:positive_maturing": pytest.approx(0.4), "forecast:positive_maturing:low": 0.0,
                            "forecast:positive_maturing:high": 2.0}
    assert forecast(30) == {"forecast:positive_maturing": pytest.approx(4.0), "forecast:positive_maturing:low": 1.0,
                            "forecast:positive_maturing:high": 7.0}


# -- every id readable now, and a block that fails ----------------------------------------------------------------------


def _populated():
    """A world with something for every section 4 id that can be read now (the docstring of learn/metrics.py)."""
    ctx = five_summaries(first_send_days_ago=30)  # enrol runs (P2, S1), sends past ramp week 3 (P3, D2)
    ctx.job = metrics.JOB
    now = THROUGHPUT_NOW
    t = now - timedelta(days=50)
    shift = datetime(2026, 9, 1, 12, tzinfo=UTC) - t  # the ten companies' step 1, moved to 50 days before now
    ev = [dict(e, occurred_at=e["occurred_at"] - shift) for e in ten_companies(50)]
    # K: sent step 1 20 days ago, a positive reply 2 days ago, answered from the reply desk 3 hours later (O8).
    ev += [sent("a-K", now - timedelta(days=20)),
           {"event_id": "r-K", "type": "replied", "account_id": "a-K", "contact_id": "c-a-K", "reply_class": "positive",
            "occurred_at": now - timedelta(days=2)},
           {"event_id": "rs-K", "type": "reply_sent", "account_id": "a-K", "contact_id": "c-a-K",
            "occurred_at": now - timedelta(days=2) + timedelta(hours=3)}]
    ctx.store.insert("events", ev)
    ctx.store.insert("contacts", [{"contact_id": "c-C", "account_id": "a-C", "email_source": "apollo", "arm": "random",
                                   "sham_arm": "a"}])
    ctx.store.insert("signal_events", [
        judgement(1, "2026-W43", "fine"), judgement(2, "2026-W43", "leave out", reason="subsidiary"),
        {"event_id": "label_policy:2026-10", "account_id": None, "source": "label_sample", "fact": "label_policy",
         "observed_at": now - timedelta(days=20),
         "value": {"policy": {}, "batches": ["2026-10"], "sample": {"judged": 25, "label_right": 17, "group_right": 22,
                                                                     "label_copy": {"judged": 30, "wrong_pitch": 4},
                                                                     "rejudged": 2, "rejudge_agreed": 2}}},
        {"event_id": "capture:2026-W44", "account_id": None, "source": "capture", "fact": "capture_unverified",
         "observed_at": now - timedelta(hours=1),
         "value": {"reasons": ["drift:replied"], "drift": [{"measure": "replied", "instantly": 2, "ours": 0}]}},
        {"event_id": "sb-C", "account_id": "a-C", "source": "source_universe", "fact": "sourced_by",
         "observed_at": now - timedelta(days=52), "value": {"slice": "legal:NY:50-99", "group": "Legal Teams",
                                                            "state": "NY", "band": "50-99"}}])
    ctx.store.insert("accounts", [{"account_id": "a-C", "domain": "c.com", "status": "enrolled", "hq_state": "NY"}])
    ctx.store.insert("events", [
        {"event_id": "apollo_balance:a", "type": "apollo_balance", "amount": 29677.0,
         "occurred_at": now - timedelta(days=8)},
        {"event_id": "apollo_balance:b", "type": "apollo_balance", "amount": 29435.0,
         "occurred_at": now - timedelta(days=1)}])
    ctx.store.insert("credit_ledger", [{"entry_id": "l1", "system": "apollo", "job": "pick_contacts", "credits": 132,
                                        "occurred_at": now - timedelta(days=4)}])
    return ctx


def test_every_section_4_id_readable_now_has_rows():
    """Acceptance (W3-1): metrics has rows for every id in design-final §4 that can be read now: all of them but D4
    (complaints, "not measured" until a feed exists, D11). Some of the populated world's numbers, by hand:
      T1 1 fine of 2 judged (W43); T2 subsidiary 1 of 1 leave-out; T3 exact 17 of 25, group 22 of 25, label copy 4 of
      30 wrong; T1r 2 of 2; D3 0 of 1 (unverified) with 1 drifting measure; S3 the balance fell 29,677 − 29,435 = 242
      over 7 days, the ledger has 132, so (242 − 132) ÷ 242 = 0.455; O8 K's positive reply (2 days ago; C's are 49
      days old, outside the 28 days) answered after 3 hours: 1 of 1 within 24 hours, median 3."""
    ctx = _populated()
    out = run(ctx)
    assert set(out["ids"]) == set(metrics.READABLE_NOW) and "D4" not in out["ids"]
    assert all(metrics.id_of(r["metric"]) for r in ctx.store.select("metrics"))
    assert (one(ctx.store, "icp_precision")["k"], one(ctx.store, "icp_precision")["n"]) == (1, 2)
    assert one(ctx.store, "leave_out:subsidiary")["k"] == 1
    assert [(one(ctx.store, m)["k"], one(ctx.store, m)["n"]) for m in
            ("label_exact", "label_group", "label_copy_wrong_pitch", "rater_agreement")] == [(17, 25), (22, 25),
                                                                                            (4, 30), (2, 2)]
    assert (one(ctx.store, "capture_verified")["k"], one(ctx.store, "capture_drift")["k"]) == (0, 1)
    s3 = one(ctx.store, "spend_drift")
    assert (s3["k"], s3["n"], round(s3["value"], 3)) == (132, 242, 0.455)
    assert (one(ctx.store, "positive_response_24h")["k"], one(ctx.store, "positive_response_median_hours")["value"]) \
        == (1, 3.0)
    assert one(ctx.store, "human_rate_28", "slice=legal:NY:50-99")["n"] == 1
    assert one(ctx.store, "human_rate_28", "state=NY")["k"] == 1


def test_a_block_that_fails_is_named_and_the_rest_are_written(monkeypatch):
    from us_outbound.learn import throughput

    def broken(*_a, **_k):
        raise KeyError("number_terms")

    monkeypatch.setattr(throughput, "report", broken)
    ctx = _populated()
    out = health.run_job(ctx, metrics.run)
    assert out["errors"] == ["metrics_daily throughput: KeyError: 'number_terms'"]
    assert "P2" not in out["ids"] and "O2" in out["ids"]
    assert ctx.store.select("heartbeats", {"job": metrics.JOB})[0]["status"] == "ok"  # findings, not a failed run


def test_it_writes_only_its_table_in_dry_run_as_live():
    ctx = _populated()
    before = {k: list(v) for k, v in ctx.store.tables.items()}
    health.run_job(ctx, metrics.run)
    assert ctx.dry_run
    assert {k for k, v in ctx.store.tables.items() if v != before[k]} == {"metrics", "heartbeats"}


def test_the_reader_takes_the_latest_version_per_day_segment_and_metric():
    ctx = make_context(BASE, now=THROUGHPUT_NOW)
    d = date(2026, 10, 20)
    ctx.store.upsert("metrics", [
        {**metrics.rate(d, "all", "delivered", 9, 10), "definition_version": "2026-10-01", "computed_at": THROUGHPUT_NOW},
        {**metrics.rate(d, "all", "delivered", 8, 10), "definition_version": "2026-10-20",
         "computed_at": THROUGHPUT_NOW - timedelta(days=1)},
        {**metrics.rate(d, "arm=random", "delivered", 1, 1), "definition_version": "2026-10-01",
         "computed_at": THROUGHPUT_NOW}])
    got = {(r["segment_key"], r["k"], r["definition_version"]) for r in metrics.latest(ctx.store, "delivered")}
    assert got == {("all", 8, "2026-10-20"), ("arm=random", 1, "2026-10-01")}


def test_registered_on_the_schedule_heartbeat_and_cli():
    from us_outbound.base import say
    from us_outbound.ops import cli, schedule

    job = schedule.by_name()[metrics.JOB]
    assert (job.cron, job.live, job.enabled) == ("50 8 * * *", False, True)
    assert cli.JOBS[metrics.JOB] == "us_outbound.learn.metrics:run" and health.EXPECTED[metrics.JOB] == 26 * 60
    assert metrics.JOB in health.IMPACT and say.job_name(metrics.JOB) == "keeping the daily numbers"
    # After the Monday readout (08:30), which scores last week's forecasts itself, before the daily post (09:00).
    assert schedule.by_name()["monday_readout"].cron.split()[:2] == ["30", "8"]


def test_metrics_daily_writes_and_upserts_on_postgres(store):  # noqa: F811
    """End to end on Postgres (tests/test_db.py's learning_world): 70 companies sent step 1 48 or more days ago, all
    past 45 days; engaged a00-a02 (positive replies; a00 and a02 also booked within 45 days) = 3 of 70. A second run
    the same day upserts the same keys (and writes no second forecast: one a week)."""
    from tests.test_db import learning_world
    from us_outbound.clients.guard import Guard

    now = learning_world(store)
    store.guard = Guard()
    ctx = make_context(BASE, store=store, now=now, job=metrics.JOB)
    out = run(ctx)
    r = one(store, "engaged_rate_45")
    assert (r["k"], r["n"], round(r["value"], 4)) == (3, 70, 0.0429)
    before = len(store.select("metrics"))
    again = run(ctx)
    assert out["forecasts"] and again["forecasts"] == [] and len(store.select("metrics")) == before
