"""The us-outbound CLI (SPEC 13): parsing, dry-run by default, the job registry, stop/start, tests, bootstrap."""

from __future__ import annotations

import json
import dataclasses
import re
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_registry import C_HANNAH, C_HARRY, C_SAM, HANNAH, HARRY, HARRY2, SAM, SETTINGS, FakeInstantly, StubSheets, slack_routes
from us_outbound.base import heartbeats
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.ops import bootstrap, cli
from us_outbound.ops import heartbeat as hb
from us_outbound.settings.model import General, Settings
from us_outbound.settings.model import Test as CopyTest

SPEC9_JOBS = [
    "settings_sync", "source_universe", "apollo_signals", "site_visits", "public_signals", "verify_in_clay", "score",
    "pick_contacts", "enrol", "poll_replies", "poll_approvals", "hubspot_readback", "sync_outcomes", "mailbox_health",
    "kill_rules", "daily_post", "monday_readout",
]
LIVE_SETTINGS = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, live_sending=True))


class Harness:
    """A context factory over one store and transport, with the real live rule (bootstrap.resolve_live)."""

    def __init__(self, settings: Settings = SETTINGS, sheet_tabs: dict | None = None):
        self.settings = settings
        self.transport = slack_routes(FakeTransport())
        self.instantly = FakeInstantly(self.transport)
        self.store = MemoryStore(Guard())
        self.sheet_tabs = sheet_tabs if sheet_tabs is not None else {}
        self.contexts = []

    def __call__(self, job, live_flag, operator=False):
        now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC) + timedelta(minutes=len(self.contexts))  # each run later
        ctx = make_context(self.settings, job=job, transport=self.transport, store=self.store, now=now)
        ctx.guard.configure(live=bootstrap.resolve_live(live_flag, self.settings, operator=operator))
        ctx.live_flag = live_flag
        ctx.clients.sheets = StubSheets(ctx.guard, self.sheet_tabs)
        self.contexts.append(ctx)
        return ctx

    def run(self, *argv):
        return cli.main(list(argv), context_factory=self)

    @property
    def last(self):
        return self.contexts[-1]

    def beats(self, job):
        return [r for r in self.store.tables["heartbeats"] if r["job"] == job]


# -- parsing -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["status"], {"command": "status"}),
        (["stop"], {"command": "stop", "live": False}),
        (["start", "--live"], {"command": "start", "live": True}),
        (["mailbox", "add", "maria@meetspill.org", "--owner", "Maria Lopez", "--domain", "meetspill.org"],
         {"action": "add", "address": "maria@meetspill.org", "owner": "Maria Lopez", "domain": "meetspill.org", "daily_cap": 30}),
        (["mailbox", "pause", HANNAH, "--live"], {"action": "pause", "address": HANNAH, "live": True}),
        (["mailbox", "retire", SAM], {"action": "retire", "address": SAM}),
        (["mailbox", "check"], {"action": "check", "address": None}),
        (["unenrol", "--month", "2026-11"], {"command": "unenrol", "month": "2026-11"}),
        (["rescore"], {"command": "rescore", "live": False}),
        (["dry-run", "enrol"], {"command": "dry-run", "job": "enrol"}),
        (["run", "settings_sync", "--live"], {"command": "run", "job": "settings_sync", "live": True}),
        (["erase", "--email", "jane@acme.com"], {"command": "erase", "email": "jane@acme.com", "live": False}),
        (["test", "start", "t1-eap-opener"], {"action": "start", "test_id": "t1-eap-opener"}),
        (["test", "read", "t1-eap-opener"], {"action": "read", "test_id": "t1-eap-opener"}),
        (["settings", "sync"], {"command": "settings", "action": "sync"}),
        (["settings", "bootstrap", "--live"], {"action": "bootstrap", "live": True, "force": False}),
        (["db", "apply"], {"command": "db", "action": "apply", "live": False}),
        (["db", "apply", "--live"], {"action": "apply", "live": True}),
        (["hubspot", "setup", "--live"], {"action": "setup", "live": True}),
        (["hubspot", "ids"], {"action": "ids"}),
        (["campaigns", "ensure", "--fix"], {"action": "ensure", "fix": True}),
        (["campaigns", "show"], {"action": "show"}),
        (["suppression", "load"], {"action": "load"}),
        (["pages", "show"], {"command": "pages", "action": "show"}),
        (["schedule"], {"command": "schedule"}),
        (["scheduler"], {"command": "scheduler", "list": False}),
        (["scheduler", "--list"], {"command": "scheduler", "list": True}),
    ],
)
def test_every_command_parses(argv, expected):
    args = cli.build_parser().parse_args(argv)
    for key, value in expected.items():
        assert getattr(args, key) == value


