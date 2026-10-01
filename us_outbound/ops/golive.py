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
  live_sending      yes on the General tab (the [ASK HARRY] sign-off, SPEC 14 phase 2)
  Approvers         approver_slack_ids is set (SPEC 11: only approvers' replies count)
  Slack             the bot token is set and the bot can see the alert channel
  Campaigns         one "US Outbound – {owner}" campaign per owner, matching the settings, the
                    registry and the ramp (the daily drift check; fix: campaigns ensure --fix --live)
  Apollo budget     credits left this month for finding emails
  Queue             verified accounts with a sendable contact, against today's number
  Hand-check        this week's hand-check approved (enrol waits for it)
  Enrollment        not stopped by an operator, the stop rule, or a positive reply waiting
  Jobs              the jobs a live send needs are built and scheduled: enrol, sync_outcomes (the
                    kill rules' data), poll_replies (replies and opt-outs), poll_approvals,
                    kill_rules, mailbox_health
  clay_verification the General key, when this build has it (the sourcing build adds it)
  Opt-out tested    optout_tested = yes: the seed-inbox test of Instantly's {{unsubscribe}} link
                    (blocker B1; PHASE0-CONFIRM in clients/instantly.py)
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
from us_outbound.enrol import enrol
from us_outbound.learn import holds
from us_outbound.logs import redact
from us_outbound.registry import ramp
from us_outbound.settings.model import GENERAL_COPY, ROLE_LINE_COLUMNS

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
RANK = {PASS: 0, WARN: 1, FAIL: 2}
DETAIL_LIMIT = 30
NEEDED_JOBS = ("enrol", "sync_outcomes", "poll_replies", "poll_approvals", "kill_rules", "mailbox_health")
_MISSING = object()


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
    return Check(PASS, "Settings", f"usable; synced {_when(ctx.settings.synced_at)}")


def contacted_roles(ctx: Context) -> list[str]:
    """Roles the Roles tab contacts at some company size (Finance is not contacted)."""
    roles = [r.role for r in ctx.settings.roles if r.first_choice_for_size or r.fallback_order]
    return roles or list(ROLE_LINE_COLUMNS)


def check_copy(ctx: Context) -> Check:
    s = ctx.settings
    active = [i for i in s.industries if i.active]
    if not active:
        return Check(FAIL, "Copy", "no industry is active on the Industries tab")
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
    if status == FAIL:
        reason = (f"{fails} of {len(active)} active industries have no approved copy with a current QA pass for "
                  "every contacted role; approve rows on the Copy tab (status approved, approved_by)")
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
    return Check(FAIL, "live_sending", "no: set live_sending = yes on the General tab once Harry signs off (SPEC 14 phase 2)")


def check_approvers(ctx: Context) -> Check:
    ids = ctx.settings.general.approver_slack_ids
    if ids:
        return Check(PASS, "Approvers", f"approver_slack_ids: {', '.join(ids)}")
    return Check(FAIL, "Approvers", "approver_slack_ids is blank: no reply can be approved (SPEC 11)")


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


def check_campaigns(ctx: Context) -> Check:
    from us_outbound.clients.instantly import CAMPAIGN_STATUS
    from us_outbound.registry import mailboxes as reg

    try:
        out = reg.ensure_campaigns(ctx, fix=False, create=False)
        status_of = {c.get("name"): CAMPAIGN_STATUS.get(c.get("status"), c.get("status"))
                     for c in ctx.clients.instantly.list_campaigns()}
    except (ConfigError, ApiError, LookupError) as exc:
        return Check(FAIL, "Campaigns", f"cannot read Instantly: {redact(str(exc))[:200]}")
    detail = [f"{name}: {', '.join(f'{k} {v[1]!r}, expected {v[0]!r}' for k, v in sorted(d.items()))}"
              for name, d in out["drift"].items()]
    detail += [f"{name}: missing" for name in out["pending"]]
    detail += [f"{name}: matches ({status_of.get(name, '?')})" for name in out["ok"]]
    detail += [f"{name}: not an owner in the registry (left alone)" for name in out.get("unknown") or ()]
    if out["drift"] or out["pending"]:
        fix = "`us-outbound campaigns ensure --fix --live`" if out["drift"] else "`us-outbound campaigns ensure --live`"
        what = []
        if out["drift"]:
            what.append(f"{len(out['drift'])} drifted")
        if out["pending"]:
            what.append(f"{len(out['pending'])} missing")
        return Check(FAIL, "Campaigns", f"{' and '.join(what)}; run {fix}, then `us-outbound start --live` to send", detail)
    if out.get("unknown"):
        return Check(WARN, "Campaigns", f"all {len(out['ok'])} match; unknown US Outbound campaigns exist", detail)
    return Check(PASS, "Campaigns", f"all {len(out['ok'])} exist and match; `us-outbound start --live` activates them", detail)


def check_apollo(ctx: Context) -> Check:
    b = budget.monthly(ctx.store, ctx.settings, "apollo", ctx.now)
    if b.budget <= 0:
        return Check(WARN, "Apollo budget", "apollo_monthly_credits is 0: no email is looked up this month")
    if b.spent:
        return Check(WARN, "Apollo budget", f"{b.describe()}. Enrollment continues from accounts already ready")
    return Check(PASS, "Apollo budget", f"{budget._n(b.remaining)} of {budget._n(b.budget)} credits left this month, "
                                        f"up to {budget._n(b.left_today)} today ({b.weekdays_left} weekdays left)")


def check_queue(ctx: Context) -> Check:
    day = ctx.now_et().date()
    _, pulled = enrol.hand_check(ctx, day)
    ready, skipped = enrol.candidates(ctx, pulled)
    lim = limits.today(ctx, day, ready_accounts=len(ready))
    t = lim.terms
    detail = [lim.explanation, *lim.detail]
    if skipped:
        detail.append("Not ready: " + ", ".join(f"{k} {v}" for k, v in skipped.most_common(8)))
    want = min(t["weekly_target"], t["sending_capacity"])
    if not ready:
        return Check(FAIL, "Queue", "no verified account has a sendable contact", detail)
    if len(ready) < want:
        return Check(WARN, "Queue", f"{len(ready)} ready (verified with a sendable contact), fewer than today's "
                                    f"{want} (weekly target {t['weekly_target']}, sending capacity {t['sending_capacity']})", detail)
    days = len(ready) / want if want else 0
    return Check(PASS, "Queue", f"{len(ready)} ready (verified with a sendable contact): about {days:.0f} send days "
                                f"at today's number, {lim.number}", detail)


def check_hand_check(ctx: Context) -> Check:
    week = enrol.iso_week(ctx.now_et().date())
    why, pulled = enrol.hand_check(ctx, ctx.now_et().date())
    if why is None:
        return Check(PASS, "Hand-check", f"{week} approved" + (f"; {len(pulled)} accounts pulled" if pulled else ""))
    return Check(FAIL, "Hand-check", f"{why}: `us-outbound handcheck show --live` (prints and records the sample), "
                                     "then `us-outbound handcheck approve --live`")


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


def check_clay_verification(ctx: Context) -> Check | None:
    value = getattr(ctx.settings.general, "clay_verification", _MISSING)
    if value is _MISSING:
        return None  # not a setting in this build
    off = value is False or str(value).strip().lower() in ("", "no", "off", "false", "skip", "none")
    if off:
        return Check(WARN, "clay_verification", f"{value!r}: accounts are not verified in Clay before they are emailed (SPEC 2)")
    return Check(PASS, "clay_verification", f"{value!r}")


def check_optout(ctx: Context) -> Check:
    if ctx.settings.general.optout_tested:
        return Check(PASS, "Opt-out tested", "optout_tested = yes: the seed-inbox test of {{unsubscribe}} is done")
    return Check(FAIL, "Opt-out tested", "seed-inbox test of {{unsubscribe}} not done: send a test from a paused campaign "
                                         "to a seed inbox in html and text, click the link, check the lead shows as "
                                         "unsubscribed, then set optout_tested = yes on the General tab")


CHECKS: tuple[tuple[str, Callable[[Context], Check | None]], ...] = (
    ("Settings", check_settings),
    ("Copy", check_copy),
    ("Mailboxes", check_mailboxes),
    ("live_sending", check_live_sending),
    ("Approvers", check_approvers),
    ("Slack", check_slack),
    ("Campaigns", check_campaigns),
    ("Apollo budget", check_apollo),
    ("Queue", check_queue),
    ("Hand-check", check_hand_check),
    ("Enrollment", check_enrolment),
    ("Jobs", check_jobs),
    ("clay_verification", check_clay_verification),
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
    return [Check(FAIL, "Settings", "unusable: fix the sheet, then `us-outbound settings sync`", detail)]


def render(checks: list[Check], now: datetime) -> str:
    lines = [f"Go-live check, {now.astimezone(UK):%a %d %b %Y %H:%M} UK (read-only: nothing was changed)"]
    for c in checks:
        lines.append(f"{c.status:<5} {c.name}: {c.reason}")
        shown = c.detail[:DETAIL_LIMIT]
        lines += [f"        {d}" for d in shown]
        if len(c.detail) > len(shown):
            lines.append(f"        … and {len(c.detail) - len(shown)} more")
    n = {s: sum(c.status == s for c in checks) for s in (FAIL, WARN, PASS)}
    verdict = "NO-GO" if n[FAIL] else "GO"
    lines.append(f"{verdict}: {n[FAIL]} FAIL, {n[WARN]} WARN, {n[PASS]} PASS."
                 + (" Fix every FAIL, then run `us-outbound golive` again." if n[FAIL] else ""))
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
