"""The enrol job: today's accounts into their senders' Instantly campaigns.

SPEC 9 ("enrol", "Daily enrolment number", "Test assignment", "Instantly campaigns",
"Sender continuity"), SPEC 10 (copy), SPEC 11 ("Enrolment pause", weekly hand-check),
SPEC 1.5 (recipients). Runs at 12:00 UK (07:00 ET) on weekdays.

  1. Gates: not a blackout date or a non-send day; optout_tested yes (a live run); no positive reply waiting longer
     than ESCALATION_HOURS (SPEC 11); no stop rule in force (SPEC 12, learn/kill_rules.py); and, while the General
     key hand_check_gate is yes, this week's hand-check approved (SPEC 11). With hand_check_gate = no (Harry's D5,
     19 Oct 2026) it holds only the companies it sampled until they are judged, for 7 days at most (hand_check()).
     Accounts pulled at the hand-check are left out either way.
  2. Today's number (queue.daily_number): the weekly target's share for today, each
     sender's free slots after the follow-ups already due (enrol/capacity.py), and the ready
     accounts. The Apollo budget is monthly and applied where credits are spent.
     Each owner's "US Outbound – {owner}" campaign is read from Instantly: in a live run an owner
     whose campaign is not active (only `us-outbound start --live` activates one), is missing, or
     cannot be read has no slots, so no lead is added for them; a dry run counts
     them and says so (enrol/capacity.campaigns_not_sending; summary campaigns_not_sending).
  3. Candidates: verified accounts in Priority, Standard or Control whose domain is not
     suppressed or a partner, whose industry is on, and with one sendable contact: a verified
     email, not suppressed, located in a known state other than CA or WA, not a personal
     domain or shared inbox, not enrolled before. Of several, the best-ranked one, as
     pick_contacts ranks them (Harry, 1 Oct 2026; clean/people.rank_person). A kill rule may
     hold back an industry group or an email source (base/holds.py). This is one check (eligible).
  4. explore_share of today's number from the random arm pick_contacts drew (accounts.arm = random; Harry's D7,
     in place of the Control tier's share), the rest in queue order (queue.order_key); while explore_share is 0
     (the default, so no random arm) control_share from the Control tier first, as before W1-6 (queue.control_count),
     each account gets: its sender (kept for life; a paused sender's accounts
     wait), its Copy row (the most specific approved, QA-passed row for its industry and its
     contact's role, else its group's, else General; a running ab test's hash split takes
     half of version_a's accounts), its opener for that contact (enrol/openers.py: the angle
     setter's line for the contact's copy role, filled with the account's stored facts, or none,
     and the opener_holdout_share held out with none), its email-1 subject arm (render.subject_arm: the
     email1_subject_share get General email1_subject, the rest the Copy row's s1_subject; Harry, 5 Oct 2026),
     a running variant test's arm (enrol/variants.py; Harry, 7 Oct 2026: one part of one email changed for every
     account, whatever its Copy row; an account where the change cannot be made is left out of the test),
     its four rendered emails (any copy-rule violation skips it), and a HubSpot
     re-check (a customer, another owner, an open deal or an opted-out contact excludes it).
  5. Each owner's leads are bulk-added to "US Outbound – {owner}" with the
     rendered steps as custom variables. A lead the add summary leaves out is looked up in the
     campaign (campaign_lead_ids): there, it is recorded; not there, Instantly refused it (its
     blocklist, or a lead in another campaign), so its contact is marked suppressed and
     pick_contacts finds the next person (mark_not_added) instead of the same one failing daily.
     Unless Instantly's plan is full (enrol/plan.py; Harry, 7 Oct 2026): then a lead left out is
     skipped as "Instantly plan limit" with its contact kept, no further owner's leads are added
     that run, and the approvers are asked, once a day, to make room.
     Send approvals (a card and an approver's ✅ before each lead was added; General auto_send) were removed with
     Harry's D6 (W2-1, 20 Oct 2026): the live sheet had auto_send = yes, which this is. Second contacts went too
     (enrol/second.py, General second_contact): one contact per company, contacts.contact_slot 1.

Dry-run: all of it runs, the guard refuses the Instantly write, and nothing is marked
enrolled. HubSpot exclusions found on the way are still written to the database (SPEC 0.3).
Live (phase 2, after Harry signs off): accounts become enrolled with their sender, and each
contact records when and in which month it was enrolled, its angle, copy version, test and arm, mailbox,
campaign and lead id, its opener arm and source (opener, holdout or none; which line), so the
readout can compare opener against none, and its subject arm (personal or copy), so it can compare
email 1's personal subject against the Copy row's; and (W1-6) its explore arm and A/A sham arm, its industry,
group and size band as they were, its copy level, email 1's subject and a hash of each email (enrol_stamps).
A HubSpot 401 or 403 ends the run with an error; per-candidate HubSpot failures and copy blocks are counted in the
summary's failures; an add Instantly never answered is looked up in the campaign before anything is concluded.
Sent events come later, from sync_outcomes.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from us_outbound.base import holds
from us_outbound.ops.health import enrolment_paused
from us_outbound.clean.domains import is_generic_mailbox, is_personal_domain, is_public_body
from us_outbound.clean.people import company_size, rank_person, state_code
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError, AuthError
from us_outbound.context import UK, Context
from us_outbound import budget, config_version, labels, parse
from us_outbound.clients import instantly as instantly_client
from us_outbound.enrol import capacity, focus, openers, plan, queue, render, variants
from us_outbound.logs import hash_email, log
from us_outbound.scoring.angle import legal_overlay
from us_outbound.scoring.score import MATCH_FACT, SCORING_SOURCE
from us_outbound.settings.model import (AB_TEST, ESCALATION_HOURS, GENERAL_COPY, SIZE_BANDS, CopyRow, CopyStep,
                                        Mailbox, Settings)
from us_outbound.settings.overrides import effective
from us_outbound.timeparse import utc


JOB = "enrol"
ENROLLED = "enrolled"
EXCLUDED = "Excluded"
SENDABLE_EMAIL_STATUSES = frozenset({"verified", "valid", "catch_all_valid"})  # Apollo verified (SPEC 8)
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
# Per-candidate skips that are failures of the run, not facts about the account: counted in the summary's failures
# ({reason: count}, beside candidates; the W1-1 contract, ops/health.py), so a day where every candidate fell to
# one of them raises a job-errors finding instead of reading as an ok run that enrolled nobody (W1-6).
HUBSPOT_FAILED, COPY_BLOCKED = "HubSpot check failed", "copy blocked"
FAILURE_REASONS = (HUBSPOT_FAILED, COPY_BLOCKED)
# A lead add whose answer never came (a timeout, a dropped connection: an OSError, as http.py raises a POST write
# that may have happened as it came): the campaign is read ADD_LOOKUP_WAITS[0] seconds later and, if none of the
# owner's leads is there yet, again ADD_LOOKUP_WAITS[1] seconds after that (RS §2 W1-6). Leads found are recorded; the
# rest are skipped with ADD_UNKNOWN, a new account keeping this owner as its sender, and tried again by the next run
# in the same campaign, whose add Instantly takes or leaves out as a lead already there (then found by
# campaign_lead_ids and recorded).
ADD_LOOKUP_WAITS = (30, 60)
ADD_UNKNOWN = "add outcome unknown: the next run tries again"
OPTOUT_UNTESTED = ("optout_tested is no: the seed-inbox test of Instantly's unsubscribe link is not done, so nothing "
                   "is sent; once it is, set optout_tested = yes on the General tab")


# -- small helpers ----------------------------------------------------------------------


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
    stop = enrolment_paused(ctx.store)
    if stop is None:
        return None
    return f"enrollment is stopped by an operator (stop at {stop.get('started_at')}); run `us-outbound start --live` to resume"


def reply_pause(ctx: Context) -> str | None:
    """SPEC 11: while any positive reply has waited more than ESCALATION_HOURS (24), new enrollment pauses.

    The reply items are poll_replies' (kind "reply"; replies/desk.py reads them, and the first kind
    name too): positive or referral, not yet handled, escalated included.
    """
    from us_outbound.replies.desk import positive_waiting

    hours = ESCALATION_HOURS
    old = positive_waiting(ctx.store, ctx.now, hours)
    if not old:
        return None
    return f"{len(old)} positive {'reply has' if len(old) == 1 else 'replies have'} waited over {hours} hours for approval"


def _item_week(item: Mapping[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    if payload.get("iso_week"):
        return str(payload["iso_week"])
    t = utc(item.get("created_at"))
    return iso_week(t.astimezone(UK).date()) if t else ""


HAND_CHECK_SKIP = "held or pulled at this week's hand-check"  # account_block's reason for hand_check()'s accounts
# account_block's reason for a verified account whose size the General range no longer takes (size_in_range), and the
# Overrides fields it reads.
SIZE_SKIP = "size outside the General range"
SIZE_FIELDS = ("employees", "size_band")


def hand_check(ctx: Context, today: date) -> tuple[str | None, frozenset[str]]:
    """(why enrollment waits, or None; the account ids it leaves out). SPEC 11 weekly hand-check; Harry's D5.

    The accounts left out are those listed in any of this ISO week's hand_check items' payload.pulled_account_ids,
    whatever the item's status (a pull with `handcheck approve --pull` only ever narrows: 9 Oct 2026; a leave out on
    the Hand-check tab is taken off again when its row's decision changes, hand_check.pull), and, while General
    hand_check_gate is no, the sampled companies still waiting for a judgement (enrol/hand_check.held: drawn under 7
    days ago, none applied yet). With hand_check_gate = no nothing else waits: the hand-check is a measure, not a gate
    (D5; on 15 Oct the whole-day gate cost about 21 leads, code-approvals-handcheck-copy B6).

    hand_check_gate = yes (the default until the sheet says no) keeps SPEC 11 as built: this week's hand_check items
    (payload.iso_week, else created_at) must all be handled, and a week with none has not been posted. A week with
    nothing to check has its item, recorded handled by the draw (hand_check.run), so it never waits."""
    from us_outbound.enrol import hand_check as weekly

    week = iso_week(today)
    items = [r for r in ctx.store.select("hitl_items", {"kind": weekly.KIND}) if _item_week(r) == week]
    pulled = frozenset(
        str(x)
        for r in items
        if isinstance(r.get("payload"), Mapping)
        for x in (r["payload"].get("pulled_account_ids") or ())
    )
    if not ctx.settings.general.hand_check_gate:
        return None, pulled | weekly.held(ctx)
    if not items:
        return f"this week's hand-check ({week}) has not been posted", pulled
    if any(r.get("status") != "handled" for r in items):
        return f"this week's hand-check ({week}) is not approved yet", pulled
    return None, pulled


def optout_untested(ctx: Context) -> str | None:
    """Live only: why nothing may be sent while the seed-inbox test of Instantly's unsubscribe link is not
    done (General optout_tested; ops/seed.py). A dry run still previews."""
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


def _in_force(ctx: Context, rows: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    """(domains, email hashes) of these suppression rows that are in force now. A row with an email hash
    suppresses only that email; its domain is only recorded (suppression.py). A domain row has no email hash."""
    domains: set[str] = set()
    hashes: set[str] = set()
    for r in rows:
        expires = utc(r.get("expires_at"))
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
    and the rows for each hash. account_stop and the accounts view read this, not the whole table."""
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
    """What the account and contact checks read, loaded once: the enrol run's candidates, pick_contacts and the
    accounts view go through the same checks (account_reason, eligible)."""

    settings: Settings
    domains: Collection[str]  # suppressed domains, aliases' roots included
    hashes: Collection[str]  # suppressed email hashes
    partners: Collection[str]
    pulled: frozenset[str] = frozenset()  # accounts held or pulled at this week's hand-check (hand_check())
    stopped: Collection[str] = frozenset()  # industry groups a kill rule stopped, casefolded (base/holds.py)
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
        return HAND_CHECK_SKIP
    ind = settings.industry(str(account.get("industry") or ""))
    if ind is not None and not ind.active:
        return "industry switched off"
    if not size_in_range(account, settings):
        return SIZE_SKIP
    return None


