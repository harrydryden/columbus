"""The production wiring on Railway (ops/bootstrap.py, context.Secrets): DATABASE_URL and the
Postgres store, the Sheets service-account key, the Application Default Credentials
fallback, and the six keys read from environment variables."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.fakes import FakeTransport
from tests.test_registry import SETTINGS
from us_outbound.clients.db import MemoryStore, PostgresStore
from us_outbound.clients.guard import Guard
from us_outbound.context import SECRET_NAMES, ConfigError, Secrets
from us_outbound.ops import bootstrap, cli

DSN = "postgresql://us_outbound@db.test:5432/railway"
KEY_VAR = bootstrap.GOOGLE_KEY_VAR
SA_EMAIL = "us-outbound-sheets@columbus-510209.iam.gserviceaccount.com"


@pytest.fixture(scope="module")
def key_info() -> dict:
    """A service-account key file made up for the test (a fresh RSA key, never a real one)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return {"type": "service_account", "project_id": "columbus-510209", "private_key_id": "test-key-id",
            "private_key": pem, "client_email": SA_EMAIL, "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token"}


def no_adc(monkeypatch):
    import google.auth

    def refuse(*a, **k):
        raise AssertionError("Application Default Credentials must not be used when the key is set")

    monkeypatch.setattr(google.auth, "default", refuse)


def build(env, job="mailbox_health", **kw):
    kw.setdefault("store", MemoryStore(Guard()))
    kw.setdefault("google_credentials", object())
    return bootstrap.build_context(job, False, env=env, load_settings=lambda store: (SETTINGS, {}),
                                   transport=FakeTransport(), **kw)


# -- the database ---------------------------------------------------------------------------------


def test_the_store_is_postgres_at_database_url():
    ctx = build({"DATABASE_URL": f"  {DSN}\n"}, store=None)
    assert isinstance(ctx.store, PostgresStore) and ctx.store.dsn == DSN
    assert ctx.store.guard is ctx.guard


@pytest.mark.parametrize("env", [{}, {"DATABASE_URL": "   "}, {"US_OUTBOUND_PROJECT": "columbus-510209"}])
def test_a_missing_database_url_is_a_config_error(env):
    with pytest.raises(ConfigError, match=r"DATABASE_URL.*\$\{\{Postgres\.DATABASE_URL\}\}"):
        build(env)
    assert bootstrap.ConfigError is ConfigError


# -- the Sheets credentials -------------------------------------------------------------------------


def test_the_service_account_key_gives_sheets_only_credentials(monkeypatch, key_info):
    no_adc(monkeypatch)
    creds = bootstrap.sheets_credentials({KEY_VAR: json.dumps(key_info, indent=2) + "\n"})
    assert creds.service_account_email == SA_EMAIL
    assert list(creds.scopes) == ["https://www.googleapis.com/auth/spreadsheets"]
    ctx = build({"DATABASE_URL": DSN, KEY_VAR: json.dumps(key_info)}, google_credentials=None)
    assert ctx.clients.google_credentials.service_account_email == SA_EMAIL
    assert ctx.clients.sheets.credentials is ctx.clients.google_credentials


SECRETISH = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC"


@pytest.mark.parametrize("raw, says", [
    ('{"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\\n' + SECRETISH, "not valid JSON"),
    ("{'type': 'service_account', 'private_key': '" + SECRETISH + "'}", "not valid JSON"),
    (SECRETISH, "not valid JSON"),
    (json.dumps({"type": "authorized_user", "refresh_token": SECRETISH}), "not a service-account key"),
    (json.dumps([SECRETISH]), "not a service-account key"),
    (json.dumps({"type": "service_account", "private_key": SECRETISH}), "missing client_email, token_uri"),
    (json.dumps({"type": "service_account", "client_email": SA_EMAIL, "token_uri": "https://oauth2.googleapis.com/token",
                 "private_key": "-----BEGIN PRIVATE KEY-----\n" + SECRETISH + "\n-----END PRIVATE KEY-----\n"}),
     "could not be read"),
])
def test_a_bad_key_is_refused_without_echoing_it(monkeypatch, raw, says):
    no_adc(monkeypatch)
    with pytest.raises(ConfigError) as err:
        bootstrap.sheets_credentials({KEY_VAR: raw})
    text = str(err.value)
    assert KEY_VAR in text and says in text
    assert SECRETISH not in text and "PRIVATE KEY" not in text and SECRETISH[:12] not in text
    e = err.value  # no chained exception (a JSONDecodeError holds the text) reaches a traceback
    assert e.__cause__ is None and (e.__context__ is None or e.__suppress_context__)


