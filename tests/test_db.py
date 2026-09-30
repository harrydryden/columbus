"""PostgresStore and the DDL against a real Postgres (SPEC 6), and the store's DB-free rules.

The database tests need US_OUTBOUND_TEST_DSN: the URL of a Postgres 15 or later whose user
may create databases, e.g. postgresql://postgres@127.0.0.1:54329/postgres. Without it (or if
it cannot be reached) they skip. The session creates a fresh database
us_outbound_test_<random>, applies sql/ into it with ops/ddl.apply, and drops it at the end,
so runs never interfere. Every test starts from empty tables.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from us_outbound.clients.db import JSON_COLUMNS, TABLE_KEYS, PostgresStore
from us_outbound.clients.guard import Guard, GuardViolation
from us_outbound import budget
from us_outbound.ops import ddl, erase, heartbeat

DSN_ENV = "US_OUTBOUND_TEST_DSN"
SCHEMA = "us_outbound"
VIEWS = ("v_account_outcomes", "v_queue", "v_signal_value", "v_readout_weekly", "v_mailbox_health",
         "v_budgets", "v_heartbeats")
T0 = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)


# -- the session database ------------------------------------------------------------


@pytest.fixture(scope="session")
def dsn() -> Iterator[str]:
    """A fresh database with sql/ applied; dropped when the session ends."""
    admin = os.environ.get(DSN_ENV)
    if not admin:
        pytest.skip(f"{DSN_ENV} is not set, so there is no Postgres to test against")
    try:
        conn = psycopg.connect(admin, autocommit=True, connect_timeout=5)
    except psycopg.Error as exc:
        pytest.skip(f"cannot reach the Postgres in {DSN_ENV}: {exc}")
    name = f"us_outbound_test_{uuid.uuid4().hex[:12]}"
    with conn:
        if conn.info.server_version < 150000:
            pytest.skip("the DDL needs Postgres 15 or later (UNIQUE NULLS NOT DISTINCT)")
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(admin, dbname=name)
    try:
        ddl.apply(Guard(), url, dry_run=False)
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def db(dsn) -> Iterator[psycopg.Connection]:
    """A plain connection to the test database, with every table emptied first."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        names = sql.SQL(", ").join(sql.Identifier(SCHEMA, t) for t in TABLE_KEYS)
        conn.execute(sql.SQL("TRUNCATE {}").format(names))
        yield conn


@pytest.fixture
def store(db, dsn) -> Iterator[PostgresStore]:
    s = PostgresStore(Guard(), dsn)
    yield s
    s.close()


def catalog(conn) -> dict[str, list]:
    """What apply created: tables, views, columns, constraints, indexes and view definitions."""
    q = {
        "objects": "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = %s ORDER BY 1",
        "columns": "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                   "WHERE table_schema = %s ORDER BY 1, 2",
        "constraints": "SELECT conrelid::regclass::text AS t, pg_get_constraintdef(oid) AS d FROM pg_constraint "
                       "WHERE connamespace = %s::regnamespace ORDER BY 1, 2",
        "indexes": "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s ORDER BY 1",
        "views": "SELECT viewname, definition FROM pg_views WHERE schemaname = %s ORDER BY 1",
    }
    return {k: conn.execute(v, [SCHEMA]).fetchall() for k, v in q.items()}


# -- DDL -------------------------------------------------------------------------------


def test_apply_creates_every_table_and_view(db):
    objects = {r["table_name"]: r["table_type"] for r in catalog(db)["objects"]}
    assert {t for t, kind in objects.items() if kind == "BASE TABLE"} == set(TABLE_KEYS)
    assert {t for t, kind in objects.items() if kind == "VIEW"} == set(VIEWS)
    types = {(r["table_name"], r["column_name"]): r["data_type"] for r in catalog(db)["columns"]}
    for table, cols in JSON_COLUMNS.items():
        assert all(types[(table, c)] == "jsonb" for c in cols), table
    assert types[("events", "occurred_at")] == "timestamp with time zone"
    schemas = {r["nspname"] for r in db.execute("SELECT nspname FROM pg_namespace").fetchall()}
    assert {s for s in schemas if not s.startswith("pg_")} == {SCHEMA, "public", "information_schema"}
    assert db.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public'").fetchone()["n"] == 0


