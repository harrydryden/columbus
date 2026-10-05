"""SPEC 6: the Postgres DDL and views in sql/, and ops/ddl.py, which checks and applies them.

The DB-free layer: every statement is split out of its file, parsed with sqlglot (Postgres
dialect) and checked against the store's contract (clients/db.py TABLE_KEYS, JSON_COLUMNS)
and the SPEC 6 column lists. The views are qualified against the DDL, so a view naming a
missing column fails here. apply() runs against a recording fake connection. The same
statements run against a real Postgres in tests/test_db.py.
"""

from __future__ import annotations

import re

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from us_outbound.clients.db import JSON_COLUMNS, TABLE_KEYS
from us_outbound.clients.guard import Boundaries, Guard, GuardViolation
from us_outbound.ops import ddl
from us_outbound.settings.model import SIZE_BANDS, TABS, TIERS

SCHEMA = "us_outbound"

# SPEC 6 column lists, copied from SPEC.md.
SPEC_COLUMNS: dict[str, set[str]] = {
    "accounts": {
        "account_id", "domain", "clean_name", "legal_name", "apollo_org_id", "hq_city", "hq_state", "industry",
        "industry_group", "naics", "employees", "us_employees", "size_band", "founded_year", "source", "score",
        "tier", "tier_reason", "angle", "sender", "status", "clay_checked_at", "clay_credits_used",
        "hubspot_company_id", "first_seen", "last_scored",
    },
    "contacts": {
        "contact_id", "account_id", "role", "title", "first_name", "last_name", "email", "email_sha256",
        "email_status", "email_source", "person_state", "enrolment_month", "angle", "copy_version", "test_id",
        "mailbox", "instantly_campaign", "instantly_lead_id", "hubspot_contact_id", "suppressed",
        "suppressed_reason", "created_at",
    },
    "signal_events": {"event_id", "account_id", "source", "fact", "value", "quote", "source_url", "observed_at"},
    "events": {
        "event_id", "contact_id", "account_id", "type", "step", "mailbox", "reply_class", "reply_text",
        "language_terms", "competitor_named", "approval", "approved_by", "occurred_at",
    },
    "suppression": {"email_sha256", "domain", "reason", "source", "added_at"},
    "settings": {"tab", "key", "values", "effective_from", "effective_to", "synced_at"},
}
# Columns the build adds to SPEC 6 tables.
BUILD_ADDITIONS: dict[str, set[str]] = {
    "contacts": {"last_step_at", "enrolled_at", "opener_arm", "opener_source", "signals_at_enrol", "score_at_enrol",
                 "tier_at_enrol"},
    "suppression": {"expires_at"},
}
# Tables the build adds, with the layouts every agent codes to.
BUILD_TABLES: dict[str, set[str]] = {
    "heartbeats": {"run_id", "job", "started_at", "finished_at", "status", "dry_run", "detail", "error"},
    "credit_ledger": {"entry_id", "system", "job", "run_id", "account_id", "credits", "usd", "occurred_at", "note"},
    "hitl_items": {
        "item_id", "kind", "account_id", "contact_id", "event_id", "slack_channel", "slack_ts", "payload", "status",
        "created_at", "reposted_at", "escalated_at", "handled_at", "handled_by",
    },
    "domain_aliases": {"alias", "root_domain", "source", "added_at"},
    "partners": {"domain", "name", "reason", "naics", "added_at"},
    "lookalike_cells": {
        "cell_id", "industry_label", "industry_group", "size_band", "active_customers", "churned_customers",
        "us_active", "us_churned", "strength", "computed_at", "run_id",
    },
    "lookalike_growth": {  # Harry, 5 Oct 2026: customers by 12-month headcount growth band
        "cell_id", "industry_group", "growth_band", "active_customers", "churned_customers", "strength", "computed_at",
        "run_id",
    },
}
RAW_TABLES = ("raw_irs_bmf", "raw_job_posts", "raw_clay_accounts", "raw_clay_contacts", "raw_site_visits", "raw_layoffs")
RAW_COLUMNS = {"loaded_at", "run_id", "key", "payload"}

