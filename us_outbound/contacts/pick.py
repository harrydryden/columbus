"""The pick_contacts job: one verified contact for each account about to be enrolled (SPEC 9; SPEC 5 Roles).

Harry, 1 Oct 2026: "Targeting the right contact is paramount... the closer to seniority and
decision maker the better, whilst also having the time to engage in understanding Spill."
Runs at 05:30 UK on weekdays, before enrol at 12:00.

  1. Accounts: verified, in Priority, Standard or Control, that enrol could take (domain not
     suppressed or a partner, industry on), with no sendable contact; in enrol's queue order,
     and only as many as the next two send days need at the weekly target's pace
     (docs/pipeline.md change 5, approved 30 Sep 2026). An account where nobody suitable was
     found is tried again after RETRY_DAYS.
  2. Budget (SPEC 1.6): before the batch, today's share of the month's Apollo budget (budget.py)
     and Apollo's own balance against apollo_floor; before each reveal, what is left of both.
     The batch stops when either runs out. And no reveal at all while a kill rule pauses the apollo
     email source (apollo_paused, below).
  3. Search (0 credits): Apollo People API Search at the organization, for the titles of every
     Roles-tab row contacted at the account's size, for people in the United States with an
     email Apollo has verified. The People leaders it finds are written as apollo_people facts,
     unless apollo_people's own fuller search (sources/apollo_people.py) is less than 30 days old.
  4. Rank (clean/people.rank_person): the Roles-tab order for the size, then seniority, then
     how well the title matches, then the newest in role. Left out: titles never contacted at
     this size, junior titles as a People leader, people located in CA, WA or outside the US
     (SPEC 1.5), and people already revealed for the account.
  5. Reveal: people/bulk_match on the top candidate, one at a time (1 credit each; written to
     credit_ledger before the call and settled after it). Kept only if Apollo calls the email
     verified, it is at the company's own domain, enrol could send to it (enrol.contact_block:
     not CA or WA, not suppressed, not a personal domain or shared inbox) and it is nobody's
     contact yet. Otherwise the next candidate, at most MAX_REVEALS per account.
  6. Write: one contact per account (v1); or, when nobody suitable exists, a contact_pick fact
     saying why, which limits.py reports as "no suitable contact".
  7. Second contacts (General second_contact, no by default; enrol/second.py; Harry, 6 Oct 2026):
     after the first contacts, and only with what they leave of the lookahead, enrolled accounts of
     second_contact_min_employees or more staff whose second contact is due within the next two send
     days get the same search, ranking, checks and reveals, leaving out people of the first contact's
     copy role. Their reveals count against the Apollo budget like any; their contact_pick fact has
     slot 2.

Every reveal and every outcome is a signal_events fact with source pick_contacts (no names or
emails), so nobody is paid for twice. SOURCE is not a SPEC 7 source key, so no Signals row can
score these facts.

Dry-run and live are the same: the job writes only to the database and its Apollo calls are
reads, which the guard allows in dry-run, so reveals spend credits in both, within the budget
(as verify_in_clay spends Clay's). The scheduler runs it without --live.

A kill rule pausing the apollo email source (learn/holds.paused_sources: Apollo's addresses bounced;
learn/kill_rules.py) stops the run before any search, reveal or credit, and the run's summary and so its
heartbeat say why (apollo_paused; Harry, 7 Oct 2026), as the clay source's pause stops Clay's lookups
(clay_room). Clay alone is not used meanwhile, even with clay_email_fallback = yes and the clay source not
paused: Clay is the fallback for Apollo's misses and catch-alls (Harry, 2 Oct 2026: Clay narrowed to that),
looked up only after an Apollo reveal, and making it the main source while Harry checks why Apollo's addresses
bounced would spend Clay's credits at a rate nobody chose. Contacts already found stay ready, and enrol still
takes those whose source is not paused. `us-outbound killrules clear ITEM_ID --live` lifts the pause.

The Clay email waterfall (SPEC 9: "misses and catch-alls go to the Clay Contacts function";
Harry, 2 Oct 2026: Clay narrowed to this), behind General clay_email_fallback (default no, as
Clay's server-callable path is unconfirmed: clients/clay.py, docs/phase0-facts.md). When it is
yes and Apollo's reveal of a candidate gave no email or one Apollo doesn't call verified (a miss,
or a catch-all), that person is looked up in Clay once: the "US Outbound – Contacts" function
when clay_contacts_function_id is set (SPEC 8's inputs: full_name, domain, linkedin_url, title),
otherwise the workspace's Work Email function as it is (its own inputs: Full Name, Company Domain,
Social Profile URL, Company Name; clients/clay.WORK_EMAIL_INPUTS; it charges only when it finds an
email). At most one lookup an account and CLAY_LOOKUPS_PER_RUN a run, each within today's share
of clay_monthly_credits (budget.py; credit_ledger, reserved before the call and settled after it),
none while a kill rule pauses the clay source (learn/holds.paused_sources) or the Clay API key is
not set, and none for the rest of the run once CLAY_MAX_ERRORS lookups have failed (a wrong key or
a function without "API & CLI" would otherwise spend every lookup's reserve). Only a "valid"
result is used: catch_all_valid waits for pipeline change 8 (whether a catch-all is sendable with
Instantly's risky contacts off), and the bounce kill rule pauses the clay source on its own if its
addresses bounce. The contact is kept by the same checks as Apollo's, with email_source = clay;
the lookup is a contact_clay fact. `us-outbound clay check-email` makes one Work Email lookup by
hand, to confirm all this before clay_email_fallback goes on (ops/clay_check.py).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Container, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound import budget, ledger
from us_outbound.clean.domains import canonical_domain
from us_outbound.clean.people import SENIORITY, Ranked, clean_person_name, company_size, rank_person, state_code
from us_outbound.clients.apollo import credits_left, people_in, while_full_page
from us_outbound.clients.clay import (
    WORK_EMAIL_FUNCTION_ID,
    ClayError,
    parse_contacts_output,
    parse_work_email_output,
    work_email_inputs,
)
from us_outbound.clients.db import Store, new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import enrol, queue, second
from us_outbound.learn import holds
from us_outbound.logs import hash_email, log
from us_outbound.settings.model import Role, Settings
from us_outbound.timeparse import iso_date, utc

JOB = "pick_contacts"
SOURCE = "pick_contacts"  # signal_events.source of this job's facts
REVEAL_FACT = "contact_reveal"  # one per reveal: who, the row they came under, the status, kept or why not
OUTCOME_FACT = "contact_pick"  # one per account tried: picked, or none and why
PICKED, NO_CONTACT = "picked", "none"
STOPPED = "stopped"  # the batch stopped partway through the account; it is tried again next run

MAX_REVEALS = 2  # Harry, 1 Oct 2026: at most two reveals an account
# PHASE0-CONFIRM: people/bulk_match spends 1 lead credit per revealed email, and reports it as
# credits_consumed; a match with no email may cost nothing.
REVEAL_CREDITS = 1.0
ACCEPTED_STATUS = "verified"  # Apollo's email_status (Harry, 1 Oct 2026: verified only)
EMAIL_SOURCE = "apollo"
LOOKAHEAD_SEND_DAYS = 2  # docs/pipeline.md change 5: reveal only for the next two enrolment days
RETRY_DAYS = 14  # an account with nobody suitable is searched again after this (Apollo's data moves)
REVEAL_AGAIN_DAYS = 90  # a person revealed for an account is not paid for again within this
SEARCH_PER_PAGE = 100
SEARCH_PAGES = 2  # 200 people: more than a 249-person company has with these titles
MAX_ERRORS = 3  # Apollo errors before the batch stops
LIST_LIMIT = 100  # per-account lists in the summary
ID_CHUNK = 1000
LEDGER_NOTE = "email reveal (people/bulk_match)"
# The Clay email waterfall (Harry, 2 Oct 2026).
CLAY_SOURCE = "clay"  # contacts.email_source, and the kill rules' source (learn/kill_rules.py SOURCES)
CLAY_FACT = "contact_clay"  # one per Clay lookup: who, the status, kept or why not
# PHASE0-CONFIRM: what one Work Email lookup costs. Reserved before the call, settled at what Clay reports
# (credits_used), or this when it found an email and reported nothing, or 0 when it found none.
CLAY_RESERVE = 2.0
CLAY_ACCEPTED = frozenset({"valid"})  # catch_all_valid waits for pipeline change 8
CLAY_LOOKUPS_PER_RUN = 25  # each lookup polls Clay until it finishes, inside the job's 60 minutes
CLAY_MAX_ERRORS = 3  # failed lookups before the run stops asking Clay
CLAY_LEDGER_NOTE = "email waterfall (Clay)"

# api_search reads "United States" in person_locations as the whole country, and honours
# contact_email_status: confirmed live 2 Oct 2026 (CA and WA people came back, and every reveal was
# verified). Per-state strings,
# which could leave out CA and WA in the search itself, are untested, so CA and WA are left out
# after the search (when it gives a state) and after the reveal (always).
US_LOCATION = "United States"
SEARCH_EMAIL_STATUSES = ("verified",)
US_COUNTRIES = frozenset({"united states", "united states of america", "us", "usa"})


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _chunks(items: Sequence[str], n: int = ID_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _fact(account_id: str, fact: str, value: Mapping[str, Any], now: datetime) -> dict:
    return {"event_id": new_id(), "account_id": account_id, "source": SOURCE, "fact": fact, "value": dict(value),
            "quote": "", "source_url": "", "observed_at": now}


# -- which accounts ---------------------------------------------------------------------------


def lookahead(settings: Settings) -> int:
    """Accounts the next LOOKAHEAD_SEND_DAYS send days enrol, at the weekly target's pace (30 a day at 150 a week)."""
    days = max(1, len(settings.general.send_window.days))
    return LOOKAHEAD_SEND_DAYS * math.ceil(settings.general.weekly_enrol_cap / days)


