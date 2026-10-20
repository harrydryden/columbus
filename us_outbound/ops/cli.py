"""The us-outbound command line (SPEC 13 Operations): the same entrypoints as the jobs.

The commands are one table (COMMANDS, W2-4, 20 Oct 2026; RS P0-10), listed by `us-outbound --help` in its order:
  status                              live_sending and what it means, when the settings were synced, what waits
                                      for Harry (replies, kill-rule holds), the jobs that failed or missed their
                                      heartbeat, retention, mailboxes, this week's number, the campaigns, and the
                                      three go-live checks (ops/live_checks.py: Copy, Campaigns, Watchdog)
  accounts [DOMAIN] [--status S]      the companies and contacts we hold, read-only (ops/accounts_view.py), or a CSV
  sync                                settings_sync now: the sheet's edits into force (else 02:00, and 11:30 weekdays)
  start | stop [--no-db]              resume or pause every US Outbound campaign, and enrolment. stop --live is the
                                      brake (live_sending = no does not stop Instantly); --no-db pauses the campaigns
                                      while the database is down. Over a blackout start leaves the campaigns paused
  replies list | send | skip [ITEM]   the reply desk without Slack (replies/desk.py); approve is the same as send
  handcheck show|approve|export|migrate-tab   the weekly hand-check (enrol/hand_check.py, hand_check_tab.py)
  killrules show | clear <item>       the kill-rule holds in force, and lifting one (learn/kill_rules.py)
  mailbox add|pause|retire|check      the registry commands of SPEC 9 (check is mailbox_health by hand; --fix)
  campaigns ensure [--fix] [--in-flight]   create the missing campaigns (paused) and put drift right; drift in the
                                      steps, delays or text_only waits while leads are in flight unless --in-flight
                                      (docs/developing-while-live.md: the one way a pinned campaign constant ships)
  cohorts in-flight | changes [A B]   the leads with a step still to send; what differs between two config versions
  test start | read <test_id>         a test on the Tests tab, started or read at a pre-registered look
  labels set | show | eval            a company's label set or shown; the label check scored on the gold set
  copy check|preview|qa|draft         the copy desk (enrol/copy_desk.py): a Copy row is sendable only once QA passes
  seed send | check                   the seed-inbox test of the unsubscribe link (ops/seed.py)
  settings load                       the build's tabs (or with --take note, the General notes) into the sheet
  report <name>                       a read-only report: cohorts, signals, readout, lookalikes, pages, daily, and
                                      capture (the reply read's canary checked by hand; with --live it deletes the
                                      canary's own lead, which reaches no prospect)
  erase --email <address>             an erasure request (SPEC 6)
  unenrol --month YYYY-MM             a month's leads out of their campaigns, recorded as stopped (ops/campaigns.py)
  verify migrate-parked               once after W1-4: the companies left out while their industry was off, parked
  db apply                            the DDL in sql/ (ops/ddl.py)
  schedule                            the job table and next runs (UK time)
  run <job> [--live] [--due MINUTE]   one job, what the scheduler starts (--due: the minute it was due, which the run
                                      claims, ops/health.run_job); its summary as one JSON line
  scheduler                           the always-on Railway worker (ops/scheduler.py)
On Railway, run a command inside the worker with `railway ssh -- us-outbound <command>` (docs/railway-setup.md).
Commands folded in W2-4: daily, signals, readout, lookalikes show, pages show and capture check are `report <name>`;
rescore, dry-run and suppression load are `run score`, `run <job>` and `run suppression_load`; settings sync is
`sync`. Removed: relabel and labels audit (verify_accounts asks the label check about 150 companies a run), labels
sample (the label_sample_monthly job), settings bootstrap, campaigns show, hubspot ids, lookalikes fit, data show.
tests/test_cli.py greps every `us-outbound <command>` in the code and docs/*.md against this table.

Dry-run is the default everywhere. Two kinds of live:
  * jobs (run, sync) and start: --live AND live_sending = yes in the synced settings, as SPEC 0.3 says (a dry-run
    sync still brings the sheet in: only its Slack message goes to the dev channel);
  * operator commands whose writes never reach a prospect (stop, mailbox, unenrol, erase, test start, settings load,
    copy qa|draft, campaigns ensure, handcheck show|approve|export|migrate-tab, killrules clear, replies skip,
    report capture, labels set, verify): --live alone, so the setup and the kill switch work while live_sending is
    still no. `replies send` sends to a prospect, so it is live like a job: --live AND live_sending = yes.
  A read-only action given --live says that --live does nothing there. A --live run that stays dry says when the
  settings in force were synced, and how to bring a sheet edit in (`us-outbound sync`).
Every run writes a heartbeats row (ops/health.run_job). stop and start write theirs under operator_stop /
operator_start, which is the enrolment pause enrol checks (enrol.operator_pause).

Exit codes: 0 done; 1 unexpected error (with its traceback, including a KeyError, IndexError or JSON/Unicode
decoding error, which are bugs rather than bad input), or a job that reported status error; 2 refused (not built,
bad input, unusable settings); 3 blocked by a guardrail (GuardViolation); 143 stopped by SIGTERM while a job ran
(the scheduler's timeout, or a redeploy; the heartbeat says error).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import signal
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from us_outbound import fmt
from us_outbound.base.holds import held_mailboxes
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import Context
from us_outbound.logs import log, redact
from us_outbound.learn import copy_tests
from us_outbound.ops import bootstrap, campaigns, health
from us_outbound.ops.health import (ERROR, OPERATOR_START, OPERATOR_STOP, Terminated, enrolment_paused, latest_runs,
                                    overdue_minutes, run_job, scheduled_jobs)

# Every SPEC 9 job: "module:function", or why it cannot run yet.
JOBS: dict[str, str] = {
    "settings_sync": "us_outbound.settings.sync:run",
    "source_universe": "us_outbound.sources.apollo_universe:run",
    "apollo_signals": "us_outbound.sources.apollo_jobs:run",
    "read_pages": "us_outbound.sources.pages:run",  # Harry, 2 Oct 2026: careers and benefits pages, and home pages
    "apollo_enrich": "us_outbound.sources.apollo_enrich:run",  # Harry, 2 Oct 2026: funding and headcount from enrich
    "apollo_people": "us_outbound.sources.apollo_people:run",  # Harry, 5 Oct 2026: People leaders at every account
    "site_visits": "us_outbound.sources.site_visits:run",  # Harry, 5 Oct 2026: Apollo's visitors to the US site
    # Build addition: verified on Apollo data and HubSpot (Harry, 1 Oct 2026; Clay's verify_in_clay was never built
    # and went with public_signals, both never scheduled, in W2-1, 20 Oct 2026).
    "verify_accounts": "us_outbound.verify:run",
    "score": "us_outbound.scoring.score:rescore",
    "pick_contacts": "us_outbound.contacts.pick:run",
    "enrol": "us_outbound.enrol.enrol:run",
    "poll_replies": "us_outbound.replies.poll:run",
    "poll_approvals": "us_outbound.replies.poll_desk:poll_desk",  # the job keeps its name (W2-1): the reply desk, the hand-check tab
    "hubspot_readback": "us_outbound.crm.readback:hubspot_readback",
    "sync_outcomes": "us_outbound.replies.outcomes:run",
    "mailbox_health": "us_outbound.registry.mailboxes:mailbox_health",
    "kill_rules": "us_outbound.learn.kill_rules:run",
    "blackout": "us_outbound.registry.blackout:run",  # Harry, 7 Oct 2026: campaigns paused over the blackout dates
    "daily_post": "us_outbound.learn.daily_post:run",
    "capture": "us_outbound.replies.capture:run",  # W1-2, 19 Oct 2026: is the reply read working? Mondays
    "monday_readout": "us_outbound.learn.readout:run",  # Harry, 6 Oct 2026: the learning loop
    # Build additions (README "Deviations").
    "heartbeat_check": "us_outbound.ops.health:run",  # missed jobs, caught errors, the outside watchdog (W2-4)
    "suppression_load": "us_outbound.suppression:load_from_hubspot",
    "lookalikes": "us_outbound.sources.lookalikes:run",  # Harry, 1 Oct 2026: Spill's HubSpot customers as lookalikes
    "lookalike_leads": "us_outbound.sources.lookalike_leads:run",  # Harry, 5 Oct 2026: Apollo's lookalikes, monthly
    "hand_check_draw": "us_outbound.enrol.hand_check:run",  # SPEC 11 weekly hand-check, Mondays (W1-5)
    "retention": "us_outbound.ops.retention:run",  # SPEC 6 and 13: what we hold leaves when its time is up
    # Harry, 11 Oct 2026: a batch of companies to judge by hand each month, last month's scored, to Slack.
    "label_sample_monthly": "us_outbound.ops.label_sample:monthly",
    "follow_up": "us_outbound.replies.follow_up:sweep",  # W2-4, 20 Oct 2026: the not-now dates kept, daily
}
# The module of every job, so one import of each proves no job lost a module it needs (W2-1, 20 Oct 2026: a removal
# wide enough to miss an importer; tests/test_cli.py imports them all).
JOB_MODULES: tuple[str, ...] = tuple(dict.fromkeys(t.partition(":")[0] for t in JOBS.values() if ":" in t))
MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
DUE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")  # `run --due`: the minute the scheduler started it for (UTC)

Factory = Callable[..., Context]


class Refused(Exception):
    """The command cannot go ahead as asked (exit code 2)."""


# -- the job registry -------------------------------------------------------------------


def _is_target(target: str) -> bool:
    return ":" in target and not target.startswith("not built")


def built_jobs() -> list[str]:
    """Jobs listed with a module:function target."""
    return [j for j, t in JOBS.items() if _is_target(t)]


def resolve_job(name: str) -> Callable[[Context], Any]:
    """The job function, or Refused with the reason it cannot run."""
    target = JOBS.get(name)
    if target is None:
        raise Refused(f"unknown job {name!r}; jobs are: {', '.join(JOBS)}")
    if not _is_target(target):
        raise Refused(f"{name}: {target}")
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)  # a broken import is a bug: it surfaces with its traceback


# -- helpers ----------------------------------------------------------------------------


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str, ensure_ascii=False))


LOG_LIST_LIMIT = 10  # items of a list kept in a scheduled run's one-line summary


def _cut(value: Any) -> Any:
    """Lists longer than LOG_LIST_LIMIT cut to that many, then "+N more", at any depth (the log line only: the
    heartbeat row keeps the whole summary)."""
    if isinstance(value, dict):
        return {k: _cut(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_cut(v) for v in value]
        return items if len(items) <= LOG_LIST_LIMIT else [*items[:LOG_LIST_LIMIT], f"+{len(items) - LOG_LIST_LIMIT} more"]
    return value


def _print_line(value: Any) -> None:
    """A scheduled run's summary as one compact JSON line (W1-1, 19 Oct 2026): pretty-printed, one summary was
    hundreds of lines, and Railway dropped 1,276 lines at its rate limit."""
    print(json.dumps(_cut(value), separators=(",", ":"), default=str, ensure_ascii=False))


def _synced(ctx: Context) -> str:
    """When the sheet was last read into the settings in force (settings/sync.last_read), in UK time."""
    from us_outbound.settings import sync

    when = sync.last_read(ctx.store, ctx.settings)
    return fmt.uk_time(when) if when else "never"


def _live_sending_note(ctx: Context) -> str:
    """Why a --live run stayed dry. Jobs read the synced copy of the sheet, so an edit counts only once synced."""
    return (f"Running dry: live_sending is no in the settings in force (synced {_synced(ctx)}). "
            "If you have just set it to yes on the sheet, run `us-outbound sync` and try again.")


READS_ONLY = "(--live does nothing here: this command only reads)"


def _only_reads(args: argparse.Namespace) -> None:
    """A read-only action given --live says so, rather than leave Harry thinking it acted."""
    if getattr(args, "live", False):
        print(READS_ONLY)


def _operate(factory: Factory, job: str, live: bool, fn: Callable[[Context], Any], *, operator: bool = False,
             dry_note: str = "", show: bool = True) -> tuple[Context, Any]:
    """Run fn as the job (health.run_job) in a fresh context. What fn refuses (a LookupError or ValueError) is the
    command's refusal, in fn's words (exit 2); a KeyError or IndexError is a bug and goes to main as one. Then its
    summary is printed (show) and what a dry run left alone (dry_note). 9 Oct 2026: 30 commands repeated this."""
    ctx = factory(job, live, operator=True) if operator else factory(job, live)
    try:
        summary = run_job(ctx, fn)
    except (KeyError, IndexError):
        raise
    except (LookupError, ValueError) as exc:
        raise Refused(str(exc)) from exc
    if show:
        _print(summary)
    if dry_note:
        _dry_note(ctx, dry_note)
    return ctx, summary


def _dry_note(ctx: Context, what: str) -> None:
    if not ctx.dry_run:
        return
    if ctx.live_flag is False:
        print(f"Dry-run: {what} Nothing was sent. Needs --live.")
        return
    print(f"Dry-run: {what} Nothing was sent.")
    print(_live_sending_note(ctx))


def _job(name: str, live_flag: bool, factory: Factory, *, one_line: bool = False, due: str = "") -> int:
    """One job run. one_line: the summary as one JSON line (`run`, what the scheduler starts); pretty otherwise. due:
    the minute the scheduler started it for, which the run claims (ops/health.run_job). A job that reports status
    error exits 1, as one that raised."""
    fn = resolve_job(name)
    when = None
    if due:
        if not DUE_RE.fullmatch(due):
            raise Refused("--due is the minute it was due, in UTC: YYYY-MM-DDTHH:MM")
        when = datetime.fromisoformat(due).replace(tzinfo=UTC)
    try:
        ctx = factory(name, live_flag)
    except bootstrap.SettingsUnusable as exc:
        health.record_unusable(exc.store, name, exc.errors, str(exc))
        raise
    if when is not None:
        ctx.due = when
        # The claim id replaces the run id the "context" line logged; this line ties the two together.
        claim = health.due_run_id(name, when)
        log("run_claim", job=name, context_run_id=ctx.run_id, run_id=claim)
        ctx.run_id = claim
    if live_flag and ctx.dry_run:
        print(_live_sending_note(ctx))
    with _sigterm_ends_the_run():
        summary = run_job(ctx, fn)
    (_print_line if one_line else _print)(summary)
    return 1 if summary.get("status") == ERROR else 0


def _sync(live_flag: bool, factory: Factory) -> tuple[Context, dict]:
    """settings_sync, as `us-outbound sync` and `settings sync` run it: the sheet's edits into force now.

    A dry-run sync still brings the sheet in (the settings live in the database); only its Slack
    message about sheet errors goes to the dev channel instead.
    """
    fn = resolve_job("settings_sync")
    try:
        ctx = factory("settings_sync", live_flag)
    except bootstrap.SettingsUnusable as exc:
        health.record_unusable(exc.store, "settings_sync", exc.errors, str(exc))
        raise
    with _sigterm_ends_the_run():
        summary = run_job(ctx, fn)
    return ctx, summary


def _sync_line(summary: dict) -> str:
    """One plain line on what a sync did."""
    if summary.get("skipped"):
        return f"Settings not synced: {summary.get('reason') or 'skipped'}."
    changed = int(summary.get("rows_opened") or 0) + int(summary.get("rows_closed") or 0)
    line = "Settings synced from the sheet just now: " + (
        f"{changed} row{'s' if changed != 1 else ''} changed." if changed else "nothing had changed.")
    if summary.get("unusable"):
        line += (f" Unusable: {', '.join(summary['unusable'])}; fix the sheet (the errors are in Slack), "
                 "then run `us-outbound sync` again.")
    elif summary.get("rejected"):
        line += (f" Kept the previous version of {', '.join(summary['rejected'])} (the errors are in Slack); "
                 "fix the sheet, then run `us-outbound sync` again.")
    return line


def _in_force_line(ctx: Context) -> str:
    """live_sending as the settings now in force have it (the store, after a sync)."""
    from us_outbound.settings.sync import load_current

    settings, errors = load_current(ctx.store)
    if settings is None:
        tabs = ", ".join(t for t, errs in errors.items() if errs) or "unknown"
        return f"The settings are not usable ({tabs}): nothing runs until the sheet is fixed and synced."
    g = settings.general
    return f"In force now: live_sending {'yes' if g.live_sending else 'no'}."


def cmd_sync(args: argparse.Namespace, factory: Factory) -> int:
    """`us-outbound sync` (and `settings sync`): bring the sheet's edits into force now, not at the next sync."""
    ctx, summary = _sync(args.live, factory)
    _print(summary)
    print(_sync_line(summary))
    print(_in_force_line(ctx))
    return 0


