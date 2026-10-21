"""monday_readout, Mondays 08:30 UK (SPEC 9, 12 "Monday readout"; Harry, 6 Oct 2026: "push ahead with building").

One message to the alert channel (#us-outbound; the dev channel in dry-run, base/notify.py), after the weekly
hand-check's draw (05:45), about the week before (Monday to Sunday, UK time), written for a reader who may be new (Harry,
12 Oct 2026; base/say.py); there is no Monday daily post from 19 Oct 2026.

Rebuilt in W3-1 (20 Oct 2026; design-final W3-1 change 3, §4, §9) to print what can be read now and nothing it
cannot: no verdict words ("below", "on track", "raise", "Needs action"), every rate with its n and its 95% interval,
a split only when every cell has MIN_CELL (15) companies, and a line saying how many of the printed intervals would
miss the pooled rate by chance alone (RT §3). Outcome rates carry Jeffreys intervals (learn/stats.jeffreys: at 0-5%
the Wilson interval is too wide at the bottom and the Wald one wrong), the hand-check and bounce shares Wilson ones.
Top to bottom:
  * the headline (how many decisions wait for someone) and last week in one line: emails sent, companies that
    replied (human: every class but out-of-office) and positively, each company once (metrics-audit H3), meetings,
    bounces and unsubscribes;
  * the operating metric, until replies mature: companies contacted last week (of weekly_enrol_cap) × ICP precision
    (T1, the hand-check pooled over 4 weeks) × the delivered share of the companies contacted in those 4 weeks, with
    its caveat: the hand-check holds its sampled companies until judged, so the product overstates by at most about
    2 points (RT §1.4);
  * the north star (O2: engaged within 45 days, learn/outcomes.py's one definition, intent to treat): counts and the
    cumulative reply curve (O5) until MIN_MATURED companies have had 45 days, then the rate; bookings two ways, any
    (O1a) and not from an ad (O1b); the leading indicator (O3, positive within 28 days) and human replies (O4) from 30
    matured at 28 days; exposure (X1); this week's reply-capture check on every outcome block (replies/capture.py);
    the stop rule's count (a brake, not advice);
  * throughput and supply: the week's sourcing plan (sources/allocation.py), what bound each send day and the leads
    lost by cause, mailbox use (learn/throughput.py), and contactable supply in send days with the yields it rests on;
  * targeting: the hand-check week (enrol/hand_check.py), ICP precision T1 and the leave-out reasons T2
    (learn/precision.py), the industry labels and the label sample T3 (learn/label_metrics.py);
  * delivery and capture: step-1 bounces per contact (D1) and by email source beside the per-send pause, each
    mailbox's last 100 sends (D2), the reply-capture check (D3), complaints "not measured" (D4), and whether
    opt-outs, replies and safety-check refusals were all dealt with;
  * splits: the explore arm against ranked (learn/experiments.arms_table, W3-4: O2-O4 with Jeffreys intervals, the
    realised share by step-1 week, and the one read at 300 random companies matured) and the A/A sham arm (a label
    that changes nothing: a difference outside its interval is pipeline bias), industry group and size band on human
    replies within 28 days, and last week's activity by cut (v_readout_weekly), counts only; the segment value
    (learn/segments.readout_line: the HubSpot prior in force, each Focus group's matured n toward 300); in the first
    readout of a month, the reply classes people corrected (replies/desk.correction_table, from 10 corrections);
  * the tests: the registry (learn/experiments.registry_lines, W3-4: General live_split, each split's
    pre-registration, planned n, reached, matured and projected date), then each running test at its looks
    (learn/looks.py); the signals (learn/signal_review.py), as monitoring;
  * the forecast scorecard (learn/metrics.scorecard: last week's forecasts against what they came to, and any
    outside its interval 3 weeks running);
  * the cohorts and the week's changes (learn/cohorts.py);
  * the multiplicity line, then the decisions waiting (each with where), and the week's contactability Suggestions.
The readout only reads and posts: it writes nothing but its heartbeat and, through base/notify.post_once, the record of
an ask it made. The job (run) first makes Monday's asks: the credit and spend asks (learn/spend.check) and the
mailboxes to add three weeks ahead (learn/spend.mailboxes_check), each once (post_once). `us-outbound report readout`
prints it without posting.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from us_outbound import budget, fmt
from us_outbound.base import notify, say
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import UK, Context
from us_outbound.learn import cohorts, experiments, kill_rules, looks, outcomes, signal_review, spend
from us_outbound.learn.outcomes import Outcome
from us_outbound.learn.stats import jeffreys, min_n_ok, newcombe, wilson
from us_outbound.logs import log
from us_outbound.replies import kinds
from us_outbound.timeparse import iso_date, utc

JOB = "monday_readout"
MIN_MATURED = outcomes.MIN_MATURED  # 30: a rate is shown only from this many matured companies
MIN_CELL = 15  # a split prints only when every cell has this many (RT §1.5)
REPLY_DAYS, MEETING_DAYS = outcomes.REPLY_DAYS, outcomes.MEETING_DAYS
POOL_WEEKS = 4  # the operating metric's delivered share and T1 are pooled over these weeks
HELD_CAVEAT = 2  # points the operating metric can overstate by (the hand-check holds its sample: RT §1.4)
CHANCE = 0.05  # the share of 95% intervals that miss the pooled rate by chance alone
SIGNAL_LINES = 5  # signals named in the readout; `report signals` has every one
UNCLASSIFIED_AFTER = timedelta(hours=1)  # poll_replies classifies a reply within 15 minutes
WEEKLY_SQL = "SELECT * FROM {schema}.v_readout_weekly"
# Each cut of v_readout_weekly, and what it splits by in a few words for a reader who has not seen it.
CUTS = (("arm", "By explore arm", "random: picked at random from the queue; ranked: in score order"),
        ("angle", "By angle", "the pitch of the copy"),
        ("industry_group", "By industry group", ""), ("sender", "By sender", "whose mailbox sent it"),
        ("step", "By step", "which email of the sequence"))
CUT_LIMIT = 8  # values listed per cut, the busiest first
# The splits of the outcomes (learn/metrics.cut_value), each on human replies within 28 days (O4: L8's read).
# The explore arm has its own table above them (learn/experiments.arms_table: O2-O4, the realised share, the read).
SPLITS = (("group", "industry group"), ("size_band", "size band"))
REPLY_ITEM, OOO_ITEM, OPT_OUT = "reply:", "out_of_office:", "unsubscribed:"  # poll_replies' and optout's ids
FORECAST_WORDS = {"contacted": "companies contacted", "icp_precision": "ICP precision",
                  "positive_maturing": "positive replies from the companies reaching 28 days"}


def last_week(ctx: Context) -> tuple[datetime, datetime]:
    """[Monday 00:00, the next Monday 00:00), UK time, of the last whole week before now."""
    this = budget.week_start(ctx.now)
    return budget.week_start(this - timedelta(days=3)), this


def with_interval(k: int, n: int) -> str:
    """"2 of 35 (5.7%, 95% 1.2–17.1%)": an outcome rate with its counts and its Jeffreys 95% interval."""
    low, high = jeffreys(k, n)
    return f"{k:,} of {n:,} ({k / n:.1%}, 95% {low * 100:.1f}–{high * 100:.1f}%)"


def _span(k: int, n: int) -> str:
    """The Wilson 95% interval in percent: "0.5–5.9%"."""
    low, high = wilson(k, n)
    return f"{low * 100:.1f}–{high * 100:.1f}%"


# -- the events, for last week's headline and the checks ------------------------------------------------------------


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


# -- the operating metric and the north star ---------------------------------------------------------------------------


def operating_line(ctx: Context, found: Sequence[Outcome], start: datetime, end: datetime,
                   precision: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The operating metric (design-final §2.2): companies contacted last week × T1 × the delivered share of the
    companies contacted over the same POOL_WEEKS weeks as T1, with its caveat."""
    cap = ctx.settings.general.weekly_enrol_cap
    contacted = sum(start <= o.step1_at < end for o in found)
    pool = [o for o in found if start - timedelta(weeks=POOL_WEEKS - 1) <= o.step1_at < end]
    delivered = sum(o.delivered for o in pool)
    fine, judged = int(precision.get("fine") or 0), int(precision.get("judged") or 0)
    nums: dict[str, Any] = {"contacted": contacted, "cap": cap, "fine": fine, "judged": judged,
                            "delivered": delivered, "pool": len(pool), "value": None}
    head = (f"*Operating metric* (until replies mature): {fmt.plural(contacted, 'company', 'companies')} contacted "
            f"last week (of the {cap:,} a week allowed)")
    if not judged:
        return (f"{head}; ICP precision not measured yet (no hand-check row judged in {POOL_WEEKS} weeks), so no "
                "product."), nums
    if not pool:
        return f"{head} × {fine / judged:.0%} judged in the ICP ({fine} of {judged}); nothing delivered to share.", nums
    value = contacted * fine / judged * delivered / len(pool)
    nums["value"] = round(value, 1)
    return (f"{head} × {fine / judged:.0%} judged in the ICP ({fine} of {judged} hand-checked, {POOL_WEEKS} weeks) × "
            f"{delivered / len(pool):.0%} delivered ({delivered} of {len(pool)} contacted in {POOL_WEEKS} weeks) ≈ "
            f"{value:.1f} ICP companies reached. The hand-check holds its sampled companies until judged, so this "
            f"overstates by at most about {HELD_CAVEAT} points."), nums


