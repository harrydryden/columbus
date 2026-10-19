"""Self-balancing tier thresholds (scoring/balance.py; Harry, 19 Oct 2026)."""

from __future__ import annotations

from datetime import timedelta

from tests.fakes import make_context
from tests.test_scoring import NOW, _with_slack, account, fact, make_settings, sig
from us_outbound.scoring import balance
from us_outbound.scoring.score import rescore
from us_outbound.settings.model import General


def spread(n: int = 100) -> list[int]:
    """Scores 0 to 99, one each."""
    return list(range(n))


def test_top_fit_is_the_best_fifth_and_the_comparison_group_about_thirty_percent():
    t = balance.balance(spread(), General(weekly_enrol_cap=10))  # a small cap: capacity is no constraint
    assert t.priority == 80 and t.top_fit == 20
    assert t.standard == 30 and t.pool - t.good_fit == 30 and not t.short


def test_too_few_companies_leave_the_sheet_s_thresholds():
    assert balance.balance(list(range(19)), General()) is None


def test_a_company_needs_a_strong_signal_to_be_top_fit():
    t = balance.balance([5] * 50 + [20] * 50, General(weekly_enrol_cap=10))
    assert t.priority == balance.PRIORITY_FLOOR and t.top_fit == 0  # nobody reaches 30 points: nobody is top fit
    t = balance.balance([0] * 90 + [35] * 10, General(weekly_enrol_cap=10))
    assert t.priority == 35 and t.top_fit == 10


def test_the_good_fit_pool_is_kept_big_enough_for_two_weeks_of_sending():
    # 150 a week, 85% not comparison: two weeks needs 255 good-fit companies; the comparison group 45
    t = balance.balance(spread(400), General(weekly_enrol_cap=150))
    assert t.need == 255 and t.good_fit >= 255 and t.pool - t.good_fit >= 45 and not t.short
    assert t.weeks >= 2


def test_a_queue_too_small_for_both_keeps_the_good_fit_pool_and_says_it_is_short():
    t = balance.balance(spread(280), General(weekly_enrol_cap=150))
    assert t.good_fit >= 255 and t.short  # the comparison group gets less than its two weeks
    t = balance.balance([0] * 200 + [10] * 50, General(weekly_enrol_cap=150))
    assert (t.standard, t.good_fit, t.short) == (balance.STANDARD_FLOOR, 50, True)  # every company with a signal


def test_ties_choose_the_nearest_share_and_thresholds_never_cross():
    t = balance.balance([40] * 60 + [10] * 40, General(weekly_enrol_cap=10))
    assert t.priority == 40 and t.standard <= t.priority


def _queue(n: int = 40):
    """n queue accounts scoring 0 to n - 1 points: a point for each open role (one signal a role)."""
    points = tuple(sig(f"Roles {k}", "apollo_org", f"open_roles >= {k}", 1) for k in range(1, n))
    rows, events = [], []
    for i in range(n):
        rows.append(account(account_id=f"q{i}", domain=f"q{i}.com"))
        events += [fact("apollo_org", "open_roles", i, account_id=f"q{i}")]
    return points, rows, events


def test_rescore_sets_the_day_s_thresholds_once_and_tiers_by_them():
    points, rows, events = _queue()
    ctx = make_context(make_settings(points, weekly_enrol_cap=5), now=NOW)
    ctx.store.tables["accounts"], ctx.store.tables["signal_events"] = rows, events
    out = rescore(ctx)
    t = out["thresholds"]
    assert (t["auto"], t["priority"], t["standard"]) == (True, 32, 12)  # the best fifth; about 30% below
    assert out["tiers"]["Priority"] == 8 and out["tiers"]["Control"] == 12
    [kept] = ctx.store.select("signal_events", {"source": balance.SOURCE, "fact": balance.FACT})
    assert kept["value"]["day"] == NOW.date().isoformat() and kept["account_id"] is None
    tiers = {a["account_id"]: a["tier"] for a in ctx.store.select("accounts")}
    scores = {a["account_id"]: a["score"] for a in ctx.store.select("accounts")}
    assert all(tiers[a] == "Priority" for a, s in scores.items() if s >= t["priority"])
    assert all(tiers[a] == "Control" for a, s in scores.items() if s < t["standard"])
    assert out["tier_share_alert"] is False
    # Later the same day: the same thresholds, even if the queue moves.
    ctx.store.tables["accounts"] += [account(account_id=f"z{i}", domain=f"z{i}.com") for i in range(40)]
    again = rescore(ctx)["thresholds"]
    assert (again["priority"], again["standard"]) == (t["priority"], t["standard"])
    assert len(ctx.store.select("signal_events", {"source": balance.SOURCE, "fact": balance.FACT})) == 1


def test_the_stored_thresholds_are_laid_over_the_sheet_s_and_off_means_as_typed():
    points, rows, events = _queue()
    ctx = make_context(make_settings(points, weekly_enrol_cap=5), now=NOW)
    ctx.store.tables["accounts"], ctx.store.tables["signal_events"] = rows, events
    t = rescore(ctx)["thresholds"]
    laid = balance.applied(ctx.store, ctx.settings)
    assert (laid.general.priority_threshold, laid.general.standard_threshold) == (t["priority"], t["standard"])
    off = make_settings(points, auto_thresholds=False)
    assert balance.applied(ctx.store, off) is off
    _, rows, events = _queue()
    ctx = make_context(off, now=NOW + timedelta(days=1))
    ctx.store.tables["accounts"], ctx.store.tables["signal_events"] = rows, events
    out = rescore(ctx)["thresholds"]
    assert (out["auto"], out["priority"], out["standard"]) == (False, 50, 20)
    assert ctx.store.select("signal_events", {"source": balance.SOURCE}) == []


def test_a_short_queue_posts_once_a_week_and_the_mix_alert_stays_quiet():
    points, rows, events = _queue()
    ctx = make_context(make_settings(points), now=NOW)  # 150 a week: 40 companies are far too few
    slack = _with_slack(ctx)
    ctx.store.tables["accounts"], ctx.store.tables["signal_events"] = rows, events
    out = rescore(ctx)
    assert out["thresholds"]["short"] is True and out["tier_share_alert"] is False
    [(_, text)] = slack.posts
    assert "Not enough companies in the queue for the next two weeks" in text
    assert "Focus tab" in text and "weekly_enrol_cap" in text
    rescore(ctx)
    assert len(slack.posts) == 1


def test_the_daily_post_line_says_what_counts_today():
    v = balance.Thresholds(priority=42, standard=12, pool=400, good_fit=300, top_fit=80, need=255, short=False).value()
    assert balance.describe(v) == ("Fit today (set automatically): top fit 42+ points (80 companies), good fit 12+ "
                                   "(300), about 2.4 weeks of sending")
    assert balance.describe(None) == ""
