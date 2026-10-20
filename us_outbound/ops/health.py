"""Job health (SPEC 9, 13 Health), in one module (W2-4, 20 Oct 2026): what a run records, what heartbeat_check finds,
and the outside watchdog. It merges ops/heartbeat.py, base/heartbeats.py, ops/job_errors.py and ops/watchdog.py,
about 920 lines for one question: is every job running, and running well? (The watchdog's guarded HTTP pinger stays
in the client layer, clients/watchdog.py.) This is the only module that writes heartbeats rows (run_job,
record_failed_run, record_unusable).

THE RUN (run_job). run_job(ctx, fn) wraps a job function run(ctx) -> summary:
  * a scheduled start carries its due minute (ctx.due, `us-outbound run <job> --due`): its row's run_id is
    "<job>:<due minute>" and it is inserted only if no row has that id (Store.claim), so a second start for the same
    minute (two schedulers over a deploy, or the scheduler's catch-up) records nothing and runs nothing;
  * a run of a scheduled job is skipped ("previous run still going") while another run of it is still "running"
    and younger than the job's own timeout (ops/schedule.py; 60 minutes for a job with none). A row left "running"
    by a run killed at its timeout lifts after that timeout, not after 60 minutes (code-platform-ops B9: a
    4-minute poll_approvals held its ✅ for an hour). Operator commands are never skipped;
  * the job's result is stored with status ok, error or skipped, finished_at, duration_s, exit_code, the summary as
    detail and the error text (clipped, emails hashed). The result is a JobResult(status, reason, counts, errors,
    metrics), or a dict summary wrapped with the W1-1 status rule (summary_status): {"skipped": True} or
    {"status": "skipped"} is a skip, any other summary is ok; a count under "skipped" (enrol's accounts passed over)
    is no skip. The row's detail is the summary with duration_s added, and settings_fallback when the run started on
    the last-good settings (ops/bootstrap.py SAFETY_JOBS). A summary may carry the per-item contract: failures
    {reason: count} for items that failed inside a run that still finished, and candidates, how many it tried.
  * an exception is recorded as an error (exit_code 1; 3 for a guardrail, 143 for SIGTERM) and re-raised, so the
    command line exits non-zero. A JobResult with status error is recorded as an error and returned.
The stop and start commands also write rows here (jobs operator_stop / operator_start): enrolment_paused() reads
them; enrol.operator_pause() checks it before enrolling.

THE CHECK (heartbeat_check, hourly; run = check). check(ctx) is: missed jobs (check_heartbeats), daily jobs whose
latest run failed (failed_jobs), the errors the jobs caught and carried on past (report_errors, with the failure-rate
rule), whether Slack can take alerts (slack_problem), then the outside watchdog's ping, /fail when any of these needs
a look. When the settings are unusable heartbeat_check starts on the General defaults (ops/bootstrap.py), so it
still alerts and pings.

Missed: a job is missed when it last showed it is alive longer ago than EXPECTED[job]; weekday jobs count only
weekday time (UK), so a weekend is not a miss. Alive is a run that finished ok, or skipped for a reason of its own (a
blackout date). A "running" row is no evidence, nor a skip behind one, except a job's only run so far, still within
OVERLAP_MINUTES of its start. One Slack message when a job is newly missed, repeated at the 09:00 UK check each day
while anything is still missed (Harry, 12 Oct 2026: clear to a newcomer; base/say.py): the jobs in plain words, what
has stopped happening (IMPACT), and the fix. When the jobs refuse to start because the settings are unusable, that
cause is said once, with a link to each failing tab and its first errors (settings_fault).

Caught errors (Harry, 7 Oct 2026). A job that meets a failure it can live with records it and finishes ok; its
heartbeat keeps it and nobody was told. report_errors reads each scheduled job's latest finished run in the last
LOOKBACK and posts what it finds, with the approvers mentioned:
  * failed      the run raised: a job that runs daily or less often (its next try is hours away), or any job when
                the error is a rejected key. A frequent job is tried again within minutes, and one that keeps failing
                is told as missed, with its error;
  * errors      a non-empty list under "errors" or a key ending "_errors", anywhere in the detail;
  * <key>       a text under a key ending "_error", or under a key named error (not a post's own result, not a dry
                run's note);
  * failures    the per-item contract: one reason counting 3 items or more and at least half the candidates;
  * <key>       a count above 0 under a key ending "_errors" (poll_replies' slack_errors);
  * claude_cap  claude_cap_reached above 0: replies were classified "other" with no draft;
  * slack_post  a post the job could not make (a {"posted": false, "error": ...} result);
  * unusable    the settings are unusable, so jobs refuse to start (record_unusable): one line for all;
  * failure_rate a frequent job whose last RATE_RUNS runs hold RATE_ERRORS or more errors (code-platform-ops B13).
A rejected key (HTTP 401 or 403, Slack's invalid_auth and kin, Anthropic's authentication_error, Google's refused
service-account key) is told apart: "<System> turned down our key", and where to put a new one. Each (job, kind) is
posted at most once for each UK day a run of it began, and each rejected key once a day (notify.post_once). A service's
own outage (HTTP 5xx or 429, a timeout) met once by a frequent job is posted only when the run before met it too.

THE OUTSIDE WATCHDOG (Harry, 7 Oct 2026). Every Slack alert comes from inside the Railway worker, so a dead worker,
database or Slack token says nothing. heartbeat_check therefore ends each run with a GET of US_OUTBOUND_WATCHDOG_URL,
a Healthchecks.io check (period 1 hour, grace 1 hour, emailing Harry; docs/railway-setup.md "h"): <url> when all is
well; <url>/fail when a job is missed, a daily job failed, or Slack cannot take alerts; nothing when heartbeat_check
cannot run, and Healthchecks emails after its grace. The scheduler pings /fail itself when it cannot hold its lock or
reach Postgres (ops/scheduler.py). The URL is a secret: never logged; the guard allows a GET of exactly it or its /fail
form (Guard._check_watchdog), in dry-run too. A ping that fails is logged and never fails the job. The pinger itself,
a guarded HTTP client, stays in the client layer (clients/watchdog.py), where the guardrail acceptance test finds
every client by introspection and only clients talk to the network.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import fmt
from us_outbound.base import notify, say
from us_outbound.clients.db import Range, Store, new_id
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.clients.watchdog import PingFailed, Watchdog
from us_outbound.context import SECRET_NAMES, UK, ConfigError, Context, Secrets
from us_outbound.logs import clip, log, redact
from us_outbound.timeparse import utc, utc_strict_or_none

JOB = "heartbeat_check"
TABLE = "heartbeats"
RUNNING, OK, ERROR, SKIPPED = "running", "ok", "error", "skipped"
STATUSES = (OK, ERROR, SKIPPED)
OVERLAP_REASON = "previous run still going"  # a skip behind a running (or dead) run: no sign of life
OPERATOR_STOP, OPERATOR_START = "operator_stop", "operator_start"
ERROR_LIMIT = 1000  # characters of error text kept on the row
OVERLAP_MINUTES = 60  # the lock age of a job with no timeout of its own, and a first run's grace in check_heartbeats
NEW_MISS_WINDOW = 75  # minutes: a job overdue by less than this at an hourly check is "newly missed"
REMINDER_HOUR_UK = 9  # the daily repeat of the missed list, beside the daily post
EXIT_GUARD, EXIT_SIGTERM, EXIT_ERROR = 3, 143, 1  # ops/cli.py's exit codes, stored on the row


class Skip(Exception):
    """Raised by a job to record its run as skipped (e.g. a blackout date), not as an error."""


class Terminated(BaseException):
    """SIGTERM reached a running job: the scheduler's timeout, or a redeploy (exit code 143).

    A BaseException, so no `except Exception` inside a job swallows it. run_job records the run as an error, so its
    heartbeat is not left "running" to block the next run.
    """


@dataclass
class JobResult:
    """What a job reports (code-platform-ops §6.1): status ok, skipped or error; why; and what it counted, the errors
    it carried on past and its measures. run_job stores it as the row's status and detail."""

    status: str = OK
    reason: str = ""
    counts: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        if self.status not in STATUSES:
            raise ValueError(f"a JobResult's status is one of {', '.join(STATUSES)}, not {self.status!r}")
        return {"status": self.status, "reason": self.reason, "counts": dict(self.counts),
                "errors": list(self.errors), "metrics": dict(self.metrics)}


