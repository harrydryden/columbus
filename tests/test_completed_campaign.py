"""A campaign Instantly has marked completed still takes leads, and the lead add resumes it (7 Oct 2026).

Instantly marks an active campaign "completed" (status 3) once no lead is left to email. On 6 Oct that happened
straight after `start` to the two campaigns with no leads yet (Harry Dryden's and Hannah MacIntosh's). Enrol gave
only active campaigns leads, so they got none, stayed completed, and golive failed them however often
`start --live` ran. Now a completed campaign takes leads, each add resumes it (capacity.resume_if_completed), and
golive passes it. A paused campaign (a stop, a kill rule) still takes nothing and is never resumed by an add."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_enrol import CAMPAIGNS, make
from tests.test_golive import lines_of, ready_world
from tests.test_seed import C_HANNAH, SEED, h  # noqa: F401  (the seed harness fixture)
from tests.test_seed import lead_posts
from us_outbound.enrol import capacity, enrol
from us_outbound.ops import cli

HARRY = "US Outbound – Harry Dryden"


def with_status(name: str, status: int) -> dict:
    return {"items": [{**c, "status": status} if c["name"] == name else c for c in CAMPAIGNS["items"]]}


def activations(t) -> list[str]:
    return [r.url.split("/campaigns/")[1].split("/")[0] for r in t.requests
            if r.method == "POST" and r.url.endswith("/activate")]


# -- which campaigns take leads -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("status, takes", [(1, True), (3, True), (2, False), (0, False)])
def test_active_and_completed_campaigns_take_leads_paused_and_draft_do_not(status, takes):
    found = [{"name": HARRY, "status": status}]
    assert (capacity.campaign_problem("Harry Dryden", found) == "") is takes


class Instantly:
    def __init__(self, status: int):
        self.campaigns = [{"name": HARRY, "status": status}]
        self.activated: list[str] = []

    def list_campaigns(self):
        return self.campaigns

    def activate_campaign(self, name):
        self.activated.append(name)


@pytest.mark.parametrize("status, resumed", [(3, True), (1, False), (2, False), (0, False)])
def test_only_a_completed_campaign_is_resumed(status, resumed):
    inst = Instantly(status)
    ctx = SimpleNamespace(clients=SimpleNamespace(instantly=inst))
    assert capacity.resume_if_completed(ctx, HARRY) is resumed
    assert inst.activated == ([HARRY] if resumed else [])


# -- enrol -----------------------------------------------------------------------------------------------------------


def test_enrol_gives_a_completed_campaign_leads_and_resumes_it():
    ctx, t = make(live=True)
    t.route("GET", "/campaigns", with_status(HARRY, 3))
    t.route("POST", "/activate", {})
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["enrolled"] == 3, out
    assert {r.json["campaign_id"] for r in t.requests if r.url.endswith("/leads/add")} == {"c-harry", "c-hannah", "c-sam"}
    assert activations(t) == ["c-harry"]  # the completed one only, after its leads went in


def test_enrol_gives_a_paused_campaign_nothing_and_leaves_it_paused():
    ctx, t = make(live=True)
    t.route("GET", "/campaigns", with_status(HARRY, 2))
    enrol.run(ctx)
    assert "c-harry" not in {r.json["campaign_id"] for r in t.requests if r.url.endswith("/leads/add")}
    assert activations(t) == []


# -- seed send -------------------------------------------------------------------------------------------------------


def test_a_seed_added_to_a_completed_campaign_resumes_it(h, capsys):  # noqa: F811
    h.instantly.by_name(C_HANNAH)["status"] = 3
    assert h.run("seed", "send", SEED, "--owner", "Hannah Spalding", "--live") == 0
    assert len(lead_posts(h)) == 1 and h.instantly.by_name(C_HANNAH)["status"] == 1  # the fake's activate sets 1
    assert "Added. Instantly sends it in the campaign's window" in capsys.readouterr().out


# -- golive ----------------------------------------------------------------------------------------------------------


def test_golive_passes_a_completed_campaign_and_says_the_next_lead_resumes_it(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    f.instantly.by_name(HARRY)["status"] = 3
    assert cli.main(["golive"], context_factory=f) == 0
    out = capsys.readouterr().out
    assert lines_of(out)["Campaigns"].startswith("PASS  Campaigns")
    assert f"{HARRY}: matches (completed); no lead left to email, and the next lead added resumes it" in out

