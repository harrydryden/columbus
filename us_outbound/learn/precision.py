"""ICP precision from the weekly hand-check, and the ICP rules it proposes (W3-2, 20 Oct 2026; design-final T1, T2, L3).

The hand-check (enrol/hand_check.py; Harry's D5) is the only human judgement of whether the companies about to be
emailed are the right ones, and until now nothing read it (data-slack §7). Each judged row is a judgement fact
(signal_events: source hand_check, fact judgement; hand_check_tab.judgement), with its decision, the leave-out
reason, whether the contact and the opening line were right, the gate version the company passed and a snapshot of
the company as it stood.

tally(ctx, weeks=4): the random-sample rows of the last `weeks` ISO weeks pooled (the doubtful-facts rows are not a
random sample, so they are left out): T1, judged fine of judged, with its Wilson 95% interval; the decisions; T2, the
leave-out reasons; the contact and opener answers; T1 by gate version (labels_hash:icp_rules_hash), so a change to
the gate can be told from a change in the pool (RT §1.5); and splits by group, size band, state, slice, score decile,
arm and label source, each marked printable only when it has two cells or more and every cell n >= MIN_CELL (15),
else the readout says "pooled only" (RT §1.5: 80 rows over 12 cells is noise). by_score_quartile, the leave-outs in
each quarter of the score, only from QUARTILE_FROM (20) leave-outs: below that the quarters mean nothing (RT §1.5); the
weights are flagged when the bottom quarter holds no more than its share of them.

propose(ctx, answer_key), monthly (label_sample_monthly): each leave-out reason with at least PROPOSE_AT (3) rows in
the pool that maps to a rule (RULES) becomes one row on the Suggestions tab (settings/definitions.py), scored two
ways before Harry sees it (RT §3 L3: a rule scored on the label answer key alone says nothing of its precision):
  * on the judgement facts: the leave-outs with that reason the rule would catch, and the fine rows it would wrongly
    exclude. A rule is read as the label check would read it, from the company's Apollo facts and home page: a row
    in the rule's labels (all rows, for an entity rule) whose NAICS codes start with one of the rule's prefixes or
    whose material says one of its words. A rule that catches none, or excludes a fine row, is worse;
  * on the label answer key (the gold rows and every company judged in the label sample) with the production-
    faithful eval (labels.evaluate), with the definitions as they are and with the rule in them, for a rule that
    changes what the label check reads (definition_not, exclude_naics). Fewer acceptable or more unsafe is worse. A
    pass cut short scores nothing, and the row is not written (definition_suggestions.incomplete). Live only: a dry
    run says "not run".
A rule worse either way is written with status "rejected by eval"; otherwise "proposed". Nothing is applied until
Harry sets status to approved (settings/definitions._apply_suggestions). entity_text rules change the prompt's
entity rule, which engineering makes: they carry the facts' score only. The evidence cell says "catches k of K
leave-outs; excludes f of F fine rows; answer key a → b of N right", and the snapshot cell the pool it rests on (n
judged, k with the reason, its interval, the weeks, labels_hash and icp_rules_hash), so each proposal can be audited.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from us_outbound import facts, icp, labels
from us_outbound.context import Context
from us_outbound.enrol import hand_check
from us_outbound.enrol.enrol import iso_week
from us_outbound.industry.material import naics_codes, texts
from us_outbound.learn import stats
from us_outbound.logs import log
from us_outbound.settings import definitions
from us_outbound.settings.definitions import (
    DEFINITION_NOT,
    ENTITY_TEXT,
    EXCLUDE_NAICS,
    PROPOSED,
    REJECTED_BY_EVAL,
)
from us_outbound.settings.model import Settings
from us_outbound.sources.apollo_universe import JOB as UNIVERSE_JOB
from us_outbound.sources.apollo_universe import SOURCE as APOLLO_SOURCE
from us_outbound.sources.apollo_universe import SOURCED_BY

SUGGESTIONS_TAB = definitions.SUGGESTIONS_TAB
WEEKS = 4  # T1 is pooled over four ISO weeks (design-final §4.2)
MIN_CELL = 15  # a split prints only when every cell has this many (RT §1.5)
QUARTILE_FROM = 20  # leave-outs before the score quartiles are computed (RT §1.5)
TARGET, TARGET_ROWS = 0.90, 150  # T1's target, and about the rows needed to show it (design-final §2.3)
PROPOSE_AT = 3  # rows with one reason that make a proposal (design-final L3)
SECONDS = 600.0  # propose's answer-key passes together (label_sample_monthly's timeout is 30 minutes)
_clock = time.monotonic  # the deadline's clock (a test sets it)
SPLITS = ("group", "size band", "state", "slice", "score decile", "arm", "label source")
NOT_APPLICABLE = "answer key: not applicable (a prompt change engineering makes)"


@dataclass(frozen=True)
class Rule:
    """What a leave-out reason proposes, and how the rule is read against a company (module docstring)."""

    reason: str
    kind: str
    text: str  # the "not X" words, or the entity rule's text
    naics: tuple[str, ...] = ()  # code prefixes the rule leaves out
    words: tuple[str, ...] = ()  # words in the material that mark a company as X
    target: str = ""  # a fixed target label; "" for definition_not: the labels its rows carried


# The reasons the hand-check's reason dropdown offers (hand_check_tab.REASONS) that map to a rule. wrong_size and
# wrong_state are the gate's own facts (the Overrides tab fixes a wrong one), and other names no rule.
RULES: dict[str, Rule] = {
    "media_publisher": Rule("media_publisher", DEFINITION_NOT, "not a media company or publication",
                            naics=("5111", "5131", "516", "51912"),
                            words=("media company", "publisher", "publishing", "publication", "newspaper",
                                   "magazine", "broadcast")),
    "it_services_outsourcing": Rule("it_services_outsourcing", EXCLUDE_NAICS, "not IT services or outsourcing",
                                    naics=("5415",), target="Technology & Startups",
                                    words=("it services", "outsourcing", "managed services", "it consulting",
                                           "staff augmentation")),
    "subsidiary": Rule("subsidiary", ENTITY_TEXT,
                       "a subsidiary, division or brand of a larger company is other, not company",
                       words=("subsidiary", "a division of", "owned by", "a brand of", "part of the")),
    "not_operating_business": Rule("not_operating_business", ENTITY_TEXT,
                                   "a holding company, fund, shell or inactive business, or a personal site is "
                                   "other, not company",
                                   words=("holding company", "shell company", "inactive", "dormant",
                                          "investment fund", "personal blog")),
}


# -- the tally (T1, T2) --------------------------------------------------------------------------------------------


def weeks_back(through: str, n: int = WEEKS) -> tuple[str, ...]:
    """The n ISO weeks ending with `through` ("2026-W43", 4 -> W40 to W43), oldest first."""
    monday = hand_check.week_monday(through)
    if monday is None:
        return ()
    return tuple(iso_week(monday - timedelta(weeks=i)) for i in reversed(range(n)))


@dataclass(frozen=True)
class Split:
    name: str
    cells: dict[str, tuple[int, int]]  # cell -> (fine, judged)
    printable: bool


@dataclass
class Tally:
    weeks: tuple[str, ...] = ()
    fine: tuple[int, int] = (0, 0)  # (judged fine, judged): T1
    leave_out: int = 0
    wrong_industry: int = 0
    reasons: dict[str, int] = field(default_factory=dict)  # T2: leave-outs by reason ("" for none given)
    contact_ok: tuple[int, int] = (0, 0)  # (yes, answered)
    opener_ok: tuple[int, int] = (0, 0)
    by_gate: dict[str, tuple[int, int]] = field(default_factory=dict)
    splits: dict[str, Split] = field(default_factory=dict)
    by_score_quartile: dict[str, tuple[int, int]] | None = None  # quarter -> (leave-outs, rows), from QUARTILE_FROM
    weights_flag: bool | None = None  # the bottom quarter holds no more than its share of leave-outs
    rows: list[dict] = field(default_factory=list)  # the judgement values pooled, each with its account_id

    @property
    def interval(self) -> tuple[float, float]:
        return stats.wilson(*self.fine)


def _sample(ctx: Context, weeks: Sequence[str]) -> list[dict]:
    """The random-sample judgement values of these weeks with a decision, each with its account_id."""
    want = set(weeks)
    out = []
    for e in ctx.store.select("signal_events", {"source": hand_check.SOURCE, "fact": hand_check.FACT}):
        v = e.get("value")
        if (isinstance(v, Mapping) and str(v.get("week")) in want and v.get("check") == hand_check.SAMPLE
                and v.get("decision") in hand_check.DECISIONS):
            out.append({**v, "account_id": str(e.get("account_id") or "")})
    return sorted(out, key=lambda v: (str(v.get("week")), v["account_id"]))


def _decile(score: Any) -> str:
    try:
        s = max(0, min(int(score), 100))
    except (TypeError, ValueError):
        return "no score"
    low = min(s // 10 * 10, 90)
    return f"{low}-{low + 9 if low < 90 else 100}"


def _cells(ctx: Context, rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Each split's cell for each row (in rows' order): the snapshot's group, size band, state, score and label
    source; the slice from the account's sourced_by fact; the arm from the account."""
    ids = sorted({r["account_id"] for r in rows if r.get("account_id")})
    accounts = {str(a["account_id"]): a for a in ctx.store.select("accounts", {"account_id": ids})} if ids else {}
    slices = facts.newest_by(ctx.store.select("signal_events", {"account_id": ids, "source": UNIVERSE_JOB,
                                                                 "fact": SOURCED_BY}) if ids else [],
                             lambda e: str(e.get("account_id")))
    out: dict[str, list[str]] = {s: [] for s in SPLITS}
    for r in rows:
        snap = r.get("snapshot") if isinstance(r.get("snapshot"), Mapping) else {}
        a = accounts.get(r["account_id"]) or {}
        sliced = (slices.get(r["account_id"]) or {}).get("value")
        out["group"].append(str(snap.get("industry_group") or "unknown"))
        out["size band"].append(str(snap.get("size_band") or "unknown"))
        out["state"].append(str(snap.get("hq_state") or "unknown"))
        out["slice"].append(str(sliced.get("slice") if isinstance(sliced, Mapping) else "") or "unknown")
        out["score decile"].append(_decile(snap.get("score")))
        out["arm"].append(str(a.get("arm") or "unknown"))
        out["label source"].append(str(snap.get("label_source") or "not checked"))
    return out


