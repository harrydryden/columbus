"""The daily post's funnel sections (learn/daily_post.py): Approvals, Found, Pipeline and To improve.

Harry, 2 Oct 2026: the daily report should carry "the number of emails sent, the number of companies
and contacts identified, the pipeline of companies to send to, the [un]subscribes, the replies, and
anything else important to surface to make the system better over time". Sends, replies and
unsubscribes were already in the post; this module adds the rest. It only reads the database, and
only in aggregate: no company or person is named here.

Approvals: the Slack send approvals, where General auto_send = no makes every email wait for an
approver's ✅. It is read only through the contract the send-approval build writes, so it works
before that build lands and says nothing until it does:
  * hitl_items, kind send_approval: status open (payload.state waiting, rejected or editing),
    sending or handled; created_at, handled_at; payload outcome, owner, copy_version, industry,
    industry_group, role, tier, opener_source, edited and send_day;
  * events, type send_approval: approval is approved, approved_edited, contact_rejected,
    company_rejected, expired or blocked, with approved_by, account_id, contact_id and occurred_at.
For the period: approved (and how many after an edit), contact declined, company dropped, expired
and blocked; how many wait now, by state, and how long the oldest has waited; and auto_send, read
with getattr from the General tab when the settings carry the key (this module never adds it).
With no key, no item and no event, the section is left out.

Found, in the period daily_post.period sets:
  * companies: accounts first seen in the period (accounts.first_seen, which the front door sets,
    accounts.admit), by accounts.source (apollo, named) and by the tier each has now. No column
    records when an account was verified or first scored (last_scored moves on every nightly
    rescore), so the tiers are those of the new accounts as they stand at 09:00;
  * verified: the verify_accounts runs that started in the period, from their heartbeat summaries
    (heartbeats.detail.verified). The summary keeps no list, so this is a total, not by tier;
  * contacts: contacts created in the period (contacts.created_at, which pick_contacts and the
    Clay waterfall set) by email source (apollo, clay) and by role;
  * no suitable contact: accounts whose contact_pick fact in the period (contacts/pick.py) says
    none, with the top reasons.

Pipeline, now:
  * ready to send: enrol.candidates, the enrol job's own rule (verified, Priority, Standard or
    Control, domain not suppressed or a partner, industry on, a sendable contact, not enrolled
    before), less the accounts with a send approval open or sending; by tier, and by the Focus
    tab's groups (enrol/focus.py), or by industry group when the tab is empty;
  * supply: ready accounts ÷ today's number before the ready accounts limit it (the smaller of
    the weekly target's share for today and sending capacity, limits.py); on a day with neither
    (a weekend, or a week whose target is met), the weekly target's steady pace;
  * waiting for a contact: pick.waiting, and of those, the accounts where nobody suitable was
    found, searched again after pick.RETRY_DAYS;
  * waiting for verification: accounts new or queued and not Excluded or Held (the sourcing job's
    queue rule), and of those, the ones whose doubtful Apollo facts wait for the hand-check
    (verify.open_doubts);
  * in sequence: per account, an enrolled contact with a step still to come (capacity.step_days,
    the send forecast's own dates); finished, all four sent; stopped, by a reply, bounce or
    unsubscribe, or the account no longer enrolled (capacity.stopped_contacts).

To improve: the last LEARN_DAYS days, at most MAX_IMPROVE lines, each only once there is enough
data to say something, and as counts, not conclusions:
  * where ❌ concentrates: send approvals declined (contact or company) by industry group, role and
    opener source, once there are MIN_DECISIONS decisions and MIN_DECLINES declines; a sign that
    targeting or scoring is off there;
  * copy rows edited before approval, each named once edited MIN_EDITS times: fix them on the Copy tab;
  * replies, opener against holdout (contacts.opener_arm), once each arm has MIN_PER_ARM contacts
    emailed (step 1 sent and not bounced); by angle and by copy version, for those that have
    MIN_PER_ARM each, once two can be compared. A human reply is any but out-of-office, as the
    kill rules count it, at any time after step 1, so recent sends have had less time to reply;
  * bounces by email source (apollo, clay): of the contacts sent step 1, how many bounced, for a
    source with MIN_PER_ARM;
  * data gaps: verified accounts with no size band, accounts waiting for verification with none,
    the main reason pick_contacts found nobody suitable, and a pointer to the page reader's own
    section rather than its numbers again;
  * the tier mix of this month's queue when a tier is outside 5 to 40% (score.tier_share, the
    score job's own check), as after the first live sourcing run (2 Oct 2026: 425 of 432 Control).
What cannot be compared yet goes in one last line, with how many it needs.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from us_outbound import verify
from us_outbound.clients.db import Store
from us_outbound.clients.guard import WARM_REPLY_CLASSES
from us_outbound.contacts import pick
from us_outbound.context import ET, Context
from us_outbound.enrol import capacity, focus, queue
from us_outbound.enrol.enrol import Candidate
from us_outbound.enrol.openers import HOLDOUT, OPENER
from us_outbound.learn import kill_rules
from us_outbound.limits import Limits
from us_outbound.scoring import score, tiers
from us_outbound.settings.model import CLAY_REQUIRED, TIERS, Settings

# The send-approval contract (hitl_items.kind and events.type).
KIND = "send_approval"
OPEN_STATUSES = ("open", "escalated")  # waiting for an approver; payload.state says how
SENDING = "sending"  # approved, going out now
APPROVED, EDITED = "approved", "approved_edited"
CONTACT_REJECTED, COMPANY_REJECTED = "contact_rejected", "company_rejected"
EXPIRED, BLOCKED = "expired", "blocked"
DECLINED = (CONTACT_REJECTED, COMPANY_REJECTED)
DECIDED = (APPROVED, EDITED, *DECLINED)
NOT_HUMAN = "out_of_office"  # every other reply class is a person writing back (kill_rules)

# To improve.
LEARN_DAYS = 90  # the window it reads: recent enough to tune on, long enough to reach the minimums below
MAX_IMPROVE = 6  # lines at most, the "too early" line last
MIN_DECISIONS = 20  # send-approval decisions before naming where declines concentrate
MIN_DECLINES = 3  # ... and declines among them
MIN_EDITS = 3  # edits before approval before a copy row is named
MIN_PER_ARM = 50  # contacts emailed per arm (opener, holdout; an angle, a copy version, an email source)
TOP = 3  # values named per list

QUEUE_OUT_TIERS = frozenset({tiers.EXCLUDED, tiers.HELD})  # not in the queue (sources/apollo_universe.py)
NOT_SCORED = "not scored yet"
IN_SEQUENCE, FINISHED, STOPPED = "in_sequence", "finished", "stopped"
_TOP_REVEALS = re.compile(r"^no sendable email among the top \d+ \(.*\)$")


# -- small helpers ---------------------------------------------------------------------------------


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    return None


def _in(v: Any, start: datetime, end: datetime) -> bool:
    t = _ts(v)
    return t is not None and start <= t < end


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    p = row.get("payload")
    return p if isinstance(p, Mapping) else {}


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _counts(c: Counter, limit: int | None = None) -> str:
    """"a 3, b 1": most first, then by name; the first `limit` only."""
    items = sorted(((k, v) for k, v in c.items() if v), key=lambda kv: (-kv[1], str(kv[0])))
    return ", ".join(f"{k} {v}" for k, v in items[:limit])


def _ordered(c: Counter, order: Sequence[str]) -> str:
    """"Priority 2, Control 5": in the given order, then any others; zeros left out."""
    keys = [k for k in order if c.get(k)] + sorted(k for k in c if k not in order and c[k])
    return ", ".join(f"{k} {c[k]}" for k in keys)


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "0%"


def _age(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return "under an hour" if hours < 1 else _n(hours, "hour")


def reason(text: Any) -> str:
    """A no-contact reason without its per-account detail, so that like reasons count together."""
    r = " ".join(str(text or "").split()) or "no reason recorded"
    return "no sendable email among the top candidates" if _TOP_REVEALS.match(r) else r


@dataclass
class Rows:
    """The rows several sections read, each loaded once."""

    accounts: list[dict]
    contacts: list[dict]
    no_contact: dict[str, str]  # account_id -> why nobody suitable was found (pick.no_contact)
    by_account: dict[str, dict] = field(default_factory=dict)
    by_contact: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_account = {str(a.get("account_id")): a for a in self.accounts}
        self.by_contact = {str(c.get("contact_id")): c for c in self.contacts}

    @classmethod
    def load(cls, ctx: Context) -> Rows:
        return cls(ctx.store.select("accounts"), ctx.store.select("contacts"), pick.no_contact(ctx.store, ctx.now))


def awaiting_approval(store: Store) -> set[str]:
    """Accounts with a send approval open or sending: the enrol job has proposed them already."""
    return {str(i.get("account_id")) for i in store.select("hitl_items", {"kind": KIND})
            if i.get("status") in (*OPEN_STATUSES, SENDING) and i.get("account_id")}


def headline(short: str, nums: Mapping[str, Any]) -> str:
    """"Yesterday: 18 sent · 2 replies (1 positive) · 0 unsubscribes · ... · 140 ready to send"."""
    return (f"{short}: {nums.get('sent', 0)} sent · {_n(nums.get('replies', 0), 'reply', 'replies')} "
            f"({nums.get('positive', 0)} positive) · {_n(nums.get('unsubscribed', 0), 'unsubscribe')} · "
            f"{_n(nums.get('found_companies', 0), 'company', 'companies')}, "
            f"{_n(nums.get('found_contacts', 0), 'contact')} found · {nums.get('ready_to_send', 0)} ready to send")


# -- Approvals -------------------------------------------------------------------------------------


def auto_send(settings: Settings) -> bool | None:
    """General auto_send, if the settings carry the key; None when they do not, or it reads as neither."""
    v = getattr(settings.general, "auto_send", None)
    if v is None or isinstance(v, bool):
        return v
    t = _lower(v)
    if t in ("yes", "true", "1", "on"):
        return True
    if t in ("no", "false", "0", "off"):
        return False
    return None


def approvals(ctx: Context, start: datetime, end: datetime) -> tuple[list[str], dict[str, Any]]:
    """The Approvals section and its numbers; ([], {}) before send approvals exist."""
    items = ctx.store.select("hitl_items", {"kind": KIND})
    events = ctx.store.select("events", {"type": KIND})
    auto = auto_send(ctx.settings)
    if auto is None and not items and not events:
        return [], {}
    state = ("auto_send: not set" if auto is None else
             "auto_send: yes, so emails go without a ✅" if auto else
             "auto_send: no, so every email waits for a ✅ in Slack")
    got = Counter(str(e.get("approval") or "") for e in events if _in(e.get("occurred_at"), start, end))
    approved = got[APPROVED] + got[EDITED]
    lines = [f"*Approvals* · {state}",
             f"  Approved {approved}" + (f" ({got[EDITED]} after an edit)" if got[EDITED] else "")
             + f" · contact declined {got[CONTACT_REJECTED]} · company dropped {got[COMPANY_REJECTED]}"
             f" · expired {got[EXPIRED]} · blocked {got[BLOCKED]}"]
    waiting = [i for i in items if i.get("status") in OPEN_STATUSES]
    sending = sum(1 for i in items if i.get("status") == SENDING)
    also = f" · sending {sending}" if sending else ""
    if waiting:
        states = Counter(str(_payload(i).get("state") or "waiting") for i in waiting)
        oldest = min((t for i in waiting if (t := _ts(i.get("created_at"))) is not None), default=None)
        age = f"; the oldest has waited {_age(ctx.now - oldest)}" if oldest else ""
        lines.append(f"  Waiting now: {len(waiting)} ({_counts(states)}){age}{also}.")
    else:
        lines.append(f"  Waiting now: none{also}.")
    nums = {"auto_send": auto, "approved": approved, "approved_edited": got[EDITED],
            "contact_declined": got[CONTACT_REJECTED], "company_dropped": got[COMPANY_REJECTED],
            "approvals_expired": got[EXPIRED], "approvals_blocked": got[BLOCKED],
            "approvals_waiting": len(waiting), "approvals_sending": sending}
    return lines, nums


# -- Found -----------------------------------------------------------------------------------------


def verified_in(store: Store, start: datetime, end: datetime) -> int | None:
    """Accounts verify_accounts verified in runs that started in the period; None when none ran."""
    runs = [r.get("detail") for r in store.select("heartbeats", {"job": verify.JOB, "status": "ok"})
            if _in(r.get("started_at"), start, end)]
    runs = [d for d in runs if isinstance(d, Mapping)]
    if not runs:
        return None
    total = 0
    for d in runs:
        try:
            total += int(d.get("verified") or 0)
        except (TypeError, ValueError):
            continue
    return total


def no_contact_in(store: Store, start: datetime, end: datetime) -> dict[str, str]:
    """account_id -> the reason, for accounts whose latest contact_pick fact in the period found nobody."""
    latest: dict[str, tuple[datetime, Mapping]] = {}
    for e in store.select("signal_events", {"source": pick.SOURCE, "fact": pick.OUTCOME_FACT}):
        t, value, aid = _ts(e.get("observed_at")), e.get("value"), str(e.get("account_id") or "")
        if t is None or not aid or not isinstance(value, Mapping) or not start <= t < end:
            continue
        if aid not in latest or t >= latest[aid][0]:
            latest[aid] = (t, value)
    return {aid: reason(v.get("reason")) for aid, (_, v) in latest.items() if v.get("outcome") == pick.NO_CONTACT}


def found(ctx: Context, rows: Rows, start: datetime, end: datetime, label: str) -> tuple[list[str], dict[str, Any]]:
    """The Found section: companies and contacts identified in the period."""
    new = [a for a in rows.accounts if _in(a.get("first_seen"), start, end)]
    by_source = Counter(str(a.get("source") or "unknown") for a in new)
    by_tier = Counter(str(a.get("tier") or NOT_SCORED) for a in new)
    lines = [f"*Found* · {label}"]
    line = f"  Companies: {len(new)} new"
    if new:
        line += f" ({_counts(by_source)}); tiers now: {_ordered(by_tier, (*TIERS, NOT_SCORED))}"
    lines.append(line)
    verified = verified_in(ctx.store, start, end)
    if ctx.settings.general.clay_verification == CLAY_REQUIRED:
        lines.append(f"  Verified: {verified or 0} (clay_verification is required, so accounts wait for Clay)")
    elif verified is None:
        lines.append("  Verified: 0 (verify_accounts did not run)")
    else:
        lines.append(f"  Verified: {verified}")
    people = [c for c in rows.contacts if _in(c.get("created_at"), start, end)]
    by_email = Counter(str(c.get("email_source") or "unknown") for c in people)
    by_role = Counter(str(c.get("role") or "no role") for c in people)
    line = f"  Contacts: {len(people)} new"
    if people:
        line += f" ({_counts(by_email)}; {_counts(by_role)})"
    lines.append(line)
    nobody = no_contact_in(ctx.store, start, end)
    if nobody:
        lines.append(f"  No suitable contact: {_n(len(nobody), 'account')} ({_counts(Counter(nobody.values()), TOP)})")
    nums = {"found_companies": len(new), "found_by_source": dict(by_source), "found_by_tier": dict(by_tier),
            "verified_in_period": verified, "found_contacts": len(people), "found_contacts_by_source": dict(by_email),
            "no_contact_found": len(nobody)}
    return lines, nums


# -- Pipeline --------------------------------------------------------------------------------------


def steady_pace(settings: Settings) -> int:
    """The weekly target's pace: weekly_enrol_cap ÷ the send days a week, rounded up (pick.lookahead's pace)."""
    days = max(1, len(settings.general.send_window.days))
    return math.ceil(settings.general.weekly_enrol_cap / days)


def sequence_states(ctx: Context, contacts: Iterable[Mapping[str, Any]]) -> Counter[str]:
    """Accounts in sequence, finished and stopped, from their enrolled contacts (enrol/capacity.py's rules)."""
    stopped = capacity.stopped_contacts(ctx.store)
    today = ctx.now_et().date()
    rank = {IN_SEQUENCE: 0, FINISHED: 1, STOPPED: 2}
    state: dict[str, str] = {}
    for c in contacts:
        start = _ts(c.get("enrolled_at"))
        if start is None:
            continue
        if str(c.get("contact_id")) in stopped:
            st = STOPPED
        else:
            days = capacity.step_days(start.astimezone(ET).date(), ctx.settings)
            st = IN_SEQUENCE if days and days[-1] >= today else FINISHED
        aid = str(c.get("account_id") or c.get("contact_id"))
        if aid not in state or rank[st] < rank[state[aid]]:
            state[aid] = st
    return Counter(state.values())


def _groups(ready: Sequence[Candidate], settings: Settings) -> str:
    """Ready accounts by the Focus tab's groups (each listed, other last), else the largest industry groups."""
    if settings.focus:
        got = Counter(focus.key_for(c.account, settings) for c in ready)
        names = [f.industry_group for f in sorted(settings.focus, key=lambda f: -f.share)]
        if focus.OTHER in focus.shares(settings) or got.get(focus.OTHER):
            names.append(focus.OTHER)
        return "Focus: " + ", ".join(f"{g} {got.get(g, 0)}" for g in names)
    got = Counter(settings.industry_group_of(c.account) or "no group" for c in ready)
    top = _counts(got, TOP)
    rest = sum(got.values()) - sum(n for _, n in got.most_common(TOP))
    return "By group: " + top + (f", others {rest}" if rest > 0 else "")


def pipeline(ctx: Context, rows: Rows, ready: Sequence[Candidate], awaiting: set[str],
             lim: Limits) -> tuple[list[str], dict[str, Any]]:
    """The Pipeline section: what is ready to send now, and what stands behind it."""
    s = ctx.settings
    tiers_ = Counter(str(c.account.get("tier")) for c in ready)
    line = f"  Ready to send: {len(ready)} (" + ", ".join(f"{t} {tiers_.get(t, 0)}" for t in queue.QUEUE_TIERS) + ")"
    if awaiting:
        line += f"; {len(awaiting)} more wait for a ✅"
    lines = ["*Pipeline* · now", line]
    if ready:
        lines.append(f"    {_groups(ready, s)}")
    pace = min(int(lim.terms.get("weekly_target") or 0), int(lim.terms.get("sending_capacity") or 0))
    pace = pace if pace > 0 else steady_pace(s)
    supply = round(len(ready) / pace, 1) if ready and pace > 0 else None
    lines.append(f"  Supply: {supply} send days at {pace} a day" if supply is not None else "  Supply: nothing ready to send")
    need, _ = pick.waiting(ctx)
    stuck = sum(1 for a in need if str(a.get("account_id")) in rows.no_contact)
    lines.append(f"  Waiting for a contact: {len(need)}" + (
        f" ({stuck} where nobody suitable was found; searched again after {pick.RETRY_DAYS} days)" if stuck else ""))
    unverified = [a for a in rows.accounts if a.get("status") in verify.WAITING_STATUSES and a.get("tier") not in QUEUE_OUT_TIERS]
    doubts = {str(d.get("account_id")) for d in verify.open_doubts(ctx)}
    doubtful = sum(1 for a in unverified if str(a.get("account_id")) in doubts)
    notes = []
    if doubtful:
        notes.append(f"{doubtful} with doubtful Apollo facts, for the hand-check")
    if s.general.clay_verification == CLAY_REQUIRED:
        notes.append("clay_verification is required, so they wait for Clay")
    lines.append(f"  Waiting for verification: {len(unverified)}" + (f" ({'; '.join(notes)})" if notes else ""))
    seq = sequence_states(ctx, rows.contacts)
    lines.append(f"  In sequence: {_n(seq[IN_SEQUENCE], 'account')} with follow-ups to come · finished {seq[FINISHED]}"
                 f" · stopped {seq[STOPPED]} (reply, bounce or unsubscribe)")
    nums = {"ready_to_send": len(ready), "ready_by_tier": dict(tiers_), "awaiting_approval": len(awaiting),
            "supply_days": supply, "supply_pace": pace, "waiting_for_contact": len(need), "no_suitable_contact": stuck,
            "waiting_for_verification": len(unverified), "in_sequence": seq[IN_SEQUENCE], "finished": seq[FINISHED],
            "stopped": seq[STOPPED]}
    return lines, nums


# -- To improve ------------------------------------------------------------------------------------


@dataclass
class _Emailed:
    """Step 1 in the window, per contact: who was emailed, who bounced, who replied."""

    step1: dict[str, kill_rules._Send]
    emailed: set[str]  # step 1 sent and not bounced
    replied: set[str]
    warm: set[str]

    @classmethod
    def read(cls, ctx: Context, since: datetime) -> _Emailed:
        ev = kill_rules._Events(ctx.store, ctx.now)
        step1: dict[str, kill_rules._Send] = {}
        for s in ev.sends:  # oldest first
            if s.step == 1 and s.contact_id and s.at >= since and s.contact_id not in step1:
                step1[s.contact_id] = s
        by_account = {s.account_id: cid for cid, s in step1.items() if s.account_id}
        replied: set[str] = set()
        warm: set[str] = set()
        for r in ev.replies:
            cid = str(r.get("contact_id") or "") or by_account.get(str(r.get("account_id") or ""), "")
            t = _ts(r.get("occurred_at"))
            if cid not in step1 or t is None or t < step1[cid].at or _lower(r.get("reply_class")) == NOT_HUMAN:
                continue
            replied.add(cid)
            if _lower(r.get("reply_class")) in WARM_REPLY_CLASSES:
                warm.add(cid)
        return cls(step1, {cid for cid, s in step1.items() if not s.bounced}, replied, warm)


def _approval_index(items: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """("contact" or "account", id) -> the payload of its latest send_approval item."""
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for i in sorted(items, key=lambda i: _ts(i.get("created_at")) or epoch):
        p = _payload(i)
        if i.get("contact_id"):
            out[("contact", str(i["contact_id"]))] = p
        if i.get("account_id"):
            out[("account", str(i["account_id"]))] = p
    return out


def _segment(e: Mapping[str, Any], key: str, index: Mapping[tuple[str, str], Mapping[str, Any]], rows: Rows,
             settings: Settings) -> str:
    """The event's industry_group, role, opener_source or copy_version: its item's payload, else the tables."""
    cid, aid = str(e.get("contact_id") or ""), str(e.get("account_id") or "")
    p = index.get(("contact", cid)) or index.get(("account", aid)) or {}
    v = str(p.get(key) or "").strip()
    if v:
        return v
    if key == "industry_group":
        return settings.industry_group_of(rows.by_account.get(aid, {}))
    return str(rows.by_contact.get(cid, {}).get(key) or "").strip()


def _declines(ctx: Context, rows: Rows, since: datetime, early: list[str]) -> list[str]:
    """Where ❌ concentrates, and the copy rows edited most before approval."""
    events = [e for e in ctx.store.select("events", {"type": KIND})
              if (t := _ts(e.get("occurred_at"))) is not None and t >= since and e.get("approval") in DECIDED]
    if not events:
        return []
    index = _approval_index(ctx.store.select("hitl_items", {"kind": KIND}))
    out: list[str] = []
    declined = [e for e in events if e.get("approval") in DECLINED]
    if len(events) < MIN_DECISIONS:
        early.append(f"❌ by segment, {len(events)} of {MIN_DECISIONS} decisions")
    elif len(declined) >= MIN_DECLINES:
        parts = []
        for key, label in (("industry_group", ""), ("role", ""), ("opener_source", "opener ")):
            total = Counter(_segment(e, key, index, rows, ctx.settings) for e in events)
            bad = Counter(_segment(e, key, index, rows, ctx.settings) for e in declined)
            bad.pop("", None)
            if bad:
                top = min(bad, key=lambda v: (-bad[v], -bad[v] / total[v], v))
                parts.append(f"{label}{top} {bad[top]} of {total[top]}")
        where = f", most in {', '.join(parts)}: check targeting and scoring there" if parts else ""
        out.append(f"  ❌ {len(declined)} of {len(events)} send approvals declined{where}.")
    approved = [e for e in events if e.get("approval") in (APPROVED, EDITED)]
    total = Counter(_segment(e, "copy_version", index, rows, ctx.settings) for e in approved)
    edits = Counter(_segment(e, "copy_version", index, rows, ctx.settings) for e in approved if e.get("approval") == EDITED)
    edits.pop("", None)
    most = [(v, n) for v, n in sorted(edits.items(), key=lambda kv: (-kv[1], kv[0])) if n >= MIN_EDITS][:TOP]
    if most:
        out.append("  Edited before approval: " + ", ".join(f"{v} {n} of {total[v]}" for v, n in most)
                   + ": fix the template on the Copy tab.")
    return out


def _cut(m: _Emailed, rows: Rows, key: str) -> list[tuple[str, int, int]]:
    """(value, emailed, replied) for the values with MIN_PER_ARM contacts emailed, most emailed first."""
    sent: Counter[str] = Counter()
    back: Counter[str] = Counter()
    for cid in m.emailed:
        v = str(rows.by_contact.get(cid, {}).get(key) or "").strip()
        if v:
            sent[v] += 1
            back[v] += cid in m.replied
    return [(v, n, back[v]) for v, n in sorted(sent.items(), key=lambda kv: (-kv[1], kv[0])) if n >= MIN_PER_ARM]


def _replies(m: _Emailed, rows: Rows, early: list[str]) -> tuple[list[str], list[str]]:
    """(opener against holdout, by angle and copy version)."""
    arm: Counter[str] = Counter()
    back: Counter[str] = Counter()
    warm: Counter[str] = Counter()
    for cid in m.emailed:
        a = _lower(rows.by_contact.get(cid, {}).get("opener_arm"))
        arm[a] += 1
        back[a] += cid in m.replied
        warm[a] += cid in m.warm
    first: list[str] = []
    if arm[OPENER] >= MIN_PER_ARM and arm[HOLDOUT] >= MIN_PER_ARM:
        first.append(f"  Replies, opener vs holdout: {back[OPENER]} of {arm[OPENER]} ({_pct(back[OPENER], arm[OPENER])}) "
                     f"vs {back[HOLDOUT]} of {arm[HOLDOUT]} ({_pct(back[HOLDOUT], arm[HOLDOUT])}); "
                     f"positive {warm[OPENER]} vs {warm[HOLDOUT]}. Counts, not conclusions.")
    else:
        early.append(f"opener vs holdout, {arm[OPENER]} and {arm[HOLDOUT]} of {MIN_PER_ARM} emailed each")
    parts = []
    for key, label in (("angle", "angle"), ("copy_version", "copy version")):
        cut = _cut(m, rows, key)
        if len(cut) >= 2:
            parts.append(f"by {label}: " + ", ".join(f"{v} {r} of {n}" for v, n, r in cut[:TOP]))
    if parts:
        return first, ["  Replies " + "; ".join(parts) + "."]
    early.append(f"angles and copy versions, {MIN_PER_ARM} emailed each, two at least")
    return first, []


def _bounces(m: _Emailed, rows: Rows, early: list[str]) -> list[str]:
    sent: Counter[str] = Counter()
    bounced: Counter[str] = Counter()
    for cid, s in m.step1.items():
        src = _lower(rows.by_contact.get(cid, {}).get("email_source")) or "unknown"
        sent[src] += 1
        bounced[src] += s.bounced
    big = [src for src in kill_rules.SOURCES if sent[src] >= MIN_PER_ARM]
    if not big:
        early.append(f"bounces by source, {max(sent[src] for src in kill_rules.SOURCES)} of {MIN_PER_ARM} emailed")
        return []
    parts = [f"{src} {bounced[src]} of {sent[src]} ({_pct(bounced[src], sent[src])})" for src in big]
    parts += [f"{src} {sent[src]} emailed, too few" for src in kill_rules.SOURCES if 0 < sent[src] < MIN_PER_ARM]
    return [f"  Bounces by email source: {', '.join(parts)}; the kill rule pauses a source over "
            f"{kill_rules.BOUNCE_RATE:.0%}."]


def _gaps(rows: Rows) -> list[str]:
    verified = [a for a in rows.accounts if a.get("status") == verify.VERIFIED]
    waiting = [a for a in rows.accounts if a.get("status") in verify.WAITING_STATUSES and a.get("tier") not in QUEUE_OUT_TIERS]
    no_band_v = sum(1 for a in verified if not a.get("size_band"))
    no_band_w = sum(1 for a in waiting if not a.get("size_band"))
    ids = {str(a.get("account_id")) for a in verified}
    nobody = Counter(reason(r) for aid, r in rows.no_contact.items() if aid in ids)
    parts = []
    if no_band_v or no_band_w:
        parts.append(f"no size band at {no_band_v} of {len(verified)} verified accounts ({_pct(no_band_v, len(verified))})"
                     f" and {no_band_w} of {len(waiting)} waiting for verification")
    if nobody:
        top, n = min(nobody.items(), key=lambda kv: (-kv[1], kv[0]))
        parts.append(f"nobody suitable at {_n(sum(nobody.values()), 'verified account')}, mostly {top} ({n})")
    if not parts:
        return []
    return ["  Data gaps: " + "; ".join(parts) + ". The page reader's coverage is under Sources."]


def _tier_mix(ctx: Context, rows: Rows) -> list[str]:
    check = score.tier_share(rows.accounts, ctx.today_uk())
    if not check or not check["off"]:
        return []
    mix = ", ".join(f"{t} {s:.0%}" for t, s in check["shares"].items())
    return [f"  Tier mix of this month's queue ({check['accounts']} accounts): {mix}; each should be "
            f"{score.SHARE_MIN:.0%} to {score.SHARE_MAX:.0%}: review the thresholds and signal weights."]


def to_improve(ctx: Context, rows: Rows) -> tuple[list[str], dict[str, Any]]:
    """The To improve section: what the data says to tune, once there is enough of it."""
    since = ctx.now - timedelta(days=LEARN_DAYS)
    early: list[str] = []
    approval_lines = _declines(ctx, rows, since, early)
    m = _Emailed.read(ctx, since)
    if m.step1:
        opener, cuts = _replies(m, rows, early)
        bounces = _bounces(m, rows, early)
    else:
        opener, cuts, bounces = [], [], []
        early.append(f"no email sent in the last {LEARN_DAYS} days")
    # In this order, so that past MAX_IMPROVE the cuts by angle and copy version go first, then "too early".
    body = [*approval_lines, *opener, *bounces, *_gaps(rows), *_tier_mix(ctx, rows), *cuts]
    if early:
        body.append("  Too early to compare: " + "; ".join(early) + ".")
    body = body[:MAX_IMPROVE]
    nums = {"emailed_in_window": len(m.emailed), "improve_lines": len(body)}
    return [f"*To improve* · last {LEARN_DAYS} days", *(body or ["  Nothing to flag."])], nums