# -- the read side: each job's latest run, and the operator stop ---------------------------------------------------

LATEST_SQL = (
    "SELECT job, run_id, status, started_at, finished_at, error, last_ok_at, last_alive_at, runs"
    " FROM {schema}.v_heartbeats"
)


def _shows_alive(row: Mapping[str, Any]) -> bool:
    """An ok run, or a skip for a reason other than an overlap (the module docstring)."""
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
        started = utc_strict_or_none(r.get("started_at"))
        old = latest.get(job)
        if old is None or ((started, str(r.get("run_id")))
                           > (utc_strict_or_none(old.get("started_at")), str(old.get("run_id")))):
            latest[job] = dict(r)
        at = utc_strict_or_none(r.get("finished_at")) or started
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


def enrolment_paused(store: Store) -> dict | None:
    """The operator_stop row in force, or None. Enrol must not enrol while this is set.

    stop counts in any mode and whatever its outcome (the safe direction); start counts only when it ran live and
    finished ok.
    """
    rows = store.select(TABLE, {"job": [OPERATOR_STOP, OPERATOR_START]})
    stops = [r for r in rows if r.get("job") == OPERATOR_STOP]
    starts = [r for r in rows if r.get("job") == OPERATOR_START and r.get("status") == OK and r.get("dry_run") is False]
    if not stops:
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    last_stop = max(stops, key=lambda r: utc_strict_or_none(r.get("started_at")) or floor)
    if starts:
        last_start = max(utc_strict_or_none(r.get("started_at")) or floor for r in starts)
        if last_start > (utc_strict_or_none(last_stop.get("started_at")) or floor):
            return None
    return last_stop


# -- the run --------------------------------------------------------------------------------------------------------


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


def lock_minutes(job: str) -> int:
    """How long a "running" row of the job blocks another run: its timeout in ops/schedule.py (the scheduler stops
    the run then), else OVERLAP_MINUTES."""
    from us_outbound.ops.schedule import by_name

    found = by_name().get(job)
    return found.timeout_minutes if found is not None and found.timeout_minutes > 0 else OVERLAP_MINUTES


def due_run_id(job: str, due: datetime) -> str:
    """The claim id of a scheduled start: "<job>:<due minute, UTC>", e.g. "enrol:2026-10-20T11:00"."""
    return f"{job}:{due.astimezone(UTC):%Y-%m-%dT%H:%M}"


def _still_running(store: Store, job: str, run_id: str, now: datetime) -> dict | None:
    age = timedelta(minutes=lock_minutes(job))
    for r in store.select(TABLE, {"job": job, "status": RUNNING}):
        started = utc_strict_or_none(r.get("started_at"))
        if r.get("run_id") != run_id and started and now - started < age:
            return r
    return None


def exit_code_of(exc: BaseException) -> int:
    if isinstance(exc, GuardViolation):
        return EXIT_GUARD
    if isinstance(exc, Terminated):
        return EXIT_SIGTERM
    return EXIT_ERROR


def run_job(ctx: Context, fn: Callable[[Context], Any]) -> dict:
    """Run fn(ctx) with a heartbeat row around it; returns the job's summary (the module docstring)."""
    store, job, now = ctx.store, ctx.job, ctx.now
    row = {"run_id": ctx.run_id, "job": job, "started_at": now, "dry_run": ctx.dry_run}
    running = {**row, "finished_at": None, "status": RUNNING, "detail": None, "error": None}
    due = getattr(ctx, "due", None)
    if due is not None and not store.claim(TABLE, running):
        detail = {"skipped": True, "reason": OVERLAP_REASON, "claimed": ctx.run_id}
        log("job_skipped", job=job, run_id=ctx.run_id, reason="this due minute was started already")
        return detail
    # Only scheduled jobs are locked: an operator command (stop above all) always runs.
    other = _still_running(store, job, ctx.run_id, now) if job in EXPECTED else None
    if other is not None:
        detail = {"skipped": True, "reason": OVERLAP_REASON, "other_run_id": other.get("run_id")}
        store.upsert(TABLE, [{**row, "finished_at": now, "status": SKIPPED, "detail": detail, "error": None,
                              "duration_s": 0.0, "exit_code": 0}])
        log("job_skipped", job=job, run_id=ctx.run_id, reason=detail["reason"])
        return detail
    if due is None:
        store.upsert(TABLE, [running])
    log("job_start", job=job, run_id=ctx.run_id, dry_run=ctx.dry_run)

    def finish(status: str, detail: Any, error: str | None, code: int) -> float:
        finished = max(datetime.now(UTC), now)  # ctx.now is the start; tests pin it in the future
        seconds = round((finished - now).total_seconds(), 1)
        store.upsert(TABLE, [{**row, "finished_at": finished, "status": status, "detail": detail, "error": error,
                              "duration_s": seconds, "exit_code": code}])
        log("job_end", job=job, run_id=ctx.run_id, status=status, dry_run=ctx.dry_run, error=error, exit_code=code)
        return seconds

    try:
        result = fn(ctx)
    except Skip as exc:
        detail = {"skipped": True, "reason": str(exc)}
        finish(SKIPPED, detail, None, 0)
        return detail
    except BaseException as exc:
        text = clip(redact(f"{type(exc).__name__}: {exc}"), ERROR_LIMIT)
        finish(ERROR, {"violation": True} if isinstance(exc, GuardViolation) else None, text, exit_code_of(exc))
        raise
    if isinstance(result, JobResult):
        summary = _json_safe(result.summary())
        status = result.status
    else:
        summary = _json_safe(result if isinstance(result, Mapping) else {"result": result})
        status = summary_status(summary)
    detail = {**summary, "duration_s": round((max(datetime.now(UTC), now) - now).total_seconds(), 1)}
    fallback = sorted(getattr(ctx.settings, "fallback_tabs", ()) or ())
    if fallback:
        detail["settings_fallback"] = fallback
    error = clip(redact(str(summary.get("reason") or "error")), ERROR_LIMIT) if status == ERROR else None
    finish(status, detail, error, EXIT_ERROR if status == ERROR else 0)
    return summary


