"""Labelled companies judged by hand, a batch a month (Harry, 10 Oct 2026: "pull the 50-company sample"; 11 Oct:
"the model classification can continuously improve over time"), so the labels' accuracy is measured against a person,
not against the label check itself, and the judged companies become the label check's answer key.

  * The Label sample tab on the settings sheet holds every batch: the company, its site, what it does, the label and
    copy we use and how it was decided, and three columns for Harry (your_verdict: the right label, or one of
    VERDICTS; right_label; note). A batch is never redrawn: judged rows stay, as the answer key.
  * `label_sample_monthly` (the 1st of each month, 09:15) scores last month's batch, adds a new one of MONTHLY
    companies not on the tab yet, and posts both to Slack with a link to the tab (say.sheet_link) and what to fill in. The draft definition changes for
    the misses follow (learn/definition_suggestions.py).
  * `us-outbound labels sample [--size 50] [--live]` adds a batch now; `--score` scores every batch.
  * `us-outbound labels eval --from-sample` scores the model on every judged company (labels.judged_rows).

Read-only otherwise: no model is asked, no credit spent, no company or card changed. settings_sync does not read
the tab.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound import fmt, labels
from us_outbound.base import say
from us_outbound.clients.sheets import read_tab, write_tab
from us_outbound.context import Context
from us_outbound.learn import definition_suggestions
from us_outbound.logs import log
from us_outbound.settings.model import Industry, Settings

JOB = "labels_sample"
MONTHLY_JOB = "label_sample_monthly"
TAB = "Label sample"
SIZE = 50  # the first batch (Harry judged 50 on 11 Oct 2026)
MONTHLY = 25
SEED = "label-sample"  # the same accounts and batch draw the same companies
EMAILED = ("verified", "enrolled", "engaged", "demo_requested", "demo_booked")
RIGHT, RIGHT_GROUP, WRONG_GROUP, OUTSIDE = VERDICTS = ("right", "right group", "wrong group", "outside")
COLUMNS = ["batch", "domain", "company", "website", "they_do", "our_label", "our_group", "copy", "decided_by",
           "rules_said", "model_said", "your_verdict", "right_label", "note"]
# What a verdict means for the pitch: the copy a company gets is wrong when it says more than the verdict allows.
WRONG_PITCH = {labels.LABEL_COPY: {RIGHT_GROUP, WRONG_GROUP, OUTSIDE}, labels.GROUP_COPY: {WRONG_GROUP, OUTSIDE},
               labels.GENERAL_COPY_LEVEL: set()}


def batch_name(ctx: Context) -> str:
    """This month's batch, "2026-11" (UK time)."""
    return ctx.today_uk().strftime("%Y-%m")


def draw(ctx: Context, size: int = SIZE, seed: str = SEED, *, batch: str = "",
         exclude: frozenset[str] = frozenset()) -> list[dict[str, str]]:
    """`size` companies at random from those emailed or to be emailed and not in `exclude` (domains), as the tab's
    rows (empty verdict columns)."""
    accounts = [a for a in ctx.store.select("accounts", {"status": list(EMAILED)})
                if str(a.get("industry") or "") and str(a.get("domain") or "") not in exclude]
    accounts.sort(key=lambda a: str(a["account_id"]))
    chosen = random.Random(f"{seed}:{batch}" if batch else seed).sample(accounts, min(size, len(accounts)))
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
            "batch": batch, "domain": domain, "company": str(a.get("clean_name") or domain),
            "website": f"https://{domain}", "they_do": (v.what_they_do or v.evidence) if v else "",
            "our_label": str(a.get("industry") or ""), "our_group": str(a.get("industry_group") or ""),
            "copy": labels.copy_level(a),
            "decided_by": str(a.get("label_source") or "not checked") + (f" ({conf})" if conf else ""),
            "rules_said": (sv.rules or "no label") if sv else "",
            "model_said": f"{v.label} ({v.confidence})" if v else "not asked",
            "your_verdict": "", "right_label": "", "note": "",
        })
    return out


FIRST_BATCH = "2026-10"  # rows written before the batch column (Harry's first 50)