def test_a_second_apply_changes_nothing(db, dsn, store):
    store.insert("accounts", [{"account_id": "a1", "domain": "acme.com"}])
    before = catalog(db)
    guard = Guard()
    statements = ddl.apply(guard, dsn, dry_run=False)
    assert len([c for c in guard.calls if c.action == "ddl" and c.sent]) == len(statements)
    assert catalog(db) == before
    [row] = store.select("accounts")
    assert (row["account_id"], row["domain"]) == ("a1", "acme.com")


# -- views -----------------------------------------------------------------------------


def test_every_view_selects_on_empty_tables(store):
    for view in VIEWS:
        rows = store.query(f"SELECT * FROM {SCHEMA}.{view}")
        if view == "v_budgets":
            assert [(r["system"], r["period"], r["used"], r["budget"], r["entries"], r["pace"]) for r in rows] == [
                ("apollo", "month", 0.0, None, 0, "on"), ("claude", "month", 0.0, None, 0, "on"),
                ("clay", "month", 0.0, None, 0, "on")]
        else:
            assert rows == [], view


def settings_row(tab: str, key: str, values: dict, at: datetime = T0, effective_to: datetime | None = None) -> dict:
    return {"tab": tab, "key": key, "values": values, "effective_from": at, "effective_to": effective_to, "synced_at": at}


def account(aid: str, **kw) -> dict:
    base = {"account_id": aid, "domain": f"{aid}.com", "clean_name": aid.title(), "status": "verified",
            "tier": "Standard", "score": 20, "size_band": "20-49", "industry": "Marketing agency",
            "first_seen": T0 - timedelta(days=10)}
    return {**base, **kw}