def size_in_range(account: Mapping[str, Any], settings: Settings) -> bool:
    """Whether the account's size (its Overrides rows applied, as verify judged it) is in the General range today: its
    employee count when known, else its size band; True when neither is known (verify left out any account with
    neither, so this gate adds nothing there).

    verify judges size once, when an account is checked; this keeps a narrower range (General max_employees 350 ->
    249, Harry's D1) from leaving accounts verified at the old range in the queue: pick, enrol, the contactable supply,
    and the hand-check pool all go through account_block (whole-wave review, 20 Oct 2026)."""
    eff = effective(account, settings, fields=SIZE_FIELDS)
    n = parse.number(eff.get("employees"))
    if n is not None:
        return settings.size_in_range(n)
    band = str(eff.get("size_band") or "")
    return band not in SIZE_BANDS or settings.band_in_range(band)


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


def account_reason(account: Mapping[str, Any], g: Gates) -> str | None:
    """Why the account may not be emailed now, or None: account_block, or a kill rule that stopped its industry group
    (SPEC 12)."""
    why = account_block(account, g.settings, g.domains, g.partners, g.pulled)
    if why is None and g.settings.industry_group_of(account).casefold() in g.stopped:
        why = "industry group stopped by a kill rule"
    return why


def eligible(account: Mapping[str, Any], contacts: Sequence[Mapping[str, Any]], g: Gates) -> tuple[dict | None, str]:
    """(the contact to email at the account, or None; why not): the one eligibility check. The account's checks
    (account_reason), then its best-ranked sendable contact (pick_contact), leaving out an email source a kill
    rule paused. The enrol run's candidates call it with every contact on file."""
    why = account_reason(account, g)
    if why is not None:
        return None, why
    usable = [c for c in contacts if _lower(c.get("email_source")) not in g.sources]
    contact, why = pick_contact(usable, g.domains, g.hashes, account, g.settings)
    if contact is None and contacts and not usable:
        why = "email source paused by a kill rule"
    return contact, why


