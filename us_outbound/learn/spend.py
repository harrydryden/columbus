"""Credit and spend alerts (Harry, 7 Oct 2026): ask for a purchase or a raise before a balance or a budget runs out.

The daily post (learn/daily_post.py, 09:00 UK Tuesday to Saturday) runs this first, and on Mondays the Monday readout
(learn/readout.py, 08:30), since there is no Monday post from 19 Oct 2026; so the asks reach the alert channel just
before the post, in one message with the approvers mentioned (base/notify.post_once keeps what was sent in events, so a
rerun or a restart never repeats a line):
  * Apollo's own balance (credit_usage: lead credits) against General apollo_floor. Below the floor every source
    and pick_contacts' email reveals stop spending (sources/apollo_credits.floor_reason), and a source's skip for
    it counts as healthy for heartbeat_check, so this is where it is told. The pace is the average daily Apollo
    spend in credit_ledger over the last PACE_DAYS days; the ask comes when the floor is under FLOOR_WARN_DAYS away
    at that pace (a new mailbox or a purchase takes time), or the balance is already under it: once a UK day. When
    Apollo's balance cannot be read, a source's skip for the floor in the last LOOKBACK says it instead.
    Apollo's balance also falls with use outside these jobs (the Apollo app), which the ledger does not see: on 13–19
    Oct the balance fell 242 credits while the ledger recorded about 132 (code-learn-reporting B10), so the floor
    warning came about 9 days late. From W2-3 each day's balance is kept (an events row, type apollo_balance, its
    amount the balance; one a UK day, written by check, never by a read-only report), and once BALANCE_READINGS exist
    the pace is the balance's fall over the last PACE_DAYS_BALANCE days ÷ the days between the readings; the ledger's
    pace stands in before then, or across a purchase (a balance that rose). When the two disagree by more than
    DRIFT_LIMIT over those days, the spend line says so and it is asked about once a week (S3, L12).
  * The monthly budget (budget.monthly: apollo_monthly_credits) at 80% and 100% used, once
    a UK month each, saying what stops at 100% and the key that raises it.
  * Claude's spend this month (credit_ledger, the UTC month, as clients/claude.py counts it) at 50%, 80% and 100%
    of claude_monthly_cap_usd, once a UTC month each. 100% is where the client refuses every call (the cap less
    its reserve, claude.cap_reached_at): replies are then classified "other" with no draft and copy QA stops.
  * The industry label check (labels.asks; Harry, 7 Oct 2026): many industry corrections at the cards, the rules and
    the model agreeing on too few companies, or the check unable to ask the model while new companies wait.
  * Apollo's website-visitor credits while site visits are on (SITE_VISIT_DOMAIN set, site_visits scheduled):
    under VISITOR_WARN_SHARE left, once a month. Not by pace: Apollo spends them itself each time its tracker names
    a visiting company, so credit_ledger has no record of their pace, and its answer gives no cycle dates.
Only the highest threshold reached is asked about, so a month that jumps from 70% to 100% gets one line.
summary_line is the daily post's one-line spend summary under the credit budgets. Nothing here raises (but a
GuardViolation): an Apollo that cannot be read leaves its balance unknown, and the daily post goes on.

The mailboxes to add three weeks ahead (learn/capacity_ahead.py until W2-3, merged here: the Monday readout's ask,
mailboxes_check below) are the other spend Monday asks for: see the section's own notes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import budget, fmt
from us_outbound.base import notify, say
from us_outbound.clients.apollo import VISITOR_CREDIT, credit_stats, credits_left
from us_outbound.clients.claude import cap_reached_at, month_spend_usd
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import Context
from us_outbound.enrol import capacity
from us_outbound.logs import log
from us_outbound.registry import ramp
from us_outbound.registry.mailboxes import MAX_CAP
from us_outbound.settings.model import SITE_VISIT_DOMAIN, SITE_VISIT_US_PATHS, Settings
from us_outbound.settings.validate import CLAUDE_CAP_USD
from us_outbound.sources import apollo_universe
from us_outbound.timeparse import utc

PACE_DAYS = 14  # the Apollo pace from the ledger: its average daily spend in credit_ledger over this many days
BALANCE = "apollo_balance"  # events.type of a day's Apollo balance (events.amount)
BALANCE_READINGS = 7  # the balance's pace once this many readings are kept
PACE_DAYS_BALANCE = 7  # ... over this many days
BALANCE_LOOKBACK = 10  # the earlier reading is at most this many days old
DRIFT_LIMIT = 0.10  # the ledger missing more than this share of the balance's fall over PACE_DAYS_BALANCE days
FLOOR_WARN_DAYS = 21  # ask when apollo_floor is fewer days away than this
BUDGET_STEPS = (0.8, 1.0)  # apollo_monthly_credits
CLAUDE_STEPS = (0.5, 0.8, 1.0)  # claude_monthly_cap_usd
VISITOR_WARN_SHARE = 0.15  # Apollo's website-visitor credits: ask when fewer than this share are left
LOOKBACK = timedelta(hours=26)  # a source's skip for the floor this recent stands in for an unread balance
# The jobs that check Apollo's balance against apollo_floor before spending (sources/apollo_credits.floor_reason).
FLOOR_JOBS = ("source_universe", "apollo_signals", "apollo_enrich", "site_visits", "lookalikes", "lookalike_leads",
              "pick_contacts")
# What we stop doing when Apollo has no credits left, in plain words ("we have stopped ...").
STOPS = {"apollo": "finding new companies and looking up contacts' emails"}
CLAUDE_STOPS = ("replies get no draft and are filed as \"other\", the copy check stops, and new companies wait unchecked "
                "for their industry label")
CLAUDE_WHAT = "Claude (the AI that reads replies, drafts answers and checks labels)"
MONTH_DAYS = 30  # "a month at this pace", for how much to buy


@dataclass
class Spend:
    """What the alerts and the daily post's spend line read, once a run."""

    apollo_left: float | None  # Apollo's lead-credit balance; None when it could not be read
    apollo_floor: int
    apollo_pace: float  # Apollo credits a day: the balance's fall when kept long enough, else the ledger's
    budgets: dict[str, budget.Budget]
    claude_usd: float
    claude_cap: float
    visitors: dict[str, float] | None = None  # credit_stats of the website-visitor credits, while site visits are on
    apollo_error: str = ""  # why the balance is unknown
    floor_skip: str = ""  # a source's recent skip for apollo_floor, when the balance is unknown
    alerts: list[tuple[str, str]] = field(default_factory=list)
    pace_basis: str = "ledger"  # balance or ledger: where apollo_pace comes from
    balance_fell: float | None = None  # the balance's fall over the pace's days (balance basis)
    ledger_spent: float | None = None  # what the ledger recorded over the same days
    pace_days: float = 0.0  # the days between the two readings

    @property
    def drift(self) -> float | None:
        """The share of the balance's fall the ledger did not record (0.45: 45% missing); None without both."""
        if self.balance_fell is None or self.ledger_spent is None or self.balance_fell <= 0:
            return None
        return (self.balance_fell - self.ledger_spent) / self.balance_fell

    @property
    def days_to_floor(self) -> float | None:
        """Days until the balance reaches apollo_floor at the pace; None with no balance, or no spend."""
        if self.apollo_left is None or self.apollo_pace <= 0:
            return None
        return max(0.0, (self.apollo_left - self.apollo_floor) / self.apollo_pace)

    def as_dict(self) -> dict[str, Any]:
        return {
            "apollo_left": self.apollo_left, "apollo_floor": self.apollo_floor, "apollo_pace": round(self.apollo_pace, 1),
            "days_to_floor": None if self.days_to_floor is None else round(self.days_to_floor, 1),
            "apollo_unknown": self.apollo_error or None, "claude_usd": round(self.claude_usd, 2),
            "claude_cap": self.claude_cap, "visitors": self.visitors, "asks": [k for k, _ in self.alerts],
            "pace_basis": self.pace_basis, "balance_fell": self.balance_fell, "ledger_spent": self.ledger_spent,
            "ledger_drift": None if self.drift is None else round(self.drift, 3),
        }


