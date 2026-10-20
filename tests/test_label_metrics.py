"""The industry labels' numbers (learn/label_metrics.py; W3-2, 20 Oct 2026: moved out of labels.py with these tests,
and rewritten on the label sample and the hand-check): the daily post's Labels block and its asks, and the Monday
readout's Industry labels block with the label sample's score, the label policy and the hand-check's wrong-industry
rows."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from tests.fakes import make_context
from tests.test_labels import DEFAULT, TECH
from us_outbound import labels
from us_outbound.learn import label_metrics
from us_outbound.learn.daily_post import period

# -- measurement: the daily post's Labels block, its asks and the readout -----------------------------------------------

TUE_9 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)  # 09:00 UK, Tue 27 Oct: the post covers Monday 26 Oct
MON = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)


def checked_fact(i, source, *, action="verify", rules="Fintech", model="Fintech", group=TECH, at=MON):
    return {"event_id": f"v{i}", "account_id": f"a{i}", "source": "label_check", "fact": "label_verdict",
            "value": {"rules": rules, "rules_group": group, "model": model, "asked": True,
                      "decision": {"source": source, "action": action}}, "observed_at": at}


def correction(i, frm, to, at=MON):
    return {"event_id": f"c{i}", "account_id": f"a{i}", "source": "label_check", "fact": "label_corrected",
            "value": {"from": frm, "to": to, "by": "U_HARRY", "via": "thread"}, "observed_at": at}


def decided(i, approval="approved", at=MON):
    """A send approval's decision, as the cards wrote them before W2-1 removed them: no label figure counts it now."""
    return {"event_id": f"send-approval:{i}", "type": "send_approval", "approval": approval, "occurred_at": at}


def measured(facts_=(), events=(), verify_labels=None, now=TUE_9):
    ctx = make_context(DEFAULT, now=now)
    ctx.store.insert("signal_events", list(facts_))
    ctx.store.insert("events", list(events))
    if verify_labels is not None:
        ctx.store.insert("heartbeats", [{"run_id": "v1", "job": "verify_accounts", "status": "ok",
                                         "started_at": now - timedelta(hours=4), "detail": {"labels": verify_labels}}])
    return ctx


def test_the_daily_posts_labels_block():
    facts_ = [*(checked_fact(i, "rules+model") for i in range(5)), checked_fact(5, "model", model="Edtech"),
              checked_fact(6, "umbrella", model="Edtech"), checked_fact(7, "disputed", action="hold"),
              checked_fact(8, "disputed", action="disqualify", model="none"),
              checked_fact(9, "rules+model", at=MON - timedelta(days=3)),  # before the period
              {**checked_fact(10, "model"), "value": {**checked_fact(10, "model")["value"], "asked": False}},  # re-decided
              correction(1, "Games studios", "Technology & Startups")]
    events = [*(decided(i) for i in range(18)), decided(30, "company_rejected"), decided(31, "expired")]
    ctx = measured(facts_, events, {"unchecked": 3, "unavailable_reason": "the monthly Claude cap is reached"})
    start, end, _ = period(ctx)
    # Agreement is the stored verdicts' rules label against the model's (W3-2), not the rules+model decisions: the
    # five rules+model rows (Fintech/Fintech) and row 7 (rules Fintech, model Fintech, held) agree on the label, 6;
    # rows 5 and 6 (Edtech) agree on the group, 8 of 9; row 8 (none) agrees on neither.
    assert label_metrics.post_lines(ctx, start, end) == [
        "  Checked: 9 (of the 9 the rules labelled, the model agreed on the label for 6 and the group for 8, 89%; "
        "no rules label 0; model overruled 1; group copy 1; General copy 0; held 1; disqualified 1; waiting "
        "unchecked 3).",
        "  The label check could not ask the model at the last verify run: the monthly Claude cap is reached.",
        # The cards' "N of N right" went with them (W2-1; design-final §4.4): the 20 decided cards count for nothing.
        "  Corrected by hand: 1 (Games studios → Technology & Startups).",
    ]
    assert not any("Corrected" in line or "Cards" in line for line in label_metrics.post_lines(measured(), start, end))