def summary_status(summary: Mapping[str, Any]) -> str:
    """SKIPPED when the job said it skipped ({"status": "skipped"} or {"skipped": True}), else OK. A count or a
    mapping under "skipped" (enrol's accounts passed over) is no skip."""
    return SKIPPED if summary.get("status") == SKIPPED or summary.get("skipped") is True else OK


def record_failed_run(store: Any, name: str, started: datetime, now: datetime, code: int, status: str,
                      seconds: float, run_id: str | None = None) -> str:
    """The error row for a run the scheduler saw exit non-zero or stopped at its timeout (W1-1, 19 Oct 2026): the
    run's own row still "running" (the latest of this job started at or after the launch) set to error, or a new row
    (run_id: its claim id) when the run wrote none. A row the run finished itself keeps its own status and error.
    Returns what it did: "left", "updated" or "written"."""
    error = f"exited {code} ({status})"
    detail = {"exit_code": code, "seconds": seconds}
    rows = store.select(TABLE, {"job": name, "started_at": Range(lo=started)})
    if run_id and not any(r.get("run_id") == run_id for r in rows):
        rows += store.select(TABLE, {"run_id": run_id})  # a claim row written before the launch time was read
    rows.sort(key=lambda r: (utc_strict_or_none(r.get("started_at")) or started, str(r.get("run_id"))))
    if rows and rows[-1].get("status") != RUNNING:
        return "left"
    # dry_run unknown on a row written here: the run decided live or dry itself (--live and live_sending), and died
    # before it said which. None claims neither; readers that want a live run test `is False`.
    row = rows[-1] if rows else {"run_id": run_id or new_id(), "job": name, "started_at": started, "dry_run": None}
    store.upsert(TABLE, [{**row, "finished_at": now, "status": ERROR, "error": error, "detail": detail,
                          "duration_s": seconds, "exit_code": code}])
    return "updated" if rows else "written"


def record_unusable(store: Store | None, job: str, errors: Mapping[str, Any], text: str) -> None:
    """A job that cannot start on unusable settings still leaves a heartbeat, so the miss is visible."""
    if store is None:
        return
    now = datetime.now(UTC)
    try:
        store.upsert(TABLE, [{
            "run_id": new_id(), "job": job, "started_at": now, "finished_at": now, "status": ERROR,
            "dry_run": True, "detail": {"unusable": sorted(errors)}, "error": text, "duration_s": 0.0,
            "exit_code": 2,
        }])
    except Exception as write_error:  # the refusal is what matters; the log says why no row
        log("heartbeat_write_failed", job=job, error=str(write_error))


# -- missed jobs ----------------------------------------------------------------------------------------------------

_H = 60
_DAY = 24 * _H
DAILY = 26 * _H  # a job expected this rarely (or rarer) waits hours for its next try
# Longest gap between healthy runs before a job counts as missed (SPEC 9 schedules plus slack). Every cadence change
# changes this in the same commit (design §9).
EXPECTED: dict[str, int] = {
    "settings_sync": 26 * _H,  # 02:00 daily (and 11:30 on weekdays; the 02:00 run alone keeps it healthy)
    "source_universe": 26 * _H,  # 03:00 weekdays (weekday time; SPEC 9 had the 1st of the month)
    "apollo_signals": 26 * _H,  # 05:10 weekdays (weekday time; 03:30 until W1-3a, 19 Oct 2026)
    "read_pages": 26 * _H,  # 03:45 weekdays (weekday time; build addition, Harry, 2 Oct 2026)
    "apollo_enrich": 26 * _H,  # 05:00 weekdays (weekday time; 04:10 until W1-3a)
    "apollo_people": 26 * _H,  # 04:20 weekdays (weekday time; build addition, Harry, 5 Oct 2026)
    "site_visits": 26 * _H,  # 06:00 daily
    "verify_accounts": 26 * _H,  # 04:30 weekdays (weekday time; build addition)
    "pick_contacts": 26 * _H,  # 05:30 weekdays (weekday time)
    "enrol": 26 * _H,  # 12:00 weekdays (weekday time)
    "poll_replies": 45,  # every 15 min, round the clock (the reply SLA)
    "poll_approvals": 20,  # every 5 min
    # Hourly from W2-4 (20 Oct 2026; RS §2 W2-4): a run at :00, so 150 minutes is two missed runs and a slow one.
    "hubspot_readback": 150,
    # Every 15 minutes inside the ET send window (13:00-22:59 UK on weekdays), hourly at :07 otherwise (W2-4): the
    # longest healthy gap is an hour, so 75 minutes is one missed run.
    "sync_outcomes": 75,
    "mailbox_health": 26 * _H,  # 07:00 daily
    "kill_rules": 150,  # hourly
    "blackout": 150,  # hourly (Harry, 7 Oct 2026; registry/blackout.py)
    "heartbeat_check": 150,  # hourly; the outside watchdog watches this one (a late ping emails Harry)
    # 09:00 Tuesday to Saturday (19 Oct 2026): Saturday 09:00 to Tuesday 09:00 is 72 hours (73 when the clocks go
    # back on the Sunday between), counted in wall time as the job runs on a Saturday, so not in WEEKDAY_JOBS.
    "daily_post": 74 * _H,
    "capture": 8 * _DAY,  # Mon 07:30 (W1-2, 19 Oct 2026; replies/capture.py)
    "monday_readout": 8 * _DAY,  # Mon 08:30 (Harry, 6 Oct 2026; learn/readout.py)
    "suppression_load": 26 * _H,  # 01:30 daily (build addition)
    "follow_up": 26 * _H,  # 08:45 daily (W2-4, 20 Oct 2026; replies/follow_up.py): the not-now dates kept
    # Monthly jobs (Harry, 5 Oct 2026): at most 31 days apart, plus a day's slack.
    "lookalikes": 32 * _DAY,  # the 1st at 02:30
    # lookalike_leads is parked (W0, 19 Oct 2026: enabled=False in ops/schedule.py), so it is not expected.
    "label_sample_monthly": 32 * _DAY,  # the 1st at 09:15
    "hand_check_draw": 8 * _DAY,  # Mon 05:45 (W1-5, 19 Oct 2026)
    "retention": 26 * _H,  # 00:40 daily (build addition: SPEC 6 and 13, ops/retention.py)
}
# score has no schedule of its own: it runs inside settings_sync, verify_accounts, pick_contacts and site_visits.
WEEKDAY_JOBS = frozenset({"source_universe", "apollo_signals", "read_pages", "apollo_enrich", "apollo_people",
                          "verify_accounts", "pick_contacts", "enrol"})
