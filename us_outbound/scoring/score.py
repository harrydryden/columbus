"""Scoring: match facts to the Signals tab, then score, tier and angle each account (SPEC 9 "Scoring").

Facts are signal_events rows {event_id, account_id, source, fact, value, quote, source_url,
observed_at} (SPEC 7). Two kinds:
  * field facts: fact is a field name (model.SOURCE_FIELDS), value a scalar or a list;
  * text facts from the text sources (model.TEXT_SOURCES): fact is one of TEXT_FACTS, and
    the text is value (a str, or a dict with item / type / provider) plus quote.
Calendar facts (month, days_to_fiscal_year_start) are computed at score time from the UK
date and are always fresh; days_to_fiscal_year_start needs the account's latest irs_bmf
fiscal_year_end_month.

SPEC 9 steps:
  1. Score: the sum of every active Score signal with a fact fresher than its
     counts_for_days, each capped at its max_weight, the total capped at score_cap.
     A term signal with max_weight counts weight once per DISTINCT term found; without
     max_weight it counts weight once. Negative weights are allowed and the total is not
     floored at 0.
  2. Tier (tiers.py). 3. Suppress: the domain goes on suppression for counts_for_days.
  4-5. Angle and opener (angle.py).

"Not read" (SPEC 8): when a source's latest read_status is blocked or error, that source's
facts add no points. Hold, Exclude and Suppress signals still count matches from any fresh
read, so a failed read never clears a Hold.

An Overrides row for the account's domain wins over every source (SPEC 5).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clients.bq import new_id
from us_outbound.context import UK, Context
from us_outbound.logs import log
from us_outbound.scoring import angle as angles
from us_outbound.scoring import tiers
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.model import Settings, Signal

TEXT_FACTS = frozenset({"benefit", "mental_health_provision", "culture_statement", "posting_text", "page_text"})
UNREAD_STATUSES = frozenset({"blocked", "error"})
SCORING_SOURCE = "scoring"
MATCH_FACT = "signal_matched"
QUOTE_LIMIT = 300  # SPEC 6: quote at most 300 characters

# The score job (SPEC 9).
SCORED_STATUSES = ("new", "queued", "verified", "enrolled", "engaged")
ANGLE_FIXED_STATUSES = frozenset({"enrolled", "engaged"})  # the angle is fixed at enrollment
QUEUE_STATUSES = frozenset({"queued", "verified"})
SHARE_TIERS = (tiers.PRIORITY, tiers.STANDARD, tiers.CONTROL)
SHARE_MIN, SHARE_MAX = 0.05, 0.40  # each tier is 5-40% of the month's queue
SHARE_MIN_ACCOUNTS = 20  # too few accounts to judge the mix below this
ID_CHUNK = 1000

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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
    opener: str
    suppress_days: int | None
    legal_overlay: str = ""
    suppress_signal: str | None = None  # the Suppress signal with the longest window
    exclusion: str | None = None
    partner: str | None = None  # partners.reason code when the account is a partner


# -- time ----------------------------------------------------------------------


def _ts(v: Any) -> datetime | None:
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UK)
    return None


def age_days(event: Mapping[str, Any], today: date) -> int | None:
    t = _ts(event.get("observed_at"))
    return None if t is None else (today - t.astimezone(UK).date()).days


def _newest_first(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(events, key=lambda e: _ts(e.get("observed_at")) or _EPOCH, reverse=True)


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
    latest: dict[str, tuple[datetime, Any]] = {}
    for e in events:
        if e.get("fact") != "read_status":
            continue
        t = _ts(e.get("observed_at")) or _EPOCH
        src = e.get("source")
        if src not in latest or t >= latest[src][0]:
            latest[src] = (t, e.get("value"))
    return frozenset(s for s, (_, v) in latest.items() if str(v or "").strip().lower() in UNREAD_STATUSES)


def latest_facts(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The latest value of each field fact, whatever its source or age (for hard exclusions)."""
    out: dict[str, Any] = {}
    for e in reversed(_newest_first(events)):
        if e.get("source") == SCORING_SOURCE or e.get("fact") in TEXT_FACTS or not e.get("fact"):
            continue
        out[e["fact"]] = e.get("value")
    return out