VIEWS = {"v_account_outcomes", "v_queue", "v_signal_value", "v_readout_weekly", "v_mailbox_health",
         "v_budgets", "v_heartbeats"}
SPEC_VIEWS = VIEWS - {"v_account_outcomes"}
RETIRED_VIEWS = {"v_credits_month"}  # replaced by v_budgets when budgets became weekly; dropped if it exists

# Types by column name (the brief's rules), as sqlglot prints them; every other column is TEXT.
TIMESTAMPS = {"first_seen", "last_scored", "effective_from", "effective_to"}  # plus every *_at
INTS = {"employees", "us_employees", "founded_year", "score", "step", "active_customers", "churned_customers",
        "us_active", "us_churned", "score_at_enrol"}
FLOATS = {"clay_credits_used", "credits", "usd", "strength"}
BOOLS = {"suppressed", "dry_run"}

# Indexes the brief asks for (table -> leading columns); more are fine.
INDEXES = {
    ("contacts", "account_id"), ("signal_events", "account_id"), ("events", "account_id"),
    ("events", "occurred_at"), ("signal_events", "observed_at"),
}
# The jobs' hot reads (table -> its index's columns, in order).
HOT_INDEXES = {
    ("accounts", ("status",)), ("events", ("type", "occurred_at")), ("signal_events", ("source", "fact")),
    ("hitl_items", ("kind", "status")), ("heartbeats", ("job", "started_at")),
}

# Enum columns: their comment lists exactly these values after "One of: ".
ENUMS: dict[tuple[str, str], set[str]] = {
    ("accounts", "tier"): set(TIERS),
    ("accounts", "size_band"): set(SIZE_BANDS),
    ("accounts", "source"): {"apollo", "irs", "site_visit", "named", "lookalike"},  # lookalike: sources/lookalike_leads.py
    ("accounts", "status"): {
        "new", "queued", "verified", "enrolled", "engaged", "demo_requested", "demo_booked", "disqualified",
    },
    ("contacts", "email_source"): {"apollo", "clay"},
    ("contacts", "opener_arm"): {"opener", "holdout", "none"},  # enrol/openers.py (Harry, 2 Oct 2026)
    ("events", "type"): {
        "sent", "bounced", "replied", "unsubscribed", "site_visit", "meeting_booked", "demo_held", "deal_created",
        "escalated", "send_approval",  # enrol/approvals.py (Harry, 2 Oct 2026)
        "reply_sent",  # replies/desk.py: a desk reply is no campaign send
    },
    ("events", "reply_class"): {
        "positive", "referral", "objection", "not_now", "negative", "out_of_office", "wrong_person", "unsubscribe",
        "other",
    },
    # A reply's approval, then a send approval's outcome (enrol/approvals.py).
    ("events", "approval"): {"approved", "edited", "skipped", "approved_edited", "contact_rejected", "company_rejected",
                             "expired", "blocked"},
    ("settings", "tab"): set(TABS) | {"_order"},  # settings.sync.ORDER_TAB
    ("heartbeats", "status"): {"running", "ok", "error", "skipped"},
    ("credit_ledger", "system"): {"clay", "apollo", "claude"},
    ("hitl_items", "kind"): {"reply", "out_of_office", "hand_check", "manual_merge", "kill_rule", "send_approval"},
    ("hitl_items", "status"): {"open", "sending", "handled", "escalated"},
    ("lookalike_cells", "size_band"): {"1-9", "10-49", "50-99", "100-249", "250+", "unknown"},
    ("lookalike_growth", "growth_band"): {"shrinking", "flat", "growing", "fast", "unknown"},
}


