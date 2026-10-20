"""metrics_daily, 08:50 UK every day (W3-1, 20 Oct 2026; design-final §4, loops L1-L13): the persisted series.

Every number the loops need lived in heartbeat JSON or 7-day logs, so no loop could read a series (design-final W3-1).
This job writes them to the metrics table (sql/ddl/22_metrics.sql), one row per (day, segment_key, metric,
definition_version), upserted, so a second run the same day replaces its rows. It reads only the database and
writes only the metrics table, in dry-run as live (the database is no outside system, README "Dry-run and live").

What it writes (IDS maps each metric to its design-final §4 id):
  * Outcomes (learn/outcomes.per_company, the one definition): for `all` and each cut (arm, sham_arm, group,
    size_band, state, slice, source, mailbox, gate) on yesterday, and for each step-1 cohort week (segment
    cohort=<ISO week>, day its Monday): contacted, delivered, matured at 28 and 45 days, engaged (O1), any booking (O1a)
    and an outbound-consistent one (O1b: the booker's HubSpot source is not paid search or paid social; a booking
    with no recorded source counts, as nothing says it came from an ad), engaged_rate_45 (O2, matured at 45 days,
    intent to treat), positive and human reply rates at 28 days and at all ages (O3, O4), the reply curve by day 7,
    14, 21 and 28 (O5), negative reception (O6), the realised explore share (O7), and exposure (X1: the share sent all
    four emails once 22 days have passed, the share whose mailbox a kill rule paused, the share the hand-check held).
    The A/A sham arm is a cut like any other (sham_arm=a, sham_arm=b): a difference there is pipeline bias.
  * The day's process numbers: P1 companies contacted this ISO week against weekly_enrol_cap; P2 leads lost by cause
    over the last 7 days (learn/throughput.py); P3 each mailbox's use past ramp week 3; D1 step-1 bounces per contact
    over 30 days by email source; D2 each mailbox's bounces over its last 100 sends; D3 this week's reply-capture
    check (replies/capture.py); T1 ICP precision pooled over 4 ISO weeks (learn/precision.py) and by gate version, T2
    the leave-out reasons, T1r the rater's agreement with himself and T3 the label sample (labels.policy_fact); T4 the
    verify pass rate and each slice's contactable yield (sources/allocation.py); T5 the contact-find yield; T6 the
    contactable supply in send days at the weekly target's pace; O8 positive replies answered within 24 hours; O9 the
    month's Apollo credits and Claude dollars per company contacted; S1 each scheduled job's runs and errors, S2 its
    per-item failures in runs that finished, S3 the Apollo balance's fall against the credit ledger.
  * Forecasts (F1, L13): for the week that today falls in, once, from data before its Monday: forecast:contacted (the
    mean of the last FORECAST_WEEKS weeks' companies contacted, ±FORECAST_BAND), forecast:icp_precision (T1 pooled
    to last week, its Wilson interval) and forecast:positive_maturing (the companies whose 28-day window closes this
    week × the matured positive rate, or SPEC 12's 1% under 30 matured; the 5% and 95% binomial quantiles); each with
    :low and :high rows. After the week ends, forecast_score:<name> (k 1 inside, 0 outside; value the actual).
    flags() names a forecast outside its interval FLAG_WEEKS weeks running: a definition or model check.
  * Then the early warning (W3-4, 21 Oct 2026; learn/early_warning.py): an alert, once an ISO week, when the matured
    outcomes are far below plan (no human reply in 100, or a meeting or positive rate likely under half of SPEC 12's),
    which holds new enrolment only with General early_warning_pause = yes. It posts to the alert channel, so the
    scheduler runs this job with --live from W3-4 (ops/schedule.py); the series is written the same either way.

Two clocks. Every row but a cohort's and a forecast's is dated yesterday (UK). P1 and S1 count that UK day and O9 its
month; everything else is as of the run itself (08:50 UK the next day): the rolling windows of P2, P3, D1, D2, O8 and
S3 end then, and every company's age (matured at 28 and 45 days, the reply curve, exposure) is taken then.

Definition versions (RT §6.7). DEFINITION_VERSION is a module constant; a change to any definition above bumps it.
Each run recomputes every open cohort (a company in it could still change: the 45-day window, OPEN_DAYS); a cohort week
among the CLOSED_WEEKS most recent closed ones is recomputed too when it has no row under the current version computed
after it closed: so once more on the first run after it closes (a reply class corrected or a booking read back in its
last day), and once after a bump, which backfills them; a cohort older than that keeps only its old version. Readers
take the latest version per (day, segment, metric) (latest()).

Each block is computed in its own try: one that fails is named under the summary's errors (heartbeat_check posts
it) and the rest are written.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import budget
from us_outbound.clients.claude import month_spend_usd
from us_outbound.clients.db import Range
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import UK, Context
from us_outbound.enrol import queue
from us_outbound.enrol.enrol import iso_week
from us_outbound.learn import outcomes, stats
from us_outbound.learn.outcomes import Outcome
from us_outbound.logs import log
from us_outbound.replies import kinds
from us_outbound.timeparse import uk_midnight, utc

JOB = "metrics_daily"
TABLE = "metrics"
DEFINITION_VERSION = "2026-10-20"  # bump on any change to a definition in this module or in learn/outcomes.py
ALL = "all"
COHORT = "cohort"
CLOSED_WEEKS = 8  # closed cohorts recomputed under a new version (RT §6.7)
# A cohort week is open until its Monday + 53 days: its last possible step 1 (Sunday 23:59 UK, Monday + 7 days) has
# had 45 days by Monday + 52 days, so the 08:50 run that day, still open, reads every company in it matured.
OPEN_DAYS = outcomes.MEETING_DAYS + 8
CURVE_DAYS = (7, 14, 21, 28)  # O5
FULL_SEQUENCE_DAYS = 22  # step 4 goes on day 21 (STEP_DAYS 0/7/14/21); a day's grace for the US send window
D1_DAYS = 30
RESPONSE_HOURS, RESPONSE_DAYS = 24, 28  # O8: positive replies of the last 28 days, answered within 24 hours
PAID_SOURCES = frozenset({"PAID_SEARCH", "PAID_SOCIAL"})  # HubSpot's hs_analytics_source values that are ads (O1b)
CUTS = ("arm", "sham_arm", "group", "size_band", "state", "slice", "source", "mailbox", "gate")
FORECAST_WEEKS = 4  # forecast:contacted averages this many weeks (fewer before the pilot has them)
FORECAST_BAND = 0.20  # its interval: ±20% of the point
FORECAST_LEVEL = 0.90  # the binomial interval of forecast:positive_maturing
PRIOR_POSITIVE = 0.01  # SPEC 12's "about 1%", until MIN_MATURED companies have had 28 days
FLAG_WEEKS = 3  # a forecast outside its interval this many weeks running is flagged (L13)
FORECASTS = ("contacted", "icp_precision", "positive_maturing")

# Each metric (or the prefix before its first ":") -> its design-final §4 id.
IDS: dict[str, str] = {
    "contacted": "O2", "delivered": "O2", "matured_45": "O2", "engaged_rate_45": "O2",
    "engaged": "O1", "meeting_in_45": "O1", "meeting_any": "O1a", "meeting_outbound": "O1b",
    "matured_28": "O3", "positive_rate_28": "O3", "positive_all": "O3",
    "human_rate_28": "O4", "human_all": "O4",
    **{f"{kind}_by_day_{d}": "O5" for kind in ("human", "positive") for d in CURVE_DAYS},
    "negative_rate_28": "O6", "explore_share": "O7",
    "positive_response_24h": "O8", "positive_response_median_hours": "O8",
    "apollo_credits_per_contacted": "O9", "claude_usd_per_contacted": "O9",
    "full_sequence": "X1", "mailbox_paused": "X1", "hand_check_held": "X1",
    "icp_precision": "T1", "rater_agreement": "T1r", "leave_out": "T2",
    "label_exact": "T3", "label_group": "T3", "label_copy_wrong_pitch": "T3",
    "verify_pass_rate": "T4", "slice_yield": "T4", "contact_yield": "T5", "supply_days": "T6",
    "contacted_week": "P1", "lost_leads": "P2", "mailbox_utilisation": "P3",
    "step1_bounce_contact": "D1", "mailbox_bounce_last_100": "D2", "capture_verified": "D3", "capture_drift": "D3",
    "job_runs": "S1", "job_errors": "S1", "item_failures": "S2", "spend_drift": "S3",
    "forecast": "F1", "forecast_score": "F1",
}
# The design-final §4 ids that can be read now (D4, complaints, is "not measured" until a feed exists: D11).
READABLE_NOW = frozenset(IDS.values())


_EPOCH = datetime.min.replace(tzinfo=UTC)


def id_of(metric: str) -> str | None:
    return IDS.get(metric) or IDS.get(metric.split(":", 1)[0])


# -- rows ----------------------------------------------------------------------------------------------------------


def row(day: date, segment: str, metric: str, *, k: int | None = None, n: int | None = None,
        value: float | None = None, now: datetime | None = None) -> dict:
    """One metrics row. A rate's value is k ÷ n (None when n is 0) unless given; a count's value is k."""
    if value is None and k is not None:
        value = (k / n if n else None) if n is not None else float(k)
    return {"day": day, "segment_key": segment, "metric": metric, "k": k, "n": n,
            "value": None if value is None else float(value), "definition_version": DEFINITION_VERSION,
            "computed_at": now}


def rate(day: date, segment: str, metric: str, k: int, n: int) -> dict:
    return row(day, segment, metric, k=int(k), n=int(n))


def count(day: date, segment: str, metric: str, k: int) -> dict:
    return row(day, segment, metric, k=int(k))


def latest(store, metric: str | Sequence[str] | None = None, segment: str | None = None,
           since: date | None = None) -> list[dict]:
    """The rows of metric (and segment, and from since), one per (day, segment_key, metric): the latest
    definition_version, then the latest computed_at (the module docstring)."""
    where: dict[str, Any] = {}
    if metric is not None:
        where["metric"] = metric if isinstance(metric, str) else list(metric)
    if segment is not None:
        where["segment_key"] = segment
    if since is not None:
        where["day"] = Range(since, None)
    best: dict[tuple, dict] = {}
    for r in store.select(TABLE, where):
        key = (r["day"], r["segment_key"], r["metric"])
        rank = (str(r.get("definition_version") or ""), utc(r.get("computed_at")) or _EPOCH)
        have = best.get(key)
        if have is None or rank > (str(have.get("definition_version") or ""),
                                   utc(have.get("computed_at")) or _EPOCH):
            best[key] = r
    return sorted(best.values(), key=lambda r: (r["day"], r["segment_key"], r["metric"]))


# -- outcomes ------------------------------------------------------------------------------------------------------


def outcome_rows(day: date, segment: str, found: Sequence[Outcome], now: datetime,
                 held: frozenset[str] = frozenset()) -> list[dict]:
    """The outcome metrics over these companies (the module docstring). held: the accounts the hand-check sampled."""
    n = len(found)
    m28 = [o for o in found if o.window_closed_28]
    m45 = [o for o in found if o.window_closed_45]
    reply_end = timedelta(days=outcomes.REPLY_DAYS)

    def negative(o: Outcome) -> bool:
        unsub = o.first_unsubscribed_at is not None and o.first_unsubscribed_at < o.step1_at + reply_end
        return o.negative_in_window or unsub

    out = [count(day, segment, "contacted", n),
           rate(day, segment, "delivered", sum(o.delivered for o in found), n),
           rate(day, segment, "matured_28", len(m28), n),
           rate(day, segment, "matured_45", len(m45), n),
           rate(day, segment, "engaged", sum(o.engaged for o in found), n),
           rate(day, segment, "meeting_in_45", sum(o.meeting_in_45 for o in found), n),
           rate(day, segment, "meeting_any", sum(o.meeting_any for o in found), n),
           rate(day, segment, "meeting_outbound", sum(outbound_booking(o) for o in found), n),
           rate(day, segment, "engaged_rate_45", sum(o.engaged for o in m45), len(m45)),
           rate(day, segment, "positive_rate_28", sum(o.positive_in_window for o in m28), len(m28)),
           rate(day, segment, "human_rate_28", sum(o.replied_in_window for o in m28), len(m28)),
           rate(day, segment, "positive_all", sum(o.positive_in_window for o in found), n),
           rate(day, segment, "human_all", sum(o.replied_in_window for o in found), n),
           rate(day, segment, "negative_rate_28", sum(negative(o) for o in m28), len(m28)),
           rate(day, segment, "explore_share", sum(o.stamp("arm") == "random" for o in found), n),
           rate(day, segment, "mailbox_paused", sum(o.mailbox_paused for o in found), n),
           rate(day, segment, "hand_check_held", sum(o.account_id in held for o in found), n)]
    for d in CURVE_DAYS:
        reached = [o for o in found if now >= o.step1_at + timedelta(days=d)]
        by = timedelta(days=d)
        out.append(rate(day, segment, f"human_by_day_{d}",
                        sum(o.first_reply_at is not None and o.first_reply_at < o.step1_at + by for o in reached),
                        len(reached)))
        out.append(rate(day, segment, f"positive_by_day_{d}",
                        sum(o.first_positive_at is not None and o.first_positive_at < o.step1_at + by for o in reached),
                        len(reached)))
    full = [o for o in found if now >= o.step1_at + timedelta(days=FULL_SEQUENCE_DAYS)]
    out.append(rate(day, segment, "full_sequence", sum(o.steps_sent >= 4 for o in full), len(full)))
    return out


def outbound_booking(o: Outcome) -> bool:
    """O1b: a booking whose recorded HubSpot source is not an ad (the module docstring)."""
    return o.meeting_any and o.meeting_analytics_source not in PAID_SOURCES


def cut_value(o: Outcome, cut: str, states: Mapping[str, str]) -> str | None:
    """The company's value in a cut; None leaves it out of that cut (an unstamped sham arm)."""
    if cut == "arm":
        return o.stamp("arm", "ranked")  # NULL reads as ranked (enrol/queue.py)
    if cut == "sham_arm":  # a contact enrolled before the stamp existed: the same hash (queue.sham_arm)
        return o.stamp("sham_arm") or (queue.sham_arm(o.account_id) if o.account_id else None)
    if cut == "state":
        return states.get(o.account_id) or "unknown"
    key = {"group": "industry_group_at_enrol", "size_band": "size_band_at_enrol", "slice": "sourced_by",
           "source": "email_source", "gate": "gate_version"}.get(cut)
    if key is not None:
        return o.stamp(key, "unknown")
    if cut == "mailbox":
        return o.mailbox or "unknown"
    raise ValueError(f"unknown cut {cut!r}")


