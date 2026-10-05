"""The seed-inbox test of the opt-out (ops/seed.py): `seed send` adds one lead of ours to an owner's campaign,
rendered as enrol renders it; `seed check` reads it back as sync_outcomes does (status -2 is the pass)."""

from __future__ import annotations

import pytest

from tests.test_cli import Harness
from tests.test_operator_cli import report
from us_outbound.clients.instantly import LEAD_UNSUBSCRIBED
from us_outbound.ops import seed

C_HANNAH = "US Outbound – Hannah Spalding"
SEED = "harry+seed1@spill.chat"


@pytest.fixture
def h(default_settings):
    h = Harness(default_settings)
    h.instantly.add_campaign(C_HANNAH, status=0)  # draft: `campaigns ensure --live` made it, start has not run

    def added(req):  # POST /leads/add: Instantly creates each lead in the campaign
        created = []
        for i, row in enumerate(req.json["leads"]):
            lead = h.instantly.add_lead(req.json["campaign_id"], row["email"])
            lead.update(company_name=row.get("company_name"), status=1)
            created.append({"id": lead["id"], "email": row["email"], "index": i})
        return {"leads_uploaded": len(created), "created_leads": created}

    h.transport.route("POST", "/leads/add", fn=added)
    return h


def lead_posts(h):
    return [r for r in h.transport.requests if r.url.endswith("/leads/add")]


def test_dry_run_shows_the_email_and_adds_nothing(h, capsys):
    assert h.run("seed", "send", SEED, "--owner", "Hannah Spalding") == 0
    lines = report(capsys.readouterr().out)
    assert lines[0].startswith(f"Seed email for {SEED}, from Hannah Spalding ({C_HANNAH}, draft), copy ")
    assert "Dry-run: no lead was added. Add --live to add it." in lines
    assert lead_posts(h) == []


def test_live_adds_one_lead_with_the_four_rendered_emails_and_says_what_comes_next(h, capsys):
    assert h.run("seed", "send", SEED, "--owner", "hannah spalding", "--live") == 0  # an operator command: --live alone
    [post] = lead_posts(h)
    [lead] = post.json["leads"]
    assert lead["email"] == SEED and lead["company_name"] == seed.SEED_COMPANY
    variables = lead["custom_variables"]
    assert sorted(variables) == [f"s{n}_{part}" for n in range(1, 5) for part in ("body", "subject")]
    assert all(variables[f"s{n}_body"] for n in range(1, 5))
    assert "{{" not in variables["s1_body"]  # every merge field filled, as enrol fills it
    out = capsys.readouterr().out
    assert "Added. The campaign is draft, so Instantly sends nothing yet. `us-outbound start --live` activates it" in out
    assert "09:00–16:00 US Eastern" in out
    assert [b["status"] for b in h.beats(seed.JOB)] == ["ok"]
    assert h.run("seed", "check") == 1  # and `seed check` finds it
    assert f"{SEED} in {C_HANNAH} (draft): active" in capsys.readouterr().out


def test_an_address_already_in_the_campaign_is_not_added_twice(h, capsys):
    h.instantly.add_lead(h.instantly.by_name(C_HANNAH)["id"], SEED)
    assert h.run("seed", "send", SEED, "--owner", "Hannah Spalding", "--live") == 2
    assert lead_posts(h) == []
    assert f"Not added: {SEED} is already a lead in {C_HANNAH}." in capsys.readouterr().out


@pytest.mark.parametrize("address, why", [
    ("dana@harborfinch.com", "a seed inbox is one of ours"),
    ("not-an-address", "is not an email address"),
])
def test_a_seed_inbox_is_ours(h, capsys, address, why):
    assert h.run("seed", "send", address, "--owner", "Hannah Spalding", "--live") == 2
    assert why in capsys.readouterr().err
    assert lead_posts(h) == []


def test_a_contact_we_hold_is_never_a_seed(h, capsys):
    h.store.insert("contacts", [{"contact_id": "k1", "account_id": "a1", "email": "Pat@Gmail.com "}])
    assert h.run("seed", "send", "pat@gmail.com", "--owner", "Hannah Spalding", "--live") == 2
    assert "is a contact we hold" in capsys.readouterr().err


def test_an_owner_without_a_campaign_is_told_how_to_make_one(h, capsys):
    assert h.run("seed", "send", SEED, "--owner", "Sam Jackson", "--live") == 2
    assert "`us-outbound campaigns ensure --live` creates it" in capsys.readouterr().err


def test_check_reads_the_seed_lead_back_and_passes_once_it_is_unsubscribed(h, capsys):
    cid = h.instantly.by_name(C_HANNAH)["id"]
    lead = h.instantly.add_lead(cid, SEED)
    lead.update(company_name=seed.SEED_COMPANY, status=1)
    h.instantly.add_lead(cid, "prospect@example.com")  # not a seed: left out
    assert h.run("seed", "check") == 1
    out = report(capsys.readouterr().out)
    assert out[0] == f"{SEED} in {C_HANNAH} (draft): active: waiting for the campaign's window, or sending, not emailed yet"
    assert out[-1].startswith("Not yet: open the seed email, click")
    lead["status"] = LEAD_UNSUBSCRIBED
    assert h.run("seed", "check") == 0
    out = report(capsys.readouterr().out)
    assert out[0].startswith(f"{SEED} in {C_HANNAH} (draft): unsubscribed")
    assert out[-1].startswith("PASS: Instantly shows the seed lead as unsubscribed (status -2)")


def test_check_with_no_seed_lead_says_how_to_send_one(h, capsys):
    assert h.run("seed", "check") == 1
    assert "No seed lead in any US Outbound campaign yet" in capsys.readouterr().out
