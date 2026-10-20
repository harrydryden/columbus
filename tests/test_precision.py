"""ICP precision from the hand-check, and the ICP rules it proposes (learn/precision.py; W3-2, 20 Oct 2026; design-final
T1, T2, L3): the judgement facts pooled over four weeks with a Wilson interval, the reasons, the splits printed only
when every cell has 15 rows, the score quarters only from 20 leave-outs, and each reason with three rows drafted as a
Suggestions row scored on the judgements and on the answer key."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from tests.fakes import make_context
from tests.test_copy_desk import Sheet
from tests.test_labels import DEFAULT
from us_outbound import labels
from us_outbound.enrol import hand_check
from us_outbound.learn import precision
from us_outbound.settings import definitions

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)  # Saturday of W43
WEEKS = ("2026-W40", "2026-W41", "2026-W42", "2026-W43")
TECH, ADS = "Technology & Startups", "Advertising agencies"
GROUPS = {TECH: TECH, ADS: "Marketing & Creative Agencies", "Publishers": "Marketing & Creative Agencies"}


def judgement(i, week, decision, *, reason="", industry=TECH, check=hand_check.SAMPLE, score=50, gate="h1:r1",
              size="10-19", state="NY", **cells):
    aid = f"a{i:02}"
    value = {"week": week, "row_id": f"{week}:{aid}.com", "check": check, "decision": decision, "reason": reason,
             "right_label": "", "contact_ok": cells.get("contact_ok", ""), "opener_ok": cells.get("opener_ok", ""),
             "note": "", "gate_version": gate,
             "snapshot": {"tier": None, "score": score, "industry_group": GROUPS[industry], "industry": industry,
                          "size_band": size, "hq_state": state, "source": "apollo", "label_source": "model"}}
    return {"event_id": hand_check.event_id(week, aid), "account_id": aid, "source": hand_check.SOURCE,
            "fact": hand_check.FACT, "value": value, "observed_at": NOW}


def apollo(aid, *, naics=("513210",), description="Software for teams.", keywords=("saas",)):
    return [{"event_id": f"{aid}-{f}", "account_id": aid, "source": "apollo_org", "fact": f, "value": v,
             "observed_at": NOW} for f, v in (("naics", list(naics)), ("description", description),
                                              ("keywords", list(keywords)))]


def w43_pool(*, media_fine=0):
    """W40-W43, 12 random rows a week: 38 fine, 8 left out (3 media_publisher, 2 subsidiary, 2 it_services_outsourcing,
    1 other), 2 wrong industry; the three media rows were labelled Advertising agencies (2) and Publishers (1), the
    two IT-services rows Technology & Startups with NAICS 541512. media_fine: fine Advertising agencies rows whose
    description says "media company" (a rule on the media words would leave them out). Beside them, rows tally
    must not count: a doubtful-facts row, a row of W39, and a row with no decision."""
    plan = ([("fine", "", TECH)] * (38 - 4) + [("fine", "", ADS)] * 4
            + [("leave out", "media_publisher", ADS)] * 2 + [("leave out", "media_publisher", "Publishers")]
            + [("leave out", "subsidiary", TECH)] * 2 + [("leave out", "it_services_outsourcing", TECH)] * 2
            + [("leave out", "other", TECH), ("wrong industry", "", TECH), ("wrong industry", "", TECH)])
    assert len(plan) == 48
    rows, events = [], []
    for i, (decision, reason, industry) in enumerate(plan):
        rows.append(judgement(i, WEEKS[i // 12], decision, reason=reason, industry=industry, score=10 + i))
        aid = f"a{i:02}"
        if reason == "media_publisher":
            events += apollo(aid, naics=("541810",), description="An online magazine and publishing house.")
        elif reason == "subsidiary":
            events += apollo(aid, description="A subsidiary of a larger group.")
        elif reason == "it_services_outsourcing":
            events += apollo(aid, naics=("541512",), description="Managed services for small firms.")
        elif industry == ADS and decision == "fine" and media_fine > 0:
            media_fine -= 1
            events += apollo(aid, naics=("541810",), description="An advertising and media company.")
        else:
            events += apollo(aid)
    rows += [judgement(90, "2026-W43", "leave out", reason="media_publisher", industry=ADS, check=hand_check.DOUBT),
             judgement(91, "2026-W39", "leave out", reason="media_publisher", industry=ADS),
             judgement(92, "2026-W43", "")]
    return rows, events


def world(*, live=False, media_fine=0, settings=DEFAULT):
    ctx = make_context(settings, live=live, now=NOW)
    ctx.clients.sheets = Sheet(ctx.guard, {})
    rows, events = w43_pool(media_fine=media_fine)
    ctx.store.insert("signal_events", rows + events)
    return ctx


def test_the_weeks_end_with_this_one():
    assert precision.weeks_back("2026-W43") == WEEKS
    assert precision.weeks_back("2026-W01", 2) == ("2025-W52", "2026-W01")


def test_the_pool_of_four_weeks_with_its_wilson_interval_and_reasons():
    """38 fine of 48 judged: 38/48 = 0.7917. Wilson 95% (z = 1.95996): centre (0.7917 + 1.9208/48)/(1 + 3.8415/48)
    = 0.8314/1.0800 = 0.7698, half-width 1.95996 * sqrt(0.7917 * 0.2083/48 + 3.8415/9216)/1.0800 = 0.1127, so
    (0.657, 0.883). A Wald interval would be 0.7917 -/+ 0.1149 = (0.677, 0.907): it fails this test. The doubtful
    row, W39's row and the undecided row are not counted."""
    t = precision.tally(world())
    assert t.weeks == WEEKS
    assert t.fine == (38, 48) and (t.leave_out, t.wrong_industry) == (8, 2)
    assert t.interval == pytest.approx((0.657, 0.883), abs=0.0005)
    assert t.reasons == {"media_publisher": 3, "subsidiary": 2, "it_services_outsourcing": 2, "other": 1}
    assert t.by_gate == {"h1:r1": (38, 48)}
    assert t.by_score_quartile is None and t.weights_flag is None  # 8 leave-outs, under 20


