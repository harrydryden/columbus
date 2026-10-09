"""`us-outbound accounts`: the companies and contacts we hold, read-only (Harry, 5 Oct 2026: "where are the
companies and contacts stored and how can I see them?").

They live in Postgres on Railway, schema us_outbound: accounts (one row per company, keyed by its root
domain) and contacts (one row per person), with signal_events (what the sources observed), events (sends,
replies, bounces, opt-outs), hitl_items (the Slack cards) and suppression beside them. The database has no
public access, so this command is how Harry looks at them, inside the worker:
`railway ssh -- us-outbound accounts`.

  accounts               a summary (companies by status and tier; contacts with an email, by source,
                         suppressed and enrolled; ready to email, as the enrol job counts them), then the
                         companies in queue order (tier, then score, as enrol/queue.order_key), one line
                         each with its first contact's role and title (the enrolled one, else the one
                         pick_contacts ranks first). The list never prints an email address.
  accounts DOMAIN        everything held on one company (a domain, a web address or a "www." form, through
                         the alias table): its facts, tier and score, its contacts with their emails, its
                         latest signals, its events (never reply text) and its Slack cards (never their
                         drafts). An unknown domain exits 1.
  accounts --csv         one row per contact with its company's fields (a company with no contact gets one
                         row), the same filters, to stdout. It holds personal data: Harry's own export.

Read-only: the database only (no Apollo, HubSpot, Instantly or Slack call), always dry-run, no heartbeat
row. Filters: --status and --tier (repeatable, or a comma list), --industry (contained in the industry or
its group, in any case), --limit (the list's length; 0 lists all).
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import IO, Any

from us_outbound import fmt
from us_outbound.clean.domains import canonical_domain, root_domain
from us_outbound.context import UK, Context
from us_outbound.enrol import enrol, queue
from us_outbound.logs import clip, hash_email, redact
from us_outbound.settings.model import TIERS, Settings
from us_outbound.timeparse import utc

# accounts.status in lifecycle order (sql/ddl/01_accounts.sql).
STATUSES = ("new", "queued", "verified", "enrolled", "engaged", "demo_requested", "demo_booked", "disqualified")
LIST_LIMIT = 25  # the list's default length
SIGNALS_SHOWN = 10
EVENTS_SHOWN = 20
CARDS_SHOWN = 20
QUOTE_CHARS = 160
VALUE_CHARS = 100
DOMAIN_WIDTH = 28
# The CSV: the account's columns, the contact's, then both ids (the database's own column names).
ACCOUNT_COLUMNS = ("domain", "clean_name", "legal_name", "industry", "industry_group",
                   "label_source", "label_confidence", "label_checked_at",  # the label check (labels.py, 7 Oct 2026)
                   "employees", "us_employees",
                   "size_band", "hq_city", "hq_state", "founded_year", "source", "tier", "tier_reason", "score",
                   "angle", "sender", "status", "first_seen", "last_scored")
CONTACT_COLUMNS = ("first_name", "last_name", "title", "role", "email", "email_status", "email_source",
                   "person_state", "suppressed", "suppressed_reason", "enrolled_at", "enrolment_month", "mailbox",
                   "instantly_campaign", "copy_version", "last_step_at")
CSV_COLUMNS = (*ACCOUNT_COLUMNS, *CONTACT_COLUMNS, "account_id", "contact_id")
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")  # a text cell a spreadsheet would read as a formula
WHERE = ("Stored in Postgres on Railway, schema us_outbound: the accounts table (one row per company) and contacts "
         "(one row per person).")


# -- small helpers ----------------------------------------------------------------------------------


def _text(v: Any) -> str:
    return str(v if v is not None else "").strip()


def _time(v: Any) -> str:
    """A stored time as Harry reads it: "05 Oct 2026 11:00 UK"; "-" when there is none."""
    return fmt.uk_time(v, "%d %b %Y %H:%M UK")


def _counts(counter: Counter[str], order: Sequence[str], blank: str) -> str:
    """'new 3, verified 2': in the given order, then any other value, then the blanks."""
    known = [k for k in order if counter.get(k)]
    other = sorted(k for k in counter if k and k not in order)
    parts = [f"{k} {counter[k]}" for k in (*known, *other)]
    if counter.get(""):
        parts.append(f"{blank} {counter['']}")
    return ", ".join(parts) or "none"


def _split(values: Iterable[str] | None) -> list[str]:
    """--status new --status queued, or --status new,queued."""
    return [v.strip() for value in values or () for v in value.split(",") if v.strip()]


def _name(c: Mapping[str, Any]) -> str:
    return " ".join(x for x in (_text(c.get("first_name")), _text(c.get("last_name"))) if x) or "(no name)"


def _company(a: Mapping[str, Any]) -> str:
    return _text(a.get("clean_name")) or _text(a.get("legal_name")) or "(no name)"


# -- filters ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    statuses: tuple[str, ...] = ()
    tiers: tuple[str, ...] = ()
    industry: str = ""

    @classmethod
    def parse(cls, statuses: Iterable[str] | None, tiers: Iterable[str] | None, industry: str | None) -> Filters:
        """The filters as given, in the database's spelling; ValueError naming the choices for an unknown one."""
        def known(given: list[str], choices: Sequence[str], what: str, plural: str) -> tuple[str, ...]:
            by_fold = {c.casefold(): c for c in choices}
            bad = [g for g in given if g.casefold() not in by_fold]
            if bad:
                raise ValueError(f"no {what} {', '.join(repr(b) for b in bad)}; the {plural} are: {', '.join(choices)}")
            return tuple(dict.fromkeys(by_fold[g.casefold()] for g in given))

        return cls(known(_split(statuses), STATUSES, "status", "statuses"),
                   known(_split(tiers), TIERS, "tier", "tiers"), _text(industry))

    def __bool__(self) -> bool:
        return bool(self.statuses or self.tiers or self.industry)

    def match(self, a: Mapping[str, Any], settings: Settings) -> bool:
        if self.statuses and _text(a.get("status")) not in self.statuses:
            return False
        if self.tiers and _text(a.get("tier")) not in self.tiers:
            return False
        if self.industry:
            want = self.industry.casefold()
            if not any(want in x.casefold() for x in (_text(a.get("industry")), settings.industry_group_of(a))):
                return False
        return True

    def words(self) -> str:
        parts = []
        if self.statuses:
            parts.append("status " + " or ".join(self.statuses))
        if self.tiers:
            parts.append("tier " + " or ".join(self.tiers))
        if self.industry:
            parts.append(f'industry or group containing "{self.industry}"')
        return ", ".join(parts)


