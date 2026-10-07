"""sync_outcomes (SPEC 6 events, 9, 12, 13): what Instantly did with our leads, into the events table.

It runs every 15 minutes, beside poll_replies. SPEC 9 had it at 01:00 daily, but the kill rules
and the send forecast (enrol/capacity.py) need today's sends, bounces and stops, and SPEC 13
honors opt-outs the same day. Each run:

  * sent:         each campaign email a registry mailbox sent to one of our contacts (GET /emails,
                  email_type sent, one request series per mailbox). step is the email's place
                  among the contact's sends: the steps go out in order, one variant each (SPEC 9),
                  and Instantly's own step numbering is not confirmed. mailbox is the address
                  that sent it. The contact's last_step_at follows its latest send, and its mailbox
                  is set from step 1 when enrol could not know it (Harry has two addresses).
                  A reply the reply desk sent is its own type, reply_sent (replies/desk.py): never
                  a step, and dropped here if Instantly lists it among the sent emails.
  * replied:      each email one of our contacts, or anyone at the account's domain, sent to a
                  registry mailbox, unclassified (reply_class NULL) for poll_replies to classify.
                  The account becomes engaged. An email Instantly marks as an auto-reply is left to
                  poll_replies: stop_on_auto_reply is off, so Instantly keeps that sequence going,
                  and an away message does not engage anyone.
  * bounced:      each lead in a US Outbound campaign whose status is bounced. The address also goes
                  on suppression (reason bounce), so a later re-contact cannot use it.
  * unsubscribed: each lead whose status is unsubscribed, which is what a click on Instantly's own
                  unsubscribe link does (Harry, 1 Oct 2026: that link is the opt-out every email
                  carries). replies/optout.py records it everywhere SPEC 11 asks; it is a go-live
                  blocker (docs/gtm-review/README.md B1b). The workspace's own blocklist and
                  unsubscribe list are not read whole: they hold the European campaigns' people too
                  (SPEC 1.2). A click marks the lead itself, so the lead's status is enough: confirmed
                  live 6 Oct 2026 (the seed test). PHASE0-CONFIRM: that the List-Unsubscribe header does too.

Event ids are the idempotency keys (SPEC 6): the Instantly email id for sent and replied rows,
"bounced:{lead id}" and "unsubscribed:lead:{lead id}" for lead statuses, so a re-run adds nothing.
enrol/capacity.py reads these event names (STOP_EVENTS). A replied row written here carries only
the columns this job owns, so it never clears a class poll_replies has already set.

What a run reads (it runs every 15 minutes, so each read is kept small):
  * emails from SYNC_OVERLAP (6 hours) before the last good run started; once a day (RECHECK_EVERY)
    the last two days (RECHECK) as well, in case Instantly lists an email late. recheck_at in the
    run's summary (its heartbeat) says when that was last done.
  * lead statuses (the full lead list of every US Outbound campaign) at most once an hour
    (LEADS_EVERY), and on the first run after a switch between dry-run and live; leads_read_at in
    the summary says when. An opt-out is still honored the same day.
  * at most CATCH_UP (7 days) of email a run: after a long outage a run reads the oldest seven days
    and its summary's resume_from tells the next run where to go on, so no run outgrows its timeout
    and none is skipped for ever behind a dead one. Events stay idempotent whatever is re-read.
The last good run is one row (Store.latest), not every heartbeat the job ever wrote.

Every read is filtered by registry mailbox or by US Outbound campaign (SPEC 1.2), and an email
or lead that belongs to no contact of ours is counted and dropped. Dry-run reads Instantly and
writes the database (SPEC 0.3), but nothing to Instantly or HubSpot.

Then the account-level stop (replies/account_stop.py; Harry, 6 Oct 2026): at an account where someone
replied, bounced, unsubscribed or complained, every other contact's lead is stopped too. That matters only
for accounts with a second contact (enrol/second.py); with one contact per account it calls nothing. And
(Harry, 7 Oct 2026) a contact in flight whose address or domain has been suppressed since enrolment (a HubSpot
opt-out from suppression_load, a customer domain) has their lead stopped, with everyone else's at the account.

Directory, since() and mark_engaged() are shared with poll_replies.

SPEC 9's other sync_outcomes duties are not here: the daily retention job deletes leads 31 days after
their last step (ops/retention.py), and Apollo deletion notices, which Apollo offers no API for, are
honoured by hand with `us-outbound erase --email ADDRESS --live` within 30 days (ops/erase.py,
docs/daily.md).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clean.domains import root_domain
from us_outbound.clients.instantly import LEAD_BOUNCED, LEAD_UNSUBSCRIBED
from us_outbound.context import Context
from us_outbound.logs import hash_email, log
from us_outbound.replies import optout
from us_outbound.settings.validate import is_spill_domain
from us_outbound import suppression

JOB = "sync_outcomes"
# The event types enrol/capacity.py reads (STOP_EVENTS, and "sent" for the forecast). SPEC 6.
SENT, BOUNCED, REPLIED, UNSUBSCRIBED = "sent", "bounced", "replied", "unsubscribed"
# A reply the reply desk sent (replies/desk.py): it uses the mailbox, but it is no campaign step,
# so it is never a "sent" row, never numbered as a step and never counted as a send.
REPLY_SENT = "reply_sent"
ENROLLED, ENGAGED = "enrolled", "engaged"
BOUNCE_REASON, INSTANTLY_SOURCE = "bounce", "instantly"
OVERLAP = timedelta(hours=1)  # poll_replies re-reads from an hour before its last good run started
RECHECK = timedelta(days=2)  # and the last two days, in case Instantly lists an email late
MAX_LOOKBACK = timedelta(days=45)  # leads leave Instantly 31 days after their last step (SPEC 13)
SYNC_OVERLAP = timedelta(hours=6)  # sync_outcomes re-reads from six hours before its last good run started,
RECHECK_EVERY = timedelta(days=1)  # and the last two days (RECHECK) once a day
LEADS_EVERY = timedelta(hours=1)  # sync_outcomes reads lead statuses at most this often
CATCH_UP = timedelta(days=7)  # the most email one sync_outcomes run reads; a longer gap takes several runs
ID_CHUNK = 1000
# System senders at a prospect's domain: a bounce notice there is not a reply from the account.
SYSTEM_SENDERS = frozenset({"mailer-daemon", "postmaster", "noreply", "no-reply", "donotreply", "do-not-reply"})


# -- small helpers -------------------------------------------------------------------------


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def to_time(v: Any) -> datetime | None:
    """An aware datetime (naive ones are taken as UTC) from a datetime, a date or ISO text; else None."""
    if isinstance(v, str) and v:
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _chunks(items: Sequence[str], n: int = ID_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def select_in(ctx: Context, table: str, column: str, values: Iterable[str], where: Mapping[str, Any] | None = None) -> list[dict]:
    """Rows whose column is one of values, read in chunks."""
    ids = sorted({str(v) for v in values if v})
    out: list[dict] = []
    for chunk in _chunks(ids):
        out.extend(ctx.store.select(table, {**(where or {}), column: list(chunk)}))
    return out


def email_time(email: Mapping[str, Any]) -> datetime | None:
    """When the email was sent or received. PHASE0-CONFIRM: timestamp_email, else timestamp_created."""
    return to_time(email.get("timestamp_email")) or to_time(email.get("timestamp_created"))


def _addresses(value: Any) -> list[str]:
    """An address field as Instantly gives it (a string, a comma-separated string or a list), lower-cased."""
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = str(value or "").split(",")
    out = []
    for p in parts:
        p = p.strip().strip("<>").strip().lower()
        if "<" in p:  # "Jane Doe <jane@acme.com>"
            p = p.rsplit("<", 1)[1].strip(">").strip()
        if "@" in p:
            out.append(p)
    return out


def from_address(email: Mapping[str, Any]) -> str:
    found = _addresses(email.get("from_address_email"))
    return found[0] if found else ""


def is_auto_reply(email: Mapping[str, Any]) -> bool:
    """Instantly's own auto-reply flag. PHASE0-CONFIRM: the is_auto_reply field and its values."""
    v = email.get("is_auto_reply")
    return v is True or _int(v) == 1


