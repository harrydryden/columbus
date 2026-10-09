"""The mailbox registry and the sender campaigns (SPEC 5 Mailboxes, 9, 13 Health).

The registry is the Mailboxes tab. Each owner has one Instantly campaign,
"US Outbound – {owner}", whose sending list is the owner's Active addresses and whose
daily limit is the sum of their caps. Campaigns are created paused (Instantly's Draft).
`start` activates them; after go-live (a live `start` that finished ok, with no `stop`
since: went_live) mailbox_health also activates one that nothing else would: a campaign
created for an owner whose mailbox has just become Active, a draft whose owner now has an
Active mailbox, and one a kill rule's pause left with no Active mailbox (_sync_campaign
pauses it) once that mailbox is Active again. A campaign holds only approved leads, so
activating an empty one sends nothing by itself. Before go-live, or if Instantly refuses,
the summary says "<owner>'s campaign is ready: run `us-outbound start --live` to start it".
Over a blackout it starts nothing, and a campaign the blackout paused is the blackout job's to
start again (registry/blackout.py; Harry, 7 Oct 2026).

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
sending lists, checks every campaign for drift (SPEC 13 Health), puts right the drift that
only follows the sheet and the ramp (the daily limit and the sending list, AUTO_FIX; any
other drift is reported for `campaigns ensure --fix --live`), starts the campaigns above,
and posts a summary to the alert channel when something changed or is wrong (otherwise
the summary is only logged and kept as the heartbeat). Sheet and Instantly writes happen
only when live; dry-run reports what it would do.

The sender name (Harry, 5 Oct 2026): prospects see each mailbox's From name, which Instantly builds
from the account's first_name and last_name. It is the owner's full name from the Mailboxes tab
("Hannah Spalding": first name the first word, last name the rest), never "Hannah at Spill" or
"Sam from Spill", which reads as marketing to people and to mail filters. mailbox_health reports a
mailbox whose name differs; `mailbox check --fix --live` sets it (fix_names), and nothing else on
the account. PHASE0-CONFIRM: see Instantly.set_sender_name.

"Warm" (what Instantly reports for our mailboxes: read live since 2 Oct 2026, when the first two were promoted):
warmup is on,
the account is active, and either Instantly's warmup score or health score is at least
WARM_SCORE, or warmup (or the registry row) is at least WARM_DAYS old.

Leads in flight (Harry, 7 Oct 2026: "a cohort system in place for contacts that have started not being
interrupted by changes"): a lead's own emails are rendered at enrolment and never rewritten, but the step template
(the unsubscribe line), the step delays and text_only are the campaign's, shared by every lead in it. So drift in
those (IN_FLIGHT_KEYS) is held, not put right, while the campaign has leads in flight (enrol/capacity.in_flight):
`campaigns ensure --fix --in-flight --live` applies it to them too, or it waits until `us-outbound cohorts
in-flight` shows 0. Nothing else is held: the daily limit, sending list and schedule follow the sheet and the ramp,
and the other campaign settings only ever go back to their pinned values, most of them safety (stop on reply, the
unsubscribe header, tracking off). Nor is a step that has lost Instantly's unsubscribe link: putting the opt-out back
is never held. Every change applied other than the daily limit and sending list, and every From name set, is a
config_log row with the leads then in flight (config_version.log_change), which the cohort report lists.

The sending ramp (registry/ramp.py; Harry, 1 Oct 2026): a campaign's daily limit is the sum
of its Active mailboxes' caps today, each the lower of the ramp (10 a day in a mailbox's
first sending week, 20 in its second) and its daily_cap, and mailbox_health sets each
Instantly account's own daily limit to the same number. So as the ramp moves, the daily drift
check finds the campaign's limit behind and mailbox_health sets it (as `campaigns ensure --fix
--live` does). A mailbox a kill rule holds (learn/holds.py) counts as Paused here before the
sheet catches up.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Collection, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound import config_version
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import (
    STEP_DAYS,
    UNSUBSCRIBE_TAG,
    campaign_settings,
    instantly_schedule,
    sequences,
    settings_drift,
    unsubscribe_line,
)
from us_outbound.context import Context, boundaries_for
from us_outbound.enrol import capacity
from us_outbound.learn import holds
from us_outbound.logs import log
from us_outbound.ops.heartbeat import OPERATOR_START, OPERATOR_STOP
from us_outbound.registry import blackout
from us_outbound.registry import ramp as ramps_
from us_outbound.settings.model import Mailbox, Settings
from us_outbound.settings.validate import SPILL_DOMAIN, is_spill_domain
from us_outbound.timeparse import uk_day, utc_strict_or_none

TAB = "Mailboxes"
ACTIVE, WARMING, PAUSED, RETIRED = "Active", "Warming", "Paused", "Retired"
WARM_DAYS = 21  # SPEC 9: promoted to Active after 21 days
WARM_SCORE = 90  # Instantly's warmup/health score (0-100, read live since 2 Oct 2026) that counts as warm
RETIRE_WAIT_DAYS = 30  # SPEC 9, 13: removed 30 days after retire, never within 30 days of last use
DEFAULT_CAP = 30  # SPEC 13
MAX_CAP = 30
SIGNATURE = "{owner}\nSpill\nspill.chat/us"  # SPEC 5 default signature
AUTO_FIX = frozenset({"daily_limit", "email_list"})  # drift mailbox_health puts right itself: it follows the sheet and ramp
# Drift that would change what, or when, every lead already in the campaign gets: held while leads are in flight
# (the module docstring; Harry, 7 Oct 2026).
IN_FLIGHT_KEYS = frozenset({"text_only", "first_email_text_only", "steps.delays", "steps.variants",
                            *(f"steps.{i}" for i in range(1, len(STEP_DAYS) + 1))})
CREATED = "created (paused)"  # _sync_campaign's outcomes
PAUSED_EMPTY = "paused: no Active mailbox left"
DRAFT, PAUSED_CAMPAIGN = 0, 2  # Instantly campaign status (clients/instantly.CAMPAIGN_STATUS)


def campaign_steps(text_only: bool = False) -> tuple[dict[str, str], ...]:
    """Each step: the lead's rendered subject and body (custom variables, SPEC 9), then Instantly's unsubscribe link.

    In HTML the body variable sits in a <div>: Instantly drops text that is outside any tag when it saves a
    step, so a bare "{{s1_body}}<p>…</p>" was saved as the unsubscribe line alone (the first live create,
    2 Oct 2026, read back with `us-outbound campaigns show`).
    """
    def body(i: int) -> str:
        var = f"{{{{s{i}_body}}}}"
        return f"{var}{unsubscribe_line(True)}" if text_only else f"<div>{var}</div>{unsubscribe_line(False)}"

    return tuple({"subject": f"{{{{s{i}_subject}}}}", "body": body(i)} for i in range(1, len(STEP_DAYS) + 1))


_EMAIL = re.compile(r"[a-z0-9._%+'-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r"""href\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


