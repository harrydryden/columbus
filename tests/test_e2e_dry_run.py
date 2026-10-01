"""SPEC 14 phase 0 acceptance, end to end in dry-run, through the production wiring:

  * "One test record flows end to end in dry-run": settings_sync -> an account and its facts
    -> score -> a contact -> enrol, every job run by the CLI (`us-outbound dry-run <job>`)
    with bootstrap.build_context and ops.heartbeat.run_job, as the Railway scheduler runs them.
  * "Changing a weight in the sheet changes a test account's score after the next sync."
  * "A bad row in the sheet is rejected, with a Slack message."

Only the outside world is faked: the Sheets, Slack, HubSpot and Instantly APIs answer on a
FakeTransport, and the Postgres database is a MemoryStore. The Sheets API serves default_tabs() with the
phase-0 fills (ids, postal address, privacy link, approved copy, Active mailboxes).

The flow runs on Tue 29 Sep 2026. The test account's raw score (80 with the design review's
Appendix A weights, 1 Oct 2026) is not cut by score_cap, and a 25-point weight change shows as 25 points.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes import FakeTransport
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.context import Secrets
from us_outbound.enrol.enrol import iso_week
from us_outbound.logs import hash_email
from us_outbound.ops import bootstrap, cli
from us_outbound.scoring.score import score_account
from us_outbound.settings.defaults import COLUMNS, default_tabs
from us_outbound.settings.model import TABS
from us_outbound.settings.sync import load_current
from us_outbound.settings.validate import validate_all

NOW = datetime(2026, 9, 29, 11, 0, tzinfo=UTC)  # 12:00 UK, 07:00 ET: enrol's slot (SPEC 9)
SHEET_ID = "sheet-e2e"
# What the Railway service has (docs/railway-setup.md); the store and credentials are injected below.
ENV = {"DATABASE_URL": "postgresql://us_outbound@db.test:5432/railway", "US_OUTBOUND_SETTINGS_SHEET_ID": SHEET_ID}
CREDS = SimpleNamespace(valid=True, token="test-token")  # google-auth credentials stand-in

HUBSPOT, INSTANTLY, SLACK = "https://api.hubapi.com", "https://api.instantly.ai", "https://slack.com/api/"
PREFIX = "US Outbound – "
CHANNELS = [{"id": "C_ALERT", "name": "us-outbound"}, {"id": "C_DEV", "name": "us-outbound-dev"}]
OWNERS = ("Hannah Spalding", "Sam Jackson", "Harry Dryden")

ACCOUNT = {
    "account_id": "acc-acme",
    "domain": "acmecreative.com",
    "clean_name": "Acme Creative",
    "legal_name": "Acme Creative LLC",
    "hq_city": "Chicago",
    "hq_state": "IL",
    "employees": 64,
    "size_band": "50-99",
    "industry": "Advertising agencies",
    "industry_group": "Marketing & Creative Agencies",
    "source": "apollo",
    "status": "verified",
    "first_seen": NOW - timedelta(days=10),
}
JANE = "jane.doe@acmecreative.com"
CONTACT = {
    "contact_id": "con-jane",
    "account_id": "acc-acme",
    "role": "People leader",
    "title": "Head of People",
    "first_name": "Jane",
    "last_name": "Doe",
    "email": JANE,
    "email_sha256": hash_email(JANE),
    "email_status": "verified",
    "email_source": "apollo",
    "person_state": "IL",
    "created_at": NOW - timedelta(days=2),
}
EAP_QUOTE = "Our Employee Assistance Program (EAP) through ComPsych offers free, confidential support."


def facts() -> list[dict]:
    seen = NOW - timedelta(days=3)

    def fact(n, source, name, value, quote="", url=""):
        return {"event_id": f"f{n}", "account_id": ACCOUNT["account_id"], "source": source, "fact": name,
                "value": value, "quote": quote, "source_url": url, "observed_at": seen}

    return [
        fact(1, "clay_careers", "read_status", "read"),
        fact(2, "clay_careers", "benefit", {"item": "mental health days"},
             "Everyone gets mental health days each quarter.", "https://acmecreative.com/careers"),
        fact(3, "clay_careers", "mental_health_provision", {"type": "eap", "provider": "ComPsych"},
             EAP_QUOTE, "https://acmecreative.com/benefits"),
        fact(4, "clay_careers", "values_page", True, url="https://acmecreative.com/values"),
        fact(5, "apollo_people", "people_leader_days_in_title", 45),
        fact(6, "apollo_people", "people_leader_count", 1),
        fact(7, "apollo_org", "open_roles", 4),
    ]


# -- the settings sheet: SPEC 5 defaults plus the phase-0 fills ---------------------------------

PHASE0_GENERAL = {
    "hubspot_pipeline_id": "pipe-spill3",
    "hubspot_deal_stage": "Discovery",
    "hubspot_deal_stage_id": "stage-first",
    "hubspot_owner_id": "owner-harry",
    "clay_accounts_function_id": "fn-us-accounts",
    "clay_contacts_function_id": "fn-us-contacts",
    "clay_monthly_credits": "2000",
    "clay_credits_per_account": "5",
    "approver_slack_ids": "U_HARRY",
}
PROOF = "Creative agencies in the UK use Spill so their teams get same-day support (a UK example)."


def phase0_sheet() -> dict[str, list[dict[str, str]]]:
    tabs = default_tabs()
    for row in tabs["General"]:
        row["value"] = PHASE0_GENERAL.get(row["key"], row["value"])
    for row in tabs["Signals"]:
        if row["signal"] == "EAP named":
            row["opener"] = "Saw your benefits page mentions {evidence}."  # SPEC 5's example opener
    for row in tabs["Industries"]:
        if row["industry"] == "Advertising agencies":
            row["proof_point"] = PROOF
    from us_outbound.enrol.copy_desk import row_from_dict

    for row in tabs["Copy"]:
        row.update(status="approved", approved_by="Harry Dryden")
        row["qa"] = f"pass {row_from_dict(row).content_hash()}"  # as `us-outbound copy qa` writes it
    for row in tabs["Mailboxes"]:
        row.update(status="Active", instantly_account_id="acct-" + row["address"].split("@")[0] + "-" + row["domain"],
                   added_on="2026-09-01")
    return tabs


def signal_row(tabs: dict, name: str) -> tuple[int, dict]:
    """(sheet row number, row) of a Signals-tab row; the header is row 1."""
    i, row = next((i, r) for i, r in enumerate(tabs["Signals"]) if r["signal"] == name)
    return i + 2, row


# -- the fake outside world ---------------------------------------------------------------------


class World:
    """A CLI context factory: production bootstrap over one MemoryStore and one FakeTransport."""

    def __init__(self):
        self.sheet = phase0_sheet()
        self.store = MemoryStore(Guard())
        self.transport = FakeTransport()
        self.contexts: list[Any] = []
        self.add_leads_calls: list[tuple[str, list[dict]]] = []
        self._routes()

    def _grid(self, tab: str) -> dict:
        return {"values": [COLUMNS[tab]] + [[row.get(c, "") for c in COLUMNS[tab]] for row in self.sheet[tab]]}

    def _routes(self) -> None:
        t = self.transport
        t.route("GET", "values:batchGet",
                fn=lambda r: {"valueRanges": [self._grid(a.strip("'")) for a in r.params["ranges"]]})
        t.route("GET", "conversations.list", {"ok": True, "channels": CHANNELS})
        t.route("POST", "chat.postMessage", fn=lambda r: {"ok": True, "channel": r.json["channel"], "ts": "1.1"})
        t.route("GET", f"{INSTANTLY}/api/v2/campaigns", {"items": [
            *({"id": f"cmp-{i}", "name": PREFIX + owner} for i, owner in enumerate(OWNERS)),
            {"id": "cmp-eu", "name": "EU Outbound – Anna"},
        ]})
        # HubSpot re-check at enrollment: nothing about Acme in HubSpot (the searches answer empty).
        for obj in ("companies", "contacts", "deals"):
            t.route("POST", f"/crm/v3/objects/{obj}/search", {"results": []})

    def __call__(self, job: str, live_flag: bool, operator: bool = False):
        secrets = Secrets(Guard(), fetch=lambda name: f"test-{name}")
        ctx = bootstrap.build_context(job, live_flag, operator=operator, env=ENV, store=self.store,
                                      transport=self.transport, secrets=secrets, google_credentials=CREDS)
        secrets.guard = ctx.guard
        ctx = dataclasses.replace(ctx, now=NOW + timedelta(minutes=len(self.contexts)))  # each run a minute later
        if job == "enrol":
            inst = ctx.clients.instantly
            real = inst.add_leads

            def spy(name, leads):
                self.add_leads_calls.append((name, copy.deepcopy(list(leads))))
                return real(name, leads)

            inst.add_leads = spy
        self.contexts.append(ctx)
        return ctx

    def run(self, *argv: str) -> dict:
        """Run one CLI command; returns what happened on the wire and in the store during it."""
        before = len(self.transport.requests)
        code = cli.main(list(argv), context_factory=self)
        assert code == 0, f"`us-outbound {' '.join(argv)}` exited {code}"
        ctx = self.contexts[-1]
        beat = self.store.get("heartbeats", run_id=ctx.run_id)  # one row per run, upserted by run_id
        return {"ctx": ctx, "requests": self.transport.requests[before:], "heartbeat": beat}

    def account(self) -> dict:
        return self.store.get("accounts", account_id=ACCOUNT["account_id"])


@pytest.fixture(scope="module")
def flow() -> dict:
    """The whole phase-0 flow, stage by stage; each test below checks one stage."""
    w = World()
    out: dict[str, Any] = {"world": w}
    assert not any(validate_all(w.sheet)[1].values()), "the phase-0 sheet must validate"

    out["sync1"] = w.run("dry-run", "settings_sync")

    # What source_universe and verify_in_clay (phase 1) will write: the account and its facts.
    w.store.insert("accounts", [dict(ACCOUNT)])
    w.store.insert("signal_events", facts())
    out["score"] = w.run("dry-run", "score")
    out["scored"] = dict(w.account())

    # What pick_contacts (phase 2) will write, and Harry's approval of this week's hand-check.
    w.store.insert("contacts", [dict(CONTACT)])
    w.store.insert("hitl_items", [{"item_id": "hc-1", "kind": "hand_check", "status": "handled",
                                   "created_at": NOW - timedelta(hours=3),
                                   "payload": {"iso_week": iso_week(NOW.date()), "pulled_account_ids": []}}])
    out["enrol"] = w.run("dry-run", "enrol")
    out["after_enrol"] = (dict(w.account()), dict(w.store.get("contacts", contact_id=CONTACT["contact_id"])))

    _, row = signal_row(w.sheet, "New People leader")
    assert row["weight"] == "30"
    row["weight"] = "5"
    out["sync2"] = w.run("dry-run", "settings_sync")
    out["reweighted"] = dict(w.account())

    out["bad_row"], bad = signal_row(w.sheet, "Q4 plan-year window")
    bad["weight"] = "ten"
    out["sync3"] = w.run("dry-run", "settings_sync")
    out["kept"], _ = load_current(w.store)
    return out


# -- one record end to end ------------------------------------------------------------------------


def test_first_sync_versions_every_tab_and_sends_nothing(flow):
    s = flow["sync1"]
    detail = s["heartbeat"]["detail"]
    assert detail["rejected"] == [] and detail["unusable"] == [] and detail["alerted"] is False
    assert {t for t, v in detail["tabs"].items() if v["status"] == "synced"} == set(TABS) - {"Overrides", "Named accounts"}  # empty tabs
    assert [r for r in s["requests"] if r.method != "GET"] == []
    settings, errors = load_current(flow["world"].store)
    assert settings is not None and not any(errors.values())
    assert settings.general.approver_slack_ids == (PHASE0_GENERAL["approver_slack_ids"],)


def test_the_account_scores_priority_with_the_eap_angle_and_evidence(flow):
    a = flow["scored"]
    # Mental health support 15, EAP named 5, Progressive benefits 5 (mental health days), People leader
    # in place 10, New People leader 30, Hiring and growth 15; the values page no longer scores.
    assert (a["score"], a["tier"], a["angle"]) == (80, "Priority", "Upgrade the EAP")
    assert "capped" not in a["tier_reason"] and "New People leader (+30)" in a["tier_reason"]
    matched = [e["value"]["signal"] for e in flow["world"].store.select("signal_events", {"source": "scoring"})]
    assert "EAP named" in matched and "New People leader" in matched
    settings, _ = load_current(flow["world"].store)
    events = flow["world"].store.select("signal_events", {"account_id": a["account_id"]})
    r = score_account(a, events, settings, NOW.date())
    assert r.opener == "Saw your benefits page mentions EAP."  # the EAP named opener, filled with its evidence


def test_enrol_renders_four_compliant_steps_for_the_senders_campaign(flow):
    e = flow["enrol"]
    detail = e["heartbeat"]["detail"]
    assert detail["status"] == "ok" and detail["dry_run"] is True
    assert detail["prepared"] == 1 and detail["enrolled"] == 0 and detail["skipped_accounts"] == []
    [(campaign, [lead])] = flow["world"].add_leads_calls
    owner = campaign.removeprefix(PREFIX)
    assert campaign.startswith(PREFIX) and owner in OWNERS and detail["by_owner"] == {owner: 1}
    assert lead["email"] == JANE and lead["company_name"] == "Acme Creative"

    ctx = e["ctx"]
    s, g = ctx.settings, ctx.settings.general
    cv = lead["custom_variables"]
    assert sorted(cv) == sorted(f"s{i}_{p}" for i in (1, 2, 3, 4) for p in ("subject", "body"))
    for step in (1, 2, 3, 4):
        subject, body = cv[f"s{step}_subject"], cv[f"s{step}_body"]
        assert subject and "{{" not in subject + body, step
        assert body.startswith("<p>Hi Jane,</p>"), step  # html, the default email_format
        # Email 1 links the industry page; emails 2 to 4 have one call to action, the demo page.
        assert body.count(f'<a href="{g.booking_page}">') == (0 if step == 1 else 1), step
        # The signature; the opt-out is Instantly's unsubscribe link in the campaign template (Harry, 1 Oct 2026).
        assert f'Book a call <a href="{g.booking_link}">here</a>' in body and "{{unsubscribe}}" not in body
    assert "Saw your benefits page mentions EAP." in cv["s1_body"]
    assert "Where we got your details" in cv["s1_body"]  # SPEC 10: Article 14 on email 1
    page = s.industry("Advertising agencies").landing_page_url
    assert f'<a href="{page}">' in cv["s1_body"] and "<strong>What is Spill?</strong>" in cv["s2_body"]

    refused = [c for c in ctx.guard.calls if c.system == "instantly" and c.write]
    assert refused and all(c.action == "lead.add" and c.target == campaign and not c.sent for c in refused)


def test_enrol_in_dry_run_writes_nothing_outside_the_database(flow):
    e = flow["enrol"]
    reqs = e["requests"]
    assert [r for r in reqs if r.url.startswith(INSTANTLY) and r.method != "GET"] == []
    # HubSpot is only searched (its search API is POST); nothing is created or changed there.
    assert [r for r in reqs if r.url.startswith(HUBSPOT) and not r.url.endswith("/search")] == []
    assert [r for r in reqs if r.url.startswith(HUBSPOT)], "enrol re-checks HubSpot before enrolling"
    assert [r for r in reqs if r.url.startswith(SLACK) and r.method != "GET"] == []
    guard = e["ctx"].guard
    for system in ("hubspot", "instantly", "sheets", "slack"):
        assert guard.writes(system, sent=True) == []
    account, contact = flow["after_enrol"]
    assert account["status"] == "verified" and not account.get("sender")
    assert not contact.get("enrolment_month") and not contact.get("instantly_lead_id")


def test_every_job_left_a_heartbeat(flow):
    beats = flow["world"].store.tables["heartbeats"]
    for stage, job in (("sync1", "settings_sync"), ("score", "score"), ("enrol", "enrol"),
                       ("sync2", "settings_sync"), ("sync3", "settings_sync")):
        hb = flow[stage]["heartbeat"]
        assert (hb["job"], hb["status"], hb["dry_run"]) == (job, "ok", True), stage
        assert hb["finished_at"] and hb["error"] is None
    assert len(beats) == 5 and all(b["status"] == "ok" for b in beats)


# -- a weight changed in the sheet ------------------------------------------------------------------


def test_changing_a_weight_changes_the_score_after_the_next_sync(flow):
    detail = flow["sync2"]["heartbeat"]["detail"]
    assert detail["tabs"]["Signals"] == {"status": "synced", "added": 0, "changed": 1, "removed": 0, "unchanged": len(default_tabs()["Signals"]) - 1}
    assert detail["rescored"] is True
    before, after = flow["scored"], flow["reweighted"]
    assert before["score"] - after["score"] == 25
    assert after["tier"] == "Priority" and "New People leader (+5)" in after["tier_reason"]
    history = flow["world"].store.select("settings", {"tab": "Signals", "key": "New People leader"})
    assert sorted((r["values"]["weight"], r["effective_to"] is None) for r in history) == [("30", False), ("5", True)]


# -- a bad row in the sheet ---------------------------------------------------------------------------


def test_a_bad_row_is_rejected_and_the_previous_version_stays(flow):
    detail = flow["sync3"]["heartbeat"]["detail"]
    assert detail["rejected"] == ["Signals"] and detail["unusable"] == []
    assert detail["tabs"]["Signals"]["status"] == "kept_previous" and detail["alerted"] is True
    kept = flow["kept"]
    assert kept is not None
    weights = {s.signal: s.weight for s in kept.signals}
    assert weights["Q4 plan-year window"] == 10 and weights["New People leader"] == 5
    assert flow["world"].account()["score"] == flow["reweighted"]["score"]


def test_the_bad_row_is_reported_once_in_the_dev_channel(flow):
    posts = [r for r in flow["sync3"]["requests"] if r.url == SLACK + "chat.postMessage"]
    assert len(posts) == 1
    [post] = posts
    assert post.json["channel"] == "C_DEV"  # dry-run: the alert goes to #us-outbound-dev
    text = post.json["text"]
    assert text.startswith("[dry-run → #us-outbound] ")
    assert f"Signals row {flow['bad_row']} (Q4 plan-year window), weight:" in text
    assert "previous version stays in force" in text
