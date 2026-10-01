"""settings_sync: versioning in the database settings table, bad tabs kept back, one Slack alert, rescore."""

import copy
import dataclasses
import sys
import types
from datetime import UTC, datetime

import pytest

import us_outbound.scoring
from tests.fakes import TEST_SHEET_ID, make_context
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.settings import sync
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.model import OPTIONAL_TABS, TABS
from us_outbound.settings.validate import FIRST_DATA_ROW, natural_key, validate_all

T1 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)
T3 = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
N_SIGNALS = len(default_tabs()["Signals"])  # 23 since the design review's Appendix A (1 Oct 2026)


class StubSheets:
    def __init__(self, tabs):
        self.tabs = tabs
        self.reads: list[tuple[str, list[str]]] = []
        self.created: list[tuple[dict, dict]] = []
        self.create_result: str | None = "new-sheet-id"

    def read_tabs(self, sheet_id, tabs):
        self.reads.append((sheet_id, list(tabs)))
        return copy.deepcopy({t: self.tabs[t] for t in tabs if t in self.tabs})

    def create_settings_sheet(self, tabs, columns):
        self.created.append((tabs, columns))
        return self.create_result


class StubSlack:
    def __init__(self, fail: bool = False):
        self.posts: list[tuple[str, str]] = []
        self.fail = fail

    def post(self, channel, text, blocks=None, thread_ts=None):
        if self.fail:
            raise RuntimeError("slack is down")
        self.posts.append((channel, text))
        return {"channel": channel, "ts": "1.0"}


@pytest.fixture(autouse=True)
def fake_score(monkeypatch):
    """Scoring is another agent's module; sync only has to call score.rescore(ctx)."""
    mod = types.ModuleType("us_outbound.scoring.score")
    mod.calls = []
    mod.rescore = lambda ctx: mod.calls.append(ctx)
    monkeypatch.setitem(sys.modules, "us_outbound.scoring.score", mod)
    monkeypatch.setattr(us_outbound.scoring, "score", mod, raising=False)
    return mod


@pytest.fixture
def sheet():
    return default_tabs()


@pytest.fixture
def ctx(sheet):
    settings, errors = validate_all(default_tabs())
    assert settings is not None, errors
    c = make_context(settings, now=T1, job="settings_sync")
    c.clients.sheets = StubSheets(sheet)
    c.clients.slack = StubSlack()
    return c


def _rows(ctx, **where):
    return ctx.store.select("settings", where)


def _in_force(ctx, tab, key):
    (row,) = _rows(ctx, tab=tab, key=key, effective_to=None)
    return row


def _row(sheet, tab, **match):
    for i, r in enumerate(sheet[tab]):
        if all(r[k] == v for k, v in match.items()):
            return i + FIRST_DATA_ROW, r
    raise KeyError(match)


def _keyed_rows(tabs) -> int:
    return sum(len(rows) for rows in tabs.values())


