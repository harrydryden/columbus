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
    sends late after a stop and a start, or a kill-rule hold, and the forecast cannot know; and so does a later
    email in the conversation, a reply of theirs or one the reply desk sent (reply_sent), since the thread goes
    on in the lead's mailbox and the desk answers in it.
It is never deleted while:
  * it is in flight (enrol/capacity.in_flight: a step still to send by the forecast);
  * a reply of theirs waits for a person (a reply card open, escalated or sending): the desk answers in the lead's
    thread;
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
which counts as deleted already; once the pause is confirmed, a paused lead is deleted 31 days after its
lead_stopped row like any other.
PHASE0-CONFIRM: that GET /leads/{id} answers 404 for a deleted lead (delete_lead reads it first, as erase and the
account-level stop rely on too).
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

Contacts who never replied, 12 months after their last step (SPEC 6). A contact we emailed (enrolled) whose last
step, dated as above, is more than CONTACT_MONTHS behind is deleted, with the personal data kept beside it: its row,
the payloads of its hitl_items (a send approval's card holds its name, email and emails; the rows keep their kind,
status, dates and ids, as erase leaves them) and the raw_clay_contacts rows that hold its address. Kept, and never
deleted here:
  * a contact who replied: a replied event of any class but out_of_office (an away message) and unsubscribe (a
    reply that only asked to stop is an opt-out, not a conversation); a reply not classified yet counts as one;
  * every contact at a company with a booking or a deal (meeting_booked, demo_held, deal_created): HubSpot holds
    that relationship, and so do we;
  * a contact whose Instantly lead is not deleted yet (rule 1 goes first), or whose opt-out by reply is not
    recorded yet.
A contact revealed but never emailed has no last step, so this rule never reaches it (docs/open-questions.md 79).
Its events stay: after the reply text is purged they carry ids, classes, steps, our mailbox and dates, no personal
data. Its suppression hash stays, so a person who opted out or bounced is still never emailed again; one who did
neither may be found and proposed again, as SPEC 6's 12 months is also recontact_person_months. What the reports
show afterwards: the readout views count the company from its events (v_account_outcomes) with the contact's
enrolment snapshot blank, so the signal table judges it by the account's matches now; the cohort report reads
contacts, so a company left with no contact leaves its enrolment week, which then reads as replying more than it
did. Both only for companies whose last step is a year old, far beyond the readout's last week, the cohorts' last
eight weeks and any test's read date.

Universe rows not refreshed in 12 months (SPEC 6). A company row is refreshed when a source sees the company again:
source_universe's monthly pass from page 1 writes Apollo's facts for every company it finds again, as do
apollo_enrich, apollo_people, apollo_signals, the page reader, site visits and the HubSpot checks. last_scored is no
sign of it (every nightly rescore moves it), and neither are the facts the system derives from what it already holds
(DERIVED_SOURCES: scoring's matches, the lookalike fit, verify's doubts, the opener's focus phrase). So an account is
deleted, with its signal_events, when it was first seen more than UNIVERSE_MONTHS ago and no source has observed it
since, and only when nothing else holds it: no contact, no event and no hitl_items row names it, it was never
enrolled (status enrolled, engaged, demo_requested or demo_booked) or disqualified, it did not come from the Named
accounts tab, and no person's decision is kept on it (an approver's 🚫 at a send approval, declined_in_slack; Harry
naming it, named). Domain aliases, partners, suppression and the credit ledger stay. A company found again later
comes in by the front door as a new account. These are companies, not people, so the rule leans to keeping.
At most CONTACTS_PER_RUN contacts and ACCOUNTS_PER_RUN accounts a run, oldest first.

Suppression is never touched: it is kept indefinitely, as hashes (SPEC 6).

Dry-run (the default; live needs --live and live_sending = yes, as for every job): it reads Instantly's lead
lists, counts what is due and what is held, and changes nothing, in Instantly or in the database. A deletion
cannot be undone, so unlike other jobs (SPEC 0.3) its database writes wait for live too.