def test_dry_run_takes_no_live_flag():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["dry-run", "enrol", "--live"])


# -- the job registry ----------------------------------------------------------------------------


def test_jobs_cover_spec9_and_the_build_additions():
    assert set(SPEC9_JOBS) <= set(cli.JOBS)
    assert set(cli.JOBS) - set(SPEC9_JOBS) == {"heartbeat_check", "suppression_load", "verify_accounts", "lookalikes",
                                               "hand_check_post", "read_pages", "apollo_enrich", "apollo_people", "lookalike_leads",
                                               "blackout", "retention"}
    assert cli.JOBS["source_universe"] == "us_outbound.sources.apollo_universe:run"
    assert cli.JOBS["apollo_signals"] == "us_outbound.sources.apollo_jobs:run"
    assert cli.JOBS["read_pages"] == "us_outbound.sources.pages:run"
    assert cli.JOBS["apollo_enrich"] == "us_outbound.sources.apollo_enrich:run"
    assert cli.JOBS["verify_accounts"] == "us_outbound.verify:run"
    assert cli.JOBS["lookalikes"] == "us_outbound.sources.lookalikes:run"
    assert cli.JOBS["lookalike_leads"] == "us_outbound.sources.lookalike_leads:run"  # Harry, 5 Oct 2026: monthly
    assert cli.JOBS["settings_sync"] == "us_outbound.settings.sync:run"
    assert cli.JOBS["score"] == "us_outbound.scoring.score:rescore"
    assert cli.JOBS["enrol"] == "us_outbound.enrol.enrol:run"
    assert cli.JOBS["mailbox_health"] == "us_outbound.registry.mailboxes:mailbox_health"
    assert cli.JOBS["heartbeat_check"] == "us_outbound.ops.heartbeat:run"
    assert cli.JOBS["suppression_load"] == "us_outbound.suppression:load_from_hubspot"
    assert cli.JOBS["poll_replies"] == "us_outbound.replies.poll:run"
    assert cli.JOBS["sync_outcomes"] == "us_outbound.replies.outcomes:run"
    assert cli.JOBS["poll_approvals"] == "us_outbound.replies.desk:poll_approvals"
    assert cli.JOBS["hubspot_readback"] == "us_outbound.crm.readback:hubspot_readback"
    assert cli.JOBS["kill_rules"] == "us_outbound.learn.kill_rules:run"
    assert cli.JOBS["blackout"] == "us_outbound.registry.blackout:run"  # Harry, 7 Oct 2026: pause over blackouts
    assert cli.JOBS["daily_post"] == "us_outbound.learn.daily_post:run"
    assert cli.JOBS["hand_check_post"] == "us_outbound.enrol.hand_check:post"
    assert cli.JOBS["retention"] == "us_outbound.ops.retention:run"  # SPEC 6 and 13
    assert set(hb.EXPECTED) == set(cli.JOBS) - {"score"}


def test_the_google_cloud_commands_are_gone():
    for argv in (["bq", "apply"], ["deploy", "plan"]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(argv)


def test_schedule_lists_every_job_with_its_next_run(capsys):
    assert cli.main(["schedule"]) == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines() if line.split() and line.split()[0] in cli.JOBS}
    assert set(lines) == set(cli.JOBS)
    syncs = [line for line in out.splitlines() if line.startswith("settings_sync ")]
    assert len(syncs) == 2 and all("--live" in line for line in syncs)  # 02:00 daily, and 11:30 on weekdays
    assert "0 2 * * *" in syncs[0] and "30 11 * * 1-5" in syncs[1]
    assert "30 8 * * 1" in lines["monday_readout"] and "on demand only" in lines["score"]
    assert re.search(r"(BST|GMT)$", lines["heartbeat_check"])
    assert "live_sending = yes" in out
    assert cli.main(["scheduler", "--list"]) == 0
    assert capsys.readouterr().out == out