def test_without_the_key_it_falls_back_to_application_default_credentials(monkeypatch):
    import google.auth
    import google.auth.exceptions

    seen, local = [], object()
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: seen.append(scopes) or (local, "local-project"))
    assert bootstrap.sheets_credentials({KEY_VAR: "  "}) is local
    assert seen == [["https://www.googleapis.com/auth/spreadsheets"]]
    assert build({"DATABASE_URL": DSN}, google_credentials=None).clients.google_credentials is local

    def none(scopes=None):
        raise google.auth.exceptions.DefaultCredentialsError("no ADC")

    monkeypatch.setattr(google.auth, "default", none)
    with pytest.raises(ConfigError, match=KEY_VAR):
        bootstrap.sheets_credentials({})


# -- the keys -------------------------------------------------------------------------------------------


def test_the_key_variables():
    assert SECRET_NAMES == {
        "apollo": "US_OUTBOUND_APOLLO_API_KEY",
        "clay": "US_OUTBOUND_CLAY_API_KEY",
        "instantly": "US_OUTBOUND_INSTANTLY_API_KEY",
        "hubspot": "US_OUTBOUND_HUBSPOT_TOKEN",
        "slack": "US_OUTBOUND_SLACK_BOT_TOKEN",
        "claude": "US_OUTBOUND_CLAUDE_API_KEY",
        "watchdog": "US_OUTBOUND_WATCHDOG_URL",  # optional: the outside watchdog's ping URL (ops/watchdog.py)
    }


def test_secrets_come_from_the_environment_through_the_guard():
    guard = Guard()
    s = Secrets(guard, env={"US_OUTBOUND_HUBSPOT_TOKEN": "  pat-test-123 \n"})
    assert s.get("hubspot") == "pat-test-123"
    assert s.get("hubspot") == "pat-test-123"  # cached: the guard sees one access
    accesses = [(c.system, c.action, c.target, c.write) for c in guard.calls if c.system == "secrets"]
    assert accesses == [("secrets", "access", "US_OUTBOUND_HUBSPOT_TOKEN", False)]


def test_a_missing_key_names_the_variable_never_a_value():
    s = Secrets(Guard(), env={"US_OUTBOUND_CLAY_API_KEY": "clay-test-value", "US_OUTBOUND_APOLLO_API_KEY": " \t"})
    for system, var in (("apollo", "US_OUTBOUND_APOLLO_API_KEY"), ("slack", "US_OUTBOUND_SLACK_BOT_TOKEN")):
        with pytest.raises(ConfigError) as err:
            s.get(system)
        assert var in str(err.value) and "clay-test-value" not in str(err.value)


def test_secrets_read_os_environ_by_default_and_fetch_gets_the_variable_name(monkeypatch):
    monkeypatch.setenv("US_OUTBOUND_SLACK_BOT_TOKEN", "xoxb-test")
    assert Secrets(Guard()).get("slack") == "xoxb-test"
    asked = []
    s = Secrets(Guard(), fetch=lambda name: asked.append(name) or f"test-{name}")
    assert s.get("claude") == "test-US_OUTBOUND_CLAUDE_API_KEY" and asked == ["US_OUTBOUND_CLAUDE_API_KEY"]


def hubspot_job(ctx):
    return {"token_length": len(ctx.clients.secrets.get("hubspot"))}


