"""The weekly hand-check on the settings sheet (Harry, 12 Oct 2026: "I don't know how to action these slack
notifications"; the hand-check asked him to paste `handcheck approve --pull ...` into a terminal for 98 companies).

hand_check_post writes the week's companies to the Hand-check tab, one row each (write), and its Slack post links
there. Harry picks a decision on each row:
  * fine            the facts are right: the company's doubts are cleared, and the next company check (verify_accounts,
                    weekdays 04:30 UK) decides on its facts as usual. A right_hq_state or right_employees typed on the
                    row goes onto the Overrides tab first (a missing HQ state or size needs one; approving alone cannot
                    supply it);
  * leave out       never email it: the company is disqualified (status disqualified, tier Excluded) and pulled from
                    the week's check;
  * wrong industry  the industry in right_label is set as an approver's correction (labels.correct: the account, a
                    label_corrected fact and the Overrides tab), any card it no longer fits is withdrawn, and its doubts
                    are cleared.
poll_approvals (every 5 minutes) applies each decided row once (apply) and dates its applied column. When every row of
the week has a decision, the week's item is marked handled with the left-out companies as its pulls
(hand_check.approve), which posts the confirmation, as `handcheck approve` does; the command still works.
A row left blank keeps that company waiting; nothing else waits on it unless auto_send = yes (then enrol waits for the
whole check, as before).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound import labels, verify
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.sheets import read_tab, write_tab
from us_outbound.context import Context
from us_outbound.logs import log

TAB = "Hand-check"
COLUMNS = ["id", "week", "company", "domain", "check", "why", "hq_state", "size", "industry", "decision",
           "right_label", "right_hq_state", "right_employees", "note", "applied"]
FINE, LEAVE_OUT, WRONG_INDUSTRY = DECISIONS = ("fine", "leave out", "wrong industry")
SAMPLE, DOUBT = "random sample", "doubtful facts"
BY = "the Hand-check tab"
OVERRIDES_TAB = "Overrides"


def row_id(week: str, domain: str) -> str:
    return f"{week}:{domain}"


def _size(a: Mapping[str, Any]) -> str:
    band, n = a.get("size_band") or "", a.get("employees")
    return f"{band} ({n} staff)" if band and n else str(band or (f"{n} staff" if n else ""))


def rows(ctx: Context, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """The week's rows: the random sample, then the companies held for doubtful facts that still wait."""
    week = str(payload.get("iso_week") or "")
    out: list[dict[str, str]] = []
    for a in payload.get("accounts") or ():
        out.append({"id": row_id(week, a.get("domain") or a["account_id"]), "week": week,
                    "company": a.get("clean_name") or "", "domain": a.get("domain") or "", "check": SAMPLE,
                    "why": "picked at random: are the name, HQ state, size, contact and opening line right?",
                    "hq_state": a.get("hq_state") or "", "size": _size(a), "industry": a.get("industry") or ""})
    doubtful = list(payload.get("doubtful") or ())
    ids = [d["account_id"] for d in doubtful]
    waiting = {a["account_id"]: a for a in ctx.store.select("accounts", {"account_id": ids,
                                                                         "status": list(verify.WAITING_STATUSES)})} \
        if ids else {}
    for d in doubtful:
        a = waiting.get(d["account_id"])
        if a is None:  # verified, left out or disqualified since: nothing to decide
            continue
        missing = verify.MISSING & set(d.get("reasons") or ())
        why = "; ".join(d.get("reasons") or ())
        columns = [c for m, c in ((verify.NO_STATE, "right_hq_state"), (verify.NO_SIZE, "right_employees"))
                   if m in missing]
        hints = [f"type it in {' and '.join(columns)}, then pick fine"] if columns else []
        if verify.NO_INDUSTRY in missing:
            hints.append("for the industry, pick wrong industry and the right one in right_label")
        if hints:
            why += f" ({'; '.join(hints)})"
        out.append({"id": row_id(week, d.get("domain") or d["account_id"]), "week": week,
                    "company": d.get("clean_name") or "", "domain": d.get("domain") or "", "check": DOUBT,
                    "why": why, "hq_state": d.get("hq_state") or "", "size": _size(d),
                    "industry": a.get("industry") or ""})
    return out