# What stops happening when a job stops, for a reader who does not know the job (say.job_name names it).
IMPACT: dict[str, str] = {
    "poll_replies": "replies from prospects are not being read",
    "capture": "nobody is checking that replies are being read at all (the weekly check against Instantly)",
    "poll_approvals": "approvals given in Slack are not being acted on",
    "hubspot_readback": "HubSpot is not being updated",
    "sync_outcomes": "bounces, opt-outs and other email results are not being recorded",
    "enrol": "nobody new is being added to the email campaign",
    "pick_contacts": "no new contacts are being chosen",
    "apollo_enrich": "verified companies' exact headcounts and funding are not being read from Apollo",
    "apollo_signals": "verified companies' open job postings are not being read from Apollo",
    "kill_rules": "bounce and complaint limits are not being checked",
    "mailbox_health": "the sending mailboxes are not being checked",
    "blackout": "sends are not being paused on holidays",
    "suppression_load": "the do-not-contact list is not being refreshed",
    "settings_sync": "edits to the settings sheet are not being picked up",
    "daily_post": "the daily summary is not being posted",
    "retention": "old personal data is not being deleted on time",
    "hand_check_draw": "nobody is checking by hand that the companies about to be emailed are the right ones",
    "follow_up": "replies that said “not now” are not being followed up on the date they gave",
}
UNUSABLE_RE = re.compile(r"settings are unusable \(([^)]*)\)")  # bootstrap.SettingsUnusable's text
FAULT_LINES = 3  # the settings errors shown in an alert


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
    times = [t for t in (utc_strict_or_none(run.get(k)) for k in ("last_alive_at", "last_ok_at")) if t is not None]
    if times:
        return max(times)
    started = utc_strict_or_none(run.get("started_at"))
    first = (run.get("runs") or 1) <= 1
    if first and run.get("status") == RUNNING and started and now - started < timedelta(minutes=OVERLAP_MINUTES):
        return started  # the job's first run, still going
    return None


def overdue_minutes(job: str, run: Mapping[str, Any] | None, now: datetime) -> float | None:
    """How far past EXPECTED[job] the job is (positive = missed); None if it never ran. A job that has runs but never
    a healthy one is missed without end (inf), so it is alerted in the daily reminder rather than as newly missed."""
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


def unusable_tabs(error: Any) -> list[str]:
    """The tabs a "settings are unusable (Copy, General); ..." error names; [] for any other error."""
    m = UNUSABLE_RE.search(str(error or ""))
    return [t for t in (x.strip() for x in m.group(1).split(",")) if t and t != "unknown"] if m else []