def test_a_split_prints_only_when_every_cell_has_fifteen():
    """Group cells of 20, 20 and 8 rows: not printable (8 < 15). A two-way state split of 24 and 24: printable. One
    cell alone (every row's label source is model) says nothing: not printable. The readout prints the state split
    with its intervals (Wilson 24 of 24: (0.862, 1.0)) and names the others as pooled only."""
    ctx = make_context(DEFAULT, now=NOW)
    groups = [TECH] * 20 + ["Marketing & Creative Agencies"] * 20 + ["Legal Teams"] * 8
    rows = []
    for i, g in enumerate(groups):
        row = judgement(i, WEEKS[i % 4], "fine", state="NY" if i % 2 else "IL")
        row["value"]["snapshot"]["industry_group"] = g
        rows.append(row)
    ctx.store.insert("signal_events", rows)
    t = precision.tally(ctx)
    group = t.splits["group"]
    assert group.cells == {"Legal Teams": (8, 8), "Marketing & Creative Agencies": (20, 20), TECH: (20, 20)}
    assert group.printable is False
    state = t.splits["state"]
    assert state.cells == {"IL": (24, 24), "NY": (24, 24)} and state.printable is True
    assert t.splits["label source"].cells == {"model": (48, 48)} and t.splits["label source"].printable is False
    lines = precision.readout_lines(ctx, t)
    assert "  By state: IL 24 of 24 (100%, 95% 86%–100%); NY 24 of 24 (100%, 95% 86%–100%)." in lines
    [pooled] = [x for x in lines if x.startswith("  Pooled only (a cell under 15 rows): ")]
    assert "group" in pooled and "label source" in pooled and "state" not in pooled