def _terminated(signum: int, frame: Any) -> None:
    raise Terminated("stopped by SIGTERM (the scheduler's timeout, or a redeploy)")


@contextmanager
def _sigterm_ends_the_run() -> Iterator[None]:
    """While a job runs, SIGTERM raises Terminated; the previous handler comes back after."""
    if threading.current_thread() is not threading.main_thread():
        yield  # signal handlers can only be set on the main thread
        return
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


# -- commands ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, factory: Factory) -> int:
    return _job(args.job, args.live, factory, one_line=True, due=args.due or "")


def _status_heartbeats(store: Any, now: datetime) -> None:
    """Only the jobs that need a look (the latest run failed, or the heartbeat is missed), then how many are ok."""
    runs = latest_runs(store)
    scheduled = set(scheduled_jobs())
    ok, never = 0, []
    print("Jobs:")
    for job in built_jobs():
        run = runs.get(job)
        if not run:
            if job in scheduled:
                never.append(job)
            continue
        over = overdue_minutes(job, run, now) if job in scheduled else None
        missed = over is not None and over > 0
        if run.get("status") != "error" and not missed:
            ok += 1
            continue
        mode = "dry-run" if run.get("dry_run") else "live"
        print(f"  {job:<18} {run.get('status')} {fmt.uk_time(run.get('started_at'))} ({mode})"
              f"{'  MISSED its heartbeat' if missed else ''}")
        if run.get("status") == "error" and run.get("error"):
            print(f"  {'':<18} error: {str(run['error'])[:160]}")
    print(f"  {ok} job{'s' if ok != 1 else ''} ok")
    if never:
        print(f"  Not run yet: {', '.join(never)}")


def _switches(g: Any) -> str:
    """The General switch and what it means (auto_send went with the send approvals: W2-1, 20 Oct 2026)."""
    if not g.live_sending:
        meaning = "dry: nothing new reaches Instantly or a prospect; `us-outbound stop --live` pauses what already sends"
    else:
        meaning = "live: enrol adds each send day's leads straight to Instantly"
    return f"Switch: live_sending {'yes' if g.live_sending else 'no'} ({meaning})"


def _waiting_for_you(ctx: Context) -> str:
    """"Waiting for you: 1 reply, 0 kill-rule holds" (learn/daily_post.for_you)."""
    from us_outbound.learn import daily_post

    w = daily_post.for_you(ctx)

    def n(count: int, one: str, many: str) -> str:
        return f"{count} {one if count == 1 else many}"

    line = (f"Waiting for you: {n(len(w.replies), 'reply', 'replies')}, "
            f"{n(len(w.holds), 'kill-rule hold', 'kill-rule holds')}")
    return line + (", and this week's hand-check" if w.hand_checks else "")