def by_cut(found: Sequence[Outcome], cut: str, states: Mapping[str, str]) -> dict[str, list[Outcome]]:
    out: dict[str, list[Outcome]] = defaultdict(list)
    for o in found:
        v = cut_value(o, cut, states)
        if v is not None:
            out[v].append(o)
    return dict(out)


def week_of(t: datetime) -> str:
    return iso_week(t.astimezone(UK).date())


def monday(week: str) -> date:
    year, _, number = week.partition("-W")
    return date.fromisocalendar(int(year), int(number), 1)


def cohorts(found: Iterable[Outcome]) -> dict[str, list[Outcome]]:
    """ISO week of step 1 (UK) -> its companies."""
    out: dict[str, list[Outcome]] = defaultdict(list)
    for o in found:
        out[week_of(o.step1_at)].append(o)
    return dict(out)


def closes(week: str) -> datetime:
    """When the cohort week closes (OPEN_DAYS)."""
    return uk_midnight(monday(week)) + timedelta(days=OPEN_DAYS)


def is_open(week: str, now: datetime) -> bool:
    """A company in it could still change (OPEN_DAYS)."""
    return now < closes(week)


def _final(ctx: Context, week: str) -> bool:
    """The closed week has a row under DEFINITION_VERSION computed after it closed (the module docstring)."""
    rows = ctx.store.select(TABLE, {"segment_key": f"{COHORT}={week}", "metric": "contacted",
                                    "definition_version": DEFINITION_VERSION})
    return any((at := utc(r.get("computed_at"))) is not None and at >= closes(week) for r in rows)


