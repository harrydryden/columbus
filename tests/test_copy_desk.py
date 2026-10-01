"""The copy desk (enrol/copy_desk.py) and `settings load` (settings/load.py): the sheet check, the
preview, QA by the task model, drafting by the writing model, and loading the build's tabs into
the sheet without losing Harry's edits (Harry, 30 Sep 2026)."""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from tests.fakes import make_context
from tests.test_client_claude import FakeSDK
from tests.test_render import AGENCIES, BODIES, COPY, DEMO, copy_row, make_settings
from us_outbound.clients.guard import Op
from us_outbound.enrol import copy_desk
from us_outbound.settings import load as loader
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.validate import validate_all

PASS = json.dumps({"verdict": "pass", "problems": [], "summary": "Specific, calm and accurate."})


class MemorySheet:
    """The settings sheet in memory; writes go through the guard's dry-run rule."""

    def __init__(self, guard, tabs):
        self.guard, self.tabs = guard, tabs
        self.writes: list[str] = []

    def _ok(self, action, sheet_id, tab):
        return self.guard.authorize("sheets", Op(action, target=sheet_id, write=True, detail={"tab": tab}))

    def read_tabs(self, sheet_id, tabs):
        return {t: [dict(r) for r in self.tabs.get(t, [])] for t in tabs}

    def replace_tab(self, sheet_id, tab, headers, rows):
        if self._ok("values.update", sheet_id, tab):
            self.tabs[tab] = [{h: str(r.get(h, "")) for h in headers} for r in rows]
            self.writes.append(f"replace {tab}")

    def update_rows(self, sheet_id, tab, key, updates):
        if self._ok("values.batchUpdate", sheet_id, tab):
            for r in self.tabs.get(tab, []):
                r.update(updates.get(r.get(key), {}))
            self.writes.append(f"update {tab}")
        return [k for k in updates if not any(r.get(key) == k for r in self.tabs.get(tab, []))]

    def append_rows(self, sheet_id, tab, rows):
        if self._ok("values.append", sheet_id, tab):
            self.tabs.setdefault(tab, []).extend(dict(r) for r in rows)
            self.writes.append(f"append {tab}")


def ctx_with(settings=None, *, text=PASS, live=False, tabs=None):
    sdk = FakeSDK(text=text)
    ctx = make_context(settings or make_settings(), live=live, claude_sdk=sdk)
    ctx.clients.sheets = MemorySheet(ctx.guard, tabs if tabs is not None else {})
    return ctx, sdk


# -- the sheet check --------------------------------------------------------------------------


def test_a_clean_row_passes_the_sheet_check():
    assert copy_desk.check_row(COPY[0], make_settings()).ok


def test_the_sheet_check_names_each_problem_once():
    bodies = dict(BODIES)
    bodies[3] = BODIES[3].replace("One question", "Our therapists get one question")
    lines = {"People leader": "x " * 150, "Founder or executive": "Keeping people.", "Operations": "Cover."}
    row = copy_row("agencies-v1", AGENCIES, bodies=bodies, role_lines=lines,
                   subjects={1: "A very long subject line that goes on and on for {{company}} and more", 2: "b",
                             3: "c", 4: "d"})
    problems = copy_desk.check_row(row, make_settings()).problems
    assert sum('"therapists"' in p for p in problems) == 1  # not once per role and sender
    assert any("email 1: subject is" in p and "typical company name" in p for p in problems)
    assert any("the People leader line is" in p for p in problems)


def test_preview_renders_the_four_emails():
    s = make_settings()
    p = copy_desk.preview(COPY[0], s, role="Operations", sender="Hannah Spalding", opener=copy_desk.SAMPLE_OPENER)
    text = p.text()
    assert text.startswith("agencies-v1, Operations, sent by Hannah Spalding")
    assert "Subject: Support for the Harbor & Finch team" in text and copy_desk.SAMPLE_OPENER in text
    assert "Best wishes,\nHannah" in text and "Problems:" not in text
    html = p.html()
    assert html.startswith("<!doctype html>") and html.count('<section class="email">') == 4
    assert f'<a href="{DEMO}">' in html and "Harbor &amp; Finch" in html


# -- QA by the task model ----------------------------------------------------------------------