def _curve(found: Sequence[Outcome], now: datetime) -> str:
    """O5: by day 7, 14, 21 and 28 since email 1, the companies with a human / a positive reply, of those that have
    reached that day."""
    parts = []
    for d in (7, 14, 21, 28):
        by = timedelta(days=d)
        reached = [o for o in found if now >= o.step1_at + by]
        if not reached:
            continue
        human = sum(o.first_reply_at is not None and o.first_reply_at < o.step1_at + by for o in reached)
        positive = sum(o.first_positive_at is not None and o.first_positive_at < o.step1_at + by for o in reached)
        parts.append(f"day {d}: {human} / {positive} of {len(reached):,}")
    return "  Reply curve (human / positive replies by day since email 1, of the companies that reached it): " + (
        " · ".join(parts) if parts else "no company has had 7 days yet") + "."


def north_star_lines(ctx: Context, found: Sequence[Outcome]) -> tuple[list[str], dict[str, Any]]:
    """The north star block (the module docstring)."""
    from us_outbound.learn import metrics
    from us_outbound.replies import capture

    now, n = ctx.now, len(found)
    head = ("*North star* (companies engaged: a positive or referral reply within 28 days of email 1, or a meeting "
            "within 45; each company once, a bounced one kept in)")
    if not n:
        return ["", head, "  No company emailed yet."], {"contacted": 0}
    m45 = [o for o in found if o.window_closed_45]
    m28 = [o for o in found if o.window_closed_28]
    engaged = sum(o.engaged for o in found)
    any_, outbound = sum(o.meeting_any for o in found), sum(metrics.outbound_booking(o) for o in found)
    out = ["", head]
    if len(m45) < MIN_MATURED:
        out.append(f"  {len(m45):,} of {n:,} companies have had {MEETING_DAYS} days, so no rate until "
                   f"{MIN_MATURED} have. So far, {engaged:,} engaged.")
        out.append(_curve(found, now))
    else:
        out.append(f"  Engaged within {MEETING_DAYS} days: {with_interval(sum(o.engaged for o in m45), len(m45))}.")
    out.append(f"  Meetings booked: {any_:,} at any time · {outbound:,} not from an ad (the booker's HubSpot source is "
               "not paid search or paid social).")
    if len(m28) >= MIN_MATURED:
        out.append(f"  Within {REPLY_DAYS} days (the leading indicator): positive "
                   f"{with_interval(sum(o.positive_in_window for o in m28), len(m28))} · any human reply "
                   f"{with_interval(sum(o.replied_in_window for o in m28), len(m28))}.")
    else:
        out.append(f"  {len(m28):,} of {n:,} have had {REPLY_DAYS} days (a rate from {MIN_MATURED}); so far "
                   f"{sum(o.replied_in_window for o in found):,} replied, "
                   f"{sum(o.positive_in_window for o in found):,} positively.")
    full = [o for o in found if now >= o.step1_at + timedelta(days=metrics.FULL_SEQUENCE_DAYS)]
    held = metrics.hand_checked(ctx)
    out.append(f"  Exposure: {sum(o.steps_sent >= 4 for o in full):,} of {len(full):,} past day "
               f"{metrics.FULL_SEQUENCE_DAYS} sent all four emails · "
               f"{sum(o.mailbox_paused for o in found):,} whose mailbox a safety pause stopped · "
               f"{sum(o.account_id in held for o in found):,} held for the hand-check first.")
    caveat = capture.readout_caveat(ctx)  # on every outcome block until the week's check passes (W1-2)
    if caveat:
        out.append(f"  {caveat}")
    g = ctx.settings.general
    first = found[: g.stop_rule_accounts]
    out.append(f"  Stop rule (a brake): {fmt.plural(sum(o.meeting_any for o in first), 'meeting')} from the first "
               f"{fmt.plural(len(first), 'company', 'companies')} emailed; enrolment pauses by itself if the first "
               f"{g.stop_rule_accounts:,} bring fewer than {g.stop_rule_meetings}.")
    nums = {"contacted": n, "matured_28": len(m28), "matured_45": len(m45), "engaged": engaged,
            "engaged_45": sum(o.engaged for o in m45), "meeting_any": any_, "meeting_outbound": outbound,
            "positive_28": sum(o.positive_in_window for o in m28), "human_28": sum(o.replied_in_window for o in m28),
            "capture_unverified": caveat is not None}
    return out, nums