def add_batch(ctx: Context, size: int, batch: str) -> dict[str, Any]:
    """Draw `size` companies not on the tab yet and, with --live, write them under the rows already there, which are
    kept as they are (a judged row is the answer key). Returns what was drawn and the rows now on the tab."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    rows = read_tab(ctx.clients.sheets, sheet_id, TAB) if sheet_id else None
    kept = [{c: str(r.get(c) or (FIRST_BATCH if c == "batch" else "")) for c in COLUMNS} for r in rows or []
            if str(r.get("domain") or "").strip()]
    cells = draw(ctx, size, batch=batch, exclude=frozenset(r["domain"] for r in kept))
    written = False
    if ctx.live and sheet_id and cells:
        write_tab(ctx.clients.sheets, sheet_id, TAB, COLUMNS, [*kept, *cells], rows)
        written = True
    log(JOB, run_id=ctx.run_id, batch=batch, drawn=len(cells), written=written, kept=len(kept))
    return {"batch": batch, "drawn": len(cells), "rows": cells, "tab_written": written,
            "verdicts_kept": sum(1 for r in kept if r["your_verdict"].strip()), "on_tab": [*kept, *cells]}


def run(ctx: Context, size: int = SIZE) -> dict[str, Any]:
    """`us-outbound labels sample`: add a batch of `size` now (this month's), with --live."""
    out = add_batch(ctx, size, batch_name(ctx))
    out.pop("on_tab")
    return out


def interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson 95% interval for k of n."""
    if not n:
        return 0.0, 0.0
    p = k / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, mid - half), min(1.0, mid + half)


def _named(text: Any) -> str:
    """A label as the tab writes it, without the confidence after it ("Edtech (medium)" is Edtech)."""
    return str(text or "").split(" (")[0].strip()


def _against(truth: Industry | str, said: Any, settings: Settings) -> str | None:
    """How a label stands against the right one: RIGHT, RIGHT_GROUP, WRONG_GROUP, or None when none was given."""
    ind = settings.label(_named(said))
    if ind is None:
        return None if not _named(said) or _named(said) in ("no label", "not asked") else WRONG_GROUP
    if truth == OUTSIDE:
        return WRONG_GROUP
    if ind.industry == truth.industry:
        return RIGHT
    return RIGHT_GROUP if ind.industry_group == truth.industry_group else WRONG_GROUP


def score(rows: Sequence[Mapping[str, str]], settings: Settings) -> dict[str, Any]:
    """Harry's verdicts counted: label right, group right, and emails whose copy says more than the verdict allows.

    A verdict is one of VERDICTS, or the right label itself (Harry, 11 Oct 2026, wrote labels in the column), in any
    case. Where the right label is known (written, "right", or right_label beside a verdict), the rules' and the
    model's labels are scored against it too: which of the three opinions the sheet should trust."""
    judged, unreadable = [], []
    for r in rows:
        said = " ".join(str(r.get("your_verdict") or "").split())
        word = said.lower()
        truth: Industry | str | None = None
        if word in VERDICTS:
            verdict = word
            if word == RIGHT:
                truth = settings.label(_named(r.get("our_label")))
            elif word == OUTSIDE:
                truth = OUTSIDE
            else:
                truth = settings.label(r.get("right_label"), fuzzy=True)
        elif said and (truth := settings.label(said, fuzzy=True)) is not None:
            verdict = _against(truth, r.get("our_label"), settings) or WRONG_GROUP
        elif said:
            unreadable.append(str(r.get("domain") or ""))
            continue
        else:
            continue
        judged.append((r, verdict, truth))
    n = len(judged)
    verdicts = Counter(v for _, v, _ in judged)
    wrong_pitch = [r for r, v, _ in judged if v in WRONG_PITCH.get(str(r.get("copy") or ""), set())]
    label_right, group_right = verdicts[RIGHT], verdicts[RIGHT] + verdicts[RIGHT_GROUP]
    opinions: dict[str, dict[str, int]] = {}
    for name, column in (("ours", "our_label"), ("rules", "rules_said"), ("model", "model_said")):
        c: Counter[str] = Counter()
        for r, _, truth in judged:
            if truth is None:
                continue
            got = _against(truth, r.get(column), settings)
            c["judged"] += 1
            c["gave_none"] += got is None
            c["label_right"] += got == RIGHT
            c["group_right"] += got in (RIGHT, RIGHT_GROUP)
        opinions[name] = {k: c[k] for k in ("judged", "gave_none", "label_right", "group_right")}
    return {
        "rows": len(rows), "judged": n, "unreadable": unreadable, "verdicts": dict(verdicts),
        "label_right": label_right, "group_right": group_right, "wrong_pitch": len(wrong_pitch),
        "intervals": {"label_right": interval(label_right, n), "group_right": interval(group_right, n),
                      "wrong_pitch": interval(len(wrong_pitch), n)},
        "opinions": opinions,
        "truths": [(str(r.get("domain") or ""), truth) for r, _, truth in judged if isinstance(truth, Industry)],
        "wrong": [{"domain": str(r.get("domain") or ""), "ours": str(r.get("our_label") or ""),
                   "copy": str(r.get("copy") or ""), "verdict": v,
                   "right_label": (truth.industry if isinstance(truth, Industry) else str(r.get("right_label") or ""))}
                  for r, v, truth in judged if v != RIGHT],
    }


def judged(rows: Sequence[Mapping[str, str]], settings: Settings) -> list[tuple[str, Industry]]:
    """The judged companies whose right label is known: (domain, label), for `labels eval --from-sample`."""
    return list(score(rows, settings)["truths"])


def sample_rows(ctx: Context) -> list[tuple[str, Industry]]:
    """The Label sample tab's judged companies with their right label (none when the tab is missing)."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    rows = read_tab(ctx.clients.sheets, sheet_id, TAB) if sheet_id else None
    return judged(rows or [], ctx.settings)


def run_score(ctx: Context) -> dict[str, Any]:
    """`us-outbound labels sample --score`: read the tab and count Harry's verdicts."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    rows = read_tab(ctx.clients.sheets, sheet_id, TAB) if sheet_id else None
    if rows is None:
        raise LookupError(f"the settings sheet has no {TAB} tab: run `us-outbound labels sample --live` first")
    out = score(rows, ctx.settings)
    log(JOB + "_score", run_id=ctx.run_id, judged=out["judged"], label_right=out["label_right"],
        group_right=out["group_right"], wrong_pitch=out["wrong_pitch"])
    return out


# -- the monthly batch (Harry, 11 Oct 2026) -----------------------------------------------------------------------


def previous_batch(batch: str) -> str:
    """The month before "2026-11": "2026-10"."""
    year, month = (int(x) for x in batch.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def _share(k: int, n: int) -> str:
    return f"{k / n:.0%}" if n else "-"


def summary_line(name: str, out: Mapping[str, Any]) -> str:
    """"October: 44 of 50 judged · label right 77% · group right 95% · untrue pitch 5%"."""
    n = out["judged"]
    if not n:
        return f"{name}: none judged yet"
    return (f"{name}: {n} of {out['rows']} judged · label right {_share(out['label_right'], n)} · group right "
            f"{_share(out['group_right'], n)} · untrue pitch {_share(out['wrong_pitch'], n)}")


def monthly(ctx: Context) -> dict[str, Any]:
    """`label_sample_monthly`: score last month's batch and every batch, add this month's, and post both to Slack with
    a link to the tab. In dry-run the batch is not written and the post goes to the dev channel."""
    from us_outbound.base import notify

    batch = batch_name(ctx)
    last = previous_batch(batch)
    added = add_batch(ctx, MONTHLY, batch)
    rows = added.pop("on_tab")
    last_rows = [r for r in rows if r["batch"] == last]
    scored_last = score(last_rows, ctx.settings)
    scored_all = score([r for r in rows if r["batch"] != batch], ctx.settings)
    link = say.sheet_link(ctx, TAB)
    head = f"{fmt.plural(added['drawn'], 'company', 'companies')} to check: is our industry label right?"
    facts = [f"Each month a person checks the industry we gave some of the companies we email. This month's "
             f"{added['drawn']} new companies to judge ({batch}) are on the {link}."]
    do = [f"On the {link}, fill `your_verdict` for each new row: the right label, or right, right group, wrong group "
          "or outside."]
    if last_rows:
        facts.append(f"• {summary_line(f'Last month ({last})', scored_last)}")
    if scored_all["judged"]:
        facts.append(f"• {summary_line('Every batch', scored_all)}")
        unjudged = sum(1 for r in last_rows if not str(r.get("your_verdict") or "").strip())
        if unjudged:
            facts.append(f"• {unjudged} of last month's companies are not judged yet.")
    if last_rows or scored_all["judged"]:
        facts.append("_Label right: the exact industry. Group right: at least its wider group. Untrue pitch: the "
                     "email said more about the company than the verdict allows._")
    judged_rows = [r for r in rows if r["batch"] != batch]
    answer_key = labels.gold_rows() + labels.judged_rows(ctx, judged(judged_rows, ctx.settings))
    suggested = definition_suggestions.suggest(ctx, judged_rows, answer_key, batch)
    if suggested["suggestions"]:
        better = "no worse" if suggested.get("better") else "worse, so check them before approving"
        changes = fmt.plural(len(suggested["suggestions"]), "definition change")
        suggestions = say.sheet_link(ctx, definition_suggestions.TAB)
        facts.append(f"• {changes} drafted for the labels the check confused ({', '.join(suggested['labels'])}):"
                     f" {suggestions}. Tried on the companies whose right label we know: now {suggested['now']}; "
                     f"with the changes {suggested['later']} ({better}).")
        do.append(f"On the {suggestions}, set `decision` to approve or reject. Approved ones go onto the Industries "
                  "tab at the next settings sync, and every company is checked again under them.")
    elif suggested["error"]:
        facts.append(f"• No definition changes drafted: {suggested['error']}.")
    lines = [say.compose(say.DECIDE, head, facts, do=do)]
    posted = notify.alert(ctx, "\n".join(lines))
    out = {**added, "last_batch": last, "last": scored_last, "all": scored_all, "link": link,
           "suggestions": suggested, "posted": posted.get("posted")}
    log(MONTHLY_JOB, run_id=ctx.run_id, batch=batch, drawn=added["drawn"], last_judged=scored_last["judged"],
        all_judged=scored_all["judged"], posted=posted.get("posted"))
    return out

