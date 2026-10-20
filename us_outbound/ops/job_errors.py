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
  * <key>       a text under a key ending "_error" (vitals_error, claude_error, alert_error), or under a key named
                error (the hand-check tab's pass inside poll_approvals: hand_check_tab_failed 208 times in 17 hours,
                19 Oct 2026, with nobody told). Not a post's own result ({"posted": ..., "error": ...}: slack_post
                below), and not a dry run's note ("dry-run: no model asked");
  * failures    the per-item contract (ops/heartbeat.py): one reason under "failures" counting 3 items or more and at
                least half the run's "candidates" (25 of 25 HubSpot checks failed, the run still exited 0);
  * <key>       a count above 0 under a key ending "_errors" (poll_replies' slack_errors);
  * claude_cap  claude_cap_reached above 0: replies were classified "other" with no draft;
  * slack_post  a post the job could not make (a {"posted": false, "error": ...} result; a post that went to
                the log for want of a token is no failure);
  * unusable    the settings are unusable, so jobs refuse to start (ops/cli._record_unusable): one line for all;
  * failure_rate a frequent job (one that runs more often than daily) whose last RATE_RUNS runs hold RATE_ERRORS or
                more errors: each failure alone waits for the next run (transient, or missed), so a job failing one
                run in four never reached anyone (code-platform-ops B13). Posted once a day, with the latest error.
A count under "errors" itself is no error list (settings_sync's sheet errors, which it posts itself), and is left
out. Each finding quotes its first error, shortened (QUOTE) and passed through
logs.redact.
A failure that says a service rejected its key (HTTP 401 or 403 from a client, Slack's invalid_auth, not_authed,
token_revoked or account_inactive, Google's 401 or 403 or a service-account key refused, Anthropic's
authentication_error or permission_error) is told apart: "<System> turned down our key", then what to do: put a new one
in <variable> under Variables in Railway and redeploy, the variable from context.SECRET_NAMES (and
ops/bootstrap.GOOGLE_KEY_VAR). Unusable settings link each failing tab and show its first errors (heartbeat.settings_fault),
and point to settings_sync's own message of every row error when its latest run posted one.
Each (job, kind) is posted at most once for each UK day a run of it began, and each rejected key once a day whichever
jobs met it (notify.post_once keeps what was sent in events): a run that failed on Thursday is told on Thursday and
not again on Friday while it is still the latest. A dead Slack token cannot post any of this: the outside
watchdog's /fail ping covers it (ops/watchdog.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from us_outbound import fmt
from us_outbound.base import notify, say
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import SECRET_NAMES, UK, Context
from us_outbound.logs import clip, log, redact
from us_outbound.ops.bootstrap import GOOGLE_KEY_VAR
from us_outbound.ops.heartbeat import DAILY, EXPECTED, and_list, settings_fault, unusable_tabs
from us_outbound.ops.watchdog import JOB as SELF  # heartbeat_check: its own detail holds what it found
from us_outbound.settings import sync
from us_outbound.settings.validate import CLAUDE_CAP_USD
from us_outbound.timeparse import utc

LOOKBACK = timedelta(hours=26)  # a daily job's latest run is always this recent
QUOTE = 160  # characters of the first error quoted
DEPTH = 4  # how deep in a job's detail errors are looked for
LATEST_SQL = (  # each job's two latest finished runs: the latest, and the one before (transient errors, below)
    "SELECT job, run_id, status, started_at, finished_at, detail, error FROM ("
    "SELECT *, ROW_NUMBER() OVER (PARTITION BY job ORDER BY started_at DESC, run_id DESC) AS n"
    " FROM {schema}.heartbeats WHERE started_at >= %(since)s AND status <> 'running') t WHERE n <= 2"
    " ORDER BY job, started_at DESC, run_id DESC"
)
# A service's own outage (HTTP 5xx or 429, a timeout, a dropped connection): a job that runs hourly or more often
# meets one now and then and the next run goes through (Harry, 14 Oct 2026: one Instantly 502 reached Slack). It is
# posted only when the run before met it too.
TRANSIENT = re.compile(r"HTTP (?:5\d\d|429)\b|timed out|timeout|ConnectionError|RemoteDisconnected|"
                       r"Connection (?:reset|aborted)", re.IGNORECASE)
SERVER_ERROR = re.compile(r"\b(\w+) HTTP (\d{3})\b")
FAILED, ERRORS, CLAUDE_CAP, SLACK_POST, UNUSABLE = "failed", "errors", "claude_cap", "slack_post", "unusable"
FAILURES, FAILURE_RATE, ERROR_KEY = "failures", "failure_rate", "error"
FAILURES_MIN, FAILURES_SHARE = 3, 0.5  # one reason failing 3 items or more, and at least half the candidates
RATE_RUNS, RATE_ERRORS = 12, 3  # the failure-rate rule: 3 errors or more in a frequent job's last 12 runs
RECENT_SQL = (  # each job's latest RATE_RUNS finished runs
    "SELECT job, run_id, status, started_at, error FROM ("
    "SELECT job, run_id, status, started_at, error,"
    " ROW_NUMBER() OVER (PARTITION BY job ORDER BY started_at DESC, run_id DESC) AS n"
    " FROM {schema}.heartbeats WHERE started_at >= %(since)s AND status <> 'running' AND job = ANY(%(jobs)s)) t"
    " WHERE n <= %(runs)s ORDER BY job, started_at DESC, run_id DESC"
)
# The post (Harry, 12 Oct 2026: clear to a newcomer; base/say.py): a headline, one bullet per finding with the job in
# plain words (say.job_name), what to do where someone must act, and where to look for more.
HEAD = f"{say.ACTION} *Some automatic jobs ran into problems*"
# A text under a key ending "_error", in plain words (the key itself, spaced, when it is not here).
ERROR_WORDS = {
    "vitals_error": "could not read the mailboxes' health from Instantly",
    "claude_error": "could not ask Claude about a reply",
    "interest_error": "could not check a reply's interest",
    "alert_error": "could not post its alert",
    "capacity_error": "could not check the sending capacity ahead",
    "customers_error": "could not read Spill's customers from HubSpot",
    ERROR_KEY: "met an error",
}
COUNT_WORDS = {"slack_errors": "Slack posts failed"}  # a count under a key ending "_errors"


def foot(stopped: bool = False) -> str:
    """The post's last line: when it fixes itself, and where the rest is (read at post time: logs_link reads env).
    stopped: the settings are unusable, so the jobs do not try again until they are fixed (its own steps say so)."""
    again = "" if stopped else "Each job tries again at its next run. "
    return f"_{again}More detail: {say.logs_link()}, or {say.command('status')} for each job's last run._"


# system -> (who Harry knows it as, the Railway variable holding its key)
KEYS = {
    "apollo": ("Apollo", SECRET_NAMES["apollo"]),
    "instantly": ("Instantly", SECRET_NAMES["instantly"]),
    "hubspot": ("HubSpot", SECRET_NAMES["hubspot"]),
    "slack": ("Slack", SECRET_NAMES["slack"]),
    "claude": ("Anthropic (Claude)", SECRET_NAMES["claude"]),
    "sheets": ("Google", GOOGLE_KEY_VAR),
}
# How a rejected key reads in an error: clients/http.ApiError ("hubspot HTTP 401 for /crm/..."), Slack's error
# codes (it answers HTTP 200 with ok false), clients/claude.ClaudeError ("Claude API error 401"), google-auth.
AUTH = (
    (re.compile(r"\b(apollo|instantly|hubspot|sheets|slack)\s+HTTP\s+(?:401|403)\b", re.I), ""),
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
    note: str = ""  # unusable: where the settings sync's own message listed every error


def latest_finished(ctx: Context, jobs: Iterable[str]) -> dict[str, dict]:
    """job -> its latest finished run that started in the last LOOKBACK, with its detail (one query)."""
    return two_latest(ctx, jobs)[0]


def two_latest(ctx: Context, jobs: Iterable[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    """(job -> its latest finished run, job -> the run before it), of runs started in the last LOOKBACK."""
    wanted = set(jobs)
    since = ctx.now - LOOKBACK
    try:
        rows = ctx.store.query(LATEST_SQL.format(schema=ctx.store.schema), {"since": since})
    except NotImplementedError:  # MemoryStore: the same in Python
        rows = []
        for r in ctx.store.select("heartbeats", {"job": sorted(wanted)}):
            started = utc(r.get("started_at"))
            if r.get("status") != "running" and started is not None and started >= since:
                rows.append(r)
        rows.sort(key=lambda r: (utc(r.get("started_at")), str(r.get("run_id"))), reverse=True)
    latest: dict[str, dict] = {}
    before: dict[str, dict] = {}
    for r in rows:
        job = str(r.get("job") or "")
        if job not in wanted:
            continue
        if job not in latest:
            latest[job] = dict(r)
        elif job not in before:
            before[job] = dict(r)
    return latest, before


def recent_runs(ctx: Context, jobs: Iterable[str]) -> dict[str, list[dict]]:
    """job -> its latest RATE_RUNS finished runs started in the last LOOKBACK, the latest first (one query)."""
    wanted = sorted(set(jobs))
    if not wanted:
        return {}
    since = ctx.now - LOOKBACK
    try:
        rows = ctx.store.query(RECENT_SQL.format(schema=ctx.store.schema),
                               {"since": since, "jobs": wanted, "runs": RATE_RUNS})
    except NotImplementedError:  # MemoryStore: the same in Python
        rows = []
        for r in ctx.store.select("heartbeats", {"job": wanted}):
            started = utc(r.get("started_at"))
            if r.get("status") != "running" and started is not None and started >= since:
                rows.append(r)
        rows.sort(key=lambda r: (utc(r.get("started_at")), str(r.get("run_id"))), reverse=True)
    out: dict[str, list[dict]] = {}
    for r in rows:
        runs = out.setdefault(str(r.get("job") or ""), [])
        if len(runs) < RATE_RUNS:
            runs.append(dict(r))
    return out


def failure_rates(ctx: Context, jobs: Iterable[str]) -> list[Finding]:
    """The failure-rate rule (module docstring): a frequent job with RATE_ERRORS or more errors in its last RATE_RUNS
    runs, with its latest error. A daily (or rarer) job is told on its first failure (failed) instead."""
    frequent = [j for j in jobs if 0 < EXPECTED.get(j, 0) < DAILY]
    out = []
    for job, runs in sorted(recent_runs(ctx, frequent).items()):
        # A refusal on unusable settings is told once for every job (unusable), not again as each job's failure rate.
        errors = [r for r in runs if r.get("status") == "error" and not unusable_tabs(r.get("error"))]
        if len(errors) >= RATE_ERRORS:
            out.append(Finding(job, FAILURE_RATE, [str(errors[0].get("error") or "no error text")], count=len(errors),
                               at=utc(errors[0].get("started_at"))))
    return out


def transient(f: Finding) -> bool:
    """Whether every error of the finding is a service's own outage (TRANSIENT)."""
    return bool(f.texts) and all(TRANSIENT.search(str(t)) for t in f.texts)


def passing(job: str, found: list[Finding], before: Mapping[str, Any] | None) -> list[Finding]:
    """The findings worth a post: an outage a frequent job met once is left out until the run before met it too."""
    if EXPECTED.get(job, 0) >= DAILY:
        return found
    earlier = {f.kind for f in findings(job, before)} if before else set()
    return [f for f in found if not transient(f) or f.kind in earlier]


def _walk(detail: Any, depth: int = 0) -> Iterator[tuple[str, Any, Mapping[str, Any]]]:
    """(key, value, the mapping holding it) for every key in the detail, nested mappings included, to DEPTH."""
    if not isinstance(detail, Mapping) or depth > DEPTH:
        return
    for key, value in detail.items():
        yield str(key), value, detail
        if isinstance(value, Mapping):
            yield from _walk(value, depth + 1)


def _error_text(value: Any, holder: Mapping[str, Any]) -> bool:
    """Whether a text under a key named error is a failure: not a post's own result (slack_post reads those), not
    a dry run's note, and not the log-only post with no token."""
    return (isinstance(value, str) and bool(value.strip()) and "posted" not in holder
            and not value.startswith("dry-run") and value != notify.NO_TOKEN)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _failures(job: str, detail: Mapping[str, Any], at: datetime | None) -> Finding | None:
    """The per-item contract (module docstring): the reasons that failed FAILURES_MIN items or more and at least
    FAILURES_SHARE of the candidates (with no candidates given, the count alone), the commonest first."""
    failures = detail.get(FAILURES)
    if not isinstance(failures, Mapping):
        return None
    tried = _count(detail.get("candidates"))
    tried = tried if tried and tried > 0 else None
    hits = sorted(((str(reason), n) for reason, n in ((r, _count(v)) for r, v in failures.items())
                   if n is not None and n >= FAILURES_MIN and (tried is None or n >= FAILURES_SHARE * tried)),
                  key=lambda x: (-x[1], x[0]))
    if not hits:
        return None
    texts = [f"{reason} ({n} of {tried})" if tried else f"{reason} ({n})" for reason, n in hits]
    return Finding(job, FAILURES, texts, count=sum(n for _, n in hits), at=at)


def findings(job: str, run: Mapping[str, Any]) -> list[Finding]:
    """What one run's heartbeat records as gone wrong (the module docstring's kinds)."""
    at = utc(run.get("started_at"))
    detail = run.get("detail") if isinstance(run.get("detail"), Mapping) else {}
    if run.get("status") == "error":
        if "unusable" in detail:
            return [Finding("settings", UNUSABLE, [str(t) for t in detail.get("unusable") or ()], at=at)]
        error = str(run.get("error") or "no error text")
        if EXPECTED.get(job, 0) < DAILY and not rejected_key(error):
            return []  # tried again within minutes; missed (with this error) if it keeps failing
        return [Finding(job, FAILED, [error], at=at)]
    out: dict[str, Finding] = {}
    failed = _failures(job, detail, at)
    if failed is not None:
        out[FAILURES] = failed
    for key, value, holder in _walk(detail):
        if key == ERROR_KEY and _error_text(value, holder):
            out.setdefault(ERROR_KEY, Finding(job, ERROR_KEY, at=at)).texts.append(value)
        elif key == "claude_cap_reached" and isinstance(value, int) and value > 0:
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
    """The error in a few words: a service's error page is named, not quoted (14 Oct 2026: a 502 quoted its HTML)."""
    text = str(redact(text))
    hit = SERVER_ERROR.search(text)
    if hit and (int(hit.group(2)) >= 500 or "<html" in text.lower() or "<!doctype" in text.lower()):
        return (f"{hit.group(1).capitalize()}'s server had an error (HTTP {hit.group(2)}), a problem on their side "
                "that usually clears by itself")
    return "“" + clip(" ".join(text.split()), QUOTE) + "”"


def _when(at: datetime | None) -> str:
    return f"{at.astimezone(UK):%a %H:%M} UK" if at else "recently"


def _job(job: str) -> str:
    """The job in plain words, capitalised to open a line: "Checking for replies"."""
    name = say.job_name(job)
    return name[:1].upper() + name[1:]


def _unusable(f: Finding, ctx: Context) -> str:
    """The settings are unusable: the tabs linked, their first errors, and the steps (heartbeat.settings_fault)."""
    facts, steps = settings_fault(ctx, f.texts)
    out = [f"• {say.URGENT} *The jobs have stopped (since {_when(f.at)})*. {facts[0]}",
           *(f"   {x.strip()}" if x.startswith("   ◦") else f"   {x}" for x in facts[1:])]
    if f.note:
        out.append(f"   {f.note}")
    out.append("   *What to do:*")
    out += [f"   {i}. {s}" for i, s in enumerate(steps, 1)]
    return "\n".join(out)


def describe(f: Finding, ctx: Context) -> str:
    """One bullet for a finding that is not a rejected key."""
    if f.kind == UNUSABLE:
        return _unusable(f, ctx)
    when = _when(f.at)
    if f.kind == CLAUDE_CAP:
        now = ctx.settings.general.claude_monthly_cap_usd
        return (f"• *Replies are going without draft answers* ({say.job_name(f.job)}, {when}): Claude's monthly "
                f"spending cap is used up, so {fmt.plural(f.count, 'reply', 'replies')} were filed as “other” with no "
                f"draft.\n   *What to do:* raise `claude_monthly_cap_usd` (now ${now:,.0f}, at most "
                f"${CLAUDE_CAP_USD:,.0f}) on the {say.sheet_link(ctx, 'General')}, and the spend limit in the Anthropic "
                "Console to match.")
    if f.kind == FAILED:
        return f"• *{_job(f.job)}* failed ({when}): {_quote(f.texts[0])}"
    if f.kind == FAILURES:
        return (f"• *{_job(f.job)}* ({when}) finished, but {fmt.plural(f.count, 'item', 'items')} in it failed. The "
                f"commonest: {_quote(f.texts[0])}")
    if f.kind == FAILURE_RATE:
        return (f"• *{_job(f.job)}* failed in {f.count} of its last {RATE_RUNS} runs (the latest {when}): each "
                f"failure alone is tried again, but this one keeps coming back. The latest: {_quote(f.texts[0])}")
    if f.kind == SLACK_POST:
        return f"• *{_job(f.job)}* ({when}) could not post its message to Slack: {_quote(f.texts[0])}"
    if f.kind == ERRORS:
        n = len(f.texts)
        return (f"• *{_job(f.job)}* ({when}) finished, but skipped past {fmt.plural(n, 'error', 'errors')}. "
                f"The first: {_quote(f.texts[0])}")
    if not f.texts:  # a count: slack_errors
        return f"• *{_job(f.job)}* ({when}): {f.count} {COUNT_WORDS.get(f.kind, f.kind.replace('_', ' '))}"
    return f"• *{_job(f.job)}* ({when}) {ERROR_WORDS.get(f.kind, f.kind.replace('_', ' '))}: {_quote(f.texts[0])}"


def _key_rejected(system: str, jobs: list[str], first: str) -> str:
    name, var = KEYS[system]
    return (f"• {say.URGENT} *{name} turned down our key* ({and_list(say.job_name(j) for j in jobs)}): "
            f"{_quote(first)}\n   *What to do:* if the key was revoked or has expired, put a new one in `{var}` "
            f"under Variables in Railway, then redeploy. If the key is current, our {name} plan may not allow this "
            "request.")


def _run_day(at: datetime | None, day: str) -> str:
    """The UK day the run began, which keys its post; `day` (today) when its start is unknown."""
    return at.astimezone(UK).date().isoformat() if at else day


def lines(found: Iterable[Finding], day: str, ctx: Context) -> list[tuple[str, str]]:
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
        out.append((f"job_error:{f.job}:{f.kind}:{_run_day(f.at, day)}", describe(f, ctx)))
    for system, hits in keys.items():
        jobs = list(dict.fromkeys(j for j, _, _ in hits))
        out.insert(0, (f"key_rejected:{system}:{max(d for _, _, d in hits)}", _key_rejected(system, jobs, hits[0][1])))
    return out


def _sync_post(run: Mapping[str, Any] | None) -> str:
    """Where settings_sync's own message listed every row error (settings/sync.format_errors), if its latest run
    posted one; "" otherwise (a version saved earlier that a newer check rejects gets no such message)."""
    detail = (run or {}).get("detail")
    if not isinstance(detail, Mapping) or not (detail.get("rejected") and detail.get("alerted")):
        return ""
    return (f"Every error on the sheet is listed in the settings sync's message of {_when(utc(run.get('started_at')))}"
            f" (“{sync.HEADLINE}…”).")


def check(ctx: Context, jobs: Iterable[str]) -> dict[str, Any]:
    """Read, then post what was not posted today. Never raises (but a GuardViolation): heartbeat_check goes on."""
    try:
        watched = [j for j in jobs if j != SELF]
        runs, before = two_latest(ctx, watched)
        found = [f for job, run in sorted(runs.items()) for f in passing(job, findings(job, run), before.get(job))]
        found += failure_rates(ctx, watched)  # never filtered as transient: it exists for the intermittent ones
        unusable = [f for f in found if f.kind == UNUSABLE]
        found = [f for f in found if f.kind != UNUSABLE] + unusable[:1]  # one line for all the jobs it stops
        if unusable:
            found[-1].note = _sync_post(runs.get("settings_sync"))
        pairs = lines(found, ctx.today_uk().isoformat(), ctx)
        sent = notify.post_once(ctx, pairs, head=HEAD, foot=foot(bool(unusable))) if pairs else {"posted": False, "keys": []}
    except GuardViolation:
        raise
    except Exception as exc:  # the database, say: the watchdog ping must still go
        log("job_errors_failed", error=str(exc)[:200])
        return {"found": [], "posted": [], "check_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    log("job_errors", found=[k for k, _ in pairs], posted=sent.get("keys"))
    return {"found": [notify.kind_of(k) for k, _ in pairs], "posted": sent.get("keys") or [],
            "post": {k: sent.get(k) for k in ("posted", "error")}}