class MailboxError(Exception):
    """A registry command that cannot go ahead; nothing was changed."""


def campaign_name(owner: str) -> str:
    return f"{US_CAMPAIGN_PREFIX}{owner}"


def sender_name(owner: str) -> tuple[str, str]:
    """(first_name, last_name) for Instantly: the owner's full name split at the first space (Harry, 5 Oct 2026)."""
    first, _, last = " ".join(owner.split()).partition(" ")
    return first, last


def _name_text(v: Any) -> str:
    return " ".join(str(v or "").split())


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
        for start in (uk_day(warmup.get("warmup_started_at")), added_on):
            if start is not None and (today - start).days >= WARM_DAYS:
                return ACTIVE
    return WARMING


# -- sending lists and campaigns -----------------------------------------------------


def text_only(settings: Settings) -> bool:
    """Instantly's text_only for the sheet's email_format: html (the default) sends links and bullets."""
    return settings.general.email_format == "text"


def sending_list(settings: Settings, owner: str) -> list[str]:
    return [m.address.lower() for m in settings.mailboxes_for(owner, ACTIVE)]


def daily_limit(settings: Settings, owner: str, caps: Mapping[str, int] | None = None) -> int:
    """The owner's campaign limit: the sum of their Active mailboxes' caps today (caps: the ramp's, by address)."""
    caps = caps or {}
    return sum(caps.get(m.address.lower(), m.daily_cap) for m in settings.mailboxes_for(owner, ACTIVE))


def _caps(ctx: Context, settings: Settings) -> dict[str, int]:
    """Each mailbox's cap today, on the sending ramp (US Eastern date, as the send window)."""
    return ramps_.caps(ctx.store, settings, ctx.now_et().date())


def _plain(text: Any) -> str:
    return _TAG.sub("", str(text or "")).strip()


