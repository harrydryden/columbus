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
  * a company whose domain is a public body's (clean/domains.is_public_body), or that the stored verdict rules out,
    is disqualified (status "disqualified", tier Excluded with the reason), so nothing proposes it again. A company
    the rules find no label for, with no verdict yet, is left as it is for the label check to decide: the rules
    alone are not enough to rule a company out (Harry, 7 Oct 2026: the first relabel disqualified 89 software
    companies, 37signals and Postmark among them, whose Apollo codes include IT services, which Technology &
    Startups excludes). A company an earlier relabel disqualified that way is put back in the queue (status queued,
    tier and label cleared), so the label check asks about it;
  * a company with no Apollo codes or keywords on file keeps its label;
  * an open send-approval card that no longer fits its company's label (approvals.unfit_cards: another label,
    disqualified, or a pitch more specific than the label now allows) is withdrawn: closed by "system" like an
    expiry (outcome expired, with the reason), its card and thread say so, nothing reaches Instantly, and the
    company goes back to the queue, so a later enrol proposes it with the right copy.

Dry-run (no --live) prints what it would change and changes nothing. Companies already enrolled are not touched:
their emails were approved as they were.

`us-outbound labels audit [--limit N] [--live]` (audit, below; Harry, 7 Oct 2026) is this with the model asked first
about each open company with no fresh verdict, as verify_accounts asks about about 150 a run: it brings the whole
queue under the label check at once. Live, it first reads the home page of each company the model was not sure of
and whose page has not been read (sources/pages.home_pass: public pages, no paid service), so those are asked once
more with what the company says it does (labels.second_look). Dry-run: how many pages it would read, how many it
would ask, what that costs at most, a sample prompt, and what the stored verdicts alone would change, asking nothing.
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
from us_outbound.sources import pages

DISQUALIFIED = labels.DISQUALIFIED
EXCLUDED = labels.EXCLUDED
CHUNK = 1000
AUDIT_JOB = "labels_audit"
AUDIT_SECONDS = 45 * 60  # well inside the hour after which a "running" heartbeat reads as dead
AUDIT_HOME_SECONDS = 15 * 60  # the home pages read first: about 2 s a company when its site answers
PROGRESS_EVERY = 25  # the audit logs how far it has got
MOVES = frozenset({"industry", "industry_group", "status", "tier"})  # what counts as a change here
PUBLIC_BODY = "a public body, never prospected"
NO_LABEL_FITS = "no Industries label fits its Apollo codes and keywords"


def _chunks(ids: list[str]) -> Iterable[list[str]]:
    for i in range(0, len(ids), CHUNK):
        yield ids[i : i + CHUNK]


def label_events(ctx: Context, ids: list[str]) -> dict[str, list[dict]]:
    """account_id -> its Apollo organization facts, its label check facts and its home page (the model reads it)."""
    out: dict[str, list[dict]] = {}
    for chunk in _chunks(ids):
        for e in ctx.store.select("signal_events", {"account_id": chunk,
                                                    "source": [universe.SOURCE, labels.JOB, pages.SOURCE]}):
            if e.get("source") != pages.SOURCE or e.get("fact") == labels.HOME_FACT:
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
        return None  # the rules alone never rule a company out: the label check decides (the module docstring)
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


def ruled_out_on_rules(ctx: Context) -> list[dict]:
    """The companies an earlier relabel disqualified because the rules found no label, with no label check verdict
    since: the label check, not the rules, decides them (the module docstring)."""
    found = [a for a in ctx.store.select("accounts", {"status": DISQUALIFIED}) if a.get("tier_reason") == NO_LABEL_FITS]
    events = label_events(ctx, [str(a["account_id"]) for a in found])
    return [a for a in found if not labels.latest_verdict(events.get(str(a["account_id"]), []))]


def restore(ctx: Context, found: Sequence[Mapping[str, Any]]) -> None:
    """Back in the queue for the label check (live): queued, so verify_accounts verifies it again and asks."""
    rows = [{"account_id": a["account_id"], "status": "queued", "tier": None, "tier_reason": None,
             "label_source": None, "label_confidence": None, "label_checked_at": None} for a in found]
    if rows:
        ctx.store.upsert("accounts", rows)