def waiting(ctx: Context) -> tuple[list[dict], int]:
    """(the accounts enrol could take that have no sendable contact, in queue order; how many already have one)."""
    s, store = ctx.settings, ctx.store
    accounts = store.select("accounts", {"status": "verified", "tier": list(queue.QUEUE_TIERS)})
    domains, hashes = enrol.suppressed(ctx)
    partners = {_lower(p.get("domain")) for p in store.select("partners")}
    contacts: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks([a["account_id"] for a in accounts]):
        for c in store.select("contacts", {"account_id": list(chunk)}):
            contacts[c["account_id"]].append(c)
    need, ready = [], 0
    for a in accounts:
        if enrol.account_block(a, s, domains, partners, frozenset()):
            continue
        if any(enrol.contact_block(c, domains, hashes) is None for c in contacts.get(a["account_id"], [])):
            ready += 1
        else:
            need.append(a)
    need.sort(key=lambda a: queue.order_key(a, s))
    return need, ready


@dataclass
class History:
    """What earlier runs found, from this job's facts."""

    outcomes: dict[str, tuple[datetime, dict]] = field(default_factory=dict)  # account_id -> its latest outcome
    revealed: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # account_id -> person ids

    def cooling(self, account_id: str, now: datetime) -> bool:
        """Nobody suitable was found at the account less than RETRY_DAYS ago."""
        latest = self.outcomes.get(account_id)
        return bool(latest) and latest[1].get("outcome") == NO_CONTACT and now - latest[0] < timedelta(days=RETRY_DAYS)


