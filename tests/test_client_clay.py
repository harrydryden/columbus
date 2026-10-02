"""Clay client: only the two US Outbound functions, run and poll, SPEC 8 output validation, CSV fallback."""

import copy
import json

import pytest

from tests.fakes import FakeTransport
from us_outbound.clients.clay import (
    Clay,
    ClayError,
    parse_accounts_output,
    parse_contacts_output,
    routine_id,
)
from us_outbound.clients.guard import Boundaries, Guard, GuardViolation

US_ACCOUNTS, US_CONTACTS = "t_usAccounts01", "t_usContacts01"
WORK_EMAIL = "t_0tk0v4lhJ895hhhhTHJ"  # existing workspace function: only while clay_email_fallback is yes
BASE = "https://api.clay.com/public/v0"

# The SPEC 8 example, with one value picked for each enum.
SPEC_ACCOUNT = {
    "clean_name": "Acme Creative",
    "legal_name": "Acme Creative LLC",
    "domain_confirmed": "acmecreative.com",
    "hq_city": "Chicago", "hq_state": "IL",
    "employees": 64, "employees_source": "linkedin",
    "industry": "Advertising agencies",
    "founded_year": 2014,
    "pages": {"careers": "https://acmecreative.com/careers", "benefits": "https://acmecreative.com/benefits",
              "values": "https://acmecreative.com/values"},
    "read_status": "read",
    "benefits": [{"item": "mental health days", "quote": "Two mental health days a quarter.", "url": "https://acmecreative.com/benefits"}],
    "mental_health_provision": [{"type": "eap", "provider": "ComPsych", "quote": "Our EAP is run by ComPsych.",
                                 "url": "https://acmecreative.com/benefits"}],
    "culture_statements": [{"quote": "People first.", "url": "https://acmecreative.com/values"}],
    "values_page": True,
    "funding": {"stage": "Series A", "amount_usd": 8000000, "date": "2026-07-14", "source": "crunchbase"},
    "credits_used": 4.5,
}


def make(live=False, results=None):
    t = FakeTransport()
    t.route("POST", "/routines/", fn=lambda req: {"routine_run_id": "run-1", "status": "in_progress"})
    if results is not None:
        t.route("GET", "/routines/run/run-1/results", fn=results)
    guard = Guard(live=live, bounds=Boundaries(clay_function_ids=frozenset({US_ACCOUNTS, US_CONTACTS})))
    sleeps: list[float] = []
    clay = Clay(guard, t, "clay-key", poll_interval=2.0, timeout=10.0, sleep=sleeps.append)
    return clay, t, guard, sleeps


@pytest.mark.parametrize("fid", [WORK_EMAIL, "t_0tk0v4ehpQ6WaeuCoQf", f"function:{US_ACCOUNTS}", ""])
def test_refuses_any_function_but_the_two_us_outbound_ones(fid):
    clay, t, guard, _ = make()
    with pytest.raises(GuardViolation):
        clay.run_function(fid, {"domain": "acme.example"})
    assert t.requests == []
    assert [(c.action, c.sent) for c in guard.calls] == [("function.run", False)]


def test_run_polls_until_finished_and_returns_the_output():
    polls = iter([
        {"routine_run_id": "run-1", "status": "in_progress", "total": 1, "finished": 0, "results": []},
        {"routine_run_id": "run-1", "status": "complete", "total": 1, "finished": 1,
         "results": [{"id": "1", "status": "complete", "output": json.dumps(SPEC_ACCOUNT)}]},
    ])
    clay, t, guard, sleeps = make(results=lambda req: next(polls))
    out = clay.run_function(US_ACCOUNTS, {"domain": "acmecreative.com", "raw_name": "Acme\x00 Creative, LLC",
                                          "apollo_hq_state": "IL", "apollo_employees": 60, "apollo_industry": None})
    assert out == SPEC_ACCOUNT
    run, first, second = t.requests
    assert (run.method, run.url) == ("POST", f"{BASE}/routines/function:{US_ACCOUNTS}/run")
    assert run.json == {"items": [{"id": "1", "inputs": {"domain": "acmecreative.com", "raw_name": "Acme Creative, LLC",
                                                          "apollo_hq_state": "IL", "apollo_employees": 60}}]}
    assert run.headers["clay-api-key"] == "clay-key" and "Authorization" not in run.headers
    assert first.url == second.url == f"{BASE}/routines/run/run-1/results"
    assert sleeps == [2.0]
    assert [(c.action, c.target, c.sent) for c in guard.calls] == [
        ("function.run", US_ACCOUNTS, True), ("run.get", US_ACCOUNTS, True), ("run.get", US_ACCOUNTS, True)]
    assert not guard.live  # Clay verification runs in dry-run too


