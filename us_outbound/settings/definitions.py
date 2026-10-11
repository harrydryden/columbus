"""Approved definition changes written to the Industries tab (Harry, 11 Oct 2026: "the industry definitions ...
stored in the settings Google sheet as the source of truth").

The monthly label sample drafts changes for the labels the label check confused (learn/definition_suggestions.py)
onto the Definition suggestions tab, each with how the answer key scores with and without it. Harry sets decision
to approve or reject. settings_sync, before it reads the sheet, writes each approved line into its label's
definition cell on the Industries tab and dates the row's applied column, so the sheet stays the one source and the
new line is in force the same run. A row is applied once; a rejected one stays as a record.
"""

from __future__ import annotations

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
    """Write each approved, not yet applied suggestion to the Industries tab (writes are skipped in dry-run)."""
    sheets = ctx.clients.sheets
    try:
        rows = sheets.read_tabs(sheet_id, [TAB]).get(TAB) or []
    except GuardViolation:
        raise
    except Exception as exc:  # no tab yet (a 400), or the read failed: the sync goes on without it
        if not (isinstance(exc, ApiError) and exc.status == 400):
            log("definitions_read_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return {"applied": [], "missing": []}
    todo = [r for r in rows if str(r.get("decision") or "").strip().lower() == APPROVE
            and not str(r.get("applied") or "").strip() and str(r.get("suggested_definition") or "").strip()]
    applied, missing = [], []
    for r in todo:
        label = str(r.get("label") or "").strip()
        text = " ".join(str(r["suggested_definition"]).split())
        if sheets.update_cell(sheet_id, "Industries", {"industry": label}, "definition", text):
            applied.append(str(r.get("id") or label))
        else:
            missing.append(label)
    if applied and ctx.live:
        sheets.update_rows(sheet_id, TAB, "id", {i: {"applied": ctx.today_uk().isoformat()} for i in applied})
    if applied or missing:
        log("definitions_applied", run_id=ctx.run_id, applied=applied, missing=missing)
    return {"applied": applied, "missing": missing}