@pytest.fixture
def fixture_rows(store) -> datetime:
    """A small realistic world; returns now (UTC). Times are relative to now so today and this month hold."""
    now = datetime.now(UTC)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    uk_month = budget.month_bounds(now)[0]  # the 1st, 00:00 UK time
    old = now - timedelta(days=30)
    store.upsert("settings", [
        settings_row("General", "clay_monthly_credits", {"key": "clay_monthly_credits", "value": "2,000"}),
        settings_row("General", "apollo_monthly_credits", {"key": "apollo_monthly_credits", "value": "500"}),
        settings_row("General", "claude_monthly_cap_usd", {"key": "claude_monthly_cap_usd", "value": "$10"}),
        # An older version of the cap, no longer in force.
        settings_row("General", "claude_monthly_cap_usd", {"value": "5"}, at=T0 - timedelta(days=9), effective_to=T0),
        settings_row("Industries", "Marketing agency", {"priority": "2", "active": "yes"}),
        settings_row("Industries", "Law firm", {"priority": " 1 ", "active": "Yes"}),
        settings_row("Industries", "Casino", {"priority": "3", "active": "no"}),
        settings_row("Signals", "eap_named", {"source": "clay_careers", "action": "Score", "weight": "+25",
                                              "max_weight": "25", "active": "yes", "suggests_angle": "Upgrade"}),
        settings_row("Signals", "old_signal", {"weight": "5", "active": "no"}),
        settings_row("Mailboxes", "Hannah@MeetSpill.org", {"domain": "meetspill.org", "owner_name": "Hannah Spalding",
                                                            "status": "Active", "daily_cap": "30"}),
    ])
    store.insert("accounts", [
        account("law", tier="Priority", score=40, industry="Law firm"),
        account("agency", tier="Priority", score=40, size_band="100-249"),
        account("queued", tier="Standard", score=30, status="queued"),
        account("noscore", tier="Standard", score=None),
        account("control", tier="Control", score=0),
        account("held", tier="Held"),
        account("new", status="new"),
        account("casino", tier="Priority", score=90, industry="Casino"),
        account("suppressed", tier="Priority", score=90),
        account("aliased", tier="Priority", score=90, domain="aliased.com"),
        account("partner", tier="Priority", score=90),
        account("sent", tier="Priority", score=10, status="enrolled"),
    ])
    store.insert("suppression", [
        {"email_sha256": None, "domain": "suppressed.com", "reason": "unsubscribe", "added_at": old},
        {"email_sha256": None, "domain": "old-alias.com", "reason": "unsubscribe", "added_at": old},
        {"email_sha256": "abc", "domain": "queued.com", "reason": "bounce", "added_at": old},  # one email only
        {"email_sha256": None, "domain": "law.com", "reason": "signal", "added_at": old, "expires_at": old},  # expired
    ])
    store.insert("domain_aliases", [{"alias": "old-alias.com", "root_domain": "aliased.com", "source": "redirect"}])
    store.insert("partners", [{"domain": "PARTNER.com", "name": "Partner", "reason": "broker"}])
    store.insert("contacts", [
        {"contact_id": "k-sent", "account_id": "sent", "email_sha256": "h1", "copy_version": "v1", "mailbox": "hannah@meetspill.org"},
        {"contact_id": "k-control", "account_id": "control", "email_sha256": "h2", "copy_version": "v1"},
    ])
    store.insert("events", [
        {"event_id": "s1", "contact_id": "k-sent", "account_id": "sent", "type": "sent", "step": 1,
         "mailbox": "Hannah@meetspill.org", "occurred_at": now - timedelta(days=29)},  # the 28-day window has closed
        {"event_id": "r1", "contact_id": "k-sent", "account_id": "sent", "type": "replied", "reply_class": "positive",
         "language_terms": ["EAP", "burnout"], "occurred_at": now - timedelta(days=28)},
        {"event_id": "s2", "contact_id": "k-control", "account_id": "control", "type": "sent", "step": 1,
         "mailbox": "hannah@meetspill.org", "occurred_at": now},
        {"event_id": "b2", "contact_id": "k-control", "account_id": "control", "type": "bounced", "step": 1,
         "occurred_at": now},
        {"event_id": "v1", "account_id": "sent", "type": "site_visit", "occurred_at": now - timedelta(days=30)},
        {"event_id": "m1", "account_id": "sent", "type": "meeting_booked", "occurred_at": now - timedelta(days=20)},
    ])
    store.insert("signal_events", [
        {"event_id": "se1", "account_id": "sent", "source": "scoring", "fact": "signal_matched",
         "value": {"signal": "eap_named", "weight": 25}, "observed_at": now - timedelta(days=26)},
        {"event_id": "se2", "account_id": "sent", "source": "apollo_org", "fact": "open_roles", "value": 4,
         "observed_at": now - timedelta(days=26)},
    ])
    store.insert("credit_ledger", [
        {"entry_id": "c1", "system": "clay", "credits": 10.0, "usd": 0.0, "occurred_at": uk_month + timedelta(minutes=1)},
        {"entry_id": "c2", "system": "clay", "credits": 5.0, "occurred_at": uk_month + timedelta(minutes=2)},
        {"entry_id": "c3", "system": "apollo", "credits": 3.0, "occurred_at": uk_month + timedelta(minutes=3)},
        {"entry_id": "c4", "system": "claude", "usd": 0.25, "occurred_at": month + timedelta(minutes=4)},
        {"entry_id": "c5", "system": "clay", "credits": 100.0, "occurred_at": uk_month - timedelta(minutes=1)},  # last month (UK)
        {"entry_id": "c6", "system": "claude", "usd": 3.0, "occurred_at": month - timedelta(minutes=1)},  # last month (UTC)
    ])
    store.insert("heartbeats", [
        {"run_id": "h1", "job": "enrol", "status": "ok", "dry_run": True, "started_at": now - timedelta(hours=3),
         "finished_at": now - timedelta(hours=3) + timedelta(seconds=90), "detail": {"enrolled": 0}},
        {"run_id": "h2", "job": "enrol", "status": "running", "started_at": now - timedelta(hours=2)},
        {"run_id": "h3", "job": "enrol", "status": "skipped", "started_at": now - timedelta(hours=1),
         "finished_at": now - timedelta(hours=1), "detail": {"skipped": True, "reason": "previous run still going"}},
    ])
    return now


