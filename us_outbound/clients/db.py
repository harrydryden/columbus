"""The database, schema us_outbound in Railway Postgres: the system of record (SPEC 6).

Jobs talk to a Store. PostgresStore is the real one; MemoryStore backs the tests and
local dry-runs. Both take and return plain dicts of Python values: JSON columns (jsonb)
take and return Python objects, and timestamps come back as aware UTC datetimes, so no
job ever handles JSON text or a naive time. The tables are created by ops/ddl.py from
sql/ddl; TABLE_KEYS, JSON_COLUMNS, TIMESTAMP_COLUMNS, DATE_COLUMNS and COLUMNS here are what that DDL
must match (tests/test_sql.py).

A run that must not be seen half done (the rescore: W2-2, 20 Oct 2026) writes inside store.transaction(), which
commits on leaving it and rolls back on an exception, and may take store.advisory_lock(key) inside it: held until
the transaction ends, so a second run asking for the same key waits for the first (up to wait_seconds, then
LockTimeout). PostgresStore uses pg_advisory_xact_lock; MemoryStore a process-wide lock per key and a copy of its
tables to roll back to.

MemoryStore stores a timestamp as Postgres does (9 Oct 2026): ISO text, a naive datetime
(UTC) and a date (UTC midnight) become an aware UTC datetime, and text that is no time
raises, as Postgres refuses it. A date column (DATE_COLUMNS) likewise keeps a date: an aware
datetime is taken on its UTC day, as the store's UTC session casts it, and text by its date. It refuses a column the table does not have, in a row or
a where. So a test cannot pass on a value or a filter that production would never see.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import groupby
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composable, Identifier, Placeholder
from psycopg.types.json import Jsonb

from us_outbound.clients.guard import DB_SCHEMA, Guard, GuardViolation, Op

# Primary keys used by upsert (ON CONFLICT). None means append-only.
TABLE_KEYS: dict[str, tuple[str, ...] | None] = {
    "accounts": ("account_id",),
    "contacts": ("contact_id",),
    "signal_events": ("event_id",),
    "events": ("event_id",),
    "suppression": ("email_sha256", "domain"),
    "settings": ("tab", "key", "effective_from"),
    # Added by the build (not in SPEC 6's table list; see README "Deviations").
    "heartbeats": ("run_id",),
    "credit_ledger": ("entry_id",),
    "hitl_items": ("item_id",),
    "domain_aliases": ("alias",),
    "partners": ("domain",),
    "lookalike_cells": ("cell_id",),  # sources/lookalikes.py (Harry, 1 Oct 2026)
    "lookalike_growth": ("cell_id",),  # sources/lookalikes.py: customers by growth band (Harry, 5 Oct 2026)
    "config_versions": ("config_version",),  # config_version.py: what a cohort was enrolled under (Harry, 7 Oct 2026)
    "config_log": ("log_id",),  # changes to what in-flight leads share (registry/mailboxes.py; Harry, 7 Oct 2026)
    "raw_irs_bmf": None,
    "raw_job_posts": None,
    "raw_clay_accounts": None,
    "raw_clay_contacts": None,
    "raw_site_visits": None,
    "raw_layoffs": None,
}

# Columns stored as jsonb.
JSON_COLUMNS: dict[str, frozenset[str]] = {
    "signal_events": frozenset({"value"}),
    "settings": frozenset({"values"}),
    "heartbeats": frozenset({"detail"}),
    "hitl_items": frozenset({"payload"}),
    "raw_clay_accounts": frozenset({"payload"}),
    "raw_clay_contacts": frozenset({"payload"}),
    "raw_irs_bmf": frozenset({"payload"}),
    "raw_job_posts": frozenset({"payload"}),
    "raw_site_visits": frozenset({"payload"}),
    "raw_layoffs": frozenset({"payload"}),
    "events": frozenset({"language_terms"}),
    "contacts": frozenset({"signals_at_enrol", "data_record",  # enrol._record_enrolled (5 Oct 2026)
                           "email_hashes"}),  # each step's rendered body, hashed (the W1-C contracts, 19 Oct 2026)
    "config_versions": frozenset({"step_days", "settings_versions", "copy_hashes", "general",  # 7 Oct 2026
                                  "copy_test"}),  # the running copy test (config_version.copy_test; 7 Oct 2026)
    "config_log": frozenset({"changed_keys", "detail"}),
}

# Columns stored as timestamptz (the date columns are DATE_COLUMNS).
TIMESTAMP_COLUMNS: dict[str, frozenset[str]] = {
    "accounts": frozenset({"label_checked_at", "clay_checked_at", "first_seen", "last_scored"}),
    "contacts": frozenset({"created_at", "last_step_at", "enrolled_at", "lead_deleted_at"}),
    "signal_events": frozenset({"observed_at"}),
    "events": frozenset({"occurred_at"}),
    "suppression": frozenset({"added_at", "expires_at"}),
    "settings": frozenset({"effective_from", "effective_to", "synced_at"}),
    "heartbeats": frozenset({"started_at", "finished_at"}),
    "credit_ledger": frozenset({"occurred_at"}),
    "hitl_items": frozenset({"created_at", "reposted_at", "escalated_at", "handled_at"}),
    "domain_aliases": frozenset({"added_at"}),
    "partners": frozenset({"added_at"}),
    "raw_irs_bmf": frozenset({"loaded_at"}),
    "raw_job_posts": frozenset({"loaded_at"}),
    "raw_clay_accounts": frozenset({"loaded_at"}),
    "raw_clay_contacts": frozenset({"loaded_at"}),
    "raw_site_visits": frozenset({"loaded_at"}),
    "raw_layoffs": frozenset({"loaded_at"}),
    "lookalike_cells": frozenset({"computed_at"}),
    "lookalike_growth": frozenset({"computed_at"}),
    "config_versions": frozenset({"first_seen"}),
    "config_log": frozenset({"changed_at"}),
}

# Columns stored as date (the W1-C contracts commit, 19 Oct 2026): a re-check falls due on a day, not at an instant.
DATE_COLUMNS: dict[str, frozenset[str]] = {
    "accounts": frozenset({"recheck_after"}),
}

# Every table's columns: MemoryStore refuses any other, in a row or a where, as Postgres does.
RAW_COLUMNS = frozenset({"loaded_at", "run_id", "key", "payload"})
COLUMNS: dict[str, frozenset[str]] = {
    "accounts": frozenset({
        "account_id", "domain", "clean_name", "legal_name", "apollo_org_id", "hq_city", "hq_state", "hq_country",
        "industry", "industry_group", "label_source", "label_confidence", "label_checked_at", "naics", "employees",
        "us_employees", "size_band", "founded_year", "source", "score", "tier", "tier_reason", "angle", "sender",
        "status", "clay_checked_at", "clay_credits_used", "hubspot_company_id", "first_seen", "last_scored",
        "status_reason", "arm", "recheck_after"}),
    "contacts": frozenset({
        "contact_id", "account_id", "role", "title", "first_name", "last_name", "email", "email_sha256", "email_status",
        "email_source", "person_state", "enrolment_month", "angle", "copy_version", "test_id", "mailbox",
        "instantly_campaign", "instantly_lead_id", "hubspot_contact_id", "suppressed", "suppressed_reason",
        "created_at", "last_step_at", "enrolled_at", "opener_arm", "opener_source", "signals_at_enrol",
        "score_at_enrol", "tier_at_enrol", "data_record", "subject_arm", "contact_slot", "config_version", "code_sha",
        "copy_hash", "lead_deleted_at", "test_arm", "arm", "sham_arm", "industry_at_enrol", "industry_group_at_enrol",
        "size_band_at_enrol", "copy_level", "email1_subject", "email_hashes"}),
    "signal_events": frozenset({
        "event_id", "account_id", "source", "fact", "value", "quote", "source_url", "observed_at"}),
    "events": frozenset({
        "event_id", "contact_id", "account_id", "type", "step", "mailbox", "reply_class", "reply_text",
        "language_terms", "competitor_named", "approval", "approved_by", "occurred_at", "source"}),
    "suppression": frozenset({"email_sha256", "domain", "reason", "source", "added_at", "expires_at"}),
    "settings": frozenset({"tab", "key", "values", "effective_from", "effective_to", "synced_at"}),
    "heartbeats": frozenset({"run_id", "job", "started_at", "finished_at", "status", "dry_run", "detail", "error"}),
    "credit_ledger": frozenset({
        "entry_id", "system", "job", "run_id", "account_id", "credits", "usd", "occurred_at", "note"}),
    "hitl_items": frozenset({
        "item_id", "kind", "account_id", "contact_id", "event_id", "slack_channel", "slack_ts", "payload", "status",
        "created_at", "reposted_at", "escalated_at", "handled_at", "handled_by"}),
    "domain_aliases": frozenset({"alias", "root_domain", "source", "added_at"}),
    "partners": frozenset({"domain", "name", "reason", "naics", "added_at"}),
    "raw_irs_bmf": RAW_COLUMNS,
    "raw_job_posts": RAW_COLUMNS,
    "raw_clay_accounts": RAW_COLUMNS,
    "raw_clay_contacts": RAW_COLUMNS,
    "raw_site_visits": RAW_COLUMNS,
    "raw_layoffs": RAW_COLUMNS,
    "lookalike_cells": frozenset({
        "cell_id", "industry_label", "industry_group", "size_band", "active_customers", "churned_customers",
        "us_active", "us_churned", "strength", "computed_at", "run_id"}),
    "lookalike_growth": frozenset({
        "cell_id", "industry_group", "growth_band", "active_customers", "churned_customers", "strength", "computed_at",
        "run_id"}),
    "config_versions": frozenset({
        "config_version", "first_seen", "code_sha", "campaign_fingerprint", "signature_hash", "step_days",
        "settings_versions", "copy_hashes", "general", "labels_hash", "run_id", "copy_test"}),
    "config_log": frozenset({
        "log_id", "changed_at", "kind", "campaign", "changed_keys", "detail", "leads_in_flight", "code_sha",
        "changed_by", "run_id"}),
}

Where = dict[str, Any]  # {col: value} equality; a list, tuple or set: IN; None: IS NULL; Range: lo <= col < hi
COLUMN_RE = re.compile(r"[a-z_][a-z0-9_]*")


@dataclass(frozen=True)
class Range:
    """A where value: lo <= column < hi, either end None for no bound; a NULL column never matches.

    9 Oct 2026: the ledger's readers (budget.spent_in, clients/claude.month_spend_usd) read every row a system
    ever had to sum one month; now the database leaves the other months out. Timestamps compare as instants: a
    naive one is taken as UTC, and MemoryStore reads an ISO text value as the time it names.
    """

    lo: Any = None
    hi: Any = None

    def holds(self, have: Any) -> bool:
        lo, hi = self.lo, self.hi
        if isinstance(lo, datetime) or isinstance(hi, datetime):
            have, lo, hi = _instant(have), _instant(lo), _instant(hi)
        if have is None:
            return False
        return (lo is None or have >= lo) and (hi is None or have < hi)


def _instant(v: Any) -> datetime | None:
    """v as an aware datetime (naive: UTC; ISO text: parsed), else None."""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return None


def new_id() -> str:
    return str(uuid.uuid4())


class LockTimeout(Exception):
    """advisory_lock waited wait_seconds and another run still holds the key."""


IN_TRANSACTION_QUERY = "Store.query is not run inside store.transaction(): read with select there"


def _check_table(table: str) -> None:
    if table not in TABLE_KEYS:
        raise GuardViolation(f"unknown table {table!r}; add it to sql/ddl and TABLE_KEYS first")


class Store(ABC):
    schema = DB_SCHEMA

    def __init__(self, guard: Guard):
        self.guard = guard

    def _authorize(self, action: str, table: str, write: bool) -> None:
        _check_table(table)
        self.guard.authorize("db", Op(action, target=f"{self.schema}.{table}", write=write))

    @abstractmethod
    def insert(self, table: str, rows: Iterable[dict]) -> int: ...

    @abstractmethod
    def upsert(self, table: str, rows: Iterable[dict]) -> int:
        """Insert rows, replacing any existing row with the same primary key (TABLE_KEYS)."""

    @abstractmethod
    def select(self, table: str, where: Where | None = None) -> list[dict]: ...

    @abstractmethod
    def update(self, table: str, where: Where, values: dict) -> int: ...

    @abstractmethod
    def delete(self, table: str, where: Where) -> int: ...

    @abstractmethod
    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Read-only SQL against the schema (views), params bound to %(name)s placeholders.

        Tests register handlers on MemoryStore.
        """

    @abstractmethod
    def transaction(self) -> Any:
        """A context manager: the writes inside it commit together on leaving it, or none on an exception. One
        inside another is a savepoint of it. query() is refused inside it: on Postgres its SET TRANSACTION READ ONLY
        would outlast its savepoint and fail every later write in the transaction."""

    @abstractmethod
    def advisory_lock(self, key: int, *, wait_seconds: float = 60.0) -> None:
        """Take the lock on key until the transaction it is taken in ends, waiting at most wait_seconds for a run
        that holds it (then LockTimeout). Only inside transaction()."""

    def _lock_op(self) -> None:
        self.guard.authorize("db", Op("advisory_lock", target=f"{self.schema}.advisory_lock", write=False))

    def get(self, table: str, **key: Any) -> dict | None:
        rows = self.select(table, key)
        return rows[0] if rows else None

    def latest(self, table: str, column: str, where: Where | None = None) -> dict | None:
        """The matching row whose `column` is greatest (NULLs left out), or None.

        One row, not the whole table: PostgresStore asks for it with ORDER BY ... LIMIT 1, so a job
        that only needs its last run's heartbeat does not read every run it ever had.
        """
        rows = [r for r in self.select(table, where) if r.get(column) is not None]
        return max(rows, key=lambda r: r[column]) if rows else None

    def close(self) -> None:
        """Release the connection, if the store holds one; the next call opens a new one."""


