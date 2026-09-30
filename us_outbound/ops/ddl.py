"""The DDL and views for schema us_outbound in the Postgres database (SPEC 6), and apply().

sql/ddl/NN_<table>.sql creates the schema and then one table per file, with its indexes and
comments; sql/views/NN_<view>.sql creates the views, numbered so a view comes after the
views it reads. A file holds one or more statements, each ending in a semicolon.

statements() checks every statement before any runs (SPEC 1.2, for the database: write
only to schema us_outbound). A statement must be one of
    CREATE SCHEMA IF NOT EXISTS us_outbound
    CREATE TABLE IF NOT EXISTS us_outbound.<table> (...)
    CREATE [UNIQUE] INDEX IF NOT EXISTS <name> ON us_outbound.<table> (...)
    CREATE OR REPLACE VIEW us_outbound.<view> AS ...
    ALTER TABLE us_outbound.<table> ADD COLUMN IF NOT EXISTS <column> <type>
    DROP VIEW IF EXISTS us_outbound.<view>
    COMMENT ON SCHEMA | TABLE | VIEW | COLUMN us_outbound[.<object>[.<column>]] IS '...'
The ALTER form only adds a column to a table created before it (no default, nothing
dropped); the DROP form removes a view that was replaced, and without CASCADE, so a view
another view reads cannot go. Neither can touch stored rows.
Every name after FROM, JOIN or REFERENCES must be us_outbound.<name>, a CTE of the
statement, or an unqualified function call such as unnest(...); unqualified table names are
refused. Any other dotted name must start with us_outbound or with an alias the statement
declares with AS. The check reads FROM literally, so views use date_part() rather than
EXTRACT(... FROM ...). Comments and string literals are ignored; escape strings (E'...') and
dollar quoting are refused, since the check cannot read them.

apply() runs every statement in one transaction, each after
guard.authorize("db", Op("ddl", target="us_outbound.<object>", write=True)). Tables and
indexes are IF NOT EXISTS, so applying again changes no table. Every view is dropped and
created again (dependents first), because CREATE OR REPLACE VIEW cannot change a view's
columns; readers never see the gap, as the whole apply is one transaction.
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
FORMS = {
    "schema": re.compile(rf"CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+(?P<name>{NAME})$", re.I),
    "table": re.compile(rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?P<name>{NAME})\s*\(", re.I),
    "index": re.compile(rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+{IDENT}\s+ON\s+(?P<name>{NAME})\s*\(", re.I),
    "view": re.compile(rf"CREATE\s+OR\s+REPLACE\s+VIEW\s+(?P<name>{NAME})\s+AS\s", re.I),
    "add_column": re.compile(
        rf"ALTER\s+TABLE\s+(?P<name>{NAME})\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+{IDENT}\s+[A-Za-z][A-Za-z ]*(?:\[\])?$", re.I
    ),
    "drop_view": re.compile(rf"DROP\s+VIEW\s+IF\s+EXISTS\s+(?P<name>{NAME})$", re.I),
    "comment": re.compile(rf"COMMENT\s+ON\s+(?P<on>SCHEMA|TABLE|VIEW|COLUMN)\s+(?P<name>{NAME})\s+IS\s+''$", re.I),
}
REFERENCE_RE = re.compile(rf"\b(?P<kw>FROM|JOIN|REFERENCES)\s+(?:(?:LATERAL|ONLY)\s+)?(?P<name>{NAME})(?P<call>\s*\()?", re.I)
CTE_RE = re.compile(rf"(?:\bWITH(?:\s+RECURSIVE)?|,)\s*(?P<name>{IDENT})\s*(?:\([^()]*\)\s*)?AS\s*(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.I)
ALIAS_RE = re.compile(rf"\bAS\s+(?P<name>{IDENT})", re.I)
NAME_RE = re.compile(NAME)
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
    """The guard target ("us_outbound.<object>") of one statement, or GuardViolation."""
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
        target = SCHEMA_TARGET
    elif on == "COLUMN":
        if len(parts) != 3:
            raise GuardViolation(f"COMMENT ON COLUMN names {DB_SCHEMA}.<table>.<column>, not {'.'.join(parts)!r}")
        target = _in_schema(parts[:2], "COMMENT ON COLUMN")
    else:
        target = _in_schema(parts, f"{form} statement on")

    ctes = {_parts(c["name"])[0] for c in CTE_RE.finditer(code)}
    for ref in REFERENCE_RE.finditer(code):
        if code[: ref.start()].rstrip().upper().endswith("DISTINCT"):
            continue  # IS [NOT] DISTINCT FROM
        ref_parts = _parts(ref["name"])
        if len(ref_parts) == 1 and (ref_parts[0] in ctes or (ref["call"] and ref["kw"].upper() != "REFERENCES")):
            continue  # a CTE, or a function in FROM such as unnest(...)
        _in_schema(ref_parts, f"statement for {target} reads")
    aliases = ctes | {_parts(a["name"])[0] for a in ALIAS_RE.finditer(code)}
    for name in NAME_RE.findall(code):
        name_parts = _parts(name)
        if len(name_parts) > 1 and name_parts[0] != DB_SCHEMA and name_parts[0] not in aliases:
            raise GuardViolation(f"statement for {target} names {name!r}, outside {DB_SCHEMA} (SPEC 1.2)")
    return target


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
    ran = 0
    try:
        for name, sql, target in todo:
            if not guard.authorize("db", Op("ddl", target=target, write=True, detail={"file": name})):
                log("ddl_statement_skipped", target=target, file=name)
                continue
            if conn is None:
                conn = connect() if connect is not None else _psycopg_connect(dsn)
            conn.execute(sql)
            ran += 1
        if conn is not None:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()  # uncommitted work is rolled back
    log("ddl_apply", dry_run=False, statements=ran, targets=targets)
    return [s for _, s, _ in todo]


def _psycopg_connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn)  # not autocommit: the statements run in one transaction
