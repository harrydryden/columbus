"""The mailbox registry and the sender campaigns (SPEC 5 Mailboxes, 9, 13 Health).

The registry is the Mailboxes tab. Each owner has one Instantly campaign,
"US Outbound – {owner}", whose sending list is the owner's Active addresses and whose
daily limit is the sum of their caps. Campaigns are created paused (Instantly's Draft)
and only `start` ever activates one.

Registry commands (SPEC 9):
  mailbox_add     adds the row (added_on today, Warming, or Active at once if Instantly
                  already shows it warm), turns warmup on, and puts it on its owner's
                  sending list once it is Active (creating the owner's campaign then);
  mailbox_pause   status Paused and off the sending list; warmup and reply reading go on;
                  a campaign left with no Active address is paused (its accounts wait);
  mailbox_retire  pauses it and sets retire_after = today + 30 days; mailbox_health
                  marks it Retired (out of the registry) once that date has passed and it
                  has not sent for 30 days, so its leads have finished (SPEC 9, 13).

mailbox_health (daily, 07:00 UK) reads warmup status for the registry addresses only,
promotes Warming mailboxes that are warm or 21 days old, turns warmup back on where it
is off (SPEC 13: warmup always on), retires mailboxes whose wait is over, updates the
sending lists, checks every campaign for drift (SPEC 13 Health) and posts a summary.
Sheet and Instantly writes happen only when live; dry-run reports what it would do.

"Warm" (PHASE0-CONFIRM: what Instantly reports for our four mailboxes): warmup is on,
the account is active, and either Instantly's warmup score or health score is at least
WARM_SCORE, or warmup (or the registry row) is at least WARM_DAYS old.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.clients.instantly import CAMPAIGN_SETTINGS, STEP_DAYS, instantly_schedule, sequences, settings_drift
from us_outbound.context import UK, Context, boundaries_for
from us_outbound.logs import log
from us_outbound.settings.model import Mailbox, Settings

TAB = "Mailboxes"
ACTIVE, WARMING, PAUSED, RETIRED = "Active", "Warming", "Paused", "Retired"
WARM_DAYS = 21  # SPEC 9: promoted to Active after 21 days
WARM_SCORE = 90  # PHASE0-CONFIRM: Instantly's warmup/health score (0-100) that counts as warm
RETIRE_WAIT_DAYS = 30  # SPEC 9, 13: removed 30 days after retire, never within 30 days of last use
DEFAULT_CAP = 30  # SPEC 13
MAX_CAP = 30
SPILL_DOMAIN = "spill.chat"  # SPEC 1.2: spill.chat never sends cold email
SIGNATURE = "{owner}\nSpill\nspill.chat/us"  # SPEC 5 default signature
# Each step's subject and body are the lead's rendered custom variables (SPEC 9).
CAMPAIGN_STEPS: tuple[dict[str, str], ...] = tuple(
    {"subject": f"{{{{s{i}_subject}}}}", "body": f"{{{{s{i}_body}}}}"} for i in range(1, len(STEP_DAYS) + 1)
)
MAILBOX_COLUMNS = (
    "address", "instantly_account_id", "domain", "provider", "owner_name", "owner_role", "signature",
    "status", "daily_cap", "added_on", "retire_after",
)
_EMAIL = re.compile(r"[a-z0-9._%+'-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
_TAG = re.compile(r"<[^>]+>")


class MailboxError(Exception):
    """A registry command that cannot go ahead; nothing was changed."""


def campaign_name(owner: str) -> str:
    return f"{US_CAMPAIGN_PREFIX}{owner}"


def _date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UK).date()
    if isinstance(v, date):
        return v
    try:
        return _date(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except ValueError:
        return None


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def warmup_to_status(warmup: Mapping[str, Any], *, today: date | None = None, added_on: date | None = None) -> str:
    """ "Active" if Instantly shows the mailbox warm, otherwise "Warming" (see the module docstring)."""
    if not warmup.get("found", True) or not warmup.get("warmup_enabled"):
        return WARMING
    if warmup.get("status") not in (None, "active"):
        return WARMING
    for score in (warmup.get("warmup_score"), warmup.get("health_score")):
        if isinstance(score, (int, float)) and not isinstance(score, bool) and score >= WARM_SCORE:
            return ACTIVE
    if today is not None:
        for start in (_date(warmup.get("warmup_started_at")), added_on):
            if start is not None and (today - start).days >= WARM_DAYS:
                return ACTIVE
    return WARMING


# -- sending lists and campaigns -----------------------------------------------------


def sending_list(settings: Settings, owner: str) -> list[str]:
    return [m.address.lower() for m in settings.mailboxes_for(owner, ACTIVE)]


def daily_limit(settings: Settings, owner: str) -> int:
    return sum(m.daily_cap for m in settings.mailboxes_for(owner, ACTIVE))


def _plain(text: Any) -> str:
    return _TAG.sub("", str(text or "")).strip()


def campaign_drift(campaign: Mapping[str, Any], settings: Settings, owner: str) -> dict[str, list]:
    """{setting: [expected, actual]} where the campaign no longer matches SPEC 9, the sheet and the registry."""
    drift = settings_drift(
        campaign,
        accounts=sending_list(settings, owner),
        daily_limit=daily_limit(settings, owner),
        window=settings.general.send_window,
    )
    out = {k: [want, got] for k, (want, got) in drift.items()}
    steps = ((campaign.get("sequences") or [{}])[0] or {}).get("steps") or []
    for i, want in enumerate(CAMPAIGN_STEPS):
        variant = ((steps[i].get("variants") or [{}])[0] or {}) if i < len(steps) else {}
        # PHASE0-CONFIRM: Instantly may hand bodies back as HTML; tags are ignored here.
        got = {"subject": _plain(variant.get("subject")), "body": _plain(variant.get("body"))}
        if got != want:
            out[f"steps.{i + 1}"] = [dict(want), got]
    return out


def _sync_campaign(ctx: Context, settings: Settings, owner: str) -> str:
    """Make the owner's campaign carry their Active addresses; returns what was done."""
    inst = ctx.clients.instantly
    name = campaign_name(owner)
    accounts, limit = sending_list(settings, owner), daily_limit(settings, owner)
    campaign = inst.get_campaign(name)
    if campaign is None:
        if not accounts:
            return "pending: no Active mailbox yet"
        inst.create_campaign(
            name,
            accounts=accounts,
            daily_limit=limit,
            schedule=settings.general.send_window,
            steps=CAMPAIGN_STEPS,
            options=dict(CAMPAIGN_SETTINGS),
        )
        return "created (paused)"
    if not accounts:
        inst.pause_campaign(name)
        return "paused: no Active mailbox left"
    have = sorted(str(a).strip().lower() for a in campaign.get("email_list") or [])
    if have == sorted(accounts) and campaign.get("daily_limit") == limit:
        return "unchanged"
    inst.update_campaign(name, {"daily_limit": limit}, accounts=accounts)
    return "sending list updated"


