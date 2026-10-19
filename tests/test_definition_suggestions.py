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
    assert out == {"applied": ["2026-11:Fintech"], "missing": [], "errors": []}
    assert [r["definition"] for r in sheets.tabs["Industries"]] == ["new fintech line", "old"]
    assert sheets.tabs[definitions.TAB][0]["applied"] == "2026-11-01"
    assert definitions.apply_approved(ctx, sheet)["applied"] == []  # once
    del sheets.tabs[definitions.TAB]
    assert definitions.apply_approved(ctx, sheet) == {"applied": [], "missing": [], "errors": []}  # no tab yet


def _set(sheets, tab, match, col, value):
    for r in sheets.tabs.get(tab, []):
        if all(str(r.get(k, "")).lower() == v.lower() for k, v in match.items()):
            r[col] = value
            return True
    return False


# -- W0, 19 Oct 2026: a pass cut short scores nothing; a bad suggestion row never stops the sync ---------------------


def _passes(monkeypatch, *passes):
    """labels.evaluate answering each pass in turn with the tallies given (as the real one does: it stops at the first
    model error and returns what it had, with an error)."""
    calls = []

    def evaluate(c, rows):
        out = passes[len(calls)]
        calls.append(len(rows))
        return dict(out)

    monkeypatch.setattr(labels, "evaluate", evaluate)
    return calls


FINTECH_DRAFT = [{"label": "Fintech", "definition": "Builds software for money: payments, lending, credit, collections "
                                                    "or pricing; not a bank.", "why": "Bectran and Revenue Analytics"}]
KEY = [{"domain": f"k{i}.com"} for i in range(40)]  # an answer key of 40 companies


def _live():
    ctx = make_context(DEFAULT, live=True, now=AT)
    ctx.clients.sheets = Sheet(ctx.guard, {})
    ctx.clients.claude = Writer(FINTECH_DRAFT)
    return ctx


def test_a_baseline_pass_cut_short_scores_nothing_and_writes_no_row(monkeypatch):
    """code-learn-reporting B7. A 529 stops the baseline pass after 5 of the 40 answer-key companies (4 right, 1 wrong:
    "4 of 5 right (80%)"); the pass with the draft checks all 40 (36 right: "36 of 40 right (90%)"). Compared as they
    stood, 36 >= 4 and 0 <= 0 made `better` True and the post said "no worse" for an unproven definition. Now the
    shorter pass's 5 of the 40 is named and nothing is written."""
    ctx = _live()
    calls = _passes(monkeypatch,
                    {labels.ACCEPTABLE: 4, labels.WRONG: 1, labels.UNSAFE: 0, "asked": 5,
                     "error": "the model could not be asked (529 overloaded)"},
                    {labels.ACCEPTABLE: 36, labels.WRONG: 4, labels.UNSAFE: 0, "asked": 40})
    out = ds.suggest(ctx, ROWS, KEY, "2026-11")
    assert calls == [40, 40]  # both passes were asked over the whole key
    assert out["answer_key_incomplete"] == "5 of 40"
    assert out["suggestions"] == [] and out["written"] is False and "better" not in out
    assert "5 of 40" in out["error"]
    assert definitions.TAB not in ctx.clients.sheets.tabs  # no row for Harry to approve


def test_a_draft_pass_cut_short_or_asked_a_different_number_of_times_scores_nothing(monkeypatch):
    full = {labels.ACCEPTABLE: 30, labels.WRONG: 10, labels.UNSAFE: 0, "asked": 40}
    for later, named in (
        # the draft's pass stopped after 38 with no error recorded: 37 + 1 = 38 of the 40
        ({labels.ACCEPTABLE: 37, labels.WRONG: 1, labels.UNSAFE: 0, "asked": 38}, "38 of 40"),
        # every row scored (39 + 1 = 40), but the model was asked 39 times against the baseline's 40
        ({labels.ACCEPTABLE: 39, labels.WRONG: 1, labels.UNSAFE: 0, "asked": 39}, "40 of 40"),
    ):
        ctx = _live()
        _passes(monkeypatch, full, later)
        out = ds.suggest(ctx, ROWS, KEY, "2026-11")
        assert (out["answer_key_incomplete"], out["suggestions"]) == (named, [])
        assert definitions.TAB not in ctx.clients.sheets.tabs
    # Both passes whole and asked alike: scored and written (the probe that the guard is not always on).
    # 30/40 = 75%; 33/40 = 0.825, held as the float 0.82499..., so it prints as 82%.
    ctx = _live()
    _passes(monkeypatch, full, {labels.ACCEPTABLE: 33, labels.WRONG: 7, labels.UNSAFE: 0, "asked": 40})
    out = ds.suggest(ctx, ROWS, KEY, "2026-11")
    assert "answer_key_incomplete" not in out and out["written"] is True
    assert (out["now"], out["later"]) == ("30 of 40 right (75%), 0 unsafe", "33 of 40 right (82%), 0 unsafe")


