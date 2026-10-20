"""settings_sync (SPEC 5, 9): the settings sheet into the database's settings table, validated and versioned.

Nightly at 02:00 UK and on demand:
  1. Read every tab of "US Outbound – Settings".
  2. Validate each tab. A tab that fails keeps the version in force, and its errors go to
     Slack in one message. A tab with no version in force leaves settings unusable.
  3. For each valid tab, diff its rows by key against the version in force: an unchanged
     key is left alone; a changed key's row is closed (effective_to = now) and a new row
     is opened (effective_from = now, effective_to NULL); a removed key's row is closed.
     An older row still in force for a key (left by a run that stopped between opening a
     new row and closing the old one) is closed too, so it can never come back.
     The tab's keys in sheet order are versioned the same way, in one row per tab with
     tab = ORDER_TAB, so the settings jobs load keep the sheet's order (roles listed first
     win a tie, the first approved copy version, and so on).
  4. Write the lookalike facts from the cells and growth counts the monthly lookalikes job stored, so an account
     sourced since then is scored on them (sources/lookalikes.py; no HubSpot call).
  5. Rescore the queue with the settings now in force, so today's change shapes
     tomorrow's enrollment.

The settings table has one row per (tab, key) version; values is the raw sheet row.
A second run over an unchanged sheet writes nothing.

load_current re-reads the version in force leniently (strict=False, settings/validate.py): a General key this code
does not know, has renamed or retired is a warning, so a package that adds or deletes a key, or a rollback after Harry
adds one, never makes the version in force unusable (W1-1, 19 Oct 2026; RS P0-2). The sync itself reads the sheet
strictly, and its summary lists the General rows it did not read (general_warnings); the unknown and renamed ones
are posted once a week each.

load_fallback is for the safety jobs alone (ops/bootstrap.py SAFETY_JOBS) when the version in force fails even so:
each failing tab's most recent earlier version (by effective_to, up to FALLBACK_VERSIONS back) that validates with
the others, or, when no combination does, General's defaults with the Mailboxes and States tabs in force if they
validate alone. live_sending is always read raw from the General row in force, never a default, so a brake on the
fallback runs live when the sheet said live. While the settings are unusable, settings_sync says once a day that
the brakes run on the fallback.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.logs import log
from us_outbound.settings.definitions import apply_approved
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings import spec
from us_outbound.settings.model import OPTIONAL_TABS, TABS, Settings
from us_outbound.settings.validate import (
    HEADER_ROW,
    KEY_COLUMNS,
    MAY_BE_EMPTY,
    RowError,
    general_warnings,
    is_legacy_copy,
    is_legacy_roles,
    natural_key,
    page_signals_notice,
    validate_all,
)
from us_outbound.timeparse import utc_strict

TABLE = "settings"
SLACK_ERROR_LINES = 20
# What unusable settings stop (W1-1: the brakes run on load_fallback); ops/health.settings_fault says it too.
REFUSE_WORDS = "every job but the safety jobs refuses to start (the safety jobs run on a fallback that only pauses)"
FALLBACK_VERSIONS = 5  # how many earlier versions of a failing tab the fallback tries, the latest first
FALLBACK_EARLIER, FALLBACK_DEFAULTS = "earlier versions", "defaults"  # Settings.fallback_mode
HEADLINE = "The settings sheet has errors"  # format_errors' headline opens with it (ops/health quotes it)
# When the worker reads the sheet by itself (ops/schedule.py: 02:00 daily, 11:30 weekdays; ops/scheduler.AT_START).
SYNC_TIMES = "each time the worker starts, and at 02:00 UK daily and 11:30 UK on weekdays"
# Tabs whose rows name rows on another tab. If one of these has to keep its previous
# version, the version of the tab it names that is now on the sheet may not fit it.
DEPENDS_ON: dict[str, tuple[str, ...]] = spec.depends_on()  # from the refs in settings/spec.py

ORDER_TAB = "_order"  # key = a tab name, values = {"keys": [its keys in sheet order]}; views ignore it

Rows = list[dict[str, str]]
InForce = dict[str, dict[str, dict]]  # tab -> key -> settings row in force
Stale = dict[str, list[dict]]  # tab -> older rows still in force for a key (see _in_force)


def _raw(row: Mapping[str, Any]) -> dict[str, str]:
    return {str(k): "" if v is None else str(v) for k, v in row.items()}


def _in_force(store: Store) -> tuple[InForce, Stale]:
    """(the newest row in force per tab and key, the older rows still in force), for TABS and ORDER_TAB.

    Two rows are in force for one key only if a run stopped between opening the new row
    and closing the old one: the newer wins, and the next sync closes the older.
    """
    out: InForce = {tab: {} for tab in (*TABS, ORDER_TAB)}
    stale: Stale = {tab: [] for tab in out}
    for r in store.select(TABLE, {"effective_to": None}):
        tab, key = r.get("tab"), r.get("key")
        if tab not in out or key is None:
            continue
        old = out[tab].get(key)
        if old is not None and utc_strict(r["effective_from"]) < utc_strict(old["effective_from"]):
            stale[tab].append(r)
            continue
        if old is not None:
            stale[tab].append(old)
        out[tab][key] = r
    return out, stale


def _order(in_force: InForce, tab: str) -> list[str]:
    row = in_force[ORDER_TAB].get(tab)
    return [str(k) for k in ((row or {}).get("values") or {}).get("keys") or ()]


def _stored(in_force: InForce) -> dict[str, Rows]:
    """Each tab's rows in force, in sheet order; keys with no recorded position last, by key."""
    out: dict[str, Rows] = {}
    for tab in TABS:
        pos = {k: i for i, k in enumerate(_order(in_force, tab))}
        keys = sorted(in_force[tab], key=lambda k: (pos.get(k, len(pos)), k))
        out[tab] = [_raw(in_force[tab][k]["values"] or {}) for k in keys]
    return out