# PHASE0-CONFIRM: Instantly's ue_type codes. Only the codes that mark an email as one of ours are
# used, so an unexpected code never drops a reply: the email_type filter and the sender decide.
UE_CAMPAIGN, UE_RECEIVED, UE_MANUAL = 1, 2, 3  # a campaign step, a received email, one sent by hand


def _ue_type(email: Mapping[str, Any]) -> int | None:
    return _int(email.get("ue_type"))


def is_campaign_send(email: Mapping[str, Any], eaccount: str) -> bool:
    """A campaign step the mailbox sent: from the mailbox itself, and not a reply sent by hand."""
    if _ue_type(email) in (UE_RECEIVED, UE_MANUAL):
        return False
    sender = from_address(email)
    return not sender or sender == eaccount


def is_received(email: Mapping[str, Any], eaccount: str, registry: frozenset[str] = frozenset()) -> bool:
    """An email someone outside Spill sent to the mailbox (not ours, not a system notice)."""
    if _ue_type(email) in (UE_CAMPAIGN, UE_MANUAL):
        return False
    sender = from_address(email)
    if not sender or sender == eaccount or sender in registry:
        return False
    local, _, domain = sender.partition("@")
    return not is_spill_domain(domain) and local not in SYSTEM_SENDERS


# -- who an email or lead belongs to --------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """The contact and account an email belongs to. from_is_contact: the contact wrote it themselves."""

    contact: dict
    account: dict
    from_is_contact: bool