# -- what is read -----------------------------------------------------------------------------------


@dataclass
class Stored:
    """Every account and contact, read once: the summary, the list and the CSV come from these."""

    accounts: list[dict]
    contacts: dict[str, list[dict]]  # account_id -> its contacts
    contact_count: int


def _read(ctx: Context) -> Stored:
    by_account: dict[str, list[dict]] = defaultdict(list)
    contacts = ctx.store.select("contacts")
    for c in contacts:
        by_account[_text(c.get("account_id"))].append(c)
    return Stored(ctx.store.select("accounts"), by_account, len(contacts))


def _in_order(ctx: Context, stored: Stored, f: Filters) -> list[dict]:
    """The matching accounts in queue order (enrol/queue.order_key: tier, score, size band, industry priority)."""
    rows = [a for a in stored.accounts if f.match(a, ctx.settings)]
    return sorted(rows, key=lambda a: queue.order_key(a, ctx.settings))


def contacts_in_order(contacts: Sequence[Mapping[str, Any]], account: Mapping[str, Any],
                      settings: Settings) -> list[dict]:
    """The enrolled contact first, then as pick_contacts ranks people (enrol.contact_order): the first is the one
    the company is emailed through."""
    return sorted((dict(c) for c in contacts),
                  key=lambda c: (c.get("enrolled_at") is None, enrol.contact_order(c, account, settings)))


def _ready(ctx: Context) -> int:
    """How many contacts enrol could email today: verified, with a sendable contact, leaving out this week's
    hand-check pulls and the companies whose send approval still waits (enrol/today.py, as every count of it)."""
    from us_outbound.enrol import today

    return today.read(ctx, campaigns=False).ready_count


