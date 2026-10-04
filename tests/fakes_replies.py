"""Test doubles for the reply jobs (replies/outcomes.py, replies/poll.py), built on tests.fakes.

ReplyWorld holds an Instantly, HubSpot and Slack that live in memory behind a FakeTransport, so
every call still goes through the real clients and the guard; and a Claude SDK that answers by
call (classification or draft). No network, no database server.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable

from tests.fakes import FakeTransport, SentRequest, make_context
from us_outbound.context import SECRET_NAMES, Secrets
from us_outbound.logs import hash_email

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)  # Tue 27 Oct 2026, 11:00 ET
HANNAH, HARRY, SAM = "hannah@meetspill.org", "harry@meetspill.org", "sam@meetspill.org"
CAMPAIGNS = {
    "cmp-hannah": "US Outbound – Hannah Spalding",
    "cmp-harry": "US Outbound – Harry Dryden",
    "cmp-sam": "US Outbound – Sam Jackson",
}
JANE = "jane.doe@acmecreative.com"
BOB = "bob@betalegal.com"
SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


def classification(cls: str = "positive", **overrides: Any) -> dict:
    """A complete SPEC 11 answer from the task model."""
    out = {
        "class": cls, "confidence": 0.95, "asks_to_stop": False, "demo_requested": cls == "positive",
        "objection": "none", "not_now_date": "", "ooo_return_date": "", "referral_name": "", "referral_title": "",
        "referral_email": "", "competitor_named": "", "language_terms": ["busy season"], "summary": "Replied.",
    }
    out.update(overrides)
    return out


def good_draft(prompt: str) -> str:
    """A draft that keeps every rule, built from what the prompt asks for."""
    first = re.search(r"^Prospect: ([^,.\s]+)", prompt, re.M)
    name = first.group(1) if first and first.group(1) != "unknown" else "there"
    sender = re.search(r"\(sign as (\w+)\)", prompt).group(1)
    demo = re.search(r'give the demo line word for word: "([^"]+)"', prompt)
    middle = "Thanks for getting back to me, and for being so clear about where things stand for your team right now."
    if demo:
        middle += "\n\n" + demo.group(1)
    return f"Hi {name},\n\n{middle}\n\nBest wishes,\n{sender}"


class ClaudeSDK:
    """Stands in for anthropic.Anthropic: a classification for the task model, a draft for the writing model."""

    def __init__(self):
        self.calls: list[dict] = []
        self.answer: dict = classification()
        self.draft: Callable[[str], str] = good_draft
        self.raises: Exception | None = None
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        prompt = kwargs["messages"][0]["content"]
        schema = kwargs["output_config"]["format"]["schema"]
        text = json.dumps({"body": self.draft(prompt)} if "body" in schema["properties"] else self.answer)
        usage = SimpleNamespace(input_tokens=(len(kwargs["system"]) + len(prompt)) // 4, output_tokens=300,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0, cache_creation=None)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=usage)

    def by_model(self, model: str) -> list[dict]:
        return [c for c in self.calls if c["model"] == model]


def _ts(v: datetime) -> str:
    return v.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class ReplyWorld:
    ctx: Any
    transport: FakeTransport
    sdk: ClaudeSDK
    emails: list[dict] = field(default_factory=list)
    leads: list[dict] = field(default_factory=list)
    hubspot_contacts: dict[str, dict] = field(default_factory=dict)
    blocked: list[list[str]] = field(default_factory=list)
    hubspot_unsubscribed: list[str] = field(default_factory=list)
    slack_posts: list[dict] = field(default_factory=list)
    slack_reactions: list[dict] = field(default_factory=list)
    lead_patches: list[tuple[str, dict]] = field(default_factory=list)

    # -- building the world --

    def reply(self, eid: str, frm: str = JANE, text: str = "Sounds good, could you send some times next week?",
              *, to: str = HANNAH, at: datetime | None = None, **extra: Any) -> dict:
        at = at or NOW - timedelta(minutes=10)
        e = {"id": eid, "eaccount": to, "from_address_email": frm, "to_address_email_list": to,
             "subject": "Busy season support for Acme Creative", "body": {"text": text}, "ue_type": 2,
             "timestamp_email": _ts(at), "timestamp_created": _ts(at), "thread_id": f"t-{eid}", "message_id": f"<{eid}@x>",
             "lead": frm if frm == JANE else "", "is_auto_reply": 0, "_type": "received", **extra}
        self.emails.append(e)
        return e

    def sent(self, eid: str, to: str = JANE, *, frm: str = HANNAH, at: datetime | None = None,
             campaign: str = "cmp-hannah", **extra: Any) -> dict:
        at = at or NOW - timedelta(days=3)
        e = {"id": eid, "eaccount": frm, "from_address_email": frm, "to_address_email_list": to, "lead": to,
             "campaign_id": campaign, "subject": "Hi", "ue_type": 1, "timestamp_email": _ts(at),
             "timestamp_created": _ts(at), "_type": "sent", **extra}
        self.emails.append(e)
        return e

    def lead(self, lead_id: str, email: str, status: int, campaign: str = "cmp-hannah") -> dict:
        lead = {"id": lead_id, "email": email, "status": status, "campaign": campaign}
        self.leads.append(lead)
        return lead

    def without_slack_token(self) -> None:
        self.ctx.clients.secrets = Secrets(
            self.ctx.guard, fetch=lambda n: "" if n == SECRET_NAMES["slack"] else f"test-{n}")
        self.ctx.clients.__dict__.pop("slack", None)

    def at(self, now: datetime, *, live: bool | None = None) -> None:
        """Move the clock (and optionally the mode) for the next run, keeping the database."""
        self.ctx.now = now
        self.ctx.run_id = f"run-{now.isoformat()}"
        if live is not None:
            self.ctx.guard.configure(live=live)

    # -- reading it back --

    def items(self, kind: str = "reply") -> list[dict]:
        return [r for r in self.ctx.store.tables["hitl_items"] if r.get("kind") == kind]

    def events(self, type_: str | None = None) -> list[dict]:
        return [e for e in self.ctx.store.tables["events"] if type_ is None or e.get("type") == type_]

    def suppressed(self, email: str) -> list[dict]:
        return [r for r in self.ctx.store.tables["suppression"] if r.get("email_sha256") == hash_email(email)]

    def instantly_writes(self) -> list[SentRequest]:
        """Requests that change Instantly (POST /leads/list is a read that goes over POST)."""
        return [r for r in self.transport.writes() if "api.instantly.ai" in r.url and not r.url.endswith("/leads/list")]

    def hubspot_writes(self) -> list[SentRequest]:
        return [r for r in self.transport.writes() if "api.hubapi.com" in r.url and not r.url.endswith("/search")]

    # -- the fake services --

    def _emails(self, req: SentRequest) -> Any:
        path = req.url.split("/api/v2", 1)[1].rstrip("/")
        public = lambda e: {k: v for k, v in e.items() if not k.startswith("_")}  # noqa: E731
        if path == "/emails":
            p = req.params or {}
            since = p.get("min_timestamp_created") or ""
            items = [public(e) for e in self.emails
                     if e["eaccount"] == p.get("eaccount") and (not p.get("email_type") or e["_type"] == p["email_type"])
                     and e["timestamp_created"] >= since]
            return {"items": items}
        eid = path.rsplit("/", 1)[1]
        return next((public(e) for e in self.emails if e["id"] == eid), {})

    def _lead_list(self, req: SentRequest) -> Any:
        return {"items": [dict(lead) for lead in self.leads if lead["campaign"] == req.json["campaign"]]}

    def _lead_get(self, req: SentRequest) -> Any:
        lead_id = req.url.rsplit("/", 1)[1]
        return next((dict(lead) for lead in self.leads if lead["id"] == lead_id), {"id": lead_id, "campaign": "cmp-hannah"})

    def _lead_patch(self, req: SentRequest) -> Any:
        self.lead_patches.append((req.url.rsplit("/", 1)[1], dict(req.json)))
        return {"id": req.url.rsplit("/", 1)[1], **req.json}

    def _block(self, req: SentRequest) -> Any:
        self.blocked.append(list(req.json["bl_values"]))
        return {}

    def _hs_search(self, req: SentRequest) -> Any:
        email = req.json["filterGroups"][0]["filters"][0]["value"]
        found = self.hubspot_contacts.get(email)
        return {"results": [{"id": "hs-" + email.split("@")[0], "properties": {"email": email, **found}}] if found is not None else []}

    def _hs_unsub(self, req: SentRequest) -> Any:
        self.hubspot_unsubscribed.append(req.url.split("/statuses/", 1)[1].split("/", 1)[0])
        return {}

    def _post(self, req: SentRequest) -> Any:
        self.slack_posts.append(dict(req.json))
        return {"ok": True, "channel": req.json["channel"], "ts": f"17000000{len(self.slack_posts):02d}.0001"}

    def _react(self, req: SentRequest) -> Any:
        self.slack_reactions.append(dict(req.json))
        return {"ok": True}

    def fail_blocklist(self, status: int) -> None:
        self.transport.route("POST", "/block-lists-entries/bulk-create", {"error": "boom"}, status=status)

    def install(self) -> ReplyWorld:
        t = self.transport
        t.route("GET", "api.instantly.ai/api/v2/campaigns",
                {"items": [{"id": i, "name": n} for i, n in CAMPAIGNS.items()]
                 + [{"id": "cmp-eu", "name": "EU Outbound – Anna"}]})
        t.route("GET", "api.instantly.ai/api/v2/emails", fn=self._emails)
        t.route("POST", "/leads/list", fn=self._lead_list)
        t.route("GET", "api.instantly.ai/api/v2/leads/", fn=self._lead_get)
        t.route("PATCH", "api.instantly.ai/api/v2/leads/", fn=self._lead_patch)
        t.route("POST", "/block-lists-entries/bulk-create", fn=self._block)
        t.route("POST", "/crm/v3/objects/contacts/search", fn=self._hs_search)
        t.route("POST", "/communication-preferences/v4/statuses/", fn=self._hs_unsub)
        t.route("GET", "conversations.list", {"ok": True, "channels": [{"id": "C_ALERT", "name": "us-outbound"},
                                                                       {"id": "C_DEV", "name": "us-outbound-dev"}]})
        t.route("POST", "chat.postMessage", fn=self._post)
        t.route("POST", "reactions.add", fn=self._react)
        return self


ACCOUNTS = [
    {"account_id": "acc-acme", "domain": "acmecreative.com", "clean_name": "Acme Creative", "hq_city": "Chicago",
     "hq_state": "IL", "industry": "Marketing agencies", "industry_group": "Marketing & Creative Agencies",
     "tier": "Priority", "tier_reason": "Benefits page lists an EAP", "status": "enrolled", "sender": "Hannah Spalding"},
    {"account_id": "acc-beta", "domain": "betalegal.com", "clean_name": "Beta Legal", "hq_state": "NY",
     "industry": "Legal Teams", "industry_group": "Legal Teams", "tier": "Standard", "status": "enrolled",
     "sender": "Harry Dryden"},
]
CONTACTS = [
    {"contact_id": "k-jane", "account_id": "acc-acme", "first_name": "Jane", "last_name": "Doe", "title": "Head of People",
     "role": "People leader", "email": JANE, "email_sha256": hash_email(JANE), "enrolment_month": "2026-10",
     "enrolled_at": NOW - timedelta(days=10), "instantly_campaign": CAMPAIGNS["cmp-hannah"], "instantly_lead_id": "L-jane",
     "mailbox": HANNAH},
    {"contact_id": "k-bob", "account_id": "acc-beta", "first_name": "Bob", "last_name": "Roe", "title": "Managing Partner",
     "role": "Founder or executive", "email": BOB, "email_sha256": hash_email(BOB), "enrolment_month": "2026-10",
     "enrolled_at": NOW - timedelta(days=8), "instantly_campaign": CAMPAIGNS["cmp-harry"], "instantly_lead_id": "L-bob",
     "mailbox": None},
    # Not enrolled: never matched.
    {"contact_id": "k-cold", "account_id": "acc-cold", "first_name": "Cy", "email": "cy@cold.com",
     "email_sha256": hash_email("cy@cold.com")},
]


def make_world(settings, *, live: bool = True, now: datetime = NOW) -> ReplyWorld:
    sdk = ClaudeSDK()
    transport = FakeTransport()
    ctx = make_context(settings, live=live, now=now, transport=transport, claude_sdk=sdk, job="poll_replies")
    ctx.store.insert("accounts", [dict(a) for a in ACCOUNTS])
    ctx.store.insert("contacts", [dict(c) for c in CONTACTS])
    return ReplyWorld(ctx, transport, sdk).install()