# -- splits ------------------------------------------------------------------------------------------------------------


def split_line(label: str, cells: Mapping[str, tuple[int, int]]) -> str:
    """One split on human replies within 28 days: every cell with its interval when there are two or more and each
    has MIN_CELL, else "pooled only" with the cells' sizes (RT §1.5)."""
    ns = {c: n for c, (_, n) in cells.items()}
    if len(cells) >= 2 and min_n_ok(ns, MIN_CELL):
        return f"  By {label}: " + " · ".join(f"{c} {with_interval(k, n)}" for c, (k, n) in sorted(cells.items())) + "."
    sizes = ", ".join(f"{c} {n}" for c, n in sorted(ns.items())) or "none"
    return f"  By {label}: pooled only (matured companies: {sizes}; a split prints once every cell has {MIN_CELL})."


def sham_line(a: tuple[int, int], b: tuple[int, int]) -> str:
    """The A/A check (queue.sham_arm: a label from a hash that changes nothing): sham a against b on positive replies
    within 28 days, the difference with its Newcombe 95% interval. A difference outside it is pipeline bias: something
    counts one label's companies differently (RT §4.3)."""
    (ka, na), (kb, nb) = a, b
    if min(na, nb) < MIN_CELL:
        return (f"  A/A check (the sham arm, a label that changes nothing): too few matured to compare (a {na}, b "
                f"{nb}; each needs {MIN_CELL}).")
    lo, hi = newcombe(ka, na, kb, nb)
    diff = ka / na - kb / nb
    verdict = "within chance" if lo <= 0 <= hi else "outside chance: look for a pipeline bias before reading a split"
    return (f"  A/A check (the sham arm, a label that changes nothing): positive within {REPLY_DAYS} days a "
            f"{ka} of {na}, b {kb} of {nb}; difference {diff * 100:+.1f} points (95% {lo * 100:+.1f} to "
            f"{hi * 100:+.1f}): {verdict}.")