def candidates(ctx: Context, pulled: frozenset[str]) -> tuple[list[Candidate], Counter[str]]:
    """Every account that could be enrolled today, with its contact; and why the others cannot."""
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
        contact, why = eligible(a, contacts.get(a["account_id"], []), g)
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


def copy_possible(account: Mapping[str, Any], settings: Settings, rows: Mapping[str, CopyRow]) -> bool:
    """Whether some sendable Copy row could serve the account, for a contact of some role: a row for one of the
    industries copy_targets allows it (its label, its group or General, as its copy level says). pick_contacts reads
    it before it spends a credit on an account enrol would skip as "no approved copy" (W1-6)."""
    allowed = {industry.casefold() for industry, _ in copy_targets(account, "", settings)}
    return any(r.industry.casefold() in allowed for r in rows.values())


def running_test_counts(ctx: Context) -> Counter[str]:
    """Accounts already in the running copy test, per arm ("a" or "b"). A contact enrolled in an ab test before
    test_arm existed (8 Oct 2026) is placed by its copy version."""
    t = ctx.settings.running_test()
    if t is None:
        return Counter()
    seen: dict[str, set[str]] = defaultdict(set)
    by_version = {t.version_a: "a", t.version_b: "b"} if t.kind == AB_TEST else {}
    for c in ctx.store.select("contacts", {"test_id": t.test_id}):
        arm = str(c.get("test_arm") or "") or by_version.get(str(c.get("copy_version") or ""), "")
        if arm:
            seen[arm].add(str(c.get("account_id")))
    return Counter({arm: len(ids) for arm, ids in seen.items()})


