"""capture (Mondays 07:30 UK; W1-2, 19 Oct 2026): is the reply read working, or is "no replies" real?

poll_replies ran 668 times to 19 Oct 2026 and saw no received email on any run. At a true 3% reply rate that is
not unlikely, and nothing could tell it from a broken read: a misread Instantly field, a mode filter, a drop
nobody counted (code-replies-crm M3, M4; RT §1.6). This job checks the read two ways, each week:

  (a) reconcile: for every US Outbound campaign, Instantly's own analytics over the last 7 days
      (GET /campaigns/analytics: emails sent, leads that replied with automatic replies left out, leads that
      bounced) against our events over the same window, counted in the same units: sent events (emails),
      distinct contacts with a replied event whose class is not out_of_office, and distinct contacts with a
      bounced event. A measure drifts when abs(ours - theirs) > max(DRIFT_MIN, DRIFT_SHARE x theirs); and replied
      drifts too whenever theirs > 0 and ours == 0, the case the tolerance cannot see at today's counts (two of
      their replies against none of ours is inside max(2, 0.2)). Once ours is above 0 the tolerance is blind to
      small gaps again (theirs 3, ours 1: |3 - 1| = 2 is not more than 2): the canary covers that case.
      The plan named the steps analytics; it has no bounce count (Instantly's OpenAPI), so the campaign totals
      are read instead, which also state their unit (leads) for replies and bounces.
  (b) canary: an email to the seed inbox through the existing seed send path (ops/seed.send: the owner's own
      campaign, its step template, a Copy row), and at the next run (or `us-outbound capture check`) the
      check that a received email from the seed address came back through the same read poll_replies uses
      (Instantly.list_emails, mode emode_all). The seed inbox answers by itself once it has an auto-reply rule
      (Harry's D10 e). The seed inbox is the one of the 6 Oct seed test, found from its lead in the campaigns
      (ops/seed.SEED_COMPANY) or from the last canary; each canary is the inbox's address tagged
      "+canary-YYYYMMDD" (a new lead each week: a campaign takes an address once), and its lead is deleted
      from the campaign once checked, so the seed inbox gets one email a week, not four. With no seed lead
      the canary is "not set up": the readout's fact says capture is unverified, and no error is raised.

A drift, a canary with no answer, or a canary that could not be sent are entries under the summary's errors (so
heartbeat_check's ops/job_errors.py posts them) and make the week's fact capture_unverified (signal_events,
source capture, event_id capture:{ISO week}, no account) for the readout to print on every outcome line;
otherwise the week's fact is capture_verified. Our own counts include the last canary (its send and its answer
are in Instantly's totals too). Dry-run reads and reports; it adds no canary lead, deletes none and writes no fact.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from us_outbound.clients.guard import US_CAMPAIGN_PREFIX, GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import UK, ConfigError, Context
from us_outbound.logs import log
from us_outbound.replies import outcomes
from us_outbound.timeparse import utc

JOB = "capture"
CHECK_JOB = "capture_check"  # `us-outbound capture check`: the canary check alone, by hand
WINDOW = timedelta(days=7)  # what reconcile compares
CANARY_WAIT = timedelta(hours=20)  # a canary younger than this is not checked yet (it goes out in the US window)
DRIFT_MIN, DRIFT_SHARE = 2, 0.10  # the tolerance: max(2, 10% of Instantly's count)
MEASURES = ("sent", "replied", "bounced")
# Instantly's campaign analytics fields for each measure, and the unit each is counted in (on both sides).
THEIR_FIELDS = {"sent": "emails_sent_count", "replied": "reply_count_unique", "bounced": "bounced_count"}
UNITS = {"sent": "emails", "replied": "leads (distinct; automatic replies and out-of-office left out)",
         "bounced": "leads (distinct)"}
SOURCE = "capture"  # signal_events.source of the facts below
VERIFIED, UNVERIFIED, CANARY_SENT = "capture_verified", "capture_unverified", "canary_sent"
CANARY_TAG = "+canary-"
NOT_SET_UP = "not_set_up"


# -- reconcile --------------------------------------------------------------------------------------------


def drift(theirs: Mapping[str, int], ours: Mapping[str, int]) -> list[dict]:
    """The measures whose counts disagree beyond the tolerance (the module docstring), in MEASURES order."""
    out = []
    for m in MEASURES:
        t, o = int(theirs.get(m) or 0), int(ours.get(m) or 0)
        if (m == "replied" and t > 0 and o == 0) or abs(o - t) > max(DRIFT_MIN, DRIFT_SHARE * t):
            out.append({"measure": m, "instantly": t, "ours": o})
    return out


def _in(e: Mapping[str, Any], start: datetime, end: datetime) -> bool:
    at = utc(e.get("occurred_at"))
    return at is not None and start <= at < end


def our_counts(ctx: Context, start: datetime, end: datetime) -> dict[str, int]:
    """Our events in [start, end), in Instantly's units (UNITS)."""
    store = ctx.store
    sent = [e for e in store.select("events", {"type": outcomes.SENT}) if _in(e, start, end) and not e.get("approval")]
    replied = {str(e.get("contact_id")) for e in store.select("events", {"type": outcomes.REPLIED})
               if _in(e, start, end) and e.get("contact_id") and e.get("reply_class") != "out_of_office"}
    bounced = {str(e.get("contact_id")) for e in store.select("events", {"type": outcomes.BOUNCED})
               if _in(e, start, end) and e.get("contact_id")}
    return {"sent": len(sent), "replied": len(replied), "bounced": len(bounced)}


