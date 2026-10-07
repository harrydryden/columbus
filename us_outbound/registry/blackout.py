"""Follow-ups pause over the blackout dates (Harry, 7 Oct 2026; docs/open-questions.md item 78, built).

Enrol skips a blackout date (General blackout_dates; budget.is_blackout) and a send approval's ✅ holds on one, but
Instantly's schedule knows weekdays only, so the later steps of the leads already in flight would still go out on a
blackout date. So the blackout job (hourly, ops/schedule.py) pauses every US Outbound campaign before a blackout
date and, once it is over, activates again exactly the campaigns it paused, so Instantly sends the steps that fell
due on the next send day. The send forecast (enrol/capacity.step_days) has always moved a step due on a blackout
date to the next send day, so the forecast and Instantly now agree.

When (hold), in US Eastern dates (the send window's time zone):
  * held: today is a blackout date on one of the send window's weekdays, or Instantly's next send day is one
    (instantly_day: its schedule's next weekday; today while the window has not closed). So the pause lands from
    the close of the window (16:00 ET) on the last send day before a blackout, at least 17 hours before 09:00 ET on
    its first day, and the resume from midnight ET after its last day (on Tue 5 Jan 2027, nine hourly runs before the
    window opens at 09:00 ET): many tries either side, whatever the UK and US clocks do, as both offsets are whole
    hours. Overlapping ranges are one hold, and so are ranges with only a weekend between them; 18 Dec 2026 to
    4 Jan 2027 spans the year end as any two dates do. A blackout date at a weekend alone holds nothing, as
    Instantly sends nothing then anyway;
  * until: the first send day after the hold (capacity.next_send_day), when the steps that fell due go out.

What it pauses while held: each campaign Instantly shows sending or able to (SENDING: active; completed, which is
active with no lead left to email and starts again the moment a lead is added, capacity.resume_if_completed; running
subsequences). Not a draft, which only `start` starts; not a campaign already paused, which a stop, a kill rule, an
empty sending list or a person paused for a reason of their own (the blackout never touches it, then or after); not
one Instantly holds for its own reasons (unhealthy accounts, bounce protection, a suspension). A campaign started
again during the hold (by hand in Instantly) is paused again by the next run. Each pause is a config_log row (kind
blackout_pause, config_version.log_change), with the leads then in flight, so the cohort report lists it.

What it starts again once the hold is over: only the campaigns whose latest blackout row is a pause (paused), each
once, and only while Instantly shows it paused (or a draft `start` deferred, below). It leaves paused, and says why:
  * one an operator stop holds (ops/heartbeat.enrolment_paused): `us-outbound start --live` starts it;
  * one a kill rule paused since (its mailbox hold left the owner no Active mailbox; registry/mailboxes
    ._kill_rule_pauses): mailbox_health starts it once that mailbox is Active again;
  * one whose owner has no Active mailbox now (a kill rule's hold counts), as _sync_campaign leaves it.
A campaign Instantly shows active (someone started it), completed (nothing left to email: the next lead added
starts it) or gone needs nothing. Each is a blackout_resume row, which settles the pause; a failed activation
leaves it open, and the next run tries again. A pause made by hand in Instantly's own pages during the blackout
cannot be told from the blackout's: `us-outbound stop --live` can, and that is the way to pause sending.

Live only for Instantly (SPEC 0.3): a dry run, or live_sending = no as for every job, says what it would pause or
start, changes nothing and records nothing. Slack: one line when it pauses and one when it starts them again
(notify.post_once, keyed by the hold's first send day after, so each is posted once), in the alert channel.

The rest of the system knows (words: "paused for the blackout until Mon 30 Nov"):
  * mailbox_health's start_waiting starts nothing while held, and reports a campaign the blackout paused as that,
    never as a pause to look into;
  * `start` during a hold resumes enrolment but leaves the campaigns paused, recorded as the blackout's, so the end
    of the hold starts them;
  * golive, status, the daily post, enrol and a send approval's hold say so (capacity.campaign_problem).

PHASE0-CONFIRM: that a campaign paused over the blackout and activated again sends the steps that fell due on its
next send day rather than skipping them, and that Instantly accepts a pause of a completed campaign.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from us_outbound import budget, config_version
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import CAMPAIGN_STATUS
from us_outbound.context import UK, Context
from us_outbound.learn import holds
from us_outbound.logs import log
from us_outbound.ops import notify
from us_outbound.ops.heartbeat import enrolment_paused
from us_outbound.settings.model import Settings

JOB = "blackout"
PAUSE, RESUME = config_version.BLACKOUT_PAUSE, config_version.BLACKOUT_RESUME  # config_log.kind
DRAFT, ACTIVE, PAUSED, COMPLETED = 0, 1, 2, 3  # Instantly's campaign status (clients/instantly.CAMPAIGN_STATUS)
SENDING = frozenset({ACTIVE, COMPLETED, 4})  # what a blackout pauses: active, completed, running subsequences
RESUMED, LEFT_PAUSED, NOT_NEEDED = "resumed", "left_paused", "not_needed"  # a blackout_resume row's outcome
HEAD = "Blackout dates:"


def _day(d: date | None) -> str:
    return f"{d:%a} {d.day} {d:%b}" if d else "the next send day"


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


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _status(campaign: Mapping[str, Any]) -> int | None:
    try:
        return int(campaign.get("status"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _owner(name: str) -> str:
    from us_outbound.enrol.capacity import owner_of

    return owner_of(name)


# -- when ---------------------------------------------------------------------------------------------------


def instantly_day(settings: Settings, now: datetime) -> date | None:
    """The day Instantly's schedule sends next: today while the send window has not closed, else its next weekday
    (the window's days, in its time zone). Instantly knows the weekdays, not our blackout dates."""
    w = settings.general.send_window
    t = now.astimezone(ZoneInfo(w.tz))
    day = t.date() + timedelta(days=1 if t.time() >= w.end else 0)
    for i in range(14):
        if (day + timedelta(days=i)).weekday() in w.days:
            return day + timedelta(days=i)
    return None


@dataclass(frozen=True)
class Hold:
    """A blackout the campaigns are paused over (US Eastern dates)."""

    first: date  # the blackout date that holds them now: today, or Instantly's next send day
    until: date | None  # the first send day after it, when the steps that fell due go out; None: none within a year

    def words(self) -> str:
        return f"paused for the blackout until {_day(self.until)}"


def hold(settings: Settings, now: datetime) -> Hold | None:
    """The blackout the campaigns are paused over now, or None (the module docstring)."""
    from us_outbound.enrol.capacity import next_send_day

    w = settings.general.send_window
    today = now.astimezone(ZoneInfo(w.tz)).date()
    nxt = instantly_day(settings, now)
    if today.weekday() in w.days and budget.is_blackout(today, settings):
        first = today
    elif nxt is not None and budget.is_blackout(nxt, settings):
        first = nxt
    else:
        return None
    return Hold(first, next_send_day(first, settings))


# -- what the blackout paused --------------------------------------------------------------------------------


def paused(store: Any) -> dict[str, dict]:
    """campaign -> its blackout_pause row, for each campaign whose latest blackout row is a pause: the blackout
    paused it (or `start` deferred it to the blackout's end) and has not started it again."""
    latest: dict[str, tuple[tuple[datetime, str], dict]] = {}
    floor = datetime.min.replace(tzinfo=UTC)
    for r in store.select(config_version.LOG_TABLE, {"kind": [PAUSE, RESUME]}):
        name = str(r.get("campaign") or "")
        key = (_ts(r.get("changed_at")) or floor, str(r.get("log_id")))
        if name and (name not in latest or key > latest[name][0]):
            latest[name] = (key, r)
    return {name: r for name, (_, r) in latest.items() if r.get("kind") == PAUSE}


def _words(until: date | None) -> str:
    return f"paused for the blackout until {_day(until)}"


def words(store: Any, settings: Settings, now: datetime) -> dict[str, str]:
    """campaign -> "paused for the blackout until Mon 30 Nov", for each campaign the blackout holds: until the first
    send day after the hold in force, or after the one it was paused for while the job has yet to start it."""
    rows = paused(store)
    if not rows:
        return {}
    h = hold(settings, now)
    return {name: _words(h.until if h else _date((r.get("detail") or {}).get("resumes_on")))
            for name, r in rows.items()}


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


class _InFlight:
    """The leads in flight per campaign (registry/mailboxes.leads_in_flight), read once a run and only when a row
    is written: live only, as the rows are."""

    def __init__(self, ctx: Context):
        self.ctx, self.counts = ctx, None

    def __getitem__(self, name: str) -> int:
        from us_outbound.registry import mailboxes as reg

        if self.counts is None:
            self.counts = reg.leads_in_flight(self.ctx, self.ctx.settings) if self.ctx.live else {}
        return int(self.counts.get(name, 0))


def _record(ctx: Context, flying: _InFlight, kind: str, name: str, keys: list[str], detail: Mapping[str, Any]) -> None:
    """One config_log row (live only, config_version.log_change), with the campaign's leads in flight."""
    if ctx.live:
        config_version.log_change(ctx, kind, name, keys, detail, flying[name])


def defer(ctx: Context, name: str, status: Any, h: Hold) -> None:
    """`start` during a hold: the campaign is left as it is and recorded as the blackout's (unless it is already), so
    the end of the hold starts it (live only, as the start itself)."""
    if name not in paused(ctx.store):
        _record(ctx, _InFlight(ctx), PAUSE, name, [],
                {"from": CAMPAIGN_STATUS.get(status, status), "resumes_on": _iso(h.until), "deferred": True})


# -- the job -------------------------------------------------------------------------------------------------


def _pause(ctx: Context, h: Hold, open_: Mapping[str, dict]) -> dict:
    """Pause every US Outbound campaign that is sending or could be; leave the rest as they are."""
    inst = ctx.clients.instantly
    flying = _InFlight(ctx)
    out: dict[str, Any] = {"paused": [], "still_paused": [], "left_alone": [], "errors": []}
    for c in inst.list_campaigns():
        name, status = str(c.get("name")), _status(c)
        if status not in SENDING:
            out["still_paused" if name in open_ and status in (DRAFT, PAUSED) else "left_alone"].append(name)
            continue
        if not ctx.dry_run:
            try:
                inst.pause_campaign(name)
            except (ApiError, LookupError) as exc:  # tried again by the next run, within the hour
                out["errors"].append(f"{name}: {str(exc)[:160]}")
                continue
        _record(ctx, flying, PAUSE, name, ["status"],
                {"from": CAMPAIGN_STATUS.get(status, status), "resumes_on": _iso(h.until)})
        out["paused"].append(name)
    if out["paused"]:
        n = len(out["paused"])
        line = (f"{'Would pause' if ctx.dry_run else 'Paused'} {n} US Outbound campaign{'' if n == 1 else 's'} for the "
                f"blackout ({', '.join(out['paused'])}), so no follow-up goes out on a blackout date. "
                f"{'They start' if n > 1 else 'It starts'} again by {'themselves' if n > 1 else 'itself'} for "
                f"{_day(h.until)}, when Instantly sends the follow-ups that fell due.")
        if ctx.dry_run:
            line += " Running dry, so nothing was paused: `us-outbound stop --live` pauses them by hand."
        out["slack"] = notify.post_once(ctx, [(f"{PAUSE}:{_iso(h.until)}", line)], head=HEAD)
    if ctx.dry_run:
        out["would_pause"] = out.pop("paused")
    return out


def _blocker(name: str, campaign: Mapping[str, Any] | None, row: Mapping[str, Any], stop: Mapping[str, Any] | None,
             kill: Mapping[str, list[dict]], settings: Settings) -> tuple[str, str] | None:
    """(outcome, why) when the campaign is not to be started again; None to start it. settings: with the kill rules'
    mailbox holds applied."""
    from us_outbound.registry import mailboxes as reg

    if campaign is None:
        return NOT_NEEDED, "it is no longer in Instantly"
    status = _status(campaign)
    if status not in (PAUSED, DRAFT):
        return NOT_NEEDED, f"Instantly shows it {CAMPAIGN_STATUS.get(status, status)}"
    if stop is not None:
        since = _ts(stop.get("started_at"))
        when = f" since {since.astimezone(UK):%a %d %b %H:%M} UK" if since else ""
        return LEFT_PAUSED, f"an operator stop is in force{when}: `us-outbound start --live` starts it"
    paused_at = _ts(row.get("changed_at"))
    killed = [i for i in kill.get(name, ()) if paused_at is None or (_ts(i.get("created_at")) or paused_at) > paused_at]
    if killed:
        why = (killed[0].get("payload") or {}).get("reason") or "a kill rule"
        return LEFT_PAUSED, f"a kill rule paused it since ({why}): it starts again once its mailbox is Active"
    if not reg.sending_list(settings, _owner(name)):
        return LEFT_PAUSED, "its owner has no Active mailbox now"
    return None


def _resume(ctx: Context, open_: Mapping[str, dict]) -> dict:
    """Start again each campaign the blackout paused, unless a stop, a kill rule or an empty sending list holds it."""
    from us_outbound.enrol.capacity import next_send_day
    from us_outbound.registry import mailboxes as reg

    inst = ctx.clients.instantly
    settings = holds.with_holds(ctx.store, ctx.settings)
    stop = enrolment_paused(ctx.store)
    since = min((t for r in open_.values() if (t := _ts(r.get("changed_at"))) is not None),
                default=ctx.now - timedelta(days=366))
    kill = reg._kill_rule_pauses(ctx.store, since - timedelta(seconds=1))
    found = {str(c.get("name")): c for c in inst.list_campaigns()}
    flying = _InFlight(ctx)
    out: dict[str, Any] = {"resumed": [], "left_paused": {}, "not_needed": {}, "errors": []}
    for name, row in sorted(open_.items()):
        blocked = _blocker(name, found.get(name), row, stop, kill, settings)
        if blocked is not None:
            outcome, why = blocked
            out[outcome][name] = why
            _record(ctx, flying, RESUME, name, [], {"outcome": outcome, "why": why})  # settles the pause
            continue
        if not ctx.dry_run:
            try:
                inst.activate_campaign(name)
            except (ApiError, LookupError) as exc:  # the pause stays open: the next run tries again
                out["errors"].append(f"{name}: {str(exc)[:160]}")
                continue
        _record(ctx, flying, RESUME, name, ["status"], {"outcome": RESUMED, "from": "paused", "to": "active"})
        out["resumed"].append(name)
    day = next_send_day(ctx.now_et().date(), ctx.settings)
    parts = []
    if out["resumed"]:
        n = len(out["resumed"])
        parts.append(f"{'Would start' if ctx.dry_run else 'Started'} {n} US Outbound campaign{'' if n == 1 else 's'} "
                     f"again after the blackout ({', '.join(out['resumed'])}): Instantly sends the follow-ups that "
                     f"fell due from {_day(day)}.")
    if out["left_paused"]:
        parts.append("Left paused: " + "; ".join(f"{c} ({why})" for c, why in out["left_paused"].items()) + ".")
    if parts:
        until = min((d for r in open_.values() if (d := _date((r.get("detail") or {}).get("resumes_on")))),
                    default=day)
        out["slack"] = notify.post_once(ctx, [(f"{RESUME}:{_iso(until)}", " ".join(parts))], head=HEAD)
    if ctx.dry_run:
        out["would_resume"] = out.pop("resumed")
    return out


def run(ctx: Context) -> dict:
    """The blackout job (JOB CONTRACT: run(ctx) -> summary), hourly: while a blackout holds, pause what sends; once
    it is over, start again what it paused. Outside a blackout, with nothing it paused, it reads nothing."""
    h = hold(ctx.settings, ctx.now)
    open_ = paused(ctx.store)
    out: dict[str, Any] = {"dry_run": ctx.dry_run, "held": h is not None,
                           "until": _iso(h.until) if h else None, "paused_by_blackout": sorted(open_)}
    if h is not None:
        out.update(_pause(ctx, h, open_))
    elif open_:
        out.update(_resume(ctx, open_))
    log(JOB, run_id=ctx.run_id, **out)
    return out