def esc(text: str) -> str:
    """Text safe inside a Slack message (&, <, >)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def and_list(items: Iterable[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}" if items else ""


def settings_fault(ctx: Context, tabs: Iterable[str]) -> tuple[list[str], list[str]]:
    """(facts, steps) for settings the jobs refuse to start on: a link to each failing tab, then the first errors of
    the version saved in the database (settings/sync.load_current), so the reader can fix them without the logs."""
    from us_outbound.settings import sync

    tabs = list(dict.fromkeys(tabs))
    errors: list[Any] = []
    try:
        _, report = sync.load_current(ctx.store)
        tabs = tabs or [t for t, errs in report.items() if errs]
        errors = [e for t in tabs for e in report.get(t, [])]
    except Exception as exc:  # the alert still goes, with the tabs alone
        log("settings_fault_unread", error=str(exc)[:160])
    links = and_list(say.sheet_link(ctx, t) for t in tabs) or say.sheet_link(ctx)
    facts = [f"The settings sheet has errors on the {links}, so {sync.REFUSE_WORDS}."]
    if errors:
        facts.append("The first error:" if len(errors) == 1 else f"The first {min(len(errors), FAULT_LINES)} errors:")
        facts += [f"   ◦ {esc(str(e))}" for e in errors[:FAULT_LINES]]
        if len(errors) > FAULT_LINES:
            facts.append(f"   ◦ …and {len(errors) - FAULT_LINES} more: the sync below lists every one in this channel.")
    steps = [f"Correct {('this' if len(errors) == 1 else 'these') if errors else 'the errors'} on the {links}. If the "
             "sheet already looks right, the copy saved earlier fails a newer check, and step 2 replaces it.",
             f"Run {say.command('sync')} to read the sheet now. Otherwise it is read {sync.SYNC_TIMES}.",
             "The jobs start again by themselves at their next run."]
    return facts, steps


def _last_good(run: Mapping[str, Any], now: datetime) -> str:
    last = _last_healthy(run, now)
    return f"last good run {last.astimezone(UK):%a %d %b %H:%M} UK" if last else "no good run yet"


def _span(minutes: int) -> str:
    if minutes % _DAY == 0:
        return f"{minutes // _DAY} days"
    if minutes >= 2 * _H:
        return f"{minutes / _H:g} hours"
    return f"{minutes} minutes"


def missed_message(ctx: Context, missed: list[str], runs: Mapping[str, Mapping[str, Any]]) -> str:
    """The Slack text for the missed jobs (the module docstring)."""
    now = ctx.now
    first = say.job_name(missed[0])
    head = (f"{first[:1].upper()}{first[1:]} has stopped running" if len(missed) == 1
            else f"{len(missed)} automatic jobs have stopped running")
    facts = ["Overdue: " + and_list(f"{say.job_name(j)} ({_last_good(runs[j], now)}; due at least every "
                                    f"{_span(EXPECTED[j])})" for j in missed) + "."]
    impacts = [IMPACT[j] for j in missed if j in IMPACT]
    if impacts:
        facts.append(f"What this means: {and_list(impacts)}.")
    failing = [j for j in missed if runs[j].get("status") == ERROR and runs[j].get("error")]
    stuck = {j: tabs for j in failing if (tabs := unusable_tabs(runs[j]["error"]))}
    steps: list[str] = []
    if stuck:  # one cause for them all: said once, not on every job
        fault, steps = settings_fault(ctx, [t for tabs in stuck.values() for t in tabs])
        who = "" if len(stuck) == len(missed) else f" ({and_list(say.job_name(j) for j in stuck)})"
        facts.append(f"Why{who}: {fault[0][:1].lower()}{fault[0][1:]}")
        facts += fault[1:]
    for j in failing:
        if j not in stuck:
            quote = clip(" ".join(str(runs[j]["error"]).split()), 200)
            facts.append(f"Why {say.job_name(j)} stopped, from its latest run: “{esc(quote)}”")
    others = [j for j in missed if j not in stuck]
    if others:
        which = f"For {and_list(say.job_name(j) for j in others)}: see" if stuck else "See"
        steps.append(f"{which} why in {say.logs_link()}, or run {say.command('status')} for each job's last run. "
                     f"This clears by itself once {'it runs' if len(others) == 1 else 'they run'} again.")
    return say.compose(say.URGENT if stuck else say.ACTION, head, facts, do=steps)


def check_heartbeats(ctx: Context, jobs: Iterable[str] | None = None) -> list[str]:
    """The jobs whose heartbeat is missed; alerts Slack when one is newly missed. A job with no heartbeat row at all
    is not flagged (it has not been deployed or run yet); `us-outbound status` lists it as never run."""
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
        notify.alert(ctx, missed_message(ctx, missed, runs))  # a Slack that fails is logged: the watchdog's ping goes
    log("heartbeat_check", missed=missed, newly_missed=new, alerted=bool(new or reminder))
    return missed


def failed_jobs(runs: Mapping[str, Mapping[str, Any]], jobs: Iterable[str], now: datetime) -> list[str]:
    """The jobs among these that run daily or less often and whose latest run failed in the last DAILY minutes: their
    next try is hours or days away. After a day a failed run no longer counts, so a monthly job's failure does not keep
    the outside watchdog failing (and blind to anything new) for a month; it is missed once its window passes."""
    out = []
    for j in jobs:
        run = runs.get(j) or {}
        started = utc_strict_or_none(run.get("started_at"))
        if (EXPECTED.get(j, 0) >= DAILY and run.get("status") == ERROR and started is not None
                and now - started <= timedelta(minutes=DAILY)):
            out.append(j)
    return out


# -- the errors the jobs caught -------------------------------------------------------------------------------------

LOOKBACK = timedelta(hours=26)  # a daily job's latest run is always this recent
QUOTE = 160  # characters of the first error quoted
DEPTH = 4  # how deep in a job's detail errors are looked for
LATEST_TWO_SQL = (  # each job's two latest finished runs: the latest, and the one before (transient errors, below)
    "SELECT job, run_id, status, started_at, finished_at, detail, error FROM ("
    "SELECT *, ROW_NUMBER() OVER (PARTITION BY job ORDER BY started_at DESC, run_id DESC) AS n"
    " FROM {schema}.heartbeats WHERE started_at >= %(since)s AND status <> 'running') t WHERE n <= 2"
    " ORDER BY job, started_at DESC, run_id DESC"
)
# A service's own outage (HTTP 5xx or 429, a timeout, a dropped connection): posted for a frequent job only when the
# run before met it too (Harry, 14 Oct 2026: one Instantly 502 reached Slack).
TRANSIENT = re.compile(r"HTTP (?:5\d\d|429)\b|timed out|timeout|ConnectionError|RemoteDisconnected|"
                       r"Connection (?:reset|aborted)", re.IGNORECASE)
SERVER_ERROR = re.compile(r"\b(\w+) HTTP (\d{3})\b")
FAILED, ERRORS, CLAUDE_CAP, SLACK_POST, UNUSABLE = "failed", "errors", "claude_cap", "slack_post", "unusable"
FAILURES, FAILURE_RATE, ERROR_KEY = "failures", "failure_rate", "error"
FAILURES_MIN, FAILURES_SHARE = 3, 0.5  # one reason failing 3 items or more, and at least half the candidates
RATE_RUNS, RATE_ERRORS = 12, 3  # the failure-rate rule: 3 errors or more in a frequent job's last 12 runs
RECENT_SQL = (  # each job's latest RATE_RUNS finished runs
    "SELECT job, run_id, status, started_at, error FROM ("
    "SELECT job, run_id, status, started_at, error,"
    " ROW_NUMBER() OVER (PARTITION BY job ORDER BY started_at DESC, run_id DESC) AS n"
    " FROM {schema}.heartbeats WHERE started_at >= %(since)s AND status <> 'running' AND job = ANY(%(jobs)s)) t"
    " WHERE n <= %(runs)s ORDER BY job, started_at DESC, run_id DESC"
)
HEAD = f"{say.ACTION} *Some automatic jobs ran into problems*"
ERROR_WORDS = {  # a text under a key ending "_error", in plain words (the key itself, spaced, when it is not here)
    "vitals_error": "could not read the mailboxes' health from Instantly",
    "claude_error": "could not ask Claude about a reply",
    "interest_error": "could not check a reply's interest",
    "alert_error": "could not post its alert",
    "capacity_error": "could not check the sending capacity ahead",
    "customers_error": "could not read Spill's customers from HubSpot",
    ERROR_KEY: "met an error",
}
COUNT_WORDS = {"slack_errors": "Slack posts failed"}  # a count under a key ending "_errors"
# How a rejected key reads in an error: clients/http.ApiError ("hubspot HTTP 401 for /crm/..."), Slack's error
# codes (it answers HTTP 200 with ok false), clients/claude.ClaudeError ("Claude API error 401"), google-auth.
AUTH = (
    (re.compile(r"\b(apollo|instantly|hubspot|sheets|slack)\s+HTTP\s+(?:401|403)\b", re.I), ""),
    (re.compile(r"\b(?:invalid_auth|not_authed|token_revoked|token_expired|account_inactive)\b"), "slack"),
    (re.compile(r"\bClaude API error (?:401|403)\b|\bauthentication_error\b|\bpermission_error\b"), "claude"),
    (re.compile(r"\bRefreshError\b|\binvalid_grant\b"), "sheets"),
)


def keys() -> dict[str, tuple[str, str]]:
    """system -> (who Harry knows it as, the Railway variable holding its key)."""
    from us_outbound.ops.bootstrap import GOOGLE_KEY_VAR  # bootstrap imports this module

    return {
        "apollo": ("Apollo", SECRET_NAMES["apollo"]),
        "instantly": ("Instantly", SECRET_NAMES["instantly"]),
        "hubspot": ("HubSpot", SECRET_NAMES["hubspot"]),
        "slack": ("Slack", SECRET_NAMES["slack"]),
        "claude": ("Anthropic (Claude)", SECRET_NAMES["claude"]),
        "sheets": ("Google", GOOGLE_KEY_VAR),
    }


def foot(stopped: bool = False) -> str:
    """The post's last line: when it fixes itself, and where the rest is (read at post time: logs_link reads env).
    stopped: the settings are unusable, so the jobs do not try again until they are fixed (its own steps say so)."""
    again = "" if stopped else "Each job tries again at its next run. "
    return f"_{again}More detail: {say.logs_link()}, or {say.command('status')} for each job's last run._"


def rejected_key(text: str) -> str | None:
    """The system whose key the error says was rejected (a keys() key), or None."""
    for pattern, system in AUTH:
        m = pattern.search(text)
        if m:
            return system or m.group(1).lower()
    return None


@dataclass
class Finding:
    job: str
    kind: str
    texts: list[str] = field(default_factory=list)  # the errors, first first
    count: int = 0  # for a counted failure (slack_errors) or the cap
    at: datetime | None = None
    note: str = ""  # unusable: where the settings sync's own message listed every error


def _finished_rows(ctx: Context, jobs: Sequence[str], since: datetime) -> list[dict]:
    """MemoryStore's stand-in for the two queries: the finished runs of these jobs since `since`, latest first."""
    rows = [r for r in ctx.store.select(TABLE, {"job": sorted(set(jobs))})
            if r.get("status") != RUNNING and (utc(r.get("started_at")) or since - timedelta(1)) >= since]
    rows.sort(key=lambda r: (utc(r.get("started_at")), str(r.get("run_id"))), reverse=True)
    return rows


