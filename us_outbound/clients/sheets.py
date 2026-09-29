"""Google Sheets API v4 client for "US Outbound – Settings" (SPEC 3, 5).

REST through HttpClient (system "sheets"), so every read and write passes the guard,
which allows only the settings sheet. Writes (append, update, create) happen only when
live. Values are read as FORMATTED_VALUE text and written RAW, so nothing typed into a
cell is ever parsed as a formula.

Auth: google-auth credentials, refreshed when not valid. With credentials=None a fixed
token is sent (tests); production passes real credentials through Clients.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound.clients.guard import SETTINGS_SHEET_TITLE, Op
from us_outbound.clients.http import HttpClient
from us_outbound.logs import log

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _quote(text: str) -> str:
    """Percent-encode one URL path segment (tab names hold spaces and en dashes)."""
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in text.encode())


def a1_tab(tab: str) -> str:
    """A tab name quoted for A1 notation: General -> 'General', Harry's -> 'Harry''s'."""
    return "'" + tab.replace("'", "''") + "'"


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    n = index + 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def rows_to_dicts(values: Sequence[Sequence[Any]]) -> list[dict[str, str]]:
    """First row is the header; every value a str ("" if blank); fully blank rows skipped."""
    if not values:
        return []
    headers = [str(h).strip() for h in values[0]]
    out = []
    for raw in values[1:]:
        cells = [str(v) if v is not None else "" for v in raw]
        if not any(c.strip() for c in cells):
            continue
        cells += [""] * (len(headers) - len(cells))
        out.append({h: cells[i] for i, h in enumerate(headers) if h})
    return out


def _cell(value: str, bold: bool = False) -> dict:
    cell: dict[str, Any] = {"userEnteredValue": {"stringValue": value}}
    if bold:
        cell["userEnteredFormat"] = {"textFormat": {"bold": True}}
    return cell


