"""The daily post's funnel sections (learn/daily_post.py): Found, Pipeline and To improve.

Harry, 2 Oct 2026: the daily report should carry "the number of emails sent, the number of companies
and contacts identified, the pipeline of companies to send to, the [un]subscribes, the replies, and
anything else important to surface to make the system better over time". Sends, replies and
unsubscribes were already in the post; this module adds the rest. It only reads the database, and
only in aggregate: no company or person is named here. (Its Approvals section, the Slack send approvals, went
with them: W2-1, 20 Oct 2026.)

Found, in the period daily_post.period sets:
  * companies: accounts first seen in the period (accounts.first_seen, which the front door sets,
    accounts.admit), by accounts.source (apollo, named) and by the tier each has now. No column
    records when an account was verified or first scored (last_scored moves on every nightly
    rescore), so the tiers are those of the new accounts as they stand at 09:00;
  * verified: the verify_accounts runs that started in the period, from their heartbeat summaries
    (heartbeats.detail.verified). The summary keeps no list, so this is a total, not by tier;
  * contacts: contacts created in the period (contacts.created_at, which pick_contacts sets) by email source and
    by role;
  * no suitable contact: accounts whose contact_pick fact in the period (contacts/pick.py) says
    none, with the top reasons.

Pipeline, now:
  * ready to send: enrol.candidates as the enrol job calls it (verified, Priority, Standard or
    Control, domain not suppressed or a partner, industry on, a sendable contact, not enrolled
    before); by tier, and by the Focus
    tab's groups (enrol/focus.py), or by industry group when the tab is empty;
  * supply: ready accounts ÷ today's number before the ready accounts limit it (the smaller of
    the weekly target's share for today and sending capacity, limits.py); on a day with neither
    (a weekend, or a week whose target is met), the weekly target's steady pace;
  * waiting for a contact: pick.waiting, and of those, the accounts where nobody suitable was
    found, searched again after pick.RETRY_DAYS;
  * waiting for verification: accounts new or queued and not Excluded or Held (the sourcing job's
    queue rule), and of those, the ones whose doubtful Apollo facts wait for the hand-check
    (verify.open_doubts);
  * parked: accounts out of the queue only while a setting keeps them out (their industry switched off, or the
    label check failing under one label list; W1-4), never in the waiting counts: their own line, when there are any;
  * in sequence: per account, an enrolled contact with a step still to come (capacity.step_days,
    the send forecast's own dates); finished, all four sent; stopped, by a reply, bounce or
    unsubscribe, or the account no longer enrolled (capacity.stopped_contacts).

To improve: the last LEARN_DAYS days, at most MAX_IMPROVE lines, each only once there is enough
data to say something, and as counts, not conclusions:
  * replies, opener against holdout (contacts.opener_arm), once each arm has MIN_PER_ARM contacts
    emailed (step 1 sent and not bounced); email 1's personal subject against the Copy row's
    (contacts.subject_arm; Harry, 5 Oct 2026) the same way; by angle and by copy version, for those that have
    MIN_PER_ARM each, once two can be compared. A human reply is any but out-of-office, as the
    kill rules count it, at any time after step 1, so recent sends have had less time to reply;
  * bounces by email source (kill_rules.SOURCES): of the contacts sent step 1, how many bounced, for a
    source with MIN_PER_ARM;
  * data gaps: verified accounts with no size band, accounts waiting for verification with none,
    the main reason pick_contacts found nobody suitable, and a pointer to the page reader's own
    section rather than its numbers again;
What cannot be compared yet goes in one last line, with how many it needs.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from us_outbound import fmt, labels, verify
from us_outbound.clients.db import Store
from us_outbound.clients.guard import WARM_REPLY_CLASSES
from us_outbound.contacts import pick
from us_outbound.context import ET, Context
from us_outbound.enrol import capacity, focus, queue
from us_outbound.enrol.enrol import Candidate
from us_outbound.enrol.openers import HOLDOUT, OPENER
from us_outbound.enrol.render import COPY_SUBJECT, PERSONAL_SUBJECT
from us_outbound.facts import newest_by
from us_outbound.learn import kill_rules
from us_outbound.limits import Limits
from us_outbound.replies import kinds
from us_outbound.scoring import tiers
from us_outbound.settings.model import TIERS, Settings
from us_outbound.timeparse import utc, utc_strict

OPEN_STATUSES = ("open", "escalated")  # hitl_items waiting for a person

# To improve.
LEARN_DAYS = 90  # the window it reads: recent enough to tune on, long enough to reach the minimums below
MAX_IMPROVE = 6  # lines at most, the "too early" line last
MIN_PER_ARM = 50  # contacts emailed per arm (opener, holdout; personal, copy; an angle, a copy version, an email source)
TOP = 3  # values named per list

QUEUE_OUT_TIERS = tiers.OUT_OF_QUEUE_TIERS  # not in the queue
NOT_SCORED = "not scored yet"
IN_SEQUENCE, FINISHED, STOPPED = "in_sequence", "finished", "stopped"
_TOP_REVEALS = re.compile(r"^no sendable email among the top \d+ \(.*\)$")
GAP_MIN_WAITING = 5  # companies still being checked with no size: fewer is the hand-check's to settle, not news
SOURCES_POINTER = " The page reader's coverage is under Sources."  # the full post's; the short post has no Sources


# -- small helpers ---------------------------------------------------------------------------------


def _in(v: Any, start: datetime, end: datetime) -> bool:
    t = utc(v)
    return t is not None and start <= t < end


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


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
    open_items: list[dict] = field(default_factory=list)  # hitl_items open or escalated, every kind
    by_account: dict[str, dict] = field(default_factory=dict)
    by_contact: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_account = {str(a.get("account_id")): a for a in self.accounts}
        self.by_contact = {str(c.get("contact_id")): c for c in self.contacts}

    @classmethod
    def load(cls, ctx: Context) -> Rows:
        store = ctx.store
        return cls(store.select("accounts"), store.select("contacts"), pick.no_contact(store, ctx.now),
                   open_items=store.select("hitl_items", {"status": list(OPEN_STATUSES)}))


def headline(short: str, nums: Mapping[str, Any]) -> str:
    """"Yesterday: 18 sent · 2 replies (1 positive) · 0 unsubscribes · ... · 140 ready to send"."""
    return (f"{short}: {nums.get('sent', 0)} sent · {fmt.plural(nums.get('replies', 0), 'reply', 'replies')} "
            f"({nums.get('positive', 0)} positive) · {fmt.plural(nums.get('unsubscribed', 0), 'unsubscribe')} · "
            f"{fmt.plural(nums.get('found_companies', 0), 'company', 'companies')}, "
            f"{fmt.plural(nums.get('found_contacts', 0), 'contact')} found · {nums.get('ready_to_send', 0)} ready to send")


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
    def counted(e: Mapping[str, Any]) -> bool:
        t = utc_strict(e.get("observed_at"))
        return bool(e.get("account_id")) and isinstance(e.get("value"), Mapping) and start <= t < end

    latest = newest_by(store.select("signal_events", {"source": pick.SOURCE, "fact": pick.OUTCOME_FACT}),
                       lambda e: str(e["account_id"]), where=counted, dated=True)
    return {aid: reason(e["value"].get("reason")) for aid, e in latest.items()
            if e["value"].get("outcome") == pick.NO_CONTACT}


def found(ctx: Context, rows: Rows, start: datetime, end: datetime, label: str) -> tuple[list[str], dict[str, Any]]:
    """The Found section: companies and contacts identified in the period."""
    new = [a for a in rows.accounts if _in(a.get("first_seen"), start, end)]
    by_source = Counter(str(a.get("source") or "unknown") for a in new)
    by_tier = Counter(str(a.get("tier") or NOT_SCORED) for a in new)
    lines = [f"*Found* · {label}"]
    line = f"  Companies: {len(new)} new"
    if new:
        # A tier from before W2-2 (Priority, Standard, Control) is named as it is until a rescore writes Queue.
        order = (*TIERS, *tiers.LEGACY_QUEUE_TIERS, NOT_SCORED)
        line += f" ({fmt.counts(by_source)}); tiers now: {fmt.counts(by_tier, order=order)}"
    lines.append(line)
    verified = verified_in(ctx.store, start, end)
    if verified is None:
        lines.append("  Verified: 0 (verify_accounts did not run)")
    else:
        lines.append(f"  Verified: {verified}")
    people = [c for c in rows.contacts if _in(c.get("created_at"), start, end)]
    by_email = Counter(str(c.get("email_source") or "unknown") for c in people)
    by_role = Counter(str(c.get("role") or "no role") for c in people)
    line = f"  Contacts: {len(people)} new"
    if people:
        line += f" ({fmt.counts(by_email)}; {fmt.counts(by_role)})"
    lines.append(line)
    nobody = no_contact_in(ctx.store, start, end)
    if nobody:
        reasons = fmt.counts(Counter(nobody.values()), limit=TOP)
        lines.append(f"  No suitable contact: {fmt.plural(len(nobody), 'account')} ({reasons})")
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
        start = utc(c.get("enrolled_at"))
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


def ready_groups(ready: Sequence[Candidate], settings: Settings) -> str:
    """"Technology & Startups 39, Legal Teams 12, …": the ready accounts by Focus share, else industry group."""
    return _groups(ready, settings).split(": ", 1)[1]


def _groups(ready: Sequence[Candidate], settings: Settings) -> str:
    """Ready accounts by the Focus tab's groups (each listed, other last), else the largest industry groups."""
    if settings.focus:
        got = Counter(focus.key_for(c.account, settings) for c in ready)
        names = [f.industry_group for f in sorted(settings.focus, key=lambda f: -f.share)]
        if focus.OTHER in focus.shares(settings) or got.get(focus.OTHER):
            names.append(focus.OTHER)
        return "Focus: " + ", ".join(f"{g} {got.get(g, 0)}" for g in names)
    got = Counter(settings.industry_group_of(c.account) or "no group" for c in ready)
    top = fmt.counts(got, limit=TOP)
    rest = sum(got.values()) - sum(n for _, n in got.most_common(TOP))
    return "By group: " + top + (f", others {rest}" if rest > 0 else "")


