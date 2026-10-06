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
    "careers_pages",  # our own read of the company's careers and benefits pages (sources/pages.py; Harry, 2 Oct 2026)
    "clay_funding",
    "job_posts",
    "irs_bmf",
    "layoffs",
    "calendar",
    "named",
    "lookalike",  # how the account compares with Spill's HubSpot customers: the lookalike fit (sources/lookalikes.py)
    "lookalike_lead",  # found by Apollo as like a Spill customer, monthly (sources/lookalike_leads.py; Harry, 5 Oct 2026)
)
# Sources whose facts carry page or posting text that term lists are matched against.
TEXT_SOURCES = frozenset({"clay_careers", "careers_pages", "job_posts"})
# Field facts each source writes to signal_events (fact = field name, value = scalar).
# A condition on a signal may only use fields its sources provide; validation checks this.
SOURCE_FIELDS: dict[str, frozenset[str]] = {
    # technologies, keywords, apollo_industry and description: lists and text from Apollo's organization
    # record (sources/apollo_universe.py); scoring/tiers.py reads keywords and apollo_industry for partners,
    # and the opener's optional "what they do" phrase reads keywords and description (enrol/openers.py).
    # employees and size_band are also read from the account's own columns when no fact carries them
    # (scoring/score.py ACCOUNT_FACTS): Apollo's search sends no employee count, only the band searched.
    "apollo_org": frozenset(
        {"employees", "size_band", "naics", "hq_state", "open_roles", "headcount_growth_12m", "days_since_funding",
         "funding_stage", "funding_amount_usd", "founded_year", "technologies", "keywords", "apollo_industry",
         "description",
         # shrinking, flat, growing, fast or unknown: 12-month headcount growth, from headcount_growth_12m or
         # Apollo's search by growth range (sources/lookalikes.py; Harry, 5 Oct 2026).
         "headcount_growth_band"}
    ),
    # people_search_coverage (the people Apollo holds over the employees) and people_found: sources/apollo_people.py
    # (Harry, 5 Oct 2026), so a People leader count of 0 is read only where Apollo's data is deep enough.
    "apollo_people": frozenset(
        {"people_leader_count", "people_leader_days_in_title", "people_search_coverage", "people_found",
         "us_headcount", "ca_wa_share", "fl_share", "states_with_staff"}
    ),
    "apollo_jobs": frozenset({"open_people_roles", "open_roles", "posting_titles"}),  # sources/apollo_jobs.py
    "site_visits": frozenset({"us_visits_30d", "pricing_or_demo_visits_30d", "top_paths", "days_since_first_visit"}),
    "clay_careers": frozenset({"values_page", "read_status"}),
    "careers_pages": frozenset({"values_page", "read_status"}),  # the facts clay_careers writes (docs/pipeline.md)
    "clay_funding": frozenset({"days_since_funding", "funding_stage", "funding_amount_usd"}),
    "job_posts": frozenset({"open_roles"}),
    "irs_bmf": frozenset(
        {"revenue", "ntee", "is_501c3", "fiscal_year_end_month", "days_to_fiscal_year_start"}
    ),
    "layoffs": frozenset({"days_since_layoff"}),
    "calendar": frozenset({"month", "days_to_fiscal_year_start"}),
    "named": frozenset({"named"}),  # the Named accounts tab (sources/named.py)
    # sources/lookalikes.py (Harry, 1 Oct 2026). lookalike_fit (0 to 100) and its three parts (0 to 1; size and
    # growth null when unknown): Harry, 5 Oct 2026.
    "lookalike": frozenset({"lookalike_active", "lookalike_strength", "lookalike_fit", "lookalike_industry_fit",
                            "lookalike_size_fit", "lookalike_growth_fit"}),
    # The company itself was found as like a Spill customer (sources/lookalike_leads.py). Its lookalike_lead fact
    # (the seed's group, band and country) is a dict, which no condition reads, so the signal reads this flag.
    "lookalike_lead": frozenset({"found_as_lookalike"}),
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
# Tokenized openers (docs/roadmap.md §4 item 2; Harry, 2 Oct 2026): the Signals-tab column holding each
# copy role's opener line, filled at enrol time from the account's stored facts (enrol/openers.py). A
# blank cell falls back to the signal's plain opener. opener_self is the line for a contact who is the
# person the signal is about (the new People leader).
OPENER_COLUMNS = {"People leader": "opener_people", "Founder or executive": "opener_founder",
                  "Operations": "opener_ops"}
OPENER_SELF_COLUMN = "opener_self"
# The tokens each kind of opener line may use. The plain opener column keeps SPEC 5's one placeholder.
PLAIN_OPENER_TOKENS = ("evidence",)
OPENER_TOKENS = ("company", "open_roles", "posting_title", "people_title", "growth", "city", "evidence", "provider",
                 "page")
# Tokens retired from the lines (Harry, 2 Oct 2026: funding is a signal, never a line). A sheet line that still
# has one validates, so the settings in force stay usable, but it never fills, and `copy check` names it.
RETIRED_OPENER_TOKENS = ("funding_stage",)
FOCUS_LINE_TOKENS = ("company", "focus", "city")  # General opener_focus_line
# The generic opener (Harry, 2 Oct 2026): General keys holding the line for an account with no signal line (the
# General angle, Control among it, or a signal whose lines all fall through), by copy role, then the plain one.
GENERIC_OPENER_KEYS = {"People leader": "opener_generic_people", "Founder or executive": "opener_generic_founder",
                       "Operations": "opener_generic_ops"}
GENERIC_OPENER_KEY = "opener_generic"
GENERIC_LINE_TOKENS = ("company", "city")
# The {{variables}} General email1_subject may use (Harry, 5 Oct 2026: a personal subject for email 1).
EMAIL1_SUBJECT_VARIABLES = ("company", "first_name")
# Condition fields that make a signal about one person, so its opener_self line can apply.
PERSON_FIELDS = frozenset({"people_leader_days_in_title"})
# The Roles tab's two size columns and the range each covers (SPEC 5 "Who to contact first").
ROLE_ORDER_COLUMNS = {"order_10_49": "10-49", "order_50_249": "50-249"}
QA_VERDICTS = ("pass", "fail")
TEST_STATUSES = ("planned", "running", "read", "stopped")
# Company size bands: SPEC 2's four (10-19 to 100-249) inside a ladder that runs further each way, so the
# General min_employees and max_employees can move (Harry, 6 Oct 2026: "update that to say 5-500 easily").
# A band's label never changes; Apollo is searched with the band clipped to the General range.
SIZE_LADDER = (1, 5, 10, 20, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
SIZE_BANDS = tuple(f"{lo}-{nxt - 1}" for lo, nxt in zip(SIZE_LADDER, SIZE_LADDER[1:]))


def band_bounds(band: str) -> tuple[int, int]:
    """"50-99" -> (50, 99)."""
    lo, _, hi = band.partition("-")
    return int(lo), int(hi)
# General clay_verification (Harry, 1 Oct 2026): required is SPEC 2 (every account through Clay before it
# can be emailed); skip lets verify_accounts verify on Apollo data and HubSpot until the Clay functions exist.
CLAY_REQUIRED, CLAY_SKIP = "required", "skip"
CLAY_VERIFICATION_MODES = (CLAY_REQUIRED, CLAY_SKIP)


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
    # (build) Harry, 2 Oct 2026: no, every email waits for an approver's ✅ in Slack before its lead is added
    # to Instantly (enrol/approvals.py); yes, enrol adds leads straight away, as before.
    auto_send: bool = False
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
    send_window: SendWindow = SendWindow((0, 1, 2, 3, 4), time(9), time(16), "America/New_York")
    blackout_dates: tuple[DateRange, ...] = (  # SPEC 5; the same as the sheet default (defaults.py)
        DateRange(date(2026, 11, 23), date(2026, 11, 27)),
        DateRange(date(2026, 12, 18), date(2027, 1, 4)),
    )
    recontact_person_months: int = 12
    recontact_account_months: int = 6
    stop_rule_accounts: int = 1500
    stop_rule_meetings: int = 5
    stop_rule_bounce_rate: float = 0.03  # (build) the stop rule's account-level bounce share (learn/kill_rules.py)
    stop_rule_complaint_rate: float = 0.003  # (build) and spam-complaint share: Google's 0.3% line
    optout_tested: bool = False  # (build) Harry: yes once a seed-inbox test shows the unsubscribe link works (golive)
    hubspot_pipeline: str = "Spill 3.0"
    hubspot_pipeline_id: str = ""  # (build) looked up through the API in phase 0
    hubspot_deal_stage: str = ""
    hubspot_deal_stage_id: str = ""  # (build)
    hubspot_owner_id: str = ""  # (build) Harry's HubSpot owner id
    clay_accounts_function_id: str = ""  # (build) "US Outbound – Accounts"
    clay_contacts_function_id: str = ""  # (build) "US Outbound – Contacts"
    clay_credits_per_account: float = 0.0  # (build) estimate until measured on the first 100
    clay_verification: str = CLAY_SKIP  # (build) Harry, 1 Oct 2026: skip until the Clay functions exist, then required
    clay_email_fallback: bool = False  # (build) Harry, 2 Oct 2026: Clay's Work Email for Apollo's misses (contacts/pick.py)
    # (build) Harry, 6 Oct 2026: Clay's cross-check of the HQ state and size Apollo leaves in doubt, before the weekly
    # hand-check (us_outbound/clay_cross_check.py), through clay_accounts_function_id; no until that function is built.
    clay_cross_check: bool = False
    apollo_credits_per_account: float = 1.0  # (build) estimate until measured
    # (build) Harry, 2 Oct 2026: the industry groups whose queue accounts apollo_enrich enriches for funding and
    # an exact headcount (sources/apollo_enrich.py), where funding is common; blank enriches none.
    apollo_enrich_groups: tuple[str, ...] = ("Technology & Startups",)
    # (build) Harry, 5 Oct 2026: the website-visit signals (sources/site_visits.py). The site Apollo's visitor tracker
    # is on (blank: the job reads nothing), and the pages that count as a US-site visit and as a pricing or demo
    # visit: Apollo matches a page whose path contains any of them.
    site_visit_domain: str = "spill.chat"
    site_visit_us_paths: tuple[str, ...] = ("/us",)
    site_visit_intent_paths: tuple[str, ...] = ("/us/pricing", "/us/demo", "/us/book")
    # (build) Harry, 6 Oct 2026: the company size range, in employees, both ends included (SPEC 2: 10 to 249).
    # Every source searches it, verify_accounts holds accounts to it, and the hand-check's size edges follow it.
    min_employees: int = 10
    max_employees: int = 249
    claude_model: str = "claude-opus-5-5"  # Harry, 30 Sep 2026: writing (copy drafts, reply drafts)
    claude_task_model: str = "claude-sonnet-5-5"  # (build) well-defined tasks: copy QA, reply classification
    claude_monthly_cap_usd: float = 10.0
    email_format: str = "html"  # (build) html: links and bullets; text: plain text, links written out
    # (build) Tokenized openers (Harry, 2 Oct 2026; enrol/openers.py): the share of accounts held out with no
    # opener, so replies can compare opener against none; and the optional "what they do" line.
    opener_holdout_share: float = 0.3
    # (build) The generic opener (Harry, 2 Oct 2026: "ever more pressure in our work and personal lives"), for an
    # account with no signal line: the contact's copy role's line, then the plain one (GENERIC_OPENER_KEYS).
    opener_generic: str = "Pressure at work and at home seems to keep rising, and most teams feel it somewhere."
    opener_generic_people: str = (
        "Pressure at work and at home seems to keep rising, and the two rarely stay separate for long.")
    opener_generic_founder: str = (
        "Work and life both seem to ask more of people every year, and even the strongest teams feel it.")
    opener_generic_ops: str = (
        "Pressure in work and life seems to keep rising, and it hardly ever waits for a convenient week.")
    opener_focus: bool = False
    opener_focus_line: str = "I came across {company} and its work on {focus}."
    # (build) Harry, 5 Oct 2026: a personal subject for email 1, as a measured split. Mail filters (Superhuman,
    # Gmail) and people read a short, lower-case subject about the recipient as personal mail; the Copy rows'
    # subjects read like headlines. email1_subject_share of accounts, by a hash of the account id (its own salt,
    # so it is independent of the opener holdout), get email1_subject instead of the Copy row's s1_subject;
    # emails 2 to 4 keep the Copy row's subjects (render.subject_arm; contacts.subject_arm).
    email1_subject: str = "support for the {{company}} team"
    email1_subject_share: float = 0.5


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
    # Tokenized openers (Harry, 2 Oct 2026): {copy role: line} from the OPENER_COLUMNS, and the line for a
    # contact who is the person the signal is about. A cell may hold alternatives, one per line.
    role_openers: Mapping[str, str] = field(default_factory=dict)
    opener_self: str = ""

    @property
    def is_condition(self) -> bool:
        return self.condition is not None

    @property
    def is_about_a_person(self) -> bool:
        """True when the condition reads a fact about one person (PERSON_FIELDS), so opener_self can apply."""
        return self.condition is not None and bool(self.condition.fields & PERSON_FIELDS)


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
    """A Roles-tab row: a group of titles, the copy its contacts get, and its place in the order by size.

    Harry, 1 Oct 2026: wide title lists, and an order for each size band (SPEC 5 "Who to contact
    first"), led by seniority. A copy role can have more than one row: "HR manager" comes fourth
    at 50 to 249 staff, after the senior People leaders, but its contacts get the People leader copy.

    role:            the row's name, unique on the tab (the settings key).
    titles:          matched as whole phrases (clean/people.py).
    order:           {size range: rank}, e.g. {"10-49": 1, "50-249": 2}; 1 is contacted first. A size
                     with no rank is not contacted (Finance has none).
    copy_role:       the role whose Copy-tab line its contacts get (ROLE_LINE_COLUMNS); blank means role.
    industry_groups: when set, the titles count only at accounts in these groups ("Partner" at law firms).
    """

    role: str
    titles: tuple[str, ...]
    order: Mapping[str, int] = field(default_factory=dict)
    copy_role: str = ""
    industry_groups: tuple[str, ...] = ()

    @property
    def writes_as(self) -> str:
        """The role written on contacts.role, which picks the copy's role line."""
        return self.copy_role or self.role

    def order_at(self, employees: int | None) -> int | None:
        """This row's rank at a company of this size, or None when it is not contacted there. A company outside
        every range on the tab (General min_employees or max_employees set wider: 5, 500) takes the order of the
        nearest range, so a 7-person company is read as 10-49 and a 400-person one as 50-249."""
        if employees is None:
            return None
        if self.order:
            spans = [band_bounds(rng) for rng in ROLE_ORDER_COLUMNS.values()]
            employees = min(max(employees, min(lo for lo, _ in spans)), max(hi for _, hi in spans))
        ranks = [rank for rng, rank in self.order.items() if _in_range(employees, rng)]
        return min(ranks) if ranks else None

    def counts_in(self, industry_group: str) -> bool:
        """Whether the titles count at an account in this industry group."""
        if not self.industry_groups:
            return True
        return industry_group.casefold() in {g.casefold() for g in self.industry_groups}

    @property
    def first_choice_for_size(self) -> tuple[str, ...]:
        """The SPEC 5 view: the size ranges where this row comes first."""
        return tuple(rng for rng, rank in self.order.items() if rank == 1)

    @property
    def fallback_order(self) -> dict[str, int]:
        """The SPEC 5 view: {size range: rank} where this row is a fallback."""
        return {rng: rank for rng, rank in self.order.items() if rank > 1}


def _in_range(employees: int, size_range: str) -> bool:
    lo, _, hi = size_range.partition("-")
    return int(lo) <= employees <= int(hi)


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
    # The owner's Slack user id (build; decision D11, approved by Harry, 1 Oct 2026): the owner may
    # approve replies to their own mailbox, beside approver_slack_ids. Blank: only those approve.
    slack_id: str = ""


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

    def size_bands(self) -> tuple[str, ...]:
        """The SIZE_BANDS the General size range touches, smallest first."""
        lo, hi = self.general.min_employees, self.general.max_employees
        return tuple(b for b in SIZE_BANDS if band_bounds(b)[0] <= hi and band_bounds(b)[1] >= lo)

    def employee_range(self, band: str) -> str:
        """Apollo's employee range for a band, clipped to the General size range: "250,499" -> "250,300"."""
        lo, hi = band_bounds(band)
        return f"{max(lo, self.general.min_employees)},{min(hi, self.general.max_employees)}"

    def size_in_range(self, employees: int | float) -> bool:
        return self.general.min_employees <= employees <= self.general.max_employees

    def band_in_range(self, band: str) -> bool:
        """Whether a band (from a size-filtered search) lies in the General range, at least in part."""
        return band in self.size_bands()

    def size_range_text(self) -> str:
        """"10 to 249", as the reasons and prompts write it."""
        return f"{self.general.min_employees} to {self.general.max_employees}"

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
