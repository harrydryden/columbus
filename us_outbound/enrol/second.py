"""A second contact at companies of second_contact_min_employees or more staff (Harry, 6 Oct 2026: "push ahead
with building").

Multi-threading: at a company big enough for more than one buyer, a second person, of another role, gets their
own sequence. The rules live here, in one place, for pick_contacts (who to reveal), enrol (who to propose today)
and a send approval's ✅ (enrol/approvals.recheck):

  Switch    General second_contact, no by default: sending capacity is the binding limit today. With no, nothing
            here reads the database and nothing anywhere changes. second_contact_min_employees (50) is read
            against the account's company_size, the headcount the Roles tab is read against (a 50-99 band is 50).
  Who       the best-ranked person, by the Roles tab's order for the account's size, whose copy role
            (contacts.role) is not the first contact's: the founder after a People leader, a People leader
            after a founder. An HR manager writes as a People leader, so never follows one. pick_contacts reveals
            them as it reveals a first contact (contacts/pick.py: verified email, Apollo or Clay's fallback; not in
            CA or WA; not suppressed, a personal domain, a shared mailbox, a customer's domain or anyone's contact
            yet; at most two reveals; each reveal against the Apollo budget). Enrol takes them through the checks
            every contact passes (enrol.eligible, and the HubSpot re-check in enrol.prepare).
  When      never before the first contact is enrolled, and only once sync_outcomes has recorded the first
            contact's email 1 as sent: second_contact_delay_days (3) after that day (US Eastern), so the two
            people's first emails never arrive the same day. Instantly also sends to at most two people a company
            a day. The account must still be enrolled (not engaged: nobody has replied), in a queue tier, with
            nobody at it who replied, bounced, unsubscribed or complained, and no second contact yet.
  Sender    the account's sender, kept for life (queue.assign_sender), so the lead goes into the same
            "US Outbound – {owner}" campaign as the first; a paused or full sender's second contacts wait.
  Capacity  a second contact counts against today's number, the week and its sender's slots like any contact,
            but takes only what the first contacts of new accounts leave: enrol walks second contacts after them,
            and pick_contacts reveals them only when the first contacts it found fall short of its lookahead.
  Copy      its own role's Copy row, falling back as a first contact's does (the industry, its group, then
            General, each for the role, then without one); skipped when there is none, or when the row found is
            the very one the first contact got (two colleagues with the same email reads as a mail merge). Its
            own opener, for its role. The test version, opener holdout and subject arm are by account, so both
            people share them.
  Approvals the card says it is the second contact, and who the first was (enrol/approvals/).
  Stops     replies/account_stop.py: anyone's reply, bounce, unsubscribe or complaint stops both sequences.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clean.people import company_size
from us_outbound.context import Context
from us_outbound.enrol import capacity, enrol, queue
from us_outbound.settings.model import Settings
from us_outbound.timeparse import et_day, utc

FIRST, SECOND = 1, 2  # contacts.contact_slot
ENROLLED = "enrolled"
# What stops every sequence at an account (replies/account_stop.py): a reply that is not an out-of-office one, a
# bounce, an unsubscribe (link or reply), a spam complaint (learn/kill_rules.py's "complained" rows).
STOP_TYPES = ("replied", "bounced", "unsubscribed", "complained")
OUT_OF_OFFICE = "out_of_office"
SENT = "sent"
ID_CHUNK = 1000
NOT_DUE = "its first contact's email 1 went out too recently"  # the stagger (a reason counted, without a date)


def on(settings: Settings) -> bool:
    return bool(settings.general.second_contact)


def describe(settings: Settings) -> str:
    """One line for `us-outbound status`, the daily post and golive."""
    g = settings.general
    if not g.second_contact:
        return "Second contact: off (General second_contact = no): one person per company."
    return (f"Second contact: on, at companies of {g.second_contact_min_employees} or more staff: a person of another "
            f"role, {g.second_contact_delay_days} days after the first person's email 1, from the same sender, with "
            "the room the first contacts of new companies leave.")


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _chunks(items: Sequence[str], n: int = ID_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def slot_of(contact: Mapping[str, Any]) -> int:
    """contacts.contact_slot; a contact enrolled before it existed (NULL) was the account's first."""
    try:
        return SECOND if int(contact.get("contact_slot") or FIRST) == SECOND else FIRST
    except (TypeError, ValueError):
        return FIRST


def is_enrolled(contact: Mapping[str, Any]) -> bool:
    return bool(contact.get("enrolled_at") or contact.get("instantly_lead_id") or contact.get("enrolment_month"))


def big_enough(account: Mapping[str, Any], settings: Settings) -> bool:
    n = company_size(account.get("employees"), account.get("size_band"))
    return n is not None and n >= settings.general.second_contact_min_employees


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


