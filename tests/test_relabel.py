"""`us-outbound relabel` (ops/relabel.py; Harry, 7 Oct 2026): the companies in the queue under the labels the
Industries rules give now (apollo_universe.best_label: a label within a group needs its own keywords), so a later
enrol renders them with the right copy.

The world is tests/test_enrol.py's three accounts (acc-1 Advertising agencies, acc-2 Fintech, acc-3 Advertising
agencies), live, every call through the real clients and the guard. (An open send-approval card under a wrong label
was withdrawn too, until send approvals went with W2-1.)"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_enrol import default_openers  # noqa: F401  (the autouse fixture)
from tests.test_enrol import instantly_posts, make
from tests.test_render import AGENCIES, TECH, make_settings
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


class FakeOverrides:
    """The settings sheet's Overrides tab behind the transport: read, a cell updated, rows appended."""

    HEAD = ["domain", "field", "value", "note"]

    def __init__(self, t, rows=()):
        self.rows = [dict(r) for r in rows]
        self.appended: list[list[str]] = []
        t.route("GET", "Overrides", fn=lambda r: {"values": [self.HEAD] + [[x.get(h, "") for h in self.HEAD]
                                                                           for x in self.rows]})
        t.route("PUT", "Overrides", fn=self._put)
        t.route("POST", ":append", fn=self._append)

    def _put(self, req):
        row = int(req.json["range"].rsplit("C", 1)[1]) - 2  # the value column, C
        self.rows[row]["value"] = req.json["values"][0][0]
        return {}

    def _append(self, req):
        for values in req.json["values"]:
            self.appended.append(values)
            self.rows.append(dict(zip(self.HEAD, values)))
        return {}


def at(ctx, when: datetime, *, live: bool | None = None, job: str = "relabel") -> None:
    ctx.now = when
    ctx.job = job
    ctx.guard.configure(job=job, **({} if live is None else {"live": live}))


def world():
    s = make_settings(live_sending=True, approver_slack_ids=("U_HARRY",))
    ctx, t = make(live=True, settings=s)
    ctx.settings = dataclasses.replace(ctx.settings, industries=INDUSTRIES)
    fact(ctx, "acc-1", "naics", ["541810"])  # an advertising code, but no keyword: the agencies umbrella
    fact(ctx, "acc-2", "naics", ["541511"])
    fact(ctx, "acc-2", "keywords", ["fintech", "payments"])  # stays Fintech
    at(ctx, ctx.now, job="relabel")
    return ctx, t


def test_dry_run_says_what_would_change_and_changes_nothing():
    ctx, t = world()
    at(ctx, ctx.now, live=False, job="relabel")
    out = relabel.run(ctx)
    assert out["changed"] == 1 and out["moves"] == {f"Advertising agencies → {AGENCIES}": 1}
    assert out["changes"] == [f"acmecreative.com: Advertising agencies → {AGENCIES}"]
    assert "cards_to_withdraw" not in out and "cards_withdrawn" not in out
    assert ctx.store.get("accounts", account_id="acc-1")["industry"] == "Advertising agencies"


def test_live_relabels_so_a_later_enrol_renders_the_new_label():
    ctx, t = world()
    out = relabel.run(ctx)
    assert out["changed"] == 1
    a = ctx.store.get("accounts", account_id="acc-1")
    assert (a["industry"], a["industry_group"], a["status"]) == (AGENCIES, AGENCIES, "verified")
    assert ctx.store.get("accounts", account_id="acc-3")["industry"] == "Advertising agencies"  # no facts: unchanged
    assert instantly_posts(t) == []
    at(ctx, ctx.now, job="enrol")
    assert enrol.run(ctx)["enrolled"] == 3
    # acc-1's contact is enrolled under its new label (enrol stamps the label each email was rendered under).
    assert ctx.store.get("contacts", contact_id="con-1")["industry_at_enrol"] == AGENCIES