def tally(ctx: Context, weeks: int = WEEKS, through: str | None = None) -> Tally:
    """T1 and T2 pooled over the `weeks` ISO weeks ending with `through` (default: this week, hand_check.this_week),
    from the judgement facts (the module docstring)."""
    span = weeks_back(through or hand_check.this_week(ctx), weeks)
    rows = _sample(ctx, span)
    t = Tally(weeks=span, rows=rows)
    if not rows:
        return t
    decisions = Counter(str(r.get("decision")) for r in rows)
    t.fine = (decisions[hand_check.FINE], len(rows))
    t.leave_out, t.wrong_industry = decisions[hand_check.LEAVE_OUT], decisions[hand_check.WRONG_INDUSTRY]
    t.reasons = dict(Counter(str(r.get("reason") or "") for r in rows if r.get("decision") == hand_check.LEAVE_OUT))
    for name in ("contact_ok", "opener_ok"):
        answered = [r.get(name) for r in rows if r.get(name) in ("yes", "no")]
        setattr(t, name, (answered.count("yes"), len(answered)))
    gates: dict[str, list[int]] = {}
    for r in rows:
        cell = gates.setdefault(str(r.get("gate_version") or "unknown"), [0, 0])
        cell[0] += r.get("decision") == hand_check.FINE
        cell[1] += 1
    t.by_gate = {k: (v[0], v[1]) for k, v in gates.items()}
    for name, cells in _cells(ctx, rows).items():
        counted: dict[str, list[int]] = {}
        for r, cell in zip(rows, cells, strict=True):
            c = counted.setdefault(cell, [0, 0])
            c[0] += r.get("decision") == hand_check.FINE
            c[1] += 1
        got = {k: (v[0], v[1]) for k, v in sorted(counted.items())}
        t.splits[name] = Split(name, got, len(got) >= 2 and stats.min_n_ok({k: n for k, (_, n) in got.items()},
                                                                             MIN_CELL))
    if t.leave_out >= QUARTILE_FROM:
        ranked = sorted(rows, key=lambda r: _score(r))
        quarters: dict[str, list[int]] = {f"Q{i + 1}": [0, 0] for i in range(4)}
        for i, r in enumerate(ranked):
            q = quarters[f"Q{i * 4 // len(ranked) + 1}"]
            q[0] += r.get("decision") == hand_check.LEAVE_OUT
            q[1] += 1
        t.by_score_quartile = {k: (v[0], v[1]) for k, v in quarters.items()}
        t.weights_flag = t.by_score_quartile["Q1"][0] * 4 <= t.leave_out
    return t


