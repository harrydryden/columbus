"""monday_readout, Mondays 08:30 UK (SPEC 9, 12 "Monday readout"; Harry, 6 Oct 2026: "push ahead with building").

One message to the alert channel (#us-outbound; the dev channel in dry-run, base/notify.py), after the weekly
hand-check (08:00) and before the daily post (09:00), about the week before (Monday to Sunday, UK time), written for
a reader who may be new (Harry, 12 Oct 2026; base/say.py). It opens with a short summary: a headline that says how
many things need someone, last week's numbers, the rates so far against the targets, and what needs action. Each
section then says in a few words what it measures, and anything that needs action says what to do and where. Top
to bottom:
  * Last week: emails sent, replies (human: every class but out-of-office) and positive replies (positive or
    referral), meetings booked (companies with a meeting_booked event: crm/readback.py, a meeting on Harry's
    calendar or a Spill 3.0 deal at Demo requested or later), bounces and unsubscribes, from the events table;
    then the companies emailed so far and how many of their 28-day reply windows have closed.
  * Against the targets: companies enrolled last week against General weekly_enrol_cap; the reply, positive and
    meeting rates so far against SPEC 12's working assumption (3 to 5%, about 1%, 0.5 to 1%); the stop rule's
    progress (stop_rule_meetings from the first stop_rule_accounts companies).
  * The exit criteria to scale (docs/roadmap.md §3): bounces under 2% of sends; no complaints (events type
    complained); unsubscribes confirmed end to end (optout_tested = yes, and every opt-out recorded everywhere:
    replies/optout.py writes its unsubscribed event only then); every reply classified and routed (a class within
    UNCLASSIFIED_AFTER, and a reply card, an out-of-office record or an opt-out for each); no unexplained refusal
    (no guard refusal in last week's heartbeats; the emails the copy rules held back are counted, each has its
    reason in enrol's run summary). Each says met or not met, with its numbers.
  * By tier, angle, industry group, sender and step: last week's activity, and for each the companies emailed so
    far and how many replied (v_readout_weekly; the step has activity only).
  * Cohorts (learn/cohorts.py; Harry, 7 Oct 2026): the last four enrolment weeks in a line each, at the latest age
    their companies have reached after email 1; the latest two compared where both have 30 companies; and what
    changed last week in the settings, the copy, the campaign constants and the code (config versions, and the
    campaign changes made under leads in flight). `us-outbound cohorts` has the full table.
  * Industry labels (Harry, 7 Oct 2026; labels.py): last week's and all cards with the right industry against the 95%
    target (a rate from 30 cards), the approvers' corrections, how often the rules and the model agreed, and the
    companies held or disqualified.
  * The signal table's headline (v_signal_value, learn/signal_value.py).
  * Tests: each running test that reached a pre-registered look in the last week, with its read, or how far it
    has got (learn/looks.py).
Small numbers are shown as small: a company-level rate on under MIN_COMPANIES (30) companies is "too few to
read", given as counts only, and a bounce rate on under MIN_SENDS (100) sends says that one bounce moves it a lot.

The numbers by cut, the companies so far and the signals come from the readout views (sql/views); on a store
without views (a MemoryStore with no handler) those sections say so and the rest still posts. The readout only
reads: in dry-run and live alike it writes nothing but its heartbeat, and the post goes to the dev channel until
live_sending = yes. `us-outbound readout` prints it without posting.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from us_outbound import budget, fmt, labels
from us_outbound.base import notify, say
from us_outbound.context import UK, Context
from us_outbound.learn import cohorts, looks, signal_value
from us_outbound.learn.kill_rules import COMPLAINT
from us_outbound.logs import log
from us_outbound.replies import kinds
from us_outbound.scoring.score import TIER_WORDS
from us_outbound.timeparse import iso_date, utc

JOB = "monday_readout"
MIN_COMPANIES = signal_value.MIN_TO_READ  # 30: a rate on fewer companies is "too few to read"
CAP_BINDING = 0.9  # last week's enrolments at 90% of weekly_enrol_cap or more: the limit held sending back
MIN_SENDS = 100  # a bounce rate on fewer sends moves a lot with one bounce (kill_rules reads a domain from 100)
BOUNCE_LIMIT = 0.02  # exit criteria to scale (docs/roadmap.md §3): bounce rate under 2%
# SPEC 12's working assumption for the funnel: (the companies-so-far column, what it is called, the low end of the
# assumption, the assumption as written). A rate at or over its low end is "on track".
TARGETS = (("accounts_replied", "replies", 0.03, "3–5%"), ("accounts_positive", "positive", 0.01, "about 1%"),
           ("accounts_meeting", "meetings", 0.005, "0.5–1%"))
UNCLASSIFIED_AFTER = timedelta(hours=1)  # poll_replies classifies a reply within 15 minutes
WEEKLY_SQL = "SELECT * FROM {schema}.v_readout_weekly"
# Each cut, and what it splits by in a few words for a reader who has not seen it.
CUTS = (("tier", "By tier", "how well the company fits our profile"), ("angle", "By angle", "the pitch of the copy"),
        ("industry_group", "By industry group", ""), ("sender", "By sender", "whose mailbox sent it"),
        ("step", "By step", "which email of the sequence"))
CUT_LIMIT = 8  # values listed per cut, the busiest first
REPLY_ITEM, OOO_ITEM, OPT_OUT = "reply:", "out_of_office:", "unsubscribed:"  # poll_replies' and optout's ids
POSITIVE = kinds.WARM


def rate(k: int, n: int, *, minimum: int = MIN_COMPANIES) -> str:
    """"4.2%", or "too few to read" when the denominator is under the minimum (Harry, 6 Oct 2026)."""
    return fmt.pct(k, n) if n >= minimum else "too few to read"


def last_week(ctx: Context) -> tuple[datetime, datetime]:
    """[Monday 00:00, the next Monday 00:00), UK time, of the last whole week before now."""
    this = budget.week_start(ctx.now)
    return budget.week_start(this - timedelta(days=3)), this


# -- the events, for last week's headline and the exit criteria ---------------------------------------------------


class _Events:
    """The events the readout counts, read once."""

    def __init__(self, ctx: Context):
        rows = ctx.store.select("events", {"type": ["sent", "bounced", "replied", "unsubscribed", "meeting_booked",
                                                    COMPLAINT]})
        self.by_type: dict[str, list[dict]] = defaultdict(list)
        for e in rows:
            if utc(e.get("occurred_at")) is not None:
                self.by_type[str(e.get("type"))].append(e)

    def between(self, type_: str, start: datetime, end: datetime) -> list[dict]:
        return [e for e in self.by_type[type_] if start <= utc(e["occurred_at"]) < end]


def week_numbers(ev: _Events, start: datetime, end: datetime) -> dict[str, int]:
    replies = [e for e in ev.between("replied", start, end) if kinds.is_human(e.get("reply_class"))]
    return {
        "sent": len(ev.between("sent", start, end)),
        "replies": len(replies),
        "positive": sum(e.get("reply_class") in POSITIVE for e in replies),
        "meetings": len({e.get("account_id") or e["event_id"] for e in ev.between("meeting_booked", start, end)}),
        "bounces": len(ev.between("bounced", start, end)),
        "unsubscribes": len(ev.between("unsubscribed", start, end)),
    }


def week_line(n: Mapping[str, int]) -> str:
    """"Last week: 40 emails sent · 3 replies (1 positive) · 1 meeting booked · 1 bounce · 0 unsubscribes"."""
    return (f"*Last week:* {fmt.plural(n['sent'], 'email')} sent · {fmt.plural(n['replies'], 'reply', 'replies')} "
            f"({n['positive']:,} positive) · {fmt.plural(n['meetings'], 'meeting')} booked · "
            f"{fmt.plural(n['bounces'], 'bounce')} · {fmt.plural(n['unsubscribes'], 'unsubscribe')}")


def target_line(total: Mapping[str, int] | None) -> str:
    """The summary's line on the rates so far against SPEC 12's working assumption, with the 28-day reply window
    said in plain words while some windows are still open."""
    if total is None:
        return "*So far:* the rates need the database's views (v_readout_weekly), which this store does not have."
    emailed, d, closed = total["accounts_enrolled"], total["accounts_delivered"], total["accounts_window_closed"]
    if not emailed:
        return "*So far:* no company emailed yet."
    if d < MIN_COMPANIES:
        return (f"*So far:* {fmt.plural(d, 'company', 'companies')} emailed: too few to read a rate (it takes "
                f"{MIN_COMPANIES}).")
    parts = []
    for col, name, low, target in TARGETS:
        parts.append(f"{name} {fmt.pct(total[col], d)} ({target}: {'on track' if total[col] / d >= low else 'below'})")
    still = (f". Only {closed:,} of {d:,} companies have had the full 28 days after email 1 to reply, so these still "
             "rise") if closed < d else ""
    return f"*So far, against the targets:* {' · '.join(parts)}{still}."


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


def exit_criteria(ctx: Context, ev: _Events, start: datetime, end: datetime, enrolled_last_week: int = 0
                  ) -> tuple[list[str], dict[str, bool], list[str]]:
    """(the section's lines, met by criterion, what needs someone in a few words each, for the summary)."""
    checks: list[tuple[bool, str, str, str]] = []  # (met, name, its numbers, what to do)
    met: dict[str, bool] = {}
    todo: list[str] = []
    general = say.sheet_link(ctx, "General")

    sends, bounces = len(ev.by_type["sent"]), len(ev.by_type["bounced"])
    # Met only on enough sends to judge (Harry, 12 Oct 2026: 0 bounces in 13 sends read as "met", and the readout
    # then said to raise the weekly limit).
    met["bounces"] = sends >= MIN_SENDS and bounces / sends < BOUNCE_LIMIT
    if not sends:
        checks.append((False, "bounces under 2%", "no email sent yet", ""))
    elif sends < MIN_SENDS:
        checks.append((False, "bounces under 2%", f"too early to tell: {bounces:,} of {fmt.plural(sends, 'send')} so "
                                                  f"far; it is judged from {MIN_SENDS} sends", ""))
    else:
        checks.append((met["bounces"], "bounces under 2%",
                       f"{bounces:,} of {fmt.plural(sends, 'send')}, {fmt.pct(bounces, sends)}", ""))

    complaints = len(ev.by_type[COMPLAINT])
    met["complaints"] = complaints == 0
    checks.append((met["complaints"], "no spam complaints", fmt.plural(complaints, "complaint") if complaints else "",
                   ""))

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
    cap = ctx.settings.general.weekly_enrol_cap
    if all(met.values()) and enrolled_last_week >= cap * CAP_BINDING:  # the limit held sending back: raising it helps
        out.append(f"  *What to do:* all {len(met)} are met and last week reached the weekly limit, so sending can "
                   f"grow: raise `weekly_enrol_cap` (now {cap:,}) on the {general} in step with the mailbox ramp")
        todo.append("decide whether to raise the weekly limit")
    elif all(met.values()):
        out.append(f"  All {len(met)} are met. The weekly limit is not what holds sending back ({enrolled_last_week:,} "
                   f"of {cap:,} last week): the mailboxes' warm-up is, and it rises by itself.")
    return out, met, todo


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
        name = TIER_WORDS.get(v, v) if cut == "tier" else v
        out.append(f"  {name}: {activity(week[v])} · {t['accounts_enrolled']:,} emailed so far, {k:,} replied "
                   f"({rate(k, d)})")
    if len(values) > CUT_LIMIT:
        out.append(f"  and {len(values) - CUT_LIMIT} more")
    return out


# -- the job -------------------------------------------------------------------------------------------------------


def build(ctx: Context) -> tuple[list[str], dict[str, Any]]:
    """(the message lines, the numbers for the summary)."""
    start, end = last_week(ctx)
    ev = _Events(ctx)
    week = week_numbers(ev, start, end)
    rows = weekly_rows(ctx)
    total = so_far(rows)["all"] if rows is not None else None
    enrolled = [c for c in ctx.store.select("contacts") if c.get("enrolled_at")]
    enrolled_last_week = budget.enrolled_this_week(enrolled, start + timedelta(days=3))

    criteria, met, todo = exit_criteria(ctx, ev, start, end, enrolled_last_week)
    body = ["", "*Against the targets*", *targets(ctx, total, enrolled_last_week),
            "", f"*Exit criteria to scale* (all {len(met)} must be met before sending grows): "
                f"{sum(met.values())} of {len(met)} met", *criteria]
    if rows is not None:
        cut_block = [line for cut, title, about in CUTS for line in cut_lines(rows, cut, title, about, start)]
        if cut_block:
            body += ["", "*Cuts* (the numbers split several ways: last week · companies emailed so far)", *cut_block]
    section, by_cohort = cohorts.readout_lines(ctx, start, end)
    body += section
    body += labels.readout_lines(ctx, start, end)  # the industry label check (labels.py; Harry, 7 Oct 2026)
    table = signal_value.rows(ctx)
    flagged = signal_value.flagged(table)
    body += ["", "*Signal value* (reply rate with each buying signal at enrolment, against without; p: the chance the "
                 "gap is luck)",
             *signal_value.headline(table, signals_tab=say.sheet_link(ctx, "Signals"))]
    todo += [f"review the weight of {name}" for name in flagged]
    tests, at_look = looks.readout_lines(ctx, ctx.now - timedelta(days=7))
    body += ["", "*Tests* (copy tests, each read only at points fixed in advance on the Tests tab, called looks)",
             *tests]
    todo += [f"decide on test {t}" for t in at_look]

    days = cohorts.week_days(start.astimezone(UK).date())
    if todo:
        head = say.compose(say.ACTION, f"Monday readout, {days}: {fmt.plural(len(todo), 'thing')} to act on")
    else:
        head = say.compose(say.INFO, f"Monday readout, {days}: nothing to act on")
    lines = [head, week_line(week), target_line(total)]
    if todo:
        lines.append(f"*Needs action:* {' · '.join(todo)} (details below)")
    lines += body
    nums: dict[str, Any] = {
        "week_start": start.astimezone(UK).date().isoformat(), "last_week": week,
        "enrolled_last_week": enrolled_last_week, "so_far": dict(total) if total is not None else None,
        "exit_criteria": met, "views": rows is not None, "to_act_on": todo,
        "signals_readable": sum(signal_value.verdict(r) != signal_value.TOO_FEW for r in table or []
                                if int(r.get("accounts_sent") or 0)),
        "tests_at_a_look": at_look,
        **by_cohort,  # the counts by enrolment week, and how many settings changes last week
    }
    return lines, nums


def run(ctx: Context) -> dict:
    """The monday_readout job (JOB CONTRACT: run(ctx) -> summary): the numbers, not the text."""
    lines, nums = build(ctx)
    sent = notify.alert(ctx, "\n".join(lines))
    summary = {"job": JOB, "dry_run": ctx.dry_run, **nums, "alert": sent}
    log("monday_readout_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "so_far"})
    return summary