def cohort_weeks(ctx: Context, weeks: Iterable[str]) -> list[str]:
    """The cohort weeks to (re)compute now: every open one, and each of the CLOSED_WEEKS weeks before the oldest open
    one (calendar weeks) with no row under DEFINITION_VERSION computed after it closed (the module docstring)."""
    now = ctx.now
    this = budget.week_start(now).date()
    first_open = this - timedelta(days=7 * (OPEN_DAYS // 7 + 1))
    while not is_open(iso_week(first_open), now):
        first_open += timedelta(days=7)
    recent_closed = {iso_week(first_open - timedelta(days=7 * i)) for i in range(1, CLOSED_WEEKS + 1)}
    out = []
    for w in sorted(set(weeks)):
        if is_open(w, now):
            out.append(w)
        elif w in recent_closed and not _final(ctx, w):
            out.append(w)
    return out


def hand_checked(ctx: Context) -> frozenset[str]:
    """The accounts the hand-check sampled (any week, any decision): held before they were sent (X1)."""
    from us_outbound.enrol import hand_check

    return frozenset(str(e.get("account_id")) for e in ctx.store.select(
        "signal_events", {"source": hand_check.SOURCE, "fact": hand_check.FACT}) if e.get("account_id"))


def states_of(ctx: Context, ids: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(ids), outcomes.ID_CHUNK):
        for a in ctx.store.select("accounts", {"account_id": list(ids[i : i + outcomes.ID_CHUNK])}):
            if a.get("hq_state"):
                out[str(a["account_id"])] = str(a["hq_state"]).upper()
    return out


def outcomes_block(ctx: Context, day: date, found: Sequence[Outcome]) -> tuple[list[dict], list[str]]:
    """(the rows for `all` and each cut on day, and each cohort week to compute; the weeks computed)."""
    now, held = ctx.now, hand_checked(ctx)
    states = states_of(ctx, sorted({o.account_id for o in found}))
    rows = outcome_rows(day, ALL, found, now, held)
    for cut in CUTS:
        for value, group in sorted(by_cut(found, cut, states).items()):
            rows += outcome_rows(day, f"{cut}={value}", group, now, held)
    by_week = cohorts(found)
    weeks = cohort_weeks(ctx, by_week)
    for w in weeks:
        rows += outcome_rows(monday(w), f"{COHORT}={w}", by_week[w], now, held)
    return rows, weeks


# -- the day's process numbers ---------------------------------------------------------------------------------------


def contacted_week(ctx: Context, day: date, found: Sequence[Outcome]) -> list[dict]:
    """P1: companies sent step 1 in day's UK week, up to the end of day, against weekly_enrol_cap."""
    start = uk_midnight(day - timedelta(days=day.weekday()))
    end = uk_midnight(day + timedelta(days=1))
    k = sum(start <= o.step1_at < end for o in found)
    return [rate(day, ALL, "contacted_week", k, ctx.settings.general.weekly_enrol_cap)]


def throughput_block(ctx: Context, day: date) -> list[dict]:
    """P2 over the last 7 days and P3 per mailbox (learn/throughput.report)."""
    from us_outbound.learn import throughput

    r = throughput.report(ctx, weeks=1)
    out = [count(day, ALL, f"lost_leads:{cause}", n) for cause, n in r.lost.items()]
    days = max(1, len(r.days))
    sent = Counter(str(e.get("mailbox") or "").lower() for e in ctx.store.select(
        "events", {"type": "sent", "occurred_at": Range(ctx.now - timedelta(weeks=1), ctx.now)}))
    caps = {m.address.lower(): m.daily_cap for m in ctx.settings.mailboxes}
    for address, use in sorted(r.utilisation.items()):
        out.append(row(day, f"mailbox={address}", "mailbox_utilisation", k=sent[address],
                       n=int(caps.get(address) or 0) * days, value=use))
    return out


def bounce_block(ctx: Context, day: date) -> list[dict]:
    """D1: contacts sent step 1 in the last D1_DAYS and how many bounced at step 1, by email source; D2: each
    mailbox's bounces over its last 100 sends."""
    from us_outbound.learn import daily_post

    sends = ctx.store.select("events", {"type": "sent", "occurred_at": Range(ctx.now - timedelta(days=D1_DAYS), ctx.now)})
    first = {str(e["contact_id"]) for e in sends if e.get("step") in (1, "1") and e.get("contact_id")}
    ids = sorted(first)
    bounced: set[str] = set()
    source: dict[str, str] = {}
    for i in range(0, len(ids), outcomes.ID_CHUNK):
        chunk = ids[i : i + outcomes.ID_CHUNK]
        bounced |= {str(e["contact_id"]) for e in ctx.store.select("events", {"type": "bounced", "contact_id": chunk})
                    if e.get("step") in (1, "1", None)}
        source.update({str(c["contact_id"]): str(c.get("email_source") or "unknown").lower()
                       for c in ctx.store.select("contacts", {"contact_id": chunk})})
    out = [rate(day, ALL, "step1_bounce_contact", len(bounced & first), len(first))]
    by_source: dict[str, set[str]] = defaultdict(set)
    for c in first:
        by_source[source.get(c, "unknown")].add(c)
    for src, cs in sorted(by_source.items()):
        out.append(rate(day, f"source={src}", "step1_bounce_contact", len(bounced & cs), len(cs)))
    for address, (n, b) in sorted(daily_post.mailbox_last_100(ctx).items()):
        out.append(rate(day, f"mailbox={address}", "mailbox_bounce_last_100", b, n))
    return out


def capture_block(ctx: Context, day: date) -> list[dict]:
    """D3: this week's reply-capture check (replies/capture.py's fact within its FACT_FRESH): verified 1 of 1, or 0 of
    1 with the drifting measures counted; 0 of 0 when no check was stored."""
    from us_outbound.replies import capture

    rows = [r for f in (capture.VERIFIED, capture.UNVERIFIED) for r in capture._facts(ctx, f)
            if (at := utc(r.get("observed_at"))) is not None and ctx.now - capture.FACT_FRESH <= at <= ctx.now]
    if not rows:
        return [rate(day, ALL, "capture_verified", 0, 0)]
    latest_fact = max(rows, key=lambda r: (utc(r.get("observed_at")), str(r.get("event_id"))))
    value = latest_fact.get("value") if isinstance(latest_fact.get("value"), Mapping) else {}
    return [rate(day, ALL, "capture_verified", int(latest_fact["fact"] == capture.VERIFIED), 1),
            count(day, ALL, "capture_drift", len(value.get("drift") or ()))]


def precision_block(ctx: Context, day: date) -> list[dict]:
    """T1 pooled over learn/precision's 4 ISO weeks ending with day's (US Eastern date's ISO week as the hand-check
    keys them), by gate version; T2 the leave-out reasons; T1r and T3 from the newest label_policy fact."""
    from us_outbound import labels
    from us_outbound.learn import precision

    t = precision.tally(ctx, through=iso_week(day))
    out = [rate(day, ALL, "icp_precision", *t.fine)]
    out += [rate(day, f"gate={g}", "icp_precision", k, n) for g, (k, n) in sorted(t.by_gate.items())]
    out += [rate(day, ALL, f"leave_out:{reason or 'none given'}", n, t.leave_out)
            for reason, n in sorted(t.reasons.items())]
    fact = labels.policy_fact(ctx)
    sample = (fact["value"].get("sample") or {}) if fact is not None else {}
    judged = int(sample.get("judged") or 0)
    if judged:
        out += [rate(day, ALL, "label_exact", int(sample.get("label_right") or 0), judged),
                rate(day, ALL, "label_group", int(sample.get("group_right") or 0), judged)]
        lc = sample.get("label_copy") or {}
        if lc.get("judged"):
            out.append(rate(day, ALL, "label_copy_wrong_pitch", int(lc.get("wrong_pitch") or 0), int(lc["judged"])))
        if sample.get("rejudged"):
            out.append(rate(day, ALL, "rater_agreement", int(sample.get("rejudge_agreed") or 0),
                            int(sample["rejudged"])))
    return out


def supply_block(ctx: Context, day: date) -> list[dict]:
    """T4 the verify pass rate (first checks, 28 days) and each slice's contactable yield (8 weeks); T5 the
    contact-find yield; T6 contactable supply in send days at the weekly target's pace (daily_post.steady_pace)."""
    from us_outbound.learn import daily_post
    from us_outbound.sources import allocation, apollo_universe

    v, c = apollo_universe.verify_yield(ctx), apollo_universe.contact_yield(ctx)
    out = [rate(day, ALL, "verify_pass_rate", v.k, v.n), rate(day, ALL, "contact_yield", c.k, c.n)]
    for (group, state, band), (n, k) in sorted(allocation.slice_yields(ctx).items()):
        out.append(rate(day, f"slice={group}|{state}|{band}", "slice_yield", k, n))
    supply = apollo_universe.contactable_supply(ctx)
    pace = daily_post.steady_pace(ctx.settings)
    out.append(row(day, ALL, "supply_days", k=round(supply.contactable), n=pace,
                   value=supply.contactable / pace if pace else None))
    return out


def response_block(ctx: Context, day: date) -> list[dict]:
    """O8: positive or referral replies of the last RESPONSE_DAYS (latest class), and how many had a desk reply
    (reply_sent) to the same contact within RESPONSE_HOURS; the median hours of those answered."""
    since = ctx.now - timedelta(days=RESPONSE_DAYS)
    rows = ctx.store.select("events", {"type": ["replied", outcomes.CORRECTED, "reply_sent"]})
    cls = outcomes.classes(rows)
    warm = [e for e in rows if e.get("type") == "replied" and kinds.is_warm(cls.get(str(e["event_id"])))
            and (t := utc(e.get("occurred_at"))) is not None and since <= t <= ctx.now]
    answers: dict[str, list[datetime]] = defaultdict(list)
    for e in rows:
        if e.get("type") == "reply_sent" and (t := utc(e.get("occurred_at"))) is not None:
            answers[str(e.get("contact_id") or e.get("account_id") or "")].append(t)
    hours = []
    for e in warm:
        t = utc(e["occurred_at"])
        after = [a for a in answers.get(str(e.get("contact_id") or e.get("account_id") or ""), ()) if a >= t]
        if after:
            hours.append((min(after) - t).total_seconds() / 3600)
    out = [rate(day, ALL, "positive_response_24h", sum(h <= RESPONSE_HOURS for h in hours), len(warm))]
    if hours:
        out.append(row(day, ALL, "positive_response_median_hours", k=len(hours), value=statistics.median(hours)))
    return out


def cost_block(ctx: Context, day: date, found: Sequence[Outcome]) -> list[dict]:
    """O9: day's UK month's Apollo credits (credit_ledger) and Claude dollars per company sent step 1 that month (the
    month of the row's date, so the 1st's run writes the old month's last day with the old month's spend)."""
    noon = uk_midnight(day) + timedelta(hours=12)
    start, end = budget.month_bounds(noon)
    contacted = sum(start <= o.step1_at < end for o in found)
    credits = budget.spent_in(ctx.store, "apollo", start, end)
    usd = month_spend_usd(ctx.store, noon)
    return [row(day, ALL, "apollo_credits_per_contacted", k=round(credits), n=contacted,
                value=credits / contacted if contacted else None),
            row(day, ALL, "claude_usd_per_contacted", k=round(usd), n=contacted,
                value=usd / contacted if contacted else None)]


def jobs_block(ctx: Context, day: date) -> list[dict]:
    """S1: each scheduled job's runs that started on day (UK) and how many ended in error (0 runs is a row too, so a
    job with no run shows); S2: the per-item failures listed under errors in its runs that finished ok."""
    from us_outbound.ops import schedule

    start, end = uk_midnight(day), uk_midnight(day + timedelta(days=1))
    runs: dict[str, list[dict]] = defaultdict(list)
    for h in ctx.store.select("heartbeats", {"started_at": Range(start, end)}):
        runs[str(h.get("job"))].append(h)
    out = []
    for job in schedule.enabled_names():
        mine = runs.get(job, [])
        errors = sum(h.get("status") == "error" for h in mine)
        items = sum(len(d.get("errors") or ()) for h in mine if h.get("status") == "ok"
                    and isinstance(d := h.get("detail"), Mapping) and isinstance(d.get("errors"), list))
        out += [rate(day, f"job={job}", "job_runs", len(mine) - errors, len(mine)),
                count(day, f"job={job}", "job_errors", errors), count(day, f"job={job}", "item_failures", items)]
    return out


def spend_block(ctx: Context, day: date) -> list[dict]:
    """S3: the Apollo balance's fall between the newest kept reading and the newest one at least 7 days before it,
    against what credit_ledger recorded between them (learn/spend.py keeps a reading a day)."""
    from us_outbound.learn import spend

    kept = spend.readings(ctx)
    if not kept:
        return []
    t1, v1 = kept[-1]
    earlier = [(t, v) for t, v in kept if t <= t1 - timedelta(days=spend.PACE_DAYS_BALANCE) + timedelta(hours=1)]
    if not earlier:
        return []
    t0, v0 = earlier[-1]
    fell = v0 - v1
    ledger = budget.spent_in(ctx.store, "apollo", t0, t1)
    return [row(day, ALL, "spend_drift", k=round(ledger), n=round(fell),
                value=(fell - ledger) / fell if fell > 0 else None)]


# -- forecasts (F1, L13) -----------------------------------------------------------------------------------------------


def _weekly_contacted(found: Sequence[Outcome]) -> Counter[str]:
    return Counter(week_of(o.step1_at) for o in found)


def predict(ctx: Context, week: str, found: Sequence[Outcome]) -> dict[str, tuple[float, float, float, int]]:
    """name -> (point, low, high, basis n) for the week, from data before its Monday only (the module docstring)."""
    from us_outbound.learn import precision

    start = uk_midnight(monday(week))
    before = [o for o in found if o.step1_at < start]
    out: dict[str, tuple[float, float, float, int]] = {}
    per_week = _weekly_contacted(before)
    if per_week:
        first = monday(min(per_week))
        weeks = [iso_week(monday(week) - timedelta(days=7 * i)) for i in range(1, FORECAST_WEEKS + 1)]
        weeks = [w for w in weeks if monday(w) >= first]
        point = sum(per_week[w] for w in weeks) / len(weeks)
        out["contacted"] = (point, round(point * (1 - FORECAST_BAND)), round(point * (1 + FORECAST_BAND)), len(weeks))
    t = precision.tally(ctx, through=iso_week(monday(week) - timedelta(days=7)))
    k, n = t.fine
    if n:
        low, high = stats.wilson(k, n)
        out["icp_precision"] = (k / n, low, high, n)
    maturing = [o for o in found if start - timedelta(days=28) <= o.step1_at < start - timedelta(days=21)]
    if maturing:
        matured = [o for o in before if o.step1_at + timedelta(days=outcomes.REPLY_DAYS) <= start]
        p = (sum(o.positive_in_window for o in matured) / len(matured)
             if len(matured) >= outcomes.MIN_MATURED else PRIOR_POSITIVE)
        m = len(maturing)
        tail = (1 - FORECAST_LEVEL) / 2
        out["positive_maturing"] = (m * p, stats.binomial_quantile(tail, m, p),
                                    stats.binomial_quantile(1 - tail, m, p), m)
    return out


def actual(ctx: Context, name: str, week: str, found: Sequence[Outcome]) -> float | None:
    """What the forecast for week came to, once the week has ended."""
    from us_outbound.learn import precision

    if name == "contacted":
        return float(_weekly_contacted(found)[week])
    if name == "icp_precision":
        k, n = precision.tally(ctx, through=week).fine
        return k / n if n else None
    if name == "positive_maturing":
        start = uk_midnight(monday(week))
        return float(sum(o.positive_in_window for o in found
                         if start - timedelta(days=28) <= o.step1_at < start - timedelta(days=21)))
    raise ValueError(name)


def forecast_block(ctx: Context, found: Sequence[Outcome]) -> tuple[list[dict], list[str], list[str]]:
    """(rows, the forecasts written, the forecasts scored): this week's forecasts if none is stored yet; each
    forecast of a week that has ended and has no score yet, scored."""
    now = ctx.now
    this = week_of(now)
    out, wrote, scored = [], [], []
    have = {(r["day"], r["metric"]) for r in latest(ctx.store, [f"forecast:{f}" for f in FORECASTS]
                                                    + [f"forecast_score:{f}" for f in FORECASTS])}
    day = monday(this)
    for name, (point, low, high, basis) in predict(ctx, this, found).items():
        if (day, f"forecast:{name}") in have:
            continue
        out += [row(day, ALL, f"forecast:{name}", n=basis, value=point),
                row(day, ALL, f"forecast:{name}:low", value=low), row(day, ALL, f"forecast:{name}:high", value=high)]
        wrote.append(name)
    stored = {(r["day"], r["metric"]): r for r in latest(ctx.store, [f"forecast:{f}{s}" for f in FORECASTS
                                                                      for s in ("", ":low", ":high")])}
    for (d, metric), r in sorted(stored.items()):
        name = metric.split(":")[1]
        if metric.count(":") != 1 or d >= day or (d, f"forecast_score:{name}") in have:
            continue
        got = actual(ctx, name, iso_week(d), found)
        low, high = stored.get((d, f"{metric}:low")), stored.get((d, f"{metric}:high"))
        if got is None or low is None or high is None:
            continue
        inside = low["value"] <= got <= high["value"]
        out.append(row(d, ALL, f"forecast_score:{name}", k=int(inside), n=1, value=got))
        scored.append(f"{name} {iso_week(d)}")
    return out, wrote, scored


def scorecard(ctx: Context, found: Sequence[Outcome] | None = None) -> list[dict]:
    """Last week's forecasts with what they came to, for the readout (which runs before metrics_daily scores them):
    [{name, point, low, high, actual, inside, outside_weeks}], outside_weeks counting the weeks running it was outside
    its interval, last week included."""
    found = outcomes.per_company(ctx) if found is None else found
    last = monday(week_of(ctx.now)) - timedelta(days=7)
    stored = {(r["day"], r["metric"]): r for r in latest(ctx.store, [f"forecast:{f}{s}" for f in FORECASTS
                                                                      for s in ("", ":low", ":high")])}
    scores = {(r["day"], r["metric"]): r for r in latest(ctx.store, [f"forecast_score:{f}" for f in FORECASTS])}
    out = []
    for name in FORECASTS:
        point = stored.get((last, f"forecast:{name}"))
        low, high = stored.get((last, f"forecast:{name}:low")), stored.get((last, f"forecast:{name}:high"))
        if point is None or low is None or high is None:
            continue
        got = actual(ctx, name, iso_week(last), found)
        inside = got is not None and low["value"] <= got <= high["value"]
        run = 0 if inside else 1
        d = last - timedelta(days=7)
        while not inside and (s := scores.get((d, f"forecast_score:{name}"))) is not None and not s.get("k"):
            run += 1
            d -= timedelta(days=7)
        out.append({"name": name, "point": point["value"], "low": low["value"], "high": high["value"],
                    "actual": got, "inside": inside, "outside_weeks": run})
    return out


def flags(card: Sequence[Mapping[str, Any]]) -> list[str]:
    """The forecasts outside their interval FLAG_WEEKS weeks running (L13): a definition or model check."""
    return [c["name"] for c in card if c["outside_weeks"] >= FLAG_WEEKS]


# -- the job -------------------------------------------------------------------------------------------------------


def run(ctx: Context) -> dict:
    """The metrics_daily job (JOB CONTRACT: run(ctx) -> summary): yesterday's rows, the open cohorts' (and, after a
    version bump, the recent closed ones'), the forecasts; each block in its own try."""
    day = ctx.today_uk() - timedelta(days=1)
    errors: list[str] = []
    rows: list[dict] = []
    weeks: list[str] = []
    wrote: list[str] = []
    scored: list[str] = []
    found: list[Outcome] = []

    def guarded(name: str, make: Callable[[], list[dict]]) -> None:
        try:
            rows.extend(make())
        except GuardViolation:
            raise
        except Exception as exc:
            errors.append(f"metrics_daily {name}: {type(exc).__name__}: {str(exc)[:160]}")
            log("metrics_block_failed", run_id=ctx.run_id, block=name, error=f"{type(exc).__name__}: {exc}"[:200])

    try:
        found = outcomes.per_company(ctx)
    except GuardViolation:
        raise
    except Exception as exc:
        errors.append(f"metrics_daily outcomes: {type(exc).__name__}: {str(exc)[:160]}")
    if found or not errors:
        def outcome_part() -> list[dict]:
            got, w = outcomes_block(ctx, day, found)
            weeks.extend(w)
            return got

        def forecast_part() -> list[dict]:
            got, w, s = forecast_block(ctx, found)
            wrote.extend(w)
            scored.extend(s)
            return got

        guarded("outcomes", outcome_part)
        guarded("contacted_week", lambda: contacted_week(ctx, day, found))
        guarded("cost", lambda: cost_block(ctx, day, found))
        guarded("forecasts", forecast_part)
    for name, block in (("throughput", throughput_block), ("bounces", bounce_block), ("capture", capture_block),
                        ("precision", precision_block), ("supply", supply_block), ("responses", response_block),
                        ("jobs", jobs_block), ("spend", spend_block)):
        guarded(name, lambda block=block: block(ctx, day))
    for r in rows:
        r["computed_at"] = ctx.now
    if rows:
        ctx.store.upsert(TABLE, rows)
    warned: dict[str, Any] = {}
    if found:  # the early warning (W3-4, learn/early_warning.py): after the series, in its own try
        try:
            from us_outbound.learn import early_warning

            warned = early_warning.check(ctx, found)
        except GuardViolation:
            raise
        except Exception as exc:
            errors.append(f"metrics_daily early_warning: {type(exc).__name__}: {str(exc)[:160]}")
            log("metrics_block_failed", run_id=ctx.run_id, block="early_warning", error=f"{type(exc).__name__}: {exc}"[:200])
    ids = sorted({i for r in rows if (i := id_of(r["metric"]))})
    summary = {"job": JOB, "dry_run": ctx.dry_run, "day": day.isoformat(), "definition_version": DEFINITION_VERSION,
               "rows": len(rows), "ids": ids, "cohorts": weeks, "forecasts": wrote, "scored": scored, "errors": errors,
               "early_warning": warned}
    log("metrics_daily_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "ids"})
    return summary

