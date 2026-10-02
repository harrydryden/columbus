"""The enrol job: today's accounts into their senders' Instantly campaigns.

SPEC 9 ("enrol", "Daily enrolment number", "Test assignment", "Instantly campaigns",
"Sender continuity"), SPEC 10 (copy), SPEC 11 ("Enrolment pause", weekly hand-check),
SPEC 1.5 (recipients). Runs at 12:00 UK (07:00 ET) on weekdays.

  1. Gates: not a blackout date or a non-send day; no positive reply waiting longer than
     escalation_hours (SPEC 11); this week's hand-check approved (SPEC 11); no stop rule in
     force (SPEC 12, learn/kill_rules.py).
  2. Today's number (queue.daily_number): the weekly target's share for today, each
     sender's free slots after the follow-ups already due (enrol/capacity.py), and the ready
     accounts. The Clay and Apollo budgets are monthly and applied where credits are spent.
  3. Candidates: verified accounts in Priority, Standard or Control whose domain is not
     suppressed or a partner, whose industry is on, with one sendable contact: a verified
     email, not suppressed, located in a known state other than CA or WA, not a personal
     domain or shared inbox, not enrolled before. Of several, the best-ranked one, as
     pick_contacts ranks them (Harry, 1 Oct 2026; clean/people.rank_person). A kill rule may
     hold back an industry group or an email source (learn/holds.py).
  4. In queue order (queue.order_key), control_share from Control and the rest from Priority
     then Standard, each account gets: its sender (kept for life; a paused sender's accounts
     wait), its Copy row (the most specific approved, QA-passed row for its industry and its
     contact's role, else its group's, else General; the running test's hash split takes
     half of version_a's accounts), its opener for that contact (enrol/openers.py: the angle
     setter's line for the contact's copy role, filled with the account's stored facts, or none,
     and the opener_holdout_share held out with none), its four rendered emails (any copy-rule
     violation skips it), and a HubSpot
     re-check (a customer, another owner, an open deal or an opted-out contact excludes it).
  5. Each owner's leads are bulk-added to "US Outbound – {owner}" with the rendered steps as
     custom variables.

Dry-run: all of it runs, the guard refuses the Instantly write, and nothing is marked
enrolled. HubSpot exclusions found on the way are still written to the database (SPEC 0.3).
Live (phase 2, after Harry signs off): accounts become enrolled with their sender, and each
contact records when and in which month it was enrolled, its angle, copy version, test, mailbox,
campaign and lead id, and its opener arm and source (opener, holdout or none; which line), so the
readout can compare opener against none.
Sent events come later, from sync_outcomes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from us_outbound.clean.domains import is_generic_mailbox, is_personal_domain
from us_outbound.clean.people import company_size, rank_person, state_code
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, Context
from us_outbound import budget, limits
from us_outbound.enrol import focus, openers, queue, render
from us_outbound.learn import holds
from us_outbound.logs import hash_email, log
from us_outbound.scoring.angle import legal_overlay
from us_outbound.settings.model import GENERAL_COPY, CopyRow, Mailbox, Settings


JOB = "enrol"
ENROLLED = "enrolled"
EXCLUDED = "Excluded"
SENDABLE_EMAIL_STATUSES = frozenset({"verified", "valid", "catch_all_valid"})  # Apollo verified; Clay (SPEC 8)
NEVER_STATES = frozenset({"CA", "WA"})  # SPEC 1.5
WAITING = ("open", "escalated")  # hitl_items still waiting for Harry
REPLY_KIND = "reply"  # hitl_items.kind of a reply waiting for a person (replies/poll.py KIND)
HUBSPOT_SOURCE = "hubspot"
# signal_events facts that scoring/tiers.py reads as hard exclusions, so a rescore keeps them.
HS_CUSTOMER = "hubspot_customer"
HS_OPEN_DEAL = "hubspot_open_deal"
HS_OTHER_OWNER = "hubspot_other_owner"
HS_OPTED_OUT = "hubspot_opted_out_or_bounced"
LIST_LIMIT = 100  # per-account lists in the summary
ID_CHUNK = 1000


# -- small helpers ----------------------------------------------------------------------


def _ts(v: Any) -> datetime | None:
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    return None


def _chunks(items: Sequence[str], n: int = ID_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def iso_week(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


# -- gates -------------------------------------------------------------------------------


def operator_pause(ctx: Context) -> str | None:
    """Why enrollment is paused by the stop command (SPEC 13); the stop rule's pause is holds.enrolment_stop."""
    from us_outbound.ops.heartbeat import enrolment_paused

    stop = enrolment_paused(ctx.store)
    if stop is None:
        return None
    return f"enrollment is stopped by an operator (stop at {stop.get('started_at')}); run `us-outbound start --live` to resume"