def _hrefs(text: Any) -> list[str]:
    """The link addresses in a step body: the text alone would not show a changed unsubscribe link."""
    return sorted(_HREF.findall(str(text or "")))


def campaign_drift(
    campaign: Mapping[str, Any], settings: Settings, owner: str, caps: Mapping[str, int] | None = None
) -> dict[str, list]:
    """{setting: [expected, actual]} where the campaign no longer matches SPEC 9, the sheet, the registry and the ramp."""
    drift = settings_drift(
        campaign,
        accounts=sending_list(settings, owner),
        daily_limit=daily_limit(settings, owner, caps),
        window=settings.general.send_window,
        text_only=text_only(settings),
    )
    out = {k: [want, got] for k, (want, got) in drift.items()}
    steps = ((campaign.get("sequences") or [{}])[0] or {}).get("steps") or []
    for i, step in enumerate(campaign_steps(text_only(settings))):
        variant = ((steps[i].get("variants") or [{}])[0] or {}) if i < len(steps) else {}
        # Instantly hands bodies back as HTML (read back 3 Oct 2026); tags are ignored here.
        want = {**{k: _plain(v) for k, v in step.items()}, "links": _hrefs(step["body"])}
        got = {"subject": _plain(variant.get("subject")), "body": _plain(variant.get("body")),
               "links": _hrefs(variant.get("body"))}
        if got != want:
            out[f"steps.{i + 1}"] = [dict(want), got]
    return out


def leads_in_flight(ctx: Context, settings: Settings) -> Counter[str]:
    """campaign name -> its leads with a step still to send (enrol/capacity.in_flight)."""
    return Counter(str(c.get("instantly_campaign")) for c in capacity.in_flight(ctx.store, settings, ctx.now_et().date()))


def _opt_out_lost(drift: Mapping[str, Any]) -> bool:
    """A step in Instantly that no longer carries the unsubscribe link: putting it back is never held."""
    for key, (_, got) in drift.items():
        if key.startswith("steps.") and key[len("steps."):].isdigit() and isinstance(got, Mapping):
            if UNSUBSCRIBE_TAG not in str(got.get("body") or "") and UNSUBSCRIBE_TAG not in (got.get("links") or ()):
                return True
    return False


def held_words(name: str, held: Mapping[str, Any]) -> str:
    """The plain-words line for drift held while leads are in flight (mailbox_health, start, golive, the CLI)."""
    n = int(held.get("leads") or 0)
    return (f"Campaign drift held, {name}: {', '.join(held.get('keys') or ())} would change {n} "
            f"lead{'' if n == 1 else 's'} in flight. Apply to them with `us-outbound campaigns ensure --fix --in-flight "
            "--live`, or wait until `us-outbound cohorts in-flight` shows 0.")


def _sync_campaign(ctx: Context, settings: Settings, owner: str) -> str:
    """Make the owner's campaign carry their Active addresses; returns what was done."""
    inst = ctx.clients.instantly
    name = campaign_name(owner)
    settings = holds.with_holds(ctx.store, settings)
    accounts, limit = sending_list(settings, owner), daily_limit(settings, owner, _caps(ctx, settings))
    campaign = inst.get_campaign(name)
    if campaign is None:
        if not accounts:
            return "pending: no Active mailbox yet"
        inst.create_campaign(
            name,
            accounts=accounts,
            daily_limit=limit,
            schedule=settings.general.send_window,
            steps=campaign_steps(text_only(settings)),
            text_only=text_only(settings),
        )
        return CREATED
    if not accounts:
        inst.pause_campaign(name)
        return PAUSED_EMPTY
    have = sorted(str(a).strip().lower() for a in campaign.get("email_list") or [])
    if have == sorted(accounts) and campaign.get("daily_limit") == limit:
        return "unchanged"
    inst.update_campaign(name, {"daily_limit": limit}, accounts=accounts)
    return "sending list updated"


def _fix_fields(
    drift: Mapping[str, Any], settings: Settings, owner: str, caps: Mapping[str, int] | None = None
) -> dict[str, Any]:
    fixed = campaign_settings(text_only(settings))
    fields: dict[str, Any] = {k: fixed[k] for k in drift if k in fixed}
    if any(k.startswith("schedule.") for k in drift):
        fields["campaign_schedule"] = instantly_schedule(settings.general.send_window)
    if any(k.startswith("steps") for k in drift):
        fields["sequences"] = sequences(campaign_steps(text_only(settings)))
    if "daily_limit" in drift:
        fields["daily_limit"] = daily_limit(settings, owner, caps)
    return fields


