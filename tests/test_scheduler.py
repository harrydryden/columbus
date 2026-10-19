"""The scheduler (ops/scheduler.py) and its table (ops/schedule.py): the cron matcher, the UK
clock changes of 2026, no overlap, the parallel limit, timeouts, SIGTERM, no backfill, the
live gate, and the table against the job registry and the heartbeat check."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from us_outbound.context import UK, ConfigError
from us_outbound.ops import cli
from us_outbound.ops import heartbeat as hb
from us_outbound.ops import schedule
from us_outbound.ops import scheduler as sch
from us_outbound.ops.schedule import SCHEDULE, ScheduledJob, by_name, enabled_names

MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)


def utc(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def job(name="j", cron="* * * * *", timeout=10, live=False) -> ScheduledJob:
    return ScheduledJob(name, cron, live=live, enabled=True, timeout_minutes=timeout, phase=0)


def runs(cron_text: str, start: datetime, end: datetime) -> list[datetime]:
    """The UTC minutes in [start, end) in which a job on this cron is due."""
    cron = sch.Cron.parse(cron_text)
    out, t = [], start
    while t < end:
        if sch.due(cron, t):
            out.append(t)
        t += MIN
    return out


def uk_times(ts) -> list[str]:
    return [t.astimezone(UK).strftime("%d %H:%M %Z") for t in ts]


class FakeClock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeProc:
    pids = iter(range(1000, 10**6))

    def __init__(self, clock: FakeClock, seconds: float | None, obeys_term: bool = True):
        self.clock, self.pid, self.obeys_term = clock, next(FakeProc.pids), obeys_term
        self.ends = clock() + timedelta(seconds=seconds) if seconds is not None else None
        self.code: int | None = None
        self.signals: list[tuple[str, datetime]] = []

    def poll(self):
        if self.code is None and self.ends is not None and self.clock() >= self.ends:
            self.code = 0
        return self.code

    def terminate(self):
        self.signals.append(("TERM", self.clock()))
        if self.obeys_term and self.code is None:
            self.code = -signal.SIGTERM

    def kill(self):
        self.signals.append(("KILL", self.clock()))
        if self.code is None:
            self.code = -signal.SIGKILL


class Launcher:
    """Starts FakeProcs and checks that no job is ever started while its last run is alive."""

    def __init__(self, clock: FakeClock, seconds: float | None = 30, obeys_term: bool = True):
        self.clock, self.seconds, self.obeys_term = clock, seconds, obeys_term
        self.started: list[tuple[str, datetime]] = []
        self.procs: dict[str, list[FakeProc]] = {}

    def __call__(self, j: ScheduledJob) -> FakeProc:
        assert all(p.poll() is not None for p in self.procs.get(j.name, [])), f"{j.name} overlapped"
        p = FakeProc(self.clock, self.seconds, self.obeys_term)
        self.started.append((j.name, self.clock()))
        self.procs.setdefault(j.name, []).append(p)
        return p

    def starts(self, name: str) -> list[datetime]:
        return [t for n, t in self.started if n == name]


def ends(text: str) -> list[dict]:
    """The scheduler_job_end log lines in the captured output, oldest first (the loop keeps no history of its own:
    W1-1)."""
    import json

    out = []
    for line in text.splitlines():
        if line.startswith("{") and '"scheduler_job_end"' in line:
            out.append(json.loads(line))
    return out


def drive(table, start: datetime, until: datetime, *, seconds=30, max_parallel=2, jump=None, at_start=()):
    """Run the real loop on a fake clock from start until `until`; jump=(at, by) stalls the loop once."""
    clock = FakeClock(start)
    launcher = Launcher(clock, seconds)
    s = sch.Scheduler(table, max_parallel=max_parallel, clock=clock, launch=launcher, at_start=at_start)
    jumped = []

    def sleep(seconds_: float) -> None:
        clock.sleep(seconds_)
        if jump and not jumped and clock.t >= jump[0]:
            jumped.append(clock.t)
            clock.t += jump[1]
        if clock.t >= until:
            s.request_stop()

    s.sleep = sleep
    assert s.run() == 0
    return s, launcher


# -- the cron matcher ------------------------------------------------------------------------------


@pytest.mark.parametrize("text, field, expected", [
    ("* * * * *", "minutes", set(range(60))),
    ("*/15 * * * *", "minutes", {0, 15, 30, 45}),
    ("0,30 * * * *", "minutes", {0, 30}),
    ("10-50/20 * * * *", "minutes", {10, 30, 50}),
    ("0 8-10,14 * * *", "hours", {8, 9, 10, 14}),
    ("0 */6 * * *", "hours", {0, 6, 12, 18}),
    ("0 0 1,15 * *", "days", {1, 15}),
    ("0 0 * 1-12/3 *", "months", {1, 4, 7, 10}),
    ("0 9 * * 1-5", "weekdays", {1, 2, 3, 4, 5}),
    ("0 9 * * 5-7", "weekdays", {5, 6, 0}),
    ("0 9 * * 0", "weekdays", {0}),
    ("0 9 * * 7", "weekdays", {0}),
])
def test_cron_fields(text, field, expected):
    assert getattr(sch.Cron.parse(text), field) == frozenset(expected)


@pytest.mark.parametrize("text", [
    "", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8",
    "5/15 * * * *", "*/0 * * * *", "10-5 * * * *", "a * * * *", "1, * * * *", "-1 * * * *", "1-2-3 * * * *",
    "MON * * * *",
])
def test_bad_crons_are_refused(text):
    with pytest.raises(ValueError, match="cron"):
        sch.Cron.parse(text)


def test_day_of_week_counts_from_sunday_zero_and_seven_is_sunday_too():
    sunday, monday = datetime(2026, 10, 4, 9, 0), datetime(2026, 10, 5, 9, 0)
    assert sch.Cron.parse("0 9 * * 0").matches(sunday) and sch.Cron.parse("0 9 * * 7").matches(sunday)
    assert not sch.Cron.parse("0 9 * * 0").matches(monday) and sch.Cron.parse("0 9 * * 1").matches(monday)
    weekdays = sch.Cron.parse("30 4 * * 1-5")  # verify_in_clay: Sat 3 Oct to Fri 9 Oct 2026
    assert [weekdays.matches(datetime(2026, 10, d, 4, 30)) for d in range(3, 10)] == [False, False] + [True] * 5
    assert not weekdays.matches(datetime(2026, 10, 5, 4, 31))


def test_day_of_month_or_day_of_week_when_both_are_restricted():
    cron = sch.Cron.parse("0 12 13 * 5")  # the 13th, or any Friday
    assert cron.matches(datetime(2026, 11, 13, 12, 0))  # Friday 13th
    assert cron.matches(datetime(2026, 10, 13, 12, 0))  # Tuesday 13th
    assert cron.matches(datetime(2026, 10, 16, 12, 0))  # Friday 16th
    assert not cron.matches(datetime(2026, 10, 14, 12, 0))  # Wednesday 14th
    both = sch.Cron.parse("0 12 */2 * 1")  # a field starting with * restricts together with the other
    assert both.matches(datetime(2026, 10, 5, 12, 0))  # Monday, odd day
    assert not both.matches(datetime(2026, 10, 12, 12, 0))  # Monday, even day
    assert not both.matches(datetime(2026, 10, 7, 12, 0))  # odd day, Wednesday
    monthly = sch.Cron.parse("0 3 1 * *")  # source_universe
    assert monthly.matches(datetime(2026, 11, 1, 3, 0)) and not monthly.matches(datetime(2026, 11, 2, 3, 0))


def test_due_reads_uk_time():
    daily = sch.Cron.parse("0 2 * * *")
    assert sch.due(daily, utc(2026, 9, 30, 1, 0))  # 02:00 BST
    assert not sch.due(daily, utc(2026, 9, 30, 2, 0))
    assert sch.due(daily, utc(2026, 12, 1, 2, 0))  # 02:00 GMT
    assert sch.due(daily, utc(2026, 12, 1, 2, 0, 42))  # any second of the minute
    enrol = sch.Cron.parse("0 12 * * 1-5")
    assert sch.due(enrol, utc(2026, 9, 29, 11, 0))  # Tue 12:00 UK, 07:00 ET (SPEC 9)
    with pytest.raises(ValueError):
        sch.due(daily, datetime(2026, 9, 30, 1, 0))  # naive


# -- the UK clock changes of 2026 ----------------------------------------------------------------------


def test_spring_forward_runs_a_skipped_fixed_time_once_at_0200():
    """29 Mar 2026: 01:00 GMT becomes 02:00 BST, so 01:00-01:59 UK never happens."""
    start, end = utc(2026, 3, 28, 0, 0), utc(2026, 3, 31, 0, 0)
    assert uk_times(runs("30 1 * * *", start, end)) == ["28 01:30 GMT", "29 02:00 BST", "30 01:30 BST"]  # suppression_load
    assert uk_times(runs("0 1 * * *", start, end)) == ["28 01:00 GMT", "29 02:00 BST", "30 01:00 BST"]  # a 01:00 daily job
    assert uk_times(runs("0 2 * * *", start, end)) == ["28 02:00 GMT", "29 02:00 BST", "30 02:00 BST"]  # settings_sync
    assert runs("0 2 * * *", start, end)[1] == utc(2026, 3, 29, 1, 0)
    assert uk_times(runs("0 7 * * *", start, end)) == ["28 07:00 GMT", "29 07:00 BST", "30 07:00 BST"]


def test_spring_forward_keeps_interval_jobs_in_real_time():
    night = (utc(2026, 3, 29, 0, 0), utc(2026, 3, 29, 3, 0))
    assert uk_times(runs("5 * * * *", *night)) == ["29 00:05 GMT", "29 02:05 BST", "29 03:05 BST"]  # heartbeat_check
    polls = runs("*/5 * * * *", *night)
    assert len(polls) == 36 and all(b - a == 5 * MIN for a, b in zip(polls, polls[1:]))


def test_fall_back_does_not_run_a_fixed_time_twice_in_the_repeated_hour():
    """25 Oct 2026: 02:00 BST becomes 01:00 GMT, so 01:00-01:59 UK happens twice."""
    start, end = utc(2026, 10, 24, 0, 0), utc(2026, 10, 27, 0, 0)
    assert uk_times(runs("30 1 * * *", start, end)) == ["24 01:30 BST", "25 01:30 BST", "26 01:30 GMT"]
    assert runs("30 1 * * *", start, end)[1] == utc(2026, 10, 25, 0, 30)  # the first pass
    assert uk_times(runs("0 1 * * *", start, end)) == ["24 01:00 BST", "25 01:00 BST", "26 01:00 GMT"]
    assert uk_times(runs("0 2 * * *", start, end)) == ["24 02:00 BST", "25 02:00 GMT", "26 02:00 GMT"]
    assert uk_times(runs("0 9 * * 1", start, end)) == ["26 09:00 GMT"]  # monday_readout


def test_fall_back_keeps_interval_jobs_running_through_both_passes():
    night = (utc(2026, 10, 25, 0, 0), utc(2026, 10, 25, 3, 0))
    assert uk_times(runs("5 * * * *", *night)) == ["25 01:05 BST", "25 01:05 GMT", "25 02:05 GMT"]
    polls = runs("*/5 * * * *", *night)
    assert len(polls) == 36 and all(b - a == 5 * MIN for a, b in zip(polls, polls[1:]))
    assert len(runs("0 * * * *", *night)) == 3  # kill_rules: hourly in real time


def fall_and_spring_table():
    t = by_name()
    return [t["settings_sync"], replace(t["poll_approvals"], enabled=True), t["heartbeat_check"], t["suppression_load"]]


def test_the_loop_on_the_fall_back_night(capsys):
    s, launcher = drive(fall_and_spring_table(), utc(2026, 10, 25, 0, 0), utc(2026, 10, 25, 3, 0))
    assert launcher.starts("suppression_load") == [utc(2026, 10, 25, 0, 30)]
    assert launcher.starts("settings_sync") == [utc(2026, 10, 25, 2, 0)]
    assert launcher.starts("heartbeat_check") == [utc(2026, 10, 25, h, 5) for h in (0, 1, 2)]
    assert len(launcher.starts("poll_approvals")) == 35  # 00:05 to 02:55; the start minute 00:00 is not run
    finished = ends(capsys.readouterr().out)
    assert finished and all(r["status"] == "ok" and r["exit_code"] == 0 for r in finished)


def test_the_loop_on_the_spring_forward_night_waits_for_a_free_slot():
    s, launcher = drive(fall_and_spring_table(), utc(2026, 3, 29, 0, 0), utc(2026, 3, 29, 3, 0))
    # 01:00 UTC is 02:00 BST: settings_sync, a poll and the skipped 01:30 suppression_load are all due.
    assert launcher.starts("settings_sync") == [utc(2026, 3, 29, 1, 0)]
    assert utc(2026, 3, 29, 1, 0) in launcher.starts("poll_approvals")
    assert launcher.starts("suppression_load") == [utc(2026, 3, 29, 1, 0, 30)]  # when the first run ended
    assert launcher.starts("heartbeat_check") == [utc(2026, 3, 29, h, 5) for h in (0, 1, 2)]


# -- overlap, the parallel limit, timeouts ------------------------------------------------------------


def test_a_job_still_running_is_skipped_not_started_twice(capsys):
    start = utc(2026, 10, 6, 9, 0)
    s, launcher = drive([job("slow")], start, start + 6 * MIN, seconds=150)  # each run 2.5 minutes
    assert launcher.starts("slow") == [start + MIN, start + 4 * MIN]
    out = capsys.readouterr().out
    skips = [line for line in out.splitlines() if "scheduler_job_skipped" in line]
    assert len(skips) == 3 and all("previous run still going" in line for line in skips)  # 09:02, 09:03, 09:05
    assert [r["status"] for r in ends(out)] == ["ok", "shutdown"]


def test_at_most_max_parallel_jobs_run_and_the_rest_wait_in_order():
    clock = FakeClock(utc(2026, 10, 6, 9, 0))
    launcher = Launcher(clock, seconds=None)  # runs until told
    jobs = [job(f"j{i}", "0 * * * *") for i in range(4)]
    s = sch.Scheduler(jobs, max_parallel=2, clock=clock, sleep=clock.sleep, launch=launcher)
    assert s.tick(clock()) == ["j0", "j1", "j2", "j3"]
    s.poll(clock())
    assert sorted(s.running) == ["j0", "j1"] and [j.name for j, _ in s.pending] == ["j2", "j3"]
    launcher.procs["j1"][0].code = 0
    clock.t += 10 * SEC
    s.poll(clock())
    assert sorted(s.running) == ["j0", "j2"] and [j.name for j, _ in s.pending] == ["j3"]
    # Due again: the running and the waiting are skipped; only j1 (finished) queues again.
    assert s.tick(utc(2026, 10, 6, 10, 0)) == ["j1"]
    assert [j.name for j, _ in s.pending] == ["j3", "j1"]
    assert [n for n, _ in launcher.started] == ["j0", "j1", "j2"]


def test_max_parallel_comes_from_the_environment():
    assert sch.max_parallel_from_env({}) == 2
    assert sch.max_parallel_from_env({"US_OUTBOUND_MAX_PARALLEL": " 3 "}) == 3
    for bad in ("0", "-1", "two", "1.5"):
        with pytest.raises(ConfigError, match="US_OUTBOUND_MAX_PARALLEL"):
            sch.max_parallel_from_env({"US_OUTBOUND_MAX_PARALLEL": bad})
    with pytest.raises(ValueError):
        sch.Scheduler([], max_parallel=0)


def _wait_until_reaped(s: sch.Scheduler, name: str, clock: FakeClock, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while name in s.running and time.monotonic() < deadline:
        time.sleep(0.02)
        s.poll(clock())


def test_a_run_past_its_timeout_is_terminated_real_subprocess(capsys):
    clock = FakeClock(utc(2026, 10, 6, 9, 0))
    procs = []

    def launch(j):
        procs.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]))
        return procs[-1]

    s = sch.Scheduler([job("slow", timeout=1)], clock=clock, sleep=clock.sleep, launch=launch)
    s.tick(clock())
    s.poll(clock())
    clock.t += 59 * SEC
    s.poll(clock())
    assert "slow" in s.running and procs[0].poll() is None  # not yet
    clock.t += SEC
    s.poll(clock())  # one minute: SIGTERM
    _wait_until_reaped(s, "slow", clock)
    assert "slow" not in s.running
    last = ends(capsys.readouterr().out)[-1]
    assert (last["job"], last["status"], last["exit_code"]) == ("slow", "timeout", -signal.SIGTERM)


def test_a_run_that_ignores_sigterm_is_killed_real_subprocess(capsys):
    clock = FakeClock(utc(2026, 10, 6, 9, 0))
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    procs = []

    def launch(j):
        procs.append(subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True))
        assert procs[-1].stdout.readline().strip() == "ready"  # SIGTERM is ignored from here on
        return procs[-1]

    s = sch.Scheduler([job("stubborn", timeout=1)], clock=clock, sleep=clock.sleep, launch=launch)
    s.tick(clock())
    s.poll(clock())
    clock.t += MIN
    s.poll(clock())  # SIGTERM, ignored
    time.sleep(0.2)
    s.poll(clock())
    assert "stubborn" in s.running
    clock.t += timedelta(seconds=sch.KILL_AFTER_SECONDS)
    s.poll(clock())  # SIGKILL
    _wait_until_reaped(s, "stubborn", clock)
    last = ends(capsys.readouterr().out)[-1]
    assert last["status"] == "timeout" and last["exit_code"] == -signal.SIGKILL
    procs[0].stdout.close()


def test_exit_codes_are_logged_with_their_meaning(capsys):
    clock = FakeClock(utc(2026, 10, 6, 9, 0))
    launcher = Launcher(clock, seconds=None)
    names = ["ok", "error", "refused", "blocked", "signal"]
    s = sch.Scheduler([job(n) for n in names], max_parallel=5, clock=clock, sleep=clock.sleep, launch=launcher)
    s.tick(clock())
    s.poll(clock())
    for name, code in zip(names, (0, 1, 2, 3, -9)):
        launcher.procs[name][0].code = code
    s.poll(clock())
    assert {r["job"]: r["status"] for r in ends(capsys.readouterr().out)} == {
        "ok": "ok", "error": "error", "refused": "refused", "blocked": "blocked", "signal": "killed by signal 9"}


def _beats(store, name):
    return [r for r in store.tables.get("heartbeats", []) if r["job"] == name]


def test_a_run_that_dies_before_its_row_and_one_killed_at_its_timeout_both_leave_an_error_row():
    """W1-1 (RS P0-4): a job that exited 1 before run_job wrote anything left no row, so status and heartbeat_check
    never saw it; one killed after ignoring SIGTERM left its row "running" for ever. Real subprocesses."""
    from tests.fakes import make_context
    from us_outbound.clients.db import MemoryStore
    from us_outbound.clients.guard import Guard
    from us_outbound.settings.model import General, Settings

    store = MemoryStore(Guard())
    clock = FakeClock(utc(2026, 10, 6, 9, 0))
    stubborn = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    procs = []

    def launch(j):
        if j.name == "enrol":  # an import error, say: exit 1 and no heartbeat row
            procs.append(subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"]))
        else:  # mailbox_health: run_job's "running" row, then a process that ignores SIGTERM
            store.upsert("heartbeats", [{"run_id": "mh-1", "job": j.name, "started_at": clock(), "finished_at": None,
                                         "status": "running", "dry_run": False, "detail": None, "error": None}])
            procs.append(subprocess.Popen([sys.executable, "-c", stubborn], stdout=subprocess.PIPE, text=True))
            assert procs[-1].stdout.readline().strip() == "ready"
        return procs[-1]

    s = sch.Scheduler([job("enrol"), job("mailbox_health", timeout=1)], clock=clock, sleep=clock.sleep, launch=launch,
                      store_factory=lambda: store)
    s.tick(clock())
    s.poll(clock())
    _wait_until_reaped(s, "enrol", clock)
    clock.t += MIN
    s.poll(clock())  # mailbox_health: SIGTERM at its 1-minute timeout, ignored
    clock.t += timedelta(seconds=sch.KILL_AFTER_SECONDS)
    s.poll(clock())  # SIGKILL
    _wait_until_reaped(s, "mailbox_health", clock)
    procs[1].stdout.close()

    [died] = _beats(store, "enrol")  # exactly one row, written by the loop
    assert died["status"] == "error" and died["detail"]["exit_code"] == 1 and died["error"] == "exited 1 (error)"
    assert died["started_at"] == utc(2026, 10, 6, 9, 0) and died["dry_run"] is None  # unknown: it died before it said
    [killed] = _beats(store, "mailbox_health")  # its own row, updated, not a second one
    assert killed["run_id"] == "mh-1" and killed["status"] == "error"
    assert killed["detail"]["exit_code"] == -signal.SIGKILL and killed["error"] == f"exited {-signal.SIGKILL} (timeout)"
    # Both now count as failing: each job's only run is an error, so heartbeat_check lists both as missed (before
    # the fix enrol had no row at all, and mailbox_health's "running" first run counted as alive).
    ctx = make_context(Settings(general=General()), store=store, now=utc(2026, 10, 6, 10, 0))
    assert sorted(hb.check_heartbeats(ctx, ["enrol", "mailbox_health"])) == ["enrol", "mailbox_health"]


def test_a_row_the_job_finished_itself_is_left_as_it_is():
    from us_outbound.clients.db import MemoryStore
    from us_outbound.clients.guard import Guard

    store = MemoryStore(Guard())
    start = utc(2026, 10, 6, 9, 0)
    store.upsert("heartbeats", [{"run_id": "r1", "job": "enrol", "started_at": start + SEC, "finished_at": start + MIN,
                                 "status": "error", "dry_run": False, "detail": None, "error": "Terminated: by SIGTERM"}])
    assert sch.record_failed_run(store, "enrol", start, start + MIN, 143, "terminated", 60.0) == "left"
    [row] = _beats(store, "enrol")
    assert row["error"] == "Terminated: by SIGTERM" and row["detail"] is None
    # An older run's row (started before this launch) is never taken for this one.
    assert sch.record_failed_run(store, "enrol", start + 2 * MIN, start + 3 * MIN, 1, "error", 60.0) == "written"
    assert len(_beats(store, "enrol")) == 2


def test_a_store_that_fails_inside_reap_never_stops_the_loop(capsys):
    start = utc(2026, 10, 6, 9, 0)
    clock = FakeClock(start)
    launcher = Launcher(clock, seconds=None)

    def broken_store():
        raise ConnectionError("Postgres is down")

    s = sch.Scheduler([job("flaky")], clock=clock, sleep=clock.sleep, launch=launcher, store_factory=broken_store)
    s.tick(clock())
    s.poll(clock())
    launcher.procs["flaky"][0].code = 1
    s.poll(clock())  # reaped: the heartbeat write fails, and is swallowed
    assert "flaky" not in s.running
    clock.t += MIN
    assert s.tick(clock()) == ["flaky"]  # the next tick runs
    s.poll(clock())
    assert len(launcher.starts("flaky")) == 2
    assert "scheduler_heartbeat_failed" in capsys.readouterr().out


def test_a_launch_failure_is_logged_and_the_others_still_start(capsys):
    clock = FakeClock(utc(2026, 10, 6, 9, 0))
    launcher = Launcher(clock)

    def launch(j):
        if j.name == "broken":
            raise OSError("no such file")
        return launcher(j)

    s = sch.Scheduler([job("broken"), job("fine")], clock=clock, sleep=clock.sleep, launch=launch)
    s.tick(clock())
    s.poll(clock())
    assert list(s.running) == ["fine"]
    assert "scheduler_launch_failed" in capsys.readouterr().out


# -- no backfill ----------------------------------------------------------------------------------------


def test_missed_minutes_are_not_backfilled_and_the_start_minute_is_not_run(capsys):
    start = utc(2026, 10, 6, 9, 0)
    s, launcher = drive([job("every")], start, start + 8 * MIN, jump=(start + 2 * MIN + 5 * SEC, 4 * MIN))  # 09:03-09:05 lost
    minutes = [t.replace(second=0) for t in launcher.starts("every")]
    assert minutes == [start + MIN, start + 2 * MIN, start + 6 * MIN, start + 7 * MIN]
    assert "scheduler_minutes_missed" in capsys.readouterr().out


def test_the_at_start_jobs_run_once_as_the_scheduler_starts_then_on_their_schedule():
    start = utc(2026, 10, 11, 20, 49, 30)  # a deploy: the stored settings are stale until the sheet is synced
    table = [job("settings_sync", cron="0 1 * * *"), job("every")]
    s, launcher = drive(table, start, start + 3 * MIN, at_start=("settings_sync",))
    assert launcher.starts("settings_sync") == [start]
    assert [t.replace(second=0) for t in launcher.starts("every")] == [start.replace(second=0) + MIN,
                                                                      start.replace(second=0) + 2 * MIN,
                                                                      start.replace(second=0) + 3 * MIN]


def test_at_start_names_only_jobs_the_table_runs():
    with pytest.raises(ValueError, match="nope"):
        sch.Scheduler([job("every")], at_start=("nope",))
    names = {j.name for j in SCHEDULE if j.enabled and j.cron}
    assert set(sch.AT_START) <= names
    sch.Scheduler(at_start=sch.AT_START)  # the worker's own call (ops/cli.py)


# -- SIGTERM and SIGINT -------------------------------------------------------------------------------


def _signal_after_first_start(s: sch.Scheduler, clock: FakeClock, launcher: Launcher, signum: int, sent: list):
    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if launcher.started and not sent:
            assert signal.getsignal(signum) == s.request_stop, "the scheduler's handler must be installed"
            sent.append(clock())
            os.kill(os.getpid(), signum)

    return sleep


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_launching_waits_then_terminates(signum, capsys):
    clock = FakeClock(utc(2026, 10, 6, 8, 59, 30))
    launcher = Launcher(clock, seconds=None)  # never ends by itself
    s = sch.Scheduler([job("a"), job("b")], max_parallel=1, clock=clock, launch=launcher)
    sent: list[datetime] = []
    s.sleep = _signal_after_first_start(s, clock, launcher, signum, sent)
    before = signal.getsignal(signum)
    assert s.run() == 0
    assert signal.getsignal(signum) is before  # the handler is put back
    assert [n for n, _ in launcher.started] == ["a"]  # b was waiting for the slot and never starts
    [(what, when)] = launcher.procs["a"][0].signals
    assert what == "TERM" and when - sent[0] >= timedelta(seconds=sch.GRACE_SECONDS)
    assert ends(capsys.readouterr().out)[-1]["status"] == "shutdown" and s.running == {} and s.pending == []


def test_a_run_that_ends_within_the_grace_period_is_left_alone(capsys):
    clock = FakeClock(utc(2026, 10, 6, 8, 59, 30))
    launcher = Launcher(clock, seconds=10)
    s = sch.Scheduler([job("a")], clock=clock, launch=launcher)
    sent: list[datetime] = []
    s.sleep = _signal_after_first_start(s, clock, launcher, signal.SIGTERM, sent)
    assert s.run() == 0
    assert launcher.procs["a"][0].signals == [] and ends(capsys.readouterr().out)[-1]["status"] == "ok"
    assert not hasattr(s, "history")  # the unread in-memory history is gone (W1-1): the log has every run


def test_a_run_that_ignores_the_shutdown_is_killed():
    clock = FakeClock(utc(2026, 10, 6, 8, 59, 30))
    launcher = Launcher(clock, seconds=None, obeys_term=False)
    s = sch.Scheduler([job("a")], clock=clock, launch=launcher)
    sent: list[datetime] = []
    s.sleep = _signal_after_first_start(s, clock, launcher, signal.SIGTERM, sent)
    assert s.run() == 0
    assert [w for w, _ in launcher.procs["a"][0].signals] == ["TERM", "KILL"]
    total = launcher.procs["a"][0].signals[-1][1] - sent[0]
    assert total <= timedelta(seconds=sch.GRACE_SECONDS + sch.KILL_AFTER_SECONDS + 2)  # inside Railway's 30 s


# -- the real launcher and the live gate -----------------------------------------------------------------


def test_the_real_launcher_runs_the_cli_in_its_own_process():
    j = job("no_such_job", live=True)
    assert sch.command(j) == [sys.executable, "-m", "us_outbound", "run", "no_such_job", "--live"]
    assert sch.command(by_name()["suppression_load"])[-2:] == ["run", "suppression_load"]
    proc = sch.launch(j)
    assert proc.wait(timeout=60) == 2  # refused (unknown job) before any context or database


def noop_job(ctx):
    return {"live": ctx.live}


@pytest.mark.parametrize("live_sending", [False, True])
def test_the_live_gate_is_unchanged(monkeypatch, live_sending):
    """What the scheduler starts is `run <job> [--live]`: --live still needs live_sending = yes (SPEC 0.3)."""
    from tests.test_cli import LIVE_SETTINGS, SETTINGS, Harness

    h = Harness(LIVE_SETTINGS if live_sending else SETTINGS)
    started = [j for j in SCHEDULE if j.enabled and j.cron]
    assert started
    for j in started:
        monkeypatch.setitem(cli.JOBS, j.name, "tests.test_scheduler:noop_job")
        argv = sch.command(j)
        assert argv[:4] == [sys.executable, "-m", "us_outbound", "run"]
        assert cli.main(argv[3:], context_factory=h) == 0
        assert h.last.job == j.name and h.last.live is (j.live and live_sending), j.name


# -- the table -------------------------------------------------------------------------------------------


SPEC9_CRONS = {
    # Build, 1 Oct 2026: source_universe and apollo_signals each weekday (SPEC 9: the 1st, and Mondays).
    "settings_sync": "0 2 * * *", "source_universe": "0 3 * * 1-5",
    "apollo_signals": "10 5 * * 1-5",  # W1-3a, 19 Oct 2026: after verify_accounts and apollo_enrich (03:30 before)
    "site_visits": "0 6 * * *", "public_signals": "0 4 * * 1", "verify_in_clay": "30 4 * * 1-5", "score": "",
    "pick_contacts": "30 5 * * 1-5", "enrol": "0 12 * * 1-5", "poll_replies": "*/15 * * * *",
    "poll_approvals": "*/5 * * * *", "hubspot_readback": "*/15 * * * *", "sync_outcomes": "7-59/15 * * * *",
    "mailbox_health": "0 7 * * *", "kill_rules": "0 * * * *",
    "daily_post": "0 9 * * 2-6",  # 19 Oct 2026: Tuesday to Saturday, each post the day before (learn/daily_post.py)
    "monday_readout": "30 8 * * 1",  # Harry, 6 Oct 2026: after the hand-check, before the daily post
    "blackout": "40 * * * *",  # Harry, 7 Oct 2026: hourly, the campaigns paused over the blackout dates
    # Build additions.
    "heartbeat_check": "5 * * * *", "suppression_load": "30 1 * * *", "verify_accounts": "30 4 * * 1-5",
    # The 1st of each month (Harry, 5 Oct 2026), after settings_sync (02:00) and before source_universe (03:00).
    "lookalikes": "30 2 1 * *",
    "lookalike_leads": "50 2 1 * *",  # Harry, 5 Oct 2026: the 1st of each month, after lookalikes (02:30)
    "hand_check_draw": "45 5 * * 1",  # W1-5, 19 Oct 2026: the draw, after pick_contacts (hand_check_post at 08:00)
    "read_pages": "45 3 * * 1-5",  # Harry, 2 Oct 2026: after source_universe, before verify_accounts (04:30)
    "apollo_enrich": "0 5 * * 1-5",  # W1-3a, 19 Oct 2026: after verify_accounts, for verified accounts (04:10 before)
    "apollo_people": "20 4 * * 1-5",  # Harry, 5 Oct 2026: after read_pages, before verify_accounts' rescore
    "retention": "40 0 * * *",  # SPEC 6 and 13 (SPEC 9 had the lead deletions in sync_outcomes at 01:00)
    "label_sample_monthly": "15 9 1 * *",  # Harry, 11 Oct 2026: the labels judged by hand, a batch a month
    "capture": "30 7 * * 1",  # W1-2, 19 Oct 2026: the reply read checked, Mondays before the readout
}
# The --live choices deploy/jobs.yaml had: every job that writes outside the database.
LIVE = {"settings_sync", "score", "enrol", "poll_replies", "poll_approvals", "hubspot_readback", "sync_outcomes",
        "mailbox_health", "kill_rules", "daily_post", "monday_readout", "heartbeat_check", "hand_check_draw",
        "blackout", "retention", "label_sample_monthly", "capture"}


def test_the_table_matches_the_job_registry_and_spec9():
    table = by_name()
    assert list(table) == list(cli.JOBS) and len(table) == len(SCHEDULE) - 1  # settings_sync has two rows
    # The second sync (4 Oct 2026): weekdays 11:30, so the morning's sheet edits are in force for enrol at 12:00.
    assert [j.cron for j in SCHEDULE if j.name == "settings_sync"] == ["0 2 * * *", "30 11 * * 1-5"]
    assert schedule.crons("settings_sync") == ["0 2 * * *", "30 11 * * 1-5"]
    assert schedule.crons("enrol") == ["0 12 * * 1-5"] and schedule.crons("score") == []
    assert {n: j.cron for n, j in table.items()} == SPEC9_CRONS
    assert {j.name for j in SCHEDULE if j.live} == LIVE
    for j in SCHEDULE:
        if j.cron:
            sch.Cron.parse(j.cron)
        # On when built, off when not built yet, and off when parked (built, off the schedule; W0, 19 Oct 2026).
        assert j.enabled is not (cli.JOBS[j.name].startswith("not built") or j.name in schedule.PARKED), j.name
        assert 1 <= j.timeout_minutes <= hb.OVERLAP_MINUTES, j.name
    assert table["poll_approvals"].timeout_minutes < 5  # done before it is due again


def test_the_morning_runs_source_verify_then_paid_enrichment_then_pick():
    """W1-3a (19 Oct 2026; code-sources B4): the paid enrichment of a company comes after the free checks and the
    label check have passed it, and before pick_contacts' rescore orders the queue. Each job starts after the one
    before it can have finished (its timeout), except pick_contacts, whose own rescore comes first: apollo_signals
    may still be reading postings then, and the rescore at 11:30 (settings_sync) scores what it adds before enrol."""
    table = by_name()

    def start(name):
        minute, hour = table[name].cron.split()[:2]
        return int(hour) * 60 + int(minute)

    assert start("source_universe") < start("verify_accounts") < start("apollo_enrich") < start("apollo_signals") \
        < start("pick_contacts") < start("enrol")
    # verify_accounts 04:30 + 30 minutes = 05:00, apollo_enrich's start; apollo_enrich 05:00 + 20 = 05:20 < 05:30.
    assert start("verify_accounts") + table["verify_accounts"].timeout_minutes <= start("apollo_enrich")
    assert start("apollo_enrich") + table["apollo_enrich"].timeout_minutes <= start("pick_contacts")
    assert {table[j].cron.split()[4] for j in ("apollo_enrich", "apollo_signals", "pick_contacts")} == {"1-5"}
    # Re-timed jobs are registered everywhere the scheduler, heartbeat_check and its alert read (standing rule).
    for j in ("apollo_enrich", "apollo_signals"):
        assert cli.JOBS[j].endswith(":run") and hb.EXPECTED[j] == 26 * 60 and j in hb.WEEKDAY_JOBS and j in hb.IMPACT


def test_enabled_jobs_are_the_ones_heartbeat_check_expects():
    enabled = enabled_names()
    assert enabled == ["settings_sync", "source_universe", "apollo_signals", "read_pages", "apollo_enrich",
                       "apollo_people", "site_visits", "verify_accounts", "pick_contacts", "enrol", "poll_replies", "poll_approvals", "hubspot_readback",
                       "sync_outcomes", "mailbox_health", "kill_rules", "blackout", "daily_post", "capture",
                       "monday_readout", "heartbeat_check",
                       "suppression_load", "lookalikes", "hand_check_draw", "retention", "label_sample_monthly"]
    # lookalike_leads is parked before its first run on Sun 1 Nov (W0, 19 Oct 2026; code-sources B6): it paid for
    # whole-US pages and kept only the active states. Not started, and not expected, so no false "missed" alert.
    assert "lookalike_leads" not in enabled and "lookalike_leads" not in hb.EXPECTED
    assert by_name()["lookalike_leads"].enabled is False and cli.JOBS["lookalike_leads"].endswith(":run")
    assert set(enabled) <= set(hb.EXPECTED)
    # score has no schedule of its own; a parked job is not expected
    assert set(hb.EXPECTED) == {j.name for j in SCHEDULE} - {"score"} - schedule.PARKED
    assert hb.scheduled_jobs() == [j for j in cli.built_jobs() if j in enabled]
    assert list(dict.fromkeys(j.name for j, _ in sch.Scheduler().jobs)) == enabled
    assert [j.name for j, _ in sch.Scheduler().jobs].count("settings_sync") == 2
    # capture (W1-2, 19 Oct 2026; RS §2 W1-2): registered everywhere a new job must be, in the same commit.
    assert cli.JOBS["capture"] == "us_outbound.replies.capture:run" and hb.EXPECTED["capture"] == 8 * 24 * 60
    assert by_name()["capture"].live is True and "capture" in hb.IMPACT


def test_the_hand_check_draw_replaces_hand_check_post_everywhere():
    """W1-5 (19 Oct 2026): hand_check_draw is registered everywhere hand_check_post was (cli.JOBS, heartbeat.EXPECTED,
    the job names Slack uses) and in IMPACT, and hand_check_post is gone from all of them. It runs Mondays at 05:45,
    after pick_contacts (05:30) and before enrol (12:00): 5 * 60 + 30 < 5 * 60 + 45 < 12 * 60."""
    from us_outbound.base import say

    table = by_name()
    assert table["hand_check_draw"].cron == "45 5 * * 1" and table["hand_check_draw"].live is True
    assert cli.JOBS["hand_check_draw"] == "us_outbound.enrol.hand_check:run"
    assert hb.EXPECTED["hand_check_draw"] == 8 * 24 * 60 and "hand_check_draw" in hb.IMPACT
    assert say.job_name("hand_check_draw") == "the weekly hand-check"
    for registry in (table, cli.JOBS, hb.EXPECTED, hb.IMPACT):
        assert "hand_check_post" not in registry
    minute, hour = table["pick_contacts"].cron.split()[:2]
    assert int(hour) * 60 + int(minute) == 5 * 60 + 30 < 5 * 60 + 45 < 12 * 60
    assert "hand_check_draw" not in hb.WEEKDAY_JOBS  # weekly: its 8 days are counted in wall time


def test_next_run_and_the_listing():
    daily = sch.Cron.parse("0 2 * * *")
    assert sch.next_run(daily, utc(2026, 9, 30, 12, 0)) == utc(2026, 10, 1, 1, 0)  # 02:00 BST
    assert sch.next_run(daily, utc(2026, 10, 25, 0, 30)) == utc(2026, 10, 25, 2, 0)  # 02:00 GMT
    assert sch.next_run(sch.Cron.parse("0 9 * * 1"), utc(2026, 9, 30, 12, 0)) == utc(2026, 10, 5, 8, 0)
    assert sch.next_run(sch.Cron.parse("0 0 31 2 *"), utc(2026, 1, 1, 0, 0), horizon=timedelta(days=40)) is None
    listing = sch.describe(now=utc(2026, 9, 30, 12, 0))
    lines = {line.split()[0]: line for line in listing}
    syncs = [line for line in listing if line.startswith("settings_sync ")]
    assert syncs[0].endswith("Thu 01 Oct 02:00 BST") and syncs[1].endswith("Thu 01 Oct 11:30 BST")
    assert lines["heartbeat_check"].endswith("Wed 30 Sep 13:05 BST")
    assert lines["monday_readout"].endswith("Mon 05 Oct 08:30 BST") and lines["score"].endswith("on demand only")
    assert lines["lookalikes"].endswith("Thu 01 Oct 02:30 BST")


def test_lookalikes_runs_on_the_1st_of_each_month_whatever_the_weekday():
    """Harry, 5 Oct 2026: monthly. Day-of-week is *, so cron's OR of the two day fields does not apply: the
    1st alone, a Sunday (1 Nov 2026) as much as a Friday (1 Jan 2027), and never on a Monday."""
    cron = sch.Cron.parse(by_name()["lookalikes"].cron)
    firsts = [datetime(2026, 11, 1, 2, 30), datetime(2026, 12, 1, 2, 30), datetime(2027, 1, 1, 2, 30)]
    assert all(cron.matches(d) for d in firsts)  # Sunday, Tuesday, Friday
    assert not cron.matches(datetime(2026, 10, 5, 2, 30))  # a Monday, the old weekly slot
    assert not cron.matches(datetime(2026, 11, 2, 2, 30)) and not cron.matches(datetime(2026, 11, 1, 3, 30))
    # 02:30 UK in BST and in GMT; after the 5 Oct run, the next is 1 Nov (after the clocks go back on 25 Oct).
    assert sch.next_run(cron, utc(2026, 9, 15, 12, 0)) == utc(2026, 10, 1, 1, 30)
    assert sch.next_run(cron, utc(2026, 10, 5, 1, 30)) == utc(2026, 11, 1, 2, 30)
    runs, t = [], utc(2026, 10, 5, 0, 0)
    for _ in range(4):
        t = sch.next_run(cron, t)
        runs.append(t.astimezone(UK).strftime("%d %b %Y %H:%M"))
    assert runs == ["01 Nov 2026 02:30", "01 Dec 2026 02:30", "01 Jan 2027 02:30", "01 Feb 2027 02:30"]
    # The longest gap between two runs (31 days) stays inside the heartbeat's expectation.
    assert 31 * 24 * 60 < hb.EXPECTED["lookalikes"] <= 32 * 24 * 60
