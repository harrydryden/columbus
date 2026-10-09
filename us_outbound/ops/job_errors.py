"""Errors the jobs caught and carried on past, and keys a service rejected (Harry, 7 Oct 2026).

A job that meets a failure it can live with records it and finishes ok: enrol's errors (an add Instantly refused, a
card not posted), the reply desk's HubSpot writes (poll_approvals), hubspot_readback's steps, a Slack post that
failed (notify.alert's "error"), kill_rules' Instantly vitals (vitals_error), poll_replies' Claude errors
(claude_error) and Claude's monthly cap (claude_cap_reached). Its heartbeat row keeps them in its detail, and nobody
was told. heartbeat_check (hourly; ops/heartbeat.run) reads each scheduled job's latest finished run in the last
LOOKBACK and posts what it finds to the alert channel, with the approvers mentioned:
  * failed      the run raised (status error), with its error: a job that runs daily or less often (its next try
                is hours away), or any job when the error is a rejected key. A job that runs every few minutes is
                tried again within minutes (a redeploy's SIGTERM, a 502), and one that keeps failing is told as
                missed, with its error (heartbeat.check_heartbeats);
  * errors      a non-empty list under "errors" or a key ending "_errors", anywhere in the detail;
  * <key>       a text under a key ending "_error" (vitals_error, claude_error, alert_error);
  * <key>       a count above 0 under a key ending "_errors" (poll_replies' slack_errors);
  * claude_cap  claude_cap_reached above 0: replies were classified "other" with no draft;
  * slack_post  a post the job could not make (a {"posted": false, "error": ...} result; a post that went to
                the log for want of a token is no failure);
  * unusable    the settings are unusable, so jobs refuse to start (ops/cli._record_unusable): one line for all.
A count under "errors" itself is no error list (settings_sync's sheet errors, which it posts itself; pick_contacts'
Clay lookups), and is left out. Each finding quotes its first error, shortened (QUOTE) and passed through
logs.redact.
A failure that says a service rejected its key (HTTP 401 or 403 from a client, Slack's invalid_auth, not_authed,
token_revoked or account_inactive, Google's 401 or 403 or a service-account key refused, Anthropic's
authentication_error or permission_error) is told apart: "<System> refused our key or this request: if the key was revoked, replace <variable> in Railway
(Variables), then redeploy", the variable from context.SECRET_NAMES (and ops/bootstrap.GOOGLE_KEY_VAR).
Each (job, kind) is posted at most once for each UK day a run of it began, and each rejected key once a day whichever
jobs met it (notify.post_once keeps what was sent in events): a run that failed on Thursday is told on Thursday and
not again on Friday while it is still the latest. A dead Slack token cannot post any of this: the outside
watchdog's /fail ping covers it (ops/watchdog.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from us_outbound.clients.guard import GuardViolation
from us_outbound.context import SECRET_NAMES, UK, Context
from us_outbound.logs import clip, log, redact
from us_outbound.ops import notify
from us_outbound.ops.bootstrap import GOOGLE_KEY_VAR
from us_outbound.ops.heartbeat import DAILY, EXPECTED
from us_outbound.ops.watchdog import JOB as SELF  # heartbeat_check: its own detail holds what it found
from us_outbound.settings.validate import CLAUDE_CAP_USD

LOOKBACK = timedelta(hours=26)  # a daily job's latest run is always this recent
QUOTE = 160  # characters of the first error quoted
DEPTH = 4  # how deep in a job's detail errors are looked for
LATEST_SQL = (
    "SELECT DISTINCT ON (job) job, run_id, status, started_at, finished_at, detail, error"
    " FROM {schema}.heartbeats WHERE started_at >= %(since)s AND status <> 'running'"
    " ORDER BY job, started_at DESC, run_id DESC"
)
FAILED, ERRORS, CLAUDE_CAP, SLACK_POST, UNUSABLE = "failed", "errors", "claude_cap", "slack_post", "unusable"
HEAD = "Errors the jobs met:"
FOOT = ("Each job tries again on its next run. `us-outbound status` lists the jobs that failed; the worker's logs in "
        "Railway have the rest.")

# system -> (who Harry knows it as, the Railway variable holding its key)
KEYS = {
    "apollo": ("Apollo", SECRET_NAMES["apollo"]),
    "clay": ("Clay", SECRET_NAMES["clay"]),
    "instantly": ("Instantly", SECRET_NAMES["instantly"]),
    "hubspot": ("HubSpot", SECRET_NAMES["hubspot"]),
    "slack": ("Slack", SECRET_NAMES["slack"]),
    "claude": ("Anthropic (Claude)", SECRET_NAMES["claude"]),
    "sheets": ("Google", GOOGLE_KEY_VAR),
}
# How a rejected key reads in an error: clients/http.ApiError ("hubspot HTTP 401 for /crm/..."), Slack's error
# codes (it answers HTTP 200 with ok false), clients/claude.ClaudeError ("Claude API error 401"), google-auth.
AUTH = (
    (re.compile(r"\b(apollo|clay|instantly|hubspot|sheets|slack)\s+HTTP\s+(?:401|403)\b", re.I), ""),
    (re.compile(r"\b(?:invalid_auth|not_authed|token_revoked|token_expired|account_inactive)\b"), "slack"),
    (re.compile(r"\bClaude API error (?:401|403)\b|\bauthentication_error\b|\bpermission_error\b"), "claude"),
    (re.compile(r"\bRefreshError\b|\binvalid_grant\b"), "sheets"),
)


def rejected_key(text: str) -> str | None:
    """The system whose key the error says was rejected (a KEYS key), or None."""
    for pattern, system in AUTH:
        m = pattern.search(text)
        if m:
            return system or m.group(1).lower()
    return None


@dataclass
class Finding:
    job: str
    kind: str
    texts: list[str] = field(default_factory=list)  # the errors, first first
    count: int = 0  # for a counted failure (slack_errors) or the cap
    at: datetime | None = None


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    return None


def latest_finished(ctx: Context, jobs: Iterable[str]) -> dict[str, dict]:
    """job -> its latest finished run that started in the last LOOKBACK, with its detail (one query)."""
    wanted = set(jobs)
    since = ctx.now - LOOKBACK
    try:
        rows = ctx.store.query(LATEST_SQL.format(schema=ctx.store.schema), {"since": since})
    except NotImplementedError:  # MemoryStore: the same in Python
        rows = []
        for r in ctx.store.select("heartbeats", {"job": sorted(wanted)}):
            started = _ts(r.get("started_at"))
            if r.get("status") != "running" and started is not None and started >= since:
                rows.append(r)
        rows.sort(key=lambda r: (_ts(r.get("started_at")), str(r.get("run_id"))), reverse=True)
    out: dict[str, dict] = {}
    for r in rows:
        job = str(r.get("job") or "")
        if job in wanted and job not in out:
            out[job] = dict(r)
    return out


def _walk(detail: Any, depth: int = 0) -> Iterator[tuple[str, Any]]:
    """(key, value) for every key in the detail, nested mappings included, to DEPTH."""
    if not isinstance(detail, Mapping) or depth > DEPTH:
        return
    for key, value in detail.items():
        yield str(key), value
        if isinstance(value, Mapping):
            yield from _walk(value, depth + 1)


def findings(job: str, run: Mapping[str, Any]) -> list[Finding]:
    """What one run's heartbeat records as gone wrong (the module docstring's kinds)."""
    at = _ts(run.get("started_at"))
    detail = run.get("detail") if isinstance(run.get("detail"), Mapping) else {}
    if run.get("status") == "error":
        if "unusable" in detail:
            tabs = ", ".join(map(str, detail.get("unusable") or ())) or "unknown"
            return [Finding("settings", UNUSABLE, [tabs], at=at)]
        error = str(run.get("error") or "no error text")
        if EXPECTED.get(job, 0) < DAILY and not rejected_key(error):
            return []  # tried again within minutes; missed (with this error) if it keeps failing
        return [Finding(job, FAILED, [error], at=at)]
    out: dict[str, Finding] = {}
    for key, value in _walk(detail):
        if key == "claude_cap_reached" and isinstance(value, int) and value > 0:
            out.setdefault(CLAUDE_CAP, Finding(job, CLAUDE_CAP, at=at)).count += value
        elif (key == "errors" or key.endswith("_errors")) and isinstance(value, list) and value:
            out.setdefault(ERRORS, Finding(job, ERRORS, at=at)).texts += [str(v) for v in value]
        elif key.endswith("_errors") and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out.setdefault(key, Finding(job, key, at=at)).count += value
        elif key.endswith("_error") and isinstance(value, str) and value.strip():
            out.setdefault(key, Finding(job, key, at=at)).texts.append(value)
        elif (isinstance(value, Mapping) and value.get("posted") is False and isinstance(value.get("error"), str)
              and value["error"] and value["error"] != notify.NO_TOKEN):
            out.setdefault(SLACK_POST, Finding(job, SLACK_POST, at=at)).texts.append(value["error"])
    return list(out.values())


# -- what Harry reads ---------------------------------------------------------------------------------------------


def _quote(text: str) -> str:
    return "“" + clip(" ".join(str(redact(text)).split()), QUOTE) + "”"


def _when(at: datetime | None) -> str:
    return f"{at.astimezone(UK):%a %H:%M} UK" if at else "recently"


def describe(f: Finding) -> str:
    """One line for a finding that is not a rejected key."""
    if f.kind == UNUSABLE:
        return (f"• The settings are unusable ({f.texts[0]}): every job refuses to run until the sheet is fixed. Fix "
                "it, then run `us-outbound sync`.")
    if f.kind == CLAUDE_CAP:
        return (f'• {f.job} ({_when(f.at)}): Claude\'s monthly cap is used up, so {f.count} replies were classified '
                f'"other" with no draft. Raise claude_monthly_cap_usd on the General tab (up to ${CLAUDE_CAP_USD:,.0f}) '
                "and the Anthropic Console spend limit.")
    if f.kind == FAILED:
        return f"• {f.job} failed ({_when(f.at)}): {_quote(f.texts[0])}"
    if f.kind == SLACK_POST:
        return f"• {f.job} ({_when(f.at)}) could not post to Slack: {_quote(f.texts[0])}"
    if f.kind == ERRORS:
        n = len(f.texts)
        errors = "1 error" if n == 1 else f"{n} errors"
        return f"• {f.job} ({_when(f.at)}) carried on past {errors}; the first: {_quote(f.texts[0])}"
    if not f.texts:  # a count: slack_errors
        return f"• {f.job} ({_when(f.at)}): {f.count} {f.kind.replace('_', ' ')}"
    return f"• {f.job} ({_when(f.at)}): {f.kind.replace('_', ' ')}: {_quote(f.texts[0])}"


def _run_day(at: datetime | None, day: str) -> str:
    """The UK day the run began, which keys its post; `day` (today) when its start is unknown."""
    return at.astimezone(UK).date().isoformat() if at else day


def lines(found: Iterable[Finding], day: str) -> list[tuple[str, str]]:
    """(key, line) per finding, a rejected key told as such (once a day per system, naming every job that met it).
    A key carries the UK day its run began, not today: a daily job's failed run stays its latest until the next run,
    and keyed on today it was told again just after midnight (read_pages, Thu 8 Oct 2026, told again at 00:05 Fri)."""
    keys: dict[str, list[tuple[str, str, str]]] = {}  # system -> [(job, the error, its run's day)]
    out: list[tuple[str, str]] = []
    for f in found:
        others = []
        for text in f.texts:
            system = rejected_key(text)
            if system in KEYS:
                keys.setdefault(system, []).append((f.job, text, _run_day(f.at, day)))
            else:
                others.append(text)
        if f.texts and not others:
            continue  # every error of it is a rejected key, told below
        f.texts = others
        out.append((f"job_error:{f.job}:{f.kind}:{_run_day(f.at, day)}", describe(f)))
    for system, hits in keys.items():
        name, var = KEYS[system]
        jobs = ", ".join(dict.fromkeys(j for j, _, _ in hits))
        out.insert(0, (f"key_rejected:{system}:{max(d for _, _, d in hits)}",
                       f"• {name} refused our key or this request: if the key was revoked, replace {var} in Railway "
                       f"(Variables), then redeploy; if it is current, the {name} plan may not allow this. "
                       f"({jobs}: {_quote(hits[0][1])})"))
    return out


def check(ctx: Context, jobs: Iterable[str]) -> dict[str, Any]:
    """Read, then post what was not posted today. Never raises (but a GuardViolation): heartbeat_check goes on."""
    try:
        runs = latest_finished(ctx, [j for j in jobs if j != SELF])
        found = [f for job, run in sorted(runs.items()) for f in findings(job, run)]
        unusable = [f for f in found if f.kind == UNUSABLE]
        found = [f for f in found if f.kind != UNUSABLE] + unusable[:1]  # one line for all the jobs it stops
        pairs = lines(found, ctx.today_uk().isoformat())
        sent = notify.post_once(ctx, pairs, head=HEAD, foot=FOOT) if pairs else {"posted": False, "keys": []}
    except GuardViolation:
        raise
    except Exception as exc:  # the database, say: the watchdog ping must still go
        log("job_errors_failed", error=str(exc)[:200])
        return {"found": [], "posted": [], "check_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    log("job_errors", found=[k for k, _ in pairs], posted=sent.get("keys"))
    return {"found": [k.rsplit(":", 1)[0] for k, _ in pairs], "posted": sent.get("keys") or [],
            "post": {k: sent.get(k) for k in ("posted", "error")}}