def ensure_campaigns(
    ctx: Context, *, fix: bool = False, create: bool = True, settings: Settings | None = None,
    fix_only: Collection[str] | None = None, in_flight: bool = False,
) -> dict:
    """One paused campaign per registry owner; reports drift, and with fix=True puts it right.

    create=False only checks (missing campaigns are reported as pending). Campaigns named
    "US Outbound – " that match no registry owner are listed, never touched. fix_only: with
    fix=True, put right only these drift keys (mailbox_health: AUTO_FIX); the rest is reported.
    drift holds all that was found; fixed_fields says what was put right, per campaign.
    held: per campaign, the IN_FLIGHT_KEYS drift left as it is because leads are in flight, and how many
    ({"keys", "leads"}); in_flight=True (`campaigns ensure --fix --in-flight`) applies it to them too.
    Each change applied other than AUTO_FIX is a config_log row (live only).
    """
    settings = holds.with_holds(ctx.store, settings or ctx.settings)
    caps = _caps(ctx, settings)
    inst = ctx.clients.instantly
    found = {c["name"]: c for c in inst.list_campaigns()}
    owners = settings.owners()
    out: dict[str, Any] = {"dry_run": ctx.dry_run, "created": [], "pending": [], "ok": [], "drift": {}, "fixed": [],
                           "fixed_fields": {}, "held": {}}
    flying: Counter[str] | None = None  # read once, only when a campaign's drift needs it
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
        drift = campaign_drift(campaign, settings, owner, caps)
        if not accounts and campaign.get("status") in (0, 2):
            # Instantly cannot hold an empty sending list: a waiting (paused) campaign keeps its old one.
            drift.pop("email_list", None)
            drift.pop("daily_limit", None)
        if not drift:
            out["ok"].append(name)
            continue
        out["drift"][name] = drift
        hold = set() if in_flight or _opt_out_lost(drift) else set(drift) & IN_FLIGHT_KEYS
        if hold:
            flying = leads_in_flight(ctx, settings) if flying is None else flying
            if flying[name]:
                out["held"][name] = {"keys": sorted(hold), "leads": flying[name]}
            else:
                hold = set()
        todo = {k: v for k, v in drift.items() if (fix_only is None or k in fix_only) and k not in hold} if fix else {}
        if todo:
            shared = set(todo) - AUTO_FIX  # what every lead in it shares: on record when it changes, with who it reached
            if "email_list" in todo and not accounts:
                inst.pause_campaign(name)
            else:
                inst.update_campaign(
                    name, _fix_fields(todo, settings, owner, caps), accounts=accounts if "email_list" in todo else None
                )
                if shared and ctx.live:
                    flying = leads_in_flight(ctx, settings) if flying is None else flying
                    config_version.log_change(ctx, config_version.CAMPAIGN_CHANGE, name, shared,
                                              {k: todo[k] for k in shared}, flying[name])
            out["fixed"].append(name)
            out["fixed_fields"][name] = sorted(todo)
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
    if is_spill_domain(own_domain):
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
    if m.status != PAUSED and not ctx.clients.sheets.update_cell(sheet_id, TAB, {"address": m.address}, "status", PAUSED):
        raise MailboxError(f"{m.address}: no row with that address on the Mailboxes tab")  # before Instantly is touched
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
    if m.retire_after is None and not ctx.clients.sheets.update_cell(
        _sheet_id(ctx), TAB, {"address": m.address}, "retire_after", retire_after.isoformat()
    ):
        raise MailboxError(f"{m.address}: no row with that address on the Mailboxes tab")
    summary = {**summary, "retire_after": retire_after.isoformat()}
    log("mailbox_retire", **summary)
    return summary


# -- mailbox_health (daily job) ----------------------------------------------------------


def last_use(ctx: Context, address: str) -> datetime | None:
    """When the mailbox last sent: its latest campaign send or reply sent from the desk, or its contacts' last step."""
    a = address.lower()
    sends = ctx.store.select("events", {"mailbox": a, "type": ["sent", "reply_sent"]})
    times = [utc_strict_or_none(e.get("occurred_at")) for e in sends]
    times += [utc_strict_or_none(c.get("last_step_at")) for c in ctx.store.select("contacts", {"mailbox": a})]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _lower_limit(row: Mapping[str, Any]) -> bool:
    try:
        cap = row.get("cap_today", row.get("daily_cap"))  # the ramp's cap when it is lower than the sheet's
        return row.get("instantly_daily_limit") is not None and int(row["instantly_daily_limit"]) < int(cap or 0)
    except (TypeError, ValueError):
        return False