def test_first_sync_stores_one_version_per_row(ctx, sheet, fake_score):
    summary = sync.run(ctx)
    # The tabs every sheet has in one read, then each optional tab on its own (a missing tab is a 400).
    assert ctx.clients.sheets.reads == [(TEST_SHEET_ID, [t for t in TABS if t not in OPTIONAL_TABS]),
                                        (TEST_SHEET_ID, ["Focus"]), (TEST_SHEET_ID, ["Named accounts"])]
    rows = [r for r in _rows(ctx) if r["tab"] in TABS]
    assert len(rows) == _keyed_rows(sheet) == summary["rows_opened"]
    assert all(r["effective_from"] == r["synced_at"] == T1 and r["effective_to"] is None for r in _rows(ctx))
    # One sheet-order row per tab with rows (Overrides is empty).
    orders = {r["key"]: r["values"]["keys"] for r in _rows(ctx, tab=sync.ORDER_TAB)}
    assert set(orders) == {t for t in TABS if sheet[t]}
    assert orders["Roles"] == [r["role"] for r in sheet["Roles"]]
    lay = _in_force(ctx, "Signals", "Layoffs")
    assert lay["values"] == _row(sheet, "Signals", signal="Layoffs")[1]
    assert _in_force(ctx, "General", "live_sending")["values"] == {"key": "live_sending", "value": "no", "note": _row(sheet, "General", key="live_sending")[1]["note"]}
    assert _in_force(ctx, "Copy", "cpa-firms-v1")["values"]["industry"] == "CPA firms"
    assert _in_force(ctx, "Mailboxes", "sam@meetspill.org")
    assert summary["tabs"]["Signals"] == {"status": "synced", "added": N_SIGNALS, "changed": 0, "removed": 0, "unchanged": 0}
    assert summary["tabs"]["Overrides"]["status"] == "unchanged"
    assert summary["rejected"] == [] and summary["errors"] == 0 and summary["rescored"] is True
    assert ctx.clients.slack.posts == []
    # The rescore sees the settings now in force.
    (rescore_ctx,) = fake_score.calls
    assert len(rescore_ctx.settings.signals) == N_SIGNALS
    assert rescore_ctx.settings.versions["Signals"] == T1
    assert rescore_ctx.run_id == ctx.run_id and rescore_ctx.store is ctx.store


def test_unchanged_second_sync_writes_nothing(ctx):
    sync.run(ctx)
    before = copy.deepcopy(ctx.store.tables["settings"])
    bq_writes = len(ctx.guard.writes("db"))
    ctx.now = T2
    summary = sync.run(ctx)
    assert ctx.store.tables["settings"] == before
    assert len(ctx.guard.writes("db")) == bq_writes
    assert summary["rows_opened"] == summary["rows_closed"] == 0
    assert {v["status"] for v in summary["tabs"].values()} == {"unchanged"}


def test_changed_weight_closes_the_old_row_and_opens_a_new_one(ctx, sheet, fake_score):
    sync.run(ctx)
    _row(sheet, "Signals", signal="EAP named")[1]["weight"] = "15"
    ctx.now = T2
    summary = sync.run(ctx)
    assert summary["tabs"]["Signals"] == {"status": "synced", "added": 0, "changed": 1, "removed": 0, "unchanged": N_SIGNALS - 1}
    assert summary["rows_opened"] == summary["rows_closed"] == 1
    old, new = sorted(_rows(ctx, tab="Signals", key="EAP named"), key=lambda r: r["effective_from"])
    assert (old["effective_from"], old["effective_to"], old["values"]["weight"]) == (T1, T2, "5")
    assert (new["effective_from"], new["effective_to"], new["synced_at"], new["values"]["weight"]) == (T2, None, T2, "15")
    assert _in_force(ctx, "Signals", "Layoffs")["effective_from"] == T1  # untouched
    settings, errors = sync.load_current(ctx.store)
    assert not any(errors.values())
    assert next(s for s in settings.signals if s.signal == "EAP named").weight == 15
    assert settings.versions["Signals"] == T2 and settings.versions["General"] == T1
    assert settings.synced_at == T2
    assert next(s for s in fake_score.calls[-1].settings.signals if s.signal == "EAP named").weight == 15


def test_removed_and_added_rows(ctx, sheet):
    sync.run(ctx)
    sheet["Signals"] = [r for r in sheet["Signals"] if r["signal"] != "Layoffs"]
    sheet["Overrides"].append({"domain": "acme.com", "field": "hq_state", "value": "NY", "note": "HQ moved"})
    ctx.now = T2
    summary = sync.run(ctx)
    assert summary["tabs"]["Signals"]["removed"] == 1
    assert summary["tabs"]["Overrides"]["added"] == 1
    (gone,) = _rows(ctx, tab="Signals", key="Layoffs")
    assert gone["effective_to"] == T2
    assert _in_force(ctx, "Overrides", "acme.com|hq_state")["values"]["value"] == "NY"
    settings, _ = sync.load_current(ctx.store)
    assert "Layoffs" not in {s.signal for s in settings.signals}
    assert settings.overrides_for("acme.com") == {"hq_state": "NY"}
    # Put it back: a new version opens; the closed one stays closed.
    sheet["Signals"].append(next(r for r in default_tabs()["Signals"] if r["signal"] == "Layoffs"))
    ctx.now = T3
    sync.run(ctx)
    assert [(r["effective_from"], r["effective_to"]) for r in sorted(_rows(ctx, tab="Signals", key="Layoffs"), key=lambda r: r["effective_from"])] == [
        (T1, T2), (T3, None),
    ]