def test_db_apply_prints_the_statements_unless_live(capsys, monkeypatch):
    from us_outbound.ops import ddl

    calls = []

    def fake_apply(guard, dsn, *, dry_run=True, connect=None):
        calls.append((guard, dsn, dry_run))
        return ["CREATE SCHEMA IF NOT EXISTS us_outbound", "CREATE TABLE IF NOT EXISTS us_outbound.accounts (a text);"]

    monkeypatch.setattr(ddl, "apply", fake_apply)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["db", "apply"]) == 0  # dry-run needs no database
    out = capsys.readouterr()
    assert out.out.strip() == ("CREATE SCHEMA IF NOT EXISTS us_outbound;\n\n"
                               "CREATE TABLE IF NOT EXISTS us_outbound.accounts (a text);")
    assert "nothing run" in out.err and calls[-1][1:] == ("", True)

    assert cli.main(["db", "apply", "--live"]) == 2  # live needs DATABASE_URL
    assert "set DATABASE_URL" in capsys.readouterr().err and len(calls) == 1

    monkeypatch.setenv("DATABASE_URL", "postgresql://us_outbound@db.test:5432/railway")
    assert cli.main(["db", "apply", "--live"]) == 0
    guard, dsn, dry_run = calls[-1]
    assert (dsn, dry_run) == ("postgresql://us_outbound@db.test:5432/railway", False)
    assert isinstance(guard, Guard) and guard.job == "db_apply"
    assert "Applied 2 statements" in capsys.readouterr().out


def test_scheduler_command_reads_the_parallel_limit(monkeypatch, capsys):
    from us_outbound.ops import scheduler

    seen = []
    monkeypatch.setattr(scheduler.Scheduler, "run", lambda self: seen.append(self.max_parallel) or 0)
    monkeypatch.setenv("US_OUTBOUND_MAX_PARALLEL", "3")
    assert cli.main(["scheduler"]) == 0 and seen == [3]
    monkeypatch.setenv("US_OUTBOUND_MAX_PARALLEL", "none")
    assert cli.main(["scheduler"]) == 2
    assert "US_OUTBOUND_MAX_PARALLEL" in capsys.readouterr().err


# -- dry-run and live ------------------------------------------------------------------------------


def test_jobs_are_dry_run_by_default():
    h = Harness(LIVE_SETTINGS)
    assert h.run("run", "suppression_load") == 0
    assert h.last.dry_run
    [beat] = h.beats("suppression_load")
    assert beat["status"] == "ok" and beat["dry_run"] is True


def test_live_flag_without_live_sending_stays_dry(capsys):
    h = Harness(SETTINGS)
    assert h.run("run", "suppression_load", "--live") == 0
    assert h.last.dry_run
    out = "\n".join(x for x in capsys.readouterr().out.splitlines() if not x.startswith('{"event"'))
    assert out.startswith("Running dry: live_sending is no in the settings in force (synced never). If you have "
                          "just set it to yes on the sheet, run `us-outbound sync` and try again.")


def test_live_needs_both_the_flag_and_the_setting():
    h = Harness(LIVE_SETTINGS)
    assert h.run("run", "suppression_load", "--live") == 0
    assert h.last.live
    assert h.run("dry-run", "suppression_load") == 0
    assert h.last.dry_run


@pytest.mark.parametrize("job, message", [
    ("verify_in_clay", "not built yet (phase 1)"),
    ("no_such_job", "unknown job"),
])
def test_unbuilt_jobs_exit_non_zero(job, message, capsys):
    h = Harness()
    assert h.run("run", job) == 2
    assert h.run("dry-run", job) == 2
    assert message in capsys.readouterr().err
    assert h.contexts == []


def failing_job(ctx):
    raise RuntimeError("HubSpot is down")


def test_a_failing_job_exits_one_and_leaves_an_error_heartbeat(capsys, monkeypatch):
    h = Harness()
    monkeypatch.setitem(cli.JOBS, "suppression_load", "tests.test_cli:failing_job")
    assert h.run("run", "suppression_load") == 1
    [beat] = h.beats("suppression_load")
    assert beat["status"] == "error" and "HubSpot is down" in beat["error"]
    assert "RuntimeError" in capsys.readouterr().err


def key_error_job(ctx):
    return {}["id"]


def bad_input_job(ctx):
    raise ValueError("bad input")


