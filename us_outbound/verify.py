"""verify_accounts: an account checked on its Apollo data and HubSpot becomes verified (Harry, 1 Oct 2026).

SPEC 2 says every account passes through Clay before it can be emailed; verify_in_clay (SPEC 9) does
that. The Clay functions are not built yet and Clay's REST API is unconfirmed (clients/clay.py,
docs/phase0-facts.md), so to go live on Monday 5 Oct the General key clay_verification decides:
  * skip (the default until the Clay functions exist): this job moves accounts from new or queued to
    verified once the checks below pass, and the enrol job can then take them;
  * required (SPEC 2): accounts wait for verify_in_clay; this job changes nothing and says why.

The checks, cheapest first. An account that fails one keeps its status and is checked again next run.
  1. The domain is present, not a personal email domain and not suppressed (aliases included).
  2. Not a partner: on the partners table, or by NAICS or keywords (scoring/tiers.partner_match).
  3. The HQ state is active on the States tab, and never CA or WA (SPEC 1.3).
  4. 10 to 249 employees (SPEC 2); when Apollo gave no count, a SPEC size band from its size filter.
  5. The industry is an active Industries label.
  6. HubSpot: not a customer, not owned by someone else, no open deal (enrol.hubspot_company_block,
     the check the enrol job makes again before it sends), and no contact on the domain who opted out
     or hard-bounced (SPEC 9 hard exclusions). A HubSpot exclusion is kept as a hubspot fact with the
     account tiered Excluded (enrol.mark_excluded), so the rescore keeps it out and it is not looked up
     again. A HubSpot error leaves the account for the next run.
Overrides win over the account's columns, as in scoring. Then the score job runs (SPEC 9: score
runs after the sources), so the new accounts and the sources' new facts have a tier and an angle
before pick_contacts at 05:30.

An account verified here has clay_checked_at empty, which is how to find them once Clay is built.
Weekdays at 04:30 UK, after source_universe (03:00) and apollo_signals (03:30).

Dry-run: the HubSpot calls are reads, so they happen. Statuses and HubSpot exclusions are database
writes, which dry-run makes too (SPEC 0.3), as enrol does with the exclusions it finds; enrolment
itself stays dry until --live and live_sending = yes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from typing import Any

from us_outbound.clean.domains import is_personal_domain
from us_outbound.clean.people import state_code
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.enrol import enrol, focus, queue
from us_outbound.logs import log
from us_outbound.scoring import tiers
from us_outbound.scoring.score import latest_facts, parse_override
from us_outbound.settings.model import CLAY_REQUIRED, SIZE_BANDS, Settings

JOB = "verify_accounts"
VERIFIED = "verified"
WAITING_STATUSES = ("new", "queued")
MIN_EMPLOYEES, MAX_EMPLOYEES = 10, 249  # SPEC 2
NEVER_STATES = frozenset({"CA", "WA"})  # SPEC 1.3
MAX_ACCOUNTS_PER_RUN = 1000  # about three HubSpot searches each, inside the 30-minute timeout
FLUSH_EVERY = 100  # verified accounts written as the run goes, so a stopped run keeps its work
LIST_LIMIT = 100
ID_CHUNK = 1000
OPTED_OUT_REASON = "a contact at the domain opted out or bounced in HubSpot"
CLAY_NOT_BUILT = ("clay_verification is required and verify_in_clay is not built yet (phase 1), so accounts "
                  "wait in new and queued; set clay_verification to skip to verify them on Apollo data and HubSpot")


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _truthy(v: Any) -> bool:
    """A HubSpot fact's value read as tiers.py reads it."""
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1"}
    return v is True or (isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0)


def with_overrides(account: Mapping[str, Any], settings: Settings) -> dict:
    """The account with its domain's Overrides rows applied (SPEC 5), as score_account does."""
    domain = _lower(account.get("domain"))
    ov = {k: parse_override(v) for k, v in settings.overrides_for(domain).items()} if domain else {}
    return {**account, **ov}