def history(store: Store, now: datetime) -> History:
    h = History()
    for e in store.select("signal_events", {"source": SOURCE}):
        t, aid, value = utc(e.get("observed_at")), str(e.get("account_id") or ""), e.get("value")
        if t is None or not aid or not isinstance(value, Mapping):
            continue
        if e.get("fact") == OUTCOME_FACT and (aid not in h.outcomes or t >= h.outcomes[aid][0]):
            h.outcomes[aid] = (t, dict(value))
        elif e.get("fact") == REVEAL_FACT and now - t < timedelta(days=REVEAL_AGAIN_DAYS) and value.get("apollo_person_id"):
            h.revealed[aid].add(str(value["apollo_person_id"]))
    return h


def no_contact(store: Store, now: datetime) -> dict[str, str]:
    """account_id -> why nobody suitable was found, for accounts tried again later (limits.py)."""
    h = history(store, now)
    return {aid: str(v.get("reason") or "") for aid, (_, v) in h.outcomes.items() if h.cooling(aid, now)}


# -- search and rank --------------------------------------------------------------------------


def search_titles(roles: Sequence[Role], employees: int | None, industry_group: str = "") -> list[str]:
    """Every title of the Roles-tab rows contacted at this size (and in this industry group), for the search."""
    out: list[str] = []
    for r in roles:
        if r.order_at(employees) is not None and r.counts_in(industry_group):
            out += [t for t in r.titles if t not in out]
    return out


def organization_filter(account: Mapping[str, Any]) -> dict[str, Any]:
    """The account's organization for People API Search: its Apollo id when it has one, else its domain."""
    org = str(account.get("apollo_org_id") or "").strip()
    if org:
        return {"organization_ids": [org]}
    return {"q_organization_domains_list": [_lower(account.get("domain"))]}


def search_filters(account: Mapping[str, Any], titles: Sequence[str], *, verified_only: bool = True) -> dict[str, Any]:
    """People API Search at the account's organization, for these titles: people in the US with a verified email.

    Similar titles stay in (Apollo's default): the search is free, and every title is checked
    against the Roles tab here. verified_only=False drops the email filter, for apollo_people's
    count of everyone in a People role (sources/apollo_people.py), not only those we could email.
    """
    f: dict[str, Any] = {
        "person_titles": list(titles),
        "include_similar_titles": True,
        "person_locations": [US_LOCATION],
    }
    if verified_only:
        f["contact_email_status"] = list(SEARCH_EMAIL_STATUSES)
    return {**f, **organization_filter(account)}


def search(ctx: Context, account: Mapping[str, Any], titles: Sequence[str]) -> list[dict]:
    """People at the account who might be contacted (0 credits), each once, up to SEARCH_PAGES pages.

    api_search returns its rows under "people", each with the person "id" bulk_match takes, their
    title, and (when given) state, country and employment_history: confirmed live 2 Oct 2026.
    """
    people: list[dict] = []
    seen: set[str] = set()
    filters = search_filters(account, titles)
    apollo = ctx.clients.apollo
    pages = apollo.iter_pages(lambda page: apollo.search_people(filters, page=page, per_page=SEARCH_PER_PAGE),
                              more=while_full_page, rows=people_in, per_page=SEARCH_PER_PAGE, max_pages=SEARCH_PAGES)
    for page in pages:
        for p in page.rows:
            pid = str(p.get("id") or "")
            if pid and pid not in seen:
                seen.add(pid)
                people.append(p)
    return people


def days_in_role(person: Mapping[str, Any], today: date) -> int | None:
    """Days since the person started their current job, when Apollo gives it (employment_history)."""
    for job in person.get("employment_history") or ():
        if isinstance(job, Mapping) and job.get("current") and job.get("start_date"):
            start = iso_date(job["start_date"])
            if start is None:
                continue
            return max(0, (today - start).days)
    return None


def location_block(person: Mapping[str, Any]) -> str | None:
    """Why the person may not be emailed for where they are (SPEC 1.5), when Apollo says; None otherwise."""
    country = _lower(person.get("country"))
    if country and country not in US_COUNTRIES:
        return "located outside the US"
    if person.get("state") and state_code(str(person["state"])) in enrol.NEVER_STATES:
        return "contact in CA or WA"
    return None


@dataclass
class Candidate:
    person: dict  # the search row
    ranked: Ranked

    @property
    def id(self) -> str:
        return str(self.person.get("id") or "")


def account_size(account: Mapping[str, Any]) -> int | None:
    return company_size(account.get("employees"), account.get("size_band"))


PEOPLE_SOURCE = "apollo_people"  # the source the People-leader signals read (settings/model.py SOURCE_FIELDS)
PEOPLE_LEADER = "People leader"
NEWEST_LEADER_FACT = "people_leader_newest"  # who the newest People leader is (enrol/openers.py)


