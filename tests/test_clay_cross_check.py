"""Clay's cross-check of doubtful HQ state and size (us_outbound/clay_cross_check.py, verify.cross_check; Harry,
6 Oct 2026). Behind General clay_cross_check (default no): verify_accounts asks the "US Outbound – Accounts"
function about its doubtful accounts once, in one batch, fills missing facts, drops the doubts Clay settles and
restates those it disagrees with for the hand-check. Clay and HubSpot answer on a FakeTransport.
"""

from __future__ import annotations

import copy

import pytest

from tests.test_verify import account, make, status
from tests.test_sources_apollo import settings_with
from us_outbound import clay_cross_check as cc, verify
from us_outbound.base import holds
from us_outbound.clients.clay import ClayError, parse_cross_check_output
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import General, Override
from us_outbound.settings.validate import validate_all

FN = "t_us_accounts"
CLAY = "https://api.clay.com/public/v0"
NEAR = "Apollo's estimate of 49 staff is within 2 of the 50-staff edge"


def on(**kw):
    return settings_with(clay_cross_check=True, clay_accounts_function_id=FN, **kw)


def clay(t, answers: dict, *, status=200, run_status="complete"):
    """Clay's batch run: answers maps an account id to its output (or to an item error)."""
    def results(_req):
        rows = []
        for aid, out in answers.items():
            if isinstance(out, Exception):
                rows.append({"id": aid, "status": "failed", "error": str(out)})
            else:
                rows.append({"id": aid, "status": "complete", "output": out})
        return {"status": run_status, "results": rows}

    t.route("POST", "/routines/", status=status, body={"routine_run_id": "run-x"})
    t.route("GET", "/routines/run/run-x/results", fn=results)
    return t


def runs(t):
    return [r for r in t.requests if r.url.startswith(CLAY) and r.method == "POST"]


def answer(hq_state="NY", employees=45, source="LinkedIn company page: 45 employees; HQ New York", **kw):
    return {"hq_state": hq_state, "employees": employees, "source": source, **kw}


def clay_facts(ctx):
    return ctx.store.select("signal_events", {"source": "clay", "fact": cc.FACT})


# -- the switch -------------------------------------------------------------------------------------------


def test_off_by_default_clay_is_never_asked_and_every_doubt_goes_to_the_hand_check():
    assert General().clay_cross_check is False
    [row] = [r for r in default_tabs()["General"] if r["key"] == "clay_cross_check"]
    assert row["value"] == "no" and "Harry, 6 Oct 2026" in row["note"] and "docs/pipeline.md" in row["note"]
    ctx, t = make([account(employees=49, size_band="20-49")],
                  settings=settings_with(clay_accounts_function_id=FN))
    clay(t, {"a1": answer()})
    out = verify.run(ctx)
    assert out["clay_cross_check"] == {"off": "clay_cross_check is no"} and runs(t) == []
    assert out["to_hand_check"] == 1 and clay_facts(ctx) == []


def test_the_switch_needs_the_function_id():
    tabs = copy.deepcopy(default_tabs())
    general = {r["key"]: r for r in tabs["General"]}
    general["clay_cross_check"]["value"] = "yes"
    [e] = validate_all(tabs)[1]["General"]
    assert e.label == "clay_cross_check" and "clay_accounts_function_id is blank" in e.message
    general["clay_accounts_function_id"]["value"] = FN
    s, errors = validate_all(tabs)
    assert not errors["General"] and s.general.clay_cross_check is True
    del tabs["General"][tabs["General"].index(general["clay_cross_check"])]
    assert validate_all(tabs)[0].general.clay_cross_check is False  # a sheet without the row reads as no


# -- settle and restate ------------------------------------------------------------------------------------


def test_a_size_doubt_clay_settles_is_dropped_and_the_account_verifies_the_same_day():
    ctx, t = make([account(employees=49, size_band="20-49"), account("a2")], settings=on())
    clay(t, {"a1": answer(employees=45, credits_used=1.5)})
    out = verify.run(ctx)
    assert status(ctx) == "verified" and out["to_hand_check"] == 0
    [post] = runs(t)
    assert post.url == f"{CLAY}/routines/function:{FN}/run"
    assert post.json == {"items": [{"id": "a1", "inputs": {"domain": "a1co.com"}}]}  # a2 has no doubt: not asked
    [fact] = clay_facts(ctx)
    assert fact["quote"] == "LinkedIn company page: 45 employees; HQ New York" and fact["account_id"] == "a1"
    assert {k: fact["value"][k] for k in ("hq_state", "employees", "credits", "apollo", "doubts")} == {
        "hq_state": "NY", "employees": 45, "credits": 1.5,
        "apollo": {"hq_state": "NY", "employees": 49, "size_band": "20-49"}, "doubts": [NEAR]}
    [entry] = ctx.store.select("credit_ledger", {"system": "clay"})
    assert (entry["credits"], entry["job"], entry["account_id"], entry["note"]) == (
        1.5, verify.JOB, "a1", cc.LEDGER_NOTE)
    report = out["clay_cross_check"]
    assert {k: report[k] for k in ("off", "doubtful", "asked", "answered", "failed", "credits", "errors")} == {
        "off": None, "doubtful": 1, "asked": 1, "answered": 1, "failed": 0, "credits": 1.5, "errors": []}
    assert report["budget"].startswith("Clay: 1.5 of 2,000 credits used this month")


