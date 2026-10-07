"""The outside watchdog (Harry, 7 Oct 2026): Healthchecks.io hears from heartbeat_check every hour, so a dead worker,
database or Slack token still reaches Harry, by email.

Every Slack alert comes from inside the Railway worker. heartbeat_check (ops/heartbeat.py) alerts on missed jobs,
but if the worker stops, the database is down or the Slack token is revoked, nothing is said at all. So each
heartbeat_check run ends with a GET of US_OUTBOUND_WATCHDOG_URL, the ping URL of a Healthchecks.io check (period 1
hour, grace 1 hour, emailing Harry; docs/railway-setup.md "h. The outside watchdog"):
  * <url>       when all is well;
  * <url>/fail  when heartbeat_check found a job that missed its heartbeat, a job that runs daily or less often
                whose latest run failed in the last day (heartbeat.failed_jobs), or that Slack cannot take its alerts
                (no token in a live run, a token Slack rejects at auth.test, or Slack out of reach): Healthchecks
                emails at once;
  * nothing     when heartbeat_check cannot run at all (the worker, the scheduler or the database is down): the
                check goes late, and Healthchecks emails after its grace.
A heartbeat_check that raises tries /fail before its error is recorded.

The URL is read through the secrets client (context.SECRET_NAMES "watchdog") when heartbeat_check's context is built
(ops/bootstrap.py), for that job only, and handed to the guard, which allows a GET of exactly it or its /fail form
(Guard._check_watchdog). Unset, or not an https URL: logged once a run and skipped (`us-outbound golive` WARNs). A
ping that fails is logged and never fails the job. The URL is a secret (anyone holding it could report the worker
alive), so it is never logged. The ping goes in dry-run too (the guard's docstring says why).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.clients.watchdog import PingFailed
from us_outbound.context import SECRET_NAMES, ConfigError, Context, Secrets
from us_outbound.logs import log

JOB = "heartbeat_check"
SECRET = "watchdog"
VAR = SECRET_NAMES[SECRET]
# What Slack answers auth.test with when the token itself is no good (Slack's API docs).
SLACK_AUTH_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"})


def read(secrets: Secrets) -> tuple[str, str]:
    """(the ping URL, "") or ("", why there is none): unset, or not an https URL."""
    try:
        value = secrets.get(SECRET)
    except ConfigError:
        return "", f"{VAR} is not set"
    if not _https_url(value):
        return "", f"{VAR} is not an https ping URL"
    return value.rstrip("/"), ""


def _https_url(value: str) -> bool:
    """https://host/path, with a dotted host and no spaces, query, fragment or user part (only clients/http.py
    imports urllib: tests/test_guardrails.py)."""
    if not value.lower().startswith("https://"):
        return False
    host = value[len("https://"):].split("/", 1)[0]
    return "." in host and not any(c.isspace() or c in "?#@" for c in value)


def slack_problem(ctx: Context) -> str:
    """Why alerts cannot reach Slack now, or "": no token in a live run, a token Slack rejects (auth.test), or Slack
    out of reach. Dry-run without a token posts to the log by design (clients/slack.SlackOff), which is no problem."""
    try:
        slack = ctx.clients.slack
    except ConfigError:
        return f"no Slack token ({SECRET_NAMES['slack']} is not set)"
    try:
        slack.auth_test()
    except GuardViolation:
        raise
    except ApiError as exc:
        code = str(exc.body.get("error") or "") if isinstance(exc.body, Mapping) else ""
        if code in SLACK_AUTH_ERRORS:
            return f"Slack rejected the bot token ({code})"
        return f"Slack could not check the bot token ({code or f'HTTP {exc.status}'})"
    except Exception as exc:  # a connection error or a timeout, after the transport's retries
        return f"Slack could not be reached ({type(exc).__name__})"
    return ""


def ping(ctx: Context, reasons: Sequence[str] = ()) -> dict[str, Any]:
    """GET the ping URL, or its /fail form when there are reasons. Never raises (but a GuardViolation)."""
    fail = bool(reasons)
    client = ctx.clients.watchdog
    if client is None:
        _, why = read(ctx.clients.secrets)
        log("watchdog_off", reason=why or f"no watchdog URL for {ctx.job}", would_fail=fail)
        return {"pinged": False, "fail": fail, "off": why or "no watchdog URL"}
    try:
        status = client.ping(fail=fail)
    except GuardViolation:
        raise
    except Exception as exc:  # the URL is in a requests error's text, so only its type is kept
        error = str(exc) if isinstance(exc, PingFailed) else type(exc).__name__
        log("watchdog_ping_failed", fail=fail, error=error)
        return {"pinged": False, "fail": fail, "ping_error": error}
    log("watchdog_ping", fail=fail, status=status, reasons=list(reasons))
    return {"pinged": True, "fail": fail, "status": status, "reasons": list(reasons)}