def _status_limits(ctx: Context) -> None:
    """This month's credit budgets and what limits today's enrollment (limits.py), as the enrol job would see it now."""
    from us_outbound.enrol import today

    try:
        lim = today.read(ctx).limits  # as enrol.run sees it
    except Exception as exc:  # status still prints what it can
        print(f"This week: unavailable ({type(exc).__name__}: {redact(str(exc))[:160]})")
        return
    print("Credit budgets this month (UK time):")
    for line in lim.budget_lines:
        print(f"  {line}")
    print("Enrolment this week (Monday to Sunday, UK time):")
    print(f"  {lim.explanation}")
    for line in lim.detail:
        print(f"  {line}")


def cmd_status(args: argparse.Namespace, factory: Factory) -> int:
    try:
        ctx = factory("status", False)
    except bootstrap.SettingsUnusable as exc:
        print(f"Settings: UNUSABLE. {exc}")
        for tab, errs in exc.errors.items():
            for e in errs[:5]:
                print(f"  {tab}: {e}")
        if exc.store is not None:
            _status_heartbeats(exc.store, datetime.now(UTC))
        return 2
    s, g = ctx.settings, ctx.settings.general
    print(_switches(g))
    print(f"Settings synced: {_synced(ctx)} (sheet edits apply at 02:00 and 11:30 UK on weekdays, or now with "
          "`us-outbound sync`)")
    try:
        print(_waiting_for_you(ctx))
    except Exception as exc:  # status still prints what it can
        print(f"Waiting for you: unavailable ({type(exc).__name__}: {redact(str(exc))[:120]})")
    paused = enrolment_paused(ctx.store)
    if paused:
        print(f"Enrollment: STOPPED since {fmt.uk_time(paused.get('started_at'))} (run `us-outbound start --live` to resume)")
    else:
        print("Enrollment: not stopped by an operator")
    running = s.running_test()
    print(f"Running test: {running.test_id} ({running.kind}, read on {running.read_date})" if running
          else "Running test: none")
    _status_heartbeats(ctx.store, ctx.now)
    try:
        from us_outbound.ops import retention

        print(retention.status_line(ctx.store))  # SPEC 6 and 13: what the daily retention job deleted, and what waits
    except Exception as exc:  # status still prints what it can
        print(f"Retention: unavailable ({type(exc).__name__}: {redact(str(exc))[:120]})")
    print("Mailboxes:")
    try:
        from us_outbound.registry.ramp import ramps

        on_ramp, held = ramps(ctx.store, s, ctx.now_et().date()), held_mailboxes(ctx.store)
    except Exception as exc:  # status still prints what it can
        print(f"  (ramp unavailable: {type(exc).__name__}: {redact(str(exc))[:120]})")
        on_ramp, held = {}, {}
    for m in s.mailboxes:
        r = on_ramp.get(m.address.lower())
        note = f"; today {r.cap}: {r.describe()}" if r is not None and r.ramping else ""
        note += f"; held by a kill rule: {held[m.address.lower()]}" if m.address.lower() in held else ""
        print(f"  {m.address:<28} {m.owner_name:<18} {m.status:<8} cap {m.daily_cap}{note}")
    _status_limits(ctx)
    try:
        campaigns = ctx.clients.instantly.list_campaigns()
        from us_outbound.clients.instantly import CAMPAIGN_STATUS
        from us_outbound.registry import blackout

        over = blackout.words(ctx.store, ctx.settings, ctx.now)  # "paused for the blackout until …" (7 Oct 2026)
        print("Campaigns:")
        for c in campaigns:
            state = CAMPAIGN_STATUS.get(c.get("status"), c.get("status"))
            print(f"  {c.get('name')}: {over[c.get('name')] if state == 'paused' and c.get('name') in over else state}")
        if not campaigns:
            print("  none yet")
    except Exception as exc:  # status still prints what it can
        print(f"Campaigns: unavailable ({type(exc).__name__}: {redact(str(exc))[:160]})")
    _status_live_checks(ctx)
    return 0


def _status_live_checks(ctx: Context) -> None:
    """The three go-live checks status keeps (ops/live_checks.py; W2-1): Copy coverage, the campaigns, the watchdog."""
    from us_outbound.ops import live_checks

    try:
        lines = live_checks.lines(live_checks.run_checks(ctx))
    except GuardViolation:
        raise
    except Exception as exc:  # status still prints what it can
        lines = [f"Go-live checks: unavailable ({type(exc).__name__}: {redact(str(exc))[:160]})"]
    for line in lines:
        print(line)


NO_DB_NOTE = ("no operator_stop row was written: enrol will keep adding leads to paused campaigns (they do not send); "
              "run `us-outbound stop --live` once the database is back")


def _brake_context(live: bool) -> Context:
    """The context of `stop --no-db` (bootstrap.brake_context); tests replace it."""
    return bootstrap.brake_context(live)


def cmd_stop(args: argparse.Namespace, factory: Factory) -> int:
    if getattr(args, "no_db", False):
        return _stop_without_db(args)
    ctx = factory(OPERATOR_STOP, args.live, operator=True)
    summary = run_job(ctx, campaigns.stop)
    _print(summary)
    print("Enrollment is stopped (in every mode) until `us-outbound start --live` runs with live_sending = yes.")
    _dry_note(ctx, "campaigns were not paused in Instantly.")
    return 0


def _stop_without_db(args: argparse.Namespace) -> int:
    """`stop --live --no-db` (W1-1, 19 Oct 2026; code-platform-ops B4): the brake when Postgres is down. No settings
    and no heartbeat (both live in the database): the campaigns named "US Outbound – " are paused through the
    Instantly client on a guard built from the environment alone, and the alert channel is told."""
    from us_outbound.base import notify

    ctx = _brake_context(args.live)
    summary = campaigns.stop(ctx)
    summary["enrollment"] = "not stopped: no database"
    _print(summary)
    print(NO_DB_NOTE)
    if ctx.live:
        paused = ", ".join(summary["campaigns_paused"]) or "none was sending"
        sent = notify.alert(ctx, f"Emergency stop without the database: paused {paused}. {NO_DB_NOTE}.")
        if not sent.get("posted"):
            print(f"(the alert was not posted to Slack: {sent.get('error') or 'no reason given'})")
    _dry_note(ctx, "campaigns were not paused in Instantly.")
    return 0


def cmd_start(args: argparse.Namespace, factory: Factory) -> int:
    """Resume. With --live it syncs the settings first, so live_sending just set on the sheet counts."""
    synced = False
    if args.live:
        try:
            _, summary = _sync(True, factory)
        except (GuardViolation, Terminated):
            raise
        except Exception as exc:  # start still goes ahead, on the settings already in force
            print(f"Could not sync the settings first ({type(exc).__name__}: {redact(str(exc))[:200]}); "
                  "going on with the settings in force.")
        else:
            synced = not summary.get("skipped")
            print(_sync_line(summary))
    ctx = factory(OPERATOR_START, args.live)  # reads the settings in force again, after the sync
    _print(run_job(ctx, campaigns.start))
    if ctx.dry_run and args.live and synced:
        print("Dry-run: campaigns were not activated and enrollment stays stopped. Nothing was sent.")
        print(f"Running dry: live_sending is no in the settings in force (synced {_synced(ctx)}, just now). "
              "Set it to yes on the General tab, then run `us-outbound start --live` again.")
    else:
        _dry_note(ctx, "campaigns were not activated and enrollment stays stopped.")
    return 0


