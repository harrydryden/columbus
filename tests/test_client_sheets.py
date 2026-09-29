"""Sheets client: batchGet parsing, append and cell updates, sheet creation, guard limits."""

import pytest

from tests.fakes import FakeTransport
from us_outbound.clients.guard import SETTINGS_SHEET_TITLE, Boundaries, Guard, GuardViolation
from us_outbound.clients.sheets import Sheets, a1_tab, column_letter

BASE = "https://sheets.googleapis.com/v4/spreadsheets"
SHEET = "sheet-test"


def make(live=True, credentials=None):
    transport = FakeTransport()
    guard = Guard(live=live, bounds=Boundaries(settings_sheet_id=SHEET))
    return Sheets(guard, transport, credentials=credentials), transport, guard


def test_helpers():
    assert a1_tab("General") == "'General'"
    assert a1_tab("Harry's") == "'Harry''s'"
    assert [column_letter(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]


def test_read_tabs_batch_get():
    sheets, t, _ = make(live=False)  # reads run in dry-run too
    t.route("GET", "values:batchGet", body={"valueRanges": [
        {"range": "'General'!A1:Z3", "values": [[" key ", "value", ""], ["live_sending", "no"], [], ["", "  "],
                                               ["daily_enrol_cap", "30", "stray"]]},
        {"range": "'States'!A1:Z1"},
    ]})
    got = sheets.read_tabs(SHEET, ["General", "States"])
    assert got == {
        "General": [{"key": "live_sending", "value": "no"}, {"key": "daily_enrol_cap", "value": "30"}],
        "States": [],
    }
    req = t.requests[0]
    assert req.url == f"{BASE}/{SHEET}/values:batchGet"
    assert req.params == {"ranges": ["'General'", "'States'"], "valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS"}
    assert req.headers["Authorization"] == "Bearer test"


def test_other_sheets_refused_before_any_request():
    sheets, t, _ = make()
    with pytest.raises(GuardViolation):
        sheets.read_tabs("someone-elses-sheet", ["General"])
    with pytest.raises(GuardViolation):
        sheets.append_rows("someone-elses-sheet", "Mailboxes", [{"address": "a@b.org"}])
    assert t.requests == []


def test_append_rows_orders_by_header():
    sheets, t, _ = make()
    t.route("GET", "/values/", body={"values": [["address", "domain", "owner_name", "status"]]})
    sheets.append_rows(SHEET, "Mailboxes", [{"status": "Warming", "address": "new@meetspill.org"}])
    get, post = t.requests
    assert get.url == f"{BASE}/{SHEET}/values/%27Mailboxes%27%211%3A1"
    assert post.url == f"{BASE}/{SHEET}/values/%27Mailboxes%27%21A1:append"
    assert post.params == {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
    assert post.json["values"] == [["new@meetspill.org", "", "", "Warming"]]


def test_append_rows_dry_run_and_unknown_columns():
    sheets, t, _ = make(live=False)
    t.route("GET", "/values/", body={"values": [["address", "status"]]})
    sheets.append_rows(SHEET, "Mailboxes", [{"address": "x@meetspill.org"}])
    assert t.writes() == [] and len(t.requests) == 1
    with pytest.raises(ValueError):
        sheets.append_rows(SHEET, "Mailboxes", [{"address": "x@meetspill.org", "colour": "red"}])


def test_update_cell_finds_row():
    sheets, t, _ = make()
    t.route("GET", "/values/", body={"values": [
        ["address", "domain", "status"],
        ["hannah@meetspill.org", "meetspill.org", "Warming"],
        [],
        ["sam@meetspill.org", "meetspill.org"],
    ]})
    assert sheets.update_cell(SHEET, "Mailboxes", {"address": "sam@meetspill.org"}, "status", "Active") is True
    put = t.writes()[0]
    assert put.method == "PUT"
    assert put.url == f"{BASE}/{SHEET}/values/%27Mailboxes%27%21C4"
    assert put.params == {"valueInputOption": "RAW"}
    assert put.json == {"range": "'Mailboxes'!C4", "majorDimension": "ROWS", "values": [["Active"]]}

    assert sheets.update_cell(SHEET, "Mailboxes", {"address": "nobody@meetspill.org"}, "status", "Active") is False
    with pytest.raises(ValueError):
        sheets.update_cell(SHEET, "Mailboxes", {"domain": "meetspill.org"}, "status", "Paused")
    with pytest.raises(ValueError):
        sheets.update_cell(SHEET, "Mailboxes", {"address": "sam@meetspill.org"}, "colour", "red")
    assert len(t.writes()) == 1


def test_create_settings_sheet():
    columns = {"General": ["key", "value"], "States": ["state", "active", "note"]}
    tabs = {"General": [{"key": "live_sending", "value": "no"}], "States": [{"state": "NY", "active": "yes"}]}

    sheets, t, guard = make(live=False)
    assert sheets.create_settings_sheet(tabs, columns) is None
    assert t.requests == []
    assert [c.action for c in guard.writes("sheets", sent=False)] == ["spreadsheet.create"]

    sheets, t, _ = make(live=True)
    t.route("POST", BASE, body={"spreadsheetId": "new-sheet", "spreadsheetUrl": "https://docs.google.com/x"})
    assert sheets.create_settings_sheet(tabs, columns) == "new-sheet"
    [req] = t.requests
    assert req.url == BASE
    assert req.json["properties"] == {"title": SETTINGS_SHEET_TITLE}
    general, states = req.json["sheets"]
    assert general["properties"]["title"] == "General" and states["properties"]["index"] == 1
    assert general["properties"]["gridProperties"]["frozenRowCount"] == 1
    rows = states["data"][0]["rowData"]
    assert [c["userEnteredValue"]["stringValue"] for c in rows[0]["values"]] == ["state", "active", "note"]
    assert rows[0]["values"][0]["userEnteredFormat"] == {"textFormat": {"bold": True}}
    assert [c["userEnteredValue"]["stringValue"] for c in rows[1]["values"]] == ["NY", "yes", ""]

    with pytest.raises(ValueError):
        sheets.create_settings_sheet({"Signals": []}, columns)


def test_credentials_refreshed_when_invalid():
    class FakeCredentials:
        valid = False
        token = None
        refreshed = 0

        def refresh(self, request):
            self.refreshed += 1
            self.valid, self.token = True, "ya29.fresh"

    creds = FakeCredentials()
    sheets, t, _ = make(credentials=creds)
    t.route("GET", "values:batchGet", body={"valueRanges": [{"values": [["key"]]}]})
    sheets.read_tabs(SHEET, ["General"])
    sheets.read_tabs(SHEET, ["General"])
    assert creds.refreshed == 1
    assert t.requests[1].headers["Authorization"] == "Bearer ya29.fresh"