def _fix_fields(drift: Mapping[str, Any], settings: Settings, owner: str) -> dict[str, Any]:
    fields: dict[str, Any] = {k: CAMPAIGN_SETTINGS[k] for k in drift if k in CAMPAIGN_SETTINGS}
    if any(k.startswith("schedule.") for k in drift):
        fields["campaign_schedule"] = instantly_schedule(settings.general.send_window)
    if any(k.startswith("steps") for k in drift):
        fields["sequences"] = sequences(CAMPAIGN_STEPS)
    if "daily_limit" in drift:
        fields["daily_limit"] = daily_limit(settings, owner)
    return fields


def ensure_campaigns(
    ctx: Context, *, fix: bool = False, create: bool = True, settings: Settings | None = None
) -> dict:
    """One paused campaign per registry owner; reports drift, and with fix=True puts it right.

    create=False only checks (missing campaigns are reported as pending). Campaigns named
    "US Outbound – " that match no registry owner are listed, never touched.
    """
    settings = settings or ctx.settings
    inst = ctx.clients.instantly
    found = {c["name"]: c for c in inst.list_campaigns()}
    owners = settings.owners()
    out: dict[str, Any] = {"dry_run": ctx.dry_run, "created": [], "pending": [], "ok": [], "drift": {}, "fixed": []}
    for owner in owners:
        name = campaign_name(owner)
        accounts = sending_list(settings, owner)
        if name not in found:
            if accounts and create:
                out["created"].append(name)
                _sync_campaign(ctx, settings, owner)
            else:
                out["pending"].append(name)
            continue
        campaign = inst.get_campaign(name) or found[name]
        drift = campaign_drift(campaign, settings, owner)
        if not accounts and campaign.get("status") in (0, 2):
            # Instantly cannot hold an empty sending list: a waiting (paused) campaign keeps its old one.
            drift.pop("email_list", None)
            drift.pop("daily_limit", None)
        if not drift:
            out["ok"].append(name)
            continue
        out["drift"][name] = drift
        if fix:
            if "email_list" in drift and not accounts:
                inst.pause_campaign(name)
            else:
                inst.update_campaign(
                    name, _fix_fields(drift, settings, owner), accounts=accounts if "email_list" in drift else None
                )
            out["fixed"].append(name)
    out["unknown"] = sorted(n for n in found if n[len(US_CAMPAIGN_PREFIX):] not in owners)
    log("ensure_campaigns", **{k: v for k, v in out.items() if k != "drift"}, drift=sorted(out["drift"]))
    return out