def reply_pause(ctx: Context) -> str | None:
    """SPEC 11: while any positive reply has waited more than escalation_hours, new enrollment pauses.

    The reply items are poll_replies' (kind "reply"; replies/items.py reads them, and the first kind
    name too): positive or referral, not yet handled, escalated included.
    """
    from us_outbound.replies.items import positive_waiting

    hours = ctx.settings.general.escalation_hours
    old = positive_waiting(ctx.store, ctx.now, hours)
    if not old:
        return None
    return f"{len(old)} positive {'reply has' if len(old) == 1 else 'replies have'} waited over {hours} hours for approval"


def _item_week(item: Mapping[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    if payload.get("iso_week"):
        return str(payload["iso_week"])
    t = _ts(item.get("created_at"))
    return iso_week(t.astimezone(UK).date()) if t else ""


def hand_check(ctx: Context, today: date) -> tuple[str | None, frozenset[str]]:
    """(why enrollment waits, or None; the account ids Harry pulled). SPEC 11 weekly hand-check.

    This ISO week's hand_check items (payload.iso_week, else created_at) must all be handled.
    Accounts listed in a handled item's payload.pulled_account_ids are not enrolled.
    """
    week = iso_week(today)
    items = [r for r in ctx.store.select("hitl_items", {"kind": "hand_check"}) if _item_week(r) == week]
    if not items:
        return f"this week's hand-check ({week}) has not been posted", frozenset()
    if any(r.get("status") != "handled" for r in items):
        return f"this week's hand-check ({week}) is not approved yet", frozenset()
    pulled = {
        str(x)
        for r in items
        if isinstance(r.get("payload"), Mapping)
        for x in (r["payload"].get("pulled_account_ids") or ())
    }
    return None, frozenset(pulled)


def gate(ctx: Context, today: date) -> str | None:
    """Why the job does nothing today, or None."""
    s = ctx.settings
    if budget.is_blackout(today, s):
        return f"{today} is a blackout date"
    if today.weekday() not in s.general.send_window.days:
        return f"{today} is not a send day"
    if ctx.live and not s.general.live_sending:
        return "live_sending is no"
    return operator_pause(ctx) or holds.enrolment_stop(ctx.store) or reply_pause(ctx)


# -- candidates --------------------------------------------------------------------------------


@dataclass
class Candidate:
    account: dict
    contact: dict


def suppressed(ctx: Context) -> tuple[set[str], set[str]]:
    """(suppressed domains, suppressed email hashes) in force now. A suppressed alias suppresses its root.

    A row with an email hash suppresses only that email; its domain is only recorded
    (suppression.py). A domain row has no email hash.
    """
    domains: set[str] = set()
    hashes: set[str] = set()
    for r in ctx.store.select("suppression"):
        expires = _ts(r.get("expires_at"))
        if expires is not None and expires <= ctx.now:
            continue
        if r.get("domain") and not r.get("email_sha256"):
            domains.add(_lower(r["domain"]))
        if r.get("email_sha256"):
            hashes.add(_lower(r["email_sha256"]))
    aliases = {_lower(a.get("alias")): _lower(a.get("root_domain")) for a in ctx.store.select("domain_aliases")}
    domains |= {aliases[d] for d in list(domains) if aliases.get(d)}
    return domains, hashes


def account_block(
    account: Mapping[str, Any], settings: Settings, domains: set[str], partners: set[str], pulled: frozenset[str]
) -> str | None:
    domain = _lower(account.get("domain"))
    if not domain:
        return "no domain"
    if domain in domains:
        return "domain suppressed"
    if domain in partners:
        return "partner domain"
    if str(account.get("account_id")) in pulled:
        return "pulled at this week's hand-check"
    ind = settings.industry(str(account.get("industry") or ""))
    if ind is not None and not ind.active:
        return "industry switched off"
    return None


def contact_block(contact: Mapping[str, Any], domains: set[str], hashes: set[str]) -> str | None:
    """Why this contact may not be emailed now (SPEC 1.5, 9 pick_contacts gate), or None."""
    email = _lower(contact.get("email"))
    if contact.get("enrolment_month") or contact.get("instantly_lead_id"):
        return "contact already enrolled"
    if "@" not in email:
        return "contact has no email"
    if contact.get("suppressed") or hash_email(email) in hashes or _lower(contact.get("email_sha256")) in hashes:
        return "contact suppressed"
    status = _lower(contact.get("email_status"))
    if status not in SENDABLE_EMAIL_STATUSES:
        return f"email status {status or 'unknown'}"
    state = state_code(str(contact.get("person_state") or ""))
    if state is None:  # blank, or not a US state ("Ontario", "London")
        return "contact state unknown"
    if state in NEVER_STATES:  # "CA", "California", "Calif.", "WA", "Washington"
        return "contact in CA or WA"
    domain = email.rsplit("@", 1)[1]
    if is_personal_domain(domain):
        return "personal email domain"
    if is_generic_mailbox(email):
        return "shared mailbox"
    if domain in domains:
        return "email domain suppressed"
    return None


def contact_order(
    contact: Mapping[str, Any], account: Mapping[str, Any] | None, settings: Settings | None
) -> tuple:
    """Where a stored contact comes for its account: as pick_contacts ranks people (clean/people.rank_person,
    Harry, 1 Oct 2026), then the first created. A title the Roles tab does not contact at the account's
    size comes after every one it does."""
    ranked = None
    if account is not None and settings is not None and settings.roles:
        size = company_size(account.get("employees"), account.get("size_band"))
        ranked = rank_person(contact.get("title"), settings.roles, size, settings.industry_group_of(account))
    rank = (0, ranked.key) if ranked else (1, ())
    return (*rank, str(contact.get("created_at") or ""), str(contact.get("contact_id")))


def pick_contact(
    contacts: Iterable[Mapping[str, Any]], domains: set[str], hashes: set[str],
    account: Mapping[str, Any] | None = None, settings: Settings | None = None,
) -> tuple[dict | None, str]:
    """The account's one contact in v1: the best-ranked sendable one (contact_order); else why the best is not."""
    first_reason = "no contact"
    for i, c in enumerate(sorted(contacts, key=lambda c: contact_order(c, account, settings))):
        why = contact_block(c, domains, hashes)
        if why is None:
            return dict(c), ""
        if i == 0:
            first_reason = why
    return None, first_reason


def candidates(ctx: Context, pulled: frozenset[str]) -> tuple[list[Candidate], Counter[str]]:
    """Every account that could be enrolled today, with its contact; and why the others cannot."""
    s, store = ctx.settings, ctx.store
    skipped: Counter[str] = Counter()
    accounts = store.select("accounts", {"status": "verified", "tier": list(queue.QUEUE_TIERS)})
    domains, hashes = suppressed(ctx)
    partners = {_lower(p.get("domain")) for p in store.select("partners")}
    stopped, sources = holds.stopped_groups(store), holds.paused_sources(store)  # kill rules (SPEC 12)
    contacts: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks([a["account_id"] for a in accounts]):
        for c in store.select("contacts", {"account_id": list(chunk)}):
            contacts[c["account_id"]].append(c)
    out: list[Candidate] = []
    for a in accounts:
        why = account_block(a, s, domains, partners, pulled)
        if why is None and s.industry_group_of(a).casefold() in stopped:
            why = "industry group stopped by a kill rule"
        contact = None
        if why is None:
            mine = contacts.get(a["account_id"], [])
            usable = [c for c in mine if _lower(c.get("email_source")) not in sources]
            contact, why = pick_contact(usable, domains, hashes, a, s)
            if contact is None and mine and not usable:
                why = "email source paused by a kill rule"
        if contact is None:
            skipped[why] += 1
            continue
        out.append(Candidate(a, contact))
    return out, skipped


# -- copy (SPEC 9 "Test assignment", SPEC 12; Harry, 30 Sep 2026: by industry and role) -------------


def sendable_copy(settings: Settings) -> dict[str, CopyRow]:
    """Copy rows that may be sent: approved, and passed QA in their current wording; in sheet order."""
    return {c.copy_version: c for c in settings.copy if c.status == "approved" and c.qa_current}


def copy_targets(account: Mapping[str, Any], role: str, settings: Settings) -> list[tuple[str, str]]:
    """(industry, role) from the most specific Copy row an account could get to the least:
    its label for its role, its label, its group for its role, its group, General for its role, General."""
    label = str(account.get("industry") or "").strip()
    group = settings.industry_group_of(account)
    out: list[tuple[str, str]] = []
    for industry in (label, group, GENERAL_COPY):
        for r in (role, ""):
            key = (industry.casefold(), r.casefold())
            if industry and key not in {(i.casefold(), x.casefold()) for i, x in out}:
                out.append((industry, r))
    return out


def _find(rows: Iterable[CopyRow], industry: str, role: str) -> CopyRow | None:
    return next((c for c in rows if c.industry.casefold() == industry.casefold()
                 and c.role.casefold() == role.casefold()), None)


def pick_copy(account: Mapping[str, Any], role: str, settings: Settings,
              rows: Mapping[str, CopyRow]) -> tuple[CopyRow | None, str]:
    """(the most specific sendable row, a note when a more specific row exists but cannot be sent yet)."""
    note = ""
    for target in copy_targets(account, role, settings):
        row = _find(rows.values(), *target)
        if row is not None:
            return row, note
        waiting = next((c for c in settings.copy if c.status != "retired" and c.industry.casefold() == target[0].casefold()
                        and c.role.casefold() == target[1].casefold()), None)
        if waiting is not None and not note:
            why = "a draft" if waiting.status == "draft" else "approved but has not passed QA in its current wording"
            note = f"{waiting.copy_version} is {why}"
    return None, note


def running_test_counts(ctx: Context) -> Counter[str]:
    """Accounts already in the running test, per copy version."""
    t = ctx.settings.running_test()
    if t is None:
        return Counter()
    seen: dict[str, set[str]] = defaultdict(set)
    for c in ctx.store.select("contacts", {"test_id": t.test_id}):
        seen[str(c.get("copy_version") or "")].add(str(c.get("account_id")))
    return Counter({v: len(ids) for v, ids in seen.items()})


def choose_copy(
    account: Mapping[str, Any], role: str, settings: Settings, counts: Mapping[str, int], rows: Mapping[str, CopyRow]
) -> tuple[CopyRow | None, str, str, str]:
    """(Copy row, test_id or "", why there is none, fallback note).

    The account gets the most specific sendable row for its industry and its contact's role
    (copy_targets). The running test takes accounts that would get its version_a, other than
    Control, and sends half of them (by account hash) version_b instead, while each version
    has fewer than accounts_per_version.
    """
    row, note = pick_copy(account, role, settings, rows)
    if row is None:
        label = str(account.get("industry") or settings.industry_group_of(account) or "its industry")
        return None, "", f"no approved copy that has passed QA for {label}, its group or General", note
    t = settings.running_test()
    if t and account.get("tier") != queue.CONTROL and row.copy_version == t.version_a:
        v = t.version_a if queue.test_version(str(account["account_id"]), t.test_id) == "a" else t.version_b
        chosen = rows.get(v)
        if chosen is not None and (t.accounts_per_version <= 0 or counts.get(v, 0) < t.accounts_per_version):
            return chosen, t.test_id, "", note
    return row, "", "", note


# -- opener -------------------------------------------------------------------------------------


def account_opener(
    ctx: Context, account: Mapping[str, Any], contact: Mapping[str, Any], check: Callable[[str], str] | None = None,
) -> tuple[openers.Opener, str]:
    """(opener, legal_overlay) for the account and its contact: the tokenized opener (enrol/openers.py).

    The opener is worked out at enrol time, when the contact and their copy role are known: the
    angle setter's line for the role, filled with the account's stored facts, else the signal's
    plain opener; a deterministic share of accounts is held out with none. The General angle
    (Control among it) has no signal line: with opener_focus on it gets the "what they do" line.
    An account with no line by then gets the generic line for the contact's role, then the plain
    generic line (General opener_generic_*; Harry, 2 Oct 2026), else none, and email 1's opener
    line disappears. check(text) is the copy-rule check each filled line must pass.
    """
    events = ctx.store.select("signal_events", {"account_id": account["account_id"]})
    op = openers.for_account(ctx, account, contact, events, check=check)
    return op, legal_overlay(ctx.settings.industry_group_of(account))


# -- HubSpot re-check (SPEC 9 enrol "re-checks HubSpot"; hard exclusions) ---------------------------


def hubspot_company_block(ctx: Context, account: Mapping[str, Any]) -> tuple[str, str] | None:
    """(fact, reason) when HubSpot excludes the company: a customer, another owner or an open deal.
    None when it is clear. verify_accounts makes the same check before an account is verified."""
    hs = ctx.clients.hubspot
    harry = ctx.settings.general.hubspot_owner_id.strip()
    for co in hs.search_companies_by_domain(str(account.get("domain") or "")):
        p = co.get("properties") or {}
        if _lower(p.get("lifecyclestage")) == "customer":
            return HS_CUSTOMER, "a customer in HubSpot"
        owner = str(p.get("hubspot_owner_id") or "").strip()
        if owner and owner != harry:
            return HS_OTHER_OWNER, "owned by someone else in HubSpot"
        if hs.open_deals_for_company(str(co["id"])):
            return HS_OPEN_DEAL, "an open deal in HubSpot"
    return None


def hubspot_block(ctx: Context, account: Mapping[str, Any], contact: Mapping[str, Any]) -> tuple[str, str] | None:
    """(fact, reason) when HubSpot now excludes the account: a customer, another owner, an open deal,
    or the contact opted out there. None when it is clear."""
    block = hubspot_company_block(ctx, account)
    if block:
        return block
    hs = ctx.clients.hubspot
    harry = ctx.settings.general.hubspot_owner_id.strip()
    for hc in hs.search_contacts_by_email(str(contact.get("email") or "")):
        p = hc.get("properties") or {}
        if _lower(p.get("hs_email_optout")) == "true":
            return HS_OPTED_OUT, "the contact opted out in HubSpot"
        if _lower(p.get("lifecyclestage")) == "customer":
            return HS_CUSTOMER, "the contact is a customer in HubSpot"
        owner = str(p.get("hubspot_owner_id") or "").strip()
        if owner and owner != harry:
            return HS_OTHER_OWNER, "the contact is owned by someone else in HubSpot"
    return None


def mark_excluded(ctx: Context, account: Mapping[str, Any], fact: str, reason: str) -> None:
    """Tier Excluded now, and a hubspot fact so the next rescore keeps it excluded (the database, so dry-run too)."""
    aid = account["account_id"]
    ctx.store.upsert("accounts", [{"account_id": aid, "tier": EXCLUDED, "tier_reason": reason}])
    ctx.store.insert(
        "signal_events",
        [{"event_id": new_id(), "account_id": aid, "source": HUBSPOT_SOURCE, "fact": fact, "value": True,
          "quote": "", "source_url": "", "observed_at": ctx.now}],
    )


# -- one account --------------------------------------------------------------------------------


@dataclass
class Prepared:
    account: dict
    contact: dict
    owner: str
    mailbox: str  # the address that sends step 1, when the owner has one Active mailbox; else ""
    copy_version: str
    angle: str
    test_id: str
    lead: dict
    opener_note: str = ""  # the opener lines passed over, and why (enrol/openers.py)
    copy_note: str = ""  # a more specific Copy row exists but cannot be sent yet
    opener_arm: str = openers.NONE  # opener, holdout or none: contacts.opener_arm, for the readout
    opener_source: str = ""  # the line's signal and column, "focus", or the generic line's General key


@dataclass
class Skip:
    reason: str  # a short category, counted in the summary
    detail: list[str] = field(default_factory=list)
    exclude_fact: str = ""  # set when HubSpot excludes the account


def prepare(
    ctx: Context, cand: Candidate, free: Mapping[str, int], counts: Mapping[str, int], rows: Mapping[str, CopyRow],
    pace: Mapping[str, int] | None = None,
) -> Prepared | Skip:
    s, g = ctx.settings, ctx.settings.general
    a, c = cand.account, cand.contact
    owner = queue.assign_sender(a, s, free, pace)
    if owner is None:
        sender = str(a.get("sender") or "")
        if sender and not s.mailboxes_for(sender, "Active"):
            return Skip("sender paused", [f"{sender} has no Active mailbox; the account waits for them"])
        if sender:
            return Skip("sender full today", [f"{sender}'s inboxes are full with follow-ups today; the account waits for them"])
        return Skip("no sending capacity")
    boxes: tuple[Mailbox, ...] = s.mailboxes_for(owner, "Active")
    mb = boxes[0]

    row, test_id, why, copy_note = choose_copy(a, str(c.get("role") or ""), s, counts, rows)
    if row is None:
        return Skip("no approved copy", [why, copy_note] if copy_note else [why])

    host = render.is_demo_host(mb, s)
    exempt = (str(a.get("clean_name") or ""), str(c.get("first_name") or ""))

    def check(text: str) -> str:
        return render.pick_opener(text, sender_is_harry=host, demo_host=g.demo_host, exempt=exempt)[1]

    op, overlay = account_opener(ctx, a, c, check)
    opener, note = render.pick_opener(op.text, sender_is_harry=host, demo_host=g.demo_host, exempt=exempt)
    if note:  # every filled opener goes through the copy rules once more, as it will be sent
        op = openers.Opener("", openers.NONE, "", (*op.notes, note))
    values = render.variables(a, c, mb, s, copy_row=row, opener=opener, legal_overlay=overlay)
    rendered = render.render_sequence(row, values, mailbox=mb, settings=s)
    problems = render.violations(rendered)
    if problems:
        return Skip("copy blocked", [f"{row.copy_version}: {p}" for p in problems])

    try:
        block = hubspot_block(ctx, a, c)
    except ApiError as exc:
        return Skip("HubSpot check failed", [str(exc)[:200]])
    if block:
        fact, reason = block
        return Skip(f"HubSpot: {reason}", [reason], exclude_fact=fact)

    lead = {
        "email": _lower(c.get("email")),
        "first_name": values["first_name"],
        "last_name": str(c.get("last_name") or "").strip(),
        "company_name": values["company"],
        "custom_variables": render.custom_variables(rendered),
    }
    return Prepared(
        account=a, contact=c, owner=owner, mailbox=mb.address if len(boxes) == 1 else "",
        copy_version=row.copy_version, angle=str(a.get("angle") or ""), test_id=test_id, lead=lead,
        opener_note="; ".join(op.notes), copy_note=copy_note, opener_arm=op.arm, opener_source=op.source,
    )


# -- the job --------------------------------------------------------------------------------------


@dataclass
class _Run:
    skipped: Counter[str] = field(default_factory=Counter)
    skipped_accounts: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    opener_fallbacks: list[dict] = field(default_factory=list)
    opener_arms: Counter[str] = field(default_factory=Counter)  # opener, holdout, none
    opener_sources: Counter[str] = field(default_factory=Counter)  # "<signal> / <column>", "focus", "opener_generic_ops"
    copy_fallbacks: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    def skip(self, account: Mapping[str, Any], reason: str, detail: Sequence[str] = ()) -> None:
        self.skipped[reason] += 1
        row = {"account_id": account.get("account_id"), "domain": account.get("domain"), "reason": reason,
               "detail": list(detail)[:5]}
        if len(self.skipped_accounts) < LIST_LIMIT:
            self.skipped_accounts.append(row)
        log("enrol_skip", account_id=row["account_id"], reason=reason, detail=row["detail"])


def _walk(
    ctx: Context, lane: Iterator[Candidate], target: int, free: Counter[str], counts: Counter[str],
    rows: Mapping[str, CopyRow], run: _Run, pace: Mapping[str, int] | None = None,
    quota: focus.Quota | None = None, held: list[Candidate] | None = None,
) -> list[Prepared]:
    """Prepare accounts in queue order until target are ready; skipped ones make way for the next.

    lane is an iterator, so a second walk carries on where the first stopped. With a quota,
    an account whose industry share is full today is held back (in held) instead, for the
    fill at the end.
    """
    out: list[Prepared] = []
    while len(out) < target:
        cand = next(lane, None)
        if cand is None:
            break
        if quota is not None and not quota.allows(cand.account):
            if held is not None:
                held.append(cand)
            continue
        p = prepare(ctx, cand, free, counts, rows, pace)
        if isinstance(p, Skip):
            run.skip(cand.account, p.reason, p.detail)
            if p.exclude_fact:
                mark_excluded(ctx, cand.account, p.exclude_fact, p.detail[0])
                run.excluded.append({"account_id": cand.account["account_id"], "reason": p.detail[0]})
            continue
        out.append(p)
        free[p.owner] -= 1
        if quota is not None:
            quota.take(cand.account)
        if p.test_id:
            counts[p.copy_version] += 1
        if p.opener_note and len(run.opener_fallbacks) < LIST_LIMIT:
            run.opener_fallbacks.append({"account_id": cand.account["account_id"], "reason": p.opener_note})
        run.opener_arms[p.opener_arm] += 1
        if p.opener_source:
            run.opener_sources[p.opener_source] += 1
        if p.copy_note:
            run.copy_fallbacks[f"sent {p.copy_version}: {p.copy_note}"] += 1
    return out


def _created_ids(result: Mapping[str, Any], leads: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """Lead position -> Instantly lead id, from the add summary's created_leads (index, else email)."""
    by_email = {_lower(lead["email"]): i for i, lead in enumerate(leads)}
    out: dict[int, str] = {}
    for c in result.get("created_leads") or ():
        lead_id = str(c.get("id") or "")
        i = c.get("index")
        if not isinstance(i, int) or not 0 <= i < len(leads) or (c.get("email") and _lower(c["email"]) != _lower(leads[i]["email"])):
            i = by_email.get(_lower(c.get("email")))
        if lead_id and i is not None:
            out[i] = lead_id
    return out


def _record_enrolled(ctx: Context, items: Sequence[Prepared], ids: Mapping[int, str], campaign: str, month: str) -> None:
    """Mark the accounts enrolled and give each contact its lead, month and enrolled_at (for the send forecast)."""
    accounts, contacts = [], []
    for i, p in enumerate(items):
        if i not in ids:
            continue
        row = {"account_id": p.account["account_id"], "status": ENROLLED}
        if not p.account.get("sender"):
            row["sender"] = p.owner  # set at first enrollment, never changed (SPEC 9)
        accounts.append(row)
        contacts.append({
            "contact_id": p.contact["contact_id"],
            "enrolment_month": month,
            "enrolled_at": ctx.now,
            "angle": p.angle,
            "copy_version": p.copy_version,
            "test_id": p.test_id or None,
            "mailbox": p.mailbox or None,
            "instantly_campaign": campaign,
            "instantly_lead_id": ids[i],
            "opener_arm": p.opener_arm,
            "opener_source": p.opener_source or None,
        })
    if accounts:
        ctx.store.upsert("accounts", accounts)
        ctx.store.upsert("contacts", contacts)


def run(ctx: Context) -> dict:
    """The enrol job (JOB CONTRACT: run(ctx) -> summary)."""
    s = ctx.settings
    today = ctx.now_et().date()
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "date": today.isoformat()}

    why = gate(ctx, today)
    pulled: frozenset[str] = frozenset()
    if why is None:
        why, pulled = hand_check(ctx, today)
    if why:
        summary.update(status="skipped", reason=why)
        log("enrol_done", run_id=ctx.run_id, **summary)
        return summary

    cands, skipped = candidates(ctx, pulled)
    lim = limits.today(ctx, today, ready_accounts=len(cands))
    n, terms = lim.number, lim.terms
    free = Counter({owner: c.free for owner, c in lim.senders.items()})
    pace = {owner: c.pace for owner, c in lim.senders.items()}
    r = _Run(skipped=skipped)

    # control_share from Control, the rest from Priority then Standard, and a shortfall in
    # either (too few, or skipped on the way) filled from the other (SPEC 9; queue.py docstring).
    in_order = sorted(cands, key=lambda c: queue.order_key(c.account, s))
    n_control = sum(c.account.get("tier") == queue.CONTROL for c in in_order)
    control = iter([c for c in in_order if c.account.get("tier") == queue.CONTROL])
    main = iter([c for c in in_order if c.account.get("tier") != queue.CONTROL])
    counts, approved = running_test_counts(ctx), sendable_copy(s)
    # Industry focus (enrol/focus.py): each share first; then, if a share had too few ready
    # accounts, the rest of the day in queue order whatever the group.
    quota = focus.today(ctx, lim.terms["send_days_left_in_week"])
    held_main: list[Candidate] = []
    held_control: list[Candidate] = []
    prepared = _walk(ctx, control, queue.control_count(n, s, n_control), free, counts, approved, r, pace, quota, held_control)
    prepared += _walk(ctx, main, n - len(prepared), free, counts, approved, r, pace, quota, held_main)
    prepared += _walk(ctx, control, n - len(prepared), free, counts, approved, r, pace, quota, held_control)
    for held in (held_main, held_control):
        fill = _walk(ctx, iter(held), n - len(prepared), free, counts, approved, r, pace)
        for p in fill:
            quota.take(p.account)
        prepared += fill

    by_owner: dict[str, list[Prepared]] = defaultdict(list)
    for p in prepared:
        by_owner[p.owner].append(p)
    enrolled: Counter[str] = Counter()
    would: Counter[str] = Counter()
    month = today.strftime("%Y-%m")
    for owner, items in by_owner.items():
        campaign = queue.campaign_name(owner)
        leads = [p.lead for p in items]
        try:
            result = ctx.clients.instantly.add_leads(campaign, leads)
        except LookupError as exc:  # the owner's campaign is missing or duplicated
            r.errors.append(f"{campaign}: {exc}")
            continue
        except ApiError as exc:  # nothing is marked; the accounts stay verified for the next run
            r.errors.append(f"{campaign}: {str(exc)[:200]}")
            continue
        if ctx.dry_run or not result or result.get("dry_run"):
            would[owner] = len(items)
            continue
        ids = _created_ids(result, leads)
        for i, p in enumerate(items):
            if i not in ids:
                r.skip(p.account, "not added by Instantly", ["no created lead in the add summary (in blocklist, or already in the workspace)"])
        _record_enrolled(ctx, items, ids, campaign, month)
        enrolled[owner] = len(ids)

    summary.update(
        status="ok",
        number=n,
        number_terms=terms,
        limited_by=lim.explanation,
        limits=lim.lines,
        focus=quota.describe() if quota.active else None,
        candidates=len(cands),
        prepared=len(prepared),
        enrolled=sum(enrolled.values()),
        by_owner=dict(enrolled) if ctx.live else dict(would),
        skipped=dict(r.skipped),
        skipped_accounts=r.skipped_accounts,
        excluded=r.excluded,
        opener_fallbacks=r.opener_fallbacks,
        openers={"arms": dict(r.opener_arms), "sources": dict(r.opener_sources)},
        copy_fallbacks=dict(r.copy_fallbacks),
        copy_sendable=len(approved),
        errors=r.errors,
    )
    log("enrol_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "skipped_accounts"})
    return summary
