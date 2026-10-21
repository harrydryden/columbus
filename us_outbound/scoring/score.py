"""Scoring: match facts to the Signals tab, then score, tier and angle each account (SPEC 9 "Scoring").

Facts are signal_events rows {event_id, account_id, source, fact, value, quote, source_url,
observed_at} (SPEC 7). Two kinds:
  * field facts: fact is a field name (model.SOURCE_FIELDS), value a scalar or a list;
  * text facts from the text sources (model.TEXT_SOURCES): fact is one of TEXT_FACTS, and
    the text is value (a str, or a dict with item / type / provider) plus quote.
The calendar and irs_bmf virtual facts (month, days_to_fiscal_year_start) went with W2-2 (20 Oct 2026): no source
writes irs_bmf facts, and the one calendar row is switched off (code-scoring B5). A Signals row on either source
still validates, and never matches.

SPEC 9 steps:
  1. Score: the sum of every active Score signal with a fact fresher than its
     counts_for_days, each capped at its max_weight, the total capped at SCORE_CAP (settings/model.py).
     A term signal with max_weight counts weight once per DISTINCT term found; without
     max_weight it counts weight once. Negative weights are allowed and the total is not
     floored at 0.
  2. Tier (tier(): Excluded, Held or Queue; tiers.py). 3. Suppress: the domain goes on suppression for
     counts_for_days. 4. Angle (angle.py). The opener is chosen at enrol time (enrol/openers.py).

"Not read" (SPEC 8): when a source's latest read_status is blocked or error, that source's
facts add no points. Hold, Exclude and Suppress signals still count matches from any fresh
read, so a failed read never clears a Hold.

An Overrides row for the account's domain wins over every source (SPEC 5).

The score job (rescore) runs in one transaction under an advisory lock (W2-2; code-scoring B4, RS P0-8): a second
rescore waits for the first, up to LOCK_WAIT_SECONDS, then skips with "rescore already running". It inserts the new
signal_matched rows, each stamped with its run (value.run_id), before it deletes the rows it read at its start, and
commits; a rescore killed part-way rolls back and the old rows stand, so no account is left with none
(enrol.signals_now reads them for the snapshot each contact keeps).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound import icp
from us_outbound.clients.db import LockTimeout, new_id
from us_outbound.context import UK, Context
from us_outbound.facts import history, latest_by_fact, newest_by
from us_outbound.industry.material import naics_codes
from us_outbound.logs import log
from us_outbound.scoring import angle as angles
from us_outbound.scoring import tiers
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.model import AGED_FACTS, SCORE_CAP, Settings, Signal
from us_outbound.settings.overrides import effective
from us_outbound.settings.overrides import parse_value as parse_override
from us_outbound.timeparse import uk_day, utc

TEXT_FACTS = frozenset({"benefit", "mental_health_provision", "culture_statement", "posting_text", "page_text"})
UNREAD_STATUSES = frozenset({"blocked", "error"})
SCORING_SOURCE = "scoring"
MATCH_FACT = "signal_matched"
QUOTE_LIMIT = 300  # SPEC 6: quote at most 300 characters

# The score job (SPEC 9).
SCORED_STATUSES = ("new", "queued", "verified", "enrolled", "engaged")
ANGLE_FIXED_STATUSES = frozenset({"enrolled", "engaged"})  # the angle is fixed at enrollment
ID_CHUNK = 1000
# The rescore's advisory lock (clients/db.Store.advisory_lock): one key for every process, and how long a second
# rescore waits for the first before it skips.
RESCORE_LOCK = int.from_bytes(hashlib.sha256(b"us_outbound:rescore").digest()[:8], "big", signed=True)
LOCK_WAIT_SECONDS = 60
ALREADY_RUNNING = "rescore already running"


@dataclass(frozen=True)
class Evidence:
    text: str  # what matched: the term as found, a fact's quote, or "field = value"
    quote: str = ""
    url: str = ""
    source: str = ""
    observed_at: datetime | date | None = None
    term: str = ""  # the sheet term, for term matches


@dataclass(frozen=True)
class Match:
    signal: Signal
    weight_applied: int
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreResult:
    score: int
    matches: list[Match]
    tier: str
    tier_reason: str
    angle: str
    suppress_days: int | None
    suppress_signal: str | None = None  # the Suppress signal with the longest window
    exclusion: str | None = None
    partner: str | None = None  # partners.reason code when the account is a partner


# -- time ----------------------------------------------------------------------


def age_days(event: Mapping[str, Any], today: date) -> int | None:
    d = uk_day(event.get("observed_at"))
    return None if d is None else (today - d).days


def fresh_facts(events: Iterable[Mapping[str, Any]], signal: Signal, today: date) -> list[Mapping[str, Any]]:
    """Events from the signal's sources observed within its counts_for_days (UK date)."""
    out = []
    for e in events:
        if e.get("source") not in signal.sources:
            continue
        age = age_days(e, today)
        if age is not None and age <= signal.counts_for_days:
            out.append(e)
    return out


