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
mailbox a kill rule holds shown as Paused, base/holds.py), and
only then the live decision and the boundaries, so nothing can be sent before the
settings are known. If the settings are unusable, jobs refuse to run; settings_sync, the
sheet bootstrap and `settings load` start from the General defaults so they can repair them,
and so does the kill switch: `stop` (it pauses every US Outbound campaign and stops enrollment)
and `unenrol` must work however broken the sheet is. Both are operator commands, live with --live
alone, and neither reaches a prospect. So does heartbeat_check (Harry, 7 Oct 2026), so a broken
sheet is still told in Slack and to the outside watchdog: on the defaults it is live with --live
alone too (ALERTS_ON_DEFAULTS), since live_sending is unknown then and it only posts to the alert
channel and pings. Its context alone also reads US_OUTBOUND_WATCHDOG_URL, through the secrets
client, into the guard's boundaries (ops/health.py).

The brakes on a broken sheet (W1-1, 19 Oct 2026; RS P0-1): when the version in force fails validation, the
SAFETY_JOBS (the kill rules, the blackout pause, reading replies and email results, the do-not-contact list, the
health check, stop and unenrol) build from the last-good settings instead (settings/sync.load_fallback): each failing
tab's latest earlier version that validates, or General's defaults with the Mailboxes and States tabs. Before, every
job but the repair ones refused (11 Oct 2026), and a plain fallback to the defaults would have run the brakes dry,
since live_sending defaults to no: so live_sending is read raw from the General row in force, and the guard runs
protective-only (clients/guard.py: pauses, stops, blocklists and posts, never an activation, a lead added, a reply,
a forward, a campaign created, or a sending list or daily limit widened). poll_approvals is not a safety job: it sends replies. Every other job refuses as before, and enrol never
runs on fallback content. The context says so: settings.fallback_tabs, and guard.protective_only. A fallback search
that fails (settings_fallback_failed in the log) is as if there were none: heartbeat_check, stop and unenrol build on
General's defaults (DEFAULTS_OK) as before, and the other safety jobs refuse.

Live (SPEC 0.3) needs the --live flag and live_sending = yes. Operator commands that
never reach a prospect (setup, pause, erase; see ops/cli.py) pass operator=True and are
live with the --live flag alone.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from us_outbound.base.holds import with_holds
from us_outbound.clients.db import Store
from us_outbound.clients.guard import Guard
from us_outbound.clients.http import Transport
from us_outbound.context import Clients, ConfigError, Context, Secrets, boundaries_for, effective_live
from us_outbound.logs import log
from us_outbound.ops import health
from us_outbound.ops.health import OPERATOR_STOP
from us_outbound.settings.model import General, Settings

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
UNENROL_JOB = "unenrol"  # ops/cli.py cmd_unenrol
# Jobs that may start from the General defaults when no valid settings are in force: the ones that
# repair the sheet, and the kill switch (`stop --live` and `unenrol --live` must never wait for a fix).
DEFAULTS_OK = frozenset({"settings_sync", "settings_bootstrap", "settings_load", OPERATOR_STOP, UNENROL_JOB,
                         health.JOB})
# Jobs that, started on the defaults, are live with --live alone: heartbeat_check's writes are a post to the alert
# channel and the watchdog's ping, which reach no prospect, and its alert must not go to the dev channel just
# because the sheet that says live_sending = yes cannot be read.
ALERTS_ON_DEFAULTS = frozenset({health.JOB})
# Jobs that run on the last-good settings, protective-only, when the version in force fails (module docstring).
SAFETY_JOBS = frozenset({"kill_rules", "blackout", "sync_outcomes", "poll_replies", "suppression_load", health.JOB,
                         OPERATOR_STOP, UNENROL_JOB})
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
            f"settings are unusable ({tabs}); fix the sheet, then run `us-outbound sync`"
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


def _in_use(store: Store, settings: Settings) -> Settings:
    """A mailbox a kill rule pauses counts as Paused at once, before the sheet syncs (base/holds.py); and the day's
    automatic thresholds stand in for the sheet's while auto_thresholds is yes (scoring/balance.py)."""
    from us_outbound.scoring import balance

    return balance.applied(store, with_holds(store, settings))


