"""What every job receives: settings, store, guard, clients, the clock and the live flag.

Live behaviour needs both the --live flag and live_sending = yes (SPEC 0.3). The guard
holds the effective value; everything else reads it from there.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cached_property
from typing import Any
from zoneinfo import ZoneInfo

from us_outbound.clients.db import Store
from us_outbound.clients.guard import Boundaries, Guard
from us_outbound.clients.http import Transport
from us_outbound.settings.model import Settings

UK = ZoneInfo("Europe/London")
ET = ZoneInfo("America/New_York")

# The environment variable that holds each key (SPEC 13, Secrets). On Railway these are
# sealed service variables (docs/railway-setup.md); they never go in the repo, logs or database.
SECRET_NAMES = {
    "apollo": "US_OUTBOUND_APOLLO_API_KEY",
    "clay": "US_OUTBOUND_CLAY_API_KEY",
    "instantly": "US_OUTBOUND_INSTANTLY_API_KEY",
    "hubspot": "US_OUTBOUND_HUBSPOT_TOKEN",
    "slack": "US_OUTBOUND_SLACK_BOT_TOKEN",
    "claude": "US_OUTBOUND_CLAUDE_API_KEY",
    # Harry, 7 Oct 2026: the outside watchdog's Healthchecks.io ping URL (ops/watchdog.py). Optional, and
    # sealed like a key: anyone holding it could report the worker alive.
    "watchdog": "US_OUTBOUND_WATCHDOG_URL",
}


class ConfigError(Exception):
    """The environment is missing something the jobs need."""


class Secrets:
    """Reads keys from environment variables (SPEC 1.7; Railway sealed variables, not Secret Manager).

    env defaults to os.environ, read when a key is first needed; fetch(name) replaces it in
    tests and receives the variable name. Values are stripped and cached; an error names
    the variable, never a value.
    """

    def __init__(
        self,
        guard: Guard,
        env: Mapping[str, str] | None = None,
        fetch: Callable[[str], str] | None = None,
    ):
        self.guard, self._env, self._fetch = guard, env, fetch
        self._cache: dict[str, str] = {}

    def get(self, system: str) -> str:
        from us_outbound.clients.guard import Op

        name = SECRET_NAMES[system]
        if name not in self._cache:
            self.guard.authorize("secrets", Op("access", target=name))
            if self._fetch is not None:
                value = self._fetch(name)
            else:
                value = (os.environ if self._env is None else self._env).get(name)
            value = (value or "").strip()
            if not value:
                raise ConfigError(f"set {name} (a sealed variable on the Railway service; docs/railway-setup.md)")
            self._cache[name] = value
        return self._cache[name]


def boundaries_for(settings: Settings, settings_sheet_id: str = "", job: str = "", watchdog_url: str = "") -> Boundaries:
    """The containers a job may touch. watchdog_url: the ping URL heartbeat_check may GET (ops/watchdog.py), only
    for that job; "" for every other."""
    from us_outbound.clients.clay import CHECK_EMAIL_JOB, WORK_EMAIL_FUNCTION_ID

    g = settings.general
    live_mailboxes = [m for m in settings.mailboxes if m.status != "Retired"]
    # The US Outbound functions, and Clay's own Work Email while clay_email_fallback is yes (Harry, 2 Oct 2026),
    # or for the one lookup `us-outbound clay check-email` makes to confirm it before the switch goes on.
    clay_functions = {x for x in (g.clay_accounts_function_id, g.clay_contacts_function_id) if x}
    if g.clay_email_fallback or job == CHECK_EMAIL_JOB:
        clay_functions.add(WORK_EMAIL_FUNCTION_ID)
    return Boundaries(
        registry_addresses=frozenset(m.address.lower() for m in live_mailboxes),
        registry_account_ids=frozenset(m.instantly_account_id for m in live_mailboxes if m.instantly_account_id),
        registry_owners=frozenset(m.owner_name for m in live_mailboxes),
        hubspot_pipeline_id=g.hubspot_pipeline_id,
        hubspot_deal_stage_id=g.hubspot_deal_stage_id,
        clay_function_ids=frozenset(clay_functions),
        settings_sheet_id=settings_sheet_id or os.environ.get("US_OUTBOUND_SETTINGS_SHEET_ID", ""),
        alert_channel=g.alert_channel,
        dev_channel=g.dev_channel,
        escalation_email=g.escalation_email.strip().lower(),
        approver_slack_ids=frozenset(x.strip() for x in g.approver_slack_ids if x.strip()),
        # D11 (Harry, 1 Oct 2026): a mailbox's owner approves replies to that mailbox only.
        owner_slack_ids=frozenset((m.address.lower(), m.slack_id) for m in live_mailboxes if m.slack_id),
        watchdog_url=watchdog_url,
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
        from us_outbound.clients.slack import Slack, SlackOff

        try:
            token = self.secrets.get("slack")
        except ConfigError:
            if self.guard.live:
                raise
            return SlackOff()  # dry-run without a token: posts go to the log
        return Slack(self.guard, self.transport, token)

    @cached_property
    def watchdog(self):
        """The outside watchdog's pinger (ops/watchdog.py), to the URL the guard was given; None when it has none."""
        from us_outbound.clients.watchdog import Watchdog

        url = self.guard.bounds.watchdog_url
        return Watchdog(self.guard, self.transport, url) if url else None

    @cached_property
    def sheets(self):
        from us_outbound.clients.sheets import Sheets

        return Sheets(self.guard, self.transport, credentials=self.google_credentials)

    def _claude(self, model: str):
        from us_outbound.clients.claude import Claude

        return Claude(
            self.guard,
            self.store,
            api_key=None if self.claude_sdk else self.secrets.get("claude"),
            model=model,
            monthly_cap_usd=self.settings.general.claude_monthly_cap_usd,
            sdk=self.claude_sdk,
        )

    @cached_property
    def claude(self):
        """The writing model (claude_model): copy drafts, reply drafts."""
        return self._claude(self.settings.general.claude_model)

    @cached_property
    def claude_task(self):
        """The model for well-defined tasks (claude_task_model): copy QA, reply classification."""
        return self._claude(self.settings.general.claude_task_model)

    @cached_property
    def public(self):
        from us_outbound.clients.public import Public

        return Public(self.guard, self.transport)

    @cached_property
    def sites(self):
        """Public reads for the page reader (sources/pages.py): one attempt each, redirects handed back.

        A prospect's site that times out is not asked again in the same run, and each redirect
        is checked against the guard and robots.txt before it is followed.
        """
        from us_outbound.clients.http import RequestsTransport
        from us_outbound.clients.public import Public

        transport = self.transport
        if isinstance(transport, RequestsTransport):
            transport = RequestsTransport(attempts=1, follow_redirects=False)
        return Public(self.guard, transport)


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