def test_the_score_quarters_come_from_twenty_leave_outs():
    """40 rows, scores 0..39, the 20 lowest left out: Q1 (scores 0-9) 10 of 10 and Q2 10 of 10. The lowest quarter
    holds half the leave-outs, more than its share: no flag. Flip it (the 20 highest left out): Q1 holds 0, the
    weights are flagged. With 19 leave-outs there are no quarters."""
    def ctx_with(left_out):
        ctx = make_context(DEFAULT, now=NOW)
        ctx.store.insert("signal_events", [judgement(i, WEEKS[i % 4], "leave out" if left_out(i) else "fine",
                                                     reason="other", score=i) for i in range(40)])
        return ctx

    low = precision.tally(ctx_with(lambda i: i < 20))
    assert low.by_score_quartile == {"Q1": (10, 10), "Q2": (10, 10), "Q3": (0, 10), "Q4": (0, 10)}
    assert low.weights_flag is False
    high = precision.tally(ctx_with(lambda i: i >= 20))
    assert high.by_score_quartile["Q1"] == (0, 10) and high.weights_flag is True
    assert precision.tally(ctx_with(lambda i: i >= 21)).by_score_quartile is None


def test_t1_by_gate_version_and_the_readout_block():
    ctx = world()
    for e in ctx.store.select("signal_events", {"source": hand_check.SOURCE}):
        if e["value"]["week"] == "2026-W43":
            ctx.store.upsert("signal_events", [{**e, "value": {**e["value"], "gate_version": "h2:r2",
                                                                "contact_ok": "yes", "opener_ok": "no"}}])
    t = precision.tally(ctx)
    # W40-W42: rows 0-35, all fine (the fine rows are 0-37); W43: rows 36-47, 2 fine.
    assert t.by_gate == {"h1:r1": (36, 36), "h2:r2": (2, 12)}
    assert (t.contact_ok, t.opener_ok) == ((12, 12), (0, 12))
    lines = precision.readout_lines(ctx, t)
    assert lines[1].startswith("*ICP precision*")
    assert lines[2] == ("  Fine: 38 of 48 (79%, 95% 66%–88%); left out 8, wrong industry 2. The 0.90 target needs about "
                        "150 rows to show; 48 so far.")
    assert lines[3] == "  Left out because: media_publisher 3, it_services_outsourcing 2, subsidiary 2, other 1."
    assert lines[4].startswith("  By gate version: h1:r1 36 of 36") and "h2:r2 2 of 12" in lines[4]
    assert "  Score quarters: from 20 leave-outs (8 so far)." in lines
    assert not any(w in " ".join(lines) for w in ("below", "on track", "met"))


# -- the proposals --------------------------------------------------------------------------------------------------


def test_one_reason_with_three_rows_makes_one_proposal_and_at_two_three():
    """media_publisher has 3 rows (>= 3): 1 row. subsidiary and it_services_outsourcing have 2 each and other names no
    rule: none. At a threshold of 2: media_publisher, it_services_outsourcing and subsidiary, 3 rows. The media draft
    against the facts: its rows carried Advertising agencies and Publishers; the 3 media rows say "magazine" and
    "publishing" (caught 3 of 3); the 38 fine rows say "Software for teams." (excluded 0 of 38)."""
    ctx = world()
    out = precision.propose(ctx)
    [row] = out["rows"]
    assert (row["id"], row["kind"], row["target"], row["text"], row["status"]) == (
        "2026-10:definition_not:media_publisher", "definition_not", "Advertising agencies; Publishers",
        "not a media company or publication", "proposed")
    assert row["evidence"] == ("catches 3 of 3 leave-outs; excludes 0 of 38 fine rows; answer key: not run "
                               "(dry run)")
    snap = json.loads(row["snapshot"])
    assert (snap["n"], snap["k"], snap["weeks"]) == (48, 3, ["2026-W40", "2026-W43"])
    # 3 of 48 by Wilson: (0.0215, 0.1684).
    assert snap["interval"] == [0.021, 0.168]
    assert snap["labels_hash"] == labels.labels_hash(DEFAULT) and snap["icp_rules_hash"]
    assert out["written"] is False and definitions.SUGGESTIONS_TAB not in ctx.clients.sheets.tabs  # dry run

    two = precision.propose(world(), at=2)
    assert [(r["kind"], r["target"]) for r in two["rows"]] == [
        ("definition_not", "Advertising agencies; Publishers"), ("exclude_naics", TECH), ("entity_text", "entity()")]
    it, sub = two["rows"][1], two["rows"][2]
    assert it["text"] == "NAICS 5415; not IT services or outsourcing"
    assert it["evidence"].startswith("catches 2 of 2 leave-outs; excludes 0 of 38 fine rows")
    assert sub["evidence"] == ("catches 2 of 2 leave-outs; excludes 0 of 38 fine rows; answer key: not applicable (a "
                               "prompt change engineering makes)")


