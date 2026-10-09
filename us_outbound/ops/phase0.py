"""`us-outbound phase0 check`: the PHASE0-CONFIRM items a live read settles, checked once on Railway.

About 116 API details in the code were written from vendor docs and never seen against the live APIs (roadmap §4,
week 1, item 1; Harry, 7 Oct 2026). docs/phase0-confirm.md lists every one, with its status: confirmed by a live
run already, settled by a probe here, settled by a write on Harry's own seed lead (--seed), left for production to
show (Watch), or moot. This command runs the probes and prints one line each, keyed by the id the doc uses:

  <ID>  CONFIRMED: what it saw
  <ID>  DIFFERS: what it saw
  <ID>  COULD NOT CHECK: why

then "What to change": the constant or comment each CONFIRMED or DIFFERS line settles, gates first. Harry pastes
the output back to the build.

  us-outbound phase0 check [--seed ADDRESS ...] [--apollo-credits] [--live]

  * Without --live it prints what each probe would call, and calls nothing (not even a read).
  * With --live the probes read, through the guarded clients: Instantly (the emails of the registry mailboxes, the
    seed leads), HubSpot (association labels, one deal search), Slack (auth.test's scopes), Apollo (credit usage,
    one people search and a company Apollo does not know: no credits), and what the live jobs stored in the
    database. Nothing is written but this run's heartbeat row.
  * --apollo-credits adds the three probes that cost an Apollo credit each (an organization search page, a
    lookalike page, one company enrich: 3 credits at most), with Apollo's usage read before and after.
  * --seed ADDRESS (live only) also writes, on that seed lead alone: Harry's own seed inbox, a lead `seed send`
    added (company "Seed test (Spill)") in a US Outbound campaign, checked as `seed send` checks an address. On a
    seed lead still in its sequence (status 1) it pauses it and sets it active again, reading each back (SEED-PAUSE,
    LEAD_PAUSE_CONFIRMED's first half). On a seed lead that has finished (unsubscribed or completed) it marks it
    "Meeting booked" and sets the interest back (SEED-INTEREST). Repeat --seed for both. It also forwards the first
    seed lead's email 1 to escalation_email, once (SEED-FORWARD). A later run with the same address reads whether
    the paused lead went on with its next step (SEED-RESUME, the second half), and pauses and forwards nothing again:
    the heartbeat keeps what an earlier run did.

Output never carries a key or a personal detail: an address is shown as its hash ("email:…", logs.hash_email),
every line passes logs.redact, and a prospect's email is never read beyond its fields' presence.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from us_outbound import parse
from us_outbound.clients.apollo import VISITOR_CREDIT, credit_stats, credits_left, enriched_in, organizations_in
from us_outbound.clients.db import new_id
from us_outbound.clients.guard import GuardViolation, Op
from us_outbound.clients.hubspot import ASSOCIATION_TYPE_IDS
from us_outbound.clients.http import ApiError
from us_outbound.clients.instantly import (
    INTEREST_INTERESTED,
    INTEREST_MEETING_BOOKED,
    LEAD_ACTIVE,
    LEAD_PAUSED,
    LEAD_UNSUBSCRIBED,
    STEP_DAYS,
)
from us_outbound.clients.instantly import _segment as path_segment
from us_outbound.clean.people import state_code
from us_outbound.context import ConfigError, Context
from us_outbound.logs import clip, hash_email, log, redact
from us_outbound.ops import seed as seed_ops
from us_outbound.registry.mailboxes import campaign_name
from us_outbound.replies.outcomes import UE_CAMPAIGN, email_time, from_address
from us_outbound.timeparse import utc

JOB = "phase0_check"
CONFIRMED, DIFFERS, NOT_CHECKED = "CONFIRMED", "DIFFERS", "COULD NOT CHECK"
READ, STORED, CREDITS, SEED, POINTER = "read", "stored", "credits", "seed", "pointer"
LOOKBACK = timedelta(days=14)  # the emails the Instantly probes read (the seed sends of 5 and 6 Oct 2026 included)
NAMES_SET = datetime.fromisoformat("2026-10-06T00:00:00+00:00")  # the morning check found no sender-name drift
UNKNOWN_DOMAIN = "spill-phase0-unknown-domain-check.com"  # a company Apollo does not know: an enrich costs nothing
SLACK_SCOPES = ("chat:write", "channels:history", "groups:history", "channels:read", "groups:read", "users:read",
                "reactions:read", "reactions:write", "im:write")  # deploy/slack-app-manifest.yaml
INTEREST_FIELD = "lt_interest_status"  # the lead's interest status in Instantly's v2 lead object
INTEREST_POLLS, INTEREST_WAIT = 6, 5.0  # Instantly sets the interest status in the background: 30 s at most
STEP_GAP = STEP_DAYS[1] - STEP_DAYS[0]  # days from email 1 to email 2 (7)
RESUME_GRACE = timedelta(days=3)  # a step due on a Friday may go on Monday
MIN_SAMPLE = 10  # stored facts needed before a stored probe says anything
SLEEP: Callable[[float], None] = time.sleep  # tests replace it


@dataclass
class Result:
    status: str
    detail: str = ""


@dataclass(frozen=True)
class Probe:
    """One probe: the question, what it calls, and what to change when it is CONFIRMED or DIFFERS."""

    id: str
    question: str
    calls: str  # what it would call, for the dry-run line
    fn: Callable[["Run"], Result]
    kind: str = READ
    gate: bool = False  # gates sending, an opt-out or a stop (roadmap §4, week 1, item 1): listed first
    credits: int = 0  # Apollo credits it may spend (only with --apollo-credits)
    if_confirmed: str = ""
    if_differs: str = ""


def _id(address: str) -> str:
    """An address as the logs show it: its hash, never the address (logs.hash_email)."""
    return "email:" + hash_email(address)[:16]


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


# -- one run's reads, made once and shared by the probes -----------------------------------------------------


@dataclass
class Run:
    ctx: Context
    seed_addresses: tuple[str, ...] = ()
    apollo_credits: bool = False
    _cache: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)  # kept in the heartbeat: the pause this run made

    def once(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    @property
    def registry(self) -> list[str]:
        return sorted(self.ctx.guard.bounds.registry_addresses)

    def emails(self, email_type: str) -> list[dict]:
        """The registry mailboxes' emails of this type over LOOKBACK (Instantly.list_emails, one series a mailbox)."""
        since = self.ctx.now - LOOKBACK
        return self.once(f"emails:{email_type}", lambda: self.ctx.clients.instantly.list_emails(
            self.registry, since, email_type=email_type))

    def campaigns(self) -> dict[str, str]:
        """US Outbound campaign id -> name."""
        return self.once("campaigns", lambda: {str(c["id"]): str(c["name"])
                                               for c in self.ctx.clients.instantly.list_campaigns()})

    def seed_leads(self) -> list[tuple[str, dict]]:
        """(campaign name, lead) for every seed lead (`seed send`'s company) in the owners' campaigns."""
        def read() -> list[tuple[str, dict]]:
            inst, names = self.ctx.clients.instantly, set(self.campaigns().values())
            out = []
            for owner in self.ctx.settings.owners():
                name = campaign_name(owner)
                if name in names:
                    out += [(name, lead) for lead in inst.list_leads(name)
                            if str(lead.get("company_name") or "") == seed_ops.SEED_COMPANY]
            return out
        return self.once("seed_leads", read)

    def seed_lead(self, address: str) -> tuple[str, dict] | None:
        return next(((n, lead) for n, lead in self.seed_leads() if _lower(lead.get("email")) == address), None)

    def owner_of(self, mailbox: str) -> str:
        return next((m.owner_name for m in self.ctx.settings.mailboxes if m.address.lower() == mailbox), "")

    def get_lead(self, name: str, lead_id: str) -> dict:
        """One lead, read back (GET /leads/{id}), checked to be the lead asked for."""
        body = self.ctx.clients.instantly.request(
            "GET", f"/leads/{path_segment(lead_id)}", Op("lead.get", target=name, detail={"lead_id": lead_id}))
        if not isinstance(body, dict) or str(body.get("id") or lead_id) != lead_id:
            raise GuardViolation(f"Instantly returned another lead for {lead_id}")
        return body


