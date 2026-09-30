"""Source "named": the companies on the Named accounts tab (Harry, 30 Sep 2026).

Run by settings_sync, after the sheet is synced and before the rescore:
  * each domain on the tab comes in by the front door (accounts.admit): a new one becomes an
    account with source "named" and status new, an existing one is found by its root domain
    or alias, and a suppressed or partner domain is refused (and reported);
  * each named account gets the fact named = true, which the "Named by Harry" signal scores;
  * an account whose domain has left the tab gets named = false, so the signal stops.
Facts are written only when they change, so a second run over the same tab writes nothing.
The phase 1 jobs take a new named account through Apollo, the free checks and Clay like any
other: being named lifts its score, it never skips a check.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from us_outbound import accounts
from us_outbound.clients.db import new_id
from us_outbound.context import Context
from us_outbound.logs import log

SOURCE = "named"
FACT = "named"


def _ts(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _latest(events: list[Mapping[str, Any]]) -> dict[str, bool]:
    """account_id -> its latest named fact."""
    out: dict[str, tuple[datetime, bool]] = {}
    for e in events:
        if e.get("fact") != FACT or not e.get("account_id"):
            continue
        t = _ts(e.get("observed_at"))
        if e["account_id"] not in out or t >= out[e["account_id"]][0]:
            out[e["account_id"]] = (t, bool(e.get("value")))
    return {a: v for a, (_, v) in out.items()}


def _fact(account_id: str, value: bool, now: datetime, quote: str) -> dict:
    return {"event_id": new_id(), "account_id": account_id, "source": SOURCE, "fact": FACT, "value": value,
            "quote": quote, "source_url": "", "observed_at": now}


def run(ctx: Context) -> dict:
    """Bring the Named accounts tab into accounts and facts; returns what happened."""
    store, now = ctx.store, ctx.now
    latest = _latest(store.select("signal_events", {"source": SOURCE}))
    named_ids: set[str] = set()
    created, refused, facts = [], {}, []
    for row in ctx.settings.named_accounts:
        got = accounts.admit(store, row.domain, source=SOURCE, now=now, name=row.name)
        if not got.ok:
            refused[row.domain] = got.outcome
            continue
        named_ids.add(got.account_id)
        if got.outcome == "created":
            created.append(got.domain)
        if latest.get(got.account_id) is not True:
            facts.append(_fact(got.account_id, True, now, f"On the Named accounts tab{': ' + row.note if row.note else ''}"))
    for account_id, value in latest.items():
        if value and account_id not in named_ids:
            facts.append(_fact(account_id, False, now, "Taken off the Named accounts tab"))
    if facts:
        store.insert("signal_events", facts)
    summary = {"named": len(named_ids), "created": created, "refused": refused,
               "marked": sum(f["value"] for f in facts), "unmarked": sum(not f["value"] for f in facts)}
    log("named_accounts", **summary)
    return summary