def site_visits_on(settings: Settings) -> bool:
    """site_visits reads Apollo's visitors: a tracked domain and US paths, and the job on the schedule."""
    from us_outbound.ops.schedule import enabled_names

    return bool(SITE_VISIT_DOMAIN.strip() and SITE_VISIT_US_PATHS) and "site_visits" in enabled_names()


def _floor_skip(ctx: Context) -> str:
    """The latest reason a floor job gave in the last LOOKBACK for not spending under apollo_floor, or ""."""
    best: tuple[datetime, str] | None = None
    for r in ctx.store.select("heartbeats", {"job": list(FLOOR_JOBS)}):
        detail = r.get("detail") if isinstance(r.get("detail"), Mapping) else {}
        reason = str(detail.get("reason") or "")
        at = utc(r.get("started_at"))
        if "apollo_floor" in reason and at is not None and ctx.now - at <= LOOKBACK and (best is None or at > best[0]):
            best = (at, reason)
    return best[1] if best else ""


def readings(ctx: Context) -> list[tuple[datetime, float]]:
    """The kept daily balances (events type apollo_balance), oldest first."""
    out = []
    for e in ctx.store.select("events", {"type": BALANCE}):
        t, v = utc(e.get("occurred_at")), e.get("amount")
        if t is not None and v is not None:
            out.append((t, float(v)))
    return sorted(out)