def expected_type(table: str, column: str) -> str:
    if column in JSON_COLUMNS.get(table, ()):
        return "JSONB"
    if column.endswith("_at") or column in TIMESTAMPS:
        return "TIMESTAMPTZ"
    if column in INTS:
        return "INT"
    if column in FLOATS:
        return "DOUBLE PRECISION"
    if column in BOOLS:
        return "BOOLEAN"
    return "TEXT"


# -- parsed statements ----------------------------------------------------------


@pytest.fixture(scope="module")
def parsed() -> list[tuple[str, str, exp.Expression]]:
    """(file, statement, parsed) for every statement, in apply order."""
    out = []
    for name, sql in ddl.statements():
        trees = sqlglot.parse(sql, read="postgres")
        assert len(trees) == 1, f"{name}: one statement per split"
        out.append((name, sql, trees[0]))
    return out


class Table:
    def __init__(self, file: str, create: exp.Create):
        self.file, self.create = file, create
        self.name = create.this.this.name
        self.columns = {cd.name: cd for cd in create.find_all(exp.ColumnDef)}
        cons = [c for c in create.this.expressions if not isinstance(c, exp.ColumnDef)]
        self.primary_key = [tuple(c.name for c in k.expressions) for k in cons if isinstance(k, exp.PrimaryKey)]
        self.unique = [(c.sql("postgres"), tuple(i.name for i in c.this.expressions))
                       for c in cons if isinstance(c, exp.UniqueColumnConstraint)]
        self.comments: dict[str, str] = {}  # "" for the table, else the column
        self.indexes: list[tuple[str, ...]] = []

    def type(self, column: str) -> str:
        return self.columns[column].args["kind"].sql("postgres")

    def not_null(self, column: str) -> bool:
        return any(isinstance(c.kind, exp.NotNullColumnConstraint) for c in self.columns[column].args.get("constraints") or [])


def _comment(e: exp.Comment) -> tuple[str, str, str]:
    """(kind, table or view, column or "") of a COMMENT ON statement."""
    kind, target = e.args["kind"].upper(), e.this
    if kind == "COLUMN":
        return kind, target.table, target.name
    return kind, target.name, ""


@pytest.fixture(scope="module")
def tables(parsed) -> dict[str, Table]:
    out: dict[str, Table] = {}
    for name, _, e in parsed:
        if isinstance(e, exp.Create) and e.kind == "TABLE":
            t = Table(name, e)
            assert t.name not in out, f"{t.name} is created by both {out[t.name].file} and {name}"
            out[t.name] = t
    for name, _, e in parsed:
        if isinstance(e, exp.Create) and e.kind == "INDEX":
            t = out[e.this.args["table"].name]
            t.indexes.append(tuple(c.this.name for c in e.this.args["params"].args["columns"]))
            assert e.args.get("exists"), f"{name}: CREATE INDEX IF NOT EXISTS"
            assert name == t.file, f"{name} indexes {t.name}, which {t.file} creates"
        elif isinstance(e, exp.Comment) and _comment(e)[0] in {"TABLE", "COLUMN"}:
            _, table, column = _comment(e)
            out[table].comments[column] = e.args["expression"].name
            assert name == out[table].file, f"{name} comments on {table}, which {out[table].file} creates"
    return out


@pytest.fixture(scope="module")
def views(parsed) -> dict[str, tuple[str, exp.Create, str]]:
    """view -> (file, CREATE VIEW, its COMMENT ON VIEW text)."""
    out = {e.this.name: (name, e) for name, _, e in parsed if isinstance(e, exp.Create) and e.kind == "VIEW"}
    comments = {_comment(e)[1]: e.args["expression"].name for _, _, e in parsed
                if isinstance(e, exp.Comment) and _comment(e)[0] == "VIEW"}
    return {v: (name, e, comments.get(v, "")) for v, (name, e) in out.items()}


# -- files and order --------------------------------------------------------------