# -- Instantly: reads -------------------------------------------------------------------------------------


def _created(e: Mapping[str, Any]) -> datetime | None:
    return utc(e.get("timestamp_created")) or email_time(e)


def emails_until(r: Run) -> Result:
    """Does GET /emails apply max_timestamp_created? Read one mailbox's sends again, cut at a time between them."""
    by_box: dict[str, list[dict]] = {}
    for e in r.emails("sent"):
        if _created(e):
            by_box.setdefault(_lower(e.get("eaccount")), []).append(e)
    usable = {box: es for box, es in by_box.items() if len({_created(e) for e in es}) >= 2}
    if not usable:
        return Result(NOT_CHECKED, f"fewer than two campaign emails at different times in the last {LOOKBACK.days} days "
                                   "from any one mailbox; run it again once the follow-ups have gone out")
    box, es = max(usable.items(), key=lambda kv: len(kv[1]))
    times = sorted({_created(e) for e in es})
    cut = times[(len(times) - 1) // 2]
    later = sum(1 for e in es if _created(e) > cut)
    got = r.ctx.clients.instantly.list_emails([box], r.ctx.now - LOOKBACK, email_type="sent", until=cut)
    after = [e for e in got if (_created(e) or cut) > cut + timedelta(seconds=1)]
    if after:
        return Result(DIFFERS, f"asked for emails up to {cut:%d %b %H:%M} UTC, Instantly also returned {len(after)} "
                               "sent after it: max_timestamp_created is ignored")
    return Result(CONFIRMED, f"{len(es)} emails from one mailbox; up to {cut:%d %b %H:%M} UTC it returned {len(got)} "
                             f"and left out the {later} sent after")


def email_fields(r: Run) -> Result:
    """timestamp_email and ue_type on the campaign emails the mailboxes sent (sync_outcomes reads both)."""
    sent = [e for e in r.emails("sent") if not from_address(e) or from_address(e) == _lower(e.get("eaccount"))]
    if not sent:
        return Result(NOT_CHECKED, f"no campaign email sent in the last {LOOKBACK.days} days")
    stamped = sum(1 for e in sent if utc(e.get("timestamp_email")))
    codes = Counter(parse.integer(e.get("ue_type")) for e in sent)
    seen = ", ".join(f"{k} ×{v}" for k, v in sorted(codes.items(), key=lambda kv: str(kv[0])))
    if stamped < len(sent) or set(codes) != {UE_CAMPAIGN}:
        return Result(DIFFERS, f"{len(sent)} sent emails: timestamp_email on {stamped}; ue_type {seen}")
    return Result(CONFIRMED, f"{len(sent)} sent emails, every one with timestamp_email and ue_type {UE_CAMPAIGN}")


def auto_reply_field(r: Run) -> Result:
    """is_auto_reply on received emails: whether Instantly gives its own auto-reply flag."""
    got = r.emails("received")
    if not got:
        return Result(NOT_CHECKED, f"no email received by the mailboxes in the last {LOOKBACK.days} days")
    flagged = [e for e in got if "is_auto_reply" in e]
    values = Counter(str(e.get("is_auto_reply")) for e in flagged)
    if not flagged:
        return Result(DIFFERS, f"{len(got)} received emails, none with is_auto_reply (keys include "
                               f"{', '.join(sorted(map(str, got[0]))[:12])})")
    return Result(CONFIRMED, f"{len(flagged)} of {len(got)} received emails carry is_auto_reply ("
                             + ", ".join(f"{k} ×{v}" for k, v in sorted(values.items())) + ")")


def reply_campaign(r: Run) -> Result:
    """Does a reply carry the campaign's id? Read the replies the seed inboxes sent to a seed email."""
    seed_boxes = {_lower(lead.get("email")) for _, lead in r.seed_leads()}
    replies = [e for e in r.emails("received") if from_address(e) in seed_boxes]
    if not replies:
        return Result(NOT_CHECKED, "no reply from a seed inbox yet: reply to a seed email from its inbox (any words), "
                                   "wait five minutes, run this again")
    ours = set(r.campaigns())
    tagged = [e for e in replies if str(e.get("campaign_id") or "") in ours]
    if not tagged:
        return Result(DIFFERS, f"{len(replies)} replies from a seed inbox, none with a US Outbound campaign_id: "
                               "Instantly.reply would refuse to answer them")
    return Result(CONFIRMED, f"{len(tagged)} of {len(replies)} replies from a seed inbox carry their campaign's id")


def _sender_name(e: Mapping[str, Any]) -> str:
    raw = e.get("from_address_json")
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, Mapping) else []
    return next((" ".join(str(i.get("name")).split()) for i in items
                 if isinstance(i, Mapping) and str(i.get("name") or "").strip()), "")


def from_name(r: Run) -> Result:
    """The From name on the latest email each mailbox sent since the names were set: its owner's full name?"""
    latest: dict[str, dict] = {}
    for e in r.emails("sent"):
        box, when = _lower(e.get("eaccount")), email_time(e)
        if when and when >= NAMES_SET and (box not in latest or when > email_time(latest[box])):
            latest[box] = e
    if not latest:
        return Result(NOT_CHECKED, "no campaign email sent since 6 Oct; the next seed or prospect email shows it")
    named = {box: _sender_name(e) for box, e in latest.items()}
    if not any(named.values()):
        return Result(NOT_CHECKED, "Instantly gives no from_address_json name on sent emails: read the From line of "
                                   "the latest seed email in its inbox")
    wrong = [f"{r.owner_of(box) or _id(box)}'s mailbox sent as {name!r}" for box, name in named.items()
             if name and " ".join(name.split()) != " ".join(r.owner_of(box).split())]
    if wrong:
        return Result(DIFFERS, "; ".join(wrong))
    return Result(CONFIRMED, f"the latest email from {len(named)} mailboxes went out under its owner's full name")


def seed_unsubscribed(r: Run) -> Result:
    """A seed lead whose unsubscribe link was clicked reads back status -2 (the opt-out sync_outcomes honours)."""
    leads = r.seed_leads()
    if not leads:
        return Result(NOT_CHECKED, "no seed lead in the US Outbound campaigns")
    done = [lead for _, lead in leads if parse.integer(lead.get("status")) == LEAD_UNSUBSCRIBED]
    if not done:
        return Result(NOT_CHECKED, f"{len(leads)} seed leads, none unsubscribed yet (statuses "
                                   + ", ".join(sorted({str(lead.get('status')) for _, lead in leads})) + ")")
    return Result(CONFIRMED, f"{len(done)} of {len(leads)} seed leads read back unsubscribed (status -2)")


# -- Instantly: writes on the seed lead (--seed, live only) -------------------------------------------------


def _earlier_pause(r: Run, lead_id: str) -> dict | None:
    """The pause an earlier phase0 check made and read back on this lead, from its heartbeat."""
    best = None
    for row in r.ctx.store.select("heartbeats", {"job": JOB}):
        pause = ((row.get("detail") or {}) if isinstance(row.get("detail"), Mapping) else {}).get("seed_pause") or {}
        for p in pause.values() if isinstance(pause, Mapping) else ():
            if isinstance(p, Mapping) and p.get("lead_id") == lead_id and p.get("confirmed"):
                if best is None or str(p.get("paused_at")) > str(best.get("paused_at")):
                    best = dict(p)
    return best


