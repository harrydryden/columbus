"""The label sample (ops/label_sample.py; Harry, 10 Oct 2026): a random draw of labelled companies for Harry to judge
by hand, and his verdicts counted."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fakes import make_context
from tests.test_crosswalk import Sheet
from tests.test_labels import DEFAULT
from us_outbound.ops import cli, label_sample as ls

AT = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)
TECH = "Technology & Startups"


def world(live=False, n=80):
    """n verified tech companies, half with a label check on file, plus companies the sample must leave out: new
    and disqualified ones, and a verified one with no label."""
    ctx = make_context(DEFAULT, live=live, now=AT)
    ctx.clients.sheets = Sheet(ctx.guard, {})
    rows = [{"account_id": f"a{i:03}", "domain": f"a{i:03}.com", "clean_name": f"Company {i}", "status": "verified",
             "industry": "Fintech" if i % 2 else TECH, "industry_group": TECH,
             "label_source": "rules+model" if i % 2 else "umbrella", "label_confidence": "high"} for i in range(n)]
    rows += [{"account_id": "new1", "domain": "new1.com", "status": "new", "industry": TECH},
             {"account_id": "dq1", "domain": "dq1.com", "status": "disqualified", "industry": TECH},
             {"account_id": "bare", "domain": "bare.com", "status": "verified", "industry": ""}]
    ctx.store.insert("accounts", rows)
    ctx.store.insert("signal_events", [{
        "event_id": f"v{i}", "account_id": f"a{i:03}", "source": "label_check", "fact": "label_verdict",
        "value": {"rules": TECH, "rules_group": TECH, "model": "Fintech", "confidence": "high", "entity": "company",
                  "what_they_do": "payments software", "asked": True, "decision": {"source": "rules+model"}},
        "observed_at": AT} for i in range(0, n, 2)])
    return ctx


def test_the_draw_is_random_repeatable_and_only_companies_we_email():
    ctx = world()
    first = ls.draw(ctx)
    assert len(first) == 50
    assert {r["domain"] for r in first} <= {f"a{i:03}.com" for i in range(80)}  # never new, disqualified or unlabelled
    assert [r["domain"] for r in ls.draw(ctx)] == [r["domain"] for r in first]  # the same accounts, the same sample
    assert len(ls.draw(ctx, 200)) == 80  # no more than there are


def test_each_row_says_what_we_would_send_and_how_it_was_decided():
    rows = {r["domain"]: r for r in ls.draw(world(n=4), 4)}
    checked, plain = rows["a000.com"], rows["a001.com"]
    assert checked["website"] == "https://a000.com"
    assert checked["they_do"] == "payments software"
    assert (checked["our_label"], checked["copy"], checked["decided_by"]) == (TECH, "group", "umbrella (high)")
    assert (checked["rules_said"], checked["model_said"]) == (TECH, "Fintech (high)")
    assert (plain["our_label"], plain["copy"], plain["model_said"]) == ("Fintech", "label", "not asked")
    assert checked["your_verdict"] == checked["right_label"] == ""


def test_live_writes_the_tab_and_never_overwrites_verdicts():
    dry = world()
    out = ls.run(dry)
    assert (out["drawn"], out["tab_written"], dry.clients.sheets.writes) == (50, False, [])

    live = world(live=True)
    assert ls.run(live)["tab_written"]
    assert live.clients.sheets.writes == [f"add {ls.TAB}", f"replace {ls.TAB}"]
    tab = live.clients.sheets.tabs[ls.TAB]
    assert len(tab) == 50 and list(tab[0]) == ls.COLUMNS

    tab[0]["your_verdict"] = "right"
    again = ls.run(live)
    assert (again["tab_written"], again["verdicts_kept"]) == (False, 1)
    assert live.clients.sheets.tabs[ls.TAB][0]["your_verdict"] == "right"


def test_the_score_counts_right_labels_groups_and_wrong_pitches():
    rows = [
        {"domain": "a.com", "copy": "label", "your_verdict": "right"},
        {"domain": "b.com", "copy": "label", "your_verdict": "Right  Group", "right_label": "Fintech"},  # wrong pitch
        {"domain": "c.com", "copy": "group", "your_verdict": "right group"},  # group copy: still true
        {"domain": "d.com", "copy": "group", "your_verdict": "wrong group"},  # wrong pitch
        {"domain": "e.com", "copy": "general", "your_verdict": "outside"},  # General copy says nothing false
        {"domain": "f.com", "copy": "label", "your_verdict": ""},  # not judged yet
        {"domain": "g.com", "copy": "label", "your_verdict": "maybe"},  # not a verdict
    ]
    out = ls.score(rows)
    assert (out["judged"], out["unreadable"]) == (5, ["g.com"])
    assert (out["label_right"], out["group_right"], out["wrong_pitch"]) == (1, 3, 2)
    assert [w["domain"] for w in out["wrong"]] == ["b.com", "c.com", "d.com", "e.com"]
    lo, hi = out["intervals"]["group_right"]
    assert lo < 3 / 5 < hi


@pytest.mark.parametrize("k, n, lo, hi", [(45, 50, 0.79, 0.96), (50, 50, 0.92, 1.0), (0, 0, 0.0, 0.0)])
def test_the_wilson_interval(k, n, lo, hi):
    got = ls.interval(k, n)
    assert got[0] == pytest.approx(lo, abs=0.01) and got[1] == pytest.approx(hi, abs=0.01)


def test_the_score_needs_the_tab_and_the_reports_print(capsys):
    ctx = world()
    with pytest.raises(LookupError):
        ls.run_score(ctx)
    live = world(live=True)
    ls.run(live)
    for r in live.clients.sheets.tabs[ls.TAB][:10]:
        r["your_verdict"] = "right"
    out = ls.run_score(live)
    assert (out["judged"], out["label_right"]) == (10, 10)
    capsys.readouterr()
    cli._score_report(out)
    text = capsys.readouterr().out
    assert "10 of 50 companies judged." in text
    assert "Label right: 10 of 10 (100%; 95% interval 72% to 100%)" in text