# -- starting what nothing else would start (after go-live) ---------------------------------------


def went_live(store: Any) -> datetime | None:
    """When sending went live and still is: the latest live `start` that finished ok, if no `stop` came after it.

    The same rule as ops/heartbeat.enrolment_paused: a stop counts in any mode and whatever its
    outcome (the safe direction); a start only when it ran live and finished ok.
    """
    rows = store.select("heartbeats", {"job": [OPERATOR_STOP, OPERATOR_START]})
    starts = [t for r in rows if r.get("job") == OPERATOR_START and r.get("status") == "ok" and r.get("dry_run") is False
              and (t := utc_strict_or_none(r.get("started_at"))) is not None]
    if not starts:
        return None
    last = max(starts)
    stops = [t for r in rows
             if r.get("job") == OPERATOR_STOP and (t := utc_strict_or_none(r.get("started_at"))) is not None]
    return last if not stops or last > max(stops) else None


def _kill_rule_pauses(store: Any, since: datetime) -> dict[str, list[dict]]:
    """campaign name -> the kill-rule items since go-live whose pause left it with no Active mailbox, not yet restarted.

    learn/kill_rules.py records those campaigns on the item (payload campaigns_paused).
    """
    out: dict[str, list[dict]] = {}
    for r in store.select("hitl_items", {"kind": holds.KIND}):
        p = r.get("payload") if isinstance(r.get("payload"), Mapping) else {}
        created = utc_strict_or_none(r.get("created_at"))
        if p.get("campaign_restarted") or created is None or created <= since:
            continue
        for name in p.get("campaigns_paused") or ():
            out.setdefault(str(name), []).append(r)
    return out


def start_waiting(ctx: Context, settings: Settings, created: Collection[str],
                  drifted: Collection[str] = ()) -> dict:
    """Start the campaigns nothing else would, once sending has gone live; before that, say a new one is ready.

    For each owner with an Active mailbox (kill-rule holds count as Paused): after go-live, a campaign
    created this run, a draft, or one a kill rule's pause left with no Active mailbox is activated
    (it holds only approved leads, so this sends nothing by itself), unless it has drift left that
    was not put right (drifted: `start` would refuse it too); a campaign paused for another reason is
    reported. Before go-live a campaign created this run is reported as ready.
    The blackout dates (registry/blackout.py; Harry, 7 Oct 2026): a campaign the blackout job paused is reported
    under blackout ("paused for the blackout until …"), never as a pause to look into, and is left to that job;
    and while a blackout holds nothing is started: the first morning check after it starts what waits.
    """
    live_since = went_live(ctx.store)
    settings = holds.with_holds(ctx.store, settings)
    inst = ctx.clients.instantly
    found = {c["name"]: c for c in inst.list_campaigns()}
    kill_paused = _kill_rule_pauses(ctx.store, live_since) if live_since else {}
    held, by_blackout = blackout.hold(settings, ctx.now), blackout.words(ctx.store, settings, ctx.now)
    out: dict[str, Any] = {"went_live": live_since.isoformat() if live_since else None, "started": [],
                           "would_start": [], "ready": [], "paused": [], "errors": [], "blackout": {}}
    for owner in settings.owners():
        name = campaign_name(owner)
        campaign = found.get(name) or ({"status": DRAFT} if name in created else None)  # dry-run: not created
        if campaign is None or not sending_list(settings, owner):
            continue
        status = campaign.get("status")
        if live_since is None:
            if name in created:
                out["ready"].append(owner)
            continue
        if status == PAUSED_CAMPAIGN and name in by_blackout:  # the blackout job starts it again after
            out["blackout"][owner] = f"{by_blackout[name]}; the blackout job starts it again then"
            continue
        if not (name in created or status == DRAFT or (status == PAUSED_CAMPAIGN and name in kill_paused)):
            if status == PAUSED_CAMPAIGN:
                out["paused"].append(owner)
            continue
        if name in drifted:  # the summary's drift line says what to fix first
            out["ready"].append(owner)
            continue
        if held is not None:  # nothing starts over a blackout: the first morning check after it does
            out["blackout"][owner] = (f"not started over the blackout; the first mailbox check after it starts it "
                                      f"(sends resume {held.until:%a} {held.until.day} {held.until:%b})"
                                      if held.until else "not started over the blackout")
            continue
        if ctx.dry_run:
            out["would_start"].append(name)
            continue
        try:
            inst.activate_campaign(name)
        except (ApiError, LookupError) as exc:
            out["errors"].append(f"{name}: {str(exc)[:160]}")
            out["ready"].append(owner)
            continue
        out["started"].append(name)
        for item in kill_paused.get(name, ()):  # each pause restarts its campaign once
            p = dict(item.get("payload") or {})
            ctx.store.update("hitl_items", {"item_id": item["item_id"]},
                             {"payload": {**p, "campaign_restarted": ctx.now.isoformat()}})
    if any(out[k] for k in ("started", "would_start", "ready", "paused", "errors")):
        log("campaigns_started", **out)
    return out


