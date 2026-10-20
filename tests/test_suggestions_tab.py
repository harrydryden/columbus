"""The Suggestions tab (settings/suggestions.py; W3-3, 20 Oct 2026): the loops' proposals for Harry to approve, written
append-only and live only, from the Monday readout (learn/readout.contact_suggestions)."""

from __future__ import annotations

from datetime import timedelta

from tests.fakes import make_context
from us_outbound.contacts import pick
from us_outbound.learn import readout
from us_outbound.settings import suggestions as tab
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

SETTINGS, _ = validate_all(default_tabs())


class Sheet:
    """The Suggestions tab in memory: read, added, appended to; what the guard allows only."""

    def __init__(self, rows=None):
        self.rows = rows
        self.calls: list[str] = []

    def read_tabs(self, sheet_id, tabs):
        return {t: [dict(r) for r in self.rows or []] for t in tabs} if self.rows is not None else {}

    def add_tab(self, sheet_id, name):
        self.calls.append("add_tab")
        self.rows = []

    def replace_tab(self, sheet_id, name, headers, rows):
        self.calls.append("replace_tab")
        self.rows = [dict(r) for r in rows]

    def append_rows(self, sheet_id, name, rows):
        self.calls.append("append_rows")
        self.rows += [dict(r) for r in rows]


def row(i: str, status=tab.PROPOSED) -> dict:
    return {"id": i, "kind": tab.DOMAIN_ALIAS, "target": "acme.com", "text": f"{i}.com", "evidence": "e", "snapshot": "{}",
            "status": status, "applied": ""}


def test_rows_are_appended_once_and_a_decided_row_is_never_rewritten():
    ctx = make_context(SETTINGS, live=True)
    ctx.clients.sheets = sheet = Sheet()
    assert tab.write(ctx, [row("a")]) == {"rows": 1, "added": 1, "written": True}
    sheet.rows[0]["status"] = "approved"  # Harry approves it
    out = tab.write(ctx, [row("a"), row("b")])
    assert out["added"] == 1 and [r["id"] for r in sheet.rows] == ["a", "b"]
    assert sheet.rows[0]["status"] == "approved" and sheet.calls == ["replace_tab", "append_rows"]


def test_a_dry_run_writes_nothing():
    ctx = make_context(SETTINGS, live=False)
    ctx.clients.sheets = sheet = Sheet()
    assert tab.write(ctx, [row("a")]) == {"rows": 1, "written": False} and sheet.calls == []


def test_the_readout_proposes_the_week_s_contactability_rows_and_says_so():
    """Two rejected reveals at example-mail.com for acme.com (a domain_alias row) through the readout's step."""
    ctx = make_context(SETTINGS, live=True)
    ctx.clients.sheets = sheet = Sheet()
    ctx.store.insert("accounts", [{"account_id": "a1", "domain": "acme.com"}])
    ctx.store.insert("signal_events", [{
        "event_id": f"r{i}", "account_id": "a1", "source": pick.SOURCE, "fact": pick.REVEAL_FACT,
        "value": {"kept": False, "rejected_email_domain": "example-mail.com"},
        "observed_at": ctx.now - timedelta(days=i + 1)} for i in range(2)])
    out = readout.contact_suggestions(ctx)
    assert out["kinds"] == {"domain_alias": 1} and out["added"] == 1
    assert [r["id"] for r in sheet.rows] == ["domain_alias:acme.com:example-mail.com"]
    assert "1 domain_alias; 1 new this week." in out["line"]
    again = readout.contact_suggestions(ctx)  # next week: proposed again, added never twice
    assert again["added"] == 0 and len(sheet.rows) == 1
