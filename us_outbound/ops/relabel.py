"""`us-outbound relabel [--live]`: put the companies waiting in the queue under the label the Industries rules give now
(Harry, 7 Oct 2026).

The first send-approval cards (7 Oct) went out as games studios, AI and adtech to a fulfilment consultancy, a
surgeons' society, a data-centre firm and a town council: within Technology & Startups every label shares the
umbrella's NAICS prefixes, so with no keyword to tell them apart the tie-break chose one (apollo_universe.best_label
says how it chooses now). A company's label is set when a source admits it and again at each monthly universe run,
so the companies already in the queue keep the old one until then. This command works it out again now, from the
Apollo facts each company already has (its NAICS codes, keywords and Apollo industry: no Apollo call, no credit):

  * the label changes when best_label gives another (an Overrides row's industry always stands);
  * a company no label fits any more, or whose domain is a public body's (clean/domains.is_public_body), is
    disqualified (status "disqualified", tier Excluded with the reason), so nothing proposes it again;
  * a company with no Apollo codes or keywords on file keeps its label;
  * an open send-approval card for a company whose label changed, or that is disqualified, is withdrawn: closed by
    "system" like an expiry (outcome expired, with the reason), its card and thread say so, nothing reaches
    Instantly, and the company goes back to the queue, so a later enrol proposes it with the right copy.

Dry-run (no --live) prints what it would change and changes nothing. Companies already enrolled are not touched:
their emails were approved as they were.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from us_outbound.clean.domains import is_public_body
from us_outbound.context import Context
from us_outbound.enrol import approvals
from us_outbound.logs import log
from us_outbound.sources import apollo_universe as universe

DISQUALIFIED = "disqualified"
EXCLUDED = "Excluded"
FACTS = ("naics", "keywords", "apollo_industry")
CHUNK = 1000


def _chunks(ids: list[str]) -> Iterable[list[str]]:
    for i in range(0, len(ids), CHUNK):
        yield ids[i : i + CHUNK]


def _texts(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    return [s.strip() for s in str(value or "").split(",") if s.strip()]


def apollo_facts(ctx: Context, ids: list[str]) -> dict[str, dict[str, Any]]:
    """account_id -> its latest apollo_org naics, keywords and apollo_industry facts."""
    latest: dict[tuple[str, str], tuple[str, Any]] = {}
    for chunk in _chunks(ids):
        for e in ctx.store.select("signal_events", {"account_id": chunk, "source": universe.SOURCE, "fact": list(FACTS)}):
            key = (str(e["account_id"]), str(e["fact"]))
            at = str(e.get("observed_at") or "")
            if key not in latest or at >= latest[key][0]:
                latest[key] = (at, e.get("value"))
    out: dict[str, dict[str, Any]] = {}
    for (aid, fact), (_, value) in latest.items():
        out.setdefault(aid, {})[fact] = value
    return out


def decide(account: Mapping[str, Any], facts: Mapping[str, Any], ctx: Context) -> dict | None:
    """What changes for one queued company, or None: {"industry", "industry_group"} or {"disqualify": reason}."""
    domain = str(account.get("domain") or "")
    if is_public_body(domain):
        return {"disqualify": "a public body, never prospected"}
    if "industry" in ctx.settings.overrides_for(domain):
        return None  # an Overrides row's industry stands
    codes = _texts(facts.get("naics"))
    text = " ; ".join([*_texts(facts.get("keywords")), str(facts.get("apollo_industry") or "").strip()]).strip(" ;")
    if not codes and not text:
        return None  # nothing on file to decide on
    label = universe.best_label(codes, text, ctx.settings)
    if label is None:
        return {"disqualify": "no Industries label fits its Apollo codes and keywords"}
    if (label.industry, label.industry_group) == (account.get("industry"), account.get("industry_group")):
        return None
    return {"industry": label.industry, "industry_group": label.industry_group}


def run(ctx: Context) -> dict:
    accounts = list(ctx.store.select("accounts", {"status": list(universe.OPEN_STATUSES)}))
    facts = apollo_facts(ctx, [str(a["account_id"]) for a in accounts])
    changes: dict[str, tuple[dict, dict]] = {}
    for a in accounts:
        d = decide(a, facts.get(str(a["account_id"]), {}), ctx)
        if d is not None:
            changes[str(a["account_id"])] = (a, d)

    moves: Counter[str] = Counter()
    lines: list[str] = []
    rows: list[dict] = []
    for aid, (a, d) in sorted(changes.items(), key=lambda kv: str(kv[1][0].get("domain") or "")):
        old = a.get("industry") or "no label"
        if "disqualify" in d:
            moves[f"{old} → disqualified"] += 1
            lines.append(f"{a.get('domain')}: {old} → disqualified ({d['disqualify']})")
            rows.append({"account_id": aid, "status": DISQUALIFIED, "tier": EXCLUDED, "tier_reason": d["disqualify"]})
        else:
            moves[f"{old} → {d['industry']}"] += 1
            lines.append(f"{a.get('domain')}: {old} → {d['industry']}")
            rows.append({"account_id": aid, "industry": d["industry"], "industry_group": d["industry_group"]})

    cards = [i for i in approvals.items(ctx.store, (approvals.OPEN,)) if i.account_id in changes]
    withdrawn: list[str] = []
    if ctx.live:
        if rows:
            ctx.store.upsert("accounts", rows)
        slack = ctx.clients.slack if cards else None
        for item in cards:
            a, d = changes[item.account_id]
            why = (f"{item.company} is {d['disqualify']}" if "disqualify" in d
                   else f"its industry was {a.get('industry') or 'not set'} and is now {d['industry']}")
            if approvals.withdraw(ctx, item, slack, why, back_to_queue="disqualify" not in d):
                withdrawn.append(item.company)
    log("relabel", run_id=ctx.run_id, accounts=len(accounts), changed=len(changes), cards=len(cards),
        withdrawn=len(withdrawn), dry_run=ctx.dry_run)
    return {
        "dry_run": ctx.dry_run, "queued_accounts": len(accounts), "changed": len(changes),
        "moves": dict(moves.most_common()), "changes": lines,
        "cards_to_withdraw": [i.company for i in cards], "cards_withdrawn": withdrawn,
    }