def people_leaders(account: Mapping[str, Any], people: Sequence[Mapping[str, Any]], settings: Settings,
                   today: date) -> list[tuple[int | None, Mapping[str, Any]]]:
    """The People leaders among search rows, each with its days in title when Apollo gives them.

    A People leader is someone this job would contact as one at the account's size (rank_person: a
    Roles-tab row whose copy role is People leader, never on a junior title), not located outside the
    US or in CA or WA (location_block). apollo_people counts them the same way, so the People signals
    mean the same whichever job wrote them.
    """
    size, group = account_size(account), settings.industry_group_of(account)
    leaders: list[tuple[int | None, Mapping[str, Any]]] = []
    for p in people:
        if location_block(p):
            continue
        r = rank_person(p.get("title"), settings.roles, size, group, days_in_role(p, today))
        if r is not None and not r.junior and r.role.writes_as == PEOPLE_LEADER:
            leaders.append((r.days_in_role, p))
    return leaders


def newest_known(leaders: Sequence[tuple[int | None, Mapping[str, Any]]]) -> tuple[int, Mapping[str, Any]] | None:
    """The leader newest in title among those whose days in title are known, or None."""
    known = [(d, p) for d, p in leaders if d is not None]
    return min(known, key=lambda x: (x[0], str(x[1].get("id") or ""))) if known else None


def leader_facts(account_id: str, count: int | None, newest: tuple[int, Mapping[str, Any] | None] | None,
                 now: datetime) -> list[dict]:
    """The People-leader facts (source apollo_people), as both this job and apollo_people write them.

    count None writes no people_leader_count. newest is (days in title, that leader's search row): the
    days go to people_leader_days_in_title and, when the row is known, people_leader_newest carries the
    leader's Apollo person id and title, so the opener can name the role, and congratulate the leader
    when they are the contact (Harry, 2 Oct 2026).
    """
    def row(fact: str, value: Any) -> dict:
        return {"event_id": new_id(), "account_id": account_id, "source": PEOPLE_SOURCE, "fact": fact,
                "value": value, "quote": "", "source_url": "", "observed_at": now}

    rows = [] if count is None else [row("people_leader_count", count)]
    if newest is not None:
        days, person = newest
        rows.append(row("people_leader_days_in_title", days))
        if person is not None:
            rows.append(row(NEWEST_LEADER_FACT, {"apollo_person_id": str(person.get("id") or ""),
                                                 "title": " ".join(str(person.get("title") or "").split()),
                                                 "days_in_title": days}))
    return rows


def people_facts(account: Mapping[str, Any], people: Sequence[Mapping[str, Any]], settings: Settings,
                 today: date, now: datetime) -> list[dict]:
    """apollo_people facts from the search pick_contacts already makes (no credits; 1 Oct 2026).

    Only positive evidence is written: this search returns people with a verified email only, so a
    count of 0 would not mean there is no People leader. apollo_people's full search (weekdays 04:20)
    is the one that may write 0, and pick_account keeps its result: these facts are written only when
    that search has not been made within its REFRESH_DAYS.
    """
    leaders = people_leaders(account, people, settings, today)
    if not leaders:
        return []
    return leader_facts(account["account_id"], len(leaders), newest_known(leaders), now)


def full_search_is_fresh(ctx: Context, account_id: str) -> bool:
    """apollo_people searched the account within its REFRESH_DAYS, so its facts stand over this narrower view."""
    from us_outbound.sources import apollo_people

    return apollo_people.searched_recently(ctx.store, account_id, ctx.now)


def rank_candidates(
    people: Sequence[Mapping[str, Any]], account: Mapping[str, Any], settings: Settings, today: date,
    revealed: Container[str] = frozenset(), exclude_roles: Container[str] = frozenset(),
) -> tuple[list[Candidate], Counter[str]]:
    """(the people who may be contacted, best first; how many were left out and why).

    exclude_roles: copy roles (lower case) left out, for a second contact: the first contact's (enrol/second.py).
    """
    size, group = account_size(account), settings.industry_group_of(account)
    out: list[Candidate] = []
    left_out: Counter[str] = Counter()
    for p in people:
        if str(p.get("id") or "") in revealed:
            left_out["revealed before"] += 1
            continue
        why = location_block(p)
        if why:
            left_out[why] += 1
            continue
        r = rank_person(p.get("title"), settings.roles, size, group, days_in_role(p, today))
        if r is None:
            left_out["title not contacted at this size"] += 1
            continue
        if r.role.writes_as.strip().lower() in exclude_roles:
            left_out["the first contact's role"] += 1
            continue
        out.append(Candidate(dict(p), r))
    out.sort(key=lambda c: (c.ranked.key, c.id))
    return out, left_out


# -- reveal and check ---------------------------------------------------------------------------