def test_a_draft_that_would_exclude_fine_rows_is_rejected_by_eval():
    """Two of the four fine Advertising agencies rows describe themselves as "an advertising and media company": the
    media rule would leave them out, so it is worse on the facts: excludes 2 of 38, status rejected by eval."""
    [row] = precision.propose(world(media_fine=2))["rows"]
    assert row["evidence"].startswith("catches 3 of 3 leave-outs; excludes 2 of 38 fine rows")
    assert row["status"] == precision.REJECTED_BY_EVAL


def _evaluate(monkeypatch, *passes):
    calls = []

    def evaluate(c, rows, **kw):
        calls.append(c.settings)
        return dict(passes[len(calls) - 1])

    monkeypatch.setattr(labels, "evaluate", evaluate)
    return calls


KEY = [{"domain": f"k{i}.com"} for i in range(63)]


def test_live_the_draft_is_scored_on_the_answer_key_and_written_once(monkeypatch):
    """Live: the answer key with the definitions as they are (50 of 63 right) and with "not a media company or
    publication" in Advertising agencies' and Publishers' definitions (51 of 63): no worse, proposed. Written to the
    Suggestions tab; a second run the same month adds nothing and keeps Harry's status."""
    calls = _evaluate(monkeypatch, {labels.ACCEPTABLE: 50, labels.WRONG: 13, labels.UNSAFE: 0, "asked": 63},
                      {labels.ACCEPTABLE: 51, labels.WRONG: 12, labels.UNSAFE: 0, "asked": 63})
    ctx = world(live=True)
    out = precision.propose(ctx, KEY)
    [row] = out["rows"]
    assert row["evidence"] == ("catches 3 of 3 leave-outs; excludes 0 of 38 fine rows; answer key 50 → 51 of 63 "
                               "right")
    assert row["status"] == precision.PROPOSED and out["written"] is True
    assert calls[0].industry(ADS).definition == DEFAULT.industry(ADS).definition
    assert calls[1].industry(ADS).definition.endswith("; not a media company or publication")
    assert calls[1].industry("Publishers").definition.endswith("; not a media company or publication")
    tab = ctx.clients.sheets.tabs[definitions.SUGGESTIONS_TAB]
    assert [r["id"] for r in tab] == ["2026-10:definition_not:media_publisher"]
    assert list(tab[0]) == definitions.SUGGESTION_COLUMNS
    tab[0]["status"] = "approved"
    _evaluate(monkeypatch, {labels.ACCEPTABLE: 50, labels.WRONG: 13, labels.UNSAFE: 0, "asked": 63},
              {labels.ACCEPTABLE: 51, labels.WRONG: 12, labels.UNSAFE: 0, "asked": 63})
    again = precision.propose(ctx, KEY)
    assert again["written"] is False and [r["status"] for r in ctx.clients.sheets.tabs[
        definitions.SUGGESTIONS_TAB]] == ["approved"]


def test_worse_on_the_answer_key_is_rejected_and_a_short_pass_writes_nothing(monkeypatch):
    _evaluate(monkeypatch, {labels.ACCEPTABLE: 50, labels.WRONG: 13, labels.UNSAFE: 0, "asked": 63},
              {labels.ACCEPTABLE: 50, labels.WRONG: 12, labels.UNSAFE: 1, "asked": 63})  # one unsafe more
    [row] = precision.propose(world(live=True), KEY)["rows"]
    assert row["status"] == precision.REJECTED_BY_EVAL and row["evidence"].endswith("answer key 50 → 50 of 63 right")
    _evaluate(monkeypatch, {labels.ACCEPTABLE: 50, labels.WRONG: 13, labels.UNSAFE: 0, "asked": 63},
              {labels.ACCEPTABLE: 20, labels.WRONG: 2, labels.UNSAFE: 0, "asked": 22, "error": "ran out"})
    ctx = world(live=True)
    out = precision.propose(ctx, KEY)
    assert out["rows"] == [] and out["skipped"] == ["media_publisher: the answer key was not checked in full (22 of 63)"]
    assert definitions.SUGGESTIONS_TAB not in ctx.clients.sheets.tabs


