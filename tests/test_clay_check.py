"""`us-outbound clay check-email` (ops/clay_check.py): the one Work Email lookup that confirms Clay's email
fallback before clay_email_fallback goes on. Dry-run calls nothing; --live makes exactly one call through the
guarded client, for a Spill colleague's own name, and says what to do next. Clay answers on a FakeTransport.
"""

from __future__ import annotations

import dataclasses

import pytest

from tests.fakes import FakeTransport, make_context
from us_outbound.clients.clay import CHECK_EMAIL_JOB, WORK_EMAIL_FUNCTION_ID
from us_outbound.logs import hash_email
from us_outbound.ops import bootstrap, cli
from us_outbound.ops.clay_check import SWITCH_ON

CLAY = "https://api.clay.com/public/v0"
FOUND = {"Work Email": "harry@spill.chat", "validation_status": "valid", "credits_used": 1.0,
         "provider": "Prospeo"}


class World:
    def __init__(self, settings, *, output=None, row=None, post_status=200, run_status="complete"):
        self.settings = dataclasses.replace(settings, general=dataclasses.replace(settings.general,
                                                                                    clay_email_fallback=False))
        self.t = FakeTransport()
        self.t.route("POST", "/routines/", status=post_status, body={"routine_run_id": "run-9", "status": "queued"})
        result = row if row is not None else {"id": "1", "status": "complete",
                                              "output": FOUND if output is None else output}
        self.t.route("GET", "/routines/run/run-9/results", {"status": run_status, "results": [result]})
        self.contexts = []

    def __call__(self, job, live_flag, operator=False):
        ctx = make_context(self.settings, job=job, transport=self.t,
                           store=self.contexts[0].store if self.contexts else None)
        ctx.guard.configure(live=bootstrap.resolve_live(live_flag, self.settings, operator=operator))
        ctx.live_flag = live_flag
        self.contexts.append(ctx)
        return ctx

    def run(self, *extra):
        return cli.main(["clay", "check-email", "--first", "Harry", "--last", "Dryden", "--domain", "spill.chat",
                         *extra], context_factory=self)

    def clay_requests(self):
        return [r for r in self.t.requests if r.url.startswith(CLAY)]


def printed(out: str) -> list[str]:
    return [line for line in out.splitlines() if line and not line.startswith("{")]


def test_without_live_it_says_what_it_would_send_and_calls_nothing(default_settings, capsys):
    w = World(default_settings)
    assert w.run() == 0
    lines = printed(capsys.readouterr().out)
    assert lines[0].startswith("Dry-run: one Work Email lookup through Clay's Routines API would be made")
    assert f"  POST {CLAY}/routines/function:{WORK_EMAIL_FUNCTION_ID}/run" in lines
    assert "  inputs: Full Name = 'Harry Dryden', Company Domain = 'spill.chat'" in lines
    assert lines[-1] == "Use your own name, never a prospect's. Nothing was called. Add --live to make the call."
    assert w.clay_requests() == [] and w.contexts[0].store.select("credit_ledger") == []
    assert w.contexts[0].job == CHECK_EMAIL_JOB and w.contexts[0].dry_run


def test_live_makes_one_call_and_says_the_output_matches_so_the_switch_can_go_on(default_settings, capsys):
    w = World(default_settings)
    assert w.run("--live") == 0
    out = capsys.readouterr().out
    lines = printed(out)
    post, get = w.clay_requests()
    assert post.method == "POST" and post.json == {"items": [{"id": "1", "inputs": {
        "Full Name": "Harry Dryden", "Company Domain": "spill.chat"}}]}
    assert get.method == "GET" and get.url.endswith("/routines/run/run-9/results")
    assert "The routines endpoint answered: run run-9, status complete; the item complete." in lines
    assert "  Work Email: an email address" in lines and "  validation_status: text (5 characters)" in lines
    assert ("parse_work_email_output reads: status valid (from 'validation_status' = 'valid'), address h…@spill.chat "
            "(under 'Work Email'), provider 'Prospeo'.") in lines
    assert "Credits Clay reports: output.credits_used = 1. Counted in credit_ledger: 1." in lines
    assert lines[-2].startswith("MATCHES what the parser expects") and lines[-1] == SWITCH_ON
    [entry] = w.contexts[0].store.select("credit_ledger")
    assert (entry["system"], entry["job"], entry["credits"], entry["account_id"]) == ("clay", CHECK_EMAIL_JOB, 1.0, None)
    assert "harry@spill.chat" not in out  # the logs hash it, and the report masks it
    assert hash_email("harry@spill.chat") in out