def test_the_daily_post_carries_the_block():
    from tests.test_daily_post import world as post_world
    from us_outbound.learn import daily_post

    ctx, _ = post_world()
    lines, _ = daily_post.build(ctx)
    i = lines.index("*Labels*")
    assert lines[i + 1].startswith("  Checked: 0 (of the 0 the rules labelled, the model agreed on the label for 0 "
                                   "and the group for 0, n/a;") and lines.index("*Mailboxes*") > i


def test_the_asks_trip_once_a_day_on_corrections_and_an_unavailable_check():
    facts_ = [*(correction(i, "Adtech & martech", "Fintech") for i in range(3)),
              *(checked_fact(10 + i, "rules+model") for i in range(10)),
              *(checked_fact(30 + i, "model", rules="Adtech & martech", model="Marketing agencies") for i in range(12)),
              *(checked_fact(50 + i, "model", rules="Adtech & martech", model="Fintech") for i in range(3))]
    ctx = measured(facts_, [decided(i) for i in range(20)], {"unchecked": 4, "unavailable_reason": "Claude did not answer"})
    got = dict(label_metrics.asks(ctx))
    tab = "<https://docs.google.com/spreadsheets/d/sheet-test/edit|Industries tab>"
    # 3 corrections reach ASK_CORRECTIONS (3) whatever the decided cards: the share of cards went with them (W2-1).
    assert got["labels_corrections:2026-10-27"] == (
        ":warning: *Industry labels: the industry of 3 companies was corrected by hand since the last send day.*\n"
        "Corrections: Adtech & martech → Fintech (3).\n"
        f"*What to do:* check those industries' definitions on the {tab}: a sharper line fixes the next ones. A "
        "changed definition has every company checked again by itself; nothing to run.")
    # The rules' disagreement with the model is no ask any more (12 Oct 2026): the model's label decides the email.
    assert "labels_agreement:2026-10-27" not in got and "labels_no_rules:2026-10-27" not in got
    assert got["labels_unavailable:2026-10-27"].startswith(
        ":rotating_light: *Industry labels: the AI check (Claude) could not run, so 4 new companies wait unchecked.*\n"
        "Why: Claude did not answer. While `label_check` is `required` (General tab), a company is not emailed")
    assert label_metrics.asks(measured([checked_fact(1, "rules+model")], [decided(1)])) == []


def test_a_company_asked_twice_counts_once_by_its_latest_check():
    """Thu 8 Oct 2026: two audits and the home-page second look asked about the same companies, and the ask said
    "1172 companies"."""
    first = checked_fact(1, "disputed", action="hold", model="Edtech", at=MON - timedelta(hours=3))
    again = {**checked_fact(1, "rules+model"), "event_id": "v1-again"}
    t = label_metrics.tally(measured([first, again]), MON - timedelta(days=1), TUE_9)
    assert (t.checked, t.labelled, t.same_label, t.decisions) == (1, 1, 1, {"rules+model": 1})


def test_no_rules_label_is_counted_apart_from_disagreement():
    """A company the rules gave no label cannot agree with the model: the tally keeps it apart, and it is no ask."""
    facts_ = [*(checked_fact(i, "rules+model") for i in range(20)),
              *(checked_fact(100 + i, "disputed", action="hold", rules=None, group=None, model="Fintech")
                for i in range(15)),
              *(checked_fact(200 + i, "model", rules=None, group=None, model="Edtech") for i in range(6)),
              checked_fact(300, "disputed", action="hold", rules=None, group=None, model="none")]
    naics = [{"event_id": f"n{i}", "account_id": f"a{i}", "source": "apollo_org", "fact": "naics",
              "value": ["522320"] if i < 112 else [], "observed_at": MON - timedelta(days=5)} for i in range(100, 115)]
    naics += [{"event_id": f"n{i}", "account_id": f"a{i}", "source": "apollo_org", "fact": "naics",
               "value": ["611710", "541990"], "observed_at": MON - timedelta(days=5)} for i in range(200, 206)]
    ctx = measured([*facts_, *naics], [decided(i) for i in range(5)])
    assert dict(label_metrics.asks(ctx)) == {}  # a gap in the rules is no ask (12 Oct 2026): the model labels them
    t = label_metrics.tally(ctx, *period(ctx)[:2])
    assert (t.checked, t.labelled, t.share_same_label()) == (42, 20, 1.0)


