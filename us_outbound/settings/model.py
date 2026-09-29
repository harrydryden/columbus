"""Typed shape of the settings sheet "US Outbound – Settings" (SPEC 5).

validate.py turns the raw rows of each tab (lists of {column: text}) into these objects.
Jobs only ever see a Settings snapshot, never raw sheet text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from us_outbound.settings.conditions import Condition, ContextRules

TABS = ("General", "Signals", "Angles", "Industries", "States", "Roles", "Copy", "Mailboxes", "Overrides", "Tests")

# Source keys (SPEC 7). A signal row names one or more of these.
SOURCE_KEYS = (
    "apollo_org",
    "apollo_people",
    "apollo_jobs",
    "site_visits",
    "clay_careers",
    "clay_funding",
    "job_posts",
    "irs_bmf",
    "layoffs",
    "calendar",
)
# Sources whose facts carry page or posting text that term lists are matched against.
TEXT_SOURCES = frozenset({"clay_careers", "job_posts"})
# Field facts each source writes to signal_events (fact = field name, value = scalar).
# A condition on a signal may only use fields its sources provide; validation checks this.
SOURCE_FIELDS: dict[str, frozenset[str]] = {
    "apollo_org": frozenset(
        {"employees", "naics", "hq_state", "open_roles", "headcount_growth_12m", "days_since_funding",
         "funding_stage", "funding_amount_usd", "founded_year"}
    ),
    "apollo_people": frozenset(
        {"people_leader_count", "people_leader_days_in_title", "us_headcount", "ca_wa_share", "fl_share", "states_with_staff"}
    ),
    "apollo_jobs": frozenset({"open_people_roles", "open_roles"}),
    "site_visits": frozenset({"us_visits_30d", "pricing_or_demo_visits_30d", "top_paths", "days_since_first_visit"}),
    "clay_careers": frozenset({"values_page", "read_status"}),
    "clay_funding": frozenset({"days_since_funding", "funding_stage", "funding_amount_usd"}),
    "job_posts": frozenset({"open_roles"}),
    "irs_bmf": frozenset(
        {"revenue", "ntee", "is_501c3", "fiscal_year_end_month", "days_to_fiscal_year_start"}
    ),
    "layoffs": frozenset({"days_since_layoff"}),
    "calendar": frozenset({"month", "days_to_fiscal_year_start"}),
}

ACTIONS = ("Score", "Hold", "Exclude", "Suppress")
TIERS = ("Priority", "Standard", "Control", "Held", "Excluded")
MAILBOX_STATUSES = ("Warming", "Active", "Paused", "Retired")
COPY_STATUSES = ("draft", "approved", "retired")
TEST_STATUSES = ("planned", "running", "read", "stopped")
SIZE_BANDS = ("10-19", "20-49", "50-99", "100-249")


@dataclass(frozen=True)
class SendWindow:
    days: tuple[int, ...]  # 0 = Monday
    start: time
    end: time
    tz: str


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusive

    def __contains__(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True)
class General:
    """The General tab. Defaults are SPEC 5's; keys marked (build) were added by the build."""

    live_sending: bool = False
    daily_enrol_cap: int = 30
    control_share: float = 0.15
    priority_threshold: int = 50
    standard_threshold: int = 20
    score_cap: int = 100
    clay_monthly_credits: float = 0.0  # SPEC: a quarter of the Clay pool; 0 until Harry confirms the pool
    apollo_monthly_credits: int = 1500
    apollo_floor: int = 5000
    approver_slack_ids: tuple[str, ...] = ()
    escalation_email: str = "harry@spill.chat"
    escalation_hours: int = 24
    alert_channel: str = "#us-outbound"
    dev_channel: str = "#us-outbound-dev"  # (build)
    booking_link: str = "https://meetings.hubspot.com/harry336/us-demo-link"
    booking_page: str = "https://www.spill.chat/us/book-demo"
    demo_host: str = "Harry Dryden"
    postal_address: str = ""
    privacy_url: str = ""
    send_window: SendWindow = SendWindow((0, 1, 2, 3, 4), time(9), time(16), "America/New_York")
    blackout_dates: tuple[DateRange, ...] = ()
    recontact_person_months: int = 12
    recontact_account_months: int = 6
    stop_rule_accounts: int = 1500
    stop_rule_meetings: int = 5
    hubspot_pipeline: str = "Spill 3.0"
    hubspot_pipeline_id: str = ""  # (build) looked up through the API in phase 0
    hubspot_deal_stage: str = ""
    hubspot_deal_stage_id: str = ""  # (build)
    hubspot_owner_id: str = ""  # (build) Harry's HubSpot owner id
    clay_accounts_function_id: str = ""  # (build) "US Outbound – Accounts"
    clay_contacts_function_id: str = ""  # (build) "US Outbound – Contacts"
    clay_credits_per_account: float = 0.0  # (build) estimate until measured on the first 100
    apollo_credits_per_account: float = 1.0  # (build) estimate until measured
    claude_model: str = "claude-haiku-4-5"
    claude_monthly_cap_usd: float = 10.0