What it did shows in three places, as counts only: its heartbeat's summary (`us-outbound run retention` prints it),
one line in the daily post when the day's run deleted or cleared anything (post_line), and one line in
`us-outbound status` (status_line), with what is held back and why.
"""

from __future__ import annotations

import calendar
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound import parse
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import LEAD_ACTIVE, LEAD_BOUNCED, LEAD_PAUSED, LEAD_UNSUBSCRIBED, STEP_DAYS
from us_outbound.context import UK, ConfigError, Context
from us_outbound.enrol import capacity
from us_outbound.enrol.openers import FOCUS_SOURCE
from us_outbound.logs import log, normalise_email, redact
from us_outbound.replies import optout
from us_outbound.replies.items import REPLY_KINDS, WAITING
from us_outbound.replies.poll import OOO_KIND
from us_outbound.scoring.score import SCORING_SOURCE
from us_outbound.scoring.tiers import DECLINED_IN_SLACK
from us_outbound.sources import named
from us_outbound.sources.lookalikes import SOURCE as LOOKALIKE_SOURCE
from us_outbound.timeparse import et_day, utc
from us_outbound.verify import DOUBT_SOURCE

JOB = "retention"
LEAD_DAYS = 31  # SPEC 13: leads stay in Instantly this long after their last step
REPLY_TEXT_DAYS = 90  # SPEC 6: reply text is purged after this
CONTACT_MONTHS = 12  # SPEC 6: contacts who never replied are deleted this long after their last step
UNIVERSE_MONTHS = 12  # SPEC 6: universe rows not refreshed this long are deleted
# Two Instantly requests a lead (delete_lead checks the lead's campaign first). The pilot adds about 650 leads a
# month, some 30 a send day, so a backlog of a few hundred clears within days, well inside the run's timeout.
LEADS_PER_RUN = 200
CONTACTS_PER_RUN = 500  # database rows only
ACCOUNTS_PER_RUN = 2000
ID_CHUNK = 1000
LIST_LIMIT = 50  # errors kept in the summary
OUT_OF_OFFICE = "out_of_office"
# A contact's own events that end its emails (each written once Instantly stopped the lead, or Instantly's own stop).
OWN_STOPS = ("replied", "bounced", "unsubscribed", "complained", "lead_stopped")
# A booking at the company stops every lead there (crm/readback.py).
ACCOUNT_STOPS = ("meeting_booked", "demo_held")
# The conversation in the lead's thread: their replies, and the replies the desk sent (replies/desk.py).
CONVERSATION = ("replied", "reply_sent")
NOT_FINISHED = frozenset({LEAD_ACTIVE, LEAD_PAUSED})  # Instantly may still send a lead in these statuses
REPLY_ITEM_KINDS = (*REPLY_KINDS, OOO_KIND)  # the hitl_items that quote a reply (replies/poll.py)
# A reply card's payload keys that hold the prospect's words, or text written from them (replies/poll.py, desk.py).
REPLY_TEXT_KEYS = ("reply_excerpt", "summary", "referral", "draft", "draft_original", "draft_rejected",
                   "draft_problems", "sent_text")
PURGED = "reply_text_purged_at"  # set on a card's payload once its text is gone
# Not a reply, for SPEC 6's "never replied": an away message, and a reply that only asked to stop (an opt-out).
NOT_A_REPLY = frozenset({OUT_OF_OFFICE, "unsubscribe"})
WARM_EVENTS = ("meeting_booked", "demo_held", "deal_created")  # a company with one keeps its people
# Account statuses whose rows are never deleted as universe rows: our history with the company, or a decision.
KEPT_STATUSES = ("enrolled", "engaged", "demo_requested", "demo_booked", "disqualified")
# Facts the system derives from what it already holds: they are no sign a source saw the company again.
DERIVED_SOURCES = (SCORING_SOURCE, LOOKALIKE_SOURCE, DOUBT_SOURCE, FOCUS_SOURCE)
# A person's decision kept on the account: an approver's 🚫 at a send approval, or Harry naming it.
DECISION_FACTS = (DECLINED_IN_SLACK, named.FACT)
EVENTS_TEXT_SQL = "SELECT event_id FROM {schema}.events WHERE reply_text IS NOT NULL AND occurred_at < %(before)s"
ITEMS_TEXT_SQL = (
    "SELECT item_id, payload FROM {schema}.hitl_items WHERE kind = ANY(%(kinds)s) AND created_at < %(before)s"
    " AND payload IS NOT NULL AND payload ->> 'reply_text_purged_at' IS NULL"
)
RAW_CONTACTS_SQL = (
    'SELECT DISTINCT r."key" FROM {schema}.raw_clay_contacts AS r'
    " JOIN unnest(%(emails)s::text[]) AS e(email) ON strpos(lower(r.payload::text), e.email) > 0"
)
STALE_ACCOUNTS_SQL = (
    "SELECT a.account_id FROM {schema}.accounts AS a"
    " WHERE a.first_seen < %(before)s"
    " AND COALESCE(a.status, '') <> ALL(%(kept)s) AND COALESCE(a.source, '') <> %(named)s"
    " AND NOT EXISTS (SELECT 1 FROM {schema}.signal_events AS s WHERE s.account_id = a.account_id"
    " AND ((s.observed_at >= %(before)s AND COALESCE(s.source, '') <> ALL(%(derived)s))"
    " OR s.fact = ANY(%(decisions)s)))"
    " AND NOT EXISTS (SELECT 1 FROM {schema}.contacts AS c WHERE c.account_id = a.account_id)"
    " AND NOT EXISTS (SELECT 1 FROM {schema}.events AS e WHERE e.account_id = a.account_id)"
    " AND NOT EXISTS (SELECT 1 FROM {schema}.hitl_items AS h WHERE h.account_id = a.account_id)"
    " ORDER BY a.first_seen, a.account_id"
)

# Why a due lead waits (the summary's leads.held).
IN_FLIGHT = "in flight"
UNDATED = "no enrolment or send to date it by"
UNREADABLE = "its campaign's leads could not be read"
STEPS_LEFT = "Instantly lists it as active or paused, with steps not recorded as sent"
REPLY_WAITING = "a reply of theirs waits for a person"
OPT_OUT_PENDING = "an opt-out not recorded yet"
BOUNCE_PENDING = "a bounce not recorded yet"
LEAD_LEFT = "its Instantly lead is not deleted yet"  # a due contact waits for rule 1


# -- small helpers -------------------------------------------------------------------------------------------------


def _chunks(items: Sequence[str], n: int = ID_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _select_in(ctx: Context, table: str, column: str, values: Iterable[Any], where: Mapping[str, Any] | None = None
               ) -> list[dict]:
    ids = sorted({str(v) for v in values if v})
    return [r for chunk in _chunks(ids) for r in ctx.store.select(table, {**(where or {}), column: list(chunk)})]


def months_before(d: date, months: int) -> date:
    """The same day `months` calendar months before d (the month's last day when it has no such day)."""
    year, month = divmod(d.year * 12 + d.month - 1 - months, 12)
    return d.replace(year=year, month=month + 1, day=min(d.day, calendar.monthrange(year, month + 1)[1]))


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
        events = ctx.store.select("events", {"type": list(dict.fromkeys([*OWN_STOPS, *ACCOUNT_STOPS, *CONVERSATION]))})
        self.own: dict[str, list[date]] = {}
        self.company: dict[str, list[date]] = {}
        self.talk: dict[str, date] = {}  # contact id -> the last email in its conversation
        for e in events:
            day = et_day(e.get("occurred_at"))
            if day is None:
                continue
            if e.get("type") in CONVERSATION and e.get("contact_id"):
                cid = str(e["contact_id"])
                self.talk[cid] = max(day, self.talk.get(cid, day))
            if e.get("type") not in (*OWN_STOPS, *ACCOUNT_STOPS) or not _ends_emails(e):
                continue
            if e.get("type") in ACCOUNT_STOPS:
                if e.get("account_id"):
                    self.company.setdefault(str(e["account_id"]), []).append(day)
            elif e.get("contact_id"):
                self.own.setdefault(str(e["contact_id"]), []).append(day)

    def stop(self, contact: Mapping[str, Any]) -> date | None:
        """The first day a stop ended the contact's emails, on or after the day it was enrolled; None if none did."""
        enrolled = et_day(contact.get("enrolled_at"))
        days = [*self.own.get(str(contact.get("contact_id")), ()),
                *self.company.get(str(contact.get("account_id")), ())]
        days = [d for d in days if enrolled is None or d >= enrolled]
        return min(days) if days else None

    def last_step(self, contact: Mapping[str, Any]) -> date | None:
        """The US Eastern day of the contact's last step (the module docstring); None when nothing dates it."""
        stop = self.stop(contact)
        start = et_day(contact.get("enrolled_at"))
        steps = capacity.step_days(start, self.settings) if start is not None else []
        end = min(steps[-1], stop) if steps and stop is not None else (steps[-1] if steps else stop)
        later = [d for d in (end, et_day(contact.get("last_step_at")), self.talk.get(str(contact.get("contact_id"))))
                 if d is not None]
        return max(later) if later else None


# -- 1. Instantly leads, 31 days after their last step (SPEC 13) -----------------------------------------------------


def _replies_waiting(ctx: Context) -> set[str]:
    """Contacts with a reply card still waiting for a person (open, escalated or sending)."""
    return {str(i["contact_id"]) for i in ctx.store.select("hitl_items", {"status": list(WAITING)})
            if i.get("contact_id") and str(i.get("kind") or "") in REPLY_KINDS}


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
    status = parse.integer(lead.get("status"))
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
            errors.append(f"{name}: its leads could not be read: {type(exc).__name__}: {redact(str(exc))[:160]}")
    recorded, waiting = _recorded(ctx), _replies_waiting(ctx)
    sends = _sends(ctx, (c["contact_id"] for c in candidates))
    ready: list[tuple[dict, bool]] = []  # (contact, Instantly listed its lead)
    for c in candidates:
        leads = listed[str(c["instantly_campaign"])]
        if str(c["contact_id"]) in waiting:
            held[REPLY_WAITING] += 1
            continue
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
                if len(errors) < LIST_LIMIT:  # an address in Instantly's answer is kept only as its hash
                    errors.append(f"{cid}: {type(exc).__name__}: {redact(str(exc))[:160]}")
                log("retention_lead_failed", run_id=ctx.run_id, contact_id=cid, error=str(exc)[:200])
                continue
            was_listed = False
        except (LookupError, ConfigError) as exc:  # the campaign is missing or named twice: the next day
            if len(errors) < LIST_LIMIT:
                errors.append(f"{cid}: {type(exc).__name__}: {redact(str(exc))[:160]}")
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
                  if e.get("reply_text") is not None and (t := utc(e.get("occurred_at"))) is not None and t < before]
        items = [i for i in store.select("hitl_items", {"kind": list(REPLY_ITEM_KINDS)})
                 if isinstance(i.get("payload"), Mapping) and PURGED not in i["payload"]
                 and (t := utc(i.get("created_at"))) is not None and t < before]
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