def test_a_job_without_its_key_exits_two_and_names_the_variable(monkeypatch, capsys):
    store = MemoryStore(Guard())
    env = {"DATABASE_URL": DSN}  # no key variables at all

    def factory(job, live_flag, operator=False):
        return build(env, job=job, store=store)

    monkeypatch.setitem(cli.JOBS, "suppression_load", "tests.test_bootstrap:hubspot_job")
    assert cli.main(["run", "suppression_load"], context_factory=factory) == 2
    assert "set US_OUTBOUND_HUBSPOT_TOKEN" in capsys.readouterr().err
    [beat] = store.tables["heartbeats"]
    assert beat["status"] == "error" and "US_OUTBOUND_HUBSPOT_TOKEN" in beat["error"]


# -- Slack left out (Harry, 30 Sep 2026) ----------------------------------------------------------------


def _clients(guard, env, transport=None):
    from us_outbound.context import Clients

    return Clients(guard, transport or FakeTransport(), Secrets(guard, env=env), MemoryStore(guard), SETTINGS)


def test_without_a_slack_token_a_dry_run_logs_posts_and_calls_nothing(capsys):
    from us_outbound.clients.slack import SlackOff

    transport = FakeTransport()
    clients = _clients(Guard(), {}, transport)
    assert isinstance(clients.slack, SlackOff)
    assert clients.slack.post("#us-outbound", "Settings rejected: someone@example.com") is None
    assert clients.slack.replies("#us-outbound", "1.2") == []
    assert transport.requests == []
    out = capsys.readouterr().out
    assert '"event": "slack_off"' in out and "someone@example.com" not in out  # emails hashed as in every log


def test_a_live_run_still_needs_the_slack_token():
    with pytest.raises(ConfigError) as err:
        _clients(Guard(live=True), {}).slack
    assert "US_OUTBOUND_SLACK_BOT_TOKEN" in str(err.value)


def test_with_a_slack_token_the_real_client_is_used():
    from us_outbound.clients.slack import Slack

    assert isinstance(_clients(Guard(), {"US_OUTBOUND_SLACK_BOT_TOKEN": "xoxb-test"}).slack, Slack)


# -- the kill switch on default settings (A12) ------------------------------------------------------------


def test_stop_and_unenrol_run_on_the_general_defaults_when_the_settings_are_unusable(capsys):
    """`stop --live` must pause the campaigns even when no valid settings are in force."""
    from us_outbound.base.heartbeats import OPERATOR_STOP, enrolment_paused
    from us_outbound.settings.model import General

    assert {OPERATOR_STOP, "unenrol"} <= bootstrap.DEFAULTS_OK and "enrol" not in bootstrap.DEFAULTS_OK
    t = FakeTransport()
    t.route("GET", "/campaigns", {"items": [{"id": "c-hannah", "name": "US Outbound – Hannah Spalding", "status": 1},
                                            {"id": "c-sam", "name": "US Outbound – Sam Jackson", "status": 0}]})
    store = MemoryStore(Guard())
    env = {"DATABASE_URL": DSN, "US_OUTBOUND_INSTANTLY_API_KEY": "test-instantly"}

    def factory(job, live_flag, operator=False):
        return bootstrap.build_context(job, live_flag, operator=operator, env=env, store=store, transport=t,
                                       load_settings=lambda s: (None, {"General": ["row 3: bad"]}),
                                       google_credentials=object())

    ctx = factory(OPERATOR_STOP, True, operator=True)
    assert ctx.live and ctx.settings.general == General()
    assert cli.main(["stop", "--live"], context_factory=factory) == 0
    assert [r.url.rsplit("/v2", 1)[1] for r in t.requests if r.method == "POST"] == ["/campaigns/c-hannah/pause"]
    assert enrolment_paused(store) is not None  # enrol's operator_pause sees the stop
    assert "already_not_sending" in capsys.readouterr().out
    assert factory("unenrol", True, operator=True).live
    with pytest.raises(bootstrap.SettingsUnusable):
        factory("enrol", True)  # a job that sends still refuses


# -- the brakes on a broken sheet: the last-good fallback, live and protective-only (W1-1; RS P0-1) --------------

T0 = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)  # the good sync
T1 = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)  # a sync by older code stored a row this code rejects
ENV = {"DATABASE_URL": DSN, "US_OUTBOUND_SETTINGS_SHEET_ID": "sheet-test"}