def balance_pace(ctx: Context, left: float | None) -> tuple[float, float, float] | None:
    """(credits a day, the balance's fall, the days between) from today's balance and the latest kept reading at
    least PACE_DAYS_BALANCE days before (at most BALANCE_LOOKBACK); None before BALANCE_READINGS readings, with no
    reading old enough, or when the balance rose (a purchase)."""
    kept = readings(ctx)
    if left is None or len(kept) < BALANCE_READINGS:
        return None
    since = ctx.now - timedelta(days=PACE_DAYS_BALANCE)
    earlier = [(t, v) for t, v in kept if t <= since + timedelta(hours=1)
               and t >= ctx.now - timedelta(days=BALANCE_LOOKBACK)]
    if not earlier:
        return None
    t0, v0 = earlier[-1]
    days = (ctx.now - t0).total_seconds() / 86400
    fell = v0 - left
    if days <= 0 or fell < 0:
        return None
    return fell / days, fell, days


def keep_balance(ctx: Context, left: float | None) -> None:
    """Today's Apollo balance as one events row a UK day (the latest read that day wins); additive (W2-3)."""
    if left is None:
        return
    ctx.store.upsert("events", [{"event_id": f"{BALANCE}:{ctx.today_uk().isoformat()}", "type": BALANCE,
                                 "occurred_at": ctx.now, "source": "apollo", "amount": float(left)}])


def read(ctx: Context) -> Spend:
    """Apollo's balances (one 0-credit read), the pace and the month's budgets and Claude spend."""
    s = ctx.settings
    left, visitors, error = None, None, ""
    try:
        usage = ctx.clients.apollo.credit_usage()
        left = credits_left(usage)
        visitors = credit_stats(usage, VISITOR_CREDIT) if site_visits_on(s) else None
        if left is None:
            error = "Apollo's answer had no lead-credit balance"
    except GuardViolation:
        raise
    except Exception as exc:  # a missing key, an API or network error: the balance is unknown today
        error = f"Apollo could not be read ({type(exc).__name__})"
        log("spend_apollo_unread", error=str(exc)[:200])
    spent = budget.spent_in(ctx.store, "apollo", ctx.now - timedelta(days=PACE_DAYS), ctx.now)
    sp = Spend(
        apollo_left=left, apollo_floor=s.general.apollo_floor, apollo_pace=spent / PACE_DAYS,
        budgets={sys: budget.monthly(ctx.store, s, sys, ctx.now) for sys in budget.MONTHLY_KEYS},
        claude_usd=month_spend_usd(ctx.store, ctx.now), claude_cap=float(s.general.claude_monthly_cap_usd),
        visitors=visitors, apollo_error=error, floor_skip=_floor_skip(ctx) if left is None else "",
    )
    by_balance = balance_pace(ctx, left)
    if by_balance is not None:
        pace, fell, days = by_balance
        sp.apollo_pace, sp.pace_basis, sp.balance_fell, sp.pace_days = pace, "balance", fell, days
        sp.ledger_spent = budget.spent_in(ctx.store, "apollo", ctx.now - timedelta(days=days), ctx.now)
    return sp


