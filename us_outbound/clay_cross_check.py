"""Clay's cross-check of the HQ state and size Apollo leaves in doubt (verify.cross_check; Harry, 6 Oct 2026).

verify_accounts holds an account whose Apollo facts are doubtful for the weekly hand-check (verify.py): no
HQ state, no size, a count and band that disagree, or a count near a size edge. With the General switch
clay_cross_check = yes (default no), Clay is asked first, so Harry sees only what Clay could not settle.

  1. Ask (the pre-pass, before verify's checks): the run's accounts with a standing HQ-state or size doubt
     that no hand-check has cleared, that no Overrides row answers, and that no other check would fail first,
     go to Clay's "US Outbound – Accounts" function (General clay_accounts_function_id; its spec is in
     docs/pipeline.md) in one batch of up to RUN_ITEMS_MAX, in verify's order. Once per account: each answer
     is a signal_events fact (source clay, fact FACT, Clay's source note as the quote), and an account with
     one is not asked again. A Clay error is not stored, so that account is asked again next run; the job
     summary reports it and the doubt stands for the hand-check. Not while a kill rule pauses the clay source
     (learn/holds.paused_sources), and only within today's share of clay_monthly_credits (budget.py): RESERVE
     is written to credit_ledger for each account before the call and settled after it at what Clay reports.
  2. Fill: a missing HQ state, or a missing size (no count and no band), is filled in on the account from
     Clay's answer before the checks, so the account can verify the same day; never a field an Overrides row
     sets. A stored answer fills again if a later Apollo sweep blanks the field.
  3. Settle (cross_check, per account, from the stored answer):
       * size: a doubt is dropped when Apollo's and Clay's counts fall on the same side of every size edge;
         otherwise it becomes one reason, "Clay says 62 staff, Apollo says 49". A count only Clay gave that sits
         near an edge becomes "Clay says 49 staff, Apollo gives none";
       * HQ state: Clay's state, when Apollo's is known and differs, is a reason ("Clay says CA, Apollo says
         NY"), and when it is the same it drops any state doubt; likewise a size Clay puts across an edge from
         Apollo's undoubted count. Both are facts Clay was asked for anyway, which the hand-check should see;
       * a missing fact Clay could not supply (verify.MISSING) stays word for word, as does a doubt about the
         industry: Clay does not settle industry, as a loose label mapping would pick the wrong copy;
       * a field an Overrides row sets is Harry's word: Clay neither fills it nor argues with it.
     Doubts are told apart by re-running verify.doubts() with a known value swapped in, not by their wording,
     as the size checks follow the settings.

Clay runs in dry-run too, as the guard allows (clients/clay.py), and spends credits within the budget, as
pick_contacts' email lookups do.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

from us_outbound import budget, verify
from us_outbound.clean.people import size_band, state_code
from us_outbound.clients.clay import RUN_ITEMS_MAX, ClayError, parse_cross_check_output
from us_outbound.clients.db import Store, new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.facts import load, newest
from us_outbound.learn import holds
from us_outbound.logs import log
from us_outbound.scoring import tiers
from us_outbound.settings.model import SIZE_BANDS, Settings

SOURCE = "clay"  # signal_events.source of Clay's answers; the clay source a kill rule pauses (learn/holds.py)
FACT = "hq_and_size"
# What one lookup costs (a data provider, or Claygent reading the company's pages) is unconfirmed, and moot until
# the "US Outbound – Accounts" function is built and clay_cross_check is yes (CLAY-CROSS-COST). Reserved for
# each account before the batch, and settled at what Clay reports, or kept when it reports nothing.
RESERVE = 3.0
LEDGER_NOTE = "HQ state and size cross-check (Clay)"
OFF = "clay_cross_check is no"
NO_FUNCTION = ("clay_accounts_function_id is blank: build the \"US Outbound – Accounts\" function in Clay "
               "(docs/pipeline.md) and paste its id on the General tab")
SIZE_FIELDS = frozenset({"employees", "size_band"})
ERROR_LIMIT = 20


# -- telling doubts apart ----------------------------------------------------------------------------------


def size_edges(settings: Settings) -> tuple[int, ...]:
    """The headcounts where what we do changes, as verify draws them from the General size range
    (verify.size_edges). Each edge is the first headcount past it."""
    return verify.size_edges(settings)


def same_side(a: int, b: int, edges: Iterable[int]) -> bool:
    """Whether two headcounts fall on the same side of every edge."""
    return all((a < e) == (b < e) for e in edges)


def _known(settings: Settings) -> tuple[dict, dict]:
    """(an HQ state, a size) that leave no doubt, to swap in: an active state, and the middle of the widest
    stretch between two size edges with its band."""
    states = settings.active_states()
    edges = size_edges(settings) or (10, 50, 250)
    lo, hi = max(zip(edges, edges[1:]), key=lambda g: g[1] - g[0]) if len(edges) > 1 else (edges[0], edges[0] + 100)
    n = (lo + hi) // 2
    return {"hq_state": states[0] if states else "NY"}, {"employees": n, "size_band": size_band(n)}


def kinds(account: Mapping[str, Any], found: Sequence[str], settings: Settings) -> tuple[list[str], list[str]]:
    """(the HQ-state doubts, the size doubts) among found: those gone once a known state or size is swapped in."""
    if not found:
        return [], []
    state, size = _known(settings)
    left_with_state = set(verify.doubts({**account, **state}, settings))
    left_with_size = set(verify.doubts({**account, **size}, settings))
    return [r for r in found if r not in left_with_state], [r for r in found if r not in left_with_size]


def overridden(settings: Settings, account: Mapping[str, Any]) -> set[str]:
    """The fields an Overrides row sets for the account's domain."""
    domain = str(account.get("domain") or "").strip().lower()
    return set(settings.overrides_for(domain)) if domain else set()