def _noteworthy(out: Mapping[str, Any]) -> list[str]:
    """Why the summary is worth a Slack post: what changed or is wrong today. Empty: only log it."""
    campaigns = out.get("campaigns") or {}
    starting = out.get("campaign_start") or {}
    why = [k for k in ("promoted", "retired", "warmup_turned_on", "limit_set", "name_drift", "not_found", "no_sheet_row")
           if out.get(k)]
    if any(st.get("code") is not None for st in (out.get("campaign_status") or {}).values()):
        why.append("held_back")
    if campaigns.get("drift"):
        why.append("drift")
    if campaigns.get("created") or any(v != "unchanged" for v in (out.get("campaign_actions") or {}).values()):
        why.append("campaigns_changed")
    if any(starting.get(k) for k in ("started", "would_start", "ready", "paused", "errors")):
        why.append("campaign_start")
    return why


def _fixed_words(name: str, fields: list[str], drift: Mapping[str, Any]) -> str:
    parts = []
    if "daily_limit" in fields:
        want, got = drift.get("daily_limit") or [None, None]
        parts.append(f"daily limit {got} → {want} (the ramp)")
    if "email_list" in fields:
        want, _ = drift.get("email_list") or [[], None]
        parts.append(f"sending list → {', '.join(want or []) or 'none'} (the Active mailboxes)")
    return f"{name}: " + "; ".join(parts)


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
            + (f", Instantly limits it to {r['instantly_daily_limit']} a day (sheet cap {r['daily_cap']}; the lower is used)"
               if _lower_limit(r) else "")
            + (f", {r['ramp']}" if r.get("ramp") else "")
        )
    for address, change in (out.get("limit_set") or {}).items():
        verb = "Would set" if ctx.dry_run else "Set"
        lines.append(f"{verb} {address}'s Instantly daily limit from {change['from']} to {change['to']} "
                     "(the Mailboxes tab's daily_cap, or the ramp's when lower)")
    for address, change in (out.get("name_drift") or {}).items():
        have, want = (" / ".join(f'"{x}"' for x in change[k]) for k in ("from", "to"))
        if address in (out.get("names_set") or ()):
            lines.append(f"{'Would set' if ctx.dry_run else 'Set'} {address}'s sender name (first / last) from {have} "
                         f"to {want}, its owner's full name")
        else:
            lines.append(f"Sender name of {address} (first / last) is {have}, not its owner's full name {want}. "
                         "Fix with `us-outbound mailbox check --fix --live`.")
    for owner, st in (out.get("campaign_status") or {}).items():
        if st.get("code") is not None:
            lines.append(f"Instantly says {campaign_name(owner)} is held back: {st['meaning']}"
                         + (". The sender is at capacity; see `us-outbound status` for whether accounts are waiting." if st.get("at_limit") else "."))
    for key, label in (
        ("promoted", "Promoted to Active"),
        ("retired", "Retired"),
        ("warmup_turned_on", "Warmup turned back on"),
        ("waiting_to_retire", "Waiting to retire (sent in the last 30 days)"),
        ("not_found", "Not found in Instantly"),
        ("no_sheet_row", "Not changed: no row with that address on the Mailboxes tab"),
    ):
        if out.get(key):
            lines.append(f"{label}: {', '.join(out[key])}")
    campaigns = out.get("campaigns") or {}
    made = list(dict.fromkeys([*(n for n, a in (out.get("campaign_actions") or {}).items() if a == CREATED),
                               *(campaigns.get("created") or [])]))
    if made:
        lines.append(f"{'Would create' if ctx.dry_run else 'Created'} (paused until started): {', '.join(made)}")
    fixed = campaigns.get("fixed_fields") or {}
    for name, fields in fixed.items():
        lines.append(f"{'Would update' if ctx.dry_run else 'Updated'} "
                     + _fixed_words(name, fields, (campaigns.get("drift") or {}).get(name) or {}))
    held = campaigns.get("held") or {}
    for name, drift in (campaigns.get("drift") or {}).items():
        hold = set((held.get(name) or {}).get("keys") or ())
        rest = sorted(k for k in drift if k not in fixed.get(name, ()) and k not in hold)
        if rest:
            lines.append(f"Campaign drift, {name}: {', '.join(rest)}. Fix with `us-outbound campaigns ensure --fix --live`.")
        if hold:
            lines.append(held_words(name, held[name]))
    starting = out.get("campaign_start") or {}
    for name in starting.get("started") or ():
        lines.append(f"Started {name}: its owner has an Active mailbox and sending is live.")
    for name in starting.get("would_start") or ():
        lines.append(f"Would start {name}: its owner has an Active mailbox and sending is live.")
    for err in starting.get("errors") or ():
        lines.append(f"Instantly would not start {err}")
    for owner in starting.get("ready") or ():
        lines.append(f"{owner}'s campaign is ready: run `us-outbound start --live` to start it.")
    for owner in starting.get("paused") or ():
        lines.append(f"{campaign_name(owner)} is paused, though {owner} has an Active mailbox: run "
                     "`us-outbound start --live` to start it.")
    for owner, why in (starting.get("blackout") or {}).items():  # never a reason to post on its own
        lines.append(f"{campaign_name(owner)}: {why}.")
    if campaigns.get("pending"):
        lines.append(f"Campaigns waiting for an Active mailbox: {', '.join(campaigns['pending'])}")
    if campaigns.get("unknown"):
        lines.append(f"Unknown US Outbound campaigns (not touched): {', '.join(campaigns['unknown'])}")
    return "\n".join(lines)