# -- the asks ----------------------------------------------------------------------------------------------
# Each ask is one block under the head (check): an icon and a bold line saying what has stopped or will stop and
# when, the numbers behind it, then "What to do:" with what to buy or which setting to change, how much, and where.


def _days(n: float) -> str:
    d = max(1, round(n))
    return f"{d} day" if d == 1 else f"{d:,} days"


def _next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def _reached(share: float, steps: tuple[float, ...]) -> float | None:
    return max((x for x in steps if share >= x), default=None)


def _general(ctx: Context) -> str:
    return say.sheet_link(ctx, "General")


def _apply() -> str:
    """How a sheet edit comes into force."""
    return f"it applies at the next settings sync, or now with {say.command('sync')}"


def _buy(sp: Spend) -> str:
    """How much Apollo to buy: a month at the pace, or what gets back above the floor when nothing is being spent."""
    if sp.apollo_pace > 0:
        return f"about {sp.apollo_pace * MONTH_DAYS:,.0f} credits covers a month at this pace"
    short = sp.apollo_floor - (sp.apollo_left or 0)
    return f"at least {short:,.0f} credits to get back above the reserve" if short > 0 else "credits"


def apollo_floor_ask(ctx: Context, sp: Spend) -> tuple[str, str] | None:
    """Apollo's balance under the floor, or the floor under FLOOR_WARN_DAYS away: once a UK day."""
    key = notify.once_key(ctx, "apollo_floor")
    floor = sp.apollo_floor
    reserve = f"`apollo_floor`, the {floor:,} credits we keep in reserve"
    if sp.apollo_left is not None and sp.apollo_left < floor:
        return key, (f"{say.URGENT} *Apollo is below our reserve, so we have stopped {STOPS['apollo']}.*\n"
                     f"{sp.apollo_left:,.0f} credits left; spending stops under {reserve}.\n"
                     f"*What to do:* buy credits in Apollo ({_buy(sp)}). Or, to spend into the reserve, lower "
                     f"`apollo_floor` (now {floor:,}) on the {_general(ctx)}.")
    days = sp.days_to_floor
    if days is not None and days < FLOOR_WARN_DAYS:
        when = ctx.today_uk() + timedelta(days=max(1, round(days)))
        monthly = ctx.settings.general.apollo_monthly_credits
        return key, (f"{say.ACTION} *Apollo runs out in about {_days(days)} (around {when:%a %-d %b}); then we "
                     f"stop {STOPS['apollo']}.*\n"
                     f"{sp.apollo_left:,.0f} credits left, spending about {sp.apollo_pace:,.0f} a day; spending stops "
                     f"under {reserve}.\n"
                     f"*What to do:* buy credits in Apollo ({_buy(sp)}). Or spend less: lower "
                     f"`apollo_monthly_credits` (now {monthly:,}) on the {_general(ctx)}.")
    if sp.apollo_left is None and sp.floor_skip:
        return key, (f"{say.URGENT} *Apollo is below our reserve, so we have stopped {STOPS['apollo']}.*\n"
                     f"{sp.floor_skip} (the last job to check said so; Apollo's balance could not be read today).\n"
                     f"*What to do:* buy credits in Apollo. Or, to spend into the reserve, lower `apollo_floor` (now "
                     f"{floor:,}) on the {_general(ctx)}.")
    return None