# -- the registry commands (SPEC 9) ---------------------------------------------------


def _sheet_id(ctx: Context) -> str:
    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise MailboxError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    return sheet_id


def _find(settings: Settings, address: str) -> Mailbox | None:
    a = address.strip().lower()
    return next((m for m in settings.mailboxes if m.address.lower() == a), None)


def _with(settings: Settings, mailbox: Mailbox) -> Settings:
    """settings with this mailbox's row replaced (or added)."""
    rows = [m for m in settings.mailboxes if m.address.lower() != mailbox.address.lower()]
    return dataclasses.replace(settings, mailboxes=(*rows, mailbox))


def _row(m: Mailbox) -> dict[str, str]:
    return {
        "address": m.address, "instantly_account_id": m.instantly_account_id, "domain": m.domain,
        "provider": m.provider, "owner_name": m.owner_name, "owner_role": m.owner_role, "signature": m.signature,
        "status": m.status, "daily_cap": str(m.daily_cap),
        "added_on": m.added_on.isoformat() if m.added_on else "",
        "retire_after": m.retire_after.isoformat() if m.retire_after else "",
    }


def mailbox_add(
    ctx: Context,
    address: str,
    *,
    owner: str,
    domain: str | None = None,
    daily_cap: int = DEFAULT_CAP,
    provider: str = "",
    owner_role: str = "",
) -> dict:
    """SPEC 9 mailbox add. The mailbox must already be connected in Instantly."""
    addr = address.strip().lower()
    m = _EMAIL.fullmatch(addr)
    if not m:
        raise MailboxError(f"{address!r} is not an email address")
    own_domain = m.group(1)
    if own_domain == SPILL_DOMAIN:
        raise MailboxError(f"{SPILL_DOMAIN} never sends cold email (SPEC 1.2)")
    if domain and domain.strip().lower() != own_domain:
        raise MailboxError(f"--domain must be the address's domain, {own_domain}")
    owner = " ".join((owner or "").split())
    if not owner:
        raise MailboxError("--owner is required: the real person named on the address (SPEC 5)")
    if not 0 <= daily_cap <= MAX_CAP:
        raise MailboxError(f"daily cap must be between 0 and {MAX_CAP} (SPEC 13)")
    settings = ctx.settings
    old = _find(settings, addr)
    if old is not None:
        raise MailboxError(f"{addr} is already on the Mailboxes tab (status {old.status})")
    sheet_id = _sheet_id(ctx)
    today = ctx.today_uk()
    new = Mailbox(
        address=addr, domain=own_domain, owner_name=owner, status=WARMING, daily_cap=daily_cap,
        provider=provider, owner_role=owner_role, signature=SIGNATURE.format(owner=owner), added_on=today,
    )
    # The address joins the registry here, so the guard lets its account be read and warmed.
    settings = _with(settings, new)
    ctx.guard.configure(bounds=boundaries_for(settings, sheet_id))
    inst = ctx.clients.instantly
    warmup = inst.warmup_status([addr]).get(addr, {})
    if not warmup.get("found"):
        raise MailboxError(f"{addr} is not connected in Instantly; connect it there first, then run mailbox add again")
    warmup_turned_on = False
    if not warmup.get("warmup_enabled"):
        inst.enable_warmup([addr])
        warmup_turned_on = True
    status = warmup_to_status(warmup, today=today)
    new = dataclasses.replace(new, status=status)
    settings = _with(settings, new)
    ctx.clients.sheets.append_rows(sheet_id, TAB, [_row(new)])
    campaign = _sync_campaign(ctx, settings, owner) if status == ACTIVE else "waits until the mailbox is Active"
    summary = {
        "dry_run": ctx.dry_run, "address": addr, "owner": owner, "status": status,
        "warmup_turned_on": warmup_turned_on, "campaign": campaign_name(owner), "campaign_action": campaign,
    }
    log("mailbox_add", **summary)
    return summary


