"""The DDL and views for schema us_outbound in the Postgres database (SPEC 6), and apply().

sql/ddl/NN_<table>.sql creates the schema and then one table per file, with its indexes and
comments; sql/views/NN_<view>.sql creates the views, numbered so a view comes after the
views it reads. A file holds one or more statements, each ending in a semicolon.

statements() checks every statement before any runs (SPEC 1.2, for the database: write only to schema
us_outbound): each must start with one of the allowed forms (W2-4, 20 Oct 2026: the check is the prefix, and the
object it names; the regex firewall that also read every FROM and JOIN of our own view files went, as
tests/test_sql.py parses every view with sqlglot and refuses any name outside the schema):
    CREATE SCHEMA IF NOT EXISTS us_outbound
    CREATE TABLE IF NOT EXISTS us_outbound.<table> (...)
    CREATE [UNIQUE] INDEX IF NOT EXISTS <name> ON us_outbound.<table> (...)
    CREATE OR REPLACE VIEW us_outbound.<view> AS ...
    ALTER TABLE us_outbound.<table> ADD COLUMN IF NOT EXISTS <column> <type>
    DROP VIEW IF EXISTS us_outbound.<view>
    COMMENT ON SCHEMA | TABLE | VIEW | COLUMN us_outbound[.<object>[.<column>]] IS '...'
The ALTER form only adds a column (a type, no default, nothing dropped); the DROP form removes a view that was
replaced, without CASCADE. Neither can touch stored rows. One statement at a time; comments and string literals are
ignored, and escape strings (E'...') and dollar quoting are refused, since the split cannot read them.

apply() runs every statement in one transaction, each after
guard.authorize("db", Op("ddl", target="us_outbound.<object>", write=True)), with SET lock_timeout = '5s' first
(W2-4; code-platform-ops B10): the pre-deploy runs against a live worker, and a statement that would wait for a lock
fails the deploy instead of queueing every query on the table behind it. An ADD COLUMN whose column
information_schema already has is skipped: ALTER TABLE takes an ACCESS EXCLUSIVE lock even when the column exists,
and every column the DDL adds is in production already but the newest. Tables and indexes are IF NOT EXISTS, so
applying again changes no table. Every view is dropped and created again (dependents first), because CREATE OR
REPLACE VIEW cannot change a view's columns; readers never see the gap, as the whole apply is one transaction.
Dry-run (the default) returns the statements and connects to nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from us_outbound.clients.guard import DB_SCHEMA, Guard, GuardViolation, Op
from us_outbound.logs import log

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
SCHEMA_TARGET = f"{DB_SCHEMA}.schema"  # the guard target of CREATE SCHEMA and COMMENT ON SCHEMA

IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_]*)'
NAME = rf"{IDENT}(?:\s*\.\s*{IDENT})*"
LOCK_TIMEOUT = "SET lock_timeout = '5s'"
COLUMNS_SQL = ("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s")
# The allowed forms, each matched from the statement's start (the module docstring).
FORMS = {
    "schema": re.compile(rf"CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+(?P<name>{NAME})$", re.I),
    "table": re.compile(rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?P<name>{NAME})\s*\(", re.I),
    "index": re.compile(rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+{IDENT}\s+ON\s+(?P<name>{NAME})\s*\(", re.I),
    "view": re.compile(rf"CREATE\s+OR\s+REPLACE\s+VIEW\s+(?P<name>{NAME})\s+AS\s", re.I),
    "add_column": re.compile(
        rf"ALTER\s+TABLE\s+(?P<name>{NAME})\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(?P<column>{IDENT})"
        rf"\s+[A-Za-z][A-Za-z ]*(?:\[\])?$", re.I),
    "drop_view": re.compile(rf"DROP\s+VIEW\s+IF\s+EXISTS\s+(?P<name>{NAME})$", re.I),
    "comment": re.compile(rf"COMMENT\s+ON\s+(?P<on>SCHEMA|TABLE|VIEW|COLUMN)\s+(?P<name>{NAME})\s+IS\s+''$", re.I),
}
SPECIAL_RE = re.compile(r"""--|/\*|['"$;]""")


def files(root: Path = SQL_DIR) -> list[Path]:
    """The DDL files in order, then the view files in order."""
    return sorted((root / "ddl").glob("*.sql")) + sorted((root / "views").glob("*.sql"))


def _segments(sql: str) -> Iterator[tuple[str, str]]:
    """sql in pieces of kind code, end (a semicolon), comment, string or name (a quoted name)."""
    i, n = 0, len(sql)
    while i < n:
        m = SPECIAL_RE.search(sql, i)
        if m is None:
            yield "code", sql[i:]
            return
        if m.start() > i:
            yield "code", sql[i : m.start()]
        i, tok = m.start(), m.group()
        if tok == ";":
            yield "end", tok
            i += 1
        elif tok == "$":
            raise GuardViolation("dollar quoting and $ parameters are not allowed in DDL")
        elif tok == "--":
            j = sql.find("\n", i)
            j = n if j < 0 else j
            yield "comment", sql[i:j]
            i = j
        elif tok == "/*":
            j = sql.find("*/", i + 2)
            if j < 0:
                raise GuardViolation("cannot check a statement with an unterminated comment")
            yield "comment", sql[i : j + 2]
            i = j + 2
        else:  # ' starts a string, " a quoted name; a doubled quote stands for itself
            if tok == "'" and re.search(r"(?<![\w$])[Ee]$", sql[:i]):
                raise GuardViolation("escape strings (E'...') are not allowed in DDL")
            j = i + 1
            while (j := sql.find(tok, j)) >= 0 and sql.startswith(tok * 2, j):
                j += 2
            if j < 0:
                raise GuardViolation("cannot check a statement with an unterminated string or quoted name")
            yield ("string" if tok == "'" else "name"), sql[i : j + 1]
            i = j + 1


def _code_only(sql: str) -> str:
    """sql with comments dropped and string literals emptied; quoted names kept."""
    blank = {"comment": " ", "string": "''"}
    return "".join(blank.get(kind, text) for kind, text in _segments(sql))


def split(sql: str) -> list[str]:
    """The statements of one file, without their semicolons. A comment goes with the statement after it."""
    out, cur = [], []
    for kind, text in _segments(sql):
        if kind == "end":
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(text)
    out.append("".join(cur).strip())
    return [s for s in out if _code_only(s).strip()]


def _parts(name: str) -> list[str]:
    """us_outbound . "Accounts" -> ["us_outbound", "Accounts"]; unquoted names fold to lower case."""
    return [p[1:-1].replace('""', '"') if p.startswith('"') else p.lower() for p in re.findall(IDENT, name)]


def _in_schema(parts: list[str], what: str) -> str:
    if len(parts) != 2 or parts[0] != DB_SCHEMA:
        raise GuardViolation(f"{what} {'.'.join(parts)!r}, not a {DB_SCHEMA}.<name> object (SPEC 1.2)")
    return f"{DB_SCHEMA}.{parts[1]}"


def check_statement(sql: str) -> str:
    """The guard target ("us_outbound.<object>") of one statement, or GuardViolation: it must be one statement, of an
    allowed form, on an object of schema us_outbound."""
    code = _code_only(sql).strip().removesuffix(";").rstrip()
    if ";" in code:
        raise GuardViolation("one statement at a time")
    form, m = next(((f, m) for f, rx in FORMS.items() if (m := rx.match(code))), ("", None))
    if m is None:
        raise GuardViolation(f"not an allowed DDL statement: {code[:80]!r}")
    parts = _parts(m["name"])
    on = (m.groupdict().get("on") or "").upper()
    if form == "schema" or on == "SCHEMA":
        if parts != [DB_SCHEMA]:
            raise GuardViolation(f"the only schema is {DB_SCHEMA}, not {'.'.join(parts)!r} (SPEC 1.2)")
        return SCHEMA_TARGET
    if on == "COLUMN":
        if len(parts) != 3:
            raise GuardViolation(f"COMMENT ON COLUMN names {DB_SCHEMA}.<table>.<column>, not {'.'.join(parts)!r}")
        return _in_schema(parts[:2], "COMMENT ON COLUMN")
    return _in_schema(parts, f"{form} statement on")


def added_column(sql: str) -> tuple[str, str] | None:
    """(table, column) of an ADD COLUMN statement, or None for any other."""
    m = FORMS["add_column"].match(_code_only(sql).strip().removesuffix(";").rstrip())
    return (_parts(m["name"])[-1], _parts(m["column"])[0]) if m else None


REFRESH_FILE = "views/(drop before recreate)"


def _checked(root: Path) -> list[tuple[str, str, str]]:
    paths = files(root)
    if not paths:
        raise FileNotFoundError(f"no DDL files under {root}")
    out = [(f"{p.parent.name}/{p.name}", s) for p in paths for s in split(p.read_text(encoding="utf-8"))]
    checked = [(name, s, check_statement(s)) for name, s in out]
    # Postgres's CREATE OR REPLACE VIEW cannot rename, drop or reorder a view's columns, so a
    # changed view would fail against a database made by an older version. Views hold no
    # data: drop every view first, dependents before what they read (the reverse of file
    # order), and create them all again, inside the one transaction apply() uses.
    views = [t for name, s, t in checked if name.startswith("views/") and FORMS["view"].match(_code_only(s).strip())]
    drops = [(REFRESH_FILE, f"DROP VIEW IF EXISTS {t}", t) for t in reversed(views)]
    for d in drops:
        check_statement(d[1])
    first_view = next((i for i, (name, _, _) in enumerate(checked) if name.startswith("views/")), len(checked))
    return checked[:first_view] + drops + checked[first_view:]


def statements(root: Path = SQL_DIR) -> list[tuple[str, str]]:
    """(file, statement) for every statement in apply order: tables, then views. All are checked first."""
    return [(name, s) for name, s, _ in _checked(root)]


def apply(
    guard: Guard,
    dsn: str,
    *,
    dry_run: bool = True,
    connect: Callable[[], Any] | None = None,
    root: Path = SQL_DIR,
) -> list[str]:
    """Check every statement, then run them in one transaction unless dry_run. Returns the statements.

    connect: a zero-argument callable returning a psycopg connection, used instead of
    psycopg.connect(dsn) (tests). Nothing connects before the guard authorizes the first
    statement; if any statement fails or is refused, the transaction is rolled back.
    """
    todo = _checked(root)
    targets = sorted({t for _, _, t in todo})
    if dry_run:
        log("ddl_apply", dry_run=True, statements=len(todo), targets=targets)
        return [s for _, s, _ in todo]
    if not dsn and connect is None:
        raise ValueError("ddl.apply needs a database URL unless dry_run")
    conn = None
    ran, present = 0, 0
    have: set[tuple[str, str]] = set()
    try:
        for name, sql, target in todo:
            if not guard.authorize("db", Op("ddl", target=target, write=True, detail={"file": name})):
                log("ddl_statement_skipped", target=target, file=name)
                continue
            if conn is None:
                conn = connect() if connect is not None else _psycopg_connect(dsn)
                conn.execute(LOCK_TIMEOUT)
                have = _columns(conn)
            if added_column(sql) in have:
                present += 1  # no ALTER, and no ACCESS EXCLUSIVE lock, for a column already there
                continue
            conn.execute(sql)
            ran += 1
        if conn is not None:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()  # uncommitted work is rolled back
    log("ddl_apply", dry_run=False, statements=ran, columns_already_there=present, targets=targets)
    return [s for _, s, _ in todo]


def _columns(conn: Any) -> set[tuple[str, str]]:
    """(table, column) of every column schema us_outbound has now (information_schema)."""
    cur = conn.execute(COLUMNS_SQL, [DB_SCHEMA])
    rows = cur.fetchall() if cur is not None else []
    return {(str(r[0] if not isinstance(r, dict) else r["table_name"]),
             str(r[1] if not isinstance(r, dict) else r["column_name"])) for r in rows}


def _psycopg_connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn)  # not autocommit: the statements run in one transaction