def test_bad_tab_keeps_previous_version_and_posts_one_message(ctx, sheet, fake_score):
    sync.run(ctx)
    n, row = _row(sheet, "Signals", signal="Layoffs")
    row["action"] = "Maybe"
    _row(sheet, "Signals", signal="EAP named")[1]["weight"] = "15"  # a good change on the same tab waits too
    _row(sheet, "States", state="NC")[1]["active"] = "yes"  # another tab's change still applies
    ctx.now = T2
    summary = sync.run(ctx)

    assert summary["rejected"] == ["Signals"] and summary["unusable"] == []
    assert summary["tabs"]["Signals"] == {"status": "kept_previous", "errors": 1}
    assert _in_force(ctx, "Signals", "Layoffs")["values"]["action"] == "Suppress"
    assert _in_force(ctx, "Signals", "EAP named")["values"]["weight"] == "5"
    assert _in_force(ctx, "States", "NC")["values"]["active"] == "yes"

    (post,) = ctx.clients.slack.posts
    channel, text = post
    assert channel == ctx.settings.general.alert_channel  # the client redirects to the dev channel in dry-run
    assert "rejected the Signals tab" in text and "previous version stays in force" in text
    assert f"Signals row {n} (Layoffs), action: must be one of Score, Hold, Exclude, Suppress, not 'Maybe'" in text
    assert summary["alerted"] is True

    settings = fake_score.calls[-1].settings
    assert "NC" in settings.active_states()
    assert next(s for s in settings.signals if s.signal == "Layoffs").action == "Suppress"

    # Fixed the next night: the change goes through and nothing is posted.
    row["action"] = "Suppress"
    ctx.now = T3
    summary = sync.run(ctx)
    assert summary["rejected"] == [] and len(ctx.clients.slack.posts) == 1
    assert _in_force(ctx, "Signals", "EAP named")["values"]["weight"] == "15"


def test_bad_tab_with_no_version_in_force_is_unusable(ctx, sheet, fake_score):
    _row(sheet, "Signals", signal="Layoffs")[1]["weight"] = "lots"
    summary = sync.run(ctx)
    assert summary["unusable"] == ["Signals"]
    assert summary["tabs"]["Signals"]["status"] == "unusable"
    assert _rows(ctx, tab="Signals") == []
    assert _rows(ctx, tab="General")  # the valid tabs are still stored
    (post,) = ctx.clients.slack.posts
    assert "Signals has no valid version in force, so settings are unusable" in post[1]
    assert summary["rescored"] is False and fake_score.calls == []
    settings, errors = sync.load_current(ctx.store)
    assert settings is None and errors["Signals"]


def test_new_signal_naming_an_angle_on_a_rejected_angles_tab_waits(ctx, sheet):
    sync.run(ctx)
    _row(sheet, "Angles", angle="Growing team")[1]["angle"] = "Growing fast"
    _row(sheet, "Angles", angle="Progressive employer")[1]["active"] = "sometimes"  # rejects Angles
    for r in sheet["Signals"]:
        if r["suggests_angle"] == "Growing team":
            r["suggests_angle"] = "Growing fast"
    ctx.now = T2
    summary = sync.run(ctx)
    assert summary["rejected"] == ["Signals", "Angles"]
    assert _in_force(ctx, "Signals", "Funding in the last 6 months")["values"]["suggests_angle"] == "Growing team"
    (post,) = ctx.clients.slack.posts
    assert "Angles row" in post[1] and "active: must be yes or no" in post[1]
    assert "suggests_angle: 'Growing fast' is not on the Angles tab" in post[1]
    settings, errors = sync.load_current(ctx.store)
    assert settings is not None and settings.angle("Growing team")