@pytest.mark.parametrize("apollo, clay_n, reasons", [
    ({"employees": 49, "size_band": "20-49"}, 62, ["Clay says 62 staff, Apollo says 49"]),
    ({"employees": 51}, 48, ["Clay says 48 staff, Apollo says 51"]),
    ({"employees": 49, "size_band": "20-49"}, 49, []),  # the same count, on the same side: settled
    ({"employees": 64, "size_band": "20-49"}, 62, []),  # count and band disagree; Clay sides with the count
    ({"employees": 64, "size_band": "20-49"}, 30, ["Clay says 30 staff, Apollo says 64"]),
    ({"employees": 248, "size_band": "100-249"}, 260, ["Clay says 260 staff, Apollo says 248"]),
    ({"employees": 11, "size_band": "10-19"}, 14, []),
])
def test_where_clay_disagrees_on_size_the_doubt_stands_with_both_counts(apollo, clay_n, reasons):
    ctx, t = make([account(**apollo)], settings=on())
    clay(t, {"a1": answer(employees=clay_n)})
    out = verify.run(ctx)
    assert out["to_hand_check_accounts"] == ([{"account_id": "a1", "domain": "a1co.com", "reasons": reasons}]
                                             if reasons else [])
    assert status(ctx) == ("new" if reasons else "verified")
    if reasons:
        [doubt] = ctx.store.select("signal_events", {"source": verify.DOUBT_SOURCE, "fact": verify.DOUBT_FACT})
        assert doubt["value"]["reasons"] == reasons  # what the hand-check shows Harry


def test_approving_the_hand_check_clears_clay_s_reason_so_the_next_run_verifies():
    ctx, t = make([account(employees=49, size_band="20-49")], settings=on())
    clay(t, {"a1": answer(employees=62)})
    verify.run(ctx)
    [held] = verify.open_doubts(ctx)
    assert held["reasons"] == ["Clay says 62 staff, Apollo says 49"]
    verify.clear_doubts(ctx, [held], "harry", "2026-W41")
    assert verify.run(ctx)["verified"] == 1 and status(ctx) == "verified" and len(runs(t)) == 1


def test_a_missing_hq_state_is_filled_from_clay_so_the_account_verifies_today():
    ctx, t = make([account(hq_state=None)], settings=on())
    clay(t, {"a1": answer(hq_state="Illinois")})  # a state name reads as its code
    out = verify.run(ctx)
    assert status(ctx) == "verified" and out["clay_cross_check"]["filled"] == {"hq_state": 1, "size": 0}
    assert ctx.store.get("accounts", account_id="a1")["hq_state"] == "IL"


def test_a_missing_size_is_filled_and_a_clay_count_near_an_edge_says_apollo_gives_none():
    ctx, t = make([account(employees=None, size_band=None), account("a2", employees=None, size_band=None)],
                  settings=on())
    clay(t, {"a1": answer(employees=49), "a2": answer(employees=120)})
    out = verify.run(ctx)
    assert out["to_hand_check_accounts"] == [
        {"account_id": "a1", "domain": "a1co.com", "reasons": ["Clay says 49 staff, Apollo gives none"]}]
    a1, a2 = (ctx.store.get("accounts", account_id=a) for a in ("a1", "a2"))
    assert (a1["employees"], a1["size_band"], a2["employees"], a2["size_band"]) == (49, "20-49", 120, "100-249")
    assert status(ctx, "a2") == "verified" and out["clay_cross_check"]["filled"] == {"hq_state": 0, "size": 2}


def test_a_missing_fact_clay_cannot_supply_stays_word_for_word_and_industry_is_not_clay_s():
    ctx, t = make([account(hq_state=None, employees=None, size_band=None, industry=None, industry_group=None)],
                  settings=on())
    clay(t, {"a1": answer(hq_state="Ontario", employees=None)})  # not a US state, no count
    out = verify.run(ctx)
    [held] = out["to_hand_check_accounts"]
    assert held["reasons"] == [verify.NO_STATE, verify.NO_INDUSTRY, verify.NO_SIZE]
    [fact] = clay_facts(ctx)
    assert (fact["value"]["hq_state"], fact["value"]["hq_state_given"]) == (None, "Ontario")
    ctx, t = make([account(industry=None, industry_group=None)], settings=on())
    clay(t, {"a1": answer()})
    assert verify.run(ctx)["clay_cross_check"]["doubtful"] == 0 and runs(t) == []  # an industry doubt alone


