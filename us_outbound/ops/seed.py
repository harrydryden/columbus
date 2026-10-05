"""The seed-inbox test of the opt-out (golive's "Opt-out tested"; Harry, 5 Oct 2026).

Before any prospect is emailed, one email goes the real way to an inbox of ours: into the owner's own
campaign, through its step template with Instantly's {{unsubscribe}} link, with a Copy row rendered as
enrol renders it. Harry reads it (the body reads as formatted text, the links work, the List-Unsubscribe
header is there), clicks the unsubscribe link, and `seed check` reads the lead back as sync_outcomes does:
status -2 (instantly.LEAD_UNSUBSCRIBED) is the pass, and settles the PHASE0-CONFIRM in clients/instantly.py.

  us-outbound seed send ADDRESS --owner NAME [--industry I] [--role R] [--subject personal|copy] [--live]
      adds ADDRESS as one lead to the owner's campaign, with the four emails of the Copy row for that
      industry and role, filled for copy preview's sample prospect (Harbor & Finch, Dana) and the
      generic opener line. A draft row is fine: the email goes only to us. Live with --live alone, as an
      operator command: it reaches no prospect. ADDRESS must be ours (SEED_DOMAINS, a registry mailbox's
      domain, or a free-mail inbox) and must not be a contact, or on an account's domain, that we hold.
      Email 1's subject is the Copy row's s1_subject, or with --subject personal the General email1_subject
      ("support for the Harbor & Finch team"), so Harry can see either arm of the subject split
      (render.subject_arm; Harry, 5 Oct 2026). Emails 2 to 4 are the same either way.
  us-outbound seed check
      every seed lead in the US Outbound campaigns, with its status. Read-only.

Instantly sends nothing from a campaign in Draft: the seed goes out once `us-outbound start --live`
activates the campaign, in its window (Mon–Fri 09:00–16:00 ET). start needs live_sending = yes; while
optout_tested is no, no prospect is added even then (enrol.gate posts no cards, and a ✅ is held).
A seed lead is no contact of ours, so sync_outcomes leaves it out ("lead: no contact of ours") and the
daily post does not count it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from us_outbound.clients.instantly import CAMPAIGN_STATUS, LEAD_BOUNCED, LEAD_UNSUBSCRIBED
from us_outbound.context import UK, Context
from us_outbound.enrol import copy_desk, render
from us_outbound.logs import hash_email
from us_outbound.ops.erase import _contacts_by_email
from us_outbound.registry.mailboxes import campaign_name
from us_outbound.settings.model import ROLE_LINE_COLUMNS, CopyRow, Settings

JOB = "seed_send"
SEED_COMPANY = "Seed test (Spill)"  # the lead's company_name: how `seed check` knows a seed lead
SEED_DOMAINS = frozenset({"spill.chat"})
FREE_MAIL = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "icloud.com",
                       "me.com", "yahoo.com", "proton.me", "protonmail.com"})
SUBJECT_NOTES = {render.PERSONAL_SUBJECT: "the personal subject, General email1_subject",
                 render.COPY_SUBJECT: "the Copy row's s1_subject"}
LEAD_STATUS = {1: "active: waiting for the campaign's window, or sending", 2: "paused",
               3: "completed: all four emails sent", LEAD_BOUNCED: "bounced", LEAD_UNSUBSCRIBED: "unsubscribed",
               -3: "skipped"}


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def check_address(ctx: Context, address: str) -> str:
    """The address, lower-cased, once it is plainly an inbox of ours; ValueError if it might be a prospect's."""
    address = address.strip().lower()
    domain = _domain(address)
    if not domain or address.count("@") != 1 or " " in address:
        raise ValueError(f"{address!r} is not an email address")
    ours = SEED_DOMAINS | {m.domain.lower() for m in ctx.settings.mailboxes} | FREE_MAIL
    if domain not in ours:
        raise ValueError(f"a seed inbox is one of ours: an address on {', '.join(sorted(SEED_DOMAINS))}, a "
                         f"mailbox domain, or a free-mail inbox, not on {domain}")
    if ctx.store.select("contacts", {"email_sha256": hash_email(address)}) or _contacts_by_email(ctx, address):
        raise ValueError(f"{address} is a contact we hold: a seed inbox is never a prospect")
    if domain not in FREE_MAIL and domain not in SEED_DOMAINS and ctx.store.select("accounts", {"domain": domain}):
        raise ValueError(f"{domain} is a company we hold: a seed inbox is never a prospect")
    return address


def pick_row(settings: Settings, industry: str, role: str) -> CopyRow:
    """The Copy row the seed shows: for the industry (default: the first active one with a row) and role."""
    rows = [c for c in settings.copy if c.status != "retired"]
    if industry:
        rows = [c for c in rows if c.industry.casefold() == industry.casefold()]
        if not rows:
            raise LookupError(f"no Copy row for the industry {industry!r}")
    else:
        active = [i.industry.casefold() for i in settings.industries if i.active]
        rows = sorted((c for c in rows if c.industry.casefold() in active),
                      key=lambda c: active.index(c.industry.casefold())) or rows
    mine = [c for c in rows if c.role.casefold() == role.casefold()]
    if not (mine or rows):
        raise LookupError("no Copy row to send")
    return (mine or rows)[0]


