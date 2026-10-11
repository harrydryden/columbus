"""Credit and spend alerts (Harry, 7 Oct 2026): ask for a purchase or a raise before a balance or a budget runs out.

The daily post (learn/daily_post.py, 09:00 UK) runs this first, so the asks reach the alert channel just before
the post, in one message with the approvers mentioned (base/notify.post_once keeps what was sent in events, so a
rerun or a restart never repeats a line):
  * Apollo's own balance (credit_usage: lead credits) against General apollo_floor. Below the floor every source
    and pick_contacts' email reveals stop spending (sources/apollo_credits.floor_reason), and a source's skip for
    it counts as healthy for heartbeat_check, so this is where it is told. The pace is the average daily Apollo
    spend in credit_ledger over the last PACE_DAYS days; the ask comes when the floor is under FLOOR_WARN_DAYS away
    at that pace (a new mailbox or a purchase takes time), or the balance is already under it: once a UK day. When
    Apollo's balance cannot be read, a source's skip for the floor in the last LOOKBACK says it instead.
    Apollo's balance also falls with use outside these jobs (the Apollo app), which the ledger does not see.
  * The monthly budgets (budget.monthly: apollo_monthly_credits, clay_monthly_credits) at 80% and 100% used, once
    a UK month each, saying what stops at 100% and the key that raises it.
  * Claude's spend this month (credit_ledger, the UTC month, as clients/claude.py counts it) at 50%, 80% and 100%
    of claude_monthly_cap_usd, once a UTC month each. 100% is where the client refuses every call (the cap less
    its reserve, claude.cap_reached_at): replies are then classified "other" with no draft and copy QA stops.
  * The industry label check (labels.asks; Harry, 7 Oct 2026): many industry corrections at the cards, the rules and
    the model agreeing on too few companies, or the check unable to ask the model while new companies wait.
  * Apollo's website-visitor credits while site visits are on (site_visit_domain set, site_visits scheduled):
    under VISITOR_WARN_SHARE left, once a month. Not by pace: Apollo spends them itself each time its tracker names
    a visiting company, so credit_ledger has no record of their pace, and its answer gives no cycle dates.
Only the highest threshold reached is asked about, so a month that jumps from 70% to 100% gets one line.
summary_line is the daily post's one-line spend summary under the credit budgets. Nothing here raises (but a
GuardViolation): an Apollo that cannot be read leaves its balance unknown, and the daily post goes on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import budget
from us_outbound.base import notify, say
from us_outbound.clients.apollo import VISITOR_CREDIT, credit_stats, credits_left
from us_outbound.clients.claude import cap_reached_at, month_spend_usd
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import UK, Context
from us_outbound.logs import log
from us_outbound.settings.model import Settings
from us_outbound.settings.validate import CLAUDE_CAP_USD
from us_outbound.timeparse import utc

PACE_DAYS = 14  # the Apollo pace: its average daily spend in credit_ledger over this many days
FLOOR_WARN_DAYS = 21  # ask when apollo_floor is fewer days away than this
BUDGET_STEPS = (0.8, 1.0)  # apollo_monthly_credits and clay_monthly_credits
CLAUDE_STEPS = (0.5, 0.8, 1.0)  # claude_monthly_cap_usd
VISITOR_WARN_SHARE = 0.15  # Apollo's website-visitor credits: ask when fewer than this share are left
LOOKBACK = timedelta(hours=26)  # a source's skip for the floor this recent stands in for an unread balance
# The jobs that check Apollo's balance against apollo_floor before spending (sources/apollo_credits.floor_reason).
FLOOR_JOBS = ("source_universe", "apollo_signals", "apollo_enrich", "site_visits", "lookalikes", "lookalike_leads",
              "pick_contacts")
# What we stop doing when Apollo or Clay has no credits left, in plain words ("we have stopped ...").
STOPS = {"apollo": "finding new companies and looking up contacts' emails",
         "clay": "checking companies in Clay and looking up emails there"}
CLAUDE_STOPS = ("replies get no draft and are filed as \"other\", the copy check stops, and new companies wait unchecked "
                "for their industry label")
CLAUDE_WHAT = "Claude (the AI that reads replies, drafts answers and checks labels)"
MONTH_DAYS = 30  # "a month at this pace", for how much to buy


@dataclass
class Spend:
    """What the alerts and the daily post's spend line read, once a run."""

    apollo_left: float | None  # Apollo's lead-credit balance; None when it could not be read
    apollo_floor: int
    apollo_pace: float  # Apollo credits a day, the last PACE_DAYS days (credit_ledger)
    budgets: dict[str, budget.Budget]
    claude_usd: float
    claude_cap: float
    visitors: dict[str, float] | None = None  # credit_stats of the website-visitor credits, while site visits are on
    apollo_error: str = ""  # why the balance is unknown
    floor_skip: str = ""  # a source's recent skip for apollo_floor, when the balance is unknown
    alerts: list[tuple[str, str]] = field(default_factory=list)

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
        }


def site_visits_on(settings: Settings) -> bool:
    """site_visits reads Apollo's visitors: a tracked domain and US paths, and the job on the schedule."""
    from us_outbound.ops.schedule import enabled_names

    g = settings.general
    return bool(g.site_visit_domain.strip() and g.site_visit_us_paths) and "site_visits" in enabled_names()


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


def read(ctx: Context) -> Spend:
    """Apollo's balances (one 0-credit read), the ledger's pace and the month's budgets and Claude spend."""
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
    return Spend(
        apollo_left=left, apollo_floor=s.general.apollo_floor, apollo_pace=spent / PACE_DAYS,
        budgets={sys: budget.monthly(ctx.store, s, sys, ctx.now) for sys in ("apollo", "clay")},
        claude_usd=month_spend_usd(ctx.store, ctx.now), claude_cap=float(s.general.claude_monthly_cap_usd),
        visitors=visitors, apollo_error=error, floor_skip=_floor_skip(ctx) if left is None else "",
    )


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


def asks(ctx: Context, sp: Spend) -> list[tuple[str, str]]:
    """Every (key, line) due today, before deduplication; the industry label check's asks last (labels.asks)."""
    from us_outbound import labels

    found = [apollo_floor_ask(ctx, sp), *(budget_ask(ctx, sp.budgets[sys]) for sys in ("apollo", "clay")),
             claude_ask(ctx, sp), visitors_ask(ctx, sp)]
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
    parts = [apollo]
    if sp.claude_cap > 0:
        share = sp.claude_usd / sp.claude_cap
        parts.append(f"Claude ${sp.claude_usd:,.2f} of ${sp.claude_cap:,.0f} this month ({share:.0%})")
    if sp.visitors:
        parts.append(f"website-visitor credits {sp.visitors['left_over']:,.0f} of {sp.visitors['limit']:,.0f} left")
    return "Spend: " + " · ".join(parts)