def test_clay_s_state_is_read_against_apollo_s_when_it_was_asked_anyway():
    ctx, t = make([account(employees=49, size_band="20-49")], settings=on())
    clay(t, {"a1": answer(hq_state="CA", employees=47)})
    out = verify.run(ctx)
    assert out["to_hand_check_accounts"][0]["reasons"] == ["Clay says CA, Apollo says NY"]  # the size doubt settled


# -- once per account, and errors ------------------------------------------------------------------------------


def test_an_account_is_asked_once_and_its_answer_is_used_again():
    ctx, t = make([account(employees=49, size_band="20-49")], settings=on())
    clay(t, {"a1": answer(employees=62)})
    verify.run(ctx)
    again = verify.run(ctx)
    assert len(runs(t)) == 1 and again["clay_cross_check"]["asked_before"] == 1
    assert again["to_hand_check_accounts"][0]["reasons"] == ["Clay says 62 staff, Apollo says 49"]
    assert len(ctx.store.select("signal_events", {"source": verify.DOUBT_SOURCE})) == 1  # the same doubt, once


def test_a_clay_error_is_reported_the_doubt_stands_and_the_account_is_asked_again_next_run():
    ctx, t = make([account(employees=49, size_band="20-49"), account("a2", hq_state=None)], settings=on())
    clay(t, {"a1": ClayError("processing_failed"), "a2": {"hq_state": "IL"}})  # a2's output lacks two keys
    out = verify.run(ctx)
    report = out["clay_cross_check"]
    assert (report["asked"], report["answered"], report["failed"], report["credits"]) == (2, 0, 2, 2 * cc.RESERVE)
    assert report["errors"][0].startswith("a1co.com: Clay item a1 failed")
    assert report["errors"][1] == "a2co.com: employees, source are missing"
    assert out["to_hand_check"] == 2 and clay_facts(ctx) == []
    assert {r["note"] for r in ctx.store.select("credit_ledger")} == {
        f"{cc.LEDGER_NOTE} failed; counted in case Clay charged it"}
    clay(t, {"a1": answer(employees=45), "a2": answer(hq_state="IL")})
    again = verify.run(ctx)
    assert again["clay_cross_check"]["answered"] == 2 and status(ctx, "a1") == status(ctx, "a2") == "verified"


def test_a_failed_run_is_one_error_and_every_doubt_stands():
    ctx, t = make([account(employees=49, size_band="20-49")], settings=on())
    clay(t, {"a1": answer()}, status=403)
    out = verify.run(ctx)
    assert out["clay_cross_check"]["errors"] == [
        f"the Clay run failed (AuthError): clay HTTP 403 for /public/v0/routines/function:{FN}/run: "
        "{'routine_run_id': 'run-x'}"]
    assert out["to_hand_check_accounts"][0]["reasons"] == [NEAR]


# -- what is asked, and when not -------------------------------------------------------------------------------


def test_overrides_are_harry_s_word_clay_neither_fills_nor_argues():
    s = on(overrides=(Override("a1co.com", "employees", "49"),))
    ctx, t = make([account(employees=None, size_band=None)], settings=s)
    clay(t, {"a1": answer(employees=62)})
    out = verify.run(ctx)
    assert runs(t) == [] and out["clay_cross_check"]["doubtful"] == 0
    assert out["to_hand_check_accounts"][0]["reasons"] == [NEAR]
    assert ctx.store.get("accounts", account_id="a1")["employees"] is None


def test_accounts_that_fail_on_something_else_or_whose_doubts_were_cleared_are_not_asked():
    ctx, t = make([account(hq_state="CA", employees=49), account("a2", employees=49, size_band="20-49")],
                  settings=on())
    verify.clear_doubts(ctx, [{"account_id": "a2", "reasons": [NEAR]}], "harry", "2026-W41")
    clay(t, {})
    out = verify.run(ctx)
    assert runs(t) == [] and out["clay_cross_check"]["doubtful"] == 0 and status(ctx, "a2") == "verified"


