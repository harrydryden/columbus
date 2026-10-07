"""The one hashed suppression list (SPEC 2 job 7, 6, 11, 12, 14 phase 0).

Only the sha256 of an email (logs.hash_email) and/or a root domain is ever stored.
  * A row with email_sha256 set suppresses that email (its domain, if given, is recorded).
  * A row with email_sha256 NULL suppresses the whole domain.
Entries are kept indefinitely (expires_at NULL), except Suppress-signal domains, which
last the signal's counts_for_days (SPEC 9 scoring step 3; scoring writes those).

load_from_hubspot is the suppression_load job: HubSpot contacts who opted out of email
or hard-bounced are added, hashed. Re-running it adds nothing new.

Customers daily (Harry, 7 Oct 2026: "customers stop within a day, not a month"): the same job then
reads Spill's customer companies from HubSpot (sources/lookalikes.read_customers, read only, company
fields only) and keeps their domains out (lookalikes.exclude: each root domain suppressed for
SUPPRESS_DAYS, renewed daily, and the hubspot_customer fact on an account already on it, which the
02:00 settings_sync rescore tiers Excluded). Only the monthly lookalikes job rebuilds the lookalike
cells. A company that becomes a customer mid-month is kept out by the next night, and its leads in
flight are stopped by sync_outcomes' sweep below. The read is about 270 HubSpot calls, a minute or
two: the customer companies (about 700) at 100 a search page, the Spill 3.0 deals, and one association
read for each won, onboarding or churned deal (about 250). It comes after the opt-outs, which are written
first: a failed customer read is recorded as customers_error, which heartbeat_check reports, and the
job still finishes ok with the opt-outs loaded; yesterday's customer entries stand.

Enrol checks this list, and so does the sending already under way (Harry, 7 Oct 2026): a contact
whose address, email domain or company domain is suppressed while their lead is in flight, by this
job or any other, has their lead stopped, and everyone else's at their account, by sync_outcomes'
account-level stop within 15 minutes (replies/account_stop.py; live only, as every Instantly write).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import Context
from us_outbound.logs import hash_email, log, normalise_email, redact

TABLE = "suppression"
HUBSPOT_REASON, HUBSPOT_SOURCE = "hubspot_opt_out_or_bounce", "hubspot"
CHUNK = 5000  # hashes per IN (...) lookup


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def clean_domain(text: str | None) -> str | None:
    """Lower case, no scheme, path or www. Callers pass root domains (clean.domains makes them)."""
    if not text:
        return None
    d = str(text).strip().lower()
    d = d.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("@")[-1].rstrip(".")
    d = d.removeprefix("www.")
    return d or None


def _parents(domain: str) -> list[str]:
    """mail.acme.com -> [mail.acme.com, acme.com]: a suppressed root also covers its subdomains."""
    labels = domain.split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)] or [domain]


def _active(row: dict, now: datetime) -> bool:
    expires = _ts(row.get("expires_at"))
    return expires is None or expires > now


def _covers(old: dict, expires_at: datetime | None) -> bool:
    """An existing entry already suppresses at least as long as the new one would."""
    old_exp = _ts(old.get("expires_at"))
    return old_exp is None or (expires_at is not None and old_exp >= expires_at)


def _chunks(items: list[str], n: int = CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def add(
    store: Store,
    *,
    email: str | None = None,
    domain: str | None = None,
    reason: str,
    source: str,
    now: datetime,
    expires_at: datetime | None = None,
) -> bool:
    """Add one entry; True if it was written, False if an entry at least as strong exists.

    email given: suppresses that email (domain, if also given, is recorded with it).
    domain only: suppresses the whole domain.
    """
    sha = hash_email(email) if email and email.strip() else None
    dom = clean_domain(domain)
    if sha is None and dom is None:
        raise ValueError("suppression needs an email or a domain")
    key = {"email_sha256": sha, "domain": dom}
    old = store.get(TABLE, **key)
    if old is not None and _covers(old, expires_at):
        return False
    # A longer entry replaces a shorter one under the same key; the first added_at is kept.
    row = {**key, "reason": reason, "source": source, "added_at": (old or {}).get("added_at") or now, "expires_at": expires_at}
    store.upsert(TABLE, [row])
    log("suppression_add", email_sha256=sha, domain=dom, reason=reason, source=source, expires_at=expires_at)
    return True


def add_emails(store: Store, emails: Iterable[str], *, reason: str, source: str, now: datetime) -> int:
    """Add many emails indefinitely in one upsert; returns how many were new."""
    hashes = sorted({hash_email(e) for e in emails if e and e.strip()})
    if not hashes:
        return 0
    known: set[str] = set()
    for chunk in _chunks(hashes):
        for r in store.select(TABLE, {"email_sha256": chunk, "domain": None}):
            if _ts(r.get("expires_at")) is None:
                known.add(r["email_sha256"])
    rows = [
        {"email_sha256": h, "domain": None, "reason": reason, "source": source, "added_at": now, "expires_at": None}
        for h in hashes
        if h not in known
    ]
    if rows:
        store.upsert(TABLE, rows)
    return len(rows)


def is_suppressed(store: Store, email: str | None = None, domain: str | None = None, now: datetime | None = None) -> bool:
    """True if the email, the given domain, or the email's own domain (or a parent) is suppressed."""
    now = now or datetime.now(UTC)
    if email and email.strip():
        if any(_active(r, now) for r in store.select(TABLE, {"email_sha256": hash_email(email)})):
            return True
    domains: list[str] = []
    for d in (clean_domain(domain), clean_domain(normalise_email(email).partition("@")[2]) if email and "@" in email else None):
        if d:
            domains += [p for p in _parents(d) if p not in domains]
    if not domains:
        return False
    return any(_active(r, now) for r in store.select(TABLE, {"domain": domains, "email_sha256": None}))