def test_qa_asks_the_task_model_and_stamps_the_wording():
    ctx, sdk = ctx_with()
    result = copy_desk.qa_row(ctx, COPY[0])
    assert result.verdict == "pass" and result.cell == f"pass {COPY[0].content_hash()}"
    [call] = sdk.calls
    assert call["model"] == ctx.settings.general.claude_task_model == "claude-sonnet-5-5"
    assert call["output_config"]["format"]["schema"] == copy_desk.QA_SCHEMA
    prompt = call["messages"][0]["content"]
    assert "## Facts" in prompt and "## Style" in prompt and "What is Spill?" in prompt
    assert result.notes.startswith("QA by claude-sonnet-5-5: Specific, calm and accurate.")
    [entry] = ctx.store.select("credit_ledger")
    assert entry["system"] == "claude" and entry["job"] == "copy_qa"


def test_a_blocker_fails_qa_whatever_the_verdict_says():
    answer = json.dumps({"verdict": "pass", "summary": "One claim is not on the page.", "problems": [
        {"email": 3, "severity": "blocker", "issue": "claims a named customer", "fix": "remove it"}]})
    ctx, _ = ctx_with(text=answer)
    result = copy_desk.qa_row(ctx, COPY[0])
    assert result.verdict == "fail" and "blocker: email 3: claims a named customer Fix: remove it" in result.notes


def test_a_row_that_fails_the_sheet_check_fails_qa_without_a_model_call():
    bodies = dict(BODIES)
    bodies[4] = BODIES[4].replace("[book a demo]({{demo_url}})", "book a demo")
    ctx, sdk = ctx_with()
    result = copy_desk.qa_row(ctx, copy_row("agencies-v1", AGENCIES, bodies=bodies))
    assert result.verdict == "fail" and "0 links to the demo page" in result.notes and sdk.calls == []


# -- drafting by the writing model ------------------------------------------------------------


def test_a_draft_comes_from_the_writing_model_as_a_new_draft_row():
    fields = {f: "" for f in copy_desk.DRAFT_FIELDS}
    ctx, sdk = ctx_with(text=json.dumps({**fields, "s1_subject": "Hello", "sources": "hero intro"}))
    row = copy_desk.draft_row(ctx, AGENCIES)
    [call] = sdk.calls
    assert call["model"] == "claude-opus-5-5" and call["max_tokens"] == copy_desk.DRAFT_MAX_TOKENS
    assert "## Style guide" in call["messages"][0]["content"] and "Marketing & Creative Agencies" in call["messages"][0]["content"]
    assert row["copy_version"] == "marketing-creative-agencies-v1" and row["status"] == "draft" and row["qa"] == ""
    assert row["s1_subject"] == "Hello" and set(row) <= set(COLUMNS["Copy"])


def test_next_version_counts_up():
    s = make_settings(copy=(copy_row("fintech-v1", "Fintech"),))
    assert copy_desk.next_version(s, "Fintech") == "fintech-v2"
    assert copy_desk.next_version(s, "Fintech", "Operations") == "fintech-operations-v1"


# -- the CLI ------------------------------------------------------------------------------------


def test_copy_qa_dry_run_calls_no_model(capsys):
    from us_outbound.ops import cli

    ctx, sdk = ctx_with(make_settings(copy=(copy_row("agencies-v1", AGENCIES, qa=False),)))
    assert cli.main(["copy", "qa", "--synced"], context_factory=lambda *a, **k: ctx) == 0
    out = capsys.readouterr().out
    assert "sheet check clean; would go to QA" in out and "no model called" in out and sdk.calls == []


def test_copy_qa_live_writes_the_verdicts_to_the_sheet(capsys):
    from us_outbound.ops import cli

    s = make_settings(copy=(copy_row("agencies-v1", AGENCIES, qa=False),))
    tabs = {"Copy": [{"copy_version": "agencies-v1", "qa": "", "qa_notes": ""}]}
    ctx, sdk = ctx_with(s, live=True, tabs=tabs)
    ctx.guard.configure(live=True)
    assert cli.main(["copy", "qa", "--synced", "--live"], context_factory=lambda *a, **k: ctx) == 0
    assert len(sdk.calls) == 1 and tabs["Copy"][0]["qa"] == f"pass {s.copy[0].content_hash()}"
    assert "1 of 1 passed." in capsys.readouterr().out


