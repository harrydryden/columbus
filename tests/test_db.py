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


def test_apply_over_views_an_older_version_left(db, dsn):
    """A deploy must apply over the views the last version made, even when their columns
    changed (CREATE OR REPLACE VIEW alone cannot rename a column): this failed on Railway
    when v_account_outcomes.replied_21d became replied_in_window."""
    before = catalog(db)
    db.execute(f"DROP VIEW {SCHEMA}.v_readout_weekly")
    db.execute(f"DROP VIEW {SCHEMA}.v_signal_value")
    db.execute(f"DROP VIEW {SCHEMA}.v_account_outcomes")
    db.execute(f"DROP VIEW {SCHEMA}.v_budgets")
    db.execute(f"CREATE VIEW {SCHEMA}.v_account_outcomes AS SELECT 'x'::text AS account_id, true AS replied_21d")
    db.execute(f"CREATE VIEW {SCHEMA}.v_signal_value AS SELECT replied_21d FROM {SCHEMA}.v_account_outcomes")
    db.execute(f"CREATE VIEW {SCHEMA}.v_budgets AS SELECT 'clay'::text AS system, 'week'::text AS period")
    db.execute(f"CREATE VIEW {SCHEMA}.v_credits_month AS SELECT 1 AS n")  # retired, dropped by the apply
    ddl.apply(Guard(), dsn, dry_run=False)
    assert catalog(db) == before
    cols = {r["column_name"] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'v_account_outcomes'",
        (SCHEMA,)).fetchall()}
    assert "replied_in_window" in cols and "replied_21d" not in cols


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


def test_v_signal_value_judges_what_an_account_showed_when_it_was_enrolled(store, fixture_rows):
    """Scoring rewrites the matches every run; the contact's snapshot at enrolment wins (Harry, 5 Oct 2026)."""
    q = lambda view: store.query(f"SELECT * FROM {SCHEMA}.{view}")  # noqa: E731
    # When it was enrolled, "sent" showed no signal and was in Control: today's eap_named match does not count.
    store.update("contacts", {"contact_id": "k-sent"}, {"signals_at_enrol": [], "tier_at_enrol": "Control"})
    [signal] = q("v_signal_value")
    assert (signal["signal"], signal["accounts"], signal["accounts_delivered"]) == ("eap_named", 0, 0)
    assert (signal["control_accounts_delivered"], signal["control_reply_rate"]) == (1, 1.0)
    outcome = {r["account_id"]: r for r in q("v_account_outcomes")}["sent"]
    assert (outcome["signals_at_enrol"], outcome["tier_at_enrol"]) == ([], "Control")

    store.update("contacts", {"contact_id": "k-sent"},
                 {"signals_at_enrol": [{"signal": "eap_named", "weight": 25}], "tier_at_enrol": "Priority"})
    [signal] = q("v_signal_value")
    assert (signal["accounts"], signal["accounts_delivered"], signal["reply_rate"]) == (1, 1, 1.0)
    assert signal["control_accounts_delivered"] == 0


