"""The BigQuery DDL and views for dataset us_outbound (SPEC 6), and the command that applies them.

sql/ddl/NN_<table>.sql creates the dataset and then one table per file; sql/views/NN_<view>.sql
creates the views, numbered so a view comes after the views it reads. Each file holds one
statement with the placeholders {project} and {location}; the dataset is always us_outbound.

apply() checks every statement before it runs any (SPEC 1.2: write only to us_outbound):
  * it creates a backticked `{project}.us_outbound[.name]`: the dataset or a table only
    IF NOT EXISTS (never OR REPLACE, which would drop data), a view either way;
  * every backticked name, and any dotted name of three parts, is in `{project}.us_outbound`;
  * it is one statement.
Comments and string literals are ignored by the checks. Each statement then passes
guard.authorize("bq", Op("ddl", target="us_outbound.<name>", write=True)) before it runs.
Dry-run (the default) returns the rendered statements and runs nothing.

    python -m us_outbound.ops.ddl --project spill-warehouse-test --location EU [--execute]
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

from us_outbound.clients.bq import Store
from us_outbound.clients.guard import BQ_DATASET, Guard, GuardViolation, Op
from us_outbound.logs import log

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
DEFAULT_LOCATION = "EU"  # the location of the project's existing datasets (docs/phase0-facts.md)
DATASET_TARGET = f"{BQ_DATASET}.dataset"  # guard target for the CREATE SCHEMA statement

PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")  # a GCP project id
LOCATION_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z0-9]+)*")  # EU, US, europe-west2
PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CREATE_RE = re.compile(
    r"\s*CREATE\s+(?P<replace>OR\s+REPLACE\s+)?(?P<kind>SCHEMA|TABLE|VIEW)\s+"
    r"(?P<ifne>IF\s+NOT\s+EXISTS\s+)?`(?P<name>[^`]+)`",
    re.IGNORECASE,
)
# A name or dotted chain of names, each backticked or bare: `p.ds.t`, `p`.`ds`.t, a.col, col.
# Matching single names too keeps the scan from starting inside a backticked name.
CHAIN_RE = re.compile(r"(?:`[^`]*`|[A-Za-z_]\w*)(?:\s*\.\s*(?:`[^`]*`|[A-Za-z_]\w*))*")


def files(root: Path = SQL_DIR) -> list[Path]:
    """The DDL files in order, then the view files in order."""
    return sorted((root / "ddl").glob("*.sql")) + sorted((root / "views").glob("*.sql"))


def render(sql: str, project: str, location: str) -> str:
    """Fill {project} and {location}; one statement, without its trailing semicolon."""
    if not PROJECT_RE.fullmatch(project):
        raise ValueError(f"not a GCP project id: {project!r}")
    if not LOCATION_RE.fullmatch(location):
        raise ValueError(f"not a BigQuery location: {location!r}")
    out = sql.replace("{project}", project).replace("{location}", location).strip()
    left = PLACEHOLDER_RE.findall(out)
    if left:
        raise ValueError(f"unknown placeholders {sorted(set(left))}; only {{project}} and {{location}} are filled")
    return out[:-1].rstrip() if out.endswith(";") else out


def _code_only(sql: str) -> str:
    """sql with comments dropped and string literals emptied; backticked names kept."""
    out: list[str] = []
    i, n = 0, len(sql)

    def unterminated(what: str) -> GuardViolation:
        return GuardViolation(f"cannot check a statement with an unterminated {what}")

    while i < n:
        c = sql[i]
        if c == "`":
            j = sql.find("`", i + 1)
            if j < 0:
                raise unterminated("backticked name")
            out.append(sql[i : j + 1])
            i = j + 1
        elif sql.startswith("--", i) or c == "#":
            j = sql.find("\n", i)
            out.append(" ")
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise unterminated("comment")
            out.append(" ")
            i = j + 2
        elif c in "'\"":
            quote = sql[i : i + 3] if sql[i : i + 3] in ("'''", '"""') else c
            j = i + len(quote)
            while j < n and not sql.startswith(quote, j):
                j += 2 if sql[j] == "\\" else 1
            if j >= n:
                raise unterminated("string")
            out.append("''")
            i = j + len(quote)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parts(chain: str) -> list[str]:
    parts: list[str] = []
    for seg in re.findall(r"`[^`]*`|[A-Za-z_]\w*", chain):
        parts += seg.strip("`").split(".") if seg.startswith("`") else [seg]
    return parts


def _in_dataset(parts: list[str], project: str) -> bool:
    return len(parts) >= 2 and parts[0] == project and parts[1] == BQ_DATASET