def choose_copy(
    account: Mapping[str, Any], role: str, settings: Settings, counts: Mapping[str, int], rows: Mapping[str, CopyRow]
) -> tuple[CopyRow | None, str, str, str, str]:
    """(Copy row, test_id or "", its arm "a" or "b" or "", why there is none, fallback note).

    The account gets the most specific sendable row for its industry and its contact's role
    (copy_targets). A running ab test takes accounts that would get its version_a, other than
    Control, and sends some of them (by account hash; share_a stay on version_a) version_b instead, while
    each arm has fewer than its cap (accounts_per_version, scaled to its share: Test.cap). (A variant test
    changes the row's emails instead: variants.choose.)
    """
    row, note = pick_copy(account, role, settings, rows)
    if row is None:
        label = str(account.get("industry") or settings.industry_group_of(account) or "its industry")
        level = labels.copy_level(account)
        where = {labels.LABEL_COPY: f"{label}, its group or General",
                 labels.GROUP_COPY: f"{label}'s group or General (its label earns the group's copy)",
                 labels.GENERAL_COPY_LEVEL: f"General (the label check left {label} in doubt)"}[level]
        return None, "", "", f"no approved copy that has passed QA for {where}", note
    t = settings.running_test()
    if t and t.kind == AB_TEST and account.get("tier") != queue.CONTROL and row.copy_version == t.version_a:
        arm = queue.test_version(str(account["account_id"]), t.test_id, t.share_a)
        chosen = rows.get(t.arm_name(arm))
        if chosen is not None and (t.accounts_per_version <= 0 or counts.get(arm, 0) < t.cap(arm)):
            return chosen, t.test_id, arm, "", note
    return row, "", "", "", note


# -- opener -------------------------------------------------------------------------------------