def test_v_queue_on_a_fixture(store, fixture_rows):
    rows = sorted(store.query(f"SELECT * FROM {SCHEMA}.v_queue"), key=lambda r: (r["tier_rank"], r["rank_in_tier"]))
    order = [(r["account_id"], r["tier"], r["rank_in_tier"]) for r in rows]
    # Priority: equal scores, so size band (20-49 before 100-249); casino's industry is off,
    # suppressed / aliased / partner are left out; Standard: the unscored account comes last.
    assert order == [
        ("law", "Priority", 1), ("agency", "Priority", 2),
        ("queued", "Standard", 1), ("noscore", "Standard", 2),
        ("control", "Control", 1),
    ]
    law = rows[0]
    assert law["verified"] is True and law["industry_priority"] == 1 and law["size_band_rank"] == 1
    assert rows[2]["verified"] is False and rows[2]["industry_priority"] == 2
    assert law["first_seen"].tzinfo == UTC
    assert set(law) >= {"account_id", "domain", "tier", "score", "tier_rank", "size_band_rank", "industry_priority",
                        "rank_in_tier", "verified", "sender", "angle"}


def test_v_budgets_on_a_fixture(store, fixture_rows):
    now = fixture_rows
    rows = {r["system"]: r for r in store.query(f"SELECT * FROM {SCHEMA}.v_budgets")}
    assert set(rows) == {"clay", "apollo", "claude"}
    clay, apollo, claude = rows["clay"], rows["apollo"], rows["claude"]
    # Apollo and Clay by the UK calendar month, as budget.py counts it; Claude by the UTC month.
    assert clay["period"] == apollo["period"] == claude["period"] == "month"
    assert clay["period_start"] == budget.month_bounds(now)[0]
    assert claude["period_start"] == now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # The month's pace matches budget.py on the same ledger.
    from us_outbound.settings.model import General, Settings

    s = Settings(general=General(clay_monthly_credits=2000.0))
    b = budget.monthly(store, s, "clay", now)
    assert clay["weekdays_in_month"] == b.weekdays_in_month and clay["weekdays_through_today"] == b.weekdays_through_today
    assert clay["expected_by_now"] == pytest.approx(b.expected_by_now) and clay["pace"] == b.pace
    assert clay["projected"] == pytest.approx(b.projected)
    assert (clay["used"], clay["budget"], clay["remaining"], clay["entries"]) == (15.0, 2000.0, 1985.0, 2)
    assert clay["share_used"] == pytest.approx(0.0075)
    assert (apollo["used"], apollo["budget"], apollo["remaining"]) == (3.0, 500.0, 497.0)
    assert (claude["unit"], claude["used"], claude["budget"], claude["remaining"]) == ("usd", 0.25, 10.0, 9.75)
    assert claude["credits_used"] == 0.0


def test_the_other_views_on_a_fixture(store, fixture_rows):
    q = lambda view: store.query(f"SELECT * FROM {SCHEMA}.{view}")  # noqa: E731
    outcomes = {r["account_id"]: r for r in q("v_account_outcomes")}
    assert set(outcomes) == {"sent", "control"}
    assert outcomes["sent"]["delivered"] and outcomes["sent"]["replied_in_window"] and outcomes["sent"]["positive_in_window"]
    assert outcomes["sent"]["window_closed"] is True
    assert outcomes["control"]["delivered"] is False and outcomes["control"]["window_closed"] is False

    [signal] = q("v_signal_value")
    assert (signal["signal"], signal["weight"], signal["max_weight"], signal["accounts"]) == ("eap_named", 25, 25, 1)
    assert (signal["reply_rate"], signal["positive_rate"], signal["below_control"]) == (1.0, 1.0, False)
    assert signal["control_accounts_delivered"] == 0 and signal["control_reply_rate"] is None

    [mailbox] = q("v_mailbox_health")
    assert (mailbox["address"], mailbox["daily_cap"], mailbox["sends_today"], mailbox["cap_left_today"]) == (
        "hannah@meetspill.org", 30, 1, 29)
    assert (mailbox["sends_total"], mailbox["bounces_last_100"], mailbox["bounce_rate_last_100"]) == (2, 1, 0.5)
    assert mailbox["hours_since_last_send"] == 0

    [enrol] = q("v_heartbeats")
    assert (enrol["run_id"], enrol["status"], enrol["runs"], enrol["unfinished"]) == ("h3", "skipped", 3, False)
    assert enrol["last_ok_at"] == enrol["last_alive_at"]  # the overlap skip is no sign of life
    assert enrol["minutes_since_ok"] in (178, 179) and enrol["age_minutes"] in (60, 61)

    readout = q("v_readout_weekly")
    alls = [r for r in readout if r["cut"] == "all"]
    assert sum(r["accounts_enrolled"] for r in alls) == 2
    assert sum(r["meetings_booked"] for r in alls) == 1
    assert sum(r["site_visits_before_first_email"] for r in alls) == 1
    assert {r["week_start"].weekday() for r in readout} == {0}  # ISO weeks start on Monday
    copy = [r for r in readout if r["cut"] == "copy_version" and r["accounts_enrolled"]]
    assert {r["cut_value"] for r in copy} == {"v1"}