def test_the_rule_in_the_settings_and_the_exclusions_in_the_prompt():
    """with_rule writes the rule where the sync would: an exclude_naics rule adds 5415 to Technology & Startups'
    exclusions and its words to the definition. The label check's list then reads "not NAICS ..., 5415" for it, and
    its hash moves (a definition or exclusion change asks every company again)."""
    rule = precision.RULES["it_services_outsourcing"]
    s = precision.with_rule(DEFAULT, rule, [TECH])
    assert s.industry(TECH).exclude_naics == ("541512", "541513", "541519", "5415")
    assert s.industry(TECH).definition.endswith("; not IT services or outsourcing")
    assert s.industry("Fintech") == DEFAULT.industry("Fintech")
    only = dataclasses.replace(DEFAULT, industries=tuple(
        dataclasses.replace(i, exclude_naics=("5415",)) if i.industry == TECH else i for i in DEFAULT.industries))
    prompt = labels.system_prompt(only)
    [line] = [x for x in prompt.splitlines() if x.startswith(f"{TECH} — {TECH} — ")]
    assert "; not NAICS 5415 — " in line
    assert labels.labels_hash(only) != labels.labels_hash(DEFAULT)
    [fin] = [x for x in labels.system_prompt(DEFAULT).splitlines() if x.startswith("Fintech — ")]
    assert "; not NAICS 541512, 541513, 541519 — " in fin


# -- applying an approved row ----------------------------------------------------------------------------------------


def _set(sheets, tab, match, col, value):
    for r in sheets.tabs.get(tab, []):
        if all(str(r.get(k, "")).lower() == str(v).lower() for k, v in match.items()):
            r[col] = value
            return True
    return False


def suggestion(row_id, kind, target, text, status):
    return {"id": row_id, "kind": kind, "target": target, "text": text, "evidence": "", "snapshot": "",
            "status": status, "applied": ""}


def test_only_an_approved_row_is_applied_and_only_once():
    """An approved definition_not row adds its words to each target's definition; an approved exclude_naics row adds
    the code to the label's exclude_naics and its words to the definition. A proposed row, one rejected by eval and
    an approved entity_text row (a prompt change engineering makes) are never applied. A second sync applies
    nothing."""
    ctx = make_context(DEFAULT, live=True, now=NOW)
    sheets = Sheet(ctx.guard, {
        "Industries": [{"industry": ADS, "definition": "Agencies that make ads.", "exclude_naics": ""},
                       {"industry": "Publishers", "definition": "Publishes books; not a printer", "exclude_naics": ""},
                       {"industry": TECH, "definition": "Makes software.", "exclude_naics": "541512"}],
        definitions.SUGGESTIONS_TAB: [
            suggestion("2026-11:definition_not:media_publisher", "definition_not", f"{ADS}; Publishers",
                       "not a media company or publication", "Approved"),
            suggestion("2026-11:exclude_naics:it_services_outsourcing", "exclude_naics", TECH,
                       "NAICS 5415; not IT services or outsourcing", "approved"),
            suggestion("2026-11:definition_not:x", "definition_not", ADS, "not a bank", "proposed"),
            suggestion("2026-11:definition_not:y", "definition_not", ADS, "not a shop", "rejected by eval"),
            suggestion("2026-11:entity_text:subsidiary", "entity_text", "entity()", "a subsidiary is other",
                       "approved")]})
    sheets.update_cell = lambda sid, tab, match, col, value: _set(sheets, tab, match, col, value)
    ctx.clients.sheets = sheets
    sheet = ctx.guard.bounds.settings_sheet_id
    out = definitions.apply_approved(ctx, sheet)
    assert out == {"applied": ["2026-11:definition_not:media_publisher",
                               "2026-11:exclude_naics:it_services_outsourcing"], "missing": [], "errors": []}
    ind = {r["industry"]: r for r in sheets.tabs["Industries"]}
    assert ind[ADS]["definition"] == "Agencies that make ads; not a media company or publication"
    assert ind["Publishers"]["definition"] == "Publishes books; not a printer; not a media company or publication"
    assert (ind[TECH]["exclude_naics"], ind[TECH]["definition"]) == (
        "541512; 5415", "Makes software; not IT services or outsourcing")
    assert [r["applied"] for r in sheets.tabs[definitions.SUGGESTIONS_TAB]] == ["2026-10-24", "2026-10-24", "", "",
                                                                                ""]
    assert definitions.apply_approved(ctx, sheet)["applied"] == []  # once
    assert ind[ADS]["definition"] == "Agencies that make ads; not a media company or publication"