def write(ctx: Context, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Put the week's rows on the tab, keeping any decision already made on a row (a rerun never loses one). Other
    weeks' rows are dropped: their companies come back on this week's check if they still wait. Live only."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    new = rows(ctx, payload)
    if not sheet_id or not ctx.live:
        return {"rows": len(new), "written": False}
    try:
        kept = read_tab(ctx.clients.sheets, sheet_id, TAB)
        before = {str(r.get("id") or ""): r for r in kept or []}
        merged = []
        for r in new:
            old = before.get(r["id"]) or {}
            merged.append({**r, **{c: str(old.get(c) or "") for c in ("decision", "right_label", "right_hq_state",
                                                                       "right_employees", "note", "applied")}})
        write_tab(ctx.clients.sheets, sheet_id, TAB, COLUMNS, merged, kept)
    except GuardViolation:
        raise
    except Exception as exc:  # the post still goes; the next poll_approvals writes the rows (apply)
        log("hand_check_tab_write_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return {"rows": len(new), "written": False}
    return {"rows": len(merged), "written": True}


def undecided(ctx: Context, week: str) -> int | None:
    """How many of the week's rows still need a decision; None when the tab cannot be read or has none of the week."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        return None
    try:
        tab = read_tab(ctx.clients.sheets, sheet_id, TAB)
    except GuardViolation:
        raise
    except Exception:
        return None
    mine = [r for r in tab or [] if str(r.get("week")) == week]
    if not mine:  # not written yet: the item's own count stands
        return None
    return sum(1 for r in mine if not _decision(r) and not str(r.get("applied") or "").strip())


def _decision(r: Mapping[str, Any]) -> str:
    return " ".join(str(r.get("decision") or "").lower().split())


def _account(ctx: Context, domain: str) -> dict | None:
    found = ctx.store.select("accounts", {"domain": domain.strip().lower()})
    return dict(found[0]) if found else None


def _settled(ctx: Context, tab_rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The rows whose company no longer waits (the label check left it out, or it was verified), with its status."""
    domains = [str(r.get("domain") or "").strip().lower() for r in tab_rows if r.get("domain")]
    if not domains:
        return []
    status = {str(a.get("domain") or "").lower(): str(a.get("status") or "")
              for a in ctx.store.select("accounts", {"domain": domains})}
    return [{**r, "_status": status[d]} for r in tab_rows
            if (d := str(r.get("domain") or "").strip().lower()) in status and status[d] not in verify.WAITING_STATUSES]


def _overrides(ctx: Context, sheet_id: str, domain: str, r: Mapping[str, Any]) -> list[str]:
    """The Overrides rows a "fine" row's corrections need (HQ state, employees); their field names."""
    out = []
    for column, field in (("right_hq_state", "hq_state"), ("right_employees", "employees")):
        value = str(r.get(column) or "").strip()
        if not value:
            continue
        if not ctx.clients.sheets.update_cell(sheet_id, OVERRIDES_TAB, {"domain": domain, "field": field}, "value",
                                              value):
            ctx.clients.sheets.append_rows(sheet_id, OVERRIDES_TAB, [{"domain": domain, "field": field, "value": value,
                                                                      "note": f"from {BY}"}])
        out.append(field)
    return out


def _doubts(payload: Mapping[str, Any], account_id: str) -> list[dict]:
    return [d for d in payload.get("doubtful") or () if d.get("account_id") == account_id]


def _apply_row(ctx: Context, sheet_id: str, payload: Mapping[str, Any], week: str, r: Mapping[str, Any]) -> str:
    """Do what one decided row says; the text for its applied column (or why it could not be done)."""
    from us_outbound.enrol import approvals  # the card withdrawal, after a label change

    decision, domain = _decision(r), str(r.get("domain") or "").strip().lower()
    a = _account(ctx, domain)
    if a is None:
        return "not done: no company with this domain"
    day = ctx.today_uk().isoformat()
    if decision == LEAVE_OUT:
        ctx.store.upsert("accounts", [{"account_id": a["account_id"], "status": "disqualified", "tier": "Excluded",
                                       "tier_reason": f"left out at the hand-check ({week})"}])
        return f"{day}: left out"
    if decision == WRONG_INDUSTRY:
        ind = labels.resolve(str(r.get("right_label") or ""), ctx.settings)
        if ind is None:
            return "not done: pick the right industry in right_label"
        labels.correct(ctx, a, ind, by=BY, via="sheet", who=f"on the {TAB} tab")
        found = approvals.unfit_cards(ctx, {a["account_id"]: ctx.store.get("accounts", account_id=a["account_id"])})
        approvals.withdraw_unfit(ctx, approvals.slack_or_none(ctx), found)
        verify.clear_doubts(ctx, _doubts(payload, a["account_id"]), BY, week)
        return f"{day}: industry set to {ind.industry}"
    if decision == FINE:
        fixed = _overrides(ctx, sheet_id, domain, r)
        verify.clear_doubts(ctx, _doubts(payload, a["account_id"]), BY, week)
        return f"{day}: fine" + (f"; {' and '.join(fixed)} added to the Overrides tab" if fixed else "")
    return f"not done: decision must be one of {', '.join(DECISIONS)}"


def apply(ctx: Context) -> dict[str, Any]:
    """Apply the decided, not yet applied rows of this week's open hand-check; close the week once every row has a
    decision. Live only (a dry run reports what it would do). Never raises but a GuardViolation: poll_approvals goes
    on."""
    from us_outbound.enrol import hand_check

    out: dict[str, Any] = {"applied": [], "closed": False}
    sheet_id = ctx.guard.bounds.settings_sheet_id
    week = hand_check.this_week(ctx)
    item = hand_check.current(ctx, week)
    if ctx.live and ctx.settings.general.auto_send and (item is None or hand_check.needs_sample(ctx, item)):
        # auto_send switched on mid-week: enrol waits for the week's sample, so post it now rather than on Monday
        out["posted"] = hand_check.post(ctx).get("status")
        item = hand_check.current(ctx, week)
    if not sheet_id or item is None or item.get("status") == "handled":
        return out
    try:
        payload = dict(item.get("payload") or {})
        tab = read_tab(ctx.clients.sheets, sheet_id, TAB) or []
        mine = [r for r in tab if str(r.get("week")) == week]
        if not mine and ctx.live:  # a week posted before the tab existed, or the tab cleared: put its rows there
            write(ctx, payload)
            tab = read_tab(ctx.clients.sheets, sheet_id, TAB) or []
            mine = [r for r in tab if str(r.get("week")) == week]
            out["written"] = len(mine)
        todo = [r for r in mine if _decision(r) and not str(r.get("applied") or "").strip()]
        if not ctx.live:
            out["would_apply"] = [f"{r.get('domain')}: {_decision(r)}" for r in todo]
            return out
        done: dict[str, dict[str, str]] = {}
        for r in todo:
            done[str(r["id"])] = {"applied": _apply_row(ctx, sheet_id, payload, week, r)}
        settled = _settled(ctx, [r for r in mine if not _decision(r) and not str(r.get("applied") or "").strip()
                                 and r.get("check") == DOUBT])
        for r in settled:  # verified, left out or disqualified since the row was written: nothing left to decide
            done[str(r["id"])] = {"applied": f"{ctx.today_uk().isoformat()}: settled without you ({r['_status']})"}
        if done:
            ctx.clients.sheets.update_rows(sheet_id, TAB, "id", done)
            out["applied"] = sorted(done)
        if mine and all(_decision(r) or str(r.get("applied") or "").strip() or str(r["id"]) in done for r in mine):
            left_out = [str(r.get("domain")) for r in mine if _decision(r) == LEAVE_OUT]
            hand_check.approve(ctx, left_out, BY, clear=False)
            out["closed"] = True
    except GuardViolation:
        raise
    except Exception as exc:  # the sheet or the database: the replies and cards must still be worked
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        log("hand_check_tab_failed", error=out["error"])
        return out
    if out["applied"] or out["closed"]:
        log("hand_check_tab", run_id=ctx.run_id, week=week, applied=out["applied"], closed=out["closed"])
    return out