def _failed(exc: Exception) -> str:
    return f"unavailable ({type(exc).__name__}: {redact(str(exc))[:160]})"


# -- the summary and the list -----------------------------------------------------------------------


def summary(ctx: Context, stored: Stored) -> list[str]:
    statuses = Counter(_text(a.get("status")) for a in stored.accounts)
    tiers = Counter(_text(a.get("tier")) for a in stored.accounts)
    every = [c for cs in stored.contacts.values() for c in cs]
    emails = [c for c in every if "@" in _text(c.get("email"))]
    sources = Counter(_text(c.get("email_source")).lower() for c in emails)
    try:
        ready = f"{_ready(ctx)} (verified, with a sendable contact, as the enrol job counts them)"
    except Exception as exc:  # the rest of the view still prints
        ready = _failed(exc)
    return [
        WHERE,
        f"Companies: {len(stored.accounts)}",
        f"  By status: {_counts(statuses, STATUSES, 'no status')}",
        f"  By tier: {_counts(tiers, TIERS, 'not scored yet')}",
        f"Contacts: {stored.contact_count}",
        f"  With an email: {len(emails)} (by source: {_counts(sources, ('apollo', 'clay'), 'unknown')})",
        f"  Suppressed: {sum(1 for c in every if c.get('suppressed'))}",
        f"  Enrolled: {sum(1 for c in every if c.get('enrolled_at') is not None)}",
        f"Ready to email: {ready}",
    ]


def _who(contact: Mapping[str, Any] | None) -> str:
    """The contact's role and title, never the email."""
    if contact is None:
        return "no contact yet"
    role, title = _text(contact.get("role")), _text(contact.get("title"))
    return ": ".join(x for x in (role, title) if x) or "a contact with no title"


def _line(a: Mapping[str, Any], contact: Mapping[str, Any] | None, width: int) -> str:
    score = a.get("score")
    parts = [
        _company(a),
        _text(a.get("industry")) or _text(a.get("industry_group")) or "industry unknown",
        f"{a['size_band']} staff" if _text(a.get("size_band")) else "size unknown",
        _text(a.get("hq_state")) or "state unknown",
        f"{_text(a.get('tier')) or 'no tier'} {score if score is not None else '-'}",
        _text(a.get("status")) or "no status",
        _who(contact),
    ]
    return f"  {_text(a.get('domain')) or '(no domain)':<{width}}  " + " · ".join(parts)


def companies(ctx: Context, stored: Stored, f: Filters, limit: int) -> list[str]:
    """Up to limit companies (0: all) in queue order, one line each."""
    rows = _in_order(ctx, stored, f)
    which = f" with {f.words()}" if f else ""
    if not rows:
        return [f"No company{which} is stored yet." if not f else f"No company{which}."]
    shown = rows if limit == 0 else rows[:limit]
    head = f"Companies{which}, in queue order (tier, then score): "
    head += (f"all {len(rows)}:" if len(shown) == len(rows)
             else f"the first {len(shown)} of {len(rows)} (`--limit 0` lists them all):")
    width = min(DOMAIN_WIDTH, max(len(_text(a.get("domain"))) for a in shown))
    lines = [head]
    for a in shown:
        contacts = contacts_in_order(stored.contacts.get(_text(a.get("account_id")), []), a, ctx.settings)
        lines.append(_line(a, contacts[0] if contacts else None, width))
    lines.append("One company in full: `us-outbound accounts DOMAIN`. As a spreadsheet (it holds names and emails): "
                 "`railway ssh -- us-outbound accounts --csv > companies.csv`.")
    return lines


def report(ctx: Context, f: Filters, limit: int = LIST_LIMIT) -> list[str]:
    """`us-outbound accounts`: the summary, then the list."""
    stored = _read(ctx)
    return [*summary(ctx, stored), "", *companies(ctx, stored, f, limit)]


# -- one company ------------------------------------------------------------------------------------


def find(ctx: Context, given: str) -> tuple[dict | None, str]:
    """(the account for this domain or web address, or None; the domain looked up). The domain goes through
    the alias table, as every way in does (accounts.admit)."""
    plain = _text(given).lower()
    root = canonical_domain(ctx.store, plain) or plain
    for domain in dict.fromkeys((root, plain)):
        account = ctx.store.get("accounts", domain=domain) if domain else None
        if account is not None:
            return account, domain
    return None, root or _text(given)