# -- PostgresStore round trips -------------------------------------------------------------

SAMPLES = {
    "text": lambda col: f"{col}-1",
    "integer": lambda col: 7,
    "double precision": lambda col: 1.5,
    "boolean": lambda col: True,
    "timestamp with time zone": lambda col: T0,
    "jsonb": lambda col: {"column": col, "list": [1, "two", None], "ok": True, "n": 1.25},
}


def full_row(db, table: str) -> dict:
    cols = db.execute("SELECT column_name, data_type FROM information_schema.columns "
                      "WHERE table_schema = %s AND table_name = %s", [SCHEMA, table]).fetchall()
    return {c["column_name"]: SAMPLES[c["data_type"]](c["column_name"]) for c in cols}


@pytest.mark.parametrize("table", sorted(TABLE_KEYS))
def test_every_table_round_trips(db, store, table):
    row = full_row(db, table)
    assert store.insert(table, [row]) == 1
    [back] = store.select(table)
    assert back == row
    assert all(v.tzinfo is UTC for v in back.values() if isinstance(v, datetime))
    key = TABLE_KEYS[table]
    if key is None:
        with pytest.raises(ValueError, match="append-only"):
            store.upsert(table, [row])
        assert store.update(table, {"run_id": row["run_id"]}, {"payload": ["replaced"]}) == 1
        assert store.select(table)[0]["payload"] == ["replaced"]
        assert store.delete(table, {"key": row["key"]}) == 1
        assert store.select(table) == []
        return
    ident = {c: row[c] for c in key}
    assert store.get(table, **ident) == row
    other = next(c for c in row if c not in key)
    # upsert sets only the columns a row carries; the rest stay.
    assert store.upsert(table, [{**ident, other: None}]) == 1
    assert store.get(table, **ident) == {**row, other: None}
    fresh = {**row, **{c: (T0 + timedelta(days=1) if isinstance(row[c], datetime) else f"{row[c]}-2") for c in key}}
    assert store.upsert(table, [fresh]) == 1
    assert len(store.select(table)) == 2
    assert store.update(table, ident, {other: row[other]}) == 1
    assert store.get(table, **ident) == row
    assert store.delete(table, ident) == 1
    assert store.select(table) == [fresh]


def test_where_semantics(store):
    store.insert("accounts", [
        {"account_id": "a1", "tier": "Priority", "score": 10},
        {"account_id": "a2", "tier": None, "score": 20},
        {"account_id": "a3", "tier": "Standard", "score": 30},
    ])
    ids = lambda rows: sorted(r["account_id"] for r in rows)  # noqa: E731
    assert ids(store.select("accounts", {"tier": None})) == ["a2"]
    assert store.select("accounts", {"tier": []}) == []
    assert ids(store.select("accounts", {"tier": ["Priority", "Standard"]})) == ["a1", "a3"]
    assert ids(store.select("accounts", {"tier": ("Priority", None)})) == ["a1", "a2"]
    assert ids(store.select("accounts", {"score": {10, 30}, "tier": "Standard"})) == ["a3"]
    assert ids(store.select("accounts", {"tier": [None]})) == ["a2"]
    assert store.update("accounts", {"account_id": []}, {"score": 1}) == 0
    assert store.update("accounts", {"tier": None}, {}) == 1  # nothing to set: counts the matches
    assert store.delete("accounts", {"score": []}) == 0
    assert ids(store.select("accounts")) == ["a1", "a2", "a3"]


def test_insert_rows_with_different_columns(store):
    store.insert("contacts", [{"contact_id": "k1", "first_name": "Jane"}, {"contact_id": "k2", "last_name": "Roe"}])
    rows = {r["contact_id"]: r for r in store.select("contacts")}
    assert (rows["k1"]["first_name"], rows["k1"]["last_name"]) == ("Jane", None)
    assert (rows["k2"]["first_name"], rows["k2"]["last_name"]) == (None, "Roe")


