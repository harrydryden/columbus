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
from datetime import datetime

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
    store.upsert("accounts", [row])
    return Admitted(root, row["account_id"], "created")