def test_an_address_without_a_status_the_parser_reads_is_not_ready(default_settings, capsys):
    w = World(default_settings, output={"Work Email": "harry@spill.chat", "Validity": "deliverable"})
    assert w.run("--live") == 1
    lines = printed(capsys.readouterr().out)
    assert any(line.startswith("NOT READY: Work Email gave an address under 'Work Email' but no status the parser "
                               "reads (none of") for line in lines)
    assert lines[-1].startswith("clay_email_fallback stays no. Send the build the keys above")
    assert "Credits Clay reports: none (pick_contacts counts 2 for each address found" in "\n".join(lines)
    [entry] = w.contexts[0].store.select("credit_ledger")
    assert entry["credits"] == 2.0  # found an address and reported nothing: counted as pick_contacts counts it


def test_nobody_found_is_inconclusive(default_settings, capsys):
    w = World(default_settings, row={"id": "1", "status": "complete"})
    assert w.run("--live") == 1
    lines = printed(capsys.readouterr().out)
    assert "NOT READY: Work Email found no email for this person, so its email field is not confirmed." in lines
    assert lines[-1] == "clay_email_fallback stays no. Try again with a colleague Clay is likely to find."
    assert [r["credits"] for r in w.contexts[0].store.select("credit_ledger")] == [0.0]


@pytest.mark.parametrize("kw, words", [
    ({"post_status": 403}, "tick \"API & CLI\" in Work Email's Integrations settings"),
    ({"post_status": 401}, "Clay refused the key (HTTP 401)"),
    ({"post_status": 404}, "Clay has no such endpoint or function (HTTP 404)"),
    ({"run_status": "validation_failed"}, "Clay refused the inputs (validation_failed)"),
])
def test_a_failed_call_says_what_failed_and_why(default_settings, capsys, kw, words):
    w = World(default_settings, **kw)
    assert w.run("--live") == 1
    lines = printed(capsys.readouterr().out)
    [failed] = [line for line in lines if line.startswith("FAILED: ")]
    assert words in failed
    started = "run_status" in kw
    assert ("The routines endpoint answered" in "\n".join(lines)) is started
    assert lines[-1] == ("2 Clay credits are counted in case Clay charged them." if started else
                         "No run started, so no Clay credits were spent.") + " clay_email_fallback stays no."
    assert [r["credits"] for r in w.contexts[0].store.select("credit_ledger")] == [2.0 if started else 0.0]


@pytest.mark.parametrize("domain, words", [
    ("brightfin.com", "never a prospect: --domain must be one of Spill's own"),
    ("not a domain", "is not a domain like spill.chat"),
])
def test_it_refuses_anything_but_spill_s_own_domain(default_settings, capsys, domain, words):
    w = World(default_settings)
    argv = ["clay", "check-email", "--first", "Pat", "--last", "Lee", "--domain", domain, "--live"]
    assert cli.main(argv, context_factory=w) == 2
    assert words in capsys.readouterr().err
    assert w.clay_requests() == []


def test_it_refuses_a_domain_we_hold_as_an_account(default_settings, capsys):
    w = World(default_settings)
    w(CHECK_EMAIL_JOB, False).store.insert("accounts", [{"account_id": "a1", "domain": "meetspill.org"}])
    assert w.run("--live") == 0  # spill.chat itself is fine
    argv = ["clay", "check-email", "--first", "Harry", "--last", "Dryden", "--domain", "meetspill.org", "--live"]
    assert cli.main(argv, context_factory=w) == 2
    assert "is an account we hold" in capsys.readouterr().err


def test_it_spends_nothing_once_the_month_s_clay_budget_is_used(default_settings, capsys):
    settings = dataclasses.replace(default_settings, general=dataclasses.replace(default_settings.general,
                                                                                 clay_monthly_credits=1.0))
    w = World(settings)
    assert w.run("--live") == 2
    assert "the month's Clay budget is used" in capsys.readouterr().err and w.clay_requests() == []


def test_without_the_clay_key_it_stops_before_spending(default_settings, capsys):
    w = World(default_settings)
    real = w.__call__

    def factory(job, live_flag, operator=False):
        ctx = real(job, live_flag, operator)
        ctx.clients.secrets._fetch = lambda name: "" if name == "US_OUTBOUND_CLAY_API_KEY" else f"test-{name}"
        return ctx

    argv = ["clay", "check-email", "--first", "Harry", "--last", "Dryden", "--domain", "spill.chat"]
    assert cli.main(argv, context_factory=factory) == 0  # the dry run needs no key
    assert cli.main([*argv, "--live"], context_factory=factory) == 2
    assert "set US_OUTBOUND_CLAY_API_KEY" in capsys.readouterr().err
    assert w.clay_requests() == [] and w.contexts[-1].store.select("credit_ledger") == []