# -- facts -----------------------------------------------------------------------


def unread_sources(events: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Sources whose latest read_status is blocked or error (SPEC 8: not read)."""
    latest = newest_by(events, lambda e: e.get("source"), "read_status")
    return frozenset(s for s, e in latest.items() if str(e.get("value") or "").strip().lower() in UNREAD_STATUSES)


def latest_facts(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The latest value of each field fact, whatever its source or age (for hard exclusions)."""
    rows = (e for e in events if e.get("source") != SCORING_SOURCE)
    return {f: e.get("value") for f, e in latest_by_fact(rows, skip_facts=TEXT_FACTS).items()}


def fact_text(event: Mapping[str, Any]) -> str:
    """The searchable text of a text fact: its value's text parts, its quote, then its provision type."""
    v = event.get("value")
    quote = str(event.get("quote") or "")
    parts: list[str] = []
    if isinstance(v, str):
        parts.append(v)
    elif isinstance(v, Mapping):
        for k in ("item", "provider", "text", "quote"):
            if isinstance(v.get(k), str) and v[k].strip() and v[k] != quote:
                parts.append(v[k])
    elif isinstance(v, (list, tuple)):
        parts.extend(x for x in v if isinstance(x, str) and x.strip())
    if quote:
        parts.append(quote)
    if isinstance(v, Mapping) and isinstance(v.get("type"), str) and v["type"].strip():
        parts.append(v["type"].replace("_", " "))  # Clay's provision type, e.g. "carrier_eap" -> "carrier eap"
    return "\n".join(parts)


def _quote(text: Any) -> str:
    q = " ".join(str(text or "").split())
    return q if len(q) <= QUOTE_LIMIT else q[: QUOTE_LIMIT - 1] + "…"


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ", ".join(_fmt(x) for x in v)
    return str(v)


# -- matching ----------------------------------------------------------------------


def _weight(signal: Signal, distinct: int) -> int:
    if signal.action != "Score":
        return 0
    if signal.max_weight is None:
        return signal.weight
    total = signal.weight * distinct
    if signal.weight >= 0:
        return min(total, signal.max_weight)
    return max(total, -abs(signal.max_weight))  # a negative weight's max_weight caps its size


def aged_value(fact: str, event: Mapping[str, Any], today: date) -> Any:
    """A fact's value as of today: an AGED_FACTS day count plus the days since it was observed."""
    value = event.get("value")
    if fact not in AGED_FACTS:
        return value
    n, age = tiers.as_number(value), age_days(event, today)
    if n is None or age is None:
        return value
    aged = n + max(0, age)
    return int(aged) if aged.is_integer() else aged


def _match_condition(
    signal: Signal,
    events: list[Mapping[str, Any]],
    today: date,
    overrides: Mapping[str, Any],
    columns: Mapping[str, Any] | None = None,
) -> Match | None:
    cond = signal.condition
    assert cond is not None
    latest = latest_by_fact(events, skip_facts=TEXT_FACTS)
    values: dict[str, Any] = {f: aged_value(f, e, today) for f, e in latest.items()}
    # Only when no size is known from a fact: a band column never overrules an employee count.
    sized = any(f in values for f in ACCOUNT_FACTS)
    from_columns = {} if sized else {f: v for f, v in (columns or {}).items() if f in cond.fields}
    if "employees" in from_columns:  # an employee count, even from the account row, settles the size
        from_columns.pop("size_band", None)
    values.update(from_columns)
    forced = {f: v for f, v in overrides.items() if f in cond.fields}
    values.update(forced)
    if not cond.evaluate(values):
        return None
    evidence = []
    for f in sorted(cond.fields, key=lambda f: (cond.text.find(f), f)):
        if f in forced:
            evidence.append(Evidence(f"{f} = {_fmt(forced[f])}", source="override", observed_at=today))
        elif f in from_columns:
            evidence.append(Evidence(f"{f} = {_fmt(from_columns[f])}", source=ACCOUNT_FACT_SOURCE, observed_at=today))
        elif f in latest:
            e = latest[f]
            quote = _quote(e.get("quote"))
            evidence.append(
                Evidence(
                    quote or f"{f} = {_fmt(values[f])}",
                    quote=quote,
                    url=str(e.get("source_url") or ""),
                    source=str(e.get("source") or ""),
                    observed_at=utc(e.get("observed_at"), date_tz=UK),
                )
            )
    return Match(signal, _weight(signal, 1), evidence)


def _match_terms(signal: Signal, events: list[Mapping[str, Any]]) -> Match | None:
    remaining = list(signal.terms)
    evidence: list[Evidence] = []
    for e in history(events):
        if e.get("fact") not in TEXT_FACTS:
            continue
        for tm in find_terms(fact_text(e), tuple(remaining), signal.context):
            evidence.append(
                Evidence(
                    tm.matched,
                    quote=_quote(e.get("quote")),
                    url=str(e.get("source_url") or ""),
                    source=str(e.get("source") or ""),
                    observed_at=utc(e.get("observed_at"), date_tz=UK),
                    term=tm.term,
                )
            )
            remaining.remove(tm.term)
        if not remaining:
            break
    if not evidence:
        return None
    return Match(signal, _weight(signal, len(evidence)), evidence)


# The account's own columns a condition may read when no fact carries them (2 Oct 2026): Apollo's
# search rows send no employee count, so size is known only as the band searched (accounts.size_band),
# and a size signal on facts alone never fired. A fact, when there is one, wins.
ACCOUNT_FACTS = ("employees", "size_band")
ACCOUNT_FACT_SOURCE = "apollo_org"


def account_facts(account: Mapping[str, Any]) -> dict[str, Any]:
    return {f: account[f] for f in ACCOUNT_FACTS if account.get(f) not in (None, "")}


def match_signal(
    signal: Signal,
    facts: Sequence[Mapping[str, Any]],
    today: date,
    *,
    overrides: Mapping[str, Any] | None = None,
    account: Mapping[str, Any] | None = None,
) -> Match | None:
    """Match one signal against an account's facts (its signal_events rows); None if it does not apply.

    A Score signal ignores facts from a source that was not read (latest read_status
    blocked or error): no points, and None when none of its sources was read.
    """
    events = fresh_facts(facts, signal, today)
    if signal.action == "Score":
        unread = unread_sources(facts) & set(signal.sources)
        if unread:
            if unread >= set(signal.sources):
                return None
            events = [e for e in events if e.get("source") not in unread]
    if signal.is_condition:
        columns = account_facts(account or {}) if ACCOUNT_FACT_SOURCE in signal.sources else {}
        return _match_condition(signal, events, today, overrides or {}, columns)
    if signal.terms:
        return _match_terms(signal, events)
    return None


def total_score(matches: Iterable[Match], score_cap: int) -> int:
    return min(sum(m.weight_applied for m in matches if m.signal.action == "Score"), score_cap)


# -- evidence, tiers ----------------------------------------------------------------------------


def _is_acronym(word: str) -> bool:
    """EAP, PTO, HRIS; or a capital after the first letter, as in ComPsych or BetterUp.

    Each hyphen or slash part is judged on its own, so "Employer-Paid" is not a brand.
    """
    for part in re.split(r"[-/]", word):
        letters = [c for c in part if c.isalpha()]
        if len(letters) >= 2 and (all(c.isupper() for c in letters) or any(c.isupper() for c in part[1:])):
            return True
    return False


def evidence_display(ev: Evidence) -> str:
    """Evidence text as it reads mid-sentence (a tier reason; enrol/openers.py's lines).

    A term match is lower-cased word by word, except where the sheet term has capitals
    (a brand or acronym: its sheet spelling is used) or the page word is an acronym.
    Other evidence (a quote, or "field = value") is used as it is.
    """
    if not ev.term:
        return ev.text
    got, want = ev.text.split(), ev.term.split()
    if len(want) != len(got):
        want = [""] * len(got)
    out = []
    for g, w in zip(got, want):
        if any(c.isupper() for c in w):
            out.append(w)
        elif _is_acronym(g):
            out.append(g)
        else:
            out.append(g.lower())
    return " ".join(out)


def _evidence_summary(m: Match, limit: int = 3) -> str:
    shown: list[str] = []
    for ev in m.evidence:
        t = evidence_display(ev)
        if t and t not in shown:
            shown.append(t)
    text = "; ".join(shown[:limit])
    return f"{m.signal.signal} ({text})" if text else m.signal.signal


def score_parts(matches: Iterable[Match], score: int) -> str:
    """ "Mental health support listed (+25), New People leader (+30)", noting a cap."""
    scored = [m for m in matches if m.signal.action == "Score"]
    parts = [f"{m.signal.signal} ({m.weight_applied:+d})" for m in scored if m.weight_applied]
    text = ", ".join(parts) or "no scoring signals found"
    raw = sum(m.weight_applied for m in scored)
    if raw > score:
        text += f"; capped at {score}"
    return text


def tier(score: int, matches: list[Match], exclusion: str | None) -> tuple[str, str]:
    """(tier, tier_reason) by SPEC 9's first-rule-wins order: Excluded, Held, else Queue (tiers.py)."""
    if exclusion:
        return tiers.EXCLUDED, f"{tiers.EXCLUDED}: {exclusion}"
    excludes = [m for m in matches if m.signal.action == "Exclude"]
    if excludes:
        return tiers.EXCLUDED, f"{tiers.EXCLUDED}: " + ", ".join(_evidence_summary(m) for m in excludes)
    holds = [m for m in matches if m.signal.action == "Hold"]
    if holds:
        return tiers.HELD, f"{tiers.HELD}: " + ", ".join(_evidence_summary(m) for m in holds)
    return tiers.QUEUE, f"{tiers.QUEUE}: {score_parts(matches, score)}"


# -- one account ---------------------------------------------------------------------


def score_account(
    account: Mapping[str, Any], events: Sequence[Mapping[str, Any]], settings: Settings, today: date
) -> ScoreResult:
    domain = str(account.get("domain") or "").strip().lower()
    overrides = {k: parse_override(v) for k, v in settings.overrides_for(domain).items()} if domain else {}
    acct = effective(account, settings)
    facts = {**latest_facts(events), **overrides}

    matches = [m for s in settings.active_signals()
               if (m := match_signal(s, events, today, overrides=overrides, account=acct))]
    score = total_score(matches, SCORE_CAP)
    exclusion = tiers.hard_exclusion(acct, facts, settings, today)
    partner = icp.partner_category(acct, facts)
    tier_name, reason = tier(score, matches, exclusion)
    suppress = [m for m in matches if m.signal.action == "Suppress"]
    longest = max(suppress, key=lambda m: m.signal.counts_for_days) if suppress else None
    return ScoreResult(
        score=score,
        matches=matches,
        tier=tier_name,
        tier_reason=reason,
        angle=angles.choose_angle(matches, settings),
        suppress_days=longest.signal.counts_for_days if longest else None,
        suppress_signal=longest.signal.signal if longest else None,
        exclusion=exclusion,
        partner=partner,
    )


# -- the score job ---------------------------------------------------------------------


def _chunks(items: list[str], n: int = ID_CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _match_row(account_id: str, m: Match, now: datetime, run_id: str) -> dict:
    """A signal_matched row, stamped with the rescore that wrote it (value.run_id; observed_at its start)."""
    first = m.evidence[0] if m.evidence else None
    return {
        "event_id": new_id(),
        "account_id": account_id,
        "source": SCORING_SOURCE,
        "fact": MATCH_FACT,
        "value": {"signal": m.signal.signal, "weight": m.weight_applied, "evidence": [ev.text for ev in m.evidence],
                  "run_id": run_id},
        "quote": first.quote if first else "",
        "source_url": first.url if first else "",
        "observed_at": now,
    }


def _apply_suppression(ctx: Context, wanted: dict[str, tuple[str, int]]) -> int:
    """Suppress each domain for its signal's counts_for_days, never shortening an existing entry."""
    if not wanted:
        return 0
    existing: dict[str, dict] = {}
    for chunk in _chunks(sorted(wanted)):
        for r in ctx.store.select("suppression", {"domain": chunk, "email_sha256": None}):
            existing[r["domain"]] = r
    rows = []
    for domain, (signal, days) in sorted(wanted.items()):
        expires = ctx.now + timedelta(days=days)
        old = existing.get(domain)
        if old is not None:
            old_expiry = utc(old.get("expires_at"), date_tz=UK)
            if old_expiry is None or old_expiry >= expires:
                continue  # indefinite, or already suppressed for longer
        rows.append({
            "email_sha256": None,
            "domain": domain,
            "reason": f"signal:{signal}",
            "source": SCORING_SOURCE,
            "added_at": old.get("added_at") if old and old.get("source") == SCORING_SOURCE else ctx.now,
            "expires_at": expires,
        })
    if rows:
        ctx.store.upsert("suppression", rows)
    return len(rows)


def _add_partners(ctx: Context, wanted: dict[str, dict]) -> int:
    if not wanted:
        return 0
    known = set()
    for chunk in _chunks(sorted(wanted)):
        known |= {r["domain"] for r in ctx.store.select("partners", {"domain": chunk})}
    rows = [row for d, row in sorted(wanted.items()) if d not in known]
    if rows:
        ctx.store.upsert("partners", rows)
    return len(rows)


def rescore(ctx: Context) -> dict:
    """The score job (SPEC 9): score, tier and angle every open account, in one transaction under the rescore lock;
    returns counts per tier and how often each signal fired. A second rescore while one runs waits up to
    LOCK_WAIT_SECONDS, then skips (the one running writes the scores)."""
    try:
        with ctx.store.transaction():
            ctx.store.advisory_lock(RESCORE_LOCK, wait_seconds=LOCK_WAIT_SECONDS)
            summary = _rescore(ctx)
    except LockTimeout:
        summary = {"skipped": True, "reason": ALREADY_RUNNING}  # the job contract's skip (ops/heartbeat.py)
        log("score_skipped", run_id=ctx.run_id, reason=ALREADY_RUNNING)
        return summary
    log("score_done", run_id=ctx.run_id, dry_run=ctx.dry_run, **summary)
    return summary


def _rescore(ctx: Context) -> dict:
    settings, store, now = ctx.settings, ctx.store, ctx.now
    today = ctx.today_uk()

    accounts = store.select("accounts", {"status": list(SCORED_STATUSES)})
    ids = [a["account_id"] for a in accounts]
    events: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks(ids):
        for e in store.select("signal_events", {"account_id": chunk}):
            events[e["account_id"]].append(e)
    # Enrolled and engaged accounts are rescored for exclusions only (W2-2; code-scoring §6): their tier and its
    # reason, and the suppression and partner rows a match brings; their score, angle and match rows stay as they
    # were when they were enrolled (contacts keep the snapshot: tier_at_enrol, signals_at_enrol).
    fixed = {a["account_id"] for a in accounts if a.get("status") in ANGLE_FIXED_STATUSES}
    # The match rows this rescore replaces: every open account's row it read, by id (under the lock, so no other
    # rescore writes in between; an earlier rescore in the same job run, settings_sync's lookalikes then its own,
    # included). Rows another rescore commits after this one are never among them.
    old_rows = [str(e["event_id"]) for aid, evs in events.items() if aid not in fixed for e in evs
                if e.get("source") == SCORING_SOURCE and e.get("fact") == MATCH_FACT]

    updates: list[dict] = []
    match_rows: list[dict] = []
    suppress: dict[str, tuple[str, int]] = {}
    partners: dict[str, dict] = {}
    tier_counts: Counter[str] = Counter()
    fired: Counter[str] = Counter()
    changed = 0
    for a in accounts:
        aid = a["account_id"]
        r = score_account(a, events.get(aid, []), settings, today)
        tier_counts[r.tier] += 1
        new: dict[str, Any] = {"tier": r.tier, "tier_reason": r.tier_reason}
        if aid not in fixed:
            new.update(score=r.score, angle=r.angle)
            fired.update(m.signal.signal for m in r.matches)
            match_rows.extend(_match_row(aid, m, now, ctx.run_id) for m in r.matches)
        diff = {k: v for k, v in new.items() if a.get(k) != v}
        if diff:
            changed += 1
            # A tier the rescore writes for the first time since W2-2 (a retired name becoming Queue) is no change.
            moved = "tier" in diff and not (a.get("tier") in tiers.LEGACY_QUEUE_TIERS and r.tier == tiers.QUEUE)
            if aid in fixed and moved:
                log("score_enrolled_tier_change", account_id=aid, old=a.get("tier"), new=r.tier)
        updates.append({"account_id": aid, **diff, "last_scored": now})
        domain = str(a.get("domain") or "").strip().lower()
        if domain and r.suppress_signal and r.suppress_days:
            suppress[domain] = (r.suppress_signal, r.suppress_days)
        if domain and r.partner:
            partners[domain] = {
                "domain": domain,
                "name": a.get("clean_name") or a.get("legal_name") or domain,
                "reason": r.partner,
                "naics": ", ".join(naics_codes(a.get("naics"))) or None,
                "added_at": now,
            }

    if updates:
        store.upsert("accounts", updates)  # partial rows: upsert leaves absent columns alone
    if match_rows:  # the new rows first, then the old ones go: in no order is an account left with none
        store.insert("signal_events", match_rows)
    for chunk in _chunks(old_rows):
        store.delete("signal_events", {"event_id": chunk})
    n_suppressed = _apply_suppression(ctx, suppress)
    n_partners = _add_partners(ctx, partners)

    return {
        "accounts": len(accounts),
        "tiers": {t: tier_counts.get(t, 0) for t in (tiers.QUEUE, tiers.HELD, tiers.EXCLUDED)},
        "changed": changed,
        "signal_matches": len(match_rows),
        "replaced": len(old_rows),
        # How many open accounts each signal fired on (W2-2), most first: what moves the order, and what never fires.
        "fired": dict(sorted(fired.items(), key=lambda kv: (-kv[1], kv[0]))),
        "suppressed": n_suppressed,
        "partners": n_partners,
    }


run = rescore  # JOB CONTRACT: run(ctx) -> dict
