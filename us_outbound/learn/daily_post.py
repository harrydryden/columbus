"""daily_post, 09:00 UK (SPEC 9, 11 "Daily post"): one message on yesterday's outbound and what waits for Harry.

It covers the last send day (Mondays cover Friday to Sunday, so weekend replies are not lost):
  * sends by step, replies by class, positive and referral replies, bounces, unsubscribes,
    demos booked and held (the events table; sync_outcomes, poll_replies and hubspot_readback
    write it);
  * objections, negatives and "other" replies, out-of-office replies, and warm accounts:
    enrolled accounts that visited the US site (SPEC 11);
  * today's number and what limits it, each sender's capacity, and the credit budgets
    (limits.py, the same lines `us-outbound status` prints);
  * mailbox health: each mailbox's sends, its bounces over its last 100 sends
    (v_mailbox_health) and its place on the sending ramp (registry/ramp.py);
  * kill rules that fired, and the holds still in force (learn/kill_rules.py);
  * the human-in-the-loop items waiting for approval, by kind, and the oldest.
It posts to the alert channel (the dev channel in dry-run), or to the log with no Slack token
(ops/notify.py). Not yet in it: not-now dates coming due (the reply desk stores them).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import budget, limits
from us_outbound.context import UK, Context
from us_outbound.enrol import enrol
from us_outbound.learn import holds, kill_rules
from us_outbound.logs import clip, log
from us_outbound.ops import notify
from us_outbound.registry import ramp

JOB = "daily_post"
WAITING = ("open", "escalated")
WARM_CLASSES = ("positive", "referral")
LISTED_CLASSES = ("objection", "negative", "other")
LIST_LIMIT = 10  # accounts listed per section
QUOTE = 120  # characters of reply text quoted
KIND_LABELS = {"reply_approval": "reply approvals", "hand_check": "hand-checks", "manual_merge": "manual merges",
               "kill_rule": "kill-rule holds"}
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


def build(ctx: Context) -> tuple[list[str], dict[str, Any]]:
    """(the message lines, the numbers for the summary)."""
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
        "unsubscribed": len(by_type.get("unsubscribed", [])), "demos_booked": len(by_type.get("meeting_booked", [])),
        "demos_held": len(by_type.get("demo_held", [])),
    }
    lines = [f"Daily post, {ctx.now.astimezone(UK):%a %d %b}" + (" (dry-run)" if ctx.dry_run else "")]
    lines.append(f"{label} (UK):")
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

    # Today's number, each sender, the budgets (limits.py).
    today_et = ctx.now_et().date()
    why, pulled = enrol.hand_check(ctx, today_et)
    ready, _ = enrol.candidates(ctx, pulled)
    lim = limits.today(ctx, today_et, ready_accounts=len(ready))
    lines.append(lim.explanation)
    lines += [f"  {line}" for line in lim.detail]
    if why:
        lines.append(f"  Enrollment waits: {why}.")
    lines.append("Credit budgets this month:")
    lines += [f"  {line}" for line in lim.budget_lines]
    nums.update(number=lim.number, limited_by=lim.explanation, ready_accounts=len(ready))

    # Mailbox health: sends in the period, bounces over the last 100, the ramp.
    lines.append("Mailboxes:")
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
    fired = [i for i in ctx.store.select("hitl_items", {"kind": holds.KIND})
             if (t := _ts(i.get("created_at"))) is not None and start <= t < ctx.now]
    in_force = holds.in_force(ctx.store)
    if fired:
        lines.append("Kill rules fired: " + "; ".join(str((i.get("payload") or {}).get("reason")) for i in fired[:LIST_LIMIT]))
    else:
        lines.append("Kill rules fired: none")
    if in_force:
        lines.append(f"  Holds in force: {len(in_force)} (`us-outbound killrules show`)")
    nums.update(kill_rules_fired=len(fired), holds_in_force=len(in_force))

    # What waits for Harry.
    waiting = ctx.store.select("hitl_items", {"status": list(WAITING)})
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
    lines, nums = build(ctx)
    sent = notify.alert(ctx, "\n".join(lines))
    summary = {"job": JOB, "dry_run": ctx.dry_run, **nums, "alert": sent}
    log("daily_post_done", run_id=ctx.run_id, **summary)
    return summary
