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
Doubtful Apollo facts (Harry, 2 Oct 2026): an account whose HQ state or size Apollo leaves in
doubt is not verified silently, nor dropped silently. It goes to the weekly hand-check
(enrol/hand_check.py) with the reason, as a doubtful_facts fact, and waits:
  * Apollo gives no HQ state, no employee count and no size band, or no industry (MISSING);
  * the employee count and the size band disagree;
  * Apollo's count (an estimate) is within EDGE_MARGIN of a SIZE_EDGES edge, where what we do
    changes: 10 staff (the floor), 50 (the Roles order, so whom we write to) and 250 (the ceiling).
When Harry approves the hand-check without pulling it, its doubts are cleared (a doubt_cleared
fact) and the checks above decide as usual; an Overrides row corrects a fact that was wrong.
A missing fact is the exception (Harry, 6 Oct 2026): approving cannot supply it, and the checks
would then fail the account unseen, so it stays on the hand-check, week after week, until an
Overrides row (hq_state, employees or size_band, industry) fills it in or Harry pulls it.
cross_check() is the hook for Clay's cross-check of HQ state and size (docs/roadmap.md): today
nothing confirms a doubt, so every one goes to the hand-check.
Overrides win over the account's columns, as in scoring. Then the score job runs (SPEC 9: score
runs after the sources), so the new accounts and the sources' new facts have a tier and an angle
before pick_contacts at 05:30.

An account verified here has clay_checked_at empty, which is how to find them once Clay is built.
Weekdays at 04:30 UK, after source_universe (03:00), apollo_signals (03:30), read_pages (03:45) and
apollo_enrich (04:10), whose exact headcounts it checks like any other.

Dry-run: the HubSpot calls are reads, so they happen. Statuses and HubSpot exclusions are database
writes, which dry-run makes too (SPEC 0.3), as enrol does with the exclusions it finds; enrolment
itself stays dry until --live and live_sending = yes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from us_outbound.clean.domains import is_personal_domain
from us_outbound.clean.people import size_band, state_code
from us_outbound.clients.db import new_id
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
# Doubtful Apollo facts (Harry, 2 Oct 2026): to the weekly hand-check instead of verified silently.
DOUBT_SOURCE = JOB  # signal_events.source of the doubt facts; not a SPEC 7 source key, so no signal scores them
DOUBT_FACT, CLEARED_FACT = "doubtful_facts", "doubt_cleared"
SIZE_EDGES = (10, 50, 250)  # the first headcount where what we do changes: the floor, the Roles order, the ceiling
EDGE_MARGIN = 2  # Apollo's count is an estimate: this close to an edge it could be either side
DOUBTFUL = "doubtful Apollo facts, waiting for the weekly hand-check"
# check() reasons a doubt can stand behind: the hand-check sees the account rather than it failing unseen.
DOUBTABLE = frozenset({"HQ state unknown", "employee count unknown", "outside 10 to 249 employees",
                       "no Industries label"})
NO_STATE = "Apollo gives no HQ state"
NO_SIZE = "Apollo gives no employee count or size band"
NO_INDUSTRY = "Apollo gives no industry"
# Doubts approving cannot settle: only an Overrides row fills the fact in (Harry, 6 Oct 2026).
MISSING = frozenset({NO_STATE, NO_SIZE, NO_INDUSTRY})


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


def near_edge(n: int) -> int | None:
    """The SIZE_EDGES edge Apollo's estimate of n staff is within EDGE_MARGIN of, or None."""
    return next((e for e in SIZE_EDGES if e - EDGE_MARGIN <= n < e + EDGE_MARGIN), None)


def doubts(account: Mapping[str, Any]) -> list[str]:
    """What in the account's Apollo facts is too doubtful to verify on unseen (Overrides already applied)."""
    out: list[str] = []
    if not state_code(str(account.get("hq_state") or "")):
        out.append(NO_STATE)
    if not str(account.get("industry") or "").strip():
        out.append(NO_INDUSTRY)
    employees = tiers.as_number(account.get("employees"))
    band = account.get("size_band")
    if employees is None:
        if band not in SIZE_BANDS:
            out.append(NO_SIZE)
        return out
    n = int(employees)
    if band in SIZE_BANDS and size_band(n) not in (None, band):
        out.append(f"the employee count ({n}) and the size band ({band}) disagree")
    edge = near_edge(n)
    if edge is not None:
        out.append(f"Apollo's estimate of {n} staff is within {EDGE_MARGIN} of the {edge}-staff edge")
    return out


def standing(found: Iterable[str], cleared: Collection[str]) -> list[str]:
    """The doubts still open: those a hand-check has not cleared, and every missing fact (MISSING), which
    approving cannot fill in."""
    return [d for d in found if d in MISSING or d not in cleared]


def cross_check(ctx: Context, account: Mapping[str, Any], found: Sequence[str]) -> list[str]:
    """The doubts a second source leaves standing: the hook for Clay's cross-check (docs/roadmap.md).

    Once Clay's Accounts function exists, its HQ state and size confirm or correct Apollo's here,
    and a doubt they settle is dropped. Today nothing cross-checks, so every doubt stands.
    """
    return list(found)