class Directory:
    """Our enrolled contacts, by email hash, lead id and account domain, with their accounts."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        contacts = [
            c for c in ctx.store.select("contacts")
            if c.get("instantly_lead_id") or c.get("enrolment_month") or c.get("enrolled_at")
        ]
        self.by_id = {str(c["contact_id"]): c for c in contacts}
        self.by_hash: dict[str, dict] = {}
        self.by_lead: dict[str, dict] = {}
        for c in contacts:
            h = _lower(c.get("email_sha256")) or (hash_email(str(c["email"])) if c.get("email") else "")
            if h:
                self.by_hash[h] = c
            if c.get("instantly_lead_id"):
                self.by_lead[str(c["instantly_lead_id"])] = c
        accounts = select_in(ctx, "accounts", "account_id", (c.get("account_id") for c in contacts))
        self.accounts = {str(a["account_id"]): a for a in accounts}
        aliases = {_lower(a.get("alias")): _lower(a.get("root_domain")) for a in ctx.store.select("domain_aliases")}
        self.by_domain: dict[str, dict] = {}
        # The account's latest enrolled contact speaks for it (one contact per account in v1).
        for c in sorted(contacts, key=lambda c: (to_time(c.get("enrolled_at")) or datetime.min.replace(tzinfo=UTC))):
            a = self.accounts.get(str(c.get("account_id")))
            domain = root_domain(a.get("domain")) if a else None
            if domain:
                self.by_domain[domain] = c
        for alias, root in aliases.items():
            if root in self.by_domain and alias not in self.by_domain:
                self.by_domain[alias] = self.by_domain[root]

    def earliest_enrolled(self) -> datetime | None:
        times = [t for c in self.by_id.values() if (t := to_time(c.get("enrolled_at")))]
        return min(times) if times else None

    def account_of(self, contact: Mapping[str, Any]) -> dict:
        return self.accounts.get(str(contact.get("account_id")), {"account_id": contact.get("account_id")})

    def contact_for(self, address: str) -> dict | None:
        return self.by_hash.get(hash_email(address)) if address else None

    def lead_contact(self, lead: Mapping[str, Any]) -> dict | None:
        """The contact a lead of one of our campaigns is: by lead id, else by its email."""
        return self.by_lead.get(str(lead.get("id") or "")) or self.contact_for(_lower(lead.get("email")))

    def _match(self, contact: dict | None, from_is_contact: bool) -> Match | None:
        return Match(contact, self.account_of(contact), from_is_contact) if contact else None

    def match_sent(self, email: Mapping[str, Any]) -> dict | None:
        """The contact a campaign email went to: by lead id, the lead's address, then the To address."""
        c = self.by_lead.get(str(email.get("lead_id") or ""))
        for addr in [*_addresses(email.get("lead")), *_addresses(email.get("to_address_email_list"))]:
            c = c or self.contact_for(addr)
        return c

    def match_reply(self, email: Mapping[str, Any]) -> Match | None:
        """Who a received email is from: the contact by address, the lead Instantly tied it to, or the account's domain.

        SPEC 2: a reply from anyone at the account counts as the account's reply.
        """
        sender = from_address(email)
        c = self.contact_for(sender)
        if c:
            return self._match(c, True)
        c = self.by_lead.get(str(email.get("lead_id") or ""))
        for addr in _addresses(email.get("lead")):
            c = c or self.contact_for(addr)
        if c:
            return self._match(c, False)
        domain = root_domain(sender.partition("@")[2]) if sender else None
        return self._match(self.by_domain.get(domain or ""), False)


