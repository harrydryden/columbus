"""Draft definition changes for the labels the label check confused, tested on the answer key, for Harry to approve
(Harry, 11 Oct 2026: "how can we ensure that the model classification can continuously improve over time").

Each month, after the label sample is scored (ops/label_sample.monthly):
  1. confusions(): the judged companies the model labelled wrong (its label against the right one Harry gave), and
     the labels most often on either side of a miss, at most MAX_LABELS.
  2. draft(): the writing model (General claude_model) rewrites those labels' definitions, one line each in the
     sheet's style ("what it is; not what it is not"), from the misses and the definitions around them.
  3. The answer key (the first cards' gold rows and every judged company, labels.judged_rows) is scored by the label
     check twice: with the definitions as they are, and with the drafts in their place.
  4. Each draft goes onto the Definition suggestions tab with both scores and a decision column. Nothing changes until
     Harry sets decision to approve; settings_sync then writes it to the Industries tab (settings/definitions.py).

It asks the models only with --live (the monthly job's scheduled run): one writing call and two answer-key runs, about
$0.01 a company each. The draft and both answer-key passes share one deadline, SECONDS (10 minutes from the
draft's start, inside the monthly job's 15-minute timeout; W2-3, code-learn-reporting B7): a pass that runs out stops
short, and a short pass is never scored (incomplete), so the budget, not the scheduler's timeout, ends a slow month. A changed definition makes every stored verdict stale, so the queue is asked again after an
approved change (labels_hash), which the Slack post says.
"""

from __future__ import annotations

import dataclasses
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound import labels
from us_outbound.clients.claude import BudgetExceeded, ClaudeError
from us_outbound.clients.sheets import read_tab, write_tab
from us_outbound.context import ConfigError, Context
from us_outbound.logs import log
from us_outbound.settings.definitions import COLUMNS, SUGGESTION_COLUMNS, SUGGESTIONS_TAB, TAB
from us_outbound.settings.model import Industry, Settings

PURPOSE = "definition_suggestions"
SECONDS = 600.0  # the draft and both answer-key passes together: 10 minutes, inside the monthly job's 15 (W2-3; B7)
_clock = time.monotonic  # the deadline's clock (a test sets it)
MAX_LABELS = 4
MAX_CHARS = 320
MAX_TOKENS, EFFORT, TIMEOUT = 2000, "medium", 120.0
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"suggestions": {"type": "array", "items": {
        "type": "object",
        "properties": {"label": {"type": "string"}, "definition": {"type": "string"}, "why": {"type": "string"}},
        "required": ["label", "definition", "why"], "additionalProperties": False}}},
    "required": ["suggestions"],
    "additionalProperties": False,
}
SYSTEM = """You maintain the one-line definitions a classifier uses to give US companies an industry label. Each
definition says what a company under the label is, then what it is not, in the form "What it is (examples); not X,
and not Y." It names a neighbouring label in brackets when that is where a near miss belongs. Keep each definition one
sentence of at most 300 characters, in US English, with no marketing words.

You are shown the definitions of the labels the classifier confused and of their neighbours, and the companies it got
wrong: what each does, the label it gave and the label a person said was right. Rewrite only the definitions you are
asked about, so that those companies would land on the right label, without moving companies that were right. Change
as little as the misses need. For each, say in one short sentence which misses the change is for."""


@dataclasses.dataclass(frozen=True)
class Miss:
    company: str
    they_do: str
    model: str  # the label the model gave
    right: str  # the label Harry gave


def _named(text: Any) -> str:
    return str(text or "").split(" (")[0].strip()