def sigterm_job(ctx):
    import os
    import signal

    os.kill(os.getpid(), signal.SIGTERM)  # what the scheduler sends at a timeout
    raise AssertionError("not reached: the handler raises first")


def test_sigterm_during_a_job_leaves_an_error_heartbeat_not_a_running_one(capsys, monkeypatch):
    import signal

    h = Harness()
    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setitem(cli.JOBS, "suppression_load", "tests.test_cli:sigterm_job")
    assert h.run("run", "suppression_load") == 143
    [beat] = h.beats("suppression_load")
    assert beat["status"] == "error" and "Terminated: stopped by SIGTERM" in beat["error"]
    assert "stopped by SIGTERM" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) is before  # put back after the run


def test_a_bug_exits_one_with_its_traceback_and_bad_input_exits_two(capsys, monkeypatch):
    h = Harness()
    monkeypatch.setitem(cli.JOBS, "suppression_load", "tests.test_cli:key_error_job")
    assert h.run("run", "suppression_load") == 1
    err = capsys.readouterr().err
    assert "Traceback" in err and "KeyError" in err
    monkeypatch.setitem(cli.JOBS, "suppression_load", "tests.test_cli:bad_input_job")
    assert h.run("run", "suppression_load") == 2
    err = capsys.readouterr().err
    assert "bad input" in err and "Traceback" not in err


def test_a_broken_job_import_surfaces_as_an_error(capsys, monkeypatch):
    monkeypatch.setitem(cli.JOBS, "enrol", "us_outbound.enrol.not_there_yet:run")
    assert Harness().run("run", "enrol", "--live") == 1
    assert "ModuleNotFoundError" in capsys.readouterr().err


def test_unusable_settings_refuse_and_leave_a_heartbeat(capsys):
    store = MemoryStore(Guard())

    def factory(job, live_flag, operator=False):
        raise bootstrap.SettingsUnusable({"Signals": ["row 3: bad"]}, store)

    assert cli.main(["run", "mailbox_health"], context_factory=factory) == 2
    [beat] = store.tables["heartbeats"]
    assert beat["job"] == "mailbox_health" and beat["status"] == "error"
    assert "settings are unusable (Signals)" in capsys.readouterr().err


# -- stop / start ------------------------------------------------------------------------------------


def test_stop_pauses_every_us_campaign_and_enrolment():
    h = Harness()
    hannah = h.instantly.add_campaign(C_HANNAH, status=1)
    harry = h.instantly.add_campaign(C_HARRY, status=0)
    eu = h.instantly.add_campaign("EU Outbound – Anna", status=1)
    assert h.run("stop") == 0  # dry-run: enrolment stops, campaigns do not
    assert heartbeats.enrolment_paused(h.store) is not None
    assert hannah["status"] == 1
    assert h.run("stop", "--live") == 0  # operator command: --live alone
    assert hannah["status"] == 2 and harry["status"] == 0 and eu["status"] == 1
    assert not [r for r in h.transport.requests if f"/{eu['id']}" in r.url]
    assert [b["status"] for b in h.beats(heartbeats.OPERATOR_STOP)] == ["ok", "ok"]


def test_campaigns_show_prints_each_owner_campaign_as_instantly_holds_it(capsys):
    h = Harness(SETTINGS)
    h.instantly.standard(C_HANNAH, [HANNAH], 30)
    assert h.run("campaigns", "show") == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"campaign"')]
    by = {line["campaign"]: line["instantly"] for line in lines}
    assert by[C_HANNAH]["name"] == C_HANNAH and by[C_SAM] is None  # Sam's is not created yet
    assert not [r for r in h.transport.requests if r.method != "GET"]  # read-only


def test_start_needs_live_sending_and_checks_drift():
    h = Harness(SETTINGS)
    for name, accounts, limit in ((C_HANNAH, [HANNAH], 30), (C_SAM, [SAM], 30), (C_HARRY, [HARRY, HARRY2], 60)):
        h.instantly.standard(name, accounts, limit)
    from tests.test_ramp import past_ramp

    past_ramp(h.store, SETTINGS.mailboxes, datetime(2026, 10, 27, 12, 0, tzinfo=UTC))  # campaigns at full caps
    assert h.run("stop", "--live") == 0
    assert h.run("start", "--live") == 0  # live_sending is no: stays dry
    assert all(c["status"] == 2 for c in h.instantly.campaigns.values())
    assert heartbeats.enrolment_paused(h.store) is not None

    live = Harness(LIVE_SETTINGS)
    live.store, live.instantly, live.transport = h.store, h.instantly, h.transport
    live.contexts = list(h.contexts)  # keep the clock moving on
    h.instantly.by_name(C_SAM)["open_tracking"] = True
    assert live.run("start", "--live") == 2  # drift: refused
    assert h.instantly.by_name(C_HANNAH)["status"] == 2
    h.instantly.by_name(C_SAM)["open_tracking"] = False
    assert live.run("start", "--live") == 0
    assert all(c["status"] == 1 for c in h.instantly.campaigns.values())
    assert heartbeats.enrolment_paused(h.store) is None