# -- 3. Contacts who never replied, 12 months after their last step (SPEC 6) ------------------------------------------


def due_contacts(ctx: Context, ends: Ends, today: date) -> tuple[list[dict], Counter[str]]:
    """(the contacts due for deletion, oldest last step first; those due but held back, by reason)."""
    store, before = ctx.store, months_before(today, CONTACT_MONTHS)
    replied = {str(e.get("contact_id")) for e in store.select("events", {"type": "replied"})
               if e.get("contact_id") and e.get("reply_class") not in NOT_A_REPLY}
    warm = store.select("events", {"type": list(WARM_EVENTS)})
    warm_accounts = {str(e["account_id"]) for e in warm if e.get("account_id")}
    replied |= {str(e["contact_id"]) for e in warm if e.get("contact_id")}
    pending = _recorded(ctx)[2]
    held: Counter[str] = Counter()
    due: list[tuple[date, str, dict]] = []
    for c in store.select("contacts"):
        cid = str(c["contact_id"])
        if not (c.get("enrolled_at") or c.get("enrolment_month") or c.get("last_step_at")):
            continue  # never emailed: no last step to date it from
        if cid in replied or str(c.get("account_id")) in warm_accounts:
            continue
        end = ends.last_step(c)
        if end is None:
            held[UNDATED] += 1
            continue
        if end >= before:
            continue
        if c.get("instantly_lead_id"):
            held[LEAD_LEFT] += 1
        elif cid in pending:
            held[OPT_OUT_PENDING] += 1
        else:
            due.append((end, cid, c))
    return [c for _, _, c in sorted(due, key=lambda x: (x[0], x[1]))], held