def learning_world(store) -> datetime:
    """The learning loop's views (Harry, 6 Oct 2026): 70 companies emailed six weeks ago, half enrolled with
    eap_named; one more enrolled with it but not yet sent. Returns now (UTC)."""
    now = datetime.now(UTC)
    monday = (now - timedelta(days=49)).date()
    monday -= timedelta(days=monday.weekday())
    sent_at = datetime(monday.year, monday.month, monday.day, 12, tzinfo=UTC) + timedelta(days=1)  # a Tuesday noon
    store.upsert("settings", [
        settings_row("Signals", "eap_named", {"source": "careers_pages", "action": "Score", "weight": "10",
                                              "active": "yes"}),
        settings_row("Signals", "hiring", {"source": "apollo_org", "action": "Score", "weight": "15", "active": "yes"}),
    ])
    accounts, contacts, events = [], [], []
    for i in range(71):
        aid, with_it = f"a{i:02d}", i < 35 or i == 70
        accounts.append(account(aid, status="enrolled", tier="Priority" if with_it else "Control",
                                angle="Upgrade the EAP" if with_it else "General", sender="Hannah Spalding",
                                industry_group="Marketing & Creative Agencies"))
        contacts.append({"contact_id": f"k{i:02d}", "account_id": aid, "email_sha256": f"h{i}", "copy_version": "v1",
                         "enrolled_at": sent_at - timedelta(hours=1), "angle": accounts[-1]["angle"],
                         "tier_at_enrol": accounts[-1]["tier"],
                         "signals_at_enrol": [{"signal": "eap_named", "weight": 10}] if with_it else []})
        if i == 70:
            continue  # enrolled, step 1 not sent yet
        events.append({"event_id": f"s{i}", "contact_id": f"k{i:02d}", "account_id": aid, "type": "sent", "step": 1,
                       "mailbox": "hannah@meetspill.org", "occurred_at": sent_at})
        replied = i < 10 or 35 <= i < 37
        if replied:
            events.append({"event_id": f"r{i}", "contact_id": f"k{i:02d}", "account_id": aid, "type": "replied",
                           "step": 1, "reply_class": "positive" if i < 3 else "objection",
                           "occurred_at": sent_at + timedelta(days=2)})
    events += [
        {"event_id": "b69", "contact_id": "k69", "account_id": "a69", "type": "bounced", "step": 1, "occurred_at": sent_at},
        # Two bookings at a00 in one week (a meeting and a deal) are one company; a01's meeting came before step 1.
        {"event_id": "hs-meeting:1", "account_id": "a00", "type": "meeting_booked", "source": "hubspot_meeting",
         "occurred_at": sent_at + timedelta(days=1)},
        {"event_id": "hs-deal:2:booked", "account_id": "a00", "type": "meeting_booked", "source": "hubspot_deal",
         "occurred_at": sent_at + timedelta(days=1, hours=1)},
        {"event_id": "hs-meeting:3", "account_id": "a01", "type": "meeting_booked", "occurred_at": sent_at - timedelta(days=7)},
        {"event_id": "hs-meeting:4", "account_id": "a02", "type": "meeting_booked", "occurred_at": sent_at + timedelta(days=35)},
    ]
    store.insert("accounts", accounts)
    store.insert("contacts", contacts)
    store.insert("events", events)
    return now


def test_the_learning_loop_views(store):
    """v_account_outcomes counts a meeting after step 1; v_signal_value compares with and without each signal, with
    meetings and too_few; v_readout_weekly cuts by tier, angle, sender and step, and counts a booking once."""
    learning_world(store)
    q = lambda view: store.query(f"SELECT * FROM {SCHEMA}.{view}")  # noqa: E731
    outcomes = {r["account_id"]: r for r in q("v_account_outcomes")}
    assert len(outcomes) == 70 and outcomes["a00"]["meeting_booked"] and outcomes["a02"]["meeting_booked"]
    assert not outcomes["a01"]["meeting_booked"]  # booked before we wrote to them
    assert (outcomes["a00"]["angle"], outcomes["a00"]["copy_version"]) == ("Upgrade the EAP", "v1")

    signals = {r["signal"]: r for r in q("v_signal_value")}
    eap = signals["eap_named"]
    assert (eap["accounts_enrolled"], eap["accounts_sent"], eap["accounts_delivered"]) == (36, 35, 35)
    assert (eap["accounts_replied"], eap["accounts_positive"], eap["accounts_meeting"]) == (10, 3, 2)
    assert (eap["without_delivered"], eap["without_replied"], eap["without_meeting"]) == (34, 2, 0)
    assert eap["reply_rate"] == 10 / 35 and eap["without_reply_rate"] == 2 / 34 and eap["meeting_rate"] == 2 / 35
    assert eap["too_few"] is False
    hiring = signals["hiring"]
    assert (hiring["accounts_enrolled"], hiring["accounts_sent"], hiring["without_delivered"]) == (0, 0, 69)
    assert hiring["too_few"] is True and hiring["reply_rate"] is None

    readout = q("v_readout_weekly")
    cuts = {(r["cut"], r["cut_value"]) for r in readout}
    assert {("tier", "Priority"), ("tier", "Control"), ("angle", "General"), ("sender", "Hannah Spalding"),
            ("industry_group", "Marketing & Creative Agencies"), ("step", "step 1"), ("all", "all")} <= cuts
    alls = [r for r in readout if r["cut"] == "all"]
    assert sum(r["sends"] for r in alls) == 70 and sum(r["bounces"] for r in alls) == 1
    assert sum(r["replies"] for r in alls) == 12 and sum(r["positive_replies"] for r in alls) == 3
    assert sum(r["accounts_meeting"] for r in alls) == 2 and sum(r["accounts_window_closed"] for r in alls) == 69
    booked = {r["week_start"]: r["meetings_booked"] for r in alls if r["meetings_booked"]}
    assert sorted(booked.values()) == [1, 1, 1]  # a00 once (meeting and deal), a01 and a02
    step = [r for r in readout if r["cut"] == "step"]
    assert sum(r["sends"] for r in step) == 70 and sum(r["replies"] for r in step) == 12
    assert all(r["accounts_enrolled"] == 0 for r in step)  # the step cut has activity only
    priority = [r for r in readout if (r["cut"], r["cut_value"]) == ("tier", "Priority")]
    assert sum(r["accounts_enrolled"] for r in priority) == 35 and sum(r["accounts_replied"] for r in priority) == 10


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