def mailbox_pause(ctx: Context, address: str, *, settings: Settings | None = None) -> dict:
    """SPEC 9 mailbox pause: off the sending list, status Paused. Warmup and reply reading go on."""
    settings = settings or ctx.settings
    m = _find(settings, address)
    if m is None:
        raise MailboxError(f"{address} is not on the Mailboxes tab")
    if m.status == RETIRED:
        raise MailboxError(f"{m.address} is retired")
    sheet_id = _sheet_id(ctx)
    if m.status != PAUSED:
        ctx.clients.sheets.update_cell(sheet_id, TAB, {"address": m.address}, "status", PAUSED)
    paused = dataclasses.replace(m, status=PAUSED)
    campaign = _sync_campaign(ctx, _with(settings, paused), m.owner_name)
    summary = {
        "dry_run": ctx.dry_run, "address": m.address, "status": PAUSED, "was": m.status,
        "campaign": campaign_name(m.owner_name), "campaign_action": campaign,
    }
    log("mailbox_pause", **summary)
    return summary


def mailbox_retire(ctx: Context, address: str) -> dict:
    """SPEC 9 mailbox retire: pause now; mailbox_health retires it after RETIRE_WAIT_DAYS."""
    m = _find(ctx.settings, address)
    if m is None:
        raise MailboxError(f"{address} is not on the Mailboxes tab")
    if m.status == RETIRED:
        raise MailboxError(f"{m.address} is already retired")
    summary = mailbox_pause(ctx, m.address)
    retire_after = m.retire_after or (ctx.today_uk() + timedelta(days=RETIRE_WAIT_DAYS))
    if m.retire_after is None:
        ctx.clients.sheets.update_cell(_sheet_id(ctx), TAB, {"address": m.address}, "retire_after", retire_after.isoformat())
    summary = {**summary, "retire_after": retire_after.isoformat()}
    log("mailbox_retire", **summary)
    return summary


# -- mailbox_health (daily job) ----------------------------------------------------------


def last_use(ctx: Context, address: str) -> datetime | None:
    """When the mailbox last sent: its latest 'sent' event, or its contacts' last step."""
    a = address.lower()
    times = [_ts(e.get("occurred_at")) for e in ctx.store.select("events", {"mailbox": a, "type": "sent"})]
    times += [_ts(c.get("last_step_at")) for c in ctx.store.select("contacts", {"mailbox": a})]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def vitals_and_placement(ctx: Context, addresses: Sequence[str]) -> dict:
    """Phase 2 fills this: Instantly vitals, seed-inbox placement and sends against cap (SPEC 9, 13)."""
    return {}


