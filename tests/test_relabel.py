"""`us-outbound relabel` (ops/relabel.py; Harry, 7 Oct 2026): the companies in the queue under the labels the
Industries rules give now (apollo_universe.best_label: a label within a group needs its own keywords), and the
open send-approval cards rendered under a wrong label withdrawn, so a later enrol proposes them with the right copy.

The world is tests/test_send_approvals.py's: three live cards (acc-1 Advertising agencies, acc-2 Fintech, acc-3
Advertising agencies), Slack on the transport, every call through the real clients and the guard."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_enrol import instantly_posts
from tests.test_render import AGENCIES, TECH
from tests.test_send_approvals import at, blocks_text, item_for, items, proposed  # noqa: F401
from tests.test_send_approvals import default_openers  # noqa: F401  (the autouse fixture)
from us_outbound.clean.domains import is_public_body
from us_outbound import labels
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


def test_a_public_body_is_disqualified_but_no_rules_label_is_left_for_the_label_check():
    """Harry, 7 Oct 2026: the rules alone never rule a company out (the first relabel disqualified 89 software
    companies whose Apollo codes include IT services); a public body always is."""
    ctx, t, sl = world()
    ctx.store.update("accounts", {"account_id": "acc-3"}, {"domain": "braintreema.gov"})
    fact(ctx, "acc-1", "naics", ["111110"], at=datetime(2026, 12, 1, tzinfo=UTC))  # the latest: soybeans, no label fits
    out = relabel.run(ctx)
    a1, a3 = (ctx.store.get("accounts", account_id=a) for a in ("acc-1", "acc-3"))
    assert (a3["status"], a3["tier"], a3["tier_reason"]) == ("disqualified", "Excluded", "a public body, never prospected")
    assert (a1["status"], a1["industry"]) == ("verified", "Advertising agencies")  # untouched: the label check decides
    assert out["cards_withdrawn"] == ["Loop Studio"]
    assert any("Loop Studio will not be emailed" in blocks_text(u) for u in sl.updates)


def test_a_company_an_earlier_relabel_disqualified_on_the_rules_alone_is_put_back_for_the_label_check():
    ctx, t, sl = world()
    ctx.store.update("accounts", {"account_id": "acc-1"}, {"status": "disqualified", "tier": "Excluded",
                                                          "tier_reason": relabel.NO_LABEL_FITS})
    ctx.store.update("accounts", {"account_id": "acc-3"}, {"status": "disqualified", "tier": "Excluded",
                                                          "tier_reason": relabel.PUBLIC_BODY})
    at(ctx, ctx.now, live=False, job="relabel")
    assert relabel.run(ctx)["restored"] == ["acmecreative.com"]
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "disqualified"  # dry-run: unchanged
    at(ctx, ctx.now, live=True, job="relabel")
    out = relabel.run(ctx)
    a1, a3 = (ctx.store.get("accounts", account_id=a) for a in ("acc-1", "acc-3"))
    assert out["restored"] == ["acmecreative.com"]
    assert (a1["status"], a1["tier"], a1["tier_reason"]) == ("queued", None, None)
    assert a1["label_source"] == "rules"  # the rules' label with its group's copy until the label check asks
    assert a3["status"] == "disqualified"  # a public body stays out
    assert relabel.run(ctx)["restored"] == []  # once


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



# -- with the label check (labels.py; Harry, 7 Oct 2026) ------------------------------------------------------------


def verdict_fact(ctx, aid: str, model: str, confidence: str = "high", entity: str = "company") -> None:
    v = labels.Verdict(model, confidence, entity, labels_hash="h-old")
    d = labels.decide(None, v, ctx.settings)
    ctx.store.insert("signal_events", [{
        "event_id": f"{aid}-verdict", "account_id": aid, "source": labels.JOB, "fact": labels.VERDICT_FACT,
        "value": labels.verdict_value(None, v, d, asked=True), "quote": "", "source_url": "", "observed_at": ctx.now}])


def home_fact(ctx, aid: str, at: datetime, value=None) -> None:
    value = {"title": "Acme Creative", "text": "An advertising agency."} if value is None else value
    ctx.store.insert("signal_events", [{
        "event_id": f"{aid}-home-{at.isoformat()}", "account_id": aid, "source": "careers_pages", "fact": "home_page",
        "value": value, "quote": "", "source_url": "", "observed_at": at}])


def test_a_label_the_model_gave_alone_from_apollos_facts_gets_its_groups_copy():
    """10 Oct 2026, the 95% target: the model sure of a label the rules did not give, asked without the home page,
    places the group, not the label, until the page is read and it is asked again."""
    ctx, t, sl = world()
    verdict_fact(ctx, "acc-1", "Advertising agencies")  # no home page on file when it was asked
    out = relabel.run(ctx)
    a = ctx.store.get("accounts", account_id="acc-1")
    assert out["changed"] == 1
    assert (a["industry"], a["label_source"]) == ("Marketing & Creative Agencies", labels.UMBRELLA)
    assert labels.copy_level(a) == labels.GROUP_COPY
    events = ctx.store.select("signal_events", {"account_id": "acc-1"})
    assert labels.wants_home_page(events) and not labels.second_look(events)
    home_fact(ctx, "acc-1", ctx.now + timedelta(hours=1))  # read: the model is asked once more
    assert labels.second_look(ctx.store.select("signal_events", {"account_id": "acc-1"}))


def test_a_stored_verdict_is_honoured_when_the_rules_change_and_no_model_is_asked():
    ctx, t, sl = world()
    home_fact(ctx, "acc-1", ctx.now - timedelta(days=1))  # read before the model was asked
    verdict_fact(ctx, "acc-1", "Advertising agencies")  # the model said so when it was checked
    out = relabel.run(ctx)
    a = ctx.store.get("accounts", account_id="acc-1")
    # The rules alone now give the agencies' own label; with the model sure of Advertising agencies, it stays.
    assert out["changed"] == 0 and (a["industry"], a.get("label_source")) == ("Advertising agencies", None)
    assert len(items(ctx, "open")) == 3
    assert ctx.store.select("credit_ledger", {"job": labels.JOB}) == []


def test_an_approvers_label_never_moves():
    ctx, t, sl = world()
    ctx.store.update("accounts", {"account_id": "acc-1"}, {"label_source": "approver"})
    assert relabel.run(ctx)["changed"] == 0
    assert ctx.store.get("accounts", account_id="acc-1")["industry"] == "Advertising agencies"


def test_a_relabelled_company_gets_the_rules_source_and_its_check_time():
    ctx, t, sl = world()
    relabel.run(ctx)
    a = ctx.store.get("accounts", account_id="acc-1")
    assert (a["industry"], a["label_source"], a["label_checked_at"]) == (AGENCIES, "rules", ctx.now)
    assert labels.copy_level(a) == "group"


def test_a_stored_verdict_that_rules_a_company_out_disqualifies_it():
    ctx, t, sl = world()
    verdict_fact(ctx, "acc-2", "none", entity="association")
    out = relabel.run(ctx)
    a = ctx.store.get("accounts", account_id="acc-2")
    assert (a["status"], a["tier"]) == ("disqualified", "Excluded") and "Brightfin" in out["cards_withdrawn"]


def test_labels_set_corrects_a_company_and_withdraws_its_card_and_show_tells_the_story(capsys):
    from tests.test_send_approvals import FakeOverrides
    from us_outbound.ops import cli

    ctx, t, sl = world()
    sheet = FakeOverrides(t)
    at(ctx, ctx.now, live=False, job="labels_set")
    dry = relabel.set_label(ctx, "https://www.loopstudio.com/", "fintech")
    assert dry == {"dry_run": True, "domain": "loopstudio.com", "from": "Advertising agencies", "to": "Fintech",
                   "active": True, "cards_to_withdraw": ["Loop Studio"]}
    assert ctx.store.get("accounts", account_id="acc-3")["industry"] == "Advertising agencies"
    at(ctx, ctx.now, live=True, job="labels_set")
    out = relabel.set_label(ctx, "loopstudio.com", "fintech")
    assert (out["sheet"], out["cards_withdrawn"]) == ("added", ["Loop Studio"])
    assert sheet.appended[0][:3] == ["loopstudio.com", "industry", "Fintech"]
    shown = relabel.show(ctx, "loopstudio.com")
    assert (shown["industry"], shown["label_source"], shown["copy_level"]) == ("Fintech", "approver", "label")
    assert shown["history"][0]["corrected"] == "Advertising agencies → Fintech" and shown["history"][0]["via"] == "cli"
    with pytest.raises(LookupError, match="no company with the domain 'nobody.com'"):
        relabel.set_label(ctx, "nobody.com", "fintech")
    with pytest.raises(ValueError, match="no Industries label called 'crypto'"):
        relabel.set_label(ctx, "loopstudio.com", "crypto")
    assert cli.main(["labels", "show", "loopstudio.com"], context_factory=lambda *a, **k: ctx) == 0
    assert '"label_source": "approver"' in capsys.readouterr().out
    assert cli.main(["labels", "set", "loopstudio.com"], context_factory=lambda *a, **k: ctx) == 2
    assert "labels set needs the label" in capsys.readouterr().err


# -- `us-outbound labels audit` ----------------------------------------------------------------------------------------


def audit_world(live=True):
    from tests.test_verify import LabelSDK

    ctx, t, sl = world()
    sdk = LabelSDK({"acmecreative.com": {"label": "Fintech", "evidence": "advertising agency"},
                    "brightfin.com": {"label": "Fintech", "evidence": "fintech"}})
    ctx.clients.claude_sdk = sdk
    fact(ctx, "acc-1", "keywords", ["advertising agency"])
    fact(ctx, "acc-2", "description", "Brightfin builds fintech for shops.")
    at(ctx, ctx.now, live=live, job=relabel.AUDIT_JOB)
    return ctx, t, sl, sdk


def test_a_dry_audit_counts_prices_and_shows_a_prompt_and_asks_nothing():
    ctx, t, sl, sdk = audit_world(live=False)
    out = relabel.audit(ctx)
    assert (out["to_check"], out["open_accounts"], out["stale"]) == (2, 3, 0)  # acc-3 has nothing to check against
    assert 0.005 < out["per_call_usd"] < 0.03 and out["most_usd"] == round(out["per_call_usd"] * 2, 2)
    assert out["sample_prompt"].startswith("<company>\nName: ") and sdk.calls == []
    assert out["stored"]["dry_run"] is True and len(items(ctx, "open")) == 3


def test_a_live_audit_asks_decides_and_withdraws_the_cards_that_no_longer_fit(capsys):
    from us_outbound.ops import cli

    ctx, t, sl, sdk = audit_world()
    out = relabel.audit(ctx)
    assert out["asked"] == 2 and out["usd"] > 0 and out["unavailable"] == ""
    a1 = ctx.store.get("accounts", account_id="acc-1")
    # The rules say Advertising agencies (its own keyword); the model is sure it is Fintech, from Apollo's facts alone
    # (the home page said nothing): Fintech's group, with its group's copy.
    assert (a1["industry"], a1["label_source"]) == ("Technology & Startups", "umbrella")
    assert out["disagreements"][0]["domain"] == "acmecreative.com"
    assert out["cards_withdrawn"] == ["Acme Creative"] and {r["account_id"] for r in items(ctx, "open")} == {
        "acc-2", "acc-3"}
    assert out["decisions"] == {"umbrella": 1, "rules+model": 1}
    # Asked once: a second audit finds nothing left to check.
    again = relabel.audit(ctx)
    assert again["to_check"] == 0 and len(sdk.calls) == 2
    assert cli.main(["labels", "audit"], context_factory=lambda *a, **k: ctx) == 0
    assert "0 of 3 open companies have no fresh label check" in capsys.readouterr().out


def test_a_live_audit_reads_the_home_page_of_a_company_the_model_was_unsure_of_and_asks_again(monkeypatch, capsys):
    """Harry, 7 Oct 2026: the first audit held 204 of 409 companies, the model unsure on Apollo's facts alone. The next
    reads the home page of each it was unsure of (sources/pages.home_pass) and asks about it once more with the page."""
    from us_outbound.ops import cli
    from us_outbound.sources import pages

    ctx, t, sl, sdk = audit_world()
    fact(ctx, "acc-3", "apollo_industry", "design services")  # no rules label
    sdk.answers["loopstudio.com"] = {"label": TECH, "confidence": "low"}
    monkeypatch.setattr(pages, "home_pass", lambda *a, **k: {})  # the pass's time ran out before these pages
    first = relabel.audit(ctx)
    monkeypatch.undo()
    assert first["decisions"]["held"] == 1
    a3 = ctx.store.get("accounts", account_id="acc-3")
    assert a3["status"] == "queued" and a3["label_source"] == "disputed"

    read: list[str] = []

    def read_home(self):
        read.append(self.domain)
        self.home = {"title": "Loop Studio", "text": "Loop makes design software."}
        self.home_url = "https://loopstudio.com/"
        return pages.READ

    monkeypatch.setattr(pages.Reader, "read_home", read_home)
    sdk.answers["loopstudio.com"] = {"label": TECH, "confidence": "high", "evidence": "Loop makes design software"}
    calls = len(sdk.calls)
    at(ctx, ctx.now + timedelta(days=1), live=True, job=relabel.AUDIT_JOB)
    second = relabel.audit(ctx)
    # Acme Creative's page too: the model gave its label (Fintech) alone, from Apollo's facts.
    assert read == ["acmecreative.com", "loopstudio.com"] and second["home_pages"]["said_what_they_do"] == 2
    assert second["asked"] == 2 and len(sdk.calls) == calls + 2
    assert "Home page: Loop Studio · Loop makes design software." in sdk.calls[-1]["messages"][0]["content"]
    a3 = ctx.store.get("accounts", account_id="acc-3")
    assert (a3["industry"], a3["label_source"]) == (TECH, "model")
    cli._audit_report(second, True)
    assert "Read the home page of 2 companies whose label the page could settle: 2 say what they do, 0 say nothing, 0 refused, " \
           "0 did not answer. Those with a page are asked again." in capsys.readouterr().out
    # Asked with its page: the verdict stands, and the page is not read again.
    at(ctx, ctx.now + timedelta(days=1), live=True, job=relabel.AUDIT_JOB)
    third = relabel.audit(ctx)
    assert third["to_check"] == 0 and third["home_pages"] == {} and len(read) == 2


# -- `approvals redo` (Harry, 7 Oct 2026: "bulk reject all of those contacts from today and send them round again") --


def test_redo_all_withdraws_each_card_and_posts_it_again_in_its_place():
    from us_outbound.enrol import approvals

    ctx, t, sl, _ = proposed()
    old = {r["item_id"] for r in items(ctx, "open")}
    at(ctx, ctx.now, job="approvals_redo")
    out = approvals.redo(ctx, "all")
    assert out["cards"] == 3 and len(out["posted_again"]) == 3 and out["back_to_queue"] == out["not_emailed"] == []
    for r in items(ctx):
        if r["item_id"] in old:
            assert (r["status"], r["payload"]["outcome"]) == ("handled", "expired")
            assert r["payload"]["reason"] == f"withdrawn: {approvals.REDO_WHY}"
    new = [r for r in items(ctx, "open")]
    assert len(new) == 3 and {r["payload"]["replaces"] for r in new} == old
    assert {r["account_id"] for r in new} == {"acc-1", "acc-2", "acc-3"}
    assert instantly_posts(t) == []


def test_redo_all_in_capitals_with_no_open_cards_does_nothing():
    """9 Oct 2026: "ALL" was compared raw, so with no open cards it read the first of none and failed."""
    from us_outbound.enrol import approvals

    ctx, t, sl, _ = proposed()
    for r in items(ctx, "open"):
        ctx.store.update("hitl_items", {"item_id": r["item_id"]}, {"status": "handled"})
    at(ctx, ctx.now, job="approvals_redo")
    out = approvals.redo(ctx, "ALL")
    assert out["cards"] == 0 and out["posted_again"] == []


def test_redo_does_not_post_again_a_company_that_may_not_be_emailed():
    from us_outbound.enrol import approvals

    ctx, t, sl, _ = proposed()
    ctx.store.update("accounts", {"account_id": "acc-3"}, {"domain": "braintreema.gov"})
    at(ctx, ctx.now, job="approvals_redo")
    out = approvals.redo(ctx, "all")
    assert out["not_emailed"] == ["Loop Studio"] and len(out["posted_again"]) == 2
    assert "acc-3" not in {r["account_id"] for r in items(ctx, "open")}


def test_a_dry_redo_changes_nothing():
    from us_outbound.enrol import approvals

    ctx, t, sl, _ = proposed()
    at(ctx, ctx.now, live=False, job="approvals_redo")
    before = (len(sl.posts), len(sl.updates))
    out = approvals.redo(ctx, "all")
    assert out["dry_run"] and len(out["would"]) == 3 and len(items(ctx, "open")) == 3
    assert (len(sl.posts), len(sl.updates)) == before


def test_an_overrides_row_the_columns_lag_is_applied():
    """relabel puts an Overrides row's label in the columns when they lag it (9 Oct 2026, defect 5), as verify does."""
    ctx, t, sl = world()
    a = ctx.store.get("accounts", account_id="acc-2")
    override = Override(a["domain"], "industry", "Edtech")
    ctx.settings = dataclasses.replace(ctx.settings, overrides=(override,))
    at(ctx, ctx.now, live=True, job="relabel")
    relabel.run(ctx)
    b = ctx.store.get("accounts", account_id="acc-2")
    assert (b["industry"], b["label_source"]) == ("Edtech", "override")