def _value(v: Any) -> str:
    text = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    return clip(text, VALUE_CHARS)


def _signal(e: Mapping[str, Any]) -> str:
    v, fact = e.get("value"), _text(e.get("fact")) or "?"
    if fact == "signal_matched" and isinstance(v, Mapping):
        what = f"matched {_text(v.get('signal')) or '?'}" + (
            f" (weight {v['weight']})" if v.get("weight") is not None else "")
    else:
        what = fact + (f" = {_value(v)}" if v not in (None, "") else "")
    parts = [_time(e.get("observed_at")), _text(e.get("source")) or "?", what]
    if _text(e.get("quote")):
        parts.append(f'"{clip(_text(e["quote"]), QUOTE_CHARS)}"')
    if _text(e.get("source_url")):
        parts.append(_text(e["source_url"]))
    return "  " + " · ".join(parts)


def _newest(rows: Iterable[Mapping[str, Any]], column: str) -> list[dict]:
    return sorted((dict(r) for r in rows), key=lambda r: utc(r.get(column)) or datetime.min.replace(tzinfo=UTC),
                  reverse=True)


def _ready_for(ctx: Context, account: Mapping[str, Any], contacts: Sequence[Mapping[str, Any]]) -> str:
    """Whether enrol could email this company today, and through whom; or why not (enrol.eligible)."""
    from us_outbound.enrol import approvals

    status, tier = _text(account.get("status")), _text(account.get("tier"))
    if status != "verified":
        return f"no: enrol takes verified companies, and this one is {status or 'without a status'}"
    if tier not in queue.QUEUE_TIERS:
        return f"no: the {tier or 'missing'} tier is not in the queue"
    try:
        emails = [_text(c.get("email")).lower() for c in contacts]
        domains = [_text(account.get("domain")), *(e.rpartition("@")[2] for e in emails if "@" in e)]
        hashes = [*(hash_email(e) for e in emails if "@" in e), *(_text(c.get("email_sha256")) for c in contacts)]
        _, pulled = enrol.hand_check(ctx, ctx.now_et().date())
        g = enrol.gates(ctx, pulled, enrol.suppressed_for(ctx, domains, hashes))
        contact, why = enrol.eligible(account, contacts, g, approvals.waiting(ctx).accounts)
    except Exception as exc:  # the rest of the view still prints
        return _failed(exc)
    if contact is None:
        return f"no: {why}"
    return f"yes, through {_name(contact)} ({_who(contact)})"


def _domain_suppression(ctx: Context, domain: str) -> str:
    rows = [r for r in ctx.store.select("suppression", {"domain": domain, "email_sha256": None})
            if (utc(r.get("expires_at")) or ctx.now) >= ctx.now]
    if not rows:
        return "no"
    r = rows[0]
    until = f", until {_time(r['expires_at'])}" if r.get("expires_at") else ""
    return f"yes ({_text(r.get('reason')) or 'no reason recorded'}, from {_text(r.get('source')) or '?'}, " \
           f"since {_time(r.get('added_at'))}{until})"


def _contact_lines(c: Mapping[str, Any]) -> list[str]:
    head = " · ".join(x for x in (_name(c), _text(c.get("title")) or "no title", _text(c.get("role")) or "no role",
                                  f"state {_text(c.get('person_state')) or 'unknown'}"))
    email = _text(c.get("email")) or "none"
    suppressed = (f"yes ({_text(c.get('suppressed_reason')) or 'no reason recorded'})" if c.get("suppressed")
                  else "no")
    if c.get("enrolled_at") is not None:
        enrolled = " · ".join([
            _time(c.get("enrolled_at")), f"mailbox {_text(c.get('mailbox')) or '?'}",
            f"copy {_text(c.get('copy_version')) or '?'}", f"campaign {_text(c.get('instantly_campaign')) or '?'}"])
    else:
        enrolled = "no"
    return [
        f"  {head}",
        f"    Email: {email} (source {_text(c.get('email_source')) or 'unknown'}, "
        f"status {_text(c.get('email_status')) or 'unknown'})",
        f"    Suppressed: {suppressed}",
        f"    Enrolled: {enrolled}",
    ]