def budget_ask(ctx: Context, b: budget.Budget) -> tuple[str, str] | None:
    """A monthly budget at 80% or 100% used: once a UK month for each step."""
    if b.budget <= 0:
        return None
    step = _reached(b.used / b.budget, BUDGET_STEPS)
    if step is None:
        return None
    name, setting = b.system.capitalize(), budget.MONTHLY_KEYS[b.system]
    nxt = _next_month(b.month_end)
    key = notify.once_key(ctx, f"{b.system}_budget_{round(step * 100)}", per="month", on=b.month_end)
    used = f"{budget._n(b.used)} of the {budget._n(b.budget)} credits we allow a month are spent."
    need = b.projected
    now = f"`{setting}` (now {budget._n(b.budget)})"
    if step >= 1.0:
        more = f" to about {budget._n(need)}, what this month needs at its pace so far," if need and need > b.budget else ""
        return key, (f"{say.URGENT} *{name}'s monthly budget is used up, so we have stopped {STOPS[b.system]} until "
                     f"{nxt:%-d %b}.*\n{used}\n"
                     f"*What to do:* to carry on this month, raise {now}{more} on the {_general(ctx)}; {_apply()}. "
                     f"Otherwise it starts again on {nxt:%-d %b}.")
    if need and need > b.budget:
        do = (f"*What to do:* this month needs about {budget._n(need)} at its pace so far: raise {now} on the "
              f"{_general(ctx)}, or let it stop.")
    else:
        do = "Nothing to do yet: at this month's pace it lasts until the month ends."
    return key, (f"{say.ACTION} *{name}'s monthly budget is {b.used / b.budget:.0%} used; at 100% we stop {STOPS[b.system]} "
                 f"until {nxt:%-d %b}.*\n{used}\n{do}")


def claude_ask(ctx: Context, sp: Spend) -> tuple[str, str] | None:
    """Claude's spend this UTC month at 50%, 80% or 100% of the cap: once a month for each step."""
    cap = sp.claude_cap
    if cap <= 0:
        return None
    share = 1.0 if sp.claude_usd >= cap_reached_at(cap) else sp.claude_usd / cap
    step = _reached(share, CLAUDE_STEPS)
    if step is None:
        return None
    month = ctx.now.astimezone(UTC).date()
    nxt = _next_month(month)
    key = notify.once_key(ctx, f"claude_cap_{round(step * 100)}", per="month", on=month)
    need = sp.claude_usd / month.day * (nxt - month.replace(day=1)).days  # the UTC month at its pace, today counted
    spent = f"${sp.claude_usd:,.2f} of the ${cap:,.0f} monthly cap is spent"
    raise_it = (f"raise `claude_monthly_cap_usd` (now ${cap:,.0f}, at most ${CLAUDE_CAP_USD:,.0f}) on the "
                f"{_general(ctx)}, and the spend limit in the Anthropic Console to match")
    if step >= 1.0:
        return key, (f"{say.URGENT} *{CLAUDE_WHAT} has reached its monthly cap: until {nxt:%-d %b} (UTC), "
                     f"{CLAUDE_STOPS}.*\n{spent}; this month needs about ${need:,.0f} at its pace so far.\n"
                     f"*What to do:* {raise_it}.")
    if need <= cap:
        pace, do = f"; at this pace the month needs about ${need:,.0f}, inside the cap.", "Nothing to do yet."
    else:
        pace, do = f"; at this pace the month needs about ${need:,.0f}.", f"*What to do:* {raise_it}, before it runs out."
    return key, (f"{say.ACTION} *{CLAUDE_WHAT} has used {share:.0%} of its monthly cap.* At the cap, until "
                 f"{nxt:%-d %b}, {CLAUDE_STOPS}.\n{spent}{pace}\n{do}")


def visitors_ask(ctx: Context, sp: Spend) -> tuple[str, str] | None:
    """Apollo's website-visitor credits under VISITOR_WARN_SHARE left: once a UK month."""
    v = sp.visitors
    if not v or v["limit"] <= 0 or v["left_over"] / v["limit"] >= VISITOR_WARN_SHARE:
        return None
    return (notify.once_key(ctx, "apollo_visitors_low", per="month"),
            f"{say.ACTION} *Apollo's website-visitor credits are running low; when they run out, we stop learning "
            "which companies visit the US site.*\n"
            f"{v['left_over']:,.0f} of {v['limit']:,.0f} left ({v['left_over'] / v['limit']:.0%}).\n"
            f"*What to do:* buy more website-visitor credits in Apollo ({v['limit'] - v['left_over']:,.0f} used so far "
            "in this allowance).")