@dataclass
class _Run:
    """The batch: what it may still spend, and what happened."""

    left: float  # what today may still spend (budget.py), less this run's reveals
    balance: float | None  # Apollo's own balance less this run's reveals; None when it gave none we can read
    floor: int
    reveals: int = 0
    credits: float = 0.0
    tried: int = 0
    picked: list[dict] = field(default_factory=list)
    no_contact: Counter[str] = field(default_factory=Counter)
    no_contact_accounts: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None
    # The Clay email waterfall: why it is not used this run (None: it is), and what it did.
    clay_off: str | None = "clay_email_fallback is no"
    clay_left: float = 0.0  # what today may still spend of the month's Clay budget, less this run's lookups
    clay_lookups: int = 0
    clay_found: int = 0
    clay_credits: float = 0.0
    clay_errors: int = 0

    def why_not_clay(self) -> str | None:
        if self.clay_off:
            return self.clay_off
        if self.clay_lookups >= CLAY_LOOKUPS_PER_RUN:
            return f"this run's {CLAY_LOOKUPS_PER_RUN} Clay lookups are made"
        if self.clay_left < CLAY_RESERVE:
            return "today's share of the month's Clay budget is used"
        return None

    def clay_spend(self, credits: float) -> None:
        self.clay_lookups += 1
        self.clay_credits += credits
        self.clay_left -= credits

    def why_not_reveal(self) -> str | None:
        if self.left < REVEAL_CREDITS:
            return "today's share of the month's Apollo budget is used"
        if self.balance is not None and self.balance - REVEAL_CREDITS < self.floor:
            return f"Apollo's balance ({self.balance:,.0f}) is at apollo_floor ({self.floor:,})"
        return None

    def spend(self, credits: float) -> None:
        self.reveals += 1
        self.credits += credits
        self.left -= credits
        if self.balance is not None:
            self.balance -= credits


def reveal(ctx: Context, account: Mapping[str, Any], cand: Candidate, batch: _Run) -> tuple[dict | None, float]:
    """(Apollo's match for the candidate, or None; the credits it cost). Recorded in credit_ledger first.

    The ledger row is written before the call with REVEAL_CREDITS and settled after it, so a
    reveal that fails or is cut off still counts against the budget. A match with an email
    counts at least REVEAL_CREDITS, whatever Apollo reports.
    """
    with ledger.charge(ctx, "apollo", JOB, REVEAL_CREDITS, note=f"{LEDGER_NOTE}, reserved",
                       account_id=account["account_id"]) as paid:
        try:
            body = ctx.clients.apollo.bulk_match([{"id": cand.id}])
        except ApiError as exc:
            batch.spend(paid.keep(note=f"{LEDGER_NOTE} failed (HTTP {exc.status}); counted in case Apollo charged it"))
            raise
        match = next((m for m in body.get("matches") or () if m), None)
        credits = float(body.get("credits_consumed") or 0)
        if match and match.get("email"):
            credits = max(credits, REVEAL_CREDITS)
        paid.settle(credits, note=LEDGER_NOTE)
    batch.spend(credits)
    return match, credits


def contact_row(ctx: Context, account: Mapping[str, Any], cand: Candidate, match: Mapping[str, Any]) -> dict:
    """The contacts row for a revealed person: names cleaned (clean/people.py), state as a USPS code."""
    first, last = clean_person_name(match.get("first_name") or cand.person.get("first_name"), match.get("last_name"))
    email = _lower(match.get("email"))
    return {
        "contact_id": new_id(),
        "account_id": account["account_id"],
        "role": cand.ranked.role.writes_as,
        "title": " ".join(str(match.get("title") or cand.person.get("title") or "").split()),
        "first_name": first,
        "last_name": last,
        "email": email,
        "email_sha256": hash_email(email) if email else None,
        "email_status": _lower(match.get("email_status")),
        "email_source": EMAIL_SOURCE,
        "person_state": state_code(str(match.get("state") or "")),
        "suppressed": False,
        "created_at": ctx.now,
    }


def why_not_keep(
    ctx: Context, account: Mapping[str, Any], row: Mapping[str, Any], match: Mapping[str, Any],
    domains: set[str], hashes: set[str], known: set[str], accepted: Container[str] = (ACCEPTED_STATUS,),
) -> str | None:
    """Why a revealed contact may not be kept, or None (SPEC 9 pick_contacts gate, SPEC 1.5, SPEC 13 dedupe)."""
    if row["email_status"] not in accepted:
        return f"email status {row['email_status'] or 'unknown'}"
    why = enrol.contact_block(row, domains, hashes)
    if why:
        return why
    country = _lower(match.get("country"))
    if country and country not in US_COUNTRIES:
        return "located outside the US"
    if canonical_domain(ctx.store, row["email"].rsplit("@", 1)[1]) != _lower(account.get("domain")):
        return "email not at the company's domain"
    if row["email_sha256"] in known:
        return "already a contact"
    if not row["first_name"]:
        return "no first name"
    return None


def wants_clay(row: Mapping[str, Any] | None, why: str | None) -> bool:
    """Apollo's reveal missed (no match, no email) or gave an email it doesn't call verified (a catch-all)."""
    return row is None or not row.get("email") or bool(why and why.startswith("email status"))


def clay_inputs(account: Mapping[str, Any], cand: Candidate, match: Mapping[str, Any] | None) -> dict | None:
    """SPEC 8's Contacts inputs (full_name, domain, linkedin_url, title); None without a full name or a LinkedIn URL.

    Work Email takes its own names for them (as_work_email)."""
    m = match or {}
    first = str(m.get("first_name") or cand.person.get("first_name") or "").strip()
    last = str(m.get("last_name") or cand.person.get("last_name") or "").strip()
    linkedin = str(m.get("linkedin_url") or cand.person.get("linkedin_url") or "").strip()
    if not ((first and last) or linkedin):
        return None
    return {"full_name": " ".join(x for x in (first, last) if x) or None, "domain": _lower(account.get("domain")),
            "linkedin_url": linkedin or None, "title": str(m.get("title") or cand.person.get("title") or "") or None}


