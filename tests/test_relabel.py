"""`us-outbound relabel` (ops/relabel.py; Harry, 7 Oct 2026): the companies in the queue under the labels the
Industries rules give now (apollo_universe.best_label: a label within a group needs its own keywords), and the
open send-approval cards rendered under a wrong label withdrawn, so a later enrol proposes them with the right copy.

The world is tests/test_send_approvals.py's: three live cards (acc-1 Advertising agencies, acc-2 Fintech, acc-3
Advertising agencies), Slack on the transport, every call through the real clients and the guard."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from tests.test_enrol import instantly_posts
from tests.test_render import AGENCIES, TECH
from tests.test_send_approvals import at, blocks_text, item_for, items, proposed  # noqa: F401
from tests.test_send_approvals import default_openers  # noqa: F401  (the autouse fixture)
from us_outbound.clean.domains import is_public_body
from us_outbound.enrol import enrol
from us_outbound.ops import relabel
from us_outbound.settings.model import Industry, Override

INDUSTRIES = (
    Industry(AGENCIES, AGENCIES, True, naics_prefixes=("5418",), apollo_keywords=("marketing agency",), priority=2),
    Industry("Advertising agencies", AGENCIES, True, naics_prefixes=("541810",),
             apollo_keywords=("advertising agency",), priority=2),
    Industry(TECH, TECH, True, naics_prefixes=("5415",), apollo_keywords=("saas",), priority=1),
    Industry("Fintech", TECH, True, naics_prefixes=("5415",), apollo_keywords=("fintech",), priority=1),
    Industry("Games studios", TECH, True, naics_prefixes=("541511",), apollo_keywords=("video games",), priority=1),
)


def fact(ctx, aid: str, name: str, value, at: datetime | None = None) -> None:
    ctx.store.insert("signal_events", [{"event_id": f"{aid}-{name}-{(at or ctx.now).isoformat()}", "account_id": aid,
                                        "source": "apollo_org", "fact": name, "value": value, "quote": "",
                                        "source_url": "", "observed_at": at or ctx.now}])


def world():
    ctx, t, sl, _ = proposed()
    assert len(items(ctx, "open")) == 3
    ctx.settings = dataclasses.replace(ctx.settings, industries=INDUSTRIES)
    fact(ctx, "acc-1", "naics", ["541810"])  # an advertising code, but no keyword: the agencies umbrella
    fact(ctx, "acc-2", "naics", ["541511"])
    fact(ctx, "acc-2", "keywords", ["fintech", "payments"])  # stays Fintech
    at(ctx, ctx.now, job="relabel")
    return ctx, t, sl


def test_dry_run_says_what_would_change_and_changes_nothing():
    ctx, t, sl = world()
    at(ctx, ctx.now, live=False, job="relabel")
    before = len(sl.updates)
    out = relabel.run(ctx)
    assert out["changed"] == 1 and out["moves"] == {f"Advertising agencies → {AGENCIES}": 1}
    assert out["changes"] == [f"acmecreative.com: Advertising agencies → {AGENCIES}"]
    assert out["cards_to_withdraw"] == ["Acme Creative"] and out["cards_withdrawn"] == []
    assert ctx.store.get("accounts", account_id="acc-1")["industry"] == "Advertising agencies"
    assert len(items(ctx, "open")) == 3 and len(sl.updates) == before


def test_live_relabels_and_withdraws_the_card_so_a_later_enrol_proposes_it_again():
    ctx, t, sl = world()
    out = relabel.run(ctx)
    assert out["cards_withdrawn"] == ["Acme Creative"]
    a = ctx.store.get("accounts", account_id="acc-1")
    assert (a["industry"], a["industry_group"], a["status"]) == (AGENCIES, AGENCIES, "verified")
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["payload"]["outcome"]) == ("handled", "system", "expired")
    assert row["payload"]["reason"] == f"withdrawn: its industry was Advertising agencies and is now {AGENCIES}"
    assert any(blocks_text(u).startswith("↩️ Withdrawn: its industry was Advertising agencies") for u in sl.updates)
    assert {r["account_id"] for r in items(ctx, "open")} == {"acc-2", "acc-3"}  # no facts for acc-3: unchanged
    assert instantly_posts(t) == []
    at(ctx, datetime(2026, 10, 29, 12, 0, tzinfo=UTC), job="enrol")
    assert enrol.run(ctx)["send_approvals"]["posted"] >= 1  # acc-1 comes back, under its new label


def test_a_public_body_or_a_company_no_label_fits_is_disqualified_and_its_card_withdrawn():
    ctx, t, sl = world()
    ctx.store.update("accounts", {"account_id": "acc-3"}, {"domain": "braintreema.gov"})
    fact(ctx, "acc-1", "naics", ["111110"], at=datetime(2026, 12, 1, tzinfo=UTC))  # the latest: soybeans, no label fits
    out = relabel.run(ctx)
    a1, a3 = (ctx.store.get("accounts", account_id=a) for a in ("acc-1", "acc-3"))
    assert (a3["status"], a3["tier"], a3["tier_reason"]) == ("disqualified", "Excluded", "a public body, never prospected")
    assert a1["status"] == "disqualified" and "no Industries label fits" in a1["tier_reason"]
    assert sorted(out["cards_withdrawn"]) == ["Acme Creative", "Loop Studio"]
    assert any("Loop Studio will not be emailed" in blocks_text(u) for u in sl.updates)


def test_an_overrides_row_keeps_its_industry():
    ctx, t, sl = world()
    override = Override("acmecreative.com", "industry", "Advertising agencies")
    ctx.settings = dataclasses.replace(ctx.settings, overrides=(override,))
    assert relabel.run(ctx)["changed"] == 0


@pytest.mark.parametrize("domain, public", [
    ("braintreema.gov", True), ("www.army.mil", True), ("https://ci.boston.ma.us/x", True), ("k12.ny.us", True),
    ("co.kings.ny.us", True), ("joe@town.lexington.ma.us", True),
    ("acme.us", False), ("governance.com", False), ("gov.uk.example.com", False), ("acme.com", False), ("", False),
])
def test_public_bodies(domain, public):
    assert is_public_body(domain) is public
