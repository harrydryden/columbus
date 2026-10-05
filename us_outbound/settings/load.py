"""`us-outbound settings load`: bring the build's General, Industries, Copy and Roles tabs into the live sheet (Harry, 30 Sep 2026).

The sheet was made from the first defaults (58 industries, copy one row per step). The build
now has all 108 industry pages and a four-email sequence per industry (settings/data/). This
command merges them into the sheet without losing Harry's own edits:

  * Industries: rows are matched by industry label. The build's columns win, except the
    ones Harry owns (KEEP: active, priority, proof_point), which keep their sheet value
    when it is not blank. Rows Harry added that the build does not have stay, at the end.
  * General: Harry's values stay. A renamed key (daily_enrol_cap is now weekly_enrol_cap) is
    renamed and given the build's value, since its unit changed; retired keys (postal_address,
    privacy_url) are removed; keys the sheet does not have
    yet are added with the build's value and note; `--set key=value` sets the values Harry has
    decided. Note-only rows stay where they are. clay_verification (Harry, 1 Oct 2026) arrives
    this way as skip; `--set clay_verification=required` once the Clay functions exist.
    `--set` refuses the sign-off keys (SIGN_OFF: live_sending, auto_send, optout_tested,
    approver_slack_ids): this command is live with --live alone, and those are Harry's to set on
    the sheet. A load never changes the note of a key the tab already has; `--take note` refreshes
    those notes from the build's, and never touches a value.
  * Copy: a tab still in the one-row-per-step layout is replaced (its rows stay in the
    database's settings history). A tab already in the new layout keeps every row as it
    is on the sheet; only copy versions it does not have are added, at the end. With
    --replace-drafts (Harry, 1 Oct 2026: the copy by industry AND role replaces the copy by
    industry), rows Harry has not approved are replaced by the build's; approved rows stay.
  * Roles (Harry, 1 Oct 2026): a tab still in SPEC 5's layout (first_choice_for_size,
    fallback_order) is replaced by the build's rows, the size-band order led by seniority. A
    tab already in the new layout keeps its rows; only roles it does not have are added.
  * Signals and Focus (Harry, 1 Oct 2026): the build's rows are added and Harry's own rows stay. A
    row the build replaced under another name (SUPERSEDED: Recent funding, now split by age) stays
    on the sheet but is switched off, so it does not score alongside its replacements. Focus rows
    take the build's values. Signals rows (SHEET_WINS; Harry, 2 Oct 2026) keep the sheet's value in
    every column the sheet already has, blank included, so a load never undoes his edits; columns
    the sheet does not have yet, like the tokenized openers' opener_people, opener_founder,
    opener_ops and opener_self, arrive with the build's lines. `--take weight` (any column) lets the
    build's value win for one load, as the design review's did on 1 Oct.
    The source column is the build's (BUILD_OWNS): it names the code that produces a signal's
    facts, so the page signals' careers_pages source (Harry, 2 Oct 2026: our own page reader,
    sources/pages.py) arrives with a plain `--tab Signals` load; until it does, settings_sync's
    summary says so.

Dry-run (the default) prints what would change and writes nothing. --live rewrites the tab
(values only; the sheet's formatting stays), then `us-outbound settings sync` brings it in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from us_outbound.context import Context
from us_outbound.logs import log
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.validate import RENAMED_GENERAL, RETIRED_GENERAL, is_legacy_copy, is_legacy_roles, validate_all

LOADABLE = ("General", "Industries", "Copy", "Roles", "Signals", "Focus")
# The columns Harry owns; `--take COLUMN` lets the build's value win for one load (Harry, 1 Oct 2026:
# "make all the changes" in the design review, so Legal Teams goes on at launch).
KEEP: dict[str, tuple[str, ...]] = {"Industries": ("active", "priority", "proof_point")}
# General: a key's note stays as the sheet has it; `--take note` refreshes the notes of the keys the tab
# already has from the build's (their values are never touched by this).
GENERAL_TAKE = ("note",)
# The sign-off keys: Harry sets them on the sheet. `settings load` is an operator command, live with
# --live alone, so its --set must not be a way round his sign-off (live_sending, auto_send and
# optout_tested decide what is sent; approver_slack_ids decides who may approve it).
SIGN_OFF = ("live_sending", "auto_send", "optout_tested", "approver_slack_ids")
# Tabs where every column the sheet already has keeps its value, blank included (Harry, 2 Oct 2026: the
# tokenized openers arrive as new Signals columns without undoing his edits). Columns the sheet does not
# have yet take the build's values; `--take COLUMN` lets the build win for one column, as the design
# review's load did for the weights on 1 Oct.
SHEET_WINS = frozenset({"Signals"})
# Columns that name code, not a judgment, so the build always wins there even on a SHEET_WINS tab: a
# Signals row's source keys are the source modules that produce its facts (careers_pages, 2 Oct 2026).
BUILD_OWNS: dict[str, tuple[str, ...]] = {"Signals": ("source",)}
KEY = {"General": "key", "Industries": "industry", "Copy": "copy_version", "Roles": "role", "Signals": "signal",
       "Focus": "industry_group"}
DEFAULT_TABS = ("General", "Industries", "Copy", "Roles")  # what a load with no --tab brings in
# Rows the build replaced under another name: a load keeps them on the sheet but switches them off,
# so the old and new rows don't both score (Recent funding was split by age; review Appendix A).
SUPERSEDED: dict[str, dict[str, str]] = {
    "Signals": {"recent funding": "Funding in the last 6 months and Funding 6–12 months ago"},
}
# Tabs whose old layout is replaced whole, and whose rows in the new layout stay as Harry has them.
LEGACY_LAYOUT = {"Copy": is_legacy_copy, "Roles": is_legacy_roles}
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


def check_sets(sets: Mapping[str, str] | None) -> None:
    """Refuse a --set of a sign-off key (SIGN_OFF): Harry sets those on the sheet."""
    refused = [k for k in SIGN_OFF if k in (sets or {})]
    if refused:
        raise ValueError(f"--set cannot change {', '.join(refused)}: {'it is' if len(refused) == 1 else 'they are'} "
                         "Harry's sign-off, set on the General tab of the settings sheet")


def plan_general(sheet_rows: Sequence[Mapping[str, str]], build_rows: Sequence[Mapping[str, str]],
                 sets: Mapping[str, str] | None = None, *, take: Sequence[str] = ()) -> Plan:
    """The General tab with renamed keys renamed, missing keys added and Harry's decided values set;
    with take ("note"), the notes of the keys it already has refreshed from the build's."""
    check_sets(sets)
    cols = COLUMNS["General"]
    build = {r["key"]: r for r in build_rows}
    unknown = sorted(set(sets or {}) - set(build))
    if unknown:
        raise ValueError(f"not General keys: {', '.join(unknown)}")
    p = Plan("General", [], len(sheet_rows))
    present = {str(r.get("key") or "").strip() for r in sheet_rows}
    for r in sheet_rows:
        row = {c: str(r.get(c, "")) for c in cols}
        key = row["key"].strip()
        if key in RETIRED_GENERAL:
            p.updated.append(f"{key} removed (no longer used)")
            continue
        if key in RENAMED_GENERAL:
            new = RENAMED_GENERAL[key][0]
            if new in present:
                p.updated.append(f"{key} removed ({new} is already on the tab)")
                continue
            row = {c: str(build[new].get(c, "")) for c in cols}
            p.updated.append(f"{key} renamed {new} = {row['value']}")
            present.add(new)
        elif "note" in take and key in build and row["note"].strip() != str(build[key].get("note", "")).strip():
            row["note"] = str(build[key].get("note", ""))
            p.updated.append(f"{key}: note refreshed")
        p.rows.append(row)
    for key, b in build.items():
        if key not in present:
            p.rows.append({c: str(b.get(c, "")) for c in cols})
            p.added.append(f"{key} = {b['value']}")
    for key, value in (sets or {}).items():
        row = next(r for r in p.rows if r["key"].strip() == key)
        if row["value"].strip() != str(value).strip():
            p.updated.append(f"{key}: {row['value'] or 'blank'} -> {value}")
            row["value"] = str(value)
    return p