def days_to_fiscal_year_start(fiscal_year_end_month: int, today: date) -> int:
    """Days from today to the first day of the month after the fiscal year-end month, next occurrence."""
    start_month = fiscal_year_end_month % 12 + 1
    start = date(today.year, start_month, 1)
    if start < today:
        start = date(today.year + 1, start_month, 1)
    return (start - today).days


def calendar_facts(events: Iterable[Mapping[str, Any]], today: date) -> dict[str, Any]:
    """The virtual calendar facts, computed from the UK date (SPEC 7: calendar, daily)."""
    facts: dict[str, Any] = {"month": today.month}
    fye = [e for e in events if e.get("source") == "irs_bmf" and e.get("fact") == "fiscal_year_end_month"]
    if fye:
        month = tiers.as_number(_newest_first(fye)[0].get("value"))
        if month is not None and 1 <= month <= 12:
            facts["days_to_fiscal_year_start"] = days_to_fiscal_year_start(int(month), today)
    return facts


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


def parse_override(text: str) -> Any:
    """An Overrides-tab value as a fact value: true/false/yes/no, a number, or the text."""
    t = str(text).strip()
    low = t.lower()
    if low in {"true", "yes"}:
        return True
    if low in {"false", "no"}:
        return False
    if t.isdigit() and not (t.startswith("0") and len(t) > 1):
        return int(t)
    try:
        return float(t)
    except ValueError:
        return t


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


def _match_condition(
    signal: Signal,
    events: list[Mapping[str, Any]],
    all_events: Sequence[Mapping[str, Any]],
    today: date,
    overrides: Mapping[str, Any],
) -> Match | None:
    cond = signal.condition
    assert cond is not None
    latest: dict[str, Mapping[str, Any]] = {}
    for e in reversed(_newest_first(events)):
        if e.get("fact") and e.get("fact") not in TEXT_FACTS:
            latest[e["fact"]] = e
    values: dict[str, Any] = {f: e.get("value") for f, e in latest.items()}
    virtual: dict[str, Any] = {}
    if {"calendar", "irs_bmf"} & set(signal.sources):
        virtual = calendar_facts(all_events, today)
        if "calendar" not in signal.sources:
            virtual.pop("month", None)
    values.update(virtual)
    forced = {f: v for f, v in overrides.items() if f in cond.fields}
    values.update(forced)
    if not cond.evaluate(values):
        return None
    evidence = []
    for f in sorted(cond.fields, key=lambda f: (cond.text.find(f), f)):
        if f in forced:
            evidence.append(Evidence(f"{f} = {_fmt(forced[f])}", source="override", observed_at=today))
        elif f in virtual:
            evidence.append(Evidence(f"{f} = {_fmt(virtual[f])}", source="calendar", observed_at=today))
        elif f in latest:
            e = latest[f]
            quote = _quote(e.get("quote"))
            evidence.append(
                Evidence(
                    quote or f"{f} = {_fmt(e.get('value'))}",
                    quote=quote,
                    url=str(e.get("source_url") or ""),
                    source=str(e.get("source") or ""),
                    observed_at=_ts(e.get("observed_at")),
                )
            )
    return Match(signal, _weight(signal, 1), evidence)


def _match_terms(signal: Signal, events: list[Mapping[str, Any]]) -> Match | None:
    remaining = list(signal.terms)
    evidence: list[Evidence] = []
    for e in _newest_first(events):
        if e.get("fact") not in TEXT_FACTS:
            continue
        for tm in find_terms(fact_text(e), tuple(remaining), signal.context):
            evidence.append(
                Evidence(
                    tm.matched,
                    quote=_quote(e.get("quote")),
                    url=str(e.get("source_url") or ""),
                    source=str(e.get("source") or ""),
                    observed_at=_ts(e.get("observed_at")),
                    term=tm.term,
                )
            )
            remaining.remove(tm.term)
        if not remaining:
            break
    if not evidence:
        return None
    return Match(signal, _weight(signal, len(evidence)), evidence)


