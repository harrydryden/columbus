"""What every job receives: settings, store, guard, clients, the clock and the live flag.

Live behaviour needs both the --live flag and live_sending = yes (SPEC 0.3). The guard
holds the effective value; everything else reads it from there.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cached_property
from typing import Any, Callable
from zoneinfo import ZoneInfo

from us_outbound.clients.db import Store
from us_outbound.clients.guard import Boundaries, Guard
from us_outbound.clients.http import Transport
from us_outbound.settings.model import Settings

UK = ZoneInfo("Europe/London")
ET = ZoneInfo("America/New_York")

# Secret Manager secret names (SPEC 13, Secrets).
SECRET_NAMES = {
    "apollo": "us-outbound-apollo-api-key",
    "clay": "us-outbound-clay-api-key",
    "instantly": "us-outbound-instantly-api-key",
    "hubspot": "us-outbound-hubspot-token",
    "slack": "us-outbound-slack-bot-token",
    "claude": "us-outbound-claude-api-key",
}


class Secrets:
    """Reads keys from Google Secret Manager. Keys never go to the repo, logs or BigQuery."""

    def __init__(self, guard: Guard, project: str, fetch: Callable[[str], str] | None = None):
        self.guard, self.project, self._fetch = guard, project, fetch
        self._cache: dict[str, str] = {}

    def get(self, system: str) -> str:
        from us_outbound.clients.guard import Op

        name = SECRET_NAMES[system]
        if name not in self._cache:
            self.guard.authorize("secrets", Op("access", target=name))
            self._cache[name] = (self._fetch or self._secret_manager)(name)
        return self._cache[name]

    def _secret_manager(self, name: str) -> str:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{self.project}/secrets/{name}/versions/latest"
        return client.access_secret_version(name=path).payload.data.decode().strip()


def boundaries_for(settings: Settings, settings_sheet_id: str = "") -> Boundaries:
    g = settings.general
    live_mailboxes = [m for m in settings.mailboxes if m.status != "Retired"]
    return Boundaries(
        registry_addresses=frozenset(m.address.lower() for m in live_mailboxes),
        registry_account_ids=frozenset(m.instantly_account_id for m in live_mailboxes if m.instantly_account_id),
        registry_owners=frozenset(m.owner_name for m in live_mailboxes),
        hubspot_pipeline_id=g.hubspot_pipeline_id,
        hubspot_deal_stage_id=g.hubspot_deal_stage_id,
        clay_function_ids=frozenset(x for x in (g.clay_accounts_function_id, g.clay_contacts_function_id) if x),
        settings_sheet_id=settings_sheet_id or os.environ.get("US_OUTBOUND_SETTINGS_SHEET_ID", ""),
        alert_channel=g.alert_channel,
        dev_channel=g.dev_channel,
        escalation_email=g.escalation_email.strip().lower(),
    )


class Clients:
    """Lazily built API clients. Each HttpClient subclass takes (guard, transport, token)."""

    def __init__(
        self,
        guard: Guard,
        transport: Transport,
        secrets: Secrets,
        store: Store,
        settings: Settings,
        google_credentials: Any = None,
        claude_sdk: Any = None,
    ):
        self.guard, self.transport, self.secrets = guard, transport, secrets
        self.store, self.settings = store, settings
        self.google_credentials, self.claude_sdk = google_credentials, claude_sdk

    @cached_property
    def apollo(self):
        from us_outbound.clients.apollo import Apollo

        return Apollo(self.guard, self.transport, self.secrets.get("apollo"))

    @cached_property
    def clay(self):
        from us_outbound.clients.clay import Clay

        return Clay(self.guard, self.transport, self.secrets.get("clay"))

    @cached_property
    def instantly(self):
        from us_outbound.clients.instantly import Instantly

        return Instantly(self.guard, self.transport, self.secrets.get("instantly"))

    @cached_property
    def hubspot(self):
        from us_outbound.clients.hubspot import HubSpot

        return HubSpot(self.guard, self.transport, self.secrets.get("hubspot"))

    @cached_property
    def slack(self):
        from us_outbound.clients.slack import Slack

        return Slack(self.guard, self.transport, self.secrets.get("slack"))

    @cached_property
    def sheets(self):
        from us_outbound.clients.sheets import Sheets

        return Sheets(self.guard, self.transport, credentials=self.google_credentials)

    @cached_property
    def claude(self):
        from us_outbound.clients.claude import Claude

        g = self.settings.general
        return Claude(
            self.guard,
            self.store,
            api_key=None if self.claude_sdk else self.secrets.get("claude"),
            model=g.claude_model,
            monthly_cap_usd=g.claude_monthly_cap_usd,
            sdk=self.claude_sdk,
        )

    @cached_property
    def public(self):
        from us_outbound.clients.public import Public

        return Public(self.guard, self.transport)


@dataclass
class Context:
    job: str
    settings: Settings
    store: Store
    guard: Guard
    clients: Clients
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    live_flag: bool = False
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def live(self) -> bool:
        return self.guard.live

    @property
    def dry_run(self) -> bool:
        return not self.guard.live

    def today_uk(self) -> date:
        return self.now.astimezone(UK).date()

    def now_et(self) -> datetime:
        return self.now.astimezone(ET)


def effective_live(live_flag: bool, settings: Settings) -> bool:
    return bool(live_flag and settings.general.live_sending)
