"""`us-outbound golive`: Harry's one go/no-go command before the first sends (Harry, 1 Oct 2026).

It walks the blockers of docs/gtm-review/README.md §4.1 and the go-live checklist of §7.6 that
the system can see, and prints one line per check, PASS, WARN or FAIL with the reason (and the
detail under it). It exits 1 if any check FAILs, 0 otherwise.

Read-only: it runs in dry-run, writes no heartbeat row, and only reads (the database, Instantly,
Slack's channel list), so the guard would refuse any write. A check that cannot be made (a
missing key, an API error) FAILs with the reason.

  Settings          the settings in force validate (otherwise nothing else can be checked)
  Copy              each active industry, for each contacted role, has an approved Copy row with a
                    current QA pass: its own (label or group: PASS), only the General fallback
                    (WARN, naming the row), or none (FAIL, naming the draft that waits)
  Mailboxes         every Active mailbox is warm in Instantly, with its place on the sending ramp
  live_sending      yes in the synced settings (the [ASK HARRY] sign-off, SPEC 14 phase 2)
  Approvers         approver_slack_ids is set (SPEC 11: only approvers' ✅ counts, on send cards and replies)
  Slack             the bot token is set and the bot can see the alert channel
  Watchdog          US_OUTBOUND_WATCHDOG_URL is set (Harry, 7 Oct 2026; ops/watchdog.py): without it a dead
                    worker, database or Slack token says nothing, so it WARNs
  Campaigns         one "US Outbound – {owner}" campaign per owner, matching the settings, the
                    registry and the ramp (the daily drift check; fix: campaigns ensure --fix --live).
                    With live_sending = yes, every owner with an Active mailbox must have it active
                    (status 1; enrol gives an owner whose campaign is not active no capacity): FAIL,
                    "run start --live". With live_sending = no an active campaign WARNs, as
                    live_sending does not stop Instantly: only `stop --live` pauses the campaigns
  Apollo budget     credits left this month for finding emails
  Queue             verified accounts with a sendable contact, against today's number, counting the
                    send approvals still waiting as enrol does (their accounts and their senders' slots)
  auto_send         the General switch (Harry, 2 Oct 2026): no PASSes, as every email then waits for
                    an approver's ✅ in Slack (enrol/approvals.py; the Slack line checks the token);
                    yes WARNs, as emails are then added to Instantly without approval
  Second contact    the General switch (Harry, 6 Oct 2026; enrol/second.py), for information: off, or on
                    with its size threshold and delay; either PASSes
  Hand-check        only while auto_send = yes: this week's hand-check approved (enrol waits for it);
                    with auto_send = no every email is approved in Slack, so the line is left out
  Enrollment        not stopped by an operator, the stop rule, or a positive reply waiting
  Jobs              the jobs a live send needs are built and scheduled: enrol, sync_outcomes (the
                    kill rules' data), poll_replies (replies and opt-outs), poll_approvals,
                    kill_rules, mailbox_health
  clay_verification skip PASSes (accounts verified on Apollo data and HubSpot); required WARNs while
                    verify_in_clay is not built, as no new account is verified then
  HubSpot ids       hubspot_pipeline_id, hubspot_deal_stage_id and hubspot_owner_id are set; WARN
                    without them, as a positive reply then creates no HubSpot deal (crm/hubspot_writes.py)
  Opt-out tested    optout_tested = yes: the seed-inbox test of Instantly's unsubscribe link
                    (blocker B1; PHASE0-CONFIRM in clients/instantly.py)

What Harry reads is plain: no SPEC numbers in a line. Every FAIL that asks him to change the sheet
ends ", then `us-outbound sync`", since jobs read the synced copy of the sheet. The GO footer says
what comes next.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from us_outbound import budget, limits
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, ConfigError, Context
from us_outbound.enrol import enrol, second
from us_outbound.enrol.capacity import CAMPAIGN_COMPLETED, TAKES_LEADS
from us_outbound.learn import holds
from us_outbound.logs import redact
from us_outbound.registry import ramp
from us_outbound.settings.model import GENERAL_COPY, ROLE_LINE_COLUMNS

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
RANK = {PASS: 0, WARN: 1, FAIL: 2}
DETAIL_LIMIT = 30
NEEDED_JOBS = ("enrol", "sync_outcomes", "poll_replies", "poll_approvals", "kill_rules", "mailbox_health")
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


def _when(v: Any) -> str:
    if not isinstance(v, datetime):
        return "never"
    return v.astimezone(UK).strftime("%a %d %b %H:%M UK")


# -- the checks ---------------------------------------------------------------------------------


def check_settings(ctx: Context) -> Check:
    from us_outbound.settings import sync

    return Check(PASS, "Settings", f"usable; synced {_when(sync.last_read(ctx.store, ctx.settings))}")


def contacted_roles(ctx: Context) -> list[str]:
    """The copy roles of the Roles rows contacted at some company size (Finance is not contacted).

    A Roles row's contacts get its copy role's copy (Role.writes_as: "HR manager" gets the People
    leader copy), so the check is per copy role, not per Roles row.
    """
    roles = [r.writes_as for r in ctx.settings.roles if r.order]
    return list(dict.fromkeys(roles)) or list(ROLE_LINE_COLUMNS)


COPY_DETAIL_LINES = 6


def check_copy(ctx: Context) -> Check:
    s = ctx.settings
    active = [i for i in s.industries if i.active]
    if not active:
        return Check(FAIL, "Copy", f"no industry is active: switch one on on the Industries tab{SYNC}")
    rows = enrol.sendable_copy(s)
    roles = contacted_roles(ctx)
    detail, statuses, own = [], [], 0
    for ind in active:
        account = {"industry": ind.industry, "industry_group": ind.industry_group}
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


def check_mailboxes(ctx: Context) -> Check:
    from us_outbound.registry import mailboxes as reg

    s = ctx.settings
    boxes = [m for m in s.mailboxes if m.status != reg.RETIRED]
    if not boxes:
        return Check(FAIL, "Mailboxes", "no mailbox in the registry")
    ramps = ramp.ramps(ctx.store, s, ctx.now_et().date())
    held = holds.held_mailboxes(ctx.store)
    try:
        warm = ctx.clients.instantly.warmup_status([m.address for m in boxes])
    except (ConfigError, ApiError) as exc:
        return Check(FAIL, "Mailboxes", f"cannot read Instantly: {redact(str(exc))[:200]}")
    detail, statuses, cap = [], [], 0
    for m in boxes:
        a = m.address.lower()
        w = warm.get(a, {})
        r = ramps.get(a)
        on_ramp = (r.describe() or f"{r.cap} a day (past the ramp)") if r else ""
        if m.status == reg.ACTIVE:
            if not w.get("found"):
                statuses.append(FAIL)
                detail.append(f"{a}: Active on the sheet, but not found in Instantly")
            elif reg.warmup_to_status(w, today=ctx.today_uk(), added_on=m.added_on) != reg.ACTIVE:
                statuses.append(FAIL)
                state = "warmup off" if not w.get("warmup_enabled") else f"warmup score {w.get('warmup_score')}"
                detail.append(f"{a}: Active on the sheet, but Instantly does not show it warm ({state}, account {w.get('status')})")
            else:
                statuses.append(PASS)
                cap += r.cap if r else m.daily_cap
                score = w.get("warmup_score")
                detail.append(f"{a}: Active and warm" + (f" (score {score})" if score is not None else "") + f"; {on_ramp}")
        else:
            statuses.append(WARN)
            why = f"; held by a kill rule: {held[a]}" if a in held else ""
            if m.status == reg.WARMING and w.get("found") and \
                    reg.warmup_to_status(w, today=ctx.today_uk(), added_on=m.added_on) == reg.ACTIVE:
                why = "; Instantly shows it warm: `us-outbound mailbox check --live` promotes it"
            detail.append(f"{a}: {m.status}, not on a sending list{why}")
    active = [m for m in boxes if m.status == reg.ACTIVE]
    if not active:
        return Check(FAIL, "Mailboxes", "no Active mailbox: nothing can send", detail)
    status = _worst(statuses)
    ok = statuses.count(PASS)
    return Check(status, "Mailboxes", f"{ok} of {len(boxes)} Active and warm; {cap} sends a day today on the ramp", detail)


def check_live_sending(ctx: Context) -> Check:
    if ctx.settings.general.live_sending:
        return Check(PASS, "live_sending", "yes")
    return Check(FAIL, "live_sending", f"no: set live_sending = yes on the General tab once Harry signs off{SYNC}")


def check_approvers(ctx: Context) -> Check:
    ids = ctx.settings.general.approver_slack_ids
    if ids:
        return Check(PASS, "Approvers", f"approver_slack_ids: {', '.join(ids)}")
    return Check(FAIL, "Approvers", "approver_slack_ids is blank: nobody can approve an email or a reply in Slack; "
                                    f"set it on the General tab{SYNC}")


def check_slack(ctx: Context) -> Check:
    channel = ctx.settings.general.alert_channel
    try:
        ctx.clients.secrets.get("slack")
    except ConfigError:
        return Check(FAIL, "Slack", "US_OUTBOUND_SLACK_BOT_TOKEN is not set: alerts and approvals need it "
                                    "(without it posts go to the log)")
    try:
        ctx.clients.slack.channel_id(channel)
    except LookupError:
        return Check(FAIL, "Slack", f"the token is set, but the bot cannot see {channel}: invite it to the channel")
    except ApiError as exc:
        return Check(FAIL, "Slack", f"the token is set, but Slack refused it: {redact(str(exc))[:160]}")
    return Check(PASS, "Slack", f"token set; the bot can see {channel}")


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
    # An owner with an Active mailbox whose campaign exists but is not active gets no capacity in a live enrol.
    names = [reg.campaign_name(o) for o in settings.owners()]
    idle = [n for n in names if n not in no_mailbox and n in raw and raw[n] not in TAKES_LEADS]
    done = {n for n in names if raw.get(n) == CAMPAIGN_COMPLETED}
    active = sorted(n for n, v in raw.items() if v == CAMPAIGN_ACTIVE)
    detail = [f"{name}: {', '.join(f'{k} {v[1]!r}, expected {v[0]!r}' for k, v in sorted(d.items()))}"
              for name, d in out["drift"].items()]
    detail += [f"{name}: missing" for name in missing]
    detail += [f"{name}: waits for a warm mailbox (created by mailbox_health once one is Active)" for name in waiting]
    detail += [f"{name}: matches ({status_of.get(name, '?')})"
               + ("; not sending: `us-outbound start --live` activates it" if name in idle else "")
               + ("; no lead left to email, and the next lead added resumes it" if name in done else "")
               for name in out["ok"]]
    detail += [f"{name}: not an owner in the registry (left alone)" for name in out.get("unknown") or ()]
    fails, warns = [], []
    if out["drift"] or missing:
        fix = "`us-outbound campaigns ensure --fix --live`" if out["drift"] else "`us-outbound campaigns ensure --live`"
        what = []
        if out["drift"]:
            what.append(f"{len(out['drift'])} drifted")
        if missing:
            what.append(f"{len(missing)} missing")
        fails.append(f"{' and '.join(what)}; run {fix}, then `us-outbound start --live` to send")
    elif not out["ok"]:
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
    if fails:
        return Check(FAIL, "Campaigns", "; ".join(fails + warns), detail)
    if warns:
        return Check(WARN, "Campaigns", f"{len(out['ok'])} exist and match; {'; '.join(warns)}", detail)
    if live:
        return Check(PASS, "Campaigns", f"all {len(out['ok'])} exist, match and are active", detail)
    return Check(PASS, "Campaigns", f"all {len(out['ok'])} exist and match; `us-outbound start --live` activates them",
                 detail)


def check_apollo(ctx: Context) -> Check:
    b = budget.monthly(ctx.store, ctx.settings, "apollo", ctx.now)
    if b.budget <= 0:
        return Check(WARN, "Apollo budget", "apollo_monthly_credits is 0: no email is looked up this month")
    if b.spent:
        return Check(WARN, "Apollo budget", f"{b.describe()}. Enrollment continues from accounts already ready")
    return Check(PASS, "Apollo budget", f"{budget._n(b.remaining)} of {budget._n(b.budget)} credits left this month, "
                                        f"up to {budget._n(b.left_today)} today ({b.weekdays_left} weekdays left)")


def check_queue(ctx: Context) -> Check:
    from us_outbound.enrol import approvals

    day = ctx.now_et().date()
    _, pulled = enrol.hand_check(ctx, day)
    # As enrol.run: the send approvals still waiting keep their accounts out and hold their senders' slots.
    held = approvals.waiting(ctx)
    ready, skipped = enrol.candidates(ctx, pulled, held.accounts)
    seconds, _ = second.candidates(ctx, pulled, held.accounts)  # none while second_contact is no
    lim = limits.today(ctx, day, ready_accounts=len(ready) + len(seconds), pending=held.by_owner, campaigns=True,
                       second_ready=len(seconds))
    t = lim.terms
    detail = [lim.explanation, *lim.detail]
    if skipped:
        detail.append("Not ready: " + ", ".join(f"{k} {v}" for k, v in skipped.most_common(8)))
    cards = f"; {held.total} {'card waits' if held.total == 1 else 'cards wait'} for a ✅" if held.total else ""
    want = min(t["weekly_target"], t["sending_capacity"])
    if not ready:
        return Check(FAIL, "Queue", f"no verified account has a sendable contact{cards}", detail)
    if len(ready) < want:
        return Check(WARN, "Queue", f"{len(ready)} ready (verified with a sendable contact), fewer than today's "
                                    f"{want} (weekly target {t['weekly_target']}, sending capacity "
                                    f"{t['sending_capacity']}){cards}", detail)
    pace = want or t["sending_capacity"]  # at the weekend, or once the week's target is met, today's number is 0
    supply = f"about {len(ready) / pace:.0f} send days at {pace} a day" if pace else "none go today"
    return Check(PASS, "Queue", f"{len(ready)} ready (verified with a sendable contact): {supply}{cards}", detail)


def check_second_contact(ctx: Context) -> Check:
    """The switch (Harry, 6 Oct 2026; enrol/second.py): informational, as either value is safe to go live with."""
    return Check(PASS, "Second contact", second.describe(ctx.settings).removeprefix("Second contact: "))


def check_hand_check(ctx: Context) -> Check | None:
    """Only while auto_send = yes: with no, every email is approved in Slack and enrol does not wait for it."""
    if not ctx.settings.general.auto_send:
        return None
    week = enrol.iso_week(ctx.now_et().date())
    why, pulled = enrol.hand_check(ctx, ctx.now_et().date())
    if why is None:
        return Check(PASS, "Hand-check", f"{week} approved" + (f"; {len(pulled)} accounts pulled" if pulled else ""))
    return Check(FAIL, "Hand-check", f"{why}: `us-outbound handcheck show` (prints the sample), "
                                     "then `us-outbound handcheck approve --live` (records and approves it)")


def check_auto_send(ctx: Context) -> Check:
    """The switch (Harry, 2 Oct 2026). With no, approvals need Slack, which the Slack line checks."""
    if ctx.settings.general.auto_send:
        return Check(WARN, "auto_send", "auto_send = yes: emails are added without approval")
    return Check(PASS, "auto_send", "auto_send = no: every email waits for approval in Slack")


def check_enrolment(ctx: Context) -> Check:
    why = [w for w in (enrol.operator_pause(ctx), holds.enrolment_stop(ctx.store), enrol.reply_pause(ctx)) if w]
    held = holds.held_mailboxes(ctx.store)
    sources, groups = holds.paused_sources(ctx.store), holds.stopped_groups(ctx.store)
    detail = [f"{a}: held by a kill rule ({r})" for a, r in held.items()]
    detail += [f"email source {k}: paused by a kill rule ({r})" for k, r in sources.items()]
    detail += [f"industry group {k}: stopped by a kill rule ({r})" for k, r in groups.items()]
    if why:
        return Check(FAIL, "Enrollment", "; ".join(why), detail)
    if detail:
        return Check(WARN, "Enrollment", "not paused, but kill rules hold some of it back (`us-outbound killrules show`)", detail)
    return Check(PASS, "Enrollment", "not stopped by an operator, the stop rule or a waiting reply")


def check_jobs(ctx: Context) -> Check:
    from us_outbound.ops import cli
    from us_outbound.ops.schedule import by_name

    table = by_name()
    detail = []
    for job in NEEDED_JOBS:
        target = cli.JOBS.get(job, "unknown")
        if not cli._is_target(target):
            detail.append(f"{job}: {target}")
        elif job not in table or not table[job].enabled or not table[job].cron:
            detail.append(f"{job}: built, but not enabled in ops/schedule.py (a reviewed code change)")
    if detail:
        return Check(FAIL, "Jobs", f"{len(detail)} of the {len(NEEDED_JOBS)} jobs a live send needs will not run", detail)
    return Check(PASS, "Jobs", f"{', '.join(NEEDED_JOBS)} are built and scheduled")


def check_clay_verification(ctx: Context) -> Check:
    from us_outbound.settings.model import CLAY_SKIP

    if ctx.settings.general.clay_verification == CLAY_SKIP:
        return Check(PASS, "clay_verification", "skip: accounts are verified on Apollo data and HubSpot")
    return Check(WARN, "clay_verification", "required: accounts wait for Clay's verification, which is not built yet, "
                                            f"so no new account is verified; set it to skip on the General tab{SYNC}")


def check_hubspot_ids(ctx: Context) -> Check:
    g = ctx.settings.general
    keys = ("hubspot_pipeline_id", "hubspot_deal_stage_id", "hubspot_owner_id")
    blank = [k for k in keys if not str(getattr(g, k)).strip()]
    if blank:
        return Check(WARN, "HubSpot ids", f"{', '.join(blank)} blank: a positive reply creates no HubSpot deal; run "
                                          f"`us-outbound hubspot ids` and paste them on the General tab{SYNC}")
    return Check(PASS, "HubSpot ids", "pipeline, deal stage and owner set: a positive reply creates a deal")


def check_optout(ctx: Context) -> Check:
    if ctx.settings.general.optout_tested:
        return Check(PASS, "Opt-out tested", "optout_tested = yes: the seed-inbox test of the unsubscribe link is done")
    return Check(FAIL, "Opt-out tested", "seed-inbox test of the unsubscribe link not done: `us-outbound seed send ADDRESS "
                                         "--owner NAME --live`; once it arrives, check it and click unsubscribe; "
                                         "`us-outbound seed check` says PASS; then set optout_tested = yes on the "
                                         f"General tab{SYNC}")


CHECKS: tuple[tuple[str, Callable[[Context], Check | None]], ...] = (
    ("Settings", check_settings),
    ("Copy", check_copy),
    ("Mailboxes", check_mailboxes),
    ("live_sending", check_live_sending),
    ("Approvers", check_approvers),
    ("Slack", check_slack),
    ("Watchdog", check_watchdog),
    ("Campaigns", check_campaigns),
    ("Apollo budget", check_apollo),
    ("Queue", check_queue),
    ("auto_send", check_auto_send),
    ("Second contact", check_second_contact),
    ("Hand-check", check_hand_check),
    ("Enrollment", check_enrolment),
    ("Jobs", check_jobs),
    ("clay_verification", check_clay_verification),
    ("HubSpot ids", check_hubspot_ids),
    ("Opt-out tested", check_optout),
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
        if c is not None:
            out.append(c)
    return out


def unusable(errors: dict[str, list[Any]]) -> list[Check]:
    detail = [f"{tab}: {e}" for tab, errs in errors.items() for e in errs[:5]]
    return [Check(FAIL, "Settings", f"unusable: fix the sheet{SYNC}", detail)]


def render(checks: list[Check], now: datetime, *, channel: str = "#us-outbound", auto_send: bool = False) -> str:
    lines = [f"Go-live check, {now.astimezone(UK):%a %d %b %Y %H:%M} UK (read-only: nothing was changed)"]
    for c in checks:
        lines.append(f"{c.status:<5} {c.name}: {c.reason}")
        shown = c.detail[:DETAIL_LIMIT]
        lines += [f"        {d}" for d in shown]
        if len(c.detail) > len(shown):
            lines.append(f"        … and {len(c.detail) - len(shown)} more")
    n = {s: sum(c.status == s for c in checks) for s in (FAIL, WARN, PASS)}
    verdict = "NO-GO" if n[FAIL] else "GO"
    if n[FAIL]:
        nxt = " Fix every FAIL, then run `us-outbound golive` again."
    elif auto_send:
        nxt = " Next: `us-outbound start --live`; enrol adds the day's leads to Instantly at 12:00 UK."
    else:
        nxt = f" Next: `us-outbound start --live`; cards arrive in {channel} after enrol at 12:00 UK."
    lines.append(f"{verdict}: {n[FAIL]} FAIL, {n[WARN]} WARN, {n[PASS]} PASS.{nxt}")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
