"""A random sample of labelled companies for Harry to judge by hand (Harry, 10 Oct 2026: "pull the 50-company
sample"), so the labels' accuracy is measured against a person, not against the label check itself.

The crosswalk measures the rules against the companies whose label is known, and those are mostly the model's own
sure verdicts; the gold set is 13 companies. This is the independent number:
  * `us-outbound labels sample [--size 50] [--live]` draws the companies at random from those that are, or will be,
    emailed (verified onwards, with a label) and, with --live, writes them to the Label sample tab: the company, its
    site, what it does, the label and copy we would use and how it was decided, and three empty columns for Harry.
    It never overwrites a tab that already holds a verdict.
  * Harry fills your_verdict with one of VERDICTS (and right_label when ours is wrong).
  * `us-outbound labels sample --score` reads the tab back and prints the share right, with a 95% interval, and how
    many of the emails would have carried the wrong pitch for the copy the company gets.

Read-only otherwise: no model is asked, no credit spent, no company or card changed. settings_sync does not read
the tab.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound import labels
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.logs import log

JOB = "labels_sample"
TAB = "Label sample"
SIZE = 50
SEED = "label-sample"  # the same accounts draw the same sample
EMAILED = ("verified", "enrolled", "engaged", "demo_requested", "demo_booked")
RIGHT, RIGHT_GROUP, WRONG_GROUP, OUTSIDE = VERDICTS = ("right", "right group", "wrong group", "outside")
COLUMNS = ["domain", "company", "website", "they_do", "our_label", "our_group", "copy", "decided_by", "rules_said",
           "model_said", "your_verdict", "right_label", "note"]
# What a verdict means for the pitch: the copy a company gets is wrong when it says more than the verdict allows.
WRONG_PITCH = {labels.LABEL_COPY: {RIGHT_GROUP, WRONG_GROUP, OUTSIDE}, labels.GROUP_COPY: {WRONG_GROUP, OUTSIDE},
               labels.GENERAL_COPY_LEVEL: set()}


def read_tab(sheets: Any, sheet_id: str, tab: str) -> list[dict[str, str]] | None:
    """The tab's rows, or None when the sheet has no such tab (the Sheets API answers that read with a 400)."""
    try:
        return sheets.read_tabs(sheet_id, [tab]).get(tab) or []
    except ApiError as exc:
        if exc.status != 400:
            raise
        return None


def write_tab(sheets: Any, sheet_id: str, tab: str, columns: Sequence[str], cells: Sequence[Mapping[str, str]],
              rows: list[dict[str, str]] | None) -> None:
    """Rewrite the tab, adding it first when read_tab found none."""
    if rows is None:
        try:
            sheets.add_tab(sheet_id, tab)
        except Exception as exc:  # it is there, only empty
            log("sample_tab_exists", tab=tab, error=str(exc)[:120])
    sheets.replace_tab(sheet_id, tab, columns, cells)


def draw(ctx: Context, size: int = SIZE, seed: str = SEED) -> list[dict[str, str]]:
    """`size` companies at random from those emailed or to be emailed, as the tab's rows (empty verdict columns)."""
    accounts = [a for a in ctx.store.select("accounts", {"status": list(EMAILED)}) if str(a.get("industry") or "")]
    accounts.sort(key=lambda a: str(a["account_id"]))
    chosen = random.Random(seed).sample(accounts, min(size, len(accounts)))
    ids = [str(a["account_id"]) for a in chosen]
    checks: dict[str, list[dict]] = {}
    if ids:
        for e in ctx.store.select("signal_events", {"account_id": ids, "source": labels.JOB,
                                                    "fact": labels.VERDICT_FACT}):
            checks.setdefault(str(e["account_id"]), []).append(e)
    out = []
    for a in sorted(chosen, key=lambda a: str(a.get("domain") or "")):
        sv = labels.latest_stored(checks.get(str(a["account_id"]), []))
        v = sv.verdict if sv else None
        domain = str(a.get("domain") or "")
        conf = str(a.get("label_confidence") or "")
        out.append({
            "domain": domain, "company": str(a.get("clean_name") or domain), "website": f"https://{domain}",
            "they_do": (v.what_they_do or v.evidence) if v else "",
            "our_label": str(a.get("industry") or ""), "our_group": str(a.get("industry_group") or ""),
            "copy": labels.copy_level(a),
            "decided_by": str(a.get("label_source") or "not checked") + (f" ({conf})" if conf else ""),
            "rules_said": (sv.rules or "no label") if sv else "",
            "model_said": f"{v.label} ({v.confidence})" if v else "not asked",
            "your_verdict": "", "right_label": "", "note": "",
        })
    return out


def run(ctx: Context, size: int = SIZE) -> dict[str, Any]:
    """`us-outbound labels sample`: draw, and with --live write the tab, unless it already holds verdicts."""
    cells = draw(ctx, size)
    sheet_id = ctx.guard.bounds.settings_sheet_id
    written, kept = False, 0
    if ctx.live and sheet_id:
        rows = read_tab(ctx.clients.sheets, sheet_id, TAB)
        kept = sum(1 for r in rows or [] if str(r.get("your_verdict") or "").strip())
        if not kept:
            write_tab(ctx.clients.sheets, sheet_id, TAB, COLUMNS, cells, rows)
            written = True
    log(JOB, run_id=ctx.run_id, drawn=len(cells), written=written, kept=kept)
    return {"drawn": len(cells), "rows": cells, "tab_written": written, "verdicts_kept": kept}


def interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson 95% interval for k of n."""
    if not n:
        return 0.0, 0.0
    p = k / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, mid - half), min(1.0, mid + half)


def score(rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """Harry's verdicts counted: label right, group right, and emails whose copy says more than the verdict allows."""
    judged, unreadable = [], []
    for r in rows:
        said = " ".join(str(r.get("your_verdict") or "").lower().split())
        if said in VERDICTS:
            judged.append((r, said))
        elif said:
            unreadable.append(str(r.get("domain") or ""))
    n = len(judged)
    verdicts = Counter(s for _, s in judged)
    wrong_pitch = [r for r, s in judged if s in WRONG_PITCH.get(str(r.get("copy") or ""), set())]
    label_right, group_right = verdicts[RIGHT], verdicts[RIGHT] + verdicts[RIGHT_GROUP]
    return {
        "rows": len(rows), "judged": n, "unreadable": unreadable, "verdicts": dict(verdicts),
        "label_right": label_right, "group_right": group_right, "wrong_pitch": len(wrong_pitch),
        "intervals": {"label_right": interval(label_right, n), "group_right": interval(group_right, n),
                      "wrong_pitch": interval(len(wrong_pitch), n)},
        "wrong": [{"domain": str(r.get("domain") or ""), "ours": str(r.get("our_label") or ""),
                   "copy": str(r.get("copy") or ""), "verdict": s, "right_label": str(r.get("right_label") or "")}
                  for r, s in judged if s != RIGHT],
    }


def run_score(ctx: Context) -> dict[str, Any]:
    """`us-outbound labels sample --score`: read the tab and count Harry's verdicts."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    rows = read_tab(ctx.clients.sheets, sheet_id, TAB) if sheet_id else None
    if rows is None:
        raise LookupError(f"the settings sheet has no {TAB} tab: run `us-outbound labels sample --live` first")
    out = score(rows)
    log(JOB + "_score", run_id=ctx.run_id, judged=out["judged"], label_right=out["label_right"],
        group_right=out["group_right"], wrong_pitch=out["wrong_pitch"])
    return out
