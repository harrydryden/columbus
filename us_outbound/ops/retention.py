"""retention, daily at 00:40 UK (SPEC 6 "Retention and erasure", SPEC 13): what we hold leaves when its time is up.

SPEC 9 gave sync_outcomes "deletes leads 31 days after their last step"; sync_outcomes runs every 15 minutes now
(replies/outcomes.py), so the deletion is this daily job of its own, at a quiet hour: no Instantly send window
(09:00 to 16:00 US Eastern) is open, and no daily job is running.

Instantly leads, 31 days after their last step (SPEC 13: "Leads stay in Instantly for 31 days after their last
step"). Each contact whose lead is still in a "US Outbound –" campaign is due once its last step is more than
LEAD_DAYS behind (US Eastern days). Its last step is dated as the send forecast dates it (enrol/capacity.step_days:
the four steps from enrolled_at, in US Eastern time, each on the next send day when its delay ends on a weekend or a
blackout date), with two corrections:
  * a stop of its own ends it sooner: a classified reply that is not an out-of-office one, a bounce, an unsubscribe,
    a spam complaint, the account-level stop of a colleague's lead (lead_stopped, replies/account_stop.py), or a
    booking at its company (meeting_booked, demo_held; crm/readback.py stops the company's leads). Only a stop on
    or after the day the contact was enrolled counts;
  * a send recorded later than that (contacts.last_step_at, which sync_outcomes keeps) moves it later: Instantly
    sends late after a stop and a start, or a kill-rule hold, and the forecast cannot know.
It is never deleted while:
  * it is in flight (enrol/capacity.in_flight: a step still to send by the forecast);
  * Instantly still lists it as active or paused, it has no stop, and sync_outcomes has recorded fewer sends than
    the sequence has steps: a campaign paused for longer than the forecast allows still holds its later steps;
  * Instantly lists it as unsubscribed and the opt-out is not recorded yet (no events row
    "unsubscribed:lead:{lead id}"), or the contact asked to stop in a reply whose opt-out is not recorded yet
    ("unsubscribed:{email id}"): replies/optout.py retries from the lead's status, and from the reply read back
    from Instantly, until it is, and deleting the lead would end that;
  * Instantly lists it as bounced and the bounce is not recorded yet ("bounced:{lead id}"): sync_outcomes'
    next lead read suppresses the address first.
A lead stopped by the account-level stop was deleted from its campaign then (until Instantly's lead pause is
confirmed, clients/instantly.LEAD_PAUSE_CONFIRMED), so 31 days after its lead_stopped row Instantly answers 404,
which counts as deleted already; once the pause is confirmed, the paused lead is deleted then like any other.
Each deletion is the guarded Instantly.delete_lead, scoped to the lead's own "US Outbound –" campaign (it checks the
lead is that campaign's first). The contact keeps everything else and records it: lead_deleted_at is set and
instantly_lead_id cleared, so the deletion is made once, and everything that reads instantly_lead_id (the
account-level stop, a booking's stop, a reply's pause, unenrol, erase, the in-flight list) treats the contact as
having no lead, which it no longer has. Its enrolment (enrolled_at, enrolment_month, instantly_campaign) stays, so
the send forecast, the second-contact rules and the cohorts read it as before. At most LEADS_PER_RUN leads a run,
oldest first; the rest are due again the next day.

Reply text, after 90 days (SPEC 6: "Reply text: purged after 90 days"). The prospect's words are kept in two
places in the database, and both go REPLY_TEXT_DAYS after the reply:
  * events.reply_text, on the replied row (poll_replies keeps up to 5,000 characters): set to NULL;
  * the reply card (hitl_items of kind reply, or reply_approval, its first name, and out_of_office): in its payload,
    the reply's words and everything written from them (REPLY_TEXT_KEYS: the excerpt, the classifier's summary, a
    referral's name, title and email, the draft, its first wording, a refused draft and its problems, and the text
    sent) are set to null, and reply_text_purged_at says when, so the card is read once.
What stays is what the readout and the reply desk count by: the class and the classifier's other fields (objection,
demo_requested, dates, language_terms, competitor_named), the step, the mailbox, the approval, every date and id.
Copies outside the database are not ours to purge here: the Slack cards (#us-outbound's own retention), the
escalation emails in Harry's inbox, the HubSpot note of a positive or referral reply (a warm lead's record in the
CRM), and Instantly's own thread, which leaves Instantly with its lead (PHASE0-CONFIRM, ops/erase.py).

Suppression is never touched: it is kept indefinitely, as hashes (SPEC 6).

Dry-run (the default; live needs --live and live_sending = yes, as for every job): it reads Instantly's lead
lists, counts what is due and what is held, and changes nothing, in Instantly or in the database. A deletion
cannot be undone, so unlike other jobs (SPEC 0.3) its database writes wait for live too.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import LEAD_ACTIVE, LEAD_BOUNCED, LEAD_PAUSED, LEAD_UNSUBSCRIBED, STEP_DAYS
from us_outbound.context import ET, ConfigError, Context
from us_outbound.enrol import capacity
from us_outbound.logs import log
from us_outbound.replies import optout
from us_outbound.replies.items import REPLY_KINDS
from us_outbound.replies.poll import OOO_KIND

JOB = "retention"
LEAD_DAYS = 31  # SPEC 13: leads stay in Instantly this long after their last step
REPLY_TEXT_DAYS = 90  # SPEC 6: reply text is purged after this
# Two Instantly requests a lead (delete_lead checks the lead's campaign first). The pilot adds about 650 leads a
# month, some 30 a send day, so a backlog of a few hundred clears within days, well inside the run's timeout.
LEADS_PER_RUN = 200
ID_CHUNK = 1000
LIST_LIMIT = 50  # errors kept in the summary
OUT_OF_OFFICE = "out_of_office"
# A contact's own events that end its emails (each written once Instantly stopped the lead, or Instantly's own stop).
OWN_STOPS = ("replied", "bounced", "unsubscribed", "complained", "lead_stopped")
# A booking at the company stops every lead there (crm/readback.py).
ACCOUNT_STOPS = ("meeting_booked", "demo_held")
NOT_FINISHED = frozenset({LEAD_ACTIVE, LEAD_PAUSED})  # Instantly may still send a lead in these statuses
REPLY_ITEM_KINDS = (*REPLY_KINDS, OOO_KIND)  # the hitl_items that quote a reply (replies/poll.py)
# A reply card's payload keys that hold the prospect's words, or text written from them (replies/poll.py, desk.py).
REPLY_TEXT_KEYS = ("reply_excerpt", "summary", "referral", "draft", "draft_original", "draft_rejected", "draft_problems",
                   "sent_text")
PURGED = "reply_text_purged_at"  # set on a card's payload once its text is gone
EVENTS_TEXT_SQL = "SELECT event_id FROM {schema}.events WHERE reply_text IS NOT NULL AND occurred_at < %(before)s"
ITEMS_TEXT_SQL = (
    "SELECT item_id, payload FROM {schema}.hitl_items WHERE kind = ANY(%(kinds)s) AND created_at < %(before)s"
    " AND payload IS NOT NULL AND payload ->> 'reply_text_purged_at' IS NULL"
)

# Why a due lead waits (the summary's leads.held).
IN_FLIGHT = "in flight"
UNDATED = "no enrolment or send to date it by"
UNREADABLE = "its campaign's leads could not be read"
STEPS_LEFT = "Instantly lists it as active or paused, with steps not recorded as sent"
OPT_OUT_PENDING = "an opt-out not recorded yet"
BOUNCE_PENDING = "a bounce not recorded yet"


# -- small helpers -------------------------------------------------------------------------------------------------


def _ts(v: Any) -> datetime | None:
    if isinstance(v, str) and v:
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return None


def _et_day(v: Any) -> date | None:
    t = _ts(v)
    return t.astimezone(ET).date() if t is not None else None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _chunks(items: Sequence[str], n: int = ID_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _select_in(ctx: Context, table: str, column: str, values: Iterable[Any], where: Mapping[str, Any] | None = None
               ) -> list[dict]:
    ids = sorted({str(v) for v in values if v})
    return [r for chunk in _chunks(ids) for r in ctx.store.select(table, {**(where or {}), column: list(chunk)})]


def _ends_emails(event: Mapping[str, Any]) -> bool:
    """Whether this stop event ended the emails: a reply only once classified, and never an out-of-office one
    (stop_on_auto_reply is off, so Instantly keeps sending; a reply not classified yet may be one)."""
    if event.get("type") != "replied":
        return True
    cls = event.get("reply_class")
    return bool(cls) and cls != OUT_OF_OFFICE


# -- the last step ---------------------------------------------------------------------------------------------------


class Ends:
    """When each contact's emails ended, read once a run: its stops, its company's bookings, the forecast."""

    def __init__(self, ctx: Context):
        self.settings = ctx.settings
        events = ctx.store.select("events", {"type": [*OWN_STOPS, *ACCOUNT_STOPS]})
        self.own: dict[str, list[date]] = {}
        self.company: dict[str, list[date]] = {}
        for e in events:
            day = _et_day(e.get("occurred_at"))
            if day is None or not _ends_emails(e):
                continue
            if e.get("type") in ACCOUNT_STOPS:
                if e.get("account_id"):
                    self.company.setdefault(str(e["account_id"]), []).append(day)
            elif e.get("contact_id"):
                self.own.setdefault(str(e["contact_id"]), []).append(day)

    def stop(self, contact: Mapping[str, Any]) -> date | None:
        """The first day a stop ended the contact's emails, on or after the day it was enrolled; None if none did."""
        enrolled = _et_day(contact.get("enrolled_at"))
        days = [*self.own.get(str(contact.get("contact_id")), ()), *self.company.get(str(contact.get("account_id")), ())]
        days = [d for d in days if enrolled is None or d >= enrolled]
        return min(days) if days else None

    def last_step(self, contact: Mapping[str, Any]) -> date | None:
        """The US Eastern day of the contact's last step (the module docstring); None when nothing dates it."""
        sent, stop = _et_day(contact.get("last_step_at")), self.stop(contact)
        start = _et_day(contact.get("enrolled_at"))
        steps = capacity.step_days(start, self.settings) if start is not None else []
        end = min(steps[-1], stop) if steps and stop is not None else (steps[-1] if steps else stop)
        if end is None:
            return sent
        return max(end, sent) if sent is not None else end


