"""The segment prior and the segment value (learn/segments.py; W3-4, 21 Oct 2026; design-final §5 L9; data-icp §3).

Each expected number is derived in its test from the definitions (redteam-stats §5 corrected the Construction one:
0.268, so 0.705).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_context
from us_outbound.learn import segments
from us_outbound.learn.outcomes import Outcome
from us_outbound.settings import suggestions as tab
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all
from us_outbound.sources import lookalikes as lk

NOW = datetime(2026, 11, 1, 2, 20, tzinfo=UTC)
LEGAL, TECH, BUILD = "Legal Teams", "Technology & Startups", "Construction & Trades"


def deals(industry: str, won: int, decided: int, *, covered: int | None = None, churned: int = 0,
          amount: float = 100.0, days: float = 30.0) -> list[dict]:
    """decided Spill 3.0 deals in one HubSpot industry: `won` won (the first `churned` of them churned since), the
    rest lost."""
    out = []
    for i in range(decided):
        stage = ("1287546140" if i < churned else "154381891") if i < won else "154381892"
        out.append({"id": f"{industry}-{i}", "properties": {
            "industry": industry, "dealstage": stage, "days_to_close": str(days), "amount_in_home_currency": str(amount),
            "employees_covered__cloned_": str(covered) if covered else ""}})
    return out


DEALS = [*deals("Law", 9, 15, amount=275, days=18), *deals("Tech", 72, 167, amount=177, days=62),
         *deals("Construction & Utilities", 5, 27, amount=90, days=21)]


@pytest.fixture
def ctx():
    settings, _ = validate_all(default_tabs())
    c = make_context(settings, now=NOW)
    asked: list = []

    def iter_deals(groups, properties):
        asked.append((groups, list(properties)))
        return iter(DEALS)

    c.clients.hubspot.iter_deals = iter_deals
    c.asked = asked  # type: ignore[attr-defined]
    return c


def test_the_prior_shrinks_each_group_s_win_rate_toward_0_38():
    """(won + 0.38 × 20) ÷ (decided + 20), the mean of Beta(won + 7.6, lost + 12.4): Legal 9 of 15 → 16.6 ÷ 35 =
    0.474; Tech 72 of 167 → 79.6 ÷ 187 = 0.426; Construction 5 of 27 → 12.6 ÷ 47 = 0.268 (RT §5)."""
    settings, _ = validate_all(default_tabs())
    cells = segments.tally(settings, DEALS)
    got = {k: (c.won, c.decided, round(c.win_rate, 3)) for (kind, k), c in cells.items() if kind == segments.GROUP}
    assert got == {LEGAL: (9, 15, 0.474), TECH: (72, 167, 0.426), BUILD: (5, 27, 0.268)}


def test_refresh_reads_the_decided_deals_once_and_stores_the_prior(ctx):
    out = segments.refresh(ctx)
    assert out["stored"] and out["deals"] == 15 + 167 + 27 and out["day"] == "2026-11-01"
    [(groups, props)] = ctx.asked  # one paged, read-only search, for the five deal properties only
    assert props == list(segments.DEAL_FIELDS)
    assert groups == [[{"propertyName": "pipeline", "operator": "EQ", "value": "82002613"},
                       {"propertyName": "dealstage", "operator": "IN",
                        "values": ["1287546140", "154381891", "154381892"]}]]
    assert lk.segment_prior(ctx.store) == {LEGAL: (9, 15), TECH: (72, 167), BUILD: (5, 27)}
    legal = {r["metric"]: r for r in lk.prior_rows_latest(ctx.store) if r["segment_key"] == f"group={LEGAL}"}
    assert legal[lk.PRIOR_GBP]["value"] == 275 and legal[lk.PRIOR_DAYS]["value"] == 18
    assert (legal[lk.PRIOR_RETAINED]["k"], legal[lk.PRIOR_RETAINED]["n"]) == (9, 9)  # none churned: (9 + 1) ÷ 11
    assert round(legal[lk.PRIOR_RETAINED]["value"], 3) == round(10 / 11, 3)
    assert all(r["definition_version"] == segments.PRIOR_VERSION for r in lk.prior_rows_latest(ctx.store))


def test_the_lookalike_fit_reads_the_prior_not_the_volume():
    """The industry part is the shrunk win rate ÷ 0.38, clipped to [0.5, 1.5]: Construction 0.268 ÷ 0.38 = 0.705;
    Legal 0.474 ÷ 0.38 = 1.248 (1.25); Tech 0.426 ÷ 0.38 = 1.120 (1.12). Tech has by far the most customers, so the
    volume term gave it the most; the prior puts Legal first. A group with no decided deal is 0.38 ÷ 0.38 = 1.0. The
    fit is capped at 100 (industry 1.248 with size and growth 1.0 is 100 × (0.45 × 1.248 + 0.55) = 111)."""
    model = lk.FitModel([], [], {LEGAL: (9, 15), TECH: (72, 167), BUILD: (5, 27)})
    assert model._industry("Construction firms", BUILD)[0] == 0.705
    assert round(model._industry(LEGAL, LEGAL)[0], 2) == 1.25 and round(model._industry(TECH, TECH)[0], 2) == 1.12
    assert model._industry("Restaurants", "Hospitality")[0] == 1.0
    assert model._industry(LEGAL, LEGAL)[1] == "Legal Teams won 9 of 15 decided deals after a demo (47% shrunk; 1.25)"
    assert lk.FitModel([], [], {BUILD: (0, 200)})._industry("x", BUILD)[0] == 0.5  # 7.6 ÷ 220 = 0.035: clipped
    fit = lk.FitModel([lk_cell(LEGAL, "10-49")], [], {LEGAL: (9, 15)}).fit(LEGAL, LEGAL, "10-49", None)
    assert fit.fit == 100


def lk_cell(group: str, band: str) -> dict:
    return {"cell_id": f"{group}|{band}", "industry_label": group, "industry_group": group, "size_band": band,
            "active_customers": 10, "churned_customers": 0, "us_active": 0, "us_churned": 0, "strength": 10.0}


def test_until_a_prior_is_stored_the_fit_is_as_before(ctx):
    """No segment prior yet (before the first segment_prior_monthly): the customers' strength stands in, so the deploy
    changes no score until the prior lands."""
    ctx.store.insert(lk.TABLE, [lk_cell(TECH, "10-49"), lk_cell(LEGAL, "10-49")])
    assert lk.fit_model(ctx.store).prior == {}
    assert lk.fit_model(ctx.store)._industry(LEGAL, LEGAL)[0] == 1.0  # 10 of the strongest group's 10: as before
    segments.refresh(ctx)
    assert lk.fit_model(ctx.store).prior[LEGAL] == (9, 15)


# -- the segment value and its proposals -------------------------------------------------------------------------------


def outcome(i: int, group: str, positive: bool, *, days: int = 40) -> Outcome:
    at = NOW - timedelta(days=days)
    return Outcome(f"a{i}", f"k{i}", "m@x.com", f"s{i}", at, NOW, positive_in_window=positive,
                   replied_in_window=positive, stamps={"industry_group_at_enrol": group, "size_band_at_enrol": "20-49"})


def focus_world(n: int = 100, k: int = 3) -> list[Outcome]:
    """n matured Legal Teams (a Focus group) companies with k positive: the pooled Focus rate is k ÷ n (3%)."""
    return [outcome(i, LEGAL, i < k) for i in range(n)]


def cell(group: str, n: int, k: int) -> segments.Cell:
    return segments.Cell(segments.GROUP, group, n, k, (2 + k) / (100 + n), 0.4, 100.0, 1.0, 1.0)


def test_a_switch_off_is_proposed_only_when_the_cell_is_surely_below_half_the_focus_rate(ctx):
    """Against a Focus rate of 3% (3 of 100), half is 1.5%. Beta(2 + k, 98 + n − k) at 0.015: 4 of 300 →
    Beta(6, 394), 0.55 (the plan's ≈ 0.52), under 0.9: no row; 0 of 300 → Beta(2, 398), 0.98: a row, read at 300;
    250 matured: no row whatever the rate (read once per 300, never under it)."""
    found = focus_world()
    assert round(segments.off_probability(4, 300, 0.03), 2) == 0.55
    assert round(segments.off_probability(0, 300, 0.03), 2) == 0.98
    rows = segments.propose(ctx, [cell(BUILD, 300, 4), cell("Retail & E-commerce", 300, 0), cell("Healthcare", 250, 0)],
                            found)
    assert [(r["kind"], r["target"], r["id"]) for r in rows] == [
        (tab.GROUP_OFF, "Retail & E-commerce", "group_off:Retail & E-commerce:300")]
    assert rows[0]["evidence"] == "P(positive rate < half the Focus rate 3.0%) = 0.98 on 0 of 300 matured"
    # Read again at 599 matured: the same read (id ...:300); at 600 a new one.
    assert segments.propose(ctx, [cell("Retail & E-commerce", 599, 0)], found)[0]["id"].endswith(":300")
    assert segments.propose(ctx, [cell("Retail & E-commerce", 600, 0)], found)[0]["id"].endswith(":600")


def test_value_samples_the_win_rate_and_gives_focus_shares_with_a_floor(ctx):
    """With the prior stored and no Columbus company matured, each group's rate is Beta(2, 98)'s mean, 0.02, and its
    p_win the prior's shrunk rate. The Focus groups' Thompson shares sum to 1 and none is under 10%. No group has 300
    matured, so propose writes nothing."""
    segments.refresh(ctx)
    cells = {c.key: c for c in segments.value(ctx, [], draws=500) if c.kind == segments.GROUP}
    assert cells[LEGAL].rate == 0.02 and round(cells[LEGAL].p_win, 3) == 0.474
    shares = {k: c.share for k, c in cells.items() if c.share is not None}
    assert set(shares) == {LEGAL, TECH}  # the Focus groups with a prior (Marketing & Creative has no deal here)
    assert round(sum(shares.values()), 6) == 1 and min(shares.values()) >= 0.10
    assert segments.propose(ctx, list(cells.values()), []) == []
    assert segments.floor_shares({"a": 0.95, "b": 0.05}, 0.10) == {"b": 0.10, "a": 0.90}


def test_the_job_refreshes_then_proposes_and_writes_nothing_in_a_dry_run(ctx):
    out = segments.run(ctx)
    assert out["stored"] and out["proposals"] == 0 and out["suggestions"] == {"rows": 0, "written": False}
    assert "Segment value: the HubSpot prior of 2026-11-01" in segments.readout_line(ctx, [])


def test_registered_on_the_schedule_heartbeat_and_cli():
    """W3-4 (the standing rule): a new job is registered in cli.JOBS, health.EXPECTED, IMPACT and the scheduler in
    the same commit. The 1st at 02:20, after settings_sync (02:00) and before lookalikes (02:30), whose fit reads it."""
    from us_outbound.base import say
    from us_outbound.ops import cli, health, schedule

    job = schedule.by_name()[segments.JOB]
    assert (job.cron, job.live, job.enabled) == ("20 2 1 * *", True, True)
    assert cli.JOBS[segments.JOB] == "us_outbound.learn.segments:run" and health.EXPECTED[segments.JOB] == 32 * 24 * 60
    assert segments.JOB in health.IMPACT and say.job_name(segments.JOB) == "the monthly segment prior from HubSpot"
    assert schedule.by_name()["lookalikes"].cron == "30 2 1 * *"
