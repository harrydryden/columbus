"""The pick_contacts job: one verified contact for each account about to be enrolled (SPEC 9; SPEC 5 Roles).

Harry, 1 Oct 2026: "Targeting the right contact is paramount... the closer to seniority and
decision maker the better, whilst also having the time to engage in understanding Spill."
Runs at 05:30 UK on weekdays, before enrol at 12:00.

  1. Accounts: verified, in the queue (tier Queue), that enrol could take (the ICP gate's send checks:
     domain not suppressed or a partner, industry on, size in range), with no sendable contact; in enrol's queue order,
     and only as many as the next two send days need at the weekly target's pace
     (docs/pipeline.md change 5, approved 30 Sep 2026). An account where nobody suitable was
     found is tried again after RETRY_DAYS. "Could take" is enrol's own check (enrol.eligible: a kill rule's
     stopped group or paused email source, and a sendable Copy row, besides the domain and industry; W1-6).
     The random explore arm (General explore_share, 0.15; Harry's D7) is explore_share of the contacts
     wanted, from the accounts due within the Focus shares (queue.strata): each group's accounts are tried first,
     in a random order, until its places each have a kept contact or it runs out; each account whose contact is
     kept is stamped accounts.arm, random or ranked, which enrol reads. The summary's explore.realised is the arm's
     share of the contacts kept.
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
Every reveal and every outcome is a signal_events fact with source pick_contacts (no names or
emails), so nobody is paid for twice. SOURCE is not a SPEC 7 source key, so no Signals row can
score these facts.

Dry-run and live are the same: the job writes only to the database and its Apollo calls are
reads, which the guard allows in dry-run, so reveals spend credits in both, within the budget.
The scheduler runs it without --live.

A kill rule pausing the apollo email source (base/holds.paused_sources: Apollo's addresses bounced;
learn/kill_rules.py) stops the run before any search, reveal or credit, and the run's summary and so its
heartbeat say why (apollo_paused; Harry, 7 Oct 2026). Contacts already found stay ready, and enrol still takes those
whose source is not paused. `us-outbound killrules clear ITEM_ID --live` lifts the pause.

Clay's email waterfall (Apollo's misses looked up in Clay, behind General clay_email_fallback, which was never
switched on) and second contacts (General second_contact) were removed with W2-1 (20 Oct 2026; Harry's D18).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Container, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound import budget, icp, ledger
from us_outbound.base import holds
from us_outbound.clean.domains import canonical_domain
from us_outbound.clean.people import SENIORITY, Ranked, clean_person_name, company_size, rank_person, state_code
from us_outbound.clients.apollo import credits_left, people_in, while_full_page
from us_outbound.clients.db import Store, new_id
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.enrol import enrol, queue
from us_outbound.facts import newest_by
from us_outbound.logs import hash_email, log
from us_outbound.scoring import tiers
from us_outbound.settings.model import Role, Settings
from us_outbound.timeparse import iso_date, utc, utc_strict

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

# api_search reads "United States" in person_locations as the whole country, and honours
# contact_email_status: confirmed live 2 Oct 2026 (CA and WA people came back, and every reveal was
# verified). Per-state strings,
# which could leave out CA and WA in the search itself, are untested, so CA and WA are left out
# after the search (when it gives a state) and after the reveal (always).
US_LOCATION = "United States"
SEARCH_EMAIL_STATUSES = ("verified",)


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


def _split(ctx: Context) -> tuple[list[dict], list[dict]]:
    """(the accounts enrol could take with no sendable contact; those with one), each in queue order.

    One read of the verified queue, its contacts, the suppression list and the partners serves both lists, so
    ready_accounts() and waiting() can never disagree about which account is which.

    The gates are enrol's own (enrol.eligible, W1-6): an account in an industry group a kill rule stopped is in
    neither list, so no credit is spent on it and its accounts do not count as ready while they cannot be emailed
    (code-contacts-enrol L1: 60 ready accounts in a stopped group read as "enough ready", and nothing was revealed
    for the others); a contact from an email source a kill rule paused is not sendable, so its account needs one; and
    an account no sendable Copy row could serve, for a contact of any role (enrol.copy_possible), is in neither, as
    enrol would skip it for want of copy. The hand-check's held accounts stay in: its draw reads this list, and they
    are emailed once judged.
    """
    s, store = ctx.settings, ctx.store
    accounts = store.select("accounts", {"status": "verified", "tier": list(tiers.QUEUE_TIERS)})
    g = enrol.gates(ctx)
    rows = enrol.sendable_copy(s)
    contacts: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks([a["account_id"] for a in accounts]):
        for c in store.select("contacts", {"account_id": list(chunk)}):
            contacts[c["account_id"]].append(c)
    need, ready = [], []
    for a in accounts:
        if enrol.account_reason(a, g) is not None or not enrol.copy_possible(a, s, rows):
            continue
        contact, _ = enrol.eligible(a, contacts.get(a["account_id"], []), g)
        (need if contact is None else ready).append(a)
    need.sort(key=lambda a: queue.order_key(a, s))
    ready.sort(key=lambda a: queue.order_key(a, s))
    return need, ready


def ready_accounts(ctx: Context) -> list[dict]:
    """The accounts enrol could take that already have a sendable contact, in queue order (queue.order_key).

    The contract later packages build on (the W1-C contracts commit, 19 Oct 2026): sourcing counts them as ready
    supply, and the weekly hand-check draws its sample from the positions enrol would reach on the coming send days,
    so both need the accounts themselves and in enrol's order, not only how many there are.
    """
    return _split(ctx)[1]


def waiting(ctx: Context) -> tuple[list[dict], int]:
    """(the accounts enrol could take that have no sendable contact, in queue order; how many already have one).

    The count is len(ready_accounts(ctx)), from the same read."""
    need, ready = _split(ctx)
    return need, len(ready)


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
    rows = [e for e in store.select("signal_events", {"source": SOURCE})
            if e.get("account_id") and isinstance(e.get("value"), Mapping) and utc(e.get("observed_at"))]
    for aid, e in newest_by(rows, lambda e: str(e["account_id"]), OUTCOME_FACT).items():
        h.outcomes[aid] = (utc_strict(e["observed_at"]), dict(e["value"]))
    for e in rows:
        value = e["value"]
        if (e.get("fact") == REVEAL_FACT and now - utc_strict(e["observed_at"]) < timedelta(days=REVEAL_AGAIN_DAYS)
                and value.get("apollo_person_id")):
            h.revealed[str(e["account_id"])].add(str(value["apollo_person_id"]))
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
    if country and country not in icp.US_COUNTRIES:
        return "located outside the US"
    if person.get("state") and state_code(str(person["state"])) in icp.NEVER_STATES:
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
    revealed: Container[str] = frozenset(),
) -> tuple[list[Candidate], Counter[str]]:
    """(the people who may be contacted, best first; how many were left out and why)."""
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
    if country and country not in icp.US_COUNTRIES:
        return "located outside the US"
    if canonical_domain(ctx.store, row["email"].rsplit("@", 1)[1]) != _lower(account.get("domain")):
        return "email not at the company's domain"
    if row["email_sha256"] in known:
        return "already a contact"
    if not row["first_name"]:
        return "no first name"
    return None


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
    domains: set[str], hashes: set[str], known: set[str],
) -> Outcome:
    """Search, rank and reveal for one account; writes the contact and the reveal facts."""
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
    cands, left_out = rank_candidates(people, account, s, ctx.today_uk(), revealed)
    detail: dict[str, Any] = {"found": len(people), "candidates": len(cands), "left_out": dict(left_out)}
    if not cands:
        return Outcome(NO_CONTACT, f"nobody suitable ({_most(left_out)})", detail=detail)
    rejected: Counter[str] = Counter()
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
        rejected[why] += 1
    detail["reveals"] = sum(rejected.values())
    reasons = ", ".join(f"{why} ({n})" if n > 1 else why for why, n in rejected.items())
    return Outcome(NO_CONTACT, f"no sendable email among the top {detail['reveals']} ({reasons})", detail=detail)


# -- the job --------------------------------------------------------------------------------------


def _record(ctx: Context, account: Mapping[str, Any], out: Outcome, batch: _Run) -> None:
    aid = account["account_id"]
    value = {"outcome": out.outcome, "reason": out.reason, **out.detail}
    if out.outcome == PICKED and out.contact and out.candidate:
        value.update(contact_id=out.contact["contact_id"], row=out.candidate.ranked.role.role,
                     role=out.contact["role"], seniority=SENIORITY[out.candidate.ranked.seniority],
                     apollo_person_id=out.candidate.id,  # the opener knows when the contact is the new leader
                     email_source=out.contact["email_source"])
        batch.picked.append({"account_id": aid, "domain": account.get("domain"), "row": value["row"],
                             "role": value["role"], "seniority": value["seniority"]})
    else:
        reason = out.reason
        batch.no_contact[reason] += 1
        if len(batch.no_contact_accounts) < LIST_LIMIT:
            batch.no_contact_accounts.append({"account_id": aid, "domain": account.get("domain"), "reason": reason})
    ctx.store.insert("signal_events", [_fact(aid, OUTCOME_FACT, value, ctx.now)])
    log("pick_contacts_account", account_id=aid, outcome=out.outcome, reason=out.reason, row=value.get("row"))


def apollo_paused(ctx: Context) -> str | None:
    """Why no Apollo reveal may be made now: a kill rule pauses the apollo email source; None when none does."""
    paused = holds.paused_sources(ctx.store)
    if EMAIL_SOURCE not in paused:
        return None
    return (f"a kill rule pauses the apollo email source ({paused[EMAIL_SOURCE] or 'bounces'}): no Apollo reveals; "
            "`us-outbound killrules clear ITEM_ID --live` lifts it once checked")


def _balance(ctx: Context) -> tuple[float | None, str | None]:
    """(Apollo's own credits left, or None if its reply has none we can read; why not to spend, or None). 0 credits."""
    try:
        return credits_left(ctx.clients.apollo.credit_usage()), None
    except ApiError as exc:
        return None, f"Apollo's balance could not be read (HTTP {exc.status}), so nothing is spent (SPEC 1.6)"


def _rescore(ctx: Context) -> dict:
    """One rescore before the queue is read (W1-3a, 19 Oct 2026), so pick takes accounts in the order the morning's
    facts give them.

    apollo_enrich (05:00) and apollo_signals (05:10) now run after verify_accounts (04:30), for verified accounts
    only, and neither rescores (RS P0-8: overlapping rescores could lose the match rows), so their exact sizes and
    hiring facts are scored here, once, at the end of the morning chain. A rescore that fails is reported in the
    summary and the run goes on in the order verify's rescore left: a missed reorder costs less than a day with no
    contacts found. Imported here, as tests replace the module."""
    from us_outbound.scoring import score

    try:
        return score.rescore(ctx)
    except GuardViolation:
        raise
    except Exception as exc:
        log("pick_contacts_rescore_failed", run_id=ctx.run_id, error=str(exc)[:200])
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def run(ctx: Context) -> dict:
    """The pick_contacts job (JOB CONTRACT: run(ctx) -> summary)."""
    s, store = ctx.settings, ctx.store
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run}
    summary["rescore"] = _rescore(ctx)
    need, ready = waiting(ctx)
    past = history(store, ctx.now)
    due = [a for a in need if not past.cooling(a["account_id"], ctx.now)]
    want = max(0, lookahead(s) - ready)
    summary.update(ready_before=ready, wanted=want, waiting=len(need), tried_recently=len(need) - len(due))

    month = budget.monthly(store, s, "apollo", ctx.now)
    batch = _Run(left=month.left_today, balance=None, floor=s.general.apollo_floor)
    held = apollo_paused(ctx)  # a kill rule holds Apollo's addresses back (Harry, 7 Oct 2026)
    if held:
        summary["apollo_paused"] = held  # in the heartbeat whatever else the run says
    why = None
    if not want or not due:
        why = "enough accounts are ready for the next two send days" if not want else "no account is waiting for a contact"
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

    domains, hashes = enrol.suppressed(ctx)
    known = {str(c["email_sha256"]) for c in store.select("contacts") if c.get("email_sha256")}
    # The random explore arm (Harry's D7; queue.strata): explore_share of today's want, counted in kept contacts,
    # from the accounts due, within the Focus shares, tried first: each stratum's accounts in a random order until
    # its places each have a contact or it runs out (a drawn account with nobody to contact is replaced by the next
    # in that order, so the arm is not cut to the share of accounts with a contact; the whole-wave review, 20 Oct
    # 2026). The strata take turns, so a budget that stops the run part-way leaves the arm's mix near the Focus mix.
    # Then the rest in queue order, those the random lane did not reach included. Drawn here, before any later
    # ordering by contactability, so the arm is a random draw from the eligible queue and not from the accounts
    # easiest to find a contact at (RS §2 W1-6). 0 by default: no draw, every account in order, as before.
    k = queue.explore_count(want, s, len(due))
    lanes = [[places, iter(pool)] for _, places, pool in queue.strata(due, s, f"pick:{ctx.today_uk().isoformat()}", k)]
    tried_random: set[str] = set()
    kept_random: list[dict] = []
    done = False

    def attempt(account: dict, arm: str) -> str | None:
        """Search and reveal for one account; its outcome, or None after an Apollo error."""
        try:
            out = pick_account(ctx, account, batch, past.revealed[account["account_id"]], domains, hashes, known)
        except ApiError as exc:
            batch.errors.append(f"{account.get('domain')}: {str(exc)[:200]}")
            if len(batch.errors) >= MAX_ERRORS:
                batch.stopped = f"{MAX_ERRORS} Apollo errors"
            return None
        if out.outcome == STOPPED:
            return STOPPED
        batch.tried += 1
        _record(ctx, account, out, batch)
        if out.outcome == PICKED:  # the arm enrol reads (accounts.arm), set when the account's contact is kept
            store.upsert("accounts", [{"account_id": account["account_id"], "arm": arm}])
        return out.outcome

    while lanes and not done:
        for lane in list(lanes):
            if len(batch.picked) >= want or batch.stopped:
                done = True
                break
            account = next(lane[1], None)
            if account is None:  # the group ran out: its places left go to the ranked rest
                lanes.remove(lane)
                continue
            tried_random.add(str(account["account_id"]))
            outcome = attempt(account, queue.RANDOM)
            if outcome == STOPPED:
                done = True
                break
            if outcome == PICKED:
                kept_random.append(account)
                lane[0] -= 1
                if lane[0] <= 0:
                    lanes.remove(lane)
    rest = [a for a in due if str(a["account_id"]) not in tried_random]  # due is in queue order (waiting())
    for account in rest:
        if done or len(batch.picked) >= want or batch.stopped:
            break
        if attempt(account, queue.RANKED) == STOPPED:
            break
    picked = len(batch.picked)
    summary["explore"] = {"share": s.general.explore_share, "target": k, "drawn": len(tried_random),
                          "picked": len(kept_random),
                          "realised": round(len(kept_random) / picked, 3) if k and picked else None,
                          "by_group": dict(Counter(s.industry_group_of(a) for a in kept_random))}

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
        picked_accounts=batch.picked[:LIST_LIMIT],
        no_contact_accounts=batch.no_contact_accounts,
    )
    log("pick_contacts_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if not k.endswith("_accounts")})
    return summary