def _summary_text(ctx: Context, rows: list[dict], out: Mapping[str, Any]) -> str:
    today = ctx.today_uk()
    lines = [f"Mailbox health, {today:%a %d %b}" + (" (dry-run: nothing changed)" if ctx.dry_run else "")]
    for r in rows:
        score = r.get("warmup_score")
        warm = "warmup on" if r.get("warmup_enabled") else "warmup OFF"
        lines.append(
            f"• {r['address']} ({r['owner']}): {r['status']}, {warm}"
            + (f", score {score}" if score is not None else "")
            + ("" if r.get("found") else ", not found in Instantly")
        )
    for key, label in (
        ("promoted", "Promoted to Active"),
        ("retired", "Retired"),
        ("warmup_turned_on", "Warmup turned back on"),
        ("waiting_to_retire", "Waiting to retire (sent in the last 30 days)"),
        ("not_found", "Not found in Instantly"),
    ):
        if out.get(key):
            lines.append(f"{label}: {', '.join(out[key])}")
    campaigns = out.get("campaigns") or {}
    if campaigns.get("drift"):
        for name, drift in campaigns["drift"].items():
            lines.append(f"Campaign drift, {name}: {', '.join(sorted(drift))}. Fix with `us-outbound campaigns ensure --fix --live`.")
    if campaigns.get("pending"):
        lines.append(f"Campaigns waiting for an Active mailbox: {', '.join(campaigns['pending'])}")
    if campaigns.get("unknown"):
        lines.append(f"Unknown US Outbound campaigns (not touched): {', '.join(campaigns['unknown'])}")
    return "\n".join(lines)


def mailbox_health(ctx: Context) -> dict:
    """The mailbox_health job (phase 0 part): warmup status, promotions, retirements, campaign drift."""
    settings = ctx.settings
    registry = [m for m in settings.mailboxes if m.status != RETIRED]
    if not registry:
        return {"mailboxes": 0}
    inst = ctx.clients.instantly
    today = ctx.today_uk()
    warmups = inst.warmup_status([m.address for m in registry])
    out: dict[str, Any] = {
        "dry_run": ctx.dry_run, "mailboxes": len(registry), "promoted": [], "retired": [],
        "warmup_turned_on": [], "waiting_to_retire": [], "not_found": [],
    }
    rows: list[dict] = []
    changes: list[tuple[Mailbox, str]] = []
    for m in registry:
        w = warmups.get(m.address.lower(), {})
        rows.append({
            "address": m.address, "owner": m.owner_name, "status": m.status, "found": bool(w.get("found")),
            "warmup_enabled": bool(w.get("warmup_enabled")), "warmup_score": w.get("warmup_score"),
            "account_status": w.get("status"),
        })
        if not w.get("found"):
            out["not_found"].append(m.address)
            continue
        if not w.get("warmup_enabled"):
            out["warmup_turned_on"].append(m.address)
        if m.status == WARMING and warmup_to_status(w, today=today, added_on=m.added_on) == ACTIVE:
            changes.append((m, ACTIVE))
            out["promoted"].append(m.address)
        elif m.status == PAUSED and m.retire_after is not None and m.retire_after <= today:
            last = last_use(ctx, m.address)
            if last is None or ctx.now - last >= timedelta(days=RETIRE_WAIT_DAYS):
                changes.append((m, RETIRED))
                out["retired"].append(m.address)
            else:
                out["waiting_to_retire"].append(m.address)

    if out["warmup_turned_on"]:
        inst.enable_warmup(out["warmup_turned_on"])  # SPEC 13: warmup always on
    sheet_id = ctx.guard.bounds.settings_sheet_id
    new_settings = settings
    for m, status in changes:
        if sheet_id:
            ctx.clients.sheets.update_cell(sheet_id, TAB, {"address": m.address}, "status", status)
        new_settings = _with(new_settings, dataclasses.replace(m, status=status))
    out["campaign_actions"] = {
        campaign_name(owner): _sync_campaign(ctx, new_settings, owner)
        for owner in dict.fromkeys(m.owner_name for m, _ in changes)
        if owner in new_settings.owners()
    }
    out["campaigns"] = ensure_campaigns(ctx, settings=new_settings)
    out["vitals"] = vitals_and_placement(ctx, [m.address for m in registry])
    ctx.clients.slack.post(settings.general.alert_channel, _summary_text(ctx, rows, out))
    log("mailbox_health", **{k: v for k, v in out.items() if k != "campaigns"})
    return out
