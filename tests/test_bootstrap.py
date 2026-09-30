"""The production wiring on Railway (ops/bootstrap.py, context.Secrets): DATABASE_URL and the
Postgres store, the Sheets service-account key, the Application Default Credentials
fallback, and the six keys read from environment variables."""

from __future__ import annotations

import json

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
