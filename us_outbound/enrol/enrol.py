"""The enrol job: today's accounts into their senders' Instantly campaigns.

SPEC 9 ("enrol", "Daily enrolment number", "Test assignment", "Instantly campaigns",
"Sender continuity"), SPEC 10 (copy), SPEC 11 ("Enrolment pause", weekly hand-check),
SPEC 1.5 (recipients). Runs at 12:00 UK (07:00 ET) on weekdays.

  1. Gates: not a blackout date or a non-send day; no positive reply waiting longer than
     escalation_hours (SPEC 11); this week's hand-check approved (SPEC 11).
  2. Today's number (queue.daily_number): Active mailbox caps, this month's Clay and Apollo
     spend from credit_ledger against the General budgets, working days left, the queue.
  3. Candidates: verified accounts in Priority, Standard or Control whose domain is not
     suppressed or a partner, whose industry is on, with one sendable contact: a verified
     email, not suppressed, located in a known state other than CA or WA, not a personal
     domain or shared inbox, not enrolled before.
  4. In queue order (queue.order_key), control_share from Control and the rest from Priority
     then Standard, each account gets: its sender (kept for life; a paused sender's accounts
     wait), its copy version (the running test's hash split, else the approved version for
     its angle), its four rendered steps (any copy-rule violation skips it), and a HubSpot
     re-check (a customer, another owner, an open deal or an opted-out contact excludes it).
  5. Each owner's leads are bulk-added to "US Outbound – {owner}" with the rendered steps as
     custom variables.

Dry-run: all of it runs, the guard refuses the Instantly write, and nothing is marked
enrolled. HubSpot exclusions found on the way are still written to BigQuery (SPEC 0.3).
Live (phase 2, after Harry signs off): accounts become enrolled with their sender, and each
contact records its enrollment month, angle, copy version, test, mailbox, campaign and lead id.
Sent events come later, from sync_outcomes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clean.domains import is_generic_mailbox, is_personal_domain
from us_outbound.clean.people import state_code
from us_outbound.clients.db import Store, new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import ET, UK, Context
from us_outbound.enrol import queue, render
from us_outbound.logs import hash_email, log
from us_outbound.scoring.score import score_account
from us_outbound.settings.model import Mailbox, Settings


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
    """Why enrollment is paused by the stop command (SPEC 13); kill-rule pauses are added in phase 3."""
    from us_outbound.ops.heartbeat import enrolment_paused

    stop = enrolment_paused(ctx.store)
    if stop is None:
        return None
    return f"enrollment is stopped by an operator (stop at {stop.get('started_at')}); run `us-outbound start --live` to resume"


def reply_pause(ctx: Context) -> str | None:
    """SPEC 11: while any positive reply has waited more than escalation_hours, new enrollment pauses."""
    hours = ctx.settings.general.escalation_hours
    cutoff = ctx.now - timedelta(hours=hours)
    rows = ctx.store.select("hitl_items", {"kind": "reply_approval", "status": list(WAITING)})
    old = [r for r in rows if (t := _ts(r.get("created_at"))) is not None and t <= cutoff]
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
    if queue.is_blackout(today, s):
        return f"{today} is a blackout date"
    if today.weekday() not in s.general.send_window.days:
        return f"{today} is not a send day"
    if ctx.live and not s.general.live_sending:
        return "live_sending is no"
    return operator_pause(ctx) or reply_pause(ctx)


# -- budgets and today's load ------------------------------------------------------------------


def month_credits(store: Store, system: str, now: datetime) -> float:
    """Credits recorded in credit_ledger this UTC calendar month (as v_credits_month counts them)."""
    now = now.astimezone(UTC)
    total = 0.0
    for r in store.select("credit_ledger", {"system": system}):
        t = _ts(r.get("occurred_at"))
        if t is not None and (t.astimezone(UTC).year, t.astimezone(UTC).month) == (now.year, now.month):
            total += float(r.get("credits") or 0)
    return total


def enrolled_today(ctx: Context, today: date) -> tuple[int, Counter[str]]:
    """Accounts already enrolled today by earlier live runs (from their heartbeats), in total and per owner."""
    total, load = 0, Counter()
    for r in ctx.store.select("heartbeats", {"job": JOB, "status": "ok", "dry_run": False}):
        t = _ts(r.get("started_at"))
        detail = r.get("detail") if isinstance(r.get("detail"), Mapping) else {}
        if t is None or t.astimezone(ET).date() != today:
            continue
        total += int(detail.get("enrolled") or 0)
        for owner, k in (detail.get("by_owner") or {}).items():
            load[owner] += int(k or 0)
    return total, load


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


def pick_contact(contacts: Iterable[Mapping[str, Any]], domains: set[str], hashes: set[str]) -> tuple[dict | None, str]:
    """The account's one contact in v1: the first sendable one by created_at; else why none is."""
    first_reason = "no contact"
    for i, c in enumerate(sorted(contacts, key=lambda c: (str(c.get("created_at") or ""), str(c.get("contact_id"))))):
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
    contacts: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks([a["account_id"] for a in accounts]):
        for c in store.select("contacts", {"account_id": list(chunk)}):
            contacts[c["account_id"]].append(c)
    out: list[Candidate] = []
    for a in accounts:
        why = account_block(a, s, domains, partners, pulled)
        contact = None
        if why is None:
            contact, why = pick_contact(contacts.get(a["account_id"], []), domains, hashes)
        if contact is None:
            skipped[why] += 1
            continue
        out.append(Candidate(a, contact))
    return out, skipped


