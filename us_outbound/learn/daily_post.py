"""daily_post, 09:00 UK (SPEC 9, 11 "Daily post"): one message on yesterday's outbound and what waits for Harry.

Harry, 2 Oct 2026: "the number of emails sent, the number of companies and contacts identified, the
pipeline of companies to send to, the [un]subscribes, the replies, and anything else important to
surface to make the system better over time". It covers the last send day (Mondays cover Friday to
Sunday, so weekend replies are not lost), and reads top to bottom:
  * the headline: sent, replies (positive or referral among them), unsubscribes, companies and
    contacts found, and accounts ready to send, on one line;
  * Needs you (4 Oct 2026), right under it: the send approvals waiting for a ✅ (the oldest's age,
    and that a card lapses at the end of the next send day), the reply items waiting (the oldest's
    age), the kill-rule holds in force and an open hand-check; or "nothing";
  * Sent and outcomes: sends by step, replies by class, positive and referral replies, bounces,
    unsubscribes, demos booked and held (the events table; sync_outcomes, poll_replies and
    hubspot_readback write it); objections, negatives and "other" replies, out-of-office replies,
    and warm accounts: enrolled accounts that visited the US site (SPEC 11);
  * Approvals, Found, Pipeline and To improve (learn/daily_report.py): the Slack send approvals
    (left out until they exist), the companies and contacts identified in the period, what is
    ready to send now and what stands behind it, and what the data says to tune, each line only
    once there is enough data;
  * Today's number: what limits it, each sender's capacity, and the credit budgets (limits.py,
    the same lines `us-outbound status` prints), with the send approvals still waiting counted as
    enrol counts them (enrol/approvals.waiting: their accounts are not ready again, and they hold
    their senders' slots); and why enrollment waits today, from enrol.gate (a blackout or non-send
    day, an operator stop, the stop rule, a positive reply waiting too long), plus the hand-check
    only while auto_send = yes, and a note when live_sending is no (enrol then runs dry); and the
    second-contact switch (enrol/second.py; 6 Oct 2026): "Second contact: off", or how many are ready;
  * Sources: the careers and benefits page reader's coverage, its last run, every account read so
    far and the decision rule for enhancing it (sources/pages.py, `us-outbound pages show`); and
    what Apollo's organization enrich found, with funding in the last 180 and 365 days
    (sources/apollo_enrich.py); one line on website visits: the companies Apollo saw on the US
    pages, or, when its tracker sends no data, to check it (sources/site_visits.py); and, on the morning of
    the monthly lookalike_leads run (the 1st, 02:50), what it found, with the yield of US and non-US seeds
    (sources/lookalike_leads.py);
  * Mailboxes: each mailbox's sends, its bounces over its last 100 sends (v_mailbox_health) and
    its place on the sending ramp (registry/ramp.py);
  * Kill rules and items waiting: rules that fired and the holds still in force
    (learn/kill_rules.py), and the human-in-the-loop items waiting for approval, by kind, and the
    oldest (a kill-rule hold is not waiting for approval: it is listed as a hold).
Each section has a bold title (Slack mrkdwn) after a blank line, and no table. It is one plain-text
message, as before, well under Slack's 40,000-character limit on text (about 5,000 in the tests):
account lists stop at LIST_LIMIT, and the funnel sections are aggregates that name no company or person.
It posts to the alert channel (the dev channel in dry-run), or to the log with no Slack token
(ops/notify.py). Not yet in it: not-now dates coming due (the reply desk stores them).

Before it, the asks (Harry, 7 Oct 2026), each in its own message with the approvers mentioned and each
line once (ops/notify.post_once): the credit and spend alerts (learn/spend.py: Apollo's balance against
apollo_floor, the monthly Apollo and Clay budgets, Claude's spend against its cap, Apollo's website-visitor
credits), and on Mondays the mailboxes to add three weeks ahead (learn/capacity_ahead.py). The credit
budget lines end with the one-line spend summary (spend.summary_line).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import budget, limits
from us_outbound.context import UK, Context
from us_outbound.enrol import approvals, enrol, second
from us_outbound.learn import capacity_ahead, daily_report, holds, kill_rules, spend
from us_outbound.logs import clip, log
from us_outbound.ops import notify
from us_outbound.registry import ramp
from us_outbound.replies.items import is_reply
from us_outbound.sources import apollo_enrich, lookalike_leads, pages, site_visits

JOB = "daily_post"
WAITING = ("open", "escalated")
WARM_CLASSES = ("positive", "referral")
LISTED_CLASSES = ("objection", "negative", "other")
LIST_LIMIT = 10  # accounts listed per section
QUOTE = 120  # characters of reply text quoted
HAND_CHECK = "hand_check"  # hitl_items.kind of the weekly hand-check (enrol/hand_check.py)
# "reply" is poll_replies' kind; "reply_approval" the first name, kept for old rows. Kill-rule holds are listed
# as holds, never as waiting for approval.
KIND_LABELS = {"reply": "replies", "reply_approval": "reply approvals", HAND_CHECK: "hand-checks",
               "manual_merge": "manual merges", daily_report.KIND: "send approvals"}
MAILBOX_HEALTH_SQL = "SELECT address, sends_last_100, bounces_last_100 FROM {schema}.v_mailbox_health"


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


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UK)


def period(ctx: Context) -> tuple[datetime, datetime, str]:
    """[start, end) and its label: the last send day before today (UK), through yesterday."""
    today = ctx.today_uk()
    day = today - timedelta(days=1)
    for _ in range(6):
        if budget.is_send_day(day, ctx.settings):
            break
        day -= timedelta(days=1)
    label = f"Yesterday, {day:%a %d %b}" if day == today - timedelta(days=1) else f"Since {day:%a %d %b}"
    return _midnight(day), _midnight(today), label


def mailbox_last_100(ctx: Context) -> dict[str, tuple[int, int]]:
    """address -> (sends, bounced) over its last 100 sends: v_mailbox_health, or the same count in Python."""
    try:
        rows = ctx.store.query(MAILBOX_HEALTH_SQL.format(schema=ctx.store.schema))
        return {str(r["address"]).lower(): (int(r.get("sends_last_100") or 0), int(r.get("bounces_last_100") or 0))
                for r in rows if r.get("address")}
    except NotImplementedError:  # MemoryStore has no views
        ev = kill_rules._Events(ctx.store, ctx.now)
        out: dict[str, list] = {}
        for s in reversed(ev.sends):
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


def _counts(c: Counter, label: str = "") -> str:
    return ", ".join(f"{label}{k} {v}" for k, v in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0]))))


# -- what waits for Harry -----------------------------------------------------------------------------


@dataclass
class ForYou:
    """What waits for a person now (the post's "Needs you" line, and `us-outbound status`)."""

    send_approvals: list[dict]  # cards waiting for a ✅: open and not lapsed
    replies: list[dict]  # reply items to answer: open or escalated
    holds: list[dict]  # kill-rule holds in force
    hand_checks: list[dict]  # the weekly hand-check, while it waits

    @property
    def counts(self) -> dict[str, int]:
        return {"send_approvals": len(self.send_approvals), "replies": len(self.replies),
                "kill_rule_holds": len(self.holds), "hand_checks": len(self.hand_checks)}


def for_you(ctx: Context, open_items: Sequence[Mapping[str, Any]] | None = None) -> ForYou:
    """From the hitl_items open or escalated (read here unless given). A send approval past the end of its
    next send day has lapsed: poll_approvals closes it within minutes, and it needs nobody."""
    rows = list(open_items) if open_items is not None else ctx.store.select("hitl_items", {"status": list(WAITING)})
    today = ctx.today_uk()
    sends = [dict(i) for i in rows if i.get("kind") == daily_report.KIND and i.get("status") == "open"
             and not approvals.is_expired(i.get("payload") if isinstance(i.get("payload"), Mapping) else {}, today)]
    return ForYou(send_approvals=sends, replies=[dict(i) for i in rows if is_reply(i)],
                  holds=[dict(i) for i in rows if i.get("kind") == holds.KIND],
                  hand_checks=[dict(i) for i in rows if i.get("kind") == HAND_CHECK])


def _oldest(ctx: Context, items: Sequence[Mapping[str, Any]]) -> str:
    oldest = min((t for i in items if (t := _ts(i.get("created_at"))) is not None), default=None)
    if oldest is None:
        return ""
    waited = daily_report._age(ctx.now - oldest)
    return f"waited {waited}" if len(items) == 1 else f"the oldest has waited {waited}"


def needs_you(ctx: Context, w: ForYou) -> str:
    """"*Needs you:* 3 send approvals (the oldest has waited 20 hours; ...) · 1 reply (waited 2 hours) · ..."."""
    parts = []
    if w.send_approvals:
        age = _oldest(ctx, w.send_approvals)
        parts.append(f"{daily_report._n(len(w.send_approvals), 'send approval')} ({age + '; ' if age else ''}"
                     "a card lapses at the end of the next send day)")
    if w.replies:
        age = _oldest(ctx, w.replies)
        parts.append(daily_report._n(len(w.replies), "reply", "replies") + (f" ({age})" if age else ""))
    if w.holds:
        parts.append(f"{daily_report._n(len(w.holds), 'kill-rule hold')} (`us-outbound killrules show`)")
    if w.hand_checks:
        parts.append("this week's hand-check (`us-outbound handcheck show`)")
    return "*Needs you:* " + (" · ".join(parts) if parts else "nothing")


def build(ctx: Context, spent: spend.Spend | None = None) -> tuple[list[str], dict[str, Any]]:
    """(the message lines, the numbers for the summary). spent: what learn/spend.py read (read here when not given)."""
    start, end, label = period(ctx)
    types = ["sent", "replied", "bounced", "unsubscribed", "meeting_booked", "demo_held", "site_visit"]
    rows = [e for e in ctx.store.select("events", {"type": types})
            if (t := _ts(e.get("occurred_at"))) is not None and start <= t < end]
    by_type: dict[str, list[dict]] = {}
    for e in rows:
        by_type.setdefault(str(e.get("type")), []).append(e)
    sent, replies = by_type.get("sent", []), by_type.get("replied", [])
    names = _names(ctx, {str(e.get("account_id") or "") for e in rows})

    def who(e: Mapping[str, Any]) -> str:
        return names.get(str(e.get("account_id") or ""), "an account")

    steps = Counter(e.get("step") for e in sent)
    classes = Counter(str(e.get("reply_class") or "not classified") for e in replies)
    warm = [e for e in replies if e.get("reply_class") in WARM_CLASSES]
    nums = {
        "period": label, "sent": len(sent), "by_step": {str(k): v for k, v in steps.items()}, "replies": len(replies),
        "by_class": dict(classes), "positive": len(warm), "bounced": len(by_type.get("bounced", [])),
        "unsubscribed": len(by_type.get("unsubscribed", [])),
        # By company: a meeting and a Spill 3.0 deal for one booking are two events (crm/readback.py, 6 Oct 2026).
        "demos_booked": len({e.get("account_id") or e["event_id"] for e in by_type.get("meeting_booked", [])}),
        "demos_held": len(by_type.get("demo_held", [])),
    }
    lines = [f"*Daily post, {ctx.now.astimezone(UK):%a %d %b}*" + (" (dry-run)" if ctx.dry_run else "")]
    lines += ["", f"*Sent and outcomes* · {label} (UK)"]
    step_text = _counts(Counter({f"{k}": v for k, v in steps.items() if k is not None}), "step ")
    lines.append(f"  Sent: {len(sent)}" + (f" ({step_text})" if step_text else ""))
    lines.append(f"  Replies: {len(replies)}" + (f" ({_counts(classes)})" if replies else ""))
    lines.append(f"  Positive or referral: {len(warm)}"
                 + (" · " + "; ".join(f"{who(e)} ({e.get('reply_class')}, {e.get('mailbox') or '?'})" for e in warm[:LIST_LIMIT])
                    if warm else ""))
    lines.append(f"  Bounces: {nums['bounced']} · Unsubscribes: {nums['unsubscribed']} · "
                 f"Demos booked: {nums['demos_booked']} · Demos held: {nums['demos_held']}")
    listed = [e for e in replies if e.get("reply_class") in LISTED_CLASSES]
    if listed:
        lines.append("Objections, negatives and other replies:")
        lines += [f"  • {who(e)} ({e.get('reply_class')}): “{clip(' '.join(str(e.get('reply_text') or '').split()), QUOTE)}”"
                  for e in listed[:LIST_LIMIT]]
    ooo = [e for e in replies if e.get("reply_class") == "out_of_office"]
    if ooo:
        lines.append("Out of office: " + ", ".join(who(e) for e in ooo[:LIST_LIMIT]))
    visits = {str(e.get("account_id") or "") for e in by_type.get("site_visit", [])}
    enrolled = {str(a["account_id"]) for a in ctx.store.select("accounts", {"account_id": sorted(visits)})
                if a.get("status") in ("enrolled", "engaged")} if visits else set()
    if enrolled:
        lines.append("Warm accounts (enrolled, visited the US site): " + ", ".join(sorted(names.get(a, a) for a in enrolled)[:LIST_LIMIT]))

    # Today's number, its senders and the ready accounts exactly as enrol.run works them out: the send
    # approvals still waiting keep their accounts out of the candidates and hold their senders' slots.
    today_et = ctx.now_et().date()
    hand_check_waits, pulled = enrol.hand_check(ctx, today_et)
    held = approvals.waiting(ctx)
    ready, _ = enrol.candidates(ctx, pulled, held.accounts)
    seconds, _ = second.candidates(ctx, pulled, held.accounts)  # none while second_contact is no
    lim = limits.today(ctx, today_et, ready_accounts=len(ready) + len(seconds), pending=held.by_owner, campaigns=True,
                       second_ready=len(seconds))
    nums.update(number=lim.number, limited_by=lim.explanation, ready_accounts=len(ready))

    # The funnel (Harry, 2 Oct 2026): approvals, what was found, the pipeline, and what to tune.
    data = daily_report.Rows.load(ctx)
    section, more = daily_report.approvals(ctx, data, start, end)
    lines += ["", *section] if section else []
    nums.update(more)
    for section, more in (daily_report.found(ctx, data, start, end, label),
                          daily_report.pipeline(ctx, data, ready, set(held.accounts), lim),
                          daily_report.to_improve(ctx, data)):
        lines += ["", *section]
        nums.update(more)
    lines.insert(1, daily_report.headline(label.split(",")[0], nums))
    waiting_on = for_you(ctx, data.open_items)
    lines.insert(2, needs_you(ctx, waiting_on))
    nums.update(needs_you=waiting_on.counts)

    # Today's number, each sender, the budgets (limits.py), and why enrollment waits: enrol's own gates, and
    # the hand-check only while auto_send = yes (with no, every email is approved in Slack instead).
    lines += ["", "*Today's number*", lim.explanation]
    lines += [f"  {line}" for line in lim.detail]
    if not second.on(ctx.settings):  # when on, limits.py's own line says how many are ready
        lines.append(f"  {second.describe(ctx.settings)}")
    why = enrol.gate(ctx, today_et)
    if why is None and ctx.settings.general.auto_send:
        why = hand_check_waits
    if why:
        lines.append(f"  Enrollment waits: {why}.")
    if not ctx.settings.general.live_sending and "live_sending" not in (why or ""):
        lines.append("  Running dry: live_sending is no, so enrol reaches nobody (a few cards go to the dev channel "
                     "as a preview).")
    lines.append("Credit budgets this month:")
    lines += [f"  {line}" for line in lim.budget_lines]
    spent = spent if spent is not None else spend.read(ctx)
    lines.append(f"  {spend.summary_line(spent)}.")
    nums.update(spend=spent.as_dict())

    # The careers and benefits page reader's coverage, for Harry's call on enhancing it (2 Oct 2026).
    lines += ["", "*Sources*", *pages.post_lines(ctx)]
    total = pages.coverage(ctx.store, ctx.settings, ctx.today_uk())
    nums.update(pages_read=total.accounts, pages_benefits_text_share=round(total.share(total.with_text), 3))

    # Funding and headcount from Apollo's organization enrich (Harry, 2 Oct 2026).
    lines += apollo_enrich.post_lines(ctx)
    lines += lookalike_leads.post_lines(ctx)  # the monthly lookalike leads, the morning they ran (5 Oct 2026)
    enriched = apollo_enrich.tally(ctx.store, ctx.today_uk())
    nums.update(enriched=enriched.accounts, enriched_with_funding=enriched.with_funding)

    # Website visits from Apollo, or that its tracker sends no data (Harry, 5 Oct 2026): one line.
    lines.append(site_visits.post_line(ctx))

    # Mailbox health: sends in the period, bounces over the last 100, the ramp.
    lines += ["", "*Mailboxes*"]
    last100 = mailbox_last_100(ctx)
    ramps = ramp.ramps(ctx.store, ctx.settings, today_et)
    sent_by = Counter(str(e.get("mailbox") or "").lower() for e in sent)
    for m in ctx.settings.mailboxes:
        if m.status == "Retired":
            continue
        a = m.address.lower()
        n, b = last100.get(a, (0, 0))
        r = ramps.get(a)
        line = f"  • {a} ({m.status}): {sent_by.get(a, 0)} sent; "
        line += f"bounces {b} of its last {n} sends ({b / n:.1%})" if n else "no sends on record yet"
        if r is not None and r.ramping:
            line += f"; {r.describe()}"
        lines.append(line)

    # Kill rules (SPEC 11: "any kill rule that fired").
    lines += ["", "*Kill rules and items waiting*"]
    fired = [i for i in ctx.store.select("hitl_items", {"kind": holds.KIND})
             if (t := _ts(i.get("created_at"))) is not None and start <= t < ctx.now]
    in_force = waiting_on.holds  # holds.in_force, from the open items already read
    if fired:
        lines.append("Kill rules fired: " + "; ".join(str((i.get("payload") or {}).get("reason")) for i in fired[:LIST_LIMIT]))
    else:
        lines.append("Kill rules fired: none")
    if in_force:
        lines.append(f"  Holds in force: {len(in_force)} (`us-outbound killrules show`)")
    nums.update(kill_rules_fired=len(fired), holds_in_force=len(in_force))

    # What waits for an approval (a kill-rule hold is a hold, listed above, not an approval).
    waiting = [i for i in data.open_items if i.get("kind") != holds.KIND]
    by_kind = Counter(KIND_LABELS.get(str(i.get("kind")), str(i.get("kind"))) for i in waiting)
    if waiting:
        oldest = min((_ts(i.get("created_at")) for i in waiting if _ts(i.get("created_at"))), default=None)
        age = f"; the oldest has waited {int((ctx.now - oldest).total_seconds() // 3600)} hours" if oldest else ""
        lines.append(f"Waiting for approval: {len(waiting)} ({_counts(by_kind)}){age}.")
    else:
        lines.append("Waiting for approval: nothing.")
    nums.update(waiting=len(waiting), waiting_by_kind=dict(by_kind))
    return lines, nums


def run(ctx: Context) -> dict:
    """The daily_post job (JOB CONTRACT: run(ctx) -> summary).

    The summary (kept in the heartbeat row) has the numbers, not the text: the text quotes
    replies, and reply text is purged after 90 days (SPEC 6), which a heartbeat row is not.
    """
    spent, asked = spend.check(ctx)  # the credit and spend asks first, so a failing post below never holds them
    lines, nums = build(ctx, spent)
    mailboxes = capacity_ahead.check(ctx, int(nums.get("ready_accounts") or 0))  # Mondays only
    sent = notify.alert(ctx, "\n".join(lines))
    summary = {"job": JOB, "dry_run": ctx.dry_run, **nums, "alert": sent, "spend_alert": asked,
               "mailboxes_ahead": mailboxes}
    log("daily_post_done", run_id=ctx.run_id, **summary)
    return summary