def seed_pause(r: Run, address: str) -> Result:
    """LEAD_PAUSE_CONFIRMED, first half: PATCH /leads/{id} takes status 2 and then 1, each read back."""
    found = r.seed_lead(address)
    if found is None:
        return Result(NOT_CHECKED, f"no seed lead for {_id(address)} in the US Outbound campaigns: `us-outbound seed "
                                   "send ADDRESS --owner NAME --live` adds one")
    name, lead = found
    lead_id = str(lead.get("id") or "")
    earlier = _earlier_pause(r, lead_id)
    if earlier:
        return Result(CONFIRMED, f"paused and set active again on {str(earlier['paused_at'])[:16]} UTC by an earlier "
                                 "run, each read back (not repeated, so SEED-RESUME can watch its next step)")
    status = parse.integer(lead.get("status"))
    if status != LEAD_ACTIVE:
        why = {LEAD_UNSUBSCRIBED: "unsubscribed", 3: "completed", LEAD_PAUSED: "paused"}.get(status, f"at status {status}")
        return Result(NOT_CHECKED, f"this seed lead is {why}: setting it active again would restart its emails. Use a "
                                   "seed lead still in its sequence (`seed send ... --live`, then wait for email 1)")
    inst = r.ctx.clients.instantly
    before = r.get_lead(name, lead_id)
    paused = active = None
    try:
        inst.set_lead_paused(name, lead_id, True)
        paused = parse.integer(r.get_lead(name, lead_id).get("status"))
    except ApiError as exc:
        return Result(DIFFERS, f"Instantly refused PATCH /leads/{{id}} with status 2: HTTP {exc.status} "
                               f"{clip(str(exc.body), 160)}")
    finally:
        try:
            inst.set_lead_paused(name, lead_id, False)  # always set it going again, whatever the pause did
            active = parse.integer(r.get_lead(name, lead_id).get("status"))
        except ApiError as exc:
            log("phase0_seed_restore_failed", lead=_id(address), status=exc.status)
    left = "" if active == LEAD_ACTIVE else (f" The seed lead reads status {active} now: set it active in Instantly by "
                                             "hand.")
    if paused != LEAD_PAUSED:
        return Result(DIFFERS, f"Instantly answered the PATCH but the lead read back status {paused}, not 2: it does "
                               f"not take a lead's status there.{left}")
    if active != LEAD_ACTIVE:
        return Result(DIFFERS, f"status 2 read back, but setting 1 again read back {active}.{left}")
    r.record.setdefault("seed_pause", {})[_id(address)] = {
        "lead_id": lead_id, "campaign": name, "confirmed": True, "paused_at": r.ctx.now.isoformat(),
        "last_contact": str(before.get("timestamp_last_contact") or "")}
    return Result(CONFIRMED, "PATCH /leads/{id} took status 2 (read back paused) and 1 (read back active)")


def seed_resume(r: Run, address: str) -> Result:
    """LEAD_PAUSE_CONFIRMED, second half: did the lead paused and set active again go on with its next step?"""
    found = r.seed_lead(address)
    if found is None:
        return Result(NOT_CHECKED, f"no seed lead for {_id(address)}")
    name, lead = found
    earlier = _earlier_pause(r, str(lead.get("id") or ""))
    if not earlier:
        return Result(NOT_CHECKED, "no earlier pause of this seed lead: this run's SEED-PAUSE starts it, and a run on or "
                                   "after its next step's day reads the answer")
    at = utc(earlier.get("paused_at"))
    last = utc(lead.get("timestamp_last_contact"))
    before = utc(earlier.get("last_contact"))
    due = max(at, before + timedelta(days=STEP_GAP)) if before else at + timedelta(days=STEP_GAP)
    if last and last > at:
        return Result(CONFIRMED, f"paused and set active on {at:%d %b}; Instantly emailed it again on {last:%d %b}")
    if parse.integer(lead.get("status")) != LEAD_ACTIVE:
        return Result(DIFFERS, f"set active on {at:%d %b}, it now reads status {lead.get('status')} and has not been "
                               "emailed since")
    if r.ctx.now > due + RESUME_GRACE:
        return Result(DIFFERS, f"set active on {at:%d %b}, its next step was due about {due:%d %b} and has not gone out")
    return Result(NOT_CHECKED, f"its next step is due about {due:%a %d %b}: run this again with the same --seed after "
                               "that (or read the seed inbox)")


def _interest(lead: Mapping[str, Any]) -> Any:
    return lead.get(INTEREST_FIELD)


def seed_interest(r: Run, address: str) -> Result:
    """mark_interested and stop_lead: POST /leads/update-interest-status takes INTEREST_INTERESTED (1, set on a
    positive reply; Harry, 7 Oct 2026) and then INTEREST_MEETING_BOOKED (2), and the lead reads each back."""
    found = r.seed_lead(address)
    if found is None:
        return Result(NOT_CHECKED, f"no seed lead for {_id(address)}")
    name, lead = found
    if parse.integer(lead.get("status")) == LEAD_ACTIVE:
        return Result(NOT_CHECKED, "this seed lead is still in its sequence, and \"Meeting booked\" may end it (the pause "
                                   "test watches it): give a seed lead that has finished, e.g. the unsubscribed one")
    inst, lead_id = r.ctx.clients.instantly, str(lead.get("id") or "")
    before = r.get_lead(name, lead_id)
    was, status_was = _interest(before), parse.integer(before.get("status"))
    now, read = before, {}
    try:
        for value, call in ((INTEREST_INTERESTED, inst.mark_interested), (INTEREST_MEETING_BOOKED, inst.stop_lead)):
            try:
                call(name, address)
            except ApiError as exc:
                return Result(DIFFERS, f"Instantly refused update-interest-status with {value}: HTTP {exc.status} "
                                       f"{clip(str(exc.body), 160)}")
            seen = None
            for i in range(INTEREST_POLLS):
                now = r.get_lead(name, lead_id)
                seen = _interest(now)
                if parse.integer(seen) == value:
                    break
                if i < INTEREST_POLLS - 1:
                    SLEEP(INTEREST_WAIT)
            read[value] = seen
    finally:
        restored = _restore_interest(r, name, lead, address, was)
    keys = "" if INTEREST_FIELD in now else f" (the lead has no {INTEREST_FIELD}; its keys: {', '.join(sorted(now))[:300]})"
    status_now = parse.integer(now.get("status"))
    changed = f" Its status went from {status_was} to {status_now}." if status_now != status_was else ""
    back = "" if restored else f" It did not read back as {was!r} again yet: check the seed lead in Instantly."
    wrong = [f"{v} read {read.get(v)!r}" for v in (INTEREST_INTERESTED, INTEREST_MEETING_BOOKED)
             if parse.integer(read.get(v)) != v]
    if wrong:
        return Result(DIFFERS, f"Instantly took the calls, but after {INTEREST_POLLS * INTEREST_WAIT:.0f} s each the lead's "
                               f"{INTEREST_FIELD}: {'; '.join(wrong)}.{keys}{changed}{back}")
    return Result(CONFIRMED, f"update-interest-status took interest_value {INTEREST_INTERESTED} (\"Interested\") and "
                             f"{INTEREST_MEETING_BOOKED} (\"Meeting booked\") and the lead read each back; set back to "
                             f"{was!r}.{changed}{back}")


