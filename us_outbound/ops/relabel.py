"""`us-outbound labels set | show` (labels.py; Harry, 7 Oct 2026): a company's label corrected by hand, and its story.

set_label: a person's correction ("cli"), as the label check's own correction path writes it (labels.correct: the
account, a label_corrected fact, the Overrides tab). show: the company's label, where it came from, and its label
check history, newest first.

The rest of this module went in W2-4 (20 Oct 2026), with the commands that ran it: `relabel`, the queue put under the
labels the rules give now, a migration done once on 7 Oct (design §3.3), and `labels audit`, the label check asked
about the whole queue at once, which verify_accounts does for about 150 companies a run (git history keeps both).
"""

from __future__ import annotations

from typing import Any

from us_outbound import facts, labels
from us_outbound.clients.guard import CLI_APPROVER
from us_outbound.context import Context


# -- `us-outbound labels set | show` (labels.py; Harry, 7 Oct 2026) --------------------------------------------------


def _account(ctx: Context, domain: str) -> dict:
    from us_outbound.clean.domains import root_domain

    d = root_domain(domain) or str(domain or "").strip().lower()
    found = ctx.store.select("accounts", {"domain": d})
    if not found:
        raise LookupError(f"no company with the domain {d!r}")
    return dict(found[0])


def set_label(ctx: Context, domain: str, text: str) -> dict:
    """`labels set DOMAIN LABEL`: a person's correction, by "cli": the label (labels.correct: the account, a
    label_corrected fact, the Overrides tab). A dry run says what would change and changes nothing."""
    a = _account(ctx, domain)
    ind = labels.resolve(text, ctx.settings)
    if ind is None:
        raise ValueError(f"no Industries label called {text!r}; the labels are:\n{labels.active_list(ctx.settings)}")
    out: dict[str, Any] = {"dry_run": ctx.dry_run, "domain": a.get("domain"), "from": a.get("industry"),
                           "to": ind.industry, "active": ind.active}
    if ctx.dry_run:
        return out
    got = labels.correct(ctx, a, ind, by=CLI_APPROVER, via="cli", who="at the command line (labels set)")
    return {**out, "sheet": got["sheet"]}


def show(ctx: Context, domain: str) -> dict:
    """`labels show DOMAIN`: the company's label, where it came from, and its label check history, newest first."""
    a = _account(ctx, domain)
    events = ctx.store.select("signal_events", {"account_id": a["account_id"], "source": labels.JOB})
    history = []
    for e in facts.history(events):
        v = e.get("value") or {}
        if e.get("fact") == labels.CORRECTED_FACT:
            history.append({"at": str(e.get("observed_at")), "corrected": f"{v.get('from') or 'no label'} → {v.get('to')}",
                            "by": v.get("by"), "via": v.get("via")})
        elif e.get("fact") == labels.VERDICT_FACT:
            sv = labels.StoredVerdict.from_value(v)
            m, d = sv.verdict, sv.decision
            history.append({"at": str(e.get("observed_at")), "asked": sv.asked, "rules": sv.rules,
                            "model": m.label, "confidence": m.confidence, "entity": m.entity,
                            "evidence": m.evidence, "what_they_do": m.what_they_do,
                            "decision": f"{d.get('action')}: {d.get('label')} ({d.get('source')}, {d.get('copy')} copy)"
                                        + (f"; {d.get('reason')}" if d.get("reason") else ""),
                            "labels_hash": m.labels_hash})
    return {"domain": a.get("domain"), "industry": a.get("industry"), "industry_group": a.get("industry_group"),
            "status": a.get("status"), "label_source": a.get("label_source") or "not checked yet",
            "copy_level": labels.copy_level(a), "label_confidence": a.get("label_confidence"),
            "label_checked_at": str(a.get("label_checked_at") or ""), "history": history}
