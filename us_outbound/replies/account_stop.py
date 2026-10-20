"""The account-level stop (Harry, 6 Oct 2026): when anyone at an account replies, bounces, unsubscribes or
complains, every sequence at the account stops, not only theirs.

With one contact per account this is the contact's own stop. An account with two leads in its sender's campaign (a
second contact, enrol/second.py, removed with W2-1, 20 Oct 2026, before any was enrolled) would have had, and the
sweep keeps covering:
  * a reply: Instantly stops the replying lead, and its campaigns stop "for the company" too (stop_for_company,
    clients/instantly.CAMPAIGN_SETTINGS), which covers the colleague's lead. Not covered: a reply Instantly ties
    to no lead, or takes for an auto-reply (stop_on_auto_reply is off). poll_replies flags those on the reply
    item (sequence_may_continue), and the account becomes engaged;
  * a bounce: sync_outcomes suppresses that address and marks that contact; Instantly stops only that lead;
  * an unsubscribe (Instantly's link, or a reply asking to stop): replies/optout.py suppresses that address, puts
    it on the blocklist and opts it out in HubSpot; Instantly stops only that lead;
  * a spam complaint (an events row of type complained; learn/kill_rules.py): nothing stops the colleague.
So sync_outcomes runs this sweep after it has read Instantly, every 15 minutes: for each account with a stop,
each other contact whose lead is in a US Outbound campaign, and that has no stop of its own and was not stopped
here before, is stopped through the guarded Instantly calls: paused (Instantly.set_lead_paused) once phase 0
confirms Instantly's lead pause (clients/instantly.LEAD_PAUSE_CONFIRMED), and until then deleted from its
campaign (Instantly.delete_lead, which first checks the lead is that campaign's), the stop that is certain.
The guard allows either only on a "US Outbound –" campaign and only live. PHASE0-CONFIRM: a deleted lead
leaves Instantly before SPEC 13's 31 days, so sync_outcomes can no longer read a later click on the unsubscribe
link in an email the colleague already has; Instantly's own unsubscribe list should still hold it.

A stop is a replied event with a class other than out_of_office (positive, referral, objection, not_now,
negative, wrong_person, unsubscribe, other), a bounced, unsubscribed or complained event. A reply not yet
classified waits for poll_replies' class, as it may be an out-of-office one. Each stop made is an events row of
type lead_stopped (event_id "lead_stopped:{contact_id}"), so it is made once and the send forecast frees the
lead's later steps (enrol/capacity.STOP_EVENTS); a failed call is tried again by the next run.

Per address (Harry, 6 Oct 2026): an unsubscribe or bounce suppresses only the address it came from. The
colleague's emails stop, but they are not suppressed, not blocklisted and not opted out in HubSpot. The
contact whose own event it was is never touched here: Instantly has stopped their lead already.

With one enrolled contact per account there is never a colleague's lead to stop, and a reply, bounce or unsubscribe
makes the sweep call nothing; it still runs, for the suppressed-in-flight stop below and for any account with two.

Suppressed while in flight (Harry, 7 Oct 2026): only enrol checked suppression, so a person who opted out in
HubSpot after they were enrolled (suppression_load, nightly), or a company whose domain was suppressed since (a
customer, a Suppress signal), still got the rest of their sequence. So the sweep also stops every contact in
flight (enrol/capacity.in_flight: a step still to send) whose own address, email domain or company domain is now
suppressed (enrol.suppressed_for, the check enrol makes), and with them everyone else at the account, as for a
reply. Its lead_stopped rows carry source "suppression". A contact whose own row is marked suppressed is left to
its own path: an Instantly unsubscribe or bounce, which Instantly has stopped already, and whose opt-out
replies/optout.py retries from the lead's status until it is recorded (deleting the lead would end that). Safety:
never held for a cohort, a config version or anything else, in any campaign.

A stop the guard refuses (W1-2, 19 Oct 2026; code-replies-crm L2): Instantly.delete_lead refuses a lead that is not
in the campaign stored for the contact. Until then the GuardViolation failed sync_outcomes, a safety job, after its
writes and again every 15 minutes, as the marker was never written. Now the marker is written with source
guard_refused, the contact is listed once under the summary's refused, and the next run makes no call for them.
The lead was due to stop, sits in a campaign other than the stored one and may still be sending, and the marker
means no later run tries again, so a person must stop it in Instantly: tell_refused posts it to the alert channel,
once per contact, from every run until the post goes (the whole-wave review, 20 Oct 2026). One errors line in the
refusing run (W1-2 review) was seen only when that run was the latest at heartbeat_check's :05, about 1 time in 4.
While the post fails, each run also puts it under errors, for heartbeat_check. Before W1-2, the failed run said so.

Dry-run: it counts what it would stop, calls nothing and writes nothing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from us_outbound.base import notify, say
from us_outbound.clients import instantly as instantly_client
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX, GuardViolation
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import capacity, enrol
from us_outbound.logs import hash_email, log

STOPPED = "lead_stopped"  # events.type (enrol/capacity.STOP_EVENTS reads it)
STOP_TYPES = ("replied", "bounced", "unsubscribed", "complained")  # events.type of a stop (the module docstring)
OUT_OF_OFFICE = "out_of_office"  # a reply of this class stops nothing
SUPPRESSED = "suppressed"  # the cause of a stop for an address or domain suppressed while the lead was in flight
SUPPRESSION_SOURCE = "suppression"  # events.source on the lead_stopped rows it makes
# events.source on a lead_stopped row whose stop the guard refused (W1-2, 19 Oct 2026; code-replies-crm L2): the
# lead is not in the campaign stored for it, so no call of ours can stop it; the summary's refused lists it once.
REFUSED_SOURCE = "guard_refused"
REFUSED_ALERT = "account_stop_refused"  # its alert's post_once key, with the contact id (tell_refused)
REFUSED_HEAD = f"{say.URGENT} *A sequence that should have stopped may still be sending*"
ID_CHUNK = 1000
LIST_LIMIT = 50


def is_stop(event: Mapping[str, Any], *, classified_only: bool = False) -> bool:
    """Whether this events row stops every sequence at its account. classified_only: an unclassified reply
    (poll_replies has not read it yet) waits for its class, as it may be an out-of-office one."""
    t = event.get("type")
    if t not in STOP_TYPES:
        return False
    if t != "replied":
        return True
    cls = event.get("reply_class")
    if not cls:
        return not classified_only
    return cls != OUT_OF_OFFICE


def marker(contact_id: str) -> str:
    return f"{STOPPED}:{contact_id}"


def _stop(ctx: Context, campaign: str, lead_id: str) -> str:
    """Stop one lead through the guarded calls: "paused" or "deleted"."""
    inst = ctx.clients.instantly
    if instantly_client.LEAD_PAUSE_CONFIRMED:
        inst.set_lead_paused(campaign, lead_id, True)
        return "paused"
    inst.delete_lead(campaign, lead_id)
    return "deleted"


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def suppressed_in_flight(ctx: Context) -> list[dict]:
    """Contacts in flight, not marked suppressed themselves, whose address, email domain or company domain is
    suppressed now (the module docstring)."""
    flying = [c for c in capacity.in_flight(ctx.store, ctx.settings, ctx.now_et().date())
              if c.get("account_id") and not c.get("suppressed")]
    if not flying:
        return []
    ids = sorted({str(c["account_id"]) for c in flying})
    domain_of = {str(a["account_id"]): _lower(a.get("domain")) for i in range(0, len(ids), ID_CHUNK)
                 for a in ctx.store.select("accounts", {"account_id": ids[i : i + ID_CHUNK]})}

    def sha(c: dict) -> str:
        return _lower(c.get("email_sha256")) or (hash_email(str(c["email"])) if c.get("email") else "")

    def domains(c: dict) -> set[str]:
        email = _lower(c.get("email"))
        return {d for d in (email.rpartition("@")[2] if "@" in email else "", domain_of.get(str(c["account_id"]))) if d}

    found_domains, found_hashes = enrol.suppressed_for(ctx, {d for c in flying for d in domains(c)},
                                                       {sha(c) for c in flying} - {""})
    return [c for c in flying if sha(c) in found_hashes or domains(c) & found_domains]


def to_stop(ctx: Context) -> list[tuple[dict, dict]]:
    """[(contact, the stop that ends their lead)]: other contacts at an account with a stop, whose lead is in a US
    Outbound campaign, with no stop of their own and not stopped here before; and contacts suppressed while in
    flight, with everyone else at their account."""
    store = ctx.store
    events = store.select("events", {"type": list(STOP_TYPES)})
    stops = [e for e in events if is_stop(e, classified_only=True)]
    suppressed = suppressed_in_flight(ctx)
    if not stops and not suppressed:
        return []
    done = {str(e["event_id"]) for e in store.select("events", {"type": STOPPED})}
    # Whoever replied, bounced, unsubscribed or complained themselves (a reply not classified yet too): Instantly
    # stopped their own lead, which is left as it is.
    own = {str(e.get("contact_id")) for e in events if e.get("contact_id") and is_stop(e)}
    by_contact: dict[str, dict] = {}
    contact_ids = sorted(own)
    for i in range(0, len(contact_ids), ID_CHUNK):
        for c in store.select("contacts", {"contact_id": contact_ids[i : i + ID_CHUNK]}):
            by_contact[str(c["contact_id"])] = c
    first_stop: dict[str, dict] = {}
    for e in sorted(stops, key=lambda e: (str(e.get("occurred_at") or ""), str(e.get("event_id")))):
        who = by_contact.get(str(e.get("contact_id"))) or {}
        aid = str(e.get("account_id") or who.get("account_id") or "")
        if aid:
            first_stop.setdefault(aid, e)
    for c in suppressed:  # their own lead is not in `own`, so it is stopped with their colleagues'
        first_stop.setdefault(str(c["account_id"]), {"type": SUPPRESSED, "contact_id": c["contact_id"],
                                                     "account_id": c["account_id"], "source": SUPPRESSION_SOURCE})
    out: list[tuple[dict, dict]] = []
    ids = sorted(first_stop)
    for i in range(0, len(ids), ID_CHUNK):
        for c in store.select("contacts", {"account_id": ids[i : i + ID_CHUNK]}):
            cid = str(c["contact_id"])
            campaign = str(c.get("instantly_campaign") or "")
            if (cid in own or marker(cid) in done or not c.get("instantly_lead_id")
                    or not campaign.startswith(US_CAMPAIGN_PREFIX)):
                continue
            out.append((c, first_stop[str(c["account_id"])]))
    return out


def sweep(ctx: Context) -> dict:
    """Stop the leads of everyone else at an account where someone replied, bounced, unsubscribed or complained, and
    of everyone at an account where a contact in flight was suppressed."""
    pending = to_stop(ctx)
    out: dict[str, Any] = {"to_stop": len(pending), "stopped": Counter(), "errors": [], "refused": []}
    if ctx.dry_run:
        out["would_stop"] = [{"account_id": c.get("account_id"), "contact_id": c["contact_id"],
                              "because": e.get("type")} for c, e in pending[:LIST_LIMIT]]
        out["stopped"] = {}
        return out
    for c, e in pending:
        cid, campaign, lead_id = str(c["contact_id"]), str(c["instantly_campaign"]), str(c["instantly_lead_id"])
        refused = ""
        try:
            how = _stop(ctx, campaign, lead_id)
        except GuardViolation as exc:  # the lead is not in its stored campaign: no call can stop it (the docstring)
            how, refused = REFUSED_SOURCE, str(exc)[:160]
            if len(out["refused"]) < LIST_LIMIT:
                out["refused"].append({"contact_id": cid, "account_id": c.get("account_id"), "why": refused})
            log("account_stop_refused", run_id=ctx.run_id, contact_id=cid, error=refused)
        except (ApiError, LookupError, ConfigError) as exc:  # tried again by the next run
            if len(out["errors"]) < LIST_LIMIT:
                out["errors"].append(f"{cid}: {type(exc).__name__}: {str(exc)[:160]}")
            log("account_stop_failed", run_id=ctx.run_id, contact_id=cid, error=str(exc)[:200])
            continue
        row = {"event_id": marker(cid), "contact_id": cid, "account_id": c.get("account_id"), "type": STOPPED,
               "mailbox": c.get("mailbox"), "occurred_at": ctx.now}
        if refused:
            row["source"] = REFUSED_SOURCE
        elif e.get("type") == SUPPRESSED:
            row["source"] = SUPPRESSION_SOURCE
        ctx.store.upsert("events", [row])
        if refused:
            continue
        out["stopped"][how] += 1
        log("account_stop", run_id=ctx.run_id, contact_id=cid, account_id=c.get("account_id"), how=how,
            because=e.get("type"), because_event=e.get("event_id"))
    out["stopped"] = dict(out["stopped"])
    tell = tell_refused(ctx)
    if tell["told"] or tell["unsent"]:
        out["refused_alert"] = tell
    for cid in tell["unsent"][: max(0, LIST_LIMIT - len(out["errors"]))]:  # Slack failed: heartbeat_check sees it too
        out["errors"].append(f"{cid}: not stopped, the guard refused it; its lead may still be sending: stop it by "
                             f"hand in Instantly (the Slack alert failed and is tried again next run)")
    return out


def refused_key(contact_id: str) -> str:
    """The post_once key of a refused stop's alert: once per contact, for good, as the marker is."""
    return f"{REFUSED_ALERT}:{contact_id}"