# -- copy version (SPEC 9 "Test assignment", SPEC 12) -----------------------------------------------


def approved_versions(settings: Settings) -> list[str]:
    """Copy versions with all four steps approved, in sheet order."""
    out = []
    for version in dict.fromkeys(c.copy_version for c in settings.copy):
        rows = render.copy_rows(settings, version)
        if all(step in rows and rows[step].status == "approved" for step in render.STEPS):
            out.append(version)
    return out


def version_angle(settings: Settings, version: str) -> str:
    return next((c.angle for c in settings.copy if c.copy_version == version), "")


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
    account: Mapping[str, Any], settings: Settings, counts: Mapping[str, int], approved: Sequence[str]
) -> tuple[str | None, str, str]:
    """(copy_version, test_id or "", why there is none).

    The running test takes accounts whose angle is its version_a's angle (the first test:
    "Upgrade the EAP" accounts, split between an EAP and a General opener), other than
    Control, while each version has fewer than accounts_per_version. Everyone else gets the
    first approved version for their angle.
    """
    angle = str(account.get("angle") or "")
    t = settings.running_test()
    if t and angle and account.get("tier") != queue.CONTROL and version_angle(settings, t.version_a) == angle:
        v = t.version_a if queue.test_version(str(account["account_id"]), t.test_id) == "a" else t.version_b
        if v in approved and (t.accounts_per_version <= 0 or counts.get(v, 0) < t.accounts_per_version):
            return v, t.test_id, ""
    for v in approved:
        if version_angle(settings, v) == angle:
            return v, "", ""
    return None, "", f"no approved copy for the {angle or 'blank'} angle"


# -- opener -------------------------------------------------------------------------------------


def account_opener(ctx: Context, account: Mapping[str, Any]) -> tuple[str, str]:
    """(opener, legal_overlay) for the account's angle, worked out as scoring does (SPEC 9 steps 4-5).

    The opener is not stored on accounts, so it is recomputed from the account's facts.
    """
    angle = ctx.settings.angle(str(account.get("angle") or ""))
    default = angle.default_opener if angle else ""
    events = ctx.store.select("signal_events", {"account_id": account["account_id"]})
    r = score_account(account, events, ctx.settings, ctx.today_uk())
    opener = r.opener if r.angle == account.get("angle") and r.opener else default
    return opener, r.legal_overlay


# -- HubSpot re-check (SPEC 9 enrol "re-checks HubSpot"; hard exclusions) ---------------------------


def hubspot_block(ctx: Context, account: Mapping[str, Any], contact: Mapping[str, Any]) -> tuple[str, str] | None:
    """(fact, reason) when HubSpot now excludes the account: a customer, another owner, an open deal,
    or the contact opted out there. None when it is clear."""
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
    """Tier Excluded now, and a hubspot fact so the next rescore keeps it excluded (BigQuery, so dry-run too)."""
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
    copy_angle: str
    test_id: str
    lead: dict
    opener_note: str = ""


@dataclass
class Skip:
    reason: str  # a short category, counted in the summary
    detail: list[str] = field(default_factory=list)
    exclude_fact: str = ""  # set when HubSpot excludes the account


def _default_opener(settings: Settings, angle: str) -> str:
    a = settings.angle(angle)
    return a.default_opener if a else ""


def prepare(
    ctx: Context, cand: Candidate, load: Mapping[str, int], counts: Mapping[str, int], approved: Sequence[str]
) -> Prepared | Skip:
    s, g = ctx.settings, ctx.settings.general
    a, c = cand.account, cand.contact
    owner = queue.assign_sender(a, s, load)
    if owner is None:
        if a.get("sender"):
            return Skip("sender paused", [f"{a['sender']} has no Active mailbox; the account waits for them"])
        return Skip("no Active mailbox")
    boxes: tuple[Mailbox, ...] = s.mailboxes_for(owner, "Active")
    mb = boxes[0]

    version, test_id, why = choose_copy(a, s, counts, approved)
    if version is None:
        return Skip("no approved copy", [why])
    copy_angle = version_angle(s, version)

    opener, overlay = account_opener(ctx, a)
    if copy_angle != a.get("angle"):
        opener = _default_opener(s, copy_angle)  # a test version on another angle uses that angle's opener
    host = render.is_demo_host(mb, s)
    opener, note = render.pick_opener(
        opener, _default_opener(s, copy_angle), sender_is_harry=host, demo_host=g.demo_host,
        exempt=(str(a.get("clean_name") or ""), str(c.get("first_name") or "")),
    )
    values = render.variables(a, c, mb, s, opener=opener, legal_overlay=overlay)
    rendered = render.render_sequence(version, values, mailbox=mb, settings=s)
    problems = render.violations(rendered)
    if problems:
        return Skip("copy blocked", problems)

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
        copy_version=version, copy_angle=copy_angle, test_id=test_id, lead=lead, opener_note=note,
    )