def confusions(rows: Sequence[Mapping[str, str]], settings: Settings) -> tuple[list[Miss], list[Industry]]:
    """The judged companies the model labelled wrong, and the labels most often on either side of a miss."""
    misses: list[Miss] = []
    counts: Counter[str] = Counter()
    for r in rows:
        said = " ".join(str(r.get("your_verdict") or "").split())
        word = said.lower()
        if not said or word == "outside":
            continue
        if word == "right":  # a verdict word, never a label ("right" is part of "Human rights")
            right = settings.label(_named(r.get("our_label")))
        elif word in ("right group", "wrong group"):
            right = settings.label(r.get("right_label"), fuzzy=True)
        else:
            right = settings.label(said, fuzzy=True)
        model = settings.label(_named(r.get("model_said")))
        if right is None or model is None or model.industry == right.industry:
            continue
        misses.append(Miss(str(r.get("company") or r.get("domain") or ""), str(r.get("they_do") or ""),
                           model.industry, right.industry))
        counts.update([model.industry, right.industry])
    chosen = [settings.industry(name) for name, _ in counts.most_common(MAX_LABELS)]
    return misses, [i for i in chosen if i is not None]


def prompt(misses: Sequence[Miss], chosen: Sequence[Industry], settings: Settings) -> str:
    groups = {i.industry_group for i in chosen}
    around = [i for i in settings.industries if i.industry_group in groups]
    lines = ["Definitions (label, group: definition):"]
    lines += [f"- {i.industry}, {i.industry_group}: {i.definition}" for i in around]
    lines += ["", "Rewrite these labels' definitions: " + "; ".join(i.industry for i in chosen), "",
              "The companies the classifier got wrong:"]
    lines += [f"- {m.company}: {m.they_do or 'no description'}. Gave {m.model}; right is {m.right}." for m in misses]
    return "\n".join(lines)


def draft(ctx: Context, misses: Sequence[Miss], chosen: Sequence[Industry]) -> list[dict[str, str]]:
    """The writing model's new definitions for the chosen labels, checked (one line, a chosen label, not too long)."""
    names = {i.industry: i for i in chosen}
    answer = ctx.clients.claude.json(SYSTEM, prompt(misses, chosen, ctx.settings), SCHEMA, max_tokens=MAX_TOKENS,
                                     purpose=PURPOSE, now=ctx.now, effort=EFFORT, timeout=TIMEOUT)
    out = []
    for s in answer.get("suggestions") or []:
        label = names.get(str(s.get("label") or "").strip())
        text = " ".join(str(s.get("definition") or "").split())
        if label is None or not text or len(text) > MAX_CHARS or text == label.definition:
            continue
        out.append({"label": label.industry, "current": label.definition, "suggested": text,
                    "why": " ".join(str(s.get("why") or "").split())[:300]})
    return out


def with_definitions(settings: Settings, drafts: Sequence[Mapping[str, str]]) -> Settings:
    new = {d["label"]: d["suggested"] for d in drafts}
    rows = tuple(dataclasses.replace(i, definition=new[i.industry]) if i.industry in new else i
                 for i in settings.industries)
    return dataclasses.replace(settings, industries=rows)


def _score(out: Mapping[str, Any]) -> str:
    done = sum(out.get(k, 0) for k in (labels.ACCEPTABLE, labels.WRONG, labels.UNSAFE))
    if not done:
        return "not scored"
    return (f"{out.get(labels.ACCEPTABLE, 0)} of {done} right ({out.get(labels.ACCEPTABLE, 0) / done:.0%}), "
            f"{out.get(labels.UNSAFE, 0)} unsafe")


def _checked(out: Mapping[str, Any]) -> int:
    return sum(int(out.get(k, 0) or 0) for k in (labels.ACCEPTABLE, labels.WRONG, labels.UNSAFE))


def incomplete(now: Mapping[str, Any], later: Mapping[str, Any], size: int) -> str | None:
    """"k of N" when either answer-key pass stopped short, else None. labels.evaluate stops at the first model error
    and returns what it had, with an error; the two passes must cover the same companies to be compared, or a
    baseline cut short after 5 rows ("4 of 5 right") makes a draft scored on all 40 look like an improvement
    (W0, 19 Oct 2026; code-learn-reporting B7). k is the shorter pass's count."""
    counts = (_checked(now), _checked(later))
    asked = (now.get("asked"), later.get("asked"))
    if now.get("error") or later.get("error") or min(counts) < size or asked[0] != asked[1]:
        return f"{min(counts)} of {size}"
    return None