@dataclass(frozen=True)
class Signal:
    signal: str
    sources: tuple[str, ...]
    looks_for: str
    weight: int
    action: str
    counts_for_days: int
    active: bool
    context_rule: str = ""
    max_weight: int | None = None
    suggests_angle: str = ""
    opener: str = ""
    note: str = ""
    # Parsed from looks_for / context_rule by validate.py; exactly one of terms/condition is set.
    terms: tuple[str, ...] = ()
    condition: Condition | None = None
    context: ContextRules = field(default_factory=dict)

    @property
    def is_condition(self) -> bool:
        return self.condition is not None


@dataclass(frozen=True)
class Angle:
    angle: str
    order: int
    argument: str
    default_opener: str
    active: bool
    landing_page_override: str = ""


@dataclass(frozen=True)
class Industry:
    industry: str
    industry_group: str
    active: bool
    naics_prefixes: tuple[str, ...] = ()
    exclude_naics: tuple[str, ...] = ()
    apollo_keywords: tuple[str, ...] = ()
    landing_page_url: str = ""
    proof_point: str = ""
    priority: int = 99


@dataclass(frozen=True)
class State:
    state: str  # USPS code
    active: bool
    note: str = ""


@dataclass(frozen=True)
class Role:
    """Roles tab. first_choice_for_size and fallback_order use size ranges like "10-49".

    first_choice_for_size: the ranges where this role is contacted first, e.g. ("50-249",).
    fallback_order: {range: rank} for the ranges where it is a fallback, e.g. {"10-49": 2}.
    A role with neither for a company's size is not contacted at that size.
    """

    role: str
    titles: tuple[str, ...]
    first_choice_for_size: tuple[str, ...] = ()
    fallback_order: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CopyRow:
    copy_version: str
    angle: str
    step: int
    subject: str
    body: str
    status: str
    approved_by: str = ""
    sources: str = ""


@dataclass(frozen=True)
class Mailbox:
    address: str
    domain: str
    owner_name: str
    status: str
    daily_cap: int
    instantly_account_id: str = ""
    provider: str = ""
    owner_role: str = ""
    signature: str = ""
    added_on: date | None = None
    retire_after: date | None = None


@dataclass(frozen=True)
class Override:
    domain: str
    field: str
    value: str
    note: str = ""


@dataclass(frozen=True)
class Test:
    test_id: str
    hypothesis: str
    version_a: str
    version_b: str
    accounts_per_version: int
    status: str
    start_date: date | None = None
    read_date: date | None = None
    decision_rule: str = ""
    result: str = ""


@dataclass(frozen=True)
class Settings:
    general: General
    signals: tuple[Signal, ...] = ()
    angles: tuple[Angle, ...] = ()
    industries: tuple[Industry, ...] = ()
    states: tuple[State, ...] = ()
    roles: tuple[Role, ...] = ()
    copy: tuple[CopyRow, ...] = ()
    mailboxes: tuple[Mailbox, ...] = ()
    overrides: tuple[Override, ...] = ()
    tests: tuple[Test, ...] = ()
    synced_at: datetime | None = None
    versions: dict[str, Any] = field(default_factory=dict)  # tab -> effective_from of the version in force

    # -- lookups used across jobs --

    def angle(self, name: str) -> Angle | None:
        return next((a for a in self.angles if a.angle == name), None)

    def active_signals(self) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.active)

    def active_angles(self) -> tuple[Angle, ...]:
        return tuple(sorted((a for a in self.angles if a.active), key=lambda a: a.order))

    def industry(self, label: str) -> Industry | None:
        return next((i for i in self.industries if i.industry == label), None)

    def active_states(self) -> tuple[str, ...]:
        return tuple(s.state for s in self.states if s.active)

    def mailboxes_for(self, owner: str, status: str | None = "Active") -> tuple[Mailbox, ...]:
        return tuple(m for m in self.mailboxes if m.owner_name == owner and (status is None or m.status == status))

    def owners(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for m in self.mailboxes:
            if m.status != "Retired":
                seen.setdefault(m.owner_name)
        return tuple(seen)

    def running_test(self) -> Test | None:
        return next((t for t in self.tests if t.status == "running"), None)

    def approved_copy(self, copy_version: str, step: int) -> CopyRow | None:
        return next(
            (c for c in self.copy if c.copy_version == copy_version and c.step == step and c.status == "approved"),
            None,
        )

    def overrides_for(self, domain: str) -> dict[str, str]:
        return {o.field: o.value for o in self.overrides if o.domain == domain}
