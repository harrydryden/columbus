"""`us-outbound relabel [--live]`: put the companies waiting in the queue under the label the Industries rules give now
(Harry, 7 Oct 2026).

The first send-approval cards (7 Oct) went out as games studios, AI and adtech to a fulfilment consultancy, a
surgeons' society, a data-centre firm and a town council: within Technology & Startups every label shares the
umbrella's NAICS prefixes, so with no keyword to tell them apart the tie-break chose one (apollo_universe.best_label
says how it chooses now). A company's label is set when a source admits it and again at each monthly universe run,
so the companies already in the queue keep the old one until then. This command works it out again now, from the
Apollo facts each company already has (its NAICS codes, keywords and Apollo industry: no Apollo call, no credit),
and from the label check's stored verdict (labels.py: no model call either):

  * the label is what labels.decide gives from the rules' label and the stored verdict; with no verdict yet, the
    rules' label with the group's copy (the label check asks about it at the next verify_accounts, or with
    `us-outbound labels audit --live`). An Overrides row's industry, or an approver's correction, always stands;
  * a company no label fits (with no verdict), whose domain is a public body's (clean/domains.is_public_body), or
    that the stored verdict rules out, is disqualified (status "disqualified", tier Excluded with the reason), so
    nothing proposes it again;
  * a company with no Apollo codes or keywords on file keeps its label;
  * an open send-approval card that no longer fits its company's label (approvals.unfit_cards: another label,
    disqualified, or a pitch more specific than the label now allows) is withdrawn: closed by "system" like an
    expiry (outcome expired, with the reason), its card and thread say so, nothing reaches Instantly, and the
    company goes back to the queue, so a later enrol proposes it with the right copy.

Dry-run (no --live) prints what it would change and changes nothing. Companies already enrolled are not touched:
their emails were approved as they were.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from us_outbound import labels
from us_outbound.clean.domains import is_public_body
from us_outbound.context import Context
from us_outbound.enrol import approvals
from us_outbound.logs import log
from us_outbound.settings.model import Industry
from us_outbound.sources import apollo_universe as universe

DISQUALIFIED = labels.DISQUALIFIED
EXCLUDED = labels.EXCLUDED
CHUNK = 1000
MOVES = frozenset({"industry", "industry_group", "status", "tier"})  # what counts as a change here
PUBLIC_BODY = "a public body, never prospected"
NO_LABEL_FITS = "no Industries label fits its Apollo codes and keywords"


def _chunks(ids: list[str]) -> Iterable[list[str]]:
    for i in range(0, len(ids), CHUNK):
        yield ids[i : i + CHUNK]


def label_events(ctx: Context, ids: list[str]) -> dict[str, list[dict]]:
    """account_id -> its Apollo organization facts and its label check facts."""
    out: dict[str, list[dict]] = {}
    for chunk in _chunks(ids):
        for e in ctx.store.select("signal_events", {"account_id": chunk, "source": [universe.SOURCE, labels.JOB]}):
            out.setdefault(str(e["account_id"]), []).append(e)
    return out


def decide(account: Mapping[str, Any], events: Sequence[Mapping[str, Any]], ctx: Context,
           verdict: labels.Verdict | None = None) -> tuple[labels.Decision, labels.Verdict | None, Industry | None] | None:
    """What changes for one queued company, or None: (the decision, the verdict it rests on, the rules' label).
    verdict: one just asked (`labels audit`); else the stored one, if any."""
    s = ctx.settings
    domain = str(account.get("domain") or "")
    if is_public_body(domain):
        d = labels.Decision(account.get("industry"), account.get("industry_group"),
                            account.get("label_source") or labels.RULES, account.get("label_confidence") or "",
                            labels.copy_level(account), labels.DISQUALIFY, PUBLIC_BODY)
        return d, None, None
    if labels.override_for(account, s)[0]:
        return None  # an Overrides row's industry, or an approver's, stands
    codes, text = labels._rules_material(events)
    if not codes and not text:
        return None  # nothing on file to decide on
    rules = labels.rules_label(account, events, s)
    stored = labels.latest_verdict(events)
    v = verdict or (labels.Verdict.from_value(stored) if stored else None)
    if v is None and rules is None:
        d = labels.Decision(account.get("industry"), account.get("industry_group"), labels.RULES, "",
                            labels.GROUP_COPY, labels.DISQUALIFY, NO_LABEL_FITS)
    else:
        d = labels.decide(rules, v, s, mode=labels.SKIP)
    if d is None:
        return None
    if d.action == labels.HOLD:  # the hand-check is verify_accounts' to run: here, the safe label and copy only
        d = replace(d, action=labels.VERIFY)
    if not MOVES & set(labels.columns(account, d)):
        return None
    return d, v, rules


def _line(a: Mapping[str, Any], d: labels.Decision) -> tuple[str, str]:
    """(the move, as counted, "Adtech & martech → Fintech"; the line for the company)."""
    old = a.get("industry") or "no label"
    if d.action == labels.DISQUALIFY:
        return f"{old} → disqualified", f"{a.get('domain')}: {old} → disqualified ({d.reason})"
    return f"{old} → {d.label}", f"{a.get('domain')}: {old} → {d.label}"


def _after(ctx: Context, a: Mapping[str, Any], d: labels.Decision) -> dict[str, Any]:
    return {**a, **labels.columns(a, d), "label_checked_at": ctx.now}


def _cards(ctx: Context, after: Mapping[str, Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """(the open cards that no longer fit, by company; those withdrawn: live only)."""
    found = approvals.unfit_cards(ctx, after) if after else []
    withdrawn: list[str] = []
    if ctx.live and found:
        withdrawn = approvals.withdraw_unfit(ctx, ctx.clients.slack, found)
    return [item.company for item, _, _ in found], withdrawn


def run(ctx: Context) -> dict:
    accounts = list(ctx.store.select("accounts", {"status": list(universe.OPEN_STATUSES)}))
    events = label_events(ctx, [str(a["account_id"]) for a in accounts])
    changes: dict[str, tuple[dict, labels.Decision, labels.Verdict | None, Industry | None]] = {}
    for a in accounts:
        got = decide(a, events.get(str(a["account_id"]), []), ctx)
        if got is not None:
            changes[str(a["account_id"])] = (a, *got)

    moves: Counter[str] = Counter()
    lines: list[str] = []
    for _, (a, d, _v, _r) in sorted(changes.items(), key=lambda kv: str(kv[1][0].get("domain") or "")):
        move, line = _line(a, d)
        moves[move] += 1
        lines.append(line)
    if ctx.live:
        for aid, (a, d, v, rules) in changes.items():
            labels.apply(ctx, a, d, v, rules=rules, stored=labels.latest_verdict(events.get(aid, [])))
    cards, withdrawn = _cards(ctx, {aid: _after(ctx, a, d) for aid, (a, d, _v, _r) in changes.items()})
    log("relabel", run_id=ctx.run_id, accounts=len(accounts), changed=len(changes), cards=len(cards),
        withdrawn=len(withdrawn), dry_run=ctx.dry_run)
    return {
        "dry_run": ctx.dry_run, "queued_accounts": len(accounts), "changed": len(changes),
        "moves": dict(moves.most_common()), "changes": lines,
        "cards_to_withdraw": cards, "cards_withdrawn": withdrawn,
    }