def as_work_email(inputs: Mapping[str, Any], account: Mapping[str, Any]) -> dict[str, str]:
    """SPEC 8's Contacts inputs under Work Email's own names, with the company's name; Work Email takes no title."""
    return work_email_inputs(inputs.get("full_name"), inputs.get("domain"), inputs.get("linkedin_url"),
                             company_name=str(account.get("clean_name") or "") or None)


def clay_lookup(ctx: Context, account: Mapping[str, Any], cand: Candidate, match: Mapping[str, Any] | None,
                batch: _Run) -> tuple[dict | None, str | None, dict]:
    """(the contacts row from Clay's waterfall, or None; why not; the lookup's fact value). Recorded in credit_ledger first.

    The "US Outbound – Contacts" function, once built, takes SPEC 8's inputs as they are; Work Email takes its
    own names for them (as_work_email). PHASE0-CONFIRM: those names, through the Routines API.
    """
    g = ctx.settings.general
    inputs = clay_inputs(account, cand, match)
    value: dict[str, Any] = {"apollo_person_id": cand.id, "row": cand.ranked.role.role, "status": None, "kept": False,
                             "credits": 0.0}
    if inputs is None:
        return None, "no full name or LinkedIn URL to look up in Clay", {**value, "reason": "nothing to look up"}
    function = g.clay_contacts_function_id or WORK_EMAIL_FUNCTION_ID
    if not g.clay_contacts_function_id:
        inputs = as_work_email(inputs, account)
    with ledger.charge(ctx, "clay", JOB, CLAY_RESERVE, note=f"{CLAY_LEDGER_NOTE}, reserved",
                       account_id=account["account_id"]) as paid:
        try:
            out = ctx.clients.clay.run_function(function, inputs)
            got = parse_contacts_output(out) if g.clay_contacts_function_id else parse_work_email_output(out)
        except (ApiError, ClayError) as exc:
            batch.clay_spend(paid.keep(note=f"{CLAY_LEDGER_NOTE} failed; counted in case Clay charged it"))
            why = f"Clay lookup failed ({type(exc).__name__})"
            batch.clay_errors += 1
            log("pick_contacts_clay_error", account_id=account["account_id"], error=str(exc)[:200])
            if batch.clay_errors >= CLAY_MAX_ERRORS:
                batch.clay_off = f"{CLAY_MAX_ERRORS} Clay lookups failed this run (the last: {str(exc)[:200]})"
            return None, why, {**value, "credits": CLAY_RESERVE, "reason": why}
        credits = got["credits_used"]
        if credits is None:
            credits = CLAY_RESERVE if got["email"] else 0.0
        paid.settle(credits, note=CLAY_LEDGER_NOTE)
    batch.clay_spend(credits)
    value.update(status=got["status"], credits=credits, provider=got.get("provider"))
    if got["status"] not in CLAY_ACCEPTED or not got["email"]:
        return None, f"Clay email status {got['status']}", value
    person = {**cand.person, **(match or {})}
    row = contact_row(ctx, account, cand, {**person, "email": got["email"], "email_status": got["status"]})
    row["email_source"] = CLAY_SOURCE
    return row, None, value


def _reveal_value(cand: Candidate, row: Mapping[str, Any] | None, credits: float, why: str | None) -> dict:
    return {
        "apollo_person_id": cand.id, "row": cand.ranked.role.role, "role": cand.ranked.role.writes_as,
        "title": str(cand.person.get("title") or ""), "seniority": SENIORITY[cand.ranked.seniority],
        "email_status": (row or {}).get("email_status") or None, "kept": why is None, "reason": why, "credits": credits,
    }


# -- one account ----------------------------------------------------------------------------------


@dataclass
class Outcome:
    outcome: str  # PICKED, NO_CONTACT or STOPPED
    reason: str = ""
    contact: dict | None = None
    candidate: Candidate | None = None
    detail: dict = field(default_factory=dict)


def _most(c: Counter[str]) -> str:
    return c.most_common(1)[0][0] if c else ""