def _load_settings(store: Store) -> tuple[Settings | None, Mapping[str, list[Any]]]:
    from us_outbound.settings.sync import load_current  # built alongside; imported when used

    settings, errors = load_current(store)
    if settings is None:
        return None, errors
    return _in_use(store, settings), errors


def _load_fallback(store: Store) -> Settings | None:
    """The last-good settings for a safety job (settings/sync.load_fallback), as the jobs use them."""
    from us_outbound.settings.sync import load_fallback

    settings = load_fallback(store)
    return _in_use(store, settings) if settings is not None else None


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


def brake_context(live: bool, *, env: Mapping[str, str] | None = None, transport: Transport | None = None,
                  secrets: Secrets | None = None) -> Context:
    """The context of `stop --no-db` (ops/cli.py): no database and no settings. A guard from the environment alone
    (the sheet id; the "US Outbound – " campaign prefix the guard always holds), live with --live alone (an
    operator command that reaches no prospect), General's defaults for the alert channel, and a store in memory that
    nothing reads. Instantly and Slack keys come from the sealed variables as for every job."""
    from us_outbound.clients.db import MemoryStore

    env = os.environ if env is None else env
    settings = Settings(general=General())
    guard = Guard(live=bool(live), job=OPERATOR_STOP)
    guard.configure(bounds=boundaries_for(settings, (env.get("US_OUTBOUND_SETTINGS_SHEET_ID") or "").strip()))
    store = MemoryStore(guard)
    secrets = secrets or Secrets(guard, env)
    if transport is None:
        from us_outbound.clients.http import RequestsTransport

        transport = RequestsTransport()
    clients = Clients(guard, transport, secrets, store, settings)
    log("context", job=OPERATOR_STOP, live=guard.live, no_db=True)
    return Context(job=OPERATOR_STOP, settings=settings, store=store, guard=guard, clients=clients, live_flag=bool(live))


def build_context(
    job: str,
    live_flag: bool,
    *,
    operator: bool = False,
    env: Mapping[str, str] | None = None,
    store: Store | None = None,
    load_settings: Callable[[Store], tuple[Settings | None, Mapping[str, list[Any]]]] | None = None,
    load_fallback: Callable[[Store], Settings | None] | None = None,
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
    protective = False
    if settings is None and job in SAFETY_JOBS:
        try:
            settings = (load_fallback or _load_fallback)(store)
        except Exception as exc:  # the search must never stop a brake: the path below is as before the fallback
            log("settings_fallback_failed", job=job, error=f"{type(exc).__name__}: {str(exc)[:160]}")
            settings = None
        if settings is not None:
            protective = True
            operator = operator or job in ALERTS_ON_DEFAULTS
            log("settings_fallback_used", job=job, tabs=sorted(settings.fallback_tabs), mode=settings.fallback_mode,
                live_sending=settings.general.live_sending, protective_only=True)
    if settings is None:
        if job not in DEFAULTS_OK:
            raise SettingsUnusable(errors, store)
        log("settings_defaults", job=job, reason="no valid settings in force; starting from the General defaults")
        settings = Settings(general=General())
        operator = operator or job in ALERTS_ON_DEFAULTS

    live = resolve_live(live_flag, settings, operator=operator)
    secrets = secrets or Secrets(guard, env)
    ping_url = health.watchdog_url(secrets)[0] if job == health.JOB else ""
    guard.configure(live=live, bounds=boundaries_for(settings, sheet_id, watchdog_url=ping_url),
                    protective_only=protective)
    if live_flag and not live:
        log("live_refused", job=job, reason="live_sending is not yes in the settings sheet; running dry")
    if transport is None:
        from us_outbound.clients.http import RequestsTransport

        transport = RequestsTransport()
    creds = google_credentials if google_credentials is not None else sheets_credentials(env)
    clients = Clients(guard, transport, secrets, store, settings, google_credentials=creds)
    ctx = Context(job=job, settings=settings, store=store, guard=guard, clients=clients, live_flag=live_flag)
    log("context", job=job, run_id=ctx.run_id, live=ctx.live, live_flag=live_flag, operator=operator,
        sheet=bool(sheet_id))
    return ctx