def test_files_are_numbered_ddl_then_views():
    paths = ddl.files()
    names = [f"{p.parent.name}/{p.name}" for p in paths]
    assert names[0] == "ddl/00_schema.sql"
    kinds = [p.parent.name for p in paths]
    assert kinds == sorted(kinds, key=["ddl", "views"].index), "ddl files before view files"
    for p in paths:
        assert re.fullmatch(r"\d{2}_[a-z0-9_]+\.sql", p.name), p.name
    for kind in ("ddl", "views"):
        numbers = [p.name[:2] for p in paths if p.parent.name == kind]
        assert len(numbers) == len(set(numbers)), f"duplicate numbers in sql/{kind}"


def test_statements_come_in_file_order_and_hold_no_semicolon():
    order = [f"{p.parent.name}/{p.name}" for p in ddl.files()]
    names = [name for name, _ in ddl.statements() if name != ddl.REFRESH_FILE]
    assert sorted(set(names), key=order.index) == order, "every file holds a statement"
    assert names == sorted(names, key=order.index)
    for _, sql in ddl.statements():
        assert ";" not in ddl._code_only(sql)


def test_every_view_is_dropped_before_the_views_are_created_again():
    """CREATE OR REPLACE VIEW cannot change a view's columns, so apply drops every view first,
    dependents before what they read (the reverse of file order)."""
    stmts = ddl.statements()
    kinds = [("drop" if name == ddl.REFRESH_FILE else name.split("/")[0]) for name, _ in stmts]
    assert kinds == sorted(kinds, key=["ddl", "drop", "views"].index)
    drops = [sql.rsplit(".", 1)[1] for name, sql in stmts if name == ddl.REFRESH_FILE]
    created = [ddl.check_statement(sql).split(".", 1)[1] for name, sql in stmts
               if name.startswith("views/0") and ddl.FORMS["view"].match(ddl._code_only(sql).strip())]
    assert drops == list(reversed(created)) and set(drops) == VIEWS


def test_every_statement_parses_as_an_allowed_kind(parsed):
    for name, _, e in parsed:
        ok = (
            (isinstance(e, exp.Create) and e.kind in {"SCHEMA", "TABLE", "INDEX", "VIEW"})
            or isinstance(e, exp.Comment)
            or (isinstance(e, exp.Alter) and all(isinstance(a, exp.ColumnDef) for a in e.args.get("actions") or []))
            or (isinstance(e, exp.Drop) and e.kind == "VIEW" and e.args.get("exists") and not e.args.get("cascade"))
        )
        assert ok, f"{name}: {type(e).__name__}"


def test_an_added_column_is_also_in_its_create_table(parsed, tables):
    """ALTER ... ADD COLUMN IF NOT EXISTS only brings an older database up to its CREATE TABLE."""
    for name, _, e in parsed:
        if isinstance(e, exp.Alter):
            table = e.this.name
            for col in e.args.get("actions") or []:
                assert col.name in tables[table].columns, f"{name}: {table}.{col.name} is not in its CREATE TABLE"


def test_the_schema_comes_first(parsed):
    name, _, e = parsed[0]
    assert name == "ddl/00_schema.sql" and e.kind == "SCHEMA" and e.args.get("exists")
    assert e.this.db == SCHEMA


# -- tables ------------------------------------------------------------------------


def test_every_store_table_has_exactly_one_ddl_file(tables):
    assert set(tables) == set(TABLE_KEYS), "sql/ddl and clients/db.py TABLE_KEYS must list the same tables"
    for name, t in tables.items():
        assert t.file == f"ddl/{t.file[4:6]}_{name}.sql", f"{t.file} should be named ddl/NN_{name}.sql"


def test_tables_are_created_if_not_exists(tables):
    for t in tables.values():
        assert t.create.args.get("exists"), f"{t.name}: CREATE TABLE IF NOT EXISTS"
        assert t.create.this.this.db == SCHEMA, t.name


def test_spec6_columns_exactly(tables):
    for name, cols in SPEC_COLUMNS.items():
        assert set(tables[name].columns) == cols | BUILD_ADDITIONS.get(name, set()), name