def test_rejected_signals_hold_back_an_angles_change_they_depend_on(ctx, sheet):
    sync.run(ctx)
    _row(sheet, "Angles", angle="Growing team")[1]["angle"] = "Growing fast"
    for r in sheet["Signals"]:
        if r["suggests_angle"] == "Growing team":
            r["suggests_angle"] = "Growing fast"
    _row(sheet, "Signals", signal="Layoffs")[1]["action"] = "Maybe"  # rejects Signals
    ctx.now = T2
    summary = sync.run(ctx)
    # The Signals in force still name "Growing team", so the renamed Angles tab cannot go in yet.
    assert summary["rejected"] == ["Signals", "Angles"] and summary["unusable"] == []
    assert _rows(ctx, tab="Angles", key="Growing fast") == []
    (post,) = ctx.clients.slack.posts
    assert "Angles row 1: kept the previous version: the Signals tab in force refers to it" in post[1]
    settings, _ = sync.load_current(ctx.store)
    assert settings.angle("Growing team") and settings.angle("Growing fast") is None


def test_general_note_rows_are_not_stored(ctx, sheet):
    sheet["General"].append({"key": "", "value": "", "note": "Section: HubSpot"})
    sync.run(ctx)
    assert _rows(ctx, tab="General", key="") == []


def test_a_failed_alert_is_raised_after_the_writes_and_rescore(ctx, sheet, fake_score):
    ctx.clients.slack = StubSlack(fail=True)
    _row(sheet, "States", state="NY")[1]["active"] = "maybe"
    with pytest.raises(RuntimeError, match="slack is down"):
        sync.run(ctx)
    assert _rows(ctx, tab="General")
    assert fake_score.calls == []  # States has no version in force yet, so settings are unusable


def test_needs_a_sheet_id(ctx):
    ctx.guard.configure(bounds=dataclasses.replace(ctx.guard.bounds, settings_sheet_id=""))
    with pytest.raises(RuntimeError, match="settings sheet id"):
        sync.run(ctx)


def test_load_current_prefers_the_newest_row_when_two_are_in_force():
    store = MemoryStore(Guard())
    tabs = default_tabs()
    rows = []
    for tab, tab_rows in tabs.items():
        for r in tab_rows:
            rows.append({"tab": tab, "key": natural_key(tab, r), "values": r, "effective_from": T1, "effective_to": None, "synced_at": T1})
    newer = dict(_row(tabs, "Signals", signal="EAP named")[1], weight="12")
    rows.append({"tab": "Signals", "key": "EAP named", "values": newer, "effective_from": T2, "effective_to": None, "synced_at": T2})
    store.insert("settings", rows)
    settings, _ = sync.load_current(store)
    assert next(s for s in settings.signals if s.signal == "EAP named").weight == 12


def _seed_two_eap_named_rows(store):
    """Every default row in force since T1, plus a newer EAP named (weight 15) whose close of the older one never ran."""
    tabs = default_tabs()
    rows = []
    for tab, tab_rows in tabs.items():
        for r in tab_rows:
            rows.append({"tab": tab, "key": natural_key(tab, r), "values": r, "effective_from": T1, "effective_to": None, "synced_at": T1})
    newer = dict(_row(tabs, "Signals", signal="EAP named")[1], weight="15")
    rows.append({"tab": "Signals", "key": "EAP named", "values": newer, "effective_from": T2, "effective_to": None, "synced_at": T2})
    store.insert("settings", rows)
    return newer


def test_a_removed_key_closes_every_row_in_force_so_none_comes_back(ctx, sheet):
    _seed_two_eap_named_rows(ctx.store)
    sheet["Signals"] = [r for r in sheet["Signals"] if r["signal"] != "EAP named"]
    ctx.now = T3
    sync.run(ctx)
    assert [r["effective_to"] for r in _rows(ctx, tab="Signals", key="EAP named")] == [T3, T3]
    settings, _ = sync.load_current(ctx.store)
    assert "EAP named" not in {s.signal for s in settings.signals}