def test_copy_check_exits_1_when_a_row_has_problems(capsys):
    from us_outbound.ops import cli

    bodies = dict(BODIES)
    bodies[2] = BODIES[2].replace("well-being", "wellbeing")
    s = make_settings(copy=(COPY[0], copy_row("general-v1", "General", bodies=bodies)))
    ctx, _ = ctx_with(s)
    assert cli.main(["copy", "check", "--synced"], context_factory=lambda *a, **k: ctx) == 1
    out = capsys.readouterr().out
    assert "general-v1:" in out and 'British spelling "wellbeing"' in out and "2 rows checked, 1 with problems" in out


# -- settings load ------------------------------------------------------------------------------


def old_sheet() -> dict:
    """A sheet as first made: 3 of the old industries, with Harry's edits, and copy one row per step."""
    tabs = default_tabs()
    ind = {r["industry"]: dict(r) for r in tabs["Industries"]}
    old = [ind["Technology & Startups"], ind["Fintech"], ind["Legal Teams"]]
    for r in old:
        for c in [c for c in r if c.startswith("page_")]:
            del r[c]  # the old tab had no page columns
    old[1].update(active="no", priority="7", proof_point="UK example: Monzo")
    old.append({**old[0], "industry": "Space tourism", "industry_group": "Technology & Startups"})
    tabs["Industries"] = old
    tabs["Copy"] = [{"copy_version": "general-v1", "angle": "General", "step": str(n), "subject": "s", "body": "b",
                     "status": "draft", "approved_by": "", "sources": ""} for n in (1, 2, 3, 4)]
    return tabs


def test_industries_load_keeps_harry_s_values_and_rows():
    sheet = old_sheet()
    plan = loader.plan_tab("Industries", sheet["Industries"], default_tabs()["Industries"])
    by = {r["industry"]: r for r in plan.rows}
    assert len(plan.rows) == 109 and len(plan.added) == 105  # 108 pages (3 already there), plus Harry's own row
    assert (by["Fintech"]["active"], by["Fintech"]["priority"], by["Fintech"]["proof_point"]) == ("no", "7", "UK example: Monzo")
    assert by["Fintech"]["page_intro"]  # the build's new columns arrive
    assert plan.extra == ["Space tourism"] and plan.rows[-1]["industry"] == "Space tourism"
    assert "Fintech (active, priority, proof_point)" in plan.kept_from_sheet
    assert "page_blurb" in plan.new_columns


def test_copy_load_replaces_the_old_layout_and_then_keeps_the_sheet_s_rows():
    sheet = old_sheet()
    build = default_tabs()["Copy"]
    plan = loader.plan_tab("Copy", sheet["Copy"], build)
    assert plan.replaced_layout and len(plan.rows) == len(build)
    edited = copy.deepcopy(build[:2])
    edited[0]["s1_subject"] = "Harry's own subject"
    edited[0]["status"], edited[0]["approved_by"] = "approved", "Harry"
    plan = loader.plan_tab("Copy", edited, build)
    assert not plan.replaced_layout and plan.rows[0]["s1_subject"] == "Harry's own subject"
    assert plan.rows[0]["status"] == "approved" and len(plan.rows) == len(build) and len(plan.added) == len(build) - 2


def test_load_is_dry_until_live_and_the_result_validates():
    sheet = old_sheet()
    ctx, _ = ctx_with(tabs=sheet)
    out = loader.load(ctx, ["Industries", "Copy"])
    assert out["dry_run"] and ctx.clients.sheets.writes == [] and len(sheet["Industries"]) == 4
    ctx, _ = ctx_with(live=True, tabs=sheet)
    ctx.guard.configure(live=True)
    out = loader.load(ctx, ["Industries", "Copy"])
    assert ctx.clients.sheets.writes == ["replace Industries", "replace Copy"]
    settings, errors = validate_all(sheet)
    assert not any(errors.values()) and len(settings.industries) == 109 and len(settings.copy) == 106
    assert settings.industry("Fintech").active is False


