"""The heartbeats table read back (SPEC 9, 13 Health): each job's latest run, and the operator stop.

ops/heartbeat.py writes the rows (run_job, one per run) and runs the heartbeat_check job on what latest_runs
reads here. A run shows it is alive when it finished ok, or was skipped for a reason of its own (a blackout
date); a skip behind a run still going (OVERLAP_REASON) is no sign of life.

The stop and start commands also write rows here (jobs operator_stop / operator_start): enrolment_paused()
reads them; enrol.operator_pause() checks it before enrolling. Moved here from ops/heartbeat.py (9 Oct 2026,
the refactoring scan's phase 6), so enrolment and the registry read them without importing ops.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.timeparse import utc_strict_or_none

TABLE = "heartbeats"
RUNNING, OK, ERROR, SKIPPED = "running", "ok", "error", "skipped"
OVERLAP_REASON = "previous run still going"  # a skip behind a running (or dead) run: no sign of life
OPERATOR_STOP, OPERATOR_START = "operator_stop", "operator_start"

LATEST_SQL = (
    "SELECT job, run_id, status, started_at, finished_at, error, last_ok_at, last_alive_at, runs"
    " FROM {schema}.v_heartbeats"
)


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
    last_stop = max(stops, key=lambda r: utc_strict_or_none(r.get("started_at")) or datetime.min.replace(tzinfo=UTC))
    if starts:
        last_start = max(utc_strict_or_none(r.get("started_at")) or datetime.min.replace(tzinfo=UTC) for r in starts)
        if last_start > (utc_strict_or_none(last_stop.get("started_at")) or datetime.min.replace(tzinfo=UTC)):
            return None
    return last_stop