def drift_ask(ctx: Context, sp: Spend) -> tuple[str, str] | None:
    """The ledger missing more than DRIFT_LIMIT of the balance's fall: once an ISO week (S3, L12)."""
    d = sp.drift
    if d is None or d <= DRIFT_LIMIT:
        return None
    return (notify.once_key(ctx, "apollo_ledger_drift", per="week"),
            f"{say.ACTION} *Apollo's balance fell by more than our records of what we spent.*\n"
            f"In the last {sp.pace_days:.0f} days the balance fell {sp.balance_fell:,.0f} credits; the jobs recorded "
            f"{sp.ledger_spent:,.0f} ({fmt.share(d, places=0)} not recorded). The warnings now use the balance's pace, "
            f"{sp.apollo_pace:,.0f} a day.\n"
            "*What to do:* check Apollo's usage page for spend outside these jobs (someone using the Apollo app, or a "
            "job that does not record its credits).")


def asks(ctx: Context, sp: Spend) -> list[tuple[str, str]]:
    """Every (key, line) due today, before deduplication; the industry label check's asks last (labels.asks)."""
    from us_outbound import labels

    found = [apollo_floor_ask(ctx, sp), *(budget_ask(ctx, sp.budgets[sys]) for sys in budget.MONTHLY_KEYS),
             claude_ask(ctx, sp), visitors_ask(ctx, sp), drift_ask(ctx, sp)]
    return [a for a in found if a is not None] + labels.asks(ctx)


def head(lines: list[tuple[str, str]]) -> str:
    """The message's first line: the most urgent icon among the asks, and how many there are."""
    icon = say.URGENT if any(line.startswith(say.URGENT) for _, line in lines) else say.ACTION
    n = len(lines)
    return f"{icon} *Credits and settings: {n} {'thing needs' if n == 1 else 'things need'} a decision*"


