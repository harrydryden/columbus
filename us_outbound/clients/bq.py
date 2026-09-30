"""BigQuery dataset us_outbound: the system of record (SPEC 6).

Jobs talk to a Store. BigQueryStore is the real one; MemoryStore backs the tests and
local dry-runs. Both take and return plain dicts of Python values: JSON columns are
encoded and decoded here, so no job ever handles JSON text.
"""

from __future__ import annotations

import copy
import json
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any

from us_outbound.clients.guard import BQ_DATASET, Guard, GuardViolation, Op

# Primary keys used by upsert. None means append-only.
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
    "raw_irs_bmf": None,
    "raw_job_posts": None,
    "raw_clay_accounts": None,
    "raw_clay_contacts": None,
    "raw_site_visits": None,
    "raw_layoffs": None,
}

# Columns stored as JSON text in BigQuery (STRING columns holding JSON).
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


def new_id() -> str:
    return str(uuid.uuid4())


def _check_table(table: str) -> None:
    if table not in TABLE_KEYS:
        raise GuardViolation(f"unknown table {table!r}; add it to sql/ddl and TABLE_KEYS first")


class Store(ABC):
    dataset = BQ_DATASET

    def __init__(self, guard: Guard):
        self.guard = guard

    def _authorize(self, action: str, table: str, write: bool) -> None:
        _check_table(table)
        self.guard.authorize("bq", Op(action, target=f"{self.dataset}.{table}", write=write))

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
        """Read-only SQL against the dataset (views). Tests register handlers on MemoryStore."""

    def get(self, table: str, **key: Any) -> dict | None:
        rows = self.select(table, key)
        return rows[0] if rows else None


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
    """Dict-of-lists store with BigQuery's write rules enforced by the same guard."""

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


# -- BigQuery ----------------------------------------------------------------


def _to_bq(table: str, row: dict) -> dict:
    out = {}
    js = JSON_COLUMNS.get(table, frozenset())
    for k, v in row.items():
        if k in js and v is not None:
            v = json.dumps(v, default=str)
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        out[k] = v
    return out


def _from_bq(table: str, row: dict) -> dict:
    js = JSON_COLUMNS.get(table, frozenset())
    return {k: (json.loads(v) if k in js and isinstance(v, str) else v) for k, v in row.items()}


