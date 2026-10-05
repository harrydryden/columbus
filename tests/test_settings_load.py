"""`us-outbound settings load` (settings/load.py): the sign-off keys are never set from the command line
(A10), and `--take note` refreshes the notes of General keys the sheet already has (A11)."""

from __future__ import annotations

import copy

import pytest

from tests.fakes import TEST_SHEET_ID, make_context
from us_outbound.settings import load as loader
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all


class StubSheets:
    """The settings sheet: its tabs read, and what a load writes back (a live load only)."""

    def __init__(self, tabs):
        self.tabs = tabs
        self.reads: list[list[str]] = []
        self.written: dict[str, list[dict]] = {}

    def read_tabs(self, sheet_id, tabs):
        self.reads.append(list(tabs))
        return copy.deepcopy({t: self.tabs[t] for t in tabs if t in self.tabs})

    def add_tab(self, sheet_id, tab):
        self.tabs.setdefault(tab, [])

    def replace_tab(self, sheet_id, tab, headers, rows):
        self.written[tab] = [dict(r) for r in rows]


def general_row(tabs, key):
    return next(r for r in tabs["General"] if r["key"] == key)


def world(sheet=None, *, live=False):
    settings, errors = validate_all(default_tabs())
    assert settings is not None, errors
    ctx = make_context(settings, live=live, job="settings_load")
    ctx.clients.sheets = StubSheets(sheet if sheet is not None else default_tabs())
    return ctx


# -- A10: the sign-off keys ------------------------------------------------------------------------------


@pytest.mark.parametrize("key", loader.SIGN_OFF)
def test_set_refuses_every_sign_off_key_before_the_sheet_is_read(key):
    ctx = world(live=True)
    with pytest.raises(ValueError, match=f"--set cannot change {key}: it is Harry's sign-off"):
        loader.load(ctx, ["General"], {key: "yes"})
    assert ctx.clients.sheets.reads == [] and ctx.clients.sheets.written == {}
    with pytest.raises(ValueError, match="Harry's sign-off"):
        loader.plan_general(default_tabs()["General"], default_tabs()["General"], {key: "yes"})


def test_set_names_every_sign_off_key_it_refuses_and_other_keys_still_set():
    with pytest.raises(ValueError, match="--set cannot change live_sending, auto_send: they are"):
        loader.check_sets({"auto_send": "yes", "live_sending": "yes", "weekly_enrol_cap": "100"})
    assert set(loader.SIGN_OFF) == {"live_sending", "auto_send", "optout_tested", "approver_slack_ids"}
    tabs = default_tabs()
    plan = loader.plan_general(tabs["General"], default_tabs()["General"], {"clay_verification": "required"})
    assert general_row({"General": plan.rows}, "clay_verification")["value"] == "required"


def test_the_cli_refuses_it_with_the_message(capsys):
    from tests.test_cli import Harness
    from us_outbound.ops import cli

    assert cli.main(["settings", "load", "--tab", "General", "--set", "live_sending=yes", "--live"],
                    context_factory=Harness()) == 2
    assert "live_sending: it is Harry's sign-off" in capsys.readouterr().err


# -- A11: --take note --------------------------------------------------------------------------------------------


def stale_sheet():
    """Harry's sheet: his values, and notes from an older build on two keys; a note-only row."""
    sheet = default_tabs()
    general_row(sheet, "auto_send").update(value="yes", note="an old note")
    general_row(sheet, "weekly_enrol_cap").update(value="90", note="")
    sheet["General"].append({"key": "", "value": "", "note": "Harry's own line"})
    return sheet


def test_a_plain_load_never_refreshes_a_note_of_a_key_the_sheet_has():
    ctx = world(stale_sheet(), live=True)
    out = loader.load(ctx, ["General"])
    written = {"General": ctx.clients.sheets.written["General"]}
    assert general_row(written, "auto_send")["note"] == "an old note"
    assert general_row(written, "weekly_enrol_cap")["note"] == ""
    assert not any("note refreshed" in u for u in out["tabs"][0]["updated_names"])


def test_take_note_refreshes_the_notes_from_the_build_and_never_a_value():
    build = {r["key"]: r for r in default_tabs()["General"]}
    ctx = world(stale_sheet(), live=True)
    out = loader.load(ctx, ["General"], take=["note"])
    rows = ctx.clients.sheets.written["General"]
    written = {"General": rows}
    for key in ("auto_send", "weekly_enrol_cap"):
        assert general_row(written, key)["note"] == build[key]["note"]
    assert general_row(written, "auto_send")["value"] == "yes" and general_row(written, "weekly_enrol_cap")["value"] == "90"
    assert {"key": "", "value": "", "note": "Harry's own line"} in rows  # a note-only row stays as it is
    summary = out["tabs"][0]
    assert {"auto_send: note refreshed", "weekly_enrol_cap: note refreshed"} <= set(summary["updated_names"])
    assert summary["rows_after"] == summary["rows_before"]


def test_take_note_needs_the_general_tab_and_a_dry_run_says_what_would_change():
    ctx = world(stale_sheet())
    with pytest.raises(ValueError, match="--take names a column the loaded tabs do not keep: note"):
        loader.load(ctx, ["Industries"], take=["note"])
    out = loader.load(ctx, ["General"], take=["note"])
    assert out["dry_run"] is True and "auto_send: note refreshed" in out["tabs"][0]["updated_names"]
    assert ctx.guard.bounds.settings_sheet_id == TEST_SHEET_ID
