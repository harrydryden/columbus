"""The Suggestions tab: the learning loops' proposals, one row each, for Harry to approve (design §5; W3-3, 20 Oct 2026).

"Proposes" in the loops (design §5) means a row here, the pattern the Definition suggestions tab set
(learn/definition_suggestions.py): nothing changes until Harry sets status to approved, and each row carries its
evidence and a snapshot of the numbers behind it, so the loops themselves can be audited (RT §6.10). W3-2 writes its
gate and definition proposals here too; the columns are the plan's: id, kind, target, text, evidence, snapshot,
status, applied.

The kinds W3-3 writes (loop L5, contactability; from pick_contacts' facts, contacts/pick.suggestions):
  roles_title   a title pick_contacts left out as "not contacted at this size" at TITLE_MISSES or more accounts in one
                Roles-tab size range: add it to a Roles-tab row (target: the size range; text: the title).
  domain_alias  an email domain revealed at least ALIAS_SEEN times for one account and rejected as not the company's:
                add it to domain_aliases for the account's domain (target: the account's domain; text: the alias).
Never a CA or WA contact, a junior title or more than two reveals an account: those rules stay in pick_contacts, and a
proposal only adds a title or an alias for Harry to judge.

write() is append-only (as the Hand-check tab, W0): a row already on the tab, whatever Harry did with it, is never
rewritten; only ids not there yet are added. Live only: in a dry run nothing reaches the sheet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.sheets import read_tab, write_tab
from us_outbound.logs import log

TAB = "Suggestions"
COLUMNS = ("id", "kind", "target", "text", "evidence", "snapshot", "status", "applied")
ROLES_TITLE, DOMAIN_ALIAS = "roles_title", "domain_alias"
KINDS = {
    ROLES_TITLE: "a title to add to a Roles-tab row for the size range named in target",
    DOMAIN_ALIAS: "an email domain to take as the account's own (domain_aliases), for the domain named in target",
}
PROPOSED = "proposed"
TITLE_MISSES = 10  # accounts in one size range where the title was left out (loop L5)
ALIAS_SEEN = 2  # revealed emails at one other domain for one account (loop L5)


def write(ctx: Any, rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """Add the rows whose id is not on the tab yet, under the rows already there. Live only; never raises (but a
    GuardViolation): a sheet that cannot be written leaves the rows for the next week's run, which proposes them again."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not rows:
        return {"rows": 0, "written": False}
    if not sheet_id or not ctx.live:
        return {"rows": len(rows), "written": False}
    try:
        kept = read_tab(ctx.clients.sheets, sheet_id, TAB)
        there = {str(r.get("id") or "").strip().casefold() for r in kept or []}
        added = [dict(r) for r in rows if str(r["id"]).casefold() not in there]
        if not kept:
            write_tab(ctx.clients.sheets, sheet_id, TAB, COLUMNS, added, kept)
        elif added:
            header = list(kept[0])
            ctx.clients.sheets.append_rows(sheet_id, TAB, [{k: v for k, v in r.items() if k in header} for r in added])
    except GuardViolation:
        raise
    except Exception as exc:
        log("suggestions_write_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return {"rows": len(rows), "written": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {"rows": len(rows), "added": len(added), "written": True}