def check(account: Mapping[str, Any], facts: Mapping[str, Any], settings: Settings,
          suppressed: Collection[str], partners: Collection[str]) -> str | None:
    """Why the account cannot be verified on what is already known (checks 1 to 5, and earlier HubSpot facts)."""
    domain = _lower(account.get("domain"))
    if not domain:
        return "no domain"
    if is_personal_domain(domain):
        return "a personal email domain"
    if domain in suppressed:
        return "domain suppressed"
    if domain in partners or tiers.partner_match(account, facts):
        return "a partner, never prospected"
    state = state_code(str(account.get("hq_state") or ""))
    if not state:
        return "HQ state unknown"
    if state in NEVER_STATES:
        return "HQ in CA or WA"
    if state not in settings.active_states():
        return "HQ state not active"
    employees = tiers.as_number(account.get("employees"))
    if employees is None and account.get("size_band") not in SIZE_BANDS:
        return "employee count unknown"
    if employees is not None and not MIN_EMPLOYEES <= employees <= MAX_EMPLOYEES:
        return "outside 10 to 249 employees"
    ind = settings.industry(str(account.get("industry") or ""))
    if ind is None:
        return "no Industries label"
    if not ind.active:
        return "industry switched off"
    for fact, reason in tiers.HUBSPOT_EXCLUSIONS:
        if _truthy(facts.get(fact)):
            return f"HubSpot: {reason}"
    return None


def hubspot_check(ctx: Context, account: Mapping[str, Any]) -> tuple[str, str] | None:
    """(fact, reason) when HubSpot excludes the company; None when it is clear."""
    block = enrol.hubspot_company_block(ctx, account)
    if block:
        return block
    if ctx.clients.hubspot.opted_out_contacts_at_domain(str(account.get("domain") or "")):
        return enrol.HS_OPTED_OUT, OPTED_OUT_REASON
    return None


def _facts(ctx: Context, account_ids: list[str]) -> dict[str, dict[str, Any]]:
    events: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(account_ids), ID_CHUNK):
        for e in ctx.store.select("signal_events", {"account_id": account_ids[i : i + ID_CHUNK]}):
            events[e["account_id"]].append(e)
    return {aid: latest_facts(evs) for aid, evs in events.items()}


def _rescore(ctx: Context) -> dict:
    """The score job (SPEC 9). Imported here, as tests replace the module."""
    from us_outbound.scoring import score

    return score.rescore(ctx)


def run(ctx: Context) -> dict:
    """The verify_accounts job (JOB CONTRACT: run(ctx) -> summary)."""
    s = ctx.settings
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "clay_verification": s.general.clay_verification}
    waiting = ctx.store.select("accounts", {"status": list(WAITING_STATUSES)})
    if s.general.clay_verification == CLAY_REQUIRED:
        log("verify_waiting_for_clay", run_id=ctx.run_id, accounts=len(waiting), reason=CLAY_NOT_BUILT)
        summary.update(skipped=True, reason=CLAY_NOT_BUILT, waiting=len(waiting))
        return summary

    todo = sorted(waiting, key=lambda a: (focus.group_rank(s.industry_group_of(a), s), queue.order_key(a, s)))
    todo = todo[:MAX_ACCOUNTS_PER_RUN]
    facts = _facts(ctx, [a["account_id"] for a in todo])
    suppressed, _ = enrol.suppressed(ctx)
    partners = {_lower(p.get("domain")) for p in ctx.store.select("partners")}
    failed: Counter[str] = Counter()
    examples: list[dict] = []
    excluded: list[dict] = []
    errors: list[str] = []
    verified: list[str] = []
    pending: list[dict] = []

    def fail(account: Mapping[str, Any], why: str) -> None:
        failed[why] += 1
        if len(examples) < LIST_LIMIT:
            examples.append({"account_id": account["account_id"], "domain": account.get("domain"), "reason": why})

    for a in todo:
        acct = with_overrides(a, s)
        why = check(acct, facts.get(a["account_id"], {}), s, suppressed, partners)
        if why:
            fail(a, why)
            continue
        try:
            block = hubspot_check(ctx, acct)
        except ApiError as exc:
            fail(a, "HubSpot check failed")
            errors.append(f"{a['account_id']}: {str(exc)[:200]}")
            continue
        if block:
            fact, reason = block
            enrol.mark_excluded(ctx, a, fact, reason)
            excluded.append({"account_id": a["account_id"], "reason": reason})
            fail(a, f"HubSpot: {reason}")
            continue
        verified.append(a["account_id"])
        pending.append({"account_id": a["account_id"], "status": VERIFIED})
        if len(pending) >= FLUSH_EVERY:
            ctx.store.upsert("accounts", pending)
            pending = []
    if pending:
        ctx.store.upsert("accounts", pending)

    scored = _rescore(ctx)
    summary.update(
        status="ok", waiting=len(waiting), checked=len(todo), verified=len(verified), excluded=excluded[:LIST_LIMIT],
        not_verified=dict(failed), not_verified_accounts=examples, errors=errors[:20],
        left_for_next_run=max(0, len(waiting) - len(todo)), rescore=scored,
    )
    log("verify_accounts_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "not_verified_accounts"})
    return summary