def _when(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def doubt_history(ctx: Context, account_ids: Iterable[str] | None = None) -> tuple[dict[str, dict], dict[str, set[str]]]:
    """(account_id -> its latest doubtful_facts value; account_id -> the doubts a hand-check cleared)."""
    where: dict[str, Any] = {"source": DOUBT_SOURCE}
    ids = list(account_ids) if account_ids is not None else None
    rows: list[dict] = []
    if ids is None:
        rows = ctx.store.select("signal_events", where)
    else:
        for i in range(0, len(ids), ID_CHUNK):
            rows += ctx.store.select("signal_events", {**where, "account_id": ids[i : i + ID_CHUNK]})
    latest: dict[str, tuple[datetime, dict]] = {}
    cleared: dict[str, set[str]] = defaultdict(set)
    for e in rows:
        aid, value = str(e.get("account_id")), e.get("value")
        if not isinstance(value, Mapping):
            continue
        if e.get("fact") == DOUBT_FACT:
            t = _when(e.get("observed_at"))
            if aid not in latest or t >= latest[aid][0]:
                latest[aid] = (t, dict(value))
        elif e.get("fact") == CLEARED_FACT:
            cleared[aid] |= {str(r) for r in value.get("reasons") or ()}
    return {aid: v for aid, (_, v) in latest.items()}, cleared


def open_doubts(ctx: Context) -> list[dict]:
    """The accounts waiting in new or queued for the hand-check, each with its open doubts: those not cleared,
    and the facts still missing once Overrides are applied (an Overrides row that fills one settles it)."""
    recorded, cleared = doubt_history(ctx)
    if not recorded:
        return []
    out = []
    for a in ctx.store.select("accounts", {"account_id": sorted(recorded), "status": list(WAITING_STATUSES)}):
        missing = MISSING & set(doubts(with_overrides(a, ctx.settings)))
        found = recorded[a["account_id"]].get("reasons") or ()
        reasons = [r for r in standing(found, cleared.get(a["account_id"], set())) if r not in MISSING or r in missing]
        if reasons:
            out.append({"account_id": a["account_id"], "domain": a.get("domain") or "", "clean_name": a.get("clean_name") or "",
                        "hq_state": a.get("hq_state") or "", "employees": a.get("employees"),
                        "size_band": a.get("size_band") or "", "source": a.get("source") or "", "reasons": reasons})
    return sorted(out, key=lambda d: str(d["domain"]))


def doubt_fact(ctx: Context, account: Mapping[str, Any], reasons: Sequence[str]) -> dict:
    """A doubtful_facts row: the reasons, and the facts as Apollo (and any Overrides) gave them."""
    seen = {k: account.get(k) for k in ("hq_state", "employees", "size_band")}
    return {"event_id": new_id(), "account_id": account["account_id"], "source": DOUBT_SOURCE, "fact": DOUBT_FACT,
            "value": {"reasons": list(reasons), "facts": seen}, "quote": "; ".join(reasons)[:300], "source_url": "",
            "observed_at": ctx.now}


def missing_facts(ctx: Context, account_ids: Iterable[str]) -> set[str]:
    """The accounts among these still missing a fact (MISSING) once Overrides are applied."""
    ids = sorted({str(i) for i in account_ids})
    out: set[str] = set()
    for i in range(0, len(ids), ID_CHUNK):
        for a in ctx.store.select("accounts", {"account_id": ids[i : i + ID_CHUNK]}):
            if MISSING & set(doubts(with_overrides(a, ctx.settings))):
                out.add(str(a["account_id"]))
    return out


def clear_doubts(ctx: Context, items: Iterable[Mapping[str, Any]], by: str, week: str) -> int:
    """Record that a hand-check cleared each item's doubts (enrol/hand_check.approve)."""
    rows = [{"event_id": new_id(), "account_id": d["account_id"], "source": DOUBT_SOURCE, "fact": CLEARED_FACT,
             "value": {"reasons": list(d.get("reasons") or ()), "by": by, "iso_week": week}, "quote": "",
             "source_url": "", "observed_at": ctx.now} for d in items]
    if rows:
        ctx.store.insert("signal_events", rows)
    return len(rows)


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
    doubtful: list[dict] = []

    def fail(account: Mapping[str, Any], why: str) -> None:
        failed[why] += 1
        if len(examples) < LIST_LIMIT:
            examples.append({"account_id": account["account_id"], "domain": account.get("domain"), "reason": why})

    recorded, cleared = doubt_history(ctx, [a["account_id"] for a in todo])
    doubt_rows: list[dict] = []
    for a in todo:
        acct = with_overrides(a, s)
        why = check(acct, facts.get(a["account_id"], {}), s, suppressed, partners)
        if why is None or why in DOUBTABLE:
            still = standing(cross_check(ctx, acct, doubts(acct)), cleared.get(a["account_id"], set()))
            if still:
                if (recorded.get(a["account_id"]) or {}).get("reasons") != still:
                    doubt_rows.append(doubt_fact(ctx, acct, still))
                fail(a, DOUBTFUL)
                doubtful.append({"account_id": a["account_id"], "domain": a.get("domain"), "reasons": still})
                continue
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
    if doubt_rows:
        ctx.store.insert("signal_events", doubt_rows)

    scored = _rescore(ctx)
    summary.update(
        status="ok", waiting=len(waiting), checked=len(todo), verified=len(verified), excluded=excluded[:LIST_LIMIT],
        to_hand_check=len(doubtful), to_hand_check_accounts=doubtful[:LIST_LIMIT],
        not_verified=dict(failed), not_verified_accounts=examples, errors=errors[:20],
        left_for_next_run=max(0, len(waiting) - len(todo)), rescore=scored,
    )
    log("verify_accounts_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "not_verified_accounts"})
    return summary