def check_statement(sql: str, project: str) -> str:
    """The guard target ("us_outbound.<name>") of one rendered statement, or GuardViolation."""
    code = _code_only(sql)
    if ";" in code.rstrip().rstrip(";"):
        raise GuardViolation("one statement per DDL file")
    m = CREATE_RE.match(code)
    if not m:
        raise GuardViolation("DDL may only CREATE the dataset, a table or a view, named in backticks")
    kind, name = m["kind"].upper(), m["name"]
    parts = name.split(".")
    if not _in_dataset(parts, project):
        raise GuardViolation(f"DDL creates {name!r}, outside `{project}.{BQ_DATASET}` (SPEC 1.2)")
    if kind == "SCHEMA":
        if len(parts) != 2 or m["replace"] or not m["ifne"]:
            raise GuardViolation(f"the dataset is created with CREATE SCHEMA IF NOT EXISTS `{project}.{BQ_DATASET}`")
        target = DATASET_TARGET
    else:
        if len(parts) != 3 or not NAME_RE.fullmatch(parts[2]):
            raise GuardViolation(f"not a {kind.lower()} name in {BQ_DATASET}: {name!r}")
        if kind == "TABLE" and (m["replace"] or not m["ifne"]):
            raise GuardViolation(f"tables are created only IF NOT EXISTS, never replaced: {name!r}")
        if m["replace"] and m["ifne"]:
            raise GuardViolation("OR REPLACE and IF NOT EXISTS cannot be used together")
        target = f"{BQ_DATASET}.{parts[2]}"
    for chain in CHAIN_RE.findall(code):
        segs = re.findall(r"`[^`]*`|[A-Za-z_]\w*", chain)
        chain_parts = _parts(chain)
        names_object = len(chain_parts) >= 3 or any(s.startswith("`") and "." in s for s in segs)
        if names_object and not _in_dataset(chain_parts, project):
            raise GuardViolation(f"statement for {target} references {chain!r}, outside `{project}.{BQ_DATASET}` (SPEC 1.2)")
    return target


def apply(
    store_or_client: Any,
    project: str,
    location: str = DEFAULT_LOCATION,
    dry_run: bool = True,
    *,
    guard: Guard | None = None,
    root: Path = SQL_DIR,
) -> list[str]:
    """Render and check every statement; run them in order unless dry_run. Returns the statements.

    store_or_client is a BigQueryStore (its client and guard are used) or a
    google.cloud.bigquery Client; it is not touched in dry-run, so None is fine there.
    """
    paths = files(root)
    if not paths:
        raise FileNotFoundError(f"no DDL files under {root}")
    statements = [render(p.read_text(encoding="utf-8"), project, location) for p in paths]
    targets = [check_statement(s, project) for s in statements]  # all checked before any runs
    names = [f"{p.parent.name}/{p.name}" for p in paths]
    if dry_run:
        log("ddl_apply", dry_run=True, project=project, location=location, statements=len(statements), targets=targets)
        return statements

    if isinstance(store_or_client, Store):
        client = getattr(store_or_client, "client", None)
        if client is None:
            raise TypeError(f"{type(store_or_client).__name__} cannot run DDL; pass a BigQueryStore or a BigQuery client")
        guard = guard or store_or_client.guard
    else:
        client = store_or_client
    if client is None:
        raise TypeError("apply needs a BigQueryStore or a BigQuery client unless dry_run")
    guard = guard or Guard()

    ran = 0
    for name, sql, target in zip(names, statements, targets):
        if not guard.authorize("bq", Op("ddl", target=target, write=True, detail={"file": name})):
            log("ddl_statement_skipped", target=target, file=name)
            continue
        client.query(sql, location=location).result()
        ran += 1
        log("ddl_statement", target=target, file=name)
    log("ddl_apply", dry_run=False, project=project, location=location, statements=ran, targets=targets)
    return statements


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m us_outbound.ops.ddl",
        description="Create the us_outbound dataset, tables and views (SPEC 6). Dry-run unless --execute.",
    )
    p.add_argument("--project", default=os.environ.get("US_OUTBOUND_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    p.add_argument("--location", default=os.environ.get("US_OUTBOUND_BQ_LOCATION", DEFAULT_LOCATION))
    p.add_argument("--execute", action="store_true", help="run the statements; without it they are only printed")
    args = p.parse_args(argv)
    if not args.project:
        p.error("--project is required (or set US_OUTBOUND_PROJECT)")
    target = None
    if args.execute:
        from us_outbound.clients.bq import BigQueryStore

        target = BigQueryStore(Guard(), args.project, args.location)
    statements = apply(target, args.project, args.location, dry_run=not args.execute)
    if not args.execute:
        print(";\n\n".join(statements) + ";")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
