"""The three go-live checks `us-outbound status` keeps (W2-1, 20 Oct 2026; they were ops/golive.py's).

The go/no-go command went with the one-off tools once the system was live (design-final §3.3). Three of its checks
still say something a live system can lose overnight, so `status` prints them; each is read-only:

  Copy        each active industry, for each contacted role, has an approved Copy row with a current QA pass: its own
              (label or group: PASS), only the General fallback (WARN, naming the row), or none (FAIL, naming the
              draft that waits)
  Campaigns   one "US Outbound – {owner}" campaign per owner, matching the settings, the registry and the ramp (the
              daily drift check; fix: campaigns ensure --fix --live). With live_sending = yes, every owner with an
              Active mailbox must have it active (enrol gives an owner whose campaign is not active no capacity):
              FAIL, "run start --live". With live_sending = no an active campaign WARNs, as live_sending does not
              stop Instantly: only `stop --live` pauses the campaigns. A campaign the blackout job paused PASSes,
              "paused for the blackout until <day>" (registry/blackout.py; Harry, 7 Oct 2026)
  Watchdog    US_OUTBOUND_WATCHDOG_URL is set (Harry, 7 Oct 2026; ops/watchdog.py): without it a dead worker,
              database or Slack token says nothing, so it WARNs

A check that cannot be made (a missing key, an API error) FAILs with the reason; one broken check never hides the
others. The HubSpot properties check is not kept (W1-2 dropped the six properties).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from us_outbound import labels
from us_outbound.base import holds
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import enrol
from us_outbound.enrol.capacity import CAMPAIGN_COMPLETED, TAKES_LEADS
from us_outbound.logs import redact
from us_outbound.registry import blackout
from us_outbound.settings.model import GENERAL_COPY, ROLE_LINE_COLUMNS

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
RANK = {PASS: 0, WARN: 1, FAIL: 2}
DETAIL_LIMIT = 30
COPY_DETAIL_LINES = 6
CAMPAIGN_ACTIVE = 1  # Instantly's campaign status for active (clients/instantly.CAMPAIGN_STATUS)
SYNC = ", then `us-outbound sync`"  # how a sheet edit comes into force


@dataclass
class Check:
    status: str
    name: str
    reason: str
    detail: list[str] = field(default_factory=list)


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: RANK[s]) if statuses else PASS


def contacted_roles(ctx: Context) -> list[str]:
    """The copy roles of the Roles rows contacted at some company size (Finance is not contacted).

    A Roles row's contacts get its copy role's copy (Role.writes_as: "HR manager" gets the People
    leader copy), so the check is per copy role, not per Roles row.
    """
    roles = [r.writes_as for r in ctx.settings.roles if r.order]
    return list(dict.fromkeys(roles)) or list(ROLE_LINE_COLUMNS)


def check_copy(ctx: Context) -> Check:
    s = ctx.settings
    active = [i for i in s.industries if i.active]
    if not active:
        return Check(FAIL, "Copy", f"no industry is active: switch one on on the Industries tab{SYNC}")
    rows = enrol.sendable_copy(s)
    roles = contacted_roles(ctx)
    detail, statuses, own = [], [], 0
    for ind in active:
        # A label the label check confirmed (labels.py): the most specific copy any of its companies can get.
        account = {"industry": ind.industry, "industry_group": ind.industry_group, "label_source": labels.AGREED}
        missing, general, notes, used = [], [], [], set()
        for role in roles:
            row, note = enrol.pick_copy(account, role, s, rows)
            if row is None:
                missing.append(role)
                if note:
                    notes.append(note)
            elif row.industry.casefold() == GENERAL_COPY.casefold():
                general.append(role)
                used.add(row.copy_version)
                if note:
                    notes.append(note)
            else:
                used.add(row.copy_version)
        why = f" ({'; '.join(dict.fromkeys(notes))})" if notes else ""
        if missing:
            statuses.append(FAIL)
            detail.append(f"{ind.industry}: nothing sendable for {', '.join(missing)}{why}")
        elif general:
            statuses.append(WARN)
            detail.append(f"{ind.industry}: falls back to General ({', '.join(sorted(used))}) for {', '.join(general)}{why}")
        else:
            statuses.append(PASS)
            own += 1
    status = _worst(statuses)
    fails, warns = statuses.count(FAIL), statuses.count(WARN)
    if len(detail) > COPY_DETAIL_LINES:  # the rest are alike, and would bury the checks below
        detail = [*detail[:COPY_DETAIL_LINES], f"… and {len(detail) - COPY_DETAIL_LINES} more industries like these"]
    if status == FAIL:
        reason = (f"{fails} of {len(active)} active industries have no approved copy with a current QA pass for "
                  f"every contacted role; approve rows on the Copy tab (status approved, approved_by){SYNC}")
    elif status == WARN:
        reason = f"{own} of {len(active)} active industries have their own approved copy; {warns} use the General row"
    else:
        reason = f"every active industry ({len(active)}) has approved, QA-current copy for {', '.join(roles)}"
    return Check(status, "Copy", reason, detail)


def check_watchdog(ctx: Context) -> Check:
    from us_outbound.ops import watchdog

    url, why = watchdog.read(ctx.clients.secrets)
    if url:
        return Check(PASS, "Watchdog", "set: heartbeat_check pings it every hour, and Healthchecks.io emails if it stops")
    if why.endswith("is not set"):
        return Check(WARN, "Watchdog", f"no outside watchdog: set {watchdog.VAR} (docs/railway-setup.md, step h)")
    return Check(WARN, "Watchdog", f"{why}: paste the check's ping URL from Healthchecks.io (docs/railway-setup.md, "
                                   "step h)")


def check_campaigns(ctx: Context) -> Check:
    from us_outbound.clients.instantly import CAMPAIGN_STATUS
    from us_outbound.registry import mailboxes as reg

    try:
        out = reg.ensure_campaigns(ctx, fix=False, create=False)
        raw = {c.get("name"): c.get("status") for c in ctx.clients.instantly.list_campaigns()}
    except (ConfigError, ApiError, LookupError) as exc:
        return Check(FAIL, "Campaigns", f"cannot read Instantly: {redact(str(exc))[:200]}")
    status_of = {name: CAMPAIGN_STATUS.get(v, v) for name, v in raw.items()}
    live = ctx.settings.general.live_sending
    # A campaign can only be created once its owner has an Active (warm) mailbox, and `start` skips an owner
    # without one, so that owner's missing campaign waits rather than blocks: the others can send (2 Oct 2026,
    # Harry's mailboxes still warming while Hannah's and Sam's were ready).
    settings = holds.with_holds(ctx.store, ctx.settings)
    no_mailbox = {reg.campaign_name(o) for o in settings.owners() if not reg.sending_list(settings, o)}
    waiting = [n for n in out["pending"] if n in no_mailbox]
    missing = [n for n in out["pending"] if n not in no_mailbox]
    # An owner with an Active mailbox whose campaign exists but is not active gets no capacity in a live enrol;
    # one the blackout job paused waits for that job, not for `start` (registry/blackout.py; Harry, 7 Oct 2026).
    names = [reg.campaign_name(o) for o in settings.owners()]
    over = blackout.words(ctx.store, ctx.settings, ctx.now)
    paused_over = {n: over[n] for n in names if n in over and raw.get(n) not in TAKES_LEADS}
    idle = [n for n in names if n not in no_mailbox and n in raw and raw[n] not in TAKES_LEADS and n not in paused_over]
    done = {n for n in names if raw.get(n) == CAMPAIGN_COMPLETED}
    active = sorted(n for n, v in raw.items() if v == CAMPAIGN_ACTIVE)
    # Drift held while leads are in flight (registry/mailboxes.IN_FLIGHT_KEYS; Harry, 7 Oct 2026) is a WARN: the
    # campaign is consistent for the leads in it, and new leads are rendered under the same constants.
    held = out.get("held") or {}
    drifted = {n: d for n, d in out["drift"].items() if set(d) - set((held.get(n) or {}).get("keys") or ())}
    detail = [f"{name}: {', '.join(f'{k} {v[1]!r}, expected {v[0]!r}' for k, v in sorted(d.items()))}"
              for name, d in out["drift"].items()]
    detail += [reg.held_words(name, h) for name, h in held.items()]
    detail += [f"{name}: missing" for name in missing]
    detail += [f"{name}: waits for a warm mailbox (created by mailbox_health once one is Active)" for name in waiting]
    detail += [f"{name}: matches ({status_of.get(name, '?')})"
               + ("; not sending: `us-outbound start --live` activates it" if name in idle else "")
               + ("; no lead left to email, and the next lead added resumes it" if name in done else "")
               + (f"; {paused_over[name]}, when the blackout job starts it again" if name in paused_over else "")
               for name in out["ok"]]
    detail += [f"{name}: not an owner in the registry (left alone)" for name in out.get("unknown") or ()]
    fails, warns = [], []
    if drifted or missing:
        fix = "`us-outbound campaigns ensure --fix --live`" if drifted else "`us-outbound campaigns ensure --live`"
        what = []
        if drifted:
            what.append(f"{len(drifted)} drifted")
        if missing:
            what.append(f"{len(missing)} missing")
        fails.append(f"{' and '.join(what)}; run {fix}, then `us-outbound start --live` to send")
    elif not out["ok"] and not held:
        fails.append("no campaign yet: every owner waits for a warm mailbox")
    if live and idle:
        whose = "its owner" if len(idle) == 1 else "their owners"
        fails.append(f"{len(idle)} not active in Instantly while live_sending = yes, so enrol gives {whose} nothing "
                     "to send: run `us-outbound start --live`")
    if not live and active:
        warns.append(f"{len(active)} active in Instantly; live_sending = no does not stop Instantly: to stop sending "
                     "run `us-outbound stop --live`")
    if waiting and (out["ok"] or out["drift"] or missing):  # not when every owner waits: the FAIL says so
        warns.append(f"{len(waiting)} {'waits' if len(waiting) == 1 else 'wait'} for a warm mailbox")
    if out.get("unknown"):
        warns.append("unknown US Outbound campaigns exist")
    if held:
        warns.append(f"{len(held)} with drift held while leads are in flight: `us-outbound campaigns ensure --fix "
                     "--in-flight --live` applies it to them, or it waits until `us-outbound cohorts in-flight` shows 0")
    if fails:
        return Check(FAIL, "Campaigns", "; ".join(fails + warns), detail)
    if warns:
        return Check(WARN, "Campaigns", f"{len(out['ok'])} exist and match; {'; '.join(warns)}", detail)
    if live and paused_over:
        until = sorted(set(paused_over.values()))[0]
        return Check(PASS, "Campaigns", f"all {len(out['ok'])} exist and match; {len(paused_over)} {until}",
                     detail)
    if live:
        return Check(PASS, "Campaigns", f"all {len(out['ok'])} exist, match and are active", detail)
    return Check(PASS, "Campaigns", f"all {len(out['ok'])} exist and match; `us-outbound start --live` activates them",
                 detail)


CHECKS: tuple[tuple[str, Callable[[Context], Check]], ...] = (
    ("Copy", check_copy),
    ("Campaigns", check_campaigns),
    ("Watchdog", check_watchdog),
)


def run_checks(ctx: Context) -> list[Check]:
    out: list[Check] = []
    for name, fn in CHECKS:
        try:
            c = fn(ctx)
        except GuardViolation:
            raise
        except Exception as exc:  # one broken check never hides the others
            c = Check(FAIL, name, f"could not check: {type(exc).__name__}: {redact(str(exc))[:200]}")
        out.append(c)
    return out


def lines(checks: list[Check]) -> list[str]:
    """The checks as `status` prints them: one line each, PASS, WARN or FAIL with the reason, its detail under it."""
    out = ["Go-live checks:"]
    for c in checks:
        out.append(f"  {c.status:<5} {c.name}: {c.reason}")
        shown = c.detail[:DETAIL_LIMIT]
        out += [f"          {d}" for d in shown]
        if len(c.detail) > len(shown):
            out.append(f"          … and {len(c.detail) - len(shown)} more")
    return out
