"""Building the production Context (SPEC 0.3, 3, 13): BigQuery, settings in force, secrets, clients.

Environment:
  US_OUTBOUND_PROJECT             the Google Cloud project (BigQuery, Secret Manager)
  US_OUTBOUND_BQ_LOCATION         the dataset's location (default EU, docs/phase0-facts.md)
  US_OUTBOUND_SETTINGS_SHEET_ID   the "US Outbound – Settings" spreadsheet id

Order: a guard that starts in dry-run, the BigQuery store, the settings in force, and
only then the live decision and the boundaries, so nothing can be sent before the
settings are known. If the settings are unusable, jobs refuse to run; settings_sync
(and the sheet bootstrap) start from the General defaults so they can repair them.

Live (SPEC 0.3) needs the --live flag and live_sending = yes. Operator commands that
never reach a prospect (setup, pause, erase; see ops/cli.py) pass operator=True and are
live with the --live flag alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from us_outbound.clients.bq import Store
from us_outbound.clients.guard import Guard
from us_outbound.clients.http import Transport
from us_outbound.context import Clients, Context, Secrets, boundaries_for, effective_live
from us_outbound.logs import log
from us_outbound.settings.model import General, Settings

DEFAULT_LOCATION = "EU"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
# Jobs that may start from the General defaults when no valid settings are in force.
DEFAULTS_OK = frozenset({"settings_sync", "settings_bootstrap"})


class ConfigError(Exception):
    """The environment is missing something the jobs need."""


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


def _env(env: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = (env.get(name) or "").strip() or default
    if not value:
        raise ConfigError(f"set {name}")
    return value


def _load_settings(store: Store) -> tuple[Settings | None, Mapping[str, list[Any]]]:
    from us_outbound.settings.sync import load_current  # built alongside; imported when used

    return load_current(store)


def _google_credentials() -> Any:
    import google.auth

    return google.auth.default(scopes=[SHEETS_SCOPE])[0]


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
    project = _env(env, "US_OUTBOUND_PROJECT")
    location = _env(env, "US_OUTBOUND_BQ_LOCATION", DEFAULT_LOCATION)
    sheet_id = (env.get("US_OUTBOUND_SETTINGS_SHEET_ID") or "").strip()

    guard = Guard(live=False, job=job)
    if store is None:
        from us_outbound.clients.bq import BigQueryStore

        store = BigQueryStore(guard, project, location)
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
    secrets = secrets or Secrets(guard, project)
    if transport is None:
        from us_outbound.clients.http import RequestsTransport

        transport = RequestsTransport()
    creds = google_credentials if google_credentials is not None else _google_credentials()
    clients = Clients(guard, transport, secrets, store, settings, google_credentials=creds)
    ctx = Context(job=job, settings=settings, store=store, guard=guard, clients=clients, live_flag=live_flag)
    log("context", job=job, run_id=ctx.run_id, live=ctx.live, live_flag=live_flag, operator=operator,
        sheet=bool(sheet_id), project=project)
    return ctx