def parked_line(parked: Mapping[str, int]) -> str:
    """"Parked (industry off): 4" or "Parked: 5 (industry off 4, label check failing 1)"."""
    off, failing = parked.get(labels.INDUSTRY_OFF, 0), parked.get(labels.FAILED_CODE, 0)
    total = sum(parked.values())
    if total == off:
        return f"Parked (industry off): {total:,}"
    parts = [f"industry off {off:,}" if off else "", f"label check failing {failing:,}" if failing else "",
             f"other {total - off - failing:,}" if total - off - failing else ""]
    return f"Parked: {total:,} ({', '.join(p for p in parts if p)})"


def pipeline(ctx: Context, rows: Rows, ready: Sequence[Candidate], lim: Limits) -> tuple[list[str], dict[str, Any]]:
    """The Pipeline section: what is ready to send now, and what stands behind it."""
    s = ctx.settings
    # The tiers went with W2-2 (Harry's D7): the split that matters now is the random arm pick drew (accounts.arm).
    drawn = sum(c.account.get("arm") == queue.RANDOM for c in ready)
    line = f"  Ready to send: {len(ready)} ({drawn} drawn for the random arm)"
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
    lines.append(f"  Waiting for verification: {len(unverified)}" + (f" ({'; '.join(notes)})" if notes else ""))
    parked = Counter(str(a.get("status_reason") or labels.INDUSTRY_OFF) for a in rows.accounts
                     if a.get("status") == labels.PARKED)
    if parked:
        lines.append(f"  {parked_line(parked)}")
    seq = sequence_states(ctx, rows.contacts)
    lines.append(f"  In sequence: {fmt.plural(seq[IN_SEQUENCE], 'account')} with follow-ups to come · finished {seq[FINISHED]}"
                 f" · stopped {seq[STOPPED]} (reply, bounce or unsubscribe)")
    nums = {"ready_to_send": len(ready), "ready_random_arm": drawn,
            "supply_days": supply, "supply_pace": pace, "waiting_for_contact": len(need), "no_suitable_contact": stuck,
            "waiting_for_verification": len(unverified), "doubtful_facts": doubtful, "in_sequence": seq[IN_SEQUENCE],
            "parked": sum(parked.values()), "parked_by_reason": dict(parked),
            "finished": seq[FINISHED], "stopped": seq[STOPPED]}
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
            t = utc(r.get("occurred_at"))
            if cid not in step1 or t is None or t < step1[cid].at or not kinds.is_human(r.get("reply_class")):
                continue
            replied.add(cid)
            if _lower(r.get("reply_class")) in WARM_REPLY_CLASSES:
                warm.add(cid)
        return cls(step1, {cid for cid, s in step1.items() if not s.bounced}, replied, warm)


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