# -- 1. Instantly leads, 31 days after their last step (SPEC 13) -----------------------------------------------------


def _recorded(ctx: Context) -> tuple[set[str], set[str], set[str]]:
    """(opt-out markers recorded, bounce markers recorded, contacts with an opt-out by reply not yet recorded)."""
    done = optout.done_markers(ctx.store)
    bounced = {str(e["event_id"]) for e in ctx.store.select("events", {"type": "bounced"})}
    asked = ctx.store.select("events", {"type": "replied", "reply_class": "unsubscribe"})
    pending = {str(e.get("contact_id")) for e in asked
               if e.get("contact_id") and optout.reply_marker(str(e["event_id"])) not in done}
    return done, bounced, pending


def _sends(ctx: Context, contact_ids: Iterable[str]) -> Counter[str]:
    """contact id -> its campaign steps sync_outcomes recorded (a sent row with an approval is an old desk reply)."""
    return Counter(str(e["contact_id"]) for e in _select_in(ctx, "events", "contact_id", contact_ids, {"type": "sent"})
                   if not e.get("approval"))


def _lead_hold(contact: Mapping[str, Any], lead: Mapping[str, Any] | None, stopped: bool, sends: int,
               recorded: tuple[set[str], set[str], set[str]]) -> str:
    """Why this due lead must wait, from what Instantly lists (None: not in its campaign's list); "" if it need not."""
    done, bounced, pending = recorded
    lead_id = str(contact.get("instantly_lead_id"))
    if str(contact.get("contact_id")) in pending:
        return OPT_OUT_PENDING
    if lead is None:
        return ""
    status = _int(lead.get("status"))
    if status == LEAD_UNSUBSCRIBED and optout.lead_marker(lead_id) not in done:
        return OPT_OUT_PENDING
    if status == LEAD_BOUNCED and f"bounced:{lead_id}" not in bounced:
        return BOUNCE_PENDING
    if status in NOT_FINISHED and not stopped and sends < len(STEP_DAYS):
        return STEPS_LEFT
    return ""