def test_batch_run_reports_items_separately():
    body = {"status": "complete", "results": [
        {"id": "a", "status": "complete", "output": {"email": "jane@acme.example", "status": "valid", "credits_used": 1.0}},
        {"id": "b", "status": "failed", "error": "provider timeout"},
        {"id": "c", "status": "complete", "output": "not json"},
    ]}
    clay, t, _, _ = make(results=lambda req: body)
    out = clay.run_function_batch(US_CONTACTS, {"a": {"full_name": "Jane Doe"}, "b": {"full_name": "Bo"},
                                                "c": {"full_name": "Cy"}, "d": {"full_name": "Di"}})
    assert out["a"] == {"email": "jane@acme.example", "status": "valid", "credits_used": 1.0}
    assert all(isinstance(out[k], ClayError) for k in "bcd")
    assert len(t.requests[0].json["items"]) == 4
    with pytest.raises(ValueError):
        clay.run_function_batch(US_CONTACTS, {str(i): {} for i in range(101)})


@pytest.mark.parametrize("results, message", [
    ({"status": "complete", "results": [{"id": "1", "status": "complete", "output": [1, 2]}]}, "JSON object"),
    ({"status": "complete", "results": [{"id": "1", "status": "failed", "error": "blocked"}]}, "failed"),
    ({"status": "processing_failed"}, "processing_failed"),
    ({"status": "in_progress", "total": 1, "finished": 0}, "did not finish"),
])
def test_run_errors(results, message):
    clay, _, _, _ = make(results=lambda req: results)
    with pytest.raises(ClayError, match=message):
        clay.run_function(US_ACCOUNTS, {"domain": "acme.example"})


def test_run_without_run_id_raises():
    clay, t, _, _ = make()
    t.route("POST", "/routines/", body={"ok": True})
    with pytest.raises(ClayError, match="no run id"):
        clay.run_function(US_CONTACTS, {"full_name": "Jane Doe"})


def test_results_follow_the_cursor():
    pages = {
        None: {"total": 2, "finished": 2, "cursor": "p2", "results": [{"id": "a", "output": {"x": 1}}]},
        "p2": {"total": 2, "finished": 2, "results": [{"id": "b", "output": {"x": 2}}]},
    }
    clay, t, _, _ = make(results=lambda req: pages[(req.params or {}).get("cursor")])
    out = clay.run_function_batch(US_ACCOUNTS, {"a": {"domain": "a.example"}, "b": {"domain": "b.example"}})
    assert out == {"a": {"x": 1}, "b": {"x": 2}}


def test_routine_id():
    assert routine_id("t_abc") == "function:t_abc"
    assert routine_id("function:t_abc") == "function:t_abc"


def test_parse_accounts_output_accepts_the_spec_example():
    out = parse_accounts_output(json.dumps(SPEC_ACCOUNT))
    assert out["read_status"] == "read" and out["employees"] == 64 and out["credits_used"] == 4.5
    assert out["mental_health_provision"][0] == {"type": "eap", "provider": "ComPsych",
                                                  "quote": "Our EAP is run by ComPsych.", "url": "https://acmecreative.com/benefits"}
    assert out["funding"]["date"] == "2026-07-14" and out["values_page"] is True
    assert out["pages"]["values"] == "https://acmecreative.com/values"


