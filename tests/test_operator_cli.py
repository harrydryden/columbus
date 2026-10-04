"""Operator surfaces of the CLI (ops/cli.py, 4 Oct 2026): `sync`, `start --live` syncing first, the dry-run notes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from tests.test_registry import FakeInstantly, StubSheets, slack_routes
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.ops import bootstrap, cli
from us_outbound.ops import heartbeat as hb
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import General, Settings
from us_outbound.settings.sync import load_current

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)  # Mon 5 Oct, 11:00 UK


def general_value(tabs, key, value):
    for row in tabs["General"]:
        if row.get("key") == key:
            row["value"] = value
    return tabs


def live_sheet(tabs=None):
    """The default sheet with Harry's sign-off: live_sending = yes needs an approver (settings/validate.py)."""
    tabs = general_value(tabs if tabs is not None else default_tabs(), "approver_slack_ids", "U098X453UAG")
    return general_value(tabs, "live_sending", "yes")


class SheetWorld:
    """bootstrap.build_context over one store and a sheet: jobs read the settings the last sync brought in."""

    def __init__(self, sheet=None):
        self.sheet = sheet if sheet is not None else default_tabs()
        self.store = MemoryStore(Guard())
        self.transport = slack_routes(FakeTransport())
        self.instantly = FakeInstantly(self.transport)
        self.calls: list[tuple[str, bool, bool]] = []
        self.contexts = []

    def __call__(self, job, live_flag, operator=False):
        self.calls.append((job, live_flag, operator))
        settings, errors = load_current(self.store)
        if settings is None:
            if job not in bootstrap.DEFAULTS_OK:
                raise bootstrap.SettingsUnusable(errors, self.store)
            settings = Settings(general=General())
        now = NOW + timedelta(minutes=len(self.contexts))
        ctx = make_context(settings, job=job, transport=self.transport, store=self.store, now=now)
        ctx.guard.configure(live=bootstrap.resolve_live(live_flag, settings, operator=operator))
        ctx.live_flag = live_flag
        ctx.clients.sheets = StubSheets(ctx.guard, self.sheet)
        self.contexts.append(ctx)
        return ctx

    def run(self, *argv):
        return cli.main(list(argv), context_factory=self)


def report(out: str) -> list[str]:
    """The printed lines, without the JSON log lines and summaries."""
    return [line for line in out.splitlines() if line and not line.startswith(("{", "}", " ", '"', "]", "["))]


# -- sync -------------------------------------------------------------------------------------------------


def test_sync_is_settings_sync_and_says_what_is_in_force(capsys):
    w = SheetWorld(live_sheet())
    assert cli.build_parser().parse_args(["sync"]).command == "sync"
    assert cli.build_parser().parse_args(["sync", "--live"]).live is True
    assert w.run("sync") == 0
    lines = report(capsys.readouterr().out)
    assert w.calls == [("settings_sync", False, False)]
    assert lines[-2].startswith("Settings synced from the sheet just now: ") and lines[-2].endswith("rows changed.")
    assert lines[-1] == "In force now: live_sending yes, auto_send no."
    assert load_current(w.store)[0].general.live_sending is True  # a dry-run sync still brings the sheet in
    assert w.run("settings", "sync") == 0  # the same command
    lines = report(capsys.readouterr().out)
    assert lines[-2] == "Settings synced from the sheet just now: nothing had changed."
    assert [b["job"] for b in w.store.select("heartbeats")] == ["settings_sync", "settings_sync"]


def test_sync_says_when_the_sheet_is_unusable(capsys):
    sheet = default_tabs()
    sheet["General"] = [r for r in sheet["General"] if r.get("key") != "alert_channel"] + [
        {"key": "weekly_enrol_cap", "value": "lots", "note": ""}]
    w = SheetWorld(sheet)
    assert w.run("sync") == 0
    lines = report(capsys.readouterr().out)
    assert "Unusable: General" in lines[-2]
    assert lines[-1].startswith("The settings are not usable (General)")


# -- start --live syncs first -------------------------------------------------------------------------------


def test_start_live_syncs_first_so_live_sending_just_set_on_the_sheet_counts(capsys):
    w = SheetWorld()
    assert w.run("sync") == 0 and w.run("stop", "--live") == 0  # live_sending no in force; enrollment stopped
    assert hb.enrolment_paused(w.store) is not None
    capsys.readouterr()
    live_sheet(w.sheet)  # Harry's edits on the sheet, not synced yet
    assert w.run("start", "--live") == 0
    lines = report(capsys.readouterr().out)
    assert w.calls[-2:] == [("settings_sync", True, False), (hb.OPERATOR_START, True, False)]
    assert lines[0] == "Settings synced from the sheet just now: 4 rows changed."  # two keys: one closed, one opened each
    assert not any("Dry-run" in line or "Running dry" in line for line in lines)
    [start] = [b for b in w.store.select("heartbeats") if b["job"] == hb.OPERATOR_START]
    assert start["dry_run"] is False and start["status"] == "ok"
    assert hb.enrolment_paused(w.store) is None  # resumed


def test_start_live_with_the_sheet_still_at_no_runs_dry_and_says_how_to_go_live(capsys):
    w = SheetWorld()
    assert w.run("start", "--live") == 0
    lines = report(capsys.readouterr().out)
    assert lines[0].startswith("Settings synced from the sheet just now")
    assert lines[-2] == "Dry-run: campaigns were not activated and enrollment stays stopped. Nothing was sent."
    assert lines[-1].startswith("Running dry: live_sending is no in the settings in force (synced Mon 05 Oct 11:00 UK, "
                                "just now). Set it to yes on the General tab, then run `us-outbound start --live` again.")


def test_start_without_live_does_not_sync(capsys):
    w = SheetWorld()
    assert w.run("sync") == 0
    capsys.readouterr()
    assert w.run("start") == 0
    assert w.calls[-1] == (hb.OPERATOR_START, False, False) and len(w.calls) == 2
    lines = report(capsys.readouterr().out)
    assert lines[-1] == "Dry-run: campaigns were not activated and enrollment stays stopped. Nothing was sent. Needs --live."


def broken_sync(ctx):
    raise RuntimeError("the Sheets API is down")


def test_start_goes_on_when_the_sync_fails(capsys, monkeypatch):
    w = SheetWorld(live_sheet())
    assert w.run("sync") == 0
    capsys.readouterr()

    monkeypatch.setitem(cli.JOBS, "settings_sync", "tests.test_operator_cli:broken_sync")
    assert w.run("start", "--live") == 0
    lines = report(capsys.readouterr().out)
    assert lines[0] == ("Could not sync the settings first (RuntimeError: the Sheets API is down); "
                        "going on with the settings in force.")
    [start] = [b for b in w.store.select("heartbeats") if b["job"] == hb.OPERATOR_START]
    assert start["dry_run"] is False  # live_sending was already yes in force


# -- the dry-run notes ------------------------------------------------------------------------------------------


def test_a_live_job_that_runs_dry_says_how_to_bring_a_sheet_edit_in(capsys):
    w = SheetWorld()
    assert w.run("sync") == 0
    capsys.readouterr()
    assert w.run("run", "suppression_load", "--live") == 0
    lines = report(capsys.readouterr().out)
    assert lines[0] == ("Running dry: live_sending is no in the settings in force (synced Mon 05 Oct 11:00 UK). "
                        "If you have just set it to yes on the sheet, run `us-outbound sync` and try again.")
