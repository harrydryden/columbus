"""settings_sync (SPEC 5, 9): the settings sheet into BigQuery settings, validated and versioned.

Nightly at 02:00 UK and on demand:
  1. Read every tab of "US Outbound – Settings".
  2. Validate each tab. A tab that fails keeps the version in force, and its errors go to
     Slack in one message. A tab with no version in force leaves settings unusable.
  3. For each valid tab, diff its rows by key against the version in force: an unchanged
     key is left alone; a changed key's row is closed (effective_to = now) and a new row
     is opened (effective_from = now, effective_to NULL); a removed key's row is closed.
  4. Rescore the queue with the settings now in force, so today's change shapes
     tomorrow's enrollment.

The settings table has one row per (tab, key) version; values is the raw sheet row.
A second run over an unchanged sheet writes nothing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from us_outbound.clients.bq import Store
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import Context
from us_outbound.logs import log
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.model import TABS, Settings
from us_outbound.settings.validate import HEADER_ROW, KEY_COLUMNS, MAY_BE_EMPTY, RowError, natural_key, validate_all

TABLE = "settings"
SLACK_ERROR_LINES = 20
# Tabs whose rows name rows on another tab. If one of these has to keep its previous
# version, the version of the tab it names that is now on the sheet may not fit it.
DEPENDS_ON: dict[str, tuple[str, ...]] = {"Signals": ("Angles",), "Copy": ("Angles",), "Tests": ("Copy",)}

Rows = list[dict[str, str]]
InForce = dict[str, dict[str, dict]]  # tab -> key -> settings row in force


def _ts(v: Any) -> datetime:
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _raw(row: Mapping[str, Any]) -> dict[str, str]:
    return {str(k): "" if v is None else str(v) for k, v in row.items()}


def _in_force(store: Store) -> InForce:
    out: InForce = {tab: {} for tab in TABS}
    for r in store.select(TABLE, {"effective_to": None}):
        tab, key = r.get("tab"), r.get("key")
        if tab not in out or key is None:
            continue
        old = out[tab].get(key)
        # Two rows in force for one key only if a run stopped between opening and closing; the newer wins.
        if old is None or _ts(r["effective_from"]) > _ts(old["effective_from"]):
            out[tab][key] = r
    return out


def _stored(in_force: InForce) -> dict[str, Rows]:
    return {tab: [_raw(in_force[tab][k]["values"] or {}) for k in sorted(in_force[tab])] for tab in TABS}


def _has_version(tab: str, in_force: InForce) -> bool:
    return bool(in_force[tab]) or tab in MAY_BE_EMPTY


def _stamp(settings: Settings, in_force: InForce) -> Settings:
    versions = {tab: max(_ts(r["effective_from"]) for r in rows.values()) for tab, rows in in_force.items() if rows}
    synced = [_ts(r["synced_at"]) for rows in in_force.values() for r in rows.values() if r.get("synced_at")]
    return dataclasses.replace(settings, versions=versions, synced_at=max(synced) if synced else None)


def load_current(store: Store) -> tuple[Settings | None, dict[str, list[RowError]]]:
    """The Settings in force (effective_to NULL), validated again; None if any tab is unusable.

    Rows come back ordered by key, not by sheet position. versions maps each tab to the
    latest effective_from among its rows; synced_at is the latest row write.
    """
    in_force = _in_force(store)
    settings, errors = validate_all(_stored(in_force))
    return (_stamp(settings, in_force) if settings else None), errors


def _choose(
    sheet: Mapping[str, Any], stored: dict[str, Rows], has_version: set[str]
) -> tuple[set[str], dict[str, list[RowError]], dict[str, list[RowError]]]:
    """Which tabs keep the version in force: (rejected, errors to report, errors left in force).

    A tab is rejected when it fails on its own, or when it does not fit the other tabs that
    will be in force (a signal naming an angle only the rejected Angles tab has). When a
    rejected tab's version in force no longer fits a tab it names, that tab is held back too.
    """
    _, sheet_errors = validate_all(sheet)
    rejected = {t for t in TABS if sheet_errors[t]}
    report = {t: list(sheet_errors[t]) for t in rejected}
    errors = sheet_errors
    for _ in range(len(TABS) + 1):
        _, errors = validate_all({t: stored[t] if t in rejected else sheet.get(t) for t in TABS})
        grew = False
        for t in TABS:
            if not errors[t]:
                continue
            if t not in rejected:
                rejected.add(t)
                report[t] = list(errors[t])
                grew = True
                continue
            for ref in DEPENDS_ON.get(t, ()) if t in has_version else ():
                if ref not in rejected:
                    rejected.add(ref)
                    report[ref] = [RowError(ref, HEADER_ROW, "", f"kept the previous version: the {t} tab in force refers to it")]
                    grew = True
        if not grew:
            break
    return rejected, report, errors


def _diff(tab: str, rows: list[Mapping[str, Any]], in_force: dict[str, dict], now: datetime):
    """New rows to open, old rows to close ({effective_from: [keys]}), and counts."""
    new: dict[str, dict[str, str]] = {}
    for row in rows:
        if all(str(row.get(c) or "").strip() for c in KEY_COLUMNS[tab]):  # skips General note-only rows
            new[natural_key(tab, row)] = _raw(row)
    opened: list[dict] = []
    closing: dict[Any, list[str]] = {}
    counts = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}
    for key, values in new.items():
        old = in_force.get(key)
        if old is not None and _raw(old["values"] or {}) == values:
            counts["unchanged"] += 1
            continue
        counts["changed" if old is not None else "added"] += 1
        opened.append(
            {"tab": tab, "key": key, "values": values, "effective_from": now, "effective_to": None, "synced_at": now}
        )
        if old is not None and _ts(old["effective_from"]) != now:  # same instant: the upsert replaces it
            closing.setdefault(old["effective_from"], []).append(key)
    for key, old in in_force.items():
        if key not in new:
            counts["removed"] += 1
            closing.setdefault(old["effective_from"], []).append(key)
    return opened, closing, counts


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_errors(report: Mapping[str, list[RowError]], unusable: list[str]) -> str:
    """One Slack message: what was rejected, then up to SLACK_ERROR_LINES errors."""
    tabs = [t for t in TABS if report.get(t)]
    noun = "tab" if len(tabs) == 1 else "tabs"
    lines = [f"Settings sync rejected the {', '.join(tabs)} {noun}. The previous version stays in force."]
    for t in unusable:
        lines.append(f"{t} has no valid version in force, so settings are unusable until it is fixed.")
    errors = [e for t in tabs for e in report[t]]
    lines += [f"• {e}" for e in errors[:SLACK_ERROR_LINES]]
    if len(errors) > SLACK_ERROR_LINES:
        lines.append(f"…and {len(errors) - SLACK_ERROR_LINES} more in the settings_sync log.")
    return _slack_escape("\n".join(lines))


def _rescore(ctx: Context) -> bool:
    """Phase 1 builds scoring; until it exists the rescore is logged and skipped."""
    try:
        from us_outbound.scoring import score
    except ImportError as exc:
        log("settings_sync_rescore_skipped", reason=f"scoring is not built yet ({exc})")
        return False
    rescore = getattr(score, "rescore", None)
    if rescore is None:
        log("settings_sync_rescore_skipped", reason="us_outbound.scoring.score has no rescore()")
        return False
    rescore(ctx)
    return True


def run(ctx: Context) -> dict:
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise RuntimeError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID (bootstrap() creates the sheet)")
    now = ctx.now
    sheet = ctx.clients.sheets.read_tabs(sheet_id, list(TABS))
    in_force = _in_force(ctx.store)
    stored = _stored(in_force)
    has_version = {t for t in TABS if _has_version(t, in_force)}
    rejected, report, left = _choose(sheet, stored, has_version)
    unusable = [t for t in TABS if left[t] or (t in rejected and t not in has_version)]

    tabs: dict[str, dict] = {}
    opened_total = closed_total = 0
    for tab in TABS:
        if tab in rejected:
            tabs[tab] = {"status": "unusable" if tab in unusable else "kept_previous", "errors": len(report.get(tab, []))}
            continue
        opened, closing, counts = _diff(tab, sheet[tab], in_force[tab], now)
        if opened:
            ctx.store.upsert(TABLE, opened)
        for effective_from, keys in closing.items():
            closed_total += ctx.store.update(
                TABLE,
                {"tab": tab, "key": keys, "effective_from": effective_from, "effective_to": None},
                {"effective_to": now},
            )
        opened_total += len(opened)
        tabs[tab] = {"status": "synced" if opened or closing else "unchanged", **counts}

    for errs in report.values():
        for e in errs:
            log("settings_row_error", tab=e.tab, row=e.row, column=e.column, label=e.label, message=e.message)

    alerted, alert_error = False, None
    if report:
        try:
            ctx.clients.slack.post(ctx.settings.general.alert_channel, format_errors(report, unusable))
            alerted = True
        except GuardViolation:
            raise
        except Exception as exc:  # the rescore still runs; the failure is raised after it
            alert_error = exc
            log("settings_sync_alert_failed", error=str(exc))

    settings, _ = load_current(ctx.store)
    rescored = False
    if settings is None:
        log("settings_sync_rescore_skipped", reason="settings unusable", unusable=unusable)
    else:
        rescored = _rescore(dataclasses.replace(ctx, settings=settings))

    summary = {
        "sheet_id": sheet_id,
        "tabs": tabs,
        "rows_opened": opened_total,
        "rows_closed": closed_total,
        "rejected": [t for t in TABS if t in rejected],
        "unusable": unusable,
        "errors": sum(len(v) for v in report.values()),
        "alerted": alerted,
        "rescored": rescored,
    }
    log("settings_sync", **summary)
    if alert_error is not None:
        raise alert_error
    return summary


def bootstrap(ctx: Context, *, force: bool = False) -> str | None:
    """Create "US Outbound – Settings" loaded with the SPEC 5 defaults; returns its id.

    Live only: in dry-run the Sheets client skips the create and this returns None.
    Refuses (returns None) when a settings sheet is already configured, unless force.
    """
    existing = ctx.guard.bounds.settings_sheet_id
    if existing and not force:
        log("settings_sheet_exists", sheet_id=existing)
        return None
    tabs = default_tabs()
    _, errors = validate_all(tabs)
    if any(errors.values()):
        raise ValueError("the default settings do not validate: " + "; ".join(str(e) for v in errors.values() for e in v))
    sheet_id = ctx.clients.sheets.create_settings_sheet(tabs, {t: list(c) for t, c in COLUMNS.items()})
    log("settings_sheet_created" if sheet_id else "settings_sheet_not_created", sheet_id=sheet_id, dry_run=ctx.dry_run)
    return sheet_id