def _arms(m: _Emailed, rows: Rows, key: str, a: str, b: str, label: str, early: list[str]) -> list[str]:
    """Replies in arm a against arm b of the contacts column key, once each has MIN_PER_ARM emailed."""
    arm: Counter[str] = Counter()
    back: Counter[str] = Counter()
    warm: Counter[str] = Counter()
    for cid in m.emailed:
        v = _lower(rows.by_contact.get(cid, {}).get(key))
        arm[v] += 1
        back[v] += cid in m.replied
        warm[v] += cid in m.warm
    if arm[a] >= MIN_PER_ARM and arm[b] >= MIN_PER_ARM:
        return [f"  Replies, {label}: {back[a]} of {arm[a]} ({fmt.pct(back[a], arm[a])}) "
                f"vs {back[b]} of {arm[b]} ({fmt.pct(back[b], arm[b])}); "
                f"interested {warm[a]} vs {warm[b]}. Early counts, not proof."]
    early.append(f"{label}, {arm[a]} and {arm[b]} of {MIN_PER_ARM} emailed each")
    return []


def _replies(m: _Emailed, rows: Rows, early: list[str]) -> tuple[list[str], list[str]]:
    """(opener against holdout and the personal subject against the Copy row's, by angle and copy version)."""
    first = _arms(m, rows, "opener_arm", OPENER, HOLDOUT, "personal opening line vs none (the holdout)", early)
    # Harry, 5 Oct 2026: email 1's personal subject (General email1_subject) against the Copy row's s1_subject.
    first += _arms(m, rows, "subject_arm", PERSONAL_SUBJECT, COPY_SUBJECT, "personal subject vs the Copy tab's", early)
    parts = []
    for key, label in (("angle", "angle"), ("copy_version", "Copy tab version")):
        cut = _cut(m, rows, key)
        if len(cut) >= 2:
            parts.append(f"by {label}: " + ", ".join(f"{v} {r} of {n}" for v, n, r in cut[:TOP]))
    if parts:
        return first, ["  Replies " + "; ".join(parts) + "."]
    early.append(f"angles and Copy tab versions, {MIN_PER_ARM} emailed each, two at least")
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
        early.append(f"bounces by where the address came from, {max(sent[src] for src in kill_rules.SOURCES)} of "
                     f"{MIN_PER_ARM} emailed")
        return []
    parts = [f"{src.capitalize()} {bounced[src]} of {sent[src]} ({fmt.pct(bounced[src], sent[src])})" for src in big]
    parts += [f"{src.capitalize()} {sent[src]} emailed, too few to judge" for src in kill_rules.SOURCES
              if 0 < sent[src] < MIN_PER_ARM]
    return [f"  Bounces by where the address came from: {', '.join(parts)}; a source over "
            f"{kill_rules.BOUNCE_RATE:.0%} is paused automatically."]


