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
     The batch stops when either runs out.
  3. Search (0 credits): Apollo People API Search at the organization, for the titles of every
     Roles-tab row contacted at the account's size, for people in the United States with an
     email Apollo has verified.
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
reads, which the guard allows in dry-run, so reveals spend credits in both, within the budget
(as verify_in_clay spends Clay's). The scheduler runs it without --live.

The Clay "US Outbound – Contacts" fallback for misses and catch-alls (SPEC 9) is not called yet:
an account Apollo has no verified email for is recorded as having no suitable contact.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Container, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import budget
from us_outbound.clean.domains import canonical_domain
from us_outbound.clean.people import SENIORITY, Ranked, clean_person_name, company_size, rank_person, state_code
from us_outbound.clients.apollo import credits_left
from us_outbound.clients.db import Store, new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.enrol import enrol, queue
from us_outbound.logs import hash_email, log
from us_outbound.settings.model import Role, Settings

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

# PHASE0-CONFIRM: api_search reads "United States" in person_locations as the whole country, and
# honours contact_email_status (both as the Apollo MCP tool documents them). Per-state strings,
# which could leave out CA and WA in the search itself, are untested, so CA and WA are left out
# after the search (when it gives a state) and after the reveal (always).
US_LOCATION = "United States"
SEARCH_EMAIL_STATUSES = ("verified",)
US_COUNTRIES = frozenset({"united states", "united states of america", "us", "usa"})


def _ts(v: Any) -> datetime | None:
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return None


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
        t, aid, value = _ts(e.get("observed_at")), str(e.get("account_id") or ""), e.get("value")
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


def search_filters(account: Mapping[str, Any], titles: Sequence[str]) -> dict[str, Any]:
    """People API Search at the account's organization, for these titles: people in the US with a verified email.

    Similar titles stay in (Apollo's default): the search is free, and every title is checked
    against the Roles tab here.
    """
    f: dict[str, Any] = {
        "person_titles": list(titles),
        "include_similar_titles": True,
        "person_locations": [US_LOCATION],
        "contact_email_status": list(SEARCH_EMAIL_STATUSES),
    }
    org = str(account.get("apollo_org_id") or "").strip()
    if org:
        f["organization_ids"] = [org]
    else:
        f["q_organization_domains_list"] = [_lower(account.get("domain"))]
    return f


def search(ctx: Context, account: Mapping[str, Any], titles: Sequence[str]) -> list[dict]:
    """People at the account who might be contacted (0 credits), each once, up to SEARCH_PAGES pages.

    PHASE0-CONFIRM: api_search returns its rows under "people", each with the person "id"
    bulk_match takes, their title, and (when given) state, country and employment_history.
    """
    people: list[dict] = []
    seen: set[str] = set()
    filters = search_filters(account, titles)
    for page in range(1, SEARCH_PAGES + 1):
        batch = ctx.clients.apollo.search_people(filters, page=page, per_page=SEARCH_PER_PAGE).get("people") or []
        for p in batch:
            pid = str(p.get("id") or "")
            if pid and pid not in seen:
                seen.add(pid)
                people.append(p)
        if len(batch) < SEARCH_PER_PAGE:
            break
    return people


def days_in_role(person: Mapping[str, Any], today: date) -> int | None:
    """Days since the person started their current job, when Apollo gives it (employment_history)."""
    for job in person.get("employment_history") or ():
        if isinstance(job, Mapping) and job.get("current") and job.get("start_date"):
            try:
                start = date.fromisoformat(str(job["start_date"])[:10])
            except ValueError:
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


def people_facts(account: Mapping[str, Any], people: Sequence[Mapping[str, Any]], settings: Settings,
                 today: date, now: datetime) -> list[dict]:
    """apollo_people facts from the search pick_contacts already makes (no credits; 1 Oct 2026).

    The People-leader signals ("New People leader", "People leader in place") read
    people_leader_count and people_leader_days_in_title, which no other job writes. Only positive
    evidence is written: the search returns people with a verified email only, so a count of 0
    would not mean there is no People leader, and "First People hire" (count = 0) must not fire on it.
    """
    size, group = account_size(account), settings.industry_group_of(account)
    days: list[int | None] = []
    for p in people:
        if location_block(p):
            continue
        r = rank_person(p.get("title"), settings.roles, size, group, days_in_role(p, today))
        if r is not None and not r.junior and r.role.writes_as == PEOPLE_LEADER:
            days.append(r.days_in_role)
    if not days:
        return []
    aid = account["account_id"]
    rows = [{"event_id": new_id(), "account_id": aid, "source": PEOPLE_SOURCE, "fact": "people_leader_count",
             "value": len(days), "quote": "", "source_url": "", "observed_at": now}]
    known = [d for d in days if d is not None]
    if known:
        rows.append({"event_id": new_id(), "account_id": aid, "source": PEOPLE_SOURCE,
                     "fact": "people_leader_days_in_title", "value": min(known), "quote": "", "source_url": "",
                     "observed_at": now})
    return rows


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
    entry = {"entry_id": new_id(), "system": "apollo", "job": JOB, "run_id": ctx.run_id,
             "account_id": account["account_id"], "credits": REVEAL_CREDITS, "usd": None, "occurred_at": ctx.now,
             "note": f"{LEDGER_NOTE}, reserved"}
    ctx.store.insert("credit_ledger", [entry])
    try:
        body = ctx.clients.apollo.bulk_match([{"id": cand.id}])
    except ApiError as exc:
        ctx.store.upsert("credit_ledger", [{**entry, "note": f"{LEDGER_NOTE} failed (HTTP {exc.status}); "
                                                             "counted in case Apollo charged it"}])
        batch.spend(REVEAL_CREDITS)
        raise
    match = next((m for m in body.get("matches") or () if m), None)
    credits = float(body.get("credits_consumed") or 0)
    if match and match.get("email"):
        credits = max(credits, REVEAL_CREDITS)
    ctx.store.upsert("credit_ledger", [{**entry, "credits": credits, "note": LEDGER_NOTE}])
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
    domains: set[str], hashes: set[str], known: set[str],
) -> str | None:
    """Why a revealed contact may not be kept, or None (SPEC 9 pick_contacts gate, SPEC 1.5, SPEC 13 dedupe)."""
    if row["email_status"] != ACCEPTED_STATUS:
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
    if facts:
        ctx.store.insert("signal_events", facts)  # read by the next rescore (settings_sync, 02:00)
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
                     role=out.contact["role"], seniority=SENIORITY[out.candidate.ranked.seniority])
        batch.picked.append({"account_id": aid, "domain": account.get("domain"), "row": value["row"],
                             "role": value["role"], "seniority": value["seniority"]})
    else:
        batch.no_contact[out.reason] += 1
        if len(batch.no_contact_accounts) < LIST_LIMIT:
            batch.no_contact_accounts.append({"account_id": aid, "domain": account.get("domain"), "reason": out.reason})
    ctx.store.insert("signal_events", [_fact(aid, OUTCOME_FACT, value, ctx.now)])
    log("pick_contacts_account", account_id=aid, outcome=out.outcome, reason=out.reason, row=value.get("row"))


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

    month = budget.monthly(store, s, "apollo", ctx.now)
    batch = _Run(left=month.left_today, balance=None, floor=s.general.apollo_floor)
    why = None
    if not want or not due:
        why = "enough accounts are ready for the next two send days" if not want else "no account is waiting for a contact"
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