def answers(store: Store, account_ids: Iterable[str]) -> dict[str, dict]:
    """account_id -> Clay's latest answer (the FACT value)."""
    rows = load(store, sorted({str(i) for i in account_ids}), SOURCE, FACT)
    latest = {aid: newest(evs, where=lambda e: isinstance(e.get("value"), Mapping)) for aid, evs in rows.items()}
    return {aid: dict(e["value"]) for aid, e in latest.items() if e is not None}


# -- 1 and 2: ask and fill (the pre-pass) ---------------------------------------------------------------------


def room(ctx: Context) -> tuple[str | None, budget.Budget | None]:
    """(why Clay is not asked this run, or None; the month's Clay budget)."""
    g = ctx.settings.general
    if not g.clay_accounts_function_id.strip():
        return NO_FUNCTION, None
    paused = holds.paused_sources(ctx.store)
    if SOURCE in paused:
        return f"a kill rule pauses the clay source ({paused[SOURCE] or 'bounces'})", None
    month = budget.monthly(ctx.store, ctx.settings, "clay", ctx.now)
    if month.budget <= 0:
        return "no monthly Clay budget (clay_monthly_credits is 0)", month
    if month.left_today < RESERVE:
        return "today's share of the month's Clay budget is used", month
    try:
        ctx.clients.clay
    except ConfigError as exc:
        return f"no Clay API key: {exc}", month
    return None, month


def wanted(ctx: Context, todo: Sequence[Mapping[str, Any]], facts: Mapping[str, Mapping[str, Any]],
           suppressed: Collection[str], partners: Collection[str],
           cleared: Mapping[str, set[str]]) -> list[tuple[Mapping[str, Any], list[str]]]:
    """(account, the doubts Clay can answer) for the run's accounts verify would hold for an HQ-state or size doubt."""
    s = ctx.settings
    out = []
    for a in todo:
        acct = verify.with_overrides(a, s)
        why = verify.check(acct, facts.get(a["account_id"], {}), s, suppressed, partners)
        if not verify.doubtable(why, s):
            continue  # it fails on something Clay is not asked about
        found = verify.standing(verify.doubts(acct, s), cleared.get(a["account_id"], set()))
        state, size = kinds(acct, found, s)
        ov = overridden(s, a)
        bears = ([] if "hq_state" in ov else state) + ([] if ov & SIZE_FIELDS else size)
        if bears:
            out.append((a, bears))
    return out