def test_a_public_body_is_disqualified_but_no_rules_label_is_left_for_the_label_check():
    """Harry, 7 Oct 2026: the rules alone never rule a company out (the first relabel disqualified 89 software
    companies whose Apollo codes include IT services); a public body always is."""
    ctx, t = world()
    ctx.store.update("accounts", {"account_id": "acc-3"}, {"domain": "braintreema.gov"})
    fact(ctx, "acc-1", "naics", ["111110"], at=datetime(2026, 12, 1, tzinfo=UTC))  # the latest: soybeans, no label fits
    relabel.run(ctx)
    a1, a3 = (ctx.store.get("accounts", account_id=a) for a in ("acc-1", "acc-3"))
    assert (a3["status"], a3["tier"], a3["tier_reason"]) == ("disqualified", "Excluded", "a public body, never prospected")
    assert (a1["status"], a1["industry"]) == ("verified", "Advertising agencies")  # untouched: the label check decides
    assert relabel.run(ctx)["changed"] == 0  # done once


def test_a_company_an_earlier_relabel_disqualified_on_the_rules_alone_is_put_back_for_the_label_check():
    ctx, t = world()
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
    ctx, t = world()
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
    ctx, t = world()
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
    ctx, t = world()
    home_fact(ctx, "acc-1", ctx.now - timedelta(days=1))  # read before the model was asked
    verdict_fact(ctx, "acc-1", "Advertising agencies")  # the model said so when it was checked
    out = relabel.run(ctx)
    a = ctx.store.get("accounts", account_id="acc-1")
    # The rules alone now give the agencies' own label; with the model sure of Advertising agencies, it stays.
    assert out["changed"] == 0 and (a["industry"], a.get("label_source")) == ("Advertising agencies", None)
    assert ctx.store.select("credit_ledger", {"job": labels.JOB}) == []


def test_an_approvers_label_never_moves():
    ctx, t = world()
    ctx.store.update("accounts", {"account_id": "acc-1"}, {"label_source": "approver"})
    assert relabel.run(ctx)["changed"] == 0
    assert ctx.store.get("accounts", account_id="acc-1")["industry"] == "Advertising agencies"


def test_a_relabelled_company_gets_the_rules_source_and_its_check_time():
    ctx, t = world()
    relabel.run(ctx)
    a = ctx.store.get("accounts", account_id="acc-1")
    assert (a["industry"], a["label_source"], a["label_checked_at"]) == (AGENCIES, "rules", ctx.now)
    assert labels.copy_level(a) == "group"


def test_a_stored_verdict_that_rules_a_company_out_disqualifies_it():
    ctx, t = world()
    verdict_fact(ctx, "acc-2", "none", entity="association")
    relabel.run(ctx)
    a = ctx.store.get("accounts", account_id="acc-2")
    assert (a["status"], a["tier"]) == ("disqualified", "Excluded")


def test_labels_set_corrects_a_company_and_show_tells_the_story(capsys):
    from us_outbound.ops import cli

    ctx, t = world()
    sheet = FakeOverrides(t)
    at(ctx, ctx.now, live=False, job="labels_set")
    dry = relabel.set_label(ctx, "https://www.loopstudio.com/", "fintech")
    assert dry == {"dry_run": True, "domain": "loopstudio.com", "from": "Advertising agencies", "to": "Fintech",
                   "active": True}
    assert ctx.store.get("accounts", account_id="acc-3")["industry"] == "Advertising agencies"
    at(ctx, ctx.now, live=True, job="labels_set")
    out = relabel.set_label(ctx, "loopstudio.com", "fintech")
    assert out["sheet"] == "added" and "cards_withdrawn" not in out
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

    ctx, t = world()
    sdk = LabelSDK({"acmecreative.com": {"label": "Fintech", "evidence": "advertising agency"},
                    "brightfin.com": {"label": "Fintech", "evidence": "fintech"}})
    ctx.clients.claude_sdk = sdk
    fact(ctx, "acc-1", "keywords", ["advertising agency"])
    fact(ctx, "acc-2", "description", "Brightfin builds fintech for shops.")
    at(ctx, ctx.now, live=live, job=relabel.AUDIT_JOB)
    return ctx, t, sdk