class Sheets(HttpClient):
    system = "sheets"
    base_url = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, guard, transport, credentials: Any = None, token: str = "test"):
        super().__init__(guard, transport, token)
        self.credentials = credentials

    def headers(self) -> dict[str, str]:
        creds = self.credentials
        if creds is not None:
            if not creds.valid:
                import google.auth.transport.requests

                creds.refresh(google.auth.transport.requests.Request())
            self.token = creds.token
        return super().headers()

    # -- reads -----------------------------------------------------------------

    def _values(self, sheet_id: str, a1: str) -> list[list[Any]]:
        body = self.request(
            "GET",
            f"/{sheet_id}/values/{_quote(a1)}",
            Op("values.get", target=sheet_id, detail={"range": a1}),
            params={"valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS"},
        ) or {}
        return body.get("values", [])

    def read_tabs(self, sheet_id: str, tabs: Sequence[str]) -> dict[str, list[dict[str, str]]]:
        """Every row of each tab as {header: text}. A tab missing from the sheet raises ApiError (400)."""
        tabs = list(tabs)
        body = self.request(
            "GET",
            f"/{sheet_id}/values:batchGet",
            Op("values.batchGet", target=sheet_id, detail={"tabs": tabs}),
            params={
                "ranges": [a1_tab(t) for t in tabs],
                "valueRenderOption": "FORMATTED_VALUE",
                "majorDimension": "ROWS",
            },
        ) or {}
        ranges = body.get("valueRanges", [])
        # valueRanges come back in the order requested.
        return {tab: rows_to_dicts(ranges[i].get("values", []) if i < len(ranges) else []) for i, tab in enumerate(tabs)}

    # -- writes ----------------------------------------------------------------

    def append_rows(self, sheet_id: str, tab: str, rows: Sequence[Mapping[str, str]]) -> None:
        """Append rows under the tab's table, each value placed by the tab's header row."""
        if not rows:
            return
        header_rows = self._values(sheet_id, f"{a1_tab(tab)}!1:1")
        headers = [str(h).strip() for h in (header_rows[0] if header_rows else [])]
        unknown = sorted({k for r in rows for k in r} - set(headers))
        if unknown:
            raise ValueError(f"columns {unknown} are not on the {tab} tab")
        values = [[str(r.get(h, "")) for h in headers] for r in rows]
        a1 = f"{a1_tab(tab)}!A1"
        self.request(
            "POST",
            f"/{sheet_id}/values/{_quote(a1)}:append",
            Op("values.append", target=sheet_id, write=True, detail={"tab": tab, "rows": len(values)}),
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"range": a1, "majorDimension": "ROWS", "values": values},
        )

    def update_cell(self, sheet_id: str, tab: str, match: Mapping[str, str], column: str, value: str) -> bool:
        """Set one cell in the row whose `match` columns equal the given text.

        Returns False if no row matches, True if one does (in dry-run the write is then
        skipped). More than one matching row raises ValueError rather than guess.
        """
        values = self._values(sheet_id, a1_tab(tab))
        if not values:
            return False
        headers = [str(h).strip() for h in values[0]]
        for col in (column, *match):
            if col not in headers:
                raise ValueError(f"column {col!r} is not on the {tab} tab")

        def cell(row: Sequence[Any], col: str) -> str:
            i = headers.index(col)
            return str(row[i]).strip() if i < len(row) and row[i] is not None else ""

        hits = [n for n, row in enumerate(values[1:], start=2) if all(cell(row, k) == str(v).strip() for k, v in match.items())]
        if not hits:
            return False
        if len(hits) > 1:
            raise ValueError(f"{len(hits)} rows on the {tab} tab match {dict(match)}")
        a1 = f"{a1_tab(tab)}!{column_letter(headers.index(column))}{hits[0]}"
        self.request(
            "PUT",
            f"/{sheet_id}/values/{_quote(a1)}",
            Op("values.update", target=sheet_id, write=True, detail={"tab": tab, "column": column, "row": hits[0]}),
            params={"valueInputOption": "RAW"},
            json={"range": a1, "majorDimension": "ROWS", "values": [[str(value)]]},
        )
        return True

    def create_settings_sheet(
        self, tabs: Mapping[str, Sequence[Mapping[str, str]]], columns: Mapping[str, Sequence[str]]
    ) -> str | None:
        """Create "US Outbound – Settings" with one tab per `columns` entry, header row bold and frozen.

        Headers and rows are written in the create call itself, so no second call has to
        reach a sheet the guard does not know yet. Returns the new spreadsheet id; None in dry-run.
        """
        # PHASE0-CONFIRM: spreadsheets.create honours sheets[].data (GridData) on create.
        extra = sorted(set(tabs) - set(columns))
        if extra:
            raise ValueError(f"tabs {extra} have no columns")
        sheets = []
        for i, (tab, headers) in enumerate(columns.items()):
            rows = list(tabs.get(tab, ()))
            unknown = sorted({k for r in rows for k in r} - set(headers))
            if unknown:
                raise ValueError(f"columns {unknown} are not on the {tab} tab")
            row_data = [{"values": [_cell(h, bold=True) for h in headers]}]
            row_data += [{"values": [_cell(str(r.get(h, ""))) for h in headers]} for r in rows]
            sheets.append(
                {
                    "properties": {
                        "title": tab,
                        "index": i,
                        "gridProperties": {
                            "frozenRowCount": 1,
                            "rowCount": max(1000, len(row_data) + 100),
                            "columnCount": max(26, len(headers)),
                        },
                    },
                    "data": [{"startRow": 0, "startColumn": 0, "rowData": row_data}],
                }
            )
        body = self.request(
            "POST",
            "",
            Op("spreadsheet.create", write=True, detail={"title": SETTINGS_SHEET_TITLE, "tabs": list(columns)}),
            json={"properties": {"title": SETTINGS_SHEET_TITLE}, "sheets": sheets},
        )
        if body is None:
            return None
        log("settings_sheet_created", spreadsheet_id=body.get("spreadsheetId"), url=body.get("spreadsheetUrl"))
        return body.get("spreadsheetId")
