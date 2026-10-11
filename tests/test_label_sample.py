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


def test_a_batch_is_added_under_the_rows_already_there_and_never_redraws_them():
    dry = world()
    out = ls.run(dry)
    assert (out["drawn"], out["tab_written"], dry.clients.sheets.writes) == (50, False, [])

    live = world(live=True)
    first = ls.run(live)
    assert first["tab_written"] and first["batch"] == "2026-10"
    assert live.clients.sheets.writes == [f"add {ls.TAB}", f"replace {ls.TAB}"]
    tab = live.clients.sheets.tabs[ls.TAB]
    assert len(tab) == 50 and list(tab[0]) == ls.COLUMNS and {r["batch"] for r in tab} == {"2026-10"}

    tab[0]["your_verdict"] = "right"
    judged = tab[0]["domain"]
    again = ls.run(live, 10)
    assert (again["tab_written"], again["drawn"], again["verdicts_kept"]) == (True, 10, 1)
    tab = live.clients.sheets.tabs[ls.TAB]
    assert len(tab) == 60 and tab[0]["domain"] == judged and tab[0]["your_verdict"] == "right"
    assert len({r["domain"] for r in tab}) == 60  # none drawn twice


def test_rows_from_before_the_batch_column_are_the_first_batch():
    live = world(live=True)
    live.clients.sheets.tabs[ls.TAB] = [{"domain": "a001.com", "your_verdict": "Fintech"}]
    out = ls.add_batch(live, 2, "2026-11")
    assert [(r["batch"], r["domain"], r["your_verdict"]) for r in out["on_tab"]][0] == ("2026-10", "a001.com", "Fintech")
    assert [r["batch"] for r in out["on_tab"][1:]] == ["2026-11", "2026-11"]


def test_the_monthly_job_scores_last_month_adds_this_months_and_posts_a_link_to_the_tab(monkeypatch):
    from us_outbound.base import notify

    posts: list[str] = []
    monkeypatch.setattr(notify, "alert", lambda ctx, text, **k: posts.append(text) or {"posted": True})
    live = world(live=True)
    ls.run(live)  # October's 50
    for r in live.clients.sheets.tabs[ls.TAB][:40]:
        r["your_verdict"] = r["our_label"]  # all right
    live.clients.sheets.gids = {ls.TAB: 1234}
    live.now = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    out = ls.monthly(live)
    assert (out["batch"], out["drawn"], out["last_batch"]) == ("2026-11", 25, "2026-10")
    assert (out["last"]["judged"], out["last"]["label_right"]) == (40, 40)
    assert len(live.clients.sheets.tabs[ls.TAB]) == 75
    [text] = posts
    sheet = live.guard.bounds.settings_sheet_id
    assert f"<https://docs.google.com/spreadsheets/d/{sheet}/edit#gid=1234|Label sample tab>" in text
    assert "25 new companies to judge (2026-11)" in text
    assert "Last month (2026-10): 40 of 50 judged · label right 100% · group right 100% · untrue pitch 0%" in text
    assert "10 of last month's companies are not judged yet." in text


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
    out = ls.score(rows, DEFAULT)
    assert (out["judged"], out["unreadable"]) == (5, ["g.com"])
    assert (out["label_right"], out["group_right"], out["wrong_pitch"]) == (1, 3, 2)
    assert [w["domain"] for w in out["wrong"]] == ["b.com", "c.com", "d.com", "e.com"]
    lo, hi = out["intervals"]["group_right"]
    assert lo < 3 / 5 < hi


def test_the_right_label_written_in_the_verdict_scores_each_opinion():
    """Harry, 11 Oct 2026: he wrote the right label itself, so the rules, the model and our label are each scored."""
    rows = [  # as he filled the tab
        {"domain": "ablyworks.com", "our_label": TECH, "copy": "group", "rules_said": "", "model_said": "not asked",
         "your_verdict": TECH},
        {"domain": "assimasolutions.com", "our_label": "Education", "copy": "general", "rules_said": "Education",
         "model_said": "Edtech (medium)", "your_verdict": "EdTech"},
        {"domain": "bectran.com", "our_label": TECH, "copy": "label", "rules_said": "no label",
         "model_said": f"{TECH} (high)", "your_verdict": "Fintech"},
        {"domain": "bittersoutherner.com", "our_label": "Publishers", "copy": "label",
         "rules_said": "Marketing & Creative Agencies", "model_said": "Publishers (high)", "your_verdict": "Publishers"},
        {"domain": "x.com", "our_label": "Fintech", "copy": "label", "rules_said": "Fintech",
         "model_said": "Fintech (high)", "your_verdict": "right"},
    ]
    out = ls.score(rows, DEFAULT)
    assert out["judged"] == 5 and out["unreadable"] == []
    # Ours: ablyworks, Bitter Southerner and x right; Bectran in the right group (Fintech is a tech label); Assima
    # Education against Edtech, another group, but with General copy, which says nothing untrue.
    assert (out["label_right"], out["group_right"], out["wrong_pitch"]) == (3, 4, 1)
    assert out["opinions"]["ours"] == {"judged": 5, "gave_none": 0, "label_right": 3, "group_right": 4}
    assert out["opinions"]["model"] == {"judged": 5, "gave_none": 1, "label_right": 3, "group_right": 4}
    assert out["opinions"]["rules"] == {"judged": 5, "gave_none": 2, "label_right": 1, "group_right": 2}
    assert {w["domain"]: w["right_label"] for w in out["wrong"]} == {"assimasolutions.com": "Edtech",
                                                                      "bectran.com": "Fintech"}


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


def test_the_judged_companies_join_the_eval_with_their_facts_and_home_page():
    """Harry, 11 Oct 2026: every company judged by hand is scored by `labels eval --from-sample`, so a change to the
    prompt or the definitions is tried on them first."""
    from us_outbound import labels

    ctx = world(live=True, n=4)
    ls.run(ctx, 4)
    tab = ctx.clients.sheets.tabs[ls.TAB]
    tab[0]["your_verdict"] = "Fintech"  # a000: we said Technology & Startups
    tab[1]["your_verdict"] = "right"  # a001: we said Fintech
    tab[2]["your_verdict"] = "outside"  # no label to expect: left out of the eval
    ctx.store.insert("signal_events", [
        {"event_id": "a000-d", "account_id": "a000", "source": "apollo_org", "fact": "description",
         "value": "Payments software.", "observed_at": AT},
        {"event_id": "a000-h", "account_id": "a000", "source": "careers_pages", "fact": "home_page",
         "value": {"title": "Company 0", "text": "Card payments for shops."}, "observed_at": AT}])
    judged = ls.sample_rows(ctx)
    assert [(d, i.industry) for d, i in judged] == [("a000.com", "Fintech"), ("a001.com", "Fintech")]
    rows = labels.judged_rows(ctx, judged)
    assert [r["expect"]["accept"] for r in rows] == [["Fintech", TECH], ["Fintech", TECH]]
    assert rows[0]["description"] == "Payments software." and rows[0]["home"]["text"] == "Card payments for shops."
    events = labels.gold_events(rows[0])
    assert {e["fact"] for e in events} == {"description", "home_page"}
    assert labels.Material.of({"domain": "a000.com"}, events).home  # the model is shown the page