def test_the_asks_go_out_with_the_spend_asks():
    from us_outbound.learn import spend

    ctx = measured([correction(i, "Games studios", "Fintech") for i in range(3)], [decided(1)])
    sp = spend.read(ctx)
    keys = [k for k, _ in spend.asks(ctx, sp)]
    assert "labels_corrections:2026-10-27" in keys


def test_the_monday_readout_carries_the_block():
    from us_outbound.learn import readout

    ctx = make_context(DEFAULT, now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    lines, _ = readout.build(ctx)
    assert any(x.startswith("*Industry labels*") for x in lines)


def test_the_readout_block():
    """Last week's check and corrections as before; then the label sample as the newest label_policy fact keeps it.
    October's sample (W3-2; data-crm-settings §Label sample): 50 judged, 34 exactly right, 44 at group; 30 sent a
    label's own copy, 4 of them a wrong pitch. Wilson: 34/50 (0.542, 0.792); 44/50 (0.762, 0.944); 4/30 (0.053,
    0.297). The policy: rules+model 20 of 21 -> label copy, model 6 of 9 and umbrella 5 of 10 -> group copy; not
    applied while copy_level_policy is no, and what it would change said either way."""
    start, end = datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 26, tzinfo=UTC)
    at = datetime(2026, 10, 21, 12, tzinfo=UTC)
    facts_ = [*(checked_fact(i, "rules+model", at=at) for i in range(8)), checked_fact(8, "disputed", action="hold", at=at),
              correction(1, "Games studios", "Technology & Startups", at=at),
              correction(2, "Games studios", "Technology & Startups", at=at)]
    ctx = measured(facts_, now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    sample = {"judged": 50, "label_right": 34, "group_right": 44, "wrong_pitch": 5,
              "label_copy": {"judged": 30, "label_right": 26, "group_right": 29, "wrong_pitch": 4},
              "rejudged": 2, "rejudge_agreed": 2}
    labels.store_policy(ctx, {"rules+model": (20, 21), "model": (6, 9), "umbrella": (5, 10)}, ["2026-10"], sample)
    lines = label_metrics.readout_lines(ctx, start, end, (2, 48))
    assert lines[1].startswith("*Industry labels*")
    assert lines[2] == "  Corrected last week: Games studios → Technology & Startups (2)"
    assert lines[3] == ("  Checked last week by the AI model against our industry-code rules: 9 · agreeing on the "
                        "group for 100% · held for the hand-check 1 · disqualified 0")
    assert lines[4] == ("  Judged by hand (the monthly label sample, 2026-10): label exactly right 34 of 50 (68%, 95% "
                        "54%–79%); group right 44 of 50 (88%, 95% 76%–94%).")
    assert lines[5] == ("  Emails sent a label's own copy: 4 of 30 pitched the wrong label (4 of 30 (13%, 95% "
                        "5%–30%)).")
    assert lines[6] == "  Judged twice: the second verdict matched the first for 2 of 2 (the rater's own drift)."
    assert lines[7] == ("  Label policy: rules+model 20 of 21 exactly right → label copy · model 6 of 9 exactly right "
                        "→ group copy · umbrella 5 of 10 exactly right → group copy; not applied (General "
                        "copy_level_policy is no, Harry's D20): with it, model labels get their group's copy when it "
                        "is.")
    assert lines[8] == "  The hand-check found the wrong industry on 2 of 48 (4%, 95% 1%–14%) random rows."
    on = dataclasses.replace(ctx, settings=dataclasses.replace(
        DEFAULT, general=dataclasses.replace(DEFAULT.general, copy_level_policy=True)))
    assert label_metrics.readout_lines(on, start, end)[7].endswith(
        "applied (General copy_level_policy is yes): with it, model labels get their group's copy.")
    assert not any("Right" in line or "%, met" in line for line in lines)
    none = label_metrics.readout_lines(measured([]), start, end)
    assert none[2:] == ["  No company checked or corrected last week.",
                        "  Judged by hand (the monthly label sample): no batch scored yet."]