def test_build_tables_columns_exactly(tables):
    for name, cols in BUILD_TABLES.items():
        assert set(tables[name].columns) == cols, name
    for name in RAW_TABLES:
        assert set(tables[name].columns) == RAW_COLUMNS, name


def test_keys_match_table_keys(tables):
    """The upsert key is the primary key; suppression's has NULL parts, so it is UNIQUE NULLS NOT DISTINCT."""
    for name, key in TABLE_KEYS.items():
        t = tables[name]
        if key is None:
            assert t.primary_key == [] and t.unique == [], f"{name} is append-only: no key"
        elif name == "suppression":
            assert t.primary_key == []
            [(text, cols)] = t.unique
            assert cols == key and "NULLS NOT DISTINCT" in text
            assert not any(t.not_null(c) for c in key), "an entry may be email-only or domain-only"
        else:
            assert t.primary_key == [key], f"{name}: PRIMARY KEY {key}"
            assert all(t.not_null(c) for c in key), f"{name}: key columns NOT NULL"


def test_json_columns_are_jsonb_and_only_they(tables):
    for t in tables.values():
        jsonb = {c for c in t.columns if t.type(c) == "JSONB"}
        assert jsonb == set(JSON_COLUMNS.get(t.name, ())), t.name


def test_column_types(tables):
    for t in tables.values():
        for col in t.columns:
            assert t.type(col) == expected_type(t.name, col), f"{t.name}.{col}"


def test_indexes(tables):
    have = {(t.name, idx[0]) for t in tables.values() for idx in t.indexes}
    assert INDEXES <= have
    assert HOT_INDEXES <= {(t.name, idx) for t in tables.values() for idx in t.indexes}


def test_enum_columns_list_their_values(tables):
    for (name, col), values in ENUMS.items():
        text = tables[name].comments.get(col, "")
        m = re.search(r"One of: ([^.(]+)", text)
        assert m, f"{name}.{col} comment lists its values: {text!r}"
        assert {v.strip() for v in m.group(1).split(",")} == values, f"{name}.{col}"


def test_tables_have_spec_comments(tables):
    for t in tables.values():
        assert "SPEC" in t.comments.get("", ""), f"{t.name} has COMMENT ON TABLE citing SPEC"
        assert set(t.comments) - {""} <= set(t.columns), f"{t.name}: a comment on a missing column"


def test_retention_is_noted_where_it_applies(tables):
    text = {name: (ddl.SQL_DIR / t.file).read_text(encoding="utf-8") for name, t in tables.items()}
    assert "90 days" in text["events"] and "reply_text" in text["events"]
    assert "12 months" in text["contacts"] and "last_step_at" in text["contacts"]
    assert "12 months" in text["accounts"]
    assert "indefinitely" in text["suppression"]


# -- views -------------------------------------------------------------------------


def test_the_views_exist_with_spec_comments(views):
    assert set(views) == VIEWS and SPEC_VIEWS <= set(views)
    for name, (file, e, comment) in views.items():
        assert file == f"views/{file[6:8]}_{name}.sql", file
        assert e.args.get("replace"), f"{name}: CREATE OR REPLACE VIEW"
        assert e.this.db == SCHEMA, name
        assert "SPEC" in comment, f"{name} has COMMENT ON VIEW citing SPEC"


def test_views_read_only_known_tables_and_columns(tables, parsed):
    """Qualify every view against the DDL (and earlier views): unknown tables or columns fail."""
    schema = {name: {c: t.type(c) for c in t.columns} for name, t in tables.items()}
    for file, _, e in parsed:
        if not (isinstance(e, exp.Create) and e.kind == "VIEW"):
            continue
        name = e.this.name
        body = e.expression.copy()
        ctes = {c.alias_or_name for c in body.find_all(exp.CTE)}
        for tbl in body.find_all(exp.Table):
            if tbl.name in ctes and not tbl.db:
                continue
            assert tbl.name in schema, f"{name} reads {tbl.name}, which is not created before it"
        q = qualify(body, schema={SCHEMA: schema}, dialect="postgres", validate_qualify_columns=True)
        schema[name] = {c: "TEXT" for c in q.named_selects}