def _ask_clay(ctx: Context, batch: Sequence[tuple[Mapping[str, Any], list[str]]], report: dict) -> dict[str, dict]:
    """Clay's answers for these accounts, each stored as a fact; credit_ledger first, errors into the report."""
    fid = ctx.settings.general.clay_accounts_function_id.strip()
    entries = {a["account_id"]: {"entry_id": new_id(), "system": "clay", "job": ctx.job, "run_id": ctx.run_id,
                                 "account_id": a["account_id"], "credits": RESERVE, "usd": None,
                                 "occurred_at": ctx.now, "note": f"{LEDGER_NOTE}, reserved"} for a, _ in batch}
    ctx.store.insert("credit_ledger", list(entries.values()))
    report["asked"] = len(batch)
    inputs = {a["account_id"]: {"domain": a.get("domain"), "company_name": a.get("clean_name") or None}
              for a, _ in batch}
    try:
        results: Mapping[str, Any] = ctx.clients.clay.run_function_batch(fid, inputs)
    except (ApiError, ClayError) as exc:
        report["errors"].append(f"the Clay run failed ({type(exc).__name__}): {str(exc)[:200]}")
        results = {}
    got: dict[str, dict] = {}
    settled: list[dict] = []
    rows: list[dict] = []
    for a, reasons in batch:
        aid = a["account_id"]
        result = results.get(str(aid))
        answer, error = None, None
        if result is None:
            error = "no result"
        elif isinstance(result, Exception):
            error = str(result)
        else:
            try:
                answer = parse_cross_check_output(result)
            except ClayError as exc:
                error = str(exc)
        if answer is None:
            settled.append({**entries[aid], "note": f"{LEDGER_NOTE} failed; counted in case Clay charged it"})
            report["failed"] += 1
            report["credits"] += RESERVE
            if results and len(report["errors"]) < ERROR_LIMIT:
                report["errors"].append(f"{a.get('domain')}: {error[:200]}")
            continue
        reported = answer["credits_used"]
        credits = RESERVE if reported is None else reported
        settled.append({**entries[aid], "credits": credits,
                        "note": LEDGER_NOTE if reported is not None else f"{LEDGER_NOTE}; no credits reported, the reserve kept"})
        report["credits"] += credits
        report["answered"] += 1
        value = {"hq_state": answer["hq_state"], "hq_state_given": answer["hq_state_given"],
                 "employees": answer["employees"], "source": answer["source"], "credits": credits,
                 "apollo": {k: a.get(k) for k in ("hq_state", "employees", "size_band")}, "doubts": list(reasons),
                 "function_id": fid}
        rows.append({"event_id": new_id(), "account_id": aid, "source": SOURCE, "fact": FACT, "value": value,
                     "quote": answer["source"], "source_url": "", "observed_at": ctx.now})
        got[aid] = value
    ctx.store.upsert("credit_ledger", settled)
    if rows:
        ctx.store.insert("signal_events", rows)
    return got


def fill(ctx: Context, accounts: Iterable[dict], have: Mapping[str, Mapping[str, Any]], report: dict) -> None:
    """A missing HQ state, or a missing size (no count, no band), filled in from Clay's answer: on the account in
    place, so this run's checks see it, and in the database. Never a field an Overrides row sets."""
    rows = []
    for a in accounts:
        answer = have.get(a["account_id"])
        if not answer:
            continue
        ov = overridden(ctx.settings, a)
        cols: dict[str, Any] = {}
        if answer.get("hq_state") and not state_code(str(a.get("hq_state") or "")) and "hq_state" not in ov:
            cols["hq_state"] = answer["hq_state"]
        n = answer.get("employees")
        if (n and tiers.as_number(a.get("employees")) is None and a.get("size_band") not in SIZE_BANDS
                and not ov & SIZE_FIELDS):
            cols.update(employees=int(n), size_band=size_band(n))
        if cols:
            a.update(cols)
            rows.append({"account_id": a["account_id"], **cols})
            report["filled"]["hq_state"] += "hq_state" in cols
            report["filled"]["size"] += "employees" in cols
    if rows:
        ctx.store.upsert("accounts", rows)


