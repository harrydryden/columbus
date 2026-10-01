"""Typed shape of the settings sheet "US Outbound – Settings" (SPEC 5).

validate.py turns the raw rows of each tab (lists of {column: text}) into these objects.
Jobs only ever see a Settings snapshot, never raw sheet text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from us_outbound.settings.conditions import Condition, ContextRules

TABS = (
    "General", "Signals", "Angles", "Industries", "States", "Roles", "Copy", "Mailboxes", "Overrides", "Tests",
    "Focus", "Named accounts",
)
# Tabs added after the sheet was first made (Harry, 30 Sep 2026): a sheet without them reads as
# if they were empty, so settings_sync keeps working until they are added.
OPTIONAL_TABS = frozenset({"Focus", "Named accounts"})

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
    "named",
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
    "named": frozenset({"named"}),  # the Named accounts tab (sources/named.py)
}
# Fields that count days up to the moment they were read. A source stores each as of its
# observed_at; scoring adds the days since then, so a fact read months ago still tells the truth.
AGED_FACTS = frozenset({"days_since_funding", "people_leader_days_in_title", "days_since_layoff", "days_since_first_visit"})

ACTIONS = ("Score", "Hold", "Exclude", "Suppress")
TIERS = ("Priority", "Standard", "Control", "Held", "Excluded")
MAILBOX_STATUSES = ("Warming", "Active", "Paused", "Retired")
COPY_STATUSES = ("draft", "approved", "retired")
COPY_STEPS = (1, 2, 3, 4)
GENERAL_COPY = "General"  # a Copy row for every industry: the fallback when an industry has no approved row
EMAIL_FORMATS = ("html", "text")
# The contacted roles (Roles tab) and the Copy-tab column holding each one's line for email 1.
ROLE_LINE_COLUMNS = {"People leader": "people_leader_line", "Founder or executive": "founder_line",
                     "Operations": "operations_line"}
QA_VERDICTS = ("pass", "fail")
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
    weekly_enrol_cap: int = 150  # (build) Harry, 30 Sep 2026: targets are weekly (Mon–Sun, UK); SPEC's 30 a day × 5
    control_share: float = 0.15
    priority_threshold: int = 50
    standard_threshold: int = 20
    score_cap: int = 100
    clay_monthly_credits: float = 2000.0  # Harry, 30 Sep 2026: 2,000 a month (about 500 a week); 0 = no Clay calls
    apollo_monthly_credits: int = 2000  # Harry, 30 Sep 2026: 2,000 a month (about 500 a week)
    apollo_floor: int = 5000
    approver_slack_ids: tuple[str, ...] = ()
    escalation_email: str = "harry@spill.chat"
    escalation_hours: int = 24
    alert_channel: str = "#us-outbound"
    dev_channel: str = "#us-outbound-dev"  # (build)
    booking_link: str = "https://meetings.hubspot.com/harry336/us-demo-link"
    booking_page: str = "https://www.spill.chat/us/book-demo"
    site_url: str = "https://www.spill.chat/us"  # (build) Harry, 1 Oct 2026: the "trusted by" link, and the page when an industry has none
    price_from: int = 195  # (build) Harry, 1 Oct 2026: "from $195 a month for the whole team", as on the website
    demo_host: str = "Harry Dryden"
    postal_address: str = ""
    privacy_url: str = ""
    send_window: SendWindow = SendWindow((0, 1, 2, 3, 4), time(9), time(16), "America/New_York")
    blackout_dates: tuple[DateRange, ...] = (  # SPEC 5; the same as the sheet default (defaults.py)
        DateRange(date(2026, 11, 23), date(2026, 11, 27)),
        DateRange(date(2026, 12, 18), date(2027, 1, 4)),
    )
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
    claude_model: str = "claude-opus-5-5"  # Harry, 30 Sep 2026: writing (copy drafts, reply drafts)
    claude_task_model: str = "claude-sonnet-5-5"  # (build) well-defined tasks: copy QA, reply classification
    claude_monthly_cap_usd: float = 10.0
    email_format: str = "html"  # (build) html: links and bullets; text: plain text, links written out


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
class IndustryPage:
    """The industry's spill.chat page, as material for writing its emails (Harry, 30 Sep 2026)."""

    blurb: str = ""
    intro: str = ""
    ticks: str = ""
    challenges: str = ""
    stats: str = ""  # context only: the copy may quote no statistic but the 30% figure
    benefits: str = ""
    features: str = ""
    faqs: str = ""
    customers: str = ""  # the page's logos heading: the kinds of teams Spill already supports

    def as_dict(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in PAGE_FIELDS}