def test_nothing_references_another_schema(parsed):
    for file, sql, e in parsed:
        assert ddl.check_statement(sql).startswith(f"{SCHEMA}."), file
        if isinstance(e, exp.Create) and e.kind == "SCHEMA":
            assert e.this.db == SCHEMA and not e.this.name
            continue
        if isinstance(e, exp.Comment) and e.args["kind"].upper() == "SCHEMA":
            assert e.this.name == SCHEMA
            continue
        ctes = {c.alias_or_name for c in e.find_all(exp.CTE)}
        for tbl in e.find_all(exp.Table):
            if tbl.name in ctes and not tbl.db:
                continue
            assert tbl.db == SCHEMA and not tbl.catalog, f"{file} references {tbl.sql('postgres')}"


def test_v_queue_orders_for_enrollment(views):
    sql = views["v_queue"][1].sql("postgres")
    for status in ("'verified'", "'queued'"):
        assert status in sql
    for tier in ("'Priority'", "'Standard'", "'Control'"):
        assert tier in sql
    # Size bands 20-49 and 50-99 first, then 100-249, then 10-19 (SPEC 9).
    ranks = dict(re.findall(r"WHEN '(\d+-\d+)' THEN (\d)", sql))
    assert ranks == {"20-49": "1", "50-99": "1", "100-249": "2", "10-19": "3"}
    assert "expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP" in sql
    assert "email_sha256 IS NULL" in sql  # an email row suppresses only that email, not its domain
    assert "score DESC NULLS LAST" in sql  # BigQuery's order: an account with no score comes last


def test_v_heartbeats_ignores_overlap_skips(views):
    sql = views["v_heartbeats"][1].sql("postgres")
    assert "last_alive_at" in sql and "'previous run still going'" in sql


# -- ops/ddl.py: split and check -----------------------------------------------------


def test_split_keeps_semicolons_in_strings_names_and_comments():
    text = (
        "-- header; with a semicolon\n"
        "CREATE SCHEMA IF NOT EXISTS us_outbound;\n"
        "COMMENT ON SCHEMA us_outbound IS 'a; b ''c;''';\n"
        '/* x; */ COMMENT ON COLUMN us_outbound.t."a;b" IS \'d\';\n'
        "-- trailing comment only\n"
    )
    assert ddl.split(text) == [
        "-- header; with a semicolon\nCREATE SCHEMA IF NOT EXISTS us_outbound",
        "COMMENT ON SCHEMA us_outbound IS 'a; b ''c;'''",
        '/* x; */ COMMENT ON COLUMN us_outbound.t."a;b" IS \'d\'',
    ]