# -- operator commands ----------------------------------------------------------------------------------


def test_mailbox_commands_run_through_the_cli():
    from us_outbound.registry.mailboxes import _row

    h = Harness(sheet_tabs={"Mailboxes": [_row(m) for m in SETTINGS.mailboxes]})
    h.instantly.standard(C_SAM, [SAM], 30)
    assert h.run("mailbox", "pause", SAM, "--live") == 0
    assert next(r for r in h.sheet_tabs["Mailboxes"] if r["address"] == SAM)["status"] == "Paused"
    assert h.instantly.by_name(C_SAM)["status"] == 2
    assert h.beats("mailbox_pause")[0]["status"] == "ok"
    assert h.run("mailbox", "add", "x@meetspill.org") == 2  # needs --owner
    assert h.run("mailbox", "pause") == 2  # needs an address


def test_mailbox_check_fix_live_sets_the_sender_names(capsys):
    """Harry, 5 Oct 2026: the From name is the owner's full name. check reports it; --fix --live sets it."""
    h = Harness(sheet_tabs={})
    h.instantly.accounts[HANNAH].update(first_name="Hannah", last_name="at Spill")

    def name_patches():  # the ramp's daily-limit PATCHes are mailbox_health's own (test_registry, test_ramp)
        return [(r.url.rsplit("/", 1)[1], r.json) for r in h.transport.requests
                if r.method == "PATCH" and "/accounts/" in r.url and "daily_limit" not in r.json]

    assert h.run("mailbox", "check") == 0 and h.run("mailbox", "check", "--fix") == 0  # dry-run by default
    assert name_patches() == [] and h.instantly.accounts[HANNAH]["last_name"] == "at Spill"
    assert h.run("mailbox", "check", "--fix", "--live") == 0  # an operator command: --live alone
    assert name_patches() == [(HANNAH, {"first_name": "Hannah", "last_name": "Spalding"})]
    assert h.instantly.accounts[HANNAH]["last_name"] == "Spalding"


def test_unenrol_removes_that_months_leads_only(capsys):
    h = Harness()
    c = h.instantly.add_campaign(C_HANNAH)
    nov, dec = h.instantly.add_lead(c["id"], "a@x.com"), h.instantly.add_lead(c["id"], "b@y.com")
    h.store.insert("contacts", [
        {"contact_id": "k1", "enrolment_month": "2026-11", "instantly_campaign": C_HANNAH, "instantly_lead_id": nov["id"]},
        {"contact_id": "k2", "enrolment_month": "2026-12", "instantly_campaign": C_HANNAH, "instantly_lead_id": dec["id"]},
    ])
    assert h.run("unenrol", "--month", "2026-11") == 0
    assert set(h.instantly.leads) == {nov["id"], dec["id"]}  # dry-run
    assert h.run("unenrol", "--month", "2026-11", "--live") == 0
    assert set(h.instantly.leads) == {dec["id"]}
    assert h.run("unenrol", "--month", "Nov 2026") == 2


def test_erase_through_the_cli_lists_the_manual_steps(capsys):
    h = Harness()
    assert h.run("erase", "--email", "jane@acme.com") == 0
    out = capsys.readouterr().out
    assert "Manual steps for Harry" in out and "Clay" in out and "jane@acme.com" not in out
    assert h.beats("erase")[0]["status"] == "ok"


