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
  * does not backfill: minutes the loop missed (a restart, a redeploy, a stall) are not run
    afterwards, and a job due in the minute the scheduler starts waits for its next time.
    heartbeat_check alerts on any job that misses its heartbeat (ops/heartbeat.py);
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
from collections import deque
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
HISTORY = 500  # finished runs kept in memory (the log has them all)
EXIT_MEANING = {0: "ok", 1: "error", 2: "refused", 3: "blocked", 143: "terminated"}

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


def command(job: ScheduledJob) -> list[str]:
    """What the scheduler runs for a job: the same CLI entrypoint as by hand (SPEC 13)."""
    return [sys.executable, "-m", "us_outbound", "run", job.name, *(["--live"] if job.live else [])]


def launch(job: ScheduledJob) -> Process:
    # A session of its own: a Ctrl-C or a signal to the scheduler's process group does not
    # reach the jobs; the scheduler decides when they stop. Output goes to the same log.
    return subprocess.Popen(command(job), start_new_session=True)


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
        launch: Callable[[ScheduledJob], Process] = launch,
        grace_seconds: float = GRACE_SECONDS,
    ):
        if max_parallel < 1:
            raise ValueError("max_parallel must be 1 or more")
        self.jobs = [(j, Cron.parse(j.cron)) for j in table if j.enabled and j.cron]
        self.max_parallel = max_parallel
        self.clock, self.sleep, self.launch = clock, sleep, launch
        self.grace_seconds = grace_seconds
        self.running: dict[str, Run] = {}
        self.pending: list[tuple[ScheduledJob, datetime]] = []
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY)  # the latest finished runs, oldest first
        self.stopping = False

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
            elif any(j.name == job.name for j, _ in self.pending):
                log("scheduler_job_skipped", job=job.name, reason="still waiting for a free slot")
            else:
                self.pending.append((job, _floor(minute)))
                queued.append(job.name)
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
            self.history.append({"job": name, "exit_code": code, "status": status, "started": run.started, "seconds": seconds})

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
            job, due_at = self.pending.pop(0)
            try:
                proc = self.launch(job)
            except OSError as exc:
                log("scheduler_launch_failed", job=job.name, error=f"{type(exc).__name__}: {exc}")
                continue
            self.running[job.name] = Run(job, proc, now, due_at)
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
            next_minute = _floor(self.clock()) + MINUTE  # the minute it starts in is not run (no backfill)
            while not self.stopping:
                now = self.clock()
                self._reap(now)  # a run that has ended is not "still going" when its job is due again
                if now >= next_minute:
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
            log("scheduler_not_started", jobs=[j.name for j, _ in self.pending], reason="scheduler stopping")
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