def test_upsert_keeps_row_order_across_column_sets(store):
    store.upsert("accounts", [
        {"account_id": "a1", "tier": "Priority"},
        {"account_id": "a1", "score": 5},
        {"account_id": "a1", "tier": "Standard"},
        {"account_id": "a1"},  # key only: nothing to update
    ])
    got = store.get("accounts", account_id="a1")
    assert (got["tier"], got["score"]) == ("Standard", 5)


def test_suppression_upsert_with_null_parts_dedupes(store):
    store.upsert("suppression", [{"email_sha256": "h", "domain": None, "reason": "a"}])
    store.upsert("suppression", [{"email_sha256": "h", "domain": None, "reason": "b"}])
    store.upsert("suppression", [{"email_sha256": None, "domain": "acme.com", "reason": "c"}])
    store.upsert("suppression", [{"domain": "acme.com", "reason": "d"}])  # a missing key part is NULL
    store.upsert("suppression", [{"email_sha256": "h", "domain": "acme.com", "reason": "e"}])
    rows = {(r["email_sha256"], r["domain"], r["reason"]) for r in store.select("suppression")}
    assert rows == {("h", None, "b"), (None, "acme.com", "d"), ("h", "acme.com", "e")}
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.insert("suppression", [{"email_sha256": "h", "domain": None}])


def test_jsonb_values(store):
    payload = {"nested": {"a": [1, 2, {"b": None}]}, "text": "quote ' and \" and é", "when": T0}
    store.insert("hitl_items", [{"item_id": "i1", "payload": payload}, {"item_id": "i2", "payload": None}])
    store.insert("events", [{"event_id": "e1", "language_terms": ["EAP"]}, {"event_id": "e2", "language_terms": []}])
    store.insert("signal_events", [{"event_id": "s1", "value": "text"}, {"event_id": "s2", "value": 4}])
    items = {r["item_id"]: r["payload"] for r in store.select("hitl_items")}
    assert items == {"i1": {**payload, "when": str(T0)}, "i2": None}  # a datetime inside JSON is kept as text
    assert {r["event_id"]: r["language_terms"] for r in store.select("events")} == {"e1": ["EAP"], "e2": []}
    assert {r["event_id"]: r["value"] for r in store.select("signal_events")} == {"s1": "text", "s2": 4}
    assert [r["event_id"] for r in store.select("signal_events", {"value": 4})] == ["s2"]
    assert [r["event_id"] for r in store.select("events", {"language_terms": [["EAP"], ["x"]]})] == ["e1"]


def test_timestamps_come_back_aware_utc(store):
    naive = datetime(2026, 10, 1, 9, 0)  # taken as UTC
    london = datetime(2026, 7, 1, 9, 0, tzinfo=ZoneInfo("Europe/London"))  # BST: 08:00 UTC
    store.insert("heartbeats", [{"run_id": "n", "started_at": naive}, {"run_id": "l", "started_at": london}])
    got = {r["run_id"]: r["started_at"] for r in store.select("heartbeats")}
    assert got == {"n": naive.replace(tzinfo=UTC), "l": datetime(2026, 7, 1, 8, 0, tzinfo=UTC)}
    assert all(v.tzinfo is UTC for v in got.values())
    assert store.select("heartbeats", {"started_at": london})[0]["run_id"] == "l"
    [row] = store.query(f"SELECT max(started_at) AS at FROM {SCHEMA}.heartbeats")
    assert row["at"].tzinfo is UTC


def test_query_binds_params(store):
    store.insert("contacts", [{"contact_id": "k1", "email": "  Jane@Acme.com "}])
    rows = store.query(f"SELECT contact_id FROM {SCHEMA}.contacts WHERE lower(trim(email)) = %(e)s", {"e": "jane@acme.com"})
    assert rows == [{"contact_id": "k1"}]