def pick_account(
    ctx: Context, account: Mapping[str, Any], batch: _Run, revealed: Container[str],
    domains: set[str], hashes: set[str], known: set[str], exclude_roles: Container[str] = frozenset(),
) -> Outcome:
    """Search, rank and reveal for one account; writes the contact and the reveal facts. exclude_roles: for a
    second contact, the first contact's copy role (rank_candidates)."""
    s = ctx.settings
    size = account_size(account)
    if size is None:
        return Outcome(NO_CONTACT, "company size unknown")
    titles = search_titles(s.roles, size, s.industry_group_of(account))
    if not titles:
        return Outcome(NO_CONTACT, "no Roles-tab row is contacted at its size")
    people = search(ctx, account, titles)
    facts = people_facts(account, people, s, ctx.today_uk(), ctx.now)
    # Read by the next rescore (settings_sync, 11:30 on weekdays). Not over apollo_people's fuller search, whose
    # count of 0 and days in title this verified-email view could otherwise replace.
    if facts and not full_search_is_fresh(ctx, account["account_id"]):
        ctx.store.insert("signal_events", facts)
    if not people:
        return Outcome(NO_CONTACT, "nobody at Apollo with a Roles-tab title for its size, in the US, with a verified email",
                       detail={"found": 0})
    cands, left_out = rank_candidates(people, account, s, ctx.today_uk(), revealed, exclude_roles)
    detail: dict[str, Any] = {"found": len(people), "candidates": len(cands), "left_out": dict(left_out)}
    if not cands:
        return Outcome(NO_CONTACT, f"nobody suitable ({_most(left_out)})", detail=detail)
    rejected: Counter[str] = Counter()
    clay_tried = False
    for cand in cands[:MAX_REVEALS]:
        stop = batch.why_not_reveal()
        if stop:
            batch.stopped = stop
            return Outcome(STOPPED, stop, detail=detail)
        match, credits = reveal(ctx, account, cand, batch)
        row = contact_row(ctx, account, cand, match) if match else None
        why = "Apollo returned no match" if row is None else why_not_keep(ctx, account, row, match, domains, hashes, known)
        ctx.store.insert("signal_events", [_fact(account["account_id"], REVEAL_FACT, _reveal_value(cand, row, credits, why),
                                                 ctx.now)])
        if why is None:
            ctx.store.insert("contacts", [row])
            known.add(row["email_sha256"])
            return Outcome(PICKED, contact=row, candidate=cand, detail={**detail, "reveals": sum(rejected.values()) + 1})
        if not clay_tried and wants_clay(row, why) and batch.why_not_clay() is None:
            clay_tried = True
            clay_row, clay_why, value = clay_lookup(ctx, account, cand, match, batch)
            if clay_row is not None:
                person = {**cand.person, **(match or {})}
                clay_why = why_not_keep(ctx, account, clay_row, person, domains, hashes, known, CLAY_ACCEPTED)
            value.update(kept=clay_why is None, reason=clay_why)
            ctx.store.insert("signal_events", [_fact(account["account_id"], CLAY_FACT, value, ctx.now)])
            if clay_row is not None and clay_why is None:
                ctx.store.insert("contacts", [clay_row])
                known.add(clay_row["email_sha256"])
                batch.clay_found += 1
                return Outcome(PICKED, contact=clay_row, candidate=cand,
                               detail={**detail, "reveals": sum(rejected.values()) + 1, "clay": True})
            why = f"{why}; Clay: {clay_why}"
        rejected[why] += 1
    detail["reveals"] = sum(rejected.values())
    reasons = ", ".join(f"{why} ({n})" if n > 1 else why for why, n in rejected.items())
    return Outcome(NO_CONTACT, f"no sendable email among the top {detail['reveals']} ({reasons})", detail=detail)


# -- the job --------------------------------------------------------------------------------------


def _record(ctx: Context, account: Mapping[str, Any], out: Outcome, batch: _Run, slot: int = 1) -> None:
    aid = account["account_id"]
    value = {"outcome": out.outcome, "reason": out.reason, **out.detail}
    if slot != 1:
        value["slot"] = slot  # a second contact (enrol/second.py)
    if out.outcome == PICKED and out.contact and out.candidate:
        value.update(contact_id=out.contact["contact_id"], row=out.candidate.ranked.role.role,
                     role=out.contact["role"], seniority=SENIORITY[out.candidate.ranked.seniority],
                     apollo_person_id=out.candidate.id,  # the opener knows when the contact is the new leader
                     email_source=out.contact["email_source"])
        batch.picked.append({"account_id": aid, "domain": account.get("domain"), "row": value["row"],
                             "role": value["role"], "seniority": value["seniority"],
                             **({"slot": slot} if slot != 1 else {})})
    else:
        reason = out.reason if slot == 1 else f"second contact: {out.reason}"
        batch.no_contact[reason] += 1
        if len(batch.no_contact_accounts) < LIST_LIMIT:
            batch.no_contact_accounts.append({"account_id": aid, "domain": account.get("domain"), "reason": reason})
    ctx.store.insert("signal_events", [_fact(aid, OUTCOME_FACT, value, ctx.now)])
    log("pick_contacts_account", account_id=aid, outcome=out.outcome, reason=out.reason, row=value.get("row"))


def apollo_paused(ctx: Context) -> str | None:
    """Why no Apollo reveal may be made now: a kill rule pauses the apollo email source; None when none does (the
    module docstring: Clay alone is not used meanwhile)."""
    paused = holds.paused_sources(ctx.store)
    if EMAIL_SOURCE not in paused:
        return None
    return (f"a kill rule pauses the apollo email source ({paused[EMAIL_SOURCE] or 'bounces'}): no Apollo reveals, "
            "and Clay alone is not used, as it is the fallback for Apollo's misses; `us-outbound killrules clear "
            "ITEM_ID --live` lifts it once checked")


def clay_room(ctx: Context) -> tuple[str | None, float]:
    """(why the Clay waterfall is not used this run, or None; what today may spend of the month's Clay budget)."""
    s = ctx.settings
    if not s.general.clay_email_fallback:
        return "clay_email_fallback is no", 0.0
    paused = holds.paused_sources(ctx.store)
    if CLAY_SOURCE in paused:
        return f"a kill rule pauses the clay email source ({paused[CLAY_SOURCE] or 'bounces'})", 0.0
    month = budget.monthly(ctx.store, s, "clay", ctx.now)
    if month.budget <= 0:
        return "no monthly Clay budget (clay_monthly_credits is 0)", 0.0
    try:
        ctx.clients.clay  # the key, read once here rather than failing the job at the first lookup
    except ConfigError as exc:
        return f"no Clay API key: {exc}", 0.0
    return None, month.left_today