def _store_version(store, tabs, at):
    """Every tab's rows in force from `at`, as settings_sync stores them (the rows, and each tab's sheet order)."""
    from us_outbound.settings.sync import ORDER_TAB, TABLE
    from us_outbound.settings.validate import KEY_COLUMNS, natural_key

    rows = []
    for tab, tab_rows in tabs.items():
        keyed = [r for r in tab_rows if all(str(r.get(c) or "").strip() for c in KEY_COLUMNS[tab])]
        rows += [{"tab": tab, "key": natural_key(tab, r), "values": dict(r), "effective_from": at, "effective_to": None,
                  "synced_at": at} for r in keyed]
        rows.append({"tab": ORDER_TAB, "key": tab, "values": {"keys": [natural_key(tab, r) for r in keyed]},
                     "effective_from": at, "effective_to": None, "synced_at": at})
    store.upsert(TABLE, rows)


def _new_version(store, tab, key, at, **values):
    """A new version of one row from `at`, as an older sync would have written it: the old row closed at `at`."""
    from us_outbound.settings.sync import TABLE

    [old] = store.select(TABLE, {"tab": tab, "key": key, "effective_to": None})
    store.update(TABLE, {"tab": tab, "key": key, "effective_from": old["effective_from"]}, {"effective_to": at})
    store.upsert(TABLE, [{**old, "values": {**old["values"], **values}, "effective_from": at, "effective_to": None,
                          "synced_at": at}])


def _live_tabs():
    """The build's tabs with the live switches: live_sending yes, an approver, the test registry's four mailboxes."""
    from tests.test_registry import sheet_rows
    from us_outbound.settings.defaults import default_tabs

    tabs = default_tabs()
    for r in tabs["General"]:
        if r["key"] == "live_sending":
            r["value"] = "yes"
        if r["key"] == "approver_slack_ids":
            r["value"] = "U_HARRY"
    tabs["Mailboxes"] = sheet_rows(SETTINGS)
    return tabs


def _fallback_world(*, both_copy_versions_bad: bool):
    """A store whose Copy version in force has a row this code rejects (status "maybe"); with both_copy_versions_bad
    the version before it is bad too ("perhaps")."""
    from tests.test_registry import FakeInstantly, StubSheets, sheet_rows, slack_routes
    from us_outbound.settings.validate import natural_key

    store = MemoryStore(Guard())
    tabs = _live_tabs()
    first = natural_key("Copy", tabs["Copy"][0])
    if both_copy_versions_bad:
        tabs["Copy"][0]["status"] = "perhaps"
    _store_version(store, tabs, T0)
    _new_version(store, "Copy", first, T1, status="maybe")
    t = slack_routes(FakeTransport())
    inst = FakeInstantly(t)
    sheets = StubSheets(Guard(live=True), {"Mailboxes": sheet_rows(SETTINGS)})
    return store, t, inst, sheets


def _factory(store, t, sheets, now):
    def factory(job, live_flag=True, operator=False):
        ctx = bootstrap.build_context(job, live_flag, operator=operator, env=ENV, store=store, transport=t,
                                      secrets=Secrets(Guard(), fetch=lambda name: f"test-{name}"),
                                      google_credentials=object())
        ctx.now = now
        sheets.guard = ctx.guard  # the sheet in memory, judged by this run's guard
        ctx.clients.sheets = sheets
        return ctx

    return factory


