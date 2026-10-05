"""Heartbeats (SPEC 9, 13 Health): every job writes one row per run; a missed heartbeat alerts in Slack.

run_job(ctx, fn) wraps a job function run(ctx) -> dict:
  * inserts the run's heartbeats row with status "running" (upserted by run_id);
  * skips the run (status "skipped") if another run of the same scheduled job is still
    going, so a slow poll never overlaps the next one (operator commands are never skipped);
  * runs the job, then upserts the row with status ok, error or skipped, finished_at,
    the job's summary as detail, and the error text (clipped, emails hashed).
A job reports a skip by returning {"skipped": True, "reason": ...} or raising Skip.
Errors are re-raised after the row is written, so the CLI exits non-zero.

check_heartbeats(ctx) is the heartbeat_check job (hourly, beside kill_rules). A job is
missed when it last showed it is alive longer ago than EXPECTED[job]; weekday jobs count
only weekday time (UK), so a weekend is not a miss. Alive means a run that finished ok,
or was skipped for a reason of its own (a blackout date). A "running" row is no evidence:
a run killed by its task timeout stays "running" for ever, and the runs skipped behind it
("previous run still going") are no evidence either. The one exception is a job's only
run so far, still within OVERLAP_MINUTES of its start.
It posts one Slack message when a job is newly missed, and repeats the list at the
09:00 UK check each day while anything is still missed.

The stop and start commands also write rows here (jobs operator_stop / operator_start):
enrolment_paused() reads them; enrol.operator_pause() checks it before enrolling.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import UK, Context
from us_outbound.logs import clip, log, redact

TABLE = "heartbeats"
RUNNING, OK, ERROR, SKIPPED = "running", "ok", "error", "skipped"
ERROR_LIMIT = 1000  # characters of error text kept on the row
OVERLAP_MINUTES = 60  # a "running" row younger than this blocks a second run (scheduler timeouts are <= 60 min)
OVERLAP_REASON = "previous run still going"  # a skip behind a running (or dead) run: no sign of life
NEW_MISS_WINDOW = 75  # minutes: a job overdue by less than this at an hourly check is "newly missed"
REMINDER_HOUR_UK = 9  # the daily repeat of the missed list, beside the daily post

_H = 60
_DAY = 24 * _H
# Longest gap between healthy runs before a job counts as missed (SPEC 9 schedules plus slack).
EXPECTED: dict[str, int] = {
    "settings_sync": 26 * _H,  # 02:00 daily (and 11:30 on weekdays; the 02:00 run alone keeps it healthy)
    "source_universe": 26 * _H,  # 03:00 weekdays (weekday time; SPEC 9 had the 1st of the month)
    "apollo_signals": 26 * _H,  # 03:30 weekdays (weekday time; SPEC 9 had Mondays)
    "read_pages": 26 * _H,  # 03:45 weekdays (weekday time; build addition, Harry, 2 Oct 2026)
    "apollo_enrich": 26 * _H,  # 04:10 weekdays (weekday time; build addition, Harry, 2 Oct 2026)
    "apollo_people": 26 * _H,  # 04:20 weekdays (weekday time; build addition, Harry, 5 Oct 2026)
    "site_visits": 26 * _H,  # 06:00 daily
    "public_signals": 8 * _DAY,  # Mon 04:00
    "verify_in_clay": 26 * _H,  # 04:30 weekdays (weekday time)
    "verify_accounts": 26 * _H,  # 04:30 weekdays (weekday time; build addition)
    "pick_contacts": 26 * _H,  # 05:30 weekdays (weekday time)
    "enrol": 26 * _H,  # 12:00 weekdays (weekday time)
    "poll_replies": 45,  # every 15 min
    "poll_approvals": 20,  # every 5 min
    "hubspot_readback": 45,  # every 15 min
    "sync_outcomes": 45,  # every 15 min (SPEC 9 had 01:00 daily)
    "mailbox_health": 26 * _H,  # 07:00 daily
    "kill_rules": 150,  # hourly
    "heartbeat_check": 150,  # hourly (nothing watches this one; the daily post reports it)
    "daily_post": 26 * _H,  # 09:00 daily
    "monday_readout": 8 * _DAY,  # Mon 09:00
    "suppression_load": 26 * _H,  # 01:30 daily (build addition)
    "lookalikes": 8 * _DAY,  # Mon 02:30 (build addition)
    # The 1st of each month, 02:50 (build addition, Harry, 5 Oct 2026): at most 31 days apart, plus a day's slack,
    # so a monthly job is never "missed" between its runs.
    "lookalike_leads": 32 * _DAY,
    "hand_check_post": 8 * _DAY,  # Mon 08:00 (build addition, SPEC 11 weekly hand-check)
}
# score has no schedule of its own: it runs inside settings_sync, verify_in_clay, verify_accounts and site_visits.
WEEKDAY_JOBS = frozenset({"source_universe", "apollo_signals", "read_pages", "apollo_enrich", "apollo_people",
                          "verify_in_clay", "verify_accounts", "pick_contacts", "enrol"})
OPERATOR_STOP, OPERATOR_START = "operator_stop", "operator_start"

LATEST_SQL = (
    "SELECT job, run_id, status, started_at, finished_at, error, last_ok_at, last_alive_at, runs"
    " FROM {schema}.v_heartbeats"
)


class Skip(Exception):
    """Raised by a job to record its run as skipped (e.g. a blackout date), not as an error."""


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _still_running(store: Store, job: str, run_id: str, now: datetime) -> dict | None:
    for r in store.select(TABLE, {"job": job, "status": RUNNING}):
        started = _ts(r.get("started_at"))
        if r.get("run_id") != run_id and started and now - started < timedelta(minutes=OVERLAP_MINUTES):
            return r
    return None


def run_job(ctx: Context, fn: Callable[[Context], Any]) -> dict:
    """Run fn(ctx) with a heartbeat row around it; returns the job's summary."""
    store, job, now = ctx.store, ctx.job, ctx.now
    row = {"run_id": ctx.run_id, "job": job, "started_at": now, "dry_run": ctx.dry_run}
    # Only scheduled jobs are locked: an operator command (stop above all) always runs.
    other = _still_running(store, job, ctx.run_id, now) if job in EXPECTED else None
    if other is not None:
        detail = {"skipped": True, "reason": OVERLAP_REASON, "other_run_id": other.get("run_id")}
        store.upsert(TABLE, [{**row, "finished_at": now, "status": SKIPPED, "detail": detail, "error": None}])
        log("job_skipped", job=job, run_id=ctx.run_id, reason=detail["reason"])
        return detail
    store.upsert(TABLE, [{**row, "finished_at": None, "status": RUNNING, "detail": None, "error": None}])
    log("job_start", job=job, run_id=ctx.run_id, dry_run=ctx.dry_run)

    def finish(status: str, detail: Any, error: str | None) -> None:
        finished = max(datetime.now(UTC), now)  # ctx.now is the start; tests pin it in the future
        store.upsert(TABLE, [{**row, "finished_at": finished, "status": status, "detail": detail, "error": error}])
        log("job_end", job=job, run_id=ctx.run_id, status=status, dry_run=ctx.dry_run, error=error)

    try:
        result = fn(ctx)
    except Skip as exc:
        detail = {"skipped": True, "reason": str(exc)}
        finish(SKIPPED, detail, None)
        return detail
    except BaseException as exc:
        text = clip(redact(f"{type(exc).__name__}: {exc}"), ERROR_LIMIT)
        finish(ERROR, {"violation": True} if isinstance(exc, GuardViolation) else None, text)
        raise
    summary = result if isinstance(result, Mapping) else {"result": result}
    summary = _json_safe(summary)
    finish(SKIPPED if summary.get("skipped") else OK, summary, None)
    return summary