def test_latest_is_one_row_by_the_greatest_value(store):
    store.insert("heartbeats", [
        {"run_id": "r1", "job": "sync_outcomes", "status": "ok", "started_at": T0, "dry_run": False, "detail": {"n": 1}},
        {"run_id": "r2", "job": "sync_outcomes", "status": "ok", "started_at": T0 + timedelta(hours=1), "dry_run": True},
        {"run_id": "r3", "job": "sync_outcomes", "status": "error", "started_at": T0 + timedelta(hours=2)},
        {"run_id": "r4", "job": "sync_outcomes", "status": "ok", "started_at": None},
        {"run_id": "r5", "job": "poll_replies", "status": "ok", "started_at": T0 + timedelta(hours=3)},
    ])
    assert store.latest("heartbeats", "started_at", {"job": "sync_outcomes", "status": "ok"})["run_id"] == "r2"
    live = store.latest("heartbeats", "started_at", {"job": "sync_outcomes", "status": "ok", "dry_run": [False, None]})
    assert live["run_id"] == "r1" and live["detail"] == {"n": 1} and live["started_at"] == T0
    assert store.latest("heartbeats", "started_at", {"job": "nothing"}) is None
    assert store.latest("heartbeats", "started_at")["run_id"] == "r5"


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
    store.insert("hitl_items", [{"item_id": "i1", "contact_id": "k2", "payload": {"referral": {"email": "Jane.Doe@acme.com"}}},
                                {"item_id": "i2", "contact_id": "k2", "payload": {"reply_excerpt": "no"}},
                                {"item_id": "i3", "contact_id": "k2", "payload": None}])
    store.insert("events", [{"event_id": "e1", "contact_id": "k2", "reply_text": "ask JANE.DOE@ACME.COM"},
                            {"event_id": "e2", "contact_id": "k2", "reply_text": "no"}])
    items, events = erase._mentions(ctx, "jane.doe@acme.com")
    assert [i["item_id"] for i in items] == ["i1"] and [e["event_id"] for e in events] == ["e1"]
    now = datetime.now(UTC)
    store.insert("heartbeats", [{"run_id": "r1", "job": "enrol", "status": "ok", "started_at": now, "finished_at": now}])
    runs = heartbeat.latest_runs(store)
    assert runs["enrol"]["run_id"] == "r1" and runs["enrol"]["last_ok_at"] == now


def test_retention_sql_runs_on_postgres(store):
    """ops/retention.py finds old reply text with SQL on Postgres (the tests' MemoryStore does it in Python)."""
    from us_outbound.ops import retention

    old, recent = T0 - timedelta(days=91), T0 - timedelta(days=89)
    store.insert("events", [
        {"event_id": "e-old", "type": "replied", "reply_text": "Not for us", "occurred_at": old},
        {"event_id": "e-empty", "type": "replied", "reply_text": "", "occurred_at": old},
        {"event_id": "e-none", "type": "replied", "reply_text": None, "occurred_at": old},
        {"event_id": "e-recent", "type": "replied", "reply_text": "Yes", "occurred_at": recent},
    ])
    store.insert("hitl_items", [
        {"item_id": "i-old", "kind": "reply", "created_at": old, "payload": {"reply_excerpt": "Not for us"}},
        {"item_id": "i-ooo", "kind": "out_of_office", "created_at": old, "payload": {"reply_excerpt": "Away"}},
        {"item_id": "i-done", "kind": "reply", "created_at": old,
         "payload": {"reply_excerpt": None, "reply_text_purged_at": old.isoformat()}},
        {"item_id": "i-erased", "kind": "reply", "created_at": old, "payload": None},
        {"item_id": "i-recent", "kind": "reply", "created_at": recent, "payload": {"reply_excerpt": "Yes"}},
        {"item_id": "i-card", "kind": "send_approval", "created_at": old, "payload": {"lead": {}}},
    ])
    events, items = retention._old_reply_texts(SimpleNamespace(store=store), T0 - timedelta(days=90))
    assert events == ["e-empty", "e-old"]
    assert sorted(i["item_id"] for i in items) == ["i-old", "i-ooo"] and items[0]["payload"]["reply_excerpt"]

    store.insert("raw_clay_contacts", [{"key": "acme.com|Jane Doe", "payload": {"email": "Jane.Doe@acme.com"}},
                                       {"key": "x.com|J", "payload": {"email": "j@x.com"}},
                                       {"key": "y.com|K", "payload": {"email": "jane_doe@acme.com"}}])
    ctx = SimpleNamespace(store=store)
    assert retention._raw_contact_keys(ctx, ["jane.doe@acme.com", "nobody@z.com"]) == ["acme.com|Jane Doe"]
    assert retention._raw_contact_keys(ctx, []) == []


