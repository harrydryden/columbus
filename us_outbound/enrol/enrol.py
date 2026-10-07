"""The enrol job: today's accounts into their senders' Instantly campaigns.

SPEC 9 ("enrol", "Daily enrolment number", "Test assignment", "Instantly campaigns",
"Sender continuity"), SPEC 10 (copy), SPEC 11 ("Enrolment pause", weekly hand-check),
SPEC 1.5 (recipients). Runs at 12:00 UK (07:00 ET) on weekdays.

  1. Gates: not a blackout date or a non-send day; no positive reply waiting longer than
     escalation_hours (SPEC 11); no stop rule in force (SPEC 12, learn/kill_rules.py); and, while
     the General key auto_send is yes, this week's hand-check approved (SPEC 11). With auto_send
     = no every email is approved in Slack before it is sent, so the hand-check is not a gate
     (Harry, 2 Oct 2026); accounts pulled at an approved hand-check are still left out.
  2. Today's number (queue.daily_number): the weekly target's share for today, each
     sender's free slots after the follow-ups already due (enrol/capacity.py), and the ready
     accounts. The Clay and Apollo budgets are monthly and applied where credits are spent.
     Send approvals still waiting in Slack count towards the week and hold their sender's slots.
     Each owner's "US Outbound – {owner}" campaign is read from Instantly: in a live run an owner
     whose campaign is not active (only `us-outbound start --live` activates one), is missing, or
     cannot be read has no slots, so no card is proposed and no lead added for them; a dry run counts
     them and says so (enrol/capacity.campaigns_not_sending; summary campaigns_not_sending).
  3. Candidates: verified accounts in Priority, Standard or Control whose domain is not
     suppressed or a partner, whose industry is on, with no send approval waiting in Slack, and
     with one sendable contact: a verified
     email, not suppressed, located in a known state other than CA or WA, not a personal
     domain or shared inbox, not enrolled before. Of several, the best-ranked one, as
     pick_contacts ranks them (Harry, 1 Oct 2026; clean/people.rank_person). A kill rule may
     hold back an industry group or an email source (learn/holds.py). This is one check (eligible),
     and a send approval's ✅ runs it again for the card's contact (enrol/approvals.recheck).
  4. In queue order (queue.order_key), control_share from Control and the rest from Priority
     then Standard, each account gets: its sender (kept for life; a paused sender's accounts
     wait), its Copy row (the most specific approved, QA-passed row for its industry and its
     contact's role, else its group's, else General; the running test's hash split takes
     half of version_a's accounts), its opener for that contact (enrol/openers.py: the angle
     setter's line for the contact's copy role, filled with the account's stored facts, or none,
     and the opener_holdout_share held out with none), its email-1 subject arm (render.subject_arm: the
     email1_subject_share get General email1_subject, the rest the Copy row's s1_subject; Harry, 5 Oct 2026),
     its four rendered emails (any copy-rule violation skips it), and a HubSpot
     re-check (a customer, another owner, an open deal or an opted-out contact excludes it).
  5. auto_send = yes: each owner's leads are bulk-added to "US Outbound – {owner}" with the
     rendered steps as custom variables. A lead the add summary leaves out is looked up in the
     campaign (campaign_lead_ids): there, it is recorded; not there, Instantly refused it (its
     blocklist, or a lead in another campaign), so its contact is marked suppressed and
     pick_contacts finds the next person (mark_not_added) instead of the same one failing daily.
     Unless Instantly's plan is full (enrol/plan.py; Harry, 7 Oct 2026): then a lead left out is
     skipped as "Instantly plan limit" with its contact kept, no further owner's leads are added
     that run, and the approvers are asked, once a day, to make room.
     auto_send = no (the default; Harry, 2 Oct 2026: "every single message that gets sent out
     comes to this channel first for approval"): each account becomes a send approval instead,
     a hitl_items row and a card in the alert channel showing every email of the sequence, and
     its lead is added only when an approver's ✅ is read (enrol/approvals.py, poll_approvals).
     A live run without US_OUTBOUND_SLACK_BOT_TOKEN refuses, as there is nowhere to approve.

  6. Second contacts (General second_contact, no by default; enrol/second.py; Harry, 6 Oct 2026): at
     enrolled accounts of second_contact_min_employees or more staff, a person of another role,
     second_contact_delay_days after the first contact's email 1. They count in ready accounts and so in
     today's number, are walked after every first contact (so they take only what new accounts leave
     of the number and of their sender's slots), keep the account's sender, and are prepared and
     approved as above. Their contact_slot is 2; their account row is left as it is.

Dry-run: all of it runs, the guard refuses the Instantly write, and nothing is marked
enrolled. With auto_send = no no item is written; a few cards are posted to the dev channel
as a preview. HubSpot exclusions found on the way are still written to the database (SPEC 0.3).
Live (phase 2, after Harry signs off): accounts become enrolled with their sender, and each
contact records when and in which month it was enrolled, its angle, copy version, test, mailbox,
campaign and lead id, its opener arm and source (opener, holdout or none; which line), so the
readout can compare opener against none, and its subject arm (personal or copy), so it can compare
email 1's personal subject against the Copy row's.
Sent events come later, from sync_outcomes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from us_outbound.clean.domains import is_generic_mailbox, is_personal_domain, is_public_body
from us_outbound.clean.people import company_size, rank_person, state_code
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, Context
from us_outbound import budget, config_version, labels, limits
from us_outbound.clients import instantly as instantly_client
from us_outbound.enrol import capacity, focus, openers, plan, queue, render
from us_outbound.learn import holds
from us_outbound.logs import hash_email, log
from us_outbound.scoring.angle import legal_overlay
from us_outbound.scoring.score import MATCH_FACT, SCORING_SOURCE
from us_outbound.settings.model import GENERAL_COPY, CopyRow, Mailbox, Settings


JOB = "enrol"
ENROLLED = "enrolled"
EXCLUDED = "Excluded"
SENDABLE_EMAIL_STATUSES = frozenset({"verified", "valid", "catch_all_valid"})  # Apollo verified; Clay (SPEC 8)
NEVER_STATES = frozenset({"CA", "WA"})  # SPEC 1.5
WAITING = ("open", "escalated")  # hitl_items still waiting for Harry
HUBSPOT_SOURCE = "hubspot"
# signal_events facts that scoring/tiers.py reads as hard exclusions, so a rescore keeps them.
HS_CUSTOMER = "hubspot_customer"
HS_OPEN_DEAL = "hubspot_open_deal"
HS_OTHER_OWNER = "hubspot_other_owner"
HS_OPTED_OUT = "hubspot_opted_out_or_bounced"
LIST_LIMIT = 100  # per-account lists in the summary
ID_CHUNK = 1000
NOT_ADDED = "not added by Instantly (blocklist, or already in another campaign)"  # contacts.suppressed_reason
OPTOUT_UNTESTED = ("optout_tested is no: the seed-inbox test of Instantly's unsubscribe link is not done, so nothing "
                   "is sent; once it is, set optout_tested = yes on the General tab")


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


def has_sample(payload: Any) -> bool:
    """Whether a hand_check item's payload holds the weekly random sample (per_group > 0). One recorded while
    auto_send was no holds only the accounts with doubtful facts (per_group 0); an item from before per_group
    was kept always had the sample."""
    n = payload.get("per_group") if isinstance(payload, Mapping) else None
    if n is None:
        return True
    try:
        return int(n) > 0
    except (TypeError, ValueError):
        return False


def hand_check(ctx: Context, today: date) -> tuple[str | None, frozenset[str]]:
    """(why enrollment waits, or None; the account ids Harry pulled). SPEC 11 weekly hand-check.

    This ISO week's hand_check items (payload.iso_week, else created_at) must all be handled.
    Accounts listed in a handled item's payload.pulled_account_ids are not enrolled. While auto_send
    is yes the week's items must hold the random sample too: one recorded while auto_send was no
    (only the doubtful accounts) does not pass the gate until the sample is added (hand_check.py).
    """
    week = iso_week(today)
    items = [r for r in ctx.store.select("hitl_items", {"kind": "hand_check"}) if _item_week(r) == week]
    if not items:
        return f"this week's hand-check ({week}) has not been posted", frozenset()
    if ctx.settings.general.auto_send and not any(has_sample(r.get("payload")) for r in items):
        return (f"this week's hand-check ({week}) has no random sample: it was recorded while auto_send was no, "
                "so it held only the accounts with doubtful facts. Run `us-outbound run hand_check_post --live` "
                "to post the sample, or `us-outbound handcheck approve --live` once you have checked it with "
                "`us-outbound handcheck show`"), frozenset()
    if any(r.get("status") != "handled" for r in items):
        return f"this week's hand-check ({week}) is not approved yet", frozenset()
    pulled = {
        str(x)
        for r in items
        if isinstance(r.get("payload"), Mapping)
        for x in (r["payload"].get("pulled_account_ids") or ())
    }
    return None, frozenset(pulled)


def optout_untested(ctx: Context) -> str | None:
    """Live only: why nothing may be sent while the seed-inbox test of Instantly's unsubscribe link is not
    done (optout_tested, golive's "Opt-out tested"). A dry run still previews."""
    if ctx.live and not ctx.settings.general.optout_tested:
        return OPTOUT_UNTESTED
    return None


def gate(ctx: Context, today: date) -> str | None:
    """Why the job does nothing today, or None.

    live_sending is not checked here: a job is live only with --live and live_sending = yes
    (bootstrap.resolve_live), so a live run always has it.
    """
    s = ctx.settings
    if budget.is_blackout(today, s):
        return f"{today} is a blackout date"
    if today.weekday() not in s.general.send_window.days:
        return f"{today} is not a send day"
    return optout_untested(ctx) or operator_pause(ctx) or holds.enrolment_stop(ctx.store) or reply_pause(ctx)


# -- candidates --------------------------------------------------------------------------------


@dataclass
class Candidate:
    account: dict
    contact: dict
    slot: int = 1  # contacts.contact_slot: 2 for a second contact at the account (enrol/second.py)
    first: dict | None = None  # a second contact's first contact, as second.first_summary gives it


def _in_force(ctx: Context, rows: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    """(domains, email hashes) of these suppression rows that are in force now. A row with an email hash
    suppresses only that email; its domain is only recorded (suppression.py). A domain row has no email hash."""
    domains: set[str] = set()
    hashes: set[str] = set()
    for r in rows:
        expires = _ts(r.get("expires_at"))
        if expires is not None and expires <= ctx.now:
            continue
        if r.get("domain") and not r.get("email_sha256"):
            domains.add(_lower(r["domain"]))
        if r.get("email_sha256"):
            hashes.add(_lower(r["email_sha256"]))
    return domains, hashes


def suppressed(ctx: Context) -> tuple[set[str], set[str]]:
    """(suppressed domains, suppressed email hashes) in force now. A suppressed alias suppresses its root.

    The whole table, for a run over every account (candidates); suppressed_for looks up a few.
    """
    domains, hashes = _in_force(ctx, ctx.store.select("suppression"))
    aliases = {_lower(a.get("alias")): _lower(a.get("root_domain")) for a in ctx.store.select("domain_aliases")}
    domains |= {aliases[d] for d in list(domains) if aliases.get(d)}
    return domains, hashes


def suppressed_for(ctx: Context, domains: Iterable[str], hashes: Iterable[str]) -> tuple[set[str], set[str]]:
    """The part of suppressed() that concerns these domains and email hashes, by indexed lookups: the
    suppression rows for each domain and for each of its aliases (a suppressed alias suppresses its root),
    and the rows for each hash. A send approval's re-check reads this, not the whole table."""
    want = sorted({_lower(d) for d in domains if _lower(d)})
    want_hashes = sorted({_lower(h) for h in hashes if _lower(h)})
    root_of = {_lower(a.get("alias")): _lower(a.get("root_domain"))
               for a in (ctx.store.select("domain_aliases", {"root_domain": want}) if want else ())}
    keys = sorted(set(want) | set(root_of))
    rows = ctx.store.select("suppression", {"domain": keys, "email_sha256": None}) if keys else []
    if want_hashes:
        rows += ctx.store.select("suppression", {"email_sha256": want_hashes})
    found, found_hashes = _in_force(ctx, rows)
    found |= {root_of[d] for d in list(found) if root_of.get(d)}
    return found & set(want), found_hashes & set(want_hashes)


@dataclass(frozen=True)
class Gates:
    """What the account and contact checks read, loaded once: the enrol run's candidates and a send approval's
    re-check (enrol/approvals.recheck) go through the same checks (account_reason, eligible)."""

    settings: Settings
    domains: Collection[str]  # suppressed domains, aliases' roots included
    hashes: Collection[str]  # suppressed email hashes
    partners: Collection[str]
    pulled: frozenset[str] = frozenset()  # accounts pulled at this week's hand-check
    stopped: Collection[str] = frozenset()  # industry groups a kill rule stopped, casefolded (learn/holds.py)
    sources: Collection[str] = frozenset()  # email sources a kill rule paused


def gates(ctx: Context, pulled: frozenset[str] = frozenset(),
          suppressed_now: tuple[set[str], set[str]] | None = None) -> Gates:
    """The Gates in force now; suppressed_now when the caller looked up only what it needs (suppressed_for)."""
    domains, hashes = suppressed(ctx) if suppressed_now is None else suppressed_now
    partners = {_lower(p.get("domain")) for p in ctx.store.select("partners")}
    return Gates(ctx.settings, domains, hashes, partners, pulled,
                 frozenset(holds.stopped_groups(ctx.store)), frozenset(holds.paused_sources(ctx.store)))


def account_block(
    account: Mapping[str, Any], settings: Settings, domains: Collection[str], partners: Collection[str],
    pulled: Collection[str],
) -> str | None:
    domain = _lower(account.get("domain"))
    if not domain:
        return "no domain"
    if is_public_body(domain):
        return "a public body, never prospected"
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


def contact_block(contact: Mapping[str, Any], domains: Collection[str], hashes: Collection[str]) -> str | None:
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
    if is_public_body(domain):
        return "a public body's email domain"
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
    contacts: Iterable[Mapping[str, Any]], domains: Collection[str], hashes: Collection[str],
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


def account_reason(account: Mapping[str, Any], g: Gates, waiting: Collection[str] = frozenset()) -> str | None:
    """Why the account may not be emailed now, or None: account_block, a send approval still waiting for it
    (waiting), or a kill rule that stopped its industry group (SPEC 12)."""
    why = account_block(account, g.settings, g.domains, g.partners, g.pulled)
    if why is None and str(account.get("account_id")) in waiting:
        why = "waiting for approval in Slack"
    if why is None and g.settings.industry_group_of(account).casefold() in g.stopped:
        why = "industry group stopped by a kill rule"
    return why


def eligible(
    account: Mapping[str, Any], contacts: Sequence[Mapping[str, Any]], g: Gates, waiting: Collection[str] = frozenset(),
) -> tuple[dict | None, str]:
    """(the contact to email at the account, or None; why not): the one eligibility check. The account's checks
    (account_reason), then its best-ranked sendable contact (pick_contact), leaving out an email source a kill
    rule paused. The enrol run's candidates call it with every contact on file; a send approval's re-check calls
    it with the card's contact (enrol/approvals.recheck)."""
    why = account_reason(account, g, waiting)
    if why is not None:
        return None, why
    usable = [c for c in contacts if _lower(c.get("email_source")) not in g.sources]
    contact, why = pick_contact(usable, g.domains, g.hashes, account, g.settings)
    if contact is None and contacts and not usable:
        why = "email source paused by a kill rule"
    return contact, why


def candidates(
    ctx: Context, pulled: frozenset[str], waiting: Collection[str] = frozenset(),
) -> tuple[list[Candidate], Counter[str]]:
    """Every account that could be enrolled today, with its contact; and why the others cannot.

    waiting: accounts with a send approval still waiting in Slack (enrol/approvals.py), which are not
    proposed again until it is approved, declined or expires.
    """
    store = ctx.store
    skipped: Counter[str] = Counter()
    accounts = store.select("accounts", {"status": "verified", "tier": list(queue.QUEUE_TIERS)})
    g = gates(ctx, pulled)
    contacts: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks([a["account_id"] for a in accounts]):
        for c in store.select("contacts", {"account_id": list(chunk)}):
            contacts[c["account_id"]].append(c)
    out: list[Candidate] = []
    for a in accounts:
        contact, why = eligible(a, contacts.get(a["account_id"], []), g, waiting)
        if contact is None:
            skipped[why] += 1
            continue
        out.append(Candidate(a, contact))
    return out, skipped


# -- copy (SPEC 9 "Test assignment", SPEC 12; Harry, 30 Sep 2026: by industry and role) -------------


def sendable_copy(settings: Settings) -> dict[str, CopyRow]:
    """Copy rows that may be sent: approved, and passed QA in their current wording; in sheet order."""
    return {c.copy_version: c for c in settings.copy if c.status == "approved" and c.qa_current}


def copy_targets(account: Mapping[str, Any], role: str, settings: Settings,
                 level: str | None = None) -> list[tuple[str, str]]:
    """(industry, role) from the most specific Copy row an account could get to the least:
    its label for its role, its label, its group for its role, its group, General for its role, General.

    The label check (labels.copy_level; Harry, 7 Oct 2026) says how specific the copy may be: a label's own rows only
    when its label was confirmed (the rules and the model agree, the model is sure, an Overrides row or an approver),
    the group's when the two disagreed within the group or it is not checked yet, General's when they disagreed
    across groups. So a doubtful label never carries a label's pitch. level: that level, when not the account's."""
    level = level or labels.copy_level(account)
    label = str(account.get("industry") or "").strip() if level == labels.LABEL_COPY else ""
    group = settings.industry_group_of(account) if level in (labels.LABEL_COPY, labels.GROUP_COPY) else ""
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
        level = labels.copy_level(account)
        where = {labels.LABEL_COPY: f"{label}, its group or General",
                 labels.GROUP_COPY: f"{label}'s group or General (its label earns the group's copy)",
                 labels.GENERAL_COPY_LEVEL: f"General (the label check left {label} in doubt)"}[level]
        return None, "", f"no approved copy that has passed QA for {where}", note
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


def mark_excluded(ctx: Context, account: Mapping[str, Any], fact: str, reason: str,
                  source: str = HUBSPOT_SOURCE) -> None:
    """Tier Excluded now, and a fact so the next rescore keeps it excluded (the database, so dry-run too).

    fact is one scoring/tiers.py reads as a hard exclusion: a hubspot one, or declined_in_slack (an
    approver dropped the company at a send approval, source send_approval; enrol/approvals.py).
    """
    aid = account["account_id"]
    ctx.store.upsert("accounts", [{"account_id": aid, "tier": EXCLUDED, "tier_reason": reason}])
    ctx.store.insert(
        "signal_events",
        [{"event_id": new_id(), "account_id": aid, "source": source, "fact": fact, "value": True,
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
    subject_arm: str = render.COPY_SUBJECT  # personal or copy: email 1's subject (contacts.subject_arm)
    # What a send approval's card shows and an edit re-renders with (enrol/approvals.py): the four emails
    # as rendered, the variables they were filled with, and the mailbox they were rendered for.
    rendered: list[render.Rendered] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    render_mailbox: str = ""
    slot: int = 1  # contacts.contact_slot (enrol/second.py)
    first: dict | None = None  # a second contact's first contact (second.first_summary), for the card
    # What the lead was rendered under (config_version.py; Harry, 7 Oct 2026): the run's config version and code,
    # and the Copy row's wording. A send approval's card carries them, so ✅ stamps what the card shows.
    config_version: str = ""
    code_sha: str = ""
    copy_hash: str = ""
    # The label check (labels.py; Harry, 7 Oct 2026): its latest verdict ({} when none) and the copy level its label
    # earns, for the card's Industry line.
    label_check: dict = field(default_factory=dict)
    copy_level: str = ""


@dataclass
class Skip:
    reason: str  # a short category, counted in the summary
    detail: list[str] = field(default_factory=list)
    exclude_fact: str = ""  # set when HubSpot excludes the account


def prepare(
    ctx: Context, cand: Candidate, free: Mapping[str, int], counts: Mapping[str, int], rows: Mapping[str, CopyRow],
    pace: Mapping[str, int] | None = None, not_sending: Mapping[str, str] | None = None,
) -> Prepared | Skip:
    """One account made ready to send, or why not. not_sending: owner -> why their campaign takes no new leads
    (limits.Limits.not_sending); an account whose sender is held by it waits, and says so."""
    s, g = ctx.settings, ctx.settings.general
    a, c = cand.account, cand.contact
    owner = queue.assign_sender(a, s, free, pace)
    if owner is None:
        sender = str(a.get("sender") or "")
        if sender and not s.mailboxes_for(sender, "Active"):
            return Skip("sender paused", [f"{sender} has no Active mailbox; the account waits for them"])
        if sender and (not_sending or {}).get(sender) and free.get(sender, 0) <= 0:
            return Skip("sender's campaign not sending", [f"{not_sending[sender]}; the account waits for {sender}"])
        if sender:
            return Skip("sender full today", [f"{sender}'s inboxes are full with follow-ups today; the account waits for them"])
        return Skip("no sending capacity")
    boxes: tuple[Mailbox, ...] = s.mailboxes_for(owner, "Active")
    mb = boxes[0]

    row, test_id, why, copy_note = choose_copy(a, str(c.get("role") or ""), s, counts, rows)
    if row is None:
        return Skip("no approved copy", [why, copy_note] if copy_note else [why])
    if cand.first and row.copy_version == cand.first.get("copy_version"):
        # Two colleagues with the same email reads as a mail merge (enrol/second.py).
        return Skip("same copy as the first contact", [f"{row.copy_version} is the row the first contact got"])

    host = render.is_demo_host(mb, s)
    exempt = (str(a.get("clean_name") or ""), str(c.get("first_name") or ""))

    def check(text: str) -> str:
        return render.pick_opener(text, sender_is_harry=host, demo_host=g.demo_host, exempt=exempt)[1]

    op, overlay = account_opener(ctx, a, c, check)
    opener, note = render.pick_opener(op.text, sender_is_harry=host, demo_host=g.demo_host, exempt=exempt)
    if note:  # every filled opener goes through the copy rules once more, as it will be sent
        op = openers.Opener("", openers.NONE, "", (*op.notes, note))
    values = render.variables(a, c, mb, s, copy_row=row, opener=opener, legal_overlay=overlay)
    arm = render.subject_arm(a.get("account_id"), s)  # Harry, 5 Oct 2026: email 1's personal subject, as a split
    rendered = render.render_sequence(row, values, mailbox=mb, settings=s, subject_arm=arm)
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
    verdict = labels.latest_verdict(ctx.store.select(
        "signal_events", {"account_id": a["account_id"], "source": labels.JOB, "fact": labels.VERDICT_FACT}))
    return Prepared(
        account=a, contact=c, owner=owner, mailbox=mb.address if len(boxes) == 1 else "",
        copy_version=row.copy_version, angle=str(a.get("angle") or ""), test_id=test_id, lead=lead,
        opener_note="; ".join(op.notes), copy_note=copy_note, opener_arm=op.arm, opener_source=op.source,
        subject_arm=arm, rendered=rendered, values=values, render_mailbox=mb.address, slot=cand.slot,
        first=cand.first, copy_hash=row.content_hash(), label_check=verdict or {}, copy_level=labels.copy_level(a),
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
    subject_arms: Counter[str] = field(default_factory=Counter)  # personal, copy
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
    quota: focus.Quota | None = None, held: list[Candidate] | None = None, not_sending: Mapping[str, str] | None = None,
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
        p = prepare(ctx, cand, free, counts, rows, pace, not_sending)
        if isinstance(p, Skip):
            run.skip(cand.account, p.reason if cand.slot == 1 else f"second contact: {p.reason}", p.detail)
            if p.exclude_fact:
                mark_excluded(ctx, cand.account, p.exclude_fact, p.detail[0])
                run.excluded.append({"account_id": cand.account["account_id"], "reason": p.detail[0]})
            continue
        out.append(p)
        free[p.owner] -= 1
        if quota is not None:
            quota.take(cand.account)
        if p.test_id and not (p.first and p.first.get("test_id") == p.test_id):  # the test counts accounts
            counts[p.copy_version] += 1
        if p.opener_note and len(run.opener_fallbacks) < LIST_LIMIT:
            run.opener_fallbacks.append({"account_id": cand.account["account_id"], "reason": p.opener_note})
        run.opener_arms[p.opener_arm] += 1
        run.subject_arms[p.subject_arm] += 1
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


def campaign_lead_ids(ctx: Context, campaign: str, emails: Iterable[str]) -> dict[str, str]:
    """email (lower case) -> Instantly lead id, for these emails among the campaign's own leads (list_leads).

    The add summary's created_leads leaves out a lead Instantly did not create, and also one that is in the
    campaign already (an earlier add whose run stopped before recording it), so a lead missing from it is
    looked up here before anything is concluded. Raises ApiError or LookupError when the campaign cannot be read.
    """
    want = {_lower(e) for e in emails if _lower(e)}
    out: dict[str, str] = {}
    if not want:
        return out
    for lead in ctx.clients.instantly.list_leads(campaign):
        email = _lower(lead.get("email"))
        if email in want and lead.get("id") and email not in out:
            out[email] = str(lead["id"])
    return out


def mark_not_added(ctx: Context, contact_id: Any) -> None:
    """Instantly would not take this contact (its blocklist, or a lead in another campaign of the workspace):
    marked suppressed, so pick_contacts finds the next person at the account and the account goes on. Not the
    suppression table: they did not opt out."""
    if contact_id:
        ctx.store.update("contacts", {"contact_id": contact_id}, {"suppressed": True, "suppressed_reason": NOT_ADDED})


def signals_now(ctx: Context, account_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """account_id -> the Score signals its latest scoring matched, as [{signal, weight}], strongest first.

    Scoring rewrites these rows every run (scoring/score.py), so a contact keeps its own copy from the
    moment it is enrolled (contacts.signals_at_enrol): the readout then judges the signals an account had
    when it was emailed, not the ones it has weeks later (v_signal_value; Harry, 5 Oct 2026).
    """
    out: dict[str, list[dict[str, Any]]] = {a: [] for a in account_ids}
    if not account_ids:
        return out
    rows = ctx.store.select("signal_events", {"account_id": list(account_ids), "source": SCORING_SOURCE,
                                              "fact": MATCH_FACT})
    for r in rows:
        v = r.get("value") if isinstance(r.get("value"), Mapping) else {}
        if v.get("signal"):
            out.setdefault(str(r["account_id"]), []).append({"signal": str(v["signal"]), "weight": v.get("weight")})
    for matches in out.values():
        matches.sort(key=lambda m: (-(m["weight"] if isinstance(m["weight"], (int, float)) else 0), m["signal"]))
    return out


def _record_enrolled(ctx: Context, items: Sequence[Prepared], ids: Mapping[int, str], campaign: str, month: str) -> None:
    """Mark the accounts enrolled and give each contact its lead, month and enrolled_at (for the send forecast),
    its opener and subject arms (the readout's two splits), and what the account looked like then: its signals,
    score and tier (signals_now); where the contact's details came from and the lawful basis
    (render.data_record), kept here since no email carries it; and what its emails were rendered under, its
    config version, code and Copy wording (config_version.py; Harry, 7 Oct 2026), so the cohort report compares
    like with like. A second contact (enrol/second.py) leaves its account as it is: enrolled already, with its
    sender, and never moved back from engaged by a late approval."""
    accounts, contacts = [], []
    record = render.data_record(ctx.settings)
    signals = signals_now(ctx, [str(p.account["account_id"]) for i, p in enumerate(items) if i in ids])
    for i, p in enumerate(items):
        if i not in ids:
            continue
        if p.slot == 1:
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
            "subject_arm": p.subject_arm,
            "signals_at_enrol": signals.get(str(p.account["account_id"]), []),
            "score_at_enrol": p.account.get("score"),
            "tier_at_enrol": p.account.get("tier") or None,
            "data_record": record,
            "contact_slot": p.slot,
            # The cohort stamp (Harry, 7 Oct 2026): NULL for a card posted before it existed, read as unstamped.
            "config_version": p.config_version or None,
            "code_sha": p.code_sha or None,
            "copy_hash": p.copy_hash or None,
        })
    if accounts:
        ctx.store.upsert("accounts", accounts)
    if contacts:
        ctx.store.upsert("contacts", contacts)


def run(ctx: Context) -> dict:
    """The enrol job (JOB CONTRACT: run(ctx) -> summary)."""
    from us_outbound.enrol import approvals, second  # they build on this module

    s = ctx.settings
    today = ctx.now_et().date()
    # Harry, 2 Oct 2026: with auto_send = no every email waits for an approver's ✅ in Slack.
    approve = not s.general.auto_send
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "date": today.isoformat(),
                               "auto_send": s.general.auto_send}

    why = gate(ctx, today)
    pulled: frozenset[str] = frozenset()
    if why is None:
        waits, pulled = hand_check(ctx, today)
        why = None if approve else waits  # every email is approved anyway, so no weekly gate
    slack = None
    if why is None and approve:
        slack, why = approvals.slack_for(ctx)  # a live run needs the token
    if why:
        summary.update(status="skipped", reason=why)
        log("enrol_done", run_id=ctx.run_id, **summary)
        return summary
    # What today's leads are rendered under (Harry, 7 Oct 2026): kept once per version, and stamped on each contact.
    cv = config_version.current(ctx)
    config_version.record(ctx, cv)
    summary["config_version"] = cv.id

    # Send approvals still waiting (in either mode: auto_send may have been switched on since) are
    # not proposed again, and hold their sender's slots and their place in the week.
    held = approvals.waiting(ctx)
    cands, skipped = candidates(ctx, pulled, held.accounts)
    # Second contacts (enrol/second.py; Harry, 6 Oct 2026): none, without a read, while second_contact is no.
    seconds, not_second = second.candidates(ctx, pulled, held.accounts)
    # campaigns=True: an owner whose Instantly campaign is not active gets no capacity in a live run (limits.py).
    lim = limits.today(ctx, today, ready_accounts=len(cands) + len(seconds), pending=held.by_owner, campaigns=True,
                       second_ready=len(seconds))
    stopped = lim.not_sending
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
    prepared = _walk(ctx, control, queue.control_count(n, s, n_control), free, counts, approved, r, pace, quota,
                     held_control, stopped)
    prepared += _walk(ctx, main, n - len(prepared), free, counts, approved, r, pace, quota, held_main, stopped)
    prepared += _walk(ctx, control, n - len(prepared), free, counts, approved, r, pace, quota, held_control, stopped)
    for held in (held_main, held_control):
        fill = _walk(ctx, iter(held), n - len(prepared), free, counts, approved, r, pace, not_sending=stopped)
        for p in fill:
            quota.take(p.account)
        prepared += fill
    # Second contacts last, from what the first contacts of new accounts left of today's number and of their
    # senders' slots; outside the industry focus, which shares out new accounts.
    n_first = len(prepared)
    prepared += _walk(ctx, iter(seconds), n - len(prepared), free, counts, approved, r, pace, not_sending=stopped)
    for p in prepared:
        p.config_version, p.code_sha = cv.id, cv.code_sha

    by_owner: dict[str, list[Prepared]] = defaultdict(list)
    for p in prepared:
        by_owner[p.owner].append(p)
    enrolled: Counter[str] = Counter()
    would: Counter[str] = Counter()
    month = today.strftime("%Y-%m")
    proposed: dict[str, Any] | None = None
    if approve:  # each account becomes a send approval instead of a lead (enrol/approvals.py)
        proposed = approvals.propose(ctx, prepared, lim, slack)
        would = Counter(proposed["by_owner"])
        r.errors += proposed["errors"]
        by_owner = {}
    plan_room: dict[str, Any] | None = None  # what the last live add said of Instantly's plan (enrol/plan.py)
    for owner, items in by_owner.items():
        campaign = queue.campaign_name(owner)
        leads = [p.lead for p in items]
        if plan_room and plan_room.get("full"):  # no room: no more adds this run, and every contact is kept
            for p in items:
                r.skip(p.account, plan.SKIP, [plan.KEPT])
            continue
        try:
            result = ctx.clients.instantly.add_leads(campaign, leads)
        except LookupError as exc:  # the owner's campaign is missing or duplicated
            r.errors.append(f"{campaign}: {exc}")
            continue
        except ApiError as exc:  # nothing is marked; the accounts stay verified for the next run
            if instantly_client.plan_full_error(exc):
                plan_room = {"remaining_in_plan": None, "full": True, "alert": plan.full_alert(ctx)}
                for p in items:
                    r.skip(p.account, plan.SKIP, [plan.KEPT])
                continue
            r.errors.append(f"{campaign}: {str(exc)[:200]}")
            continue
        if ctx.dry_run or not result or result.get("dry_run"):
            would[owner] = len(items)
            continue
        plan_room = plan.after_add(ctx, result)
        try:
            if capacity.resume_if_completed(ctx, campaign):
                log("campaign_resumed", campaign=campaign, leads=len(items))
        except (ApiError, LookupError) as exc:
            r.errors.append(f"{campaign} is completed in Instantly and could not be resumed ({str(exc)[:160]}): "
                            "run `us-outbound start --live`, or the leads just added wait unsent")
        ids = _created_ids(result, leads)
        missing = [i for i in range(len(items)) if i not in ids]
        found: dict[str, str] | None = {}
        if missing:  # in the campaign after all (recorded), or refused (the contact suppressed, so the next is found)
            try:
                found = campaign_lead_ids(ctx, campaign, [leads[i]["email"] for i in missing])
            except (ApiError, LookupError) as exc:
                found = None
                r.errors.append(f"{campaign}: the leads Instantly left out could not be looked up ({str(exc)[:160]})")
        for i in missing:
            email = _lower(leads[i]["email"])
            if found is None:
                r.skip(items[i].account, "not added by Instantly",
                       ["not in the add summary, and the campaign could not be read: the next run tries again"])
            elif email in found:
                ids[i] = found[email]
            elif plan_room.get("full"):  # left out for want of room, not refused: the contact is kept
                r.skip(items[i].account, plan.SKIP, [plan.KEPT])
            else:
                mark_not_added(ctx, items[i].contact.get("contact_id"))
                r.skip(items[i].account, "not added by Instantly",
                       [f"{NOT_ADDED}: the contact is suppressed, so pick_contacts finds the next person"])
        _record_enrolled(ctx, items, ids, campaign, month)
        enrolled[owner] = len(ids)

    summary.update(
        status="ok",
        number=n,
        number_terms=terms,
        limited_by=lim.explanation,
        limits=lim.lines,
        campaigns_not_sending=stopped,
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
        subjects={"arms": dict(r.subject_arms)},
        copy_fallbacks=dict(r.copy_fallbacks),
        copy_sendable=len(approved),
        second_contacts=second.tally(seconds, not_second, len(prepared) - n_first, s),
        instantly_plan=plan_room,
        errors=r.errors,
    )
    if proposed is not None:  # by_owner: the cards posted (live) or that would be (dry-run)
        summary.update(by_owner=dict(would), send_approvals=proposed)
    log("enrol_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "skipped_accounts"})
    return summary