def check(ctx: Context) -> tuple[Spend, dict[str, Any]]:
    """The daily check: read, then post the asks not posted before. Never raises (but a GuardViolation)."""
    sp = read(ctx)
    sp.alerts = asks(ctx, sp)
    try:
        keep_balance(ctx, sp.apollo_left)
        done = notify.sent_before(ctx, [k for k, _ in sp.alerts]) if sp.alerts else set()
        new = [(k, line) for k, line in sp.alerts if k not in done]
        sent = notify.post_once(ctx, new, head=head(new)) if new else {"posted": False, "keys": []}
    except GuardViolation:
        raise
    except Exception as exc:  # the database, say: the daily post still goes
        log("spend_alert_failed", error=str(exc)[:200])
        sent = {"posted": False, "keys": [], "alert_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    log("spend_check", **sp.as_dict(), posted=sent.get("keys"))
    return sp, sent


# -- the daily post's line ---------------------------------------------------------------------------------


def summary_line(sp: Spend) -> str:
    """"Spend: Apollo 28,512 credits left (about 570 days to the 5,000 reserve at 41 a day) · Claude $12.40 of $50
    this month (25%) · website-visitor credits 1,150 of 1,200 left"."""
    if sp.apollo_left is None:
        apollo = f"Apollo balance unknown ({sp.apollo_error or 'not read'})"
    else:
        apollo = f"Apollo {sp.apollo_left:,.0f} credits left"
        days = sp.days_to_floor
        if sp.apollo_left < sp.apollo_floor:
            apollo += f", below the {sp.apollo_floor:,} reserve: finding companies and emails has stopped"
        elif days is None:
            apollo += f" ({sp.apollo_floor:,} kept in reserve; none spent in {PACE_DAYS} days)"
        else:
            apollo += f" (about {_days(days)} to the {sp.apollo_floor:,} reserve at {sp.apollo_pace:,.0f} a day)"
        if sp.drift is not None and sp.drift > DRIFT_LIMIT:
            apollo += (f"; the balance fell {sp.balance_fell:,.0f} in {sp.pace_days:.0f} days, our records "
                       f"{sp.ledger_spent:,.0f}")
    parts = [apollo]
    if sp.claude_cap > 0:
        share = sp.claude_usd / sp.claude_cap
        parts.append(f"Claude ${sp.claude_usd:,.2f} of ${sp.claude_cap:,.0f} this month ({share:.0%})")
    if sp.visitors:
        parts.append(f"website-visitor credits {sp.visitors['left_over']:,.0f} of {sp.visitors['limit']:,.0f} left")
    return "Spend: " + " · ".join(parts)


# -- the mailboxes to add three weeks ahead (learn/capacity_ahead.py until W2-3) ----------------------------------
#
# Mailbox capacity three weeks ahead (Harry, 7 Oct 2026; learn/capacity_ahead.py until W2-3): ask for mailboxes
# while a new one still has time to warm.
#
# A new mailbox warms for about 21 days before it may send (registry/mailboxes.py promotes it then), so limits.py's
# same-day line ("Add a mailbox for ...", when sending capacity is already today's limit) comes about three weeks
# late. That line stays; this one looks ahead. On Mondays the Monday readout (learn/readout.py, 08:30; the daily post
# until 19 Oct 2026, when it moved to Tuesday to Saturday) compares:
#   * capacity at full ramp: each Active mailbox's daily cap once its sending ramp is over (ramp.Ramp.full_cap), each
#     sender's steady pace of new companies a day (capacity.steady_pace: the cap ÷ the 4 emails each new company
#     receives), and the send days in a week (the send window's weekdays): X new companies a week;
#   * what is needed: weekly_enrol_cap (Y), as far as there are companies to fill it in the WARMUP_WEEKS a new
#     mailbox takes: the Z ready now (verified, with a sendable contact, as the daily post counts them) spread over
#     those weeks, plus the recent supply in companies that can be emailed: the accounts verify_accounts verified a
#     week over the last SUPPLY_DAYS (learn/daily_post.verified_in) times p_c, the share pick_contacts finds a
#     contact at (sources/apollo_universe.contactable_supply; W1-3a, 19 Oct 2026). Until then every verified
#     account counted, though pick found nobody at about half of them (code-learn-reporting B11): on 19 Oct the need
#     read 148 a week while about 35 were enrolled, and after a cap raise it would have asked for idle mailboxes.
# When the need is above X, it posts once that ISO week, with the approvers mentioned (notify.post_once): "Add N
# mailboxes now: a new mailbox takes about 3 weeks to warm up. At full ramp your M Active mailboxes take about X new
# companies a week; weekly_enrol_cap is Y and Z companies are ready (W weeks)." N is how many mailboxes at the
# largest Active cap (MAX_CAP when none is Active) close the gap, less the ones already Warming; W is how long the
# ready companies last at X a week. Nothing is posted when no mailbox is needed.

WARMUP_WEEKS = 3  # a new mailbox's warmup (registry/mailboxes.py: Active after 21 days of warmup)
SUPPLY_DAYS = 14  # the recent supply: accounts verified over these days, as a weekly rate
ACTIVE, WARMING = "Active", "Warming"
MAILBOX_HEAD = f"{say.ACTION} *Sending mailboxes: add more now, as a new one takes {WARMUP_WEEKS} weeks to warm up*"


@dataclass(frozen=True)
class Ahead:
    active: int  # Active mailboxes (a mailbox a kill rule holds counts as Paused: ops/bootstrap.py)
    warming: int  # Warming mailboxes: Active within the warmup, so counted against what is asked for
    weekly: int  # X: new companies a week at full ramp
    cap: int  # Y: weekly_enrol_cap
    ready: int  # Z: companies ready to send now
    supply: float  # accounts verified a week, recently, times p_c: the ones that will have a contact
    per_mailbox: int  # new companies a week one more mailbox takes, at full ramp
    need: float  # what the companies on hand can fill a week, up to Y
    add: int  # N: mailboxes to add now
    p_c: float = 1.0  # the contact yield the supply is discounted by (apollo_universe.contact_yield)
    p_c_basis: str = ""  # measured, default or floored

    @property
    def weeks_ready(self) -> float | None:
        """How long the ready companies last at full-ramp capacity."""
        return self.ready / self.weekly if self.weekly > 0 else None

    def as_dict(self) -> dict[str, Any]:
        return {"active": self.active, "warming": self.warming, "weekly_at_full_ramp": self.weekly,
                "weekly_enrol_cap": self.cap, "ready": self.ready, "supply_per_week": round(self.supply, 1),
                "p_c": round(self.p_c, 3), "p_c_basis": self.p_c_basis,
                "per_mailbox": self.per_mailbox, "need_per_week": round(self.need, 1), "add": self.add}


def mailbox_plan(ctx: Context, ready: int) -> Ahead:
    """The comparison, from the ramp and capacity code's own arithmetic."""
    s = ctx.settings
    ramps = ramp.ramps(ctx.store, s, ctx.now_et().date())
    full: dict[str, int] = {}  # owner -> sends a day at full ramp
    caps: list[int] = []
    for m in s.mailboxes:
        if m.status != ACTIVE:
            continue
        r = ramps.get(m.address.lower())
        cap = r.full_cap if r is not None else max(0, int(m.daily_cap or 0))
        full[m.owner_name] = full.get(m.owner_name, 0) + cap
        caps.append(cap)
    days = len(s.general.send_window.days)
    weekly = sum(capacity.steady_pace(c) for c in full.values()) * days
    per_mailbox = capacity.steady_pace(max(caps, default=MAX_CAP)) * days
    from us_outbound.learn.daily_post import verified_in  # here: daily_post imports this module

    verified = verified_in(ctx.store, ctx.now - timedelta(days=SUPPLY_DAYS), ctx.now) or 0
    p_c = apollo_universe.contact_yield(ctx)
    supply = verified * 7 / SUPPLY_DAYS * p_c.value
    want = s.general.weekly_enrol_cap
    need = min(float(want), ready / WARMUP_WEEKS + supply)
    warming = sum(m.status == WARMING for m in s.mailboxes)
    add = 0
    if need > weekly and per_mailbox > 0:
        add = max(0, math.ceil((need - weekly) / per_mailbox) - warming)
    return Ahead(active=len(caps), warming=warming, weekly=weekly, cap=want, ready=ready, supply=supply,
                 p_c=p_c.value, p_c_basis=p_c.basis, per_mailbox=per_mailbox, need=need, add=add)


def mailbox_line(a: Ahead) -> str:
    """The ask, as anyone reads it: how many to add, the numbers behind it, the steps, and what happens without."""
    takes = "takes" if a.active == 1 else "take"
    text = (f"*Add {fmt.plural(a.add, 'mailbox', 'mailboxes')} now.* Fully warmed up, your "
            f"{fmt.plural(a.active, 'sending mailbox', 'sending mailboxes')} {takes} about {a.weekly} new companies a "
            f"week; the weekly target (`weekly_enrol_cap`) is {a.cap}, and {a.ready} companies are ready")
    weeks = a.weeks_ready
    text += f" ({weeks:.1f} weeks' worth)." if weeks is not None else "."
    if a.warming:
        text += f" {fmt.plural(a.warming, 'mailbox is', 'mailboxes are')} already warming up, counted in."
    add = say.command('mailbox add ADDRESS --owner "NAME" --live')
    return text + (
        "\n*What to do:*\n1. Set up each new mailbox and connect it in Instantly.\n"
        f"2. Add it here: {add} (NAME: the person named on the "
        "address). It warms up for 21 days, then its sending builds up week by week.\n"
        f"_Without them, sending stays at about {a.weekly} new companies a week._")


def mailboxes_check(ctx: Context, ready: int) -> dict[str, Any]:
    """Mondays (UK): post the ask when a mailbox is needed, once that ISO week. Never raises (but a GuardViolation)."""
    if ctx.today_uk().weekday() != 0:
        return {"checked": False}
    try:
        a = mailbox_plan(ctx, ready)
        out: dict[str, Any] = {"checked": True, **a.as_dict()}
        if a.add > 0:
            out["alert"] = notify.post_once(ctx, [(notify.once_key(ctx, "mailboxes", per="week"), mailbox_line(a))],
                                            head=MAILBOX_HEAD)
    except GuardViolation:
        raise
    except Exception as exc:  # the post it runs before still goes
        log("capacity_ahead_failed", error=str(exc)[:200])
        return {"checked": False, "capacity_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    log("capacity_ahead", **out)
    return out