class StrictSheet(Sheet):
    """The Sheets client's refusals: update_cell and update_rows raise ValueError when two rows match, as
    clients/sheets.py does, rather than guess."""

    def update_cell(self, sheet_id, tab, match, column, value):
        hits = [r for r in self.tabs.get(tab, [])
                if all(str(r.get(k, "")).strip().lower() == str(v).strip().lower() for k, v in match.items())]
        if len(hits) > 1:
            raise ValueError(f"{len(hits)} rows on the {tab} tab match {dict(match)}")
        if hits and self._ok("values.update", sheet_id, tab):
            hits[0][column] = value
        return bool(hits)

    def update_rows(self, sheet_id, tab, key, updates):
        for value in updates:
            n = sum(1 for r in self.tabs.get(tab, []) if str(r.get(key, "")).strip().lower() == value.lower())
            if n > 1:
                raise ValueError(f"{n} rows on the {tab} tab have {key} {value!r}")
        return super().update_rows(sheet_id, tab, key, updates)


def suggestion(row_id, label, text):
    return {"id": row_id, "label": label, "suggested_definition": text, "decision": "approve", "applied": ""}


def test_rows_that_cannot_be_applied_are_recorded_and_the_rest_applied():
    """code-platform-ops B8. Two approved rows share an id: before, both lines were written to the Industries tab and
    then marking them applied raised, so every sync stopped there. A row with no id was applied and never marked,
    so each sync wrote it again. Two Industries rows for one label made update_cell raise. Now each is one error
    entry and the good row still goes in: 5 approved rows -> 1 applied, 3 errors (1 for the 2 rows sharing an id,
    1 blank id, 1 doubled label)."""
    ctx = make_context(DEFAULT, live=True, now=AT)
    sheets = StrictSheet(ctx.guard, {
        "Industries": [{"industry": "Fintech", "definition": "old"}, {"industry": "Edtech", "definition": "old"},
                       {"industry": "Games studios", "definition": "old"},
                       {"industry": "Games studios", "definition": "old too"}],
        definitions.TAB: [suggestion("2026-11:Fintech", "Fintech", "one"),
                          suggestion("2026-11:Fintech", "Fintech", "two"),
                          suggestion("", "Edtech", "no id"),
                          suggestion("2026-11:Games", "Games studios", "games"),
                          suggestion("2026-12:Fintech", "Fintech", "good")]})
    ctx.clients.sheets = sheets
    out = definitions.apply_approved(ctx, ctx.guard.bounds.settings_sheet_id)
    assert out["applied"] == ["2026-12:Fintech"] and out["missing"] == []
    assert [(e["id"], e["label"]) for e in out["errors"]] == [
        ("2026-11:Fintech", "Fintech"), ("", "Edtech"), ("2026-11:Games", "Games studios")]
    assert out["errors"][0]["error"] == "skipped: 2 rows on the tab have this id; give each its own"
    assert out["errors"][2]["error"].startswith("ValueError: 2 rows on the Industries tab match")
    # Only the good row's line went in; the shared id's two lines and the blank id's line did not.
    assert [r["definition"] for r in sheets.tabs["Industries"]] == ["good", "old", "old", "old too"]
    assert [r["applied"] for r in sheets.tabs[definitions.TAB]] == ["", "", "", "", "2026-11-01"]
