"""The weekly hand-check on the settings sheet (Harry, 12 Oct 2026: "I don't know how to action these slack
notifications"; the hand-check asked him to paste `handcheck approve --pull ...` into a terminal for 98 companies).

hand_check_draw writes the week's companies to the Hand-check tab, one row each (write: appended under every earlier
week, W0, 19 Oct 2026), and its Slack post links there. The columns (COLUMNS; W1-5, 19 Oct 2026):
  week, id, domain, name, group, industry, size, state     the company as it was drawn
  contact, title, opener                                   who enrol would write to, and the line it would open with
  check, why                                               random sample, or held for doubtful facts, and why
  decision                                                 fine, leave out or wrong industry (a dropdown)
  reason                                                   why left out (a dropdown, REASONS); words with spaces count
  right_label                                              the right industry, for wrong industry (a dropdown)
  contact_ok, opener_ok                                    yes or no: was the person, and the opening line, right
  note                                                     Harry's words; a decision that cannot be applied says why
  applied                                                  what was done and when, with the decision's hash
  gate_version                                             the labels' and ICP rules' hashes the company passed
The decisions:
  * fine            the facts are right: the company's doubts are cleared (the next verify_accounts decides on its
                    facts as usual). A company this tab left out earlier is put back (verified, or new for a doubtful
                    one), so a leave out changed to fine is undone;
  * leave out       never email it: the company is disqualified (tier Excluded, status_reason hand_check, the reason in
                    tier_reason). One already emailed is not changed: the judgement is still recorded;
  * wrong industry  the industry in right_label is set as an approver's correction (labels.correct: the account, a
                    label_corrected fact and the Overrides tab), and its doubts are cleared.

poll_approvals (every 5 minutes) runs apply. It first draws the week if its Monday draw did not happen
(hand_check.catch_up). Each decided row is applied, one at a time, and its own applied cell is written as soon as it
is done, with a hash of (decision, reason, right_label); a row is applied again whenever that hash changes, so a
later change is never lost (code-approvals-handcheck-copy B1). A decision that cannot be applied
(a wrong industry with no right_label, a decision not on the list) leaves applied blank and says why in note, and the
company stays held; one whose domain has no company says so and counts as settled (B2). Every applied row writes one
hand_check judgement fact (hand_check.event_id: one per account and week, upserted), so a row applied twice is still
one fact, and so are the doubt and label facts it writes (B3); a fine is recorded like any other (B4). Errors go to
hand_check_errors, a list heartbeat_check reads (ops/health.py), and never stop poll_approvals: a Sheets failure
ends the pass there, and the next poll goes on from the row it stopped at (a row done whose cell could not be
written is listed as unmarked, not applied: the next poll applies it again, to the same end). The two columns the old
tab had for a missing fact (right_hq_state, right_employees, kept by migrate-tab where they hold a value) are not read:
a row applied with a value in either says so in its applied cell, as the fact belongs on the Overrides tab.

A week closes (hand_check.close) once every sampled row is applied or settled, or HOLD_DAYS after the draw; a week
still open at Wednesday 09:00 UK posts one reminder (hand_check.remind).

Rows applied before 19 Oct 2026 carry no hash; one whose applied text matches its decision is adopted: its judgement
fact is written and the hash added, and nothing is done to the company again.

`us-outbound handcheck migrate-tab [--live]` (migrate) moves a tab still under the old 15 columns (OLD_COLUMNS) to
COLUMNS once, after a backup tab that is never overwritten; `us-outbound handcheck export --live` (export) copies
every decided row into the database as judgement facts without applying anything (W0).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from us_outbound import labels, verify
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.clients.sheets import a1_tab, column_letter, read_tab, write_tab
from us_outbound.context import UK, Context
from us_outbound.enrol.hand_check import (
    DECISIONS,
    DOUBT,
    FACT,
    FINE,
    LEAVE_OUT,
    SAMPLE,
    SOURCE,
    WRONG_INDUSTRY,
    event_id,
)
from us_outbound.logs import log
from us_outbound.scoring.score import score_account

TAB = "Hand-check"
COLUMNS = ["week", "id", "domain", "name", "group", "industry", "size", "state", "contact", "title", "opener", "check",
           "why", "decision", "reason", "right_label", "contact_ok", "opener_ok", "note", "applied", "gate_version"]
# The tab as it stood until W1-5 (19 Oct 2026), and the old names of two columns that moved.
OLD_COLUMNS = ["id", "week", "company", "domain", "check", "why", "hq_state", "size", "industry", "decision",
               "right_label", "right_hq_state", "right_employees", "note", "applied"]
RENAMED = {"company": "name", "hq_state": "state"}
OLD_NAME = {new: old for old, new in RENAMED.items()}
REASONS = ("media_publisher", "subsidiary", "it_services_outsourcing", "not_operating_business", "wrong_size",
           "wrong_state", "other")
YES_NO = ("yes", "no")
BY = "the Hand-check tab"
NOT_DONE = "not done: "
NOTE_JOIN = " — "  # between Harry's note and the code's "not done: ..."
NO_COMPANY = "no company with this domain"
# The old tab's columns for a missing fact: migrate keeps them where they hold a value, and apply never reads them.
NOT_READ = ("right_hq_state", "right_employees")
NOT_READ_NOTE = "not read: put the fact on the Overrides tab"
# A company already emailed (the statuses past verify; sources/apollo_universe.FURTHER_STATUSES): a leave out records,
# it changes nothing.
EMAILED = frozenset({"enrolled", "engaged", "demo_requested", "demo_booked"})
UNKNOWN_VERSION = "unknown"  # a row with no gate_version, of a week whose item has none: never today's, stamped late
SNAPSHOT = ("tier", "score", "industry_group", "industry", "size_band", "hq_state", "source", "label_source")
HASH_RE = re.compile(r"·\s*([0-9a-f]{10})\s*$")
# What the applied column said before W1-5 for each decision: such a row is adopted, not applied again.
LEGACY_APPLIED = {FINE: ": fine", LEAVE_OUT: ": left out", WRONG_INDUSTRY: ": industry set to "}

__all__ = ["DECISIONS", "DOUBT", "FACT", "FINE", "LEAVE_OUT", "SAMPLE", "SOURCE", "WRONG_INDUSTRY", "event_id"]


def row_id(week: str, domain: str) -> str:
    return f"{week}:{domain}"


def _size(a: Mapping[str, Any]) -> str:
    band, n = a.get("size_band") or "", a.get("employees")
    return f"{band} ({n} staff)" if band and n else str(band or (f"{n} staff" if n else ""))


def rows(ctx: Context, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """The week's rows: the random sample, then the companies held for doubtful facts that still wait."""
    from us_outbound.enrol import hand_check

    week = str(payload.get("iso_week") or "")
    version = str(payload.get("gate_version") or "")
    out: list[dict[str, str]] = []
    for a in payload.get("accounts") or ():
        c = a.get("contact") or {}
        out.append({"week": week, "id": row_id(week, a.get("domain") or a["account_id"]), "domain": a.get("domain") or "",
                    "name": a.get("clean_name") or "", "group": a.get("industry_group") or "",
                    "industry": a.get("industry") or "", "size": _size(a), "state": a.get("hq_state") or "",
                    "contact": c.get("role") or c.get("note") or "", "title": c.get("title") or "",
                    "opener": hand_check._opener(a), "check": SAMPLE,
                    "why": "picked at random from those about to be emailed: are the name, HQ state, size, contact "
                           "and opening line right?",
                    "gate_version": version})
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
        if missing - {verify.NO_INDUSTRY}:
            why += " (add the missing fact on the Overrides tab: hq_state or employees)"
        if verify.NO_INDUSTRY in missing:
            why += " (for the industry, pick wrong industry and the right one in right_label)"
        out.append({"week": week, "id": row_id(week, d.get("domain") or d["account_id"]), "domain": d.get("domain") or "",
                    "name": d.get("clean_name") or "", "group": ctx.settings.industry_group_of(a),
                    "industry": a.get("industry") or "", "size": _size(d), "state": d.get("hq_state") or "",
                    "check": DOUBT, "why": why, "gate_version": version})
    return out