S = SCHEMA


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE IF NOT EXISTS other.t (a text)",
        "CREATE TABLE IF NOT EXISTS public.accounts (a text)",
        "CREATE TABLE IF NOT EXISTS accounts (a text)",
        'CREATE TABLE IF NOT EXISTS "other"."t" (a text)',
        'CREATE TABLE IF NOT EXISTS "US_OUTBOUND".t (a text)',
        f"CREATE TABLE {S}.accounts (a text)",
        f"CREATE UNLOGGED TABLE IF NOT EXISTS {S}.t (a text)",
        f"CREATE TEMP TABLE {S}.t (a text)",
        f"CREATE TABLE IF NOT EXISTS {S}.t AS SELECT * FROM other.t",
        f"CREATE TABLE IF NOT EXISTS {S}.t (LIKE other.t)",
        f"CREATE TABLE IF NOT EXISTS {S}.t (a text REFERENCES other.t (a))",
        f"CREATE TABLE IF NOT EXISTS {S}.t (a text REFERENCES t2 (a))",
        f"CREATE TABLE IF NOT EXISTS {S}.t PARTITION OF other.t FOR VALUES IN (1)",
        "CREATE SCHEMA IF NOT EXISTS other",
        f"CREATE SCHEMA {S}",
        f"CREATE SCHEMA IF NOT EXISTS {S} AUTHORIZATION someone",
        "CREATE INDEX IF NOT EXISTS i ON other.t (a)",
        "CREATE INDEX IF NOT EXISTS i ON accounts (a)",
        f"CREATE INDEX i ON {S}.accounts (a)",
        "COMMENT ON TABLE other.t IS 'x'",
        "COMMENT ON COLUMN other.t.c IS 'x'",
        f"COMMENT ON COLUMN {S}.t IS 'x'",
        "COMMENT ON SCHEMA public IS 'x'",
        f"DROP TABLE {S}.accounts",
        f"TRUNCATE {S}.accounts",
        f"DELETE FROM {S}.accounts",
        f"INSERT INTO {S}.accounts (account_id) VALUES ('x')",
        f"UPDATE {S}.accounts SET tier = 'x'",
        f"ALTER TABLE {S}.accounts ADD COLUMN x text",
        f"GRANT SELECT ON {S}.accounts TO someone",
        f"CREATE MATERIALIZED VIEW {S}.v AS SELECT 1",
        f"CREATE VIEW {S}.v AS SELECT 1",
        "CREATE OR REPLACE VIEW other.v AS SELECT 1",
        f"CREATE OR REPLACE FUNCTION {S}.f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT * FROM other.t",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT * FROM accounts",
        f'CREATE OR REPLACE VIEW {S}.v AS SELECT * FROM "other".t',
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT * FROM {S}.a AS a JOIN other.t AS t ON t.x = a.x",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT * FROM {S}.a AS a, other.t AS t",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT * FROM {S}.a AS a WHERE a.x IN (SELECT x FROM other.t)",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT other.f(a.x) FROM {S}.a AS a",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT * FROM db.{S}.a",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT * FROM pg_catalog.pg_tables",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT 1; DROP TABLE {S}.accounts",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT 'unterminated",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT 1 /* unterminated",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT E'\\'' FROM other.t --'",
        f"CREATE OR REPLACE VIEW {S}.v AS SELECT $$x$$ FROM other.t",
    ],
)
def test_check_statement_refuses(sql):
    with pytest.raises(GuardViolation):
        ddl.check_statement(sql)


def test_check_statement_ignores_comments_and_strings():
    sql = (
        "-- this view once read other.t\n"
        f"CREATE OR REPLACE VIEW {S}.v AS\n"
        "/* FROM other.t */ SELECT s.\"key\", 'x FROM other.t' AS s, a.b IS DISTINCT FROM a.c AS d\n"
        f"FROM {S}.settings AS s JOIN {S}.accounts AS a ON a.x = s.x CROSS JOIN LATERAL unnest(a.y) AS u"
    )
    assert ddl.check_statement(sql) == "us_outbound.v"


def test_check_statement_targets():
    assert ddl.check_statement(f"CREATE SCHEMA IF NOT EXISTS {S}") == "us_outbound.schema"
    assert ddl.check_statement(f"COMMENT ON SCHEMA {S} IS 'x'") == "us_outbound.schema"
    assert ddl.check_statement(f"CREATE TABLE IF NOT EXISTS {S}.t (a text)") == "us_outbound.t"
    assert ddl.check_statement(f'CREATE TABLE IF NOT EXISTS "{S}".t (a text);') == "us_outbound.t"
    assert ddl.check_statement(f"CREATE UNIQUE INDEX IF NOT EXISTS t_a ON {S}.t (a)") == "us_outbound.t"
    assert ddl.check_statement(f"COMMENT ON TABLE {S}.t IS 'x'") == "us_outbound.t"
    assert ddl.check_statement(f"COMMENT ON COLUMN {S}.t.\"key\" IS 'x'") == "us_outbound.t"
    assert ddl.check_statement(f"CREATE OR REPLACE VIEW {S}.v AS WITH w AS (SELECT 1 AS x) SELECT x FROM w") == "us_outbound.v"