@dataclass
class Thread:
    """One account's contacts as the second-contact rules read them."""

    account: dict
    contacts: list[dict] = field(default_factory=list)
    stops: list[dict] = field(default_factory=list)  # its events that stop every sequence (is_stop)
    first_sends: list[datetime] = field(default_factory=list)  # when the first contact's campaign emails went

    @property
    def enrolled(self) -> list[dict]:
        floor = datetime.min.replace(tzinfo=UTC)
        return sorted((c for c in self.contacts if is_enrolled(c)),
                      key=lambda c: (utc(c.get("enrolled_at")) or floor, str(c.get("contact_id"))))

    @property
    def first(self) -> dict | None:
        """The account's first contact: slot 1 (or none recorded), the earliest enrolled."""
        enrolled = self.enrolled
        return next((c for c in enrolled if slot_of(c) == FIRST), enrolled[0] if enrolled else None)

    @property
    def first_role(self) -> str:
        return _lower((self.first or {}).get("role"))

    @property
    def first_email(self) -> date | None:
        """The US Eastern day the first contact's email 1 went, as sync_outcomes recorded it."""
        return et_day(min(self.first_sends)) if self.first_sends else None

    def due(self, settings: Settings) -> date | None:
        """The first day a second contact may be enrolled: second_contact_delay_days after the first's email 1."""
        sent = self.first_email
        return sent + timedelta(days=settings.general.second_contact_delay_days) if sent else None

    def others(self) -> list[dict]:
        """Contacts on file, not enrolled, of another role than the first contact's."""
        role = self.first_role
        return [c for c in self.contacts if not is_enrolled(c) and _lower(c.get("role")) not in ("", role)]


def threads(ctx: Context, accounts: Sequence[Mapping[str, Any]]) -> dict[str, Thread]:
    """account_id -> its Thread: contacts, stop events (by account and by contact) and the first contact's sends."""
    out = {str(a["account_id"]): Thread(dict(a)) for a in accounts}
    ids = sorted(out)
    if not ids:
        return out
    store = ctx.store
    for chunk in _chunks(ids):
        for c in store.select("contacts", {"account_id": list(chunk)}):
            out[str(c["account_id"])].contacts.append(c)
    contact_account = {str(c["contact_id"]): aid for aid, t in out.items() for c in t.contacts}
    seen: set[str] = set()

    def stop(e: Mapping[str, Any]) -> None:
        aid = str(e.get("account_id") or "") or contact_account.get(str(e.get("contact_id") or ""), "")
        if aid in out and str(e.get("event_id")) not in seen and is_stop(e):
            seen.add(str(e.get("event_id")))
            out[aid].stops.append(dict(e))

    for chunk in _chunks(ids):
        for e in store.select("events", {"type": list(STOP_TYPES), "account_id": list(chunk)}):
            stop(e)
    for chunk in _chunks(sorted(contact_account)):
        for e in store.select("events", {"type": list(STOP_TYPES), "contact_id": list(chunk)}):
            stop(e)
    firsts = {str(t.first["contact_id"]): aid for aid, t in out.items() if t.first}
    for chunk in _chunks(sorted(firsts)):
        for e in store.select("events", {"type": SENT, "contact_id": list(chunk)}):
            t = utc(e.get("occurred_at"))
            if t is not None and not e.get("approval"):  # an approval marks an old desk reply, not a step
                out[firsts[str(e["contact_id"])]].first_sends.append(t)
    return out


def why_not(t: Thread, settings: Settings, day: date) -> str | None:
    """Why the account may not have a second contact enrolled on `day` (US Eastern), or None."""
    g, a = settings.general, t.account
    if not g.second_contact:
        return "second_contact is no"
    if a.get("status") != ENROLLED:
        return f"the account is {a.get('status') or 'without a status'}, not enrolled"
    if a.get("tier") not in queue.QUEUE_TIERS:
        return f"the account is {a.get('tier') or 'untiered'}"
    if not big_enough(a, settings):
        return f"fewer than {g.second_contact_min_employees} staff"
    first = t.first
    if first is None:
        return "its first contact is not enrolled yet"
    if len(t.enrolled) > 1:
        return "it has its second contact"
    if t.stops:
        return "someone there replied, bounced, unsubscribed or complained"
    if not t.first_role:
        return "its first contact's role is not recorded"
    due = t.due(settings)
    if due is None:
        return "its first contact's email 1 is not recorded as sent yet"
    if day < due:
        return NOT_DUE
    return None


def choose(t: Thread, g: enrol.Gates) -> tuple[dict | None, str]:
    """(the second contact to email, or None; why not): the best-ranked sendable contact of another role on file,
    by the checks every contact passes (enrol.pick_contact), leaving out an email source a kill rule paused."""
    others = t.others()
    if not others:
        return None, "no contact of another role on file"
    usable = [c for c in others if _lower(c.get("email_source")) not in g.sources]
    contact, why = enrol.pick_contact(usable, g.domains, g.hashes, t.account, g.settings)
    if contact is None and not usable:
        why = "email source paused by a kill rule"
    return contact, why


def sendable_on_file(t: Thread, domains: Collection[str], hashes: Collection[str]) -> bool:
    """Whether a contact of another role on file could be emailed now (pick_contacts' "ready")."""
    return any(enrol.contact_block(c, domains, hashes) is None for c in t.others())