def run(ctx: Context) -> dict:
    """The relabel command: every open company decided again from the rules and its stored verdict."""
    back = ruled_out_on_rules(ctx)
    if ctx.live:
        restore(ctx, back)
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
        "restored": sorted(str(a.get("domain")) for a in back),
    }


# -- `us-outbound labels set | show` (labels.py; Harry, 7 Oct 2026) --------------------------------------------------


def _account(ctx: Context, domain: str) -> dict:
    from us_outbound.clean.domains import root_domain

    d = root_domain(domain) or str(domain or "").strip().lower()
    found = ctx.store.select("accounts", {"domain": d})
    if not found:
        raise LookupError(f"no company with the domain {d!r}")
    return dict(found[0])


def set_label(ctx: Context, domain: str, text: str) -> dict:
    """`labels set DOMAIN LABEL`: an approver's correction for a company with no card (or any), by "cli": the label
    (labels.correct: the account, a label_corrected fact, the Overrides tab), and an open card that no longer fits it
    withdrawn. A dry run says what would change and changes nothing."""
    a = _account(ctx, domain)
    ind = labels.resolve(text, ctx.settings)
    if ind is None:
        raise ValueError(f"no Industries label called {text!r}; the labels are:\n{labels.active_list(ctx.settings)}")
    out: dict[str, Any] = {"dry_run": ctx.dry_run, "domain": a.get("domain"), "from": a.get("industry"),
                           "to": ind.industry, "active": ind.active}
    if ctx.dry_run:
        after = {**a, "industry": ind.industry, "industry_group": ind.industry_group,
                 "label_source": labels.APPROVER, "label_checked_at": ctx.now}
        out["cards_to_withdraw"] = [item.company for item, _, _ in approvals.unfit_cards(ctx, {a["account_id"]: after})]
        return out
    got = labels.correct(ctx, a, ind, by=approvals.CLI_APPROVER, via="cli", who="at the command line (labels set)")
    found = approvals.unfit_cards(ctx, {a["account_id"]: ctx.store.get("accounts", account_id=a["account_id"])})
    return {**out, "sheet": got["sheet"], "cards_withdrawn": approvals.withdraw_unfit(ctx, approvals.slack_or_none(ctx),
                                                                                       found)}


def show(ctx: Context, domain: str) -> dict:
    """`labels show DOMAIN`: the company's label, where it came from, and its label check history, newest first."""
    a = _account(ctx, domain)
    events = ctx.store.select("signal_events", {"account_id": a["account_id"], "source": labels.JOB})
    history = []
    for e in sorted(events, key=lambda e: str(e.get("observed_at") or ""), reverse=True):
        v = e.get("value") or {}
        if e.get("fact") == labels.CORRECTED_FACT:
            history.append({"at": str(e.get("observed_at")), "corrected": f"{v.get('from') or 'no label'} → {v.get('to')}",
                            "by": v.get("by"), "via": v.get("via")})
        elif e.get("fact") == labels.VERDICT_FACT:
            d = v.get("decision") or {}
            history.append({"at": str(e.get("observed_at")), "asked": bool(v.get("asked")), "rules": v.get("rules"),
                            "model": v.get("model"), "confidence": v.get("confidence"), "entity": v.get("entity"),
                            "evidence": v.get("evidence"), "what_they_do": v.get("what_they_do"),
                            "decision": f"{d.get('action')}: {d.get('label')} ({d.get('source')}, {d.get('copy')} copy)"
                                        + (f"; {d.get('reason')}" if d.get("reason") else ""),
                            "labels_hash": v.get("labels_hash")})
    return {"domain": a.get("domain"), "industry": a.get("industry"), "industry_group": a.get("industry_group"),
            "status": a.get("status"), "label_source": a.get("label_source") or "not checked yet",
            "copy_level": labels.copy_level(a), "label_confidence": a.get("label_confidence"),
            "label_checked_at": str(a.get("label_checked_at") or ""), "history": history}


# -- `us-outbound labels audit` (labels.py; Harry, 7 Oct 2026) -------------------------------------------------------


