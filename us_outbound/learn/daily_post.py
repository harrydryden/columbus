"""daily_post, 09:00 UK Tuesday to Saturday (SPEC 9, 11 "Daily post"): one message on yesterday's outbound and what
waits for Harry.

Since 19 Oct 2026 (the metrics review: metrics-audit L1 and H3) it runs Tuesday to Saturday and each post covers the
day before only, the previous US send day: Saturday's covers Friday and Tuesday's covers Monday, so Friday's numbers
are no longer posted three times over a weekend and no day is in two posts. Weekend replies are in the Monday
readout's last week. Replies and positive replies are counted by company (distinct accounts with a human, or a
positive or referral, reply in the period), as every company-level rate counts them; the emails themselves are kept
as reply_emails. Monday's asks (the credit and spend asks, and the mailboxes to add three weeks ahead) are made by
the Monday readout (learn/readout.py, Mondays 08:30) since there is no Monday post, so an ask that comes due on a
Sunday is made on Monday, a day later than before.

Since 9 Oct 2026 (Harry: "a more readable pithier version of the daily report") the job posts the short post
(compact). Since 12 Oct 2026 (Harry: clear to anyone, even new to the system) its first line says whether anything
needs you today; the asks follow (from W3-1, 20 Oct 2026, three more after the replies, holds and hand-check: "Supply
stalled" when no company and no contact was found on the last 3 send days while 50 or more wait; "Capture drift" or
"Canary failed" from this week's reply-capture check; "Leads lost" when yesterday's enrolment was held by a gate or
lost leads to errors: watch_asks), numbered, each with where to go or the command to paste; then yesterday in a few
lines, today's number with what limits it and each sender's room, the pipeline, the one-line spend summary, and a
"Worth a look" block only for what needs a look (a mailbox bouncing BOUNCE_WATCH or more, a kill rule, a campaign
not sending, the To improve lines), ending with the areas checked that are fine. Every number is
gathered once (gather) and the full post below is kept as it was, for `us-outbound report daily --full`; the summary
numbers in the heartbeat are the same either way. What follows describes the full post.

Harry, 2 Oct 2026: "the number of emails sent, the number of companies and contacts identified, the
pipeline of companies to send to, the [un]subscribes, the replies, and anything else important to
surface to make the system better over time". It covers the day before, and reads top to bottom:
  * the headline: sent, companies that replied (positive or referral among them), unsubscribes, companies and
    contacts found, and accounts ready to send, on one line;
  * Needs you (4 Oct 2026), right under it: the reply items waiting (the oldest's age), the kill-rule holds in
    force and an open hand-check; or "nothing";
  * Sent and outcomes: sends by step, replies by class, positive and referral replies, bounces,
    unsubscribes, demos booked and held (the events table; sync_outcomes, poll_replies and
    hubspot_readback write it); objections, negatives and "other" replies, out-of-office replies,
    and warm accounts: enrolled accounts that visited the US site (SPEC 11);
  * Found, Pipeline and To improve (below): the companies and contacts identified in the period,
    what is ready to send now and what stands behind it, and what the data says to tune, each line only once
    there is enough data;
  * Today's number: what limits it, each sender's capacity, and the credit budgets (limits.py,
    the same lines `us-outbound status` prints); and why enrollment waits today, from enrol.gate (a blackout or
    non-send day, an operator stop, the stop rule, a positive reply waiting too long), plus the hand-check while
    it gates the day (enrol.hand_check), and a note when live_sending is no (enrol then runs dry);
  * Sources: the careers and benefits page reader's coverage, its last run, every account read so
    far and the decision rule for enhancing it (sources/pages.py, `us-outbound report pages`); and
    what Apollo's organization enrich found, with funding in the last 180 and 365 days
    (sources/apollo_enrich.py); one line on website visits: the companies Apollo saw on the US
    pages, or, when its tracker sends no data, to check it (sources/site_visits.py); and, on the morning of
    the monthly lookalike_leads run (the 1st, 02:50), what it found, with the yield of US and non-US seeds
    (sources/lookalike_leads.py);
  * Labels (Harry, 7 Oct 2026; learn/label_metrics.py): the industry label check's companies checked in the period
    and their decisions (agreed, overruled, the group's or General copy, held, disqualified), the companies the last
    verify_accounts left unchecked and why, and the industry corrections made by hand;
  * Mailboxes: each mailbox's sends, its bounces over its last 100 sends (v_mailbox_health) and
    its place on the sending ramp (registry/ramp.py);
  * Kill rules and items waiting: rules that fired and the holds still in force
    (learn/kill_rules.py), and the human-in-the-loop items waiting for approval, by kind, and the
    oldest (a kill-rule hold is not waiting for approval: it is listed as a hold);
  * Retention (SPEC 6, 13), one line and only when the day's retention run (00:40 UK) deleted or
    cleared anything: the counts of Instantly leads, reply texts, contacts and companies
    (ops/retention.py post_line).
Each section has a bold title (Slack mrkdwn) after a blank line, and no table. Sources and Labels are built only for
the full post (full), each in its own try: a section that fails says "could not be built" and the rest prints
(W2-3; code-learn-reporting B6). The job's short post (compact) is made from what gather read, which reaches neither,
so a failing page reader or label check never stops the 09:00 post. It is one plain-text
message, as before, well under Slack's 40,000-character limit on text (about 5,000 in the tests):
account lists stop at LIST_LIMIT, and the funnel sections are aggregates that name no company or person.
It posts to the alert channel (the dev channel in dry-run), or to the log with no Slack token
(base/notify.py). Not yet in it: not-now dates coming due (the reply desk stores them).

Before it, the asks (Harry, 7 Oct 2026), each in its own message with the approvers mentioned and each
line once (base/notify.post_once): the credit and spend alerts (learn/spend.py: Apollo's balance against
apollo_floor, the monthly Apollo budget, Claude's spend against its cap, Apollo's website-visitor
credits). The mailboxes to add three weeks ahead (learn/spend.mailboxes_check) are a Monday ask, so the Monday
readout makes it. The credit budget lines end with the one-line spend summary (spend.summary_line).


The funnel sections, Found, Pipeline and To improve (learn/daily_report.py until W2-3, merged here):
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
  * replies, opener against holdout (contacts.opener_arm), once each arm has MIN_PER_ARM companies
    emailed (step 1 sent and not bounced); email 1's personal subject against the Copy row's
    (contacts.subject_arm; Harry, 5 Oct 2026) the same way; by angle and by copy version, for those that have
    MIN_PER_ARM each, once two can be compared. Counted by company as learn/outcomes.py defines it (W2-3): a human
    reply (any class but out-of-office) within 28 days of step 1, so recent sends have had less time to reply;
  * bounces by email source (kill_rules.SOURCES): of the companies sent step 1, how many bounced there, for a
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from us_outbound import fmt, labels, limits, verify
from us_outbound.base import holds, notify, say
from us_outbound.clients.db import Range, Store
from us_outbound.clients.guard import GuardViolation
from us_outbound.contacts import pick
from us_outbound.context import ET, UK, Context
from us_outbound.enrol import capacity, enrol, focus, queue, today
from us_outbound.enrol.enrol import Candidate
from us_outbound.enrol.openers import HOLDOUT, OPENER
from us_outbound.enrol.render import COPY_SUBJECT, PERSONAL_SUBJECT
from us_outbound.facts import newest_by
from us_outbound.learn import kill_rules, label_metrics, outcomes, spend
from us_outbound.limits import Limits
from us_outbound.logs import clip, log
from us_outbound.ops import retention
from us_outbound.registry import ramp
from us_outbound.replies import kinds
from us_outbound.replies.desk import is_reply
from us_outbound.scoring import tiers
from us_outbound.settings.model import TIERS, Settings
from us_outbound.timeparse import uk_midnight, utc, utc_strict

JOB = "daily_post"
WAITING = ("open", "escalated")
LISTED_CLASSES = ("objection", "negative", "other")
LIST_LIMIT = 10  # accounts listed per section
QUOTE = 120  # characters of reply text quoted
HAND_CHECK = "hand_check"  # hitl_items.kind of the weekly hand-check (enrol/hand_check.py)
# "reply" is poll_replies' kind; "reply_approval" the first name, kept for old rows. Kill-rule holds are listed
# as holds, never as waiting for approval.
KIND_LABELS = {"reply": "replies", "reply_approval": "reply approvals", HAND_CHECK: "hand-checks",
               "manual_merge": "manual merges"}
SEND_APPROVAL_KIND = "send_approval"  # the removed send approvals' kind (replies/poll_desk.close_send_approvals)
MAILBOX_HEALTH_SQL = "SELECT address, sends_last_100, bounces_last_100 FROM {schema}.v_mailbox_health"


# -- the funnel: Found, Pipeline and To improve (learn/daily_report.py until W2-3) -----------------------------------

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
    """The companies sent step 1 in the window (learn/outcomes.per_company, W2-3): who was emailed (step 1
    delivered), and who replied and replied warmly within the 28 days, as every company-level rate counts them."""

    step1: dict[str, outcomes.Outcome]  # account -> its outcome
    emailed: set[str]  # step 1 delivered
    replied: set[str]
    warm: set[str]

    @classmethod
    def read(cls, ctx: Context, since: datetime) -> _Emailed:
        found = {o.account_id: o for o in outcomes.per_company(ctx) if o.step1_at >= since}
        emailed = {a for a, o in found.items() if o.delivered}
        return cls(found, emailed, {a for a in emailed if found[a].replied_in_window},
                   {a for a in emailed if found[a].positive_in_window})


def _cut(m: _Emailed, key: str) -> list[tuple[str, int, int]]:
    """(value, emailed, replied) for the values with MIN_PER_ARM contacts emailed, most emailed first."""
    sent: Counter[str] = Counter()
    back: Counter[str] = Counter()
    for aid in m.emailed:
        v = m.step1[aid].stamp(key)
        if v:
            sent[v] += 1
            back[v] += aid in m.replied
    return [(v, n, back[v]) for v, n in sorted(sent.items(), key=lambda kv: (-kv[1], kv[0])) if n >= MIN_PER_ARM]


def _arms(m: _Emailed, key: str, a: str, b: str, label: str, early: list[str]) -> list[str]:
    """Replies in arm a against arm b of the contacts column key, once each has MIN_PER_ARM emailed."""
    arm: Counter[str] = Counter()
    back: Counter[str] = Counter()
    warm: Counter[str] = Counter()
    for aid in m.emailed:
        v = _lower(m.step1[aid].stamp(key))
        arm[v] += 1
        back[v] += aid in m.replied
        warm[v] += aid in m.warm
    if arm[a] >= MIN_PER_ARM and arm[b] >= MIN_PER_ARM:
        return [f"  Replies, {label}: {back[a]} of {arm[a]} ({fmt.pct(back[a], arm[a])}) "
                f"vs {back[b]} of {arm[b]} ({fmt.pct(back[b], arm[b])}); "
                f"interested {warm[a]} vs {warm[b]}. Early counts, not proof."]
    early.append(f"{label}, {arm[a]} and {arm[b]} of {MIN_PER_ARM} emailed each")
    return []


def _replies(m: _Emailed, early: list[str]) -> tuple[list[str], list[str]]:
    """(opener against holdout and the personal subject against the Copy row's, by angle and copy version)."""
    first = _arms(m, "opener_arm", OPENER, HOLDOUT, "personal opening line vs none (the holdout)", early)
    # Harry, 5 Oct 2026: email 1's personal subject (General email1_subject) against the Copy row's s1_subject.
    first += _arms(m, "subject_arm", PERSONAL_SUBJECT, COPY_SUBJECT, "personal subject vs the Copy tab's", early)
    parts = []
    for key, label in (("angle", "angle"), ("copy_version", "Copy tab version")):
        cut = _cut(m, key)
        if len(cut) >= 2:
            parts.append(f"by {label}: " + ", ".join(f"{v} {r} of {n}" for v, n, r in cut[:TOP]))
    if parts:
        return first, ["  Replies " + "; ".join(parts) + "."]
    early.append(f"angles and Copy tab versions, {MIN_PER_ARM} emailed each, two at least")
    return first, []


def _bounces(m: _Emailed, early: list[str]) -> list[str]:
    sent: Counter[str] = Counter()
    bounced: Counter[str] = Counter()
    for o in m.step1.values():
        src = _lower(o.stamp("email_source")) or "unknown"
        sent[src] += 1
        bounced[src] += not o.delivered
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


def to_improve(ctx: Context, rows: Rows) -> tuple[list[str], dict[str, Any]]:
    """The To improve section: what the data says to tune, once there is enough of it."""
    since = ctx.now - timedelta(days=LEARN_DAYS)
    early: list[str] = []
    m = _Emailed.read(ctx, since)
    if m.step1:
        opener, cuts = _replies(m, early)
        bounces = _bounces(m, early)
    else:
        opener, cuts, bounces = [], [], []
        early.append(f"no email sent in the last {LEARN_DAYS} days")
    # In this order, so that past MAX_IMPROVE the cuts by angle and copy version go first, then "too early".
    body = [*opener, *bounces, *_gaps(rows), *cuts]
    if early:
        body.append("  Too early to compare: " + "; ".join(early) + ".")
    body = body[:MAX_IMPROVE]
    nums = {"emailed_in_window": len(m.emailed), "improve_lines": len(body)}
    return [f"*To improve* · last {LEARN_DAYS} days", *(body or ["  Nothing to flag."])], nums


# -- the post --------------------------------------------------------------------------------------------------------


def period(ctx: Context) -> tuple[datetime, datetime, str]:
    """[start, end) and its label: yesterday (UK), and nothing before it. The job runs Tuesday to Saturday
    (ops/schedule.py), so yesterday is the previous US send day. A post never reaches back over a weekend, so no day
    is in two posts (metrics-audit L1: Friday's numbers went out on Saturday, Sunday and Monday)."""
    today = ctx.today_uk()
    day = today - timedelta(days=1)
    return uk_midnight(day), uk_midnight(today), f"Yesterday, {day:%a %d %b}"


def _company(e: Mapping[str, Any]) -> str:
    """The company an event belongs to; an event with no account counts by itself."""
    return str(e.get("account_id") or f"event:{e.get('event_id')}")


def by_company(events: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Each company's first event among these: a company that writes three times is one company (metrics-audit
    H3: sync_outcomes writes one replied event per email received, from the contact or anyone at the domain)."""
    first: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (utc(e.get("occurred_at")) or datetime.min.replace(tzinfo=UK),
                                           str(e.get("event_id")))):
        first.setdefault(_company(e), dict(e))
    return list(first.values())


def mailbox_last_100(ctx: Context) -> dict[str, tuple[int, int]]:
    """address -> (sends, bounced) over its last 100 sends: v_mailbox_health, or the same count in Python."""
    try:
        rows = ctx.store.query(MAILBOX_HEALTH_SQL.format(schema=ctx.store.schema))
        return {str(r["address"]).lower(): (int(r.get("sends_last_100") or 0), int(r.get("bounces_last_100") or 0))
                for r in rows if r.get("address")}
    except NotImplementedError:  # MemoryStore has no views
        out: dict[str, list] = {}
        for s in reversed(kill_rules.recent_sends(ctx.store, ctx.now, None)):
            out.setdefault(s.mailbox, [])
            if len(out[s.mailbox]) < kill_rules.LAST_SENDS:
                out[s.mailbox].append(s.bounced)
        return {a: (len(v), sum(v)) for a, v in out.items()}


def _names(ctx: Context, ids: set[str]) -> dict[str, str]:
    ids_ = sorted(i for i in ids if i)
    out: dict[str, str] = {}
    for i in range(0, len(ids_), 1000):
        for a in ctx.store.select("accounts", {"account_id": ids_[i : i + 1000]}):
            out[str(a["account_id"])] = str(a.get("clean_name") or a.get("domain") or a["account_id"])
    return out


# -- what waits for Harry -----------------------------------------------------------------------------


@dataclass
class ForYou:
    """What waits for a person now (the post's "Needs you" line, and `us-outbound status`)."""

    replies: list[dict]  # reply items to answer: open or escalated
    holds: list[dict]  # kill-rule holds in force
    hand_checks: list[dict]  # the weekly hand-check, while it waits

    @property
    def counts(self) -> dict[str, int]:
        return {"replies": len(self.replies),
                "kill_rule_holds": len(self.holds), "hand_checks": len(self.hand_checks)}


def for_you(ctx: Context, open_items: Sequence[Mapping[str, Any]] | None = None) -> ForYou:
    """From the hitl_items open or escalated (read here unless given)."""
    rows = list(open_items) if open_items is not None else ctx.store.select("hitl_items", {"status": list(WAITING)})
    return ForYou(replies=[dict(i) for i in rows if is_reply(i)],
                  holds=[dict(i) for i in rows if i.get("kind") == holds.KIND],
                  hand_checks=[dict(i) for i in rows if i.get("kind") == HAND_CHECK])


def _oldest(ctx: Context, items: Sequence[Mapping[str, Any]]) -> str:
    oldest = min((t for i in items if (t := utc(i.get("created_at"))) is not None), default=None)
    if oldest is None:
        return ""
    waited = fmt.hours(ctx.now - oldest)
    return f"waited {waited}" if len(items) == 1 else f"the oldest has waited {waited}"


def needs_you(ctx: Context, w: ForYou) -> str:
    """"*Needs you:* 1 reply (waited 2 hours) · 1 kill-rule hold (...) · ..."."""
    parts = []
    if w.replies:
        age = _oldest(ctx, w.replies)
        parts.append(fmt.plural(len(w.replies), "reply", "replies") + (f" ({age})" if age else ""))
    if w.holds:
        parts.append(f"{fmt.plural(len(w.holds), 'kill-rule hold')} (`us-outbound killrules show`)")
    if w.hand_checks:
        parts.append("this week's hand-check (the Hand-check tab on the settings sheet)")
    return "*Needs you:* " + (" · ".join(parts) if parts else "nothing")


# -- W3-1's asks (design-final W3-1 change 4; loops L7, L12, L6) ----------------------------------------------------

STALL_DAYS = 3  # send days with no company and no contact found
STALL_WAITING = 50  # ... while at least this many companies wait for a contact or a check
CANARY_FAILED = ("canary:no_answer", "canary:not_sent", "canary_send:failed")  # replies/capture.py's reasons


def stalled(ctx: Context, waiting: int) -> str | None:
    """Supply stalled: no account first seen and no contact created over the last STALL_DAYS send days (US send
    window, blackouts left out) while STALL_WAITING or more wait (L12)."""
    days, d = [], ctx.today_uk()
    for _ in range(STALL_DAYS):
        d = capacity.previous_send_day(d, ctx.settings)
        if d is None:
            return None
        days.append(d)
    since = uk_midnight(days[-1])
    if waiting < STALL_WAITING or ctx.store.select("accounts", {"first_seen": Range(since, ctx.now)}) \
            or ctx.store.select("contacts", {"created_at": Range(since, ctx.now)}):
        return None
    return (f"Supply stalled: no new company and no new contact on the last {STALL_DAYS} send days while "
            f"{waiting:,} companies wait for a check or a contact: see what {say.job_name('source_universe')} and "
            f"{say.job_name('pick_contacts')} last said with {say.command('status')}")


def capture_ask(ctx: Context) -> str | None:
    """This week's reply-capture check (replies/capture.py) when it found a drift or the canary failed (L7); a check
    not run, or the canary not set up yet (D10 e), is no ask: the readout says the counts are unverified."""
    from us_outbound.replies import capture

    rows = [r for f in (capture.VERIFIED, capture.UNVERIFIED) for r in capture._facts(ctx, f)
            if (at := utc(r.get("observed_at"))) is not None and ctx.now - capture.FACT_FRESH <= at <= ctx.now]
    if not rows:
        return None
    latest = max(rows, key=lambda r: (utc(r.get("observed_at")), str(r.get("event_id"))))
    value = latest.get("value") if isinstance(latest.get("value"), Mapping) else {}
    if latest.get("fact") != capture.UNVERIFIED:
        return None
    drift = [d for d in value.get("drift") or () if isinstance(d, Mapping)]
    if drift:
        what = "; ".join(f"{d.get('measure')}: Instantly {d.get('instantly')}, ours {d.get('ours')}" for d in drift)
        return (f"Capture drift: this week's check found Instantly's counts and ours apart ({what}), so replies may be "
                f"going unread: look in Instantly's inbox, then {say.command('report capture')}")
    failed = [r for r in value.get("reasons") or () if r in CANARY_FAILED]
    if "canary:not_sent" in failed and canary_kept(ctx, utc(latest.get("observed_at")) or ctx.now):
        failed.remove("canary:not_sent")  # not failed yet: its lead is kept and the next check looks again
    if failed:
        return (f"Canary failed ({', '.join(failed)}): the seed inbox's answer did not come back through the reply "
                f"read: {say.command('report capture')}")
    return None


def canary_kept(ctx: Context, checked_at: datetime) -> bool:
    """A canary the check at checked_at found not sent and kept (replies/capture._check_one): unchecked, added at
    least CANARY_WAIT before that check (so not the canary the same run added) and younger than CANARY_GIVE_UP."""
    from us_outbound.replies import capture

    for r in capture._facts(ctx, capture.CANARY_SENT):
        if (r.get("value") or {}).get("checked_at"):
            continue
        added = capture._added(r, ctx.now)
        if added <= checked_at - capture.CANARY_WAIT and ctx.now - added < capture.CANARY_GIVE_UP:
            return True
    return False


def lost_ask(ctx: Context, start: datetime, end: datetime) -> str | None:
    """Yesterday's enrol run lost leads to a gate (it did not run: a hold, a pause) or to errors (prepared, not
    enrolled), from its run summary as learn/throughput.read_day reads it (L6: looked at the same day)."""
    from us_outbound.learn import throughput

    runs = ctx.store.select("heartbeats", {"job": throughput.ENROL, "started_at": Range(start, end)})
    days = [(utc(r.get("started_at")), d, r) for r in runs if (d := throughput.read_day(ctx, r)) is not None]
    if not days:
        return None
    _, day, row = max(days, key=lambda x: x[0])
    errors = day.lost.get("errors", 0)
    if day.binding == "gate":
        why = str((row.get("detail") or {}).get("reason") or "a gate").rstrip(".")
        return (f"Leads lost: enrolment did not run yesterday ({why}), about {day.lost.get('gate', 0)} leads: "
                f"{say.command('status')}")
    if errors:
        return (f"Leads lost: {fmt.plural(errors, 'lead')} prepared yesterday and not enrolled (errors): read the "
                f"error with {say.command('status')}")
    return None


def watch_asks(ctx: Context, nums: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The asks W3-1 adds, in order: supply stalled, capture drift or canary failed, leads lost."""
    start, end, _ = period(ctx)
    waiting = int(nums.get("waiting_for_contact") or 0) + int(nums.get("waiting_for_verification") or 0)
    out = [a for a in (stalled(ctx, waiting), capture_ask(ctx), lost_ask(ctx, start, end)) if a]
    return out, {}


@dataclass
class Day:
    """Everything the post reads, gathered once: the full post's lines (`us-outbound report daily --full`), the numbers
    for the summary, and what the short post (compact) picks from."""

    start: datetime
    end: datetime
    label: str
    lines: list[str]  # the full post down to the credit budgets; Sources and Labels follow (full), then tail
    nums: dict[str, Any]
    waiting_on: ForYou
    lim: limits.Limits
    why: str | None = None
    tail: list[str] = field(default_factory=list)  # Mailboxes, kill rules, waiting, retention
    warm: list[str] = field(default_factory=list)  # "Acme Creative (positive reply to hannah@…)"
    objections: list[str] = field(default_factory=list)  # the full post's quoted lines
    visited: list[str] = field(default_factory=list)  # enrolled accounts that visited the US site
    groups: str = ""  # ready accounts by Focus share or industry group
    improve: list[str] = field(default_factory=list)  # To improve, "too early" and "nothing" left out
    mailboxes: list[tuple[str, int, int, int]] = field(default_factory=list)  # (address, sent, sends, bounces)
    ramping: int = 0  # Active mailboxes still on the sending ramp
    fired: list[str] = field(default_factory=list)  # kill rules fired in the period
    holds: int = 0  # kill-rule holds in force
    retention: str = ""
    spend: str = ""  # "Spend: Apollo balance …" (spend.summary_line)
    asks: list[str] = field(default_factory=list)  # W3-1's: a supply stall, capture drift, leads lost (watch_asks)


def build(ctx: Context, spent: spend.Spend | None = None) -> tuple[list[str], dict[str, Any]]:
    """(the full post's lines, the numbers for the summary): `us-outbound report daily --full`. The job posts the short
    post (compact) made from the same day."""
    return full(ctx, gather(ctx, spent))


COULD_NOT = "could not be built"


def _section(ctx: Context, title: str, make: Callable[[], tuple[list[str], dict[str, Any]]]
             ) -> tuple[list[str], dict[str, Any]]:
    """A section built in its own try: (its lines, its numbers), or its title and "could not be built" with the error
    named, so one failing source never stops the rest of the post (W2-3; code-learn-reporting B6)."""
    try:
        return make()
    except GuardViolation:
        raise
    except Exception as exc:
        log("daily_post_section_failed", run_id=ctx.run_id, section=title, error=f"{type(exc).__name__}: {exc}"[:200])
        return [f"*{title}*", f"  {COULD_NOT} ({type(exc).__name__}); the rest of the post is complete."], {
            "sections_failed": [title]}


def _sources(ctx: Context) -> tuple[list[str], dict[str, Any]]:
    """The careers and benefits page reader's coverage (Harry, 2 Oct 2026), funding and headcount from Apollo's
    organization enrich, the monthly lookalike leads the morning they ran (5 Oct 2026), and website visits."""
    from us_outbound.sources import apollo_enrich, lookalike_leads, pages, site_visits

    lines = ["*Sources*", *pages.post_lines(ctx)]
    total = pages.coverage(ctx.store, ctx.settings, ctx.today_uk())
    nums: dict[str, Any] = {"pages_read": total.accounts,
                            "pages_benefits_text_share": round(total.share(total.with_text), 3)}
    lines += apollo_enrich.post_lines(ctx)
    lines += lookalike_leads.post_lines(ctx)
    enriched = apollo_enrich.tally(ctx.store, ctx.today_uk())
    nums.update(enriched=enriched.accounts, enriched_with_funding=enriched.with_funding)
    lines.append(site_visits.post_line(ctx))  # from Apollo, or that its tracker sends no data (5 Oct 2026)
    return lines, nums


def full(ctx: Context, day: Day) -> tuple[list[str], dict[str, Any]]:
    """The full post (`us-outbound report daily --full`): what gather read, with Sources and Labels, each section in
    its own try. (its lines, the day's numbers with the sections' own.)"""
    nums = dict(day.nums)
    lines = list(day.lines)
    failed: list[str] = []
    for title, make in (("Sources", lambda: _sources(ctx)),
                        ("Labels", lambda: (["*Labels*", *label_metrics.post_lines(ctx, day.start, day.end)], {}))):
        section, more = _section(ctx, title, make)
        failed += more.pop("sections_failed", [])
        lines += ["", *section]
        nums.update(more)
    if failed or nums.get("sections_failed"):
        nums["sections_failed"] = [*nums.get("sections_failed", []), *failed]
    return lines + day.tail, nums


def gather(ctx: Context, spent: spend.Spend | None = None) -> Day:
    """The day's numbers and the full post but its Sources and Labels (full adds them). spent: what learn/spend.py
    read (read here when not given)."""
    start, end, label = period(ctx)
    types = ["sent", "replied", "bounced", "unsubscribed", "meeting_booked", "demo_held", "site_visit"]
    rows = ctx.store.select("events", {"type": types, "occurred_at": Range(start, end)})  # the period's only
    by_type: dict[str, list[dict]] = {}
    for e in rows:
        by_type.setdefault(str(e.get("type")), []).append(e)
    sent, replies = by_type.get("sent", []), by_type.get("replied", [])
    names = _names(ctx, {str(e.get("account_id") or "") for e in rows})

    def who(e: Mapping[str, Any]) -> str:
        return names.get(str(e.get("account_id") or ""), "an account")

    steps = Counter(e.get("step") for e in sent)
    # A reply is a person writing back (replies/kinds.py), as the readout counts it; away messages are listed apart.
    # Replies, positive replies and away messages count by company (by_company); reply_emails keeps the emails.
    human_emails = [e for e in replies if kinds.is_human(e.get("reply_class"))]
    human = by_company(human_emails)
    seen: dict[str, set[str]] = {}
    for e in human_emails:
        seen.setdefault(_company(e), set()).add(str(e.get("reply_class") or "not classified"))
    classes = Counter(c for found in seen.values() for c in found)  # companies per class
    warm = by_company(e for e in replies if kinds.is_warm(e.get("reply_class")))
    away = by_company(e for e in replies if not kinds.is_human(e.get("reply_class")))
    nums = {
        "period": label, "sent": len(sent), "by_step": {str(k): v for k, v in steps.items()}, "replies": len(human),
        "reply_emails": len(human_emails), "out_of_office": len(away),
        "by_class": dict(classes), "positive": len(warm), "bounced": len(by_type.get("bounced", [])),
        "unsubscribed": len(by_type.get("unsubscribed", [])),
        # By company: a meeting and a Spill 3.0 deal for one booking are two events (crm/readback.py, 6 Oct 2026).
        "demos_booked": len({_company(e) for e in by_type.get("meeting_booked", [])}),
        "demos_held": len({_company(e) for e in by_type.get("demo_held", [])}),
    }
    lines = [f"*Daily post, {ctx.now.astimezone(UK):%a %d %b}*" + (" (dry-run)" if ctx.dry_run else "")]
    lines += ["", f"*Sent and outcomes* · {label} (UK)"]
    step_text = fmt.counts(Counter({f"{k}": v for k, v in steps.items() if k is not None}), label="step ")
    lines.append(f"  Sent: {len(sent)}" + (f" ({step_text})" if step_text else ""))
    lines.append(f"  Replies: {fmt.plural(len(human), 'company', 'companies')}"
                 + (f" ({fmt.counts(classes)})" if human else "")
                 + (f", in {fmt.plural(len(human_emails), 'email')}" if len(human_emails) != len(human) else "")
                 + (f" · out of office {nums['out_of_office']}" if nums["out_of_office"] else ""))
    lines.append(f"  Positive or referral: {len(warm)}"
                 + (" · " + "; ".join(f"{who(e)} ({e.get('reply_class')}, {e.get('mailbox') or '?'})" for e in warm[:LIST_LIMIT])
                    if warm else ""))
    lines.append(f"  Bounces: {nums['bounced']} · Unsubscribes: {nums['unsubscribed']} · "
                 f"Demos booked: {nums['demos_booked']} · Demos held: {nums['demos_held']}")
    listed = [e for e in human if e.get("reply_class") in LISTED_CLASSES]
    objections = [f"{who(e)} ({e.get('reply_class')}): “{clip(' '.join(str(e.get('reply_text') or '').split()), QUOTE)}”"
                  for e in listed[:LIST_LIMIT]]
    if listed:
        lines.append("Objections, negatives and other replies:")
        lines += [f"  • {x}" for x in objections]
    if away:
        lines.append("Out of office: " + ", ".join(who(e) for e in away[:LIST_LIMIT]))
    visits = {str(e.get("account_id") or "") for e in by_type.get("site_visit", [])}
    enrolled = {str(a["account_id"]) for a in ctx.store.select("accounts", {"account_id": sorted(visits)})
                if a.get("status") in ("enrolled", "engaged")} if visits else set()
    visited = sorted(names.get(a, a) for a in enrolled)[:LIST_LIMIT]
    if enrolled:
        lines.append("Warm accounts (enrolled, visited the US site): " + ", ".join(visited))

    # Today's number, its senders and the ready accounts exactly as enrol.run works them out.
    t = today.read(ctx)
    today_et, hand_check_waits, lim = t.day, t.waits, t.limits
    ready = t.ready
    nums.update(number=lim.number, limited_by=lim.explanation, ready_accounts=t.ready_count)

    # The funnel (Harry, 2 Oct 2026): what was found, the pipeline, and what to tune.
    data = Rows.load(ctx)
    improve: list[str] = []
    for section, more in (found(ctx, data, start, end, label), pipeline(ctx, data, ready, lim),
                          _section(ctx, "To improve", lambda: to_improve(ctx, data))):
        lines += ["", *section]
        nums.update(more)
        if section[0].startswith("*To improve*"):
            improve = [x.strip() for x in section[1:] if not x.strip().startswith(("Too early", "Nothing to flag"))
                       and not x.strip().startswith(COULD_NOT)]
    lines.insert(1, headline(label.split(",")[0], nums))
    waiting_on = for_you(ctx, data.open_items)
    lines.insert(2, needs_you(ctx, waiting_on))
    nums.update(needs_you=waiting_on.counts)

    # Today's number, each sender, the budgets (limits.py), and why enrollment waits: enrol's own gates, and the
    # hand-check while it gates the day; with hand_check_gate = no it never waits (enrol.hand_check: only the sampled
    # companies are held).
    lines += ["", "*Today's number*", lim.explanation]
    lines += [f"  {line}" for line in lim.detail]
    why = enrol.gate(ctx, today_et)
    if why is None:
        why = hand_check_waits
    if why:
        lines.append(f"  Enrollment waits: {why}.")
    if not ctx.settings.general.live_sending and "live_sending" not in (why or ""):
        lines.append("  Running dry: live_sending is no, so enrol reaches nobody.")
    lines.append("Credit budgets this month:")
    lines += [f"  {line}" for line in lim.budget_lines]
    spent = spent if spent is not None else spend.read(ctx)
    spend_line = spend.summary_line(spent)
    lines.append(f"  {spend_line}.")
    nums.update(spend=spent.as_dict())

    # Sources and Labels go in the full post only (full), each in its own try (W2-3; code-learn-reporting B6).
    head = lines
    lines = []

    # Mailbox health: sends in the period, bounces over the last 100, the ramp.
    lines += ["", "*Mailboxes*"]
    last100 = mailbox_last_100(ctx)
    ramps = ramp.ramps(ctx.store, ctx.settings, today_et)
    sent_by = Counter(str(e.get("mailbox") or "").lower() for e in sent)
    boxes: list[tuple[str, int, int, int]] = []
    ramping = 0
    for m in ctx.settings.mailboxes:
        if m.status == "Retired":
            continue
        a = m.address.lower()
        n, b = last100.get(a, (0, 0))
        r = ramps.get(a)
        boxes.append((a, sent_by.get(a, 0), n, b))
        ramping += bool(m.status == "Active" and r is not None and r.ramping)
        line = f"  • {a} ({m.status}): {sent_by.get(a, 0)} sent; "
        line += f"bounces {b} of its last {n} sends ({b / n:.1%})" if n else "no sends on record yet"
        if r is not None and r.ramping:
            line += f"; {r.describe()}"
        lines.append(line)

    # Kill rules (SPEC 11: "any kill rule that fired").
    lines += ["", "*Kill rules and items waiting*"]
    fired = [i for i in ctx.store.select("hitl_items", {"kind": holds.KIND})
             if (t := utc(i.get("created_at"))) is not None and start <= t < ctx.now]
    in_force = waiting_on.holds  # holds.in_force, from the open items already read
    if fired:
        lines.append("Kill rules fired: " + "; ".join(str((i.get("payload") or {}).get("reason")) for i in fired[:LIST_LIMIT]))
    else:
        lines.append("Kill rules fired: none")
    if in_force:
        lines.append(f"  Holds in force: {len(in_force)} (`us-outbound killrules show`)")
    nums.update(kill_rules_fired=len(fired), holds_in_force=len(in_force))

    # What waits for an approval (a kill-rule hold is a hold, listed above, not an approval; a send approval left
    # open from before W2-1 waits for nobody: the reply desk closes it).
    waiting = [i for i in data.open_items if i.get("kind") not in (holds.KIND, SEND_APPROVAL_KIND)]
    by_kind = Counter(KIND_LABELS.get(str(i.get("kind")), str(i.get("kind"))) for i in waiting)
    if waiting:
        oldest = min((utc(i.get("created_at")) for i in waiting if utc(i.get("created_at"))), default=None)
        age = f"; the oldest has waited {int((ctx.now - oldest).total_seconds() // 3600)} hours" if oldest else ""
        lines.append(f"Waiting for approval: {len(waiting)} ({fmt.counts(by_kind)}){age}.")
    else:
        lines.append("Waiting for approval: nothing.")
    nums.update(waiting=len(waiting), waiting_by_kind=dict(by_kind))

    # Retention (SPEC 6, 13): one line, counts only, when today's run deleted or cleared anything.
    line, done = retention.post_line(ctx)
    if line:
        lines += ["", line]
    nums.update(retention=done)
    # W3-1's asks (design-final W3-1 change 4): a supply stall, the reply capture's drift or canary, leads lost to a
    # gate or an error; each in the asks at the top, never stopping the post.
    asks, more = _section(ctx, "Asks", lambda: watch_asks(ctx, nums))
    if more.get("sections_failed"):
        nums["sections_failed"] = [*nums.get("sections_failed", []), *more["sections_failed"]]
        asks = []
    nums.update(asks=[a.split(":")[0] for a in asks])
    warm_lines = [f"{who(e)} ({e.get('reply_class')} reply to {e.get('mailbox') or '?'})" for e in warm[:LIST_LIMIT]]
    return Day(start, end, label, head, nums, waiting_on, lim, why=why, warm=warm_lines, objections=objections,
               tail=lines, visited=visited, groups=ready_groups(ready, ctx.settings) if ready else "",
               improve=improve, mailboxes=boxes, ramping=ramping,
               fired=[str((i.get("payload") or {}).get("reason")) for i in fired], holds=len(in_force),
               retention=line or "", spend=spend_line, asks=asks)


# -- the short post (Harry, 9 Oct 2026: "a more readable pithier version of the daily report") -------------------
# Harry, 12 Oct 2026: clear to anyone, even new to the system. The first line says whether anything needs you today;
# the asks come first, numbered, each with where to go or the command to paste; then yesterday and today in a few
# lines of plain numbers, with every internal name translated or explained (base/say.py).

BOUNCE_WATCH = 0.03  # a mailbox's bounces over its last 100 sends, from MIN_WATCH_SENDS sends, that the post flags
MIN_WATCH_SENDS = 20
# What holds today's number down (limits.LABELS), for a reader who does not know the terms.
LIMIT_WORDS = {"weekly_target": "this week's target (`weekly_enrol_cap`)",
               "sending_capacity": "how much the mailboxes can send",
               "ready_accounts": "the companies ready to email"}


def _span(day: Day) -> str:
    """"Mon 26 Oct": the one day the post covers (period)."""
    return f"{day.start.astimezone(UK):%a %-d %b}"


def _short_names(owners: Sequence[str]) -> dict[str, str]:
    """Each sender by first name, with the surname's initial where two share one ("Hannah S", "Hannah M")."""
    first = Counter(o.split()[0] for o in owners if o.split())
    return {o: (o.split()[0] if first[o.split()[0]] == 1 or len(o.split()) < 2 else f"{o.split()[0]} {o.split()[-1][0]}")
            for o in owners if o.split()}


def _waited(ctx: Context, items: Sequence[Mapping[str, Any]]) -> str:
    """"waiting 30 hours" / "the oldest waiting 30 hours", or ""."""
    oldest = min((t for i in items if (t := utc(i.get("created_at"))) is not None), default=None)
    if oldest is None:
        return ""
    return ("waiting " if len(items) == 1 else "the oldest waiting ") + fmt.hours(ctx.now - oldest)


def _needs(ctx: Context, w: ForYou, more: Sequence[str] = ()) -> list[str]:
    """The asks, numbered: what to do, and where (the channel, or the command to paste); more: watch_asks' after."""
    channel = ctx.settings.general.alert_channel
    out = []
    if w.replies:
        age = _waited(ctx, w.replies)
        n = len(w.replies)
        out.append(f"Answer {fmt.plural(n, 'prospect reply', 'prospect replies')}{f' ({age})' if age else ''}: ✅ or "
                   f"`edit: <text>` in {'its' if n == 1 else 'each'} thread in {channel}")
    if w.holds:
        out.append(f"{fmt.plural(len(w.holds), 'safety pause')} on (bounce or complaint limit hit): "
                   f"{say.command('killrules show')}")
    from us_outbound.enrol import hand_check, hand_check_tab

    week = hand_check.this_week(ctx)  # an earlier week's item still open is superseded by this week's (12 Oct 2026)
    item = next((i for i in w.hand_checks if enrol._item_week(i) == week), None)
    if item is not None:
        left = hand_check_tab.undecided(ctx, week)
        p = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        n = left if left is not None else len(p.get("accounts") or ()) + len(p.get("doubtful") or ())
        then = "; new leads wait for it" if hand_check.gates(ctx) else ""
        if n:
            out.append(f"Weekly hand-check: {fmt.plural(n, 'company', 'companies')} still need a decision on the "
                       f"{say.sheet_link(ctx, hand_check_tab.TAB)}{then}")
    out += list(more)
    return [f"{i}. {x}" for i, x in enumerate(out, 1)]


def _yesterday(day: Day) -> list[str]:
    n = day.nums
    if n.get("sent") or n.get("replies"):
        steps = n.get("by_step", {})
        first = int(steps.get("1", 0))
        later = sum(int(v) for k, v in steps.items() if k != "1")
        kinds_ = ", ".join(x for x in (f"{first} first" if first else "", f"{later} follow-up{'s' if later != 1 else ''}"
                                       if later else "") if x)
        # By company (metrics-audit H3); the reply emails only when a company wrote more than once.
        emails = int(n.get("reply_emails", n.get("replies", 0)) or 0)
        parts = [f"Sent {n.get('sent', 0)} emails" + (f" ({kinds_})" if kinds_ else ""),
                 f"{fmt.plural(n.get('replies', 0), 'company', 'companies')} replied ({n.get('positive', 0)} interested"
                 + (f"; {fmt.plural(emails, 'reply email')}" if emails != n.get("replies", 0) else "") + ")"]
    else:
        parts = ["No emails sent"]
    parts += [fmt.plural(n[k], word) for k, word in (("bounced", "bounce"), ("unsubscribed", "unsubscribe"))
              if n.get(k)]
    if n.get("demos_booked"):
        parts.append(f"{fmt.plural(n['demos_booked'], 'demo')} booked")
    out = [" · ".join(parts)]
    found = f"Found {fmt.plural(n.get('found_companies', 0), 'company', 'companies')}"
    sources = n.get("found_by_source") or {}
    if len(sources) == 1:
        found += f" (all {next(iter(sources)).replace('_', ' ')})"
    elif sources:
        found += " (" + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(sources.items(), key=lambda kv: -kv[1])) + ")"
    found += f" and {fmt.plural(n.get('found_contacts', 0), 'contact')}"
    if n.get("verified_in_period"):
        found += f" · {n['verified_in_period']} passed the company check"
    out.append(found)
    out += [f"Interested: {x}" for x in day.warm]
    out += [f"• {x}" for x in day.objections]
    if day.visited:
        out.append("Visited the US site after we emailed: " + ", ".join(day.visited))
    return out


def _today(ctx: Context, day: Day) -> list[str]:
    lim, t = day.lim, day.lim.terms
    if day.why:
        out = ["*Today: no new people added to the emails*", f"Why: {day.why}."]
    else:
        out = [f"*Today: up to {fmt.plural(lim.number, 'new person', 'new people')} added to the emails*"]
        binding = str(t.get("binding"))
        limit = LIMIT_WORDS.get(binding, limits.LABELS.get(binding, binding))
        if binding == "sending_capacity" and day.ramping:
            limit += f" ({fmt.plural(day.ramping, 'mailbox', 'mailboxes')} still warming up)"
        out.append(f"Held to that by {limit}")
    names = _short_names(list(lim.senders))
    senders = []
    for owner, c in ({} if day.why else lim.senders).items():  # no room to show on a day nobody is added
        part = f"{names.get(owner, owner)} {c.free}"
        if c.not_sending:
            part += " (not sending)"
        senders.append(part)
    if senders:
        out.append("Room per sender: " + " · ".join(senders))
    week = (f"This week: {t.get('enrolled_this_week', 0)} of {t.get('weekly_enrol_cap', 0)} added, "
            f"{fmt.plural(int(t.get('send_days_left_in_week') or 0), 'send day')} left")
    q = focus.today(ctx, int(t.get("send_days_left_in_week") or 0))
    if q.active:
        week += " · " + " · ".join(f"{k} {q.done[k]}/{q.targets[k]}" for k in q.targets)
    out.append(week)
    if not ctx.settings.general.live_sending:
        out.append("Test mode: `live_sending` is no, so no prospect is emailed.")
    return out


def _pipeline(day: Day) -> list[str]:
    n = day.nums
    ready = n.get("ready_to_send", 0)
    line = f"Ready to email: {ready:,}"
    if n.get("supply_days") is not None:
        line += f", about {n['supply_days']} send days' worth"
    if day.groups:
        line += f" ({day.groups})"
    behind = [f"{n.get('waiting_for_contact', 0):,} need a contact",
              f"{n.get('waiting_for_verification', 0):,} need their facts checked"
              + (f" ({n['doubtful_facts']} in the weekly hand-check)" if n.get("doubtful_facts") else "")]
    out = [line, "Being prepared: " + " · ".join(behind)]
    if n.get("parked"):  # out of the queue while a setting keeps them out (W1-4): not being prepared
        out.append(parked_line(n.get("parked_by_reason") or {labels.INDUSTRY_OFF: n["parked"]}))
    if n.get("in_sequence") or n.get("finished") or n.get("stopped"):
        out.append(f"Emailed: {n.get('in_sequence', 0)} with follow-ups to come · {n.get('finished', 0)} finished · "
                   f"{n.get('stopped', 0)} stopped (replied, bounced or unsubscribed)")
    return out


def _watch(day: Day) -> tuple[list[str], list[str]]:
    """(the lines that need a look, the areas checked that need none)."""
    out = [f"• {x.removesuffix(SOURCES_POINTER)}" for x in day.improve]
    fine = []
    loud = [(a, b, n) for a, _, n, b in day.mailboxes if n >= MIN_WATCH_SENDS and b / n >= BOUNCE_WATCH]
    out += [f"• {a}: {b} of its last {n} emails bounced ({b / n:.0%}); over {kill_rules.BOUNCE_RATE:.0%} of 100 it "
            "is paused automatically" for a, b, n in loud]
    if not loud:
        fine.append("mailboxes")
    out += [f"• Safety pause set off: {x}" for x in day.fired]
    if not day.fired and not day.holds:
        fine.append("safety limits")
    stopped = [c for c in day.lim.senders.values() if c.not_sending]
    out += [f"• {c.owner}: {c.not_sending}" for c in stopped]
    if not stopped:
        fine.append("campaigns")
    return out, fine


def compact(ctx: Context, day: Day) -> list[str]:
    """The short post: whether anything needs you, the asks, yesterday, today, the pipeline, and only what needs a
    look; the rest is in `us-outbound report daily --full` and the Monday readout."""
    needs = _needs(ctx, day.waiting_on, day.asks)
    date = f"{ctx.now.astimezone(UK):%a %-d %b}"
    if needs:
        head = f"{say.ACTION} *Daily summary, {date}: {fmt.plural(len(needs), 'thing needs', 'things need')} you*"
    else:
        head = f"{say.INFO} *Daily summary, {date}: nothing needs you today*"
    lines = [head, *needs, "", f"*Yesterday ({_span(day)})*", *_yesterday(day), "", *_today(ctx, day), "",
             "*Pipeline*", *_pipeline(day)]
    if day.spend:
        lines += ["", day.spend]
    watch, fine = _watch(day)
    if watch:
        lines += ["", "*Worth a look*", *watch]
    if day.retention:
        lines += ["", day.retention]
    listed = " and ".join([", ".join(fine[:-1]), fine[-1]] if len(fine) > 1 else fine)
    lines += ["", (f"Fine: {listed}. " if fine else "") + f"Full report: {say.command('report daily --full')}"]
    return lines


def run(ctx: Context) -> dict:
    """The daily_post job (JOB CONTRACT: run(ctx) -> summary).

    The summary (kept in the heartbeat row) has the numbers, not the text: the text quotes
    replies, and reply text is purged after 90 days (SPEC 6), which a heartbeat row is not.
    """
    spent, asked = spend.check(ctx)  # the credit and spend asks first, so a failing post below never holds them
    day = gather(ctx, spent)
    lines, nums = compact(ctx, day), day.nums
    # The mailboxes three weeks ahead are a Monday ask, and there is no Monday post: the Monday readout makes it.
    sent = notify.alert(ctx, "\n".join(lines))
    summary = {"job": JOB, "dry_run": ctx.dry_run, **nums, "alert": sent, "spend_alert": asked}
    log("daily_post_done", run_id=ctx.run_id, **summary)
    return summary