def their_counts(ctx: Context, start: datetime, end: datetime) -> tuple[dict[str, int], list[str]]:
    """Instantly's totals over the US Outbound campaigns, and the campaigns read."""
    inst = ctx.clients.instantly
    names = sorted(str(c["name"]) for c in inst.list_campaigns() if str(c.get("name", "")).startswith(US_CAMPAIGN_PREFIX))
    total: Counter[str] = Counter({m: 0 for m in MEASURES})
    for name in names:
        row = inst.campaign_analytics(name, start_date=start, end_date=end)
        for m, key in THEIR_FIELDS.items():
            try:
                total[m] += int(row.get(key) or 0)
            except (TypeError, ValueError):
                pass
    return dict(total), names


def reconcile(ctx: Context, *, canary: Mapping[str, int] | None = None) -> dict:
    """Instantly against our events over the last WINDOW (the module docstring). canary: the last canary's send and
    answer, which are in Instantly's totals and not in our events (no contact of ours)."""
    end = ctx.now
    start = end - WINDOW
    theirs, names = their_counts(ctx, start, end)
    ours = our_counts(ctx, start, end)
    extra = {m: int((canary or {}).get(m) or 0) for m in MEASURES}
    ours_with = {m: ours[m] + extra[m] for m in MEASURES}
    return {"since": start.isoformat(), "until": end.isoformat(), "campaigns": len(names), "instantly": theirs,
            "ours": ours_with, "ours_includes_canary": {m: v for m, v in extra.items() if v}, "units": UNITS,
            "drift": drift(theirs, ours_with)}


# -- the canary ---------------------------------------------------------------------------------------------


def base_address(address: str) -> str:
    """The seed inbox's own address: a canary's tag cut ("seed+canary-20261026@x" -> "seed@x")."""
    a = str(address or "").strip().lower()
    local, at, domain = a.partition("@")
    return f"{local.split(CANARY_TAG, 1)[0]}{at}{domain}" if at else a


def inbox_of(address: str) -> str:
    """The mailbox an address delivers to, every "+tag" cut ("harry+seed1@x" -> "harry@x"): an inbox's auto-reply
    comes from its own address, not the tagged one the canary went to."""
    local, at, domain = str(address or "").strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}{at}{domain}" if at else local


def canary_address(base: str, when: datetime) -> str:
    local, _, domain = base.partition("@")
    return f"{local}{CANARY_TAG}{when.astimezone(UK):%Y%m%d}@{domain}"


def _facts(ctx: Context, fact: str) -> list[dict]:
    rows = ctx.store.select("signal_events", {"source": SOURCE, "fact": fact})
    return sorted(rows, key=lambda r: (str(r.get("observed_at") or ""), str(r.get("event_id"))))