def plan_tab(tab: str, sheet_rows: Sequence[Mapping[str, str]], build_rows: Sequence[Mapping[str, str]],
             sets: Mapping[str, str] | None = None, *, replace_drafts: bool = False,
             take: Sequence[str] = ()) -> Plan:
    if tab == "General":
        return plan_general(sheet_rows, build_rows, sets, take=take)
    cols = COLUMNS[tab]
    present = set().union(*(set(r) for r in sheet_rows)) if sheet_rows else set()
    p = Plan(tab, [], len(sheet_rows), new_columns=[c for c in cols if present and c not in present])
    sheet = {_key(r, tab): r for r in sheet_rows if _key(r, tab)}
    build = {_key(r, tab): r for r in build_rows}

    if tab in LEGACY_LAYOUT and LEGACY_LAYOUT[tab](present):
        p.replaced_layout = True
        p.rows = [{c: str(r.get(c, "")) for c in cols} for r in build_rows]
        p.added = [str(r[KEY[tab]]) for r in build_rows]
        return p
    if tab == "Copy" and replace_drafts:
        kept = [r for r in sheet_rows if _key(r, tab) and str(r.get("status", "")).strip().casefold() == "approved"]
        p.rows = [{c: str(r.get(c, "")) for c in cols} for r in kept]
        p.kept_from_sheet = [str(r.get(KEY[tab], "")) for r in kept]
        p.updated = [f"{r.get(KEY[tab], '')} removed (not approved)" for r in sheet_rows
                     if _key(r, tab) and r not in kept]
        have = {_key(r, tab) for r in kept}
        for k, r in build.items():
            if k not in have:
                p.rows.append({c: str(r.get(c, "")) for c in cols})
                p.added.append(str(r[KEY[tab]]))
        return p
    if tab in LEGACY_LAYOUT:
        p.rows = [{c: str(r.get(c, "")) for c in cols} for r in sheet_rows]
        p.kept_from_sheet = [str(r.get(KEY[tab], "")) for r in sheet_rows if _key(r, tab)]
        for k, r in build.items():
            if k not in sheet:
                p.rows.append({c: str(r.get(c, "")) for c in cols})
                p.added.append(str(r[KEY[tab]]))
        return p

    keep = tuple(c for c in KEEP.get(tab, ()) if c not in take)
    if tab in SHEET_WINS:
        keep = tuple(c for c in cols if c in present and c != KEY[tab] and c not in take
                     and c not in BUILD_OWNS.get(tab, ()))
    for k, r in build.items():
        row = {c: str(r.get(c, "")) for c in cols}
        old = sheet.get(k)
        if old is None:
            p.added.append(row[KEY[tab]])
        else:
            kept = [c for c in keep if str(old.get(c, "")).strip() != row[c].strip()
                    and (tab in SHEET_WINS or str(old.get(c, "")).strip())]
            for c in kept:
                row[c] = str(old[c])
            if kept:
                p.kept_from_sheet.append(f"{row[KEY[tab]]} ({', '.join(kept)})")
            if any(str(old.get(c, "")).strip() != row[c].strip() for c in cols if c not in kept):
                p.updated.append(row[KEY[tab]])
        p.rows.append(row)
    for k, old in sheet.items():
        if k not in build:
            row = {c: str(old.get(c, "")) for c in cols}
            by = SUPERSEDED.get(tab, {}).get(k)
            if by and "active" in cols and row["active"].strip().casefold() not in ("no", "false"):
                row.update(active="no", note=f"Replaced by {by} (1 Oct 2026). {row.get('note', '')}".strip())
                p.updated.append(f"{row[KEY[tab]]} switched off (replaced by {by})")
            p.rows.append(row)
            p.extra.append(str(old.get(KEY[tab], "")))
    return p


