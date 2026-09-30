"""The us-outbound command line (SPEC 13 Operations): the same entrypoints as the jobs.

SPEC 13 commands:
  status                              settings, heartbeats, registry, campaigns
  stop | start                        pause or resume every US Outbound campaign, and enrollment
  mailbox add|pause|retire <address>  the registry commands of SPEC 9 (add takes --owner)
  unenrol --month YYYY-MM             remove that month's leads from their campaigns
  rescore                             the score job
  dry-run <job>                       run a job in dry-run whatever the flags
  erase --email <address>             an erasure request (SPEC 6)
  test start|read <test_id>           start the copy test, or read it (SPEC 12)
Build support: run <job> [--live] (the Cloud Run entrypoint), mailbox check (mailbox_health
by hand), settings sync|bootstrap, bq apply, hubspot setup|ids, campaigns ensure [--fix],
suppression load, deploy plan.

Dry-run is the default everywhere. Two kinds of live:
  * jobs (run, rescore, settings sync, suppression load) and start: --live AND
    live_sending = yes, as SPEC 0.3 says;
  * operator commands whose writes never reach a prospect (stop, mailbox, unenrol, erase,
    test start, settings bootstrap, hubspot setup, campaigns ensure): --live alone, so the
    phase-0 setup and the kill switch work while live_sending is still no.
Every run writes a heartbeats row (ops/heartbeat.run_job). stop and start write theirs
under operator_stop / operator_start, which is the enrollment pause enrol checks (enrol.operator_pause).

Exit codes: 0 done; 1 unexpected error (with its traceback, including a KeyError, IndexError
or JSON/Unicode decoding error, which are bugs rather than bad input); 2 refused (not built,
bad input, unusable settings); 3 blocked by a guardrail (GuardViolation).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
    "source_universe": "not built yet (phase 1)",
    "apollo_signals": "not built yet (phase 1)",
    "site_visits": "not built yet (phase 1)",
    "public_signals": "not built yet (phase 1)",
    "verify_in_clay": "not built yet (phase 1)",
    "score": "us_outbound.scoring.score:rescore",
    "pick_contacts": "not built yet (phase 2)",
    "enrol": "us_outbound.enrol.enrol:run",
    "poll_replies": "not built yet (phase 2)",
    "poll_approvals": "not built yet (phase 2)",
    "hubspot_readback": "not built yet (phase 2)",
    "sync_outcomes": "not built yet (phase 2)",
    "mailbox_health": "us_outbound.registry.mailboxes:mailbox_health",
    "kill_rules": "not built yet (phase 3)",
    "daily_post": "not built yet (phase 3)",
    "monday_readout": "not built yet (phase 3)",
    # Build additions (README "Deviations").
    "heartbeat_check": "us_outbound.ops.heartbeat:check_heartbeats",
    "suppression_load": "us_outbound.suppression:load_from_hubspot",
}
JOBS_FILE = Path(__file__).resolve().parents[2] / "deploy" / "jobs.yaml"
MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
TEST_WINDOW_DAYS = 21  # SPEC 12: human replies within 21 days of step 1

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


def _operator() -> str:
    return os.environ.get("USER") or os.environ.get("CLOUD_RUN_EXECUTION") or "unknown"


def _dry_note(ctx: Context, what: str) -> None:
    if ctx.dry_run:
        need = "--live" if ctx.live_flag is False else "live_sending = yes in the settings sheet"
        print(f"Dry-run: {what} Nothing was sent. Needs {need}.")


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
        print("Running dry: --live was given but live_sending is not yes in the settings sheet.")
    _print(run_job(ctx, fn))
    return 0


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
    runs = latest_runs(store)
    built = set(built_jobs())
    scheduled = set(scheduled_jobs())
    print("Jobs:")
    for job, target in JOBS.items():
        run = runs.get(job)
        if job not in built:
            print(f"  {job:<18} {target if not _is_target(target) else 'not built yet'}")
            continue
        if not run:
            print(f"  {job:<18} never run")
            continue
        flag = ""
        if job in scheduled:
            over = overdue_minutes(job, run, now)
            flag = "  MISSED" if over is not None and over > 0 else ""
        mode = "dry-run" if run.get("dry_run") else "live"
        print(f"  {job:<18} {run.get('status')} {_fmt_time(run.get('started_at'))} ({mode}){flag}")
        if run.get("status") == "error" and run.get("error"):
            print(f"  {'':<18} error: {str(run['error'])[:160]}")


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
    print(f"live_sending: {'yes' if g.live_sending else 'no'} (jobs are live only with --live and live_sending = yes)")
    print(f"Settings synced: {_fmt_time(s.synced_at)}")
    paused = enrolment_paused(ctx.store)
    if paused:
        print(f"Enrollment: STOPPED since {_fmt_time(paused.get('started_at'))} (run `us-outbound start --live` to resume)")
    else:
        print("Enrollment: not stopped by an operator")
    running = s.running_test()
    print(f"Running test: {running.test_id} (read on {running.read_date})" if running else "Running test: none")
    _status_heartbeats(ctx.store, ctx.now)
    print("Mailboxes:")
    for m in s.mailboxes:
        print(f"  {m.address:<28} {m.owner_name:<18} {m.status:<8} cap {m.daily_cap}")
    try:
        campaigns = ctx.clients.instantly.list_campaigns()
        from us_outbound.clients.instantly import CAMPAIGN_STATUS

        print("Campaigns:")
        for c in campaigns:
            print(f"  {c.get('name')}: {CAMPAIGN_STATUS.get(c.get('status'), c.get('status'))}")
        if not campaigns:
            print("  none yet")
    except Exception as exc:  # status still prints what it can
        print(f"Campaigns: unavailable ({type(exc).__name__}: {redact(str(exc))[:160]})")
    try:
        open_items = ctx.store.select("hitl_items", {"status": "open"})
        print(f"Open human-in-the-loop items: {len(open_items)}")
    except Exception as exc:
        print(f"Open human-in-the-loop items: unavailable ({type(exc).__name__})")
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
    from us_outbound.registry.mailboxes import campaign_name, ensure_campaigns, sending_list

    check = ensure_campaigns(ctx, create=False)
    if check["drift"]:
        raise Refused(
            "campaign settings have drifted: "
            + "; ".join(f"{n}: {', '.join(sorted(d))}" for n, d in check["drift"].items())
            + ". Fix with `us-outbound campaigns ensure --fix --live` first."
        )
    inst = ctx.clients.instantly
    found = {c["name"]: c for c in inst.list_campaigns()}
    started, skipped = [], []
    for owner in ctx.settings.owners():
        name = campaign_name(owner)
        if name not in found or not sending_list(ctx.settings, owner):
            skipped.append(name)
            continue
        if found[name].get("status") != 1:
            inst.activate_campaign(name)
        started.append(name)
    return {"dry_run": ctx.dry_run, "enrollment": "resumed" if ctx.live else "still stopped (dry-run)",
            "by": _operator(), "campaigns_started": started, "skipped_no_active_mailbox": skipped}


def cmd_start(args: argparse.Namespace, factory: Factory) -> int:
    ctx = factory(OPERATOR_START, args.live)
    _print(run_job(ctx, _start))
    _dry_note(ctx, "campaigns were not activated and enrollment stays stopped.")
    return 0


def cmd_mailbox(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.registry import mailboxes as reg

    if args.action == "check":
        ctx = factory("mailbox_health", args.live, operator=True)
        _print(run_job(ctx, reg.mailbox_health))
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
        print("The sheet is changed; the next settings_sync brings it into BigQuery (or run `us-outbound settings sync`).")
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
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise Refused("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    rows = ctx.clients.sheets.read_tabs(sheet_id, ["Tests"])["Tests"]
    row = next((r for r in rows if r.get("test_id", "").strip() == test_id), None)
    if row is None:
        raise Refused(f"no test {test_id!r} on the Tests tab")
    others = [r["test_id"] for r in rows if r.get("status", "").strip().lower() == "running" and r.get("test_id", "").strip() != test_id]
    if others:
        raise Refused(f"only one test runs at a time (SPEC 9); {', '.join(others)} is running")
    if row.get("status", "").strip().lower() == "running":
        return {"test_id": test_id, "status": "running", "changed": False}
    read_date = row.get("read_date", "").strip()
    if not read_date or not row.get("decision_rule", "").strip():
        raise Refused("pre-register the read_date and decision_rule on the Tests tab first (SPEC 12)")
    start = row.get("start_date", "").strip() or ctx.today_uk().isoformat()
    if read_date <= start:
        raise Refused(f"read_date {read_date} must be after the start date {start}")
    missing = [
        f"{v} step {step}"
        for v in (row.get("version_a", "").strip(), row.get("version_b", "").strip())
        for step in (1, 2, 3, 4)
        if ctx.settings.approved_copy(v, step) is None
    ]
    if missing:
        raise Refused("copy is not approved in the synced settings for: " + ", ".join(missing))
    sheets = ctx.clients.sheets
    writes = [("start_date", start)] if not row.get("start_date", "").strip() else []
    for column, value in [*writes, ("status", "running")]:
        if not sheets.update_cell(sheet_id, "Tests", {"test_id": test_id}, column, value):
            raise Refused(f"no row for test {test_id!r} on the Tests tab to update")
    return {"dry_run": ctx.dry_run, "test_id": test_id, "status": "running", "start_date": start,
            "read_date": read_date, "changed": ctx.live}


def _when(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def read_test(ctx: Context, test_id: str) -> dict:
    """Per version: accounts with step 1 delivered, and their human, positive and meeting rates (SPEC 12).

    Reply rate = accounts with a human reply (any class but out_of_office) within 21 days of
    step 1 ÷ accounts whose step 1 was delivered, as in v_account_outcomes.
    """
    test = next((t for t in ctx.settings.tests if t.test_id == test_id), None)
    contacts = ctx.store.select("contacts", {"test_id": test_id})
    version_of: dict[str, str] = {}
    for c in contacts:
        if c.get("account_id"):
            version_of.setdefault(c["account_id"], c.get("copy_version") or "unknown")
    events = ctx.store.select("events", {"account_id": sorted(version_of)}) if version_of else []
    by_account: dict[str, list[dict]] = {}
    for e in events:
        by_account.setdefault(e["account_id"], []).append(e)
    stats: dict[str, dict[str, int]] = {}
    for account_id, version in version_of.items():
        s = stats.setdefault(version, {"accounts": 0, "delivered": 0, "replied": 0, "positive": 0, "meetings": 0})
        s["accounts"] += 1
        evs = sorted(by_account.get(account_id, []), key=lambda e: (_when(e.get("occurred_at")) or datetime.max.replace(tzinfo=UTC)))
        step1 = next((e for e in evs if e.get("type") == "sent" and e.get("step") == 1), None)
        if step1 is None:
            continue
        bounced = any(
            e.get("type") == "bounced" and e.get("contact_id") == step1.get("contact_id") and e.get("step") in (1, None)
            for e in evs
        )
        if bounced:
            continue
        s["delivered"] += 1
        at = _when(step1.get("occurred_at"))
        window = [
            e for e in evs
            if e.get("type") == "replied" and (e.get("reply_class") or "") != "out_of_office"
            and at is not None and at <= (_when(e.get("occurred_at")) or at) < at + timedelta(days=TEST_WINDOW_DAYS)
        ]
        s["replied"] += bool(window)
        s["positive"] += any(e.get("reply_class") in ("positive", "referral") for e in window)
        s["meetings"] += any(e.get("type") == "meeting_booked" for e in evs)

    def rate(n: int, d: int) -> float | None:
        return round(n / d, 4) if d else None

    versions = {
        v: {**s, "reply_rate": rate(s["replied"], s["delivered"]), "positive_rate": rate(s["positive"], s["delivered"]),
            "meeting_rate": rate(s["meetings"], s["delivered"])}
        for v, s in sorted(stats.items())
    }
    today = ctx.today_uk()
    return {
        "test_id": test_id,
        "version_a": test.version_a if test else None,
        "version_b": test.version_b if test else None,
        "read_date": test.read_date.isoformat() if test and test.read_date else None,
        "early_look": bool(test and test.read_date and today < test.read_date),
        "versions": versions,
    }


def cmd_test(args: argparse.Namespace, factory: Factory) -> int:
    if args.action == "start":
        ctx = factory("test_start", args.live, operator=True)
        _print(run_job(ctx, lambda c: _test_start(c, args.test_id)))
        _dry_note(ctx, "the Tests tab was not changed.")
        return 0
    ctx = factory("test_read", False)
    result = read_test(ctx, args.test_id)
    _print(result)
    if result["early_look"]:
        print(f"This is an early look: SPEC 12 reads the test once, on {result['read_date']}.")
    print("Reply rate decides (a 2x difference is what the test detects); positive and meeting rates are for information.")
    print("Harry writes the result on the Tests tab.")
    return 0


# -- build support ------------------------------------------------------------------------


def cmd_settings(args: argparse.Namespace, factory: Factory) -> int:
    if args.action == "sync":
        return _job("settings_sync", args.live, factory)
    from us_outbound.settings.sync import bootstrap as create_sheet

    ctx = factory("settings_bootstrap", args.live, operator=True)
    summary = run_job(ctx, lambda c: {"sheet_id": create_sheet(c, force=args.force), "dry_run": c.dry_run})
    _print(summary)
    if summary.get("sheet_id"):
        print("Set US_OUTBOUND_SETTINGS_SHEET_ID to this id (and in deploy), share the sheet with the jobs' "
              "service account as Editor, then run `us-outbound settings sync`.")
    else:
        _dry_note(ctx, "no sheet was created.")
    return 0


def cmd_bq(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.ops import ddl

    argv = []
    if args.project:
        argv += ["--project", args.project]
    if args.location:
        argv += ["--location", args.location]
    if args.live or args.execute:
        argv.append("--execute")
    return ddl.main(argv)


def cmd_hubspot(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.crm import hubspot_writes as hw

    if args.action == "setup":
        ctx = factory("hubspot_setup", args.live, operator=True)
        _print(run_job(ctx, hw.ensure_properties))
        _dry_note(ctx, "no HubSpot property or group was created.")
    else:
        ctx = factory("hubspot_ids", False)
    ids = hw.lookup_pipeline(ctx)
    print("Paste these into the General tab (they are never written automatically):")
    for key, value in ids.items():
        print(f"  {key}: {value if value else 'NOT FOUND'}")
    return 0


def cmd_campaigns(args: argparse.Namespace, factory: Factory) -> int:
    from us_outbound.registry.mailboxes import ensure_campaigns

    ctx = factory("campaigns_ensure", args.live, operator=True)
    _print(run_job(ctx, lambda c: ensure_campaigns(c, fix=args.fix)))
    _dry_note(ctx, "no campaign was created or changed.")
    return 0


def cmd_suppression(args: argparse.Namespace, factory: Factory) -> int:
    return _job("suppression_load", args.live, factory)


def _scalar(text: str) -> Any:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(x) for x in t[1:-1].split(",") if x.strip()]
    if t in ("true", "false"):
        return t == "true"
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return t


def _strip_comment(line: str) -> str:
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def read_jobs_file(path: Path = JOBS_FILE) -> list[dict]:
    """deploy/jobs.yaml, which keeps to a flat list of `key: value` maps so no YAML library is needed."""
    jobs: list[dict] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = _strip_comment(raw)
        if not line.strip() or line.strip() == "jobs:":
            continue
        body = line.strip()
        if body.startswith("- "):
            jobs.append({})
            body = body[2:]
        if not jobs or ":" not in body:
            raise ValueError(f"{path.name} line {n}: expected `key: value` inside a `- name:` item")
        key, _, value = body.partition(":")
        jobs[-1][key.strip()] = _scalar(value)
    return jobs


def cmd_deploy(args: argparse.Namespace, factory: Factory) -> int:
    """One tab-separated line per job for deploy/deploy.sh: name, schedule, disabled, args, timeout, memory."""
    for j in read_jobs_file(Path(args.file) if args.file else JOBS_FILE):
        print("\t".join([
            j["name"], j.get("schedule") or "-", "true" if j.get("disabled") else "false",
            ",".join(str(a) for a in j.get("args") or []), str(j.get("timeout") or "600s"), str(j.get("memory") or "512Mi"),
        ]))
    return 0


# -- parser and entrypoint ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="us-outbound", description="Spill's US Outbound engine (SPEC.md). Dry-run by default.")
    sub = p.add_subparsers(dest="command", required=True)
    live = argparse.ArgumentParser(add_help=False)
    live.add_argument("--live", action="store_true", help="write for real (jobs also need live_sending = yes)")

    sub.add_parser("status", help="settings, heartbeats, mailboxes and campaigns").set_defaults(fn=cmd_status)
    sub.add_parser("stop", parents=[live], help="pause every US Outbound campaign and enrollment").set_defaults(fn=cmd_stop)
    sub.add_parser("start", parents=[live], help="resume the campaigns and enrollment").set_defaults(fn=cmd_start)

    mb = sub.add_parser("mailbox", parents=[live], help="mailbox registry commands (SPEC 9)")
    mb.add_argument("action", choices=["add", "pause", "retire", "check"])
    mb.add_argument("address", nargs="?")
    mb.add_argument("--owner", help="the real person named on the address (add)")
    mb.add_argument("--domain", help="the address's domain (add; defaults to it)")
    mb.add_argument("--daily-cap", type=int, default=30, help="sends per day (add; at most 30)")
    mb.set_defaults(fn=cmd_mailbox)

    un = sub.add_parser("unenrol", parents=[live], help="remove a month's leads from their campaigns")
    un.add_argument("--month", required=True, help="YYYY-MM")
    un.set_defaults(fn=cmd_unenrol)

    sub.add_parser("rescore", parents=[live], help="run the score job").set_defaults(fn=cmd_rescore)

    dr = sub.add_parser("dry-run", help="run a job in dry-run")
    dr.add_argument("job")
    dr.set_defaults(fn=cmd_dry_run)

    rn = sub.add_parser("run", parents=[live], help="run a job (the Cloud Run entrypoint)")
    rn.add_argument("job")
    rn.set_defaults(fn=cmd_run)

    er = sub.add_parser("erase", parents=[live], help="handle an erasure request (SPEC 6)")
    er.add_argument("--email", required=True)
    er.set_defaults(fn=cmd_erase)

    ts = sub.add_parser("test", parents=[live], help="start or read the copy test (SPEC 12)")
    ts.add_argument("action", choices=["start", "read"])
    ts.add_argument("test_id")
    ts.set_defaults(fn=cmd_test)

    st = sub.add_parser("settings", parents=[live], help="sync the sheet, or create it with the defaults")
    st.add_argument("action", choices=["sync", "bootstrap"])
    st.add_argument("--force", action="store_true", help="bootstrap even if a sheet id is set")
    st.set_defaults(fn=cmd_settings)

    bq = sub.add_parser("bq", parents=[live], help="BigQuery DDL")
    bq.add_argument("action", choices=["apply"])
    bq.add_argument("--execute", action="store_true", help="same as --live")
    bq.add_argument("--project")
    bq.add_argument("--location")
    bq.set_defaults(fn=cmd_bq)

    hs = sub.add_parser("hubspot", parents=[live], help="the six properties, and the ids for the General tab")
    hs.add_argument("action", choices=["setup", "ids"])
    hs.set_defaults(fn=cmd_hubspot)

    cp = sub.add_parser("campaigns", parents=[live], help="create the sender campaigns (paused) and check drift")
    cp.add_argument("action", choices=["ensure"])
    cp.add_argument("--fix", action="store_true", help="put drifted settings and sending lists right")
    cp.set_defaults(fn=cmd_campaigns)

    sp = sub.add_parser("suppression", parents=[live], help="load HubSpot opt-outs and bounces")
    sp.add_argument("action", choices=["load"])
    sp.set_defaults(fn=cmd_suppression)

    dp = sub.add_parser("deploy", help="the job list for deploy/deploy.sh")
    dp.add_argument("action", choices=["plan"])
    dp.add_argument("--file")
    dp.set_defaults(fn=cmd_deploy)
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
    except Exception as exc:
        from us_outbound.crm.hubspot_writes import PropertyClash
        from us_outbound.registry.mailboxes import MailboxError

        bug = isinstance(exc, (KeyError, IndexError, UnicodeError, json.JSONDecodeError))
        if not bug and isinstance(exc, (MailboxError, PropertyClash, ValueError, LookupError)):
            print(f"us-outbound {args.command}: {redact(str(exc))}", file=sys.stderr)
            return 2
        print(redact(traceback.format_exc()), file=sys.stderr)
        return 1