def send_window(settings: Settings) -> str:
    """The campaign's window in its own time zone and in UK time, today: "09:00–16:00 ET (14:00–21:00 UK)"."""
    w = settings.general.send_window
    tz = ZoneInfo(w.tz)
    today = datetime.now(tz).date()
    start, end = (datetime.combine(today, t, tzinfo=tz).astimezone(UK) for t in (w.start, w.end))
    return f"{w.start:%H:%M}–{w.end:%H:%M} US Eastern, Monday to Friday ({start:%H:%M}–{end:%H:%M} UK today)"


def send(ctx: Context, address: str, owner: str, *, industry: str = "", role: str = "",
         subject: str = render.COPY_SUBJECT) -> dict[str, Any]:
    """Add one seed lead to the owner's campaign (dry-run: say what would be added). subject is email 1's subject
    arm: personal (General email1_subject) or copy (the Copy row's s1_subject)."""
    s = ctx.settings
    if subject not in render.SUBJECT_ARMS:
        raise ValueError(f"--subject is {' or '.join(render.SUBJECT_ARMS)}, not {subject!r}")
    if subject == render.PERSONAL_SUBJECT and not s.general.email1_subject.strip():
        raise ValueError("email1_subject is blank on the General tab, so there is no personal subject to send")
    address = check_address(ctx, address)
    owners = {o.casefold(): o for o in s.owners()}
    owner = owners.get(owner.strip().casefold(), "")
    if not owner:
        raise LookupError(f"no mailbox owner by that name: one of {', '.join(s.owners())}")
    boxes = s.mailboxes_for(owner, "Active")
    if not boxes:
        raise LookupError(f"{owner} has no Active mailbox yet, so their campaign sends nothing")
    name = campaign_name(owner)
    inst = ctx.clients.instantly
    campaign = next((c for c in inst.list_campaigns() if c.get("name") == name), None)
    if campaign is None:
        raise LookupError(f"{name} is not in Instantly yet: `us-outbound campaigns ensure --live` creates it")
    role = role or next(iter(ROLE_LINE_COLUMNS))
    row = pick_row(s, industry, role)
    preview = copy_desk.preview(row, s, role=role, sender=owner, opener=copy_desk.SAMPLE_OPENER, subject_arm=subject)
    problems = render.violations(preview.emails)
    if problems:
        raise ValueError(f"{row.copy_version} breaks the copy rules, so it would not be sent: " + "; ".join(problems))
    lead = {"email": address, "first_name": copy_desk.SAMPLE_CONTACT["first_name"], "company_name": SEED_COMPANY,
            "custom_variables": render.custom_variables(preview.emails)}
    out: dict[str, Any] = {"dry_run": ctx.dry_run, "address": address, "campaign": name,
                           "campaign_status": _campaign_status(campaign), "copy_version": row.copy_version,
                           "copy_status": row.status, "role": preview.role, "sender": owner,
                           "subject": preview.emails[0].subject, "subject_arm": subject, "email_1": preview.emails[0].text}
    already = [x for x in inst.list_leads(name) if str(x.get("email") or "").lower() == address]
    if already:
        out.update(added=False, why=f"{address} is already a lead in {name}")
        return out
    added = inst.add_leads(name, [lead]) or {}
    if ctx.dry_run:
        out["added"] = False
        return out
    created = [c for c in added.get("created_leads") or () if c.get("id")]
    out["added"] = bool(created)
    if not created:
        counts = {k: v for k, v in added.items() if k in ("in_blocklist", "skipped_count", "duplicated_leads",
                                                          "invalid_email_count") and v}
        out["why"] = ("Instantly did not take the lead (" + ", ".join(f"{k} {v}" for k, v in counts.items())
                      + "): it may be on the workspace's blocklist or in another campaign; try another address"
                      if counts else "Instantly did not report the lead as created: run `us-outbound seed check`")
    return out


def _campaign_status(campaign: Mapping[str, Any]) -> str:
    try:
        return CAMPAIGN_STATUS.get(int(campaign.get("status")), str(campaign.get("status")))
    except (TypeError, ValueError):
        return "unknown"


def check(ctx: Context) -> list[dict[str, Any]]:
    """Every seed lead in the owners' campaigns, with its status as sync_outcomes reads it."""
    inst = ctx.clients.instantly
    found = {c.get("name"): c for c in inst.list_campaigns()}
    out = []
    for owner in ctx.settings.owners():
        name = campaign_name(owner)
        if name not in found:
            continue
        for lead in inst.list_leads(name):
            if str(lead.get("company_name") or "") != SEED_COMPANY:
                continue
            try:
                code = int(lead.get("status"))
            except (TypeError, ValueError):
                code = None
            out.append({"address": str(lead.get("email") or "").lower(), "campaign": name,
                        "campaign_status": _campaign_status(found[name]), "status": code,
                        "status_text": LEAD_STATUS.get(code, f"status {lead.get('status')}") if code is not None
                        else "status unknown",
                        "last_contact": lead.get("timestamp_last_contact") or "",
                        "unsubscribed": code == LEAD_UNSUBSCRIBED})
    return out
