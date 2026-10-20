"""The scheduler (SPEC 3, 9): one always-on process that starts every job on its schedule.

It replaced Cloud Run Jobs and Cloud Scheduler (Harry, 30 Sep 2026). On Railway it is the
worker service's start command, `us-outbound scheduler` (the Dockerfile's default).

The loop:
  * wakes at each minute boundary and works out which jobs of ops/schedule.py are due in
    that minute, in Europe/London time (SPEC 9: all times are UK time);
  * starts each due job as its own process, `python -m us_outbound run <job> [--live]`, so
    a crash or a memory blow-up in one job never takes the scheduler down; the job's
    output goes to the same log;
  * never overlaps two runs of one job: a job still running, or still waiting for a free
    slot, when it is due again is skipped and logged;
  * runs at most US_OUTBOUND_MAX_PARALLEL jobs at once (default 2); a due job waits for a
    free slot, in schedule order;
  * stops a run that passes its timeout: SIGTERM, then SIGKILL 3 s later. On SIGTERM the
    job records its heartbeat as an error and exits 143 (ops/cli.py), so the next run is
    not blocked by a heartbeat left "running";
  * logs each start and finish with the exit code (0 ok, 1 error, 2 refused, 3 blocked by
    a guardrail, 143 stopped by SIGTERM; negative: killed by that signal);
  * on a non-zero exit, or a run stopped at its timeout, makes sure the run has an error row in
    heartbeats (W1-1, 19 Oct 2026): a job that died before run_job wrote anything (an import
    error, a crash building its context) left no row at all, and one killed after SIGTERM left its
    row "running", so neither showed in `status` as a failure. The row of that start still
    "running" is set to error; with none, one is written. It goes through a store opened for the
    write alone, with a 5 s connect timeout (heartbeat_store), and any failure is logged and
    swallowed: the loop never stops for a heartbeat. A row the job finished itself is left as it is;
  * runs nothing until it holds the scheduler lock (W2-4, 20 Oct 2026; RS P0-4), a Postgres session advisory
    lock (clients/db.SessionLock), so one scheduler runs: over a deploy the old and the new container are alive
    together for 4 to 13 s, and each scheduler kept only its own runs apart (code-platform-ops B5). The new one tries
    the lock every LOCK_POLL_SECONDS, logging each wait (scheduler_lock_wait), and takes over once the old one's
    SIGTERM ends its connection. Still waiting after LOCK_WAIT_SECONDS, it pings the outside watchdog's /fail and
    waits on: it never runs beside another, and never exits for want of the lock (a scheduler that exited left
    production with none). With Postgres out of reach it retries with backoff, and pings /fail after
    DB_DOWN_FAIL_SECONDS; never a silent exit. A session lock dies with its connection, so before each minute's
    tick the loop checks it still holds it, and takes it again (the same wait) before it starts anything more;
  * catches up (W2-4; code-platform-ops B7): when it starts, or takes the lock back, each job with a fixed time of
    day whose time fell in the last CATCH_UP_MINUTES with no heartbeat since is started once, for that minute.
    Every start from the table carries its due minute (`run <job> --due`), and the run claims the row
    "<job>:<due minute>" in heartbeats before it does anything (ops/health.run_job), so two starts for one minute
    (two schedulers, or a catch-up of a run the old container began) run once. Other minutes the loop missed are
    not run afterwards; heartbeat_check alerts on any job that misses its heartbeat (ops/health.py);
  * runs the jobs in AT_START once as it starts: settings_sync, so a deploy whose validation
    is stricter than the version in force brings the sheet in at once, instead of every job
    refusing until the next scheduled sync (11 Oct 2026: the definition check left the
    Industries version synced that morning unusable for the evening);
  * on SIGTERM or SIGINT (a Railway redeploy or restart) starts nothing more, waits up to
    25 s for running jobs, then terminates them. Railway must allow longer than that
    between SIGTERM and SIGKILL: RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30.

The UK clock changes (these are the classic Unix cron rules):
  * Spring (last Sunday of March; 29 Mar 2026): 01:00-01:59 does not happen. A job fixed
    at a time in that hour (suppression_load 01:30; sync_outcomes 01:00 from phase 2) runs
    once at 02:00 BST instead. Jobs with * in the minute or hour (the polls, the hourly
    checks) carry on at their real-time rhythm.
  * Autumn (last Sunday of October; 25 Oct 2026): 01:00-01:59 happens twice. A job fixed at
    a time in that hour runs on the first pass only; jobs with * in the minute or hour run
    on both passes.

Live is unchanged (SPEC 0.3): --live from the table still needs live_sending = yes, which
the job checks when it builds its context (ops/bootstrap.py).

Cron: five fields (minute, hour, day of month, month, day of week), each a comma list of
numbers, *, */n, a-b or a-b/n; day of week 0-6 with 0 (or 7) = Sunday. If both day of
month and day of week are restricted (neither starts with *), a day matching either counts.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from us_outbound.context import UK, ConfigError
from us_outbound.logs import log
from us_outbound.ops.schedule import SCHEDULE, ScheduledJob

MINUTE = timedelta(minutes=1)
MAX_PARALLEL_VAR = "US_OUTBOUND_MAX_PARALLEL"
DEFAULT_MAX_PARALLEL = 2
GRACE_SECONDS = 25.0  # after SIGTERM: how long running jobs get to finish
KILL_AFTER_SECONDS = 3.0  # after terminate(): how long before kill()
POLL_SECONDS = 1.0  # between checks for finished runs, timeouts and free slots
HORIZON = timedelta(days=366)  # how far next_run looks ahead
EXIT_MEANING = {0: "ok", 1: "error", 2: "refused", 3: "blocked", 143: "terminated"}
CONNECT_TIMEOUT_SECONDS = 5  # the heartbeat write after a failed run waits no longer than this for Postgres
STATEMENT_TIMEOUT_MS = 5000  # nor this long for a statement: a lock on heartbeats must not hold the loop
AT_START = ("settings_sync",)  # run once when the worker starts (module docstring)
# The scheduler lock (module docstring): one 64-bit key for pg_try_advisory_lock, fixed for good ("usoutbo" in hex).
SCHEDULER_KEY = 0x75736F7574626F
LOCK_POLL_SECONDS = 2.0  # between tries while another scheduler holds the lock
LOCK_WAIT_SECONDS = 180.0  # the old container's SIGTERM frees it within 4 to 13 s; past this the watchdog hears
LOCK_LOG_EVERY_SECONDS = 60.0  # how often a wait past LOCK_WAIT_SECONDS is logged
DB_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0, 30.0)  # between tries while Postgres is out of reach, then 30 s each
DB_DOWN_FAIL_SECONDS = 300.0  # Postgres out of reach this long at the lock: the watchdog's /fail
CATCH_UP_MINUTES = 90  # how far back a missed daily job is caught up (module docstring)

# -- cron -------------------------------------------------------------------------------

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))


def _number(text: str, what: str, cron: str) -> int:
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"cron {cron!r}: {what} {text!r} is not a number")
    return int(text)


def _field(text: str, what: str, lo: int, hi: int, cron: str) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        base, slash, step_text = item.partition("/")
        step = _number(step_text, what, cron) if slash else 1
        if step < 1:
            raise ValueError(f"cron {cron!r}: {what} step must be 1 or more")
        if base == "*":
            first, last = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            first, last = _number(a, what, cron), _number(b, what, cron)
        elif slash:
            raise ValueError(f"cron {cron!r}: {what} {item!r}: a step needs * or a range (a-b/n)")
        else:
            first = last = _number(base, what, cron)
        if not lo <= first <= last <= hi:
            raise ValueError(f"cron {cron!r}: {what} {item!r} must be within {lo}-{hi}, low to high")
        values.update(range(first, last + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    day_star: bool  # day of month starts with *
    weekday_star: bool  # day of week starts with *
    fixed: bool  # neither minute nor hour starts with *: a fixed time of day (the DST rules above)

    @classmethod
    def parse(cls, text: str) -> "Cron":
        parts = text.split()
        if len(parts) != 5:
            raise ValueError(f"cron {text!r}: expected 5 fields (minute hour day-of-month month day-of-week)")
        sets = [_field(p, what, lo, hi, text) for p, (what, lo, hi) in zip(parts, _FIELDS)]
        return cls(
            text=text, minutes=sets[0], hours=sets[1], days=sets[2], months=sets[3],
            weekdays=frozenset(d % 7 for d in sets[4]),  # 7 is Sunday too
            day_star=parts[2].startswith("*"), weekday_star=parts[4].startswith("*"),
            fixed=not (parts[0].startswith("*") or parts[1].startswith("*")),
        )

    def matches(self, wall: datetime) -> bool:
        """Whether this local wall-clock minute matches (tzinfo, if any, is ignored)."""
        if wall.minute not in self.minutes or wall.hour not in self.hours or wall.month not in self.months:
            return False
        dom = wall.day in self.days
        dow = (wall.weekday() + 1) % 7 in self.weekdays  # Python: Monday 0; cron: Sunday 0
        return (dom and dow) if (self.day_star or self.weekday_star) else (dom or dow)


def _floor(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def due(cron: Cron, minute: datetime) -> bool:
    """Whether a job on `cron` runs in the minute starting at `minute` (aware), by UK time."""
    if minute.tzinfo is None:
        raise ValueError("due() needs an aware datetime")
    minute = _floor(minute)
    wall = minute.astimezone(UK)
    if cron.matches(wall):
        return not (cron.fixed and wall.fold == 1)  # autumn: the repeated hour's second pass
    if not cron.fixed:
        return False
    # Spring: the first minute after the skipped hour takes the fixed times that fell in it.
    t = (minute - MINUTE).astimezone(UK).replace(tzinfo=None) + MINUTE
    here = wall.replace(tzinfo=None)
    while t < here:
        if cron.matches(t):
            return True
        t += MINUTE
    return False


def next_run(cron: Cron, after: datetime, horizon: timedelta = HORIZON) -> datetime | None:
    """The first minute after `after` in which the job is due (UTC), or None within the horizon."""
    t = _floor(after.astimezone(UTC)) + MINUTE
    end = t + horizon
    while t < end:
        if due(cron, t):
            return t
        t += MINUTE
    return None


# -- running jobs ------------------------------------------------------------------------


class Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def command(job: ScheduledJob, due: datetime | None = None) -> list[str]:
    """What the scheduler runs for a job: the same CLI entrypoint as by hand (SPEC 13), with the minute it was due
    (UTC), which the run claims (ops/health.run_job); a start at the scheduler's start (AT_START) has none."""
    when = ["--due", due.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")] if due is not None else []
    return [sys.executable, "-m", "us_outbound", "run", job.name, *(["--live"] if job.live else []), *when]


def launch(job: ScheduledJob, due: datetime | None = None) -> Process:
    # A session of its own: a Ctrl-C or a signal to the scheduler's process group does not
    # reach the jobs; the scheduler decides when they stop. Output goes to the same log.
    return subprocess.Popen(command(job, due), start_new_session=True)


def heartbeat_store(env: Mapping[str, str] | None = None) -> Any:
    """A store for one heartbeat write from the loop: DATABASE_URL, a CONNECT_TIMEOUT_SECONDS connect timeout and a
    STATEMENT_TIMEOUT_MS statement timeout (the loop is one thread: a row locked by a stuck job's transaction would
    otherwise hold every other job), closed by the caller after the write. Dry-run guard: database writes are allowed
    in every mode (clients/guard.py)."""
    from us_outbound.clients.db import PostgresStore
    from us_outbound.clients.guard import Guard
    from us_outbound.ops.bootstrap import database_url

    dsn = database_url(os.environ if env is None else env)
    return PostgresStore(Guard(live=False, job="scheduler"), dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS,
                         options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")


def scheduler_lock(env: Mapping[str, str] | None = None) -> Any:
    """The scheduler lock on DATABASE_URL (clients/db.SessionLock, SCHEDULER_KEY). Dry-run guard: it reads only."""
    from us_outbound.clients.db import SessionLock
    from us_outbound.clients.guard import Guard
    from us_outbound.ops.bootstrap import database_url

    dsn = database_url(os.environ if env is None else env)
    return SessionLock(Guard(live=False, job="scheduler"), dsn, SCHEDULER_KEY, connect_timeout=CONNECT_TIMEOUT_SECONDS)


def watchdog_fail(reason: str, env: Mapping[str, str] | None = None) -> dict:
    """The outside watchdog's /fail, from the environment alone (ops/health.ping_from_env). Never raises."""
    from us_outbound.ops import health

    return health.ping_from_env(reason, env=env)


def max_parallel_from_env(env: Mapping[str, str] | None = None) -> int:
    raw = ((os.environ if env is None else env).get(MAX_PARALLEL_VAR) or "").strip()
    if not raw:
        return DEFAULT_MAX_PARALLEL
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise ConfigError(f"{MAX_PARALLEL_VAR} must be a whole number, 1 or more")
    return int(raw)


@dataclass
class Run:
    job: ScheduledJob
    proc: Process
    started: datetime
    due_at: datetime
    claimed: bool = False  # started with --due: its row's run_id is the claim (ops/health.due_run_id)
    stop_reason: str | None = None  # "timeout" or "shutdown" once terminate() was sent
    stop_sent: datetime | None = None
    killed: bool = False


class Scheduler:
    """The loop (module docstring). clock, sleep and launch are injectable for tests."""

    def __init__(
        self,
        table: Iterable[ScheduledJob] = SCHEDULE,
        *,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        launch: Callable[..., Process] = launch,
        grace_seconds: float = GRACE_SECONDS,
        at_start: Iterable[str] = (),
        store_factory: Callable[[], Any] | None = None,
        lock: Any = None,
        fail: Callable[[str], Any] | None = None,
    ):
        """store_factory: opens the store a failed run's heartbeat is written through, and the catch-up reads
        (heartbeat_store in production, `us-outbound scheduler`); None writes and catches up nothing (tests of the loop
        alone). lock: the scheduler lock (scheduler_lock(); None runs without one, tests of the loop alone). fail: pings
        the outside watchdog's /fail with a reason (watchdog_fail)."""
        if max_parallel < 1:
            raise ValueError("max_parallel must be 1 or more")
        self.jobs = [(j, Cron.parse(j.cron)) for j in table if j.enabled and j.cron]
        self.max_parallel = max_parallel
        self.clock, self.sleep, self.launch = clock, sleep, launch
        self.grace_seconds = grace_seconds
        self.running: dict[str, Run] = {}
        self.pending: list[tuple[ScheduledJob, datetime, bool]] = []  # (job, due minute, start it with --due)
        self.store_factory = store_factory
        self.lock = lock
        self.fail = fail
        self.stopping = False
        unknown = set(at_start) - {j.name for j, _ in self.jobs}
        if unknown:
            raise ValueError(f"at_start names jobs the table does not run: {', '.join(sorted(unknown))}")
        self.at_start = tuple(at_start)

    # -- one minute, one poll --

    def tick(self, minute: datetime) -> list[str]:
        """Queue the jobs due in this minute; returns their names."""
        if self.stopping:
            return []
        queued = []
        for job, cron in self.jobs:
            if not due(cron, minute):
                continue
            if job.name in self.running:
                log("scheduler_job_skipped", job=job.name, reason="previous run still going",
                    running_since=self.running[job.name].started)
            elif any(j.name == job.name for j, _, _ in self.pending):
                log("scheduler_job_skipped", job=job.name, reason="still waiting for a free slot")
            else:
                self.pending.append((job, _floor(minute), True))
                queued.append(job.name)
        return queued

    # -- the lock and the catch-up --

    def _wait_step(self, seconds: float) -> None:
        """One wait while the lock is not held: running jobs are still collected and stopped at their timeout, but
        nothing new starts."""
        now = self.clock()
        self._reap(now)
        self._enforce_timeouts(now)
        self.sleep(seconds)

    def acquire_lock(self) -> bool:
        """Block until this scheduler holds the lock (module docstring); False if a stop came first. Never raises."""
        if self.lock is None:
            return True
        began = self.clock()
        tries, down_tries, told, logged_at = 0, 0, False, None
        while not self.stopping:
            now = self.clock()
            waited = (now - began).total_seconds()
            try:
                if self.lock.try_acquire():
                    log("scheduler_lock_held", waited_seconds=round(waited, 1), tries=tries + 1)
                    return True
                tries += 1
                down_tries = 0
                quiet = logged_at is not None and (now - logged_at).total_seconds() < LOCK_LOG_EVERY_SECONDS
                if waited < LOCK_WAIT_SECONDS or not quiet:
                    log("scheduler_lock_wait", waited_seconds=round(waited, 1), tries=tries,
                        reason="another scheduler holds the lock")
                    logged_at = now
                if waited >= LOCK_WAIT_SECONDS and not told:
                    told = True
                    self._tell(f"the scheduler has waited {int(waited)} s for its lock: another still holds it")
                pause = LOCK_POLL_SECONDS
            except Exception as exc:  # Postgres out of reach (LockUnavailable), or anything else: retry, never exit
                pause = DB_BACKOFF_SECONDS[min(down_tries, len(DB_BACKOFF_SECONDS) - 1)]
                down_tries += 1
                log("scheduler_lock_wait", waited_seconds=round(waited, 1), reason="database unreachable",
                    error=type(exc).__name__, retry_in_seconds=pause)
                if waited >= DB_DOWN_FAIL_SECONDS and not told:
                    told = True
                    self._tell(f"the scheduler cannot reach Postgres for its lock ({int(waited)} s)")
            self._wait_step(pause)
        return False

    def _tell(self, reason: str) -> None:
        log("scheduler_lock_alert", reason=reason)
        if self.fail is None:
            return
        try:
            self.fail(reason)
        except Exception as exc:  # the watchdog must never stop the loop
            log("scheduler_watchdog_failed", error=type(exc).__name__)

    def catch_up(self, now: datetime) -> list[str]:
        """Queue once each fixed-time job whose time fell in the last CATCH_UP_MINUTES with no heartbeat since that
        minute (module docstring). Never raises: a database it cannot read catches nothing up."""
        if self.store_factory is None:
            return []
        from us_outbound.clients.db import Range

        wanted = []
        current = _floor(now)
        for job, cron in self.jobs:
            if not cron.fixed or job.name in self.running:
                continue
            t = current
            while t > current - timedelta(minutes=CATCH_UP_MINUTES):
                if due(cron, t):
                    wanted.append((job, t))
                    break
                t -= MINUTE
        if not wanted:
            return []
        queued: list[str] = []
        store = None
        try:
            store = self.store_factory()
            for job, minute in wanted:
                if any(j.name == job.name for j, _, _ in self.pending):
                    continue  # waiting already (AT_START), or a job with two rows (settings_sync): once
                if store.select("heartbeats", {"job": job.name, "started_at": Range(lo=minute)}):
                    continue
                self.pending.append((job, minute, True))
                queued.append(job.name)
                log("scheduler_catch_up", job=job.name, due=minute)
        except Exception as exc:  # the database, say: nothing is caught up, and the loop goes on
            log("scheduler_catch_up_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        finally:
            try:
                if store is not None:
                    store.close()
            except Exception:
                pass
        return queued

    def poll(self, now: datetime) -> None:
        """Collect finished runs, stop runs past their timeout, and start waiting jobs in free slots."""
        self._reap(now)
        self._enforce_timeouts(now)
        if not self.stopping:
            self._start_pending(now)

    def _reap(self, now: datetime) -> None:
        for name, run in list(self.running.items()):
            code = run.proc.poll()
            if code is None:
                continue
            del self.running[name]
            status = run.stop_reason or EXIT_MEANING.get(code) or (f"killed by signal {-code}" if code < 0 else "error")
            seconds = round((now - run.started).total_seconds(), 1)
            log("scheduler_job_end", job=name, pid=run.proc.pid, exit_code=code, status=status, seconds=seconds)
            if code != 0 or run.stop_reason == "timeout":
                self._record_failure(name, run, now, code, status, seconds)

    def _record_failure(self, name: str, run: Run, now: datetime, code: int, status: str, seconds: float) -> None:
        """The failed run's heartbeat (health.record_failed_run). Never raises: the loop goes on whatever the database
        does."""
        from us_outbound.ops.health import due_run_id, record_failed_run

        if self.store_factory is None:
            return
        store = None
        try:
            store = self.store_factory()
            claim = due_run_id(name, run.due_at) if run.claimed else None
            outcome = record_failed_run(store, name, run.started, now, code, status, seconds, run_id=claim)
            log("scheduler_heartbeat_written", job=name, exit_code=code, outcome=outcome)
        except Exception as exc:  # Postgres down, say: the log line is the record
            log("scheduler_heartbeat_failed", job=name, exit_code=code, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        finally:
            try:
                if store is not None:
                    store.close()
            except Exception:
                pass

    def _stop(self, run: Run, now: datetime, reason: str) -> None:
        try:
            run.proc.terminate()
        except OSError:  # it has just ended; the next reap collects it
            pass
        run.stop_reason, run.stop_sent = reason, now

    def _enforce_timeouts(self, now: datetime) -> None:
        for run in self.running.values():
            if run.stop_reason is None and now >= run.started + timedelta(minutes=run.job.timeout_minutes):
                log("scheduler_job_timeout", job=run.job.name, pid=run.proc.pid, timeout_minutes=run.job.timeout_minutes)
                self._stop(run, now, "timeout")
            elif run.stop_sent and not run.killed and now - run.stop_sent >= timedelta(seconds=KILL_AFTER_SECONDS):
                try:
                    run.proc.kill()
                except OSError:
                    pass
                run.killed = True
                log("scheduler_job_killed", job=run.job.name, pid=run.proc.pid, reason=run.stop_reason)

    def _start_pending(self, now: datetime) -> None:
        while self.pending and len(self.running) < self.max_parallel:
            job, due_at, claimed = self.pending.pop(0)
            try:
                proc = self.launch(job, due_at if claimed else None)
            except OSError as exc:
                log("scheduler_launch_failed", job=job.name, error=f"{type(exc).__name__}: {exc}")
                continue
            self.running[job.name] = Run(job, proc, now, due_at, claimed)
            log("scheduler_job_start", job=job.name, pid=proc.pid, live_flag=job.live, due=due_at,
                waited_seconds=round((now - due_at).total_seconds()), timeout_minutes=job.timeout_minutes)

    # -- the loop --

    def request_stop(self, signum: int | None = None, frame: Any = None) -> None:
        """The SIGTERM / SIGINT handler: start nothing more; run() then shuts down."""
        if not self.stopping:
            name = signal.Signals(signum).name if signum else None
            log("scheduler_stopping", signal=name, running=sorted(self.running))
        self.stopping = True

    def run(self) -> int:
        """Loop until SIGTERM or SIGINT, then shut down; returns the exit code."""
        previous = self._install_signal_handlers()
        try:
            log("scheduler_start", jobs=[j.name for j, _ in self.jobs], max_parallel=self.max_parallel, pid=os.getpid())
            if not self.acquire_lock():
                self.shutdown()
                return 0
            start = self.clock()
            next_minute = _floor(start) + MINUTE  # the minute it starts in is caught up, not ticked
            by_name = {j.name: j for j, _ in self.jobs}
            self.pending.extend((by_name[name], start, False) for name in self.at_start)
            self.catch_up(start)
            while not self.stopping:
                now = self.clock()
                self._reap(now)  # a run that has ended is not "still going" when its job is due again
                if now >= next_minute:
                    if self.lock is not None and not self.lock.held():
                        log("scheduler_lock_lost", reason="the lock's connection dropped; taking it again")
                        if not self.acquire_lock():
                            break
                        now = self.clock()
                        self.catch_up(now)
                    minute = _floor(now)
                    if minute > next_minute:
                        log("scheduler_minutes_missed", first=next_minute, last=minute - MINUTE, backfilled=False)
                    self.tick(minute)
                    next_minute = minute + MINUTE
                self.poll(now)
                if self.stopping:
                    break
                self.sleep(min(POLL_SECONDS, max((next_minute - self.clock()).total_seconds(), 0.0)))
            self.shutdown()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 0

    def shutdown(self) -> None:
        """Start nothing more; give running jobs grace_seconds to finish, then terminate, then kill."""
        self.stopping = True
        if self.pending:
            log("scheduler_not_started", jobs=[j.name for j, _, _ in self.pending], reason="scheduler stopping")
            self.pending.clear()
        deadline = self.clock() + timedelta(seconds=self.grace_seconds)
        while self.running and self.clock() < deadline:
            self.poll(self.clock())
            if self.running:
                self.sleep(0.2)
        now = self.clock()
        for run in self.running.values():
            if run.stop_reason is None:
                log("scheduler_job_terminated", job=run.job.name, pid=run.proc.pid, reason="scheduler stopping")
                self._stop(run, now, "shutdown")
        kill_at = now + timedelta(seconds=KILL_AFTER_SECONDS)
        while self.running and self.clock() < kill_at:
            self._reap(self.clock())
            if self.running:
                self.sleep(0.1)
        for run in self.running.values():
            try:
                run.proc.kill()
            except OSError:
                pass
            log("scheduler_job_killed", job=run.job.name, pid=run.proc.pid, reason="scheduler stopping")
        self._reap(self.clock())
        if self.lock is not None:
            self.lock.close()  # the next scheduler takes over at once, not after the connection times out
        log("scheduler_stopped", still_running=sorted(self.running))

    def _install_signal_handlers(self) -> dict[int, Any]:
        if threading.current_thread() is not threading.main_thread():
            return {}  # signals can only be caught on the main thread (tests may run the loop elsewhere)
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, self.request_stop)
        return previous


# -- the listing ---------------------------------------------------------------------------


def describe(table: Iterable[ScheduledJob] = SCHEDULE, now: datetime | None = None) -> list[str]:
    """The schedule as printed by `us-outbound schedule`: one line per job, with its next run (UK)."""
    now = now or datetime.now(UTC)
    lines = [f"{'job':<18} {'schedule (UK)':<15} {'flag':<7} {'timeout':<8} next run (UK)"]
    for j in table:
        if not j.cron:
            when = "on demand only"
        elif not j.enabled:
            when = f"disabled until phase {j.phase}"
        else:
            nxt = next_run(Cron.parse(j.cron), now)
            when = nxt.astimezone(UK).strftime("%a %d %b %H:%M %Z") if nxt else "never"
        cron = j.cron or "-"
        flag = "--live" if j.live else ""
        lines.append(f"{j.name:<18} {cron:<15} {flag:<7} {str(j.timeout_minutes) + ' min':<8} {when}")
    return lines