# -- in-memory ---------------------------------------------------------------


def _matches(row: dict, where: Where | None) -> bool:
    for col, want in (where or {}).items():
        have = row.get(col)
        if isinstance(want, Range):
            if not want.holds(have):
                return False
        elif callable(want):
            if not want(have):
                return False
        elif isinstance(want, (list, tuple, set, frozenset)):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


def _timestamp(v: Any) -> Any:
    """A timestamptz value as Postgres stores and returns it: None, or an aware UTC datetime. Text that is no time
    raises (timeparse.utc_strict), blank text included, as Postgres refuses '' for a timestamptz."""
    from us_outbound.timeparse import utc_strict  # timeparse imports context, which imports this module

    return None if v is None else utc_strict(v).astimezone(UTC)


def _date(v: Any) -> Any:
    """A date value as Postgres stores and returns it: None, or a date. An aware datetime is taken on its UTC day (the
    store's session time zone is UTC), a naive one on its own day, and text by its leading YYYY-MM-DD; any other text,
    blank included, raises, as Postgres refuses it for a date."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v.astimezone(UTC) if v.tzinfo else v).date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v.strip()[:10])  # ValueError on blank or no date, as Postgres refuses it
    raise TypeError(f"not a date: {v!r}")


def _known(table: str, cols: Iterable[str]) -> None:
    """Refuse a column the table does not have, as Postgres does (an unknown table is the guard's to refuse)."""
    have = COLUMNS.get(table)
    if have is not None and (unknown := sorted(set(cols) - have)):
        raise ValueError(f"{table} has no column {', '.join(map(repr, unknown))} (sql/ddl)")


def _stored(table: str, row: Mapping[str, Any]) -> dict:
    """A copy of row as Postgres would store it: its columns checked, its timestamptz columns converted."""
    _known(table, row)
    times, days = TIMESTAMP_COLUMNS.get(table, frozenset()), DATE_COLUMNS.get(table, frozenset())
    return {c: _timestamp(v) if c in times else _date(v) if c in days else v
            for c, v in copy.deepcopy(dict(row)).items()}


class MemoryStore(Store):
    """Dict-of-lists store with the database's write rules enforced by the same guard.

    transaction() keeps a copy of the tables to roll back to, per thread; advisory_lock() takes a process-wide lock
    per key (LOCKS), held until the thread's outermost transaction ends, and the copy is taken again once the lock is
    held, so a run that waited for another rolls back to what that one committed, never past it. Writes made in the
    transaction before the lock was taken are not rolled back (the rescore takes it first)."""

    LOCKS: dict[int, threading.Lock] = {}
    _LOCKS_GUARD = threading.Lock()

    def __init__(self, guard: Guard):
        super().__init__(guard)
        self.tables: dict[str, list[dict]] = {t: [] for t in TABLE_KEYS}
        self.query_handlers: dict[str, Callable[..., list[dict]]] = {}
        self._local = threading.local()

    def _state(self) -> Any:
        st = self._local
        if not hasattr(st, "saved"):
            st.saved, st.held = [], []
        return st

    @contextmanager
    def transaction(self) -> Iterator[None]:
        st = self._state()
        st.saved.append(copy.deepcopy(self.tables))
        try:
            yield
        except BaseException:
            self.tables = st.saved[-1]
            raise
        finally:
            st.saved.pop()
            if not st.saved:
                while st.held:
                    st.held.pop().release()

    def advisory_lock(self, key: int, *, wait_seconds: float = 60.0) -> None:
        st = self._state()
        if not st.saved:
            raise RuntimeError("advisory_lock is taken inside store.transaction()")
        self._lock_op()
        with self._LOCKS_GUARD:
            lock = self.LOCKS.setdefault(key, threading.Lock())
        if not lock.acquire(timeout=max(0.0, wait_seconds)):
            raise LockTimeout(f"advisory lock {key} is held by another run")
        st.held.append(lock)
        st.saved[0] = copy.deepcopy(self.tables)

    def _where(self, table: str, where: Where | None) -> Where:
        """where with its columns checked against the table and a timestamptz column's values stored as Postgres
        compares them."""
        _known(table, where or {})
        times, days = TIMESTAMP_COLUMNS.get(table, frozenset()), DATE_COLUMNS.get(table, frozenset())
        out: Where = {}
        for col, want in (where or {}).items():
            as_stored = _timestamp if col in times else _date if col in days else None
            if as_stored is not None and isinstance(want, Range):
                want = Range(as_stored(want.lo), as_stored(want.hi))  # an open end stays open
            elif as_stored is not None and not callable(want):
                many = isinstance(want, (list, tuple, set, frozenset))
                want = [as_stored(v) for v in want] if many else as_stored(want)
            out[col] = want
        return out

    def insert(self, table, rows):
        rows = [_stored(table, r) for r in rows]
        self._authorize("insert", table, write=True)
        self.tables[table].extend(rows)
        return len(rows)

    def upsert(self, table, rows):
        rows = [_stored(table, r) for r in rows]
        self._authorize("upsert", table, write=True)
        key = TABLE_KEYS[table]
        if key is None:
            raise ValueError(f"{table} is append-only")
        existing = self.tables[table]
        for r in rows:
            k = tuple(r.get(c) for c in key)
            for i, old in enumerate(existing):
                if tuple(old.get(c) for c in key) == k:
                    existing[i] = {**old, **r}
                    break
            else:
                existing.append(r)
        return len(rows)

    def select(self, table, where=None):
        self._authorize("select", table, write=False)
        where = self._where(table, where)
        return [copy.deepcopy(r) for r in self.tables[table] if _matches(r, where)]

    def update(self, table, where, values):
        self._authorize("update", table, write=True)
        where, values = self._where(table, where), _stored(table, values)
        n = 0
        for r in self.tables[table]:
            if _matches(r, where):
                r.update(copy.deepcopy(values))
                n += 1
        return n

    def delete(self, table, where):
        self._authorize("delete", table, write=True)
        where = self._where(table, where)
        keep = [r for r in self.tables[table] if not _matches(r, where)]
        n = len(self.tables[table]) - len(keep)
        self.tables[table] = keep
        return n

    def query(self, sql, params=None):
        if self._state().saved:
            raise RuntimeError(IN_TRANSACTION_QUERY)
        for name, handler in self.query_handlers.items():
            if name in sql:
                return handler(self, params or {})
        raise NotImplementedError(f"MemoryStore has no handler for query: {sql[:80]}")


# -- Postgres ----------------------------------------------------------------


def _column(col: Any) -> str:
    if not isinstance(col, str) or not COLUMN_RE.fullmatch(col):
        raise ValueError(f"bad column {col!r}")
    return col


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)  # a datetime inside a payload is stored as its text