def due_leads(ctx: Context, ends: Ends, today: date) -> tuple[list[dict], Counter[str]]:
    """(the contacts whose lead is due for deletion, oldest last step first; the due ones held back, by reason)."""
    held: Counter[str] = Counter()
    flying = {str(c["contact_id"]) for c in capacity.in_flight(ctx.store, ctx.settings, today)}
    due: list[tuple[date, str, dict]] = []
    for c in ctx.store.select("contacts"):
        if not c.get("instantly_lead_id") or not str(c.get("instantly_campaign") or "").startswith(US_CAMPAIGN_PREFIX):
            continue
        end = ends.last_step(c)
        if end is None:
            held[UNDATED] += 1
            continue
        if (today - end).days <= LEAD_DAYS:
            continue
        if str(c["contact_id"]) in flying:
            held[IN_FLIGHT] += 1
            continue
        due.append((end, str(c["contact_id"]), c))
    return [c for _, _, c in sorted(due, key=lambda x: (x[0], x[1]))], held


def _record_deleted(ctx: Context, contact: Mapping[str, Any]) -> None:
    ctx.store.update("contacts", {"contact_id": contact["contact_id"]},
                     {"instantly_lead_id": None, "lead_deleted_at": ctx.now})


def delete_leads(ctx: Context, ends: Ends, today: date, errors: list[str]) -> dict:
    """Delete from Instantly each lead more than LEAD_DAYS past its last step (the module docstring)."""
    candidates, held = due_leads(ctx, ends, today)
    out: dict[str, Any] = {"due": 0, "deleted": 0, "already_gone": 0, "held": held, "left_for_next_run": 0}
    if not candidates:
        out["held"] = dict(held)
        return out
    inst = ctx.clients.instantly
    listed: dict[str, dict[str, dict] | None] = {}
    for name in sorted({str(c["instantly_campaign"]) for c in candidates}):
        try:
            listed[name] = {str(lead.get("id")): lead for lead in inst.list_leads(name)}
        except (ApiError, LookupError, ConfigError) as exc:  # tried again the next day
            listed[name] = None
            errors.append(f"{name}: its leads could not be read: {type(exc).__name__}: {str(exc)[:160]}")
    recorded = _recorded(ctx)
    sends = _sends(ctx, (c["contact_id"] for c in candidates))
    ready: list[tuple[dict, bool]] = []  # (contact, Instantly listed its lead)
    for c in candidates:
        leads = listed[str(c["instantly_campaign"])]
        if leads is None:
            held[UNREADABLE] += 1
            continue
        lead = leads.get(str(c["instantly_lead_id"]))
        why = _lead_hold(c, lead, ends.stop(c) is not None, sends[str(c["contact_id"])], recorded)
        if why:
            held[why] += 1
            continue
        ready.append((c, lead is not None))
    out.update(due=len(ready), held=dict(held), left_for_next_run=max(0, len(ready) - LEADS_PER_RUN))
    if ctx.dry_run:
        return out
    for c, was_listed in ready[:LEADS_PER_RUN]:
        cid, campaign, lead_id = str(c["contact_id"]), str(c["instantly_campaign"]), str(c["instantly_lead_id"])
        try:
            inst.delete_lead(campaign, lead_id)  # 404 on its check: gone already, and it returns
        except ApiError as exc:
            if exc.status != 404:  # 404 on the delete itself: gone between the check and the delete
                if len(errors) < LIST_LIMIT:
                    errors.append(f"{cid}: {type(exc).__name__}: {str(exc)[:160]}")
                log("retention_lead_failed", run_id=ctx.run_id, contact_id=cid, error=str(exc)[:200])
                continue
            was_listed = False
        except (LookupError, ConfigError) as exc:  # the campaign is missing or named twice: the next day
            if len(errors) < LIST_LIMIT:
                errors.append(f"{cid}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        _record_deleted(ctx, c)
        out["deleted" if was_listed else "already_gone"] += 1
        log("retention_lead_deleted", run_id=ctx.run_id, contact_id=cid, account_id=c.get("account_id"),
            campaign=campaign, lead_id=lead_id, listed=was_listed)
    return out


# -- 2. Reply text, after 90 days (SPEC 6) ---------------------------------------------------------------------------


def _old_reply_texts(ctx: Context, before: datetime) -> tuple[list[str], list[dict]]:
    """(events whose reply_text is older than `before`, reply cards created before it not yet purged)."""
    store = ctx.store
    try:
        events = [str(r["event_id"]) for r in store.query(EVENTS_TEXT_SQL.format(schema=store.schema),
                                                          {"before": before})]
        items = store.query(ITEMS_TEXT_SQL.format(schema=store.schema), {"before": before,
                                                                         "kinds": list(REPLY_ITEM_KINDS)})
    except NotImplementedError:  # MemoryStore without a handler: the same in Python
        events = [str(e["event_id"]) for e in store.select("events")
                  if e.get("reply_text") is not None and (t := _ts(e.get("occurred_at"))) is not None and t < before]
        items = [i for i in store.select("hitl_items", {"kind": list(REPLY_ITEM_KINDS)})
                 if isinstance(i.get("payload"), Mapping) and PURGED not in i["payload"]
                 and (t := _ts(i.get("created_at"))) is not None and t < before]
    return sorted(events), [i for i in items if isinstance(i.get("payload"), Mapping)]


def _has_text(v: Any) -> bool:
    return bool(v) if not isinstance(v, str) else bool(v.strip())


def purge_reply_text(ctx: Context) -> dict:
    """Clear the prospect's words from events and the reply cards REPLY_TEXT_DAYS after the reply (SPEC 6)."""
    events, items = _old_reply_texts(ctx, ctx.now - timedelta(days=REPLY_TEXT_DAYS))
    with_text = [i for i in items if any(_has_text(i["payload"].get(k)) for k in REPLY_TEXT_KEYS)]
    out = {"events_due": len(events), "events_cleared": 0, "items_due": len(with_text), "items_cleared": 0}
    if ctx.dry_run:
        return out
    for chunk in _chunks(events):
        out["events_cleared"] += ctx.store.update("events", {"event_id": list(chunk)}, {"reply_text": None})
    for i in items:  # each card once: those with nothing left to clear are only marked
        p = dict(i["payload"])
        p.update({k: None for k in REPLY_TEXT_KEYS if k in p}, **{PURGED: ctx.now.isoformat()})
        ctx.store.update("hitl_items", {"item_id": i["item_id"]}, {"payload": p})
    out["items_cleared"] = len(with_text)
    if events or with_text:
        log("retention_reply_text_purged", run_id=ctx.run_id, events=out["events_cleared"], items=len(with_text))
    return out


# -- the job ---------------------------------------------------------------------------------------------------------


def run(ctx: Context) -> dict:
    """The retention job (JOB CONTRACT: run(ctx) -> summary)."""
    today = ctx.now_et().date()
    errors: list[str] = []
    ends = Ends(ctx)
    out: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run}
    out["leads"] = delete_leads(ctx, ends, today, errors)
    out["reply_text"] = purge_reply_text(ctx)
    out["errors"] = errors
    log("retention_done", run_id=ctx.run_id, **out)
    return out