def _restore_interest(r: Run, name: str, lead: Mapping[str, Any], address: str, was: Any) -> bool:
    """Set the seed lead's interest status back to what it was; True once it reads back so."""
    payload = {"lead_email": address, "campaign_id": str(lead.get("campaign") or ""), "interest_value": was}
    try:
        r.ctx.clients.instantly.request(
            "POST", "/leads/update-interest-status",
            Op("lead.update", target=name, write=True, detail={"lead_id": lead.get("id"), "interest_value": was}),
            json=payload, dry_result={"dry_run": True})
        for i in range(INTEREST_POLLS):
            if _interest(r.get_lead(name, str(lead.get("id") or ""))) == was:
                return True
            if i < INTEREST_POLLS - 1:
                SLEEP(INTEREST_WAIT)
    except ApiError as exc:
        log("phase0_seed_restore_failed", lead=_id(address), status=exc.status)
    return False


def seed_forward(r: Run, address: str) -> Result:
    """POST /emails/forward on our plan: email 1 of the seed lead, forwarded to escalation_email only."""
    to = r.ctx.settings.general.escalation_email.strip()
    if not to:
        return Result(NOT_CHECKED, "escalation_email is blank on the General tab")
    earlier = _earlier_forward(r)
    if earlier:
        return Result(CONFIRMED, f"Instantly took a forward on {earlier[:10]}, in an earlier run (not repeated)")
    sent = [e for e in r.emails("sent")
            if address in {_lower(x) for x in (e.get("lead"), e.get("to_address_email_list")) if x}]
    if not sent:
        return Result(NOT_CHECKED, f"no email to {_id(address)} in the last {LOOKBACK.days} days to forward")
    first = min(sent, key=lambda e: email_time(e) or r.ctx.now)
    note = "phase0 check: a test of Instantly's forward endpoint (SEED-FORWARD), from a seed email. Nothing to do."
    try:
        r.ctx.clients.instantly.forward(_lower(first.get("eaccount")), str(first.get("id")), to, note)
    except ApiError as exc:
        return Result(DIFFERS, f"Instantly answered the forward with HTTP {exc.status}: {clip(str(exc.body), 160)}")
    r.record["seed_forward"] = {"confirmed": True, "at": r.ctx.now.isoformat()}
    return Result(CONFIRMED, "Instantly took the forward: it should reach escalation_email within minutes")


def _earlier_forward(r: Run) -> str:
    """When an earlier phase0 check's forward was taken, from its heartbeat; "" if none was."""
    for row in r.ctx.store.select("heartbeats", {"job": JOB}):
        done = ((row.get("detail") or {}) if isinstance(row.get("detail"), Mapping) else {}).get("seed_forward") or {}
        if isinstance(done, Mapping) and done.get("confirmed"):
            return str(done.get("at") or "")
    return ""


# -- HubSpot and Slack ---------------------------------------------------------------------------------------


def association_ids(r: Run) -> Result:
    """Each association type id the client sends is a HubSpot-defined one for that pair (v4 labels)."""
    hs, wrong = r.ctx.clients.hubspot, []
    for (src, dst), want in ASSOCIATION_TYPE_IDS.items():
        body = hs.request("GET", f"/crm/v4/associations/{src}/{dst}/labels",
                          Op("association.labels", target=f"{src}/{dst}")) or {}
        rows = {parse.integer(x.get("typeId")): x for x in body.get("results") or ()
                if x.get("category") == "HUBSPOT_DEFINED"}
        got = rows.get(want)
        if got is None:
            wrong.append(f"{src}→{dst}: {want} is not listed (HubSpot lists {sorted(k for k in rows if k)})")
        elif (src, dst) == ("deals", "companies") and "primary" not in _lower(got.get("label")):
            wrong.append(f"deals→companies: {want} is labelled {got.get('label')!r}, not Primary")
    if wrong:
        return Result(DIFFERS, "; ".join(wrong))
    return Result(CONFIRMED, f"all {len(ASSOCIATION_TYPE_IDS)} ids are HubSpot-defined for their pair; deals→companies 5 "
                             "is Primary")


def _deal_total(r: Run, groups: list[dict]) -> int:
    body = r.ctx.clients.hubspot.request(
        "POST", "/crm/v3/objects/deals/search", Op("deal.search", target="deals"),
        json={"filterGroups": groups, "properties": ["dealname"], "limit": 1}) or {}
    return int(body.get("total") or 0)


def deals_by_company(r: Run) -> Result:
    """The deal search takes the associations.company pseudo-property (verify's and enrol's open-deal check)."""
    company = next((str(a["hubspot_company_id"]) for a in r.ctx.store.select("accounts")
                    if a.get("hubspot_company_id")), "")
    if not company:
        found = r.ctx.clients.hubspot.search_companies_by_domain("spill.chat")
        company = str(found[0]["id"]) if found else ""
    if not company:
        return Result(NOT_CHECKED, "no HubSpot company id to search by")
    try:
        one = _deal_total(r, [{"filters": [{"propertyName": "associations.company", "operator": "EQ",
                                             "value": company}]}])
    except ApiError as exc:
        return Result(DIFFERS, f"HubSpot refused the filter: HTTP {exc.status} {clip(str(exc.body), 160)}")
    every = _deal_total(r, [])
    if every > 1 and one >= every:
        return Result(DIFFERS, f"one company's search counted {one} deals, as many as the whole portal ({every}): the "
                               "filter was ignored")
    return Result(CONFIRMED, f"HubSpot took associations.company: {one} deals for one company, of {every} in all")


def slack_scopes(r: Run) -> Result:
    """The installed app's scopes (auth.test's X-OAuth-Scopes header) hold what the manifest asks for."""
    slack = r.ctx.clients.slack
    if not hasattr(slack, "request"):
        return Result(NOT_CHECKED, "no Slack token here (US_OUTBOUND_SLACK_BOT_TOKEN)")
    resp = slack.request("GET", "auth.test", Op("auth.test"), raw=True)
    headers = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}
    if resp.status >= 300 or not isinstance(resp.body, dict) or not resp.body.get("ok"):
        return Result(DIFFERS, f"auth.test answered {resp.status} {clip(str(resp.body), 120)}")
    if "x-oauth-scopes" not in headers:
        return Result(NOT_CHECKED, "Slack sent no X-OAuth-Scopes header: read the scopes on the app's OAuth page")
    have = {s.strip() for s in headers["x-oauth-scopes"].split(",") if s.strip()}
    missing = [s for s in SLACK_SCOPES if s not in have]
    if missing:
        return Result(DIFFERS, f"the installed app lacks {', '.join(missing)}: reinstall it from "
                               "deploy/slack-app-manifest.yaml")
    return Result(CONFIRMED, f"the installed app has all {len(SLACK_SCOPES)} manifest scopes, reactions:read and "
                             "im:write among them")


# -- Apollo: free reads ----------------------------------------------------------------------------------------


def apollo_usage(r: Run) -> Result:
    """credit_usage_stats: lead_credit and inbound_website_visitor_credit, each with limit and left_over."""
    usage = r.ctx.clients.apollo.credit_usage()
    left, visitors = credits_left(usage), credit_stats(usage, VISITOR_CREDIT)
    lead = credit_stats(usage, "lead_credit")
    types = ", ".join(sorted((usage.get("credit_usage_stats") or {}).keys()))[:300]
    if left is None or lead is None:
        return Result(DIFFERS, f"no lead_credit with limit and left_over; the credit types given: {types}")
    r.record["apollo_lead_consumed"] = lead["consumed"]
    if visitors is None:
        return Result(DIFFERS, f"lead_credit reads ({left:,.0f} left), but no {VISITOR_CREDIT} with limit and left_over; "
                               f"the types given: {types}")
    return Result(CONFIRMED, f"lead_credit {left:,.0f} left of {lead['limit']:,.0f}; {VISITOR_CREDIT} "
                             f"{visitors['left_over']:,.0f} left of {visitors['limit']:,.0f}")


