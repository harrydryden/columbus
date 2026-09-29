"""The us-outbound CLI (SPEC 13): parsing, dry-run by default, the job registry, stop/start, tests, bootstrap."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_registry import C_HANNAH, C_HARRY, C_SAM, HANNAH, HARRY, HARRY2, SAM, SETTINGS, FakeInstantly, StubSheets, slack_routes
from us_outbound.clients.bq import MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.ops import bootstrap, cli
from us_outbound.ops import heartbeat as hb
from us_outbound.settings.model import CopyRow, General, Settings
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
        (["bq", "apply", "--project", "spill-warehouse-test"], {"action": "apply", "project": "spill-warehouse-test"}),
        (["hubspot", "setup", "--live"], {"action": "setup", "live": True}),
        (["hubspot", "ids"], {"action": "ids"}),
        (["campaigns", "ensure", "--fix"], {"action": "ensure", "fix": True}),
        (["suppression", "load"], {"action": "load"}),
        (["deploy", "plan"], {"action": "plan"}),
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
    assert set(cli.JOBS) - set(SPEC9_JOBS) == {"heartbeat_check", "suppression_load"}
    assert cli.JOBS["settings_sync"] == "us_outbound.settings.sync:run"
    assert cli.JOBS["score"] == "us_outbound.scoring.score:rescore"
    assert cli.JOBS["enrol"] == "us_outbound.enrol.enrol:run"
    assert cli.JOBS["mailbox_health"] == "us_outbound.registry.mailboxes:mailbox_health"
    assert cli.JOBS["heartbeat_check"] == "us_outbound.ops.heartbeat:check_heartbeats"
    assert cli.JOBS["suppression_load"] == "us_outbound.suppression:load_from_hubspot"
    assert cli.JOBS["poll_replies"] == "not built yet (phase 2)"
    assert set(hb.EXPECTED) == set(cli.JOBS) - {"score"}


def test_jobs_yaml_matches_the_registry_and_spec9_schedules():
    jobs = {j["name"]: j for j in cli.read_jobs_file()}
    assert set(jobs) == set(cli.JOBS)
    crons = {
        "settings_sync": "0 2 * * *", "source_universe": "0 3 1 * *", "apollo_signals": "30 3 * * 1",
        "site_visits": "0 6 * * *", "public_signals": "0 4 * * 1", "verify_in_clay": "30 4 * * 1-5", "score": "",
        "pick_contacts": "30 5 * * 1-5", "enrol": "0 12 * * 1-5", "poll_replies": "*/15 * * * *",
        "poll_approvals": "*/5 * * * *", "hubspot_readback": "*/15 * * * *", "sync_outcomes": "0 1 * * *",
        "mailbox_health": "0 7 * * *", "kill_rules": "0 * * * *", "daily_post": "0 9 * * *",
        "monday_readout": "0 9 * * 1",
    }
    for name, cron in crons.items():
        assert jobs[name]["schedule"] == cron, name
    for name, j in jobs.items():
        assert j["args"][:2] == ["run", name]
        assert j["disabled"] is (cli.JOBS[name].startswith("not built") or name == "enrol"), name
        assert int(str(j["timeout"]).rstrip("s")) <= hb.OVERLAP_MINUTES * 60
    assert "--live" in jobs["enrol"]["args"] and "--live" not in jobs["source_universe"]["args"]


def test_deploy_plan_prints_one_line_per_job(capsys):
    assert cli.main(["deploy", "plan"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == len(cli.JOBS)
    fields = dict((line.split("\t")[0], line.split("\t")) for line in lines)
    assert fields["enrol"] == ["enrol", "0 12 * * 1-5", "true", "run,enrol,--live", "1800s", "512Mi"]
    assert fields["score"][1] == "-"


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
    assert "live_sending is not yes" in capsys.readouterr().out


def test_live_needs_both_the_flag_and_the_setting():
    h = Harness(LIVE_SETTINGS)
    assert h.run("run", "suppression_load", "--live") == 0
    assert h.last.live
    assert h.run("dry-run", "suppression_load") == 0
    assert h.last.dry_run


@pytest.mark.parametrize("job, message", [
    ("poll_replies", "not built yet (phase 2)"),
    ("kill_rules", "not built yet (phase 3)"),
    ("source_universe", "not built yet (phase 1)"),
    ("no_such_job", "unknown job"),
])
def test_unbuilt_jobs_exit_non_zero(job, message, capsys):
    h = Harness()
    assert h.run("run", job) == 2
    assert h.run("dry-run", job) == 2
    assert message in capsys.readouterr().err
    assert h.contexts == []


def test_a_listed_job_whose_module_is_missing_says_its_phase(monkeypatch, capsys):
    monkeypatch.setitem(cli.JOBS, "enrol", "us_outbound.enrol.not_there_yet:run")
    h = Harness()
    assert h.run("run", "enrol", "--live") == 2
    assert "enrol: not built yet (phase 2)" in capsys.readouterr().err
    assert "enrol" not in cli.built_jobs()


def failing_job(ctx):
    raise RuntimeError("HubSpot is down")


def test_a_failing_job_exits_one_and_leaves_an_error_heartbeat(capsys, monkeypatch):
    h = Harness()
    monkeypatch.setitem(cli.JOBS, "suppression_load", "tests.test_cli:failing_job")
    assert h.run("run", "suppression_load") == 1
    [beat] = h.beats("suppression_load")
    assert beat["status"] == "error" and "HubSpot is down" in beat["error"]
    assert "RuntimeError" in capsys.readouterr().err


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
    assert hb.enrolment_paused(h.store) is not None
    assert hannah["status"] == 1
    assert h.run("stop", "--live") == 0  # operator command: --live alone
    assert hannah["status"] == 2 and harry["status"] == 0 and eu["status"] == 1
    assert not [r for r in h.transport.requests if f"/{eu['id']}" in r.url]
    assert [b["status"] for b in h.beats(hb.OPERATOR_STOP)] == ["ok", "ok"]


def test_start_needs_live_sending_and_checks_drift():
    h = Harness(SETTINGS)
    for name, accounts, limit in ((C_HANNAH, [HANNAH], 30), (C_SAM, [SAM], 30), (C_HARRY, [HARRY, HARRY2], 60)):
        h.instantly.standard(name, accounts, limit)
    assert h.run("stop", "--live") == 0
    assert h.run("start", "--live") == 0  # live_sending is no: stays dry
    assert all(c["status"] == 2 for c in h.instantly.campaigns.values())
    assert hb.enrolment_paused(h.store) is not None

    live = Harness(LIVE_SETTINGS)
    live.store, live.instantly, live.transport = h.store, h.instantly, h.transport
    live.contexts = list(h.contexts)  # keep the clock moving on
    h.instantly.by_name(C_SAM)["open_tracking"] = True
    assert live.run("start", "--live") == 2  # drift: refused
    assert h.instantly.by_name(C_HANNAH)["status"] == 2
    h.instantly.by_name(C_SAM)["open_tracking"] = False
    assert live.run("start", "--live") == 0
    assert all(c["status"] == 1 for c in h.instantly.campaigns.values())
    assert hb.enrolment_paused(h.store) is None


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
    assert "live_sending: no" in out and "settings_sync" in out and "poll_replies" in out
    assert HANNAH in out and "Campaigns:" in out


# -- copy tests -------------------------------------------------------------------------------------------


def _tests_tab(**over):
    row = {"test_id": "t1", "hypothesis": "EAP opener wins", "version_a": "eap-v1", "version_b": "general-v1",
           "accounts_per_version": "400", "start_date": "", "read_date": "2026-12-15", "decision_rule": "reply rate",
           "status": "planned", "result": ""}
    return {**row, **over}


def _approved():
    return tuple(CopyRow(v, a, step, "s", "b", "approved", "Harry Dryden")
                 for v, a in (("eap-v1", "Upgrade the EAP"), ("general-v1", "General")) for step in (1, 2, 3, 4))


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


def test_test_read_reports_reply_rate_per_version(capsys):
    test = CopyTest("t1", "h", "eap-v1", "general-v1", 400, "running", date(2026, 10, 1), date(2026, 12, 15), "reply rate")
    h = Harness(dataclasses.replace(SETTINGS, tests=(test,)))
    t0 = datetime(2026, 10, 5, 14, tzinfo=UTC)
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
         "occurred_at": t0 + timedelta(days=30)},  # outside 21 days
        {"event_id": "b1", "contact_id": "k1", "account_id": "a1", "type": "bounced", "step": 1, "occurred_at": t0},
    ]
    h.store.insert("contacts", contacts)
    h.store.insert("events", events)
    assert h.run("test", "read", "t1") == 0
    out = capsys.readouterr().out
    result = cli.read_test(h.last, "t1")
    assert result["versions"]["eap-v1"] == {"accounts": 2, "delivered": 1, "replied": 1, "positive": 1, "meetings": 0,
                                            "reply_rate": 1.0, "positive_rate": 1.0, "meeting_rate": 0.0}
    assert result["versions"]["general-v1"]["delivered"] == 2 and result["versions"]["general-v1"]["reply_rate"] == 0.0
    assert result["early_look"] is True and "early look" in out and "Harry writes the result" in out
    assert h.store.tables["heartbeats"] == []  # a read writes nothing


# -- bootstrap ---------------------------------------------------------------------------------------------


ENV = {"US_OUTBOUND_PROJECT": "spill-warehouse-test", "US_OUTBOUND_SETTINGS_SHEET_ID": "sheet-1"}


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
    with pytest.raises(bootstrap.ConfigError, match="US_OUTBOUND_PROJECT"):
        _build("status", False, SETTINGS, env={})


def test_no_http_library_outside_the_http_client():
    from pathlib import Path

    root = Path(cli.__file__).resolve().parents[1]
    mine = ["ops/cli.py", "ops/heartbeat.py", "ops/erase.py", "ops/bootstrap.py", "registry/mailboxes.py",
            "suppression.py", "crm/hubspot_writes.py", "__main__.py"]
    for rel in mine:
        text = (root / rel).read_text()
        assert not re.search(r"^\s*(import|from)\s+(requests|httpx|urllib)", text, re.M), rel