def test_an_exclude_naics_row_without_a_code_or_with_an_unknown_label_is_not_applied():
    ctx = make_context(DEFAULT, live=True, now=NOW)
    sheets = Sheet(ctx.guard, {
        "Industries": [{"industry": TECH, "definition": "Makes software.", "exclude_naics": ""}],
        definitions.SUGGESTIONS_TAB: [
            suggestion("a", "exclude_naics", TECH, "not IT services", "approved"),
            suggestion("b", "definition_not", "Nope", "not X", "approved")]})
    sheets.update_cell = lambda sid, tab, match, col, value: _set(sheets, tab, match, col, value)
    ctx.clients.sheets = sheets
    out = definitions.apply_approved(ctx, ctx.guard.bounds.settings_sheet_id)
    assert out["applied"] == [] and out["missing"] == ["Nope"]
    assert out["errors"] == [{"id": "a", "label": TECH,
                              "error": "skipped: no NAICS code in the text (write it as NAICS 5415; not X)"}]
    assert sheets.tabs["Industries"][0] == {"industry": TECH, "definition": "Makes software.", "exclude_naics": ""}


def test_naics_text():
    assert definitions.naics_text("NAICS 5415, 541519; not IT services") == (("5415", "541519"), "not IT services")
    assert definitions.naics_text("not IT services") == ((), "")


def test_the_monday_readout_shows_t1_by_gate_version_and_t3_with_the_label_copy_figure():
    """Acceptance (W3-2): the readout carries T1 pooled with its interval and by gate version (W43 drawn under a new
    gate), and the label sample's exact and group precision with the label-copy emails apart (October: 4 of 30)."""
    from us_outbound.learn import readout

    ctx = world()
    for e in ctx.store.select("signal_events", {"source": hand_check.SOURCE}):
        if e["value"]["week"] == "2026-W43":
            ctx.store.upsert("signal_events", [{**e, "value": {**e["value"], "gate_version": "h2:r2"}}])
    labels.store_policy(ctx, {"rules+model": (20, 21), "model": (6, 9)}, ["2026-10"],
                        {"judged": 50, "label_right": 34, "group_right": 44, "wrong_pitch": 5,
                         "label_copy": {"judged": 30, "label_right": 26, "group_right": 29, "wrong_pitch": 4}})
    lines, nums = readout.build(ctx)
    text = "\n".join(lines)
    assert "  Fine: 38 of 48 (79%, 95% 66%–88%); left out 8, wrong industry 2." in text
    assert "  By gate version: h1:r1 36 of 36" in text and "h2:r2 2 of 12" in text
    assert "Emails sent a label's own copy: 4 of 30 pitched the wrong label" in text
    assert "label exactly right 34 of 50" in text and "group right 44 of 50" in text
    assert "  The hand-check found the wrong industry on 2 of 48" in text
    assert nums["icp_precision"]["fine"] == 38 and nums["icp_precision"]["judged"] == 48
    assert nums["icp_precision"]["interval"] == [0.6574, 0.8827]