def audit(ctx: Context, limit: int | None = None) -> dict:
    """The label check for the whole queue now: each open company with no fresh verdict asked (live), those with a
    card waiting first, then Focus groups, then queue order, up to limit; its decision written as verify_accounts writes it (verify.converge: a held
    one back to queued and to the hand-check, one ruled out disqualified); then relabel for the rest, and every open
    card that no longer fits withdrawn. Dry-run asks nothing: the count, the cost at most, a sample prompt, and what
    the stored verdicts alone would change."""
    from us_outbound import verify

    s = ctx.settings
    accounts = [a for a in ctx.store.select("accounts", {"status": list(universe.OPEN_STATUSES)})
                if a.get("tier") not in universe.OUT_OF_QUEUE_TIERS]
    events = label_events(ctx, [str(a["account_id"]) for a in accounts])
    order = verify.check_order(accounts, s, verify.carded(ctx))  # cards waiting first, then Focus, then queue
    unsure = pages.unsure(ctx, order)  # asked, not sure, and the home page not read: read it first (live)
    home: dict[str, int] = {}
    if ctx.live and unsure:
        home = pages.home_pass(ctx, unsure, seconds=AUDIT_HOME_SECONDS)
        events.update(label_events(ctx, [str(a["account_id"]) for a in unsure]))
    run_ = verify.LabelRun(labels.Checker(ctx, spend=ctx.live, budget=labels.Budget(calls=len(order) or 1,
                                                                                    seconds=AUDIT_SECONDS)))
    ch = run_.checker
    todo = [a for a in order if verify.needs_verdict(run_, a, events.get(str(a["account_id"]), []))]
    todo = todo[:limit] if limit else todo
    per_call = ch.estimate_usd()
    out: dict[str, Any] = {
        "dry_run": ctx.dry_run, "open_accounts": len(accounts), "to_check": len(todo),
        "stale": sum(1 for a in todo if labels.latest_verdict(events.get(str(a["account_id"]), []))),
        "model": ch.model, "labels_hash": ch.hash, "per_call_usd": round(per_call, 4),
        "most_usd": round(per_call * len(todo), 2), "home_pages_to_read": len(unsure), "home_pages": home,
    }
    if ctx.dry_run:
        first = next(iter(todo), None)
        out["sample_prompt"] = labels.Material.of(first, events.get(str(first["account_id"]), [])).prompt() if first else ""
        out["stored"] = run(ctx)  # what the stored verdicts and the rules alone would change, in dry-run
        return out
    for n, a in enumerate(todo, start=1):
        ch.verdict(a, events.get(str(a["account_id"]), []))
        if n % PROGRESS_EVERY == 0:
            log("labels_audit_progress", run_id=ctx.run_id, done=n, of=len(todo), asked=ch.asked)
        if ch.unavailable or ch.budget.why_not():
            break
    _, cleared = verify.doubt_history(ctx, [str(a["account_id"]) for a in todo])
    doubts = verify.converge(ctx, run_, todo, events, cleared)
    if doubts:
        ctx.store.insert("signal_events", doubts)
    disagreements = []
    for a in todo:
        v, _, asked = ch.answers.get(str(a["account_id"]), (None, "", False))
        if v is None or not asked:
            continue
        rules = labels.rules_label(a, events.get(str(a["account_id"]), []), s)
        if (rules.industry if rules else None) != v.label or v.entity != labels.COMPANY:
            row = ctx.store.get("accounts", account_id=a["account_id"]) or a
            disagreements.append({"domain": a.get("domain"), "rules": rules.industry if rules else None,
                                  "model": v.label, "confidence": v.confidence, "entity": v.entity,
                                  "now": f"{row.get('status')}: {row.get('industry')} ({row.get('label_source')})",
                                  "evidence": v.evidence})
    rest = run(ctx)  # the rules and stored verdicts for the companies not asked (it withdraws its own cards)
    found = approvals.unfit_cards(ctx)
    withdrawn = [*rest["cards_withdrawn"], *approvals.withdraw_unfit(ctx, approvals.slack_or_none(ctx), found)]
    out.update(asked=ch.asked, unavailable=ch.unavailable, usd=ch.usd(), decisions=dict(run_.tally),
               disagreements=disagreements, changed=run_.changed, relabelled=rest["changes"],
               cards_withdrawn=withdrawn)
    log(AUDIT_JOB, run_id=ctx.run_id, to_check=len(todo), asked=ch.asked, usd=out["usd"],
        disagreements=len(disagreements), withdrawn=len(withdrawn), unavailable=ch.unavailable[:120])
    return out
