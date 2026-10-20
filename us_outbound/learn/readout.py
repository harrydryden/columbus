"""monday_readout, Mondays 08:30 UK (SPEC 9, 12 "Monday readout"; Harry, 6 Oct 2026: "push ahead with building").

One message to the alert channel (#us-outbound; the dev channel in dry-run, base/notify.py), after the weekly
hand-check's draw (05:45), about the week before (Monday to Sunday, UK time), written for a reader who may be new (Harry,
12 Oct 2026; base/say.py); there is no Monday daily post from 19 Oct 2026. It opens with a short summary: a headline
that says how many things need someone, last week's numbers, the rates so far, and what needs action. Each
section then says in a few words what it measures, and anything that needs action says what to do and where. Top
to bottom:
  * Last week: emails sent, companies that replied (human: every class but out-of-office) and that replied
    positively (positive or referral), each company once however many times it wrote (metrics-audit H3; the reply
    emails are counted beside), meetings booked (companies with a meeting_booked event: crm/readback.py, a meeting on
    Harry's calendar or a Spill 3.0 deal at Demo requested or later), bounces and unsubscribes, from the events table.
  * So far (target_line, from learn/outcomes.per_company, the Python twin of v_account_outcomes since W2-3, so it reads
    on any store): only companies whose reply window has closed are judged
    (metrics-audit H2: on 19 Oct every rate read "below" with 0 of 47 windows closed). Under MIN_COMPANIES matured
    companies it gives counts and how many have had the 28 days, and no rate; from 30, each rate with its Wilson 95%
    interval and SPEC 12's working assumption beside it (3 to 5%, about 1%, 0.5 to 1%), and no "on track" or
    "below": at these numbers an interval spans the assumption either way. Meetings are judged at 45 days.
  * Against the targets: companies enrolled last week against General weekly_enrol_cap; the companies emailed so
    far; the stop rule's progress (stop_rule_meetings from the first stop_rule_accounts companies).
  * The exit criteria to scale (docs/roadmap.md §3): bounces under 2% of the contacts sent email 1, judged from
    MIN_CONTACTS (a bounced lead stops, so per send the rate fell about 4x as sequences matured: metrics-audit H4),
    with its interval; unsubscribes confirmed end to end (optout_tested = yes, and every opt-out recorded
    everywhere: replies/optout.py writes its unsubscribed event only then); every reply classified and routed (a
    class within UNCLASSIFIED_AFTER, and a reply card, an out-of-office record or an opt-out for each); no
    unexplained refusal (no guard refusal in last week's heartbeats; the emails the copy rules held back are
    counted, each has its reason in enrol's run summary). Each says met or not met, with its numbers. Spam
    complaints are "not measured": nothing writes a complaint event yet, so a constant 0 is never counted as met
    (metrics-audit H1). The "raise weekly_enrol_cap" line comes only when every measured criterion is met and last
    week's enrolment reached CAP_BINDING of the cap. Beside them, each email source's bounces per contact over the
    kill rule's 7 days, next to the per-send figure source_bounce_rate pauses on (D19 decides which it should use).
  * By explore arm (random or ranked: Harry's D7, in place of the tiers, W2-2), angle, industry group, sender and
    step: last week's activity, and for each the companies emailed so far and how many replied (v_readout_weekly;
    the step has activity only). Monitoring only: the arms are read once, at 300 matured in the random arm (L8).
  * Cohorts (learn/cohorts.py; Harry, 7 Oct 2026): the last four enrolment weeks in a line each, at the latest age
    their companies have reached after email 1, and what changed last week in the settings, the copy, the campaign
    constants and the code (config versions, and the campaign changes made under leads in flight). No pair is judged
    (W2-3: the verdicts were chance at these numbers). `us-outbound report cohorts` has the full table.
  * Hand-check (enrol/hand_check.readout_lines; Harry's D5, 19 Oct 2026): last week's sample, how many were judged
    and each decision counted, the reasons for leaving out, and the companies sent unjudged (released 7 days after
    the draw) or still held. Counts only: at 10 to 20 rows a week a rate's interval would be about ±25 points.
  * ICP precision (learn/precision.readout_lines; W3-2, 20 Oct 2026): T1, the hand-check's random rows judged fine
    pooled over four weeks, with its Wilson interval and by gate version; the leave-out reasons (T2); the contact
    and opener answers; a split only when every cell has 15 rows.
  * Industry labels (learn/label_metrics.readout_lines; W3-2): last week's label check and corrections; the label
    sample's exact and group precision (T3) with intervals and the label-copy emails apart; the label policy and
    whether it is applied (General copy_level_policy, D20); the hand-check's wrong-industry rows. (The cards' "right
    N of N" went with them in W2-1.)
  * Signals (learn/signal_review.py, descriptive from W2-3): the explore arm and the signals with 15 companies a side,
    replies with against without, each with its Jeffreys interval, as monitoring and never a "Needs action" item
    (v_signal_value and its below_control flag went: with equal true rates it flagged about 46% of signals,
    metrics-audit H5).
  * Tests: each running test that reached a pre-registered look in the last week, with its read, or how far it
    has got (learn/looks.py).
Small numbers are shown as small: a company-level rate on under MIN_COMPANIES (30) companies is "too few to
read", given as counts only, and the bounce criterion is "too early" on under MIN_CONTACTS (100) contacts.

The numbers by cut, the companies so far and the signals come from the readout views (sql/views); on a store
without views (a MemoryStore with no handler) those sections say so and the rest still posts. The readout only
reads and posts: it writes nothing but its heartbeat and, through base/notify.post_once, the record of an ask it
made. The job (run) first makes Monday's asks, as the daily post does on the other days and there is no Monday post
since 19 Oct 2026: the credit and spend asks (learn/spend.check) and the mailboxes to add three weeks ahead
(learn/spend.mailboxes_check), each once (post_once). The post goes to the dev channel until live_sending = yes.
`us-outbound report readout` prints it without posting.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from us_outbound import budget, fmt
from us_outbound.base import notify, say
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import UK, Context
from us_outbound.learn import cohorts, kill_rules, looks, outcomes, signal_review, spend
from us_outbound.learn.stats import wilson
from us_outbound.logs import log
from us_outbound.replies import kinds
from us_outbound.timeparse import iso_date, utc

JOB = "monday_readout"
MIN_COMPANIES = outcomes.MIN_MATURED  # 30: a rate on fewer companies is "too few to read"
CAP_BINDING = 0.9  # last week's enrolments at 90% of weekly_enrol_cap or more: the limit held sending back
MIN_CONTACTS = 100  # the bounce criterion is judged from 100 contacts sent email 1 (12 Oct 2026: 13 sends read "met")
BOUNCE_LIMIT = 0.02  # exit criteria to scale (docs/roadmap.md §3): bounces under 2% of the contacts sent email 1
REPLY_DAYS, MEETING_DAYS = outcomes.REPLY_DAYS, outcomes.MEETING_DAYS  # replies judged at 28 days, meetings at 45
SIGNAL_LINES = 5  # signals named in the readout; `report signals` has every one
# SPEC 12's working assumption for the funnel, printed beside each rate and never judged against it: (the key, what
# it is called, the assumption as written).
TARGETS = (("replies", "replies", "3–5%"), ("positive", "positive", "about 1%"), ("meetings", "meetings", "0.5–1%"))
UNCLASSIFIED_AFTER = timedelta(hours=1)  # poll_replies classifies a reply within 15 minutes
WEEKLY_SQL = "SELECT * FROM {schema}.v_readout_weekly"
# Each cut, and what it splits by in a few words for a reader who has not seen it.
CUTS = (("arm", "By explore arm", "random: picked at random from the queue; ranked: in score order"),
        ("angle", "By angle", "the pitch of the copy"),
        ("industry_group", "By industry group", ""), ("sender", "By sender", "whose mailbox sent it"),
        ("step", "By step", "which email of the sequence"))
CUT_LIMIT = 8  # values listed per cut, the busiest first
REPLY_ITEM, OOO_ITEM, OPT_OUT = "reply:", "out_of_office:", "unsubscribed:"  # poll_replies' and optout's ids


def rate(k: int, n: int, *, minimum: int = MIN_COMPANIES) -> str:
    """"4.2%", or "too few to read" when the denominator is under the minimum (Harry, 6 Oct 2026)."""
    return fmt.pct(k, n) if n >= minimum else "too few to read"


def last_week(ctx: Context) -> tuple[datetime, datetime]:
    """[Monday 00:00, the next Monday 00:00), UK time, of the last whole week before now."""
    this = budget.week_start(ctx.now)
    return budget.week_start(this - timedelta(days=3)), this


# -- the events, for last week's headline and the exit criteria ---------------------------------------------------


class Recorded:
    """The events the readout counts, read once."""

    def __init__(self, ctx: Context):
        rows = ctx.store.select("events", {"type": ["sent", "bounced", "replied", "unsubscribed", "meeting_booked"]})
        self.by_type: dict[str, list[dict]] = defaultdict(list)
        for e in rows:
            if utc(e.get("occurred_at")) is not None:
                self.by_type[str(e.get("type"))].append(e)

    def between(self, type_: str, start: datetime, end: datetime) -> list[dict]:
        return [e for e in self.by_type[type_] if start <= utc(e["occurred_at"]) < end]


def _company(e: Mapping[str, Any]) -> str:
    """The company an event belongs to; an event with no account counts by itself."""
    return str(e.get("account_id") or f"event:{e.get('event_id')}")


def week_numbers(ev: Recorded, start: datetime, end: datetime) -> dict[str, int]:
    """Last week's numbers. Replies and positive replies are companies, each once however many emails it sent
    (metrics-audit H3); reply_emails keeps the emails."""
    replies = [e for e in ev.between("replied", start, end) if kinds.is_human(e.get("reply_class"))]
    return {
        "sent": len(ev.between("sent", start, end)),
        "replies": len({_company(e) for e in replies}),
        "positive": len({_company(e) for e in replies if kinds.is_warm(e.get("reply_class"))}),
        "reply_emails": len(replies),
        "meetings": len({_company(e) for e in ev.between("meeting_booked", start, end)}),
        "bounces": len(ev.between("bounced", start, end)),
        "unsubscribes": len(ev.between("unsubscribed", start, end)),
    }


def week_line(n: Mapping[str, int]) -> str:
    """"Last week: 40 emails sent · 3 companies replied (1 positive) · 1 meeting booked · 1 bounce · 0 unsubscribes",
    with the reply emails when a company wrote more than once ("3 companies replied (1 positive; 5 reply emails)")."""
    emails = f"; {fmt.plural(n['reply_emails'], 'reply email')}" if n["reply_emails"] != n["replies"] else ""
    return (f"*Last week:* {fmt.plural(n['sent'], 'email')} sent · "
            f"{fmt.plural(n['replies'], 'company', 'companies')} replied ({n['positive']:,} positive{emails}) · "
            f"{fmt.plural(n['meetings'], 'meeting')} booked · "
            f"{fmt.plural(n['bounces'], 'bounce')} · {fmt.plural(n['unsubscribes'], 'unsubscribe')}")


def with_interval(k: int, n: int) -> str:
    """"5.7% (2 of 35; 95% 1.6–18.6)": the rate, its counts and its Wilson 95% interval in percent."""
    low, high = wilson(k, n)
    return f"{k / n:.1%} ({k:,} of {n:,}; 95% {low * 100:.1f}–{high * 100:.1f})"


def _estimate(k: int, n: int) -> dict[str, Any]:
    low, high = wilson(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4), "low": round(low, 4), "high": round(high, 4)}


@dataclass
class Matured:
    """The companies emailed so far (v_account_outcomes), and those whose windows have closed: delivered companies
    28 days after email 1 for replies (its window_closed), 45 days after for meetings."""

    emailed: int = 0
    delivered: int = 0
    replied: int = 0  # among all delivered, windows open or closed: counts only
    positive: int = 0
    meetings: int = 0
    at_28: int = 0  # delivered companies that have had the 28 days
    replied_28: int = 0
    positive_28: int = 0
    at_45: int = 0  # delivered companies that have had 45 days
    meetings_45: int = 0  # a first meeting booked within 45 days of email 1

    def targets(self) -> dict[str, Any] | None:
        """Each rate with its interval, or None under MIN_COMPANIES matured companies (no rate, no verdict)."""
        if self.at_28 < MIN_COMPANIES:
            return None
        return {"matured": self.at_28,
                "replies": _estimate(self.replied_28, self.at_28), "positive": _estimate(self.positive_28, self.at_28),
                "meetings": _estimate(self.meetings_45, self.at_45) if self.at_45 >= MIN_COMPANIES else None}


def matured(ctx: Context) -> Matured | None:
    """From learn/outcomes.per_company (one per company sent email 1; the view's Python twin, W2-3)."""
    m = Matured()
    for o in outcomes.per_company(ctx):
        m.emailed += 1
        if not o.delivered:
            continue
        m.delivered += 1
        m.replied += o.replied_in_window
        m.positive += o.positive_in_window
        m.meetings += o.meeting_any
        if o.window_closed_28:
            m.at_28 += 1
            m.replied_28 += o.replied_in_window
            m.positive_28 += o.positive_in_window
        if o.window_closed_45:
            m.at_45 += 1
            m.meetings_45 += o.meeting_in_45
    return m


def target_line(m: Matured | None) -> str:
    """The summary's line on the rates so far: judged only on companies whose reply window has closed. Under
    MIN_COMPANIES of them, counts and how many have had the 28 days, and no rate; from 30, each rate with its
    interval and SPEC 12's assumption beside it, and no "on track" or "below" (metrics-audit H2)."""
    if m is None:
        return "*So far:* the rates need the database's views (v_account_outcomes), which this store does not have."
    if not m.emailed:
        return "*So far:* no company emailed yet."
    d = m.delivered
    had = (f"{m.at_28:,} of {fmt.plural(d, 'company', 'companies')} emailed have had the full {REPLY_DAYS} days "
           "after email 1 to reply")
    if m.at_28 < MIN_COMPANIES:
        return (f"*So far:* {had}, too few to give a rate (it takes {MIN_COMPANIES}). Counts so far, windows still "
                f"open: {m.replied:,} replied · {m.positive:,} positive · {fmt.plural(m.meetings, 'meeting')}.")
    parts = [f"replies {with_interval(m.replied_28, m.at_28)}", f"positive {with_interval(m.positive_28, m.at_28)}"]
    if m.at_45 >= MIN_COMPANIES:
        parts.append(f"meetings within {MEETING_DAYS} days {with_interval(m.meetings_45, m.at_45)}")
    else:
        parts.append(f"meetings: {m.at_45:,} have had {MEETING_DAYS} days, too few to give a rate")
    assumed = ", ".join(f"{name} {target}" for _, name, target in TARGETS)
    return f"*So far* ({had}): {' · '.join(parts)}. SPEC 12 assumes {assumed}."


def targets(ctx: Context, total: Mapping[str, int] | None, enrolled_last_week: int) -> list[str]:
    """The counts behind the summary's rates, last week's enrolment against its limit, and the stop rule."""
    g = ctx.settings.general
    out = [f"  Enrolled last week: {fmt.plural(enrolled_last_week, 'person', 'people')}, of the "
           f"{g.weekly_enrol_cap:,} allowed a week (`weekly_enrol_cap`)"]
    if total is None or not total["accounts_enrolled"]:
        return out
    d, meetings = total["accounts_delivered"], total["accounts_meeting"]
    out.append(f"  Companies emailed so far: {total['accounts_enrolled']:,} ({d:,} delivered) · "
               f"{total['accounts_replied']:,} replied · {total['accounts_positive']:,} positive · "
               f"{fmt.plural(meetings, 'meeting')}")
    out.append(f"  Stop rule: {fmt.plural(meetings, 'meeting')} from the first {fmt.plural(d, 'company', 'companies')}; "
               f"enrolment pauses by itself if the first {g.stop_rule_accounts:,} bring fewer than "
               f"{g.stop_rule_meetings}")
    return out


# -- the exit criteria to scale (docs/roadmap.md §3) ---------------------------------------------------------------


def criteria_lines(checks: Sequence[tuple[bool, str, str, str]]) -> list[str]:
    """The met criteria on one line, then each one not met on its own, with a What to do line when it has one:
    "  Met: bounces under 2% (1 of 150 sends, 0.7%) · no spam complaints"."""
    met = [f"{name} ({detail})" if detail else name for ok, name, detail, _ in checks if ok]
    out = [f"  Met: {' · '.join(met)}"] if met else []
    for ok, name, detail, do in checks:
        if not ok:
            out += [f"  *Not met:* {name}: {detail}", *([f"    *What to do:* {do}"] if do else [])]
    return out


def exit_criteria(ctx: Context, ev: Recorded, start: datetime, end: datetime, enrolled_last_week: int = 0
                  ) -> tuple[list[str], dict[str, bool], list[str]]:
    """(the section's lines, met by criterion, what needs someone in a few words each, for the summary)."""
    checks: list[tuple[bool, str, str, str]] = []  # (met, name, its numbers, what to do)
    met: dict[str, bool] = {}
    todo: list[str] = []
    general = say.sheet_link(ctx, "General")

    # Per contact sent email 1, not per send (metrics-audit H4): a bounced lead stops while a delivered one gets up to
    # three more emails, so 2 bounces in 48 contacts (4.2%) read as 2 in 187 sends (1.1%, "met") once the
    # sequences finished. Met only from MIN_CONTACTS (Harry, 12 Oct 2026: 0 bounces in 13 sends read as "met", and
    # the readout then said to raise the weekly limit).
    n, b = bounce_contacts(ev)
    met["bounces"] = n >= MIN_CONTACTS and b / n < BOUNCE_LIMIT
    name = "bounces under 2% of the contacts sent email 1"
    if not n:
        checks.append((False, name, "no email sent yet", ""))
    elif n < MIN_CONTACTS:
        checks.append((False, name, f"too early: {n:,} of {MIN_CONTACTS} contacts sent email 1 so far, "
                                    f"{fmt.plural(b, 'bounce')}", ""))
    else:
        detail = f"{b:,} of {n:,} contacts, {fmt.pct(b, n)} (95% {_span(b, n)})"
        if met["bounces"] and wilson(b, n)[1] >= BOUNCE_LIMIT:
            detail += f"; the interval still reaches 2%: {n:,} contacts cannot show the rate is under it"
        checks.append((met["bounces"], name, detail, ""))

    # An opt-out is recorded everywhere (suppression, Instantly's blocklist, HubSpot) once its unsubscribed event
    # exists (replies/optout.py); a contact suppressed for unsubscribe without one is still pending.
    done = {str(e.get("contact_id")) for e in ev.by_type["unsubscribed"] if e.get("contact_id")}
    pending = [c for c in ctx.store.select("contacts", {"suppressed_reason": "unsubscribe"})
               if str(c.get("contact_id")) not in done]
    tested = ctx.settings.general.optout_tested
    met["unsubscribes"] = tested and not pending
    why, do = [], ""
    if not tested:
        why.append("the unsubscribe-link test has not passed (`optout_tested` is no)")
        do = (f"{say.command('seed send ADDRESS --owner NAME --live')} to an inbox of ours, click the email's "
              f"unsubscribe link, then {say.command('seed check')}; at PASS, set `optout_tested` to yes on the "
              f"{general}")
        todo.append("test the unsubscribe link")
    if pending:
        why.append(f"{fmt.plural(len(pending), 'opt-out')} not yet in Instantly's blocklist and HubSpot (retried by "
                   f"the next live run of {say.job_name('sync_outcomes')} or {say.job_name('poll_replies')})")
    n_unsub = len(ev.by_type["unsubscribed"])
    recorded = (f"{fmt.plural(n_unsub, 'opt-out')} so far, each in suppression, Instantly's blocklist and HubSpot"
                if n_unsub else "no opt-out so far")
    checks.append((met["unsubscribes"], "unsubscribes work end to end",
                   "; ".join(why) if why else f"link tested; {recorded}", do))

    replies = ev.by_type["replied"]
    items = {str(i.get("item_id")) for i in ctx.store.select("hitl_items", {"kind": ["reply", "out_of_office"]})}
    opted = {str(e.get("event_id")) for e in ev.by_type["unsubscribed"]}
    late = ctx.now - UNCLASSIFIED_AFTER
    unclassified = [e for e in replies if not e.get("reply_class") and utc(e["occurred_at"]) < late]
    unrouted = [e for e in replies if e.get("reply_class")
                and not {f"{REPLY_ITEM}{e['event_id']}", f"{OOO_ITEM}{e['event_id']}"} & items
                and f"{OPT_OUT}{e['event_id']}" not in opted]
    met["replies"] = not unclassified and not unrouted
    if met["replies"]:
        checks.append((True, "every reply sorted and routed", f"{len(replies):,} so far", ""))
    else:
        why = ([f"{len(unclassified)} not sorted (positive, objection, …) after an hour"] if unclassified else []) + (
            [f"{len(unrouted)} with no reply card in Slack, out-of-office record or opt-out"] if unrouted else [])
        stuck = len(unclassified) + len(unrouted)
        checks.append((False, "every reply sorted and routed", "; ".join(why),
                       f"look at them with {say.command('replies list')}"))
        todo.append(f"check {fmt.plural(stuck, 'reply', 'replies')}")

    week = [h for h in ctx.store.select("heartbeats", {"status": "error"})
            if (t := utc(h.get("started_at"))) is not None and start <= t < end]
    refusals = [h for h in week if isinstance(h.get("detail"), Mapping) and h["detail"].get("violation")]
    blocked = 0
    for h in ctx.store.select("heartbeats", {"job": "enrol"}):
        t, d = utc(h.get("started_at")), h.get("detail")
        if t is not None and start <= t < end and isinstance(d, Mapping) and isinstance(d.get("skipped"), Mapping):
            blocked += int(d["skipped"].get("copy blocked") or 0)
    held = f"{fmt.plural(blocked, 'email')} held back by the copy rules, each with its reason" if blocked else ""
    met["refusals"] = not refusals
    name = "no unexplained safety-check refusal"
    if refusals:
        jobs = ", ".join(sorted({say.job_name(str(h.get("job"))) for h in refusals}))
        checks.append((False, name, f"{fmt.plural(len(refusals), 'refusal')} last week, in {jobs}"
                       + (f"; {held}" if held else ""),
                       f"read each error with {say.command('status')} or in {say.logs_link()}"))
        todo.append(f"explain {fmt.plural(len(refusals), 'refusal')}")
    else:
        checks.append((True, name, held, ""))

    out = criteria_lines(checks)
    # Nothing writes a complained event (metrics-audit H1), so "no spam complaints" was a constant met: it is out of
    # the count until a feed exists (D11: Google Postmaster Tools, or seed placement checks).
    out.append("  Not measured: spam complaints (nothing reports them to us yet), so they are not counted as met")
    cap = ctx.settings.general.weekly_enrol_cap
    if all(met.values()) and enrolled_last_week >= cap * CAP_BINDING:  # the limit held sending back: raising it helps
        out.append(f"  *What to do:* all {len(met)} measured are met and last week reached the weekly limit, so "
                   f"sending can grow: raise `weekly_enrol_cap` (now {cap:,}) on the {general} in step with the "
                   "mailbox ramp")
        todo.append("decide whether to raise the weekly limit")
    elif all(met.values()):
        out.append(f"  All {len(met)} measured are met. The weekly limit is not what holds sending back "
                   f"({enrolled_last_week:,} of {cap:,} last week): the mailboxes' warm-up is, and it rises by itself.")
    return out, met, todo


def _key(e: Mapping[str, Any]) -> str:
    """The contact an event is about (its account, else the event itself, when a row has no contact)."""
    if e.get("contact_id"):
        return str(e["contact_id"])
    return f"account:{e['account_id']}" if e.get("account_id") else f"event:{e.get('event_id')}"


def bounce_contacts(ev: Recorded) -> tuple[int, int]:
    """(contacts sent email 1, how many of them have bounced), all time: the exit criterion's per-contact count."""
    sent = {_key(e) for e in ev.by_type["sent"] if e.get("step") in (1, "1")}
    bounced = {_key(e) for e in ev.by_type["bounced"]} & sent
    return len(sent), len(bounced)


def _span(k: int, n: int) -> str:
    """The Wilson 95% interval in percent: "0.5–5.9%"."""
    low, high = wilson(k, n)
    return f"{low * 100:.1f}–{high * 100:.1f}%"


def source_lines(ctx: Context) -> tuple[list[str], dict[str, Any]]:
    """Each email source's bounces over the kill rule's window, per contact sent email 1 beside the per-send rate
    source_bounce_rate pauses on (D19: whether the rule should count contacts is Harry's, a SPEC 12 change; at 4.2% per
    contact it would pause the only email source). Monitoring: it changes nothing."""
    start = ctx.now - timedelta(days=kill_rules.WINDOW_DAYS)
    recent = kill_rules.recent_sends(ctx.store, ctx.now, start)
    ids = sorted({s.contact_id for s in recent if s.contact_id})
    source = {str(c["contact_id"]): str(c.get("email_source") or "").strip().lower()
              for i in range(0, len(ids), 1000) for c in ctx.store.select("contacts", {"contact_id": ids[i : i + 1000]})}
    out: list[str] = []
    nums: dict[str, Any] = {}
    for src in kill_rules.SOURCES:
        mine = [s for s in recent if source.get(s.contact_id) == src]
        if not mine:
            continue
        sends, bounced_sends = len(mine), sum(s.bounced for s in mine)
        first = {s.contact_id for s in mine if s.step in (1, "1")}
        bounced = {s.contact_id for s in mine if s.bounced} & first
        n, b = len(first), len(bounced)
        per_contact = (f"{b:,} of {fmt.plural(n, 'contact')} sent email 1 bounced ({fmt.pct(b, n)}; 95% {_span(b, n)})"
                       if n else "no contact sent email 1")
        out.append(f"  Emails found by {src}, last {kill_rules.WINDOW_DAYS} days: {per_contact} · the safety pause "
                   f"counts sends: {bounced_sends:,} of {fmt.plural(sends, 'send')} ({fmt.pct(bounced_sends, sends)}), "
                   f"and pauses the source over {kill_rules.BOUNCE_RATE:.0%} from {kill_rules.MIN_SENDS} sends")
        nums[src] = {"contacts": n, "bounced_contacts": b, "sends": sends, "bounced_sends": bounced_sends}
    return out, nums


# -- the readout views ---------------------------------------------------------------------------------------------


def weekly_rows(ctx: Context) -> list[dict] | None:
    """v_readout_weekly's rows, or None when the store has no views."""
    try:
        return [dict(r) for r in ctx.store.query(WEEKLY_SQL.format(schema=ctx.store.schema))]
    except NotImplementedError:
        return None


COHORT = ("accounts_enrolled", "accounts_delivered", "accounts_replied", "accounts_positive", "accounts_meeting",
          "accounts_window_closed")
ACTIVITY = ("sends", "bounces", "replies", "positive_replies", "meetings_booked", "unsubscribes")


def so_far(rows: Iterable[Mapping[str, Any]], cut: str = "all") -> dict[str, Counter]:
    """cut_value -> the cohort columns summed over every week (the companies emailed so far)."""
    out: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.get("cut") == cut:
            out[str(r.get("cut_value"))].update({k: int(r.get(k) or 0) for k in COHORT})
    return out


def in_week(rows: Iterable[Mapping[str, Any]], start: datetime, cut: str) -> dict[str, Counter]:
    """cut_value -> last week's activity columns."""
    out: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.get("cut") == cut and iso_date(r.get("week_start")) == start.date():
            out[str(r.get("cut_value"))].update({k: int(r.get(k) or 0) for k in ACTIVITY})
    return out


def activity(w: Mapping[str, int]) -> str:
    """Last week's activity in a cut: "30 sent, 2 replies (1 positive), 1 meeting, 1 bounce"; zeros left out."""
    out = f"{w['sends']:,} sent"
    if w["replies"]:
        out += f", {fmt.plural(w['replies'], 'reply', 'replies')}" + (
            f" ({w['positive_replies']:,} positive)" if w["positive_replies"] else "")
    if w["meetings_booked"]:
        out += f", {fmt.plural(w['meetings_booked'], 'meeting')}"
    if w["bounces"]:
        out += f", {fmt.plural(w['bounces'], 'bounce')}"
    return out


def cut_lines(rows: Sequence[Mapping[str, Any]], cut: str, title: str, about: str, start: datetime) -> list[str]:
    week, total = in_week(rows, start, cut), so_far(rows, cut)
    values = [v for v in set(week) | set(total) if any(week[v].values()) or total[v]["accounts_enrolled"]]
    if not values:
        return []
    values.sort(key=lambda v: (-week[v]["sends"], -total[v]["accounts_enrolled"], v))
    out = [f"_{title}_" + (f" ({about})" if about else "")]
    if cut == "step":
        return out + [f"  {v}: {activity(week[v])}" for v in sorted(values)]
    for v in values[:CUT_LIMIT]:
        t = total[v]
        d, k = t["accounts_delivered"], t["accounts_replied"]
        out.append(f"  {v}: {activity(week[v])} · {t['accounts_enrolled']:,} emailed so far, {k:,} replied "
                   f"({rate(k, d)})")
    if len(values) > CUT_LIMIT:
        out.append(f"  and {len(values) - CUT_LIMIT} more")
    return out


def signal_section(ctx: Context) -> tuple[list[str], int | None]:
    """The Signals section's lines and how many signal splits are readable (None when it could not be built). The
    review is monitoring, so a failure in it says so in its section and never stops the readout (W2-3 r1)."""
    try:
        signals = signal_review.review(ctx)
    except GuardViolation:
        raise
    except Exception as exc:
        log("readout_signals_failed", run_id=ctx.run_id, error=f"{type(exc).__name__}: {exc}"[:200])
        return [f"  could not be built ({type(exc).__name__}); the rest of the readout is complete."], None
    text = signal_review.lines(signals, SIGNAL_LINES)
    # The first line is the review's own summary, which the readout says elsewhere; with nothing emailed it is the
    # only line ("No company has been emailed yet"), so it stays.
    return [f"  {x}" for x in (text[1:] if signals.companies else text)], sum(sp.readable for sp in signals.splits)


def precision_section(ctx: Context) -> tuple[list[str], dict[str, Any]]:
    """The ICP precision block (learn/precision.readout_lines) and its numbers: T1 pooled with its interval, the
    reasons, the gate versions. Monitoring: a failure says so in its section and never stops the readout."""
    from us_outbound.learn import precision

    try:
        t = precision.tally(ctx)
        lines = precision.readout_lines(ctx, t)
    except GuardViolation:
        raise
    except Exception as exc:
        log("readout_precision_failed", run_id=ctx.run_id, error=f"{type(exc).__name__}: {exc}"[:200])
        return ["", "*ICP precision*", f"  could not be built ({type(exc).__name__}); the rest of the readout is "
                                       "complete."], {}
    k, n = t.fine
    return lines, {"weeks": list(t.weeks), "fine": k, "judged": n,
                   "interval": [round(x, 4) for x in t.interval] if n else None, "reasons": t.reasons,
                   "by_gate": {g: list(v) for g, v in t.by_gate.items()}, "wrong_industry": (t.wrong_industry, n)}


def label_section(ctx: Context, start: datetime, end: datetime,
                  wrong_industry: tuple[int, int] | None) -> list[str]:
    """The Industry labels block (learn/label_metrics.readout_lines), in its own guard."""
    from us_outbound.learn import label_metrics

    try:
        return label_metrics.readout_lines(ctx, start, end, wrong_industry)
    except GuardViolation:
        raise
    except Exception as exc:
        log("readout_labels_failed", run_id=ctx.run_id, error=f"{type(exc).__name__}: {exc}"[:200])
        return ["", "*Industry labels*", f"  could not be built ({type(exc).__name__}); the rest of the readout is "
                                         "complete."]


# -- the job -------------------------------------------------------------------------------------------------------


def build(ctx: Context) -> tuple[list[str], dict[str, Any]]:
    """(the message lines, the numbers for the summary)."""
    start, end = last_week(ctx)
    ev = Recorded(ctx)
    week = week_numbers(ev, start, end)
    rows = weekly_rows(ctx)
    total = so_far(rows)["all"] if rows is not None else None
    enrolled = [c for c in ctx.store.select("contacts") if c.get("enrolled_at")]
    enrolled_last_week = budget.enrolled_this_week(enrolled, start + timedelta(days=3))

    criteria, met, todo = exit_criteria(ctx, ev, start, end, enrolled_last_week)
    sources, by_source = source_lines(ctx)
    body = ["", "*Against the targets*", *targets(ctx, total, enrolled_last_week),
            "", f"*Exit criteria to scale* (all {len(met)} measured must be met before sending grows): "
                f"{sum(met.values())} of {len(met)} met", *criteria, *sources]
    if rows is not None:
        cut_block = [line for cut, title, about in CUTS for line in cut_lines(rows, cut, title, about, start)]
        if cut_block:
            body += ["", "*Cuts* (the numbers split several ways: last week · companies emailed so far)", *cut_block]
    section, by_cohort = cohorts.readout_lines(ctx, start, end)
    body += section
    from us_outbound.enrol import hand_check  # imported when used, as mailboxes_ahead imports enrol/today

    checked, by_hand = hand_check.readout_lines(ctx, start)
    body += checked
    # ICP precision (T1, T2) pooled over four weeks, and the industry labels (the check, the label sample's T3, the
    # label policy): W3-2, each in its own guard, so neither stops the readout.
    t1, precision_nums = precision_section(ctx)
    body += t1
    body += label_section(ctx, start, end, precision_nums.get("wrong_industry"))
    # Monitoring, not a decision (metrics-audit H5; W2-3): counts and intervals, no verdict, never a "Needs action".
    signal_lines, signals_readable = signal_section(ctx)
    body += ["", "*Signals* (replies with each buying signal at enrolment against without, and the random arm against "
                 "the ranked, with 95% intervals)", *signal_lines]
    tests, at_look = looks.readout_lines(ctx, ctx.now - timedelta(days=7))
    body += ["", "*Tests* (copy tests, each read only at points fixed in advance on the Tests tab, called looks)",
             *tests]
    todo += [f"decide on test {t}" for t in at_look]

    days = cohorts.week_days(start.astimezone(UK).date())
    if todo:
        head = say.compose(say.ACTION, f"Monday readout, {days}: {fmt.plural(len(todo), 'thing')} to act on")
    else:
        head = say.compose(say.INFO, f"Monday readout, {days}: nothing to act on")
    m = matured(ctx)
    lines = [head, week_line(week), target_line(m)]
    if todo:
        lines.append(f"*Needs action:* {' · '.join(todo)} (details below)")
    lines += body
    n, b = bounce_contacts(ev)
    nums: dict[str, Any] = {
        "week_start": start.astimezone(UK).date().isoformat(), "last_week": week,
        "enrolled_last_week": enrolled_last_week, "so_far": dict(total) if total is not None else None,
        "matured": {"emailed": m.emailed, "delivered": m.delivered, "at_28": m.at_28, "at_45": m.at_45}
        if m is not None else None,
        "targets": m.targets() if m is not None else None,
        "exit_criteria": met, "views": rows is not None, "to_act_on": todo,
        "bounces": {"contacts": n, "bounced": b, **({"interval": [round(x, 4) for x in wilson(b, n)]} if n else {})},
        "source_bounces": by_source,
        "signals_readable": signals_readable,
        "tests_at_a_look": at_look,
        "hand_check": by_hand,
        "icp_precision": precision_nums,
        **by_cohort,  # the counts by enrolment week, and how many settings changes last week
    }
    return lines, nums


def mailboxes_ahead(ctx: Context) -> dict[str, Any]:
    """Monday's ask for mailboxes three weeks ahead (learn/spend.mailboxes_check), with the companies ready to email
    counted as the daily post counts them (enrol/today.py; campaigns not read, so no Instantly call). Never raises
    (but a GuardViolation): the readout still posts."""
    try:
        from us_outbound.enrol import today

        ready = today.read(ctx, campaigns=False).ready_count
    except GuardViolation:
        raise
    except Exception as exc:
        log("capacity_ahead_failed", error=str(exc)[:200])
        return {"checked": False, "capacity_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return spend.mailboxes_check(ctx, ready)


def run(ctx: Context) -> dict:
    """The monday_readout job (JOB CONTRACT: run(ctx) -> summary): the numbers, not the text.

    Monday's asks go first, as the daily post makes them on the other days (there is no Monday post since 19 Oct
    2026), so a readout that fails below never holds them: the credit and spend asks (spend.check, each line once a
    day or a month as before) and the mailboxes to add three weeks ahead (once an ISO week)."""
    _, asked = spend.check(ctx)
    mailboxes = mailboxes_ahead(ctx)
    lines, nums = build(ctx)
    sent = notify.alert(ctx, "\n".join(lines))
    summary = {"job": JOB, "dry_run": ctx.dry_run, **nums, "alert": sent, "spend_alert": asked,
               "mailboxes_ahead": mailboxes}
    log("monday_readout_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "so_far"})
    return summary
