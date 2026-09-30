"""The front door: the one place a company becomes an account (docs/pipeline.md, "The funnel").

Every way in (the Apollo universe, site visitors, IRS matches, named accounts) comes through
admit(), so every account is deduplicated and checked the same way:
  * the root domain, mapped through the alias table (one account per root domain, SPEC 13);
  * an account already on that domain is returned as it is (never a second row);
  * a suppressed domain or a partner (never prospected, SPEC 9) is refused.
A new account starts with status new. The stages after the door (Apollo enrichment and the
free checks, HubSpot's exclusions, Clay) are the same for every source, so a company that
came in by a named row still has to pass them all before it can be emailed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from us_outbound import suppression
from us_outbound.clean.domains import canonical_domain, is_personal_domain
from us_outbound.clean.names import clean_company_name
from us_outbound.clients.db import Store, new_id

NEW = "new"


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
    if is_personal_domain(root):
        return Admitted(root, None, "a personal email domain")
    existing = store.get("accounts", domain=root)
    if existing:
        return Admitted(root, existing["account_id"], "exists")
    if store.get("partners", domain=root):
        return Admitted(root, None, "a partner, never prospected")
    if suppression.is_suppressed(store, domain=root, now=now):
        return Admitted(root, None, "suppressed")
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