def test_an_unchanged_key_closes_its_older_duplicate(ctx, sheet):
    newer = _seed_two_eap_named_rows(ctx.store)
    _row(sheet, "Signals", signal="EAP named")[1].update(newer)
    ctx.now = T3
    sync.run(ctx)
    rows = sorted(_rows(ctx, tab="Signals", key="EAP named"), key=lambda r: r["effective_from"])
    assert [(r["effective_from"], r["effective_to"]) for r in rows] == [(T1, T3), (T2, None)]
    settings, _ = sync.load_current(ctx.store)
    assert next(s for s in settings.signals if s.signal == "EAP named").weight == 15


def test_load_current_keeps_the_sheet_order(ctx, sheet):
    """The Roles order decides title ties (people.map_title_to_role): 'CFO & COO' is Operations, not Finance."""
    from us_outbound.clean.people import map_title_to_role

    sync.run(ctx)
    settings, _ = sync.load_current(ctx.store)
    assert [r.role for r in settings.roles] == [r["role"] for r in sheet["Roles"]]
    assert [c.copy_version for c in settings.copy] == [c["copy_version"] for c in sheet["Copy"]]
    assert map_title_to_role("CFO & COO", settings.roles) == "Operations"
    # Reordering the sheet changes only the order row.
    sheet["Roles"].reverse()
    ctx.now = T2
    summary = sync.run(ctx)
    assert summary["rows_opened"] == 0 and summary["tabs"]["Roles"]["status"] == "unchanged"
    settings, _ = sync.load_current(ctx.store)
    assert [r.role for r in settings.roles] == [r["role"] for r in sheet["Roles"]]
    assert [(r["effective_from"], r["effective_to"]) for r in _rows(ctx, tab=sync.ORDER_TAB, key="Roles")] == [
        (T1, T2), (T2, None)
    ]


def test_format_errors_caps_the_lines_and_escapes():
    from us_outbound.settings.validate import RowError

    report = {"Signals": [RowError("Signals", i + 2, "looks_for", "x <= y & z", f"s{i}") for i in range(25)]}
    text = sync.format_errors(report, [])
    lines = text.splitlines()
    assert len(lines) == 1 + sync.SLACK_ERROR_LINES + 1
    assert lines[-1] == "…and 5 more in the settings_sync log."
    assert "&lt;=" in text and "&amp;" in text and "<" not in text


def test_bootstrap(ctx):
    assert sync.bootstrap(ctx) is None  # a sheet is already configured
    assert ctx.clients.sheets.created == []
    assert sync.bootstrap(ctx, force=True) == "new-sheet-id"
    ((tabs, columns),) = ctx.clients.sheets.created
    assert tabs == default_tabs() and columns == COLUMNS


def test_bootstrap_in_dry_run_returns_none(ctx):
    ctx.clients.sheets.create_result = None  # what the Sheets client returns when the guard skips the create
    assert ctx.dry_run
    assert sync.bootstrap(ctx, force=True) is None


def test_a_roles_tab_in_the_spec5_layout_still_syncs_with_a_notice(ctx, sheet):
    """The Roles tab gained columns on 1 Oct 2026; a sheet made before keeps working until it is loaded."""
    from tests.test_settings_validate import LEGACY_ROLES

    sheet["Roles"] = copy.deepcopy(LEGACY_ROLES)
    summary = sync.run(ctx)
    assert summary["rejected"] == [] and "settings load --tab Roles --live" in summary["roles_notice"]
    settings, _ = sync.load_current(ctx.store)
    assert [r.role for r in settings.roles] == ["People leader", "Founder or executive", "Operations", "Finance"]
    sheet["Roles"] = default_tabs()["Roles"]  # what `settings load --tab Roles --live` writes
    ctx.now = T2
    summary = sync.run(ctx)
    assert "roles_notice" not in summary and summary["tabs"]["Roles"]["status"] == "synced"
    settings, _ = sync.load_current(ctx.store)
    assert next(r for r in settings.roles if r.role == "HR manager").order == {"50-249": 4}