def test_load_refuses_other_tabs_and_a_merge_that_would_not_validate():
    ctx, _ = ctx_with(tabs=old_sheet())
    with pytest.raises(ValueError, match="only General, Industries, Copy"):
        loader.load(ctx, ["Signals"])
    bad = old_sheet()
    bad["Industries"][1]["priority"] = "zero"
    ctx, _ = ctx_with(tabs=bad)
    with pytest.raises(ValueError, match="would not validate"):
        loader.load(ctx, ["Industries"])


def test_the_build_s_tabs_are_complete_and_valid():
    tabs = default_tabs()
    settings, errors = validate_all(tabs)
    assert not any(errors.values())
    assert [list(r) for r in tabs["Copy"][:1]] == [COLUMNS["Copy"]]
    assert dataclasses.asdict(settings.general)["email_format"] == "html"


def live_general() -> list[dict]:
    """The General tab as Harry's sheet has it on 1 Oct: the old daily cap, no new keys, Clay off."""
    rows = [dict(r) for r in default_tabs()["General"]
            if r["key"] not in {"claude_task_model", "email_format", "site_url", "price_from"}]
    for r in rows:
        if r["key"] == "weekly_enrol_cap":
            r.update(key="daily_enrol_cap", value="30")
        if r["key"] == "clay_monthly_credits":
            r["value"] = "0"
    # The two keys retired on 1 Oct (no postal address, no privacy link) are still on the sheet.
    rows += [{"key": "postal_address", "value": "", "note": ""}, {"key": "privacy_url", "value": "", "note": ""}]
    return rows


def test_general_load_renames_adds_and_sets():
    sets = {"clay_monthly_credits": "2000", "claude_model": "claude-opus-5-5"}
    plan = loader.plan_tab("General", live_general(), default_tabs()["General"], sets)
    by = {r["key"]: r["value"] for r in plan.rows if r["key"]}
    assert "daily_enrol_cap" not in by and by["weekly_enrol_cap"] == "150"  # renamed, with the weekly value
    assert by["price_from"] == "195" and by["site_url"] == "https://www.spill.chat/us"
    assert by["claude_task_model"] == "claude-sonnet-5-5" and by["email_format"] == "html"
    assert by["clay_monthly_credits"] == "2000"
    assert "daily_enrol_cap renamed weekly_enrol_cap = 150" in plan.updated
    assert "clay_monthly_credits: 0 -> 2000" in plan.updated and len(plan.added) == 4
    assert "postal_address" not in by and "privacy_url" not in by  # retired keys are removed
    assert "postal_address removed (no longer used)" in plan.updated
    with pytest.raises(ValueError, match="not General keys"):
        loader.plan_tab("General", live_general(), default_tabs()["General"], {"nonsense": "1"})


def test_load_all_three_tabs_validates_and_writes_live():
    sheet = old_sheet()
    sheet["General"] = live_general()
    ctx, _ = ctx_with(live=True, tabs=sheet)
    ctx.guard.configure(live=True)
    loader.load(ctx, ["General", "Industries", "Copy"], {"clay_monthly_credits": "2000"})
    assert ctx.clients.sheets.writes == ["replace General", "replace Industries", "replace Copy"]
    settings, errors = validate_all(sheet)
    assert not any(errors.values()) and settings.general.weekly_enrol_cap == 150
    assert settings.general.clay_monthly_credits == 2000 and settings.general.price_from == 195


def test_copy_load_replace_drafts_keeps_approved_rows_and_replaces_the_rest():
    build = default_tabs()["Copy"]
    approved = {**build[0], "copy_version": "harrys-own-v1", "status": "approved", "approved_by": "Harry"}
    draft = {**build[0], "copy_version": "old-draft-v1", "status": "draft"}
    plan = loader.plan_tab("Copy", [approved, draft], build, replace_drafts=True)
    versions = [r["copy_version"] for r in plan.rows]
    assert versions[0] == "harrys-own-v1" and "old-draft-v1" not in versions
    assert len(plan.rows) == len(build) + 1 and "old-draft-v1 removed (not approved)" in plan.updated
    kept = loader.plan_tab("Copy", [approved, draft], build)  # without the flag nothing on the sheet goes
    assert {"harrys-own-v1", "old-draft-v1"} <= {r["copy_version"] for r in kept.rows}
