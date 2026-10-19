"""Approved definition changes written to the Industries tab (Harry, 11 Oct 2026: "the industry definitions ...
stored in the settings Google sheet as the source of truth").

The monthly label sample drafts changes for the labels the label check confused (learn/definition_suggestions.py)
onto the Definition suggestions tab, each with how the answer key scores with and without it. Harry sets decision
to approve or reject. settings_sync, before it reads the sheet, writes each approved line into its label's
definition cell on the Industries tab and dates the row's applied column, so the sheet stays the one source and the
new line is in force the same run. A row is applied once; a rejected one stays as a record.
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


def apply_approved(ctx: Context, sheet_id: str) -> dict[str, Any]:
    """Write each approved, not yet applied suggestion to the Industries tab (writes are skipped in dry-run).

    It never stops the sync (W0, 19 Oct 2026; code-platform-ops B8): settings_sync runs it before it reads the sheet,
    so a raise here kept every other edit Harry made, `live_sending = no` included, out of force on every run. A row
    that cannot be applied (two rows with one id, two Industries rows for one label, a sheet refusal) is recorded in
    errors and the sync goes on; a row with no id is skipped, as it could never be marked applied and each sync would
    write its definition again over any later hand edit."""
    sheets = ctx.clients.sheets
    out: dict[str, Any] = {"applied": [], "missing": [], "errors": []}
    try:
        rows = sheets.read_tabs(sheet_id, [TAB]).get(TAB) or []
    except GuardViolation:
        raise
    except Exception as exc:  # no tab yet (a 400), or the read failed: the sync goes on without it
        if not (isinstance(exc, ApiError) and exc.status == 400):
            log("definitions_read_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return out
    todo = [r for r in rows if str(r.get("decision") or "").strip().lower() == APPROVE
            and not str(r.get("applied") or "").strip() and str(r.get("suggested_definition") or "").strip()]
    # Every row's id, decided or not: the applied date is written by id, and update_rows refuses an id two rows share.
    ids = Counter(str(r.get("id") or "").strip().casefold() for r in rows)
    applied, missing, errors = out["applied"], out["missing"], out["errors"]
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
    if applied and ctx.live:
        try:
            sheets.update_rows(sheet_id, TAB, "id", {i: {"applied": ctx.today_uk().isoformat()} for i in applied})
        except GuardViolation:
            raise
        except (ValueError, ApiError) as exc:  # the definitions are in; the applied dates are written next run
            errors.append({"id": ", ".join(applied), "label": "", "error": "applied, but the applied date was not "
                                                                         f"written: {type(exc).__name__}: "
                                                                         f"{str(exc)[:160]}"})
    if applied or missing or errors:
        log("definitions_applied", run_id=ctx.run_id, applied=applied, missing=missing, errors=errors)
    return out