def seed_inbox(ctx: Context) -> tuple[str, str] | None:
    """(the seed inbox's address, the owner whose campaign sends to it): from its seed lead in the campaigns, else
    from the last canary; None when there is neither (the canary is not set up)."""
    from us_outbound.ops import seed

    rows = sorted(seed.check(ctx), key=lambda r: (CANARY_TAG in r["address"], r["address"]))
    if rows:
        return base_address(rows[0]["address"]), rows[0]["campaign"].removeprefix(US_CAMPAIGN_PREFIX)
    last = _facts(ctx, CANARY_SENT)
    if last:
        v = last[-1].get("value") or {}
        if v.get("base") and v.get("owner"):
            return str(v["base"]), str(v["owner"])
    return None


def _emails_with(emails: Iterable[Mapping[str, Any]], base: str, *, field: str) -> list[Mapping[str, Any]]:
    """The emails whose sender (field from) or recipient (field to) is the seed inbox, whatever "+tag" it carries."""
    out = []
    inbox = inbox_of(base)
    for e in emails:
        addresses = ([outcomes.from_address(e)] if field == "from"
                     else outcomes._addresses(e.get("to_address_email_list")) + outcomes._addresses(e.get("lead")))
        if any(inbox_of(a) == inbox for a in addresses if a):
            out.append(e)
    return out


def _delete_canary_lead(ctx: Context, campaign: str, address: str) -> str:
    """Take the checked canary's lead out of its campaign, so emails 2 to 4 never go; what happened."""
    if ctx.dry_run:
        return "dry-run"
    inst = ctx.clients.instantly
    try:
        leads = [x for x in inst.list_leads(campaign) if str(x.get("email") or "").strip().lower() == address]
        for lead in leads:
            inst.delete_lead(campaign, str(lead["id"]))
    except (ApiError, LookupError, ConfigError, GuardViolation) as exc:
        return f"not deleted: {type(exc).__name__}: {str(exc)[:120]}"
    return "deleted" if leads else "not found"


def check(ctx: Context) -> dict:
    """The last canary's check (the module docstring): {status, ...}. status is ok, no_answer, not_sent (the
    campaign never sent it), waiting (younger than CANARY_WAIT), checked (already) or none (no canary yet)."""
    pending = [r for r in _facts(ctx, CANARY_SENT) if not (r.get("value") or {}).get("checked_at")]
    if not pending:
        return {"status": "none"}
    row = pending[-1]
    v = dict(row.get("value") or {})
    added = utc(v.get("added_at")) or utc(row.get("observed_at")) or ctx.now
    if ctx.now - added < CANARY_WAIT:
        return {"status": "waiting", "added_at": added.isoformat()}
    base, campaign, address = str(v.get("base") or ""), str(v.get("campaign") or ""), str(v.get("address") or "")
    inst = ctx.clients.instantly
    registry = sorted(ctx.guard.bounds.registry_addresses)
    since = added - timedelta(hours=1)
    answers = _emails_with(inst.list_emails(registry, since, email_type="received"), base, field="from")
    reasons = Counter(outcomes.received_reason(e, outcomes._lower(e.get("eaccount")), frozenset(registry)) or "received"
                      for e in answers)
    sent = 1 if answers else len(_emails_with(inst.list_emails(registry, since, email_type="sent"), base, field="to"))
    status = "ok" if answers else ("no_answer" if sent else "not_sent")
    out = {"status": status, "added_at": added.isoformat(), "answers": len(answers), "reasons": dict(reasons),
           "sent": sent, "lead": _delete_canary_lead(ctx, campaign, address)}
    if ctx.live:
        v.update(checked_at=ctx.now.isoformat(), status=status, answers=len(answers), sent=sent)
        ctx.store.upsert("signal_events", [{**row, "value": v}])
    log("capture_canary_checked", run_id=ctx.run_id, status=status, answers=len(answers))
    return out


