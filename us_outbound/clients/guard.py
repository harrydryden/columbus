"""The guardrails of SPEC section 1, enforced at the single point every client call passes.

Every outbound call (HTTP, the database, Claude) is described as an Op and handed to
Guard.authorize() before it is made. authorize() either:

  * raises GuardViolation: the call would break a guardrail, in any mode; or
  * returns False: the call is a write that dry-run must not make (the caller skips it); or
  * returns True: make the call.

Dry-run (SPEC 0.3): compute, log and write to the database, but send nothing. Nothing is
written to HubSpot, Instantly or the settings sheet, and nothing to Slack except the dev
channel. Live needs both the --live flag and live_sending = yes; the caller works that out
and passes `live`.

Every call is recorded in Guard.calls so tests can inspect them all.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Mapping

from us_outbound.logs import log, redact

US_CAMPAIGN_PREFIX = "US Outbound – "  # en dash, as in SPEC 9
ERASE_JOB = "erase"  # the only job that may GDPR-delete in HubSpot (SPEC 6 erase --email)
# SPEC 1.3, as changed by decision D11 (Harry, 1 Oct 2026): nothing goes to a prospect after their
# first reply unless an approver approved it. An approver is a Slack id on approver_slack_ids, or the
# owner of the mailbox the prospect wrote to (Mailboxes slack_id). At the command line (Slack may not
# be set up for the pilot) the approver is the person running `us-outbound replies approve`, so the
# reply carries approved_by = "cli" and only that command's job may send it.
REPLIES_CLI_JOB = "replies_approve"
CLI_APPROVER = "cli"
SETTINGS_SHEET_TITLE = "US Outbound – Settings"
DB_SCHEMA = "us_outbound"

# HubSpot write allowlist (SPEC 1.2).
HUBSPOT_PROPERTY_GROUP = "us_outbound"
HUBSPOT_COMPANY_PROPS = frozenset(
    {"us_outbound_account_id", "us_outbound_tier", "us_outbound_industry_group", "us_outbound_top_signals"}
)
HUBSPOT_CONTACT_PROPS = frozenset({"us_outbound_angle", "us_outbound_reply_class"})
HUBSPOT_SIX_PROPS = HUBSPOT_COMPANY_PROPS | HUBSPOT_CONTACT_PROPS
HUBSPOT_EMPTY_ONLY = frozenset({"hubspot_owner_id", "lifecyclestage", "hs_lead_status"})
# Identity fields a new warm record needs; only on create.
HUBSPOT_COMPANY_CREATE_FIELDS = frozenset({"name", "domain"})
HUBSPOT_CONTACT_CREATE_FIELDS = frozenset({"email", "firstname", "lastname", "jobtitle"})
WARM_REPLY_CLASSES = frozenset({"positive", "referral"})

APOLLO_READ_ACTIONS = frozenset(
    {
        "organizations.search",
        "organizations.enrich",
        "organizations.bulk_enrich",
        "organizations.job_postings",
        "people.search",
        "people.match",
        "people.bulk_match",
        "website_visitors.search",
        "website_visitors.domain_aggregates",
        "usage.credits",
        "auth.health",
    }
)

PUBLIC_HOSTS = frozenset(
    {
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.ashbyhq.com",
        "apply.workable.com",
        "www.irs.gov",
        "irs.gov",
        "layoffs.fyi",
        "raw.githubusercontent.com",
        "github.com",
        "storage.googleapis.com",
    }
)


class GuardViolation(Exception):
    """A call would break a SPEC guardrail. Never caught to carry on."""


@dataclass(frozen=True)
class Op:
    """One outbound call, described by what it does rather than how.

    action: a dotted verb, e.g. "company.create", "lead.add", "chat.postMessage".
    target: the container it touches: a campaign name, table, channel, sheet id, host.
    write:  True if it changes anything outside this process (except database reads).
    detail: what the guard needs to judge it (property names, account ids, ...).
    """

    action: str
    target: str = ""
    write: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CallRecord:
    system: str
    action: str
    target: str
    write: bool
    sent: bool
    live: bool
    detail: Mapping[str, Any]
    at: datetime


@dataclass(frozen=True)
class Boundaries:
    """The containers this system may touch. Built from settings and the environment."""

    registry_addresses: frozenset[str] = frozenset()  # lower-case mailbox addresses
    registry_account_ids: frozenset[str] = frozenset()  # Instantly account ids
    registry_owners: frozenset[str] = frozenset()  # mailbox owner names
    hubspot_pipeline_id: str = ""
    hubspot_deal_stage_id: str = ""
    # The two US Outbound functions, and Work Email while clay_email_fallback is yes (context.boundaries_for).
    clay_function_ids: frozenset[str] = frozenset()
    settings_sheet_id: str = ""
    alert_channel: str = "#us-outbound"
    dev_channel: str = "#us-outbound-dev"
    escalation_email: str = ""  # the only address an Instantly forward may go to (SPEC 11)
    approver_slack_ids: frozenset[str] = frozenset()  # General approver_slack_ids; the only Slack DM recipients
    # (mailbox address, its owner's Slack id): D11, owners approve replies to their own mailbox.
    owner_slack_ids: frozenset[tuple[str, str]] = frozenset()
    # The outside watchdog's ping URL (US_OUTBOUND_WATCHDOG_URL; ops/watchdog.py), set for heartbeat_check only.
    watchdog_url: str = ""
    db_schema: str = DB_SCHEMA

    @property
    def registry_accounts(self) -> frozenset[str]:
        return self.registry_addresses | self.registry_account_ids


class Guard:
    def __init__(self, live: bool = False, bounds: Boundaries | None = None, job: str = ""):
        self.live = live
        self.bounds = bounds or Boundaries()
        self.job = job  # the job this process runs (Context.job); some writes belong to one job only
        self.calls: list[CallRecord] = []

    def configure(self, *, live: bool | None = None, bounds: Boundaries | None = None, job: str | None = None) -> None:
        if live is not None:
            self.live = live
        if bounds is not None:
            self.bounds = bounds
        if job is not None:
            self.job = job

    # -- the one entry point -------------------------------------------------

    def authorize(self, system: str, op: Op) -> bool:
        check = getattr(self, f"_check_{system}", None)
        if check is None:
            raise GuardViolation(f"unknown system {system!r}")
        try:
            send = check(op)
        except GuardViolation as exc:
            self._record(system, op, sent=False)
            log("guard_violation", system=system, action=op.action, target=op.target, reason=str(exc))
            raise
        if send is None:
            send = True
        self._record(system, op, sent=send)
        return send

    def writes(self, system: str | None = None, sent: bool | None = None) -> list[CallRecord]:
        return [
            c
            for c in self.calls
            if c.write and (system is None or c.system == system) and (sent is None or c.sent == sent)
        ]

    def _record(self, system: str, op: Op, sent: bool) -> None:
        rec = CallRecord(
            system=system,
            action=op.action,
            target=op.target,
            write=op.write,
            sent=sent,
            live=self.live,
            detail=redact(dict(op.detail)),
            at=datetime.now(UTC),
        )
        self.calls.append(rec)
        log(
            "call",
            system=system,
            action=op.action,
            target=op.target,
            write=op.write,
            sent=sent,
            live=self.live,
            accounts=sorted(op.detail.get("accounts", ())) if system == "instantly" else None,
        )

    def _live_only(self, op: Op) -> bool:
        """Writes to HubSpot, Instantly and the sheet happen only when live."""
        return self.live if op.write else True

    # -- per-system policy ---------------------------------------------------

    def _check_apollo(self, op: Op) -> bool:
        if op.write:
            raise GuardViolation("Apollo is read only (SPEC 1.2)")
        if op.action not in APOLLO_READ_ACTIONS:
            raise GuardViolation(f"Apollo action {op.action!r} is not an allowed read")
        return True

    def _check_clay(self, op: Op) -> bool:
        if op.action == "function.run":
            if op.target not in self.bounds.clay_function_ids:
                raise GuardViolation(
                    f"Clay function {op.target!r} is not one of the US Outbound functions (SPEC 1.2, 8)"
                )
            return True  # Clay verification runs in dry-run too; budgets are checked by the caller.
        if op.action in {"function.get", "function.list", "workspace.get", "run.get"} and not op.write:
            return True
        raise GuardViolation(f"Clay action {op.action!r} is not allowed from Python")

    def _check_instantly(self, op: Op) -> bool:
        b = self.bounds
        a = op.action
        accounts = {str(x).lower() for x in op.detail.get("accounts", ())}

        def need_registry_accounts() -> None:
            if not accounts:
                raise GuardViolation(f"Instantly {a} must be filtered by registry account ids (SPEC 1.2)")
            outside = accounts - {x.lower() for x in b.registry_accounts}
            if outside:
                raise GuardViolation(f"Instantly {a} touches accounts outside the registry: {sorted(outside)}")

        def need_us_campaign() -> None:
            if not op.target.startswith(US_CAMPAIGN_PREFIX):
                raise GuardViolation(f"Instantly {a} targets campaign {op.target!r}, not a US Outbound campaign")

        if not op.write:
            if a in {"email.list", "email.get", "account.list", "account.get", "account.vitals", "warmup.analytics",
                     "account.analytics_daily"}:
                need_registry_accounts()
            elif a in {"lead.list", "lead.get", "campaign.get", "campaign.analytics", "campaign.steps_analytics",
                       "campaign.sending_status"}:
                need_us_campaign()
            elif a == "campaign.list":
                if not str(op.detail.get("search", "")).startswith(US_CAMPAIGN_PREFIX.strip()):
                    raise GuardViolation("Instantly campaign.list must search for 'US Outbound'")
            elif a in {"email.unread_count", "custom_variable.limit"}:
                need_registry_accounts()
            else:
                raise GuardViolation(f"Instantly read {a!r} is not allowed")
            return True

        if a == "campaign.create":
            need_us_campaign()
            owner = op.target[len(US_CAMPAIGN_PREFIX):]
            if owner not in b.registry_owners:
                raise GuardViolation(f"no registry owner {owner!r} for campaign {op.target!r}")
            need_registry_accounts()
        elif a in {"campaign.update", "campaign.pause", "campaign.activate"}:
            need_us_campaign()
            if "accounts" in op.detail:
                need_registry_accounts()
        elif a in {"lead.add", "lead.delete", "lead.update", "lead.stop", "lead.interest"}:
            need_us_campaign()
        elif a == "email.reply":
            need_registry_accounts()
            if not str(op.detail.get("campaign", "")).startswith(US_CAMPAIGN_PREFIX):
                raise GuardViolation("Instantly replies go only in threads of a US Outbound campaign (SPEC 1.2)")
            by = str(op.detail.get("approved_by") or "").strip()
            owners = {sid for address, sid in b.owner_slack_ids if address.lower() in accounts}
            approved = (by == CLI_APPROVER and self.job == REPLIES_CLI_JOB) or (
                by not in ("", CLI_APPROVER) and by in b.approver_slack_ids | owners
            )
            if not approved:
                raise GuardViolation(
                    "nothing goes to a prospect after their reply unless an approver approved it (SPEC 1.3, D11)"
                )
        elif a == "email.forward":
            need_registry_accounts()
            to = {str(x).strip().lower() for x in op.detail.get("to", ())}
            if not to or not b.escalation_email or to != {b.escalation_email.strip().lower()}:
                raise GuardViolation("Instantly forwards go only to escalation_email (SPEC 11)")
        elif a in {"account.warmup_enable", "account.warmup_disable", "account.pause", "account.resume"}:
            need_registry_accounts()
        elif a == "account.update_limit":
            need_registry_accounts()  # only the daily limit, only on a registry account (Instantly.set_daily_limit)
            if set(op.detail) - {"accounts", "daily_limit"}:
                raise GuardViolation("Instantly account.update_limit may change only the daily limit")
        elif a == "account.update_name":
            # Only the sender name, only on a registry account, and only to a registry owner's full name
            # (Instantly.set_sender_name; Harry, 5 Oct 2026).
            need_registry_accounts()
            if set(op.detail) - {"accounts", "first_name", "last_name"}:
                raise GuardViolation("Instantly account.update_name may change only first_name and last_name")
            name = " ".join(f"{op.detail.get('first_name') or ''} {op.detail.get('last_name') or ''}".split())
            if name not in {" ".join(o.split()) for o in b.registry_owners}:
                raise GuardViolation(f"Instantly sender name {name!r} is not a mailbox owner's name on the Mailboxes tab")
        elif a == "blocklist.add":
            if not op.detail.get("entries"):
                raise GuardViolation("blocklist.add needs entries")
        else:
            raise GuardViolation(f"Instantly write {a!r} is not allowed (never change workspace settings)")
        return self._live_only(op)

    def _check_hubspot(self, op: Op) -> bool:
        if not op.write:
            return True
        a = op.action
        props = set(op.detail.get("properties", ()))
        current = op.detail.get("current", {}) or {}

        def empty_only_ok() -> None:
            for p in sorted(props & HUBSPOT_EMPTY_ONLY):
                if p not in current:
                    raise GuardViolation(f"HubSpot {p} may only be set when empty; current value not supplied (SPEC 1.2)")
                if current[p] not in (None, ""):
                    raise GuardViolation(f"HubSpot {p} may only be set when empty (SPEC 1.2)")

        def need_warm() -> None:
            if op.detail.get("reply_class") not in WARM_REPLY_CLASSES:
                raise GuardViolation("HubSpot records are created only for positive or referral replies (SPEC 1.2)")

        if a == "company.create":
            need_warm()
            extra = props - HUBSPOT_COMPANY_PROPS - HUBSPOT_EMPTY_ONLY - HUBSPOT_COMPANY_CREATE_FIELDS
            if extra:
                raise GuardViolation(f"HubSpot company.create may not set {sorted(extra)}")
        elif a == "company.update":
            extra = props - HUBSPOT_COMPANY_PROPS - HUBSPOT_EMPTY_ONLY
            if extra:
                raise GuardViolation(f"HubSpot company.update may not set {sorted(extra)}")
            empty_only_ok()
        elif a == "contact.create":
            need_warm()
            extra = props - HUBSPOT_CONTACT_PROPS - HUBSPOT_EMPTY_ONLY - HUBSPOT_CONTACT_CREATE_FIELDS
            if extra:
                raise GuardViolation(f"HubSpot contact.create may not set {sorted(extra)}")
        elif a == "contact.update":
            extra = props - HUBSPOT_CONTACT_PROPS - HUBSPOT_EMPTY_ONLY
            if extra:
                raise GuardViolation(f"HubSpot contact.update may not set {sorted(extra)}")
            empty_only_ok()
        elif a in {"note.create", "task.create", "association.create"}:
            pass
        elif a == "deal.create":
            b = self.bounds
            if not b.hubspot_pipeline_id or op.detail.get("pipeline") != b.hubspot_pipeline_id:
                raise GuardViolation("HubSpot deals go only in the Spill 3.0 pipeline (SPEC 1.2)")
            if not b.hubspot_deal_stage_id or op.detail.get("dealstage") != b.hubspot_deal_stage_id:
                raise GuardViolation("HubSpot deals start only at the first Spill 3.0 stage (SPEC 11)")
            if not str(op.detail.get("dealname", "")).startswith(US_CAMPAIGN_PREFIX):
                raise GuardViolation("HubSpot deal names start 'US Outbound – ' (SPEC 11)")
            extra = props - {"dealname", "pipeline", "dealstage", "hubspot_owner_id"}
            if extra:
                raise GuardViolation(f"HubSpot deal.create may not set {sorted(extra)}")
        elif a == "property_group.create":
            if op.target != HUBSPOT_PROPERTY_GROUP:
                raise GuardViolation("the only HubSpot property group we create is 'US Outbound'")
        elif a == "property.create":
            if op.target not in HUBSPOT_SIX_PROPS:
                raise GuardViolation(f"HubSpot property {op.target!r} is not one of the six us_outbound_* properties")
        elif a == "communication.unsubscribe":
            pass  # SPEC 11 routing: opted out in HubSpot when the contact exists there
        elif a == "contact.gdpr_delete":
            if not op.detail.get("erasure_request") or self.job != ERASE_JOB:
                raise GuardViolation("HubSpot GDPR delete only runs from the erase command (SPEC 6)")
        else:
            raise GuardViolation(f"HubSpot write {a!r} is not allowed")
        return self._live_only(op)

    def _check_slack(self, op: Op) -> bool:
        if not op.write:
            return True
        b = self.bounds
        if op.action not in {"chat.postMessage", "chat.update", "reactions.add"}:
            raise GuardViolation(f"Slack write {op.action!r} is not allowed")
        if op.target.startswith("@"):
            # A direct message (build: SPEC 11 escalation when the forward endpoint is missing), only to
            # an approver on approver_slack_ids (Harry). Dry-run sends none; the client redirects it to dev.
            if op.action != "chat.postMessage" or op.target[1:] not in b.approver_slack_ids:
                raise GuardViolation(f"Slack direct message to {op.target!r}: only approver_slack_ids get one")
            return self.live
        if op.target not in {b.alert_channel, b.dev_channel}:
            raise GuardViolation(f"Slack channel {op.target!r} is not a US Outbound channel")
        if not self.live and op.target != b.dev_channel:
            return False  # dry-run: nothing to Slack except the dev channel
        return True

    def _check_sheets(self, op: Op) -> bool:
        if op.action == "spreadsheet.create":
            if op.detail.get("title") != SETTINGS_SHEET_TITLE:
                raise GuardViolation("the only sheet we create is 'US Outbound – Settings'")
            return self._live_only(replace(op, write=True))
        if op.target != self.bounds.settings_sheet_id or not op.target:
            raise GuardViolation(f"sheet {op.target!r} is not the settings sheet")
        return self._live_only(op)

    def _check_db(self, op: Op) -> bool:
        if not op.write:
            return True
        dataset = op.target.split(".")[-2] if op.target.count(".") >= 1 else ""
        if dataset != self.bounds.db_schema:
            raise GuardViolation(f"Database writes go only to schema {self.bounds.db_schema} (SPEC 1.2), not {op.target!r}")
        return True  # dry-run still writes to the database (SPEC 0.3)

    def _check_claude(self, op: Op) -> bool:
        if op.write or op.action != "messages.create":
            raise GuardViolation(f"Claude action {op.action!r} is not allowed")
        return True

    def _check_public(self, op: Op) -> bool:
        if op.write:
            raise GuardViolation("public sources are read only")
        if op.action == "resolve_redirect":
            return True  # HEAD on a prospect's own domain to follow one redirect (SPEC 13 data cleaning)
        if op.action == "site.get":
            # A GET of a page on the account's own site (sources/pages.py; Harry, 2 Oct 2026): the host
            # must be the account's root domain or one of its subdomains, never anywhere else.
            domain = str(op.detail.get("domain") or "").strip().lower().rstrip(".")
            if not domain or "." not in domain:
                raise GuardViolation("a site read names the account's domain")
            if op.target != domain and not op.target.endswith("." + domain):
                raise GuardViolation(f"site read of {op.target!r} is not on the account's domain {domain!r}")
            return True
        if op.action != "get":
            raise GuardViolation("public sources are read with GET only")
        if op.target not in PUBLIC_HOSTS:
            raise GuardViolation(f"public host {op.target!r} is not an allowed source")
        return True

    def _check_watchdog(self, op: Op) -> bool:
        """The outside watchdog (Harry, 7 Oct 2026; ops/watchdog.py): a GET of exactly the configured ping URL, or
        of its /fail form, and nothing else. Sent in dry-run too: it tells Healthchecks.io the worker is alive (or
        that it needs a look), reaches no prospect and changes nothing of ours, and a dead worker is as dead before
        go-live as after. The URL is never put in a reason or a log line: it is a secret."""
        url = self.bounds.watchdog_url
        if op.action != "get":
            raise GuardViolation("the watchdog is pinged with GET only")
        if not url or op.detail.get("url") not in {url, url + "/fail"}:
            raise GuardViolation("a watchdog ping goes only to US_OUTBOUND_WATCHDOG_URL or its /fail form")
        return True

    def _check_secrets(self, op: Op) -> bool:
        if op.write or op.action != "access":
            raise GuardViolation("secrets are read only from the jobs")
        return True