def split_lines(ctx: Context, found: Sequence[Outcome], counts: dict[str, Any] | None = None) -> list[str]:
    """The splits: the explore arm's table (W3-4: learn/experiments.arms_table: O2-O4 by arm, the realised share by
    step-1 week, the one read when it is due), the group and size-band splits on human replies, and the sham arm.
    counts gets the table's numbers; its intervals (written without "95% ") join the multiplicity line in build."""
    from us_outbound.learn import experiments, metrics

    m28 = [o for o in found if o.window_closed_28]
    out = ["", f"*Splits* (human replies within {REPLY_DAYS} days, on companies that have had them; monitoring: the "
               "explore arm is read once, at 300 matured in the random arm)"]
    table, table_nums = experiments.arms_table(ctx, found)
    if counts is not None:
        counts.update(table_nums)
    out += table
    for cut, label in SPLITS:
        cells = {v: (sum(o.replied_in_window for o in g), len(g)) for v, g in metrics.by_cut(m28, cut, {}).items()}
        out.append(split_line(label, cells))
    out.append(sham_line(*sham_counts(m28)))
    return out


def segment_lines(ctx: Context, found: Sequence[Outcome]) -> list[str]:
    """Loop L9 (W3-4: learn/segments.py): the HubSpot prior in force and how far each Focus group is from the 300
    matured a proposal waits for; monitoring, with its n."""
    from us_outbound.learn import segments

    return ["", "*Segment value* (Spill's HubSpot history as the prior; Columbus's own replies move it as they "
                "mature)", segments.readout_line(ctx, found)]


def reply_class_lines(ctx: Context) -> list[str]:
    """Loop L11 (W3-4): the monthly agreement and confidence-calibration table of the reply classes people corrected
    with `class:` (replies/desk.correction_table), in the first readout of each month, once 10 corrections exist."""
    from us_outbound.replies import desk

    if ctx.now.astimezone(UK).day > 7:
        return []
    table = desk.correction_table(ctx)
    return ["", "*Reply classes* (this month: how often the model's class stood once a person read the reply)",
            *table] if table else []


def sham_counts(m28: Sequence[Outcome]) -> tuple[tuple[int, int], tuple[int, int]]:
    """((positive, companies) in sham arm a, the same in b) over the companies that have had 28 days, each once."""
    from us_outbound.learn import metrics

    sham = metrics.by_cut(m28, "sham_arm", {})
    a, b = sham.get("a", []), sham.get("b", [])
    return (sum(o.positive_in_window for o in a), len(a)), (sum(o.positive_in_window for o in b), len(b))


# -- delivery and capture ---------------------------------------------------------------------------------------------


def _key(e: Mapping[str, Any]) -> str:
    """The contact an event is about (its account, else the event itself, when a row has no contact)."""
    if e.get("contact_id"):
        return str(e["contact_id"])
    return f"account:{e['account_id']}" if e.get("account_id") else f"event:{e.get('event_id')}"