def load(ctx: Context, tabs: Sequence[str], sets: Mapping[str, str] | None = None, *,
         replace_drafts: bool = False, take: Sequence[str] = ()) -> dict:
    """Plan (and, live, write) each tab; refuses if the result would not validate."""
    bad = [t for t in tabs if t not in LOADABLE]
    if bad:
        raise ValueError(f"only {', '.join(LOADABLE)} can be loaded, not {', '.join(bad)}")
    check_sets(sets)  # before the sheet is read
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise ValueError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    from us_outbound.settings.model import OPTIONAL_TABS
    from us_outbound.settings.sync import read_sheet

    sheet = read_sheet(ctx, sheet_id)
    build = default_tabs()
    if sets and "General" not in tabs:
        raise ValueError("--set changes the General tab; load it too (--tab General)")
    kept = {c for t in tabs for c in (COLUMNS[t] if t in SHEET_WINS else KEEP.get(t, ()))}
    kept |= set(GENERAL_TAKE) if "General" in tabs else set()
    unknown = sorted(set(take) - kept)
    if unknown:
        raise ValueError(f"--take names a column the loaded tabs do not keep: {', '.join(unknown)}")
    plans = {t: plan_tab(t, sheet.get(t) or [], build[t], sets, replace_drafts=replace_drafts, take=take)
             for t in tabs}
    merged = {**sheet, **{t: p.rows for t, p in plans.items()}}
    _, errors = validate_all(merged)
    problems = [str(e) for t in (*tabs, "Tests", "Focus") for e in errors.get(t, [])]
    if problems:
        raise ValueError("the loaded tabs would not validate: " + "; ".join(problems[:10]))
    for t, p in plans.items():
        if t in OPTIONAL_TABS and not sheet.get(t) and p.rows:
            ctx.clients.sheets.add_tab(sheet_id, t)  # Focus may not be on the sheet yet; a no-op in dry-run
        ctx.clients.sheets.replace_tab(sheet_id, t, COLUMNS[t], p.rows)  # the guard skips it in dry-run
        log("settings_load", tab=t, dry_run=ctx.dry_run, **{k: v for k, v in p.summary().items() if k != "tab"})
    return {"dry_run": ctx.dry_run, "tabs": [p.summary() for p in plans.values()]}