def apollo_rate_limits(r: Run) -> Result:
    """The plan's rate limits, from the headers of one free People API Search (apollo_people paces 40 a minute)."""
    from us_outbound.contacts.pick import organization_filter
    from us_outbound.sources.apollo_people import MAX_REQUESTS_PER_RUN, PACE_SECONDS

    # The filter pick_contacts sends for an account with no Apollo id (live since 2 Oct), for Spill's own domain.
    body = {**organization_filter({"domain": "spill.chat"}), "page": 1, "per_page": 1}
    resp = r.ctx.clients.apollo.request(
        "POST", "/mixed_people/api_search", Op("people.search", target="mixed_people/api_search",
                                               detail={"page": 1, "filters": sorted(body)}), json=body, raw=True)
    if resp.status >= 300:
        return Result(DIFFERS, f"People API Search answered HTTP {resp.status}")
    limits = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()
              if any(w in str(k).lower() for w in ("rate-limit", "usage", "requests-left"))}
    if not limits:
        return Result(NOT_CHECKED, "Apollo sent no rate-limit headers; the plan's limits are on Apollo's API page")
    shown = "; ".join(f"{k} {v}" for k, v in sorted(limits.items()))
    minute = next((parse.integer(v) for k, v in limits.items() if "minute" in k and "limit" in k), None)
    pace = int(60 / PACE_SECONDS)
    if minute is not None and minute < pace:
        return Result(DIFFERS, f"{shown}: under apollo_people's {pace} a minute")
    return Result(CONFIRMED, f"{shown} (apollo_people: {pace} a minute, {MAX_REQUESTS_PER_RUN} a run)")


def enrich_not_found(r: Run) -> Result:
    """A domain Apollo does not know: what organizations/enrich answers (apollo_enrich reads 404 as not found)."""
    try:
        body = r.ctx.clients.apollo.enrich_organization(UNKNOWN_DOMAIN)
    except ApiError as exc:
        if exc.status == 404:
            return Result(CONFIRMED, "404 for a domain Apollo does not know, which apollo_enrich reads as not found "
                                     "(no credit)")
        return Result(DIFFERS, f"HTTP {exc.status} for a domain Apollo does not know: apollo_enrich counts that as a "
                               "failed call, not \"not found\"")
    if enriched_in(body):
        _paid(r, "enrich_not_found")
        return Result(NOT_CHECKED, f"Apollo knows {UNKNOWN_DOMAIN} (1 credit): change UNKNOWN_DOMAIN")
    return Result(CONFIRMED, f"200 with no organization (keys {', '.join(sorted(map(str, body)))[:120] or 'none'}), "
                             "which enriched_in reads as not found")


def bulk_fields(r: Run) -> Result:
    """The bulk enrich answer's keys, for one domain Apollo does not know (no credit)."""
    try:
        body = r.ctx.clients.apollo.bulk_enrich_organizations([UNKNOWN_DOMAIN])
    except ApiError as exc:
        return Result(DIFFERS, f"bulk_enrich answered HTTP {exc.status} for one unknown domain")
    keys = sorted(map(str, body))
    if "organizations" not in body:
        return Result(DIFFERS, f"no organizations key (keys: {', '.join(keys)[:200]})")
    counts = [k for k in ("unique_enriched_records", "credits_consumed") if k in body]
    return Result(CONFIRMED, f"organizations is there (keys: {', '.join(keys)[:200]}); "
                             + ("it reports " + " and ".join(counts) if counts else
                                "it reports neither unique_enriched_records nor credits_consumed, so the job settles at "
                                "the records it carries"))


# -- Apollo: what the live jobs stored (free) -------------------------------------------------------------------


def _facts(r: Run, source: str, fact: str) -> list[dict]:
    return r.ctx.store.select("signal_events", {"source": source, "fact": fact})


def _searched_only(r: Run) -> set[str]:
    """Accounts source_universe found that apollo_enrich never answered: their apollo_org facts are the search row's."""
    from us_outbound.sources import apollo_enrich as enrich
    from us_outbound.sources import apollo_universe as uni

    found = {str(a["account_id"]) for a in r.ctx.store.select("accounts", {"source": uni.ACCOUNT_SOURCE})}
    return found - {str(e["account_id"]) for e in _facts(r, uni.SOURCE, enrich.MARKER)}


def _row_fact(r: Run, fact: str, what: str) -> Result:
    from us_outbound.sources import apollo_universe as uni

    pool = _searched_only(r)
    if len(pool) < MIN_SAMPLE:
        return Result(NOT_CHECKED, f"only {len(pool)} accounts found by search and never enriched")
    have = pool & {str(e["account_id"]) for e in _facts(r, uni.SOURCE, fact)}
    if not have:
        return Result(DIFFERS, f"none of {len(pool)} accounts found by search (never enriched) has {what} from its row")
    return Result(CONFIRMED, f"{len(have)} of {len(pool)} accounts found by search (never enriched) have {what} from "
                             "their search row")


def row_naics(r: Run) -> Result:
    return _row_fact(r, "naics", "NAICS codes")


def row_description(r: Run) -> Result:
    return _row_fact(r, "description", "a description")


def growth_fraction(r: Run) -> Result:
    """headcount_growth_12m as stored: a fraction (0.12 for 12%), or a percent."""
    from us_outbound.sources import apollo_universe as uni

    values = [float(v) for e in _facts(r, uni.SOURCE, "headcount_growth_12m")
              if isinstance(v := e.get("value"), (int, float)) and not isinstance(v, bool)]
    if len(values) < MIN_SAMPLE:
        return Result(NOT_CHECKED, f"only {len(values)} growth figures stored")
    big = sum(1 for v in values if abs(v) > 3)
    text = f"{len(values)} figures from {min(values):g} to {max(values):g}, median {statistics.median(values):g}"
    if big > len(values) * 0.2:
        return Result(DIFFERS, f"{text}: {big} above 3 in size, so they are percents, not fractions")
    return Result(CONFIRMED, f"{text}: fractions (0.12 is 12%)")


def funding_amount(r: Run) -> Result:
    """Enrich's funding: a dated round's stage and its amount, read from text like "8M"."""
    from us_outbound.sources import apollo_universe as uni

    dated = {str(e["account_id"]) for e in _facts(r, uni.SOURCE, "days_since_funding")}
    if len(dated) < 3:
        return Result(NOT_CHECKED, f"only {len(dated)} accounts with a dated funding round stored")
    amount = dated & {str(e["account_id"]) for e in _facts(r, uni.SOURCE, "funding_amount_usd")}
    stage = dated & {str(e["account_id"]) for e in _facts(r, uni.SOURCE, "funding_stage")}
    if not amount:
        return Result(DIFFERS, f"{len(dated)} accounts with a dated round, none with an amount read "
                               f"({len(stage)} with a stage)")
    return Result(CONFIRMED, f"of {len(dated)} accounts with a dated round, {len(amount)} have an amount and "
                             f"{len(stage)} a stage")


def postings_key(r: Run) -> Result:
    """Job postings: titles read from the page's list (postings_in), for accounts Apollo lists postings for."""
    from us_outbound.sources import apollo_jobs as jobs

    roles = {str(e["account_id"]) for e in _facts(r, jobs.SOURCE, "open_roles")
             if (parse.integer(e.get("value")) or 0) > 0}
    if not roles:
        return Result(NOT_CHECKED, "no account with open postings stored yet")
    titled = roles & {str(e["account_id"]) for e in _facts(r, jobs.SOURCE, "posting_titles") if e.get("value")}
    if not titled:
        return Result(DIFFERS, f"{len(roles)} accounts with open roles, none with a posting title: the page's list is "
                               "under another key")
    return Result(CONFIRMED, f"{len(titled)} of {len(roles)} accounts with open roles have posting titles from the list")