def bounce_contacts(ev: Recorded) -> tuple[int, int]:
    """(contacts sent email 1, how many of them bounced at step 1), all time: D1 per contact (metrics-audit H4). A
    bounce with no step counts as step 1, as learn/metrics.bounce_block counts it; a later step's bounce does not."""
    sent = {_key(e) for e in ev.by_type["sent"] if e.get("step") in (1, "1")}
    bounced = {_key(e) for e in ev.by_type["bounced"] if e.get("step") in (1, "1", None)} & sent
    return len(sent), len(bounced)


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
        bounced = {s.contact_id for s in mine if s.bounced and s.step in (1, "1")} & first  # at step 1 (D1)
        n, b = len(first), len(bounced)
        per_contact = (f"{b:,} of {fmt.plural(n, 'contact')} sent email 1 bounced ({fmt.pct(b, n)}; 95% {_span(b, n)})"
                       if n else "no contact sent email 1")
        out.append(f"  Emails found by {src}, last {kill_rules.WINDOW_DAYS} days: {per_contact} · the safety pause "
                   f"counts sends: {bounced_sends:,} of {fmt.plural(sends, 'send')} ({fmt.pct(bounced_sends, sends)}), "
                   f"and pauses the source over {kill_rules.BOUNCE_RATE:.0%} from {kill_rules.MIN_SENDS} sends")
        nums[src] = {"contacts": n, "bounced_contacts": b, "sends": sends, "bounced_sends": bounced_sends}
    return out, nums


def checks(ctx: Context, ev: Recorded, start: datetime, end: datetime) -> tuple[list[str], list[str]]:
    """Whether opt-outs, replies and safety-check refusals were all dealt with: (lines, the decisions waiting, in a
    few words each). Facts and where to act, no verdict on a rate."""
    out: list[str] = []
    todo: list[str] = []
    general = say.sheet_link(ctx, "General")
    # An opt-out is recorded everywhere (suppression, Instantly's blocklist, HubSpot) once its unsubscribed event
    # exists (replies/optout.py); a contact suppressed for unsubscribe without one is still pending.
    done = {str(e.get("contact_id")) for e in ev.by_type["unsubscribed"] if e.get("contact_id")}
    pending = [c for c in ctx.store.select("contacts", {"suppressed_reason": "unsubscribe"})
               if str(c.get("contact_id")) not in done]
    n_unsub = len(ev.by_type["unsubscribed"])
    recorded = (f"{fmt.plural(n_unsub, 'opt-out')} so far, each in suppression, Instantly's blocklist and HubSpot"
                if n_unsub else "no opt-out so far")
    if ctx.settings.general.optout_tested:
        out.append(f"  Opt-outs: the unsubscribe link is tested; {recorded}.")
    else:
        out.append(f"  Opt-outs: the unsubscribe-link test has not passed (`optout_tested` is no); {recorded}.")
        out.append(f"    *What to do:* {say.command('seed send ADDRESS --owner NAME --live')} to an inbox of ours, click "
                   f"the email's unsubscribe link, then {say.command('seed check')}; at PASS, set `optout_tested` to "
                   f"yes on the {general}")
        todo.append("test the unsubscribe link")
    if pending:
        out.append(f"  {fmt.plural(len(pending), 'opt-out')} not yet in Instantly's blocklist and HubSpot (retried by "
                   f"the next live run of {say.job_name('sync_outcomes')} or {say.job_name('poll_replies')}).")

    replies = ev.by_type["replied"]
    items = {str(i.get("item_id")) for i in ctx.store.select("hitl_items", {"kind": ["reply", "out_of_office"]})}
    opted = {str(e.get("event_id")) for e in ev.by_type["unsubscribed"]}
    late = ctx.now - UNCLASSIFIED_AFTER
    unclassified = [e for e in replies if not e.get("reply_class") and utc(e["occurred_at"]) < late]
    unrouted = [e for e in replies if e.get("reply_class")
                and not {f"{REPLY_ITEM}{e['event_id']}", f"{OOO_ITEM}{e['event_id']}"} & items
                and f"{OPT_OUT}{e['event_id']}" not in opted]
    if unclassified or unrouted:
        why = ([f"{len(unclassified)} not sorted (positive, objection, …) after an hour"] if unclassified else []) + (
            [f"{len(unrouted)} with no reply card in Slack, out-of-office record or opt-out"] if unrouted else [])
        out.append(f"  Replies: {'; '.join(why)}.")
        out.append(f"    *What to do:* look at them with {say.command('replies list')}")
        todo.append(f"check {fmt.plural(len(unclassified) + len(unrouted), 'reply', 'replies')}")
    else:
        out.append(f"  Replies: every one of the {len(replies):,} so far sorted and routed.")

    week = [h for h in ctx.store.select("heartbeats", {"status": "error"})
            if (t := utc(h.get("started_at"))) is not None and start <= t < end]
    refusals = [h for h in week if isinstance(h.get("detail"), Mapping) and h["detail"].get("violation")]
    blocked = 0
    for h in ctx.store.select("heartbeats", {"job": "enrol"}):
        t, d = utc(h.get("started_at")), h.get("detail")
        if t is not None and start <= t < end and isinstance(d, Mapping) and isinstance(d.get("skipped"), Mapping):
            blocked += int(d["skipped"].get("copy blocked") or 0)
    held = f"; {fmt.plural(blocked, 'email')} held back by the copy rules, each with its reason" if blocked else ""
    if refusals:
        jobs = ", ".join(sorted({say.job_name(str(h.get("job"))) for h in refusals}))
        out.append(f"  Safety-check refusals: {len(refusals)} last week, in {jobs}{held}.")
        out.append(f"    *What to do:* read each error with {say.command('status')} or in {say.logs_link()}")
        todo.append(f"explain {fmt.plural(len(refusals), 'refusal')}")
    else:
        out.append(f"  Safety-check refusals: none last week{held}.")
    return out, todo


