"""The database, schema us_outbound in Railway Postgres: the system of record (SPEC 6).

Jobs talk to a Store. PostgresStore is the real one; MemoryStore backs the tests and
local dry-runs. Both take and return plain dicts of Python values: JSON columns (jsonb)
take and return Python objects, and timestamps come back as aware UTC datetimes, so no
job ever handles JSON text or a naive time. The tables are created by ops/ddl.py from
sql/ddl; TABLE_KEYS and JSON_COLUMNS here are what that DDL must match (tests/test_sql.py).
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
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
}

Where = dict[str, Any]  # {col: value} equality; list/tuple/set value means IN; None means IS NULL
COLUMN_RE = re.compile(r"[a-z_][a-z0-9_]*")


def new_id() -> str:
    return str(uuid.uuid4())


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

    def get(self, table: str, **key: Any) -> dict | None:
        rows = self.select(table, key)
        return rows[0] if rows else None

    def close(self) -> None:
        """Release the connection, if the store holds one; the next call opens a new one."""


# -- in-memory ---------------------------------------------------------------


def _matches(row: dict, where: Where | None) -> bool:
    for col, want in (where or {}).items():
        have = row.get(col)
        if callable(want):
            if not want(have):
                return False
        elif isinstance(want, (list, tuple, set, frozenset)):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


class MemoryStore(Store):
    """Dict-of-lists store with the database's write rules enforced by the same guard."""

    def __init__(self, guard: Guard):
        super().__init__(guard)
        self.tables: dict[str, list[dict]] = {t: [] for t in TABLE_KEYS}
        self.query_handlers: dict[str, Callable[..., list[dict]]] = {}

    def insert(self, table, rows):
        rows = [copy.deepcopy(dict(r)) for r in rows]
        self._authorize("insert", table, write=True)
        self.tables[table].extend(rows)
        return len(rows)

    def upsert(self, table, rows):
        rows = [copy.deepcopy(dict(r)) for r in rows]
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
        return [copy.deepcopy(r) for r in self.tables[table] if _matches(r, where)]

    def update(self, table, where, values):
        self._authorize("update", table, write=True)
        n = 0
        for r in self.tables[table]:
            if _matches(r, where):
                r.update(copy.deepcopy(values))
                n += 1
        return n

    def delete(self, table, where):
        self._authorize("delete", table, write=True)
        keep = [r for r in self.tables[table] if not _matches(r, where)]
        n = len(self.tables[table]) - len(keep)
        self.tables[table] = keep
        return n

    def query(self, sql, params=None):
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


class PostgresStore(Store):
    """Schema us_outbound in the Postgres database at dsn.

    One connection, opened on first use, in autocommit with the session time zone UTC.
    Each method is one statement or one transaction. Table and column names are quoted
    identifiers, never text from the data; values are always bound parameters.
    """

    def __init__(self, guard: Guard, dsn: str, *, connect: Callable[[], Any] | None = None):
        """connect: a zero-argument callable returning a psycopg connection, used instead of
        psycopg.connect(dsn) (tests). The dsn carries the password: never log it."""
        super().__init__(guard)
        self.dsn = dsn
        self._connect = connect
        self._conn: Any = None

    def _connection(self) -> Any:
        if self._conn is None or self._conn.closed:
            conn = self._connect() if self._connect is not None else psycopg.connect(self.dsn)
            conn.autocommit = True
            conn.row_factory = dict_row
            conn.execute("SET TIME ZONE 'UTC'")  # timestamptz values load as UTC datetimes
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _table(self, table: str) -> Identifier:
        return Identifier(self.schema, table)

    def _value(self, table: str, col: str, v: Any) -> Any:
        if v is not None and col in JSON_COLUMNS.get(table, ()):
            return Jsonb(v, dumps=_dumps)
        return v

    def _where(self, table: str, where: Where | None) -> tuple[Composable, list]:
        """MemoryStore's where semantics: equality, IN a list (an empty list matches nothing), IS NULL."""
        parts: list[Composable] = []
        params: list = []
        for col, want in (where or {}).items():
            ident = Identifier(_column(col))
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
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                return conn.execute(sql, params, binary=True).fetchall()
        except psycopg.errors.ReadOnlySqlTransaction as exc:
            raise GuardViolation("Store.query is read only; it may not change data") from exc
