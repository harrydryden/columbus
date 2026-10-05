"""hubspot_readback, every 15 minutes (SPEC 9; SPEC 11 "HubSpot writes" step 5; SPEC 12 meetings booked).

  1. Meetings. Harry's meetings created or changed in the last LOOKBACK_DAYS that were booked through
     his meetings link (the emails' link and the website's booking page both book into it;
     PHASE0-CONFIRM that such a meeting has hs_meeting_source MEETINGS_PUBLIC) are matched to US
     Outbound accounts: by the meeting's companies (accounts.hubspot_company_id), its contacts
     (contacts.hubspot_contact_id, else the email's hash), or the email's domain for an account
     already enrolled (the company is the unit, SPEC 2). A meeting made before the account's first
     enrolment does not count. Each is recorded once as meeting_booked (and as demo_held once its
     outcome is COMPLETED); the account moves to demo_booked; the deal is made if the company has no
     open deal (SPEC 11 step 5); and the account's leads are stopped in their sender's campaign
     (SPEC 9: "stops leads when a meeting is booked"; clients/instantly.stop_lead, PHASE0-CONFIRM).
  2. Deals. For each enrolled account with a HubSpot company, its deals in Spill 3.0, each recorded once
     as deal_created. A deal at "Demo created" or later counts as a booking (meeting_booked, if the
     account has none yet, and demo_booked); at "Demo held" or later, as demo_held (if none yet). A
     Closed lost deal counts for neither. The stage labels are docs/phase0-facts.md's (PHASE0-CONFIRM).
  3. An account at demo_requested or demo_booked whose company has never had a deal gets one
     (one that could not be made when the reply came, e.g. before the pipeline ids were set).
The events are idempotent on the HubSpot object ids: event_id "hs-meeting:{id}" and "hs-deal:{id}",
with ":held" or ":booked" for the ones derived from them. Dry-run reads HubSpot and reports what it
would record; it writes nothing (no events, statuses, deals or lead stops).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from us_outbound.clients.hubspot import CONTACT_PROPS, MEETING_PROPS
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.crm.hubspot_writes import DEAL_EVENT, advance_status, ensure_deal
from us_outbound.logs import hash_email, log
from us_outbound.replies.items import ts

JOB = "hubspot_readback"
LOOKBACK_DAYS = 3  # each run re-reads three days of changes, so a missed run or two loses nothing
BOOKING_SOURCES = frozenset({"MEETINGS_PUBLIC"})  # PHASE0-CONFIRM: booked through a HubSpot meetings link
HELD_OUTCOMES = frozenset({"COMPLETED"})
MEETING_EVENT = "hs-meeting:"
BOOKED_LABEL, HELD_LABEL, LOST_LABEL = "demo created", "demo held", "closed lost"  # Spill 3.0 (phase0-facts)
ENROLLED = ("enrolled", "engaged", "demo_requested", "demo_booked")  # accounts a booking or deal can belong to
LIST_LIMIT = 50


@dataclass
class _Run:
    counts: Counter[str] = field(default_factory=Counter)
    events: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def note(self, entry: dict) -> None:
        if len(self.events) < LIST_LIMIT:
            self.events.append(entry)


def _event(ctx: Context, run: _Run, event_id: str, type_: str, account_id: str, contact_id: str | None,
           occurred_at: Any) -> bool:
    """Record the event once; True if it is new. Dry-run only reports it."""
    if ctx.store.get("events", event_id=event_id) is not None:
        return False
    run.counts[type_] += 1
    run.note({"event": type_, "account_id": account_id, "id": event_id})
    if ctx.live:
        ctx.store.upsert("events", [{"event_id": event_id, "type": type_, "account_id": account_id,
                                     "contact_id": contact_id or None, "occurred_at": ts(occurred_at) or ctx.now}])
    return True


def _has(ctx: Context, account_id: str, type_: str) -> bool:
    return bool(ctx.store.select("events", {"account_id": account_id, "type": type_}))


def _status(ctx: Context, account_id: str, status: str) -> None:
    if ctx.live:
        advance_status(ctx, account_id, status)


def _first_enrolled(ctx: Context, account_id: str) -> datetime | None:
    times = [t for c in ctx.store.select("contacts", {"account_id": account_id}) if (t := ts(c.get("enrolled_at")))]
    return min(times) if times else None


# -- 1. meetings ---------------------------------------------------------------------------------------


def _match(ctx: Context, company_ids: Iterable[str], contact_ids: Iterable[str]) -> dict[str, dict]:
    """account_id -> {"account", "contact_id"} for the US Outbound accounts a meeting belongs to."""
    store, hs = ctx.store, ctx.clients.hubspot
    out: dict[str, dict] = {}
    companies = sorted(set(company_ids))
    if companies:
        for a in store.select("accounts", {"hubspot_company_id": companies}):
            out.setdefault(a["account_id"], {"account": a, "contact_id": None})
    for hid in dict.fromkeys(contact_ids):
        ours = store.select("contacts", {"hubspot_contact_id": hid})
        if not ours:
            rec = hs.get_record("contacts", hid, CONTACT_PROPS)
            email = str(((rec or {}).get("properties") or {}).get("email") or "").strip().lower()
            if "@" not in email:
                continue
            ours = store.select("contacts", {"email_sha256": hash_email(email)})
            if not ours:  # someone else at an enrolled account (a reply from anyone counts, SPEC 2)
                domain = email.rsplit("@", 1)[1]
                for a in store.select("accounts", {"domain": domain, "status": list(ENROLLED)}):
                    out.setdefault(a["account_id"], {"account": a, "contact_id": None})
                continue
        for c in ours:
            a = store.get("accounts", account_id=c.get("account_id")) if c.get("account_id") else None
            if a is not None:
                entry = out.setdefault(a["account_id"], {"account": a, "contact_id": None})
                entry["contact_id"] = entry["contact_id"] or c["contact_id"]
    return out


def _company_for(ctx: Context, account: Mapping[str, Any], company_ids: list[str]) -> str | None:
    """The account's HubSpot company: stored, else the meeting's only company, else the only one with its domain."""
    if account.get("hubspot_company_id"):
        return str(account["hubspot_company_id"])
    found = company_ids if len(company_ids) == 1 else []
    if not found:
        found = [str(c["id"]) for c in ctx.clients.hubspot.search_companies_by_domain(str(account.get("domain") or ""))]
    if len(found) != 1:
        return None
    if ctx.live:
        ctx.store.upsert("accounts", [{"account_id": account["account_id"], "hubspot_company_id": found[0]}])
    return found[0]


def _stop_leads(ctx: Context, run: _Run, account_id: str) -> None:
    """SPEC 9: the account's leads get no more steps once a meeting is booked."""
    if ctx.dry_run:
        return
    inst = ctx.clients.instantly
    for c in ctx.store.select("contacts", {"account_id": account_id}):
        if not (c.get("instantly_campaign") and c.get("instantly_lead_id") and c.get("email")):
            continue
        try:
            inst.stop_lead(str(c["instantly_campaign"]), str(c["email"]))
            run.counts["leads_stopped"] += 1
        except (ApiError, LookupError) as exc:
            run.errors.append(f"stop lead of {account_id}: {str(exc)[:160]}")


