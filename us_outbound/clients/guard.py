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

Protective-only (W1-1, 19 Oct 2026; RS P0-1): a safety job running on the last-good settings (ops/bootstrap.py
SAFETY_JOBS) may only slow or stop sending. Instantly campaign.activate, campaign.create, account.resume, lead.add,
email.reply, email.forward and a daily limit raised (account.update_limit without a higher previous_limit) are refused
with "protective-only mode"; pauses, lead stops and deletes, the blocklist, HubSpot unsubscribes, sheet status writes,
database writes and Slack posts go on as in any run. A campaign.update may only shrink: its sending list a subset of
the one the campaign had (previous_accounts, read just before), its daily limit no higher than before
(previous_daily_limit), and nothing else changed. The fallback's Mailboxes tab can be an earlier version, in which a
mailbox paused since is still Active: a kill rule's pause of another mailbox rebuilds the owner's sending list from it,
and without this rule would put the paused one back (registry/mailboxes._sync_campaign shrinks as well; this is the
second lock).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Mapping

from us_outbound.replies.kinds import WARM
from us_outbound.logs import log, redact

US_CAMPAIGN_PREFIX = "US Outbound – "  # en dash, as in SPEC 9
ERASE_JOB = "erase"  # the only job that may GDPR-delete in HubSpot (SPEC 6 erase --email)
# What a protective-only guard refuses (module docstring): everything that starts sending or reaches a prospect.
PROTECTIVE_REFUSED = frozenset({"campaign.activate", "campaign.create", "account.resume", "lead.add", "email.reply",
                                "email.forward"})
# What a protective-only campaign.update may change (the module docstring): the sending list and the daily limit, down.
PROTECTIVE_CAMPAIGN_FIELDS = frozenset({"daily_limit"})
PROTECTIVE_ONLY = "protective-only mode"
# SPEC 1.3, as changed by decision D11 (Harry, 1 Oct 2026): nothing goes to a prospect after their
# first reply unless an approver approved it. An approver is a Slack id on approver_slack_ids, or the
# owner of the mailbox the prospect wrote to (Mailboxes slack_id). At the command line (Slack may not
# be set up for the pilot) the approver is the person running `us-outbound replies approve`, so the
# reply carries approved_by = "cli" and only that command's job may send it.
REPLIES_CLI_JOB = "replies_approve"
CLI_APPROVER = "cli"
SETTINGS_SHEET_TITLE = "US Outbound – Settings"
DB_SCHEMA = "us_outbound"

# HubSpot write allowlist (SPEC 1.2). Standard properties only (W1-2, 19 Oct 2026; Harry's D9): SPEC 11's six
# us_outbound_* custom properties were never created in the portal, so every warm reply's company and contact write
# would have failed with a 400, every 5 minutes, and nothing read them. They are gone, and with them the property
# group and property creates (`hubspot setup`): the guard refuses both as any other HubSpot write it does not list.
HUBSPOT_EMPTY_ONLY = frozenset({"hubspot_owner_id", "lifecyclestage", "hs_lead_status"})
# Identity fields a new warm record needs; only on create.
HUBSPOT_COMPANY_CREATE_FIELDS = frozenset({"name", "domain"})
HUBSPOT_CONTACT_CREATE_FIELDS = frozenset({"email", "firstname", "lastname", "jobtitle"})
WARM_REPLY_CLASSES = WARM  # replies/kinds.py

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
    settings_sheet_id: str = ""
    alert_channel: str = "#us-outbound"
    dev_channel: str = "#us-outbound-dev"
    escalation_email: str = ""  # the only address an Instantly forward may go to (SPEC 11)
    approver_slack_ids: frozenset[str] = frozenset()  # General approver_slack_ids; the only Slack DM recipients
    # (mailbox address, its owner's Slack id): D11, owners approve replies to their own mailbox.
    owner_slack_ids: frozenset[tuple[str, str]] = frozenset()
    # The outside watchdog's ping URL (US_OUTBOUND_WATCHDOG_URL; ops/health.py), set for heartbeat_check only.
    watchdog_url: str = ""
    db_schema: str = DB_SCHEMA

    @property
    def registry_accounts(self) -> frozenset[str]:
        return self.registry_addresses | self.registry_account_ids