# -- the job --------------------------------------------------------------------------------------


@dataclass
class _Run:
    skipped: Counter[str] = field(default_factory=Counter)
    skipped_accounts: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    opener_fallbacks: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def skip(self, account: Mapping[str, Any], reason: str, detail: Sequence[str] = ()) -> None:
        self.skipped[reason] += 1
        row = {"account_id": account.get("account_id"), "domain": account.get("domain"), "reason": reason,
               "detail": list(detail)[:5]}
        if len(self.skipped_accounts) < LIST_LIMIT:
            self.skipped_accounts.append(row)
        log("enrol_skip", account_id=row["account_id"], reason=reason, detail=row["detail"])


def _walk(
    ctx: Context, lane: Iterator[Candidate], target: int, load: Counter[str], counts: Counter[str],
    approved: Sequence[str], run: _Run,
) -> list[Prepared]:
    """Prepare accounts in queue order until target are ready; skipped ones make way for the next.

    lane is an iterator, so a second walk carries on where the first stopped.
    """
    out: list[Prepared] = []
    while len(out) < target:
        cand = next(lane, None)
        if cand is None:
            break
        p = prepare(ctx, cand, load, counts, approved)
        if isinstance(p, Skip):
            run.skip(cand.account, p.reason, p.detail)
            if p.exclude_fact:
                mark_excluded(ctx, cand.account, p.exclude_fact, p.detail[0])
                run.excluded.append({"account_id": cand.account["account_id"], "reason": p.detail[0]})
            continue
        out.append(p)
        load[p.owner] += 1
        if p.test_id:
            counts[p.copy_version] += 1
        if p.opener_note and len(run.opener_fallbacks) < LIST_LIMIT:
            run.opener_fallbacks.append({"account_id": cand.account["account_id"], "reason": p.opener_note})
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
            "angle": p.copy_angle,
            "copy_version": p.copy_version,
            "test_id": p.test_id or None,
            "mailbox": p.mailbox or None,
            "instantly_campaign": campaign,
            "instantly_lead_id": ids[i],
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
    done_today, load = enrolled_today(ctx, today)
    g = s.general
    n, terms = queue.daily_number(
        s,
        active_mailbox_caps=[m.daily_cap for m in s.mailboxes if m.status == "Active"],
        clay_remaining=g.clay_monthly_credits - month_credits(ctx.store, "clay", ctx.now),
        clay_per_account=g.clay_credits_per_account,
        apollo_remaining=g.apollo_monthly_credits - month_credits(ctx.store, "apollo", ctx.now),
        apollo_per_account=g.apollo_credits_per_account,
        working_days_left=queue.working_days_left(today, s),
        verified_queue_size=len(cands),
    )
    n = max(0, n - done_today)
    r = _Run(skipped=skipped)

    # control_share from Control, the rest from Priority then Standard, and a shortfall in
    # either (too few, or skipped on the way) filled from the other (SPEC 9; queue.py docstring).
    in_order = sorted(cands, key=lambda c: queue.order_key(c.account, s))
    n_control = sum(c.account.get("tier") == queue.CONTROL for c in in_order)
    control = iter([c for c in in_order if c.account.get("tier") == queue.CONTROL])
    main = iter([c for c in in_order if c.account.get("tier") != queue.CONTROL])
    counts, approved = running_test_counts(ctx), approved_versions(s)
    prepared = _walk(ctx, control, queue.control_count(n, s, n_control), load, counts, approved, r)
    prepared += _walk(ctx, main, n - len(prepared), load, counts, approved, r)
    prepared += _walk(ctx, control, n - len(prepared), load, counts, approved, r)

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
        already_enrolled_today=done_today,
        candidates=len(cands),
        prepared=len(prepared),
        enrolled=sum(enrolled.values()),
        by_owner=dict(enrolled) if ctx.live else dict(would),
        skipped=dict(r.skipped),
        skipped_accounts=r.skipped_accounts,
        excluded=r.excluded,
        opener_fallbacks=r.opener_fallbacks,
        errors=r.errors,
    )
    log("enrol_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "skipped_accounts"})
    return summary