def _event(e: Mapping[str, Any], names: Mapping[str, str]) -> str:
    parts = [_time(e.get("occurred_at")), _text(e.get("type")) or "?"]
    if e.get("step") is not None:
        parts.append(f"step {e['step']}")
    for key in ("mailbox", "reply_class", "approval"):
        if _text(e.get(key)):
            parts.append(_text(e[key]))
    if names.get(_text(e.get("contact_id"))):
        parts.append(names[_text(e["contact_id"])])
    return "  " + " · ".join(parts)


def _card(item: Mapping[str, Any]) -> str:
    p = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    parts = [_time(item.get("created_at")), _text(item.get("kind")) or "?", _text(item.get("status")) or "no status"]
    for key in ("state", "outcome"):
        if _text(p.get(key)):
            parts.append(f"{key} {_text(p[key])}")
    if item.get("handled_at"):
        parts.append(f"handled {_time(item['handled_at'])} by {_text(item.get('handled_by')) or '?'}")
    return "  " + " · ".join(parts)


def _shown(rows: list[dict], cap: int, what: str) -> str:
    return f"{what}: {len(rows)}" + (f", the latest {cap}:" if len(rows) > cap else "")


def _label_line(a: Mapping[str, Any], signals: Sequence[Mapping[str, Any]]) -> str:
    """How the industry label check placed the company (labels.py; Harry, 7 Oct 2026): its label source and the
    copy it earns, and the model's latest answer beside the rules' label."""
    from us_outbound import labels

    source = _text(a.get("label_source"))
    if not source:
        return "  Label check: not checked yet (the group's copy)"
    confidence = _text(a.get("label_confidence"))
    line = (f"  Label check: {source}" + (f" ({confidence})" if confidence else "")
            + f" · {labels.copy_level(a)} copy · decided {_time(a.get('label_checked_at'))}")
    v = labels.latest_verdict(signals)
    if v:
        line += (f" · the rules said {_text(v.get('rules')) or 'no label'}, the model {_text(v.get('model'))} "
                 f"({_text(v.get('confidence'))}, {_text(v.get('entity'))})")
        if v.get("what_they_do"):
            line += f" · they do: {_text(v['what_they_do'])}"
        if v.get("evidence"):
            line += f" · “{_text(v['evidence'])}”"
    return line