def _deal(ctx: Context, run: _Run, account: Mapping[str, Any], company_id: str | None, contact_id: str | None) -> None:
    if company_id is None:
        run.counts["no_hubspot_company"] += 1
        return
    res = ensure_deal(ctx, account, company_id, contact_id=contact_id)
    if res["created"]:
        run.counts["deals_made"] += 1
        run.note({"event": "deal_created", "account_id": account["account_id"], "id": f"{DEAL_EVENT}{res['deal_id']}"})
    elif res["why"] and not res["deal_id"]:
        run.counts[f"no deal: {res['why']}"] += 1


def meetings(ctx: Context, run: _Run) -> None:
    g, hs = ctx.settings.general, ctx.clients.hubspot
    owner = g.hubspot_owner_id.strip()
    if not owner:
        run.counts["meetings_skipped_no_owner_id"] += 1
        return
    for m in hs.search_meetings(owner, ctx.now - timedelta(days=LOOKBACK_DAYS)):
        p = m.get("properties") or {}
        if str(p.get("hs_meeting_source") or "").upper() not in BOOKING_SOURCES:
            continue
        run.counts["meetings_booked_seen"] += 1
        rec = hs.get_record("meetings", m["id"], MEETING_PROPS, associations=("contacts", "companies"))
        if rec is None:
            continue
        links = rec.get("associations") or {}
        company_ids = list(links.get("companies") or [])
        created = ts(p.get("hs_createdate")) or ctx.now
        matched = _match(ctx, company_ids, links.get("contacts") or [])
        if not matched:
            run.counts["meetings_not_ours"] += 1
            continue
        for aid, hit in matched.items():
            account, contact_id = hit["account"], hit["contact_id"]
            first = _first_enrolled(ctx, aid)
            if first is not None and created < first:
                continue  # booked before we ever wrote to them
            if first is None and account.get("status") not in ENROLLED:
                continue
            new = _event(ctx, run, f"{MEETING_EVENT}{m['id']}", "meeting_booked", aid, contact_id, created)
            if str(p.get("hs_meeting_outcome") or "").upper() in HELD_OUTCOMES:
                _event(ctx, run, f"{MEETING_EVENT}{m['id']}:held", "demo_held", aid, contact_id,
                       p.get("hs_meeting_start_time") or ctx.now)
            _status(ctx, aid, "demo_booked")
            if new:
                _deal(ctx, run, account, _company_for(ctx, account, company_ids), contact_id)
                _stop_leads(ctx, run, aid)
            log("meeting_booked", account_id=aid, meeting_id=m["id"], new=new)