# -- shared with poll_replies --------------------------------------------------------------------


_UNREAD: Any = object()  # since(): the last run not read yet


def last_run(ctx: Context, job: str, *, live_only: bool = False) -> dict | None:
    """The job's latest heartbeat that finished ok (one row, Store.latest), with its summary as detail.

    live_only: count only live runs (poll_replies: a dry run classifies nothing).
    """
    where: dict[str, Any] = {"job": job, "status": "ok"}
    if live_only:
        where["dry_run"] = [False, None]
    return ctx.store.latest("heartbeats", "started_at", where)


def detail_of(run: Mapping[str, Any] | None) -> Mapping[str, Any]:
    d = (run or {}).get("detail")
    return d if isinstance(d, Mapping) else {}


def since(ctx: Context, job: str, *, live_only: bool = False, recheck: timedelta = RECHECK,
          earliest: datetime | None = None, overlap: timedelta = OVERLAP, last: Any = _UNREAD) -> datetime:
    """Where this run starts reading: `overlap` before the last good run began, and at least `recheck` back.

    live_only: count only live runs (poll_replies: a dry run classifies nothing, so it must not
    move the start on). With no such run yet, it starts from the first enrollment. A run that
    stopped before reading everything (its time budget, or sync_outcomes' CATCH_UP) left
    resume_from in its summary: the start is never after that, less `overlap`. last: that run,
    when the caller has already read it.
    """
    if last is _UNREAD:
        last = last_run(ctx, job, live_only=live_only)
    began = to_time((last or {}).get("started_at"))
    if began is not None:
        start = began - overlap
    elif earliest is not None:
        start = earliest - overlap
    else:
        start = ctx.now - recheck
    resume = to_time(detail_of(last).get("resume_from"))
    if resume is not None:
        start = min(start, resume - overlap)
    return max(ctx.now - MAX_LOOKBACK, min(start, ctx.now - recheck))


def mark_engaged(ctx: Context, account_ids: Iterable[str]) -> list[str]:
    """Enrolled accounts that replied become engaged (SPEC 6); a later status (a demo) is never moved back."""
    ids = sorted({str(a) for a in account_ids if a})
    done: list[str] = []
    for a in select_in(ctx, "accounts", "account_id", ids, {"status": ENROLLED}):
        ctx.store.update("accounts", {"account_id": a["account_id"], "status": ENROLLED}, {"status": ENGAGED})
        done.append(str(a["account_id"]))
    if done:
        log("accounts_engaged", run_id=ctx.run_id, accounts=done)
    return done