def test_status_prints_jobs_and_mailboxes(capsys):
    h = Harness()
    h.store.upsert("heartbeats", [{"run_id": "r1", "job": "settings_sync", "started_at": datetime(2026, 10, 27, 2, tzinfo=UTC),
                                   "finished_at": datetime(2026, 10, 27, 2, 1, tzinfo=UTC), "status": "ok", "dry_run": True}])
    assert h.run("status") == 0
    out = capsys.readouterr().out
    assert any(line.startswith("Switches: live_sending no · auto_send no (dry: ") for line in out.splitlines())
    assert "  1 job ok" in out and "Not run yet: " in out and "poll_replies" in out  # settings_sync ran ok
    assert HANNAH in out and "Campaigns:" in out
    assert "Credit budgets this month (UK time):" in out and "Enrolment this week (Monday to Sunday, UK time):" in out
    assert "Today: 0, limited by ready accounts" in out
    assert "Apollo: 0 of 2,000 credits used this month (0%)" in out
    assert "Retention (00:40 UK daily): not run yet" in out  # ops/retention.py


def test_retention_runs_by_hand_and_shows_in_status(capsys):
    h = Harness()
    assert h.run("run", "retention", "--live") == 0  # live_sending is no: it stays dry
    out = capsys.readouterr().out
    assert "Running dry: live_sending is no" in out and '"leads"' in out
    assert h.beats("retention")[0]["status"] == "ok" and h.beats("retention")[0]["dry_run"] is True
    assert h.run("status") == 0
    assert "Retention: last run Tue 27 Oct 12:00 UK (dry-run): nothing due" in capsys.readouterr().out


# -- copy tests -------------------------------------------------------------------------------------------


def _tests_tab(**over):
    row = {"test_id": "t1", "hypothesis": "EAP opener wins", "version_a": "eap-v1", "version_b": "general-v1",
           "accounts_per_version": "400", "start_date": "", "read_date": "2026-12-15", "decision_rule": "reply rate",
           "status": "planned", "result": ""}
    return {**row, **over}


def _approved():
    from tests.test_render import copy_row

    return (copy_row("eap-v1", "Marketing & Creative Agencies"), copy_row("general-v1", "General"))


def test_test_start_sets_running_and_refuses_a_second():
    settings = dataclasses.replace(SETTINGS, copy=_approved())
    h = Harness(settings, sheet_tabs={"Tests": [_tests_tab(), _tests_tab(test_id="t2")]})
    assert h.run("test", "start", "t1") == 0  # dry-run
    assert h.sheet_tabs["Tests"][0]["status"] == "planned"
    assert h.run("test", "start", "t1", "--live") == 0
    assert h.sheet_tabs["Tests"][0]["status"] == "running" and h.sheet_tabs["Tests"][0]["start_date"] == "2026-10-27"
    assert h.run("test", "start", "t2", "--live") == 2
    assert h.sheet_tabs["Tests"][1]["status"] == "planned"


def test_test_start_needs_a_read_date_and_approved_copy(capsys):
    h = Harness(SETTINGS, sheet_tabs={"Tests": [_tests_tab()]})
    assert h.run("test", "start", "t1", "--live") == 2
    assert "not approved" in capsys.readouterr().err
    h2 = Harness(dataclasses.replace(SETTINGS, copy=_approved()), sheet_tabs={"Tests": [_tests_tab(read_date="")]})
    assert h2.run("test", "start", "t1", "--live") == 2
    assert "pre-register" in capsys.readouterr().err


def test_test_start_checks_the_row_as_it_will_be_written(capsys):
    """9 Oct 2026: a read_date of 2026-9-30 passed a comparison of text; the sheet said running and the next
    settings_sync refused the Tests tab, so the test the operator was told is running was not."""
    settings = dataclasses.replace(SETTINGS, copy=_approved())
    for bad, says in (("2026-9-30", "read_date"), ("30/11/2026", "read_date"), ("2026-10-01", "must be after")):
        h = Harness(settings, sheet_tabs={"Tests": [_tests_tab(read_date=bad)]})
        assert h.run("test", "start", "t1", "--live") == 2
        err = capsys.readouterr().err
        assert "would not pass the checks settings_sync makes once it is running" in err and says in err
        assert h.sheet_tabs["Tests"][0]["status"] == "planned"


