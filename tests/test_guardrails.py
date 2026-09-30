"""SPEC 14 phase 0 acceptance: "A test that inspects every client call finds no write outside
the allowed containers" (SPEC 1.2 isolation, 1.8 tracking, 0.3 dry-run).

  (a) Every public method of every client class (found by introspection) is called with
      plausible arguments, LIVE, against a FakeTransport, with a guard whose boundaries are
      the test registry. Every write the guard recorded must pass ALLOWLIST below, which
      restates SPEC 1.2 as data independently of guard.py, and every non-GET request must
      pair with a call the guard authorized. A client method missing from EXERCISES fails.
  (b) Each disallowed write raises GuardViolation before any request is made.
  (c) Static: only clients/http.py imports an HTTP library; only clients/bq.py and ops/ddl.py
      import BigQuery; only clients/claude.py imports anthropic.
  (d) The same exercise in dry-run makes no write request to HubSpot, Instantly or the sheet,
      and Slack posts only to the dev channel.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import us_outbound.clients
from tests.fakes import FakeTransport
from us_outbound.clients.apollo import Apollo
from us_outbound.clients.bq import BigQueryStore, MemoryStore
from us_outbound.clients.claude import Claude
from us_outbound.clients.clay import Clay
from us_outbound.clients.guard import Boundaries, CallRecord, Guard, GuardViolation, Op
from us_outbound.clients.hubspot import HubSpot
from us_outbound.clients.http import HttpClient
from us_outbound.clients.instantly import Instantly
from us_outbound.clients.public import Public
from us_outbound.clients.sheets import Sheets
from us_outbound.clients.slack import Slack
from us_outbound.logs import redact

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]

# -- the test registry -----------------------------------------------------------------------

ADDRESSES = ("hannah@meetspill.org", "sam@meetspill.org", "harry@meetspill.org", "harry@tryspill.org")
OWNERS = ("Hannah Spalding", "Sam Jackson", "Harry Dryden")
PIPELINE, FIRST_STAGE = "pipe-spill3", "stage-first"
CLAY_FUNCTIONS = ("fn-us-accounts", "fn-us-contacts")
SHEET = "sheet-test"
ALERT, DEV = "#us-outbound", "#us-outbound-dev"
ESCALATION = "harry@spill.chat"  # SPEC 11: reply items are forwarded only here
PROJECT = "test-project"

BOUNDS = Boundaries(
    registry_addresses=frozenset(ADDRESSES),
    registry_owners=frozenset(OWNERS),
    hubspot_pipeline_id=PIPELINE,
    hubspot_deal_stage_id=FIRST_STAGE,
    clay_function_ids=frozenset(CLAY_FUNCTIONS),
    settings_sheet_id=SHEET,
    alert_channel=ALERT,
    dev_channel=DEV,
    escalation_email=ESCALATION,
)

# -- SPEC 1.2 restated as data (deliberately not imported from guard.py) ----------------------

PREFIX = "US Outbound – "  # "US Outbound – ", en dash (SPEC 9)
HS_COMPANY_PROPS = {"us_outbound_account_id", "us_outbound_tier", "us_outbound_industry_group", "us_outbound_top_signals"}
HS_CONTACT_PROPS = {"us_outbound_angle", "us_outbound_reply_class"}
HS_EMPTY_ONLY = {"hubspot_owner_id", "lifecyclestage", "hs_lead_status"}
HS_IDENTITY_ON_CREATE = {"company": {"name", "domain"}, "contact": {"email", "firstname", "lastname", "jobtitle"}}
HS_SIX = {"company": HS_COMPANY_PROPS, "contact": HS_CONTACT_PROPS}
HS_DEAL_PROPS = {"dealname", "pipeline", "dealstage", "hubspot_owner_id"}
HS_WARM = {"positive", "referral"}
HS_FREE_WRITES = {"note.create", "task.create", "association.create", "communication.unsubscribe"}
SLACK_WRITES = {"chat.postMessage", "chat.update"}
SETTINGS_TITLE = "US Outbound – Settings"
BQ_DATASET = "us_outbound"
REGISTRY = set(redact(sorted(ADDRESSES)))  # CallRecord details carry hashed addresses (logs.redact)
REDACTED_EMAIL = re.compile(r"^email:[0-9a-f]{16}$")

# Reads that go over POST, by system: (action, URL path suffix). Anything else not GET is a write.
READS_OVER_POST = {
    "hubspot": {("company.search", "/search"), ("contact.search", "/search"), ("deal.search", "/search")},
    "instantly": {("lead.list", "/leads/list"), ("warmup.analytics", "/accounts/warmup-analytics")},
    "apollo": {
        ("usage.credits", "/usage_stats/credit_usage_stats"),
        ("organizations.search", "/mixed_companies/search"),
        ("people.search", "/mixed_people/api_search"),
        ("people.bulk_match", "/people/bulk_match"),
    },
    "clay": {("function.run", "/run")},  # SPEC 8: only the two US Outbound functions (checked below)
}
HOSTS = {
    "api.hubapi.com": "hubspot",
    "slack.com": "slack",
    "sheets.googleapis.com": "sheets",
    "api.instantly.ai": "instantly",
    "api.apollo.io": "apollo",
    "api.clay.com": "clay",
}


def spec_violation(rec: CallRecord) -> str | None:
    """Why this recorded call breaks SPEC 1.2, or None. Reads are judged only where SPEC limits them."""
    s, a, t, d = rec.system, rec.action, rec.target, dict(rec.detail)
    accounts = set(d.get("accounts") or ())
    in_registry = bool(accounts) and accounts <= REGISTRY
    if s == "instantly":
        if not rec.write:
            if a in {"email.list", "email.get", "account.get", "account.list", "warmup.analytics"}:
                return None if in_registry else f"Instantly {a} not filtered by registry accounts"
            if a in {"lead.list", "lead.get", "campaign.get", "campaign.steps_analytics"}:
                return None if t.startswith(PREFIX) else f"Instantly {a} on {t!r}"
            if a == "campaign.list":
                return None if str(d.get("search", "")).startswith(PREFIX.strip()) else "unfiltered campaign list"
            return f"Instantly read {a} is not one SPEC 1.2 allows"
        if a == "campaign.create":
            if not t.startswith(PREFIX) or t[len(PREFIX):] not in OWNERS or not in_registry:
                return f"Instantly campaign.create {t!r} outside the registry"
            return None
        if a in {"campaign.update", "campaign.pause", "campaign.activate"}:
            if not t.startswith(PREFIX) or ("accounts" in d and not in_registry):
                return f"Instantly {a} on {t!r}"
            return None
        if a in {"lead.add", "lead.delete", "lead.update", "lead.stop"}:
            return None if t.startswith(PREFIX) else f"Instantly {a} on {t!r}"
        if a == "email.forward" and set(d.get("to") or ()) != set(redact([ESCALATION])):
            return "Instantly forward to someone other than escalation_email"
        if a in {"email.reply", "email.forward", "account.warmup_enable", "account.pause", "account.resume"}:
            return None if in_registry else f"Instantly {a} from outside the registry"
        if a == "blocklist.add":
            entries = list(d.get("entries") or ())
            return None if entries and all(REDACTED_EMAIL.match(e) for e in entries) else "blocklist entry not an email"
        return f"Instantly write {a} is not allowed"
    if not rec.write:
        if s == "sheets":
            return None if t == SHEET else f"read of sheet {t!r}"
        return None
    if s == "hubspot":
        props = set(d.get("properties") or ())
        obj = a.split(".", 1)[0]
        if a in {"company.create", "contact.create"}:
            if d.get("reply_class") not in HS_WARM:
                return f"HubSpot {a} without a positive or referral reply"
            extra = props - HS_SIX[obj] - HS_EMPTY_ONLY - HS_IDENTITY_ON_CREATE[obj]
            return f"HubSpot {a} sets {sorted(extra)}" if extra else None
        if a in {"company.update", "contact.update"}:
            extra = props - HS_SIX[obj] - HS_EMPTY_ONLY
            current = dict(d.get("current") or {})
            filled = [p for p in props & HS_EMPTY_ONLY if p not in current or current[p] not in (None, "")]
            if extra or filled:
                return f"HubSpot {a} sets {sorted(extra)} / overwrites {sorted(filled)}"
            return None
        if a == "deal.create":
            ok = (
                d.get("pipeline") == PIPELINE
                and d.get("dealstage") == FIRST_STAGE
                and str(d.get("dealname", "")).startswith(PREFIX)
                and props <= HS_DEAL_PROPS
            )
            return None if ok else "HubSpot deal outside Spill 3.0's first stage"
        if a == "property_group.create":
            return None if t == "us_outbound" else f"property group {t!r}"
        if a == "property.create":
            return None if t in HS_COMPANY_PROPS | HS_CONTACT_PROPS else f"property {t!r}"
        if a == "contact.gdpr_delete":
            return None if d.get("erasure_request") is True else "GDPR delete outside an erasure request"
        return None if a in HS_FREE_WRITES else f"HubSpot write {a} is not allowed"
    if s == "slack":
        if a not in SLACK_WRITES or t not in {ALERT, DEV}:
            return f"Slack {a} to {t!r}"
        if rec.sent and not rec.live and t != DEV:
            return f"dry-run Slack post to {t!r}"
        return None
    if s == "sheets":
        if a == "spreadsheet.create":
            return None if d.get("title") == SETTINGS_TITLE else f"created sheet {d.get('title')!r}"
        return None if t == SHEET else f"write to sheet {t!r}"
    if s == "bq":
        return None if t.split(".")[0] == BQ_DATASET else f"BigQuery write to {t!r}"
    return f"{s} is never written to (SPEC 1.2)"  # apollo, clay, public, claude, secrets


# -- recording doubles -------------------------------------------------------------------------


class PairingGuard(Guard):
    """A Guard that remembers the call it just authorized, so a send can be paired with it."""

    def __init__(self, live: bool):
        super().__init__(live=live, bounds=BOUNDS)
        self.last: CallRecord | None = None

    def authorize(self, system: str, op: Op) -> bool:
        self.last = None
        send = super().authorize(system, op)
        self.last = self.calls[-1] if send else None
        return send


@dataclass
class PairingTransport(FakeTransport):
    """FakeTransport that pairs every request with the guard call authorized just before it."""

    guard: PairingGuard | None = None
    pairs: list[tuple[Any, CallRecord | None]] = field(default_factory=list)

    def send(self, method, url, **kw):
        rec, self.guard.last = self.guard.last, None
        resp = super().send(method, url, **kw)
        self.pairs.append((self.requests[-1], rec))
        return resp


class FakeBigQuery:
    """Stands in for google.cloud.bigquery.Client; each call must follow an authorized bq call."""

    def __init__(self, guard: Guard):
        self.guard = guard
        self.calls: list[tuple[str, str]] = []
        self.loads: dict[str, list[dict]] = {}
        self.unauthorized: list[tuple[str, str]] = []

    def _check(self, kind: str, text: str, write: bool) -> None:
        last = self.guard.calls[-1] if self.guard.calls else None
        if last is None or last.system != "bq" or not last.sent or (write and not last.write):
            self.unauthorized.append((kind, text))
        self.calls.append((kind, text))

    def query(self, sql, job_config=None, **_):
        self._check("query", sql, sql.split(None, 1)[0].upper() not in {"SELECT", "WITH"})
        return self

    def result(self):
        return []

    def load_table_from_json(self, rows, dest, job_config=None):
        self._check("load", dest, True)
        self.loads[dest] = list(rows)
        return self

    def delete_table(self, name, not_found_ok=False):
        self._check("delete_table", name, True)


class FakeSDK:
    """Stands in for anthropic.Anthropic; each create must follow an authorized claude call."""

    def __init__(self, guard: Guard):
        self.guard = guard
        self.calls: list[dict] = []
        self.unauthorized = 0
        self.messages = self

    def create(self, **kwargs):
        last = self.guard.calls[-1] if self.guard.calls else None
        if last is None or last.system != "claude" or not last.sent:
            self.unauthorized += 1
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=500, output_tokens=50, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0, cache_creation=None)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"class": "positive"}')],
                               stop_reason="end_turn", usage=usage)


# -- the world the exercise runs in ---------------------------------------------------------------

HANNAH_CAMPAIGN = PREFIX + "Hannah Spalding"
SAM_CAMPAIGN = PREFIX + "Sam Jackson"
EU_CAMPAIGN, EU_ID = "EU Outbound – Anna", "cmp-eu"
JANE = "jane.doe@acmecreative.com"
SCHEMA = {"type": "object", "properties": {"class": {"type": "string"}}, "required": ["class"],
          "additionalProperties": False}
STEPS = [{"subject": f"{{{{s{i}_subject}}}}", "body": f"{{{{s{i}_body}}}}"} for i in (1, 2, 3, 4)]
CHANNELS = {"channels": [{"id": "C_ALERT", "name": "us-outbound"}, {"id": "C_DEV", "name": "us-outbound-dev"}]}


def install_routes(t: FakeTransport) -> None:
    # HubSpot (create routes before the search routes: the latest matching route wins).
    t.route("POST", "/crm/v3/objects/companies", {"id": "c2"})
    t.route("POST", "/crm/v3/objects/contacts", {"id": "k2"})
    t.route("POST", "/crm/v3/objects/companies/search",
            {"results": [{"id": "c1", "properties": {"name": "Acme Creative", "domain": "acmecreative.com"}}]})
    t.route("POST", "/crm/v3/objects/contacts/search", {"results": [{"id": "k1", "properties": {"email": JANE}}]})
    t.route("POST", "/crm/v3/objects/deals/search", {"results": [{"id": "d1", "properties": {"hs_is_closed": "false"}}]})
    t.route("GET", "/crm/v3/objects/contacts/k1", {"properties": {"hs_lead_status": ""}})
    t.route("GET", "/crm/v3/pipelines/deals",
            {"results": [{"id": PIPELINE, "label": "Spill 3.0", "stages": [{"id": FIRST_STAGE, "displayOrder": 0}]}]})
    t.route("GET", "/crm/v3/owners/", {"results": [{"id": "owner-harry"}]})
    t.route("GET", "/crm/v3/properties/companies", {"results": []})
    t.route("GET", "/crm/v3/properties/companies/groups", {"results": []})
    # Slack.
    t.route("GET", "conversations.list", {"ok": True, **CHANNELS})
    t.route("POST", "chat.postMessage", fn=lambda r: {"ok": True, "channel": r.json["channel"], "ts": "1.1"})
    t.route("POST", "chat.update", fn=lambda r: {"ok": True, "channel": r.json["channel"], "ts": r.json["ts"]})
    t.route("GET", "conversations.replies", {"ok": True, "messages": [{"ts": "1.1"}, {"ts": "1.2", "text": "send"}]})
    # Sheets.
    t.route("POST", "sheets.googleapis.com/v4/spreadsheets", {"spreadsheetId": "new-sheet"})
    t.route("POST", ":append", {})
    t.route("GET", "values:batchGet", {"valueRanges": [{"values": [["key", "value"], ["live_sending", "no"]]}]})
    t.route("GET", "/values/", {"values": [["address", "status"], ["hannah@meetspill.org", "Warming"]]})
    # Instantly.
    t.route("GET", "api.instantly.ai/api/v2/accounts/",
            fn=lambda r: {"email": r.url.rsplit("/", 1)[1], "status": 1, "warmup_status": 1, "stat_warmup_score": 100})
    t.route("POST", "/accounts/warmup-analytics", {"aggregate_data": {}})
    t.route("GET", "api.instantly.ai/api/v2/campaigns",
            {"items": [{"id": "cmp-h", "name": HANNAH_CAMPAIGN}, {"id": EU_ID, "name": EU_CAMPAIGN}]})
    t.route("GET", "/campaigns/cmp-h", {"id": "cmp-h", "name": HANNAH_CAMPAIGN})
    t.route("GET", "/campaigns/analytics/steps", [])
    t.route("POST", "api.instantly.ai/api/v2/campaigns", {"id": "cmp-s", "name": SAM_CAMPAIGN, "status": 0})
    t.route("POST", "/leads/add", {"leads_uploaded": 1, "created_leads": [{"index": 0, "id": "L1", "email": JANE}]})
    t.route("POST", "/leads/list", {"items": [{"id": "L1", "campaign": "cmp-h", "email": JANE}]})
    t.route("GET", "/leads/L1", {"id": "L1", "campaign": "cmp-h"})
    t.route("GET", "api.instantly.ai/api/v2/emails",
            fn=lambda r: {"items": [{"id": "E1", "eaccount": r.params["eaccount"], "subject": "Re: hi"}]})
    t.route("GET", "/emails/E1", {"id": "E1", "eaccount": "hannah@meetspill.org", "subject": "Re: hi"})
    # Clay.
    t.route("POST", "api.clay.com/public/v0/routines/", {"routine_run_id": "R1"})
    t.route("GET", "/routines/run/R1/results", {"status": "complete", "results": [
        {"id": "1", "status": "complete", "output": {"clean_name": "Acme Creative"}},
        {"id": "2", "status": "complete", "output": {"email": JANE, "status": "valid"}},
    ]})
    # Public.
    t.route("GET", "boards-api.greenhouse.io", {"jobs": []})
    t.route("HEAD", "acmecreative.com", status=301, headers={"Location": "https://www.acmecreative.com/"})


@dataclass
class World:
    guard: PairingGuard
    transport: PairingTransport
    bq: FakeBigQuery
    sdk: FakeSDK
    tmp: Path
    clients: dict[str, Any]
    calls: Counter[str] = field(default_factory=Counter)

    def as_job(self, job: str, fn: Callable[[], Any]) -> Any:
        old = self.guard.job
        self.guard.configure(job=job)
        try:
            return fn()
        finally:
            self.guard.configure(job=old)


def make_world(live: bool, tmp: Path) -> World:
    guard = PairingGuard(live)
    t = PairingTransport(guard=guard)
    install_routes(t)
    bq = FakeBigQuery(guard)
    sdk = FakeSDK(guard)
    memory = MemoryStore(guard)
    memory.query_handlers["v_queue"] = lambda store, params: []
    clients = {
        "HubSpot": HubSpot(guard, t, "tok"),
        "Slack": Slack(guard, t, "tok"),
        "Sheets": Sheets(guard, t),
        "Instantly": Instantly(guard, t, "tok"),
        "Apollo": Apollo(guard, t, "tok"),
        "Clay": Clay(guard, t, "tok", sleep=lambda s: None),
        "Public": Public(guard, t),
        "Claude": Claude(guard, memory, api_key=None, model="claude-haiku-4-5", monthly_cap_usd=10.0, sdk=sdk),
        "MemoryStore": memory,
        "BigQueryStore": BigQueryStore(guard, PROJECT, "EU", client=bq),
    }
    return World(guard, t, bq, sdk, tmp, clients)


# -- every public client method, with plausible arguments ---------------------------------------

Ex = Callable[[Any, World], Any]
ACCOUNT_ROWS = [
    {"account_id": "acc-1", "domain": "acmecreative.com", "clean_name": "Acme Creative", "tier": "Priority"},
    {"account_id": "acc-2", "domain": "beta.com", "clean_name": "Beta"},  # a different column set
]


def _store_exercises() -> dict[str, Ex]:
    return {
        "insert": lambda s, w: s.insert("signal_events", [{"event_id": "e1", "account_id": "acc-1", "source": "apollo_org",
                                                          "fact": "open_roles", "value": 4, "observed_at": NOW}]),
        "upsert": lambda s, w: s.upsert("accounts", ACCOUNT_ROWS),
        "select": lambda s, w: s.select("accounts", {"domain": ["acmecreative.com"]}),
        "get": lambda s, w: s.get("accounts", account_id="acc-1"),
        "update": lambda s, w: s.update("accounts", {"account_id": "acc-1"}, {"score": 55}),
        "delete": lambda s, w: s.delete("signal_events", {"account_id": "acc-1", "source": "scoring"}),
        "query": lambda s, w: s.query(f"SELECT * FROM `{PROJECT}.us_outbound.v_queue`"),
    }


EXERCISES: dict[str, dict[str, Ex]] = {
    "HubSpot": {
        "find_pipeline": lambda c, w: c.find_pipeline("Spill 3.0"),
        "owner_id_for_email": lambda c, w: c.owner_id_for_email("harry@spill.chat"),
        "search_companies_by_domain": lambda c, w: c.search_companies_by_domain("acmecreative.com"),
        "search_contacts_by_email": lambda c, w: c.search_contacts_by_email(JANE),
        "open_deals_for_company": lambda c, w: c.open_deals_for_company("c1"),
        "properties": lambda c, w: c.properties("companies"),
        "property_groups": lambda c, w: c.property_groups("companies"),
        "iter_opted_out_or_bounced_emails": lambda c, w: list(c.iter_opted_out_or_bounced_emails()),
        "create_company": lambda c, w: c.create_company(
            {"name": "Acme Creative", "domain": "acmecreative.com", "us_outbound_account_id": "acc-1",
             "us_outbound_tier": "Priority", "us_outbound_industry_group": "Marketing & Creative Agencies"},
            reply_class="positive"),
        "update_company": lambda c, w: c.update_company(
            "c1", {"us_outbound_top_signals": "EAP named (ComPsych)", "lifecyclestage": "lead",
                   "hubspot_owner_id": "owner-harry"}, current={"lifecyclestage": "", "hubspot_owner_id": None}),
        "create_contact": lambda c, w: c.create_contact(
            {"email": JANE, "firstname": "Jane", "lastname": "Doe", "jobtitle": "Head of People",
             "us_outbound_angle": "Upgrade the EAP (eap-v1)"}, reply_class="referral"),
        "update_contact": lambda c, w: c.update_contact(
            "k1", {"us_outbound_reply_class": "positive", "hs_lead_status": "CONNECTED"}, current=None),
        "associate": lambda c, w: c.associate("contacts", "k1", "companies", "c1"),
        "create_note": lambda c, w: c.create_note("Reply: sounds good", [("companies", "c1"), ("contacts", "k1")], at=NOW),
        "create_task": lambda c, w: c.create_task("Reply from Jane", "Approve in Slack", "owner-harry", NOW,
                                                  [("companies", "c1")]),
        "create_deal": lambda c, w: c.create_deal(PREFIX + "Acme Creative", PIPELINE, FIRST_STAGE, "owner-harry", "c1"),
        "ensure_property_group": lambda c, w: c.ensure_property_group("companies"),
        "create_property": lambda c, w: c.create_property(
            "companies", {"name": "us_outbound_tier", "label": "US Outbound tier", "type": "string",
                          "fieldType": "text", "groupName": "us_outbound"}),
        "unsubscribe": lambda c, w: c.unsubscribe(JANE),
        "gdpr_delete_contact": lambda c, w: w.as_job("erase", lambda: c.gdpr_delete_contact(JANE)),
    },
    "Slack": {
        "channel_id": lambda c, w: c.channel_id(ALERT),
        "post": lambda c, w: (c.post(ALERT, "Positive reply · Acme Creative", blocks=[{"type": "section"}]),
                              c.post(DEV, "dev note")),
        "update": lambda c, w: (c.update(ALERT, "1.1", "Handled"), c.update(DEV, "1.1", "Handled")),
        "replies": lambda c, w: c.replies(ALERT, "1.1"),
    },
    "Sheets": {
        "read_tabs": lambda c, w: c.read_tabs(SHEET, ["General"]),
        "append_rows": lambda c, w: c.append_rows(SHEET, "Mailboxes", [{"address": "new@meetspill.org", "status": "Warming"}]),
        "update_cell": lambda c, w: c.update_cell(SHEET, "Mailboxes", {"address": "hannah@meetspill.org"}, "status", "Active"),
        "create_settings_sheet": lambda c, w: c.create_settings_sheet(
            {"General": [{"key": "live_sending", "value": "no"}]}, {"General": ["key", "value"]}),
    },
    "Instantly": {
        "list_accounts": lambda c, w: c.list_accounts(ADDRESSES),
        "warmup_status": lambda c, w: c.warmup_status(ADDRESSES),
        "enable_warmup": lambda c, w: c.enable_warmup(ADDRESSES),
        "list_campaigns": lambda c, w: c.list_campaigns(),
        "get_campaign": lambda c, w: c.get_campaign(HANNAH_CAMPAIGN),
        "create_campaign": lambda c, w: c.create_campaign(
            SAM_CAMPAIGN, accounts=["sam@meetspill.org"], daily_limit=30, steps=STEPS),
        "update_campaign": lambda c, w: c.update_campaign(HANNAH_CAMPAIGN, {"daily_limit": 30},
                                                          accounts=["hannah@meetspill.org"]),
        "pause_campaign": lambda c, w: c.pause_campaign(HANNAH_CAMPAIGN),
        "activate_campaign": lambda c, w: c.activate_campaign(HANNAH_CAMPAIGN),
        "add_leads": lambda c, w: c.add_leads(HANNAH_CAMPAIGN, [
            {"email": JANE, "first_name": "Jane", "last_name": "Doe", "company_name": "Acme Creative",
             "custom_variables": {"s1_subject": "Hi", "s1_body": "Hello Jane"}}]),
        "list_leads": lambda c, w: c.list_leads(HANNAH_CAMPAIGN),
        "delete_lead": lambda c, w: c.delete_lead(HANNAH_CAMPAIGN, "L1"),
        "list_emails": lambda c, w: c.list_emails(["hannah@meetspill.org"], since=NOW),
        "reply": lambda c, w: c.reply("hannah@meetspill.org", "E1", "Re: hi", "Thanks, Jane."),
        "forward": lambda c, w: c.forward("hannah@meetspill.org", "E1", "harry@spill.chat", "Waiting 24 hours."),
        "blocklist_add": lambda c, w: c.blocklist_add([JANE]),
        "step_analytics": lambda c, w: c.step_analytics(HANNAH_CAMPAIGN),
    },
    "Apollo": {
        "credit_usage": lambda c, w: c.credit_usage(),
        "search_organizations": lambda c, w: c.search_organizations({"organization_locations[]": ["Illinois, US"]}),
        "enrich_organization": lambda c, w: c.enrich_organization("acmecreative.com"),
        "search_people": lambda c, w: c.search_people({"person_titles[]": ["Head of People"]}),
        "bulk_match": lambda c, w: c.bulk_match([{"first_name": "Jane", "last_name": "Doe", "domain": "acmecreative.com"}]),
        "website_visitor_aggregates": lambda c, w: c.website_visitor_aggregates("spill.chat", ["org-1"]),
    },
    "Clay": {
        "run_function": lambda c, w: c.run_function(CLAY_FUNCTIONS[0], {"domain": "acmecreative.com"}),
        "run_function_batch": lambda c, w: c.run_function_batch(
            CLAY_FUNCTIONS[1], {"1": {"full_name": "Jane Doe"}, "2": {"full_name": "John Roe"}}),
        "write_import_csv": lambda c, w: c.write_import_csv([{"domain": "acmecreative.com"}], w.tmp / "import.csv"),
        "read_export_csv": lambda c, w: c.read_export_csv(w.tmp / "import.csv"),
    },
    "Public": {
        "get": lambda c, w: c.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs", params={"content": "true"}),
        "resolve_redirect": lambda c, w: c.resolve_redirect("https://acmecreative.com"),
    },
    "Claude": {
        "json": lambda c, w: c.json("Classify the reply.", "Sounds good", SCHEMA, now=NOW),
        "month_spend_usd": lambda c, w: c.month_spend_usd(NOW),
        "estimate_usd": lambda c, w: c.estimate_usd("Classify the reply.", "Sounds good", SCHEMA, 256),
    },
    "MemoryStore": _store_exercises(),
    "BigQueryStore": _store_exercises(),
}

CLIENT_CLASSES = {cls.__name__: cls for cls in (HubSpot, Slack, Sheets, Instantly, Apollo, Clay, Public, Claude,
                                                MemoryStore, BigQueryStore)}
# HttpClient's own plumbing: request() is the guarded path every method above goes through
# (the negative cases call it directly), and headers() builds auth headers.
PLUMBING = frozenset({"request", "headers"})


def public_methods(cls: type) -> set[str]:
    return {n for n, v in inspect.getmembers(cls) if not n.startswith("_") and callable(v)} - PLUMBING


def run_exercise(live: bool, tmp: Path) -> World:
    """Call every public method of every client; each step must call the method it is named for."""
    w = make_world(live, tmp)
    for name, client in w.clients.items():
        for method in public_methods(type(client)):
            fn = getattr(client, method)

            def counted(*a, _fn=fn, _key=f"{name}.{method}", **k):
                w.calls[_key] += 1
                return _fn(*a, **k)

            setattr(client, method, counted)
    for name, methods in EXERCISES.items():
        for method, ex in methods.items():
            ex(w.clients[name], w)
            assert w.calls[f"{name}.{method}"], f"the {name}.{method} exercise did not call {method}"
    return w


@pytest.fixture(scope="module")
def live_world(tmp_path_factory) -> World:
    return run_exercise(True, tmp_path_factory.mktemp("live"))


@pytest.fixture(scope="module")
def dry_world(tmp_path_factory) -> World:
    return run_exercise(False, tmp_path_factory.mktemp("dry"))


def _system_of(url: str) -> str:
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return HOSTS.get(host, "public")


def _path(url: str) -> str:
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1]


# -- (a) every client call, live ------------------------------------------------------------------


def test_every_client_class_and_method_is_exercised():
    """A new client class or method must be added to EXERCISES (and so to this acceptance test)."""
    found = {}
    for mod in pkgutil.iter_modules(us_outbound.clients.__path__):
        module = importlib.import_module(f"us_outbound.clients.{mod.name}")
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or cls is HttpClient or inspect.isabstract(cls):
                continue
            params = inspect.signature(cls.__init__).parameters
            if "guard" in params:
                found[name] = cls
    assert set(found) == set(CLIENT_CLASSES), "a client class is missing from CLIENT_CLASSES"
    for name, cls in CLIENT_CLASSES.items():
        methods = public_methods(cls)
        missing, stale = methods - set(EXERCISES[name]), set(EXERCISES[name]) - methods
        assert not missing, f"{name} methods not exercised by this test: {sorted(missing)}"
        assert not stale, f"{name} exercises for methods that no longer exist: {sorted(stale)}"


def test_live_writes_stay_inside_the_allowed_containers(live_world):
    w = live_world
    problems = [(c.system, c.action, c.target, why) for c in w.guard.calls if (why := spec_violation(c))]
    assert problems == []
    sent = Counter(c.system for c in w.guard.calls if c.write and c.sent)
    for system in ("hubspot", "instantly", "slack", "sheets", "bq"):
        assert sent[system], f"the exercise made no {system} write, so it proves nothing about {system}"
    assert sent["apollo"] == 0 and sent["public"] == 0 and sent["claude"] == 0


def test_clay_runs_only_the_two_us_outbound_functions(live_world):
    runs = [c for c in live_world.guard.calls if c.system == "clay" and c.action == "function.run"]
    assert runs and {c.target for c in runs} <= set(CLAY_FUNCTIONS)
    assert all(re.search(r"/routines/function:fn-us-(accounts|contacts)/run$", r.url)
               for r in live_world.transport.requests if r.url.endswith("/run"))


def test_every_request_pairs_with_an_authorized_call(live_world):
    for req, rec in live_world.transport.pairs:
        assert rec is not None and rec.sent, f"{req.method} {req.url} was sent without the guard authorizing it"
        assert rec.system == _system_of(req.url), (req.url, rec.system)
        if req.method in {"GET", "HEAD"}:
            continue
        if not rec.write:  # a read over POST: only the named search and list endpoints
            assert any(rec.action == a and _path(req.url).endswith(p) for a, p in READS_OVER_POST.get(rec.system, ())), (
                f"{req.method} {req.url} is labeled a read ({rec.action})"
            )


def test_instantly_touches_only_us_campaigns_and_registry_accounts(live_world):
    reqs = [r for r in live_world.transport.requests if _system_of(r.url) == "instantly"]
    assert reqs
    for r in reqs:
        blob = f"{r.url} {r.params} {r.json}"
        assert EU_ID not in blob and EU_CAMPAIGN not in blob, f"{r.method} {r.url} touched the EU campaign"
        assert _path(r.url) not in {"/api/v2/accounts", "/api/v2/leads"}, "an unfiltered list call"
        if _path(r.url) == "/api/v2/emails":
            assert r.params.get("eaccount") in ADDRESSES
        if _path(r.url).startswith("/api/v2/accounts/") and r.method == "GET":
            assert _path(r.url).rsplit("/", 1)[1] in ADDRESSES
        body = r.json if isinstance(r.json, dict) else {}
        for key in ("emails", "email_list"):
            assert set(body.get(key) or ()) <= set(ADDRESSES)
        assert body.get("open_tracking") in (None, False) and body.get("link_tracking") in (None, False)


def test_bigquery_writes_only_to_us_outbound(live_world):
    bq = live_world.bq
    assert bq.calls and bq.unauthorized == []
    for kind, text in bq.calls:
        names = re.findall(r"`([^`]+)`", text) if kind == "query" else [text]
        for name in names:
            parts = name.split(".")
            assert len(parts) == 3 and parts[0] == PROJECT and parts[1] == BQ_DATASET, f"{kind} touches {name}"


def test_bigquery_upsert_merges_each_column_set_separately(live_world):
    """A MERGE over the union of columns would NULL a column a row leaves out (foundation fix)."""
    bq = live_world.bq
    merges = [t for k, t in bq.calls if k == "query" and t.startswith("MERGE") and ".accounts`" in t.split(" USING")[0]]
    assert len(merges) == 2
    for sql in merges:
        stage = re.search(r"USING `([^`]+)`", sql).group(1)
        cols = {c for row in bq.loads[stage] for c in row}
        m = re.search(r"UPDATE SET (.*?) WHEN NOT MATCHED", sql)
        assert {s.split(" = ")[0].strip() for s in m.group(1).split(",")} == cols - {"account_id"}


def test_claude_is_called_only_after_the_guard(live_world):
    assert live_world.sdk.calls and live_world.sdk.unauthorized == 0
    [ledger] = live_world.clients["MemoryStore"].select("credit_ledger", {"system": "claude"})
    assert ledger["usd"] > 0


# -- (b) disallowed writes ------------------------------------------------------------------------


def _other_store(cls):
    class OtherDataset(cls):
        dataset = "spill_prod"

    return OtherDataset


NEGATIVE: dict[str, Callable[[World], Any]] = {
    # HubSpot (SPEC 1.2): only the six properties, notes, tasks, Spill 3.0 deals and the empty-only fields.
    "hubspot company.update of name": lambda w: w.clients["HubSpot"].update_company(
        "c1", {"name": "Acme"}, current={}),
    "hubspot lifecyclestage over a value": lambda w: w.clients["HubSpot"].update_company(
        "c1", {"lifecyclestage": "lead"}, current={"lifecyclestage": "customer"}),
    "hubspot hubspot_owner_id over a value": lambda w: w.clients["HubSpot"].update_contact(
        "k1", {"hubspot_owner_id": "owner-harry"}, current={"hubspot_owner_id": "owner-sally"}),
    "hubspot contact.update of phone": lambda w: w.clients["HubSpot"].update_contact("k1", {"phone": "1"}, current={}),
    "hubspot contact for an objection": lambda w: w.clients["HubSpot"].create_contact({"email": JANE}, reply_class="objection"),
    "hubspot company without a reply": lambda w: w.clients["HubSpot"].create_company({"name": "Acme"}, reply_class=None),
    "hubspot company.create of annualrevenue": lambda w: w.clients["HubSpot"].create_company(
        {"name": "Acme", "annualrevenue": "1"}, reply_class="positive"),
    "hubspot deal in another pipeline": lambda w: w.clients["HubSpot"].create_deal(
        PREFIX + "Acme", "pipe-sales", FIRST_STAGE, "owner-harry", "c1"),
    "hubspot deal at a later stage": lambda w: w.clients["HubSpot"].create_deal(
        PREFIX + "Acme", PIPELINE, "stage-won", "owner-harry", "c1"),
    "hubspot deal not named US Outbound": lambda w: w.clients["HubSpot"].create_deal(
        "Acme renewal", PIPELINE, FIRST_STAGE, "owner-harry", "c1"),
    "hubspot property of another team": lambda w: w.clients["HubSpot"].create_property(
        "companies", {"name": "annualrevenue", "groupName": "companyinformation"}),
    "hubspot gdpr delete outside erase": lambda w: w.as_job("enrol", lambda: w.clients["HubSpot"].gdpr_delete_contact(JANE)),
    "hubspot workflow edit": lambda w: w.clients["HubSpot"].request(
        "PATCH", "/automation/v4/flows/1", Op("workflow.update", target="flows", write=True)),
    "hubspot list create": lambda w: w.clients["HubSpot"].request(
        "POST", "/crm/v3/lists", Op("list.create", target="lists", write=True)),
    # Instantly (SPEC 1.2): only "US Outbound – {owner}" campaigns and the registry accounts.
    "instantly EU campaign create": lambda w: w.clients["Instantly"].create_campaign(
        EU_CAMPAIGN, accounts=["hannah@meetspill.org"], daily_limit=30, steps=STEPS),
    "instantly campaign for no registry owner": lambda w: w.clients["Instantly"].create_campaign(
        PREFIX + "Anna Other", accounts=["hannah@meetspill.org"], daily_limit=30, steps=STEPS),
    "instantly campaign with a non-registry account": lambda w: w.clients["Instantly"].create_campaign(
        SAM_CAMPAIGN, accounts=["anna@spill.eu"], daily_limit=30, steps=STEPS),
    "instantly open tracking on": lambda w: w.clients["Instantly"].create_campaign(
        SAM_CAMPAIGN, accounts=["sam@meetspill.org"], daily_limit=30, steps=STEPS, options={"open_tracking": True}),
    "instantly EU campaign update": lambda w: w.clients["Instantly"].update_campaign(EU_CAMPAIGN, {"daily_limit": 5}),
    "instantly EU campaign pause": lambda w: w.clients["Instantly"].pause_campaign(EU_CAMPAIGN),
    "instantly EU campaign activate": lambda w: w.clients["Instantly"].activate_campaign(EU_CAMPAIGN),
    "instantly EU leads add": lambda w: w.clients["Instantly"].add_leads(EU_CAMPAIGN, [{"email": JANE}]),
    "instantly EU lead delete": lambda w: w.clients["Instantly"].delete_lead(EU_CAMPAIGN, "L9"),
    "instantly EU leads list": lambda w: w.clients["Instantly"].list_leads(EU_CAMPAIGN),
    "instantly emails of a non-registry account": lambda w: w.clients["Instantly"].list_emails(["anna@spill.eu"]),
    "instantly emails unfiltered": lambda w: w.clients["Instantly"].list_emails([]),
    "instantly accounts outside the registry": lambda w: w.clients["Instantly"].list_accounts(["anna@spill.eu"]),
    "instantly warmup outside the registry": lambda w: w.clients["Instantly"].enable_warmup(["anna@spill.eu"]),
    "instantly reply from outside the registry": lambda w: w.clients["Instantly"].reply("anna@spill.eu", "E1", "Re", "Hi"),
    "instantly forward outside Spill": lambda w: w.clients["Instantly"].forward(
        "hannah@meetspill.org", "E1", "someone@other.com", "Waiting 24 hours."),
    "instantly workspace settings": lambda w: w.clients["Instantly"].request(
        "PATCH", "/workspaces/current", Op("workspace.update", target="workspace", write=True)),
    # Slack: only #us-outbound and #us-outbound-dev.
    "slack post to #general": lambda w: w.clients["Slack"].post("#general", "hello"),
    "slack update in #general": lambda w: w.clients["Slack"].update("#general", "1.1", "hello"),
    "slack channel create": lambda w: w.clients["Slack"].request(
        "POST", "conversations.create", Op("conversations.create", target=ALERT, write=True)),
    # Sheets: only the settings sheet.
    "sheets append to another sheet": lambda w: w.clients["Sheets"].append_rows("other-sheet", "General", [{"key": "x"}]),
    "sheets update another sheet": lambda w: w.clients["Sheets"].update_cell("other-sheet", "General", {"key": "x"}, "value", "y"),
    "sheets read another sheet": lambda w: w.clients["Sheets"].read_tabs("other-sheet", ["General"]),
    "sheets create another sheet": lambda w: w.clients["Sheets"].request(
        "POST", "", Op("spreadsheet.create", write=True, detail={"title": "Finance 2027"})),
    # Apollo: read only.
    "apollo contact save": lambda w: w.clients["Apollo"].request("POST", "/contacts", Op("contacts.create", write=True)),
    "apollo label (not an allowed read)": lambda w: w.clients["Apollo"].request("GET", "/labels", Op("labels.list")),
    # Clay: only the two US Outbound functions.
    "clay Work Email function": lambda w: w.clients["Clay"].run_function("t_0tk0v4lhJ895hhhhTHJ", {"domain": "a.com"}),
    "clay Company Latest Funding function": lambda w: w.clients["Clay"].run_function("t_0tk0v4ehpQ6WaeuCoQf", {}),
    "clay table write": lambda w: w.clients["Clay"].request("POST", "/tables/t1/rows", Op("table.write", write=True)),
    # Public sources: allowlisted GETs only.
    "public unlisted host": lambda w: w.clients["Public"].get("https://evil.example/jobs"),
    "public POST": lambda w: w.clients["Public"].request(
        "POST", "https://boards-api.greenhouse.io/x", Op("post", target="boards-api.greenhouse.io", write=True)),
    # BigQuery: dataset us_outbound only, and query() is read only.
    "bq memory store in another dataset": lambda w: _other_store(MemoryStore)(w.guard).insert("accounts", [{"account_id": "x"}]),
    "bq store in another dataset": lambda w: _other_store(BigQueryStore)(w.guard, PROJECT, "EU", client=w.bq).insert(
        "accounts", [{"account_id": "x"}]),
    "bq unknown table": lambda w: w.clients["MemoryStore"].insert("hubspot_contacts", [{"id": "x"}]),
    "bq DML through query": lambda w: w.clients["BigQueryStore"].query(f"DELETE FROM `{PROJECT}.us_outbound.accounts` WHERE TRUE"),
}


@pytest.mark.parametrize("case", sorted(NEGATIVE))
def test_disallowed_call_raises_before_any_request(case, tmp_path):
    w = make_world(True, tmp_path)
    with pytest.raises(GuardViolation):
        NEGATIVE[case](w)
    assert w.transport.requests == [], f"{case}: a request went out before the guard refused"
    assert w.bq.calls == [] and w.sdk.calls == []
    assert all(not c.sent for c in w.guard.calls if c.write), f"{case}: a write was recorded as sent"
    assert all(rows == [] for rows in w.clients["MemoryStore"].tables.values())


# -- (c) static: who may import what ----------------------------------------------------------------

IMPORT_RULES: list[tuple[str, set[str]]] = [
    # (module prefix, the only files that may import it)
    ("requests", {"us_outbound/clients/http.py"}),
    ("httpx", {"us_outbound/clients/http.py"}),
    ("urllib", {"us_outbound/clients/http.py"}),
    ("urllib3", {"us_outbound/clients/http.py"}),
    ("http.client", {"us_outbound/clients/http.py"}),
    ("aiohttp", set()),
    ("socket", set()),
    ("smtplib", set()),  # spill.chat never sends email itself (SPEC 1.4)
    ("google.cloud.bigquery", {"us_outbound/clients/bq.py", "us_outbound/ops/ddl.py"}),
    ("anthropic", {"us_outbound/clients/claude.py"}),
    # Credentials only, never data calls: Secret Manager reads keys; google-auth refreshes the Sheets token.
    ("google.cloud.secretmanager", {"us_outbound/context.py"}),
    ("google.auth", {"us_outbound/clients/sheets.py", "us_outbound/ops/bootstrap.py"}),
]


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out += [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in {"import_module", "__import__"} and isinstance(node.args[0].value, str):
                out.append(node.args[0].value)
    return out


def test_only_the_client_layer_imports_network_and_vendor_libraries():
    files = sorted((ROOT / "us_outbound").rglob("*.py"))
    assert files
    problems = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        for name in _imports(path):
            for prefix, allowed in IMPORT_RULES:
                if (name == prefix or name.startswith(prefix + ".")) and rel not in allowed:
                    problems.append(f"{rel} imports {name}")
    assert problems == []


# -- (d) the same exercise in dry-run ---------------------------------------------------------------


def test_dry_run_sends_no_writes_to_hubspot_instantly_or_the_sheet(dry_world):
    w = dry_world
    assert [c for c in w.guard.calls if spec_violation(c)] == []
    for system in ("hubspot", "instantly", "sheets"):
        assert w.guard.writes(system), f"the dry-run exercise attempted no {system} write"
        assert w.guard.writes(system, sent=True) == [], f"a {system} write was sent in dry-run"
    for req, rec in w.transport.pairs:
        system = _system_of(req.url)
        if system in {"hubspot", "instantly", "sheets"} and req.method not in {"GET", "HEAD"}:
            assert any(_path(req.url).endswith(p) for _, p in READS_OVER_POST.get(system, ())), (
                f"dry-run sent {req.method} {req.url}"
            )
            assert rec is not None and not rec.write


def test_dry_run_slack_posts_only_to_the_dev_channel(dry_world):
    w = dry_world
    posts = [r for r in w.transport.requests if _system_of(r.url) == "slack" and r.method == "POST"]
    assert posts, "the dry-run exercise posted nothing to Slack"
    assert {r.json["channel"] for r in posts} == {"C_DEV"}
    redirected = [r for r in posts if r.json["text"].startswith(f"[dry-run → {ALERT}] ")]
    assert redirected, "the alert-channel post was not redirected with its dry-run prefix"
    refused = [c for c in w.guard.calls if c.system == "slack" and c.write and not c.sent]
    assert refused and {c.target for c in refused} == {ALERT}


def test_dry_run_still_writes_bigquery(dry_world):
    """SPEC 0.3: dry-run computes, logs and writes to BigQuery."""
    assert dry_world.guard.writes("bq", sent=True)
    assert dry_world.bq.calls and dry_world.bq.unauthorized == []
