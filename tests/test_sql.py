"""SPEC 6: the BigQuery DDL and views in sql/, and ops/ddl.py, which applies them.

Every statement is rendered, parsed with sqlglot (BigQuery dialect) and checked against
the store's contract (clients/bq.py TABLE_KEYS, JSON_COLUMNS) and the SPEC 6 column lists.
The views are qualified against the DDL, so a view naming a missing column fails here.
Nothing touches BigQuery: apply() runs against a recording fake client.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from us_outbound.clients.bq import JSON_COLUMNS, TABLE_KEYS, BigQueryStore, MemoryStore
from us_outbound.clients.guard import Boundaries, Guard, GuardViolation
from us_outbound.ops import ddl
from us_outbound.settings.model import SIZE_BANDS, TABS, TIERS

PROJECT = "test-project"
LOCATION = "EU"
DATASET = "us_outbound"

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
BUILD_ADDITIONS: dict[str, set[str]] = {"contacts": {"last_step_at"}, "suppression": {"expires_at"}}
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
}
RAW_TABLES = ("raw_irs_bmf", "raw_job_posts", "raw_clay_accounts", "raw_clay_contacts", "raw_site_visits", "raw_layoffs")
RAW_COLUMNS = {"loaded_at", "run_id", "key", "payload"}

SPEC_VIEWS = {"v_queue", "v_signal_value", "v_readout_weekly", "v_mailbox_health", "v_credits_month", "v_heartbeats"}

# Types by column name (the brief's rules); every other column is STRING, JSON columns included.
TIMESTAMPS = {"first_seen", "last_scored", "effective_from", "effective_to"}  # plus every *_at
INTS = {"employees", "us_employees", "founded_year", "score", "step"}
FLOATS = {"clay_credits_used", "credits", "usd"}
BOOLS = {"suppressed", "dry_run"}

PARTITIONS = {
    "events": "occurred_at",
    "signal_events": "observed_at",
    "heartbeats": "started_at",
    "credit_ledger": "occurred_at",
    **{t: "loaded_at" for t in RAW_TABLES},
}

# Enum columns: their description lists exactly these values after "One of: ".
ENUMS: dict[tuple[str, str], set[str]] = {
    ("accounts", "tier"): set(TIERS),
    ("accounts", "size_band"): set(SIZE_BANDS),
    ("accounts", "source"): {"apollo", "irs", "site_visit"},
    ("accounts", "status"): {
        "new", "queued", "verified", "enrolled", "engaged", "demo_requested", "demo_booked", "disqualified",
    },
    ("contacts", "email_source"): {"apollo", "clay"},
    ("events", "type"): {
        "sent", "bounced", "replied", "unsubscribed", "site_visit", "meeting_booked", "demo_held", "deal_created",
        "escalated",
    },
    ("events", "reply_class"): {
        "positive", "referral", "objection", "not_now", "negative", "out_of_office", "wrong_person", "unsubscribe",
        "other",
    },
    ("events", "approval"): {"approved", "edited", "skipped"},
    ("settings", "tab"): set(TABS) | {"_order"},  # settings.sync.ORDER_TAB
    ("heartbeats", "status"): {"running", "ok", "error", "skipped"},
    ("credit_ledger", "system"): {"clay", "apollo", "claude"},
    ("hitl_items", "kind"): {"reply_approval", "hand_check", "manual_merge", "kill_rule"},
    ("hitl_items", "status"): {"open", "handled", "escalated"},
}


def expected_type(column: str) -> str:
    if column.endswith("_at") or column in TIMESTAMPS:
        return "TIMESTAMP"
    if column in INTS:
        return "INT64"
    if column in FLOATS:
        return "FLOAT64"
    if column in BOOLS:
        return "BOOL"
    return "STRING"


# -- parsed files ----------------------------------------------------------------


def _parse(path: Path) -> exp.Expression:
    sql = ddl.render(path.read_text(encoding="utf-8"), PROJECT, LOCATION)
    parsed = sqlglot.parse(sql, read="bigquery")
    assert len(parsed) == 1, f"{path.name}: one statement per file"
    return parsed[0]


@pytest.fixture(scope="module")
def parsed() -> list[tuple[Path, exp.Expression]]:
    return [(p, _parse(p)) for p in ddl.files()]


def _description(props: exp.Expression) -> str:
    """The description in an OPTIONS list (column or statement level), "" if none."""
    for prop in props.expressions:
        if type(prop) is exp.Property and prop.name.lower() == "description":
            return prop.args["value"].name
    return ""


def _column_description(cd: exp.ColumnDef) -> str:
    for c in cd.args.get("constraints") or []:
        if isinstance(c.kind, exp.Properties):
            return _description(c.kind)
    return ""


class Table:
    def __init__(self, path: Path, create: exp.Create):
        self.path, self.create = path, create
        self.name = create.this.this.name
        self.columns = {cd.name: cd for cd in create.find_all(exp.ColumnDef)}
        props = create.args["properties"].expressions
        part = [p for p in props if isinstance(p, exp.PartitionedByProperty)]
        clus = [p for p in props if isinstance(p, exp.ClusterProperty)]
        self.partition = part[0].this.sql("bigquery") if part else None
        self.cluster = [e.name for e in clus[0].expressions] if clus else []
        self.description = _description(create.args["properties"])

    def type(self, column: str) -> str:
        return self.columns[column].args["kind"].sql("bigquery")

    def not_null(self, column: str) -> bool:
        return any(isinstance(c.kind, exp.NotNullColumnConstraint) for c in self.columns[column].args.get("constraints") or [])


@pytest.fixture(scope="module")
def tables(parsed) -> dict[str, Table]:
    out: dict[str, Table] = {}
    for path, e in parsed:
        if isinstance(e, exp.Create) and e.kind == "TABLE":
            t = Table(path, e)
            assert t.name not in out, f"{t.name} is created by both {out[t.name].path.name} and {path.name}"
            out[t.name] = t
    return out


@pytest.fixture(scope="module")
def views(parsed) -> dict[str, tuple[Path, exp.Create]]:
    return {e.this.name: (p, e) for p, e in parsed if isinstance(e, exp.Create) and e.kind == "VIEW"}


# -- files and order --------------------------------------------------------------


def test_files_are_numbered_ddl_then_views():
    paths = ddl.files()
    names = [f"{p.parent.name}/{p.name}" for p in paths]
    assert names[0] == "ddl/00_dataset.sql"
    kinds = [p.parent.name for p in paths]
    assert kinds == sorted(kinds, key=["ddl", "views"].index), "ddl files before view files"
    for p in paths:
        assert re.fullmatch(r"\d{2}_[a-z0-9_]+\.sql", p.name), p.name
    for kind in ("ddl", "views"):
        numbers = [p.name[:2] for p in paths if p.parent.name == kind]
        assert len(numbers) == len(set(numbers)), f"duplicate numbers in sql/{kind}"


def test_every_statement_parses_and_is_one_create(parsed):
    for path, e in parsed:
        assert isinstance(e, exp.Create), path.name
        assert e.kind in {"SCHEMA", "TABLE", "VIEW"}, path.name


def test_dataset_is_created_in_the_location(parsed):
    path, e = parsed[0]
    assert e.kind == "SCHEMA" and e.args.get("exists")
    sql = ddl.render(path.read_text(encoding="utf-8"), PROJECT, LOCATION)
    assert f"`{PROJECT}.{DATASET}`" in sql
    assert f'location = "{LOCATION}"' in sql


# -- tables ------------------------------------------------------------------------


def test_every_store_table_has_exactly_one_ddl_file(tables):
    assert set(tables) == set(TABLE_KEYS), "sql/ddl and clients/bq.py TABLE_KEYS must list the same tables"
    for name, t in tables.items():
        assert t.path.parent.name == "ddl"
        assert t.path.name[3:] == f"{name}.sql", f"{t.path.name} should be named NN_{name}.sql"


def test_tables_are_created_if_not_exists_never_replaced(tables):
    for t in tables.values():
        assert t.create.args.get("exists"), f"{t.name}: CREATE TABLE IF NOT EXISTS"
        assert not t.create.args.get("replace"), f"{t.name}: never CREATE OR REPLACE a table"


def test_spec6_columns_exactly(tables):
    for name, cols in SPEC_COLUMNS.items():
        assert set(tables[name].columns) == cols | BUILD_ADDITIONS.get(name, set()), name


def test_build_tables_columns_exactly(tables):
    for name, cols in BUILD_TABLES.items():
        assert set(tables[name].columns) == cols, name
    for name in RAW_TABLES:
        assert set(tables[name].columns) == RAW_COLUMNS, name


def test_key_columns_present_and_required(tables):
    for name, key in TABLE_KEYS.items():
        for col in key or ():
            assert col in tables[name].columns, f"{name}.{col}"
            # A suppression entry may be email-only or domain-only, so its key columns are nullable.
            assert tables[name].not_null(col) == (name != "suppression"), f"{name}.{col} NOT NULL"


def test_json_columns_are_string(tables):
    for name, cols in JSON_COLUMNS.items():
        for col in cols:
            assert tables[name].type(col) == "STRING", f"{name}.{col} holds JSON text in a STRING"


def test_column_types(tables):
    for t in tables.values():
        for col in t.columns:
            assert t.type(col) == expected_type(col), f"{t.name}.{col}"


def test_partitioning_and_clustering(tables):
    for t in tables.values():
        want = PARTITIONS.get(t.name)
        assert t.partition == (f"DATE({want})" if want else None), t.name
        if "account_id" in t.columns:
            assert "account_id" in t.cluster, f"{t.name} clusters by account_id"


def test_enum_columns_list_their_values(tables):
    for (name, col), values in ENUMS.items():
        desc = _column_description(tables[name].columns[col])
        m = re.search(r"One of: ([^.(]+)", desc)
        assert m, f"{name}.{col} description lists its values: {desc!r}"
        assert {v.strip() for v in m.group(1).split(",")} == values, f"{name}.{col}"


def test_tables_have_spec_descriptions(tables):
    for t in tables.values():
        assert "SPEC" in t.description, f"{t.name} has a table description citing SPEC"


def test_retention_is_noted_where_it_applies(tables):
    text = {name: t.path.read_text(encoding="utf-8") for name, t in tables.items()}
    assert "90 days" in text["events"] and "reply_text" in text["events"]
    assert "12 months" in text["contacts"] and "last_step_at" in text["contacts"]
    assert "12 months" in text["accounts"]
    assert "indefinitely" in text["suppression"]


# -- views -------------------------------------------------------------------------


def test_the_six_spec_views_exist(views):
    assert SPEC_VIEWS <= set(views)
    for name, (path, e) in views.items():
        assert path.parent.name == "views" and path.name[3:] == f"{name}.sql"
        assert e.args.get("replace"), f"{name}: CREATE OR REPLACE VIEW"
        assert "SPEC" in _description(e.args["properties"]), f"{name} has a description citing SPEC"


def _schema(tables: dict[str, Table]) -> dict[str, dict[str, str]]:
    return {name: {c: t.type(c) for c in t.columns} for name, t in tables.items()}


def test_views_read_only_known_tables_and_columns(tables, views, parsed):
    """Qualify every view against the DDL (and earlier views): unknown tables or columns fail."""
    schema = _schema(tables)
    for path, e in parsed:
        if not (isinstance(e, exp.Create) and e.kind == "VIEW"):
            continue
        name = e.this.name
        body = e.expression.copy()
        ctes = {c.alias_or_name for c in body.find_all(exp.CTE)}
        for tbl in body.find_all(exp.Table):
            if tbl.name in ctes and not tbl.db:
                continue
            assert tbl.name in schema, f"{name} reads {tbl.name}, which is not created before it"
        q = qualify(body, schema={PROJECT: {DATASET: schema}}, dialect="bigquery", validate_qualify_columns=True)
        schema[name] = {c: "STRING" for c in q.named_selects}


def test_nothing_references_another_dataset(parsed):
    for path, e in parsed:
        sql = ddl.render(path.read_text(encoding="utf-8"), PROJECT, LOCATION)
        if e.kind == "SCHEMA":
            assert e.this.name == "" and e.this.db == f"{PROJECT}.{DATASET}", path.name
            continue
        ctes = {c.alias_or_name for c in e.find_all(exp.CTE)}
        for tbl in e.find_all(exp.Table):
            if tbl.name in ctes and not tbl.db:
                continue
            assert (tbl.catalog, tbl.db) == (PROJECT, DATASET), f"{path.name} references {tbl.sql('bigquery')}"
        assert ddl.check_statement(sql, PROJECT).startswith(f"{DATASET}.")


def test_v_queue_orders_for_enrollment(views):
    sql = views["v_queue"][1].sql("bigquery")
    for status in ("'verified'", "'queued'"):
        assert status in sql
    for tier in ("'Priority'", "'Standard'", "'Control'"):
        assert tier in sql
    # Size bands 20-49 and 50-99 first, then 100-249, then 10-19 (SPEC 9).
    ranks = dict(re.findall(r"WHEN '(\d+-\d+)' THEN (\d)", sql))
    assert ranks == {"20-49": "1", "50-99": "1", "100-249": "2", "10-19": "3"}
    assert "expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP()" in sql
    assert "email_sha256 IS NULL" in sql  # an email row suppresses only that email, not its domain


def test_v_heartbeats_ignores_overlap_skips(views):
    sql = views["v_heartbeats"][1].sql("bigquery")
    assert "last_alive_at" in sql and "'previous run still going'" in sql


# -- ops/ddl.py ---------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.queries: list[tuple[str, str | None]] = []

    def query(self, sql, location=None, **_):
        self.queries.append((sql, location))
        return self

    def result(self):
        return []


class NoClient:
    def query(self, *a, **k):
        raise AssertionError("dry-run must not run anything")


def test_render_fills_placeholders_and_strips_semicolon():
    out = ddl.render('CREATE SCHEMA IF NOT EXISTS `{project}.us_outbound` OPTIONS (location = "{location}");\n', "my-project", "europe-west2")
    assert out == 'CREATE SCHEMA IF NOT EXISTS `my-project.us_outbound` OPTIONS (location = "europe-west2")'


@pytest.mark.parametrize("project", ["Bad_Project", "abc", "x`; DROP TABLE y; --", "a" * 31, "ends-with-"])
def test_render_rejects_bad_project(project):
    with pytest.raises(ValueError):
        ddl.render("SELECT 1", project, LOCATION)


@pytest.mark.parametrize("location", ['EU"); DROP', "", "eu west"])
def test_render_rejects_bad_location(location):
    with pytest.raises(ValueError):
        ddl.render("SELECT 1", PROJECT, location)


def test_render_rejects_unknown_placeholders():
    with pytest.raises(ValueError, match="dataset"):
        ddl.render("CREATE TABLE IF NOT EXISTS `{project}.{dataset}.t` (a STRING)", PROJECT, LOCATION)


P = PROJECT


@pytest.mark.parametrize(
    "sql",
    [
        f"CREATE TABLE IF NOT EXISTS `{P}.other.t` (a STRING)",
        "CREATE TABLE IF NOT EXISTS `other-project.us_outbound.t` (a STRING)",
        "CREATE TABLE IF NOT EXISTS other.t (a STRING)",
        f"CREATE OR REPLACE TABLE `{P}.us_outbound.accounts` (a STRING)",
        f"CREATE TABLE `{P}.us_outbound.accounts` (a STRING)",
        f"CREATE SCHEMA IF NOT EXISTS `{P}.other`",
        f"CREATE OR REPLACE SCHEMA `{P}.us_outbound`",
        f"DROP TABLE `{P}.us_outbound.accounts`",
        f"DELETE FROM `{P}.us_outbound.accounts` WHERE TRUE",
        f"CREATE MATERIALIZED VIEW `{P}.us_outbound.v` AS SELECT 1",
        f"CREATE TEMP TABLE `{P}.us_outbound.t` (a STRING)",
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` AS SELECT * FROM `{P}.other.t`",
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` AS SELECT * FROM `{P}`.`other`.`t`",
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` AS SELECT * FROM `{P}`.other.t",
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` AS SELECT * FROM `other.t`",
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` AS SELECT * FROM proj.other.t",
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` AS SELECT * FROM `other-project.us_outbound.accounts`",
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` AS SELECT 1; DROP TABLE `{P}.us_outbound.accounts`",
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` AS SELECT 'unterminated",
        f"CREATE OR REPLACE VIEW IF NOT EXISTS `{P}.us_outbound.v` AS SELECT 1",
    ],
)
def test_check_statement_refuses(sql):
    with pytest.raises(GuardViolation):
        ddl.check_statement(sql, PROJECT)


def test_check_statement_ignores_comments_and_strings():
    sql = (
        f"-- this view once read `{P}.other.t`\n"
        f"CREATE OR REPLACE VIEW `{P}.us_outbound.v` OPTIONS (description = \"not `{P}.other.t`; nor this\") AS\n"
        f"/* `{P}.other.t` */ SELECT a.`key`, 'x.y.z' AS s FROM `{P}.us_outbound.settings` AS a"
    )
    assert ddl.check_statement(sql, PROJECT) == "us_outbound.v"


def test_check_statement_targets():
    assert ddl.check_statement(f"CREATE SCHEMA IF NOT EXISTS `{P}.us_outbound`", P) == "us_outbound.dataset"
    assert ddl.check_statement(f"CREATE TABLE IF NOT EXISTS `{P}.us_outbound.t` (a STRING)", P) == "us_outbound.t"
    assert ddl.check_statement(f"CREATE VIEW `{P}.us_outbound.v` AS SELECT 1", P) == "us_outbound.v"


def test_apply_dry_run_returns_statements_and_runs_nothing():
    guard = Guard()
    statements = ddl.apply(NoClient(), PROJECT, LOCATION, guard=guard)
    assert len(statements) == len(ddl.files())
    assert f"CREATE SCHEMA IF NOT EXISTS `{PROJECT}.us_outbound`" in statements[0]
    assert all("{project}" not in s and "{location}" not in s for s in statements)
    assert guard.calls == []
    assert ddl.apply(None, PROJECT, LOCATION) == statements  # dry-run needs no client


def test_apply_runs_every_statement_in_order_through_the_guard():
    guard, client = Guard(), FakeClient()
    statements = ddl.apply(client, PROJECT, LOCATION, dry_run=False, guard=guard)
    assert client.queries == [(s, LOCATION) for s in statements]
    calls = [c for c in guard.calls if c.system == "bq"]
    assert len(calls) == len(statements)
    assert all(c.action == "ddl" and c.write and c.sent for c in calls)
    targets = [c.target for c in calls]
    assert targets[0] == "us_outbound.dataset"
    assert set(targets[1 : 1 + len(TABLE_KEYS)]) == {f"us_outbound.{t}" for t in TABLE_KEYS}
    assert {f"us_outbound.{v}" for v in SPEC_VIEWS} <= set(targets)


def test_apply_uses_a_bigquery_stores_client_and_guard():
    guard, client = Guard(), FakeClient()
    store = BigQueryStore(guard, PROJECT, LOCATION, client=client)
    ddl.apply(store, PROJECT, LOCATION, dry_run=False)
    assert len(client.queries) == len(ddl.files())
    assert len([c for c in guard.calls if c.action == "ddl"]) == len(ddl.files())


def test_apply_refuses_the_memory_store():
    with pytest.raises(TypeError):
        ddl.apply(MemoryStore(Guard()), PROJECT, LOCATION, dry_run=False)


def test_apply_checks_every_file_before_running_any(tmp_path):
    (tmp_path / "ddl").mkdir()
    (tmp_path / "views").mkdir()
    (tmp_path / "ddl" / "00_dataset.sql").write_text("CREATE SCHEMA IF NOT EXISTS `{project}.us_outbound`;\n")
    (tmp_path / "views" / "00_v_bad.sql").write_text(
        "CREATE OR REPLACE VIEW `{project}.us_outbound.v_bad` AS SELECT * FROM `{project}.crm.companies`;\n"
    )
    client = FakeClient()
    with pytest.raises(GuardViolation, match="crm"):
        ddl.apply(client, PROJECT, LOCATION, dry_run=False, guard=Guard(), root=tmp_path)
    assert client.queries == []


def test_apply_stops_when_the_guard_refuses():
    guard, client = Guard(bounds=Boundaries(bq_dataset="somewhere_else")), FakeClient()
    with pytest.raises(GuardViolation):
        ddl.apply(client, PROJECT, LOCATION, dry_run=False, guard=guard)
    assert client.queries == []


def test_main_prints_the_statements_in_dry_run(capsys):
    assert ddl.main(["--project", PROJECT, "--location", LOCATION]) == 0
    out = capsys.readouterr().out
    assert f"CREATE SCHEMA IF NOT EXISTS `{PROJECT}.us_outbound`" in out
    assert f"CREATE OR REPLACE VIEW `{PROJECT}.us_outbound.v_heartbeats`" in out