def _has_version(tab: str, in_force: InForce) -> bool:
    return bool(in_force[tab]) or tab in MAY_BE_EMPTY


def _stamp(settings: Settings, in_force: InForce) -> Settings:
    tabs = {tab: in_force[tab] for tab in TABS}
    versions = {tab: max(utc_strict(r["effective_from"]) for r in rows.values()) for tab, rows in tabs.items() if rows}
    synced = [utc_strict(r["synced_at"]) for rows in tabs.values() for r in rows.values() if r.get("synced_at")]
    return dataclasses.replace(settings, versions=versions, synced_at=max(synced) if synced else None)


def last_read(store: Store, settings: Settings) -> datetime | None:
    """When the sheet was last read into the settings in force: the start of the latest settings_sync that
    finished ok (a dry-run sync writes the settings too), or, with none recorded, the latest row written.

    settings.synced_at alone is when a row last changed: an unchanged sheet writes nothing, so it
    would say "Friday" after a weekend of good syncs.
    """
    times = [settings.synced_at] if settings.synced_at else []
    try:  # the run's start: the sheet is read first (heartbeats are indexed by job, started_at)
        run = store.latest("heartbeats", "started_at", {"job": "settings_sync", "status": "ok"})
    except Exception as exc:  # a status line, never a reason to fail
        log("settings_last_read_unknown", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        run = None
    if run and run.get("started_at"):
        times.append(utc_strict(run["started_at"]))
    return max(times) if times else None


def load_current(store: Store) -> tuple[Settings | None, dict[str, list[RowError]]]:
    """The Settings in force (effective_to NULL), validated again; None if any tab is unusable.

    Rows come back in sheet order, as the last sync of each tab saw it. versions maps each
    tab to the latest effective_from among its rows; synced_at is the latest row write.
    """
    in_force, _ = _in_force(store)
    settings, errors = validate_all(_stored(in_force), strict=False)
    return (_stamp(settings, in_force) if settings else None), errors


def _rows_before(rows: list[dict], order: list[dict], at: datetime) -> Rows:
    """A tab's rows as they stood just before `at` (in force from before it, closed at or after it), in the sheet
    order recorded then."""
    def held(r: Mapping[str, Any]) -> bool:
        to = r.get("effective_to")
        return utc_strict(r["effective_from"]) < at and (to is None or utc_strict(to) >= at)

    then = {str(r["key"]): r for r in sorted((r for r in rows if held(r)), key=lambda r: utc_strict(r["effective_from"]))}
    keys = next((list((r.get("values") or {}).get("keys") or ()) for r in order if held(r)), [])
    pos = {k: i for i, k in enumerate(keys)}
    return [_raw(then[k]["values"] or {}) for k in sorted(then, key=lambda k: (pos.get(k, len(pos)), k))]


def earlier_versions(store: Store, tab: str, limit: int = FALLBACK_VERSIONS) -> list[Rows]:
    """The tab's versions before the one in force, the latest first: the rows as they stood just before each time a
    row of it was closed (effective_to descending), up to `limit` that differ."""
    rows = store.select(TABLE, {"tab": tab})
    order = [r for r in store.select(TABLE, {"tab": ORDER_TAB}) if r.get("key") == tab]
    closes = sorted({utc_strict(r["effective_to"]) for r in rows + order if r.get("effective_to") is not None},
                    reverse=True)
    out: list[Rows] = []
    for at in closes:
        version = _rows_before(rows, order, at)
        if version and version not in out:
            out.append(version)
        if len(out) >= limit:
            break
    return out


def raw_live_sending(in_force: InForce) -> bool:
    """live_sending as the General row in force has it, read on its own: the fallback never takes the default (no)
    for it, or a brake would run dry while the sheet says live (RS P0-1). Unreadable reads as no."""
    from us_outbound.settings.parsers import parse_bool

    row = in_force["General"].get("live_sending")
    text = str(((row or {}).get("values") or {}).get("value") or "").strip()
    try:
        return bool(parse_bool(text)) if text else False
    except ValueError:
        log("settings_fallback_live_sending_unreadable", value=text[:20])
        return False


def load_fallback(store: Store) -> Settings | None:
    """The last-good settings for the safety jobs (module docstring), or None when nothing at all is stored. The
    settings in force when they validate (fallback_tabs empty)."""
    in_force, _ = _in_force(store)
    if not any(in_force[t] for t in TABS):
        return None
    stored = _stored(in_force)
    settings, errors = validate_all(stored, strict=False)
    if settings is not None:
        return _stamp(settings, in_force)
    failing = [t for t in TABS if errors[t]]
    live = raw_live_sending(in_force)
    chosen = dict(stored)
    for tab in failing:
        for version in earlier_versions(store, tab):
            _, errs = validate_all({**chosen, tab: version}, strict=False)
            if not errs[tab]:
                chosen[tab] = version
                break
    settings, _ = validate_all(chosen, strict=False)
    if settings is not None:
        mode = FALLBACK_EARLIER
    else:  # no combination validates: General's defaults, with the registry and the states if they stand alone
        from us_outbound.settings.model import General
        from us_outbound.settings.validate import validate_tab

        mailboxes, m_errors = validate_tab("Mailboxes", stored["Mailboxes"], strict=False)
        states, s_errors = validate_tab("States", stored["States"], strict=False)
        settings = Settings(general=General(), mailboxes=() if m_errors else mailboxes, states=() if s_errors else states)
        mode = FALLBACK_DEFAULTS
    settings = dataclasses.replace(_stamp(settings, in_force), fallback_tabs=frozenset(failing), fallback_mode=mode,
                                   general=dataclasses.replace(settings.general, live_sending=live))
    log("settings_fallback", tabs=failing, mode=mode, live_sending=live)
    return settings


def fallback_line(settings: Settings) -> str:
    """The fallback in words, for Slack and the heartbeat_check readers."""
    tabs = _and([f"{t} tab" for t in sorted(settings.fallback_tabs)])
    stand_in = (f"an earlier good version of the {tabs}" if settings.fallback_mode == FALLBACK_EARLIER else
                "the code defaults for the General tab, with the Mailboxes and States tabs as they are")
    return (f"The safety jobs (the kill rules, the blackout pause, reading replies and email results, the do-not-contact "
            f"list, the health check and the stop command) are running on {stand_in}, because the {tabs} in force "
            f"{'fails' if len(settings.fallback_tabs) == 1 else 'fail'} a check. They only pause and stop: nothing "
            f"is started, added or sent, live_sending stays as the sheet has it "
            f"({'yes' if settings.general.live_sending else 'no'}), and enrolment and the reply desk stay stopped "
            "until the sheet is fixed.")


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
    """New rows to open, old rows to close ({effective_from: [keys]}), counts, and the keys in sheet order."""
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
        if old is not None and utc_strict(old["effective_from"]) != now:  # same instant: the upsert replaces it
            closing.setdefault(old["effective_from"], []).append(key)
    for key, old in in_force.items():
        if key not in new:
            counts["removed"] += 1
            closing.setdefault(old["effective_from"], []).append(key)
    return opened, closing, counts, list(new)


def _diff_order(tab: str, keys: list[str], in_force: InForce, stale: Stale, now: datetime):
    """The tab's ORDER_TAB row: a new one to open if the sheet order changed, and rows to close."""
    closing: dict[Any, list[str]] = {}
    for r in stale[ORDER_TAB]:
        if r.get("key") == tab:
            closing.setdefault(r["effective_from"], []).append(tab)
    old = in_force[ORDER_TAB].get(tab)
    if (old is None and not keys) or (old is not None and _order(in_force, tab) == keys):
        return [], closing
    if old is not None and utc_strict(old["effective_from"]) != now:
        closing.setdefault(old["effective_from"], []).append(tab)
    row = {"tab": ORDER_TAB, "key": tab, "values": {"keys": keys}, "effective_from": now, "effective_to": None, "synced_at": now}
    return [row], closing


def _close(store: Store, tab: str, closing: Mapping[Any, list[str]], now: datetime) -> int:
    n = 0
    for effective_from, keys in closing.items():
        n += store.update(
            TABLE, {"tab": tab, "key": keys, "effective_from": effective_from, "effective_to": None}, {"effective_to": now}
        )
    return n


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}" if items else ""