# -- the heartbeat check ---------------------------------------------------------


def _shows_alive(row: Mapping[str, Any]) -> bool:
    """An ok run, or a skip for a reason other than an overlap (see the module docstring)."""
    if row.get("status") == OK:
        return True
    if row.get("status") != SKIPPED:
        return False
    detail = row.get("detail")
    reason = detail.get("reason") if isinstance(detail, Mapping) else None
    return reason != OVERLAP_REASON


def _latest_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """What v_heartbeats returns, computed in Python (MemoryStore has no views)."""
    latest: dict[str, dict] = {}
    last_ok: dict[str, datetime] = {}
    last_alive: dict[str, datetime] = {}
    runs: dict[str, int] = {}
    for r in rows:
        job = r.get("job")
        if not job:
            continue
        runs[job] = runs.get(job, 0) + 1
        started = _ts(r.get("started_at"))
        old = latest.get(job)
        if old is None or (started, str(r.get("run_id"))) > (_ts(old.get("started_at")), str(old.get("run_id"))):
            latest[job] = dict(r)
        at = _ts(r.get("finished_at")) or started
        if at and r.get("status") == OK and (job not in last_ok or at > last_ok[job]):
            last_ok[job] = at
        if at and _shows_alive(r) and (job not in last_alive or at > last_alive[job]):
            last_alive[job] = at
    return [
        {**r, "last_ok_at": last_ok.get(job), "last_alive_at": last_alive.get(job), "runs": runs[job]}
        for job, r in latest.items()
    ]


def latest_runs(store: Store) -> dict[str, dict]:
    """job -> its latest heartbeat plus last_ok_at, from the v_heartbeats view."""
    try:
        rows = store.query(LATEST_SQL.format(schema=store.schema))
    except NotImplementedError:  # MemoryStore without a view handler
        rows = _latest_from_rows(store.select(TABLE))
    return {r["job"]: dict(r) for r in rows if r.get("job")}