def account_opener(
    ctx: Context, account: Mapping[str, Any], contact: Mapping[str, Any], check: Callable[[str], str] | None = None,
) -> tuple[openers.Opener, str]:
    """(opener, legal_overlay) for the account and its contact: the tokenized opener (enrol/openers.py).

    The opener is worked out at enrol time, when the contact and their copy role are known: the
    angle setter's line for the role, filled with the account's stored facts, else the signal's
    plain opener; a deterministic share of accounts is held out with none. The General angle
    (Control among it) has no signal line. An account with no line by then gets the generic line for the contact's role, then the plain
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

    fact is one scoring/tiers.py reads as a hard exclusion: a hubspot one (declined_in_slack, an approver's 🚫 at a
    send approval before they were removed, stays one too).
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
    test_id: str  # the running copy test the contact is in, else ""
    lead: dict
    opener_note: str = ""  # the opener lines passed over, and why (enrol/openers.py)
    copy_note: str = ""  # a more specific Copy row exists but cannot be sent yet
    opener_arm: str = openers.NONE  # opener, holdout or none: contacts.opener_arm, for the readout
    opener_source: str = ""  # the line's signal and column, or the generic line's General key
    subject_arm: str = render.COPY_SUBJECT  # personal or copy: email 1's subject (contacts.subject_arm)
    # The four emails as rendered, the variables they were filled with, and the mailbox they were rendered for.
    rendered: list[render.Rendered] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    render_mailbox: str = ""
    # What the lead was rendered under (config_version.py; Harry, 7 Oct 2026): the run's config version and code,
    # and the Copy row's wording.
    config_version: str = ""
    code_sha: str = ""
    copy_hash: str = ""
    # The label check (labels.py; Harry, 7 Oct 2026): its latest verdict ({} when none) and the copy level its label
    # earns (enrol_stamps).
    label_check: dict = field(default_factory=dict)
    copy_level: str = ""
    # The running copy test (Harry, 7 Oct 2026; enrol/variants.py): the contact's arm ("a" or "b", with test_id), or
    # why it is not in the test; and the emails as written in place of the Copy row's own (a variant's change).
    test_arm: str = ""
    test_note: str = ""
    written: dict[int, CopyStep] = field(default_factory=dict)
    # The arm it was enrolled in (W1-6; contacts.arm): random when it was walked in the random arm's lane, ranked
    # otherwise; set by run() once the walks are done (enrol_stamps).
    arm: str = ""


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

    row, test_id, test_arm, why, copy_note = choose_copy(a, str(c.get("role") or ""), s, counts, rows)
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
    arm = render.subject_arm(a.get("account_id"), s)  # Harry, 5 Oct 2026: email 1's personal subject, as a split
    first_subject = render.email1_subject(arm, s)

    def email_problems(n: int, written: CopyStep) -> list[str]:
        return list(render.render_step(row, values, step=n, mailbox=mb, settings=s, for_send=False, written=written,
                                       subject=first_subject if n == render.SUBJECT_STEP else "").violations)

    # A variant test (Harry, 7 Oct 2026): the account's arm, made in its Copy row's email, or why it is not in the test.
    variant = variants.choose(s.running_test(), a, row, today=ctx.today_uk(), subject_arm=arm, counts=counts,
                              check=email_problems)
    if variant is not None and variant.arm:
        test_id, test_arm = variant.test_id, variant.arm
    written = dict(variant.written) if variant is not None else {}
    rendered = render.render_sequence(row, values, mailbox=mb, settings=s, subject_arm=arm, written=written)
    problems = render.violations(rendered)
    if problems:
        return Skip(COPY_BLOCKED, [f"{row.copy_version}: {p}" for p in problems])

    try:
        block = hubspot_block(ctx, a, c)
    except AuthError:
        # A 401 or 403 fails every account the same way: the run ends with an error heartbeat rather than a day of
        # "HubSpot check failed" skips that reads as ok (code-contacts-enrol M2: enrol.py 718-721 before W1-6).
        # Nothing has been added to Instantly yet: prepare runs before the adds.
        raise
    except ApiError as exc:
        return Skip(HUBSPOT_FAILED, [str(exc)[:200]])
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
        subject_arm=arm, rendered=rendered, values=values, render_mailbox=mb.address, copy_hash=row.content_hash(), label_check=verdict or {}, copy_level=labels.copy_level(a),
        test_arm=test_arm, test_note=variant.note if variant is not None else "",
        written=written,
    )


# -- the job --------------------------------------------------------------------------------------


@dataclass
class _Run:
    skipped: Counter[str] = field(default_factory=Counter)
    skipped_accounts: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    opener_fallbacks: list[dict] = field(default_factory=list)
    opener_arms: Counter[str] = field(default_factory=Counter)  # opener, holdout, none
    opener_sources: Counter[str] = field(default_factory=Counter)  # "<signal> / <column>", "opener_generic_ops"
    subject_arms: Counter[str] = field(default_factory=Counter)  # personal, copy
    test_arms: Counter[str] = field(default_factory=Counter)  # the running copy test's arms: a, b
    not_in_test: Counter[str] = field(default_factory=Counter)  # why a contact is not in the running variant test
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
            run.skip(cand.account, p.reason, p.detail)
            if p.exclude_fact:
                mark_excluded(ctx, cand.account, p.exclude_fact, p.detail[0])
                run.excluded.append({"account_id": cand.account["account_id"], "reason": p.detail[0]})
            continue
        out.append(p)
        free[p.owner] -= 1
        if quota is not None:
            quota.take(cand.account)
        if p.test_id:  # the test counts accounts
            counts[p.test_arm] += 1
        if p.test_arm:
            run.test_arms[p.test_arm] += 1
        elif p.test_note:
            run.not_in_test[p.test_note] += 1
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


def _sleep(seconds: float) -> None:
    """The waits before an unanswered add is looked up (ADD_LOOKUP_WAITS); tests replace it."""
    time.sleep(seconds)


def _unanswered_add(ctx: Context, campaign: str, items: Sequence[Prepared], leads: Sequence[Mapping[str, Any]],
                    exc: BaseException, run: _Run) -> dict[int, str]:
    """Lead position -> Instantly lead id, for the leads of an add that got no answer (a ReadTimeout on /leads/add)
    that are in the campaign all the same; the others are skipped as ADD_UNKNOWN, their contacts kept.

    Before W1-6 the OSError escaped run() after the add may have happened, so no lead of that run was recorded and the
    other owners' adds never ran (code-contacts-enrol M3). Now: wait, read the campaign; if none of the leads is
    there yet, wait longer and read it again (Instantly can take a few seconds to list a bulk add); record what is
    there; one errors line for the owner; and the next owner's add goes ahead. A lead not found is not marked
    refused: the next run adds it again.

    Not being listed after 90 s does not prove the add failed, so a new account skipped here is given this owner as
    its sender now, as its first enrolment would have (SPEC 9), not left without one. Otherwise the next run's
    assign_sender could give it to another owner (it goes by the share of pace left), whose add Instantly leaves out
    because the lead is in this owner's campaign (skip_if_in_workspace); campaign_lead_ids would then look in the
    wrong campaign, not find it, and mark the contact not added: the account would stay verified, a colleague be
    emailed from the second campaign, and the first lead run its sequence unrecorded. With the sender kept, the retry
    goes to this campaign, where Instantly either takes the lead or leaves it out as one there already, which
    campaign_lead_ids then finds and records."""
    emails = [_lower(lead["email"]) for lead in leads]
    found: dict[str, str] = {}
    lookups, failed = 0, ""
    for wait in ADD_LOOKUP_WAITS:
        _sleep(wait)
        lookups += 1
        try:
            found = campaign_lead_ids(ctx, campaign, emails)
        except (ApiError, LookupError, OSError) as err:
            failed = f"; the campaign could not be read ({type(err).__name__}: {str(err)[:120]})"
            continue
        if found:
            break
    ids = {i: found[e] for i, e in enumerate(emails) if e in found}
    pinned = []
    for i, p in enumerate(items):
        if i not in ids:
            run.skip(p.account, ADD_UNKNOWN, [f"{campaign}: Instantly did not answer the add ({type(exc).__name__}), "
                                              f"and the lead was not in the campaign after {lookups} lookups"])
            if not str(p.account.get("sender") or "").strip():
                pinned.append({"account_id": p.account["account_id"], "sender": p.owner})
    if pinned:  # the retry goes to this campaign, where a lead that did land is found (the docstring)
        ctx.store.upsert("accounts", pinned)
    run.errors.append(f"{campaign}: Instantly did not answer the add ({type(exc).__name__}: {str(exc)[:120]}); "
                      f"{len(ids)} of {len(leads)} leads found in the campaign after {lookups} lookups and recorded, "
                      f"the rest tried again next run{failed}")
    log("enrol_add_unanswered", campaign=campaign, leads=len(leads), found=len(ids), lookups=lookups)
    return ids


def _resume(ctx: Context, campaign: str, leads: int, run: _Run) -> None:
    """After leads went into the campaign: activate it again if Instantly has marked it completed
    (capacity.resume_if_completed), or say in errors that the leads wait unsent until someone does. An OSError (no
    answer from the campaign list) is caught too: the leads are in by now, and must still be recorded."""
    try:
        if capacity.resume_if_completed(ctx, campaign):
            log("campaign_resumed", campaign=campaign, leads=leads)
    except (ApiError, LookupError, OSError) as exc:
        run.errors.append(f"{campaign} may be completed in Instantly and could not be checked or resumed "
                          f"({str(exc)[:160]}): run `us-outbound start --live`, or the leads just added wait unsent")


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


def email_hashes(custom_variables: Mapping[str, Any]) -> list[str] | None:
    """sha256 of each step's rendered body (s1_body, s2_body, ...: the lead's custom variables, in the General tab's
    email_format, HTML as sent), in step order; None when there are none. What a contact was sent can then be told
    apart, and two contacts' emails compared, without keeping the text (contacts.email_hashes)."""
    steps = sorted(int(k[1:-5]) for k in custom_variables if k.startswith("s") and k.endswith("_body")
                   and k[1:-5].isdigit())
    if not steps:
        return None
    return [hashlib.sha256(str(custom_variables[f"s{n}_body"]).encode("utf-8")).hexdigest() for n in steps]


