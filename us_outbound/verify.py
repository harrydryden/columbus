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
  3. The HQ state is active on the States tab, and never CA or WA (SPEC 1.3). Not for a website
     visitor (Harry, 6 Oct 2026; accounts.any_us_state): any US state will do, and an unknown one
     too once Apollo puts the HQ in the US.
  4. Inside the General size range, min_employees to max_employees (SPEC 2: 10 to 249; Harry,
     6 Oct 2026); when Apollo gave no count, a size band from its size filter that the range touches.
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
  * Apollo's count (an estimate) is within EDGE_MARGIN of an edge where what we do changes
    (size_edges): the floor (min_employees, 10 by default), 50 (the Roles order, so whom we write
    to) and one past the ceiling (max_employees + 1, 250 by default).
When Harry approves the hand-check without pulling it, its doubts are cleared (a doubt_cleared
fact) and the checks above decide as usual; an Overrides row corrects a fact that was wrong.
A missing fact is the exception (Harry, 6 Oct 2026): approving cannot supply it, and the checks
would then fail the account unseen, so it stays on the hand-check, week after week, until an
Overrides row (hq_state, employees or size_band, industry) fills it in or Harry pulls it.
Clay's cross-check (Harry, 6 Oct 2026; us_outbound/clay_cross_check.py), while General clay_cross_check
is yes: before the checks, the run's accounts with an HQ-state or size doubt go to Clay's "US Outbound –
Accounts" function once, in one batch, within the Clay budget, and a missing HQ state or size is filled
in from its answer. cross_check() then drops a doubt Clay settles, and restates one Clay disagrees with
("Clay says 62 staff, Apollo says 49"), so the hand-check shows both. With the switch off (the default),
or no answer, every doubt goes to the hand-check as before.
Overrides win over the account's columns, as in scoring. Then the score job runs (SPEC 9: score
runs after the sources), so the new accounts and the sources' new facts have a tier and an angle
before pick_contacts at 05:30.