def _weekday_minutes(start: datetime, end: datetime) -> float:
    """Minutes between start and end, leaving out Saturdays and Sundays (UK dates)."""
    if end <= start:
        return 0.0
    total = 0.0
    cur = start.astimezone(UK)
    stop = end.astimezone(UK)
    while cur < stop:
        midnight = datetime(cur.year, cur.month, cur.day, tzinfo=UK) + timedelta(days=1)
        nxt = min(midnight, stop)
        if cur.weekday() < 5:
            total += (nxt - cur).total_seconds() / 60
        cur = nxt
    return total


def _last_healthy(run: Mapping[str, Any], now: datetime) -> datetime | None:
    """When the job last showed it is alive (last_alive_at), or when its only run started if still going."""
    times = [t for t in (_ts(run.get("last_alive_at")), _ts(run.get("last_ok_at"))) if t is not None]
    if times:
        return max(times)
    started = _ts(run.get("started_at"))
    first = (run.get("runs") or 1) <= 1
    if first and run.get("status") == RUNNING and started and now - started < timedelta(minutes=OVERLAP_MINUTES):
        return started  # the job's first run, still going
    return None


def overdue_minutes(job: str, run: Mapping[str, Any] | None, now: datetime) -> float | None:
    """How far past EXPECTED[job] the job is (positive = missed); None if it never ran.

    A job that has runs but never a healthy one is missed without end (inf), so it is
    alerted in the daily reminder rather than as newly missed.
    """
    if not run:
        return None
    last = _last_healthy(run, now)
    if last is None:
        return float("inf")
    elapsed = _weekday_minutes(last, now) if job in WEEKDAY_JOBS else (now - last).total_seconds() / 60
    return elapsed - EXPECTED[job]


def scheduled_jobs() -> list[str]:
    """The built jobs that ops/schedule.py runs (a job only run by hand is never expected)."""
    from us_outbound.ops.cli import built_jobs
    from us_outbound.ops.schedule import enabled_names

    enabled = set(enabled_names())
    return [j for j in built_jobs() if j in EXPECTED and j in enabled]


def _describe(job: str, run: Mapping[str, Any], now: datetime) -> str:
    last = _last_healthy(run, now)
    when = f"last healthy run {last.astimezone(UK):%a %d %b %H:%M} UK" if last else "no healthy run on record"
    line = f"• {job}: {when} (expected at least every {_span(EXPECTED[job])})"
    if run.get("status") == ERROR and run.get("error"):
        line += f"; latest run failed: {clip(str(run['error']), 200)}"
    return line


def _span(minutes: int) -> str:
    if minutes % _DAY == 0:
        return f"{minutes // _DAY} days"
    if minutes >= 2 * _H:
        return f"{minutes / _H:g} hours"
    return f"{minutes} minutes"


def check_heartbeats(ctx: Context, jobs: Iterable[str] | None = None) -> list[str]:
    """The heartbeat_check job: the jobs whose heartbeat is missed; alerts Slack when one is newly missed.

    A job with no heartbeat row at all is not flagged (it has not been deployed or run yet);
    `us-outbound status` lists it as never run.
    """
    now = ctx.now
    wanted = list(jobs) if jobs is not None else scheduled_jobs()
    runs = latest_runs(ctx.store)
    missed: list[str] = []
    new: list[str] = []
    for job in wanted:
        run = runs.get(job)
        if not run or job not in EXPECTED:
            continue
        over = overdue_minutes(job, run, now)
        if over is not None and over > 0:
            missed.append(job)
            if over <= NEW_MISS_WINDOW:
                new.append(job)
    reminder = bool(missed) and now.astimezone(UK).hour == REMINDER_HOUR_UK
    if new or reminder:
        lines = ["Missed heartbeats: these US Outbound jobs have not run when they should have."]
        lines += [_describe(j, runs[j], now) for j in missed]
        lines.append("Check the worker's logs in Railway, or run `us-outbound status`.")
        ctx.clients.slack.post(ctx.settings.general.alert_channel, "\n".join(lines))
    log("heartbeat_check", missed=missed, newly_missed=new, alerted=bool(new or reminder))
    return missed


# -- operator stop / start (the enrolment pause) ------------------------------------


def enrolment_paused(store: Store) -> dict | None:
    """The operator_stop row in force, or None. Enrol (phase 2) must not enrol while this is set.

    stop counts in any mode and whatever its outcome (the safe direction); start counts
    only when it ran live and finished ok.
    """
    rows = store.select(TABLE, {"job": [OPERATOR_STOP, OPERATOR_START]})
    stops = [r for r in rows if r.get("job") == OPERATOR_STOP]
    starts = [r for r in rows if r.get("job") == OPERATOR_START and r.get("status") == OK and r.get("dry_run") is False]
    if not stops:
        return None
    last_stop = max(stops, key=lambda r: _ts(r.get("started_at")) or datetime.min.replace(tzinfo=UTC))
    if starts:
        last_start = max(_ts(r.get("started_at")) or datetime.min.replace(tzinfo=UTC) for r in starts)
        if last_start > (_ts(last_stop.get("started_at")) or datetime.min.replace(tzinfo=UTC)):
            return None
    return last_stop