def cmd_mailbox(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.registry import mailboxes as reg

    if args.fix and args.action != "check":
        raise Refused("--fix goes with check")
    if args.action == "check":
        ctx = factory("mailbox_health", args.live, operator=True)
        # --fix also sets each drifted sender name to its owner's full name (Harry, 5 Oct 2026).
        _print(run_job(ctx, lambda c: reg.mailbox_health(c, fix_names=args.fix)))
        if args.fix:  # and what `campaigns ensure --fix` does, so one command sets the senders up
            fix_ctx = factory("campaigns_ensure", args.live, operator=True)
            _print(run_job(fix_ctx, lambda c: reg.ensure_campaigns(c, fix=True)))
        _dry_note(ctx, "the sheet and Instantly were not changed.")
        return 0
    if not args.address:
        raise Refused(f"mailbox {args.action} needs an address")
    ctx = factory(f"mailbox_{args.action}", args.live, operator=True)
    if args.action == "add":
        if not args.owner:
            raise Refused("mailbox add needs --owner \"Full Name\" (the real person named on the address)")
        fn = lambda c: reg.mailbox_add(  # noqa: E731
            c, args.address, owner=args.owner, domain=args.domain, daily_cap=args.daily_cap
        )
    elif args.action == "pause":
        fn = lambda c: reg.mailbox_pause(c, args.address)  # noqa: E731
    else:
        fn = lambda c: reg.mailbox_retire(c, args.address)  # noqa: E731
    _print(run_job(ctx, fn))
    _dry_note(ctx, "the sheet and Instantly were not changed.")
    if ctx.live:
        print("The sheet is changed; the next settings sync brings it into force (or run `us-outbound sync` now).")
    return 0


def cmd_unenrol(args: argparse.Namespace, factory: Factory) -> int:
    if not MONTH_RE.fullmatch(args.month or ""):
        raise Refused("--month must be written YYYY-MM")
    ctx, _ = _operate(factory, "unenrol", args.live, lambda c: campaigns.unenrol(c, args.month),
                     operator=True, dry_note="no lead was removed from Instantly.")
    return 0


def cmd_verify(args: argparse.Namespace, factory: Factory) -> int:
    """`us-outbound verify migrate-parked [--live]` (W1-4, 19 Oct 2026; RS P0-3), once after the deploy: the companies
    the label check left out for good while their industry was switched off (~50 on 13 Oct) are parked, so the next
    verify_accounts brings back those whose industry is on (verify.migrate_parked). A database write that reaches no
    prospect: --live alone."""
    from us_outbound import verify

    ctx = factory("verify_migrate_parked", args.live, operator=True)
    out = run_job(ctx, verify.migrate_parked)
    print(f"{'Parked' if ctx.live else 'Would park'} {out['to_park']} of {out['disqualified']} disqualified companies: "
          "left out by the label check while their industry was switched off.")
    if out["domains"]:
        print("  " + ", ".join(out["domains"]))
    _dry_note(ctx, "no company was moved.")
    return 0


def cmd_erase(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.ops.erase import erase

    ctx = factory("erase", args.live, operator=True)
    report = run_job(ctx, lambda c: erase(c, args.email))
    _print({k: v for k, v in report.items() if k != "manual_steps"})
    print("Manual steps for Harry:")
    for step in report.get("manual_steps", []):
        print(f"  - {step}")
    return 0


# -- copy tests (SPEC 12) -----------------------------------------------------------------


def read_test(ctx: Context, test_id: str) -> dict:
    """The test at its latest pre-registered look (learn/copy_tests.read)."""
    return copy_tests.read(ctx, test_id)


def cmd_test(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.learn import looks

    if args.action == "start":
        ctx, _ = _operate(factory, "test_start", args.live, lambda c: copy_tests.start(c, args.test_id),
                         operator=True, dry_note="the Tests tab was not changed.")
        return 0
    _only_reads(args)
    ctx = factory("test_read", False)
    try:
        result = read_test(ctx, args.test_id)
    except (looks.NotYet, LookupError) as exc:  # the no-peek rule (Harry, 6 Oct 2026)
        raise Refused(str(exc)) from exc
    _print(result)
    look = result["look"]
    print(f"Read at {look['label']}" + ("." if look["final"] else f"; the next is {result['next_look']}."
                                         if result["next_look"] else "."))
    print(looks.summary_line(result))
    print("Reply rate decides (a 2x difference is what the test detects); positive and meeting rates are for information.")
    print("Harry writes the result on the Tests tab.")
    return 0


# -- build support ------------------------------------------------------------------------


def cmd_settings(args: argparse.Namespace, factory: Factory) -> int:
    """`settings load`: the build's tabs (or the General notes) into the sheet (settings/load.py)."""
    from us_outbound.settings.load import DEFAULT_TABS, load

    ctx = factory("settings_load", args.live, operator=True)
    tabs = args.tab or list(DEFAULT_TABS)
    sets = {}
    for item in args.set or ():
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise Refused(f"--set takes key=value, not {item!r}")
        sets[key.strip()] = value.strip()
    try:
        summary = run_job(ctx, lambda c: load(c, tabs, sets, replace_drafts=args.replace_drafts,
                                               take=args.take or (), keep=args.keep or ()))
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    for t in summary["tabs"]:
        _print(t)
    _dry_note(ctx, "the sheet is unchanged.")
    if ctx.live:
        print("Now run `us-outbound sync` to bring the tabs into force (or wait for the next sync: 02:00, and "
              "11:30 UK on weekdays).")
    return 0


def _sheet_settings(ctx: Context):
    """The settings as the sheet has them now, validated (not yet synced), so copy can be checked as it is edited."""
    from us_outbound.settings.sync import read_sheet
    from us_outbound.settings.validate import validate_all

    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        return ctx.settings
    settings, errors = validate_all(read_sheet(ctx, sheet_id))
    if settings is None:
        lines = [str(e) for errs in errors.values() for e in errs]
        raise Refused("the sheet does not validate: " + "; ".join(lines[:10]))
    return settings




def _pick_rows(settings, versions: Sequence[str] | None, industry: str | None, role: str | None) -> list:
    rows = [c for c in settings.copy if c.status != "retired"]
    if versions:
        want = {v.casefold() for v in versions}
        rows = [c for c in rows if c.copy_version.casefold() in want]
    if industry:
        rows = [c for c in rows if c.industry.casefold() == industry.casefold()]
    if role is not None and industry:
        rows = [c for c in rows if c.role.casefold() == role.casefold()] or rows
    return rows


def _preview(ctx: Context, settings: Any, args: argparse.Namespace, copy_desk: Any) -> Any:
    """copy preview: the sample prospect (with --opener, a real Signals-tab line filled with sample facts, or
    with --generic the General tab's generic line), or with --account a stored account, its contact and the
    opener enrol would give it (no model call). Email 1's subject is the account's own arm, or with --subject
    personal|copy that arm (General email1_subject, or the Copy row's s1_subject; Harry, 5 Oct 2026)."""
    from us_outbound.enrol import enrol, openers
    from us_outbound.settings.model import ROLE_LINE_COLUMNS

    if args.subject == "personal" and not settings.general.email1_subject.strip():
        raise Refused("email1_subject is blank on the General tab, so there is no personal subject to show")
    if not args.account:
        rows = _pick_rows(settings, args.version, args.industry, args.role or "")
        if not rows:
            raise Refused("no Copy row matches; give --version or --industry")
        role = args.role or rows[0].role or next(iter(ROLE_LINE_COLUMNS))
        text, note = ("", "")
        if args.opener:
            if args.generic and (args.signal or args.leader):
                raise Refused("--generic shows the line for an account with no signal line; leave out --signal and --leader")
            try:
                text, note = copy_desk.sample_opener(settings, role, args.signal or "", leader=args.leader,
                                                     generic=args.generic)
            except ValueError as exc:
                raise Refused(str(exc)) from None
            note = f"{text or 'none'} ({note})"
        return copy_desk.preview(rows[0], settings, role=role, sender=args.sender or "", opener=text, opener_note=note,
                                 subject_arm=args.subject or "")
    domain = args.account.strip().lower()
    account = next(iter(ctx.store.select("accounts", {"domain": domain})), None)
    if account is None:
        raise Refused(f"no account with the domain {domain!r}")
    domains, hashes = enrol.suppressed(ctx)
    contacts = ctx.store.select("contacts", {"account_id": account["account_id"]})
    contact, why = enrol.pick_contact(contacts, domains, hashes, account, settings)
    contact = contact or (sorted(contacts, key=lambda c: str(c.get("created_at") or ""))[:1] or [{}])[0]
    role = args.role or str(contact.get("role") or "") or next(iter(ROLE_LINE_COLUMNS))
    rows = _pick_rows(settings, args.version, args.industry, role) if args.version or args.industry else []
    row = rows[0] if rows else enrol.pick_copy(
        account, role, settings, {c.copy_version: c for c in settings.copy if c.status != "retired"})[0]
    if row is None:
        raise Refused("no Copy row for this account; give --version")
    events = ctx.store.select("signal_events", {"account_id": account["account_id"]})
    op = openers.for_account(ctx, account, {**contact, "role": role}, events, check=copy_desk._opener_check(settings),
                             settings=settings)
    arm = op.arm + (f", would be: {op.would_be}" if op.would_be else "")
    note = f"{op.text or 'none'} ({arm}; {op.source or 'no line'})" + (f"; passed over: {'; '.join(op.notes)}" if op.notes else "")
    if why:
        note += f". The contact is not sendable yet: {why}"
    return copy_desk.preview(row, settings, role=role, sender=args.sender or "", opener=op.text, opener_note=note,
                             account=account, contact=contact, subject_arm=args.subject or "")


def cmd_copy(args: argparse.Namespace, factory: Factory) -> int:
    """The copy desk (enrol/copy_desk.py): check, preview, QA and draft the Copy tab."""
    from us_outbound.enrol import copy_desk

    live = args.live if args.action in ("qa", "draft") else False
    if not live:
        _only_reads(args)
    ctx = factory(f"copy_{args.action}", live, operator=True)
    settings = _sheet_settings(ctx) if not args.synced else ctx.settings
    if args.action in ("qa", "draft"):
        from us_outbound.clients.claude import PRICES

        key = "claude_task_model" if args.action == "qa" else "claude_model"
        model = getattr(settings.general, key)
        if model not in PRICES:
            raise Refused(f"{key} is {model!r}, which the monthly cap cannot price; use one of {', '.join(PRICES)}")
    if args.action == "check":
        checks = copy_desk.check_all(settings, args.version)
        bad = [c for c in checks if not c.ok]
        for c in bad:
            print(f"{c.copy_version}:")
            for p in c.problems:
                print(f"  - {p}")
        by_status: Counter[str] = Counter(c.status for c in settings.copy)
        qa_ok = sum(1 for c in settings.copy if c.qa_current)
        sendable = sum(1 for c in settings.copy if c.status == "approved" and c.qa_current)
        print(f"{len(checks)} rows checked, {len(bad)} with problems. By status: "
              + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
              + f". QA passed in the current wording: {qa_ok}. Sendable (approved and QA passed): {sendable}.")
        lines = copy_desk.check_openers(settings)  # the Signals tab's opener lines and General's (enrol/openers.py)
        for p in lines.problems:
            print(f"Opener: {p}")
        print(f"Opener lines (Signals tab, and General's generic and focus lines): {len(lines.problems)} problems.")
        return 1 if bad or not lines.ok else 0
    if args.action == "preview":
        p = _preview(ctx, settings, args, copy_desk)
        print(p.text())
        if args.html:
            Path(args.html).write_text(p.html(), encoding="utf-8")
            print(f"Wrote {args.html}")
        return 0
    if args.action == "qa":
        rows = _pick_rows(settings, args.version, args.industry, None)
        if args.active:  # the rows enrol can send now: the active industries' and General's (Harry, 6 Oct 2026)
            from us_outbound.settings.model import GENERAL_COPY

            on = {i.industry.casefold() for i in settings.industries if i.active} | {GENERAL_COPY.casefold()}
            rows = [c for c in rows if c.industry.casefold() in on]
        if not args.all:
            rows = [c for c in rows if not c.qa_current]
        if not ctx.live:
            est = sum(ctx.clients.claude_task.estimate_usd(copy_desk.QA_SYSTEM, copy_desk.qa_prompt(r, settings),
                                                           copy_desk.QA_SCHEMA, copy_desk.QA_MAX_TOKENS) for r in rows)
            for row in rows:
                c = copy_desk.check_row(row, settings)
                print(f"{row.copy_version}: " + ("sheet check clean; would go to QA" if c.ok
                                                 else "sheet check fails, so QA fails it without a model call"))
            print(f"Dry-run: {len(rows)} rows, no model called. With --live they go to "
                  f"{settings.general.claude_task_model}, at most ${est:.2f} of the monthly cap.")
            return 0
        results = copy_desk.qa_run(ctx, rows, settings, say=lambda line: print(line, flush=True))
        print(f"{sum(r.verdict == 'pass' for r in results)} of {len(results)} passed.")
        _dry_note(ctx, "the verdicts were not written to the sheet.")
        return 0
    # draft
    if not args.industry:
        raise Refused("give --industry (an Industries label, an industry group, or General)")
    if not ctx.live:
        prompt = copy_desk.draft_prompt(args.industry, args.role or "", settings)
        est = ctx.clients.claude.estimate_usd(copy_desk.DRAFT_SYSTEM, prompt, copy_desk.DRAFT_SCHEMA,
                                              copy_desk.DRAFT_MAX_TOKENS)
        print(f"Dry-run: no model called. With --live, {settings.general.claude_model} drafts {args.industry} "
              f"for at most ${est:.2f} of the monthly cap, and the draft is added to the Copy tab.")
        return 0
    row = copy_desk.draft_row(ctx, args.industry, args.role or "", settings=settings)
    check = copy_desk.check_row(copy_desk.row_from_dict(row), settings)
    print(copy_desk.preview(copy_desk.row_from_dict(row), settings, role=args.role or "").text())
    print("Sheet check: " + ("clean" if check.ok else "; ".join(check.problems)))
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if sheet_id:
        ctx.clients.sheets.append_rows(sheet_id, "Copy", [row])
    _dry_note(ctx, "the draft was not added to the sheet.")
    if ctx.live:
        print(f"Added {row['copy_version']} as a draft. Next: `us-outbound copy qa --version {row['copy_version']} --live`.")
    return 0


def _print_reply_items(items: list[dict]) -> None:
    if not items:
        print("No reply items are waiting.")
        return
    print(f"{len(items)} reply item{'s' if len(items) != 1 else ''} waiting, oldest first:")
    for i in items:
        who = ", ".join(x for x in (i["person"] or "someone", i["title"]) if x)
        role = f" ({i['role']})" if i["role"] else ""
        waited = f"waited {i['waited_hours']} h" if i["waited_hours"] is not None else "waiting"
        print(f"\n{i['id']}  {i['reply_class'] or '?'}  {i['account'] or 'unknown account'} · {who}{role}")
        print(f"  to {i['mailbox'] or '?'} ({i['owner'] or 'owner unknown'}) · {waited} · {i['status']}"
              f"{' · escalated' if i['escalated'] else ''} · in Slack: {'yes' if i['in_slack'] else 'no'}")
        if i["note"]:
            print(f"  NOTE: {i['note']}")
        if i["excerpt"]:
            print(f"  They wrote: \"{i['excerpt']}\"")
        if i["referral"]:
            print("  Referred us to: " + ", ".join(v for v in i["referral"].values() if v))
        print(f"  Draft{' (edited)' if i['draft_edited'] else ''}:" if i["draft"] else "  No draft: approve with --edit \"text\".")
        for line in (i["draft"] or "").splitlines():
            print(f"    {line}")
        if i["hubspot"]:
            print(f"  HubSpot: {i['hubspot']}")
        print(f"  Send it: us-outbound replies approve {i['id']} --live   (--edit \"text\" sends that instead)")
        print(f"  Skip it: us-outbound replies skip {i['id']} --live")


def cmd_replies(args: argparse.Namespace, factory: Factory) -> int:
    """The reply desk at the command line (replies/desk.py): Slack may not be set up for the pilot."""
    from us_outbound.clients.guard import REPLIES_CLI_JOB
    from us_outbound.replies import desk

    if args.action == "list":
        _only_reads(args)
        _print_reply_items(desk.list_items(factory("replies_list", False, operator=True)))
        return 0
    if not args.item_id:
        raise Refused(f"replies {args.action} needs an item id from `us-outbound replies list`")
    if args.action == "skip":
        if args.edit is not None:
            raise Refused("--text (--edit) goes with send, not skip")
        ctx = factory("replies_skip", args.live, operator=True)
        summary = run_job(ctx, lambda c: desk.skip(c, args.item_id))
        _print(summary)
        _dry_note(ctx, "the item was not marked handled.")
        return 0 if summary.get("skipped") or ctx.dry_run else 2
    # send (approve) sends to a prospect: --live and live_sending = yes (SPEC 0.3), not --live alone.
    ctx = factory(REPLIES_CLI_JOB, args.live)
    summary = run_job(ctx, lambda c: desk.approve(c, args.item_id, text=args.edit))
    _print(summary)
    _dry_note(ctx, "the reply was not sent, HubSpot was not written and the item is unchanged.")
    return 0 if summary.get("sent") or ctx.dry_run else 2


def _eval_report(out: dict, live: bool) -> int:
    if not live:
        print(f"{out['rows']} companies; a live eval asks {out['model']} about each, at most ${out['most_usd']:.2f}.")
        return 0
    for r in out["results"]:
        if r.get("error"):
            print(f"  {r['domain']}: {r['error']}")
            continue
        print(f"  {r['score']:<10} {r['domain']} · expected {r['expected']} ({r['expected_action']}) · rules "
              f"{r['rules'] or 'no label'} · model {r['model']} ({r['confidence']}, {r['entity']}) · {r['decision']}")
    print(f"{out.get('acceptable', 0)} acceptable, {out.get('wrong', 0)} wrong, {out.get('unsafe', 0)} unsafe of "
          f"{out['rows']} (${out.get('usd', 0):.2f}; labels_hash {out['labels_hash']}). "
          + ("Passed." if out.get("passed") else "Failed: it needs 90% acceptable and nothing unsafe."))
    return 0 if out.get("passed") else 1


def cmd_labels(args: argparse.Namespace, factory: Factory) -> int:
    """`us-outbound labels set | show | eval` (us_outbound/labels.py, ops/relabel.py; Harry, 7 Oct 2026)."""
    from us_outbound import labels
    from us_outbound.ops import relabel

    if args.action == "eval":
        ctx = factory("labels_eval", args.live, operator=True)
        rows = labels.gold_rows() + (labels.corrected_rows(ctx) if args.from_corrections else [])
        if args.from_sample:
            from us_outbound.ops import label_sample

            rows += labels.judged_rows(ctx, label_sample.sample_rows(ctx))
        out = run_job(ctx, lambda c: labels.evaluate(c, rows))
        code = _eval_report(out, ctx.live)
        _dry_note(ctx, "no model was asked.")
        return code
    if not args.domain:
        raise Refused(f"labels {args.action} needs a company's domain")
    if args.action == "show":
        _only_reads(args)
        try:
            _print(relabel.show(factory("labels_show", False, operator=True), args.domain))
        except LookupError as exc:
            raise Refused(str(exc)) from exc
        return 0
    if not args.label:
        raise Refused('labels set needs the label: `us-outbound labels set acme.com "Fintech" --live`')
    _operate(factory, "labels_set", args.live, lambda c: relabel.set_label(c, args.domain, args.label),
             operator=True, dry_note="the label and the Overrides tab are unchanged.")  # it reaches no prospect
    return 0


def cmd_db(args: argparse.Namespace, factory: Factory) -> int:
    """The DDL in sql/ (ops/ddl.py): printed in dry-run; run against DATABASE_URL with --live."""
    from us_outbound.clients.guard import Guard
    from us_outbound.ops import ddl

    dsn = bootstrap.database_url(os.environ) if args.live else (os.environ.get(bootstrap.DATABASE_VAR) or "").strip()
    statements = ddl.apply(Guard(job="db_apply"), dsn, dry_run=not args.live)
    if args.live:
        print(f"Applied {len(statements)} statements to schema us_outbound (tables and indexes IF NOT EXISTS, views OR REPLACE).")
    else:
        print(";\n\n".join(s.rstrip().rstrip(";") for s in statements) + ";")
        print(f"Dry-run: {len(statements)} statements printed, nothing run. Add --live to run them.", file=sys.stderr)
    return 0


def cmd_campaigns(args: argparse.Namespace, factory: Factory) -> int:
    """`campaigns ensure [--fix] [--in-flight]` (registry/mailboxes.ensure_campaigns)."""
    from us_outbound.registry.mailboxes import ensure_campaigns, held_words

    if args.in_flight and not args.fix:
        raise Refused("--in-flight goes with ensure --fix")
    ctx = factory("campaigns_ensure", args.live, operator=True)
    summary = run_job(ctx, lambda c: ensure_campaigns(c, fix=args.fix, in_flight=args.in_flight))
    _print(summary)
    for name, held in (summary.get("held") or {}).items():
        print(held_words(name, held))
    _dry_note(ctx, "no campaign was created or changed.")
    return 0


def cmd_cohorts(args: argparse.Namespace, factory: Factory) -> int:
    """`cohorts in-flight | changes [A B]` (learn/cohorts.py; Harry, 7 Oct 2026): the leads in flight per campaign, or
    what changed between two config versions. Read-only; the weekly table is `report cohorts`."""
    from us_outbound.learn import cohorts

    ctx = factory("cohorts", False)
    if args.action == "changes":
        if len(args.versions) not in (0, 2):
            raise Refused("cohorts changes takes two config versions, or none for the last two")
        lines = cohorts.changes_lines(ctx, *args.versions)
    else:
        if args.versions:
            raise Refused("config versions go with `cohorts changes`")
        lines = cohorts.in_flight_lines(ctx)
    for line in lines:
        print(line)
    return 0


# -- report: the read-only reports in one command (W2-4) ---------------------------------------------------------------


REPORTS = ("cohorts", "signals", "readout", "lookalikes", "pages", "daily", "capture")


def cmd_report(args: argparse.Namespace, factory: Factory) -> int:
    """`report <name>`: the read-only reports, one command (W2-4, 20 Oct 2026). capture is the one that acts with
    --live: it deletes the canary's own lead from its campaign (replies/capture.check_only), which reaches no
    prospect, so --live alone."""
    name = args.name
    if name == "capture":
        from us_outbound.replies import capture

        _, summary = _operate(factory, capture.CHECK_JOB, args.live, capture.check_only, operator=True,
                              dry_note="the canary's lead was not deleted and its check was not recorded.")
        return 0 if not summary.get("errors") else 2
    _only_reads(args)
    if name == "cohorts":
        from us_outbound.learn import cohorts

        lines = cohorts.lines(factory("cohorts", False), cut=args.cut, age=args.age, weeks=args.weeks)
    elif name == "signals":
        from us_outbound.learn import signal_review

        lines = signal_review.lines(signal_review.review(factory("signals_review", False)))
    elif name == "readout":
        from us_outbound.learn import readout

        lines, _ = readout.build(factory("readout_show", False))
    elif name == "lookalikes":
        from us_outbound.sources import lookalikes

        if args.top < 1:
            raise Refused("--top must be 1 or more")
        ctx = factory("lookalikes_show", False)
        lines = lookalikes.report(ctx.settings, ctx.store, top=args.top, all_bands=args.all)
    elif name == "pages":
        from us_outbound.sources import pages

        lines = pages.report(factory("pages_show", False))
    else:  # daily
        from us_outbound.learn import daily_post

        ctx = factory("daily_preview", False)
        day = daily_post.gather(ctx)
        lines = day.lines if args.full else daily_post.compact(ctx, day)
    for line in lines:
        print(line)
    return 0


# -- the companies and contacts we hold (Harry, 5 Oct 2026) -----------------------------------------


def cmd_accounts(args: argparse.Namespace, factory: Factory) -> int:
    """The companies and contacts we hold (ops/accounts_view.py): the database only, no heartbeat, no --live.
    Exits 1 for a domain we hold no company for."""
    from us_outbound.ops import accounts_view as view

    try:
        f = view.Filters.parse(args.status, args.tier, args.industry)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    if args.limit is not None and args.limit < 0:
        raise Refused("--limit takes 0 (all) or more")
    if args.domain and (f or args.limit is not None):
        raise Refused("a domain shows one company; --status, --tier, --industry and --limit go with the list")
    if args.csv:
        # Every database read logs a JSON line to stdout (logs.log reads sys.stdout at each call): here they go to
        # stderr, so stdout holds the CSV only (`railway ssh -- ... > companies.csv` runs without a terminal).
        out = sys.stdout
        with redirect_stdout(sys.stderr):
            rows = view.csv_rows(factory("accounts", False), f, limit=args.limit, domain=args.domain)
        if rows is None:
            print(f"No company with domain {args.domain}.", file=sys.stderr)
            return 1
        view.write_csv(out, rows)
        print(view.csv_note(rows), file=sys.stderr)
        return 0
    ctx = factory("accounts", False)
    if args.domain:
        found, lines = view.company(ctx, args.domain)
    else:
        found, lines = True, view.report(ctx, f, view.LIST_LIMIT if args.limit is None else args.limit)
    for line in lines:
        print(line)
    return 0 if found else 1


def cmd_handcheck(args: argparse.Namespace, factory: Factory) -> int:
    """This week's hand-check without Slack (enrol/hand_check.py): show it, or approve it with pulls."""
    from us_outbound.enrol import hand_check

    if args.action == "show":
        ctx = factory("handcheck_show", args.live, operator=True)
        holder: dict[str, Any] = {}

        def show(c: Context) -> dict:
            item, payload = hand_check.show(c)
            holder["payload"] = payload
            return {"dry_run": c.dry_run, "iso_week": payload.get("iso_week"), "item_id": (item or {}).get("item_id"),
                    "status": (item or {}).get("status") or "not recorded", "accounts": len(payload.get("accounts") or ()),
                    "doubtful": len(payload.get("doubtful") or ())}

        summary = run_job(ctx, show)
        payload = holder.get("payload") or {}
        if not hand_check.has_work(payload):
            print(f"Hand-check {summary['iso_week']}: nothing to check (no company is ready to email beyond today's, "
                  "and none is held for doubtful facts).")
            return 0
        for line in hand_check.lines(payload):
            print(line)
        if summary["status"] == "not recorded":
            print("Not recorded yet: `us-outbound handcheck approve --live` records this sample and approves it.")
        else:
            print(f"Item {summary['item_id']}: {summary['status']}.")
        return 0
    if args.pull and args.action != "approve":
        raise Refused("--pull goes with approve")
    if args.action == "migrate-tab":  # W1-5, 19 Oct 2026: the tab's header moved to the new columns, once
        from us_outbound.enrol import hand_check_tab

        ctx, summary = _operate(factory, "handcheck_migrate_tab", args.live, hand_check_tab.migrate, operator=True,
                                dry_note="the Hand-check tab was not changed.")
        print(f"Hand-check tab, {summary['rows']} rows. Header now: {', '.join(summary['before']) or '(none)'}")
        if summary.get("note"):  # the tab is empty: nothing moved, nothing backed up (the W1-5 review, round 2)
            print(summary["note"])
        elif summary["already"]:
            print("It is under the new columns already: nothing to move.")
        else:
            print(f"Header after: {', '.join(summary['after'])}")
            print(("Moved, after a backup to the tab " if summary["written"] else "Would move it, after a backup to "
                   "the tab ") + repr(summary["backup"]) + ".")
        if summary.get("refused"):  # a dry run while the week is open: the live run refuses (exit 2)
            print(f"A live run would refuse now: {summary['refused']}.")
        if summary.get("dropdowns") is False:
            print(f"The dropdowns could not be set ({summary.get('dropdowns_error')}): set decision, reason, "
                  "right_label, contact_ok and opener_ok by hand on the sheet.")
        return 0
    if args.action == "export":  # W0, 19 Oct 2026: every decided row into the database, kept whatever the tab does
        from us_outbound.enrol import hand_check_tab

        ctx, summary = _operate(factory, "handcheck_export", args.live, hand_check_tab.export, operator=True,
                                dry_note="no judgement was written to the database.")
        print(("Wrote " if summary["written"] else "Would write ")
              + f"{summary['facts']} hand-check judgements ({summary['new']} new) from {summary['decided']} decided "
              f"rows of {summary['rows']}" + (f"; {len(summary['skipped'])} skipped" if summary["skipped"] else "")
              + ".")
        return 0
    ctx, summary = _operate(
        factory, "handcheck_approve", args.live, lambda c: hand_check.approve(c, args.pull or [], campaigns.operator()),
        operator=True, dry_note="the hand-check was not marked approved.",
    )
    if ctx.live:
        print(f"Approved {summary['iso_week']}; enrollment can go ahead"
              + (f", leaving out {len(summary['pulled_account_ids'])} pulled accounts." if summary["pulled_account_ids"] else "."))
    return 0


def cmd_seed(args: argparse.Namespace, factory: Factory) -> int:
    """The seed-inbox test of the opt-out (ops/seed.py): add a seed lead to a campaign, or read seed leads back."""
    from us_outbound.ops import seed

    if args.action == "check":
        _only_reads(args)
        rows = seed.check(factory("seed_check", False, operator=True))
        if not rows:
            print("No seed lead in any US Outbound campaign yet: `us-outbound seed send ADDRESS --owner NAME --live`.")
            return 1
        for r in rows:
            sent = f", last emailed {fmt.uk_time(r['last_contact'])}" if r["last_contact"] else ", not emailed yet"
            print(f"{r['address']} in {r['campaign']} ({r['campaign_status']}): {r['status_text']}{sent}")
        if any(r["unsubscribed"] for r in rows):
            print("PASS: Instantly shows the seed lead as unsubscribed (status -2), which is what sync_outcomes reads. "
                  "Set optout_tested = yes on the General tab, then run `us-outbound sync`.")
            return 0
        print("Not yet: open the seed email, click \"Unsubscribe here\" at the bottom, then run `us-outbound seed check` "
              "again (Instantly can take a minute or two).")
        return 1
    if not args.address or not args.owner:
        raise Refused("seed send needs the seed inbox's address and --owner (whose campaign sends it)")
    ctx, summary = _operate(
        factory, seed.JOB, args.live,
        lambda c: seed.send(c, args.address, args.owner, industry=args.industry or "", role=args.role or "",
                            subject=args.subject),
        operator=True, show=False,
    )
    print(f"Seed email for {summary['address']}, from {summary['sender']} ({summary['campaign']}, "
          f"{summary['campaign_status']}), copy {summary['copy_version']} ({summary['copy_status']}, {summary['role']}):")
    print(f"  Subject: {summary['subject']} ({seed.SUBJECT_NOTES[summary['subject_arm']]})")
    for line in summary["email_1"].splitlines():
        print(f"  {line}" if line else "")
    if ctx.dry_run:
        print("Dry-run: no lead was added. Add --live to add it.")
        return 0
    if not summary["added"]:
        print(f"Not added: {summary.get('why')}.")
        return 2
    window = seed.send_window(ctx.settings)
    if summary["campaign_status"] == "active":
        print(f"Added. Instantly sends it in the campaign's window: {window}.")
    else:
        print(f"Added. The campaign is {summary['campaign_status']}, so Instantly sends nothing yet. "
              f"`us-outbound start --live` activates it (it needs live_sending = yes; while optout_tested is no, "
              f"no prospect is added). Then Instantly sends it in the campaign's window: {window}.")
    print("When it arrives: check it reads as formatted text with working links, click \"Unsubscribe here\", then "
          "run `us-outbound seed check`.")
    return 0


def cmd_killrules(args: argparse.Namespace, factory: Factory) -> int:
    """The kill-rule holds in force (learn/kill_rules.py), and lifting one once Harry has checked it."""
    from us_outbound.learn import kill_rules

    if args.action == "show":
        _only_reads(args)
        ctx = factory("killrules_show", False)
        rows = kill_rules.show(ctx)
        if not rows:
            print("No kill-rule hold is in force.")
        for r in rows:
            until = f" until {r['until']}" if r.get("until") else ""
            print(f"{r['item_id']}  {r['rule']}: {r['action']} {r['target']}{until} ({fmt.uk_time(r.get('created_at'))}"
                  f"{', dry-run' if r.get('dry_run') else ''})")
            print(f"    {r['reason']}")
        return 0
    if not args.item:
        raise Refused("killrules clear needs the item id (`us-outbound killrules show` lists them)")
    ctx = factory("killrules_clear", args.live, operator=True)
    try:
        summary = run_job(ctx, lambda c: kill_rules.clear(c, args.item, campaigns.operator()))
    except LookupError as exc:
        raise Refused(str(exc)) from exc
    _print(summary)
    _dry_note(ctx, "the hold is still in force.")
    if summary.get("next"):
        print(summary["next"])
    return 0


def cmd_schedule(args: argparse.Namespace, factory: Factory) -> int:
    """The job table with each enabled job's next run (UK time)."""
    from us_outbound.ops import scheduler

    for line in scheduler.describe():
        print(line)
    print(f"At most {scheduler.max_parallel_from_env()} jobs run at once ({scheduler.MAX_PARALLEL_VAR}). "
          "A --live job is still dry until live_sending = yes in the synced settings (sheet edits apply at the "
          "next settings_sync, or now with `us-outbound sync`).")
    return 0


def cmd_scheduler(args: argparse.Namespace, factory: Factory) -> int:
    """The always-on worker (ops/scheduler.py): starts every job on its schedule until SIGTERM."""
    from us_outbound.ops import scheduler

    if args.list:
        return cmd_schedule(args, factory)
    return scheduler.Scheduler(max_parallel=scheduler.max_parallel_from_env(), at_start=scheduler.AT_START,
                               store_factory=scheduler.heartbeat_store, lock=scheduler.scheduler_lock(),
                               fail=scheduler.watchdog_fail).run()


# -- parser and entrypoint ----------------------------------------------------------------


HELP = ("Spill's US Outbound engine. Dry-run by default. `--live` makes a command act. Anything that reaches a "
        "prospect (start, replies send, every scheduled job) also needs live_sending = yes in the "
        "synced settings.")


@dataclass(frozen=True)
class Command:
    """One row of the command table: its name, its line in --help, what runs it, whether it takes --live, and its own
    arguments (args adds them to its parser)."""

    name: str
    about: str
    fn: Callable[[argparse.Namespace, Factory], int]
    live: bool = False
    args: Callable[[argparse.ArgumentParser], None] | None = None


def _accounts_args(ac: argparse.ArgumentParser) -> None:
    # The choices are written out here, so --help does not import the view (ops/accounts_view.py STATUSES, TIERS).
    ac.add_argument("domain", nargs="?", metavar="DOMAIN",
                    help="one company in full: its facts, its contacts with their emails, signals, events and Slack "
                         "cards (a domain or a web address)")
    ac.add_argument("--status", action="append", metavar="STATUS",
                    help="only companies with this status (repeat it, or a comma list): new, queued, verified, "
                         "enrolled, engaged, demo_requested, demo_booked, disqualified or parked")
    ac.add_argument("--tier", action="append", metavar="TIER",
                    help="only this tier (repeat it, or a comma list): Priority, Standard, Control, Held or Excluded")
    ac.add_argument("--industry", metavar="TEXT",
                    help="only companies whose industry or industry group contains this (any case)")
    ac.add_argument("--limit", type=int, metavar="N",
                    help="how many companies the list shows (default 25; 0 lists them all; the CSV holds every "
                         "match unless --limit is given)")
    ac.add_argument("--csv", action="store_true",
                    help="write a CSV to stdout instead: one row per contact with its company's fields (a company "
                         "with no contact gets one row), the same filters, times in UK time. It holds personal data "
                         "(names and emails): save it with `railway ssh -- us-outbound accounts --csv > "
                         "companies.csv`, keep it private and delete it when you are done")


def _stop_args(so: argparse.ArgumentParser) -> None:
    so.add_argument("--no-db", action="store_true",
                    help="the database is down: pause the campaigns through Instantly alone (no settings, no "
                         "operator_stop row; run stop --live again once it is back)")


def _replies_args(rp: argparse.ArgumentParser) -> None:
    rp.add_argument("action", choices=["list", "send", "skip", "approve"], help="send = ✅ (approve)")
    rp.add_argument("item_id", nargs="?", help="send, skip: the id `replies list` shows (or its first characters)")
    rp.add_argument("--text", "--edit", dest="edit", help="send: send this text instead of the draft (recorded as edited)")


def _handcheck_args(hc: argparse.ArgumentParser) -> None:
    hc.add_argument("action", choices=["show", "approve", "export", "migrate-tab"])
    hc.add_argument("--pull", nargs="+", action="extend", metavar="ACCOUNT_ID",
                    help="approve: accounts to leave out (ids or domains)")


def _killrules_args(kr: argparse.ArgumentParser) -> None:
    kr.add_argument("action", choices=["show", "clear"])
    kr.add_argument("item", nargs="?", help="clear: the item id")


def _mailbox_args(mb: argparse.ArgumentParser) -> None:
    mb.add_argument("action", choices=["add", "pause", "retire", "check"])
    mb.add_argument("address", nargs="?")
    mb.add_argument("--owner", help="the real person named on the address (add)")
    mb.add_argument("--domain", help="the address's domain (add; defaults to it)")
    mb.add_argument("--daily-cap", type=int, default=30, help="sends per day (add; at most 30)")
    mb.add_argument("--fix", action="store_true",
                    help="check: also set each sender name to its owner's full name, create missing campaigns and put "
                         "drifted ones right (campaigns ensure --fix)")


def _campaigns_args(cp: argparse.ArgumentParser) -> None:
    cp.add_argument("action", choices=["ensure"])
    cp.add_argument("--fix", action="store_true", help="put drifted settings and sending lists right")
    cp.add_argument("--in-flight", action="store_true",
                    help="ensure --fix: also apply drift in the steps, delays or text_only to campaigns with leads in "
                         "flight (held otherwise; it reaches every lead's remaining emails, and is logged)")


def _cohorts_args(ch: argparse.ArgumentParser) -> None:
    ch.add_argument("action", choices=["changes", "in-flight"],
                    help="changes: what differs between two config versions (default: the last two); in-flight: each "
                         "campaign's leads with a step still to send")
    ch.add_argument("versions", nargs="*", metavar="VERSION", help="changes: two config versions, older first")


def _test_args(ts: argparse.ArgumentParser) -> None:
    ts.add_argument("action", choices=["start", "read"],
                    help="read refuses before the test's first look (its looks, then read_date), so nobody peeks")
    ts.add_argument("test_id")


def _labels_args(lb: argparse.ArgumentParser) -> None:
    lb.add_argument("action", choices=["set", "show", "eval"])
    lb.add_argument("domain", nargs="?", help="set, show: the company's domain")
    lb.add_argument("label", nargs="?", help="set: the Industries label, like \"Fintech\"")
    lb.add_argument("--from-corrections", action="store_true",
                    help="eval: also score the companies corrected by hand, expecting their new label")
    lb.add_argument("--from-sample", action="store_true",
                    help="eval: also score the companies judged on the Label sample tab, expecting their right label")


def _copy_args(co: argparse.ArgumentParser) -> None:
    co.add_argument("action", choices=["check", "preview", "qa", "draft"])
    co.add_argument("--version", action="append", help="a copy_version (repeatable)")
    co.add_argument("--industry", help="an Industries label, an industry group, or General")
    co.add_argument("--role", help="a Roles-tab role (preview: whose line to show; draft: a role-only row)")
    co.add_argument("--sender", help="preview: the mailbox owner who sends it")
    co.add_argument("--opener", action="store_true",
                    help="preview: with a real Signals-tab opener line, filled with sample facts")
    co.add_argument("--signal", help="preview --opener: the Signals row whose line to show (default: the first with one)")
    co.add_argument("--leader", action="store_true",
                    help="preview --opener: the line for a contact who is the new People leader (opener_self)")
    co.add_argument("--generic", action="store_true",
                    help="preview --opener: the generic line, for an account with no signal line (General "
                         "opener_generic_*)")
    co.add_argument("--account", metavar="DOMAIN",
                    help="preview: a stored account, its contact and the opener enrol would give it")
    co.add_argument("--subject", choices=["personal", "copy"],
                    help="preview: email 1's subject: personal (General email1_subject) or copy (the Copy row's "
                         "s1_subject); default: a stored account's own arm, else copy")
    co.add_argument("--html", help="preview: also write the four emails as an HTML page to this path")
    co.add_argument("--all", action="store_true", help="qa: check rows that already passed too")
    co.add_argument("--active", action="store_true",
                    help="qa: only the rows of industries active on the Industries tab, and General's")
    co.add_argument("--synced", action="store_true", help="use the synced settings, not the sheet as it is now")


def _seed_args(sd: argparse.ArgumentParser) -> None:
    sd.add_argument("action", choices=["send", "check"])
    sd.add_argument("address", nargs="?", help="send: the seed inbox (one of ours, never a prospect's)")
    sd.add_argument("--owner", help="send: whose campaign sends it, e.g. \"Hannah Spalding\"")
    sd.add_argument("--industry", help="send: whose Copy row to use (default: the first active industry)")
    sd.add_argument("--role", help="send: the copy role (default: People leader)")
    sd.add_argument("--subject", choices=["personal", "copy"], default="copy",
                    help="send: email 1's subject: personal (General email1_subject) or copy (the Copy row's "
                         "s1_subject, the default)")


def _settings_args(st: argparse.ArgumentParser) -> None:
    st.add_argument("action", choices=["load"])
    st.add_argument("--tab", action="append",
                    choices=["General", "Industries", "Copy", "Roles", "Signals", "Focus", "Tests"],
                    help="the tab (default General, Industries, Copy and Roles)")
    st.add_argument("--take", action="append", metavar="COLUMN",
                    help="let the build's value win for a column Harry owns (Industries active, priority; any "
                         "Signals column, like weight; General note)")
    st.add_argument("--keep", action="append", metavar="COLUMN",
                    help="keep the sheet's value in this column for this load, where the build's would win (an edit "
                         "made on the sheet, like a page_faqs line)")
    st.add_argument("--set", action="append", metavar="KEY=VALUE", help="a General value Harry has decided")
    st.add_argument("--replace-drafts", action="store_true",
                    help="replace the Copy rows Harry has not approved with the build's (approved rows stay)")


def _report_args(rp: argparse.ArgumentParser) -> None:
    # The choices are written out here, so --help does not import the reports (learn/cohorts.py CUTS, AGES).
    rp.add_argument("name", choices=list(REPORTS),
                    help="cohorts: each enrolment week at 7, 14, 21 and 28 days; signals: which signals predict "
                         "replies; readout: the Monday readout for last week; lookalikes: the top lookalike cells; "
                         "pages: the careers and benefits page reader's coverage; daily: the daily post as it would "
                         "read now; capture: the reply read's canary checked by hand")
    rp.add_argument("--cut", default="all",
                    choices=["all", "tier", "angle", "industry_group", "sender", "copy_version", "subject_arm",
                             "opener_arm", "config_version"], help="cohorts: split each week by this (default all)")
    rp.add_argument("--age", type=int, choices=[7, 14, 21, 28], help="cohorts: only this age, in days after email 1")
    rp.add_argument("--weeks", type=int, default=8, help="cohorts: how many enrolment weeks, the latest first")
    rp.add_argument("--top", type=int, default=20, help="lookalikes: how many cells to list (default 20)")
    rp.add_argument("--all", action="store_true", help="lookalikes: every size band, not only 10 to 249 staff")
    rp.add_argument("--full", action="store_true", help="daily: every section: sources, labels, mailboxes, credits")


def _erase_args(er: argparse.ArgumentParser) -> None:
    er.add_argument("--email", required=True)


def _unenrol_args(un: argparse.ArgumentParser) -> None:
    un.add_argument("--month", required=True, help="YYYY-MM")


def _verify_args(vf: argparse.ArgumentParser) -> None:
    vf.add_argument("action", choices=["migrate-parked"])


def _db_args(db: argparse.ArgumentParser) -> None:
    db.add_argument("action", choices=["apply"])


def _run_args(rn: argparse.ArgumentParser) -> None:
    rn.add_argument("job")
    rn.add_argument("--due", metavar="YYYY-MM-DDTHH:MM",
                    help="the minute (UTC) the scheduler started it for: the run claims it, so a second start for the "
                         "same minute runs nothing")


def _scheduler_args(sc: argparse.ArgumentParser) -> None:
    sc.add_argument("--list", action="store_true", help="print the job table instead (same as `schedule`)")


# The command table (W2-4, 20 Oct 2026; RS P0-10): every command, in --help's order. Every alert and doc that names a
# command names one of these (tests/test_cli.py greps them).
COMMANDS: tuple[Command, ...] = (
    Command("status", "live_sending, what waits for you, jobs, mailboxes, campaigns and the go-live checks", cmd_status),
    Command("accounts", "the companies and contacts we hold: a summary, a list, one company, or a CSV (read-only)",
            cmd_accounts, args=_accounts_args),
    Command("sync", "bring the sheet's edits into force now", cmd_sync, live=True),
    Command("start", "sync the sheet, then resume the campaigns and enrolment", cmd_start, live=True),
    Command("stop", "pause every US Outbound campaign and enrolment (the brake)", cmd_stop, live=True, args=_stop_args),
    Command("replies", "the replies waiting, without Slack: list, send the draft (or your text), or skip", cmd_replies,
            live=True, args=_replies_args),
    Command("handcheck", "this week's hand-check: show it, or approve it; export every decided row of the Hand-check "
            "tab to the database; or move the tab to its new columns (migrate-tab)", cmd_handcheck, live=True,
            args=_handcheck_args),
    Command("killrules", "the kill-rule holds in force, or lift one", cmd_killrules, live=True, args=_killrules_args),
    Command("mailbox", "add, pause, retire or check a mailbox", cmd_mailbox, live=True, args=_mailbox_args),
    Command("campaigns", "create the campaigns (paused) and put drift right (ensure)", cmd_campaigns, live=True,
            args=_campaigns_args),
    Command("cohorts", "the leads in flight per campaign (in-flight), or what changed between two config versions "
            "(changes); read-only", cmd_cohorts, args=_cohorts_args),
    Command("test", "start a test on the Tests tab, or read it at a pre-registered look", cmd_test, live=True,
            args=_test_args),
    Command("labels", "a company's industry label: set it or show it; or score the label check (eval)", cmd_labels,
            live=True, args=_labels_args),
    Command("copy", "check, preview, QA (task model) or draft (writing model) copy", cmd_copy, live=True,
            args=_copy_args),
    Command("seed", "the seed-inbox test of the unsubscribe link: send a seed email, or check it", cmd_seed,
            live=True, args=_seed_args),
    Command("settings", "load the build's tabs or notes into the sheet", cmd_settings, live=True, args=_settings_args),
    Command("report", "a read-only report: cohorts, signals, readout, lookalikes, pages, daily; or the reply read's "
            "canary checked by hand (capture)", cmd_report, live=True, args=_report_args),
    Command("erase", "an erasure request: remove a person everywhere we hold them", cmd_erase, live=True,
            args=_erase_args),
    Command("unenrol", "take a month's leads out of their campaigns, recorded as stopped", cmd_unenrol, live=True,
            args=_unenrol_args),
    Command("verify", "one-off: park the companies left out while their industry was switched off (W1-4)", cmd_verify,
            live=True, args=_verify_args),
    Command("db", "create the tables and views in DATABASE_URL (prints them unless --live)", cmd_db, live=True,
            args=_db_args),
    Command("schedule", "the job table and each job's next run (UK time)", cmd_schedule),
    Command("run", "run one job now (what the scheduler starts)", cmd_run, live=True, args=_run_args),
    Command("scheduler", "the always-on worker: start every job on its schedule", cmd_scheduler, args=_scheduler_args),
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="us-outbound", description=HELP)
    sub = p.add_subparsers(dest="command", required=True)
    live = argparse.ArgumentParser(add_help=False)
    live.add_argument("--live", action="store_true",
                      help="act for real (anything that reaches a prospect also needs live_sending = yes)")
    for c in COMMANDS:
        parser = sub.add_parser(c.name, help=c.about, description=c.about, parents=[live] if c.live else [])
        parser.set_defaults(fn=c.fn)
        if c.args is not None:
            c.args(parser)
    return p


def main(argv: list[str] | None = None, *, context_factory: Factory | None = None) -> int:
    args = build_parser().parse_args(argv)
    factory = context_factory or bootstrap.build_context
    try:
        return args.fn(args, factory)
    except (Refused, bootstrap.ConfigError, bootstrap.SettingsUnusable) as exc:
        print(f"us-outbound {args.command}: {exc}", file=sys.stderr)
        return 2
    except GuardViolation as exc:
        print(f"us-outbound {args.command}: blocked by a guardrail (SPEC 1): {redact(str(exc))}", file=sys.stderr)
        return 3
    except Terminated as exc:
        print(f"us-outbound {args.command}: {exc}", file=sys.stderr)
        return 128 + signal.SIGTERM
    except Exception as exc:
        from us_outbound.registry.mailboxes import MailboxError

        bug = isinstance(exc, (KeyError, IndexError, UnicodeError, json.JSONDecodeError))
        if not bug and isinstance(exc, (MailboxError, ValueError, LookupError)):
            print(f"us-outbound {args.command}: {redact(str(exc))}", file=sys.stderr)
            return 2
        print(redact(traceback.format_exc()), file=sys.stderr)
        return 1
