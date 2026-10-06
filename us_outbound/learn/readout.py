"""monday_readout, Mondays 08:30 UK (SPEC 9, 12 "Monday readout"; Harry, 6 Oct 2026: "push ahead with building").

One message to the alert channel (#us-outbound; the dev channel in dry-run, ops/notify.py), after the weekly
hand-check (08:00) and before the daily post (09:00), about the week before (Monday to Sunday, UK time). In plain
words, top to bottom:
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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import budget
from us_outbound.context import UK, Context
from us_outbound.learn import looks, signal_value
from us_outbound.learn.kill_rules import COMPLAINT
from us_outbound.logs import log
from us_outbound.ops import notify

JOB = "monday_readout"
MIN_COMPANIES = signal_value.MIN_TO_READ  # 30: a rate on fewer companies is "too few to read"
MIN_SENDS = 100  # a bounce rate on fewer sends moves a lot with one bounce (kill_rules reads a domain from 100)
BOUNCE_LIMIT = 0.02  # exit criteria to scale (docs/roadmap.md §3): bounce rate under 2%
# SPEC 12's working assumption for the funnel.
REPLY_ASSUMED, POSITIVE_ASSUMED, MEETING_ASSUMED = "3 to 5%", "about 1%", "0.5 to 1%"
UNCLASSIFIED_AFTER = timedelta(hours=1)  # poll_replies classifies a reply within 15 minutes
WEEKLY_SQL = "SELECT * FROM {schema}.v_readout_weekly"
CUTS = (("tier", "By tier"), ("angle", "By angle"), ("industry_group", "By industry group"), ("sender", "By sender"),
        ("step", "By step"))
CUT_LIMIT = 8  # values listed per cut, the busiest first
REPLY_ITEM, OOO_ITEM, OPT_OUT = "reply:", "out_of_office:", "unsubscribed:"  # poll_replies' and optout's ids
POSITIVE = frozenset({"positive", "referral"})


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


def _day(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _pct(k: int, n: int) -> str:
    return f"{k / n:.1%}" if n else "-"


def rate(k: int, n: int, *, minimum: int = MIN_COMPANIES) -> str:
    """"4.2%", or "too few to read" when the denominator is under the minimum (Harry, 6 Oct 2026)."""
    return _pct(k, n) if n >= minimum else "too few to read"


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
            if _ts(e.get("occurred_at")) is not None:
                self.by_type[str(e.get("type"))].append(e)

    def between(self, type_: str, start: datetime, end: datetime) -> list[dict]:
        return [e for e in self.by_type[type_] if start <= _ts(e["occurred_at"]) < end]


def week_numbers(ev: _Events, start: datetime, end: datetime) -> dict[str, int]:
    replies = [e for e in ev.between("replied", start, end) if e.get("reply_class") != "out_of_office"]
    return {
        "sent": len(ev.between("sent", start, end)),
        "replies": len(replies),
        "positive": sum(e.get("reply_class") in POSITIVE for e in replies),
        "meetings": len({e.get("account_id") or e["event_id"] for e in ev.between("meeting_booked", start, end)}),
        "bounces": len(ev.between("bounced", start, end)),
        "unsubscribes": len(ev.between("unsubscribed", start, end)),
    }


def week_line(n: Mapping[str, int], label: str) -> str:
    return (f"*Last week* ({label}, UK): {_n(n['sent'], 'email')} sent · {_n(n['replies'], 'reply', 'replies')}, "
            f"{n['positive']:,} positive · {_n(n['meetings'], 'meeting')} booked · {_n(n['bounces'], 'bounce')} · "
            f"{_n(n['unsubscribes'], 'unsubscribe')}")


# -- the exit criteria to scale (docs/roadmap.md §3) ---------------------------------------------------------------


def criterion(ok: bool, name: str, detail: str) -> str:
    """"  Met: bounces under 2%: 1 of 150 sends so far (0.7%)."."""
    return f"  {'Met' if ok else 'Not met'}: {name}: {detail}."


def exit_criteria(ctx: Context, ev: _Events, start: datetime, end: datetime) -> tuple[list[str], dict[str, bool]]:
    out: list[str] = []
    met: dict[str, bool] = {}

    sends, bounces = len(ev.by_type["sent"]), len(ev.by_type["bounced"])
    met["bounces"] = bool(sends) and bounces / sends < BOUNCE_LIMIT
    if not sends:
        out.append(criterion(False, "bounces under 2%", "no email sent yet"))
    else:
        caution = f"; on under {MIN_SENDS} sends one bounce moves it a lot" if sends < MIN_SENDS else ""
        out.append(criterion(met["bounces"], "bounces under 2%",
                             f"{bounces:,} of {_n(sends, 'send')} so far ({_pct(bounces, sends)}{caution})"))

    complaints = len(ev.by_type[COMPLAINT])
    met["complaints"] = complaints == 0
    out.append(criterion(met["complaints"], "no complaints",
                         "none recorded" if not complaints else f"{_n(complaints, 'spam complaint')} recorded"))

    # An opt-out is recorded everywhere (suppression, Instantly's blocklist, HubSpot) once its unsubscribed event
    # exists (replies/optout.py); a contact suppressed for unsubscribe without one is still pending.
    done = {str(e.get("contact_id")) for e in ev.by_type["unsubscribed"] if e.get("contact_id")}
    pending = [c for c in ctx.store.select("contacts", {"suppressed_reason": "unsubscribe"})
               if str(c.get("contact_id")) not in done]
    tested = ctx.settings.general.optout_tested
    met["unsubscribes"] = tested and not pending
    why = []
    if not tested:
        why.append("optout_tested is no on the General tab, until the seed-inbox test passes (`us-outbound seed send`, "
                   "then `seed check`)")
    if pending:
        why.append(f"{_n(len(pending), 'opt-out')} not yet recorded in Instantly's blocklist and HubSpot; the next "
                   "live sync_outcomes or poll_replies run tries again")
    n_unsub = len(ev.by_type["unsubscribed"])
    recorded = (f"{_n(n_unsub, 'opt-out')} so far, each recorded in suppression, Instantly's blocklist and HubSpot"
                if n_unsub else "no opt-out so far")
    out.append(criterion(met["unsubscribes"], "unsubscribes confirmed end to end",
                         "; ".join(why) if why else f"the seed test passed; {recorded}"))

    replies = ev.by_type["replied"]
    items = {str(i.get("item_id")) for i in ctx.store.select("hitl_items", {"kind": ["reply", "out_of_office"]})}
    opted = {str(e.get("event_id")) for e in ev.by_type["unsubscribed"]}
    late = ctx.now - UNCLASSIFIED_AFTER
    unclassified = [e for e in replies if not e.get("reply_class") and _ts(e["occurred_at"]) < late]
    unrouted = [e for e in replies if e.get("reply_class")
                and not {f"{REPLY_ITEM}{e['event_id']}", f"{OOO_ITEM}{e['event_id']}"} & items
                and f"{OPT_OUT}{e['event_id']}" not in opted]
    met["replies"] = not unclassified and not unrouted
    if met["replies"]:
        out.append(criterion(True, "every reply classified and routed",
                             f"{_n(len(replies), 'reply', 'replies')} so far"))
    else:
        why = ([f"{len(unclassified)} not classified after an hour"] if unclassified else []) + (
            [f"{len(unrouted)} with no reply card, out-of-office record or opt-out"] if unrouted else [])
        out.append(criterion(False, "every reply classified and routed",
                             f"{'; '.join(why)} (`us-outbound replies list`)"))

    week = [h for h in ctx.store.select("heartbeats", {"status": "error"})
            if (t := _ts(h.get("started_at"))) is not None and start <= t < end]
    refusals = [h for h in week if isinstance(h.get("detail"), Mapping) and h["detail"].get("violation")]
    blocked = 0
    for h in ctx.store.select("heartbeats", {"job": "enrol"}):
        t, d = _ts(h.get("started_at")), h.get("detail")
        if t is not None and start <= t < end and isinstance(d, Mapping) and isinstance(d.get("skipped"), Mapping):
            blocked += int(d["skipped"].get("copy blocked") or 0)
    held = (f"; {_n(blocked, 'email')} held back by the copy rules, each with its reason in enrol's run summary"
            if blocked else "")
    met["refusals"] = not refusals
    if refusals:
        jobs = ", ".join(sorted({str(h.get("job")) for h in refusals}))
        out.append(criterion(False, "no unexplained refusal", f"{_n(len(refusals), 'guard refusal')} last week, in "
                             f"{jobs}, each with its error in `us-outbound status`{held}"))
    else:
        out.append(criterion(True, "no unexplained refusal", f"no guard refusal last week{held}"))
    return out, met


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
        if r.get("cut") == cut and _day(r.get("week_start")) == start.date():
            out[str(r.get("cut_value"))].update({k: int(r.get(k) or 0) for k in ACTIVITY})
    return out


def so_far_line(total: Mapping[str, int]) -> str:
    emailed, delivered = total["accounts_enrolled"], total["accounts_delivered"]
    closed = total["accounts_window_closed"]
    if not emailed:
        return "So far: no company emailed yet."
    still = "" if closed >= delivered else ", so the rates still rise as replies come in"
    return (f"So far: {_n(emailed, 'company', 'companies')} emailed, {delivered:,} delivered; {closed:,} of their "
            f"28-day reply windows have closed{still}.")


def targets(ctx: Context, total: Mapping[str, int] | None, enrolled_last_week: int) -> list[str]:
    g = ctx.settings.general
    out = [f"  Enrolled last week: {_n(enrolled_last_week, 'company', 'companies')}, against weekly_enrol_cap "
           f"{g.weekly_enrol_cap:,}."]
    if total is None:
        return out + ["  The rates so far need the database's views (v_readout_weekly)."]
    d = total["accounts_delivered"]
    replied, positive, meetings = total["accounts_replied"], total["accounts_positive"], total["accounts_meeting"]
    out.append(f"  Replied so far: {replied:,} of {_n(d, 'company', 'companies')} emailed ({rate(replied, d)}; "
               f"the working assumption is {REPLY_ASSUMED}).")
    out.append(f"  Positive: {positive:,} ({rate(positive, d)}; {POSITIVE_ASSUMED} assumed). Meetings: {meetings:,} "
               f"({rate(meetings, d)}; {MEETING_ASSUMED} assumed).")
    out.append(f"  Stop rule: {_n(meetings, 'meeting')} from the first {_n(d, 'company', 'companies')} emailed; "
               f"enrolment pauses if the first {g.stop_rule_accounts:,} bring fewer than {g.stop_rule_meetings}.")
    return out


def cut_lines(rows: Sequence[Mapping[str, Any]], cut: str, title: str, start: datetime) -> list[str]:
    week, total = in_week(rows, start, cut), so_far(rows, cut)
    values = [v for v in set(week) | set(total) if any(week[v].values()) or total[v]["accounts_enrolled"]]
    if not values:
        return []
    values.sort(key=lambda v: (-week[v]["sends"], -total[v]["accounts_enrolled"], v))
    if cut == "step":
        out = ["", f"*{title}* (last week)"]
        for v in sorted(values):
            w = week[v]
            out.append(f"  {v}: {w['sends']:,} sent, {_n(w['replies'], 'reply', 'replies')} "
                       f"({w['positive_replies']:,} positive), {_n(w['bounces'], 'bounce')}")
        return out
    out = ["", f"*{title}* (last week · so far)"]
    for v in values[:CUT_LIMIT]:
        w, t = week[v], total[v]
        d, k = t["accounts_delivered"], t["accounts_replied"]
        out.append(f"  {v}: {w['sends']:,} sent, {_n(w['replies'], 'reply', 'replies')} ({w['positive_replies']:,} "
                   f"positive), {_n(w['meetings_booked'], 'meeting')}, {_n(w['bounces'], 'bounce')} · "
                   f"{_n(t['accounts_enrolled'], 'company', 'companies')} emailed, {k:,} replied ({rate(k, d)})")
    if len(values) > CUT_LIMIT:
        out.append(f"  and {len(values) - CUT_LIMIT} more")
    return out


# -- the job -------------------------------------------------------------------------------------------------------


def build(ctx: Context) -> tuple[list[str], dict[str, Any]]:
    """(the message lines, the numbers for the summary)."""
    start, end = last_week(ctx)
    label = f"Mon {start.astimezone(UK):%d %b} to Sun {(end - timedelta(days=1)).astimezone(UK):%d %b}"
    ev = _Events(ctx)
    week = week_numbers(ev, start, end)
    rows = weekly_rows(ctx)
    total = so_far(rows)["all"] if rows is not None else None
    enrolled = [c for c in ctx.store.select("contacts") if c.get("enrolled_at")]
    enrolled_last_week = budget.enrolled_this_week(enrolled, start + timedelta(days=3))

    lines = [f"*Monday readout, {ctx.now.astimezone(UK):%a %d %b}*" + (" (dry-run)" if ctx.dry_run else ""),
             week_line(week, label)]
    lines.append(so_far_line(total) if total is not None
                 else "So far, by cut and the signal table: these need the database's views, which this store does "
                      "not have.")
    lines += ["", "*Against the targets*", *targets(ctx, total, enrolled_last_week)]
    criteria, met = exit_criteria(ctx, ev, start, end)
    lines += ["", "*Exit criteria to scale*", *criteria]
    if rows is not None:
        for cut, title in CUTS:
            lines += cut_lines(rows, cut, title, start)
    table = signal_value.rows(ctx)
    lines += ["", "*Signal value* (companies with the signal at enrolment, against those without)",
              *signal_value.headline(table)]
    tests, at_look = looks.readout_lines(ctx, ctx.now - timedelta(days=7))
    lines += ["", "*Tests*", *tests]
    nums: dict[str, Any] = {
        "week_start": start.astimezone(UK).date().isoformat(), "last_week": week,
        "enrolled_last_week": enrolled_last_week, "so_far": dict(total) if total is not None else None,
        "exit_criteria": met, "views": rows is not None,
        "signals_readable": sum(signal_value.verdict(r) != signal_value.TOO_FEW for r in table or []
                                if int(r.get("accounts_sent") or 0)),
        "tests_at_a_look": at_look,
    }
    return lines, nums


def run(ctx: Context) -> dict:
    """The monday_readout job (JOB CONTRACT: run(ctx) -> summary): the numbers, not the text."""
    lines, nums = build(ctx)
    sent = notify.alert(ctx, "\n".join(lines))
    summary = {"job": JOB, "dry_run": ctx.dry_run, **nums, "alert": sent}
    log("monday_readout_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "so_far"})
    return summary
