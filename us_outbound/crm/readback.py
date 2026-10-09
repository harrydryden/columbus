"""hubspot_readback, every 15 minutes (SPEC 9; SPEC 11 "HubSpot writes" step 5; SPEC 12 meetings booked).

  1. Meetings. Harry's meetings created or changed in the last LOOKBACK_DAYS that were booked through
     his meetings link (the emails' link and the website's booking page both book into it; such a
     meeting has hs_meeting_source MEETINGS_PUBLIC: confirmed 7 Oct 2026, every meeting booked through a
     meetings page since 7 Sep is, and none is MEETINGS_EMBEDDED) are matched to US
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
     Closed lost deal counts for neither. The stage labels are docs/phase0-facts.md's (confirmed 7 Oct 2026).
  3. An account at demo_requested or demo_booked whose company has never had a deal gets one
     (one that could not be made when the reply came, e.g. before the pipeline ids were set).
  4. Bookings (Harry, 6 Oct 2026: demo bookings read back, so the readout and the signal table count them).
     A demo is booked in two ways, and both book with Harry: the signature's "Book a call here" opens his
     HubSpot meetings link (General booking_link), which step 1 reads; the emails' call to action opens
     spill.chat/us/book-demo (General booking_page), which books into the same calendar (SPEC 4) and, through
     Spill's own website flow, may raise a Spill 3.0 deal at "Demo requested" (PHASE0-CONFIRM). Step 2 sees such
     a deal only at a company whose HubSpot id we hold, so this step searches the whole pipeline once a run: the
     deals made in the last LOOKBACK_DAYS (a demo request is new; an older deal at our company is step 2's) at
     "Demo requested" or later (not Closed lost) are matched to enrolled accounts by their companies (our stored
     id, else the company's domain) and contacts (as step 1: the id, the email's hash, else the email's domain),
     and count only when made after the account's first enrolment. Our own deal from a reply asking for a demo
     ("US Outbound – …", at Demo requested) is not a booking until it moves on. Each is recorded as
     meeting_booked (event_id "hs-deal:{id}:booked") unless the account already has one, the account moves to
     demo_requested (demo_booked from Demo created on), and its leads are stopped. A deal is looked up only
     once its stage and date make it a candidate, and not again once recorded. HubSpot is only read here; the
     guard allows no other write than SPEC 11's.
events.source says where a booking came from: hubspot_meeting (step 1) or hubspot_deal (steps 2 and 4).
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

from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.clients.hubspot import CONTACT_PROPS, DEAL_PROPS, MEETING_PROPS
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.crm.hubspot_writes import DEAL_EVENT, advance_status, ensure_deal
from us_outbound.logs import hash_email, log
from us_outbound.timeparse import utc

JOB = "hubspot_readback"
LOOKBACK_DAYS = 3  # each run re-reads three days of changes, so a missed run or two loses nothing
BOOKING_SOURCES = frozenset({"MEETINGS_PUBLIC"})  # booked through a HubSpot meetings link (confirmed 7 Oct 2026)
HELD_OUTCOMES = frozenset({"COMPLETED"})
MEETING_EVENT = "hs-meeting:"
REQUESTED_LABEL, BOOKED_LABEL, HELD_LABEL, LOST_LABEL = (  # Spill 3.0's stages (docs/phase0-facts.md)
    "demo requested", "demo created", "demo held", "closed lost")
SOURCE_MEETING, SOURCE_DEAL = "hubspot_meeting", "hubspot_deal"  # events.source of a booking
ENROLLED = ("enrolled", "engaged", "demo_requested", "demo_booked")  # accounts a booking or deal can belong to
LIST_LIMIT = 50


@dataclass
class _Run:
    counts: Counter[str] = field(default_factory=Counter)
    events: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stages: dict[str, tuple[int, str]] | None = None  # Spill 3.0's stages, read once a run

    def note(self, entry: dict) -> None:
        if len(self.events) < LIST_LIMIT:
            self.events.append(entry)


def _event(ctx: Context, run: _Run, event_id: str, type_: str, account_id: str, contact_id: str | None,
           occurred_at: Any, source: str | None = None) -> bool:
    """Record the event once; True if it is new. Dry-run only reports it. source: where a booking came from."""
    if ctx.store.get("events", event_id=event_id) is not None:
        return False
    run.counts[type_] += 1
    run.note({"event": type_, "account_id": account_id, "id": event_id, **({"source": source} if source else {})})
    if ctx.live:
        ctx.store.upsert("events", [{"event_id": event_id, "type": type_, "account_id": account_id,
                                     "contact_id": contact_id or None, "occurred_at": utc(occurred_at) or ctx.now,
                                     "source": source}])
    return True


def _has(ctx: Context, account_id: str, type_: str) -> bool:
    return bool(ctx.store.select("events", {"account_id": account_id, "type": type_}))


def _status(ctx: Context, account_id: str, status: str) -> None:
    if ctx.live:
        advance_status(ctx, account_id, status)


def _first_enrolled(ctx: Context, account_id: str) -> datetime | None:
    times = [t for c in ctx.store.select("contacts", {"account_id": account_id}) if (t := utc(c.get("enrolled_at")))]
    return min(times) if times else None


# -- 1. meetings ---------------------------------------------------------------------------------------


def _root(domain: Any) -> str:
    return str(domain or "").strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.") \
        .split("/", 1)[0]


def _match(ctx: Context, company_ids: Iterable[str], contact_ids: Iterable[str]) -> dict[str, dict]:
    """account_id -> {"account", "contact_id"} for the US Outbound accounts a meeting or deal belongs to.

    A company whose id we hold is ours; another counts when its domain is an enrolled account's (a company
    HubSpot made when the prospect booked, before any write of ours gave us its id; Harry, 6 Oct 2026)."""
    store, hs = ctx.store, ctx.clients.hubspot
    out: dict[str, dict] = {}
    companies = sorted(set(company_ids))
    if companies:
        known = store.select("accounts", {"hubspot_company_id": companies})
        for a in known:
            out.setdefault(a["account_id"], {"account": a, "contact_id": None})
        for cid in [c for c in companies if c not in {str(a.get("hubspot_company_id")) for a in known}]:
            rec = hs.get_record("companies", cid, ("domain",))
            domain = _root(((rec or {}).get("properties") or {}).get("domain"))
            if domain:
                for a in store.select("accounts", {"domain": domain, "status": list(ENROLLED)}):
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
        created = utc(p.get("hs_createdate")) or ctx.now
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
            new = _event(ctx, run, f"{MEETING_EVENT}{m['id']}", "meeting_booked", aid, contact_id, created,
                         SOURCE_MEETING)
            if str(p.get("hs_meeting_outcome") or "").upper() in HELD_OUTCOMES:
                _event(ctx, run, f"{MEETING_EVENT}{m['id']}:held", "demo_held", aid, contact_id,
                       p.get("hs_meeting_start_time") or ctx.now)
            _status(ctx, aid, "demo_booked")
            if new:
                _deal(ctx, run, account, _company_for(ctx, account, company_ids), contact_id)
                _stop_leads(ctx, run, aid)
            log("meeting_booked", account_id=aid, meeting_id=m["id"], new=new)


# -- 2 and 3. deals --------------------------------------------------------------------------------------


def _stages(ctx: Context, run: _Run) -> dict[str, tuple[int, str]]:
    """Spill 3.0 stage id -> (board position, lower-case label), read once a run."""
    pipeline = ctx.settings.general.hubspot_pipeline_id.strip()
    if not pipeline:
        return {}
    if run.stages is None:
        run.stages = {str(s["id"]): (int(s.get("displayOrder") or 0), str(s.get("label") or "").strip().lower())
                      for s in ctx.clients.hubspot.pipeline_stages(pipeline)}
    return run.stages


def deals(ctx: Context, run: _Run) -> None:
    g, hs = ctx.settings.general, ctx.clients.hubspot
    pipeline = g.hubspot_pipeline_id.strip()
    if not pipeline:
        run.counts["deals_skipped_no_pipeline_id"] += 1
        return
    stages = _stages(ctx, run)
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
                    _event(ctx, run, f"{DEAL_EVENT}{d['id']}:booked", "meeting_booked", aid, None, ctx.now, SOURCE_DEAL)
                _status(ctx, aid, "demo_booked")
            if held_from is not None and pos >= held_from and not _has(ctx, aid, "demo_held"):
                _event(ctx, run, f"{DEAL_EVENT}{d['id']}:held", "demo_held", aid, None, ctx.now)
        if found:
            _status(ctx, aid, "demo_requested")
        elif account.get("status") in ("demo_requested", "demo_booked") and not _has(ctx, aid, "deal_created"):
            _deal(ctx, run, account, company_id, None)


# -- 4. bookings ----------------------------------------------------------------------------------------


def _deal_accounts(ctx: Context, run: _Run, deal_id: str) -> tuple[dict[str, dict], list[str]]:
    """(account_id -> {"account", "contact_id"}, the deal's company ids): from the deal_created event when steps 2
    or 4 already matched it, else from its associations (one HubSpot read)."""
    known = ctx.store.get("events", event_id=f"{DEAL_EVENT}{deal_id}")
    account = (ctx.store.get("accounts", account_id=known["account_id"])
               if known and known.get("account_id") else None)
    if account is not None:
        return {account["account_id"]: {"account": account, "contact_id": known.get("contact_id")}}, []
    run.counts["demo_deals_looked_up"] += 1
    rec = ctx.clients.hubspot.get_record("deals", deal_id, DEAL_PROPS, associations=("contacts", "companies"))
    links = (rec or {}).get("associations") or {}
    company_ids = list(links.get("companies") or [])
    return _match(ctx, company_ids, links.get("contacts") or []), company_ids


def bookings(ctx: Context, run: _Run) -> None:
    """Demo requests and bookings in Spill 3.0 at companies we emailed (step 4 of the module docstring)."""
    pipeline = ctx.settings.general.hubspot_pipeline_id.strip()
    if not pipeline:
        run.counts["bookings_skipped_no_pipeline_id"] += 1
        return
    stages = _stages(ctx, run)
    position = {label: pos for pos, label in stages.values()}
    requested_from, booked_from = position.get(REQUESTED_LABEL), position.get(BOOKED_LABEL)
    if requested_from is None:
        run.counts["bookings_skipped_no_demo_requested_stage"] += 1
        return
    since = ctx.now - timedelta(days=LOOKBACK_DAYS)
    for d in ctx.clients.hubspot.search_pipeline_deals(pipeline, since):
        p = d.get("properties") or {}
        pos, label = stages.get(str(p.get("dealstage") or ""), (None, ""))
        if pos is None or label == LOST_LABEL or pos < requested_from:
            continue
        booked = booked_from is not None and pos >= booked_from
        if str(p.get("dealname") or "").startswith(US_CAMPAIGN_PREFIX) and not booked:
            continue  # ours, from a reply that asked for a demo: a booking once it reaches Demo created
        created = utc(p.get("createdate")) or ctx.now
        event_id = f"{DEAL_EVENT}{d['id']}:booked"
        if created < since or ctx.store.get("events", event_id=event_id) is not None:
            continue  # an older deal at our company is step 2's; a recorded one is done
        run.counts["demo_deals_seen"] += 1
        matched, company_ids = _deal_accounts(ctx, run, str(d["id"]))
        if not matched:
            run.counts["demo_deals_not_ours"] += 1
            continue
        for aid, hit in matched.items():
            account, contact_id = hit["account"], hit["contact_id"]
            first = _first_enrolled(ctx, aid)
            if (first is not None and created < first) or (first is None and account.get("status") not in ENROLLED):
                continue  # asked before we ever wrote to them
            _event(ctx, run, f"{DEAL_EVENT}{d['id']}", "deal_created", aid, contact_id, created)
            new = not _has(ctx, aid, "meeting_booked") and _event(
                ctx, run, event_id, "meeting_booked", aid, contact_id, created, SOURCE_DEAL)
            _status(ctx, aid, "demo_booked" if booked else "demo_requested")
            _company_for(ctx, account, company_ids)  # so step 2 follows the deal from here
            if new:
                _stop_leads(ctx, run, aid)
            log("demo_booking", account_id=aid, deal_id=d["id"], stage=label, new=new)


def hubspot_readback(ctx: Context) -> dict:
    """The hubspot_readback job (JOB CONTRACT: run(ctx) -> summary)."""
    run = _Run()
    for step in (meetings, deals, bookings):
        try:
            step(ctx, run)
        except ApiError as exc:  # the other step still runs; the next run reads the same window again
            run.errors.append(f"{step.__name__}: {str(exc)[:200]}")
    summary = {"job": JOB, "dry_run": ctx.dry_run, "counts": dict(run.counts), "events": run.events,
               "errors": run.errors}
    log("hubspot_readback_done", run_id=ctx.run_id, counts=dict(run.counts), errors=run.errors)
    return summary