def load_customers(ctx: Context) -> dict:
    """Spill's HubSpot customers kept out, daily (the module docstring): the domains and accounts lookalikes.exclude
    counts, or customers_error when HubSpot could not be read; never raises but for a guardrail."""
    from us_outbound.sources import lookalikes

    try:
        customers = lookalikes.read_customers(ctx)
    except GuardViolation:
        raise
    except Exception as exc:  # the opt-outs are loaded already; yesterday's customer entries stand
        log("suppression_customers_failed", run_id=ctx.run_id, error=redact(str(exc))[:200])
        return {"customers_error": f"Spill's customers could not be read from HubSpot ({type(exc).__name__}: "
                                   f"{redact(str(exc))[:200]}); yesterday's entries stand"}
    if not customers:  # surely a fault, as the lookalikes job's NoCustomers says
        return {"customers_error": "HubSpot returned no Spill customer companies; yesterday's entries stand"}
    return {"customers": lookalikes.exclude(ctx, customers)}


def load_from_hubspot(ctx: Context) -> dict:
    """The suppression_load job: HubSpot opt-outs and hard bounces into suppression, hashed; then Spill's customers'
    domains (load_customers)."""
    seen = 0
    added = 0
    batch: list[str] = []
    for email in ctx.clients.hubspot.iter_opted_out_or_bounced_emails():
        seen += 1
        batch.append(email)
        if len(batch) >= CHUNK:
            added += add_emails(ctx.store, batch, reason=HUBSPOT_REASON, source=HUBSPOT_SOURCE, now=ctx.now)
            batch = []
    if batch:
        added += add_emails(ctx.store, batch, reason=HUBSPOT_REASON, source=HUBSPOT_SOURCE, now=ctx.now)
    summary = {"seen": seen, "added": added, "already_suppressed": seen - added, **load_customers(ctx)}
    log("suppression_load", **summary)
    return summary