def _read_world(read_date: date):
    """Four accounts in t1, step 1 on Mon 21 Sep (their windows closed on 19 Oct); the Harness's now is 27 Oct."""
    test = CopyTest("t1", "h", "eap-v1", "general-v1", 400, "running", date(2026, 9, 14), read_date, "reply rate")
    h = Harness(dataclasses.replace(SETTINGS, tests=(test,)))
    t0 = datetime(2026, 9, 21, 14, tzinfo=UTC)
    contacts, events = [], []
    for i in range(4):
        version = "eap-v1" if i < 2 else "general-v1"
        contacts.append({"contact_id": f"k{i}", "account_id": f"a{i}", "test_id": "t1", "copy_version": version})
        events.append({"event_id": f"s{i}", "contact_id": f"k{i}", "account_id": f"a{i}", "type": "sent", "step": 1, "occurred_at": t0})
    events += [
        {"event_id": "r0", "contact_id": "k0", "account_id": "a0", "type": "replied", "reply_class": "positive",
         "occurred_at": t0 + timedelta(days=2)},
        {"event_id": "r2", "contact_id": "k2", "account_id": "a2", "type": "replied", "reply_class": "out_of_office",
         "occurred_at": t0 + timedelta(days=1)},
        {"event_id": "r3", "contact_id": "k3", "account_id": "a3", "type": "replied", "reply_class": "objection",
         "occurred_at": t0 + timedelta(days=30)},  # outside the 28-day window
        {"event_id": "b1", "contact_id": "k1", "account_id": "a1", "type": "bounced", "step": 1, "occurred_at": t0},
    ]
    h.store.insert("contacts", contacts)
    h.store.insert("events", events)
    return h


def test_test_read_reports_reply_rate_per_version_at_the_read_date(capsys):
    h = _read_world(date(2026, 10, 26))
    assert h.run("test", "read", "t1") == 0
    out = capsys.readouterr().out
    result = cli.read_test(h.last, "t1")
    assert result["versions"]["eap-v1"] == {"accounts": 2, "delivered": 1, "replied": 1, "positive": 1, "meetings": 0,
                                            "edited": 0, "reply_rate": 1.0, "positive_rate": 1.0, "meeting_rate": 0.0}
    assert result["versions"]["general-v1"]["delivered"] == 2 and result["versions"]["general-v1"]["reply_rate"] == 0.0
    assert result["look"]["final"] and "Read at look 1 (the read date)" in out and "Harry writes the result" in out
    assert h.store.tables["heartbeats"] == []  # a read writes nothing


def test_test_read_refuses_before_the_first_look(capsys):
    """Harry, 6 Oct 2026: no peeking. Before a pre-registered look the read says how far the arms have got, and no
    reply."""
    h = _read_world(date(2026, 12, 15))
    assert h.run("test", "read", "t1") == 2
    err = capsys.readouterr().err
    assert "reached no pre-registered look" in err and "the first is look 1 (the read date): Tue 15 Dec 2026" in err
    assert "eap-v1 1 emailed (1 window closed), general-v1 2 emailed (2 windows closed)" in err
    assert "replied" not in err and "%" not in err


# -- bootstrap ---------------------------------------------------------------------------------------------


ENV = {"DATABASE_URL": "postgresql://us_outbound@db.test:5432/railway", "US_OUTBOUND_SETTINGS_SHEET_ID": "sheet-1"}


def _build(job, live_flag, settings, operator=False, env=ENV):
    return bootstrap.build_context(
        job, live_flag, operator=operator, env=env, store=MemoryStore(Guard()),
        load_settings=lambda store: (settings, {} if settings else {"Signals": ["row 3: bad"]}),
        transport=FakeTransport(), google_credentials=object(),
    )


def test_build_context_applies_the_live_rule_and_boundaries():
    ctx = _build("mailbox_health", True, SETTINGS)
    assert ctx.dry_run and ctx.guard.bounds.settings_sheet_id == "sheet-1"
    assert HANNAH in ctx.guard.bounds.registry_addresses and ctx.store.guard is ctx.guard
    assert _build("mailbox_health", True, LIVE_SETTINGS).live
    assert not _build("mailbox_health", False, LIVE_SETTINGS).live
    assert _build("mailbox_pause", True, SETTINGS, operator=True).live


def test_build_context_refuses_unusable_settings_except_for_sync():
    with pytest.raises(bootstrap.SettingsUnusable, match="Signals"):
        _build("mailbox_health", False, None)
    ctx = _build("settings_sync", True, None)
    assert ctx.settings.general == General() and ctx.dry_run  # defaults: live_sending is no
    assert _build("settings_load", True, None, operator=True).live  # it repairs the sheet: --live alone
    with pytest.raises(bootstrap.ConfigError, match="DATABASE_URL"):
        _build("status", False, SETTINGS, env={})


