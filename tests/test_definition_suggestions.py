"""Definition suggestions (learn/definition_suggestions.py, settings/definitions.py; Harry, 11 Oct 2026): the labels the
label check confused get draft definitions, scored on the answer key, which Harry approves on the sheet."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.fakes import make_context
from tests.test_crosswalk import Sheet
from tests.test_labels import DEFAULT
from us_outbound import labels
from us_outbound.learn import definition_suggestions as ds
from us_outbound.settings import definitions

AT = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
TECH = "Technology & Startups"
ROWS = [  # judged rows from the Label sample tab
    {"company": "Bectran", "they_do": "B2B credit and collections software", "model_said": f"{TECH} (high)",
     "your_verdict": "Fintech"},
    {"company": "Revenue Analytics", "they_do": "pricing software", "model_said": f"{TECH} (high)",
     "your_verdict": "Fintech"},
    {"company": "VMock", "they_do": "career platform for students", "model_said": "Edtech (medium)",
     "your_verdict": TECH},
    {"company": "Binti", "they_do": "child welfare software", "model_said": f"{TECH} (high)", "your_verdict": TECH},
    {"company": "Iron Galaxy", "they_do": "games", "model_said": "Games studios (medium)", "your_verdict": "right",
     "our_label": "Games studios"},
    {"company": "Unjudged", "model_said": f"{TECH} (high)", "your_verdict": ""},
]


class Writer:
    """The writing model: answers with the drafts given, and records the prompt."""

    def __init__(self, suggestions):
        self.suggestions, self.calls = suggestions, []

    def json(self, system, prompt, schema, **kw):
        self.calls.append(prompt)
        return {"suggestions": self.suggestions}


def test_the_misses_and_the_labels_on_either_side_of_them():
    misses, chosen = ds.confusions(ROWS, DEFAULT)
    assert [(m.company, m.model, m.right) for m in misses] == [
        ("Bectran", TECH, "Fintech"), ("Revenue Analytics", TECH, "Fintech"), ("VMock", "Edtech", TECH)]
    assert [i.industry for i in chosen] == [TECH, "Fintech", "Edtech"]  # most often in a miss first
    prompt = ds.prompt(misses, chosen, DEFAULT)
    assert "Rewrite these labels' definitions: Technology & Startups; Fintech; Edtech" in prompt
    assert "- Bectran: B2B credit and collections software. Gave Technology & Startups; right is Fintech." in prompt
    assert f"- Games studios, {TECH}: Makes and publishes video games itself" in prompt  # the group around them


def test_drafts_are_checked_scored_and_written_for_approval(monkeypatch):
    ctx = make_context(DEFAULT, live=True, now=AT)
    ctx.clients.sheets = Sheet(ctx.guard, {})
    long = "x " * 200
    ctx.clients.claude = Writer([
        {"label": "Fintech", "definition": "Builds software for money: payments, lending, credit, collections or "
                                           "pricing; not a bank.", "why": "Bectran and Revenue Analytics"},
        {"label": "Hospitals", "definition": "A hospital.", "why": "not asked about"},  # dropped
        {"label": "Edtech", "definition": long, "why": "too long"},  # dropped
    ])
    scored = []

    def evaluate(c, rows):
        scored.append(c.settings.industry("Fintech").definition)
        better = len(scored) == 2
        return {labels.ACCEPTABLE: 9 if better else 7, labels.WRONG: 1 if better else 3, labels.UNSAFE: 0}

    monkeypatch.setattr(labels, "evaluate", evaluate)
    out = ds.suggest(ctx, ROWS, [{"domain": "x"}], "2026-11")
    assert scored[0] == DEFAULT.industry("Fintech").definition and scored[1].startswith("Builds software for money")
    assert (out["now"], out["later"], out["better"]) == ("7 of 10 right (70%), 0 unsafe", "9 of 10 right (90%), 0 unsafe",
                                                         True)
    [row] = ctx.clients.sheets.tabs[definitions.TAB]
    assert (row["id"], row["label"], row["decision"], row["applied"]) == ("2026-11:Fintech", "Fintech", "", "")
    assert row["current_definition"] == DEFAULT.industry("Fintech").definition
    assert row["answer_key_with"] == "9 of 10 right (90%), 0 unsafe"


def test_a_dry_run_asks_no_model_and_nothing_confused_asks_nothing():
    dry = make_context(DEFAULT, live=False, now=AT)
    dry.clients.claude = Writer([])
    assert ds.suggest(dry, ROWS, [], "2026-11")["error"] == "dry-run: no model asked" and dry.clients.claude.calls == []
    live = make_context(DEFAULT, live=True, now=AT)
    live.clients.claude = Writer([])
    assert ds.suggest(live, ROWS[3:], [], "2026-11")["misses"] == 0 and live.clients.claude.calls == []


def test_an_approved_suggestion_goes_onto_the_industries_tab_once():
    ctx = make_context(DEFAULT, live=True, now=AT)
    sheets = Sheet(ctx.guard, {
        "Industries": [{"industry": "Fintech", "definition": "old"}, {"industry": "Edtech", "definition": "old"}],
        definitions.TAB: [
            {"id": "2026-11:Fintech", "label": "Fintech", "suggested_definition": "new  fintech line",
             "decision": "Approve", "applied": ""},
            {"id": "2026-11:Edtech", "label": "Edtech", "suggested_definition": "new edtech", "decision": "reject"},
            {"id": "2026-10:Fintech", "label": "Fintech", "suggested_definition": "older", "decision": "approve",
             "applied": "2026-10-02"}]})
    sheets.update_cell = lambda sid, tab, match, col, value: _set(sheets, tab, match, col, value)
    ctx.clients.sheets = sheets
    sheet = ctx.guard.bounds.settings_sheet_id
    out = definitions.apply_approved(ctx, sheet)
    assert out == {"applied": ["2026-11:Fintech"], "missing": []}
    assert [r["definition"] for r in sheets.tabs["Industries"]] == ["new fintech line", "old"]
    assert sheets.tabs[definitions.TAB][0]["applied"] == "2026-11-01"
    assert definitions.apply_approved(ctx, sheet)["applied"] == []  # once
    del sheets.tabs[definitions.TAB]
    assert definitions.apply_approved(ctx, sheet) == {"applied": [], "missing": []}  # no tab yet


def _set(sheets, tab, match, col, value):
    for r in sheets.tabs.get(tab, []):
        if all(str(r.get(k, "")).lower() == v.lower() for k, v in match.items()):
            r[col] = value
            return True
    return False