def test_a_dry_audit_counts_prices_and_shows_a_prompt_and_asks_nothing():
    ctx, t, sdk = audit_world(live=False)
    out = relabel.audit(ctx)
    assert (out["to_check"], out["open_accounts"], out["stale"]) == (2, 3, 0)  # acc-3 has nothing to check against
    assert 0.005 < out["per_call_usd"] < 0.03 and out["most_usd"] == round(out["per_call_usd"] * 2, 2)
    assert out["sample_prompt"].startswith("<company>\nName: ") and sdk.calls == []
    assert out["stored"]["dry_run"] is True


def test_a_live_audit_asks_and_decides(capsys):
    from us_outbound.ops import cli

    ctx, t, sdk = audit_world()
    out = relabel.audit(ctx)
    assert out["asked"] == 2 and out["usd"] > 0 and out["unavailable"] == ""
    a1 = ctx.store.get("accounts", account_id="acc-1")
    # The rules say Advertising agencies (its own keyword); the model is sure it is Fintech, from Apollo's facts alone
    # (the home page said nothing): Fintech's group, with its group's copy.
    assert (a1["industry"], a1["label_source"]) == ("Technology & Startups", "umbrella")
    assert out["disagreements"][0]["domain"] == "acmecreative.com"
    # Brightfin too: the model is sure of Fintech, but from Apollo's facts alone, so the group's copy until its page
    # is read (11 Oct 2026: the rules agreeing no longer counts).
    a2 = ctx.store.get("accounts", account_id="acc-2")
    assert (a2["industry"], a2["label_source"]) == ("Technology & Startups", "umbrella")
    assert out["decisions"] == {"umbrella": 2}
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

    ctx, t, sdk = audit_world()
    fact(ctx, "acc-3", "apollo_industry", "design services")  # no rules label
    sdk.answers["loopstudio.com"] = {"label": TECH, "confidence": "low"}
    monkeypatch.setattr(pages, "home_pass", lambda *a, **k: {})  # the pass's time ran out before these pages
    first = relabel.audit(ctx)
    monkeypatch.undo()
    # Unsure, the model's label still decides (11 Oct 2026): its group's own label and copy, not the hand-check.
    assert "held" not in first["decisions"]
    a3 = ctx.store.get("accounts", account_id="acc-3")
    assert (a3["industry"], a3["label_source"]) == (TECH, "umbrella")

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
    # Every company asked without its page gets it read and is asked again (11 Oct 2026).
    assert read == ["acmecreative.com", "brightfin.com", "loopstudio.com"]
    assert second["home_pages"]["said_what_they_do"] == 3
    assert second["asked"] == 3 and len(sdk.calls) == calls + 3
    assert "Home page: Loop Studio · Loop makes design software." in sdk.calls[-1]["messages"][0]["content"]
    a3 = ctx.store.get("accounts", account_id="acc-3")
    assert (a3["industry"], a3["label_source"]) == (TECH, "model")
    cli._audit_report(second, True)
    assert "Read the home page of 3 companies whose label the page could settle: 3 say what they do, 0 say nothing, 0 refused, " \
           "0 did not answer. Those with a page are asked again." in capsys.readouterr().out
    # Asked with its page: the verdict stands, and the page is not read again.
    at(ctx, ctx.now + timedelta(days=1), live=True, job=relabel.AUDIT_JOB)
    third = relabel.audit(ctx)
    assert third["to_check"] == 0 and third["home_pages"] == {} and len(read) == 3


def test_an_overrides_row_the_columns_lag_is_applied():
    """relabel puts an Overrides row's label in the columns when they lag it (9 Oct 2026, defect 5), as verify does."""
    ctx, t = world()
    a = ctx.store.get("accounts", account_id="acc-2")
    override = Override(a["domain"], "industry", "Edtech")
    ctx.settings = dataclasses.replace(ctx.settings, overrides=(override,))
    at(ctx, ctx.now, live=True, job="relabel")
    relabel.run(ctx)
    b = ctx.store.get("accounts", account_id="acc-2")
    assert (b["industry"], b["label_source"]) == ("Edtech", "override")