def delivery_lines(ctx: Context, ev: Recorded, start: datetime, end: datetime
                   ) -> tuple[list[str], dict[str, Any], list[str]]:
    """Delivery and capture (D1-D4) and the checks: (lines, numbers, decisions waiting)."""
    from us_outbound.learn import daily_post
    from us_outbound.replies import capture

    n, b = bounce_contacts(ev)
    out = ["", "*Delivery and capture*"]
    if n:
        out.append(f"  Step-1 bounces per contact: {b:,} of {fmt.plural(n, 'contact')} sent email 1 ({fmt.pct(b, n)}; "
                   f"95% {_span(b, n)}).")
    else:
        out.append("  Step-1 bounces per contact: no email sent yet.")
    sources, by_source = source_lines(ctx)
    out += sources
    boxes = daily_post.mailbox_last_100(ctx)
    if boxes:
        out.append("  Each mailbox's last 100 sends (the safety pause acts over "
                   f"{kill_rules.BOUNCE_RATE:.0%} of 100): " + " · ".join(
                       f"{a} {bb} of {s}" for a, (s, bb) in sorted(boxes.items())) + ".")
    caveat = capture.readout_caveat(ctx)
    out.append(f"  {caveat}" if caveat else
               "  Reply capture: this week's check found Instantly's counts and ours the same, and the canary back.")
    out.append("  Complaints and inbox placement: not measured (nothing reports them to us yet: D11).")
    lines, todo = checks(ctx, ev, start, end)
    out += lines
    nums = {"bounces": {"contacts": n, "bounced": b, **({"interval": [round(x, 4) for x in wilson(b, n)]} if n else {})},
            "source_bounces": by_source}
    return out, nums, todo


# -- the readout views (last week's activity by cut) --------------------------------------------------------------


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
    """Last week's activity and the companies emailed so far in one cut: counts only (a rate on open windows is no
    rate: metrics-audit H2; the rates by cut are the Splits)."""
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
        out.append(f"  {v}: {activity(week[v])} · {t['accounts_enrolled']:,} emailed so far, "
                   f"{t['accounts_replied']:,} replied")
    if len(values) > CUT_LIMIT:
        out.append(f"  and {len(values) - CUT_LIMIT} more")
    return out


# -- sections built from other modules, each in its own guard --------------------------------------------------------


def _guarded(ctx: Context, name: str, title: str, make: Callable[[], list[str]]) -> list[str]:
    """A section's lines, or its title and one line saying it could not be built: a failure never stops the readout."""
    try:
        return make()
    except GuardViolation:
        raise
    except Exception as exc:
        log("readout_section_failed", run_id=ctx.run_id, section=name, error=f"{type(exc).__name__}: {exc}"[:200])
        return ["", title, f"  could not be built ({type(exc).__name__}); the rest of the readout is complete."]


def signal_section(ctx: Context) -> list[str]:
    """The Signals section's lines (learn/signal_review.py, descriptive since W2-3): monitoring, never a decision."""
    try:
        signals = signal_review.review(ctx)
    except GuardViolation:
        raise
    except Exception as exc:
        log("readout_signals_failed", run_id=ctx.run_id, error=f"{type(exc).__name__}: {exc}"[:200])
        return [f"  could not be built ({type(exc).__name__}); the rest of the readout is complete."]
    text = signal_review.lines(signals, SIGNAL_LINES)
    # The first line is the review's own summary, which the readout says elsewhere; with nothing emailed it is the
    # only line ("No company has been emailed yet"), so it stays.
    return [f"  {x}" for x in (text[1:] if signals.companies else text)]


def precision_section(ctx: Context, start: datetime) -> tuple[list[str], dict[str, Any]]:
    """The ICP precision block (learn/precision.readout_lines) and its numbers: T1 pooled with its interval, the
    reasons, the gate versions, over the four weeks ending with the week starting at start (the week the hand-check
    section reports). Not this week: the readout runs Monday 08:30 UK, after hand_check_draw has drawn the new week's
    rows and before any is judged, so this week would count as judged none of them and push the oldest judged week out
    of the pool. Monitoring: a failure says so in its section and never stops the readout."""
    from us_outbound.enrol import enrol  # imported when used, as build imports hand_check
    from us_outbound.learn import precision

    try:
        t = precision.tally(ctx, through=enrol.iso_week(start.astimezone(UK).date()))
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


