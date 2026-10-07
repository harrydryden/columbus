"""The us-outbound command line (SPEC 13 Operations): the same entrypoints as the jobs.

What Harry uses (`us-outbound --help` lists these, in this order):
  status                              the two switches and what they mean, when the settings were
                                      synced, what waits for him (send approvals, replies, kill-rule
                                      holds), the jobs that failed or missed their heartbeat, mailboxes,
                                      this week's number and the campaigns
  golive                              the read-only go/no-go check (ops/golive.py); exits 1 on a FAIL
  accounts [DOMAIN] [--status S]      the companies and contacts we hold, read-only (ops/accounts_view.py):
    [--tier T] [--industry TEXT]      a summary and the list in queue order, one company in full (exits 1
    [--limit N] [--csv]               for a domain we do not hold), or a CSV to stdout (personal data)
  sync                                settings_sync now: the sheet's edits into force (jobs read the
                                      synced copy; settings_sync runs at 02:00, and 11:30 on weekdays).
                                      The same as `settings sync`
  start | stop                        resume or pause every US Outbound campaign, and enrollment. start
                                      --live syncs the sheet first, so live_sending just set counts.
                                      stop --live is the brake: live_sending = no does not stop Instantly.
                                      Over a blackout start leaves the campaigns paused, and the blackout
                                      job starts them after it (registry/blackout.py; 7 Oct 2026)
  approvals list | send | contact |   send approvals without Slack (enrol/approvals.py; Harry, 2 Oct 2026:
    company [ID]                      while auto_send = no every email waits for approval), in the Slack
                                      words: send = ✅ (its lead goes to Instantly), contact = 👤 not this
                                      person, company = 🚫 not this company. approve, reject --contact and
                                      reject --company are the same; approved_by "cli"
  approvals industry ID LABEL         the card's company is LABEL ("industry: LABEL" in its thread): set,
                                      kept on the Overrides tab, and the card withdrawn for a new one
  labels audit [--limit N] | eval     the industry label check (us_outbound/labels.py; Harry, 7 Oct 2026):
    [--from-corrections] | set        audit: the model asked about every open company with no fresh verdict
    DOMAIN LABEL | show DOMAIN        now (verify_accounts asks about 150 a run), cards that no longer fit
                                      withdrawn; dry-run says how many, the cost and a sample prompt. eval:
                                      the model scored on the gold set (and, with --from-corrections, the
                                      approvers' corrections); exits 1 below 90% acceptable or on any unsafe
                                      answer. set: a company's label (an approver's correction, for a company
                                      with no card). show: its label and check history. audit and eval call
                                      the model only with --live (about $0.01 a company)
  replies list | send | skip [ITEM]   the reply desk without Slack (replies/desk.py): send = ✅ (--text
    [--text "..."]                    "..." sends that instead; --edit is the same), or skip; the same
                                      send, HubSpot and close path as Slack. approve is the same as send
  signals review | value              which signals predict replies (learn/signal_review.py), or the
                                      signal-value table from v_signal_value, with meetings and the rates
                                      against the companies without each signal (learn/signal_value.py)
  readout                             the Monday readout for last week (learn/readout.py), printed only
  cohorts [--cut CUT] [--age N]       each enrolment week's companies at 7, 14, 21 and 28 days after email 1,
    [--weeks N] | changes [A B] |     with what changed between them (learn/cohorts.py; Harry, 7 Oct 2026);
    in-flight                         changes: what differs between two config versions (default the last
                                      two); in-flight: each campaign's leads with a step still to send
  test start|read <test_id>           start a test on the Tests tab (SPEC 12), or read it at its latest
                                      pre-registered look; read refuses before the first look, so nobody
                                      peeks (learn/looks.py; Harry, 6 Oct 2026)
  killrules show|clear <item>         the kill-rule holds in force, and lifting one (learn/kill_rules.py)
  mailbox add|pause|retire <address>  the registry commands of SPEC 9 (add takes --owner); mailbox check
  mailbox check [--fix]               is mailbox_health by hand, and --fix also sets each sender name to
                                      its owner's full name and runs campaigns ensure --fix
  campaigns show | ensure [--fix]     each owner's campaign as Instantly holds it (read-only); or create
    [--in-flight]                     the missing ones (paused) and put drift right. Drift in the steps,
                                      delays or text_only is held while leads are in flight, unless
                                      --in-flight (registry/mailboxes.IN_FLIGHT_KEYS; Harry, 7 Oct 2026)
  copy check|preview|qa|draft         the copy desk (enrol/copy_desk.py): check every Copy row,
                                      preview one, QA it (task model), draft one (writing model)
  settings sync|load|bootstrap        sync; load the build's tabs (or, with --take note, the General
                                      notes) into the sheet; create the sheet
  handcheck show|approve [--pull ID]  this week's hand-check without Slack (enrol/hand_check.py)
  clay check-email --first NAME       one Work Email lookup through Clay's API for a Spill colleague's own
    --last NAME --domain DOMAIN       name (never a prospect), to confirm the email fallback before
    [--live]                          clay_email_fallback goes on (ops/clay_check.py); without --live it
                                      only says what it would send. With --live it exits 1 unless the
                                      fallback can go on
  erase --email <address>             an erasure request (SPEC 6)
  schedule                            the job table and next runs (UK time)
  run <job> [--live]                  one job, what the scheduler starts
Build and duplicate commands, left out of --help but unchanged (HIDDEN): unenrol --month YYYY-MM
(remove that month's leads), rescore (the score job), dry-run <job>, db apply, hubspot setup|ids,
suppression load, lookalikes show [--top N] [--all]
(the cells the lookalikes job last stored), lookalikes fit (the accounts' lookalike fits and the tier mix the
build's lookalike rows would give, scored in memory), pages show (the careers and benefits page reader's
coverage), data show (what the sources have stored, in aggregate), and scheduler (the always-on
Railway worker, ops/scheduler.py). On Railway, run a command inside the worker with
`railway ssh -- us-outbound <command>` (docs/railway-setup.md).

Dry-run is the default everywhere. Two kinds of live:
  * jobs (run, rescore, sync, settings sync, suppression load) and start: --live AND
    live_sending = yes in the synced settings, as SPEC 0.3 says (a dry-run sync still brings the
    sheet in: only its Slack message goes to the dev channel);
  * operator commands whose writes never reach a prospect (stop, mailbox, unenrol, erase,
    test start, settings bootstrap|load, copy qa|draft, hubspot setup, campaigns ensure,
    handcheck show|approve, killrules clear, replies skip, approvals contact|company|reject,
    clay check-email): --live alone, so the phase-0 setup and the kill switch work while
    live_sending is still no.
    `replies send` sends to a prospect, and `approvals send` adds one to Instantly, so they are live
    like a job: --live AND live_sending = yes.
    copy qa and copy draft call Claude only with --live, so a dry run spends nothing; so does
    clay check-email with Clay.
  A read-only action given --live (approvals list, replies list, killrules show, campaigns show,
  copy check|preview, hubspot ids, test read) says that --live does nothing there.
  A --live run that stays dry says when the settings in force were synced, and how to bring a
  sheet edit in (`us-outbound sync`).
Every run writes a heartbeats row (ops/heartbeat.run_job). stop and start write theirs
under operator_stop / operator_start, which is the enrollment pause enrol checks (enrol.operator_pause).

Exit codes: 0 done; 1 unexpected error (with its traceback, including a KeyError, IndexError
or JSON/Unicode decoding error, which are bugs rather than bad input), or a golive check that
FAILs; 2 refused (not built,
bad input, unusable settings); 3 blocked by a guardrail (GuardViolation); 143 stopped by
SIGTERM while a job ran (the scheduler's timeout, or a redeploy; the heartbeat says error).
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from us_outbound.clients.db import new_id
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import UK, Context
from us_outbound.logs import log, redact
from us_outbound.ops import bootstrap
from us_outbound.ops.heartbeat import (
    OPERATOR_START,
    OPERATOR_STOP,
    enrolment_paused,
    latest_runs,
    overdue_minutes,
    run_job,
    scheduled_jobs,
)

# Every SPEC 9 job: "module:function", or why it cannot run yet.
JOBS: dict[str, str] = {
    "settings_sync": "us_outbound.settings.sync:run",
    "source_universe": "us_outbound.sources.apollo_universe:run",
    "apollo_signals": "us_outbound.sources.apollo_jobs:run",
    "read_pages": "us_outbound.sources.pages:run",  # Harry, 2 Oct 2026: careers and benefits pages without Clay
    "apollo_enrich": "us_outbound.sources.apollo_enrich:run",  # Harry, 2 Oct 2026: funding and headcount from enrich
    "apollo_people": "us_outbound.sources.apollo_people:run",  # Harry, 5 Oct 2026: People leaders at every account
    "site_visits": "us_outbound.sources.site_visits:run",  # Harry, 5 Oct 2026: Apollo's visitors to the US site
    "public_signals": "not built yet (phase 1)",
    "verify_in_clay": "not built yet (phase 1)",
    # Build addition: verified on Apollo data and HubSpot while clay_verification = skip (Harry, 1 Oct 2026).
    "verify_accounts": "us_outbound.verify:run",
    "score": "us_outbound.scoring.score:rescore",
    "pick_contacts": "us_outbound.contacts.pick:run",
    "enrol": "us_outbound.enrol.enrol:run",
    "poll_replies": "us_outbound.replies.poll:run",
    "poll_approvals": "us_outbound.replies.desk:poll_approvals",
    "hubspot_readback": "us_outbound.crm.readback:hubspot_readback",
    "sync_outcomes": "us_outbound.replies.outcomes:run",
    "mailbox_health": "us_outbound.registry.mailboxes:mailbox_health",
    "kill_rules": "us_outbound.learn.kill_rules:run",
    "blackout": "us_outbound.registry.blackout:run",  # Harry, 7 Oct 2026: campaigns paused over the blackout dates
    "daily_post": "us_outbound.learn.daily_post:run",
    "monday_readout": "us_outbound.learn.readout:run",  # Harry, 6 Oct 2026: the learning loop
    # Build additions (README "Deviations").
    "heartbeat_check": "us_outbound.ops.heartbeat:run",  # missed jobs, caught errors, the outside watchdog
    "suppression_load": "us_outbound.suppression:load_from_hubspot",
    "lookalikes": "us_outbound.sources.lookalikes:run",  # Harry, 1 Oct 2026: Spill's HubSpot customers as lookalikes
    "lookalike_leads": "us_outbound.sources.lookalike_leads:run",  # Harry, 5 Oct 2026: Apollo's lookalikes, monthly
    "hand_check_post": "us_outbound.enrol.hand_check:post",  # SPEC 11 weekly hand-check, Mondays
}
MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")

Factory = Callable[..., Context]


class Refused(Exception):
    """The command cannot go ahead as asked (exit code 2)."""


class Terminated(BaseException):
    """SIGTERM reached a running job: the scheduler's timeout, or a redeploy (exit code 143).

    A BaseException, so no `except Exception` inside a job swallows it. run_job records the
    run as an error, so its heartbeat is not left "running" to block the next run.
    """


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


def _operator() -> str:
    return os.environ.get("USER") or os.environ.get("RAILWAY_SERVICE_NAME") or "unknown"


def _synced(ctx: Context) -> str:
    """When the sheet was last read into the settings in force (settings/sync.last_read), in UK time."""
    from us_outbound.settings import sync

    when = sync.last_read(ctx.store, ctx.settings)
    return _fmt_time(when) if when else "never"


def _live_sending_note(ctx: Context) -> str:
    """Why a --live run stayed dry. Jobs read the synced copy of the sheet, so an edit counts only once synced."""
    return (f"Running dry: live_sending is no in the settings in force (synced {_synced(ctx)}). "
            "If you have just set it to yes on the sheet, run `us-outbound sync` and try again.")


READS_ONLY = "(--live does nothing here: this command only reads)"


def _only_reads(args: argparse.Namespace) -> None:
    """A read-only action given --live says so, rather than leave Harry thinking it acted."""
    if getattr(args, "live", False):
        print(READS_ONLY)


def _dry_note(ctx: Context, what: str) -> None:
    if not ctx.dry_run:
        return
    if ctx.live_flag is False:
        print(f"Dry-run: {what} Nothing was sent. Needs --live.")
        return
    print(f"Dry-run: {what} Nothing was sent.")
    print(_live_sending_note(ctx))


def _record_unusable(exc: bootstrap.SettingsUnusable, job: str) -> None:
    """A job that cannot start still leaves a heartbeat, so the miss is visible."""
    if exc.store is None:
        return
    now = datetime.now(UTC)
    try:
        exc.store.upsert("heartbeats", [{
            "run_id": new_id(), "job": job, "started_at": now, "finished_at": now, "status": "error",
            "dry_run": True, "detail": {"unusable": sorted(exc.errors)}, "error": str(exc),
        }])
    except Exception as write_error:  # the refusal is what matters; the log says why no row
        log("heartbeat_write_failed", job=job, error=str(write_error))


def _job(name: str, live_flag: bool, factory: Factory) -> int:
    fn = resolve_job(name)
    try:
        ctx = factory(name, live_flag)
    except bootstrap.SettingsUnusable as exc:
        _record_unusable(exc, name)
        raise
    if live_flag and ctx.dry_run:
        print(_live_sending_note(ctx))
    with _sigterm_ends_the_run():
        summary = run_job(ctx, fn)
    _print(summary)
    return 0


def _sync(live_flag: bool, factory: Factory) -> tuple[Context, dict]:
    """settings_sync, as `us-outbound sync` and `settings sync` run it: the sheet's edits into force now.

    A dry-run sync still brings the sheet in (the settings live in the database); only its Slack
    message about sheet errors goes to the dev channel instead.
    """
    fn = resolve_job("settings_sync")
    try:
        ctx = factory("settings_sync", live_flag)
    except bootstrap.SettingsUnusable as exc:
        _record_unusable(exc, "settings_sync")
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
    """The two switches as the settings now in force have them (the store, after a sync)."""
    from us_outbound.settings.sync import load_current

    settings, errors = load_current(ctx.store)
    if settings is None:
        tabs = ", ".join(t for t, errs in errors.items() if errs) or "unknown"
        return f"The settings are not usable ({tabs}): nothing runs until the sheet is fixed and synced."
    g = settings.general
    return (f"In force now: live_sending {'yes' if g.live_sending else 'no'}, "
            f"auto_send {'yes' if g.auto_send else 'no'}.")


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
    return _job(args.job, args.live, factory)


def cmd_dry_run(args: argparse.Namespace, factory: Factory) -> int:
    return _job(args.job, False, factory)


def cmd_rescore(args: argparse.Namespace, factory: Factory) -> int:
    return _job("score", args.live, factory)


def _fmt_time(v: Any) -> str:
    if not v:
        return "-"
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    d = d if d.tzinfo else d.replace(tzinfo=UTC)
    return d.astimezone(UK).strftime("%a %d %b %H:%M UK")


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
        print(f"  {job:<18} {run.get('status')} {_fmt_time(run.get('started_at'))} ({mode})"
              f"{'  MISSED its heartbeat' if missed else ''}")
        if run.get("status") == "error" and run.get("error"):
            print(f"  {'':<18} error: {str(run['error'])[:160]}")
    print(f"  {ok} job{'s' if ok != 1 else ''} ok")
    if never:
        print(f"  Not run yet: {', '.join(never)}")


def _switches(g: Any) -> str:
    """The two General switches and what they mean together."""
    if not g.live_sending:
        meaning = "dry: nothing new reaches Instantly or a prospect; `us-outbound stop --live` pauses what already sends"
    elif g.auto_send:
        meaning = "live: emails go straight to Instantly once the weekly hand-check is approved"
    else:
        meaning = "live: every email waits for an approver's ✅ in Slack"
    return (f"Switches: live_sending {'yes' if g.live_sending else 'no'} · auto_send {'yes' if g.auto_send else 'no'} "
            f"({meaning})")


def _waiting_for_you(ctx: Context) -> str:
    """"Waiting for you: 2 send approvals, 1 reply, 0 kill-rule holds" (learn/daily_post.for_you)."""
    from us_outbound.learn import daily_post

    w = daily_post.for_you(ctx)

    def n(count: int, one: str, many: str) -> str:
        return f"{count} {one if count == 1 else many}"

    line = (f"Waiting for you: {n(len(w.send_approvals), 'send approval', 'send approvals')}, "
            f"{n(len(w.replies), 'reply', 'replies')}, {n(len(w.holds), 'kill-rule hold', 'kill-rule holds')}")
    return line + (", and this week's hand-check" if w.hand_checks else "")


def _status_limits(ctx: Context) -> None:
    """This month's credit budgets and what limits today's enrollment (limits.py), as the enrol job would see it now."""
    from us_outbound import limits
    from us_outbound.enrol import approvals, enrol, second

    try:
        # As enrol.run: pulled accounts and waiting cards stay out, and the cards hold their senders' slots.
        day = ctx.now_et().date()
        _, pulled = enrol.hand_check(ctx, day)
        held = approvals.waiting(ctx)
        ready, _ = enrol.candidates(ctx, pulled, held.accounts)
        seconds, _ = second.candidates(ctx, pulled, held.accounts)  # none while second_contact is no
        lim = limits.today(ctx, day, ready_accounts=len(ready) + len(seconds), pending=held.by_owner, campaigns=True,
                           second_ready=len(seconds))
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
        print(f"Enrollment: STOPPED since {_fmt_time(paused.get('started_at'))} (run `us-outbound start --live` to resume)")
    else:
        print("Enrollment: not stopped by an operator")
    running = s.running_test()
    print(f"Running test: {running.test_id} (read on {running.read_date})" if running else "Running test: none")
    from us_outbound.enrol import second

    print(second.describe(s))  # the second-contact switch (Harry, 6 Oct 2026)
    _status_heartbeats(ctx.store, ctx.now)
    print("Mailboxes:")
    try:
        from us_outbound.learn.holds import held_mailboxes
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
    return 0


def _stop(ctx: Context) -> dict:
    inst = ctx.clients.instantly
    paused, left = [], []
    for c in inst.list_campaigns():
        # 0 draft, 2 paused, 3 completed: nothing is sending.
        if c.get("status") in (0, 2, 3):
            left.append(c["name"])
            continue
        inst.pause_campaign(c["name"])
        paused.append(c["name"])
    return {"dry_run": ctx.dry_run, "enrollment": "stopped", "by": _operator(),
            "campaigns_paused": paused, "already_not_sending": left}


def cmd_stop(args: argparse.Namespace, factory: Factory) -> int:
    ctx = factory(OPERATOR_STOP, args.live, operator=True)
    summary = run_job(ctx, _stop)
    _print(summary)
    print("Enrollment is stopped (in every mode) until `us-outbound start --live` runs with live_sending = yes.")
    _dry_note(ctx, "campaigns were not paused in Instantly.")
    return 0


def _start(ctx: Context) -> dict:
    from us_outbound.registry import blackout
    from us_outbound.registry.mailboxes import campaign_name, ensure_campaigns, held_words, sending_list

    check = ensure_campaigns(ctx, create=False)
    # Drift held while leads are in flight does not refuse: the campaign is consistent for them (Harry, 7 Oct 2026).
    held = check.get("held") or {}
    drift = {n: sorted(set(d) - set((held.get(n) or {}).get("keys") or ())) for n, d in check["drift"].items()}
    if any(drift.values()):
        raise Refused(
            "campaign settings have drifted: "
            + "; ".join(f"{n}: {', '.join(keys)}" for n, keys in drift.items() if keys)
            + ". Fix with `us-outbound campaigns ensure --fix --live` first."
        )
    inst = ctx.clients.instantly
    found = {c["name"]: c for c in inst.list_campaigns()}
    # Over a blackout the campaigns stay paused, recorded as the blackout's, and the blackout job starts them after
    # it (registry/blackout.py; Harry, 7 Oct 2026): enrollment still resumes.
    over = blackout.hold(ctx.settings, ctx.now)
    started, skipped, waiting = [], [], []
    for owner in ctx.settings.owners():
        name = campaign_name(owner)
        if name not in found or not sending_list(ctx.settings, owner):
            skipped.append(name)
            continue
        if found[name].get("status") != 1 and over is not None:
            blackout.defer(ctx, name, found[name].get("status"), over)
            waiting.append(name)
            continue
        if found[name].get("status") != 1:
            inst.activate_campaign(name)
        started.append(name)
    out = {"dry_run": ctx.dry_run, "enrollment": "resumed" if ctx.live else "still stopped (dry-run)",
           "by": _operator(), "campaigns_started": started, "skipped_no_active_mailbox": skipped}
    if waiting:
        out["paused_for_blackout"] = waiting
        out["blackout"] = f"{over.words()}: the blackout job starts them then" if over else ""
    if held:
        out["drift_held"] = [held_words(n, h) for n, h in held.items()]
    return out


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
    _print(run_job(ctx, _start))
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


def _unenrol(ctx: Context, month: str) -> dict:
    inst = ctx.clients.instantly
    contacts = ctx.store.select("contacts", {"enrolment_month": month})
    removed, without_lead = [], 0
    for c in contacts:
        lead_id, campaign = c.get("instantly_lead_id"), c.get("instantly_campaign")
        if not lead_id or not campaign:
            without_lead += 1
            continue
        inst.delete_lead(str(campaign), str(lead_id))  # the client refuses a non-US Outbound campaign
        removed.append(str(lead_id))
    return {"dry_run": ctx.dry_run, "month": month, "contacts": len(contacts),
            "leads_removed": len(removed) if ctx.live else 0, "leads_found": len(removed),
            "contacts_without_lead": without_lead}


def cmd_unenrol(args: argparse.Namespace, factory: Factory) -> int:
    if not MONTH_RE.fullmatch(args.month or ""):
        raise Refused("--month must be written YYYY-MM")
    ctx = factory("unenrol", args.live, operator=True)
    _print(run_job(ctx, lambda c: _unenrol(c, args.month)))
    _dry_note(ctx, "no lead was removed from Instantly.")
    return 0


def cmd_relabel(args: argparse.Namespace, factory: Factory) -> int:
    """`us-outbound relabel [--live]`: the queued companies under the labels the Industries rules give now
    (ops/relabel.py; Harry, 7 Oct 2026)."""
    from us_outbound.ops import relabel

    ctx = factory("relabel", args.live, operator=True)
    out = run_job(ctx, relabel.run)
    print(f"{out['changed']} of {out['queued_accounts']} companies in the queue change label.")
    for move, n in out["moves"].items():
        print(f"  {n:>4}  {move}")
    if args.all:
        for line in out["changes"]:
            print(f"  {line}")
    cards = out["cards_withdrawn"] if ctx.live else out["cards_to_withdraw"]
    if cards:
        print(f"{'Withdrew' if ctx.live else 'Would withdraw'} {len(cards)} open card(s): {', '.join(cards)}.")
    _dry_note(ctx, "no company or card was changed.")
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


def _test_start(ctx: Context, test_id: str) -> dict:
    """Pre-registration first (SPEC 12; Harry, 6 Oct 2026: kind and looks, learn/looks.py). One copy test (kind ab)
    runs at a time, since it decides each account's copy (SPEC 9); a holdout assigns nothing, so it may run beside
    it, and its versions are the arms enrol records, not Copy rows."""
    from us_outbound.settings.model import AB_TEST, HOLDOUT_TEST
    from us_outbound.settings.validate import parse_looks

    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise Refused("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    rows = ctx.clients.sheets.read_tabs(sheet_id, ["Tests"])["Tests"]
    row = next((r for r in rows if r.get("test_id", "").strip() == test_id), None)
    if row is None:
        raise Refused(f"no test {test_id!r} on the Tests tab")

    def kind(r: dict) -> str:
        return (r.get("kind") or "").strip().lower() or AB_TEST

    if kind(row) not in (AB_TEST, HOLDOUT_TEST):
        raise Refused(f"kind {row.get('kind')!r} on the Tests tab is neither {AB_TEST} nor {HOLDOUT_TEST}")
    others = [r["test_id"] for r in rows if r.get("status", "").strip().lower() == "running"
              and r.get("test_id", "").strip() != test_id and kind(r) == AB_TEST]
    if others and kind(row) == AB_TEST:
        raise Refused(f"only one copy test runs at a time (SPEC 9); {', '.join(others)} is running")
    if row.get("status", "").strip().lower() == "running":
        return {"test_id": test_id, "status": "running", "changed": False}
    read_date = row.get("read_date", "").strip()
    if not read_date or not row.get("decision_rule", "").strip():
        raise Refused("pre-register the read_date and decision_rule on the Tests tab first (SPEC 12)")
    try:
        parse_looks(row.get("looks", "") or "")
    except ValueError as exc:
        raise Refused(f"looks on the Tests tab: {exc}") from exc
    start = row.get("start_date", "").strip() or ctx.today_uk().isoformat()
    if read_date <= start:
        raise Refused(f"read_date {read_date} must be after the start date {start}")
    if kind(row) == AB_TEST:
        missing = [
            v for v in (row.get("version_a", "").strip(), row.get("version_b", "").strip())
            if (c := ctx.settings.copy_row(v)) is None or c.status != "approved" or not c.qa_current
        ]
        if missing:
            raise Refused("copy is not approved, with a current QA pass, in the synced settings for: "
                          + ", ".join(missing))
    sheets = ctx.clients.sheets
    writes = [("start_date", start)] if not row.get("start_date", "").strip() else []
    for column, value in [*writes, ("status", "running")]:
        if not sheets.update_cell(sheet_id, "Tests", {"test_id": test_id}, column, value):
            raise Refused(f"no row for test {test_id!r} on the Tests tab to update")
    return {"dry_run": ctx.dry_run, "test_id": test_id, "kind": kind(row), "status": "running", "start_date": start,
            "looks": row.get("looks", "").strip(), "read_date": read_date, "changed": ctx.live}


def read_test(ctx: Context, test_id: str) -> dict:
    """The test at its latest pre-registered look (learn/looks.read); looks.NotYet before the first one.

    Reply rate = accounts with a human reply (any class but out_of_office) within REPLY_WINDOW_DAYS (28) days of
    step 1 ÷ accounts whose step 1 was delivered, as in v_account_outcomes, over the accounts the look covers.
    """
    from us_outbound.learn import looks

    return looks.read(ctx, test_id)


def cmd_test(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.learn import looks

    if args.action == "start":
        ctx = factory("test_start", args.live, operator=True)
        _print(run_job(ctx, lambda c: _test_start(c, args.test_id)))
        _dry_note(ctx, "the Tests tab was not changed.")
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
    if args.action == "sync":
        return cmd_sync(args, factory)
    if args.action == "load":
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
    from us_outbound.settings.sync import bootstrap as create_sheet

    ctx = factory("settings_bootstrap", args.live, operator=True)
    summary = run_job(ctx, lambda c: {"sheet_id": create_sheet(c, force=args.force), "dry_run": c.dry_run})
    _print(summary)
    if summary.get("sheet_id"):
        print("Set US_OUTBOUND_SETTINGS_SHEET_ID to this id in the Railway service's variables, share the sheet "
              "with the Sheets service account as Editor, then run `us-outbound sync`.")
    else:
        _dry_note(ctx, "no sheet was created.")
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


QA_ERRORS_IN_A_ROW = 3  # copy qa: one row's failed answer is skipped; this many in a row stops the run


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
                             spend=False, settings=settings)
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
        from us_outbound.clients.claude import BudgetExceeded

        sheet_id = ctx.guard.bounds.settings_sheet_id
        results, errors = [], 0
        for row in rows:
            try:
                r = copy_desk.qa_row(ctx, row, settings)
                errors = 0
            except BudgetExceeded as exc:  # the month's cap: stop, keeping what is done
                print(f"{row.copy_version}: QA stopped: {exc}")
                break
            except Exception as exc:  # one row's answer failed: it stays unchecked, the rest go on
                errors += 1
                print(f"{row.copy_version}: not checked ({exc}); run copy qa again to retry it")
                if errors >= QA_ERRORS_IN_A_ROW:
                    print(f"QA stopped after {errors} failures in a row: the API may be down")
                    break
                continue
            results.append(r)
            print(f"{r.copy_version}: {r.cell}" + ("" if r.verdict == "pass" else f"\n  {r.notes[:600]}"), flush=True)
            # Each verdict goes to the sheet as it comes (6 Oct 2026: a dropped SSH session lost 43 held to the end),
            # so a run cut short keeps what it paid for, and the next run starts from the rows still unchecked.
            if sheet_id:
                ctx.clients.sheets.update_rows(sheet_id, "Copy", "copy_version",
                                               {r.copy_version: {"qa": r.cell, "qa_notes": r.notes}})
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


def _print_send_approvals(items: list[dict]) -> None:
    if not items:
        print("No send approvals are waiting.")
        return
    print(f"{len(items)} send approval{'s' if len(items) != 1 else ''} waiting, oldest first:")
    for i in items:
        who = ", ".join(x for x in (i["person"], i["title"]) if x)
        expired = " (expired: poll_approvals closes it)" if i["expired"] else ""
        print(f"\n{i['id']}  {i['company']} ({i['domain']}) · {who}")
        print(f"  from {i['owner']} ({i['mailbox'] or 'mailbox unknown'}) · {i['state']}{' · edited' if i['edited'] else ''}"
              f" · posted {i['send_day']}, until the end of {i['expires_on']}{expired} · in Slack: "
              f"{'yes' if i['in_slack'] else 'no'}")
        print(f"  Subject: {i['subject']}")
        print(f"  Send it: us-outbound approvals send {i['id']} --live")
        print(f"  Not this person: us-outbound approvals contact {i['id']} --live"
              f"   Not this company: us-outbound approvals company {i['id']} --live")


def cmd_approvals(args: argparse.Namespace, factory: Factory) -> int:
    """Send approvals at the command line (enrol/approvals.py): the same close path as Slack, approved_by "cli"."""
    from us_outbound.enrol import approvals

    if args.action == "list":
        _only_reads(args)
        _print_send_approvals(approvals.list_items(factory("approvals_list", False, operator=True)))
        return 0
    if not args.item_id:
        raise Refused(f"approvals {args.action} needs an item id from `us-outbound approvals list`")
    action = args.action
    if action == "industry":  # Harry, 7 Oct 2026: "industry: LABEL" in the card's thread (labels.py)
        if not args.label:
            raise Refused('approvals industry needs the label: `us-outbound approvals industry ID "Fintech" --live`')
        ctx = factory("approvals_industry", args.live, operator=True)  # it reaches no prospect: --live alone
        try:
            summary = run_job(ctx, lambda c: approvals.industry_item(c, args.item_id, args.label))
        except (LookupError, ValueError) as exc:
            raise Refused(str(exc)) from exc
        _print(summary)
        _dry_note(ctx, "the label, the Overrides tab and the card are unchanged.")
        return 0
    if action in ("contact", "company"):  # the Slack words: 👤 not this person, 🚫 not this company
        if args.contact or args.company:
            raise Refused(f"`approvals {action}` takes no --contact or --company")
        args.contact, args.company, action = action == "contact", action == "company", "reject"
    if action == "reject":
        if args.contact == args.company:
            raise Refused("approvals reject needs one of --contact (not this person) or --company (not this company)")
        # Rejecting reaches no prospect (a contact suppressed, or an account excluded): --live alone.
        ctx = factory("approvals_reject", args.live, operator=True)
        try:
            summary = run_job(ctx, lambda c: approvals.reject_item(c, args.item_id,
                                                                   "contact" if args.contact else "company"))
        except (LookupError, ValueError) as exc:
            raise Refused(str(exc)) from exc
        _print(summary)
        _dry_note(ctx, "the item, the contact and the account are unchanged.")
        return 0 if summary.get("done") or ctx.dry_run else 2
    if args.contact or args.company:
        raise Refused("--contact and --company go with reject, not send")
    # send (approve) is ✅.
    # approve adds the lead to Instantly, so it is live like a job: --live and live_sending = yes (SPEC 0.3).
    ctx = factory(approvals.APPROVALS_CLI_JOB, args.live)
    try:
        summary = run_job(ctx, lambda c: approvals.approve(c, args.item_id))
    except (LookupError, ValueError) as exc:
        raise Refused(str(exc)) from exc
    _print(summary)
    _dry_note(ctx, "the lead was not added to Instantly and the item is unchanged.")
    if summary.get("held"):
        print("Held, not sent yet: " + "; ".join(summary["held"]) + ". The approval stands: poll_approvals adds the "
              "lead within 5 minutes of that clearing, unless the card expires first.")
    elif summary.get("outcome") == approvals.BLOCKED:
        print("Not sent, and closed: " + "; ".join(summary.get("why") or ()) + ".")
    return 0 if summary.get("added") or ctx.dry_run else 2


def _audit_report(out: dict, live: bool) -> None:
    print(f"{out['to_check']} of {out['open_accounts']} open companies have no fresh label check"
          f" ({out['stale']} with one under an older label list or prompt).")
    if not live:
        print(f"A live audit asks {out['model']} about each: at most ${out['per_call_usd']:.4f} a company, "
              f"${out['most_usd']:.2f} in all (the Claude cap applies).")
        if out.get("sample_prompt"):
            print("The first company's prompt, as the model reads it:\n" + out["sample_prompt"])
        stored = out.get("stored") or {}
        print(f"From the rules and the stored checks alone, {stored.get('changed', 0)} companies change:")
        for move, n in (stored.get("moves") or {}).items():
            print(f"  {n:>4}  {move}")
        return
    print(f"Asked {out['asked']} (${out['usd']:.2f})." + (f" Stopped: {out['unavailable']}." if out["unavailable"] else ""))
    print("Decisions: " + (", ".join(f"{k} {v}" for k, v in sorted(out["decisions"].items())) or "none"))
    if out["disagreements"]:
        print("Where the rules and the model differ:")
        for d in out["disagreements"]:
            quote = f" · “{d['evidence']}”" if d.get("evidence") else ""
            print(f"  {d['domain']} · rules {d['rules'] or 'no label'} · model {d['model']} ({d['confidence']}, "
                  f"{d['entity']}) · now {d['now']}{quote}")
    for line in out["relabelled"]:
        print(f"  {line}")
    if out["cards_withdrawn"]:
        print(f"Withdrew {len(out['cards_withdrawn'])} open card(s): {', '.join(out['cards_withdrawn'])}.")


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
    """`us-outbound labels audit | eval | set | show` (us_outbound/labels.py, ops/relabel.py; Harry, 7 Oct 2026)."""
    from us_outbound import labels
    from us_outbound.ops import relabel

    if args.action == "audit":  # the model is asked only with --live; it reaches no prospect: --live alone
        ctx = factory(relabel.AUDIT_JOB, args.live, operator=True)
        out = run_job(ctx, lambda c: relabel.audit(c, args.limit))
        _audit_report(out, ctx.live)
        _dry_note(ctx, "no model was asked and no company or card was changed.")
        return 0
    if args.action == "eval":
        ctx = factory("labels_eval", args.live, operator=True)
        rows = labels.gold_rows() + (labels.corrected_rows(ctx) if args.from_corrections else [])
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
    ctx = factory("labels_set", args.live, operator=True)  # it reaches no prospect: --live alone
    try:
        summary = run_job(ctx, lambda c: relabel.set_label(c, args.domain, args.label))
    except (LookupError, ValueError) as exc:
        raise Refused(str(exc)) from exc
    _print(summary)
    _dry_note(ctx, "the label, the Overrides tab and any card are unchanged.")
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


def cmd_hubspot(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.crm import hubspot_writes as hw

    if args.action == "setup":
        ctx = factory("hubspot_setup", args.live, operator=True)
        _print(run_job(ctx, hw.ensure_properties))
        _dry_note(ctx, "no HubSpot property or group was created.")
    else:
        _only_reads(args)
        ctx = factory("hubspot_ids", False)
    ids = hw.lookup_pipeline(ctx)
    print("Paste these into the General tab (they are never written automatically):")
    for key, value in ids.items():
        print(f"  {key}: {value if value else 'NOT FOUND'}")
    return 0


def cmd_campaigns(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.registry.mailboxes import campaign_name, ensure_campaigns, held_words

    if args.in_flight and not (args.action == "ensure" and args.fix):
        raise Refused("--in-flight goes with ensure --fix")
    if args.action == "show":
        # Read-only: each owner's campaign as Instantly holds it, one JSON line each, for checking what
        # Instantly kept of the settings and step templates it was given (PHASE0-CONFIRM items).
        _only_reads(args)
        ctx = factory("campaigns_show", False)
        for owner in ctx.settings.owners():
            name = campaign_name(owner)
            body = ctx.clients.instantly.get_campaign(name)
            print(json.dumps({"campaign": name, "instantly": body}, sort_keys=True, default=str))
        return 0
    ctx = factory("campaigns_ensure", args.live, operator=True)
    summary = run_job(ctx, lambda c: ensure_campaigns(c, fix=args.fix, in_flight=args.in_flight))
    _print(summary)
    for name, held in (summary.get("held") or {}).items():
        print(held_words(name, held))
    _dry_note(ctx, "no campaign was created or changed.")
    return 0


def cmd_cohorts(args: argparse.Namespace, factory: Factory) -> int:
    """The cohort report (learn/cohorts.py; Harry, 7 Oct 2026): performance by enrolment week at fixed ages, what
    changed between versions, or the leads in flight per campaign. Read-only."""
    from us_outbound.learn import cohorts

    ctx = factory("cohorts", False)
    if args.action == "changes":
        if len(args.versions) not in (0, 2):
            raise Refused("cohorts changes takes two config versions, or none for the last two")
        lines = cohorts.changes_lines(ctx, *args.versions)
    elif args.action == "in-flight":
        lines = cohorts.in_flight_lines(ctx)
    else:
        if args.versions:
            raise Refused("config versions go with `cohorts changes`")
        lines = cohorts.lines(ctx, cut=args.cut, age=args.age, weeks=args.weeks)
    for line in lines:
        print(line)
    return 0


def cmd_suppression(args: argparse.Namespace, factory: Factory) -> int:
    return _job("suppression_load", args.live, factory)


def _fit_row(value: str | None, flag: str) -> tuple[int, int] | bool | None:
    """--close / --some LOWEST_FIT:WEIGHT as (lowest fit, weight); --some off as False; absent as None."""
    if not value:
        return None
    if flag == "--some" and value.strip().lower() == "off":
        return False
    try:
        low, weight = (int(v) for v in value.split(":"))
    except ValueError:
        raise Refused(f"{flag} takes LOWEST_FIT:WEIGHT, like 90:10, not {value!r}") from None
    if not (0 <= low <= 100 and 0 < weight <= 100):
        raise Refused(f"{flag}: the lowest fit is 0 to 100 and the weight 1 to 100, not {value!r}")
    return low, weight


def cmd_lookalikes(args: argparse.Namespace, factory: Factory) -> int:
    """show: the lookalike cells from the last lookalikes run, for the Focus tab and sourcing. fit: the accounts'
    lookalike fits and the tier mix the build's lookalike rows would give, scored in memory (Harry's check before
    the Signals tab changes). Both read the database only and write nothing."""
    from us_outbound.sources import lookalikes

    if args.action == "fit":
        close, some = _fit_row(args.close, "--close"), _fit_row(args.some, "--some")
        ctx = factory("lookalikes_fit", False)
        try:
            lines = lookalikes.fit_report(ctx, close, some)
        except ValueError as exc:
            raise Refused(str(exc)) from exc
        for line in lines:
            print(line)
        return 0
    if args.top < 1:
        raise Refused("--top must be 1 or more")
    ctx = factory("lookalikes_show", False)
    for line in lookalikes.report(ctx.settings, ctx.store, top=args.top, all_bands=args.all):
        print(line)
    return 0


def cmd_pages(args: argparse.Namespace, factory: Factory) -> int:
    """What the careers and benefits page reader has found (the database only): coverage and the decision rule."""
    from us_outbound.sources import pages

    ctx = factory("pages_show", False)
    for line in pages.report(ctx):
        print(line)
    return 0


def cmd_data(args: argparse.Namespace, factory: Factory) -> int:
    """What the sources have stored, in aggregate (the database only; ops/data_health.py)."""
    from us_outbound.ops import data_health

    ctx = factory("data_show", False)
    for line in data_health.report(ctx):
        print(line)
    return 0


# -- go-live (Harry, 1 Oct 2026) ------------------------------------------------------------------


def cmd_golive(args: argparse.Namespace, factory: Factory) -> int:
    """The read-only go/no-go check (ops/golive.py). Exits 1 if any check FAILs."""
    from us_outbound.ops import golive

    try:
        ctx = factory("golive", False)
    except bootstrap.SettingsUnusable as exc:
        checks = golive.unusable(exc.errors)
        print(golive.render(checks, datetime.now(UTC)))
        return golive.exit_code(checks)
    checks = golive.run_checks(ctx)
    g = ctx.settings.general
    print(golive.render(checks, ctx.now, channel=g.alert_channel, auto_send=g.auto_send))
    return golive.exit_code(checks)


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
            why = (f"{hand_check.AUTO_SEND_OFF}, and no account is held for doubtful facts"
                   if not ctx.settings.general.auto_send else "no queued or verified account in an active industry group")
            print(f"Hand-check {summary['iso_week']}: nothing to check ({why}).")
            return 0
        print(hand_check.text(payload, detailed=True))
        if summary["status"] == "not recorded":
            print("Not recorded yet: `us-outbound handcheck approve --live` records this sample and approves it.")
        else:
            print(f"Item {summary['item_id']}: {summary['status']}.")
        return 0
    if args.pull and args.action != "approve":
        raise Refused("--pull goes with approve")
    ctx = factory("handcheck_approve", args.live, operator=True)
    try:
        summary = run_job(ctx, lambda c: hand_check.approve(c, args.pull or [], _operator()))
    except (LookupError, ValueError) as exc:
        raise Refused(str(exc)) from exc
    _print(summary)
    _dry_note(ctx, "the hand-check was not marked approved.")
    if ctx.live:
        print(f"Approved {summary['iso_week']}; enrollment can go ahead"
              + (f", leaving out {len(summary['pulled_account_ids'])} pulled accounts." if summary["pulled_account_ids"] else "."))
    return 0


def cmd_clay(args: argparse.Namespace, factory: Factory) -> int:
    """`clay check-email`: one Work Email lookup for a Spill colleague, to confirm the email fallback (ops/clay_check.py).
    Calls Clay only with --live; exits 1 when the call failed or its output is not what the parser expects."""
    from us_outbound.clients.clay import CHECK_EMAIL_JOB
    from us_outbound.ops import clay_check

    ctx = factory(CHECK_EMAIL_JOB, args.live, operator=True)
    try:
        report = clay_check.check_email(ctx, args.first, args.last, args.domain)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    for line in clay_check.lines(report):
        print(line)
    return 0 if report["dry_run"] or report["verdict"] == clay_check.READY else 1


def cmd_signals(args: argparse.Namespace, factory: Factory) -> int:
    """Which signals predict replies (learn/signal_review.py), or the signal-value table (learn/signal_value.py,
    from v_signal_value; Harry, 6 Oct 2026): read-only, the evidence for reweighting the Signals tab."""
    if args.action == "value":
        from us_outbound.learn import signal_value

        ctx = factory("signals_value", False)
        for line in signal_value.table_lines(signal_value.rows(ctx)):
            print(line)
        return 0
    from us_outbound.learn import signal_review

    for line in signal_review.lines(signal_review.review(factory("signals_review", False))):
        print(line)
    return 0


def cmd_readout(args: argparse.Namespace, factory: Factory) -> int:
    """The Monday readout (learn/readout.py), printed here and posted nowhere: read-only."""
    from us_outbound.learn import readout

    lines, _ = readout.build(factory("readout_show", False))
    for line in lines:
        print(line)
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
            sent = f", last emailed {_fmt_time(r['last_contact'])}" if r["last_contact"] else ", not emailed yet"
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
    ctx = factory(seed.JOB, args.live, operator=True)
    try:
        summary = run_job(ctx, lambda c: seed.send(c, args.address, args.owner, industry=args.industry or "",
                                                   role=args.role or "", subject=args.subject))
    except (LookupError, ValueError) as exc:
        raise Refused(str(exc)) from exc
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
            print(f"{r['item_id']}  {r['rule']}: {r['action']} {r['target']}{until} ({_fmt_time(r.get('created_at'))}"
                  f"{', dry-run' if r.get('dry_run') else ''})")
            print(f"    {r['reason']}")
        return 0
    if not args.item:
        raise Refused("killrules clear needs the item id (`us-outbound killrules show` lists them)")
    ctx = factory("killrules_clear", args.live, operator=True)
    try:
        summary = run_job(ctx, lambda c: kill_rules.clear(c, args.item, _operator()))
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
    return scheduler.Scheduler(max_parallel=scheduler.max_parallel_from_env()).run()


# -- parser and entrypoint ----------------------------------------------------------------


HELP = ("Spill's US Outbound engine. Dry-run by default. `--live` makes a command act. Anything that reaches a "
        "prospect (start, approvals send, replies send, every scheduled job) also needs live_sending = yes in the "
        "synced settings.")
# Build and duplicate commands (4 Oct 2026): each still works as before, but `--help` lists only what Harry uses.
HIDDEN = frozenset({"unenrol", "rescore", "dry-run", "db", "hubspot", "suppression", "lookalikes", "pages",
                    "data", "scheduler"})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="us-outbound", description=HELP)
    sub = p.add_subparsers(dest="command", required=True)
    live = argparse.ArgumentParser(add_help=False)
    live.add_argument("--live", action="store_true",
                      help="act for real (anything that reaches a prospect also needs live_sending = yes)")

    def command(name: str, about: str, fn: Callable[..., int], *, takes_live: bool = False) -> argparse.ArgumentParser:
        """A subcommand; a hidden one has no help=, so argparse leaves it out of the list (it still parses)."""
        kw: dict[str, Any] = {"description": about, "parents": [live] if takes_live else []}
        if name not in HIDDEN:
            kw["help"] = about
        parser = sub.add_parser(name, **kw)
        parser.set_defaults(fn=fn)
        return parser

    # What Harry uses, in the order `--help` lists it.
    command("status", "the switches, what waits for you, jobs, mailboxes and campaigns", cmd_status)
    command("golive", "the read-only go/no-go check before the first sends", cmd_golive)
    # The choices are written out here, so --help does not import the view (ops/accounts_view.py STATUSES, TIERS).
    ac = command("accounts", "the companies and contacts we hold: a summary, a list, one company, or a CSV "
                 "(read-only)", cmd_accounts)
    ac.add_argument("domain", nargs="?", metavar="DOMAIN",
                    help="one company in full: its facts, its contacts with their emails, signals, events and Slack "
                         "cards (a domain or a web address)")
    ac.add_argument("--status", action="append", metavar="STATUS",
                    help="only companies with this status (repeat it, or a comma list): new, queued, verified, "
                         "enrolled, engaged, demo_requested, demo_booked or disqualified")
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
    command("sync", "bring the sheet's edits into force now (the same as settings sync)", cmd_sync, takes_live=True)
    command("start", "sync the sheet, then resume the campaigns and enrollment", cmd_start, takes_live=True)
    command("stop", "pause every US Outbound campaign and enrollment (the brake)", cmd_stop, takes_live=True)

    ap = command("approvals", "the emails waiting for a ✅, without Slack: list, send, or not this contact or company",
                 cmd_approvals, takes_live=True)
    ap.add_argument("action", choices=["list", "send", "contact", "company", "approve", "reject", "industry"],
                    help="send = ✅ (approve); contact = 👤 not this person; company = 🚫 not this company "
                         "(reject --contact / --company); industry = the company's label is another")
    ap.add_argument("item_id", nargs="?", help="the id `approvals list` shows (or its first characters)")
    ap.add_argument("label", nargs="?", help="industry: the Industries label, like \"Fintech\"")
    ap.add_argument("--contact", action="store_true", help="reject: not this person; pick_contacts finds the next")
    ap.add_argument("--company", action="store_true", help="reject: not this company; it is excluded")

    rp = command("replies", "the replies waiting, without Slack: list, send the draft (or your text), or skip",
                 cmd_replies, takes_live=True)
    rp.add_argument("action", choices=["list", "send", "skip", "approve"], help="send = ✅ (approve)")
    rp.add_argument("item_id", nargs="?", help="send, skip: the id `replies list` shows (or its first characters)")
    rp.add_argument("--text", "--edit", dest="edit",
                    help="send: send this text instead of the draft (recorded as edited)")

    sg = command("signals", "which signals predict replies (review), or the signal-value table (value); read-only",
                 cmd_signals)
    sg.add_argument("action", choices=["review", "value"],
                    help="review: each signal's verdict from the events; value: the table, with meetings, against "
                         "the companies without it")
    command("readout", "the Monday readout for last week, printed here and posted nowhere (read-only)", cmd_readout)
    # The choices are written out here, so --help does not import the report (learn/cohorts.py CUTS, AGES).
    ch = command("cohorts", "each enrolment week's results at 7, 14, 21 and 28 days; what changed between versions "
                 "(changes); the leads in flight (in-flight). Read-only", cmd_cohorts)
    ch.add_argument("action", nargs="?", choices=["changes", "in-flight"],
                    help="changes: what differs between two config versions (default: the last two); in-flight: "
                         "each campaign's leads with a step still to send")
    ch.add_argument("versions", nargs="*", metavar="VERSION", help="changes: two config versions, older first")
    ch.add_argument("--cut", default="all",
                    choices=["all", "tier", "angle", "industry_group", "sender", "copy_version", "subject_arm",
                             "opener_arm", "config_version"],
                    help="split each week by this (default all)")
    ch.add_argument("--age", type=int, choices=[7, 14, 21, 28], help="only this age, in days after email 1")
    ch.add_argument("--weeks", type=int, default=8, help="how many enrolment weeks, the latest first (default 8)")
    ts = command("test", "start a test on the Tests tab, or read it at a pre-registered look", cmd_test,
                 takes_live=True)
    ts.add_argument("action", choices=["start", "read"],
                    help="read refuses before the test's first look (its looks, then read_date), so nobody peeks")
    ts.add_argument("test_id")

    sd = command("seed", "the seed-inbox test of the unsubscribe link: send a seed email, or check it",
                 cmd_seed, takes_live=True)
    sd.add_argument("action", choices=["send", "check"])
    sd.add_argument("address", nargs="?", help="send: the seed inbox (one of ours, never a prospect's)")
    sd.add_argument("--owner", help="send: whose campaign sends it, e.g. \"Hannah Spalding\"")
    sd.add_argument("--industry", help="send: whose Copy row to use (default: the first active industry)")
    sd.add_argument("--role", help="send: the copy role (default: People leader)")
    sd.add_argument("--subject", choices=["personal", "copy"], default="copy",
                    help="send: email 1's subject: personal (General email1_subject) or copy (the Copy row's "
                         "s1_subject, the default)")

    kr = command("killrules", "the kill-rule holds in force, or lift one", cmd_killrules, takes_live=True)
    kr.add_argument("action", choices=["show", "clear"])
    kr.add_argument("item", nargs="?", help="clear: the item id")

    mb = command("mailbox", "add, pause, retire or check a mailbox", cmd_mailbox, takes_live=True)
    mb.add_argument("action", choices=["add", "pause", "retire", "check"])
    mb.add_argument("address", nargs="?")
    mb.add_argument("--owner", help="the real person named on the address (add)")
    mb.add_argument("--domain", help="the address's domain (add; defaults to it)")
    mb.add_argument("--daily-cap", type=int, default=30, help="sends per day (add; at most 30)")
    mb.add_argument("--fix", action="store_true",
                    help="check: also set each sender name to its owner's full name, create missing campaigns "
                         "and put drifted ones right (campaigns ensure --fix)")

    cp = command("campaigns", "show each sender's campaign, or create them (paused) and put drift right",
                 cmd_campaigns, takes_live=True)
    cp.add_argument("action", choices=["ensure", "show"])
    cp.add_argument("--fix", action="store_true", help="put drifted settings and sending lists right")
    cp.add_argument("--in-flight", action="store_true",
                    help="ensure --fix: also apply drift in the steps, delays or text_only to campaigns with leads in "
                         "flight (held otherwise; it reaches every lead's remaining emails, and is logged)")

    co = command("copy", "check, preview, QA (task model) or draft (writing model) copy", cmd_copy, takes_live=True)
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
                         "opener_generic_*; Control accounts too)")
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

    st = command("settings", "sync the sheet, load the build's tabs or notes into it, or create it", cmd_settings,
                 takes_live=True)
    st.add_argument("action", choices=["sync", "load", "bootstrap"])
    st.add_argument("--force", action="store_true", help="bootstrap even if a sheet id is set")
    st.add_argument("--tab", action="append",
                    choices=["General", "Industries", "Copy", "Roles", "Signals", "Focus", "Tests"],
                    help="load: the tab (default General, Industries, Copy and Roles)")
    st.add_argument("--take", action="append", metavar="COLUMN",
                    help="load: let the build's value win for a column Harry owns (Industries active, priority; "
                         "any Signals column, like weight; General note)")
    st.add_argument("--keep", action="append", metavar="COLUMN",
                    help="load: keep the sheet's value in this column for this load, where the build's would win "
                         "(an edit made on the sheet, like a page_faqs line)")
    st.add_argument("--set", action="append", metavar="KEY=VALUE", help="load: a General value Harry has decided")
    st.add_argument("--replace-drafts", action="store_true",
                    help="load: replace the Copy rows Harry has not approved with the build's (approved rows stay)")

    hc = command("handcheck", "this week's hand-check: show it, or approve it", cmd_handcheck, takes_live=True)
    hc.add_argument("action", choices=["show", "approve"])
    hc.add_argument("--pull", nargs="+", action="extend", metavar="ACCOUNT_ID",
                    help="approve: accounts to leave out (ids or domains)")

    cl = command("clay", "one Work Email lookup for your own name, to confirm Clay's email fallback", cmd_clay,
                 takes_live=True)
    cl.add_argument("action", choices=["check-email"])
    cl.add_argument("--first", required=True, help="your first name (a Spill colleague's own, never a prospect's)")
    cl.add_argument("--last", required=True, help="your last name")
    cl.add_argument("--domain", required=True, help="Spill's own domain, like spill.chat")

    lb = command("labels", "the industry label check: check the queue (audit), score the model (eval), set a "
                 "company's label, or show its label and history", cmd_labels, takes_live=True)
    lb.add_argument("action", choices=["audit", "eval", "set", "show"])
    lb.add_argument("domain", nargs="?", help="set, show: the company's domain")
    lb.add_argument("label", nargs="?", help="set: the Industries label, like \"Fintech\"")
    lb.add_argument("--limit", type=int, help="audit: ask about at most this many companies (default: all)")
    lb.add_argument("--from-corrections", action="store_true",
                    help="eval: also score the companies approvers corrected, expecting their new label")
    rl = command("relabel", "put the companies in the queue under the industry labels the rules give now, and "
                 "withdraw open cards whose label changes", cmd_relabel, takes_live=True)
    rl.add_argument("--all", action="store_true", help="list every company that changes, not only the counts")
    er = command("erase", "an erasure request: remove a person everywhere we hold them", cmd_erase, takes_live=True)
    er.add_argument("--email", required=True)

    command("schedule", "the job table and each job's next run (UK time)", cmd_schedule)
    rn = command("run", "run one job now (what the scheduler starts)", cmd_run, takes_live=True)
    rn.add_argument("job")

    # Hidden: build and duplicate commands, unchanged.
    un = command("unenrol", "remove a month's leads from their campaigns", cmd_unenrol, takes_live=True)
    un.add_argument("--month", required=True, help="YYYY-MM")
    command("rescore", "run the score job", cmd_rescore, takes_live=True)
    dr = command("dry-run", "run a job in dry-run", cmd_dry_run)
    dr.add_argument("job")
    db = command("db", "create the tables and views in DATABASE_URL (prints them unless --live)", cmd_db, takes_live=True)
    db.add_argument("action", choices=["apply"])
    hs = command("hubspot", "the six properties, and the ids for the General tab", cmd_hubspot, takes_live=True)
    hs.add_argument("action", choices=["setup", "ids"])
    sp = command("suppression", "load HubSpot opt-outs and bounces", cmd_suppression, takes_live=True)
    sp.add_argument("action", choices=["load"])
    lk = command("lookalikes", "the top lookalike cells from Spill's HubSpot customers, or the accounts' fits",
                 cmd_lookalikes)
    lk.add_argument("action", choices=["show", "fit"])
    lk.add_argument("--top", type=int, default=20, help="how many cells to list (default 20)")
    lk.add_argument("--all", action="store_true", help="every size band, not only 10 to 249 staff")
    lk.add_argument("--close", metavar="FIT:WEIGHT", help="fit: try Close match at this lowest fit and weight, like 90:10")
    lk.add_argument("--some", metavar="FIT:WEIGHT", help="fit: try Some match from this lowest fit up to Close match's, "
                    "like 60:4, or off")
    pg = command("pages", "what the careers and benefits page reader has found, and its coverage", cmd_pages)
    pg.add_argument("action", choices=["show"])
    dt = command("data", "what the sources have stored, in aggregate (read-only)", cmd_data)
    dt.add_argument("action", choices=["show"])
    sc = command("scheduler", "the always-on worker: start every job on its schedule", cmd_scheduler)
    sc.add_argument("--list", action="store_true", help="print the job table instead (same as `schedule`)")

    sub.metavar = "{" + ",".join(name for name in sub.choices if name not in HIDDEN) + "}"
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
        from us_outbound.crm.hubspot_writes import PropertyClash
        from us_outbound.registry.mailboxes import MailboxError

        bug = isinstance(exc, (KeyError, IndexError, UnicodeError, json.JSONDecodeError))
        if not bug and isinstance(exc, (MailboxError, PropertyClash, ValueError, LookupError)):
            print(f"us-outbound {args.command}: {redact(str(exc))}", file=sys.stderr)
            return 2
        print(redact(traceback.format_exc()), file=sys.stderr)
        return 1