def suggest(ctx: Context, sample_rows: Sequence[Mapping[str, str]], answer_key: Sequence[Mapping[str, Any]],
            month: str) -> dict[str, Any]:
    """Draft, score and write this month's suggestions (the models are asked only with --live)."""
    misses, chosen = confusions(sample_rows, ctx.settings)
    out: dict[str, Any] = {"misses": len(misses), "labels": [i.industry for i in chosen], "suggestions": [],
                           "written": False, "error": ""}
    if not misses:
        return out
    if not ctx.live:
        out["error"] = "dry-run: no model asked"
        return out
    deadline = _clock() + SECONDS

    def left() -> float:
        return max(0.0, deadline - _clock())

    try:
        drafts = draft(ctx, misses, chosen)
        if not drafts:
            return out
        now = labels.evaluate(ctx, answer_key, seconds=left())
        later = labels.evaluate(dataclasses.replace(ctx, settings=with_definitions(ctx.settings, drafts)), answer_key,
                                seconds=left())
    except (ClaudeError, BudgetExceeded, ConfigError) as exc:
        out["error"] = f"the model could not be asked ({str(exc)[:160]})"
        log(PURPOSE + "_failed", run_id=ctx.run_id, error=out["error"])
        return out
    if (short := incomplete(now, later, len(answer_key))) is not None:
        out["answer_key_incomplete"] = short
        out["error"] = f"the answer key was not checked in full ({short} companies), so nothing is scored"
        log(PURPOSE + "_incomplete", run_id=ctx.run_id, month=month, answer_key=short, now_error=now.get("error"),
            later_error=later.get("error"))
        return out
    rows = [{"id": f"{month}:{d['label']}", "month": month, "label": d["label"], "current_definition": d["current"],
             "suggested_definition": d["suggested"], "why": d["why"], "answer_key_now": _score(now),
             "answer_key_with": _score(later), "decision": "", "applied": ""} for d in drafts]
    out.update(suggestions=rows, now=_score(now), later=_score(later),
               better=later.get(labels.ACCEPTABLE, 0) >= now.get(labels.ACCEPTABLE, 0)
               and later.get(labels.UNSAFE, 0) <= now.get(labels.UNSAFE, 0))
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if sheet_id:
        kept = read_tab(ctx.clients.sheets, sheet_id, TAB)
        old = [{c: str(r.get(c) or "") for c in COLUMNS} for r in kept or [] if str(r.get("id") or "").strip()
               and str(r.get("id")) not in {x["id"] for x in rows}]
        write_tab(ctx.clients.sheets, sheet_id, TAB, COLUMNS, [*old, *rows], kept)
        out["written"] = True
    log(PURPOSE, run_id=ctx.run_id, month=month, misses=len(misses), drafted=len(rows), now=out["now"],
        later=out["later"])
    return out


# -- the Suggestions tab (W3-2, 20 Oct 2026): the ICP rules proposed from the hand-check -----------------------------


def write_suggestions(ctx: Context, rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """Add proposal rows (settings/definitions.SUGGESTION_COLUMNS; learn/precision.propose drafts the kinds
    definition_not, exclude_naics and entity_text) to the Suggestions tab. Live only. Append-only: a row whose id is
    on the tab already is left exactly as it is, Harry's status with it, so a rerun of the month never undoes a
    decision; the tab is written whole only when it has no rows (new, or only its header). A read refused for any
    reason but "no such tab" raises (clients/sheets.read_tab), so the tab is never replaced by this month's rows."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not ctx.live or not sheet_id or not rows:
        return {"added": 0, "written": False}
    kept = read_tab(ctx.clients.sheets, sheet_id, SUGGESTIONS_TAB)
    there = {str(r.get("id") or "").strip().casefold() for r in kept or []}
    added = [{c: str(r.get(c) or "") for c in SUGGESTION_COLUMNS} for r in rows
             if str(r.get("id") or "").strip().casefold() not in there]
    if not kept:
        write_tab(ctx.clients.sheets, sheet_id, SUGGESTIONS_TAB, SUGGESTION_COLUMNS, added, kept)
    elif added:
        ctx.clients.sheets.append_rows(sheet_id, SUGGESTIONS_TAB, added)
    log("suggestions_written", run_id=ctx.run_id, added=len(added), kept=len(kept or []))
    return {"added": len(added), "written": bool(added)}
