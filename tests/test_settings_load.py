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


# -- Industries: the label check's definitions and NAICS trims (Harry, 7 Oct 2026) -----------------------------------


def live_industries():
    """The live tab as it was on 7 Oct: no definition column, the old NAICS codes, and Harry's edits (a proof point,
    one of his own columns; and a page_faqs line, which is the build's column)."""
    sheet = default_tabs()
    old_naics = {"AI & deep tech": "5112; 513210; 5415; 518210; 541715; 541713",
                 "Games studios": "513210; 5112; 541511", "Traveltech": "5112; 513210; 5415; 518210; 5615"}
    for r in sheet["Industries"]:
        del r["definition"]
        r["naics_prefixes"] = old_naics.get(r["industry"], r["naics_prefixes"])
        if r["industry"] == "Fintech":
            r["proof_point"] = "Harry's proof point"
        if r["industry"] == "Technology & Startups":
            r["page_faqs"] = r["page_faqs"].replace("Setup takes hours", "setup takes minutes")
    return sheet


def test_a_dry_run_load_of_industries_says_every_cell_it_would_change():
    ctx = world(live_industries())
    out = loader.load(ctx, ["Industries"])
    [tab] = out["tabs"]
    assert out["dry_run"] is True and tab["new_columns"] == ["definition"]  # the real client's guard skips the write
    changes = tab["changes"]
    assert tab["cells_changed"] == len(changes) == 50 + 3 + 1  # the definitions, the trims, Harry's FAQ line
    assert ("AI & deep tech: naics_prefixes: '5112; 513210; 5415; 518210; 541715; 541713' → "
            "'5112; 513210; 5415; 518210'") in changes
    assert "Games studios: naics_prefixes: '513210; 5112; 541511' → '513210; 5112'" in changes
    assert ("Games studios: definition (a new column): blank → 'Makes and publishes video games itself; not tools, "
            "engines or platforms for game makers (Gametech), and not an agency or app studio building software for "
            "clients.'") in changes
    [faq] = [c for c in changes if "page_faqs" in c]
    # A long cell shows the part that differs, with a little either side.
    assert faq.startswith("Technology & Startups: page_faqs: '…") and "setup takes minutes" in faq
    assert "Setup takes hours" in faq and len(faq) < 300
    assert not any("proof_point" in c for c in changes) and tab["kept_from_sheet"] == ["Fintech (proof_point)"]


def test_keep_holds_the_sheets_value_in_a_column_for_one_load():
    ctx = world(live_industries(), live=True)
    out = loader.load(ctx, ["Industries"], keep=["page_faqs"])
    [tab] = out["tabs"]
    assert not any("page_faqs" in c for c in tab["changes"]) and tab["cells_changed"] == 53
    rows = {r["industry"]: r for r in ctx.clients.sheets.written["Industries"]}
    assert "setup takes minutes" in rows["Technology & Startups"]["page_faqs"]  # Harry's line stays
    assert rows["Games studios"]["naics_prefixes"] == "513210; 5112" and rows["Fintech"]["definition"]
    assert "Technology & Startups (page_faqs)" in tab["kept_from_sheet"]
    with pytest.raises(ValueError, match="--keep names a column the loaded tabs do not have: colour"):
        loader.load(ctx, ["Industries"], keep=["colour"])


def test_cell_change_shows_short_cells_whole_and_long_ones_by_the_part_that_differs():
    assert loader.cell_change("", "a definition") == "blank → 'a definition'"
    assert loader.cell_change("5112; 541511", "5112") == "'5112; 541511' → '5112'"
    long_old, long_new = "x" * 400 + " hours " + "y" * 400, "x" * 400 + " minutes " + "y" * 400
    old, new = loader.cell_change(long_old, long_new).split(" → ")
    assert old.startswith("'…xxx") and old.endswith("yyy…'") and " hours " in old and len(old) < 100
    assert new.startswith("'…xxx") and new.endswith("yyy…'") and " minutes " in new and len(new) < 100