def _raw_contact_keys(ctx: Context, emails: Sequence[str]) -> list[str]:
    """raw_clay_contacts keys whose record holds one of these addresses (lower case), as erase finds them."""
    if not emails:
        return []
    store = ctx.store
    try:
        rows = store.query(RAW_CONTACTS_SQL.format(schema=store.schema), {"emails": list(emails)})
    except NotImplementedError:  # MemoryStore without a handler: scan the table
        rows = [r for r in store.select("raw_clay_contacts")
                if any(e in str(r.get("payload") or "").lower() for e in emails)]
    return sorted({str(r["key"]) for r in rows if r.get("key") is not None})


def delete_contacts(ctx: Context, ends: Ends, today: date) -> dict:
    """Delete each contact who never replied, CONTACT_MONTHS after its last step, with its personal data beside it."""
    due, held = due_contacts(ctx, ends, today)
    take = due[:CONTACTS_PER_RUN]
    out: dict[str, Any] = {"due": len(due), "deleted": 0, "held": dict(held),
                           "left_for_next_run": len(due) - len(take)}
    if ctx.dry_run or not take:
        return out
    store = ctx.store
    ids = [str(c["contact_id"]) for c in take]
    emails = sorted({normalise_email(str(c["email"])) for c in take if "@" in str(c.get("email") or "")})
    keys = _raw_contact_keys(ctx, emails)
    items = deleted = raw = 0
    for chunk in _chunks(ids):
        items += store.update("hitl_items", {"contact_id": list(chunk)}, {"payload": None})
        deleted += store.delete("contacts", {"contact_id": list(chunk)})
    for chunk in _chunks(keys):
        raw += store.delete("raw_clay_contacts", {"key": list(chunk)})
    out.update(deleted=deleted, hitl_items_cleared=items, raw_clay_contacts_deleted=raw)
    log("retention_contacts_deleted", run_id=ctx.run_id, contacts=deleted, hitl_items=items, raw_clay_contacts=raw)
    return out


