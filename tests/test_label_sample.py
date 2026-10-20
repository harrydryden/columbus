"""The label sample (ops/label_sample.py; Harry, 10 Oct 2026): a random draw of labelled companies for Harry to judge
by hand, and his verdicts counted."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fakes import make_context
from tests.test_copy_desk import Sheet
from tests.test_labels import DEFAULT
from us_outbound.ops import label_sample as ls

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


def test_a_refused_read_never_rewrites_the_answer_key():
    """W0, 19 Oct 2026 (code-verify-labels B12). October's 50 rows are on the tab, 50 judged. A read refused with a
    400 that is not "no such tab" (an invalid request) used to read as "no tab", so add_batch added the tab and
    replaced it with November's 25 alone: the 50 verdicts, the answer key, gone. Now the read raises, the batch
    fails, and the tab keeps its 50 rows with nothing written."""
    from us_outbound.clients.http import ApiError

    live = world(live=True)
    ls.run(live)
    tab = live.clients.sheets.tabs[ls.TAB]
    for r in tab:
        r["your_verdict"] = "right"
    before = [dict(r) for r in tab]
    writes = list(live.clients.sheets.writes)

    def refused(sheet_id, tabs):
        raise ApiError("sheets", 400, {"error": {"code": 400, "message": "Invalid JSON payload received."}})

    live.clients.sheets.read_tabs = refused
    with pytest.raises(ApiError):
        ls.add_batch(live, 25, "2026-11")
    assert live.clients.sheets.tabs[ls.TAB] == before and len(before) == 50
    assert live.clients.sheets.writes == writes  # no add, no replace


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
    # 25 new companies and REJUDGE (2) of October's 40 judged put back unmarked (W3-2, T1r): 27 rows, 50 + 27 = 77.
    assert (out["batch"], out["drawn"], out["rejudged"], out["last_batch"]) == ("2026-11", 25, 2, "2026-10")
    assert (out["last"]["judged"], out["last"]["label_right"]) == (40, 40)
    assert len(live.clients.sheets.tabs[ls.TAB]) == 77
    [text] = posts
    sheet = live.guard.bounds.settings_sheet_id
    tab = f"<https://docs.google.com/spreadsheets/d/{sheet}/edit#gid=1234|Label sample tab>"
    assert text.startswith(":raised_hand: *27 companies to check: is our industry label right?*")
    assert f"27 new companies to judge (2026-11) are on the {tab}." in text
    assert (f"On the {tab}, fill `your_verdict` for each new row: the right label, or right, right "
            "group, wrong group or outside.") in text
    assert "Untrue pitch: the email said more about the company than the verdict allows." in text  # the terms explained
    assert "Last month (2026-10): 40 of 50 judged · label right 100% · group right 100% · untrue pitch 0%" in text
    assert "10 of last month's companies are not judged yet." in text
    # Every row was judged right (its own label), but the model said Fintech of the tech ones: the misses get drafts.
    assert out["suggestions"]["misses"] > 0


def test_the_monthly_post_links_the_definition_suggestions(monkeypatch):
    from tests.test_definition_suggestions import Writer
    from us_outbound import labels
    from us_outbound.base import notify

    posts: list[str] = []
    monkeypatch.setattr(notify, "alert", lambda ctx, text, **k: posts.append(text) or {"posted": True})
    # Both answer-key passes check every row (a pass cut short scores nothing: test_definition_suggestions).
    monkeypatch.setattr(labels, "evaluate", lambda c, rows, **kw: {labels.ACCEPTABLE: len(rows) - 2, labels.WRONG: 2,
                                                             labels.UNSAFE: 0, "asked": len(rows)})
    live = world(live=True)
    ls.run(live)
    for r in live.clients.sheets.tabs[ls.TAB]:
        r["your_verdict"] = r["our_label"]
    live.clients.sheets.gids = {ls.TAB: 1, "Definition suggestions": 2}
    live.clients.claude = Writer([{"label": TECH, "definition": "A technology company with its own product.",
                                   "why": "the Fintech misses"}])
    live.now = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    out = ls.monthly(live)
    assert len(out["suggestions"]["suggestions"]) == 1
    [text] = posts
    sheet = live.guard.bounds.settings_sheet_id
    assert (f"1 definition change drafted for the labels the check confused (Fintech, Technology & Startups): "
            f"<https://docs.google.com/spreadsheets/d/{sheet}/edit#gid=2|Definition suggestions tab>") in text
    assert (f"2. On the <https://docs.google.com/spreadsheets/d/{sheet}/edit#gid=2|Definition suggestions tab>, set "
            "`decision` to approve or reject. Approved ones go onto the Industries tab at the next settings sync") in text


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


def test_ten_right_verdicts_of_fifty_score_ten_of_ten():
    """The score of a batch with 10 of its 50 rows judged right: 10 judged, 10 right (the `labels sample --score`
    command went in W2-4; the monthly job scores each batch with score())."""
    live = world(live=True)
    ls.run(live)
    for r in live.clients.sheets.tabs[ls.TAB][:10]:
        r["your_verdict"] = "right"
    out = ls.score(live.clients.sheets.tabs[ls.TAB], live.settings)
    assert (out["judged"], out["label_right"]) == (10, 10)


def test_the_judged_companies_join_the_eval_with_their_facts_and_home_page():
    """Harry, 11 Oct 2026: every company judged by hand is scored by `labels eval --from-sample`, so a change to the
    prompt or the definitions is tried on them first."""
    from us_outbound import labels

    ctx = world(live=True, n=4)
    ls.run(ctx, 4)
    tab = ctx.clients.sheets.tabs[ls.TAB]
    tab[0]["your_verdict"] = "Fintech"  # a000: we said Technology & Startups
    tab[1]["your_verdict"] = "right"  # a001: we said Fintech
    tab[2]["your_verdict"] = "outside"  # a negative: the eval expects it left out (W3-2; B11)
    ctx.store.insert("signal_events", [
        {"event_id": "a000-d", "account_id": "a000", "source": "apollo_org", "fact": "description",
         "value": "Payments software.", "observed_at": AT},
        {"event_id": "a000-h", "account_id": "a000", "source": "careers_pages", "fact": "home_page",
         "value": {"title": "Company 0", "text": "Card payments for shops."}, "observed_at": AT}])
    judged = ls.sample_rows(ctx)
    assert [(d, getattr(i, "industry", i)) for d, i in judged] == [
        ("a000.com", "Fintech"), ("a001.com", "Fintech"), ("a002.com", ls.OUTSIDE)]
    rows = labels.judged_rows(ctx, judged)
    assert [r["expect"] for r in rows] == [{"accept": ["Fintech", TECH], "action": "verify"},
                                           {"accept": ["Fintech", TECH], "action": "verify"},
                                           {"accept": [], "action": "disqualify"}]
    assert rows[0]["description"] == "Payments software." and rows[0]["home"]["text"] == "Card payments for shops."
    events = labels.gold_events(rows[0])
    assert {e["fact"] for e in events} == {"description", "home_page"}
    assert labels.Material.of({"domain": "a000.com"}, events).home  # the model is shown the page


# -- W3-2 (20 Oct 2026; code-verify-labels B11, RT §1.5, §3 L4) ------------------------------------------------------


def test_outside_scores_an_opinion_of_no_label_as_right():
    """A company judged outside: the model's none and the rules' "no label" are right (they read as a wrong group
    before); a label is a wrong group; "not asked" is no opinion."""
    rows = [{"domain": "o.org", "our_label": TECH, "copy": "general", "rules_said": "no label",
             "model_said": "none (high)", "your_verdict": "outside"},
            {"domain": "p.org", "our_label": TECH, "copy": "general", "rules_said": TECH,
             "model_said": "not asked", "your_verdict": "outside"}]
    out = ls.score(rows, DEFAULT)
    assert out["opinions"]["model"] == {"judged": 2, "gave_none": 1, "label_right": 1, "group_right": 1}
    assert out["opinions"]["rules"] == {"judged": 2, "gave_none": 0, "label_right": 1, "group_right": 1}
    assert [t for _, t in out["truths"]] == [ls.OUTSIDE, ls.OUTSIDE]


def test_the_untrue_pitch_reads_the_copy_level_enrol_stamped():
    """The row's copy column is the level at the draw (group: the account was umbrella then); enrol later sent it the
    label's own copy (contacts.copy_level = label). Judged "right group" (the label was Fintech, the right one
    Healthtech): with label copy that pitch was untrue, so it counts; by the draw-time column it would not. The
    label-copy rows are counted apart: 1 judged, 0 exactly right, 1 at group, 1 wrong pitch."""
    rows = [{"domain": "b.com", "our_label": "Fintech", "copy": "group", "decided_by": "umbrella (medium)",
             "your_verdict": "right group", "right_label": "Healthtech"}]
    assert ls.score(rows, DEFAULT)["wrong_pitch"] == 0
    out = ls.score(rows, DEFAULT, {"b.com": "label"})
    assert out["wrong_pitch"] == 1
    assert out["label_copy"] == {"judged": 1, "label_right": 0, "group_right": 1, "wrong_pitch": 1}
    ctx = world(n=2)
    ctx.store.insert("contacts", [{"contact_id": "c1", "account_id": "a001", "enrolled_at": AT, "copy_level": "label"},
                                  {"contact_id": "c2", "account_id": "a000", "copy_level": "label"}])  # not enrolled
    assert ls.sent_levels(ctx, ["a000", "a001"]) == {"a001": "label"}
    assert {r["domain"]: r["copy"] for r in ls.draw(ctx, 2)} == {"a000.com": "group", "a001.com": "label"}


def test_the_score_by_how_the_label_was_decided_feeds_the_policy():
    """October (data-crm-settings §Label sample), by decided_by: rules+model 20 of 21 exact, model 6 of 9, umbrella 5
    of 10. by_source counts (exactly right, judged); labels.policy routes rules+model to label copy and the other two
    to group copy."""
    def rows(source, right, n):
        return [{"domain": f"{source}{i}.com", "our_label": "Fintech", "copy": "label",
                 "decided_by": f"{source} (high)", "your_verdict": "right" if i < right else "Healthtech"}
                for i in range(n)]

    out = ls.score(rows("rules+model", 20, 21) + rows("model", 6, 9) + rows("umbrella", 5, 10), DEFAULT)
    assert out["by_source"] == {"rules+model": [20, 21], "model": [6, 9], "umbrella": [5, 10]}
    assert labels_policy({k: tuple(v) for k, v in out["by_source"].items()}) == {
        "rules+model": "label", "model": "group", "umbrella": "group", "rules": "group"}
    # The 40 label-copy rows: 31 exactly right; the 9 others are Healthtech, in Fintech's group: 40 at group.
    assert out["label_copy"] == {"judged": 40, "label_right": 31, "group_right": 40, "wrong_pitch": 9}


def labels_policy(counts):
    from us_outbound import labels

    return labels.policy(counts)


def test_two_earlier_rows_come_back_unmarked_and_their_agreement_is_reported(monkeypatch):
    """The re-judge draw (T1r): November's batch is 25 new companies and 2 of October's judged ones, as rows of the
    batch with nothing to mark them (blank verdict columns, in domain order with the rest). They are scored once,
    by their first verdict (the October score keeps 40 judged), and December's post reports the second verdict
    against the first: both given again the same, 2 of 2."""
    from us_outbound.base import notify

    posts: list[str] = []
    monkeypatch.setattr(notify, "alert", lambda ctx, text, **k: posts.append(text) or {"posted": True})
    live = world(live=True)
    ls.run(live)
    for r in live.clients.sheets.tabs[ls.TAB][:40]:
        r["your_verdict"] = "right"
    october = {r["domain"] for r in live.clients.sheets.tabs[ls.TAB][:40]}
    live.now = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    out = ls.monthly(live)
    tab = live.clients.sheets.tabs[ls.TAB]
    november = [r for r in tab if r["batch"] == "2026-11"]
    again = [r for r in november if r["domain"] in october]
    assert (len(november), len(again)) == (27, 2)
    assert list(again[0]) == ls.COLUMNS and {r["your_verdict"] for r in november} == {""}
    assert [r["domain"] for r in november] == sorted(r["domain"] for r in november)  # unmarked: domain order
    assert out["agreement"] == (0, 0)  # none judged twice yet
    first, repeats = ls.split_repeats(tab)
    assert (len(first), len(repeats)) == (75, 2)
    for r in november:
        r["your_verdict"] = "right"
    live.now = datetime(2026, 12, 1, 9, 15, tzinfo=UTC)
    december = ls.monthly(live)
    assert december["agreement"] == (2, 2)
    assert december["all"]["judged"] == 40 + 25  # each company once: the 2 again are not counted twice
    assert ("Judged again: 2 companies you judged before came back unmarked; your verdict matched the first for 2 "
            "of 2.") in posts[-1]
    # A second verdict that differs (the right label is Healthtech, not the Fintech/Tech label given before).
    for r in live.clients.sheets.tabs[ls.TAB]:
        if r["batch"] == "2026-11" and r["domain"] in october:
            r["your_verdict"] = "Healthtech"
    assert ls.agreement(live.clients.sheets.tabs[ls.TAB], live.settings) == (0, 2)


def test_the_monthly_job_stores_the_label_policy(monkeypatch):
    """Each month the policy is computed from the last labels.POLICY_BATCHES batches' first verdicts, by decided_by,
    and stored as this month's label_policy fact with the sample's score; the post says what it routes and that it
    is not applied (copy_level_policy no)."""
    from us_outbound import labels
    from us_outbound.base import notify

    posts: list[str] = []
    monkeypatch.setattr(notify, "alert", lambda ctx, text, **k: posts.append(text) or {"posted": True})
    live = world(live=True)
    ls.run(live)
    tab = live.clients.sheets.tabs[ls.TAB]
    for r in tab:
        r["your_verdict"] = "right"  # 25 rules+model (Fintech) and 25 umbrella (the group's label), each right
    by = {}
    for r in tab:
        by[r["decided_by"]] = by.get(r["decided_by"], 0) + 1
    live.now = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    out = ls.monthly(live)
    stored = labels.stored_policy(live)
    assert stored == out["policy"] and stored["batches"] == ["2026-10"]
    n_agreed = by["rules+model (high)"]
    assert stored["counts"]["rules+model"] == [n_agreed, n_agreed]
    assert stored["policy"]["rules+model"] == ("label" if n_agreed >= 20 else "group")
    assert stored["sample"]["judged"] == 50 and stored["sample"]["label_right"] == 50
    [e] = live.store.select("signal_events", {"fact": labels.POLICY_FACT})
    assert e["event_id"] == "label_policy:2026-11" and e["account_id"] is None
    assert "not applied: General `copy_level_policy` is no, so nothing changes" in posts[-1]