def format_errors(report: Mapping[str, list[RowError]], unusable: list[str], ctx: Context | None = None) -> str:
    """One Slack message (Harry, 12 Oct 2026: clear to a newcomer; base/say.py): what was not applied and what that
    stops, up to SLACK_ERROR_LINES row errors, and the fix. With ctx each tab links to itself on the sheet.
    ops/health points to this message (HEADLINE) when the jobs refuse to start on unusable settings."""
    from us_outbound.base import say

    def tab(t: str) -> str:
        return say.sheet_link(ctx, t) if ctx is not None else f"{t} tab"

    tabs = [t for t in TABS if report.get(t)]
    kept = [t for t in tabs if t not in unusable]
    head = f"{HEADLINE}, so " + ("the jobs have stopped" if unusable else "some edits were not applied")
    facts = []
    if kept:
        facts.append(f"Not applied: the {_and([tab(t) for t in kept])}. The version read before stays in use "
                     "until the errors are fixed.")
    if unusable:
        facts.append(f"The {_and([tab(t) for t in unusable])} {'has' if len(unusable) == 1 else 'have'} no earlier "
                     f"good version to fall back on, so {REFUSE_WORDS} until the errors are fixed.")
    errors = [e for t in tabs for e in report[t]]
    facts += [f"• {_slack_escape(str(e))}" for e in errors[:SLACK_ERROR_LINES]]
    if len(errors) > SLACK_ERROR_LINES:
        facts.append(f"…and {len(errors) - SLACK_ERROR_LINES} more in {say.logs_link('the settings sync log in Railway')}.")
    do = (f"correct {'this' if len(errors) == 1 else 'these'} on the sheet, then run {say.command('sync')} to read it "
          f"now. Otherwise it is read {SYNC_TIMES}.")
    return say.compose(say.URGENT if unusable else say.ACTION, head, facts, do=do)