# -- 4. Universe rows not refreshed in 12 months (SPEC 6) -----------------------------------------------------------


def stale_accounts(ctx: Context, before: datetime) -> list[str]:
    """Accounts first seen before `before` that no source has observed since, and that nothing else holds (the module
    docstring), oldest first."""
    store = ctx.store
    params = {"before": before, "kept": list(KEPT_STATUSES), "named": named.SOURCE,
              "derived": list(DERIVED_SOURCES), "decisions": list(DECISION_FACTS)}
    try:
        return [str(r["account_id"]) for r in store.query(STALE_ACCOUNTS_SQL.format(schema=store.schema), params)]
    except NotImplementedError:  # MemoryStore without a handler: the same in Python
        pass
    held: set[str] = set()
    for e in store.select("signal_events"):
        seen = utc(e.get("observed_at"))
        if (seen is not None and seen >= before and (e.get("source") or "") not in DERIVED_SOURCES) \
                or e.get("fact") in DECISION_FACTS:
            held.add(str(e.get("account_id")))
    for table in ("contacts", "events", "hitl_items"):
        held |= {str(r["account_id"]) for r in store.select(table) if r.get("account_id")}
    found = [(t, str(a["account_id"])) for a in store.select("accounts")
             if (t := utc(a.get("first_seen"))) is not None and t < before
             and (a.get("status") or "") not in KEPT_STATUSES and (a.get("source") or "") != named.SOURCE
             and str(a["account_id"]) not in held]
    return [aid for _, aid in sorted(found)]


def delete_accounts(ctx: Context) -> dict:
    """Delete the universe rows no source has refreshed in UNIVERSE_MONTHS, with their facts (SPEC 6)."""
    ids = stale_accounts(ctx, months_before(ctx.now, UNIVERSE_MONTHS))
    take = ids[:ACCOUNTS_PER_RUN]
    out = {"due": len(ids), "deleted": 0, "left_for_next_run": len(ids) - len(take)}
    if ctx.dry_run or not take:
        return out
    facts = 0
    for chunk in _chunks(take):
        facts += ctx.store.delete("signal_events", {"account_id": list(chunk)})
        out["deleted"] += ctx.store.delete("accounts", {"account_id": list(chunk)})
    log("retention_accounts_deleted", run_id=ctx.run_id, accounts=out["deleted"], facts=facts)
    return out


# -- what it did: the daily post and status --------------------------------------------------------------------------


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _done(detail: Mapping[str, Any]) -> dict[str, int]:
    """What one run's summary says it deleted or cleared (live), or found due (dry-run)."""
    def get(section: str, key: str) -> int:
        part = detail.get(section)
        return int((part or {}).get(key) or 0) if isinstance(part, Mapping) else 0

    dry = bool(detail.get("dry_run"))
    return {"leads": get("leads", "due" if dry else "deleted"),
            "replies": get("reply_text", "events_due" if dry else "events_cleared"),
            "reply_cards": get("reply_text", "items_due" if dry else "items_cleared"),
            "contacts": get("contacts", "due" if dry else "deleted"),
            "companies": get("accounts", "due" if dry else "deleted")}