@pytest.mark.parametrize("kw, why", [
    ({"clay_monthly_credits": 2.0}, "today's share of the month's Clay budget is used"),
    ({"clay_monthly_credits": 0.0}, "no monthly Clay budget (clay_monthly_credits is 0)"),
    ({"clay_accounts_function_id": ""}, cc.NO_FUNCTION),
])
def test_without_budget_or_a_function_clay_is_not_asked(kw, why):
    settings = settings_with(**{"clay_cross_check": True, "clay_accounts_function_id": FN, **kw})
    ctx, t = make([account(employees=49, size_band="20-49")], settings=settings)
    clay(t, {"a1": answer()})
    out = verify.run(ctx)
    assert runs(t) == [] and out["clay_cross_check"]["not_asked"] == why
    assert out["clay_cross_check"]["left_for_next_run"] == 1 and out["to_hand_check"] == 1


def test_a_kill_rule_pausing_the_clay_source_stops_the_asking():
    ctx, t = make([account(employees=49, size_band="20-49")], settings=on())
    ctx.store.insert("hitl_items", [{"item_id": "k1", "kind": holds.KIND, "status": "open",
                                     "payload": {"action": holds.PAUSE_SOURCE, "target": "clay", "reason": "4% bounced"}}])
    clay(t, {"a1": answer()})
    out = verify.run(ctx)
    assert runs(t) == [] and out["clay_cross_check"]["not_asked"] == "a kill rule pauses the clay source (4% bounced)"


def test_one_batch_of_at_most_run_items_max_within_the_budget(monkeypatch):
    accounts = [account(f"a{i}", employees=49, size_band="20-49") for i in range(1, 5)]
    monkeypatch.setattr(cc, "RUN_ITEMS_MAX", 2)
    ctx, t = make(copy.deepcopy(accounts), settings=on())
    clay(t, {f"a{i}": answer() for i in range(1, 5)})
    out = verify.run(ctx)
    [post] = runs(t)
    assert [i["id"] for i in post.json["items"]] == ["a1", "a2"] and out["clay_cross_check"]["left_for_next_run"] == 2
    monkeypatch.setattr(cc, "RUN_ITEMS_MAX", 100)
    # Monday 5 Oct: 20 weekdays left in October, so today may spend a twentieth: three reserves.
    ctx, t = make(copy.deepcopy(accounts), settings=on(clay_monthly_credits=cc.RESERVE * 3 * 20))
    clay(t, {f"a{i}": answer() for i in range(1, 5)})
    out = verify.run(ctx)
    [post] = runs(t)
    assert len(post.json["items"]) == 3 and out["clay_cross_check"]["left_for_next_run"] == 1


# -- the pieces --------------------------------------------------------------------------------------------------


def test_doubts_are_told_apart_by_swapping_in_a_known_value_not_by_their_wording():
    s = on()
    acct = account(hq_state=None, employees=64, size_band="20-49", industry=None)
    found = verify.doubts(acct, s)
    state, size = cc.kinds(acct, found, s)
    assert state == [verify.NO_STATE] and size == ["the employee count (64) and the size band (20-49) disagree"]
    assert verify.NO_INDUSTRY in found and verify.NO_INDUSTRY not in state + size


@pytest.mark.parametrize("a, b, same", [(49, 45, True), (49, 50, False), (9, 10, False), (249, 250, False),
                                         (60, 240, True), (300, 260, True)])
def test_same_side_of_every_size_edge(a, b, same):
    assert cc.same_side(a, b, cc.size_edges(on())) is same


@pytest.mark.parametrize("raw, want", [
    ({"hq_state": "il", "employees": 62, "source": "LinkedIn"},
     {"hq_state": "IL", "hq_state_given": "il", "employees": 62, "source": "LinkedIn", "credits_used": None}),
    ('{"hq_state": null, "employees": "1,200", "source": null, "credits_used": 2}',
     {"hq_state": None, "hq_state_given": None, "employees": 1200, "source": "", "credits_used": 2.0}),
    ({"hq_state": "Ontario", "employees": 0, "source": "Claygent"},
     {"hq_state": None, "hq_state_given": "Ontario", "employees": None, "source": "Claygent", "credits_used": None}),
])
def test_parse_cross_check_output(raw, want):
    assert parse_cross_check_output(raw) == want


@pytest.mark.parametrize("raw", [
    {"hq_state": "NY", "employees": 40},  # source is missing
    {"hq_state": "NY", "employees": "51-200", "source": "x"},  # a range, not a count
    {"hq_state": 12, "employees": 40, "source": "x"},
    {"hq_state": "NY", "employees": 40, "source": "x", "credits_used": -1},
    "not json",
])
def test_parse_cross_check_output_refuses_what_is_not_the_spec(raw):
    with pytest.raises(ClayError):
        parse_cross_check_output(raw)


def test_dry_run_asks_clay_too_and_writes_only_the_database():
    ctx, t = make([account(employees=49, size_band="20-49")], settings=on())
    clay(t, {"a1": answer()})
    verify.run(ctx)
    assert ctx.dry_run and len(runs(t)) == 1
    assert {c.system for c in ctx.guard.writes()} == {"db"}