def owner(t: Thread) -> str:
    """The account's sender: kept on the account at first enrollment, else its first contact's campaign's owner."""
    campaign = str((t.first or {}).get("instantly_campaign") or "")
    return str(t.account.get("sender") or "").strip() or capacity.owner_of(campaign)


def enrolled_accounts(ctx: Context) -> list[dict]:
    """Enrolled accounts in a queue tier, big enough for a second contact."""
    s = ctx.settings
    rows = ctx.store.select("accounts", {"status": ENROLLED, "tier": list(queue.QUEUE_TIERS)})
    return [a for a in rows if big_enough(a, s)]


def candidates(
    ctx: Context, pulled: frozenset[str] = frozenset(), waiting: Collection[str] = frozenset(),
) -> tuple[list[enrol.Candidate], Counter[str]]:
    """The second contacts enrol could propose today, in queue order; and why the other big enough enrolled
    accounts have none. ([], {}) while second_contact is no, without a read."""
    s = ctx.settings
    if not on(s):
        return [], Counter()
    accounts = enrolled_accounts(ctx)
    if not accounts:
        return [], Counter()
    g = enrol.gates(ctx, pulled)
    day = ctx.now_et().date()
    out: list[enrol.Candidate] = []
    skipped: Counter[str] = Counter()
    for t in threads(ctx, accounts).values():
        why = enrol.account_reason(t.account, g, waiting) or why_not(t, s, day)
        contact = None
        if why is None:
            contact, why = choose(t, g)
        sender = owner(t)
        if contact is not None and not sender:
            contact, why = None, "its sender is not recorded"
        if contact is None:
            skipped[why] += 1
            continue
        out.append(enrol.Candidate({**t.account, "sender": sender}, contact, slot=SECOND, first=first_summary(t)))
    out.sort(key=lambda c: queue.order_key(c.account, s))
    return out, skipped


def due_by(ctx: Context, send_days: int) -> date:
    """The last of the next `send_days` send days, today counted if it is one (pick_contacts' lookahead)."""
    s = ctx.settings
    day = ctx.now_et().date()
    found = 0
    for _ in range(60):
        nxt = capacity.next_send_day(day, s)
        if nxt is None:
            break
        found += 1
        if found >= send_days:
            return nxt
        day = nxt + timedelta(days=1)
    return day


def to_pick(ctx: Context, by: date) -> tuple[list[tuple[dict, str]], int]:
    """For pick_contacts: ([(account, its first contact's role)] due a second contact by `by` with nobody of another
    role on file to email, in queue order; how many due by then have one). ([], 0) while second_contact is no."""
    s = ctx.settings
    if not on(s):
        return [], 0
    accounts = enrolled_accounts(ctx)
    if not accounts:
        return [], 0
    domains, hashes = enrol.suppressed(ctx)
    partners = {_lower(p.get("domain")) for p in ctx.store.select("partners")}
    need: list[tuple[dict, str]] = []
    ready = 0
    for t in threads(ctx, accounts).values():
        if enrol.account_block(t.account, s, domains, partners, frozenset()) or why_not(t, s, by):
            continue
        if sendable_on_file(t, domains, hashes):
            ready += 1
        else:
            need.append((t.account, t.first_role))
    need.sort(key=lambda x: queue.order_key(x[0], s))
    return need, ready


def recheck(ctx: Context, account: Mapping[str, Any], contact: Mapping[str, Any]) -> str | None:
    """A second contact's send approval at its ✅: why it may no longer go (the rules above, as of now), or None."""
    t = threads(ctx, [account])[str(account["account_id"])]
    why = why_not(t, ctx.settings, ctx.now_et().date())
    if why is None and _lower(contact.get("role")) == t.first_role:
        why = "the same role as its first contact"
    return why


def first_summary(t: Thread) -> dict:
    """The first contact as a second contact's card and enrol need it: who (name, title, role), when their email 1
    went, and their Copy row and test (enrol.prepare compares them)."""
    f = t.first or {}
    sent = t.first_email
    return {"contact_id": str(f.get("contact_id") or ""), "first_name": str(f.get("first_name") or "").strip(),
            "last_name": str(f.get("last_name") or "").strip(), "title": " ".join(str(f.get("title") or "").split()),
            "role": str(f.get("role") or ""), "email_1": sent.isoformat() if sent else "",
            "copy_version": str(f.get("copy_version") or ""), "test_id": str(f.get("test_id") or "")}


def tally(cands: Iterable[enrol.Candidate], skipped: Mapping[str, int], prepared: int, settings: Settings) -> dict:
    """The enrol summary's second_contacts entry."""
    if not on(settings):
        return {"switch": "off"}
    by = Counter(str(c.account.get("sender") or "") for c in cands)
    return {"switch": "on", "ready": sum(by.values()), "prepared": prepared, "by_owner": dict(by),
            "not_ready": dict(Counter(skipped).most_common(8))}