def company(ctx: Context, given: str) -> tuple[bool, list[str]]:
    """`us-outbound accounts DOMAIN`: (found; the lines). Everything held on one company, emails included."""
    a, domain = find(ctx, given)
    if a is None:
        return False, [f"No company with domain {domain}."]
    s, aid = ctx.settings, _text(a.get("account_id"))
    store = ctx.store
    contacts = contacts_in_order(store.select("contacts", {"account_id": aid}), a, s)
    signals = _newest(store.select("signal_events", {"account_id": aid}), "observed_at")
    events = _newest(store.select("events", {"account_id": aid}), "occurred_at")
    cards = _newest(store.select("hitl_items", {"account_id": aid}), "created_at")
    asked = root_domain(given)
    group = s.industry_group_of(a)
    employees = _text(a.get("employees")) or "unknown"
    if a.get("us_employees") is not None:
        employees += f" ({a['us_employees']} in the US)"
    lines = [f"Company: {_company(a)} ({domain})"]
    if asked and asked != domain and store.get("domain_aliases", alias=asked):
        lines.append(f"  ({asked} is recorded as an alias of {domain})")
    lines += [
        f"  Legal name: {_text(a.get('legal_name')) or '-'}",
        f"  HQ: {', '.join(x for x in (_text(a.get('hq_city')), _text(a.get('hq_state'))) if x) or 'unknown'}",
        f"  Industry: {_text(a.get('industry')) or 'unknown'} (group {group or 'unknown'})",
        _label_line(a, signals),
        f"  Employees: {employees} · size band {_text(a.get('size_band')) or 'unknown'} · "
        f"founded {_text(a.get('founded_year')) or 'unknown'}",
        f"  Source: {_text(a.get('source')) or 'unknown'} · first seen {_time(a.get('first_seen'))} · "
        f"last scored {_time(a.get('last_scored'))}",
        f"  Tier: {_text(a.get('tier')) or 'not scored yet'} ({_text(a.get('tier_reason')) or 'no reason recorded'}) · "
        f"score {a.get('score') if a.get('score') is not None else '-'}",
        f"  Angle: {_text(a.get('angle')) or '-'} · sender {_text(a.get('sender')) or 'not assigned yet'}",
        f"  Status: {_text(a.get('status')) or 'no status'}",
        f"  Ids: account {aid} · Apollo {_text(a.get('apollo_org_id')) or '-'} · "
        f"HubSpot {_text(a.get('hubspot_company_id')) or '-'}",
    ]
    if a.get("clay_checked_at"):
        lines.append(f"  Clay checked: {_time(a['clay_checked_at'])}")
    try:
        lines.append(f"  Domain suppressed: {_domain_suppression(ctx, domain)}")
    except Exception as exc:  # the rest of the view still prints
        lines.append(f"  Domain suppressed: {_failed(exc)}")
    lines.append(f"  Ready to email: {_ready_for(ctx, a, contacts)}")
    lines.append(f"Contacts: {len(contacts)}" + (" (the enrolled one first, then in the order enrol picks from)"
                                                 if len(contacts) > 1 else ""))
    for c in contacts:
        lines += _contact_lines(c)
    lines.append(_shown(signals, SIGNALS_SHOWN, "Signals"))
    lines += [_signal(e) for e in signals[:SIGNALS_SHOWN]]
    names = {_text(c.get("contact_id")): _name(c) for c in contacts}
    lines.append(_shown(events, EVENTS_SHOWN, "Events (sends, replies, bounces, opt-outs)"))
    lines += [_event(e, names) for e in events[:EVENTS_SHOWN]]
    lines.append(_shown(cards, CARDS_SHOWN, "Slack cards"))
    lines += [_card(i) for i in cards[:CARDS_SHOWN]]
    return True, lines


# -- the CSV ------------------------------------------------------------------------------------------


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UK).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    text = str(v)
    # A name or title from a source that starts like a formula stays text when the file is opened.
    return "'" + text if isinstance(v, str) and text.startswith(FORMULA_START) else text


def _csv_row(a: Mapping[str, Any], c: Mapping[str, Any] | None) -> dict[str, str]:
    c = c or {}
    row = {k: _cell(a.get(k)) for k in ACCOUNT_COLUMNS}
    row.update({k: _cell(c.get(k)) for k in CONTACT_COLUMNS})
    row.update(account_id=_cell(a.get("account_id")), contact_id=_cell(c.get("contact_id")))
    return row


def csv_rows(ctx: Context, f: Filters, limit: int | None = None, domain: str | None = None) -> list[dict] | None:
    """One row per contact with its company's fields, a company with no contact as one row; in queue order.
    Every matching company unless limit is given (0: all). With a domain, that company only; None if unknown."""
    if domain:
        a, _ = find(ctx, domain)
        if a is None:
            return None
        accounts = [a]
        contacts = {_text(a.get("account_id")): ctx.store.select("contacts", {"account_id": a["account_id"]})}
    else:
        stored = _read(ctx)
        accounts = _in_order(ctx, stored, f)
        accounts = accounts[:limit] if limit else accounts
        contacts = stored.contacts
    rows = []
    for a in accounts:
        people = contacts_in_order(contacts.get(_text(a.get("account_id")), []), a, ctx.settings)
        rows += [_csv_row(a, c) for c in people] or [_csv_row(a, None)]
    return rows


def write_csv(stream: IO[str], rows: Sequence[Mapping[str, str]]) -> None:
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    stream.flush()


def csv_note(rows: Sequence[Mapping[str, str]]) -> str:
    companies_ = len({r["account_id"] for r in rows})
    people = sum(1 for r in rows if r["contact_id"])
    return (f"Wrote {len(rows)} row{'s' if len(rows) != 1 else ''}: {companies_} compan{'ies' if companies_ != 1 else 'y'}, "
            f"{people} contact{'s' if people != 1 else ''}. The file holds personal data (names and emails): keep it "
            "private and delete it when you are done.")
