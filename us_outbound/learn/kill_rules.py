"""kill_rules, hourly (SPEC 9, 12 "Kill rules"): pause what is hurting deliverability, and say so in Slack.

It reads the events table, which sync_outcomes fills from Instantly: type sent (one row per
campaign email, with its mailbox, contact and step; a reply sent from the reply desk is
reply_sent, so it is never in a rate's denominator) and bounced, the names enrol/capacity.py
STOP_EVENTS uses. A send counts as bounced when a bounced event for the same contact, at the
same step or with no step, came at or after it (as v_mailbox_health counts it). Contract for
the writers (PHASE0-CONFIRM what Instantly reports):
  * a bounced row carries the bounce's SMTP status in reply_text or reply_class, so a block
    ("5.7.1 ... blocked") can be told from a full mailbox;
  * a spam complaint, if Instantly reports one, is a row of type complained;
  * a seed inbox that found an email in spam (SPEC 13, checked by hand) is a row of type
    seed_spam with the mailbox that sent it.

The rules (SPEC 12; Harry, 1 Oct 2026, docs/gtm-review/README.md §4.2 D4 and §7.6: bounce rules
from the first send, not phase 3). WINDOW_DAYS is 7; a rule looks only at events after the
last time it fired for the same mailbox, domain, source or group, so old bounces never count twice.

  rule                      fires when                                              action
  mailbox_bounce_rate       a mailbox's sends in the window (its latest 100 at      pause the mailbox
                            most): more than 3% bounced, and at least 2
  domain_bounce_rate        a domain's sends in the window, 100 or more: more       pause its mailboxes
                            than 3% bounced (SPEC 12's own rule)
  block_bounce              a 5.7.x bounce from a mailbox in the window             pause it for 14 days
  spam_complaint            a spam complaint on a mailbox's email in the window     pause it for 14 days
  seed_spam                 a seed inbox found its email in spam                    pause it for 14 days
  vitals                    Instantly reports the account in error or its warmup    pause it for 14 days
                            banned (PHASE0-CONFIRM which states Instantly uses)
  source_bounce_rate        an email source's sends in the window (Apollo verified, pause the source until
                            Clay waterfall), 100 or more: more than 3% bounced      checked
  group_reply_rate          an industry group's accounts whose step 1 was           stop enrolling it
                            delivered and whose 28-day reply window has closed, 400
                            or more: human replies under 0.5%
  stop_rule_meetings        the first stop_rule_accounts accounts' reply windows    pause enrolment (the
                            have closed with fewer than stop_rule_meetings meetings  stop rule)
  stop_rule_bounce_rate     of the accounts sent step 1 in the last 30 days (100 or pause enrolment
                            more), more than stop_rule_bounce_rate bounced
  stop_rule_complaint_rate  ... more than stop_rule_complaint_rate complained       pause enrolment

At least 2 bounces (MIN_BOUNCES) before a rate rule fires: on the ramp's 10 a day a single
bounce would be 10%, and would pause a healthy mailbox (a block bounce still pauses at once).

Each firing is a hitl_items row (kind kill_rule; base/holds.py says what it holds). A mailbox
is paused through the registry pause path (registry/mailboxes.mailbox_pause: Paused on the
Mailboxes tab and off its campaign's sending list); if the sheet cannot be written it still
comes off the sending list, and the hold keeps it out of every job either way. A rule does not
fire again while its item is open. Every bounced contact goes on suppression (reason bounce).
All firings of a run go in one Slack message to the alert channel, mentioning the approvers;
with no Slack token the message goes to the log (base/notify.py). Dry-run computes, records the
items and posts to the dev channel, but changes neither the sheet nor Instantly.

Not here: a human-in-the-loop item left for 24 hours (the reply desk escalates it; enrol already
pauses while a positive reply waits, enrol.reply_pause); the credit rules (budget.py and the
jobs that spend credits); a failed settings sync (settings_sync keeps the version in force).
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import fmt, suppression
from us_outbound.base import holds, notify, say
from us_outbound.base.holds import KIND, PAUSE_ENROLMENT, PAUSE_MAILBOX, PAUSE_SOURCE, STOP_GROUP, WAITING
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import REPLY_WINDOW_DAYS
from us_outbound.context import ConfigError, Context
from us_outbound.logs import log
from us_outbound.replies import kinds
from us_outbound.settings.model import Settings
from us_outbound.timeparse import utc

JOB = "kill_rules"
ACTIVE = "Active"
WINDOW_DAYS = 7
LAST_SENDS = 100  # a mailbox's rate is over its latest 100 sends in the window
BOUNCE_RATE = 0.03  # SPEC 12: over 3%
MIN_BOUNCES = 2
MIN_SENDS = 100  # SPEC 12: "on 100+ sends" (domain and source rules)
BLOCK_PAUSE_DAYS = 14  # SPEC 12
GROUP_REPLY_FLOOR = 0.005  # SPEC 12: under 0.5% ...
GROUP_MIN_DELIVERED = 400  # ... after 400 delivered
STOP_WINDOW_DAYS = 30
STOP_MIN_ACCOUNTS = 100
BLOCK_CODE = re.compile(r"\b5\.7\.\d{1,3}\b")
COMPLAINT, SEED_SPAM = "complained", "seed_spam"
VITALS_FAIL_ACCOUNT = frozenset({"soft_bounce_error", "sending_error"})  # instantly.ACCOUNT_STATUS
VITALS_FAIL_WARMUP = frozenset({"banned", "permanent_suspension"})  # instantly.WARMUP_STATUS
SOURCES = ("apollo", "clay")  # contacts.email_source
BOUNCE_REASON = "bounce"
LIST_LIMIT = 100


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


# -- what fired --------------------------------------------------------------------------------


@dataclass
class Firing:
    rule: str
    scope: str  # mailbox, domain, source, industry_group, enrolment
    target: str
    action: str  # holds.PAUSE_MAILBOX, PAUSE_SOURCE, STOP_GROUP, PAUSE_ENROLMENT
    reason: str
    mailboxes: list[str] = field(default_factory=list)
    until: date | None = None
    numbers: dict[str, Any] = field(default_factory=dict)
    item_id: str = ""
    outcome: str = ""  # what the pause did
    campaigns: list[str] = field(default_factory=list)  # campaigns the pause left with no Active mailbox


@dataclass
class _Send:
    event_id: str
    mailbox: str
    contact_id: str
    account_id: str
    step: Any
    at: datetime
    bounced: bool = False


class _Events:
    """The events kill_rules reads, loaded once a run."""

    def __init__(self, store, now: datetime):
        self.now = now
        rows = store.select("events", {"type": ["sent", "bounced", "replied", "meeting_booked", COMPLAINT, SEED_SPAM]})
        by_type: dict[str, list[dict]] = defaultdict(list)
        for e in rows:
            if utc(e.get("occurred_at")) is not None:
                by_type[str(e.get("type"))].append(e)
        self.bounces = by_type["bounced"]
        self.replies = by_type["replied"]
        self.meetings = by_type["meeting_booked"]
        self.complaints = by_type[COMPLAINT]
        self.seed_spam = by_type[SEED_SPAM]
        bounce_at: dict[str, list[tuple[Any, datetime]]] = defaultdict(list)
        for b in self.bounces:
            if b.get("contact_id"):
                bounce_at[str(b["contact_id"])].append((b.get("step"), utc(b["occurred_at"])))
        self.sends: list[_Send] = []
        for e in by_type["sent"]:
            at = utc(e["occurred_at"])
            s = _Send(str(e.get("event_id")), _lower(e.get("mailbox")), str(e.get("contact_id") or ""),
                      str(e.get("account_id") or ""), e.get("step"), at)
            s.bounced = any((step is None or step == s.step) and t >= at for step, t in bounce_at.get(s.contact_id, ()))
            self.sends.append(s)
        self.sends.sort(key=lambda s: (s.at, s.event_id))
        self.account_of = {s.contact_id: s.account_id for s in self.sends if s.contact_id and s.account_id}
        self.mailbox_of = {(s.contact_id, s.step): s.mailbox for s in self.sends}

    def bounce_mailbox(self, b: Mapping[str, Any]) -> str:
        """The mailbox a bounce came back to: its own column, else the send it answers."""
        return _lower(b.get("mailbox")) or self.mailbox_of.get((str(b.get("contact_id") or ""), b.get("step")), "") \
            or self.mailbox_of.get((str(b.get("contact_id") or ""), 1), "")

    def account(self, e: Mapping[str, Any]) -> str:
        return str(e.get("account_id") or "") or self.account_of.get(str(e.get("contact_id") or ""), "")


# -- earlier firings ---------------------------------------------------------------------------------


class _Fired:
    """kill_rule items already on record: what is still open, and when each target last fired."""

    def __init__(self, store):
        self.items = store.select("hitl_items", {"kind": KIND})

    @staticmethod
    def _p(item: Mapping[str, Any]) -> Mapping[str, Any]:
        return item.get("payload") if isinstance(item.get("payload"), Mapping) else {}

    def _match(self, scope: str, target: str) -> list[dict]:
        t = target.casefold()
        return [i for i in self.items if self._p(i).get("scope") == scope and str(self._p(i).get("target") or "").casefold() == t]

    def open(self, scope: str, target: str) -> bool:
        return any(i.get("status") in WAITING for i in self._match(scope, target))

    def since(self, *keys: tuple[str, str]) -> datetime | None:
        """When any of these (scope, target) last fired; events at or before it are already counted."""
        times = [utc(i.get("created_at")) for k in keys for i in self._match(*k)]
        times = [t for t in times if t is not None]
        return max(times) if times else None

    def ever(self, rule: str) -> bool:
        return any(self._p(i).get("rule") == rule for i in self.items)


def _after(t: datetime, since: datetime | None, start: datetime) -> bool:
    return t >= start and (since is None or t > since)


def _rate(sends: list[_Send]) -> tuple[int, int, float]:
    n = len(sends)
    b = sum(s.bounced for s in sends)
    return n, b, (b / n if n else 0.0)


def _over(n: int, b: int, rate: float, *, min_sends: int = 1) -> bool:
    return n >= min_sends and b >= MIN_BOUNCES and rate > BOUNCE_RATE


# -- the mailbox and domain rules ------------------------------------------------------------------


def _vitals(ctx: Context, addresses: list[str]) -> tuple[dict[str, str], str]:
    """(address -> why Instantly says it fails, an error if Instantly could not be read)."""
    if not addresses:
        return {}, ""
    try:
        status = ctx.clients.instantly.warmup_status(addresses)
    except (ConfigError, ApiError) as exc:
        return {}, f"{type(exc).__name__}: {str(exc)[:200]}"
    out: dict[str, str] = {}
    for a, w in status.items():
        if not w.get("found"):
            continue
        if w.get("status") in VITALS_FAIL_ACCOUNT:
            out[a] = f"Instantly reports the account as {w['status']}"
        elif w.get("warmup_status") in VITALS_FAIL_WARMUP:
            out[a] = f"Instantly reports its warmup as {w['warmup_status']}"
    return out, ""


def mailbox_rules(ctx: Context, settings: Settings, ev: _Events, fired: _Fired) -> tuple[list[Firing], str]:
    """Block signals (14 days) first, then the bounce rate, for each Active mailbox."""
    now, today = ctx.now, ctx.today_uk()
    start = now - timedelta(days=WINDOW_DAYS)
    active = [m for m in settings.mailboxes if m.status == ACTIVE]
    vitals, vitals_error = _vitals(ctx, [m.address.lower() for m in active])
    out: list[Firing] = []
    for m in active:
        a = m.address.lower()
        if fired.open("mailbox", a) or fired.open("domain", _domain(a)):
            continue
        since = fired.since(("mailbox", a), ("domain", _domain(a)))
        blocks = [b for b in ev.bounces if ev.bounce_mailbox(b) == a and _after(utc(b["occurred_at"]), since, start)
                  and BLOCK_CODE.search(f"{b.get('reply_class') or ''} {b.get('reply_text') or ''}")]
        complaints = [c for c in ev.complaints if ev.bounce_mailbox(c) == a and _after(utc(c["occurred_at"]), since, start)]
        seeds = [s for s in ev.seed_spam if _lower(s.get("mailbox")) == a and _after(utc(s["occurred_at"]), since, start)]
        why = ""
        rule = ""
        if blocks:
            code = BLOCK_CODE.search(f"{blocks[-1].get('reply_class') or ''} {blocks[-1].get('reply_text') or ''}")
            rule, why = "block_bounce", f"{len(blocks)} block bounce{'s' if len(blocks) > 1 else ''} ({code.group(0)})"
        elif complaints:
            rule, why = "spam_complaint", f"{len(complaints)} spam complaint{'s' if len(complaints) > 1 else ''}"
        elif seeds:
            rule, why = "seed_spam", "a seed inbox found its email in spam"
        elif a in vitals:
            rule, why = "vitals", vitals[a]
        if rule:
            until = today + timedelta(days=BLOCK_PAUSE_DAYS)
            out.append(Firing(rule, "mailbox", a, PAUSE_MAILBOX, f"{a}: {why}", [a], until,
                              {"block_bounces": len(blocks), "complaints": len(complaints), "seed_spam": len(seeds)}))
            continue
        sends = [s for s in ev.sends if s.mailbox == a and _after(s.at, since, start)][-LAST_SENDS:]
        n, b, rate = _rate(sends)
        if _over(n, b, rate):
            out.append(Firing("mailbox_bounce_rate", "mailbox", a, PAUSE_MAILBOX,
                              f"{a}: {b} of its last {n} sends bounced ({fmt.share(rate)}; the limit is {BOUNCE_RATE:.0%})",
                              [a], None, {"sends": n, "bounced": b, "rate": round(rate, 4), "window_days": WINDOW_DAYS}))
    return out, vitals_error


def domain_rules(ctx: Context, settings: Settings, ev: _Events, fired: _Fired, already: set[str]) -> list[Firing]:
    start = ctx.now - timedelta(days=WINDOW_DAYS)
    out: list[Firing] = []
    for domain in sorted({_domain(m.address) for m in settings.mailboxes if m.status == ACTIVE}):
        if fired.open("domain", domain):
            continue
        since = fired.since(("domain", domain))
        n, b, rate = _rate([s for s in ev.sends if _domain(s.mailbox) == domain and _after(s.at, since, start)])
        boxes = [m.address.lower() for m in settings.mailboxes if m.status == ACTIVE and _domain(m.address) == domain
                 and m.address.lower() not in already]
        if boxes and _over(n, b, rate, min_sends=MIN_SENDS):
            out.append(Firing("domain_bounce_rate", "domain", domain, PAUSE_MAILBOX,
                              f"{domain}: {b} of its {n} sends in the last {WINDOW_DAYS} days bounced "
                              f"({fmt.share(rate)}; the limit is {BOUNCE_RATE:.0%})",
                              boxes, None, {"sends": n, "bounced": b, "rate": round(rate, 4), "window_days": WINDOW_DAYS}))
    return out


# -- the source and group rules --------------------------------------------------------------------


def _contacts(ctx: Context, ids: Iterable[str]) -> dict[str, dict]:
    ids = sorted({i for i in ids if i})
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 1000):
        for c in ctx.store.select("contacts", {"contact_id": ids[i : i + 1000]}):
            out[str(c["contact_id"])] = c
    return out


def source_rules(ctx: Context, ev: _Events, fired: _Fired) -> list[Firing]:
    start = ctx.now - timedelta(days=WINDOW_DAYS)
    recent = [s for s in ev.sends if s.at >= start]
    source_of = {cid: _lower(c.get("email_source")) for cid, c in _contacts(ctx, (s.contact_id for s in recent)).items()}
    out: list[Firing] = []
    for src in SOURCES:
        if fired.open("source", src):
            continue
        since = fired.since(("source", src))
        n, b, rate = _rate([s for s in recent if source_of.get(s.contact_id) == src and _after(s.at, since, start)])
        if _over(n, b, rate, min_sends=MIN_SENDS):
            out.append(Firing("source_bounce_rate", "source", src, PAUSE_SOURCE,
                              f"emails found by {src}: {b} of {n} sends in the last {WINDOW_DAYS} days bounced "
                              f"({fmt.share(rate)}; the limit is {BOUNCE_RATE:.0%})",
                              numbers={"sends": n, "bounced": b, "rate": round(rate, 4), "window_days": WINDOW_DAYS}))
    return out


def _step1(ev: _Events) -> dict[str, _Send]:
    """account -> its first step-1 send."""
    out: dict[str, _Send] = {}
    for s in ev.sends:
        if s.step == 1 and s.account_id and s.account_id not in out:
            out[s.account_id] = s
    return out


def group_rules(ctx: Context, settings: Settings, ev: _Events, fired: _Fired) -> list[Firing]:
    window = timedelta(days=REPLY_WINDOW_DAYS)
    step1 = {a: s for a, s in _step1(ev).items() if s.at + window <= ctx.now and not s.bounced}
    if not step1:
        return []
    accounts = {}
    ids = sorted(step1)
    for i in range(0, len(ids), 1000):
        for a in ctx.store.select("accounts", {"account_id": ids[i : i + 1000]}):
            accounts[str(a["account_id"])] = a
    replied = defaultdict(list)
    for r in ev.replies:
        if kinds.is_human(r.get("reply_class")):
            replied[ev.account(r)].append(utc(r["occurred_at"]))
    by_group: dict[str, list[tuple[str, _Send]]] = defaultdict(list)
    for aid, s in step1.items():
        group = settings.industry_group_of(accounts.get(aid, {}))
        if group:
            by_group[group].append((aid, s))
    out: list[Firing] = []
    for group, rows in sorted(by_group.items()):
        if fired.open("industry_group", group):
            continue
        since = fired.since(("industry_group", group))
        rows = [(aid, s) for aid, s in rows if since is None or s.at > since]
        delivered = len(rows)
        humans = sum(any(s.at <= t < s.at + window for t in replied.get(aid, ())) for aid, s in rows)
        rate = humans / delivered if delivered else 0.0
        if delivered >= GROUP_MIN_DELIVERED and rate < GROUP_REPLY_FLOOR:
            out.append(Firing("group_reply_rate", "industry_group", group, STOP_GROUP,
                              f"{group}: {humans} human replies from {delivered} accounts delivered "
                              f"({fmt.share(rate)}; the floor is {GROUP_REPLY_FLOOR:.1%})",
                              numbers={"delivered": delivered, "replied": humans, "rate": round(rate, 4)}))
    return out


# -- the stop rule ---------------------------------------------------------------------------------


def stop_rules(ctx: Context, settings: Settings, ev: _Events, fired: _Fired) -> list[Firing]:
    g, now = settings.general, ctx.now
    out: list[Firing] = []
    if fired.open("enrolment", "stop_rule"):
        return out
    # Fewer than stop_rule_meetings meetings from the first stop_rule_accounts accounts (SPEC 12).
    if g.stop_rule_accounts > 0 and g.stop_rule_meetings > 0 and not fired.ever("stop_rule_meetings"):
        first: dict[str, datetime] = {}
        for c in ctx.store.select("contacts"):
            t = utc(c.get("enrolled_at"))
            aid = str(c.get("account_id") or "")
            if t is not None and aid and (aid not in first or t < first[aid]):
                first[aid] = t
        if len(first) >= g.stop_rule_accounts:
            cohort = sorted(first.items(), key=lambda kv: (kv[1], kv[0]))[: g.stop_rule_accounts]
            last = cohort[-1][1]
            if now >= last + timedelta(days=REPLY_WINDOW_DAYS):
                ids = {aid for aid, _ in cohort}
                met = len({ev.account(e) for e in ev.meetings} & ids)
                if met < g.stop_rule_meetings:
                    out.append(Firing("stop_rule_meetings", "enrolment", "stop_rule", PAUSE_ENROLMENT,
                                      f"the first {g.stop_rule_accounts} accounts brought {met} meetings, fewer than "
                                      f"stop_rule_meetings ({g.stop_rule_meetings}): a profile and copy review",
                                      numbers={"accounts": g.stop_rule_accounts, "meetings": met}))
    # Account-level bounces and spam complaints over the last 30 days (General stop_rule_*_rate).
    since = fired.since(("enrolment", "stop_rule"))
    start = now - timedelta(days=STOP_WINDOW_DAYS)
    cohort_ids = {a for a, s in _step1(ev).items() if _after(s.at, since, start)}
    n = len(cohort_ids)
    if n >= STOP_MIN_ACCOUNTS:
        bounced = {ev.account(b) for b in ev.bounces} & cohort_ids
        complained = {ev.account(c) for c in ev.complaints} & cohort_ids
        if len(bounced) / n > g.stop_rule_bounce_rate:
            out.append(Firing("stop_rule_bounce_rate", "enrolment", "stop_rule", PAUSE_ENROLMENT,
                              f"{len(bounced)} of the {n} accounts sent step 1 in the last {STOP_WINDOW_DAYS} days bounced "
                              f"({fmt.share(len(bounced) / n)}; the limit, stop_rule_bounce_rate, is "
                              f"{fmt.share(g.stop_rule_bounce_rate)})",
                              numbers={"accounts": n, "bounced": len(bounced)}))
        elif len(complained) / n > g.stop_rule_complaint_rate:
            out.append(Firing("stop_rule_complaint_rate", "enrolment", "stop_rule", PAUSE_ENROLMENT,
                              f"{len(complained)} of the {n} accounts sent step 1 in the last {STOP_WINDOW_DAYS} days "
                              f"made a spam complaint ({fmt.share(len(complained) / n)}; the limit, stop_rule_complaint_rate, "
                              f"is {fmt.share(g.stop_rule_complaint_rate)})",
                              numbers={"accounts": n, "complained": len(complained)}))
    return out[:1]  # one stop is enough; the next fires only after Harry clears it


# -- suppression ---------------------------------------------------------------------------------


def suppress_bounced(ctx: Context, ev: _Events) -> int:
    """Every bounced contact on suppression (reason bounce) and marked suppressed; returns how many were new."""
    contacts = _contacts(ctx, (str(b.get("contact_id") or "") for b in ev.bounces))
    fresh = [c for c in contacts.values() if not c.get("suppressed") and "@" in str(c.get("email") or "")]
    if not fresh:
        return 0
    added = suppression.add_emails(ctx.store, [str(c["email"]) for c in fresh], reason=BOUNCE_REASON, source=JOB, now=ctx.now)
    ctx.store.upsert("contacts", [{"contact_id": c["contact_id"], "suppressed": True,
                                   "suppressed_reason": BOUNCE_REASON} for c in fresh])
    return added


# -- acting on it --------------------------------------------------------------------------------


def _pause(ctx: Context, settings: Settings, address: str) -> tuple[str, str]:
    """Pause through the registry path; if the sheet cannot be written, still take it off its sending list.

    Returns (what happened, the campaign the pause left with no Active mailbox, or ""): mailbox_health
    starts that campaign again once the mailbox is Active again (registry/mailboxes.start_waiting).
    """
    from us_outbound.registry import mailboxes as reg

    try:
        out = reg.mailbox_pause(ctx, address, settings=settings)
        emptied = out["campaign"] if out["campaign_action"] == reg.PAUSED_EMPTY else ""
        return f"Paused on the Mailboxes tab; {out['campaign']}: {out['campaign_action']}", emptied
    except (reg.MailboxError, ApiError, LookupError, ValueError, ConfigError) as exc:
        m = next((x for x in settings.mailboxes if x.address.lower() == address), None)
        if m is None:
            return f"not paused: {exc}", ""
        try:
            action = reg._sync_campaign(ctx, reg._with(settings, dataclasses.replace(m, status=reg.PAUSED)), m.owner_name)
        except (ApiError, LookupError, ValueError, ConfigError) as exc2:
            return f"NOT PAUSED: {exc}; {exc2}. Pause it by hand: {say.command(f'mailbox pause {address} --live')}", ""
        name = reg.campaign_name(m.owner_name)
        return f"the Mailboxes tab was not changed ({exc}); {name}: {action}", name if action == reg.PAUSED_EMPTY else ""


def _payload(ctx: Context, f: Firing) -> dict:
    return {"rule": f.rule, "scope": f.scope, "target": f.target, "action": f.action, "reason": f.reason,
            "mailboxes": f.mailboxes, "until": f.until.isoformat() if f.until else None, "numbers": f.numbers,
            "dry_run": ctx.dry_run, "outcome": f.outcome, "campaigns_paused": f.campaigns}


def _record(ctx: Context, f: Firing) -> None:
    f.item_id = f.item_id or new_id()
    ctx.store.upsert("hitl_items", [{"item_id": f.item_id, "kind": KIND, "status": "open", "created_at": ctx.now,
                                     "payload": _payload(ctx, f)}])


def expire(ctx: Context) -> list[dict]:
    """14-day mailbox pauses whose time is up: handled, so the hold lifts (the sheet still says Paused)."""
    today = ctx.today_uk()
    out = []
    for r in holds.in_force(ctx.store):
        p = r.get("payload") if isinstance(r.get("payload"), Mapping) else {}
        until = p.get("until")
        if p.get("action") == PAUSE_MAILBOX and until and date.fromisoformat(str(until)) <= today:
            ctx.store.upsert("hitl_items", [{"item_id": r["item_id"], "status": "handled", "handled_at": ctx.now,
                                             "handled_by": f"{JOB}: {BLOCK_PAUSE_DAYS} days over"}])
            out.append({"item_id": r["item_id"], "mailboxes": list(p.get("mailboxes") or ()), "rule": p.get("rule")})
    return out


# The alert (Slack, base/say.py): what stopped and why in plain words, who it affects, whether first emails and
# follow-ups still go, and the exact steps to resume. Each address once a line.
RULE_WORDS = {"block_bounce": "a block bounce", "spam_complaint": "a spam complaint",
              "seed_spam": "a test inbox found its email in spam", "vitals": "Instantly's account checks"}
PAUSED_OK = "Paused on the Mailboxes tab"  # _pause's outcome when the registry path worked
SOURCE_NAMES = {"apollo": "Apollo", "clay": "Clay"}
WHY_IT_MATTERS = "too many bounces or spam complaints make mail providers treat all our email as spam."


def _clear(f: Firing) -> str:
    return say.command(f"killrules clear {f.item_id} --live")


def _why(f: Firing) -> str:
    """The reason without the target in front: the line names it already."""
    for prefix in (f"{f.target}: ", f"emails found by {f.target}: "):
        if f.reason.startswith(prefix):
            return f.reason[len(prefix):]
    return f.reason


def _first(owner: str) -> str:
    return (owner.split() or ["Its owner"])[0]


def _owner_of(settings: Settings, address: str) -> str:
    return next((m.owner_name for m in settings.mailboxes if m.address.lower() == address.lower()), "")


def _s(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _plain_why(f: Firing, settings: Settings) -> str:
    """Why it fired, in words a newcomer follows (f.reason, kept on the item, stays as it is)."""
    n, g = f.numbers, settings.general
    rate = fmt.share(float(n.get("rate") or 0))
    limit = f"the limit is {BOUNCE_RATE:.0%}"
    if f.rule == "mailbox_bounce_rate":
        return f"{n['bounced']} of its last {n['sends']} emails bounced ({rate}; {limit})"
    if f.rule == "domain_bounce_rate":
        return (f"{n['bounced']} of the {n['sends']} emails sent from {f.target} in the last {WINDOW_DAYS} days bounced "
                f"({rate}; {limit})")
    if f.rule == "block_bounce":
        code = BLOCK_CODE.search(f.reason)
        return (f"a receiving mail server refused its email ({_s(int(n.get('block_bounces') or 1), 'block bounce')}"
                + (f", code {code.group(0)}" if code else "") + ")")
    if f.rule == "spam_complaint":
        k = int(n.get("complaints") or 1)
        return f"{'a recipient' if k == 1 else f'{k} recipients'} marked its email as spam"
    if f.rule == "seed_spam":
        return "one of our test inboxes found its email in the spam folder"
    if f.rule == "source_bounce_rate":
        return (f"{n['bounced']} of the {n['sends']} emails sent to these addresses in the last {WINDOW_DAYS} days bounced "
                f"({rate}; {limit})")
    if f.rule == "group_reply_rate":
        return (f"only {n['replied']} of the {n['delivered']} companies emailed in it replied ({rate}; the minimum is "
                f"{GROUP_REPLY_FLOOR:.1%})")
    if f.rule == "stop_rule_meetings":
        return (f"the first {n['accounts']} companies emailed brought {_s(int(n['meetings']), 'meeting')}, fewer than "
                f"`stop_rule_meetings` ({g.stop_rule_meetings}) on the General tab")
    if f.rule == "stop_rule_bounce_rate":
        return (f"{n['bounced']} of the {n['accounts']} companies first emailed in the last {STOP_WINDOW_DAYS} days "
                f"bounced ({fmt.pct(n['bounced'], n['accounts'])}; the limit, `stop_rule_bounce_rate` on the General "
                f"tab, is {fmt.share(g.stop_rule_bounce_rate)})")
    if f.rule == "stop_rule_complaint_rate":
        return (f"{n['complained']} of the {n['accounts']} companies first emailed in the last {STOP_WINDOW_DAYS} days "
                f"marked our email as spam ({fmt.pct(n['complained'], n['accounts'])}; the limit, "
                f"`stop_rule_complaint_rate` on the General tab, is {fmt.share(g.stop_rule_complaint_rate)})")
    return _why(f)  # vitals: "Instantly reports the account as sending_error"


def _boxes(settings: Settings, f: Firing, *, short: bool = False) -> str:
    """"Sam's mailbox (sam@…)"; "2 mailboxes on meetspill.org (hannah@… for Hannah, harry@… for Harry)".
    short: without the addresses, for a headline."""
    if len(f.mailboxes) == 1:
        first = _first(_owner_of(settings, f.mailboxes[0]))
        return f"{first}'s mailbox" + ("" if short else f" ({f.mailboxes[0]})")
    where = f" on {f.target}" if f.scope == "domain" else ""
    each = ", ".join(f"{a} for {_first(_owner_of(settings, a))}" for a in f.mailboxes)
    return f"{len(f.mailboxes)} mailboxes{where}" + ("" if short else f" ({each})")


def _what(ctx: Context, f: Firing, settings: Settings, *, short: bool = False) -> str:
    """What the safety stop did, as a sentence without its reason ("would" in a dry run, for a mailbox)."""
    if f.action == PAUSE_MAILBOX:
        many = len(f.mailboxes) > 1
        for_days = f" for {BLOCK_PAUSE_DAYS} days" if f.until else ""
        verb = "would stop sending" if ctx.dry_run else f"{'have' if many else 'has'} stopped sending"
        return f"{_boxes(settings, f, short=short)} {verb}{for_days}"
    if f.action == PAUSE_SOURCE:
        src = SOURCE_NAMES.get(f.target, f.target)
        return (f"email addresses from {src} are no longer used" if short else
                f"New contacts whose email address came from {src} are no longer added to the campaign")
    if f.action == STOP_GROUP:
        return (f"no new {f.target} companies are being emailed" if short else
                f"No new companies in the {f.target} industry group are added to the campaign")
    return ("no new companies are being emailed" if short else
            "No new companies are added to the campaign at all (the stop rule)")


def _who_waits(settings: Settings, mailboxes: list[str], dry: bool) -> list[str]:
    """Per owner of these mailboxes: their campaign goes on from another Active mailbox, or it is paused."""
    paused = {a.lower() for a in mailboxes}
    owners = dict.fromkeys(m.owner_name for m in settings.mailboxes if m.address.lower() in paused)
    out = []
    for owner in owners:
        first = _first(owner)
        others = [m.address.lower() for m in settings.mailboxes_for(owner, ACTIVE) if m.address.lower() not in paused]
        if others:
            out.append(f"• {first}'s campaign {'would carry' if dry else 'carries'} on from {', '.join(others)}: first "
                       "emails and follow-ups still go.")
        else:
            out.append(f"• {first}'s campaign {'would be' if dry else 'is'} paused: none of {first}'s contacts get a "
                       f"first email or a follow-up until one of {first}'s mailboxes is Active again.")
    return out


def _back_on(it: str = "it") -> str:
    """How a mailbox set Active again gets back on its campaign's sending list: settings_sync reads the sheet, then
    mailbox_health (07:00 UK) puts it on the list and starts a campaign the pause left empty."""
    return (f"The next settings sync and mailbox check (07:00 UK) put {it} back on the sending list; for sooner, run "
            f"{say.command('sync --live')}, then {say.command('mailbox check --live')}")


def _effects(ctx: Context, f: Firing, settings: Settings) -> list[str]:
    """Who it affects, whether first emails and follow-ups still go, and how to resume."""
    if f.action == PAUSE_SOURCE:
        src = SOURCE_NAMES.get(f.target, f.target)
        other = " and ".join(SOURCE_NAMES.get(s, s) for s in SOURCES if s != f.target)
        return ["• Contacts already in the campaign carry on: their follow-ups still go."
                + (f" Contacts found by {other} are still added." if other else ""),
                f"• To use {src} addresses again once they are checked: {_clear(f)}."]
    if f.action == STOP_GROUP:
        return ["• Companies already emailed carry on: their follow-ups still go.",
                f"• To add {f.target} companies again: {_clear(f)}."]
    if f.action != PAUSE_MAILBOX:
        return ["• Emails already under way carry on: their follow-ups still go.",
                f"• To start adding companies again after a review of who we target and the email copy: {_clear(f)}."]
    it = "them" if len(f.mailboxes) > 1 else "it"
    lines = _who_waits(settings, f.mailboxes, ctx.dry_run)
    if f.outcome and not f.outcome.startswith((PAUSED_OK, "dry-run")):
        lines.append(f"• :warning: The pause did not fully go through: {f.outcome}.")  # see _pause
    if ctx.dry_run:
        lifts = f"It lifts by itself after {f.until:%a %d %b}, or sooner with" if f.until else "To lift it:"
        lines.append(f"• Nothing was changed in the sheet or Instantly; only the stop is recorded. {lifts} {_clear(f)}.")
        return lines
    tab = say.sheet_link(ctx, "Mailboxes")
    if f.until:
        lines.append(f"• To resume once the cause is fixed: the stop lifts by itself after {f.until:%a %d %b} (or "
                     f"sooner with {_clear(f)}); then set {it} to Active on the {tab}. {_back_on(it)}.")
    else:
        lines.append(f"• To resume once the cause is fixed: set {it} to Active on the {tab}, then run {_clear(f)}. "
                     f"{_back_on(it)}.")
    return lines


def _lines(ctx: Context, f: Firing, settings: Settings, *, alone: bool) -> list[str]:
    """One firing. alone: the headline already says what stopped, so the first line gives the reason only."""
    why = _plain_why(f, settings)
    if alone:
        first = f"{', '.join(f.mailboxes)}: {why}." if f.action == PAUSE_MAILBOX else f"Why: {why}."
    else:
        first = f"*{_what(ctx, f, settings)}* because {why}."
    return [first, *_effects(ctx, f, settings)]


def _headline(ctx: Context, fired: list[Firing], settings: Settings) -> str:
    if len(fired) > 1:
        return f"Safety stop: {len(fired)} things paused to protect our email reputation"
    return f"Safety stop: {_what(ctx, fired[0], settings, short=True)}"


def _expired_line(e: Mapping[str, Any], settings: Settings) -> str:
    boxes = list(e["mailboxes"])
    who = ", ".join(f"{_first(_owner_of(settings, a))}'s mailbox {a}" for a in boxes)
    return (f"• {who}: the {BLOCK_PAUSE_DAYS}-day pause after {RULE_WORDS.get(e['rule'], e['rule'])} is over, but the "
            f"Mailboxes tab still says Paused, so {'they send' if len(boxes) > 1 else 'it sends'} nothing yet.")


def message(ctx: Context, fired: list[Firing], expired: list[dict]) -> str:
    settings = holds.with_holds(ctx.store, ctx.settings)  # the holds just recorded count as Paused
    over = [_expired_line(e, settings) for e in expired]
    n_over = sum(len(e["mailboxes"]) for e in expired)
    it = "it" if n_over == 1 else "them"
    set_active = (f"Once you are happy the cause is fixed, set {it} to Active on the "
                  f"{say.sheet_link(ctx, 'Mailboxes')}. {_back_on(it)}.")
    if not fired:
        head = "Safety stop over: " + ("a mailbox" if n_over == 1 else f"{n_over} mailboxes") + " can send again"
        return notify.mention(ctx) + say.compose(say.ACTION, head, over, do=set_active)
    facts: list[str] = []
    for f in fired:
        facts += _lines(ctx, f, settings, alone=len(fired) == 1)
    if over:
        facts += [f"*Also, a {BLOCK_PAUSE_DAYS}-day pause is over:*", *over, f"• {set_active}"]
    one = len(fired) == 1
    do = (f"Find out the cause, then follow the step{'' if one else 's'} above to resume. Until then "
          f"{'it stays' if one else 'they stay'} paused.")
    return notify.mention(ctx) + say.compose(say.URGENT, _headline(ctx, fired, settings), facts, do=do,
                                             why=WHY_IT_MATTERS)


def run(ctx: Context) -> dict:
    """The kill_rules job (JOB CONTRACT: run(ctx) -> summary)."""
    settings = holds.with_holds(ctx.store, ctx.settings)
    ev = _Events(ctx.store, ctx.now)
    fired_before = _Fired(ctx.store)
    suppressed = suppress_bounced(ctx, ev)
    expired = expire(ctx)

    firing, vitals_error = mailbox_rules(ctx, settings, ev, fired_before)
    already = {a for f in firing for a in f.mailboxes}
    firing += domain_rules(ctx, settings, ev, fired_before, already)
    firing += source_rules(ctx, ev, fired_before)
    firing += group_rules(ctx, settings, ev, fired_before)
    firing += stop_rules(ctx, settings, ev, fired_before)

    # The hold first, so the mailbox is out of every job even if the pause below fails.
    for f in firing:
        _record(ctx, f)
    paused: dict[str, str] = {}
    for f in firing:
        if f.action != PAUSE_MAILBOX:
            continue
        outcomes = []
        for a in f.mailboxes:
            if ctx.live:
                paused[a], emptied = _pause(ctx, ctx.settings, a)
                if emptied and emptied not in f.campaigns:
                    f.campaigns.append(emptied)
            else:
                paused[a] = "dry-run: not paused in the sheet or Instantly"
            outcomes.append(paused[a])
        f.outcome = "; ".join(dict.fromkeys(outcomes))
        ctx.store.upsert("hitl_items", [{"item_id": f.item_id, "payload": _payload(ctx, f)}])

    alert: dict[str, Any] = {}
    if firing or expired:
        alert = notify.alert(ctx, message(ctx, firing, expired))
        if alert.get("ts"):
            for f in firing:
                ctx.store.upsert("hitl_items", [{"item_id": f.item_id, "slack_channel": alert.get("channel"),
                                                 "slack_ts": alert["ts"]}])
    summary = {
        "job": JOB, "dry_run": ctx.dry_run,
        "fired": [{"rule": f.rule, "target": f.target, "action": f.action, "reason": f.reason, "item_id": f.item_id,
                   "until": f.until.isoformat() if f.until else None} for f in firing][:LIST_LIMIT],
        "paused": paused, "expired": expired, "suppressed": suppressed,
        "checked": {"mailboxes": sum(m.status == ACTIVE for m in settings.mailboxes), "sends": len(ev.sends),
                    "bounces": len(ev.bounces)},
        "holds_in_force": len(holds.in_force(ctx.store)),
        "vitals_error": vitals_error or None,
        "alert": alert or None,
    }
    log("kill_rules_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "fired"},
        fired=[f.rule for f in firing])
    return summary


# -- the killrules command (operator) ----------------------------------------------------------------


def show(ctx: Context) -> list[dict]:
    """The kill-rule holds in force, oldest first."""
    rows = sorted(holds.in_force(ctx.store), key=lambda r: utc(r.get("created_at")) or datetime.min.replace(tzinfo=UTC))
    out = []
    for r in rows:
        p = r.get("payload") if isinstance(r.get("payload"), Mapping) else {}
        out.append({"item_id": r["item_id"], "rule": p.get("rule"), "action": p.get("action"), "target": p.get("target"),
                    "mailboxes": list(p.get("mailboxes") or ()), "until": p.get("until"), "reason": p.get("reason"),
                    "created_at": r.get("created_at"), "status": r.get("status"), "dry_run": p.get("dry_run")})
    return out


def clear(ctx: Context, item_id: str, by: str) -> dict:
    """Lift one hold after Harry has checked it: the item is handled. Live only; dry-run says what it would do."""
    item = ctx.store.get("hitl_items", item_id=item_id)
    if item is None or item.get("kind") != KIND:
        raise LookupError(f"no kill-rule item {item_id!r}; `us-outbound killrules show` lists them")
    p = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    if item.get("status") not in WAITING:
        return {"item_id": item_id, "changed": False, "status": item.get("status"), "dry_run": ctx.dry_run}
    if ctx.live:
        ctx.store.upsert("hitl_items", [{"item_id": item_id, "status": "handled", "handled_at": ctx.now, "handled_by": by}])
    next_step = ""
    if p.get("action") == PAUSE_MAILBOX:
        next_step = ("The mailbox stays Paused on the Mailboxes tab until you set it Active there; then "
                     "`us-outbound settings sync` and `us-outbound campaigns ensure --fix --live` put it back on its sending list.")
    return {"item_id": item_id, "changed": ctx.live, "dry_run": ctx.dry_run, "rule": p.get("rule"),
            "action": p.get("action"), "target": p.get("target"), "next": next_step}