def _lookalikes(ctx: Context) -> dict:
    """Lookalike facts from the stored cells, before the rescore scores them (sources/lookalikes.py)."""
    from us_outbound.sources import lookalikes

    return lookalikes.apply(ctx)


def _rescore(ctx: Context) -> bool:
    """Rescore the queue with the settings now in force (SPEC 5). Imported here, as tests replace the module."""
    from us_outbound.scoring import score

    score.rescore(ctx)
    return True


def read_sheet(ctx: Context, sheet_id: str) -> dict[str, Rows]:
    """Every tab. An optional tab the sheet does not have yet reads as empty (a missing tab is a 400)."""
    sheet = ctx.clients.sheets.read_tabs(sheet_id, [t for t in TABS if t not in OPTIONAL_TABS])
    for tab in sorted(OPTIONAL_TABS):
        try:
            sheet.update(ctx.clients.sheets.read_tabs(sheet_id, [tab]))
        except ApiError as exc:
            if exc.status != 400:
                raise
            log("settings_tab_missing", tab=tab, note="read as empty until the tab is added")
        sheet.setdefault(tab, [])
    return sheet


def run(ctx: Context) -> dict:
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise RuntimeError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID (bootstrap() creates the sheet)")
    now = ctx.now
    # Definition changes Harry approved on the Definition suggestions tab go onto the Industries tab first, so the
    # sheet stays their one source and this run reads them (11 Oct 2026). Nothing there may stop the sync (W0, 19 Oct
    # 2026): apply_approved records a row it cannot apply, and anything it did not foresee is recorded here, so the
    # rest of the sheet, live_sending = no included, still comes into force this run.
    try:
        definitions = apply_approved(ctx, sheet_id)
    except GuardViolation:
        raise
    except Exception as exc:
        log("definitions_apply_failed", run_id=ctx.run_id, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        definitions = {"applied": [], "missing": [],
                       "errors": [{"id": "", "label": "", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}]}
    sheet = read_sheet(ctx, sheet_id)
    in_force, stale = _in_force(ctx.store)
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
        opened, closing, counts, keys = _diff(tab, sheet[tab], in_force[tab], now)
        for r in stale[tab]:  # an older duplicate an interrupted run left in force
            closing.setdefault(r["effective_from"], []).append(r["key"])
        order_opened, order_closing = _diff_order(tab, keys, in_force, stale, now)
        if opened or order_opened:
            ctx.store.upsert(TABLE, opened + order_opened)
        closed_total += _close(ctx.store, tab, closing, now)
        _close(ctx.store, ORDER_TAB, order_closing, now)
        opened_total += len(opened)
        tabs[tab] = {"status": "synced" if opened or closing else "unchanged", **counts}

    for errs in report.values():
        for e in errs:
            log("settings_row_error", tab=e.tab, row=e.row, column=e.column, label=e.label, message=e.message)

    alerted, alert_error = False, None
    if report:
        try:
            ctx.clients.slack.post(ctx.settings.general.alert_channel, format_errors(report, unusable, ctx))
            alerted = True
        except GuardViolation:
            raise
        except Exception as exc:  # the rescore still runs; the failure is raised after it
            alert_error = exc
            log("settings_sync_alert_failed", error=str(exc))

    warnings = general_warnings(sheet.get("General"))
    warned = _post_warnings(ctx, warnings)
    settings, _ = load_current(ctx.store)
    rescored, lookalike, fallback = False, None, None
    if settings is None:
        log("settings_sync_rescore_skipped", reason="settings unusable", unusable=unusable)
        fallback = _post_fallback(ctx)
    else:
        current = dataclasses.replace(ctx, settings=settings)
        lookalike = _lookalikes(current)
        rescored = _rescore(current)

    summary = {
        "sheet_id": sheet_id,
        "definitions_applied": definitions["applied"],
        "definition_apply_errors": definitions.get("errors") or [],
        "tabs": tabs,
        "rows_opened": opened_total,
        "rows_closed": closed_total,
        "rejected": [t for t in TABS if t in rejected],
        "unusable": unusable,
        "errors": sum(len(v) for v in report.values()),
        "alerted": alerted,
        "lookalike_facts": lookalike,
        "rescored": rescored,
        "general_warnings": [str(w) for w in warnings],
        "warnings_posted": warned,
        "fallback": fallback,
    }
    if sheet.get("Copy") and is_legacy_copy(set().union(*(set(r) for r in sheet["Copy"]))):
        summary["copy_notice"] = ("the Copy tab is still one row per step, so no copy is in force; "
                                  "run `us-outbound settings load --tab Copy --live`")
    if sheet.get("Roles") and is_legacy_roles(set().union(*(set(r) for r in sheet["Roles"]))):
        summary["roles_notice"] = ("the Roles tab is still in SPEC 5's layout, so contacts follow the old order; "
                                   "run `us-outbound settings load --tab Roles --live` for Harry's 1 Oct order")
    if settings is not None:
        notice = page_signals_notice(settings)
        if notice:
            summary["signals_notice"] = notice
    log("settings_sync", **summary)
    if alert_error is not None:
        raise alert_error
    return summary


WARNING_HEAD = "The settings sheet has General rows the jobs do not read"


def _post_warnings(ctx: Context, warnings: list[RowError]) -> list[str]:
    """The unknown and renamed General keys, once a week each (a retired key's row is harmless and is only listed in
    the summary). Never raises: the sync goes on."""
    from us_outbound.base import notify, say
    from us_outbound.settings.validate import RETIRED_GENERAL

    told = [w for w in warnings if w.label not in RETIRED_GENERAL]
    if not told:
        return []
    try:
        pairs = [(notify.once_key(ctx, "settings_warning", w.label, per="week"), f"• {_slack_escape(str(w))}")
                 for w in told]
        sent = notify.post_once(ctx, pairs, head=f"{say.ACTION} *{WARNING_HEAD}*",
                                foot=f"_A typo in a key name leaves its value unread: correct it on the "
                                     f"{say.sheet_link(ctx, 'General')}._")
        return list(sent.get("keys") or [])
    except GuardViolation:
        raise
    except Exception as exc:
        log("settings_warnings_not_posted", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return []


def _post_fallback(ctx: Context) -> dict | None:
    """While the settings are unusable: what the safety jobs run on, once a day (load_fallback). Never raises."""
    from us_outbound.base import notify, say

    try:
        fallback = load_fallback(ctx.store)
        if fallback is None:
            return None
        sent = notify.post_once(ctx, [(notify.once_key(ctx, "settings_fallback"), fallback_line(fallback))],
                                head=f"{say.URGENT} *The safety jobs are running on a fallback of the settings*")
        return {"tabs": sorted(fallback.fallback_tabs), "mode": fallback.fallback_mode, "posted": sent.get("keys") or []}
    except GuardViolation:
        raise
    except Exception as exc:
        log("settings_fallback_not_posted", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return None


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
