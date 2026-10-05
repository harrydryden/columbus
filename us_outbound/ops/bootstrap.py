"""Building the production Context (SPEC 0.3, 3, 13): the database, settings in force, secrets, clients.

Environment (on Railway: service variables, the keys sealed; docs/railway-setup.md):
  DATABASE_URL                            the Railway Postgres URL, as the reference
                                          variable ${{Postgres.DATABASE_URL}}
  US_OUTBOUND_SETTINGS_SHEET_ID           the "US Outbound – Settings" spreadsheet id
  US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON the Sheets service account's JSON key (Google
                                          project columbus-510209). Unset: Application
                                          Default Credentials, for local development
  US_OUTBOUND_*_API_KEY / _TOKEN          the six keys (context.SECRET_NAMES), read when used

Order: a guard that starts in dry-run, the database store, the settings in force (with any
mailbox a kill rule holds shown as Paused, learn/holds.py), and
only then the live decision and the boundaries, so nothing can be sent before the
settings are known. If the settings are unusable, jobs refuse to run; settings_sync, the
sheet bootstrap and `settings load` start from the General defaults so they can repair them,
and so does the kill switch: `stop` (it pauses every US Outbound campaign and stops enrollment)
and `unenrol` must work however broken the sheet is. Both are operator commands, live with --live
alone, and neither reaches a prospect.

Live (SPEC 0.3) needs the --live flag and live_sending = yes. Operator commands that
never reach a prospect (setup, pause, erase; see ops/cli.py) pass operator=True and are
live with the --live flag alone.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.clients.guard import Guard
from us_outbound.clients.http import Transport
from us_outbound.context import Clients, ConfigError, Context, Secrets, boundaries_for, effective_live
from us_outbound.learn.holds import with_holds
from us_outbound.logs import log
from us_outbound.ops.heartbeat import OPERATOR_STOP
from us_outbound.settings.model import General, Settings

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
UNENROL_JOB = "unenrol"  # ops/cli.py cmd_unenrol
# Jobs that may start from the General defaults when no valid settings are in force: the ones that
# repair the sheet, and the kill switch (`stop --live` and `unenrol --live` must never wait for a fix).
DEFAULTS_OK = frozenset({"settings_sync", "settings_bootstrap", "settings_load", OPERATOR_STOP, UNENROL_JOB})
DATABASE_VAR = "DATABASE_URL"
GOOGLE_KEY_VAR = "US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON"
# ConfigError (the environment is missing something) lives in context.py, beside Secrets;
# bootstrap.ConfigError is the same class.


class SettingsUnusable(Exception):
    """No valid version of some settings tab is in force, so the job refuses to run."""

    def __init__(self, errors: Mapping[str, list[Any]], store: Store | None = None):
        self.errors = {tab: list(errs) for tab, errs in errors.items() if errs}
        self.store = store
        tabs = ", ".join(self.errors) or "unknown"
        super().__init__(
            f"settings are unusable ({tabs}); fix the sheet, then run `us-outbound settings sync`"
        )


def resolve_live(live_flag: bool, settings: Settings, *, operator: bool = False) -> bool:
    """Jobs: --live and live_sending = yes. Operator commands: --live alone."""
    return bool(live_flag) if operator else effective_live(live_flag, settings)


def database_url(env: Mapping[str, str]) -> str:
    """DATABASE_URL, or a ConfigError saying how to set it (the value is never echoed)."""
    value = (env.get(DATABASE_VAR) or "").strip()
    if not value:
        raise ConfigError(f"set {DATABASE_VAR} (on Railway: the reference variable ${{{{Postgres.DATABASE_URL}}}})")
    return value


def _load_settings(store: Store) -> tuple[Settings | None, Mapping[str, list[Any]]]:
    from us_outbound.settings.sync import load_current  # built alongside; imported when used

    settings, errors = load_current(store)
    # A mailbox a kill rule pauses counts as Paused at once, before the sheet syncs (learn/holds.py).
    return (with_holds(store, settings) if settings is not None else None), errors


def sheets_credentials(env: Mapping[str, str]) -> Any:
    """Credentials for the Sheets API only: the service-account key in GOOGLE_KEY_VAR, else
    Application Default Credentials (local development). Errors never echo the key."""
    raw = (env.get(GOOGLE_KEY_VAR) or "").strip()
    if not raw:
        import google.auth
        import google.auth.exceptions

        try:
            return google.auth.default(scopes=[SHEETS_SCOPE])[0]
        except google.auth.exceptions.DefaultCredentialsError:
            raise ConfigError(
                f"no Google credentials for the settings sheet: set {GOOGLE_KEY_VAR} (on Railway), or run "
                "`gcloud auth application-default login` with the spreadsheets scope (locally)"
            ) from None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:  # its message gives a position, never the content
        raise ConfigError(f"{GOOGLE_KEY_VAR} is not valid JSON ({exc.msg} at line {exc.lineno}); "
                          "paste the whole key file") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ConfigError(f"{GOOGLE_KEY_VAR} is not a service-account key (its type must be service_account)")
    missing = [k for k in ("client_email", "private_key", "token_uri") if not info.get(k)]
    if missing:
        raise ConfigError(f"{GOOGLE_KEY_VAR} is missing {', '.join(missing)}; paste the whole key file")
    from google.oauth2 import service_account

    try:
        return service_account.Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
    except ValueError:  # e.g. a damaged private_key; the library's message is not repeated
        raise ConfigError(f"{GOOGLE_KEY_VAR} could not be read as a service-account key; create a new key") from None


def build_context(
    job: str,
    live_flag: bool,
    *,
    operator: bool = False,
    env: Mapping[str, str] | None = None,
    store: Store | None = None,
    load_settings: Callable[[Store], tuple[Settings | None, Mapping[str, list[Any]]]] | None = None,
    transport: Transport | None = None,
    secrets: Secrets | None = None,
    google_credentials: Any = None,
) -> Context:
    """The Context for one run of `job` in production. The keyword arguments are for tests."""
    env = os.environ if env is None else env
    dsn = database_url(env)
    sheet_id = (env.get("US_OUTBOUND_SETTINGS_SHEET_ID") or "").strip()

    guard = Guard(live=False, job=job)
    if store is None:
        from us_outbound.clients.db import PostgresStore

        store = PostgresStore(guard, dsn)
    store.guard = guard
    settings, errors = (load_settings or _load_settings)(store)
    if settings is None:
        if job not in DEFAULTS_OK:
            raise SettingsUnusable(errors, store)
        log("settings_defaults", job=job, reason="no valid settings in force; starting from the General defaults")
        settings = Settings(general=General())

    live = resolve_live(live_flag, settings, operator=operator)
    guard.configure(live=live, bounds=boundaries_for(settings, sheet_id))
    if live_flag and not live:
        log("live_refused", job=job, reason="live_sending is not yes in the settings sheet; running dry")
    secrets = secrets or Secrets(guard, env)
    if transport is None:
        from us_outbound.clients.http import RequestsTransport

        transport = RequestsTransport()
    creds = google_credentials if google_credentials is not None else sheets_credentials(env)
    clients = Clients(guard, transport, secrets, store, settings, google_credentials=creds)
    ctx = Context(job=job, settings=settings, store=store, guard=guard, clients=clients, live_flag=live_flag)
    log("context", job=job, run_id=ctx.run_id, live=ctx.live, live_flag=live_flag, operator=operator,
        sheet=bool(sheet_id))
    return ctx