def describe(counts: Mapping[str, int]) -> str:
    """"12 Instantly leads (31 days after their last step), the reply text of 3 replies and 3 reply cards (90 days)
    and 2 contacts who never replied (12 months)"; "" when every count is 0."""
    parts = []
    if counts.get("leads"):
        parts.append(f"{_n(counts['leads'], 'Instantly lead')} ({LEAD_DAYS} days after their last step)")
    texts = [_n(counts[k], one, many) for k, one, many in (("replies", "reply", "replies"),
                                                           ("reply_cards", "reply card", None)) if counts.get(k)]
    if texts:
        parts.append(f"the reply text of {' and '.join(texts)} ({REPLY_TEXT_DAYS} days)")
    if counts.get("contacts"):
        parts.append(f"{_n(counts['contacts'], 'contact')} who never replied ({CONTACT_MONTHS} months)")
    if counts.get("companies"):
        companies = _n(counts["companies"], "company", "companies")
        parts.append(f"{companies} no source has refreshed ({UNIVERSE_MONTHS} months)")
    return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else ""


def _runs(store: Any, since: datetime) -> list[dict]:
    """This job's runs that finished ok since then, oldest first."""
    rows = [r for r in store.select("heartbeats", {"job": JOB, "status": "ok"}) if isinstance(r.get("detail"), Mapping)
            and (t := utc(r.get("started_at"))) is not None and t >= since]
    return sorted(rows, key=lambda r: utc(r["started_at"]))


def post_line(ctx: Context) -> tuple[str, dict[str, Any]]:
    """The daily post's one line on what today's retention runs (since midnight UK) deleted or cleared, counts only,
    and the counts for its summary; ("", {}) when they changed nothing. A dry run says what it would have done."""
    today = ctx.today_uk()
    runs = _runs(ctx.store, datetime(today.year, today.month, today.day, tzinfo=UK))
    live = [r for r in runs if not r.get("dry_run")]
    if live:
        counts = {k: sum(_done(r["detail"])[k] for r in live) for k in _done({})}
        text = describe(counts)
        return (f"Retention deleted {text}." if text else ""), ({**counts, "dry_run": False} if text else {})
    if runs:
        counts = _done(runs[-1]["detail"])
        text = describe(counts)
        if text:
            return f"Retention would delete {text} (dry-run: nothing changed).", {**counts, "dry_run": True}
    return "", {}


def status_line(store: Any) -> str:
    """`us-outbound status`'s line: the last run, what it did or found due, what waits and why."""
    last = store.latest("heartbeats", "started_at", {"job": JOB, "status": "ok"})
    detail = (last or {}).get("detail")
    if not isinstance(detail, Mapping):
        return "Retention (00:40 UK daily): not run yet"
    when = utc(last.get("started_at"))
    stamp = when.astimezone(UK).strftime("%a %d %b %H:%M UK") if when else "?"
    text = describe(_done(detail))
    dry = bool(detail.get("dry_run"))
    line = f"Retention: last run {stamp} ({'dry-run' if dry else 'live'}): "
    line += (f"would delete {text}" if dry else f"deleted {text}") if text else "nothing due"
    held: Counter[str] = Counter()
    left = 0
    for section in ("leads", "contacts", "accounts"):
        part = detail.get(section) if isinstance(detail.get(section), Mapping) else {}
        held.update({str(k): int(v) for k, v in (part.get("held") or {}).items()})
        left += int(part.get("left_for_next_run") or 0)
    if held:
        line += "; held: " + ", ".join(f"{why} {n}" for why, n in sorted(held.items(), key=lambda x: (-x[1], x[0])))
    if left:
        line += f"; {left:,} left for the next run"
    return line


# -- the job ---------------------------------------------------------------------------------------------------------


def run(ctx: Context) -> dict:
    """The retention job (JOB CONTRACT: run(ctx) -> summary)."""
    today = ctx.now_et().date()
    errors: list[str] = []
    ends = Ends(ctx)
    out: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run}
    out["leads"] = delete_leads(ctx, ends, today, errors)
    out["reply_text"] = purge_reply_text(ctx)
    out["contacts"] = delete_contacts(ctx, ends, today)
    out["accounts"] = delete_accounts(ctx)
    out["errors"] = errors
    log("retention_done", run_id=ctx.run_id, **out)
    return out