def enrol_stamps(settings: Settings, p: Prepared) -> dict[str, Any]:
    """What the contact was enrolled as, kept on the contact so later relabels, resizes and copy changes cannot move
    it between the readout's cells (W1-6; code-contacts-enrol L6): the arm it was walked in (Prepared.arm, from
    accounts.arm as drawn at pick_contacts; the account's arm, else ranked, when unset), the A/A sham arm
    (queue.sham_arm), the industry label, group and size band as they were, the copy level its emails were rendered
    at, email 1's subject and each step's body hash.

    From the Prepared and the lead as it goes to Instantly (its custom variables)."""
    a = p.account
    variables = p.lead.get("custom_variables") if isinstance(p.lead.get("custom_variables"), Mapping) else {}
    return {
        # The arm it was walked in (run() sets p.arm), not accounts.arm alone: an account pick drew at random while
        # explore_share was above 0 is walked in queue order once the share is back at 0, and is ranked then.
        "arm": p.arm or str(a.get("arm") or "") or queue.RANKED,
        "sham_arm": queue.sham_arm(a.get("account_id")),
        "industry_at_enrol": str(a.get("industry") or "") or None,
        "industry_group_at_enrol": settings.industry_group_of(a) or None,
        "size_band_at_enrol": str(a.get("size_band") or "") or None,
        "copy_level": p.copy_level or labels.copy_level(a),
        "email1_subject": str(variables.get(f"s{render.SUBJECT_STEP}_subject") or "") or None,
        "email_hashes": email_hashes(variables),
    }