def sourcing_lines(ctx: Context) -> list[str]:
    """The week's allocation plan (sources/allocation.py, loop L2), last week's throughput (learn/throughput.py, loop
    L6) and contactable supply in send days with its yields (T6, loop L1), each in its own guard: a failure is one
    line, never the readout."""
    from us_outbound.learn import daily_post, throughput
    from us_outbound.sources import allocation, apollo_universe

    def supply() -> list[str]:
        s = apollo_universe.contactable_supply(ctx)
        pace = daily_post.steady_pace(ctx.settings)
        return [f"  Contactable supply: about {s.contactable:,.0f} companies ({s.ready:,} ready, {s.findable:,} still "
                f"needing a contact, {s.raw:,} not checked yet), {s.contactable / pace:.1f} send days at {pace} a day; "
                f"{s.yields_text()}."]

    out: list[str] = []
    for name, fn in (("the sourcing plan", lambda: [allocation.readout_line(ctx)]),
                     ("throughput", lambda: throughput.lines(throughput.report(ctx))),
                     ("contactable supply", supply)):
        try:
            out += fn()
        except GuardViolation:
            raise
        except Exception as exc:
            log("readout_section_failed", section=name, error=f"{type(exc).__name__}: {str(exc)[:160]}")
            out.append(f"  {name[:1].upper()}{name[1:]} could not be read ({type(exc).__name__}).")
    return out


def forecast_lines(ctx: Context, found: Sequence[Outcome]) -> tuple[list[str], list[dict]]:
    """The forecast scorecard (F1, L13; learn/metrics.scorecard) and any forecast outside its interval 3 weeks
    running."""
    from us_outbound.learn import metrics

    card = metrics.scorecard(ctx, found)
    head = ["", "*Forecasts* (last week's, made the Monday before, against what they came to)"]
    if not card:
        return [*head, "  No forecast was made for last week (metrics_daily makes them each Monday)."], card

    def num(name: str, v: float | None) -> str:
        if v is None:
            return "unknown"
        return f"{v:.0%}" if name == "icp_precision" else f"{v:,.1f}".removesuffix(".0")

    out = [*head]
    for c in card:
        name = c["name"]
        out.append(f"  {FORECAST_WORDS.get(name, name)}: forecast {num(name, c['point'])} ({num(name, c['low'])}–"
                   f"{num(name, c['high'])}), came to {num(name, c['actual'])}: "
                   f"{'inside' if c['inside'] else 'outside'} its interval.")
    out.append(f"  Inside {sum(c['inside'] for c in card)} of {len(card)}.")
    flagged = metrics.flags(card)
    if flagged:
        out.append(f"  Outside its interval {metrics.FLAG_WEEKS} weeks running: "
                   f"{', '.join(FORECAST_WORDS.get(f, f) for f in flagged)}. Check its definition or the forecast "
                   "(loop L13).")
    return out, card


def multiplicity_line(intervals: int) -> str:
    """RT §3: with many cuts some intervals miss the pooled rate by chance alone: about 1 in 20 at 95%."""
    expect = intervals * CHANCE
    about = f"about {round(expect)}" if expect >= 0.5 else "fewer than one"
    return (f"_{fmt.plural(intervals, 'interval')} at 95% in this readout: expect {about} a week to exclude the pooled "
            "rate by chance alone, so one interval apart from the rest is no finding._")


def count_intervals(lines: Iterable[str]) -> int:
    """The 95% intervals the readout printed: each is written "95% " (with_interval, _span, the sections' own)."""
    return sum(x.count("95% ") for x in lines)


# -- the job -------------------------------------------------------------------------------------------------------