def test_parse_accounts_output_minimal_blocked_read():
    out = parse_accounts_output({"clean_name": "Beta", "domain_confirmed": "Beta.example", "read_status": "blocked",
                                 "funding": None, "credits_used": 0})
    assert out["read_status"] == "blocked" and out["benefits"] == [] and out["funding"] is None
    assert out["domain_confirmed"] == "beta.example" and out["values_page"] is None


def test_parse_accounts_output_clips_quotes_to_300():
    o = copy.deepcopy(SPEC_ACCOUNT)
    o["benefits"][0]["quote"] = "x" * 1000
    o["culture_statements"][0]["quote"] = "y" * 300
    out = parse_accounts_output(o)
    assert len(out["benefits"][0]["quote"]) == 300 and out["benefits"][0]["quote"].endswith("…")
    assert out["culture_statements"][0]["quote"] == "y" * 300


@pytest.mark.parametrize("change", [
    {"read_status": "read | no_pages_found | blocked | error"},
    {"read_status": "partial"},
    {"mental_health_provision": [{"type": "wellness_app", "quote": "q", "url": "u"}]},
    {"funding": {"stage": "Seed", "date": "July 2026"}},
    {"funding": "Series A"},
    {"employees": "about sixty"},
    {"employees": -3},
    {"values_page": "yes"},
    {"credits_used": "4.5"},
    {"benefits": "mental health days"},
    {"clean_name": None, "__drop__": "clean_name"},
])
def test_parse_accounts_output_rejects(change):
    o = copy.deepcopy(SPEC_ACCOUNT)
    drop = change.pop("__drop__", None)
    o.update(change)
    if drop:
        del o[drop]
    with pytest.raises(ClayError):
        parse_accounts_output(o)


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", 42, None])
def test_parse_outputs_reject_non_objects(bad):
    with pytest.raises(ClayError):
        parse_accounts_output(bad)
    with pytest.raises(ClayError):
        parse_contacts_output(bad)


def test_parse_contacts_output():
    out = parse_contacts_output({"email": " Jane@Acme.example ", "status": "catch_all_valid", "provider": "prospeo", "credits_used": 1.0})
    assert out == {"email": "jane@acme.example", "status": "catch_all_valid", "provider": "prospeo", "credits_used": 1.0}
    assert parse_contacts_output('{"email": "", "status": "not_found", "provider": null, "credits_used": 0.5}')["email"] is None
    for bad in (
        {"email": "jane@acme.example", "status": "risky", "credits_used": 1},
        {"email": "", "status": "valid", "credits_used": 1},
        {"email": "jane@acme.example", "status": "valid"},
    ):
        with pytest.raises(ClayError):
            parse_contacts_output(bad)


def test_csv_fallback_round_trip(tmp_path):
    path = tmp_path / "import.csv"
    rows = [
        {"domain": "acme.example", "raw_name": "Acme Café, LLC", "apollo_employees": 60},
        {"domain": "beta.example", "apollo_hq_state": "NY", "pages": {"careers": "https://beta.example/jobs"}, "tags": ["a", "b"]},
    ]
    Clay.write_import_csv(rows, path)
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "domain,raw_name,apollo_employees,apollo_hq_state,pages,tags"
    assert "Acme Café, LLC" in text
    back = Clay.read_export_csv(path)
    assert back[0] == {"domain": "acme.example", "raw_name": "Acme Café, LLC", "apollo_employees": "60",
                       "apollo_hq_state": "", "pages": "", "tags": ""}
    assert back[1]["pages"] == {"careers": "https://beta.example/jobs"} and back[1]["tags"] == ["a", "b"]


def test_read_export_csv_skips_blank_rows_and_bom(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("﻿email,status\njane@acme.example,valid\n,\n", encoding="utf-8")
    assert Clay.read_export_csv(path) == [{"email": "jane@acme.example", "status": "valid"}]