def _record_enrolled(ctx: Context, items: Sequence[Prepared], ids: Mapping[int, str], campaign: str, month: str) -> None:
    """Mark the accounts enrolled and give each contact its lead, month and enrolled_at (for the send forecast),
    its opener and subject arms (the readout's two splits), and what the account looked like then: its signals,
    score and tier (signals_now); where the contact's details came from and the lawful basis
    (render.data_record), kept here since no email carries it; what its emails were rendered under, its
    config version, code and Copy wording (config_version.py; Harry, 7 Oct 2026), so the cohort report compares
    like with like; and its arms and what it was enrolled as (enrol_stamps)."""
    accounts, contacts = [], []
    record = render.data_record(ctx.settings)
    signals = signals_now(ctx, [str(p.account["account_id"]) for i, p in enumerate(items) if i in ids])
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
            "test_arm": (p.test_arm or None) if p.test_id else None,  # Harry, 7 Oct 2026: a or b (enrol/variants.py)
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
            "contact_slot": 1,  # one contact per company (second contacts were removed: W2-1)
            # The cohort stamp (Harry, 7 Oct 2026).
            "config_version": p.config_version or None,
            "code_sha": p.code_sha or None,
            "copy_hash": p.copy_hash or None,
            **enrol_stamps(ctx.settings, p),
        })
    if accounts:
        ctx.store.upsert("accounts", accounts)
    if contacts:
        ctx.store.upsert("contacts", contacts)


def _failures(skipped: Mapping[str, int]) -> dict[str, int]:
    """The summary's failures: {reason: count} for the skips that are failures of the run (FAILURE_REASONS), so
    heartbeat_check sees them."""
    out = {why: skipped.get(why, 0) for why in FAILURE_REASONS}
    return {why: k for why, k in out.items() if k}