def sent_events(ctx: Context, contact_ids: Iterable[str]) -> dict[str, list[dict]]:
    """contact id -> its campaign sends (sent events), oldest first.

    A sent row with an approval is a desk reply written before desk replies had their own type
    (reply_sent): it is left out, so it is never numbered as a step.
    """
    out: dict[str, list[dict]] = defaultdict(list)
    for e in select_in(ctx, "events", "contact_id", contact_ids, {"type": SENT}):
        if e.get("approval"):
            continue
        out[str(e["contact_id"])].append(e)
    for evs in out.values():
        evs.sort(key=_send_order)
    return out


def _send_order(e: Mapping[str, Any]) -> tuple[datetime, str]:
    return (to_time(e.get("occurred_at")) or datetime.min.replace(tzinfo=UTC), str(e.get("event_id")))


def step_before(sends: Sequence[Mapping[str, Any]], at: datetime | None) -> int | None:
    """The step of the contact's last send at or before `at`: the email a reply answers."""
    steps = [_int(e.get("step")) for e in sends if at is None or (to_time(e.get("occurred_at")) or at) <= at]
    steps = [s for s in steps if s is not None]
    return max(steps) if steps else None


def event_ids(ctx: Context, type_: str) -> set[str]:
    return {str(e["event_id"]) for e in ctx.store.select("events", {"type": type_})}


# -- the job --------------------------------------------------------------------------------------


def _record_sent(ctx: Context, d: Directory, emails: Iterable[Mapping[str, Any]], campaign_ids: set[str],
                 dropped: Counter[str]) -> tuple[int, set[str]]:
    """Sent events, with each contact's steps numbered in send order; contacts' last_step_at and mailbox."""
    new: dict[str, dict] = {}
    for e in emails:
        eid, acct = str(e.get("id") or ""), _lower(e.get("eaccount"))
        if not eid or eid in new:
            continue
        cid = str(e.get("campaign_id") or "")
        if cid and campaign_ids and cid not in campaign_ids:
            dropped["sent: another campaign"] += 1
            continue
        if not is_campaign_send(e, acct):
            dropped["sent: not a campaign step"] += 1
            continue
        contact = d.match_sent(e)
        if contact is None:
            dropped["sent: no contact of ours"] += 1
            continue
        new[eid] = {"event_id": eid, "contact_id": contact["contact_id"], "account_id": contact.get("account_id"),
                    "type": SENT, "mailbox": acct, "occurred_at": email_time(e) or ctx.now}
    # A reply the desk sent may be listed as a sent email too: its own row stays as it is.
    for r in select_in(ctx, "events", "event_id", new):
        if r.get("type") == REPLY_SENT or (r.get("type") == SENT and r.get("approval")):
            new.pop(str(r["event_id"]), None)
            dropped["sent: a reply sent from the reply desk"] += 1
    if not new:
        return 0, set()
    by_contact = sent_events(ctx, {r["contact_id"] for r in new.values()})
    known = {str(e["event_id"]) for evs in by_contact.values() for e in evs}
    for r in new.values():
        if r["event_id"] not in known:
            by_contact[str(r["contact_id"])].append(r)
    rows, contacts = [], []
    for contact_id, evs in by_contact.items():
        evs.sort(key=_send_order)
        for i, ev in enumerate(evs, start=1):
            if ev["event_id"] not in known or _int(ev.get("step")) != i:
                rows.append({**ev, "step": i})
        c = d.by_id.get(contact_id, {})
        update: dict[str, Any] = {}
        last = to_time(evs[-1].get("occurred_at"))
        if last and (not to_time(c.get("last_step_at")) or last > to_time(c.get("last_step_at"))):
            update["last_step_at"] = last
        if not c.get("mailbox") and evs[0].get("mailbox"):
            update["mailbox"] = evs[0]["mailbox"]  # the address that sent step 1 (SPEC 6)
        if update:
            contacts.append({"contact_id": contact_id, **update})
    if rows:
        ctx.store.upsert("events", rows)
    if contacts:
        ctx.store.upsert("contacts", contacts)
    return sum(1 for r in new.values() if r["event_id"] not in known), set(by_contact)