def ask(ctx: Context, todo: Sequence[dict], facts: Mapping[str, Mapping[str, Any]], suppressed: Collection[str],
        partners: Collection[str], cleared: Mapping[str, set[str]]) -> dict:
    """The pre-pass: ask Clay about the run's doubtful accounts not asked before, store its answers, and fill in
    missing facts on todo's rows. Returns the job summary's clay_cross_check entry."""
    if not ctx.settings.general.clay_cross_check:
        return {"off": OFF}
    want = wanted(ctx, todo, facts, suppressed, partners, cleared)
    report: dict[str, Any] = {"off": None, "doubtful": len(want), "asked_before": 0, "asked": 0, "answered": 0,
                              "failed": 0, "not_asked": None, "left_for_next_run": 0, "credits": 0.0,
                              "filled": {"hq_state": 0, "size": 0}, "errors": []}
    if not want:
        return report
    have = answers(ctx.store, [a["account_id"] for a, _ in want])
    report["asked_before"] = len(have)
    new = [(a, r) for a, r in want if a["account_id"] not in have]
    if new:
        why, month = room(ctx)
        if why is None and month is not None:
            n = min(len(new), RUN_ITEMS_MAX, int(month.left_today // RESERVE))
            have.update(_ask_clay(ctx, new[:n], report))
            report["left_for_next_run"] = len(new) - n
        else:
            report.update(not_asked=why, left_for_next_run=len(new))
        if month is not None:
            report["budget"] = budget.monthly(ctx.store, ctx.settings, "clay", ctx.now).describe()
    fill(ctx, [a for a, _ in want], have, report)
    log("clay_cross_check", run_id=ctx.run_id, **{k: v for k, v in report.items() if k != "errors"},
        errors=len(report["errors"]))
    return report


# -- 3: settle ---------------------------------------------------------------------------------------------


def settle(ctx: Context, account: Mapping[str, Any], found: Sequence[str]) -> list[str]:
    """The doubts left once Clay's stored answer is read against them (Overrides already applied to account)."""
    found = list(found)
    s = ctx.settings
    if not found or not s.general.clay_cross_check:
        return found
    ov = overridden(s, account)
    if "hq_state" in ov and ov & SIZE_FIELDS:
        return found
    answer = answers(ctx.store, [account["account_id"]]).get(str(account["account_id"]))
    if answer is None:
        return found
    state, size = kinds(account, found, s)
    instead: dict[str, str | None] = {}  # a doubt -> what replaces it (None: dropped)
    extra: list[str] = []  # Clay's disagreements with a fact Apollo gave without doubt

    def judge(doubted: list[str], say: str | None) -> None:
        doubted = [r for r in doubted if r not in verify.MISSING]  # word for word (Harry, 6 Oct 2026)
        if doubted:
            instead.update(dict.fromkeys(doubted, say))
        elif say:
            extra.append(say)

    clay_state, apollo_state = answer.get("hq_state"), state_code(str(account.get("hq_state") or ""))
    if clay_state and apollo_state and "hq_state" not in ov:
        judge(state, None if clay_state == apollo_state else f"Clay says {clay_state}, Apollo says {apollo_state}")
    n_c, n_a = answer.get("employees"), tiers.as_number(account.get("employees"))
    if n_c and n_a is not None and not ov & SIZE_FIELDS:
        n_a, apollo = int(n_a), answer.get("apollo") or {}
        if n_a == n_c and tiers.as_number(apollo.get("employees")) is None and apollo.get("size_band") not in SIZE_BANDS:
            if [r for r in size if r not in verify.MISSING]:  # the count is Clay's own, filled in
                judge(size, f"Clay says {n_c} staff, Apollo gives none")
        else:
            judge(size, None if same_side(n_a, n_c, size_edges(s)) else f"Clay says {n_c} staff, Apollo says {n_a}")
    out: list[str] = []
    for r in [*found, *extra]:
        r = instead.get(r, r) if r in instead else r
        if r is not None and r not in out:
            out.append(r)
    if out != found:
        log("clay_cross_check_settled", account_id=account["account_id"], dropped=[r for r in found if r not in out],
            stands=out)
    return out