def _copy_test_summary(s: Settings, r: _Run) -> dict[str, Any] | None:
    """The enrol summary's copy_test: the running copy test, today's contacts in each arm, and why others were not in
    it (a variant whose change could not be made, or whose arm is full)."""
    t = s.running_test()
    if t is None:
        return None
    return {"test_id": t.test_id, "kind": t.kind, "arms": {t.arm_name(a): r.test_arms[a] for a in ("a", "b")},
            "not_in_test": dict(r.not_in_test.most_common(LIST_LIMIT // 10))}


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
    # What today's leads are rendered under (Harry, 7 Oct 2026): kept once per version, and stamped on each contact.
    cv = config_version.current(ctx)
    config_version.record(ctx, cv)
    summary["config_version"] = cv.id

    # An owner whose Instantly campaign is not active gets no capacity in a live run (limits.py). enrol/today.py is
    # how the daily post, status and the accounts view count the same.
    from us_outbound.enrol.today import read as read_today

    t = read_today(ctx, today, pulled=pulled)
    cands, skipped, lim = t.ready, t.skipped, t.limits
    stopped = lim.not_sending
    n, terms = lim.number, lim.terms
    free = Counter({owner: c.free for owner, c in lim.senders.items()})
    pace = {owner: c.pace for owner, c in lim.senders.items()}
    r = _Run(skipped=skipped)

    # explore_share from the random arm pick_contacts drew (accounts.arm = random; Harry's D7), the rest in queue
    # order, and a shortfall in either (too few, or skipped on the way) filled from the other (queue.py docstring).
    # The random walk ignores tier and the Focus quota's allows(): pick drew it within the Focus shares already, so
    # holding it to today's quota would bend the arm towards whichever group the ranked accounts left room in. Its
    # accounts still count against the quota, so the day's groups keep their shares. With explore_share 0 (the
    # default) there is no random walk: the Control tier keeps its control_share first, as before W1-6, and the rest
    # are walked in queue order, those pick drew while the share was above 0 included, so a sheet without the row is
    # today's walk exactly (the plan's W1-6 acceptance; the whole-wave review, 20 Oct 2026); their contacts are
    # stamped ranked. The Control lane goes through the quota, as it did.
    in_order = sorted(cands, key=lambda c: queue.order_key(c.account, s))
    exploring = s.general.explore_share > 0

    def in_random_arm(account: Mapping[str, Any]) -> bool:
        return exploring and account.get("arm") == queue.RANDOM

    def in_first_lane(account: Mapping[str, Any]) -> bool:
        return in_random_arm(account) if exploring else account.get("tier") == queue.CONTROL

    drawn = [c for c in in_order if in_random_arm(c.account)]
    first = [c for c in in_order if in_first_lane(c.account)]
    first_lane = iter(first)
    ranked_lane = iter([c for c in in_order if not in_first_lane(c.account)])
    n_random = queue.explore_count(n, s, len(drawn))
    n_lead = n_random if exploring else queue.control_count(n, s, len(first))
    counts, approved = running_test_counts(ctx), sendable_copy(s)
    # Industry focus (enrol/focus.py): each share first; then, if a share had too few ready
    # accounts, the rest of the day in queue order whatever the group.
    quota = focus.today(ctx, lim.terms["send_days_left_in_week"])
    held_ranked: list[Candidate] = []
    held_first: list[Candidate] = []  # the first lane's: the random arm's, or (explore_share 0) the Control tier's
    if exploring:
        prepared = _walk(ctx, first_lane, n_lead, free, counts, approved, r, pace, not_sending=stopped)
        for p in prepared:
            quota.take(p.account)
    else:
        prepared = _walk(ctx, first_lane, n_lead, free, counts, approved, r, pace, quota, held_first, stopped)
    prepared += _walk(ctx, ranked_lane, n - len(prepared), free, counts, approved, r, pace, quota, held_ranked, stopped)
    prepared += _walk(ctx, first_lane, n - len(prepared), free, counts, approved, r, pace, quota, held_first,
                      stopped)
    for held in (held_ranked, held_first):
        fill = _walk(ctx, iter(held), n - len(prepared), free, counts, approved, r, pace, not_sending=stopped)
        for p in fill:
            quota.take(p.account)
        prepared += fill
    n_first = len(prepared)
    n_random_prepared = sum(in_random_arm(p.account) for p in prepared)
    for p in prepared:
        p.config_version, p.code_sha = cv.id, cv.code_sha
        # The arm each contact is stamped with is the lane its account was walked in, so with explore_share at 0
        # every contact is ranked, an account drawn at random earlier included.
        p.arm = queue.RANDOM if in_random_arm(p.account) else queue.RANKED

    by_owner: dict[str, list[Prepared]] = defaultdict(list)
    for p in prepared:
        by_owner[p.owner].append(p)
    enrolled: Counter[str] = Counter()
    would: Counter[str] = Counter()
    month = today.strftime("%Y-%m")
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
        except OSError as exc:  # no answer: the add may have happened (http.py raises it as it came)
            ids = _unanswered_add(ctx, campaign, items, leads, exc, r)
            if ids:  # leads that are in a completed campaign wait unsent until it is activated again
                _resume(ctx, campaign, len(ids), r)
            _record_enrolled(ctx, items, ids, campaign, month)
            enrolled[owner] = len(ids)
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
        _resume(ctx, campaign, len(items), r)
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
        failures=_failures(r.skipped),
        # realised: the random arm's share of today's first contacts, which falls short of the share when pick kept
        # too few random-arm contacts (drawn_ready below target), so a short arm is seen, not only filled from ranked.
        explore={"share": s.general.explore_share, "drawn_ready": len(drawn), "target": n_random,
                 "prepared": n_random_prepared,
                 "realised": round(n_random_prepared / n_first, 3) if exploring and n_first else None},
        # The Control tier's places while explore_share is 0 (control_count); None once the random arm has them.
        control=None if exploring else {
            "share": s.general.control_share, "ready": len(first), "target": n_lead,
            "prepared": sum(p.account.get("tier") == queue.CONTROL for p in prepared[:n_first])},
        prepared=len(prepared),
        enrolled=sum(enrolled.values()),
        by_owner=dict(enrolled) if ctx.live else dict(would),
        skipped=dict(r.skipped),
        skipped_accounts=r.skipped_accounts,
        excluded=r.excluded,
        opener_fallbacks=r.opener_fallbacks,
        openers={"arms": dict(r.opener_arms), "sources": dict(r.opener_sources)},
        subjects={"arms": dict(r.subject_arms)},
        copy_test=_copy_test_summary(s, r),
        copy_fallbacks=dict(r.copy_fallbacks),
        copy_sendable=len(approved),
        instantly_plan=plan_room,
        errors=r.errors,
    )
    log("enrol_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "skipped_accounts"})
    return summary
