"""kill_rules, hourly and after every recorded bounce (SPEC 9, 12 "Kill rules"): pause what is hurting
deliverability, and say so in Slack.

It reads the events table, which sync_outcomes fills from Instantly: type sent (one row per campaign email, with its
mailbox, contact and step; a reply sent from the reply desk is reply_sent, so it is never in a rate's denominator) and
bounced, the names enrol/capacity.py STOP_EVENTS uses. A send counts as bounced when a bounced event for the same
contact, at the same step or with no step, came at or after it (as v_mailbox_health counts it).

The rules (SPEC 12; Harry, 1 Oct 2026, docs/gtm-review/README.md §4.2 D4 and §7.6: bounce rules from the first send,
not phase 3). WINDOW_DAYS is 7; a rule looks only at events after the last time it fired for the same mailbox,
domain, source or group, so old bounces never count twice.

  rule                      fires when                                              action
  mailbox_bounce_rate       a mailbox's sends in the window (its latest 100 at      pause the mailbox
                            most): more than 3% bounced, and at least 2
  domain_bounce_rate        a domain's sends in the window, 100 or more: more       pause its mailboxes
                            than 3% bounced (SPEC 12's own rule)
  vitals                    Instantly reports the account disconnected            pause it for 14 days
                            (connection_error) or in error, or its warmup banned
  source_bounce_rate        an email source's sends in the window, 100 or more:     pause the source until
                            more than 3% bounced                                     checked
  group_reply_rate          an industry group's companies whose step 1 was          stop enrolling it
                            delivered and whose 28-day reply window has closed, 400
                            or more: human replies under 0.5%
  stop_rule_meetings        the first stop_rule_accounts accounts' reply windows    pause enrolment (the
                            have closed with fewer than stop_rule_meetings meetings  stop rule)
  stop_rule_bounce_rate     of the accounts sent step 1 in the last 30 days (100 or pause enrolment
                            more), more than stop_rule_bounce_rate bounced

At least 2 bounces (MIN_BOUNCES) before a rate rule fires: on the ramp's 10 a day a single bounce would be 10%, and
would pause a healthy mailbox.

W2-3 (20 Oct 2026; code-learn-reporting B2, B12, B16, B17): four rules had no input and went, block_bounce (no writer
puts the SMTP status on a bounced row), spam_complaint and seed_spam (nothing writes a complained or seed_spam row)
and stop_rule_complaint_rate (its General key is retired); the readout says complaints are "not measured" until a
feed exists. suppress_bounced went too: sync_outcomes suppresses every bounced address before it records the bounce
(replies/outcomes._record_bounce), so the event cannot exist without the suppression. Only the bounce rules' reads are
windowed (sends and bounces of the last WINDOW_DAYS); the group and stop rules read every company's outcome
(learn/outcomes.per_company), with no window, as before (RS P0-7). Vitals read the account GET only (no warmup
analytics call), and connection_error (a lost OAuth grant: Instantly sends nothing) is a failing status. Beside the
hourly run, sync_outcomes calls after_bounce at its end whenever it recorded a bounce: the three bounce-rate rules
run then, so a mailbox over the limit is paused within that run, not up to an hour later. Nothing is slower or
narrower than before. A failure reading the outcomes (or in the group or stop rule) never costs the bounce rules'
pauses: those are acted on first, and the run then ends in that error (W2-3 r1).

Each firing is a hitl_items row (kind kill_rule; base/holds.py says what it holds). A mailbox
is paused through the registry pause path (registry/mailboxes.mailbox_pause: Paused on the
Mailboxes tab and off its campaign's sending list); if the sheet cannot be written it still
comes off the sending list, and the hold keeps it out of every job either way. A rule does not
fire again while its item is open. All firings of a run go in one Slack message to the alert channel, mentioning
the approvers; with no Slack token the message goes to the log (base/notify.py). Dry-run computes, records the
items and posts to the dev channel, but changes neither the sheet nor Instantly.

The outbox (W1-1, 19 Oct 2026; code-platform-ops B2): the pause happens before the alert, so a Slack that
times out used to leave a mailbox paused with nobody told. Each live item keeps its slack_ts empty until
a post went, and its attempts in its payload (alert_attempts). Before it looks for new firings, every
run posts again, in one message, the open live items written by this code (payload outbox) whose alert
never went and that are under OUTBOX_DAYS old; an item tried OUTBOX_TRIES times waits for the run at
OUTBOX_HOUR_UK, once a day. A dry-run item is never posted again, and nothing is posted again from a
dry run; an alert that went to the log for want of a token is not tried again. A post that timed out may
have reached Slack after all, so the repost says it may be a repeat. The outbox can never stop the brake:
an error in it (a payload edited by hand, a store error) is logged and named in the summary
(reposted_error), and the run goes on to the new firings.

Not here: a human-in-the-loop item left for 24 hours (the reply desk escalates it; enrol already
pauses while a positive reply waits, enrol.reply_pause); the credit rules (budget.py and the
jobs that spend credits); a failed settings sync (settings_sync keeps the version in force).
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import fmt
from us_outbound.base import holds, notify, say
from us_outbound.base.holds import KIND, PAUSE_ENROLMENT, PAUSE_MAILBOX, PAUSE_SOURCE, STOP_GROUP, WAITING
from us_outbound.clients.db import Range, new_id
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import REPLY_WINDOW_DAYS
from us_outbound.context import UK, ConfigError, Context
from us_outbound.learn import outcomes
from us_outbound.logs import log
from us_outbound.settings.model import Settings
from us_outbound.timeparse import utc

JOB = "kill_rules"
ACTIVE = "Active"
WINDOW_DAYS = 7
LAST_SENDS = 100  # a mailbox's rate is over its latest 100 sends in the window
BOUNCE_RATE = 0.03  # SPEC 12: over 3%
MIN_BOUNCES = 2
MIN_SENDS = 100  # SPEC 12: "on 100+ sends" (domain and source rules)
BLOCK_PAUSE_DAYS = 14  # SPEC 12: a vitals pause lasts this long
GROUP_REPLY_FLOOR = 0.005  # SPEC 12: under 0.5% ...
GROUP_MIN_DELIVERED = 400  # ... after 400 delivered
STOP_WINDOW_DAYS = 30
STOP_MIN_ACCOUNTS = 100
# instantly.ACCOUNT_STATUS: connection_error is a lost connection (an expired OAuth grant): Instantly sends nothing
# from it, so it fails too (W2-3; code-learn-reporting B12). registry/mailboxes.mailbox_health reads the same set.
VITALS_FAIL_ACCOUNT = frozenset({"connection_error", "soft_bounce_error", "sending_error"})
VITALS_FAIL_WARMUP = frozenset({"banned", "permanent_suspension"})  # instantly.WARMUP_STATUS
SOURCES = ("apollo", "clay")  # contacts.email_source
LIST_LIMIT = 100
OUTBOX_DAYS = 7  # an item whose alert never went is posted again for this long
OUTBOX_TRIES = 3  # then only at the OUTBOX_HOUR_UK run, once a day
OUTBOX_HOUR_UK = 9


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
    """The sends and bounces the bounce rules read, loaded once a run: those since `since` (the last WINDOW_DAYS for
    the rules; None reads every one, for the daily post's fallback count over a mailbox's last 100 sends)."""

    def __init__(self, store, now: datetime, since: datetime | None = None):
        self.now = now
        where: dict[str, Any] = {"type": ["sent", "bounced"]}
        if since is not None:
            where["occurred_at"] = Range(since, None)
        by_type: dict[str, list[dict]] = defaultdict(list)
        for e in store.select("events", where):
            if utc(e.get("occurred_at")) is not None:
                by_type[str(e.get("type"))].append(e)
        self.bounces = by_type["bounced"]
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


def recent_sends(store, now: datetime, since: datetime | None) -> list[_Send]:
    """Campaign sends since `since` (None: every one), oldest first, each marked bounced or not as the bounce rules
    count them: the readout's source lines and the daily post's fallback count read them here."""
    return _Events(store, now, since).sends


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
    """(address -> why Instantly says it fails, an error if Instantly could not be read). The account GET only: the
    warmup analytics POST adds nothing the rule reads (W2-3; code-learn-reporting B17)."""
    if not addresses:
        return {}, ""
    try:
        status = ctx.clients.instantly.warmup_status(addresses, analytics=False)
    except (ConfigError, ApiError) as exc:
        return {}, f"{type(exc).__name__}: {str(exc)[:200]}"
    return failing(status), ""


def failing(status: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """address -> why, for the accounts Instantly reports failing (instantly.warmup_status's answer).
    registry/mailboxes.mailbox_health reads the same rule, so a failing mailbox has no capacity even before the
    hourly rule pauses it."""
    out: dict[str, str] = {}
    for a, w in status.items():
        if not w.get("found"):
            continue
        if w.get("status") in VITALS_FAIL_ACCOUNT:
            out[str(a).lower()] = f"Instantly reports the account as {w['status']}"
        elif w.get("warmup_status") in VITALS_FAIL_WARMUP:
            out[str(a).lower()] = f"Instantly reports its warmup as {w['warmup_status']}"
    return out


def mailbox_rules(ctx: Context, settings: Settings, ev: _Events, fired: _Fired, *, vitals: bool = True
                  ) -> tuple[list[Firing], str]:
    """Instantly's vitals (14 days) first, then the bounce rate, for each Active mailbox. vitals=False: the bounce
    rate only (after_bounce asks Instantly nothing)."""
    now, today = ctx.now, ctx.today_uk()
    start = now - timedelta(days=WINDOW_DAYS)
    active = [m for m in settings.mailboxes if m.status == ACTIVE]
    found, vitals_error = _vitals(ctx, [m.address.lower() for m in active]) if vitals else ({}, "")
    out: list[Firing] = []
    for m in active:
        a = m.address.lower()
        if fired.open("mailbox", a) or fired.open("domain", _domain(a)):
            continue
        if a in found:
            until = today + timedelta(days=BLOCK_PAUSE_DAYS)
            out.append(Firing("vitals", "mailbox", a, PAUSE_MAILBOX, f"{a}: {found[a]}", [a], until, {}))
            continue
        since = fired.since(("mailbox", a), ("domain", _domain(a)))
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


def group_rules(ctx: Context, settings: Settings, found: list[outcomes.Outcome], fired: _Fired) -> list[Firing]:
    """Per industry group (as the account stands now), the companies whose step 1 was delivered and whose 28-day
    window has closed: every one ever, no window on this rule (outcomes.per_company; RS P0-7)."""
    matured = {o.account_id: o for o in found if o.delivered and o.window_closed_28}
    if not matured:
        return []
    accounts = {}
    ids = sorted(matured)
    for i in range(0, len(ids), 1000):
        for a in ctx.store.select("accounts", {"account_id": ids[i : i + 1000]}):
            accounts[str(a["account_id"])] = a
    by_group: dict[str, list[outcomes.Outcome]] = defaultdict(list)
    for aid, o in matured.items():
        group = settings.industry_group_of(accounts.get(aid, {}))
        if group:
            by_group[group].append(o)
    out: list[Firing] = []
    for group, rows in sorted(by_group.items()):
        if fired.open("industry_group", group):
            continue
        since = fired.since(("industry_group", group))
        rows = [o for o in rows if since is None or o.step1_at > since]
        delivered = len(rows)
        humans = sum(o.replied_in_window for o in rows)
        rate = humans / delivered if delivered else 0.0
        if delivered >= GROUP_MIN_DELIVERED and rate < GROUP_REPLY_FLOOR:
            out.append(Firing("group_reply_rate", "industry_group", group, STOP_GROUP,
                              f"{group}: {humans} human replies from {delivered} accounts delivered "
                              f"({fmt.share(rate)}; the floor is {GROUP_REPLY_FLOOR:.1%})",
                              numbers={"delivered": delivered, "replied": humans, "rate": round(rate, 4)}))
    return out


# -- the stop rule ---------------------------------------------------------------------------------


def stop_rules(ctx: Context, settings: Settings, found: list[outcomes.Outcome], fired: _Fired) -> list[Firing]:
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
                met = sum(o.meeting_any for o in found if o.account_id in ids)
                if met < g.stop_rule_meetings:
                    out.append(Firing("stop_rule_meetings", "enrolment", "stop_rule", PAUSE_ENROLMENT,
                                      f"the first {g.stop_rule_accounts} accounts brought {met} meetings, fewer than "
                                      f"stop_rule_meetings ({g.stop_rule_meetings}): a profile and copy review",
                                      numbers={"accounts": g.stop_rule_accounts, "meetings": met}))
    # Account-level bounces over the last 30 days (General stop_rule_bounce_rate): a bounce at any step at a company
    # whose step 1 went in the window.
    since = fired.since(("enrolment", "stop_rule"))
    start = now - timedelta(days=STOP_WINDOW_DAYS)
    cohort_rows = [o for o in found if _after(o.step1_at, since, start)]
    n = len(cohort_rows)
    if n >= STOP_MIN_ACCOUNTS:
        bounced = sum(o.bounced_any for o in cohort_rows)
        if bounced / n > g.stop_rule_bounce_rate:
            out.append(Firing("stop_rule_bounce_rate", "enrolment", "stop_rule", PAUSE_ENROLMENT,
                              f"{bounced} of the {n} accounts sent step 1 in the last {STOP_WINDOW_DAYS} days bounced "
                              f"({fmt.share(bounced / n)}; the limit, stop_rule_bounce_rate, is "
                              f"{fmt.share(g.stop_rule_bounce_rate)})",
                              numbers={"accounts": n, "bounced": bounced}))
    return out[:1]  # one stop is enough; the next fires only after Harry clears it


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
    """outbox: written by the code that posts an unsent alert again (the module docstring's outbox)."""
    return {"rule": f.rule, "scope": f.scope, "target": f.target, "action": f.action, "reason": f.reason,
            "mailboxes": f.mailboxes, "until": f.until.isoformat() if f.until else None, "numbers": f.numbers,
            "dry_run": ctx.dry_run, "outcome": f.outcome, "campaigns_paused": f.campaigns, "outbox": True}


def _firing(item: Mapping[str, Any]) -> Firing:
    """The Firing an item's payload records, for its alert posted again."""
    p = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    until = p.get("until")
    return Firing(rule=str(p.get("rule") or ""), scope=str(p.get("scope") or ""), target=str(p.get("target") or ""),
                  action=str(p.get("action") or ""), reason=str(p.get("reason") or ""),
                  mailboxes=list(p.get("mailboxes") or ()), until=date.fromisoformat(str(until)) if until else None,
                  numbers=dict(p.get("numbers") or {}), item_id=str(item["item_id"]), outcome=str(p.get("outcome") or ""),
                  campaigns=list(p.get("campaigns_paused") or ()))


def _attempted(ctx: Context, item_id: str, payload: Mapping[str, Any], sent: Mapping[str, Any]) -> None:
    """One more try at the item's alert: its attempts and last error in the payload, its slack_ts once it went."""
    row: dict[str, Any] = {"item_id": item_id, "payload": {
        **payload, "alert_attempts": int(payload.get("alert_attempts") or 0) + 1, "alert_error": sent.get("error")}}
    if sent.get("ts"):
        row.update(slack_channel=sent.get("channel"), slack_ts=sent["ts"])
    ctx.store.upsert("hitl_items", [row])


def unsent(ctx: Context) -> list[dict]:
    """The open live items whose alert has not gone and is due another try (the module docstring's outbox)."""
    since = ctx.now - timedelta(days=OUTBOX_DAYS)
    daily = ctx.now.astimezone(UK).hour == OUTBOX_HOUR_UK
    out = []
    for r in holds.in_force(ctx.store):
        p = r.get("payload") if isinstance(r.get("payload"), Mapping) else {}
        created = utc(r.get("created_at"))
        if (r.get("slack_ts") or not p.get("outbox") or p.get("dry_run") is not False or created is None
                or created < since or p.get("alert_error") == notify.NO_TOKEN):
            continue
        if int(p.get("alert_attempts") or 0) >= OUTBOX_TRIES and not daily:
            continue
        out.append(r)
    return sorted(out, key=lambda r: (utc(r.get("created_at")), str(r["item_id"])))


def post_unsent(ctx: Context) -> dict:
    """Post the outbox's items again, in one message, at most once a run; live runs only."""
    if not ctx.live:
        return {}
    items = unsent(ctx)
    if not items:
        return {}
    fired = [_firing(r) for r in items]
    first = min(utc(r.get("created_at")) for r in items)
    text = (message(ctx, fired, []) + f"\n_Posted again: this alert may not have reached Slack when it was first "
            f"sent ({first.astimezone(UK):%a %d %b %H:%M} UK), so it may be a repeat._")
    sent = notify.alert(ctx, text)
    for r in items:
        _attempted(ctx, r["item_id"], r.get("payload") or {}, sent)
    log("kill_rules_reposted", run_id=ctx.run_id, items=[r["item_id"] for r in items], posted=bool(sent.get("ts")),
        error=sent.get("error"))
    return {"items": [r["item_id"] for r in items], "posted": bool(sent.get("ts")), "error": sent.get("error")}


def _record(ctx: Context, f: Firing) -> None:
    f.item_id = f.item_id or new_id()
    ctx.store.upsert("hitl_items", [{"item_id": f.item_id, "kind": KIND, "status": "open", "created_at": ctx.now,
                                     "payload": _payload(ctx, f)}])


def holding(ctx: Context, scope: str, target: str) -> bool:
    """A hold for (scope, target) is open or escalated (learn/early_warning.py asks before it holds; W3-4)."""
    return _Fired(ctx.store).open(scope, target)


def ever_fired(ctx: Context) -> set[str]:
    """The rules a kill_rule item was ever recorded for, in any status (learn/early_warning.py holds a warning once,
    as stop_rule_meetings fires once: _Fired.ever; W3-4 r1)."""
    fired = _Fired(ctx.store)
    return {str(fired._p(i).get("rule") or "") for i in fired.items} - {""}


def hold(ctx: Context, f: Firing) -> None:
    """Record a hold another rule decided (learn/early_warning.py with early_warning_pause = yes; W3-4), as a firing
    of this module is recorded: in any mode, the safe direction. Its alert is the caller's (posted once a week), so
    the item is no outbox entry: post_unsent never posts it again as a kill rule's."""
    f.item_id = f.item_id or new_id()
    ctx.store.upsert("hitl_items", [{"item_id": f.item_id, "kind": KIND, "status": "open", "created_at": ctx.now,
                                     "payload": {**_payload(ctx, f), "outbox": False}}])


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
RULE_WORDS = {"vitals": "Instantly's account checks"}
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


def _act(ctx: Context, firing: list[Firing], expired: list[dict]) -> tuple[dict[str, str], dict[str, Any]]:
    """Record each firing's hold, pause its mailboxes, and post one alert: (what each pause did, the alert sent)."""
    # The hold first, so the mailbox is out of every job even if the pause below fails.
    for f in firing:
        _record(ctx, f)
    paused: dict[str, str] = {}
    for f in firing:
        if f.action != PAUSE_MAILBOX:
            continue
        results = []
        for a in f.mailboxes:
            if ctx.live:
                paused[a], emptied = _pause(ctx, ctx.settings, a)
                if emptied and emptied not in f.campaigns:
                    f.campaigns.append(emptied)
            else:
                paused[a] = "dry-run: not paused in the sheet or Instantly"
            results.append(paused[a])
        f.outcome = "; ".join(dict.fromkeys(results))
        ctx.store.upsert("hitl_items", [{"item_id": f.item_id, "payload": _payload(ctx, f)}])
    alert: dict[str, Any] = {}
    if firing or expired:
        alert = notify.alert(ctx, message(ctx, firing, expired))
        for f in firing:  # slack_ts once it went; otherwise the outbox posts it again next run
            _attempted(ctx, f.item_id, _payload(ctx, f), alert)
    return paused, alert


def _fired_list(firing: list[Firing]) -> list[dict]:
    return [{"rule": f.rule, "target": f.target, "action": f.action, "reason": f.reason, "item_id": f.item_id,
             "until": f.until.isoformat() if f.until else None} for f in firing][:LIST_LIMIT]


def bounce_rules(ctx: Context, settings: Settings, ev: _Events, fired: _Fired, *, vitals: bool = True
                 ) -> tuple[list[Firing], str]:
    """The mailbox (with Instantly's vitals unless vitals=False), domain and source rules, over the window's sends."""
    firing, vitals_error = mailbox_rules(ctx, settings, ev, fired, vitals=vitals)
    already = {a for f in firing for a in f.mailboxes}
    firing += domain_rules(ctx, settings, ev, fired, already)
    firing += source_rules(ctx, ev, fired)
    return firing, vitals_error


def run(ctx: Context) -> dict:
    """The kill_rules job (JOB CONTRACT: run(ctx) -> summary)."""
    # First: an earlier run's pause whose alert never went. Nothing in the outbox may stop the brake below (a payload
    # edited by hand, a store error): it is logged and named in the summary, and the run goes on.
    reposted_error = None
    try:
        reposted = post_unsent(ctx)
    except GuardViolation:
        raise
    except Exception as exc:
        reposted, reposted_error = {}, f"{type(exc).__name__}: {str(exc)[:160]}"
        log("kill_rules_repost_failed", run_id=ctx.run_id, error=reposted_error)
    settings = holds.with_holds(ctx.store, ctx.settings)
    ev = _Events(ctx.store, ctx.now, ctx.now - timedelta(days=WINDOW_DAYS))
    fired_before = _Fired(ctx.store)
    expired = expire(ctx)

    firing, vitals_error = bounce_rules(ctx, settings, ev, fired_before)
    # The group and stop rules read every company's outcome, no window (RS P0-7). outcomes.per_company also reads the
    # enrolment stamps and exposure; if it (or either rule) fails, the bounce rules' firings above are still acted on
    # below, and the run then raises, so it is an error the failure-rate alert sees, not a lost pause (W2-3 r1).
    found: list[outcomes.Outcome] = []
    outcome_error: Exception | None = None
    try:
        found = outcomes.per_company(ctx)
        firing += group_rules(ctx, settings, found, fired_before)
        firing += stop_rules(ctx, settings, found, fired_before)
    except GuardViolation:
        raise
    except Exception as exc:
        outcome_error = exc
        log("kill_rules_outcome_rules_failed", run_id=ctx.run_id, error=f"{type(exc).__name__}: {str(exc)[:160]}")
    paused, alert = _act(ctx, firing, expired)
    summary = {
        "job": JOB, "dry_run": ctx.dry_run, "fired": _fired_list(firing), "paused": paused, "expired": expired,
        "checked": {"mailboxes": sum(m.status == ACTIVE for m in settings.mailboxes), "sends": len(ev.sends),
                    "bounces": len(ev.bounces), "companies": len(found)},
        "holds_in_force": len(holds.in_force(ctx.store)),
        "vitals_error": vitals_error or None,
        "alert": alert or None,
        "reposted": reposted or None,
        "reposted_error": reposted_error,
        "outcome_rules_error": f"{type(outcome_error).__name__}: {str(outcome_error)[:160]}" if outcome_error else None,
    }
    log("kill_rules_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "fired"},
        fired=[f.rule for f in firing])
    if outcome_error is not None:
        raise outcome_error  # the bounce rules' pauses above stand; the group and stop rules run again next hour
    return summary


def after_bounce(ctx: Context) -> dict:
    """The bounce-rate rules (mailbox, domain, source), run by sync_outcomes at its end whenever it recorded a bounce
    (replies/outcomes.run): a mailbox over the limit is paused in that run, not at the next hourly kill_rules. No
    Instantly read (vitals stay hourly); the same holds, pauses and alert as the hourly run, which then finds the hold
    open and fires nothing new. Never raises (but a GuardViolation): the sync's own work is done by then, and the
    hourly run is the backstop."""
    try:
        settings = holds.with_holds(ctx.store, ctx.settings)
        ev = _Events(ctx.store, ctx.now, ctx.now - timedelta(days=WINDOW_DAYS))
        firing, _ = bounce_rules(ctx, settings, ev, _Fired(ctx.store), vitals=False)
        paused, alert = _act(ctx, firing, [])
    except GuardViolation:
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        log("kill_rules_after_bounce_failed", run_id=ctx.run_id, error=error)
        return {"ran": True, "error": error}
    out = {"ran": True, "fired": _fired_list(firing), "paused": paused, "alert": alert or None}
    log("kill_rules_after_bounce", run_id=ctx.run_id, fired=[f.rule for f in firing], paused=paused)
    return out


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
                     "`us-outbound sync` and `us-outbound campaigns ensure --fix --live` put it back on its sending list.")
    return {"item_id": item_id, "changed": ctx.live, "dry_run": ctx.dry_run, "rule": p.get("rule"),
            "action": p.get("action"), "target": p.get("target"), "next": next_step}