def _campaign_widened(detail: Mapping[str, Any]) -> str:
    """Why a campaign.update does more than shrink the campaign (the module docstring's protective-only rule), or "".
    What it cannot compare (no previous list or limit given) counts as widening: the rule fails closed."""
    other = set(detail.get("fields") or ()) - PROTECTIVE_CAMPAIGN_FIELDS
    if other:
        return f"changes {sorted(other)}"
    if "accounts" in detail:
        before = detail.get("previous_accounts")
        if not isinstance(before, (list, tuple)):
            return "replaces the sending list with no previous list to compare"
        added = {str(x).lower() for x in detail["accounts"]} - {str(x).lower() for x in before}
        if added:
            return f"adds {len(added)} account(s) to the sending list"
    if "daily_limit" in set(detail.get("fields") or ()):
        limit, before = detail.get("daily_limit"), detail.get("previous_daily_limit")
        if not (isinstance(limit, int) and isinstance(before, int)):
            return "sets the daily limit with no previous limit to compare"
        if limit > before:
            return f"raises the daily limit from {before} to {limit}"
    return ""


class Guard:
    def __init__(self, live: bool = False, bounds: Boundaries | None = None, job: str = "",
                 protective_only: bool = False):
        self.live = live
        self.bounds = bounds or Boundaries()
        self.job = job  # the job this process runs (Context.job); some writes belong to one job only
        self.protective_only = protective_only  # the module docstring: pause and stop only
        self.calls: list[CallRecord] = []

    def configure(self, *, live: bool | None = None, bounds: Boundaries | None = None, job: str | None = None,
                  protective_only: bool | None = None) -> None:
        if live is not None:
            self.live = live
        if bounds is not None:
            self.bounds = bounds
        if job is not None:
            self.job = job
        if protective_only is not None:
            self.protective_only = protective_only

    # -- the one entry point -------------------------------------------------

    def authorize(self, system: str, op: Op) -> bool:
        send = self._judge(system, op)
        self._record(system, op, sent=send)
        return send

    def refuse_unless_allowed(self, system: str, op: Op) -> bool:
        """The policy for op, judged before anything is sent for it, even a lookup it needs first (a campaign id, the
        email replied to, a channel id): a refusal is recorded and raised as authorize() records and raises it.
        Otherwise it says whether authorize() will send op (False: dry-run skips it) and records nothing, since
        authorize() records the call when it is made. 9 Oct 2026: the Instantly and Slack clients each carried a
        copy of these rules to refuse early; they ask here instead."""
        return self._judge(system, op)

    def _judge(self, system: str, op: Op) -> bool:
        check = getattr(self, f"_check_{system}", None)
        if check is None:
            raise GuardViolation(f"unknown system {system!r}")
        try:
            send = check(op)
        except GuardViolation as exc:
            self._record(system, op, sent=False)
            log("guard_violation", system=system, action=op.action, target=op.target, reason=str(exc))
            raise
        return True if send is None else send

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

        if op.write and self.protective_only:
            raised = a == "account.update_limit" and not (
                isinstance(op.detail.get("previous_limit"), int) and isinstance(op.detail.get("daily_limit"), int)
                and op.detail["daily_limit"] < op.detail["previous_limit"])
            if a in PROTECTIVE_REFUSED or raised:
                raise GuardViolation(f"{PROTECTIVE_ONLY}: Instantly {a} is refused while the settings run on their "
                                     "last-good fallback (ops/bootstrap.py)")
            if a == "campaign.update":
                widened = _campaign_widened(op.detail)
                if widened:
                    raise GuardViolation(f"{PROTECTIVE_ONLY}: Instantly campaign.update {widened}; on the last-good "
                                         "fallback a campaign may only shrink (ops/bootstrap.py)")

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
            if set(op.detail) - {"accounts", "daily_limit", "previous_limit"}:
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
            extra = props - HUBSPOT_EMPTY_ONLY - HUBSPOT_COMPANY_CREATE_FIELDS
            if extra:
                raise GuardViolation(f"HubSpot company.create may not set {sorted(extra)}")
        elif a == "company.update":
            extra = props - HUBSPOT_EMPTY_ONLY
            if extra:
                raise GuardViolation(f"HubSpot company.update may not set {sorted(extra)}")
            empty_only_ok()
        elif a == "contact.create":
            need_warm()
            extra = props - HUBSPOT_EMPTY_ONLY - HUBSPOT_CONTACT_CREATE_FIELDS
            if extra:
                raise GuardViolation(f"HubSpot contact.create may not set {sorted(extra)}")
        elif a == "contact.update":
            extra = props - HUBSPOT_EMPTY_ONLY
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
        """The outside watchdog (Harry, 7 Oct 2026; ops/health.py): a GET of exactly the configured ping URL, or
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
