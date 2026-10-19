"""Heartbeats (SPEC 9, 13 Health): every job writes one row per run; a missed heartbeat alerts in Slack.

run_job(ctx, fn) wraps a job function run(ctx) -> dict:
  * inserts the run's heartbeats row with status "running" (upserted by run_id);
  * skips the run (status "skipped") if another run of the same scheduled job is still
    going, so a slow poll never overlaps the next one (operator commands are never skipped);
  * runs the job, then upserts the row with status ok, error or skipped, finished_at,
    the job's summary as detail, and the error text (clipped, emails hashed).
A job reports a skip by returning {"skipped": True, "reason": ...} or raising Skip.
Errors are re-raised after the row is written, so the CLI exits non-zero.

run(ctx) is the heartbeat_check job (hourly, beside kill_rules; Harry, 7 Oct 2026): check_heartbeats,
then the errors the jobs caught (ops/job_errors.py), then the outside watchdog's ping (ops/watchdog.py),
which fails when a job is missed, a daily (or rarer) job's latest run failed (failed_jobs), or Slack
cannot take alerts. When the settings are unusable it starts on the General defaults
(ops/bootstrap.py), so it still alerts and pings. check_heartbeats(ctx): a job is
missed when it last showed it is alive longer ago than EXPECTED[job]; weekday jobs count
only weekday time (UK), so a weekend is not a miss. Alive means a run that finished ok,
or was skipped for a reason of its own (a blackout date). A "running" row is no evidence:
a run killed by its task timeout stays "running" for ever, and the runs skipped behind it
("previous run still going") are no evidence either. The one exception is a job's only
run so far, still within OVERLAP_MINUTES of its start.
It posts one Slack message when a job is newly missed, and repeats the list at the
09:00 UK check each day while anything is still missed. The message (Harry, 12 Oct 2026: clear to a newcomer;
base/say.py) names the jobs in plain words in one sentence, says what has stopped happening (IMPACT), and gives the fix:
when the jobs refuse to start because the settings are unusable, that cause is said once, with a link to each failing
tab and its first errors (settings_fault, shared with ops/job_errors.py); otherwise the logs and `us-outbound status`.

The stop and start commands also write rows here (jobs operator_stop / operator_start). The read side
(latest_runs, enrolment_paused and the row's names) is base/heartbeats.py (9 Oct 2026), so enrolment and the
registry read it without importing ops.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.base import notify, say
from us_outbound.base.heartbeats import ERROR, OK, OVERLAP_REASON, RUNNING, SKIPPED, TABLE, latest_runs
from us_outbound.clients.db import Store
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import UK, Context
from us_outbound.logs import clip, log, redact
from us_outbound.settings.sync import SYNC_TIMES
from us_outbound.timeparse import utc_strict_or_none

ERROR_LIMIT = 1000  # characters of error text kept on the row
OVERLAP_MINUTES = 60  # a "running" row younger than this blocks a second run (scheduler timeouts are <= 60 min)
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
    "blackout": 150,  # hourly (Harry, 7 Oct 2026; registry/blackout.py)
    "heartbeat_check": 150,  # hourly; the outside watchdog watches this one (ops/watchdog.py: a late ping emails Harry)
    "daily_post": 26 * _H,  # 09:00 daily
    "monday_readout": 8 * _DAY,  # Mon 08:30 (Harry, 6 Oct 2026; learn/readout.py)
    "suppression_load": 26 * _H,  # 01:30 daily (build addition)
    # Monthly jobs (build additions, Harry, 5 Oct 2026): at most 31 days apart, plus a day's slack, so a monthly
    # job is never "missed" between its runs.
    "lookalikes": 32 * _DAY,  # the 1st at 02:30 (weekly until 5 Oct 2026)
    # lookalike_leads is parked (W0, 19 Oct 2026: enabled=False in ops/schedule.py), so it is not expected.
    "label_sample_monthly": 32 * _DAY,  # the 1st at 09:15
    "hand_check_post": 8 * _DAY,  # Mon 08:00 (build addition, SPEC 11 weekly hand-check)
    "retention": 26 * _H,  # 00:40 daily (build addition: SPEC 6 and 13, ops/retention.py)
}
# score has no schedule of its own: it runs inside settings_sync, verify_in_clay, verify_accounts and site_visits.
WEEKDAY_JOBS = frozenset({"source_universe", "apollo_signals", "read_pages", "apollo_enrich", "apollo_people",
                          "verify_in_clay", "verify_accounts", "pick_contacts", "enrol"})


class Skip(Exception):
    """Raised by a job to record its run as skipped (e.g. a blackout date), not as an error."""


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
        started = utc_strict_or_none(r.get("started_at"))
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


# What stops happening when a job stops, for a reader who does not know the job (say.job_name names it).
IMPACT: dict[str, str] = {
    "poll_replies": "replies from prospects are not being read",
    "poll_approvals": "approvals given in Slack are not being acted on",
    "hubspot_readback": "HubSpot is not being updated",
    "sync_outcomes": "bounces, opt-outs and other email results are not being recorded",
    "enrol": "nobody new is being added to the email campaign",
    "pick_contacts": "no new contacts are being chosen",
    "kill_rules": "bounce and complaint limits are not being checked",
    "mailbox_health": "the sending mailboxes are not being checked",
    "blackout": "sends are not being paused on holidays",
    "suppression_load": "the do-not-contact list is not being refreshed",
    "settings_sync": "edits to the settings sheet are not being picked up",
    "daily_post": "the daily summary is not being posted",
    "retention": "old personal data is not being deleted on time",
}
UNUSABLE = re.compile(r"settings are unusable \(([^)]*)\)")  # bootstrap.SettingsUnusable's text
FAULT_LINES = 3  # the settings errors shown in an alert


def unusable_tabs(error: Any) -> list[str]:
    """The tabs a "settings are unusable (Copy, General); ..." error names; [] for any other error."""
    m = UNUSABLE.search(str(error or ""))
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
    tabs = list(dict.fromkeys(tabs))
    errors: list[Any] = []
    try:
        from us_outbound.settings import sync

        _, report = sync.load_current(ctx.store)
        tabs = tabs or [t for t, errs in report.items() if errs]
        errors = [e for t in tabs for e in report.get(t, [])]
    except Exception as exc:  # the alert still goes, with the tabs alone
        log("settings_fault_unread", error=str(exc)[:160])
    links = and_list(say.sheet_link(ctx, t) for t in tabs) or say.sheet_link(ctx)
    facts = [f"The settings sheet has errors on the {links}, so every job except the hourly health check refuses to start."]
    if errors:
        facts.append("The first error:" if len(errors) == 1 else f"The first {min(len(errors), FAULT_LINES)} errors:")
        facts += [f"   ◦ {esc(str(e))}" for e in errors[:FAULT_LINES]]
        if len(errors) > FAULT_LINES:
            facts.append(f"   ◦ …and {len(errors) - FAULT_LINES} more: the sync below lists every one in this channel.")
    steps = [f"Correct {('this' if len(errors) == 1 else 'these') if errors else 'the errors'} on the {links}. If the sheet already looks right, the "
             "copy saved earlier fails a newer check, and step 2 replaces it.",
             f"Run {say.command('sync')} to read the sheet now. Otherwise it is read {SYNC_TIMES}.",
             "The jobs start again by themselves at their next run."]
    return facts, steps


def _last_good(run: Mapping[str, Any], now: datetime) -> str:
    last = _last_healthy(run, now)
    return f"last good run {last.astimezone(UK):%a %d %b %H:%M} UK" if last else "no good run yet"


def missed_message(ctx: Context, missed: list[str], runs: Mapping[str, Mapping[str, Any]]) -> str:
    """The Slack text for the missed jobs (module docstring)."""
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
        notify.alert(ctx, missed_message(ctx, missed, runs))  # a Slack that fails is logged: the watchdog's ping goes
    log("heartbeat_check", missed=missed, newly_missed=new, alerted=bool(new or reminder))
    return missed


DAILY = 26 * _H  # a job expected this rarely (or rarer) waits hours for its next try


def failed_jobs(runs: Mapping[str, Mapping[str, Any]], jobs: Iterable[str], now: datetime) -> list[str]:
    """The jobs among these that run daily or less often and whose latest run failed in the last DAILY minutes:
    their next try is hours or days away. A job that runs every few minutes is tried again within the hour; if it
    keeps failing, it is missed. After a day a failed run no longer counts, so a monthly job's failure does not keep
    the outside watchdog failing (and blind to anything new) for a month; it is missed once its window passes."""
    out = []
    for j in jobs:
        run = runs.get(j) or {}
        started = utc_strict_or_none(run.get("started_at"))
        if (EXPECTED.get(j, 0) >= DAILY and run.get("status") == ERROR and started is not None
                and now - started <= timedelta(minutes=DAILY)):
            out.append(j)
    return out


def run(ctx: Context) -> dict:
    """The heartbeat_check job (JOB CONTRACT: run(ctx) -> summary): missed heartbeats, the errors the jobs caught,
    then the outside watchdog's ping, /fail when something needs a look (ops/watchdog.py)."""
    from us_outbound.ops import job_errors, watchdog

    jobs = scheduled_jobs()
    try:
        missed = check_heartbeats(ctx, jobs)
        runs = latest_runs(ctx.store)
    except GuardViolation:
        raise
    except Exception:  # the database, say: Healthchecks hears it at once, then the error is recorded
        watchdog.ping(ctx, ["heartbeat_check could not read the heartbeats"])
        raise
    failed = failed_jobs(runs, jobs, ctx.now)
    errors = job_errors.check(ctx, jobs)
    slack = watchdog.slack_problem(ctx)
    reasons = ([f"missed: {', '.join(missed)}"] if missed else []) + (
        [f"failed: {', '.join(failed)}"] if failed else []) + ([slack] if slack else [])
    ping = watchdog.ping(ctx, reasons)
    return {"missed": missed, "failed": failed, "job_errors": errors, "slack": slack or "ok", "watchdog": ping}