def job_boards(r: Run) -> Result:
    """The public job boards' shapes (sources/job_posts.py): posting text read from each vendor's feed."""
    from us_outbound.sources import job_posts

    hosts = {"greenhouse": job_posts.GREENHOUSE, "lever": job_posts.LEVER, "ashbyhq": job_posts.ASHBY,
             "workable": job_posts.WORKABLE}
    seen: Counter[str] = Counter()
    for e in _facts(r, job_posts.SOURCE, "posting_text"):
        host = str(e.get("source_url") or "").split("://", 1)[-1].split("/", 1)[0].lower()
        vendor = next((v for k, v in hosts.items() if k in host), None)
        if vendor and (e.get("value") or {}).get("text"):
            seen[vendor] += 1
    if not seen:
        return Result(NOT_CHECKED, "no posting text from a job board stored yet")
    rest = [v for v in job_posts.VENDORS if v not in seen]
    return Result(CONFIRMED, ", ".join(f"{v} {n} postings" for v, n in sorted(seen.items())) + " read with text"
                  + (f"; not seen yet: {', '.join(rest)}" if rest else ""))


# -- Apollo: probes that cost a credit (--apollo-credits) --------------------------------------------------------


def _paid(r: Run, key: str) -> None:
    """A paid answer: counted for APO-CREDIT-COST, and in credit_ledger as the jobs count theirs (budget.py), so the
    month's Apollo budget sees it."""
    r.record.setdefault("apollo_paid", []).append(key)
    r.ctx.store.insert("credit_ledger", [{
        "entry_id": new_id(), "system": "apollo", "job": JOB, "run_id": r.ctx.run_id, "account_id": None,
        "credits": 1.0, "usd": None, "occurred_at": r.ctx.now, "note": f"phase0 check: {key}"}])


def search_row(r: Run) -> Result:
    """One organization search page in one HQ state (1 credit): rows in that state, and the fields rows carry."""
    from us_outbound.sources import apollo_universe as uni

    state = next(iter(sorted(uni.allowed_states(r.ctx.settings))), "GA")
    page = r.ctx.clients.apollo.search_organizations({"organization_locations": [uni.location(state)]}, per_page=10)
    rows = organizations_in(page)
    if not rows:
        return Result(NOT_CHECKED, f"no company came back for {uni.location(state)} (no credit)")
    _paid(r, "search_row")
    there = sum(1 for o in rows if state_code(str(o.get("state") or "")) == state)
    fields = {f: sum(1 for o in rows if o.get(f) not in (None, "", [])) for f in
              ("state", "country", "naics_codes", "short_description", "estimated_num_employees",
               "organization_headcount_twelve_month_growth")}
    shape = ", ".join(f"{k} {v}/{len(rows)}" for k, v in fields.items())
    if there < len(rows) * 0.9:
        return Result(DIFFERS, f"{there} of {len(rows)} rows in {state} for {uni.location(state)!r}; rows carry {shape}")
    return Result(CONFIRMED, f"{there} of {len(rows)} rows in {state} for {uni.location(state)!r}; rows carry {shape}")


def lookalike_page(r: Run) -> Result:
    """One lookalike page (1 credit if it returns any): the body key, US-only locations, seeds left out."""
    from us_outbound.sources import lookalike_leads as ll

    ids = [str(a["apollo_org_id"]) for a in r.ctx.store.select("accounts", {"status": ["queued", "verified"]})
           if a.get("apollo_org_id")][:5]
    if not ids:
        return Result(NOT_CHECKED, "no queued account with an Apollo id to seed it")
    page = r.ctx.clients.apollo.search_lookalike_organizations(
        ids, {"organization_locations": [ll.US_LOCATION], "organization_not_locations": list(ll.NOT_LOCATIONS)},
        per_page=10)
    rows = organizations_in(page)
    if not rows:
        return Result(NOT_CHECKED, f"no lookalike came back for {len(ids)} seeds (no credit): try again later")
    _paid(r, "lookalike")
    off = [o for o in rows if not ll.us_hq(o)]
    never = [o for o in rows if state_code(str(o.get("state") or "")) in {"CA", "WA"}]
    echoed = [o for o in rows if str(o.get("organization_id")) in ids]
    if off or never or echoed:
        return Result(DIFFERS, f"{len(rows)} lookalikes: {len(off)} outside the US, {len(never)} in CA or WA, "
                               f"{len(echoed)} of the seeds themselves")
    return Result(CONFIRMED, f"{len(rows)} lookalikes for {len(ids)} seeds, all in the US outside CA and WA, no seed "
                             "among them")


def enrich_one(r: Run) -> Result:
    """One single enrich of spill.chat (1 credit): the answer's organization key and its funding fields."""
    body = r.ctx.clients.apollo.enrich_organization("spill.chat")
    rows = enriched_in(body)
    if not rows:
        return Result(DIFFERS, f"no organization in the answer (keys {', '.join(sorted(map(str, body)))[:120]})")
    _paid(r, "enrich")
    org = rows[0]
    have = [k for k in ("latest_funding_round_date", "latest_funding_stage", "funding_events",
                        "organization_headcount_twelve_month_growth", "estimated_num_employees") if k in org]
    return Result(CONFIRMED, "{\"organization\": {...}} with " + (", ".join(have) or "none of the funding fields"))


def credit_cost(r: Run) -> Result:
    """Apollo's lead credits used, before and after the paid probes: one a paid answer."""
    before = r.record.get("apollo_lead_consumed")
    paid = len(r.record.get("apollo_paid") or [])
    if before is None or not paid:
        return Result(NOT_CHECKED, "no paid answer, or Apollo's usage could not be read before them")
    after = credit_stats(r.ctx.clients.apollo.credit_usage(), "lead_credit")
    if after is None:
        return Result(NOT_CHECKED, "Apollo's usage could not be read after them")
    used = after["consumed"] - before
    if used == paid:
        return Result(CONFIRMED, f"{paid} paid answers, {used:g} lead credits used")
    if used == 0:
        return Result(NOT_CHECKED, f"Apollo's usage had not moved yet after {paid} paid answers (it can lag): read its "
                                   "usage page")
    return Result(DIFFERS, f"{paid} paid answers, {used:g} lead credits used (someone else may have used Apollo meanwhile)")


def clay_pointer(r: Run) -> Result:
    return Result(NOT_CHECKED, "covered by `us-outbound clay check-email --first YOUR_FIRST_NAME --last YOUR_LAST_NAME "
                               "--domain spill.chat --live` (one Work Email lookup; Clay charges only if it finds one)")


# -- the probes, gates first -------------------------------------------------------------------------------------