# -- 2 and 3. deals --------------------------------------------------------------------------------------


def _stages(ctx: Context) -> dict[str, tuple[int, str]]:
    """Spill 3.0 stage id -> (board position, lower-case label)."""
    pipeline = ctx.settings.general.hubspot_pipeline_id.strip()
    if not pipeline:
        return {}
    return {str(s["id"]): (int(s.get("displayOrder") or 0), str(s.get("label") or "").strip().lower())
            for s in ctx.clients.hubspot.pipeline_stages(pipeline)}


def deals(ctx: Context, run: _Run) -> None:
    g, hs = ctx.settings.general, ctx.clients.hubspot
    pipeline = g.hubspot_pipeline_id.strip()
    if not pipeline:
        run.counts["deals_skipped_no_pipeline_id"] += 1
        return
    stages = _stages(ctx)
    position = {label: pos for pos, label in stages.values()}
    booked_from, held_from = position.get(BOOKED_LABEL), position.get(HELD_LABEL)
    for account in ctx.store.select("accounts", {"status": list(ENROLLED)}):
        company_id = str(account.get("hubspot_company_id") or "")
        if not company_id:
            continue
        aid = account["account_id"]
        found = [d for d in hs.deals_for_company(company_id) if (d.get("properties") or {}).get("pipeline") == pipeline]
        for d in found:
            p = d.get("properties") or {}
            _event(ctx, run, f"{DEAL_EVENT}{d['id']}", "deal_created", aid, None, p.get("createdate"))
            pos, label = stages.get(str(p.get("dealstage") or ""), (None, ""))
            if pos is None or label == LOST_LABEL:
                continue
            if booked_from is not None and pos >= booked_from:
                if not _has(ctx, aid, "meeting_booked"):
                    _event(ctx, run, f"{DEAL_EVENT}{d['id']}:booked", "meeting_booked", aid, None, ctx.now)
                _status(ctx, aid, "demo_booked")
            if held_from is not None and pos >= held_from and not _has(ctx, aid, "demo_held"):
                _event(ctx, run, f"{DEAL_EVENT}{d['id']}:held", "demo_held", aid, None, ctx.now)
        if found:
            _status(ctx, aid, "demo_requested")
        elif account.get("status") in ("demo_requested", "demo_booked") and not _has(ctx, aid, "deal_created"):
            _deal(ctx, run, account, company_id, None)


def hubspot_readback(ctx: Context) -> dict:
    """The hubspot_readback job (JOB CONTRACT: run(ctx) -> summary)."""
    run = _Run()
    for step in (meetings, deals):
        try:
            step(ctx, run)
        except ApiError as exc:  # the other step still runs; the next run reads the same window again
            run.errors.append(f"{step.__name__}: {str(exc)[:200]}")
    summary = {"job": JOB, "dry_run": ctx.dry_run, "counts": dict(run.counts), "events": run.events,
               "errors": run.errors}
    log("hubspot_readback_done", run_id=ctx.run_id, counts=dict(run.counts), errors=run.errors)
    return summary