def _gaps(rows: Rows) -> list[str]:
    verified = [a for a in rows.accounts if a.get("status") == verify.VERIFIED]
    waiting = [a for a in rows.accounts if a.get("status") in verify.WAITING_STATUSES and a.get("tier") not in QUEUE_OUT_TIERS]
    no_band_v = sum(1 for a in verified if not a.get("size_band"))
    no_band_w = sum(1 for a in waiting if not a.get("size_band"))
    ids = {str(a.get("account_id")) for a in verified}
    nobody = Counter(reason(r) for aid, r in rows.no_contact.items() if aid in ids)
    out = []
    # Only gaps that change what is emailed (Harry, 14 Oct 2026: "no company size at 0 of 414" was noise).
    if no_band_v or no_band_w >= GAP_MIN_WAITING:
        out.append(f"  No company size from Apollo: {no_band_v} of {len(verified)} companies ready to email and "
                   f"{no_band_w} still being checked. A size missing on a ready company means its contact is chosen "
                   "without it; type it on the Overrides tab (field employees) if it matters.")
    if nobody:
        top, n = min(nobody.items(), key=lambda kv: (-kv[1], kv[0]))
        k = sum(nobody.values())
        out.append(f"  {fmt.plural(k, 'company', 'companies')} ready to email {'has' if k == 1 else 'have'} nobody we "
                   f"can write to, so {'it is' if k == 1 else 'they are'} skipped (most often: {top}, {n}). Nothing to "
                   "do unless this grows: to widen the search, add job titles on the Roles tab.")
    return out


def _review_ready(ctx: Context) -> list[str]:
    """On Mondays, once enough companies have been emailed: the signal review is worth reading (learn/signal_review.py)."""
    from us_outbound.learn import signal_review

    if ctx.today_uk().weekday() != 0:
        return []
    line = signal_review.ready(ctx)
    return [line] if line else []


def to_improve(ctx: Context, rows: Rows) -> tuple[list[str], dict[str, Any]]:
    """The To improve section: what the data says to tune, once there is enough of it."""
    since = ctx.now - timedelta(days=LEARN_DAYS)
    early: list[str] = []
    m = _Emailed.read(ctx, since)
    if m.step1:
        opener, cuts = _replies(m, rows, early)
        bounces = _bounces(m, rows, early)
    else:
        opener, cuts, bounces = [], [], []
        early.append(f"no email sent in the last {LEARN_DAYS} days")
    # In this order, so that past MAX_IMPROVE the cuts by angle and copy version go first, then "too early".
    body = [*_review_ready(ctx), *opener, *bounces, *_gaps(rows), *cuts]
    if early:
        body.append("  Too early to compare: " + "; ".join(early) + ".")
    body = body[:MAX_IMPROVE]
    nums = {"emailed_in_window": len(m.emailed), "improve_lines": len(body)}
    return [f"*To improve* · last {LEARN_DAYS} days", *(body or ["  Nothing to flag."])], nums