def send_canary(ctx: Context) -> dict:
    """This week's canary through ops/seed.send: {status, ...}; status sent, not_set_up, failed, already_sent (a
    second run the same day) or dry_run."""
    from us_outbound.ops import seed

    found = seed_inbox(ctx)
    if found is None:
        return {"status": NOT_SET_UP, "why": "no seed lead in the campaigns: `us-outbound seed send ADDRESS --owner "
                                              "NAME --live` sets one up (and give the inbox an auto-reply)"}
    base, owner = found
    address = canary_address(base, ctx.now)
    try:
        sent = seed.send(ctx, address, owner)
    except (ValueError, LookupError, ApiError, ConfigError, GuardViolation) as exc:
        return {"status": "failed", "why": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if ctx.dry_run:
        return {"status": "dry_run", "campaign": sent.get("campaign")}
    if not sent.get("added"):
        why = str(sent.get("why") or "Instantly did not add the canary lead")
        return {"status": "already_sent" if "already a lead" in why else "failed", "why": why}  # a re-run same day
    ctx.store.upsert("signal_events", [{
        "event_id": f"{SOURCE}:canary:{ctx.now.astimezone(UK):%Y%m%d}", "account_id": None, "source": SOURCE,
        "fact": CANARY_SENT, "observed_at": ctx.now,
        "value": {"address": address, "base": base, "owner": owner, "campaign": sent["campaign"],
                  "added_at": ctx.now.isoformat(), "checked_at": None},
    }])
    return {"status": "sent", "campaign": sent["campaign"]}


def _canary_in_window(ctx: Context, checked: Mapping[str, Any]) -> dict[str, int]:
    """The last canary's own send and answer, which Instantly counts in the reconcile window and we do not."""
    added = utc(checked.get("added_at"))
    if added is None or ctx.now - added > WINDOW:
        return {}
    return {"sent": int(checked.get("sent") or 0), "replied": 1 if checked.get("answers") else 0}


# -- the job ------------------------------------------------------------------------------------------------


def week_key(ctx: Context) -> str:
    year, week, _ = ctx.now.astimezone(UK).isocalendar()
    return f"{year}-W{week:02d}"


def run(ctx: Context) -> dict:
    """The capture job (JOB CONTRACT: run(ctx) -> summary): the last canary checked, reconcile, this week's canary."""
    if not ctx.guard.bounds.registry_addresses:
        return {"skipped": True, "reason": "no registry mailboxes"}
    errors: list[str] = []
    canary = check(ctx)
    rec = reconcile(ctx, canary=_canary_in_window(ctx, canary) if canary["status"] in ("ok", "no_answer") else None)
    for d in rec["drift"]:
        errors.append(f"capture drift on {d['measure']}: Instantly {d['instantly']}, ours {d['ours']} "
                      f"({UNITS[d['measure']]}, last 7 days)")
    if canary["status"] == "no_answer":
        errors.append("capture canary: the seed inbox's answer never reached the reply read")
    elif canary["status"] == "not_sent":
        errors.append("capture canary: the campaign never sent the seed email (paused, or out of its window?)")
    new = send_canary(ctx)
    if new["status"] == "failed":
        errors.append(f"capture canary not sent: {new['why']}")
    reasons = [*(f"drift:{d['measure']}" for d in rec["drift"]),
               *([f"canary:{canary['status']}"] if canary["status"] not in ("ok",) else []),
               *([f"canary_send:{new['status']}"] if new["status"] in ("failed", NOT_SET_UP) else [])]
    verified = not rec["drift"] and canary["status"] == "ok"
    fact = VERIFIED if verified else UNVERIFIED
    if ctx.live:
        ctx.store.upsert("signal_events", [{
            "event_id": f"{SOURCE}:{week_key(ctx)}", "account_id": None, "source": SOURCE, "fact": fact,
            "observed_at": ctx.now, "value": {"reasons": reasons, "drift": rec["drift"], "canary": canary["status"]},
        }])
    summary = {"job": JOB, "dry_run": ctx.dry_run, "verified": verified, "fact": fact, "reasons": reasons,
               "reconcile": rec, "canary_check": canary, "canary_send": new, "errors": errors}
    log("capture_done", run_id=ctx.run_id, verified=verified, reasons=reasons)
    return summary


def check_only(ctx: Context) -> dict:
    """`us-outbound capture check`: the last canary's check alone (on a Tuesday, the day after it went)."""
    out = check(ctx)
    errors = {"no_answer": ["capture canary: the seed inbox's answer never reached the reply read"],
              "not_sent": ["capture canary: the campaign never sent the seed email"]}.get(out["status"], [])
    return {"job": CHECK_JOB, "dry_run": ctx.dry_run, "canary_check": out, "errors": errors}