def tell_refused(ctx: Context) -> dict[str, list[str]]:
    """Post each refused stop (a lead_stopped marker with source guard_refused) not posted before, every run until the
    post goes (the whole-wave review, 20 Oct 2026). Before, the refusing run's one errors line was the only signal:
    heartbeat_check reads only each job's latest run, and sync_outcomes runs four times an hour, so a later clean run
    hid it 3 times in 4, and the marker meant no run tried again, so a person who had opted out could go on getting
    emails with nobody told. The alert is keyed on the contact (notify.post_once), so it is posted once, and a Slack
    failure leaves it for the next run, which also puts it under errors for heartbeat_check."""
    rows = ctx.store.select("events", {"type": STOPPED, "source": REFUSED_SOURCE})
    ids = sorted({str(r["contact_id"]) for r in rows if r.get("contact_id")})
    keys = {refused_key(cid): cid for cid in ids}
    done = notify.sent_before(ctx, keys)
    new = [cid for k, cid in keys.items() if k not in done]
    if not new:
        return {"told": [], "unsent": []}
    contacts = {str(c["contact_id"]): c for c in ctx.store.select("contacts", {"contact_id": new})}
    aids = sorted({str(c.get("account_id")) for c in contacts.values() if c.get("account_id")})
    domain = {str(a["account_id"]): str(a.get("domain") or "")
              for a in (ctx.store.select("accounts", {"account_id": aids}) if aids else [])}

    def line(cid: str) -> str:
        c = contacts.get(cid, {})
        who = " ".join(str(c.get(f) or "") for f in ("first_name", "last_name")).strip() or "a contact"
        at = domain.get(str(c.get("account_id") or ""), "") or "their company"
        return (f"• {who} at {at} (contact {cid}): their sequence was due to stop, but the lead is not in its stored "
                f"campaign ({c.get('instantly_campaign') or 'none stored'}), so it may still be sending. Find the lead "
                f"in Instantly and delete it; no run will try again.")

    sent = notify.post_once(ctx, [(refused_key(cid), line(cid)) for cid in new], head=REFUSED_HEAD)
    return {"told": [keys[k] for k in sent["keys"]], "unsent": [keys[k] for k in sent["unsent"]]}