INSTANTLY = "https://api.instantly.ai/api/v2"
PROBES: tuple[Probe, ...] = (
    Probe("INST-LEAD-UNSUB", "a clicked unsubscribe link reads back as lead status -2", f"POST {INSTANTLY}/leads/list "
          "(each US Outbound campaign; seed leads only)", seed_unsubscribed, gate=True,
          if_confirmed="",  # already marked confirmed live (6 Oct 2026); this re-reads it
          if_differs="LEAD_UNSUBSCRIBED in clients/instantly.py, and sync_outcomes' opt-out read"),
    Probe("INST-REPLY-CAMPAIGN", "a reply carries its campaign's id (Instantly.reply)", f"GET {INSTANTLY}/emails "
          "(email_type received, each registry mailbox, 14 days)", reply_campaign, gate=True,
          if_confirmed="clients/instantly.py reply(): drop the marker on campaign_id (reply_to_uuid stays Watch)",
          if_differs="clients/instantly.py reply(): find the campaign from the lead instead of the email's campaign_id"),
    Probe("INST-AUTO-REPLY", "received emails carry is_auto_reply", f"GET {INSTANTLY}/emails (email_type received)",
          auto_reply_field, gate=True,
          if_confirmed="replies/outcomes.py is_auto_reply: drop the marker",
          if_differs="replies/outcomes.py is_auto_reply: read the flag Instantly does give (poll_replies classifies "
                     "away messages meanwhile)"),
    Probe("HS-DEALS-ASSOC", "the deal search takes associations.company", "POST https://api.hubapi.com/crm/v3/objects/"
          "deals/search (one company's deals, then all deals, limit 1)", deals_by_company, gate=True,
          if_confirmed="clients/hubspot.py open_deals_for_company and deals_for_company: drop the markers",
          if_differs="clients/hubspot.py: read a company's deals through its associations (get_record) instead"),
    Probe("INST-EMAILS-UNTIL", "GET /emails applies max_timestamp_created", f"GET {INSTANTLY}/emails (email_type sent, "
          "one mailbox, twice: without and with max_timestamp_created)", emails_until,
          if_confirmed="clients/instantly.py list_emails: drop the marker on until=",
          if_differs="clients/instantly.py list_emails: until= has no effect, so sync_outcomes' catch-up reads up to now "
                     "(safe: events are idempotent); say so in the docstring"),
    Probe("INST-EMAIL-FIELDS", "sent emails carry timestamp_email and ue_type 1", f"GET {INSTANTLY}/emails "
          "(email_type sent)", email_fields,
          if_confirmed="replies/outcomes.py email_time and the ue_type codes: drop the markers",
          if_differs="replies/outcomes.py UE_CAMPAIGN / email_time: take the values the line shows"),
    Probe("INST-FROM-NAME", "Instantly sends under the account's first and last name", f"GET {INSTANTLY}/emails "
          "(email_type sent; from_address_json)", from_name,
          if_confirmed="clients/instantly.py set_sender_name, registry/mailboxes.py and replies/poll.py _sender_name: "
                       "drop the From-name and from_address_json markers (the PATCH stays Watch)",
          if_differs="set the name in Instantly by hand, then `mailbox check`; the build looks at what makes the From name"),
    Probe("HS-ASSOC-IDS", "the association type ids are HubSpot-defined", "GET https://api.hubapi.com/crm/v4/associations/"
          "{from}/{to}/labels (8 pairs)", association_ids,
          if_confirmed="clients/hubspot.py ASSOCIATION_TYPE_IDS: drop the marker",
          if_differs="clients/hubspot.py ASSOCIATION_TYPE_IDS: use the ids the line names"),
    Probe("SLK-SCOPES", "the installed Slack app has reactions:read and im:write", "GET https://slack.com/api/auth.test "
          "(its X-OAuth-Scopes header)", slack_scopes,
          if_confirmed="clients/slack.py reactions() and dm(): drop the scope markers (the Messages tab stays Watch)",
          if_differs="reinstall the Slack app from deploy/slack-app-manifest.yaml"),
    Probe("APO-USAGE", "credit usage: lead_credit and inbound_website_visitor_credit", "POST https://api.apollo.io/api/v1/"
          "usage_stats/credit_usage_stats (0 credits)", apollo_usage,
          if_confirmed="clients/apollo.py credit_stats: drop the marker on inbound_website_visitor_credit",
          if_differs="clients/apollo.py credits_left / VISITOR_CREDIT: the credit type the line names"),
    Probe("APO-RATE-LIMITS", "the plan's People API Search rate limits", "POST https://api.apollo.io/api/v1/mixed_people/"
          "api_search (per_page 1, 0 credits; its rate-limit headers)", apollo_rate_limits,
          if_confirmed="sources/apollo_people.py docstring: write the limits in, drop the marker",
          if_differs="sources/apollo_people.py PACE_SECONDS: slow it to the per-minute limit"),
    Probe("APO-ENRICH-NOTFOUND", "organizations/enrich for a company Apollo does not know", "GET https://api.apollo.io/"
          f"api/v1/organizations/enrich?domain={UNKNOWN_DOMAIN} (0 credits: none found)", enrich_not_found,
          if_confirmed="sources/apollo_enrich.py (404) and clients/apollo.py enriched_in: drop the markers",
          if_differs="sources/apollo_enrich.py single_enrich: read the status the line shows as not found"),
    Probe("APO-BULK-FIELDS", "the bulk enrich answer's keys", "POST https://api.apollo.io/api/v1/organizations/bulk_enrich "
          f"({UNKNOWN_DOMAIN} alone, 0 credits)", bulk_fields,
          if_confirmed="sources/apollo_enrich.py bulk_enrich docstring: drop the marker",
          if_differs="clients/apollo.py enriched_in: read the key the line shows"),
    Probe("APO-ROW-NAICS", "organization search rows carry naics_codes", "the database: apollo_org naics facts of accounts "
          "found by search and never enriched", row_naics, kind=STORED,
          if_confirmed="sources/apollo_universe.py org_naics: drop the marker",
          if_differs="sources/apollo_universe.py org_naics: say rows carry none (labels then come from keywords)"),
    Probe("APO-ROW-DESCRIPTION", "organization search rows carry short_description", "the database: apollo_org "
          "description facts of accounts found by search and never enriched", row_description, kind=STORED,
          if_confirmed="sources/apollo_universe.py org_description: drop the marker",
          if_differs="sources/apollo_universe.py org_description: rows carry none; opener_focus waits for enrich"),
    Probe("APO-GROWTH-FRACTION", "headcount_growth_12m is a fraction", "the database: stored headcount_growth_12m facts",
          growth_fraction, kind=STORED,
          if_confirmed="sources/apollo_universe.py and sources/lookalikes.py growth_band_of: drop the markers",
          if_differs="sources/apollo_universe.py org_facts: divide the growth by 100 when it is stored"),
    Probe("APO-FUNDING-AMOUNT", "enrich's funding round date, stage and text amount", "the database: stored funding facts",
          funding_amount, kind=STORED,
          if_confirmed="sources/apollo_universe.py funding_usd and org_funding: drop the markers",
          if_differs="sources/apollo_universe.py funding_usd: read the amount as enrich gives it"),
    Probe("APO-POSTINGS-KEY", "job postings come under organization_job_postings", "the database: stored apollo_jobs facts",
          postings_key, kind=STORED,
          if_confirmed="clients/apollo.py postings_in: drop the marker",
          if_differs="clients/apollo.py postings_in: the list's key"),
    Probe("JOB-BOARDS", "the job boards' feed shapes", "the database: stored job_posts posting_text facts by feed",
          job_boards, kind=STORED,
          if_confirmed="sources/job_posts.py docstring: mark the vendors seen as confirmed",
          if_differs=""),
    Probe("APO-SEARCH-ROW", "organization_locations \"<state>, US\", and the fields rows carry",
          "POST https://api.apollo.io/api/v1/mixed_companies/search (one HQ state, per_page 10: 1 credit)", search_row,
          kind=CREDITS, credits=1,
          if_confirmed="sources/apollo_universe.py location(): drop the marker",
          if_differs="sources/apollo_universe.py location(): the form the rows show"),
    Probe("APO-LOOKALIKE", "the lookalike search's body key and US-only locations",
          "POST https://api.apollo.io/api/v1/mixed_companies/search (lookalike_organization_ids, 5 seeds, per_page 10: "
          "1 credit)", lookalike_page, kind=CREDITS, credits=1,
          if_confirmed="clients/apollo.py search_lookalike_organizations and sources/lookalike_leads.py: drop the "
                       "markers on the key and the locations",
          if_differs="sources/lookalike_leads.py: filter the rows the line names after the search"),
    Probe("APO-ENRICH-ONE", "a single enrich answers {\"organization\": ...}", "GET https://api.apollo.io/api/v1/"
          "organizations/enrich?domain=spill.chat (1 credit)", enrich_one, kind=CREDITS, credits=1,
          if_confirmed="clients/apollo.py enriched_in: drop the marker on the single answer",
          if_differs="clients/apollo.py enriched_in: the key the line shows"),
    Probe("APO-CREDIT-COST", "a paid answer costs 1 lead credit", "POST https://api.apollo.io/api/v1/usage_stats/"
          "credit_usage_stats again after the paid probes (0 credits)", credit_cost, kind=CREDITS,
          if_confirmed="sources/apollo_enrich.py and clients/apollo.py search_lookalike_organizations: drop the cost "
                       "markers",
          if_differs="budget.py's per-call credits: the numbers the line shows"),
    Probe("CLAY-CHECK", "Clay's Routines API, Work Email's inputs, outputs and cost", "nothing here: `us-outbound clay "
          "check-email` makes the one lookup", clay_pointer, kind=POINTER),
)
SEED_PROBES: tuple[Probe, ...] = (
    Probe("SEED-RESUME", "a lead paused and set active again goes on with its next step", f"GET {INSTANTLY}/leads/{{id}} "
          "(the seed lead; when an earlier run paused it)", seed_resume, kind=SEED, gate=True,
          if_confirmed="with SEED-PAUSE CONFIRMED: set LEAD_PAUSE_CONFIRMED = True in clients/instantly.py (the "
                       "out-of-office pause and the account stop's pause instead of delete)",
          if_differs="keep LEAD_PAUSE_CONFIRMED = False: a lead set active again does not go on"),
    Probe("SEED-PAUSE", "PATCH /leads/{id} takes status 2 and 1", f"PATCH {INSTANTLY}/leads/{{id}} {{status: 2}}, GET it, "
          "PATCH {status: 1}, GET it (the seed lead, if it is still in its sequence)", seed_pause, kind=SEED, gate=True,
          if_confirmed="LEAD_PAUSE_CONFIRMED's first half: wait for SEED-RESUME on the date it gives",
          if_differs="keep LEAD_PAUSE_CONFIRMED = False; the build looks for another way to pause a lead"),
    Probe("SEED-INTEREST", "update-interest-status takes 2 (Meeting booked)", f"POST {INSTANTLY}/leads/update-interest-"
          "status (2, then back), GET the lead (the seed lead, if it has finished)", seed_interest, kind=SEED, gate=True,
          if_confirmed="clients/instantly.py INTEREST_MEETING_BOOKED and stop_lead: drop the marker on the "
                       "endpoint and value (\"no further steps\" stays Watch)",
          if_differs="clients/instantly.py stop_lead: use delete_lead, the stop that is certain"),
    Probe("SEED-FORWARD", "POST /emails/forward works on our plan", f"POST {INSTANTLY}/emails/forward (the seed lead's "
          "email 1, to escalation_email only)", seed_forward, kind=SEED, gate=True,
          if_confirmed="clients/instantly.py forward and replies/desk.py: drop the markers",
          if_differs="replies/desk.py: escalate by HubSpot task and Slack DM only (it falls back to them now)"),
)