def _fit(row: Mapping[str, str], header: Sequence[str]) -> dict[str, str]:
    """A row's cells placed by the tab's header: under the new names, or under the old names on a tab not migrated
    yet (company, hq_state). A column the tab does not have is left out, so the append never fails on it."""
    out: dict[str, str] = {}
    for k, v in row.items():
        if k in header:
            out[k] = v
        elif OLD_NAME.get(k) in header:
            out[OLD_NAME[k]] = v
    return out


def write(ctx: Context, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add the week's rows under the rows already on the tab. Live only.

    Append-only (W0, 19 Oct 2026; code-approvals-handcheck-copy B4): the tab is the only record of what Harry
    judged that a person can read, so no week's rows are ever cleared or rewritten. A row of this week already there
    (a rerun, or apply writing a week whose write failed) is left exactly as it is, its decision with it; only rows not
    yet on the tab are added, each placed under the header as it stands (_fit). The tab is written whole only when it
    has no rows at all (it is new, or only its header is there), as there is nothing on it to lose."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    new = rows(ctx, payload)
    if not sheet_id or not ctx.live:
        return {"rows": len(new), "written": False}
    try:
        kept = read_tab(ctx.clients.sheets, sheet_id, TAB)
        there = {str(r.get("id") or "").strip().casefold() for r in kept or []}
        added = [r for r in new if r["id"].casefold() not in there]
        if not kept:
            write_tab(ctx.clients.sheets, sheet_id, TAB, COLUMNS, added, kept)
        elif added:
            header = list(kept[0])
            ctx.clients.sheets.append_rows(sheet_id, TAB, [_fit(r, header) for r in added])
    except GuardViolation:
        raise
    except Exception as exc:  # the post still goes; the next poll_approvals writes the rows (apply)
        log("hand_check_tab_write_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return {"rows": len(new), "written": False}
    return {"rows": len(new), "added": len(added), "written": True}


# -- reading a row -------------------------------------------------------------------------------------


def _decision(r: Mapping[str, Any]) -> str:
    return " ".join(str(r.get("decision") or "").lower().split())


def _reason(r: Mapping[str, Any]) -> str:
    """The reason as one of REASONS ("media publisher" and "Media-Publisher" are media_publisher), "" when blank, and
    other for words that are none of them (the words themselves are kept in the fact's reason_text)."""
    words = "_".join(re.split(r"[\s\-_/]+", str(r.get("reason") or "").strip().lower())).strip("_")
    if not words:
        return ""
    return words if words in REASONS else "other"


def _yes_no(v: Any) -> str:
    t = str(v or "").strip().lower()
    return "yes" if t in {"yes", "y", "ok", "right", "true"} else "no" if t in {"no", "n", "wrong", "false"} else ""


def _hash(r: Mapping[str, Any]) -> str:
    """What the applied cell records of the decision: a change to any of these applies the row again."""
    key = "|".join((_decision(r), _reason(r), " ".join(str(r.get("right_label") or "").lower().split())))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _stamp(r: Mapping[str, Any]) -> str | None:
    m = HASH_RE.search(str(r.get("applied") or ""))
    return m.group(1) if m else None


def _done(r: Mapping[str, Any]) -> bool:
    """The row's decision, as it stands, has been applied."""
    return bool(_decision(r)) and _stamp(r) == _hash(r)


def _legacy(r: Mapping[str, Any]) -> bool:
    """A row applied before W1-5 (no hash), whose applied text says its decision was done."""
    text = str(r.get("applied") or "").strip()
    marker = LEGACY_APPLIED.get(_decision(r))
    return bool(text) and marker is not None and _stamp(r) is None and not text.startswith(NOT_DONE) and marker in text


def _harrys(note: Any) -> str:
    """The note without the code's "not done: ..." part."""
    text = str(note or "").strip()
    if text.startswith(NOT_DONE):
        return ""
    i = text.find(NOTE_JOIN + NOT_DONE)
    return text[:i].rstrip() if i >= 0 else text


def _with_why(note: Any, why: str) -> str:
    mine = _harrys(note)
    return f"{mine}{NOTE_JOIN}{NOT_DONE}{why}" if mine else f"{NOT_DONE}{why}"


def _domain(r: Mapping[str, Any]) -> str:
    return str(r.get("domain") or "").strip().lower()


def _account(ctx: Context, domain: str) -> dict | None:
    found = ctx.store.select("accounts", {"domain": domain}) if domain else []
    return dict(found[0]) if found else None


# -- the judgement facts ----------------------------------------------------------------------------


def _version(r: Mapping[str, Any], item: Mapping[str, Any] | None) -> str:
    """The gate version the row's company passed: the row's own cell, else the one its week's item recorded at the
    draw, else UNKNOWN_VERSION. Never the version in force when the row is applied: every W43 row, and every row
    appended before migrate-tab, has no cell, and today's hash would date an old judgement to a gate it never passed
    (the W1-5 review, 19 Oct 2026)."""
    payload = (item or {}).get("payload")
    drawn = payload.get("gate_version") if isinstance(payload, Mapping) else None
    return str(r.get("gate_version") or "").strip() or str(drawn or "").strip() or UNKNOWN_VERSION


def judgement(ctx: Context, r: Mapping[str, Any], a: Mapping[str, Any], week: str,
              item: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One row's judgement fact (W0's shape, plus the reason, the contact and opener answers and the gate version,
    _version), with a snapshot of the company as it stood before the decision did anything to it. item: the week's
    hand_check item, for the gate version recorded at the draw."""
    reason = _reason(r)
    typed = str(r.get("reason") or "").strip()
    value = {"week": week, "row_id": str(r.get("id") or ""), "check": str(r.get("check") or ""),
             "decision": _decision(r), "reason": reason, "right_label": str(r.get("right_label") or "").strip(),
             "contact_ok": _yes_no(r.get("contact_ok")), "opener_ok": _yes_no(r.get("opener_ok")),
             "note": _harrys(r.get("note")), "gate_version": _version(r, item),
             "snapshot": {c: a.get(c) for c in SNAPSHOT}}
    if reason == "other" and typed.lower() != "other":
        value["reason_text"] = typed
    eid = event_id(week, str(a["account_id"]))
    return {"event_id": eid, "account_id": a["account_id"], "source": SOURCE, "fact": FACT, "value": value,
            "quote": f"{week}: {value['decision']}", "source_url": "", "observed_at": ctx.now}


def _keep_first(ctx: Context, facts: Sequence[dict]) -> list[dict]:
    """The facts with the first one's snapshot and time kept, where one was written before: exporting or applying
    again is safe, and a changed decision is the one kept. So is the first one's gate version where this one's is
    unknown (W0's export stamped W43's facts on 19 Oct, nearer the draw than any later pass)."""
    before = {e["event_id"]: e for e in ctx.store.select("signal_events", {"event_id": [f["event_id"] for f in facts]})} \
        if facts else {}
    for fact in facts:
        old = before.get(fact["event_id"])
        if old is not None and isinstance(old.get("value"), Mapping):
            fact["value"]["snapshot"] = old["value"].get("snapshot", fact["value"]["snapshot"])
            fact["observed_at"] = old.get("observed_at") or fact["observed_at"]
            if fact["value"].get("gate_version") == UNKNOWN_VERSION and old["value"].get("gate_version"):
                fact["value"]["gate_version"] = old["value"]["gate_version"]
    return list(facts)


def export(ctx: Context) -> dict[str, Any]:
    """`us-outbound handcheck export [--live]`: one hand_check judgement fact per decided row of the Hand-check tab,
    every week's (W0, 19 Oct 2026; the database half of the fix for code-approvals-handcheck-copy B4).

    The facts are the ones apply writes (judgement), so exporting after apply changes nothing but a decision typed
    since; the companies are not touched. A second export updates the decision cells and keeps the first snapshot and
    time. Rows with no decision are not exported; a row whose domain has no company, or that apply would not apply
    (refused: a decision not one of DECISIONS, a wrong industry with no right_label it can read), is skipped with the
    reason, so an export never writes a judgement that apply holds back (held() would release its company, and tally
    count it). Dry-run counts what it would write and writes nothing."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise LookupError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    tab = read_tab(ctx.clients.sheets, sheet_id, TAB)
    if tab is None:
        raise LookupError(f"the settings sheet has no {TAB} tab")
    decided = [r for r in tab if _decision(r)]
    domains = sorted({_domain(r) for r in decided} - {""})
    found = {str(a.get("domain") or "").strip().lower(): dict(a)
             for a in (ctx.store.select("accounts", {"domain": domains}) if domains else [])}
    by_week = _by_week(ctx)
    facts: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []
    for r in decided:
        decision, domain, week = _decision(r), _domain(r), str(r.get("week") or "")
        a = found.get(domain)
        why = (_refused(ctx, r) if decision not in DECISIONS else "no week on the row" if not week.strip()
               else NO_COMPANY if a is None else _refused(ctx, r))
        if why:
            skipped.append({"id": str(r.get("id") or ""), "domain": domain, "why": why})
            continue
        fact = judgement(ctx, r, found[domain], week, by_week.get(week.strip()))
        facts[fact["event_id"]] = fact
    known = {e["event_id"] for e in ctx.store.select("signal_events", {"event_id": list(facts)})} if facts else set()
    _keep_first(ctx, list(facts.values()))
    weeks: dict[str, dict[str, int]] = {}
    for fact in facts.values():
        count = weeks.setdefault(fact["value"]["week"], {})
        count[fact["value"]["decision"]] = count.get(fact["value"]["decision"], 0) + 1
    out = {"dry_run": ctx.dry_run, "rows": len(tab), "decided": len(decided), "facts": len(facts),
           "new": sum(1 for e in facts if e not in known), "already": sum(1 for e in facts if e in known),
           "weeks": dict(sorted(weeks.items())), "skipped": skipped, "written": False}
    if ctx.live and facts:
        ctx.store.upsert("signal_events", list(facts.values()))
        out["written"] = True
    log("hand_check_export", run_id=ctx.run_id, dry_run=ctx.dry_run, facts=len(facts), new=out["new"],
        skipped=len(skipped), written=out["written"])
    return out


def undecided(ctx: Context, week: str) -> int | None:
    """How many of the week's rows still need a decision (none, or one that could not be applied); None when the tab
    cannot be read or has none of the week."""
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
    return sum(1 for r in mine if not _done(r) and not _legacy(r))


# -- applying a row ------------------------------------------------------------------------------------


@dataclass
class Outcome:
    done: str = ""  # what was done, for the applied cell; "" when nothing could be
    why: str = ""  # why not, for the note
    settled: bool = False  # nothing left to apply: no company has the row's domain


def _left_out_here(a: Mapping[str, Any]) -> bool:
    """Disqualified by this tab (status_reason hand_check; before W1-5, the tier_reason said so)."""
    return a.get("status") == "disqualified" and (
        a.get("status_reason") == "hand_check"
        or str(a.get("tier_reason") or "").startswith(("hand_check:", "left out at the hand-check")))


def _put_back(ctx: Context, a: Mapping[str, Any], check: str) -> str:
    """Undo this tab's leave out: a sampled company back to verified (it had passed verify), a doubtful one to new
    (verify_accounts decides on it again), scored now so it is in the queue for the next enrol."""
    status = "verified" if check == SAMPLE else "new"
    back = {**a, "status": status, "status_reason": None}
    events = ctx.store.select("signal_events", {"account_id": a["account_id"]})
    r = score_account(back, events, ctx.settings, ctx.today_uk())
    ctx.store.upsert("accounts", [{"account_id": a["account_id"], "status": status, "status_reason": None,
                                   "score": r.score, "tier": r.tier, "tier_reason": r.tier_reason}])
    return status


def _doubts(payload: Mapping[str, Any], account_id: str) -> list[dict]:
    return [d for d in payload.get("doubtful") or () if d.get("account_id") == account_id]


def _refused(ctx: Context, r: Mapping[str, Any]) -> str:
    """Why a decided row cannot be applied as it stands (the note says it), or "": a decision not on the list, or a
    wrong industry with no right_label that names an industry. export skips the same rows."""
    decision = _decision(r)
    if decision not in DECISIONS:
        return f"the decision must be one of {', '.join(DECISIONS)}"
    if decision == WRONG_INDUSTRY and labels.resolve(str(r.get("right_label") or ""), ctx.settings) is None:
        return "pick the right industry in right_label"
    return ""


def _apply_row(ctx: Context, r: Mapping[str, Any], item: Mapping[str, Any] | None) -> Outcome:
    """Do what one decided row says, and write its judgement fact."""
    from us_outbound.enrol import hand_check  # it builds on this module

    decision, domain, week = _decision(r), _domain(r), str(r.get("week") or "").strip()
    if decision not in DECISIONS:
        return Outcome(why=_refused(ctx, r))
    a = _account(ctx, domain)
    if a is None:
        return Outcome(why=NO_COMPANY, settled=True)
    why = _refused(ctx, r)
    if why:
        return Outcome(why=why)
    ind = labels.resolve(str(r.get("right_label") or ""), ctx.settings) if decision == WRONG_INDUSTRY else None
    fact = _keep_first(ctx, [judgement(ctx, r, a, week, item)])[0]  # the snapshot before the decision changes it
    payload = dict((item or {}).get("payload") or {})
    reason = _reason(r)
    aid = str(a["account_id"])
    if decision == LEAVE_OUT:
        if a.get("status") in EMAILED:
            done = "left out, but it was emailed already, so only the judgement is recorded"
        else:
            ctx.store.upsert("accounts", [{"account_id": aid, "status": "disqualified", "tier": "Excluded",
                                           "status_reason": "hand_check",
                                           "tier_reason": f"hand_check: {reason or 'no reason given'} ({week})"}])
            done = f"left out ({reason})" if reason else "left out"
    else:
        back = _put_back(ctx, a, str(r.get("check") or "")) if _left_out_here(a) else ""
        if ind is not None:
            labels.correct(ctx, a, ind, by=BY, via="sheet", who=f"on the {TAB} tab", week=week)
            done = f"industry set to {ind.industry}"
        else:
            done = "fine"
        verify.clear_doubts(ctx, _doubts(payload, aid), BY, week)
        if back:
            done += f"; back to {back}"
    ctx.store.upsert("signal_events", [fact])
    hand_check.pull(ctx, item, aid, out=decision == LEAVE_OUT)
    return Outcome(done=f"{ctx.today_uk().isoformat()}: {done}")


def _adopt(ctx: Context, r: Mapping[str, Any], item: Mapping[str, Any] | None) -> bool:
    """A row applied before W1-5: its judgement fact, and nothing done to the company. False when it has no company."""
    a = _account(ctx, _domain(r))
    if a is None:
        return False
    ctx.store.upsert("signal_events", _keep_first(ctx, [judgement(ctx, r, a, str(r.get("week") or "").strip(), item)]))
    return True


def _not_read(r: Mapping[str, Any]) -> str:
    """What the applied cell adds when a column apply never reads (NOT_READ) holds a value: a value typed there after
    the move is otherwise ignored with no word (the W1-5 review, round 2)."""
    typed = [c for c in NOT_READ if str(r.get(c) or "").strip()]
    return f"; {' and '.join(typed)} {'are' if len(typed) > 1 else 'is'} {NOT_READ_NOTE}" if typed else ""


def _write(ctx: Context, sheet_id: str, r: dict, cells: Mapping[str, str]) -> None:
    """Write the cells that change on this row, and keep them on r so the rest of the pass reads the tab as it is."""
    change = {k: v for k, v in cells.items() if str(r.get(k) or "") != v}
    if change:
        ctx.clients.sheets.update_rows(sheet_id, TAB, "id", {str(r["id"]).strip(): change})
        r.update(change)


def _settled(r: Mapping[str, Any], status: Mapping[str, str]) -> bool:
    """Nothing left for Harry on the row: its decision is applied, its company is not on file, or (a doubtful row)
    the company no longer waits for him."""
    if _done(r) or _legacy(r):
        return True
    d = _domain(r)
    if _decision(r) and d not in status:
        return True
    return r.get("check") == DOUBT and d in status and status[d] not in verify.WAITING_STATUSES


def apply(ctx: Context) -> dict[str, Any]:
    """Apply every decided row of the tab whose decision is not applied as it stands, one at a time; close the weeks
    that are done or HOLD_DAYS old; remind once about this week's. Live only (a dry run reports what it would apply).
    Never raises but a GuardViolation: poll_approvals goes on, and every failure is in hand_check_errors."""
    from us_outbound.enrol import hand_check

    out: dict[str, Any] = {"applied": [], "not_done": [], "unmarked": [], "adopted": 0, "closed": [],
                           "hand_check_errors": []}
    errors: list[str] = out["hand_check_errors"]
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        return out
    week = hand_check.this_week(ctx)
    if ctx.live:
        try:
            drew = hand_check.catch_up(ctx)
            if drew is not None:
                out["drawn"] = drew.get("status")
                if drew.get("number_error"):
                    errors.append(f"the catch-up draw: {drew['number_error']}")
        except GuardViolation:
            raise
        except Exception as exc:  # the draw's trouble: the decisions on the tab are still applied
            errors.append(f"the catch-up draw: {type(exc).__name__}: {str(exc)[:160]}")
            log("hand_check_catch_up_failed", error=errors[-1])
    # Read after the catch-up draw, so the week it recorded is here. close() and remind() each start from the item as
    # stored (hand_check._fresh), so the pulls the rows make in the pass below are never written over by this copy.
    by_week = _by_week(ctx)
    try:
        tab = read_tab(ctx.clients.sheets, sheet_id, TAB) or []
        item = by_week.get(week)
        if (item is not None and item.get("status") != "handled" and ctx.live
                and not any(str(r.get("week")) == week for r in tab)):
            # A week recorded but not on the tab (its write failed, or the tab was cleared): put its rows there.
            write(ctx, item.get("payload") or {})
            tab = read_tab(ctx.clients.sheets, sheet_id, TAB) or []
            out["written"] = sum(1 for r in tab if str(r.get("week")) == week)
    except GuardViolation:
        raise
    except Exception as exc:  # the sheet: the replies must still be worked
        errors.append(f"reading the {TAB} tab: {type(exc).__name__}: {str(exc)[:160]}")
        log("hand_check_tab_failed", error=errors[-1])
        return out
    todo = [r for r in tab if _decision(r) and not _done(r)]
    if not ctx.live:
        out["would_apply"] = [f"{r.get('domain')}: {_decision(r)}" for r in todo if not _legacy(r)]
        return out
    adopted: dict[str, dict[str, str]] = {}
    for r in todo:
        rid = str(r.get("id") or "").strip()
        if not rid:
            errors.append(f"a row for {r.get('domain') or 'no domain'} has no id, so it cannot be marked applied")
            continue
        try:
            if _legacy(r):
                if _adopt(ctx, r, by_week.get(str(r.get("week") or "").strip())):
                    adopted[rid] = {"applied": f"{str(r.get('applied')).strip()} · {_hash(r)}"}
                continue
            got = _apply_row(ctx, r, by_week.get(str(r.get("week") or "").strip()))
        except GuardViolation:
            raise
        except Exception as exc:  # this row's trouble: the others go on, and the next poll tries it again
            errors.append(f"{rid}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        if got.done:
            cells = {"applied": f"{got.done}{_not_read(r)} · {_hash(r)}", "note": _harrys(r.get("note"))}
        else:
            cells = {"applied": "", "note": _with_why(r.get("note"), got.why)}
        try:
            _write(ctx, sheet_id, r, cells)
        except GuardViolation:
            raise
        except ValueError as exc:  # this row's id is on the tab twice, or a column is missing: the others go on
            errors.append(f"{rid}: {str(exc)[:160]}")
            out["unmarked"].append(rid)
            continue
        except (ApiError, OSError) as exc:  # the sheet is failing: stop here; the next poll goes on from this row
            errors.append(f"{rid}: the {TAB} tab could not be written ({type(exc).__name__}: {str(exc)[:120]})")
            out["unmarked"].append(rid)
            break
        # Listed once its cell says so: a row done whose cell failed is applied again at the next poll (unmarked).
        out["applied" if got.done else "not_done"].append(rid)
    if adopted:
        try:
            ctx.clients.sheets.update_rows(sheet_id, TAB, "id", adopted)
            for r in tab:
                r.update(adopted.get(str(r.get("id") or "").strip(), {}))
            out["adopted"] = len(adopted)
        except GuardViolation:
            raise
        except Exception as exc:
            errors.append(f"marking {len(adopted)} earlier rows: {type(exc).__name__}: {str(exc)[:120]}")
    try:
        _close_and_remind(ctx, tab, by_week, week, out)
    except GuardViolation:
        raise
    except Exception as exc:
        errors.append(f"closing the week: {type(exc).__name__}: {str(exc)[:160]}")
    if out["applied"] or out["closed"] or out["adopted"] or out["unmarked"] or errors:
        log("hand_check_tab", run_id=ctx.run_id, week=week, applied=out["applied"], not_done=out["not_done"],
            unmarked=out["unmarked"], adopted=out["adopted"], closed=out["closed"], errors=errors[:5])
    return out


def _by_week(ctx: Context) -> dict[str, dict]:
    """ISO week -> its hand_check item (the one with the week's own item id, where a week has more than one)."""
    from us_outbound.enrol import enrol, hand_check

    by_week: dict[str, dict] = {}
    for it in hand_check.items(ctx):
        w = enrol._item_week(it)
        if w not in by_week or it.get("item_id") == hand_check.item_id(w):
            by_week[w] = it
    return by_week


def _close_and_remind(ctx: Context, tab: Sequence[dict], by_week: Mapping[str, dict], week: str,
                      out: dict[str, Any]) -> None:
    """Close each open week whose rows are all settled, or that is HOLD_DAYS old; remind once about this week's."""
    from us_outbound.enrol import hand_check

    domains = sorted({_domain(r) for r in tab} - {""})
    status = {str(a.get("domain") or "").strip().lower(): str(a.get("status") or "")
              for a in (ctx.store.select("accounts", {"domain": domains}) if domains else [])}
    for w, it in sorted(by_week.items()):
        if it.get("status") == "handled":
            continue
        mine = [r for r in tab if str(r.get("week")) == w]
        samples = [r for r in mine if r.get("check") == SAMPLE]
        counted = samples or [r for r in mine if r.get("check") == DOUBT]
        waiting = sum(1 for r in counted if not _settled(r, status))
        # Every drawn company has its row (a write that failed has not put them there yet): counted, not matched by
        # domain, as Harry may have corrected a row's domain.
        drawn = len((it.get("payload") or {}).get("accounts") or ())
        if counted and not waiting and len(samples) >= drawn:
            hand_check.close(ctx, it, BY)
            out["closed"].append(w)
        elif hand_check.expired(ctx, it):
            hand_check.close(ctx, it, BY, announce=False)
            out["closed"].append(w)
        elif w == week and mine:
            sent = hand_check.remind(ctx, it, waiting)
            if sent is not None:
                out["reminded"] = waiting


# -- the one-off header move ---------------------------------------------------------------------------------


def _header_after(before: Sequence[str], tab: Sequence[Mapping[str, str]]) -> list[str]:
    """COLUMNS, then any column of the old tab that has no place in them and holds a value in some row (a column Harry
    added, or right_hq_state where one was typed), so no value is lost; an empty one is dropped."""
    placed = set(COLUMNS) | set(RENAMED)
    extra = [c for c in before if c not in placed and any(str(r.get(c) or "").strip() for r in tab)]
    return [*COLUMNS, *extra]


def dropdowns(ctx: Context, sheet_id: str, header: Sequence[str]) -> dict[str, Sequence[str] | str]:
    """The tab's dropdowns: decision, reason, contact_ok and opener_ok from their lists, and right_label from the
    Industries tab's industry column."""
    out: dict[str, Sequence[str] | str] = {"decision": DECISIONS, "reason": REASONS, "contact_ok": YES_NO,
                                           "opener_ok": YES_NO}
    industries = read_tab(ctx.clients.sheets, sheet_id, "Industries") or []
    if industries and "industry" in industries[0]:
        letter = column_letter(list(industries[0]).index("industry"))
        out["right_label"] = f"{a1_tab('Industries')}!{letter}2:{letter}"
    return {k: v for k, v in out.items() if k in header}


EMPTY = (f"the {TAB} tab is empty: nothing to move, and an empty tab is never backed up. If a migrate-tab stopped part "
         f"way, the rows are on the newest '{TAB} backup' tab: copy them back to the {TAB} tab, then run it again.")


def _backup_tab(ctx: Context, sheet_id: str) -> tuple[str, list[dict[str, str]] | None]:
    """(the backup tab's name, read_tab's answer for it: None when there is no such tab yet, [] when it is there with
    nothing in it). Never a tab that holds rows: "Hand-check backup <date>", else the same with the time (UK) for a
    second run that day. The first round of W1-5 wrote today's backup again on every run, so a run after one that had
    cleared the tab and then failed backed up the empty tab over the only copy of every row (the W1-5 review, round 2).
    ValueError (the command's refusal) when both hold rows."""
    base = f"{TAB} backup {ctx.today_uk().isoformat()}"
    names = (base, f"{base} {ctx.now.astimezone(UK):%H%M}")
    for name in names:
        there = read_tab(ctx.clients.sheets, sheet_id, name)
        if not there:
            return name, there
    raise ValueError(f"the tabs {' and '.join(repr(n) for n in names)} both hold rows already: none is written "
                     "over, so run migrate-tab again in a minute")


def migrate(ctx: Context) -> dict[str, Any]:
    """`us-outbound handcheck migrate-tab [--live]`: move the tab to COLUMNS, once (W1-5, 19 Oct 2026; RS §2 W1-5).

    The tab's columns live here, not in `settings load` (settings/load.LOADABLE), so a new column never arrives by
    itself, and rows written under the old header would put values in the wrong places. Live: a backup tab
    "Hand-check backup <date>" gets the tab exactly as it is, and is read back to hold every row; then the tab is
    rewritten with the new header and every row's values under their columns (company becomes name, hq_state state; a
    column with no place and a value somewhere is kept at the right); then the dropdowns are set again on the columns'
    new places. A tab already under COLUMNS is left as it is (its dropdowns are set again, live). Dry-run prints the
    header before and after.

    The tab is the only record of Harry's judgements a person can read, and the rewrite is two calls (clear, then
    write), so a failure between them leaves it empty with the rows on the backup alone. So (the W1-5 review, round
    2): an empty tab is never moved or backed up (EMPTY says where the rows are); a backup tab that holds rows is never
    written over (_backup_tab); a failed rewrite says, on the error, which tab holds the rows. And it refuses
    (ValueError) while this week's hand-check is open: a decision typed while it runs could be lost, and with the tab
    empty poll_approvals would write this week's rows alone onto it (apply). Run it once the week's item is handled."""
    from us_outbound.enrol import hand_check

    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise LookupError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    sheets = ctx.clients.sheets
    tab = read_tab(sheets, sheet_id, TAB)
    if tab is None:
        raise LookupError(f"the settings sheet has no {TAB} tab")
    out: dict[str, Any] = {"dry_run": ctx.dry_run, "rows": len(tab), "before": [], "after": [], "already": False,
                           "backup": None, "written": False}
    if not tab:  # nothing to move, and backing up nothing could only lose a backup
        out["note"] = EMPTY
        log("hand_check_tab_migrated", run_id=ctx.run_id, rows=0, already=False, written=False, note="empty")
        return out
    before = list(tab[0])
    already = before[: len(COLUMNS)] == COLUMNS
    after = before if already else _header_after(before, tab)
    out.update(before=before, after=after, already=already)
    if not already:
        item = hand_check.current(ctx)
        if item is not None and item.get("status") != "handled":
            why = (f"this week's hand-check ({hand_check.this_week(ctx)}) is still open: run migrate-tab once its "
                   "rows are decided (or after `us-outbound handcheck approve`), so no decision is typed while the tab "
                   "moves")
            if ctx.live:
                raise ValueError(why)
            out["refused"] = why
        backup, there = _backup_tab(ctx, sheet_id)
        out["backup"] = backup
    if not ctx.live:
        return out
    if not already:
        moved = [{h: str({RENAMED.get(k, k): v for k, v in r.items()}.get(h, "")) for h in after} for r in tab]
        write_tab(sheets, sheet_id, backup, before, tab, there)  # added when it is not there, then written as it stood
        kept = read_tab(sheets, sheet_id, backup) or []
        if len(kept) != len(tab):
            raise RuntimeError(f"the backup tab {backup!r} holds {len(kept)} rows, not {len(tab)}: the {TAB} tab was "
                               "not touched")
        try:
            sheets.replace_tab(sheet_id, TAB, after, moved)
        except GuardViolation:
            raise
        except Exception as exc:
            log("hand_check_tab_migrate_failed", run_id=ctx.run_id, backup=backup, rows=len(tab),
                error=f"{type(exc).__name__}: {str(exc)[:160]}")
            exc.add_note(f"The {TAB} tab may be empty now: its {len(tab)} rows are on the tab {backup!r}, as they "
                         f"were. Copy them back to the {TAB} tab, then run migrate-tab again.")
            raise
        out["written"] = True
    try:
        sheets.set_validation(sheet_id, TAB, after, dropdowns(ctx, sheet_id, after))
        out["dropdowns"] = True
    except GuardViolation:
        raise
    except Exception as exc:  # the rows are moved; Harry can set the dropdowns by hand
        out["dropdowns"] = False
        out["dropdowns_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    log("hand_check_tab_migrated", run_id=ctx.run_id, rows=len(tab), already=already, written=out["written"],
        backup=out["backup"], dropdowns=out["dropdowns"])
    return out