def test_retention_finds_the_same_stale_universe_rows_on_postgres(store):
    """The universe rule's SQL (Postgres) and its Python (the tests' MemoryStore) agree on every case."""
    from tests.test_retention import UNIVERSE_NOW, universe_rows
    from us_outbound.ops import retention

    universe_rows(store)
    before = retention.months_before(UNIVERSE_NOW, retention.UNIVERSE_MONTHS)
    assert retention.stale_accounts(SimpleNamespace(store=store), before) == ["a-stale", "a-stale-too"]


def test_job_errors_sql_and_the_alert_keys_run_on_postgres(store):
    """ops/job_errors.py reads each job's latest finished run with its detail; notify.post_once keeps its keys in
    events (Harry, 7 Oct 2026)."""
    from us_outbound.ops import job_errors, notify

    now = datetime.now(UTC)
    store.insert("heartbeats", [
        {"run_id": "e1", "job": "enrol", "status": "ok", "started_at": now - timedelta(hours=2),
         "detail": {"errors": ["older"]}},
        {"run_id": "e2", "job": "enrol", "status": "ok", "started_at": now - timedelta(hours=1),
         "detail": {"errors": ["latest"], "send_approvals": {"errors": []}}},
        {"run_id": "e3", "job": "enrol", "status": "running", "started_at": now},
        {"run_id": "k1", "job": "kill_rules", "status": "ok", "started_at": now - timedelta(hours=30), "detail": {}},
    ])
    ctx = SimpleNamespace(store=store, now=now)
    runs = job_errors.latest_finished(ctx, ["enrol", "kill_rules"])
    assert set(runs) == {"enrol"} and runs["enrol"]["run_id"] == "e2"
    assert runs["enrol"]["detail"] == {"errors": ["latest"], "send_approvals": {"errors": []}}
    store.upsert("events", [{"event_id": "alert:apollo_floor:2026-10-07", "type": "alert", "occurred_at": now}])
    ctx = SimpleNamespace(store=store, dry_run=False)
    assert notify.sent_before(ctx, ["apollo_floor:2026-10-07", "claude_cap_80:2026-10"]) == {"apollo_floor:2026-10-07"}


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
    with pytest.raises(ValueError, match="bad column"):
        s.latest("heartbeats", "started_at DESC; DROP TABLE x --")
    with pytest.raises(GuardViolation, match="unknown table"):
        s.latest("hubspot_contacts", "id")
    assert s.insert("accounts", []) == 0 and s.upsert("accounts", []) == 0  # nothing to write: no connection


def test_bad_columns_are_refused_before_connecting():
    s = PostgresStore(Guard(), "postgresql://unused", connect=no_connection)
    with pytest.raises(ValueError, match="bad column"):
        s.insert("accounts", [{"account_id": "a", "Tier": "x"}])
    with pytest.raises(ValueError, match="bad column"):
        s.upsert("accounts", [{"account_id": "a"}, {"account_id": "b", "tier x": "y"}])


def test_a_nul_character_is_dropped_rather_than_failing_the_write(store):
    """8 Oct 2026: read_pages failed on a home page whose text held a NUL (Postgres stores none, in text or jsonb:
    "unsupported Unicode escape sequence \\u0000"). The store drops it from every value it writes."""
    store.insert("signal_events", [{"event_id": "n1", "account_id": "a\x001", "source": "careers_pages",
                                    "fact": "home_page", "quote": "Acme\x00 Co",
                                    "value": {"title": "Acme\x00", "text": ["a\x00b", {"c": "d\x00"}]}}])
    [row] = store.select("signal_events", {"event_id": "n1"})
    assert (row["account_id"], row["quote"]) == ("a1", "Acme Co")
    assert row["value"] == {"title": "Acme", "text": ["ab", {"c": "d"}]}
    store.upsert("accounts", [{"account_id": "n2", "clean_name": "Nul\x00 Inc"}])
    assert store.get("accounts", account_id="n2")["clean_name"] == "Nul Inc"