@pytest.mark.parametrize("both_bad, mode", [(False, "earlier versions"), (True, "defaults")])
def test_the_kill_rules_run_live_and_protective_only_on_the_last_good_settings(both_bad, mode):
    """In force: a Copy row a newer check rejects, so load_current fails and, before W1-1, every job refused, the
    brakes included (11 Oct 2026). The kill rules now build from the previous Copy version (or, when that is bad too,
    General's defaults with the Mailboxes and States tabs), live because the stored General row says live_sending
    yes, and a mailbox with 2 bounces in its last 40 sends (5.0% > 3%, and 2 >= MIN_BOUNCES) is paused in the
    Instantly fake. Jobs that send or add still refuse."""
    from tests.test_kill_rules import campaigns, sends
    from tests.test_ramp import past_ramp
    from tests.test_registry import C_HANNAH, HANNAH, NOW, row
    from us_outbound.learn import kill_rules
    from us_outbound.ops.heartbeat import run_job
    from us_outbound.settings import sync

    store, t, inst, sheets = _fallback_world(both_copy_versions_bad=both_bad)
    assert sync.load_current(store)[0] is None  # the version in force is unusable
    campaigns(inst)
    past_ramp(store, SETTINGS.mailboxes, NOW)
    factory = _factory(store, t, sheets, NOW)
    ctx = factory("kill_rules")
    assert ctx.settings.fallback_tabs == {"Copy"} and ctx.settings.fallback_mode == mode
    assert ctx.settings.general.live_sending is True and ctx.live and ctx.protective_only
    if both_bad:
        assert ctx.settings.copy == () and len(ctx.settings.mailboxes) == 4 and ctx.settings.states
    else:
        assert len(ctx.settings.copy) == len(_live_tabs()["Copy"])
    sends(ctx, HANNAH, 40, bounced=2)
    out = run_job(ctx, kill_rules.run)
    assert [f["rule"] for f in out["fired"]] == ["mailbox_bounce_rate"]
    assert inst.by_name(C_HANNAH)["status"] == 2  # paused in Instantly, live
    assert row(sheets, HANNAH)["status"] == "Paused"
    [beat] = [r for r in store.select("heartbeats") if r["job"] == "kill_rules"]
    assert beat["status"] == "ok" and beat["detail"]["settings_fallback"] == ["Copy"]
    for job in ("enrol", "poll_approvals", "pick_contacts"):
        with pytest.raises(bootstrap.SettingsUnusable):
            factory(job)


def test_the_blackout_pauses_on_the_fallback_but_starts_nothing_after_it():
    """Thanksgiving week is a blackout (the build's blackout_dates, 2026-11-23..2026-11-27): on Mon 23 Nov the job on
    the fallback pauses the sending campaign; on Mon 30 Nov, the hold over, it calls no activate (protective-only)
    and says the start waits for settings in force."""
    from tests.test_kill_rules import campaigns
    from tests.test_registry import C_HANNAH
    from us_outbound.ops.heartbeat import run_job
    from us_outbound.registry import blackout

    store, t, inst, sheets = _fallback_world(both_copy_versions_bad=False)
    campaigns(inst)
    inst.by_name(C_HANNAH)["status"] = 1  # sending
    held_at = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)  # 10:00 ET, a blackout date
    ctx = _factory(store, t, sheets, held_at)("blackout")
    assert ctx.protective_only and ctx.live
    out = run_job(ctx, blackout.run)
    assert out["held"] is True and inst.by_name(C_HANNAH)["status"] == 2
    after = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)  # the Monday after, 07:00 ET
    out = run_job(_factory(store, t, sheets, after)("blackout"), blackout.run)
    assert out["held"] is False and C_HANNAH in out["paused_by_blackout"] and "resume_deferred" in out
    assert not [r for r in t.requests if r.url.endswith("/activate")]
    assert inst.by_name(C_HANNAH)["status"] == 2  # still paused: the first run on good settings starts it


def test_the_fallback_never_takes_live_sending_from_the_defaults():
    """live_sending is read raw from the General row in force: a stored yes stays yes even when General itself is
    the failing tab, and a stored no stays no (the brake runs dry, as the sheet says)."""
    from us_outbound.settings import sync

    store = MemoryStore(Guard())
    _store_version(store, _live_tabs(), T0)
    _new_version(store, "General", "weekly_enrol_cap", T1, value="-5")  # a value a check rejects
    fb = sync.load_fallback(store)
    assert fb.fallback_tabs == {"General"} and fb.fallback_mode == "earlier versions"
    assert fb.general.live_sending is True and fb.general.weekly_enrol_cap == 150  # the earlier version's
    _new_version(store, "General", "live_sending", datetime(2026, 10, 22, 2, 0, tzinfo=UTC), value="no")
    assert sync.load_fallback(store).general.live_sending is False
    assert sync.load_fallback(MemoryStore(Guard())) is None  # nothing stored: no fallback at all
