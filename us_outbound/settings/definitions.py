"""Approved definition changes written to the Industries tab (Harry, 11 Oct 2026: "the industry definitions ...
stored in the settings Google sheet as the source of truth").

The monthly label sample drafts changes for the labels the label check confused (learn/definition_suggestions.py)
onto the Definition suggestions tab, each with how the answer key scores with and without it. Harry sets decision
to approve or reject. settings_sync, before it reads the sheet, writes each approved line into its label's
definition cell on the Industries tab and dates the row's applied column, so the sheet stays the one source and the
new line is in force the same run. A row is applied once; a rejected one stays as a record.

The Suggestions tab (W3-2, 20 Oct 2026; design-final L3): the ICP rules learn/precision.propose drafts each month from
the hand-check's leave-out reasons, one row each (SUGGESTION_COLUMNS: id, kind, target, text, evidence, snapshot,
status, applied). Its status is "proposed", or "rejected by eval" when the draft did worse on the hand-check's
judgements or on the answer key; Harry sets it to approved (or rejected). A row is applied only when its status is
approved, once (its applied date), before the sheet is read, as above:
  * definition_not  target: the labels (";"-separated); text: "not X", added to each label's definition;
  * exclude_naics   target: one label; text: "NAICS 5415; not X": the codes join the label's exclude_naics (the rules
                    leave those codes out, and the label check's list reads them: labels.Entry) and "not X" its
                    definition;
  * entity_text     target: entity(); a change to the label check's prompt, which engineering makes: never applied
                    here (W3-3 adds its own kinds, applied where they belong).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.logs import log

TAB = "Definition suggestions"
COLUMNS = ["id", "month", "label", "current_definition", "suggested_definition", "why", "answer_key_now",
           "answer_key_with", "decision", "applied"]
APPROVE, REJECT = DECISIONS = ("approve", "reject")
# The Suggestions tab (module docstring). Not a settings tab settings_sync validates (settings/model.TABS): like the
# Definition suggestions tab it is written by its proposer and read here, so a sheet without it still loads.
SUGGESTIONS_TAB = "Suggestions"
SUGGESTION_COLUMNS = ["id", "kind", "target", "text", "evidence", "snapshot", "status", "applied"]
DEFINITION_NOT, EXCLUDE_NAICS, ENTITY_TEXT = SUGGESTION_KINDS = ("definition_not", "exclude_naics", "entity_text")
PROPOSED, REJECTED_BY_EVAL, APPROVED = "proposed", "rejected by eval", "approved"
INDUSTRIES = "Industries"


def apply_approved(ctx: Context, sheet_id: str) -> dict[str, Any]:
    """Write each approved, not yet applied suggestion to the Industries tab (writes are skipped in dry-run): the
    Definition suggestions tab's (_apply_definitions), then the Suggestions tab's (_apply_suggestions), into one
    result. Never raises but a GuardViolation (below)."""
    out: dict[str, Any] = {"applied": [], "missing": [], "errors": []}
    _apply_definitions(ctx, sheet_id, out)
    _apply_suggestions(ctx, sheet_id, out)
    if out["applied"] or out["missing"] or out["errors"]:
        log("definitions_applied", run_id=ctx.run_id, applied=out["applied"], missing=out["missing"],
            errors=out["errors"])
    return out


def _read(ctx: Context, sheet_id: str, tab: str) -> list[dict[str, str]] | None:
    """The tab's rows, or None when it cannot be read (no tab yet: a 400; else logged): the sync goes on without."""
    try:
        return ctx.clients.sheets.read_tabs(sheet_id, [tab]).get(tab) or []
    except GuardViolation:
        raise
    except Exception as exc:
        if not (isinstance(exc, ApiError) and exc.status == 400):
            log("definitions_read_failed", tab=tab, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return None


def _shared_ids(rows: list[dict[str, str]]) -> Counter[str]:
    # Every row's id, decided or not: the applied date is written by id, and update_rows refuses an id two rows share.
    return Counter(str(r.get("id") or "").strip().casefold() for r in rows)


def _mark_applied(ctx: Context, sheet_id: str, tab: str, applied: list[str], errors: list[dict]) -> None:
    if not applied or not ctx.live:
        return
    try:
        ctx.clients.sheets.update_rows(sheet_id, tab, "id", {i: {"applied": ctx.today_uk().isoformat()}
                                                             for i in applied})
    except GuardViolation:
        raise
    except (ValueError, ApiError) as exc:  # the change is in; the applied dates are written next run
        errors.append({"id": ", ".join(applied), "label": "", "error": "applied, but the applied date was not "
                                                                     f"written: {type(exc).__name__}: "
                                                                     f"{str(exc)[:160]}"})


def _apply_definitions(ctx: Context, sheet_id: str, out: dict[str, Any]) -> None:
    """The Definition suggestions tab's approved rows, each written to its label's definition cell.

    It never stops the sync (W0, 19 Oct 2026; code-platform-ops B8): settings_sync runs it before it reads the sheet,
    so a raise here kept every other edit Harry made, `live_sending = no` included, out of force on every run. A row
    that cannot be applied (two rows with one id, two Industries rows for one label, a sheet refusal) is recorded in
    errors and the sync goes on; a row with no id is skipped, as it could never be marked applied and each sync would
    write its definition again over any later hand edit."""
    sheets = ctx.clients.sheets
    rows = _read(ctx, sheet_id, TAB)
    if rows is None:
        return
    todo = [r for r in rows if str(r.get("decision") or "").strip().lower() == APPROVE
            and not str(r.get("applied") or "").strip() and str(r.get("suggested_definition") or "").strip()]
    ids = _shared_ids(rows)
    applied: list[str] = []
    missing, errors = out["missing"], out["errors"]
    shared: set[str] = set()
    for r in todo:
        label, row_id = str(r.get("label") or "").strip(), str(r.get("id") or "").strip()
        if not row_id:
            errors.append({"id": "", "label": label,
                           "error": "skipped: the row has no id, so it could not be marked applied"})
            continue
        if ids[row_id.casefold()] > 1:  # neither row is applied: which one Harry meant is not ours to guess
            if row_id.casefold() not in shared:
                shared.add(row_id.casefold())
                errors.append({"id": row_id, "label": label,
                               "error": f"skipped: {ids[row_id.casefold()]} rows on the tab have this id; give each "
                                        "its own"})
            continue
        text = " ".join(str(r["suggested_definition"]).split())
        try:
            if sheets.update_cell(sheet_id, "Industries", {"industry": label}, "definition", text):
                applied.append(row_id)
            else:
                missing.append(label)
        except GuardViolation:
            raise
        except (ValueError, ApiError) as exc:  # two Industries rows for the label, or the sheet refused the write
            errors.append({"id": row_id, "label": label, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    _mark_applied(ctx, sheet_id, TAB, applied, errors)
    out["applied"].extend(applied)


def naics_text(text: str) -> tuple[tuple[str, ...], str]:
    """An exclude_naics row's text, "NAICS 5415, 541519; not IT services or outsourcing": (the codes, the "not X"
    words). Codes are 2 to 6 digits; no codes reads as ()."""
    head, _, rest = str(text or "").partition(";")
    words = head.strip()
    if words.upper().startswith("NAICS"):
        words = words[5:]
    codes = tuple(c for c in (x.strip() for x in words.replace(",", " ").split()) if c.isdigit() and 2 <= len(c) <= 6)
    return codes, " ".join(rest.split())


def _with_not(definition: str, text: str) -> str:
    """The definition with "not X" added, once: unchanged when it already says so."""
    text = " ".join(str(text or "").split())
    current = " ".join(str(definition or "").split())
    if not text or text.casefold() in current.casefold():
        return current
    return f"{current.rstrip(' .;')}; {text}" if current else text


def _with_codes(cell: str, codes: tuple[str, ...]) -> str:
    have = [c.strip() for c in str(cell or "").replace(",", ";").split(";") if c.strip()]
    return "; ".join([*have, *(c for c in codes if c not in have)])


def _apply_suggestions(ctx: Context, sheet_id: str, out: dict[str, Any]) -> None:
    """The Suggestions tab's rows with status approved and no applied date, of a kind applied here (definition_not,
    exclude_naics: the module docstring), each written to the Industries tab, then dated applied. A row of another
    status is never applied, whatever its other cells say (design-final W3-2: a Suggestions row cannot be applied
    without status approved). The same refusals as the Definition suggestions tab: two rows with one id, no id, a
    label not on the Industries tab (missing), a sheet refusal (errors)."""
    sheets = ctx.clients.sheets
    rows = _read(ctx, sheet_id, SUGGESTIONS_TAB)
    if not rows:
        return
    todo = [r for r in rows if str(r.get("status") or "").strip().casefold() == APPROVED
            and not str(r.get("applied") or "").strip()
            and str(r.get("kind") or "").strip() in (DEFINITION_NOT, EXCLUDE_NAICS)]
    if not todo:
        return
    industries = _read(ctx, sheet_id, INDUSTRIES)
    if industries is None:
        out["errors"].append({"id": "", "label": "", "error": "the Industries tab could not be read"})
        return
    by_label = {str(r.get("industry") or "").strip().casefold(): r for r in industries}
    ids = _shared_ids(rows)
    applied: list[str] = []
    missing, errors = out["missing"], out["errors"]
    for r in todo:
        row_id, kind = str(r.get("id") or "").strip(), str(r.get("kind") or "").strip()
        targets = [t.strip() for t in str(r.get("target") or "").split(";") if t.strip()]
        if not row_id:
            errors.append({"id": "", "label": "; ".join(targets),
                           "error": "skipped: the row has no id, so it could not be marked applied"})
            continue
        if ids[row_id.casefold()] > 1:
            errors.append({"id": row_id, "label": "; ".join(targets),
                           "error": f"skipped: {ids[row_id.casefold()]} rows on the tab have this id; give each its own"})
            continue
        codes, words = naics_text(r.get("text") or "") if kind == EXCLUDE_NAICS else ((), str(r.get("text") or ""))
        if kind == EXCLUDE_NAICS and not codes:
            errors.append({"id": row_id, "label": "; ".join(targets),
                           "error": "skipped: no NAICS code in the text (write it as NAICS 5415; not X)"})
            continue
        unknown = [t for t in targets if t.casefold() not in by_label]
        if not targets or unknown:
            missing.extend(unknown or ["(no target)"])
            continue
        gone: list[str] = []
        try:
            for t in targets:
                row = by_label[t.casefold()]
                label = str(row.get("industry") or t)
                changes = {}
                if codes:
                    changes["exclude_naics"] = _with_codes(row.get("exclude_naics") or "", codes)
                changes["definition"] = _with_not(row.get("definition") or "", words)
                for column, value in changes.items():
                    if value == " ".join(str(row.get(column) or "").split()):
                        continue
                    if not sheets.update_cell(sheet_id, INDUSTRIES, {"industry": label}, column, value):
                        gone.append(label)  # the row went since the read
                        break
                    row[column] = value
        except GuardViolation:
            raise
        except (ValueError, ApiError) as exc:
            errors.append({"id": row_id, "label": "; ".join(targets), "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            continue
        if gone:
            missing.extend(gone)
            continue
        applied.append(row_id)
    _mark_applied(ctx, sheet_id, SUGGESTIONS_TAB, applied, errors)
    out["applied"].extend(applied)
