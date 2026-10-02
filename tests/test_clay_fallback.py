"""Clay narrowed to the email waterfall (contacts/pick.py; Harry, 2 Oct 2026).

When Apollo has no verified email for the chosen person, pick_contacts asks Clay once, behind
General clay_email_fallback (default no): Work Email as it is, or the "US Outbound – Contacts"
function once its id is set. Only a valid result is used, within the Clay budget, never while a
kill rule pauses the clay source, and the guard allows Work Email only while the key is yes.
Apollo and Clay answer on a FakeTransport.
"""

from __future__ import annotations

import dataclasses

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_pick_contacts import ORG, TEAM, FakeApollo, account, everyone_verified, facts, match
from us_outbound.clients.clay import WORK_EMAIL_FUNCTION_ID, ClayError, parse_work_email_output
from us_outbound.clients.guard import GuardViolation
from us_outbound.contacts import pick
from us_outbound.context import boundaries_for
from us_outbound.learn import holds
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

CLAY = "https://api.clay.com/public/v0"


def world(default_settings, *, output=None, clay_status=200, matches=None, **general):
    g = dataclasses.replace(default_settings.general, **{"clay_contacts_function_id": "", **general})
    settings = dataclasses.replace(default_settings, general=g)
    t = FakeTransport()
    ctx = make_context(settings, transport=t)
    ctx.store.insert("accounts", [account()])
    if matches is None:
        matches = everyone_verified()
        matches["p-ceo"] = match("p-ceo", "ceo@brightfin.com", status="unverified", linkedin_url="https://linkedin.com/in/pl")
    apollo = FakeApollo(t, {ORG: TEAM}, matches)
    t.route("POST", "/routines/", status=clay_status, fn=lambda req: {"routine_run_id": "run-1"})
    t.route("GET", "/routines/run/run-1/results", {"status": "complete", "results": [
        {"id": "1", "status": "complete", "output": output if output is not None else
         {"email": "Pat.Lee@brightfin.com", "status": "valid", "credits_used": 1.5}}]})
    return ctx, t, apollo


def clay_runs(t):
    return [r for r in t.requests if r.url.startswith(CLAY) and r.method == "POST"]


def test_off_by_default_apollo_misses_go_to_the_next_candidate_and_clay_is_never_called(default_settings):
    settings, _ = validate_all(default_tabs())
    assert settings.general.clay_email_fallback is False  # the General default
    ctx, t, apollo = world(default_settings)
    out = pick.run(ctx)
    assert clay_runs(t) == [] and out["clay"]["off"] == "clay_email_fallback is no"
    assert apollo.revealed == ["p-ceo", "p-hop"] and ctx.store.select("contacts")[0]["email_source"] == "apollo"
    assert WORK_EMAIL_FUNCTION_ID not in boundaries_for(ctx.settings).clay_function_ids
    with pytest.raises(GuardViolation):  # and the guard refuses Work Email while the key is no
        ctx.clients.clay.run_function(WORK_EMAIL_FUNCTION_ID, {"full_name": "Pat Lee"})


def test_on_apollo_s_catch_all_goes_to_clay_s_work_email_and_a_valid_result_is_kept(default_settings):
    ctx, t, apollo = world(default_settings, clay_email_fallback=True)
    assert WORK_EMAIL_FUNCTION_ID in boundaries_for(ctx.settings).clay_function_ids
    out = pick.run(ctx)
    assert apollo.revealed == ["p-ceo"]  # Clay found the founder: no second reveal
    [run] = clay_runs(t)
    assert run.url == f"{CLAY}/routines/function:{WORK_EMAIL_FUNCTION_ID}/run"
    assert run.json["items"][0]["inputs"] == {"full_name": "Pat Lee", "domain": "brightfin.com",
                                              "linkedin_url": "https://linkedin.com/in/pl", "title": "Co-Founder & CEO"}
    [c] = ctx.store.select("contacts")
    assert (c["email"], c["email_status"], c["email_source"], c["role"], c["person_state"]) == (
        "pat.lee@brightfin.com", "valid", "clay", "Founder or executive", "NY")
    ledger = {r["system"]: r for r in ctx.store.select("credit_ledger")}
    assert (ledger["clay"]["credits"], ledger["clay"]["job"], ledger["clay"]["note"]) == (1.5, "pick_contacts",
                                                                                         "email waterfall (Clay)")
    assert ledger["apollo"]["credits"] == 1.0
    [lookup] = facts(ctx, pick.CLAY_FACT)
    assert {k: lookup["value"][k] for k in ("apollo_person_id", "status", "kept", "reason", "credits")} == {
        "apollo_person_id": "p-ceo", "status": "valid", "kept": True, "reason": None, "credits": 1.5}
    [outcome] = facts(ctx, pick.OUTCOME_FACT)
    assert outcome["value"]["email_source"] == "clay" and outcome["value"]["clay"] is True
    assert {k: out["clay"][k] for k in ("off", "lookups", "found", "credits")} == {
        "off": None, "lookups": 1, "found": 1, "credits": 1.5}
    assert "Clay:" in out["clay"]["budget"]


@pytest.mark.parametrize("output, reason", [
    ({"email": "pat@brightfin.com", "status": "catch_all", "credits_used": 1.0}, "Clay email status catch_all_valid"),
    ({"email": "pat@brightfin.com"}, "Clay email status unverified"),  # an address with no validation is never used
    ({"status": "not_found"}, "Clay email status not_found"),
])
def test_only_a_valid_clay_result_is_used(default_settings, output, reason):
    ctx, t, apollo = world(default_settings, output=output, clay_email_fallback=True)
    out = pick.run(ctx)
    assert len(clay_runs(t)) == 1  # one lookup an account, though Apollo is asked again
    assert apollo.revealed == ["p-ceo", "p-hop"]
    [c] = ctx.store.select("contacts")
    assert c["email_source"] == "apollo" and c["role"] == "People leader"
    [lookup] = facts(ctx, pick.CLAY_FACT)
    assert (lookup["value"]["kept"], lookup["value"]["reason"]) == (False, reason)
    assert out["clay"]["found"] == 0