def _record_replies(ctx: Context, d: Directory, emails: Iterable[Mapping[str, Any]], dropped: Counter[str]) -> tuple[int, int]:
    """Unclassified replied events for new replies; their accounts become engaged. (new, auto-replies left)"""
    known = event_ids(ctx, REPLIED)
    registry = ctx.guard.bounds.registry_addresses
    rows: dict[str, dict] = {}
    auto = 0
    for e in emails:
        eid, acct = str(e.get("id") or ""), _lower(e.get("eaccount"))
        if not eid or eid in known or eid in rows or not is_received(e, acct, registry):
            continue
        if is_auto_reply(e):
            auto += 1  # poll_replies decides: an away message engages nobody
            continue
        m = d.match_reply(e)
        if m is None:
            dropped["received: no contact or account of ours"] += 1
            continue
        rows[eid] = {"event_id": eid, "contact_id": m.contact["contact_id"], "account_id": m.contact.get("account_id"),
                     "type": REPLIED, "mailbox": acct, "occurred_at": email_time(e) or ctx.now}
    if rows:
        sends = sent_events(ctx, {r["contact_id"] for r in rows.values()})
        for r in rows.values():
            r["step"] = step_before(sends.get(str(r["contact_id"]), []), to_time(r["occurred_at"]))
        ctx.store.upsert("events", list(rows.values()))  # only these columns: a class already set stays
        mark_engaged(ctx, (r["account_id"] for r in rows.values()))
    return len(rows), auto


def _record_bounce(ctx: Context, contact: dict, lead: Mapping[str, Any], known: set[str]) -> bool:
    lead_id = str(lead.get("id") or "")
    event_id = f"{BOUNCED}:{lead_id}"
    if event_id in known:
        return False
    sends = sent_events(ctx, [contact["contact_id"]]).get(str(contact["contact_id"]), [])
    last = sends[-1] if sends else {}
    at = to_time(last.get("occurred_at")) or to_time(lead.get("timestamp_last_contact")) or ctx.now
    ctx.store.upsert("events", [{
        "event_id": event_id, "contact_id": contact["contact_id"], "account_id": contact.get("account_id"),
        "type": BOUNCED, "step": _int(last.get("step")), "mailbox": last.get("mailbox") or contact.get("mailbox"),
        "occurred_at": at,
    }])
    address = _lower(lead.get("email")) or _lower(contact.get("email"))
    if address:
        suppression.add(ctx.store, email=address, reason=BOUNCE_REASON, source=INSTANTLY_SOURCE, now=ctx.now)
    if not contact.get("suppressed"):
        ctx.store.update("contacts", {"contact_id": contact["contact_id"]},
                         {"suppressed": True, "suppressed_reason": BOUNCE_REASON})
    known.add(event_id)
    return True


def _lead_outcomes(ctx: Context, d: Directory, campaigns: Iterable[str], out: dict, dropped: Counter[str]) -> None:
    """Bounced and unsubscribed leads in our campaigns (SPEC 11, 13)."""
    inst = ctx.clients.instantly
    bounced = event_ids(ctx, BOUNCED)
    done = optout.done_markers(ctx.store)
    for name in campaigns:
        for lead in inst.list_leads(name):
            status = _int(lead.get("status"))
            if status not in (LEAD_BOUNCED, LEAD_UNSUBSCRIBED):
                continue
            contact = d.lead_contact(lead)
            if contact is None:
                dropped["lead: no contact of ours"] += 1
                continue
            if status == LEAD_BOUNCED:
                out["bounced"] += _record_bounce(ctx, contact, lead, bounced)
                continue
            address = _lower(lead.get("email")) or _lower(contact.get("email"))
            if not address:
                dropped["lead: unsubscribed with no address"] += 1
                continue
            result = optout.opt_out(
                ctx, marker=optout.lead_marker(str(lead.get("id"))), addresses=[address], contact=contact,
                account_id=contact.get("account_id"), mailbox=contact.get("mailbox"),
                occurred_at=to_time(lead.get("timestamp_updated")) or ctx.now, source=INSTANTLY_SOURCE, done=done,
            )
            out["unsubscribed"][result] += 1


