"""`us-outbound settings load`: bring the build's Industries and Copy tabs into the live sheet (Harry, 30 Sep 2026).

The sheet was made from the first defaults (58 industries, copy one row per step). The build
now has all 108 industry pages and a four-email sequence per industry (settings/data/). This
command merges them into the sheet without losing Harry's own edits:

  * Industries: rows are matched by industry label. The build's columns win, except the
    ones Harry owns (KEEP: active, priority, proof_point), which keep their sheet value
    when it is not blank. Rows Harry added that the build does not have stay, at the end.
  * Copy: a tab still in the one-row-per-step layout is replaced (its rows stay in the
    database's settings history). A tab already in the new layout keeps every row as it
    is on the sheet; only copy versions it does not have are added, at the end.

Dry-run (the default) prints what would change and writes nothing. --live rewrites the tab
(values only; the sheet's formatting stays), then `us-outbound settings sync` brings it in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from us_outbound.context import Context
from us_outbound.logs import log
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.validate import is_legacy_copy, validate_all

LOADABLE = ("Industries", "Copy")
KEEP: dict[str, tuple[str, ...]] = {"Industries": ("active", "priority", "proof_point")}
KEY = {"Industries": "industry", "Copy": "copy_version"}
SHOW = 12  # names listed per change in the summary


@dataclass
class Plan:
    tab: str
    rows: list[dict[str, str]]
    before: int
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)  # rows where a build column changed
    kept_from_sheet: list[str] = field(default_factory=list)  # Harry's values kept, or whole rows kept
    extra: list[str] = field(default_factory=list)  # sheet rows the build does not have
    replaced_layout: bool = False
    new_columns: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        def few(xs: Sequence[str]) -> list[str] | str:
            return list(xs) if len(xs) <= SHOW else [*xs[:SHOW], f"... {len(xs) - SHOW} more"]

        return {
            "tab": self.tab, "rows_before": self.before, "rows_after": len(self.rows),
            "added": len(self.added), "added_names": few(self.added),
            "updated": len(self.updated), "updated_names": few(self.updated),
            "kept_from_sheet": few(self.kept_from_sheet), "extra_sheet_rows_kept": few(self.extra),
            "replaced_old_layout": self.replaced_layout, "new_columns": self.new_columns,
        }


def _key(row: Mapping[str, str], tab: str) -> str:
    return str(row.get(KEY[tab]) or "").strip().casefold()


def plan_tab(tab: str, sheet_rows: Sequence[Mapping[str, str]], build_rows: Sequence[Mapping[str, str]]) -> Plan:
    cols = COLUMNS[tab]
    present = set().union(*(set(r) for r in sheet_rows)) if sheet_rows else set()
    p = Plan(tab, [], len(sheet_rows), new_columns=[c for c in cols if present and c not in present])
    sheet = {_key(r, tab): r for r in sheet_rows if _key(r, tab)}
    build = {_key(r, tab): r for r in build_rows}

    if tab == "Copy" and is_legacy_copy(present):
        p.replaced_layout = True
        p.rows = [{c: str(r.get(c, "")) for c in cols} for r in build_rows]
        p.added = [str(r[KEY[tab]]) for r in build_rows]
        return p
    if tab == "Copy":
        p.rows = [{c: str(r.get(c, "")) for c in cols} for r in sheet_rows]
        p.kept_from_sheet = [str(r.get(KEY[tab], "")) for r in sheet_rows if _key(r, tab)]
        for k, r in build.items():
            if k not in sheet:
                p.rows.append({c: str(r.get(c, "")) for c in cols})
                p.added.append(str(r[KEY[tab]]))
        return p

    keep = KEEP.get(tab, ())
    for k, r in build.items():
        row = {c: str(r.get(c, "")) for c in cols}
        old = sheet.get(k)
        if old is None:
            p.added.append(row[KEY[tab]])
        else:
            kept = [c for c in keep if str(old.get(c, "")).strip() and str(old.get(c, "")).strip() != row[c].strip()]
            for c in kept:
                row[c] = str(old[c])
            if kept:
                p.kept_from_sheet.append(f"{row[KEY[tab]]} ({', '.join(kept)})")
            if any(str(old.get(c, "")).strip() != row[c].strip() for c in cols if c not in kept):
                p.updated.append(row[KEY[tab]])
        p.rows.append(row)
    for k, old in sheet.items():
        if k not in build:
            p.rows.append({c: str(old.get(c, "")) for c in cols})
            p.extra.append(str(old.get(KEY[tab], "")))
    return p


def load(ctx: Context, tabs: Sequence[str]) -> dict:
    """Plan (and, live, write) each tab; refuses if the result would not validate."""
    bad = [t for t in tabs if t not in LOADABLE]
    if bad:
        raise ValueError(f"only {', '.join(LOADABLE)} can be loaded, not {', '.join(bad)}")
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise ValueError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    from us_outbound.settings.sync import read_sheet

    sheet = read_sheet(ctx, sheet_id)
    build = default_tabs()
    plans = {t: plan_tab(t, sheet.get(t) or [], build[t]) for t in tabs}
    merged = {**sheet, **{t: p.rows for t, p in plans.items()}}
    _, errors = validate_all(merged)
    problems = [str(e) for t in (*tabs, "Tests", "Focus") for e in errors.get(t, [])]
    if problems:
        raise ValueError("the loaded tabs would not validate: " + "; ".join(problems[:10]))
    for t, p in plans.items():
        ctx.clients.sheets.replace_tab(sheet_id, t, COLUMNS[t], p.rows)  # the guard skips it in dry-run
        log("settings_load", tab=t, dry_run=ctx.dry_run, **{k: v for k, v in p.summary().items() if k != "tab"})
    return {"dry_run": ctx.dry_run, "tabs": [p.summary() for p in plans.values()]}