class BigQueryStore(Store):
    def __init__(self, guard: Guard, project: str, location: str, client: Any = None):
        super().__init__(guard)
        from google.cloud import bigquery

        self.bigquery = bigquery
        self.project = project
        self.client = client or bigquery.Client(project=project, location=location)

    def _ref(self, table: str) -> str:
        return f"`{self.project}.{self.dataset}.{table}`"

    def _param(self, name: str, value: Any):
        bq = self.bigquery
        if isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
            typ = _scalar_type(values[0]) if values else "STRING"
            return bq.ArrayQueryParameter(name, typ, values)
        return bq.ScalarQueryParameter(name, _scalar_type(value), value)

    def _where(self, where: Where | None, params: list) -> str:
        parts = []
        for i, (col, want) in enumerate((where or {}).items()):
            if not col.isidentifier():
                raise ValueError(f"bad column {col!r}")
            if callable(want):
                raise ValueError("BigQueryStore does not take callable filters; use query()")
            if want is None:
                parts.append(f"{col} IS NULL")
            elif isinstance(want, (list, tuple, set, frozenset)) and not want:
                parts.append("FALSE")  # IN an empty list; an empty ARRAY<STRING> would not fit a non-string column
            elif isinstance(want, (list, tuple, set, frozenset)):
                parts.append(f"{col} IN UNNEST(@w{i})")
                params.append(self._param(f"w{i}", want))
            else:
                parts.append(f"{col} = @w{i}")
                params.append(self._param(f"w{i}", want))
        return " AND ".join(parts) or "TRUE"

    def _run(self, sql: str, params: list | None = None) -> Any:
        cfg = self.bigquery.QueryJobConfig(query_parameters=params or [])
        return self.client.query(sql, job_config=cfg).result()

    def insert(self, table, rows):
        rows = [_to_bq(table, dict(r)) for r in rows]
        self._authorize("insert", table, write=True)
        if not rows:
            return 0
        cfg = self.bigquery.LoadJobConfig(
            source_format=self.bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=self.bigquery.WriteDisposition.WRITE_APPEND,
        )
        dest = f"{self.project}.{self.dataset}.{table}"
        self.client.load_table_from_json(rows, dest, job_config=cfg).result()
        return len(rows)

    def upsert(self, table, rows):
        rows = [_to_bq(table, dict(r)) for r in rows]
        self._authorize("upsert", table, write=True)
        key = TABLE_KEYS[table]
        if key is None:
            raise ValueError(f"{table} is append-only")
        # One MERGE per column set: a MERGE over the union of columns would set a column
        # that a row leaves out to NULL (MemoryStore keeps it), so rows are grouped.
        groups: dict[tuple[str, ...], list[dict]] = {}
        for r in rows:
            groups.setdefault(tuple(sorted(r)), []).append(r)
        for group in groups.values():
            self._merge(table, key, group)
        return len(rows)

    def _merge(self, table: str, key: tuple[str, ...], rows: list[dict]) -> None:
        # The staging table lives in the same dataset and expires within the hour.
        stage = f"_stage_{table}_{uuid.uuid4().hex[:12]}"
        self._run(
            f"CREATE TABLE `{self.project}.{self.dataset}.{stage}` LIKE {self._ref(table)} "
            f"OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR))"
        )
        try:
            cfg = self.bigquery.LoadJobConfig(
                source_format=self.bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                write_disposition=self.bigquery.WriteDisposition.WRITE_APPEND,
            )
            self.client.load_table_from_json(rows, f"{self.project}.{self.dataset}.{stage}", job_config=cfg).result()
            cols = sorted({c for r in rows for c in r})
            on = " AND ".join(f"T.{c} IS NOT DISTINCT FROM S.{c}" for c in key)
            sets = ", ".join(f"{c} = S.{c}" for c in cols if c not in key)
            self._run(
                f"MERGE {self._ref(table)} T USING `{self.project}.{self.dataset}.{stage}` S ON {on} "
                + (f"WHEN MATCHED THEN UPDATE SET {sets} " if sets else "")
                + f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) VALUES ({', '.join('S.' + c for c in cols)})"
            )
        finally:
            self.client.delete_table(f"{self.project}.{self.dataset}.{stage}", not_found_ok=True)

    def select(self, table, where=None):
        self._authorize("select", table, write=False)
        params: list = []
        sql = f"SELECT * FROM {self._ref(table)} WHERE {self._where(where, params)}"
        return [_from_bq(table, dict(r.items())) for r in self._run(sql, params)]

    def update(self, table, where, values):
        self._authorize("update", table, write=True)
        params: list = []
        cond = self._where(where, params)
        sets = []
        for i, (col, v) in enumerate(_to_bq(table, values).items()):
            if not col.isidentifier():
                raise ValueError(f"bad column {col!r}")
            if v is None:  # an untyped NULL fits any column; a NULL parameter would be a STRING
                sets.append(f"{col} = NULL")
                continue
            sets.append(f"{col} = @s{i}")
            params.append(self._param(f"s{i}", v))
        job = self._run(f"UPDATE {self._ref(table)} SET {', '.join(sets)} WHERE {cond}", params)
        return getattr(job, "num_dml_affected_rows", 0) or 0

    def delete(self, table, where):
        self._authorize("delete", table, write=True)
        params: list = []
        job = self._run(f"DELETE FROM {self._ref(table)} WHERE {self._where(where, params)}", params)
        return getattr(job, "num_dml_affected_rows", 0) or 0

    def query(self, sql, params=None):
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head not in {"SELECT", "WITH"}:
            raise GuardViolation("Store.query is read only; use insert/upsert/update/delete")
        self.guard.authorize("bq", Op("query", target=f"{self.dataset}.query", write=False))
        qp = [self._param(k, v) for k, v in (params or {}).items()]
        return [dict(r.items()) for r in self._run(sql, qp)]


def _scalar_type(v: Any) -> str:
    if isinstance(v, bool):
        return "BOOL"
    if isinstance(v, int):
        return "INT64"
    if isinstance(v, float):
        return "FLOAT64"
    if isinstance(v, datetime):
        return "TIMESTAMP"
    if isinstance(v, date):
        return "DATE"
    return "STRING"
