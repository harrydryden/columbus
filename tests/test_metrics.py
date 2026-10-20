"""metrics_daily (learn/metrics.py; W3-1, 20 Oct 2026; design-final §4): the persisted series. Each expected number is
worked out by hand from the world in its test's docstring or comment, not read off the code's output."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_learn_outcomes import world as ten_companies
from tests.test_precision import judgement
from tests.test_throughput import NOW as THROUGHPUT_NOW
from tests.test_throughput import week as five_summaries
from us_outbound.learn import metrics
from us_outbound.ops import health
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

BASE, _ = validate_all(default_tabs())


def one(store, metric: str, segment: str = "all") -> dict:
    [r] = [r for r in metrics.latest(store, metric, segment)]
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
    # The cohort week of 1 Sep (Tuesday of 2026-W36, whose Monday is 31 Aug) is still open on 21 Oct: 31 Aug + 52 days
    # = 22 Oct. Its row sits on its Monday.
    assert out["cohorts"] == ["2026-W36"]
    c = one(ctx.store, "engaged_rate_45", "cohort=2026-W36")
    assert (c["day"], c["k"], c["n"]) == (date(2026, 8, 31), 2, 10)
    before = len(ctx.store.select("metrics"))
    run(ctx)  # the same day again: the same keys, upserted
    assert len(ctx.store.select("metrics")) == before
    assert one(ctx.store, "engaged_rate_45")["k"] == 2


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
    Open on 21 Oct (Monday + 52 days still ahead): W36 (31 Aug + 52 = 22 Oct) to W43; W35 (24 Aug + 52 = 15 Oct) is
    closed. The 8 calendar weeks before W36 are W28 to W35.
    A run a week earlier (14 Oct) under version 1: open then W35 to W42, closed and recomputed W27 to W34.
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
    # A third run the same day under version 2 recomputes the open weeks only: the closed ones have their rows.
    assert run(ctx2)["cohorts"] == [f"2026-W{n}" for n in range(36, 44)]


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