# -- ops/ddl.py: apply ---------------------------------------------------------------


class FakeConnection:
    """Records what apply() runs; stands in for a psycopg connection."""

    def __init__(self, log: list):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append(("execute", sql))

    def commit(self):
        self.log.append(("commit", None))

    def close(self):
        self.log.append(("close", None))


def no_connection():
    raise AssertionError("must not connect")


def test_apply_dry_run_returns_statements_and_connects_to_nothing():
    guard = Guard()
    statements = ddl.apply(guard, "", connect=no_connection)
    assert statements == [s for _, s in ddl.statements()]
    assert statements[0].endswith(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    assert guard.calls == []


def test_apply_runs_every_statement_in_one_transaction_through_the_guard():
    guard, log = Guard(), []
    statements = ddl.apply(guard, "postgresql://unused", dry_run=False, connect=lambda: FakeConnection(log))
    assert log == [("execute", s) for s in statements] + [("commit", None), ("close", None)]
    calls = [c for c in guard.calls if c.system == "db"]
    assert len(calls) == len(statements)
    assert all(c.action == "ddl" and c.write and c.sent for c in calls)
    targets = [c.target for c in calls]
    assert targets[0] == "us_outbound.schema"
    objects = {f"us_outbound.{t}" for t in TABLE_KEYS} | {f"us_outbound.{v}" for v in VIEWS}
    assert set(targets) == objects | {"us_outbound.schema"} | {f"us_outbound.{v}" for v in RETIRED_VIEWS}


def test_apply_checks_every_file_before_running_any(tmp_path):
    (tmp_path / "ddl").mkdir()
    (tmp_path / "views").mkdir()
    (tmp_path / "ddl" / "00_schema.sql").write_text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};\n")
    (tmp_path / "views" / "00_v_bad.sql").write_text(
        f"CREATE OR REPLACE VIEW {SCHEMA}.v_bad AS SELECT * FROM crm.companies;\n"
    )
    with pytest.raises(GuardViolation, match="crm"):
        ddl.apply(Guard(), "postgresql://unused", dry_run=False, connect=no_connection, root=tmp_path)
    with pytest.raises(GuardViolation, match="crm"):
        ddl.statements(tmp_path)


def test_apply_stops_when_the_guard_refuses_before_connecting():
    guard = Guard(bounds=Boundaries(db_schema="somewhere_else"))
    with pytest.raises(GuardViolation):
        ddl.apply(guard, "postgresql://unused", dry_run=False, connect=no_connection)


def test_apply_rolls_back_when_a_statement_fails():
    log = []

    class Failing(FakeConnection):
        def execute(self, sql, params=None):
            super().execute(sql)
            if len(log) == 3:
                raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        ddl.apply(Guard(), "postgresql://unused", dry_run=False, connect=lambda: Failing(log))
    assert ("commit", None) not in log and log[-1] == ("close", None)


def test_apply_needs_a_database_url_unless_dry_run():
    with pytest.raises(ValueError):
        ddl.apply(Guard(), "", dry_run=False)


def test_ddl_files_need_no_placeholders():
    for p in ddl.files():
        assert not re.search(r"\{[a-z_]+\}", ddl._code_only(p.read_text(encoding="utf-8"))), p.name


def test_the_readout_reply_window_matches_the_sequence():
    """Replies count for a week after the last step; the views and the client share the number."""
    from us_outbound.clients.instantly import REPLY_WINDOW_DAYS, STEP_DAYS

    assert REPLY_WINDOW_DAYS == STEP_DAYS[-1] + 7 == 28
    text = (ddl.SQL_DIR / "views" / "00_v_account_outcomes.sql").read_text()
    assert f"INTERVAL '{REPLY_WINDOW_DAYS} days'" in text and "INTERVAL '21 days'" not in text
