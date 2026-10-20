"""The front door: the one place a company becomes an account (docs/pipeline.md, "The funnel").

Every way in (the Apollo universe, site visitors, the lookalike leads) comes through
admit(), so every account is deduplicated and checked the same way:
  * the root domain, mapped through the alias table (one account per root domain, SPEC 13);
  * a personal email domain or a public body is refused (the ICP gate, icp.ADMIT);
  * an account already on that domain is returned as it is (never a second row);
  * a suppressed domain or a partner (never prospected, SPEC 9) is refused (icp.ADMIT).
A new account starts with status new. The stages after the door (Apollo enrichment and the
free checks, HubSpot's exclusions, Clay) are the same for every source, so a company that
came in by a named row still has to pass them all before it can be emailed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from us_outbound import icp, suppression
from us_outbound.clean.domains import canonical_domain
from us_outbound.clean.names import clean_company_name
from us_outbound.clients.db import Store, new_id

# accounts.status, in the order an account moves through them (the W1-C contracts commit, 19 Oct 2026):
#   new           admitted here, not checked yet
#   queued        waiting to be checked again: held for the hand-check, or put back for the label check
#   verified      passed verify_accounts: enrol may take it
#   disqualified  left out, for good or until accounts.recheck_after; accounts.status_reason says which
#   parked        out of the queue while a setting keeps it out (its industry switched off, or the label check
#                 failing under one label list), back to new when it changes (verify.redecide_parked, W1-4; every
#                 reader of status names it on purpose)
#   enrolled      its contact is in a sender's campaign
#   engaged       someone there replied (replies/outcomes.mark_engaged)
# SPEC 6 also lists demo_requested and demo_booked; nothing writes them, but readers still count them as emailed.
NEW = "new"
SITE_VISIT = icp.VISITOR_SOURCE  # accounts.source of a company that came in by visiting (sources/site_visits.py)
SUPPRESSED = "suppressed"  # admit()'s outcome for a suppressed domain


@dataclass(frozen=True)
class Admitted:
    domain: str | None
    account_id: str | None
    outcome: str  # "created", "exists", or why it was refused

    @property
    def ok(self) -> bool:
        return self.account_id is not None


def admit(store: Store, domain: str, *, source: str, now: datetime, name: str = "") -> Admitted:
    """The account for this domain: an existing one, a new one, or a refusal with the reason."""
    root = canonical_domain(store, domain)
    if not root:
        return Admitted(None, None, "not a company domain")
    why = icp.gate({"domain": root}, {}, None, checks={icp.PERSONAL, icp.PUBLIC_BODY})
    if why:
        return Admitted(root, None, why.text)
    existing = store.get("accounts", domain=root)
    if existing:
        return Admitted(root, existing["account_id"], "exists")
    # A partner first, as before the gate (so suppression is looked up only for a company that is not one).
    partners = {root} if store.get("partners", domain=root) else set()
    suppressed = {root} if not partners and suppression.is_suppressed(store, domain=root, now=now) else set()
    why = icp.gate({"domain": root}, {}, None, suppressed=suppressed, partners=partners,
                   checks={icp.PARTNER, icp.SUPPRESSED})
    if why:
        # The outcome the sources count a refusal under (sources/lookalike_leads.py reads it), as it was.
        return Admitted(root, None, SUPPRESSED if why.code == "suppressed" else why.text)
    clean, legal = clean_company_name(name or "")
    row = {
        "account_id": new_id(),
        "domain": root,
        "clean_name": clean or None,
        "legal_name": legal or None,
        "source": source,
        "status": NEW,
        "first_seen": now,
    }
    # One statement that makes the row only if no rule of the table forbids it (W3-3; code-sources B7): with the
    # unique index on accounts(domain), a second run admitting the same domain at the same moment gets the first's
    # account rather than a second row (two contacts and two sequences at one company). Before the index exists
    # (`accounts dedupe --live` applies it) it is the insert it always was.
    if not store.insert_new("accounts", row):
        existing = store.get("accounts", domain=root)
        if existing:
            return Admitted(root, existing["account_id"], "exists")
        return Admitted(root, None, "could not be added")
    return Admitted(root, row["account_id"], "created")


# -- duplicate domains: `us-outbound accounts dedupe` (W3-3, 20 Oct 2026; code-sources B7) ---------------------------

# The tables whose rows name an account, re-pointed when two accounts on one domain become one.
ACCOUNT_TABLES = ("contacts", "signal_events", "events", "hitl_items", "credit_ledger")


@dataclass
class Pair:
    """Accounts on one domain: the one kept, the ones merged into it, or why they are left for a person."""

    domain: str
    keep: str
    merge: list[str]
    refused: str = ""  # why not merged ("" when merged)

    def as_dict(self) -> dict:
        return {"domain": self.domain, "keep": self.keep, "merge": list(self.merge), "refused": self.refused or None}


def duplicates(store: Store) -> dict[str, list[dict]]:
    """domain -> its accounts, oldest first, for every domain more than one account holds."""
    by: dict[str, list[dict]] = {}
    for a in store.select("accounts"):
        if a.get("domain"):
            by.setdefault(str(a["domain"]), []).append(a)
    far = datetime.max.replace(tzinfo=UTC)
    return {d: sorted(rows, key=lambda a: (a.get("first_seen") or far, str(a["account_id"])))
            for d, rows in sorted(by.items()) if len(rows) > 1}


def plan_dedupe(store: Store) -> list[Pair]:
    """Each duplicate domain's merge: into the oldest account when at most one of them has an enrolled contact.

    When only a newer one has enrolled contacts, it is the one kept: its id is in HubSpot (us_outbound_account_id,
    SPEC 11) and on the contact in flight, so the merge never changes the account a lead in flight belongs to. Two or
    more with enrolled contacts are refused and listed, for a person to look at: each has a sequence running."""
    out: list[Pair] = []
    for domain, rows in duplicates(store).items():
        ids = [str(a["account_id"]) for a in rows]
        enrolled = {str(c["account_id"]) for c in store.select("contacts", {"account_id": ids}) if c.get("enrolled_at")}
        if len(enrolled) > 1:
            out.append(Pair(domain, "", ids, refused=f"{len(enrolled)} of its accounts have enrolled contacts"))
            continue
        keep = next(iter(enrolled)) if enrolled else ids[0]
        out.append(Pair(domain, keep, [i for i in ids if i != keep]))
    return out


def merge(store: Store, keep: str, drop: str) -> dict[str, int]:
    """Re-point every row of `drop` to `keep` and delete `drop`'s account row, in one transaction."""
    moved: dict[str, int] = {}
    with store.transaction():
        for table in ACCOUNT_TABLES:
            moved[table] = store.update(table, {"account_id": drop}, {"account_id": keep})
        store.delete("accounts", {"account_id": drop})
    return moved


def dedupe(ctx: Any) -> dict[str, Any]:
    """`us-outbound accounts dedupe [--live]`: the duplicate domains, and with --live each merged (plan_dedupe). Dry-run
    (the default) lists what it would do and changes nothing; a database write that reaches no prospect, so --live
    alone. remaining: the duplicate domains left after it, which must be 0 before the unique index is applied."""
    pairs = plan_dedupe(ctx.store)
    merged: list[dict] = []
    if ctx.live:
        for p in pairs:
            if p.refused:
                continue
            for drop in p.merge:
                merged.append({"domain": p.domain, "keep": p.keep, "dropped": drop, "moved": merge(ctx.store, p.keep, drop)})
    remaining = len(duplicates(ctx.store))
    return {"dry_run": not ctx.live, "duplicates": len(pairs), "pairs": [p.as_dict() for p in pairs],
            "refused": [p.as_dict() for p in pairs if p.refused], "merged": merged, "remaining": remaining}