PAGE_FIELDS = ("blurb", "intro", "ticks", "challenges", "stats", "benefits", "features", "faqs", "customers")


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
    page: IndustryPage = field(default_factory=lambda: IndustryPage())


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
class CopyStep:
    subject: str
    body: str


@dataclass(frozen=True)
class CopyRow:
    """One Copy-tab row: a four-email sequence for one industry, optionally for one role (Harry, 30 Sep 2026).

    industry is an Industries label, an industry group, or "General" (every industry). role is a
    Roles-tab role, or "" for every role. role_lines holds each role's line for {{role_line}}.
    qa is the QA step's verdict and the content hash it checked ("pass 1a2b3c4d"): editing the
    copy changes the hash, so edited copy is checked again before it can be sent.
    """

    copy_version: str
    industry: str
    status: str
    steps: tuple[CopyStep, ...]
    role: str = ""
    role_lines: Mapping[str, str] = field(default_factory=dict)
    approved_by: str = ""
    qa: str = ""
    qa_notes: str = ""
    sources: str = ""

    def step(self, n: int) -> CopyStep:
        return self.steps[n - 1]

    def content_hash(self) -> str:
        """Eight hex characters over everything the QA step reads."""
        import hashlib

        parts = [self.industry, self.role]
        for st in self.steps:
            parts += [st.subject.strip(), st.body.replace("\r\n", "\n").strip()]
        parts += [self.role_lines.get(r, "").strip() for r in ROLE_LINE_COLUMNS]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:8]

    @property
    def qa_verdict(self) -> str:
        return self.qa.split()[0].lower() if self.qa.split() else ""

    @property
    def qa_hash(self) -> str:
        parts = self.qa.split()
        return parts[1].lower() if len(parts) > 1 else ""

    @property
    def qa_current(self) -> bool:
        """True when QA passed this exact copy."""
        return self.qa_verdict == "pass" and self.qa_hash == self.content_hash()


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
class Focus:
    """A row of the Focus tab: the share of each week's enrolment to give an industry group."""

    industry_group: str
    share: float  # 0 to 1
    note: str = ""


@dataclass(frozen=True)
class NamedAccount:
    """A row of the Named accounts tab: a company Harry wants approached."""

    domain: str
    name: str = ""
    note: str = ""


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
    focus: tuple[Focus, ...] = ()
    named_accounts: tuple[NamedAccount, ...] = ()
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

    def industry_group_of(self, account: Mapping[str, Any]) -> str:
        """The account's industry group: its own, else its label's on the Industries tab."""
        group = str(account.get("industry_group") or "").strip()
        if group:
            return group
        ind = self.industry(str(account.get("industry") or ""))
        return ind.industry_group if ind else ""

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

    def copy_row(self, copy_version: str) -> CopyRow | None:
        return next((c for c in self.copy if c.copy_version == copy_version), None)

    def industry_page_url(self, label: str) -> str:
        """The page of an Industries label or group (the group's own row), else ""."""
        row = self.industry(label)
        if row is None:
            row = next((i for i in self.industries if i.industry_group == label and i.industry == label), None)
        return row.landing_page_url.strip() if row else ""

    def overrides_for(self, domain: str) -> dict[str, str]:
        return {o.field: o.value for o in self.overrides if o.domain == domain}