def test_query_is_read_only(db, store):
    store.insert("accounts", [{"account_id": "a1"}])
    with pytest.raises(GuardViolation):
        store.query(f"DELETE FROM {SCHEMA}.accounts")
    with pytest.raises(GuardViolation):
        store.query(f"WITH gone AS (DELETE FROM {SCHEMA}.accounts RETURNING *) SELECT * FROM gone")
    with pytest.raises(psycopg.errors.SyntaxError):  # one statement only: no COMMIT out of the READ ONLY
        store.query(f"SELECT 1; COMMIT; DELETE FROM {SCHEMA}.accounts")
    with pytest.raises(GuardViolation):
        store.query(f"SELECT * FROM {SCHEMA}.accounts FOR UPDATE")
    assert [r["account_id"] for r in store.select("accounts")] == ["a1"]
    assert store.select("accounts", {"account_id": "a1"})  # the connection is still usable


def test_close_and_reconnect(store):
    store.insert("partners", [{"domain": "a.com"}])
    store.close()
    store.close()
    assert [r["domain"] for r in store.select("partners")] == ["a.com"]


def test_erase_and_heartbeat_sql_run_on_postgres(store):
    """ops/erase.py and ops/heartbeat.py query the database directly; their SQL must run on it."""
    store.insert("contacts", [{"contact_id": "k1", "email": "Jane.Doe@Acme.com "}, {"contact_id": "k2", "email": "j@x.com"}])
    store.insert("raw_clay_contacts", [{"key": "acme.com|Jane Doe", "payload": {"email": "Jane.Doe@acme.com"}},
                                       {"key": "x.com|J", "payload": {"email": "j@x.com"}}])
    ctx = SimpleNamespace(store=store)
    assert [c["contact_id"] for c in erase._contacts_by_email(ctx, "jane.doe@acme.com")] == ["k1"]
    assert erase._raw_contact_keys(ctx, "jane.doe@acme.com") == ["acme.com|Jane Doe"]
    now = datetime.now(UTC)
    store.insert("heartbeats", [{"run_id": "r1", "job": "enrol", "status": "ok", "started_at": now, "finished_at": now}])
    runs = heartbeat.latest_runs(store)
    assert runs["enrol"]["run_id"] == "r1" and runs["enrol"]["last_ok_at"] == now


# -- DB-free: the store's rules hold before any SQL ---------------------------------------------


def no_connection():
    raise AssertionError("must not connect")


class OtherSchema(PostgresStore):
    schema = "spill_prod"


@pytest.mark.parametrize("call", [
    lambda s: s.insert("accounts", [{"account_id": "x"}]),
    lambda s: s.upsert("accounts", [{"account_id": "x"}]),
    lambda s: s.update("accounts", {"account_id": "x"}, {"tier": "Held"}),
    lambda s: s.delete("accounts", {"account_id": "x"}),
])
def test_a_write_to_another_schema_is_refused_before_any_sql(call):
    guard = Guard()
    with pytest.raises(GuardViolation, match="us_outbound"):
        call(OtherSchema(guard, "postgresql://unused", connect=no_connection))
    assert [c.sent for c in guard.calls] == [False]


def test_unknown_tables_bad_columns_and_callables_are_refused_before_any_sql():
    s = PostgresStore(Guard(), "postgresql://unused", connect=no_connection)
    with pytest.raises(GuardViolation, match="unknown table"):
        s.select("hubspot_contacts")
    with pytest.raises(GuardViolation, match="unknown table"):
        s.insert("hubspot_contacts", [{"id": "x"}])
    with pytest.raises(ValueError, match="callable"):
        s.select("accounts", {"score": lambda v: v > 1})
    with pytest.raises(ValueError, match="bad column"):
        s.select("accounts", {"tier; DROP TABLE x": "a"})
    with pytest.raises(ValueError, match="bad column"):
        s.update("accounts", {"account_id": "a"}, {'tier" = 1 --': "a"})
    with pytest.raises(ValueError, match="append-only"):
        s.upsert("raw_layoffs", [{"key": "x"}])
    with pytest.raises(GuardViolation, match="read only"):
        s.query("UPDATE us_outbound.accounts SET tier = 'x'")
    assert s.insert("accounts", []) == 0 and s.upsert("accounts", []) == 0  # nothing to write: no connection


def test_bad_columns_are_refused_before_connecting():
    s = PostgresStore(Guard(), "postgresql://unused", connect=no_connection)
    with pytest.raises(ValueError, match="bad column"):
        s.insert("accounts", [{"account_id": "a", "Tier": "x"}])
    with pytest.raises(ValueError, match="bad column"):
        s.upsert("accounts", [{"account_id": "a"}, {"account_id": "b", "tier x": "y"}])