def two_latest(ctx: Context, jobs: Iterable[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    """(job -> its latest finished run, job -> the run before it), of runs started in the last LOOKBACK."""
    wanted = set(jobs)
    since = ctx.now - LOOKBACK
    try:
        rows = ctx.store.query(LATEST_TWO_SQL.format(schema=ctx.store.schema), {"since": since})
    except NotImplementedError:
        rows = _finished_rows(ctx, sorted(wanted), since)
    latest: dict[str, dict] = {}
    before: dict[str, dict] = {}
    for r in rows:
        job = str(r.get("job") or "")
        if job not in wanted:
            continue
        if job not in latest:
            latest[job] = dict(r)
        elif job not in before:
            before[job] = dict(r)
    return latest, before


def recent_runs(ctx: Context, jobs: Iterable[str]) -> dict[str, list[dict]]:
    """job -> its latest RATE_RUNS finished runs started in the last LOOKBACK, the latest first (one query)."""
    wanted = sorted(set(jobs))
    if not wanted:
        return {}
    since = ctx.now - LOOKBACK
    try:
        rows = ctx.store.query(RECENT_SQL.format(schema=ctx.store.schema),
                               {"since": since, "jobs": wanted, "runs": RATE_RUNS})
    except NotImplementedError:
        rows = _finished_rows(ctx, wanted, since)
    out: dict[str, list[dict]] = {}
    for r in rows:
        runs = out.setdefault(str(r.get("job") or ""), [])
        if len(runs) < RATE_RUNS:
            runs.append(dict(r))
    return out


def failure_rates(ctx: Context, jobs: Iterable[str]) -> list[Finding]:
    """The failure-rate rule: a frequent job with RATE_ERRORS or more errors in its last RATE_RUNS runs, with its
    latest error. A daily (or rarer) job is told on its first failure (failed) instead."""
    frequent = [j for j in jobs if 0 < EXPECTED.get(j, 0) < DAILY]
    out = []
    for job, runs in sorted(recent_runs(ctx, frequent).items()):
        # A refusal on unusable settings is told once for every job (unusable), not again as each job's failure rate.
        errors = [r for r in runs if r.get("status") == ERROR and not unusable_tabs(r.get("error"))]
        if len(errors) >= RATE_ERRORS:
            out.append(Finding(job, FAILURE_RATE, [str(errors[0].get("error") or "no error text")], count=len(errors),
                               at=utc(errors[0].get("started_at"))))
    return out


def transient(f: Finding) -> bool:
    """Whether every error of the finding is a service's own outage (TRANSIENT)."""
    return bool(f.texts) and all(TRANSIENT.search(str(t)) for t in f.texts)


def passing(job: str, found: list[Finding], before: Mapping[str, Any] | None) -> list[Finding]:
    """The findings worth a post: an outage a frequent job met once is left out until the run before met it too."""
    if EXPECTED.get(job, 0) >= DAILY:
        return found
    earlier = {f.kind for f in findings(job, before)} if before else set()
    return [f for f in found if not transient(f) or f.kind in earlier]


def _walk(detail: Any, depth: int = 0) -> Iterator[tuple[str, Any, Mapping[str, Any]]]:
    """(key, value, the mapping holding it) for every key in the detail, nested mappings included, to DEPTH."""
    if not isinstance(detail, Mapping) or depth > DEPTH:
        return
    for key, value in detail.items():
        yield str(key), value, detail
        if isinstance(value, Mapping):
            yield from _walk(value, depth + 1)


def _error_text(value: Any, holder: Mapping[str, Any]) -> bool:
    """Whether a text under a key named error is a failure: not a post's own result (slack_post reads those), not a
    dry run's note, and not the log-only post with no token."""
    return (isinstance(value, str) and bool(value.strip()) and "posted" not in holder
            and not value.startswith("dry-run") and value != notify.NO_TOKEN)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _failures(job: str, detail: Mapping[str, Any], at: datetime | None) -> Finding | None:
    """The per-item contract: the reasons that failed FAILURES_MIN items or more and at least FAILURES_SHARE of the
    candidates (with no candidates given, the count alone), the commonest first."""
    failures = detail.get(FAILURES)
    if not isinstance(failures, Mapping):
        return None
    tried = _count(detail.get("candidates"))
    tried = tried if tried and tried > 0 else None
    hits = sorted(((str(reason), n) for reason, n in ((r, _count(v)) for r, v in failures.items())
                   if n is not None and n >= FAILURES_MIN and (tried is None or n >= FAILURES_SHARE * tried)),
                  key=lambda x: (-x[1], x[0]))
    if not hits:
        return None
    texts = [f"{reason} ({n} of {tried})" if tried else f"{reason} ({n})" for reason, n in hits]
    return Finding(job, FAILURES, texts, count=sum(n for _, n in hits), at=at)


def findings(job: str, run: Mapping[str, Any]) -> list[Finding]:
    """What one run's heartbeat records as gone wrong (the module docstring's kinds)."""
    at = utc(run.get("started_at"))
    detail = run.get("detail") if isinstance(run.get("detail"), Mapping) else {}
    if run.get("status") == ERROR:
        if "unusable" in detail:
            return [Finding("settings", UNUSABLE, [str(t) for t in detail.get("unusable") or ()], at=at)]
        error = str(run.get("error") or "no error text")
        if EXPECTED.get(job, 0) < DAILY and not rejected_key(error):
            return []  # tried again within minutes; missed (with this error) if it keeps failing
        return [Finding(job, FAILED, [error], at=at)]
    out: dict[str, Finding] = {}
    failed = _failures(job, detail, at)
    if failed is not None:
        out[FAILURES] = failed
    for key, value, holder in _walk(detail):
        if key == ERROR_KEY and _error_text(value, holder):
            out.setdefault(ERROR_KEY, Finding(job, ERROR_KEY, at=at)).texts.append(value)
        elif key == "claude_cap_reached" and isinstance(value, int) and value > 0:
            out.setdefault(CLAUDE_CAP, Finding(job, CLAUDE_CAP, at=at)).count += value
        elif (key == "errors" or key.endswith("_errors")) and isinstance(value, list) and value:
            out.setdefault(ERRORS, Finding(job, ERRORS, at=at)).texts += [str(v) for v in value]
        elif key.endswith("_errors") and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out.setdefault(key, Finding(job, key, at=at)).count += value
        elif key.endswith("_error") and isinstance(value, str) and value.strip():
            out.setdefault(key, Finding(job, key, at=at)).texts.append(value)
        elif (isinstance(value, Mapping) and value.get("posted") is False and isinstance(value.get("error"), str)
              and value["error"] and value["error"] != notify.NO_TOKEN):
            out.setdefault(SLACK_POST, Finding(job, SLACK_POST, at=at)).texts.append(value["error"])
    return list(out.values())


def _quote(text: str) -> str:
    """The error in a few words: a service's error page is named, not quoted (14 Oct 2026: a 502 quoted its HTML)."""
    text = str(redact(text))
    hit = SERVER_ERROR.search(text)
    if hit and (int(hit.group(2)) >= 500 or "<html" in text.lower() or "<!doctype" in text.lower()):
        return (f"{hit.group(1).capitalize()}'s server had an error (HTTP {hit.group(2)}), a problem on their side "
                "that usually clears by itself")
    return "“" + clip(" ".join(text.split()), QUOTE) + "”"


def _when(at: datetime | None) -> str:
    return f"{at.astimezone(UK):%a %H:%M} UK" if at else "recently"


def _job(job: str) -> str:
    """The job in plain words, capitalised to open a line: "Checking for replies"."""
    name = say.job_name(job)
    return name[:1].upper() + name[1:]


def _unusable(f: Finding, ctx: Context) -> str:
    """The settings are unusable: the tabs linked, their first errors, and the steps (settings_fault)."""
    facts, steps = settings_fault(ctx, f.texts)
    out = [f"• {say.URGENT} *The jobs have stopped (since {_when(f.at)})*. {facts[0]}",
           *(f"   {x.strip()}" if x.startswith("   ◦") else f"   {x}" for x in facts[1:])]
    if f.note:
        out.append(f"   {f.note}")
    out.append("   *What to do:*")
    out += [f"   {i}. {s}" for i, s in enumerate(steps, 1)]
    return "\n".join(out)


def describe(f: Finding, ctx: Context) -> str:
    """One bullet for a finding that is not a rejected key."""
    if f.kind == UNUSABLE:
        return _unusable(f, ctx)
    when = _when(f.at)
    if f.kind == CLAUDE_CAP:
        from us_outbound.settings.validate import CLAUDE_CAP_USD

        now = ctx.settings.general.claude_monthly_cap_usd
        return (f"• *Replies are going without draft answers* ({say.job_name(f.job)}, {when}): Claude's monthly "
                f"spending cap is used up, so {fmt.plural(f.count, 'reply', 'replies')} were filed as “other” with no "
                f"draft.\n   *What to do:* raise `claude_monthly_cap_usd` (now ${now:,.0f}, at most "
                f"${CLAUDE_CAP_USD:,.0f}) on the {say.sheet_link(ctx, 'General')}, and the spend limit in the Anthropic "
                "Console to match.")
    if f.kind == FAILED:
        return f"• *{_job(f.job)}* failed ({when}): {_quote(f.texts[0])}"
    if f.kind == FAILURES:
        return (f"• *{_job(f.job)}* ({when}) finished, but {fmt.plural(f.count, 'item', 'items')} in it failed. The "
                f"commonest: {_quote(f.texts[0])}")
    if f.kind == FAILURE_RATE:
        return (f"• *{_job(f.job)}* failed in {f.count} of its last {RATE_RUNS} runs (the latest {when}): each "
                f"failure alone is tried again, but this one keeps coming back. The latest: {_quote(f.texts[0])}")
    if f.kind == SLACK_POST:
        return f"• *{_job(f.job)}* ({when}) could not post its message to Slack: {_quote(f.texts[0])}"
    if f.kind == ERRORS:
        n = len(f.texts)
        return (f"• *{_job(f.job)}* ({when}) finished, but skipped past {fmt.plural(n, 'error', 'errors')}. "
                f"The first: {_quote(f.texts[0])}")
    if not f.texts:  # a count: slack_errors
        return f"• *{_job(f.job)}* ({when}): {f.count} {COUNT_WORDS.get(f.kind, f.kind.replace('_', ' '))}"
    return f"• *{_job(f.job)}* ({when}) {ERROR_WORDS.get(f.kind, f.kind.replace('_', ' '))}: {_quote(f.texts[0])}"


def _key_rejected(system: str, jobs: list[str], first: str) -> str:
    name, var = keys()[system]
    return (f"• {say.URGENT} *{name} turned down our key* ({and_list(say.job_name(j) for j in jobs)}): "
            f"{_quote(first)}\n   *What to do:* if the key was revoked or has expired, put a new one in `{var}` "
            f"under Variables in Railway, then redeploy. If the key is current, our {name} plan may not allow this "
            "request.")


def _run_day(at: datetime | None, day: str) -> str:
    """The UK day the run began, which keys its post; `day` (today) when its start is unknown."""
    return at.astimezone(UK).date().isoformat() if at else day


def lines(found: Iterable[Finding], day: str, ctx: Context) -> list[tuple[str, str]]:
    """(key, line) per finding, a rejected key told as such (once a day per system, naming every job that met it).
    A key carries the UK day its run began, not today: a daily job's failed run stays its latest until the next run,
    and keyed on today it was told again just after midnight (read_pages, Thu 8 Oct 2026, told again at 00:05 Fri)."""
    systems = keys()
    rejected: dict[str, list[tuple[str, str, str]]] = {}  # system -> [(job, the error, its run's day)]
    out: list[tuple[str, str]] = []
    for f in found:
        others = []
        for text in f.texts:
            system = rejected_key(text)
            if system in systems:
                rejected.setdefault(system, []).append((f.job, text, _run_day(f.at, day)))
            else:
                others.append(text)
        if f.texts and not others:
            continue  # every error of it is a rejected key, told below
        f.texts = others
        out.append((f"job_error:{f.job}:{f.kind}:{_run_day(f.at, day)}", describe(f, ctx)))
    for system, hits in rejected.items():
        jobs = list(dict.fromkeys(j for j, _, _ in hits))
        out.insert(0, (f"key_rejected:{system}:{max(d for _, _, d in hits)}", _key_rejected(system, jobs, hits[0][1])))
    return out


def _sync_post(run: Mapping[str, Any] | None) -> str:
    """Where settings_sync's own message listed every row error, if its latest run posted one; "" otherwise."""
    from us_outbound.settings import sync

    detail = (run or {}).get("detail")
    if not isinstance(detail, Mapping) or not (detail.get("rejected") and detail.get("alerted")):
        return ""
    return (f"Every error on the sheet is listed in the settings sync's message of {_when(utc(run.get('started_at')))}"
            f" (“{sync.HEADLINE}…”).")


def report_errors(ctx: Context, jobs: Iterable[str]) -> dict[str, Any]:
    """Read, then post what was not posted today. Never raises (but a GuardViolation): heartbeat_check goes on."""
    try:
        watched = [j for j in jobs if j != JOB]
        runs, before = two_latest(ctx, watched)
        found = [f for job, run in sorted(runs.items()) for f in passing(job, findings(job, run), before.get(job))]
        found += failure_rates(ctx, watched)  # never filtered as transient: it exists for the intermittent ones
        unusable = [f for f in found if f.kind == UNUSABLE]
        found = [f for f in found if f.kind != UNUSABLE] + unusable[:1]  # one line for all the jobs it stops
        if unusable:
            found[-1].note = _sync_post(runs.get("settings_sync"))
        pairs = lines(found, ctx.today_uk().isoformat(), ctx)
        sent = notify.post_once(ctx, pairs, head=HEAD, foot=foot(bool(unusable))) if pairs else {"posted": False, "keys": []}
    except GuardViolation:
        raise
    except Exception as exc:  # the database, say: the watchdog ping must still go
        log("job_errors_failed", error=str(exc)[:200])
        return {"found": [], "posted": [], "check_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    log("job_errors", found=[k for k, _ in pairs], posted=sent.get("keys"))
    return {"found": [notify.kind_of(k) for k, _ in pairs], "posted": sent.get("keys") or [],
            "post": {k: sent.get(k) for k in ("posted", "error")}}


# -- the outside watchdog -------------------------------------------------------------------------------------------

WATCHDOG_SECRET = "watchdog"
WATCHDOG_VAR = SECRET_NAMES[WATCHDOG_SECRET]
# What Slack answers auth.test with when the token itself is no good (Slack's API docs).
SLACK_AUTH_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"})


def watchdog_url(secrets: Secrets) -> tuple[str, str]:
    """(the ping URL, "") or ("", why there is none): unset, or not an https URL."""
    try:
        value = secrets.get(WATCHDOG_SECRET)
    except ConfigError:
        return "", f"{WATCHDOG_VAR} is not set"
    if not _https_url(value):
        return "", f"{WATCHDOG_VAR} is not an https ping URL"
    return value.rstrip("/"), ""


def _https_url(value: str) -> bool:
    """https://host/path, with a dotted host and no spaces, query, fragment or user part (only clients/http.py
    imports urllib: tests/test_guardrails.py)."""
    if not value.lower().startswith("https://"):
        return False
    host = value[len("https://"):].split("/", 1)[0]
    return "." in host and not any(c.isspace() or c in "?#@" for c in value)


def slack_problem(ctx: Context) -> str:
    """Why alerts cannot reach Slack now, or "": no token in a live run, a token Slack rejects (auth.test), or Slack
    out of reach. Dry-run without a token posts to the log by design (clients/slack.SlackOff), which is no problem."""
    try:
        slack = ctx.clients.slack
    except ConfigError:
        return f"no Slack token ({SECRET_NAMES['slack']} is not set)"
    try:
        slack.auth_test()
    except GuardViolation:
        raise
    except ApiError as exc:
        code = str(exc.body.get("error") or "") if isinstance(exc.body, Mapping) else ""
        if code in SLACK_AUTH_ERRORS:
            return f"Slack rejected the bot token ({code})"
        return f"Slack could not check the bot token ({code or f'HTTP {exc.status}'})"
    except Exception as exc:  # a connection error or a timeout, after the transport's retries
        return f"Slack could not be reached ({getattr(exc, 'reason', type(exc).__name__)})"
    return ""


def ping(ctx: Context, reasons: Sequence[str] = ()) -> dict[str, Any]:
    """GET the ping URL, or its /fail form when there are reasons. Never raises (but a GuardViolation)."""
    fail = bool(reasons)
    client = ctx.clients.watchdog
    if client is None:
        _, why = watchdog_url(ctx.clients.secrets)
        log("watchdog_off", reason=why or f"no watchdog URL for {ctx.job}", would_fail=fail)
        return {"pinged": False, "fail": fail, "off": why or "no watchdog URL"}
    try:
        status = client.ping(fail=fail)
    except GuardViolation:
        raise
    except Exception as exc:  # the URL is in a requests error's text, so only its type is kept
        error = str(exc) if isinstance(exc, PingFailed) else getattr(exc, "reason", type(exc).__name__)
        log("watchdog_ping_failed", fail=fail, error=error)
        return {"pinged": False, "fail": fail, "ping_error": error}
    log("watchdog_ping", fail=fail, status=status, reasons=list(reasons))
    return {"pinged": True, "fail": fail, "status": status, "reasons": list(reasons)}


def ping_from_env(reason: str, *, env: Mapping[str, str] | None = None, transport: Any = None) -> dict[str, Any]:
    """The watchdog's /fail from the environment alone, for the scheduler while it cannot hold its lock or reach
    Postgres (ops/scheduler.py): no settings and no database. Never raises."""
    import os

    from us_outbound.clients.guard import Guard
    from us_outbound.context import boundaries_for
    from us_outbound.settings.model import General, Settings

    try:
        guard = Guard(live=False, job="scheduler")
        url, why = watchdog_url(Secrets(guard, os.environ if env is None else env))
        if not url:
            log("watchdog_off", reason=why, would_fail=True)
            return {"pinged": False, "fail": True, "off": why}
        guard.configure(bounds=boundaries_for(Settings(general=General()), "", watchdog_url=url))
        if transport is None:
            from us_outbound.clients.http import RequestsTransport

            transport = RequestsTransport()
        status = Watchdog(guard, transport, url).ping(fail=True)
    except Exception as exc:  # the URL is in a requests error's text, so only its type is kept
        error = str(exc) if isinstance(exc, PingFailed) else type(exc).__name__
        log("watchdog_ping_failed", fail=True, error=error)
        return {"pinged": False, "fail": True, "ping_error": error}
    log("watchdog_ping", fail=True, status=status, reasons=[reason])
    return {"pinged": True, "fail": True, "status": status, "reasons": [reason]}


# -- heartbeat_check ------------------------------------------------------------------------------------------------


def check(ctx: Context) -> dict:
    """heartbeat_check: missed + failed + the caught errors (with the failure-rate rule) + Slack, then the outside
    watchdog's ping, /fail when something needs a look."""
    jobs = scheduled_jobs()
    try:
        missed = check_heartbeats(ctx, jobs)
        runs = latest_runs(ctx.store)
    except GuardViolation:
        raise
    except Exception:  # the database, say: Healthchecks hears it at once, then the error is recorded
        ping(ctx, ["heartbeat_check could not read the heartbeats"])
        raise
    failed = failed_jobs(runs, jobs, ctx.now)
    errors = report_errors(ctx, jobs)
    slack = slack_problem(ctx)
    reasons = ([f"missed: {', '.join(missed)}"] if missed else []) + (
        [f"failed: {', '.join(failed)}"] if failed else []) + ([slack] if slack else [])
    sent = ping(ctx, reasons)
    return {"missed": missed, "failed": failed, "job_errors": errors, "slack": slack or "ok", "watchdog": sent}


def run(ctx: Context) -> dict:
    """The heartbeat_check job (JOB CONTRACT: run(ctx) -> summary)."""
    return check(ctx)