def test_not_found_costs_nothing_and_a_clay_email_must_pass_the_same_checks(default_settings):
    ctx, _, _ = world(default_settings, output={"status": "not_found"}, clay_email_fallback=True)
    pick.run(ctx)
    assert [r["credits"] for r in ctx.store.select("credit_ledger", {"system": "clay"})] == [0.0]
    ctx, _, _ = world(default_settings, output={"email": "pat@gmail.com", "status": "valid", "credits_used": 1.0},
                      clay_email_fallback=True)
    pick.run(ctx)
    [lookup] = facts(ctx, pick.CLAY_FACT)
    assert lookup["value"]["kept"] is False and lookup["value"]["reason"] == "personal email domain"


def test_over_the_clay_budget_or_paused_by_a_kill_rule_clay_is_not_called(default_settings):
    ctx, t, _ = world(default_settings, clay_email_fallback=True, clay_monthly_credits=1.0)  # under one reserve
    out = pick.run(ctx)
    assert clay_runs(t) == [] and out["clay"]["lookups"] == 0
    assert ctx.store.select("contacts")[0]["email_source"] == "apollo"
    ctx, t, _ = world(default_settings, clay_email_fallback=True, clay_monthly_credits=0.0)
    assert pick.run(ctx)["clay"]["off"] == "no monthly Clay budget (clay_monthly_credits is 0)" and clay_runs(t) == []
    ctx, t, _ = world(default_settings, clay_email_fallback=True)
    ctx.store.insert("hitl_items", [{"item_id": "k1", "kind": holds.KIND, "status": "open",
                                     "payload": {"action": holds.PAUSE_SOURCE, "target": "clay", "reason": "4% bounced"}}])
    out = pick.run(ctx)
    assert clay_runs(t) == [] and out["clay"]["off"] == "a kill rule pauses the clay email source (4% bounced)"


def test_the_run_stops_asking_clay_at_its_cap(default_settings, monkeypatch):
    monkeypatch.setattr(pick, "CLAY_LOOKUPS_PER_RUN", 0)
    ctx, t, _ = world(default_settings, clay_email_fallback=True)
    pick.run(ctx)
    assert clay_runs(t) == []


def test_a_failed_clay_call_counts_its_reserve(default_settings):
    ctx, t, _ = world(default_settings, clay_email_fallback=True, clay_status=500)
    pick.run(ctx)
    [row] = ctx.store.select("credit_ledger", {"system": "clay"})
    assert row["credits"] == pick.CLAY_RESERVE and "counted in case Clay charged it" in row["note"]
    [lookup] = facts(ctx, pick.CLAY_FACT)
    assert lookup["value"]["reason"] == "Clay lookup failed (ApiError)"
    assert ctx.store.select("contacts")[0]["email_source"] == "apollo"


def test_the_us_outbound_contacts_function_is_used_once_its_id_is_set(default_settings):
    output = {"email": "pat.lee@brightfin.com", "status": "valid", "provider": "Prospeo", "credits_used": 1.0}
    ctx, t, _ = world(default_settings, output=output, clay_email_fallback=True, clay_contacts_function_id="fn-us-contacts")
    pick.run(ctx)
    [run] = clay_runs(t)
    assert run.url.endswith("/routines/function:fn-us-contacts/run")
    assert ctx.store.select("contacts")[0]["email_source"] == "clay"


def test_nobody_to_look_up_without_a_full_name_or_linkedin(default_settings):
    matches = everyone_verified()
    del matches["p-ceo"]  # Apollo has no match: the search row has only an obfuscated last name
    ctx, t, _ = world(default_settings, matches=matches, clay_email_fallback=True)
    pick.run(ctx)
    assert clay_runs(t) == []
    [lookup] = facts(ctx, pick.CLAY_FACT)
    assert lookup["value"]["reason"] == "no full name or LinkedIn URL to look up in Clay"


@pytest.mark.parametrize("raw, want", [
    ({"email": "A@Acme.com", "status": "valid", "credits_used": 1.0, "provider": "x"},
     {"email": "a@acme.com", "status": "valid", "provider": "x", "credits_used": 1.0}),  # the SPEC 8 shape
    ({"work_email": "a@acme.com", "validation_status": "Deliverable"},
     {"email": "a@acme.com", "status": "valid", "provider": None, "credits_used": None}),
    ({"Work Email": "a@acme.com", "email_status": "accept_all", "credits_used": 2, "source": "Hunter"},
     {"email": "a@acme.com", "status": "catch_all_valid", "provider": "Hunter", "credits_used": 2.0}),
    ({"email": "a@acme.com"}, {"email": "a@acme.com", "status": "unverified", "provider": None, "credits_used": None}),
    ({}, {"email": None, "status": "not_found", "provider": None, "credits_used": None}),
    ('{"email": "a@acme.com", "status": "undeliverable"}',
     {"email": "a@acme.com", "status": "invalid", "provider": None, "credits_used": None}),
])
def test_parse_work_email_output(raw, want):
    assert parse_work_email_output(raw) == want


def test_parse_work_email_output_rejects_a_malformed_address():
    with pytest.raises(ClayError):
        parse_work_email_output({"email": "not-an-email", "status": "valid"})