# -- the run -----------------------------------------------------------------------------------------------------


def _one(r: Run, p: Probe, *args: Any) -> Result:
    """Run one probe; a failure is that probe's COULD NOT CHECK, never the run's end (a guard refusal is)."""
    try:
        return p.fn(r, *args)
    except GuardViolation:
        raise
    except ApiError as exc:
        return Result(NOT_CHECKED, f"{exc.system} answered HTTP {exc.status}: {clip(str(exc.body), 160)}")
    except ConfigError as exc:
        return Result(NOT_CHECKED, str(exc))
    except Exception as exc:  # one probe's surprise should not hide the others' answers
        return Result(NOT_CHECKED, f"the probe failed: {type(exc).__name__}: {clip(str(exc), 160)}")


def check(ctx: Context, *, seeds: Sequence[str] = (), apollo_credits: bool = False) -> dict[str, Any]:
    """Run the probes (with --live) or say what each would call. Returns the report lines() prints.

    seeds: the --seed addresses, each checked as `seed send` checks one (ours, never a contact we hold) before
    anything is called.
    """
    addresses = tuple(dict.fromkeys(seed_ops.check_address(ctx, a) for a in seeds))
    r = Run(ctx, addresses, apollo_credits)
    rows: list[dict[str, Any]] = []
    plan: list[tuple[Probe, tuple[Any, ...]]] = [(p, ()) for p in PROBES]
    forwarded = False
    for a in addresses:
        for p in SEED_PROBES:
            if p.fn is seed_forward:
                if forwarded:
                    continue
                forwarded = True
            plan.append((p, (a,)))
    for p, args in plan:
        label = p.id + (f" ({_id(args[0])})" if args else "")
        row: dict[str, Any] = {"id": p.id, "label": label, "gate": p.gate, "kind": p.kind}
        if ctx.dry_run:
            row.update(status=None, detail=f"would call: {p.calls}" + (f" [{p.credits} Apollo credit]" if p.credits else ""))
        elif p.kind == CREDITS and not apollo_credits:
            row.update(status=NOT_CHECKED, detail="needs --apollo-credits" + (f" ({p.credits} Apollo credit)"
                                                                               if p.credits else ""))
        else:
            res = _one(r, p, *args)
            row.update(status=res.status, detail=redact(res.detail))
        row["change"] = (p.if_confirmed if row["status"] == CONFIRMED else p.if_differs if row["status"] == DIFFERS
                         else "")
        rows.append(row)
    counts = Counter(row["status"] for row in rows if row["status"])
    log("phase0_check", run_id=ctx.run_id, dry_run=ctx.dry_run, results={row["label"]: row["status"] for row in rows})
    return {"job": JOB, "dry_run": ctx.dry_run, "seeds": len(addresses), "apollo_credits": apollo_credits,
            "results": rows, "counts": dict(counts), **r.record}


def lines(report: Mapping[str, Any]) -> list[str]:
    """The report in plain words: one line a probe, then what to change."""
    rows = report["results"]
    if report["dry_run"]:
        out = ["Dry-run: nothing is called. With --live each probe below makes these calls, read-only"
               + (" (and the seed lead's writes)" if report["seeds"] else "") + ":"]
        out += [f"  {row['label']}  {row['detail']}" for row in rows]
        return out + ["Add --live to run them (docs/phase0-confirm.md says what each settles)."]
    width = max(len(row["label"]) for row in rows)
    out = [f"Phase 0 check (docs/phase0-confirm.md), live"
           + (", with Apollo credits" if report["apollo_credits"] else "") + ":"]
    for row in rows:
        out.append(f"{row['label']:<{width}}  {row['status']}" + (f": {row['detail']}" if row["detail"] else ""))
    changes = sorted((row for row in rows if row["change"]), key=lambda row: not row["gate"])
    out.append("")
    out.append("What to change" + (":" if changes else ": nothing yet."))
    out += [f"  {row['label']} ({row['status']}{', a gate' if row['gate'] else ''}): {row['change']}" for row in changes]
    c = report["counts"]
    out.append("")
    out.append(f"{c.get(CONFIRMED, 0)} confirmed, {c.get(DIFFERS, 0)} differ, {c.get(NOT_CHECKED, 0)} not checked. "
               "Paste all of this back to the build.")
    return [redact(x) for x in out]