def build(ctx: Context) -> tuple[list[str], dict[str, Any]]:
    """(the message lines, the numbers for the summary)."""
    start, end = last_week(ctx)
    ev = Recorded(ctx)
    week = week_numbers(ev, start, end)
    found = outcomes.per_company(ctx)
    rows = weekly_rows(ctx)

    from us_outbound.enrol import hand_check  # imported when used, as mailboxes_ahead imports enrol/today

    checked, by_hand = hand_check.readout_lines(ctx, start)
    t1, precision_nums = precision_section(ctx, start)
    operating, operating_nums = operating_line(ctx, found, start, end, precision_nums)
    star, star_nums = north_star_lines(ctx, found)
    body = [operating, *star,
            "", "*Throughput and supply* (where sourcing looks this week, what held last week's enrolment, and what "
                "is left to email)", *sourcing_lines(ctx),
            *checked, *t1, *label_section(ctx, start, end, precision_nums.get("wrong_industry"))]
    delivery, delivery_nums, todo = delivery_lines(ctx, ev, start, end)
    body += delivery
    arm_nums: dict[str, Any] = {}
    body += _guarded(ctx, "splits", "*Splits*", lambda: split_lines(ctx, found, arm_nums))
    body += _guarded(ctx, "segments", "*Segment value*", lambda: segment_lines(ctx, found))
    body += _guarded(ctx, "reply_classes", "*Reply classes*", lambda: reply_class_lines(ctx))
    if rows is not None:
        cut_block = [line for cut, title, about in CUTS for line in cut_lines(rows, cut, title, about, start)]
        if cut_block:
            body += ["", "*Last week by cut* (activity · companies emailed so far)", *cut_block]
    tests, at_look = looks.readout_lines(ctx, ctx.now - timedelta(days=7))
    body += ["", "*Tests* (the one experiment, read only at points fixed in advance on the Tests tab, called looks)",
             *_guarded(ctx, "registry", "*Experiment registry*", lambda: experiments.registry_lines(ctx)), *tests]
    todo += [f"decide on test {t}" for t in at_look]
    body += ["", "*Signals* (monitoring: replies with each buying signal at enrolment against without, with 95% "
                 "intervals)", *signal_section(ctx)]
    card: list[dict] = []

    def forecasts() -> list[str]:
        lines_, got = forecast_lines(ctx, found)
        card.extend(got)
        return lines_

    body += _guarded(ctx, "forecasts", "*Forecasts*", forecasts)
    section, by_cohort = cohorts.readout_lines(ctx, start, end)
    body += section

    days = cohorts.week_days(start.astimezone(UK).date())
    if todo:
        head = say.compose(say.ACTION, f"Monday readout, {days}: {fmt.plural(len(todo), 'decision')} waiting")
    else:
        head = say.compose(say.INFO, f"Monday readout, {days}: no decision waiting")
    lines = [head, week_line(week), *body]
    intervals = count_intervals(lines) + int(arm_nums.get("intervals") or 0)
    lines += ["", multiplicity_line(intervals),
              "*Decisions waiting:* " + (" · ".join(todo) if todo else "none")]
    nums: dict[str, Any] = {
        "week_start": start.astimezone(UK).date().isoformat(), "last_week": week,
        "so_far": dict(so_far(rows)["all"]) if rows is not None else None, "views": rows is not None,
        "operating": operating_nums, "north_star": star_nums, **delivery_nums,
        "decisions_waiting": todo, "hand_check": by_hand, "icp_precision": precision_nums,
        "forecasts": card, "intervals": intervals, "explore_arm": arm_nums,
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


def contact_suggestions(ctx: Context) -> dict[str, Any]:
    """The week's contactability proposals (loop L5, W3-3: pick.suggestions) onto the Suggestions tab, each once
    (settings/suggestions.write, live only), and one line for the readout. Never raises (but a GuardViolation): the
    readout posts whatever happens here."""
    from us_outbound.contacts import pick
    from us_outbound.settings import suggestions as tab

    try:
        rows = pick.suggestions(ctx)
        wrote = tab.write(ctx, rows)
    except GuardViolation:
        raise
    except Exception as exc:
        log("readout_suggestions_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    kinds_ = Counter(r["kind"] for r in rows)
    if not rows:
        return {**wrote, "kinds": {}}
    added = wrote.get("added", 0 if wrote.get("written") else len(rows))
    what = ", ".join(f"{n} {k}" for k, n in sorted(kinds_.items()))
    line = (f"*Suggestions* (contactability, for you to approve on the {say.sheet_link(ctx, tab.TAB)}): {what}; "
            f"{added} new this week" + ("" if wrote.get("written") else " (not written: a dry run)") + ".")
    return {**wrote, "kinds": dict(kinds_), "line": line}


def run(ctx: Context) -> dict:
    """The monday_readout job (JOB CONTRACT: run(ctx) -> summary): the numbers, not the text.

    Monday's asks go first, as the daily post makes them on the other days (there is no Monday post since 19 Oct
    2026), so a readout that fails below never holds them: the credit and spend asks (spend.check, each line once a
    day or a month as before) and the mailboxes to add three weeks ahead (once an ISO week)."""
    _, asked = spend.check(ctx)
    mailboxes = mailboxes_ahead(ctx)
    lines, nums = build(ctx)
    proposed = contact_suggestions(ctx)
    if proposed.get("line"):
        lines += ["", proposed["line"]]
    sent = notify.alert(ctx, "\n".join(lines))
    summary = {"job": JOB, "dry_run": ctx.dry_run, **nums, "alert": sent, "spend_alert": asked,
               "mailboxes_ahead": mailboxes, "suggestions": proposed}
    log("monday_readout_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k not in ("so_far", "forecasts")})
    return summary