The industry label check (Harry, 7 Oct 2026: "industry categorisation is critical to the efficacy of the system";
us_outbound/labels.py). Once an account passes checks 1 to 4 (or fails only on its label: no Industries label, or one
switched off), the task model checks its label, once (a stored verdict is kept until the label list or a definition
changes), and labels.decide sets its label, label_source (which copy it may get: the label's own, its group's or
General's) and whether it goes on:
  * the model unavailable while General label_check = required (the default): the account is not verified ("label
    not checked: why") and the next run asks again; label_check = skip decides on the rules alone, with the group's
    copy. Accounts already verified are never un-verified for it: they keep their label, and an unchecked one gets
    its group's copy (labels.copy_level);
  * a public body is disqualified, as are a membership body or society, a company no label fits or one whose
    industry is switched off when the model is sure; when it is not, the doubt ("industry uncertain: ...") joins
    the account's doubts for the weekly hand-check, and approving it verifies the account with General copy;
  * otherwise the account goes on to HubSpot with the label the rule gives.
The run also checks accounts already verified that have no fresh verdict, so the queue converges with no command
run; their decisions are written the same way (a held one goes back to queued and to the hand-check). Both kinds
share the run's share of calls (labels.MAX_LABEL_CALLS_PER_RUN, LABEL_SECONDS): accounts with a send-approval card waiting first,
then Focus groups, then queue order; the rest wait for the next run. A send-approval card rendered before its account's label was decided, which
no longer fits it (another label, a less specific copy level, held or disqualified), is withdrawn by the next
poll_approvals (enrol/approvals.withdraw_unfit: this job runs dry and edits no card in Slack), so the next enrol
proposes the company again with the right copy. A verdict is a paid read that reaches no prospect, like an Apollo
search page, so it is asked in dry-run too (each call is about $0.01, within the Claude cap).

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
from dataclasses import dataclass, field
from typing import Any

from us_outbound import labels, parse
from us_outbound.accounts import any_us_state, us_country
from us_outbound.clean.domains import is_personal_domain, is_public_body
from us_outbound.clean.people import size_band, state_code
from us_outbound.clients.db import new_id
from us_outbound.clients.http import ApiError
from us_outbound.context import Context
from us_outbound.enrol import enrol, focus, queue
from us_outbound.facts import newest_by
from us_outbound.logs import log
from us_outbound.scoring import tiers
from us_outbound.scoring.score import latest_facts
from us_outbound.settings.overrides import effective
from us_outbound.settings.model import CLAY_REQUIRED, SIZE_BANDS, General, Settings

JOB = "verify_accounts"
VERIFIED = "verified"
WAITING_STATUSES = ("new", "queued")
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
ROLES_EDGE = 50  # the Roles tab's 10-49 and 50-249 orders: whom we write to changes here
EDGE_MARGIN = 2  # Apollo's count is an estimate: this close to an edge it could be either side
DOUBTFUL = "doubtful Apollo facts, waiting for the weekly hand-check"
# check() reasons a doubt can stand behind: the hand-check sees the account rather than it failing unseen.
DOUBTABLE = frozenset({"HQ state unknown", "employee count unknown", "no Industries label"})
NO_STATE = "Apollo gives no HQ state"
NO_SIZE = "Apollo gives no employee count or size band"
NO_INDUSTRY = "Apollo gives no industry"
# Doubts approving cannot settle: only an Overrides row fills the fact in (Harry, 6 Oct 2026).
MISSING = frozenset({NO_STATE, NO_SIZE, NO_INDUSTRY})
# check() reasons the label check can settle: it sets the label, so the account is checked again after it.
LABEL_REASONS = frozenset({"no Industries label", "industry switched off"})
OUT_OF_QUEUE_TIERS = frozenset({tiers.EXCLUDED, tiers.HELD})


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def with_overrides(account: Mapping[str, Any], settings: Settings) -> dict:
    """The account with its domain's Overrides rows applied (SPEC 5; settings/overrides.py)."""
    return effective(account, settings)


def check(account: Mapping[str, Any], facts: Mapping[str, Any], settings: Settings,
          suppressed: Collection[str], partners: Collection[str]) -> str | None:
    """Why the account cannot be verified on what is already known (checks 1 to 5, and earlier HubSpot facts)."""
    domain = _lower(account.get("domain"))
    if not domain:
        return "no domain"
    if is_personal_domain(domain):
        return "a personal email domain"
    if is_public_body(domain):
        return "a public body, never prospected"
    if domain in suppressed:
        return "domain suppressed"
    if domain in partners or tiers.partner_match(account, facts):
        return "a partner, never prospected"
    state = state_code(str(account.get("hq_state") or ""))
    visitor = any_us_state(account)
    if not state and not (visitor and us_country(account.get("hq_country"))):
        return "HQ state unknown"
    if state and not visitor:
        if state in NEVER_STATES:
            return "HQ in CA or WA"
        if state not in settings.active_states():
            return "HQ state not active"
    employees = tiers.as_number(account.get("employees"))
    band = account.get("size_band")
    if employees is None and band not in SIZE_BANDS:
        return "employee count unknown"
    if not (settings.size_in_range(employees) if employees is not None else settings.band_in_range(band)):
        return outside(settings)
    ind = settings.industry(str(account.get("industry") or ""))
    if ind is None:
        return "no Industries label"
    if not ind.active:
        return "industry switched off"
    for fact, reason in tiers.HUBSPOT_EXCLUSIONS:
        if parse.truthy(facts.get(fact)):
            return f"HubSpot: {reason}"
    return None


def outside(settings: Settings) -> str:
    """check()'s reason for a size outside the General range: "outside 10 to 249 employees"."""
    return f"outside {settings.size_range_text()} employees"


def doubtable(why: str | None, settings: Settings) -> bool:
    """Whether a doubt can stand behind check()'s reason: the hand-check sees the account rather than it failing
    unseen."""
    return why is None or why in DOUBTABLE or why == outside(settings)


def size_edges(settings: Settings | None) -> tuple[int, ...]:
    """The first headcounts where what we do changes: the floor, the Roles order, one past the ceiling."""
    g = settings.general if settings is not None else General()
    return tuple(sorted({g.min_employees, ROLES_EDGE, g.max_employees + 1}))


def near_edge(n: int, settings: Settings | None = None) -> int | None:
    """The size edge Apollo's estimate of n staff is within EDGE_MARGIN of, or None."""
    return next((e for e in size_edges(settings) if e - EDGE_MARGIN <= n < e + EDGE_MARGIN), None)


def doubts(account: Mapping[str, Any], settings: Settings | None = None) -> list[str]:
    """What in the account's Apollo facts is too doubtful to verify on unseen (Overrides already applied).
    settings gives the size edges (the General range); without it, SPEC 2's 10 to 249."""
    out: list[str] = []
    if not state_code(str(account.get("hq_state") or "")) and not (
            any_us_state(account) and us_country(account.get("hq_country"))):
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
    g = settings.general if settings is not None else General()
    if band in SIZE_BANDS and g.min_employees <= n <= g.max_employees and size_band(n) != band:  # outside: fails
        out.append(f"the employee count ({n}) and the size band ({band}) disagree")
    edge = near_edge(n, settings)
    if edge is not None:
        out.append(f"Apollo's estimate of {n} staff is within {EDGE_MARGIN} of the {edge}-staff edge")
    return out


def standing(found: Iterable[str], cleared: Collection[str]) -> list[str]:
    """The doubts still open: those a hand-check has not cleared, and every missing fact (MISSING), which
    approving cannot fill in."""
    return [d for d in found if d in MISSING or d not in cleared]


def cross_check(ctx: Context, account: Mapping[str, Any], found: Sequence[str]) -> list[str]:
    """The doubts a second source leaves standing: Clay's cross-check of HQ state and size (clay_cross_check.settle).

    With clay_cross_check = yes and Clay's answer stored, a doubt it settles is dropped and one it disagrees with
    is restated with both values; otherwise every doubt stands.
    """
    from us_outbound import clay_cross_check

    return clay_cross_check.settle(ctx, account, found)


def clay_pre_pass(ctx: Context, todo: Sequence[dict], facts: Mapping[str, Mapping[str, Any]],
                  suppressed: Collection[str], partners: Collection[str], cleared: Mapping[str, set[str]]) -> dict:
    """Ask Clay about the run's doubtful accounts and fill their missing facts, before the checks
    (clay_cross_check.ask); the job summary's clay_cross_check entry."""
    from us_outbound import clay_cross_check

    return clay_cross_check.ask(ctx, todo, facts, suppressed, partners, cleared)


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
    latest = newest_by(rows, lambda e: str(e.get("account_id")), DOUBT_FACT,
                       where=lambda e: isinstance(e.get("value"), Mapping))
    cleared: dict[str, set[str]] = defaultdict(set)
    for e in rows:
        if e.get("fact") == CLEARED_FACT and isinstance(value := e.get("value"), Mapping):
            cleared[str(e.get("account_id"))] |= {str(r) for r in value.get("reasons") or ()}
    return {aid: dict(e["value"]) for aid, e in latest.items()}, cleared


def open_doubts(ctx: Context) -> list[dict]:
    """The accounts waiting in new or queued for the hand-check, each with its open doubts: those not cleared,
    and the facts still missing once Overrides are applied (an Overrides row that fills one settles it)."""
    recorded, cleared = doubt_history(ctx)
    if not recorded:
        return []
    out = []
    for a in ctx.store.select("accounts", {"account_id": sorted(recorded), "status": list(WAITING_STATUSES)}):
        missing = MISSING & set(doubts(with_overrides(a, ctx.settings), ctx.settings))
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
            if MISSING & set(doubts(with_overrides(a, ctx.settings), ctx.settings)):
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


def _events(ctx: Context, account_ids: list[str]) -> dict[str, list[dict]]:
    events: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(account_ids), ID_CHUNK):
        for e in ctx.store.select("signal_events", {"account_id": account_ids[i : i + ID_CHUNK]}):
            events[e["account_id"]].append(e)
    return events


def _facts(ctx: Context, account_ids: list[str]) -> dict[str, dict[str, Any]]:
    return {aid: latest_facts(evs) for aid, evs in _events(ctx, account_ids).items()}


@dataclass
class LabelRun:
    """The run's label checks: the Checker, the candidates asked first, and the tally for the summary."""

    checker: labels.Checker
    tally: Counter[str] = field(default_factory=Counter)
    changed: list[dict] = field(default_factory=list)  # verified accounts whose label, copy level or status moved

    def decided(self, d: labels.Decision) -> None:
        self.tally[{labels.HOLD: "held", labels.DISQUALIFY: "disqualified"}.get(d.action, d.source)] += 1

    def summary(self) -> dict[str, Any]:
        ch = self.checker
        return {"checked": ch.asked, **dict(self.tally), "unavailable_reason": ch.unavailable,
                "usd": ch.usd() if ch.asked else 0.0, "labels_hash": ch.hash, "changed": self.changed[:LIST_LIMIT]}


def needs_verdict(run: LabelRun, account: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the model would be asked about this account: no Overrides or approver label, no fresh verdict, and
    something to check against."""
    if labels.override_for(account, run.checker.settings)[0]:
        return False
    stored = labels.latest_verdict(events)
    return not run.checker.fresh(stored, events) and not labels.Material.of(account, events).empty


def carded(ctx: Context) -> set[str]:
    """The accounts with a send-approval card waiting in Slack: the next to be emailed, so checked first."""
    from us_outbound.enrol import approvals

    return {i.account_id for i in approvals.items(ctx.store, (approvals.OPEN,))}


def check_order(accounts: Iterable[Mapping[str, Any]], settings: Settings,
                first: Collection[str] = ()) -> list[Mapping[str, Any]]:
    """The label check's order: accounts with a card waiting first, then Focus groups, then queue order."""
    return sorted(accounts, key=lambda a: (str(a.get("account_id")) not in first,
                                           focus.group_rank(settings.industry_group_of(a), settings),
                                           queue.order_key(a, settings)))


def ask_first(run: LabelRun, accounts: Iterable[Mapping[str, Any]], events: Mapping[str, Sequence[Mapping[str, Any]]],
              settings: Settings, first: Collection[str] = ()) -> None:
    """Ask about the run's candidates (new and verified alike) in check_order, within the run's share; the Checker
    keeps each answer for the decisions after (labels.Checker.verdict)."""
    order = check_order(accounts, settings, first)
    for a in order:
        if run.checker.unavailable or run.checker.budget.why_not():
            return
        run.checker.verdict(a, events.get(a["account_id"], []))


def converge(ctx: Context, run: LabelRun, accounts: Sequence[Mapping[str, Any]],
             events: Mapping[str, Sequence[Mapping[str, Any]]], cleared: Mapping[str, set[str]]) -> list[dict]:
    """The label check for accounts already verified (Harry, 7 Oct 2026: the queue converges with no command run).
    A held one goes back to queued with its doubt, for the hand-check; a disqualified one leaves the queue; the model
    unavailable changes nothing (they keep flowing). Returns the doubt rows to write."""
    rows: list[dict] = []
    for a in accounts:
        evs = events.get(a["account_id"], [])
        d, v, rules, _, asked = labels.judge(run.checker, a, evs)
        if d is None:
            run.tally["verified_unchecked"] += 1
            continue
        level = labels.copy_level(a)
        cols = labels.apply(ctx, a, d, v, rules=rules, asked=asked, stored=labels.latest_verdict(evs))
        run.decided(d)
        held = d.action == labels.HOLD and d.reason not in cleared.get(a["account_id"], set())
        if held:
            ctx.store.upsert("accounts", [{"account_id": a["account_id"], "status": "queued"}])
            rows.append(doubt_fact(ctx, a, [d.reason]))
        after = {**a, **cols}
        if held or d.action == labels.DISQUALIFY or after.get("industry") != a.get("industry") \
                or labels.copy_level(after) != level:
            run.changed.append({"account_id": a["account_id"], "domain": a.get("domain"),
                                "from": a.get("industry"), "to": d.label, "source": d.source, "action": d.action,
                                "reason": d.reason})
    return rows


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
    required = s.general.label_check == labels.REQUIRED
    # Verified accounts the label check looks at again; with label_check = skip, only those an Overrides row moves.
    verified_open = [a for a in ctx.store.select("accounts", {"status": VERIFIED})
                     if a.get("tier") not in OUT_OF_QUEUE_TIERS and (required or labels.override_moves(a, s))]
    events = _events(ctx, [a["account_id"] for a in [*todo, *verified_open]])
    facts = {aid: latest_facts(evs) for aid, evs in events.items()}
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

    recorded, cleared = doubt_history(ctx, [a["account_id"] for a in [*todo, *verified_open]])
    summary["clay_cross_check"] = clay_pre_pass(ctx, todo, facts, suppressed, partners, cleared)
    # The label check (labels.py): the model is asked first about the candidates, new and verified alike: those with
    # a card waiting in Slack first, then Focus groups, then queue order, so the run's share goes where it matters.
    lab = LabelRun(labels.Checker(ctx))

    def cheap_ok(a: Mapping[str, Any]) -> bool:
        """Checks 1 to 4 pass, or fail only on the label: an account failing on its state or size costs no call."""
        why = check(with_overrides(a, s), facts.get(a["account_id"], {}), s, suppressed, partners)
        return why is None or why in LABEL_REASONS

    def asks(a: Mapping[str, Any]) -> bool:
        return needs_verdict(lab, a, events.get(a["account_id"], []))

    unchecked = [a for a in verified_open
                 if cheap_ok(a) and (asks(a) or not a.get("label_source") or labels.override_moves(a, s))]
    ask_first(lab, [*(a for a in todo if required and cheap_ok(a) and asks(a)), *filter(asks, unchecked)], events, s,
              carded(ctx) if unchecked else ())
    doubt_rows: list[dict] = converge(ctx, lab, unchecked, events, cleared)
    for a in todo:
        acct = with_overrides(a, s)
        why = check(acct, facts.get(a["account_id"], {}), s, suppressed, partners)
        label_doubt = ""
        if why is None or why in LABEL_REASONS:
            evs = events.get(a["account_id"], [])
            d, v, rules, not_asked, asked = labels.judge(lab.checker, a, evs)
            if d is None:
                lab.tally["unchecked"] += 1
                fail(a, f"label not checked: {not_asked}")
                continue
            cols = labels.apply(ctx, a, d, v, rules=rules, asked=asked, stored=labels.latest_verdict(evs))
            lab.decided(d)
            if d.action == labels.DISQUALIFY:
                fail(a, f"disqualified: {d.reason}")
                continue
            acct = with_overrides({**a, **cols}, s)
            label_doubt = d.reason if d.action == labels.HOLD else ""
            why = check(acct, facts.get(a["account_id"], {}), s, suppressed, partners)
        if doubtable(why, s):
            found = [*cross_check(ctx, acct, doubts(acct, s)), *([label_doubt] if label_doubt else [])]
            still = standing(found, cleared.get(a["account_id"], set()))
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
    summary["labels"] = lab.summary()
    summary.update(
        status="ok", waiting=len(waiting), checked=len(todo), verified=len(verified), excluded=excluded[:LIST_LIMIT],
        to_hand_check=len(doubtful), to_hand_check_accounts=doubtful[:LIST_LIMIT],
        not_verified=dict(failed), not_verified_accounts=examples, errors=errors[:20],
        left_for_next_run=max(0, len(waiting) - len(todo)), rescore=scored,
    )
    log("verify_accounts_done", run_id=ctx.run_id, **{k: v for k, v in summary.items() if k != "not_verified_accounts"})
    return summary