def test_no_http_library_outside_the_http_client():
    from pathlib import Path

    root = Path(cli.__file__).resolve().parents[1]
    mine = ["ops/cli.py", "ops/heartbeat.py", "base/heartbeats.py", "ops/erase.py", "ops/bootstrap.py", "ops/schedule.py", "ops/scheduler.py",
            "ops/retention.py", "registry/mailboxes.py", "suppression.py", "crm/hubspot_writes.py", "__main__.py"]
    for rel in mine:
        text = (root / rel).read_text()
        assert not re.search(r"^\s*(import|from)\s+(requests|httpx|urllib)", text, re.M), rel


# -- leads in flight (Harry, 7 Oct 2026) ---------------------------------------------------------------------------


def held_world() -> Harness:
    """Live settings, every campaign standard and active but Sam's step 1 worded as before, and one lead in it."""
    h = Harness(LIVE_SETTINGS)
    for name, accounts, limit in ((C_HANNAH, [HANNAH], 30), (C_SAM, [SAM], 30), (C_HARRY, [HARRY, HARRY2], 60)):
        h.instantly.standard(name, accounts, limit, status=2)
    from tests.test_ramp import past_ramp

    past_ramp(h.store, SETTINGS.mailboxes, datetime(2026, 10, 27, 12, 0, tzinfo=UTC))
    variant = h.instantly.by_name(C_SAM)["sequences"][0]["steps"][0]["variants"][0]
    variant["body"] = variant["body"].replace("Not relevant?", "To stop hearing from us,")
    h.store.insert("contacts", [{"contact_id": "con-1", "account_id": "acc-1", "instantly_campaign": C_SAM,
                                 "instantly_lead_id": "L1", "enrolled_at": datetime(2026, 10, 26, 15, tzinfo=UTC)}])
    return h


def test_start_goes_ahead_over_drift_held_for_leads_in_flight(capsys):
    h = held_world()
    assert h.run("start", "--live") == 0
    assert all(c["status"] == 1 for c in h.instantly.campaigns.values())
    assert "Campaign drift held, US Outbound – Sam Jackson: steps.1 would change 1 lead in flight." in capsys.readouterr().out
    h.instantly.by_name(C_SAM)["open_tracking"] = True  # drift that is not held still refuses
    assert h.run("start", "--live") == 2
    assert "US Outbound – Sam Jackson: open_tracking. Fix with" in capsys.readouterr().err


def test_campaigns_ensure_fix_in_flight_applies_it_to_them_and_logs_it(capsys):
    h = held_world()
    assert h.run("campaigns", "ensure", "--fix", "--live") == 0
    assert "Campaign drift held, US Outbound – Sam Jackson: steps.1" in capsys.readouterr().out
    assert h.store.tables["config_log"] == []
    assert h.run("campaigns", "ensure", "--fix", "--in-flight", "--live") == 0
    [row] = h.store.tables["config_log"]
    assert (row["campaign"], row["changed_keys"], row["leads_in_flight"], row["changed_by"]) == (
        C_SAM, ["steps.1"], 1, "campaigns_ensure")
    assert h.run("campaigns", "ensure", "--in-flight") == 2  # --in-flight goes with --fix
    assert "--in-flight goes with ensure --fix" in capsys.readouterr().err


def test_operate_refuses_in_the_jobs_words_and_lets_a_bug_through(capsys):
    """cli._operate (9 Oct 2026): a LookupError or ValueError from the job is the command's refusal (exit 2); a
    KeyError is a bug, so main prints its traceback instead of passing it off as a refusal."""
    ctx = make_context(SETTINGS, job="x")

    def factory(job, live, operator=False):
        return ctx

    def refuses(c):
        raise LookupError("no send approval 'zz'")

    with pytest.raises(cli.Refused, match="no send approval 'zz'"):
        cli._operate(factory, "x", False, refuses)
    with pytest.raises(KeyError):
        cli._operate(factory, "x", False, lambda c: {}["missing"])
    _, summary = cli._operate(factory, "x", False, lambda c: {"done": 1}, dry_note="nothing was changed.")
    out = capsys.readouterr().out
    assert summary == {"done": 1} and '"done": 1' in out and "Dry-run: nothing was changed." in out