def _no_nul(v: Any) -> Any:
    """v without NUL characters, which Postgres stores in no text or jsonb value (8 Oct 2026: a home page holding
    one failed read_pages). Dropped at the one place every write passes, so no source can fail a job with one."""
    if isinstance(v, str):
        return v.replace("\x00", "") if "\x00" in v else v
    if isinstance(v, Mapping):
        return {_no_nul(k): _no_nul(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_no_nul(x) for x in v]
    return v


class PostgresStore(Store):
    """Schema us_outbound in the Postgres database at dsn.

    One connection, opened on first use, in autocommit with the session time zone UTC.
    Each method is one statement or one transaction. Table and column names are quoted
    identifiers, never text from the data; values are always bound parameters.
    """

    def __init__(self, guard: Guard, dsn: str, *, connect: Callable[[], Any] | None = None,
                 connect_timeout: int | None = None, options: str | None = None):
        """connect: a zero-argument callable returning a psycopg connection, used instead of
        psycopg.connect(dsn) (tests). connect_timeout: seconds to wait for the server (the scheduler's
        heartbeat write after a failed run, ops/scheduler.py); None waits as libpq does. options: libpq's
        options string, such as "-c statement_timeout=5000" (the same store). The dsn carries the password: never
        log it."""
        super().__init__(guard)
        self.dsn = dsn
        self._connect = connect
        self._connect_kwargs: dict[str, Any] = {"connect_timeout": connect_timeout} if connect_timeout else {}
        if options:
            self._connect_kwargs["options"] = options
        self._conn: Any = None

    def _connection(self) -> Any:
        if self._conn is None or self._conn.closed:
            conn = self._connect() if self._connect is not None else psycopg.connect(self.dsn, **self._connect_kwargs)
            conn.autocommit = True
            conn.row_factory = dict_row
            conn.execute("SET TIME ZONE 'UTC'")  # timestamptz values load as UTC datetimes
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """psycopg's: BEGIN, COMMIT on leaving, ROLLBACK on an exception; the one-statement transactions of the
        methods inside it become savepoints of it."""
        with self._connection().transaction():
            yield

    def advisory_lock(self, key: int, *, wait_seconds: float = 60.0) -> None:
        """pg_advisory_xact_lock(key) with lock_timeout wait_seconds for this statement only (restored after), so a
        held key raises LockTimeout rather than waiting for ever; released at COMMIT or ROLLBACK."""
        conn = self._connection()
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
            raise RuntimeError("advisory_lock is taken inside store.transaction()")
        self._lock_op()
        before = conn.execute("SELECT current_setting('lock_timeout') AS t").fetchone()["t"]
        conn.execute("SELECT set_config('lock_timeout', %s, true)", [f"{max(1, int(wait_seconds * 1000))}ms"])
        try:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", [key])
        except psycopg.errors.LockNotAvailable as exc:
            raise LockTimeout(f"advisory lock {key} is held by another run") from exc
        conn.execute("SELECT set_config('lock_timeout', %s, true)", [before])

    def _table(self, table: str) -> Identifier:
        return Identifier(self.schema, table)

    def _value(self, table: str, col: str, v: Any) -> Any:
        if v is not None and col in JSON_COLUMNS.get(table, ()):
            return Jsonb(_no_nul(v), dumps=_dumps)
        return v.replace("\x00", "") if isinstance(v, str) and "\x00" in v else v

    def _where(self, table: str, where: Where | None) -> tuple[Composable, list]:
        """MemoryStore's where semantics: equality, IN a list (an empty list matches nothing), IS NULL."""
        parts: list[Composable] = []
        params: list = []
        for col, want in (where or {}).items():
            ident = Identifier(_column(col))
            if isinstance(want, Range):
                bounds = [(op, v) for op, v in ((">=", want.lo), ("<", want.hi)) if v is not None]
                parts.append(SQL(" AND ").join(SQL("{} " + op + " %s").format(ident) for op, _ in bounds)
                             if bounds else SQL("{} IS NOT NULL").format(ident))
                params.extend(self._value(table, col, v) for _, v in bounds)
                continue
            if callable(want):
                raise ValueError("PostgresStore does not take callable filters; use query()")
            if want is None:
                parts.append(SQL("{} IS NULL").format(ident))
            elif isinstance(want, (list, tuple, set, frozenset)):
                terms: list[Composable] = []
                values = [self._value(table, col, v) for v in want if v is not None]
                if values:
                    terms.append(SQL("{} = ANY(%s)").format(ident))
                    params.append(values)
                if any(v is None for v in want):
                    terms.append(SQL("{} IS NULL").format(ident))
                parts.append(SQL("({})").format(SQL(" OR ").join(terms)) if terms else SQL("FALSE"))
            else:
                parts.append(SQL("{} = %s").format(ident))
                params.append(self._value(table, col, want))
        return (SQL(" AND ").join(parts) if parts else SQL("TRUE")), params

    def _insert_sql(self, table: str, cols: list[str]) -> Composable:
        return SQL("INSERT INTO {} ({}) VALUES ({})").format(
            self._table(table),
            SQL(", ").join(Identifier(_column(c)) for c in cols),
            SQL(", ").join([Placeholder()] * len(cols)),
        )

    def _row(self, table: str, cols: list[str], row: dict) -> list:
        return [self._value(table, c, row.get(c)) for c in cols]

    def insert(self, table, rows):
        rows = [dict(r) for r in rows]
        self._authorize("insert", table, write=True)
        if not rows:
            return 0
        cols = sorted({c for r in rows for c in r})  # a column a row leaves out is NULL, as in MemoryStore
        query = self._insert_sql(table, cols)
        conn = self._connection()
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(query, [self._row(table, cols, r) for r in rows])
        return len(rows)

    def upsert(self, table, rows):
        rows = [dict(r) for r in rows]
        self._authorize("upsert", table, write=True)
        key = TABLE_KEYS[table]
        if key is None:
            raise ValueError(f"{table} is append-only")
        if not rows:
            return 0
        # A row sets only the columns it carries (MemoryStore keeps the rest), so each run of
        # rows with the same columns gets its own statement, in the rows' order.
        batches = [
            (self._upsert_sql(table, key, cols), [self._row(table, cols, r) for r in run])
            for cols, run in groupby(rows, key=lambda r: sorted(set(r) | set(key)))
        ]
        conn = self._connection()
        with conn.transaction(), conn.cursor() as cur:
            for query, params in batches:
                cur.executemany(query, params)
        return len(rows)

    def _upsert_sql(self, table: str, key: tuple[str, ...], cols: list[str]) -> Composable:
        sets = [SQL("{0} = EXCLUDED.{0}").format(Identifier(c)) for c in cols if c not in key]
        action = SQL("DO UPDATE SET {}").format(SQL(", ").join(sets)) if sets else SQL("DO NOTHING")
        # suppression's key is UNIQUE NULLS NOT DISTINCT (sql/ddl), so a NULL part still conflicts.
        return SQL("{} ON CONFLICT ({}) {}").format(
            self._insert_sql(table, cols), SQL(", ").join(Identifier(c) for c in key), action
        )

    def select(self, table, where=None):
        self._authorize("select", table, write=False)
        cond, params = self._where(table, where)
        query = SQL("SELECT * FROM {} WHERE {}").format(self._table(table), cond)
        return self._connection().execute(query, params).fetchall()

    def latest(self, table, column, where=None):
        ident = Identifier(_column(column))
        self._authorize("select", table, write=False)
        cond, params = self._where(table, where)
        query = SQL("SELECT * FROM {} WHERE {} AND {} IS NOT NULL ORDER BY {} DESC LIMIT 1").format(
            self._table(table), cond, ident, ident
        )
        return self._connection().execute(query, params).fetchone()

    def update(self, table, where, values):
        self._authorize("update", table, write=True)
        cond, params = self._where(table, where)
        if not values:  # nothing to set: count the matches, as MemoryStore does
            query = SQL("SELECT count(*) AS n FROM {} WHERE {}").format(self._table(table), cond)
            return self._connection().execute(query, params).fetchone()["n"]
        sets = SQL(", ").join(SQL("{} = %s").format(Identifier(_column(c))) for c in values)
        query = SQL("UPDATE {} SET {} WHERE {}").format(self._table(table), sets, cond)
        set_params = [self._value(table, c, v) for c, v in values.items()]
        return self._connection().execute(query, set_params + params).rowcount

    def delete(self, table, where):
        self._authorize("delete", table, write=True)
        cond, params = self._where(table, where)
        query = SQL("DELETE FROM {} WHERE {}").format(self._table(table), cond)
        return self._connection().execute(query, params).rowcount

    def query(self, sql, params=None):
        """SELECT or WITH only, run in a READ ONLY transaction, so a data-modifying CTE fails too.

        It goes over the extended protocol (binary results), which takes one statement, so no
        COMMIT inside the text can end the READ ONLY transaction early.
        """
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head not in {"SELECT", "WITH"}:
            raise GuardViolation("Store.query is read only; use insert/upsert/update/delete")
        self.guard.authorize("db", Op("query", target=f"{self.schema}.query", write=False))
        conn = self._connection()
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS:
            raise RuntimeError(IN_TRANSACTION_QUERY)
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                return conn.execute(sql, params, binary=True).fetchall()
        except psycopg.errors.ReadOnlySqlTransaction as exc:
            raise GuardViolation("Store.query is read only; it may not change data") from exc