def mailbox_health(ctx: Context, *, fix_names: bool = False) -> dict:
    """The mailbox_health job: warmup status, promotions, retirements, campaign drift and starts (module docstring).

    fix_names (`mailbox check --fix`): also set each drifted sender name to its owner's full name.
    """
    settings = ctx.settings
    registry = [m for m in settings.mailboxes if m.status != RETIRED]
    if not registry:
        return {"mailboxes": 0}
    inst = ctx.clients.instantly
    today = ctx.today_uk()
    warmups = inst.warmup_status([m.address for m in registry])
    out: dict[str, Any] = {
        "dry_run": ctx.dry_run, "mailboxes": len(registry), "promoted": [], "retired": [],
        "warmup_turned_on": [], "waiting_to_retire": [], "not_found": [], "no_sheet_row": [],
    }
    rows: list[dict] = []
    changes: list[tuple[Mailbox, str]] = []
    # What Instantly reports back (enrol/capacity.py and limits.py read this job's latest summary):
    #   instantly_daily_limits: each mailbox's own daily limit in Instantly;
    #   sent_by_day:            campaign emails each mailbox sent on each of the last 7 days;
    #   campaign_status:        per owner, why Instantly says the campaign is not sending, if it is not.
    # The Mailboxes tab is where caps are set: a mailbox whose Instantly limit differs from its
    # cap today (its daily_cap, or the sending ramp's when lower) is set to it (live; reported
    # in dry-run), as warmup is turned back on.
    ramp = ramps_.ramps(ctx.store, settings, ctx.now_et().date())
    out["ramp"] = {a: r.as_dict() for a, r in ramp.items()}
    out["instantly_daily_limits"] = {}
    out["limit_set"] = {}
    for m in registry:
        w = warmups.get(m.address.lower(), {})
        if not w.get("found") or w.get("daily_limit") is None:
            continue
        out["instantly_daily_limits"][m.address.lower()] = w["daily_limit"]
        cap = ramp[m.address.lower()].cap if m.address.lower() in ramp else int(m.daily_cap or 0)
        if m.status != RETIRED and _as_int(w["daily_limit"]) != cap:
            out["limit_set"][m.address.lower()] = {"from": w["daily_limit"], "to": cap}
    # The sender name (Harry, 5 Oct 2026): Instantly's first_name and last_name, which make the From name
    # prospects see, against the owner's full name. Reported every day; set only with fix_names.
    out["name_drift"] = {}
    for m in registry:
        w = warmups.get(m.address.lower(), {})
        if not w.get("found"):
            continue
        have, want = (_name_text(w.get("first_name")), _name_text(w.get("last_name"))), sender_name(m.owner_name)
        if have != want:
            out["name_drift"][m.address.lower()] = {"from": list(have), "to": list(want)}
    out["names_set"] = sorted(out["name_drift"]) if fix_names else []
    present = [m.address.lower() for m in registry if warmups.get(m.address.lower(), {}).get("found")]
    out["sent_by_day"] = {}
    if present:
        sends = inst.daily_sends(present, start_date=(today - timedelta(days=7)).isoformat(), end_date=today.isoformat())
        out["sent_by_day"] = {a: {d: _as_int(r.get("sent")) for d, r in days.items()} for a, days in sends.items()}
    out["campaign_status"] = {}
    for owner in settings.owners():
        try:
            out["campaign_status"][owner] = inst.sending_status(campaign_name(owner))
        except LookupError:  # no campaign yet (ensure_campaigns below creates or reports it)
            continue

    for m in registry:
        w = warmups.get(m.address.lower(), {})
        rows.append({
            "address": m.address, "owner": m.owner_name, "status": m.status, "found": bool(w.get("found")),
            "warmup_enabled": bool(w.get("warmup_enabled")), "warmup_score": w.get("warmup_score"),
            "account_status": w.get("status"), "daily_cap": m.daily_cap, "instantly_daily_limit": w.get("daily_limit"),
            "ramp": ramp[m.address.lower()].describe() if m.address.lower() in ramp else "",
            "cap_today": ramp[m.address.lower()].cap if m.address.lower() in ramp else m.daily_cap,
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
    for address, change in out["limit_set"].items():
        inst.set_daily_limit(address, change["to"])  # the sheet's daily_cap (or the ramp's) is the one place caps are set
    # The From name of the follow-ups already in flight changes too: on record, with how many (Harry, 7 Oct 2026).
    flying = leads_in_flight(ctx, settings) if out["names_set"] and ctx.live else Counter()
    for address in out["names_set"]:
        inst.set_sender_name(address, *out["name_drift"][address]["to"])  # the account's name only, nothing else
        name = campaign_name(next((m.owner_name for m in registry if m.address.lower() == address), ""))
        config_version.log_change(ctx, config_version.SENDER_NAME, name, [address], out["name_drift"][address],
                                  flying[name])
    sheet_id = ctx.guard.bounds.settings_sheet_id
    new_settings = settings
    for m, status in changes:
        if sheet_id and not ctx.clients.sheets.update_cell(sheet_id, TAB, {"address": m.address}, "status", status):
            out["no_sheet_row"].append(m.address)  # the change is not made; the summary says so
            out["promoted" if status == ACTIVE else "retired"].remove(m.address)
            continue
        new_settings = _with(new_settings, dataclasses.replace(m, status=status))
    out["campaign_actions"] = {
        campaign_name(owner): _sync_campaign(ctx, new_settings, owner)
        for owner in dict.fromkeys(m.owner_name for m, _ in changes)
        if owner in new_settings.owners()
    }
    # The daily limit follows the ramp and the sending list the sheet: those are put right here; other drift is reported.
    out["campaigns"] = ensure_campaigns(ctx, settings=new_settings, fix=True, fix_only=AUTO_FIX)
    found = out["campaigns"]
    created = {n for n, a in out["campaign_actions"].items() if a == CREATED} | set(found["created"])
    # Held drift does not keep a campaign from starting: it is consistent for its leads in flight.
    drifted = {n for n, d in found["drift"].items()
               if set(d) - set(found["fixed_fields"].get(n, ())) - set((found["held"].get(n) or {}).get("keys") or ())}
    out["campaign_start"] = start_waiting(ctx, new_settings, created, drifted)
    out["post_reasons"] = _noteworthy(out)
    out["posted"] = bool(out["post_reasons"])
    if out["posted"]:  # otherwise nothing changed and nothing is wrong: the log and the heartbeat have it
        ctx.clients.slack.post(settings.general.alert_channel, _summary_text(ctx, rows, out))
    log("mailbox_health", **{k: v for k, v in out.items() if k != "campaigns"})
    return out