def match_signal(
    signal: Signal,
    facts: Sequence[Mapping[str, Any]],
    today: date,
    *,
    overrides: Mapping[str, Any] | None = None,
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
        return _match_condition(signal, events, facts, today, overrides or {})
    if signal.terms:
        return _match_terms(signal, events)
    return None


def total_score(matches: Iterable[Match], score_cap: int) -> int:
    return min(sum(m.weight_applied for m in matches if m.signal.action == "Score"), score_cap)


# -- one account ---------------------------------------------------------------------


def score_account(
    account: Mapping[str, Any], events: Sequence[Mapping[str, Any]], settings: Settings, today: date
) -> ScoreResult:
    domain = str(account.get("domain") or "").strip().lower()
    overrides = {k: parse_override(v) for k, v in settings.overrides_for(domain).items()} if domain else {}
    acct = {**account, **overrides}
    facts = {**latest_facts(events), **overrides}

    matches = [m for s in settings.active_signals() if (m := match_signal(s, events, today, overrides=overrides))]
    score = total_score(matches, settings.general.score_cap)
    exclusion = tiers.hard_exclusion(acct, facts, settings, today)
    partner = tiers.partner_category(acct, facts)
    tier, reason = tiers.tier(score, matches, exclusion, settings)
    choice = angles.choose_angle(tier, matches, settings, acct.get("industry_group"))
    suppress = [m for m in matches if m.signal.action == "Suppress"]
    longest = max(suppress, key=lambda m: m.signal.counts_for_days) if suppress else None
    return ScoreResult(
        score=score,
        matches=matches,
        tier=tier,
        tier_reason=reason,
        angle=choice.angle,
        opener=choice.opener,
        suppress_days=longest.signal.counts_for_days if longest else None,
        legal_overlay=choice.legal_overlay,
        suppress_signal=longest.signal.signal if longest else None,
        exclusion=exclusion,
        partner=partner,
    )


# -- the score job ---------------------------------------------------------------------


def _chunks(items: list[str], n: int = ID_CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _match_row(account_id: str, m: Match, now: datetime) -> dict:
    first = m.evidence[0] if m.evidence else None
    return {
        "event_id": new_id(),
        "account_id": account_id,
        "source": SCORING_SOURCE,
        "fact": MATCH_FACT,
        "value": {"signal": m.signal.signal, "weight": m.weight_applied, "evidence": [ev.text for ev in m.evidence]},
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
            old_expiry = _ts(old.get("expires_at"))
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


def _in_month(v: Any, today: date) -> bool:
    t = _ts(v)
    if t is None:
        return False
    d = t.astimezone(UK).date()
    return (d.year, d.month) == (today.year, today.month)


def tier_share(accounts: Iterable[Mapping[str, Any]], today: date) -> dict[str, Any] | None:
    """Each tier's share of this month's queue; None when there are too few accounts to judge.

    The month's queue: accounts first seen this calendar month (UK), status queued or
    verified, tier Priority, Standard or Control.
    """
    queue = [
        a for a in accounts
        if a.get("status") in QUEUE_STATUSES and a.get("tier") in SHARE_TIERS and _in_month(a.get("first_seen"), today)
    ]
    if len(queue) < SHARE_MIN_ACCOUNTS:
        return None
    counts = Counter(a["tier"] for a in queue)
    shares = {t: counts.get(t, 0) / len(queue) for t in SHARE_TIERS}
    off = {t: s for t, s in shares.items() if not SHARE_MIN <= s <= SHARE_MAX}
    return {"accounts": len(queue), "shares": shares, "off": off}


def _share_alert(ctx: Context, check: dict[str, Any], today: date) -> None:
    mix = ", ".join(f"{t} {s:.0%}" for t, s in check["shares"].items())
    off = ", ".join(
        f"{t} is {'under 5%' if s < SHARE_MIN else 'over 40%'}" for t, s in check["off"].items()
    )
    text = (
        f"Tier mix check: this month's queue ({today:%B %Y}, {check['accounts']} accounts) is {mix}. "
        f"Each tier should be 5–40% of the queue; {off}. "
        "Review the thresholds and signal weights in the settings sheet."
    )
    ctx.clients.slack.post(ctx.settings.general.alert_channel, text)


def rescore(ctx: Context) -> dict:
    """The score job (SPEC 9): score, tier and angle every open account; returns counts per tier."""
    settings, store, now = ctx.settings, ctx.store, ctx.now
    today = ctx.today_uk()

    accounts = store.select("accounts", {"status": list(SCORED_STATUSES)})
    ids = [a["account_id"] for a in accounts]
    events: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks(ids):
        for e in store.select("signal_events", {"account_id": chunk}):
            events[e["account_id"]].append(e)

    updates: list[dict] = []
    match_rows: list[dict] = []
    suppress: dict[str, tuple[str, int]] = {}
    partners: dict[str, dict] = {}
    tier_counts: Counter[str] = Counter()
    changed = 0
    scored: list[dict] = []
    for a in accounts:
        aid = a["account_id"]
        r = score_account(a, events.get(aid, []), settings, today)
        tier_counts[r.tier] += 1
        new: dict[str, Any] = {"score": r.score, "tier": r.tier, "tier_reason": r.tier_reason}
        if a.get("status") not in ANGLE_FIXED_STATUSES:
            new["angle"] = r.angle
        diff = {k: v for k, v in new.items() if a.get(k) != v}
        if diff:
            changed += 1
            if a.get("status") in ANGLE_FIXED_STATUSES and "tier" in diff:
                log("score_enrolled_tier_change", account_id=aid, old=a.get("tier"), new=r.tier)
        updates.append({"account_id": aid, **diff, "last_scored": now})
        scored.append({**a, **diff})
        match_rows.extend(_match_row(aid, m, now) for m in r.matches)
        domain = str(a.get("domain") or "").strip().lower()
        if domain and r.suppress_signal and r.suppress_days:
            suppress[domain] = (r.suppress_signal, r.suppress_days)
        if domain and r.partner:
            partners[domain] = {
                "domain": domain,
                "name": a.get("clean_name") or a.get("legal_name") or domain,
                "reason": r.partner,
                "naics": ", ".join(tiers.naics_codes(a.get("naics"))) or None,
                "added_at": now,
            }

    if updates:
        store.upsert("accounts", updates)  # partial rows: upsert leaves absent columns alone
    for chunk in _chunks(ids):
        store.delete("signal_events", {"account_id": chunk, "source": SCORING_SOURCE, "fact": MATCH_FACT})
    if match_rows:
        store.insert("signal_events", match_rows)
    n_suppressed = _apply_suppression(ctx, suppress)
    n_partners = _add_partners(ctx, partners)

    check = tier_share(scored, today)
    if check and check["off"]:
        _share_alert(ctx, check, today)

    summary = {
        "accounts": len(accounts),
        "tiers": {t: tier_counts.get(t, 0) for t in (*SHARE_TIERS, tiers.HELD, tiers.EXCLUDED)},
        "changed": changed,
        "signal_matches": len(match_rows),
        "suppressed": n_suppressed,
        "partners": n_partners,
        "tier_share": None if check is None else {t: round(s, 3) for t, s in check["shares"].items()},
        "tier_share_alert": bool(check and check["off"]),
    }
    log("score_done", run_id=ctx.run_id, dry_run=ctx.dry_run, **summary)
    return summary


run = rescore  # JOB CONTRACT: run(ctx) -> dict