@dataclass(frozen=True)
class Window:
    """What one sync_outcomes run reads (see the module docstring)."""

    start: datetime
    end: datetime | None  # None: up to now; else a catch-up run, and the next goes on from here
    recheck_at: str | None  # when the last two days were last re-read (ISO), carried run to run
    read_leads: bool
    leads_read_at: str | None  # when lead statuses were last read (ISO), carried run to run


def window(ctx: Context, earliest: datetime | None) -> Window:
    """This run's reads, from the last good run's summary (one heartbeat row)."""
    last = last_run(ctx, JOB)
    prev = detail_of(last)
    now = ctx.now
    rechecked = to_time(prev.get("recheck_at"))
    recheck_due = rechecked is None or now - rechecked >= RECHECK_EVERY
    start = since(ctx, JOB, earliest=earliest, recheck=RECHECK if recheck_due else timedelta(0),
                  overlap=SYNC_OVERLAP, last=last)
    end = start + CATCH_UP if now - start > CATCH_UP else None
    leads_at = to_time(prev.get("leads_read_at"))
    # The first run after a switch between dry-run and live reads them: a live run opts out what a dry run could not.
    mode_changed = last is not None and bool(last.get("dry_run")) != ctx.dry_run
    read_leads = leads_at is None or now - leads_at >= LEADS_EVERY or mode_changed
    return Window(
        start=start, end=end,
        recheck_at=now.isoformat() if recheck_due and end is None else (rechecked.isoformat() if rechecked else None),
        read_leads=read_leads,
        leads_read_at=now.isoformat() if read_leads else (leads_at.isoformat() if leads_at else None),
    )


def run(ctx: Context) -> dict:
    """The sync_outcomes job (JOB CONTRACT: run(ctx) -> summary)."""
    registry = sorted(ctx.guard.bounds.registry_addresses)
    if not registry:
        return {"skipped": True, "reason": "no registry mailboxes"}
    d = Directory(ctx)
    inst = ctx.clients.instantly
    campaigns = {str(c["id"]): str(c["name"]) for c in inst.list_campaigns()}
    w = window(ctx, d.earliest_enrolled())
    dropped: Counter[str] = Counter()
    out: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "since": w.start.isoformat(),
                           "until": w.end.isoformat() if w.end else None, "campaigns": len(campaigns),
                           "bounced": 0, "unsubscribed": Counter(), "leads_read": w.read_leads,
                           "leads_read_at": w.leads_read_at, "recheck_at": w.recheck_at}
    if w.end is not None:
        out["resume_from"] = w.end.isoformat()  # a catch-up run: the next run goes on from here

    sent_emails = inst.list_emails(registry, w.start, email_type="sent", until=w.end)
    sent, touched = _record_sent(ctx, d, sent_emails, set(campaigns), dropped)
    received = inst.list_emails(registry, w.start, email_type="received", until=w.end)
    replied, auto = _record_replies(ctx, d, received, dropped)
    if w.read_leads:
        _lead_outcomes(ctx, d, campaigns.values(), out, dropped)
    # Anyone's reply, bounce, unsubscribe or complaint stops every sequence at the account (Harry, 6 Oct 2026).
    from us_outbound.replies import account_stop

    out["account_stops"] = account_stop.sweep(ctx)

    out.update(sent=sent, contacts_with_new_sends=len(touched), replied=replied, auto_replies_left_to_poll_replies=auto,
               unsubscribed=dict(out["unsubscribed"]), dropped=dict(dropped))
    log("sync_outcomes_done", run_id=ctx.run_id, **out)
    return out