def _score(r: Mapping[str, Any]) -> int:
    snap = r.get("snapshot") if isinstance(r.get("snapshot"), Mapping) else {}
    try:
        return int(snap.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def _pct(k: int, n: int) -> str:
    return f"{k / n:.0%}" if n else "n/a"


def _ci(k: int, n: int) -> str:
    lo, hi = stats.wilson(k, n)
    return f"{k} of {n} ({_pct(k, n)}, 95% {lo:.0%}–{hi:.0%})"


def readout_lines(ctx: Context, t: Tally | None = None) -> list[str]:
    """The Monday readout's ICP precision block: T1 pooled with its interval, by gate version; T2's reasons; the
    contact and opener answers; each split printed only when printable, else named as pooled only; the score
    quarters only from QUARTILE_FROM leave-outs. Counts and intervals only, no verdict (design-final §9)."""
    t = t if t is not None else tally(ctx)
    head = ["", f"*ICP precision* (the hand-check's random rows judged fine, pooled over {len(t.weeks)} weeks, "
                f"{t.weeks[0] if t.weeks else ''} to {t.weeks[-1] if t.weeks else ''})"]
    k, n = t.fine
    if not n:
        return [*head, "  No random row judged in these weeks."]
    out = [*head, f"  Fine: {_ci(k, n)}; left out {t.leave_out}, wrong industry {t.wrong_industry}. The 0.90 "
                  f"target needs about {TARGET_ROWS} rows to show; {n} so far."]
    if t.reasons:
        out.append("  Left out because: " + ", ".join(f"{r or 'no reason given'} {c}" for r, c in
                                                      sorted(t.reasons.items(), key=lambda kv: (-kv[1], kv[0]))) + ".")
    if len(t.by_gate) > 1:
        out.append("  By gate version: " + "; ".join(f"{g} {_ci(*v)}" for g, v in sorted(t.by_gate.items())) + ".")
    answers = [f"{label} right {y} of {a}" for label, (y, a) in (("contact", t.contact_ok), ("opening line",
                                                                                            t.opener_ok)) if a]
    if answers:
        out.append("  " + "; ".join(answers) + ".")
    printed = [s for s in t.splits.values() if s.printable]
    for s in printed:
        out.append(f"  By {s.name}: " + "; ".join(f"{c} {_ci(*v)}" for c, v in s.cells.items()) + ".")
    pooled = [s.name for s in t.splits.values() if not s.printable]
    if pooled:
        out.append(f"  Pooled only (a cell under {MIN_CELL} rows): {', '.join(pooled)}.")
    if t.by_score_quartile is None:
        out.append(f"  Score quarters: from {QUARTILE_FROM} leave-outs ({t.leave_out} so far).")
    else:
        q = "; ".join(f"{k_} {v[0]} of {v[1]}" for k_, v in t.by_score_quartile.items())
        flag = " The lowest quarter holds no more than its share: the weights may not be finding them." \
            if t.weights_flag else ""
        out.append(f"  Leave-outs by score quarter (Q1 lowest): {q}.{flag}")
    return out


# -- the proposals (L3) ---------------------------------------------------------------------------------------------


def _material(ctx: Context, ids: Sequence[str]) -> dict[str, tuple[tuple[str, ...], str]]:
    """account id -> (its newest Apollo NAICS codes, its Apollo industry, keywords, description and home page text,
    lower case): what the label check reads of it."""
    if not ids:
        return {}
    events = ctx.store.select("signal_events", {"account_id": list(ids), "source": [APOLLO_SOURCE, "careers_pages"],
                                                "fact": [*labels.MATERIAL_FACTS, labels.HOME_FACT]})
    by: dict[str, list[dict]] = {}
    for e in events:
        by.setdefault(str(e["account_id"]), []).append(e)
    out = {}
    for aid in ids:
        evs = by.get(aid, [])
        fact = {f: facts.value(evs, f, APOLLO_SOURCE) for f in labels.MATERIAL_FACTS}
        page = facts.newest(evs, labels.HOME_FACT, where=lambda e: isinstance(e.get("value"), Mapping))
        home = " ".join(str(v) for v in (page["value"].values() if page else ()) if isinstance(v, str))
        text = " ".join([str(fact["apollo_industry"] or ""), "; ".join(texts(fact["keywords"])),
                         str(fact["description"] or ""), home])
        out[aid] = (tuple(naics_codes(fact["naics"])), " ".join(text.casefold().split()))
    return out


def catches(rule: Rule, targets: Sequence[str], row: Mapping[str, Any],
            material: tuple[tuple[str, ...], str]) -> bool:
    """Whether the rule would leave the row's company out: in the rule's labels (any, for an entity rule) and its
    NAICS codes start with a prefix of the rule's, or its material says one of its words."""
    snap = row.get("snapshot") if isinstance(row.get("snapshot"), Mapping) else {}
    if rule.kind != ENTITY_TEXT and str(snap.get("industry") or "") not in targets:
        return False
    codes, text = material
    return any(c.startswith(p) for c in codes for p in rule.naics) or any(w in text for w in rule.words)


def with_rule(settings: Settings, rule: Rule, targets: Sequence[str]) -> Settings:
    """The settings with the rule in the targets' Industries rows, as the sync would write it: the "not X" words in
    the definition and, for exclude_naics, the codes in exclude_naics."""
    def change(i):
        if i.industry not in targets:
            return i
        excl = tuple(dict.fromkeys([*i.exclude_naics, *rule.naics])) if rule.kind == EXCLUDE_NAICS else i.exclude_naics
        return dataclasses.replace(i, definition=definitions._with_not(i.definition, rule.text), exclude_naics=excl)

    return dataclasses.replace(settings, industries=tuple(change(i) for i in settings.industries))


def _right(out: Mapping[str, Any]) -> int:
    return int(out.get(labels.ACCEPTABLE) or 0)


def propose(ctx: Context, answer_key: Sequence[Mapping[str, Any]] = (), *, at: int = PROPOSE_AT,
            weeks: int = WEEKS) -> dict[str, Any]:
    """This month's proposals (the module docstring): the rows, scored, written to the Suggestions tab (live)."""
    from us_outbound.learn import definition_suggestions

    t = tally(ctx, weeks)
    n = t.fine[1]
    month = ctx.today_uk().strftime("%Y-%m")
    out: dict[str, Any] = {"rows": [], "written": False, "error": "", "skipped": [], "pooled": n}
    leave = [r for r in t.rows if r.get("decision") == hand_check.LEAVE_OUT]
    fine = [r for r in t.rows if r.get("decision") == hand_check.FINE]
    material = _material(ctx, sorted({r["account_id"] for r in t.rows}))
    deadline = _clock() + SECONDS
    baseline: dict[str, Any] | None = None
    version = {"labels_hash": labels.labels_hash(ctx.settings), "icp_rules_hash": icp.rules_hash(ctx.settings)}
    for reason, k in sorted(t.reasons.items(), key=lambda kv: (-kv[1], kv[0])):
        rule = RULES.get(reason)
        if rule is None or k < at:
            continue
        mine = [r for r in leave if r.get("reason") == reason]
        if rule.kind == ENTITY_TEXT:
            targets: list[str] = []
            target = "entity()"
        else:
            targets = [rule.target] if rule.target else sorted({str((r.get("snapshot") or {}).get("industry") or "")
                                                                for r in mine} - {""})
            target = "; ".join(targets)
        if rule.kind != ENTITY_TEXT and not targets:
            out["skipped"].append(f"{reason}: no label to put the rule on")
            continue
        caught = sum(catches(rule, targets, r, material.get(r["account_id"], ((), ""))) for r in mine)
        wrongly = sum(catches(rule, targets, r, material.get(r["account_id"], ((), ""))) for r in fine)
        worse = caught == 0 or wrongly > 0
        evidence = f"catches {caught} of {len(mine)} leave-outs; excludes {wrongly} of {len(fine)} fine rows"
        if rule.kind == ENTITY_TEXT:
            evidence += f"; {NOT_APPLICABLE}"
        elif not ctx.live:
            evidence += "; answer key: not run (dry run)"
        elif worse:
            evidence += "; answer key: not run (worse on the hand-check already)"
        elif not answer_key:
            out["skipped"].append(f"{reason}: no answer key")
            continue
        else:
            left = max(0.0, deadline - _clock())
            if baseline is None:
                baseline = labels.evaluate(ctx, answer_key, seconds=left)
                left = max(0.0, deadline - _clock())
            later = labels.evaluate(dataclasses.replace(ctx, settings=with_rule(ctx.settings, rule, targets)),
                                    answer_key, seconds=left)
            short = definition_suggestions.incomplete(baseline, later, len(answer_key))
            if short is not None:
                out["skipped"].append(f"{reason}: the answer key was not checked in full ({short})")
                continue
            worse = worse or _right(later) < _right(baseline) \
                or int(later.get(labels.UNSAFE) or 0) > int(baseline.get(labels.UNSAFE) or 0)
            evidence += f"; answer key {_right(baseline)} → {_right(later)} of {len(answer_key)} right"
        lo, hi = stats.wilson(k, n)
        text = f"NAICS {', '.join(rule.naics)}; {rule.text}" if rule.kind == EXCLUDE_NAICS else rule.text
        snapshot = {"n": n, "k": k, "interval": [round(lo, 3), round(hi, 3)],
                    "weeks": [t.weeks[0], t.weeks[-1]] if t.weeks else [], **version}
        out["rows"].append({"id": f"{month}:{rule.kind}:{reason}", "kind": rule.kind, "target": target, "text": text,
                            "evidence": evidence, "snapshot": json.dumps(snapshot, sort_keys=True),
                            "status": REJECTED_BY_EVAL if worse else PROPOSED, "applied": ""})
    if out["skipped"] and not out["rows"]:
        out["error"] = "; ".join(out["skipped"])
    if out["rows"]:
        out["written"] = definition_suggestions.write_suggestions(ctx, out["rows"])["written"]
    log("icp_proposals", run_id=ctx.run_id, pooled=n, rows=len(out["rows"]), skipped=out["skipped"],
        written=out["written"])
    return out