def _balance(ctx: Context) -> tuple[float | None, str | None]:
    """(Apollo's own credits left, or None if its reply has none we can read; why not to spend, or None). 0 credits."""
    try:
        return credits_left(ctx.clients.apollo.credit_usage()), None
    except ApiError as exc:
        return None, f"Apollo's balance could not be read (HTTP {exc.status}), so nothing is spent (SPEC 1.6)"


def run(ctx: Context) -> dict:
    """The pick_contacts job (JOB CONTRACT: run(ctx) -> summary)."""
    s, store = ctx.settings, ctx.store
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run}
    need, ready = waiting(ctx)
    past = history(store, ctx.now)
    due = [a for a in need if not past.cooling(a["account_id"], ctx.now)]
    want = max(0, lookahead(s) - ready)
    summary.update(ready_before=ready, wanted=want, waiting=len(need), tried_recently=len(need) - len(due))
    # Second contacts (enrol/second.py; Harry, 6 Oct 2026), due within the same lookahead: ([], 0), without a
    # read, while second_contact is no. They are revealed only with what the first contacts leave of `want`.
    need2, ready2 = second.to_pick(ctx, second.due_by(ctx, LOOKAHEAD_SEND_DAYS))
    due2 = [(a, role) for a, role in need2 if not past.cooling(a["account_id"], ctx.now)]
    if second.on(s):
        summary["second_contacts"] = {"ready_before": ready2, "waiting": len(need2),
                                      "tried_recently": len(need2) - len(due2), "picked": 0}

    month = budget.monthly(store, s, "apollo", ctx.now)
    batch = _Run(left=month.left_today, balance=None, floor=s.general.apollo_floor)
    held = apollo_paused(ctx)  # a kill rule holds Apollo's addresses back (Harry, 7 Oct 2026)
    if held:
        summary["apollo_paused"] = held  # in the heartbeat whatever else the run says
    why = None
    if not want or not (due or (due2 and want > ready2)):
        enough = not want or (not due and due2)
        why = "enough accounts are ready for the next two send days" if enough else "no account is waiting for a contact"
    elif held:
        why = held
    elif month.budget <= 0:
        why = "no monthly Apollo budget (apollo_monthly_credits is 0)"
    why = why or batch.why_not_reveal()
    if not why:
        batch.balance, why = _balance(ctx)
        why = why or batch.why_not_reveal()
        if batch.balance is None and not why:
            log("pick_contacts_balance_unknown", note="Apollo's reply had no balance we can read; the monthly budget still applies")
    if why:
        summary.update(status="skipped", reason=why, budget=month.describe())
        log("pick_contacts_done", run_id=ctx.run_id, **summary)
        return summary

    batch.clay_off, batch.clay_left = clay_room(ctx)
    domains, hashes = enrol.suppressed(ctx)
    known = {str(c["email_sha256"]) for c in store.select("contacts") if c.get("email_sha256")}
    for account in due:
        if len(batch.picked) >= want or batch.stopped:
            break
        try:
            out = pick_account(ctx, account, batch, past.revealed[account["account_id"]], domains, hashes, known)
        except ApiError as exc:
            batch.errors.append(f"{account.get('domain')}: {str(exc)[:200]}")
            if len(batch.errors) >= MAX_ERRORS:
                batch.stopped = f"{MAX_ERRORS} Apollo errors"
            continue
        if out.outcome == STOPPED:
            break
        batch.tried += 1
        _record(ctx, account, out, batch)
    # Then second contacts, with what is left of the lookahead once the first contacts are counted: the first
    # contacts of new accounts come first when capacity is short (Harry, 6 Oct 2026).
    room2 = max(0, lookahead(s) - ready - len(batch.picked) - ready2)
    picked2 = 0
    for account, role in due2:
        if picked2 >= room2 or batch.stopped:
            break
        try:
            out = pick_account(ctx, account, batch, past.revealed[account["account_id"]], domains, hashes, known,
                               exclude_roles=frozenset({role}))
        except ApiError as exc:
            batch.errors.append(f"{account.get('domain')}: {str(exc)[:200]}")
            if len(batch.errors) >= MAX_ERRORS:
                batch.stopped = f"{MAX_ERRORS} Apollo errors"
            continue
        if out.outcome == STOPPED:
            break
        batch.tried += 1
        picked2 += out.outcome == PICKED
        _record(ctx, account, out, batch, slot=second.SECOND)
    if "second_contacts" in summary:
        summary["second_contacts"]["picked"] = picked2

    summary.update(
        status="ok",
        tried=batch.tried,
        picked=len(batch.picked),
        picked_by_row=dict(Counter(p["row"] for p in batch.picked)),
        no_contact=dict(batch.no_contact),
        reveals=batch.reveals,
        credits=batch.credits,
        credits_per_account=round(batch.credits / batch.tried, 2) if batch.tried else None,
        stopped=batch.stopped,
        errors=batch.errors,
        budget=budget.monthly(store, s, "apollo", ctx.now).describe(),
        clay={"off": batch.clay_off, "lookups": batch.clay_lookups, "found": batch.clay_found,
              "credits": batch.clay_credits, "errors": batch.clay_errors,
              "budget": budget.monthly(store, s, "clay", ctx.now).describe()},
        picked_accounts=batch.picked[:LIST_LIMIT],
        no_contact_accounts=batch.no_contact_accounts,
    )
    log("pick_contacts_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if not k.endswith("_accounts")})
    return summary
