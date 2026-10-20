"""The industry labels' numbers in the daily post and the Monday readout (W3-2, 20 Oct 2026: moved out of labels.py,
whose measurement half it was, and rewritten on the label sample and the hand-check's judgements).

  * The daily post's Labels block (post_lines) and its asks (asks, posted by learn/spend.asks): the label check's
    work since the last send day, each company once by its latest check: its decisions, and how often the model put
    the company where the Industries rules did (StoredVerdict.rules against the model's label: agreement, as a
    measure of the rules' sourcing, not of the labels' accuracy); the corrections made by hand; and whether the model
    could be asked at the last verify run.
  * The Monday readout's Industry labels block (readout_lines): last week's check and corrections as above; then
    how often the labels are right, which only a person can say: the label sample (ops/label_sample.py; T3) as the
    newest label_policy fact keeps it (labels.store_policy, monthly), exact and group with Wilson intervals, the
    emails sent a label's own copy apart (October: 4 of 30 pitched the wrong label, RT §3 L4), and the rater's
    agreement with himself (T1r); the label policy, what it routes and whether it is applied (General
    copy_level_policy, Harry's D20); and the hand-check's wrong-industry rows, pooled over learn/precision's weeks.
The cards' "N of N right" went with the send approvals (W2-1; design-final §4.4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from us_outbound import facts, fmt, labels
from us_outbound.context import Context
from us_outbound.learn import stats
from us_outbound.settings.model import LABEL_CHECK_REQUIRED
from us_outbound.timeparse import utc_or_epoch

# Corrections in a day that ask for a look at the Industries tab.
ASK_CORRECTIONS = 3
VERIFY_JOB = "verify_accounts"


@dataclass
class Tally:
    """The label check's numbers over a period: the model's checks, their decisions, and the corrections by hand."""

    checked: int = 0  # companies the model was asked about, each once (its latest check in the period)
    decisions: dict[str, int] = field(default_factory=dict)  # source, or held / disqualified
    labelled: int = 0  # checked companies the rules gave a label: what agreement is measured over
    same_group: int = 0  # of those, the model's label in the rules' group (the same label, or one within the group)
    same_label: int = 0  # of those, the model's label the rules' own
    disagreed_groups: dict[str, int] = field(default_factory=dict)  # the rules' group, where the model's group differed
    no_rules: dict[str, str] = field(default_factory=dict)  # account_id -> the model's label, where the rules had none
    corrections: list[tuple[str, str]] = field(default_factory=list)  # (from, to)

    def share_same_label(self) -> float | None:
        """Of the companies the rules labelled, the share the model gave the same label (from the stored verdicts:
        StoredVerdict.rules against the model's label; before W3-2 it counted the rules+model decisions, which the
        decision rule has not made since 11 Oct 2026). A company the rules gave no label cannot agree; it is counted
        in no_rules instead (9 Oct 2026)."""
        return self.same_label / self.labelled if self.labelled else None

    def share_same_group(self) -> float | None:
        """Of the companies the rules labelled, the share the model placed in the rules' group (9 Oct 2026: the
        rules give a group's own label when the codes alone place a company, and the model names the label within it)."""
        return self.same_group / self.labelled if self.labelled else None


def _between(rows: Iterable[Mapping[str, Any]], key: str, start: datetime | None, end: datetime) -> list[Mapping]:
    return [r for r in rows if (start is None or utc_or_epoch(r.get(key)) >= start) and utc_or_epoch(r.get(key)) < end]


def tally(ctx: Context, start: datetime | None, end: datetime) -> Tally:
    """The companies checked and the corrections made by hand in [start, end) (start None: ever). A company asked
    more than once in the period (an audit, then a second look) counts once, by its latest check."""
    t = Tally()
    decisions: dict[str, int] = {}
    groups: dict[str, int] = {}
    rows = _between(ctx.store.select("signal_events", {"source": labels.JOB,
                                                       "fact": [labels.VERDICT_FACT, labels.CORRECTED_FACT]}),
                    "observed_at", start, end)
    for e in rows:
        if e.get("fact") == labels.CORRECTED_FACT:
            v = e.get("value") or {}
            t.corrections.append((str(v.get("from") or "no label"), str(v.get("to") or "")))
    latest = facts.newest_by(rows, lambda e: str(e.get("account_id") or e.get("event_id")), labels.VERDICT_FACT,
                             where=lambda e: bool((e.get("value") or {}).get("asked")))
    for aid, e in latest.items():
        sv = labels.StoredVerdict.from_value(e["value"])
        t.checked += 1
        decisions[sv.outcome] = decisions.get(sv.outcome, 0) + 1
        if not sv.rules:
            t.no_rules[aid] = sv.verdict.label
            continue
        t.labelled += 1
        t.same_label += sv.rules == sv.verdict.label
        if sv.same_group(ctx.settings):
            t.same_group += 1
        else:
            group = sv.rules_group or sv.rules
            groups[group] = groups.get(group, 0) + 1
    t.decisions, t.disagreed_groups = decisions, groups
    return t


def last_verify(ctx: Context) -> dict:
    """The newest verify_accounts run's labels entry (its heartbeat), or {}."""
    rows = [r for r in ctx.store.select("heartbeats", {"job": VERIFY_JOB}) if isinstance(r.get("detail"), Mapping)]
    if not rows:
        return {}
    newest = max(rows, key=lambda r: utc_or_epoch(r.get("started_at")))
    return dict(newest["detail"].get("labels") or {})


_pct = partial(fmt.pct, places=0, empty="n/a")


def _moves(pairs: Sequence[tuple[str, str]], n: int = 5) -> str:
    counted: dict[str, int] = {}
    for a, b in pairs:
        counted[f"{a} → {b}"] = counted.get(f"{a} → {b}", 0) + 1
    ranked = sorted(counted.items(), key=lambda kv: -kv[1])[:n]
    return "; ".join(m + (f" ({k})" if k > 1 else "") for m, k in ranked)


def post_lines(ctx: Context, start: datetime, end: datetime) -> list[str]:
    """The daily post's Labels block: the checks and their decisions in the period, the latest verify run's unchecked
    companies (and why, when the model could not be asked), and the industry corrections made by hand."""
    t, run = tally(ctx, start, end), last_verify(ctx)
    d = t.decisions
    unchecked = int(run.get("unchecked") or 0)
    lines = [f"  Checked: {t.checked} (of the {t.labelled} the rules labelled, the model agreed on the label for "
             f"{t.same_label} and the group for {t.same_group}, {_pct(t.same_group, t.labelled)}; no rules label "
             f"{len(t.no_rules)}; model overruled {d.get(labels.MODEL, 0)}; "
             f"group copy {d.get(labels.UMBRELLA, 0)}; General copy {d.get(labels.DISPUTED, 0)}; held "
             f"{d.get('held', 0)}; disqualified {d.get('disqualified', 0)}; waiting unchecked {unchecked})."]
    if run.get("unavailable_reason"):
        lines.append(f"  The label check could not ask the model at the last verify run: {run['unavailable_reason']}.")
    if t.corrections:
        lines.append(f"  Corrected by hand: {len(t.corrections)} ({_moves(t.corrections)}).")
    return lines


def asks(ctx: Context) -> list[tuple[str, str]]:
    """The daily post's label asks (learn/spend.asks posts them, each key once): many corrections, and checks that
    could not run. Each says what happened in plain words, then what to do and where."""
    from us_outbound.base import say
    from us_outbound.learn.daily_post import period

    start, end, _ = period(ctx)
    t, run, day = tally(ctx, start, end), last_verify(ctx), ctx.today_uk().isoformat()
    out: list[tuple[str, str]] = []

    def tab() -> str:
        return say.sheet_link(ctx, "Industries")

    ai = "the AI check (Claude)"
    n = len(t.corrections)
    if n >= ASK_CORRECTIONS:
        out.append((f"labels_corrections:{day}",
                    f"{say.ACTION} *Industry labels: the industry of {n} companies was corrected by hand since the "
                    "last send day.*\n"
                    f"Corrections: {_moves(t.corrections)}.\n"
                    f"*What to do:* check those industries' definitions on the {tab()}: a sharper line fixes the "
                    "next ones. A changed definition has every company checked again by itself; nothing to run."))
    # The rules' disagreement with the model, and the companies they gave no label, are not asked about (Harry,
    # 12 Oct 2026): the model's label decides the email and the rules only source companies.
    waiting = int(run.get("unchecked") or 0)
    if run.get("unavailable_reason") and waiting:
        companies = f"{waiting} new compan{'y waits' if waiting == 1 else 'ies wait'}"
        held = (" While `label_check` is `required` (General tab), a company is not emailed until it is checked."
                if ctx.settings.general.label_check == LABEL_CHECK_REQUIRED else "")
        out.append((f"labels_unavailable:{day}",
                    f"{say.URGENT} *Industry labels: {ai} could not run, so {companies} unchecked.*\n"
                    f"Why: {run['unavailable_reason']}.{held} Companies already checked carry on as before.\n"
                    "*What to do:* fix the cause above; the next company check (weekdays 04:30 UK) tries again."))
    return out


def _ci(k: int, n: int) -> str:
    lo, hi = stats.wilson(k, n)
    return f"{k} of {n} ({k / n:.0%}, 95% {lo:.0%}–{hi:.0%})" if n else "none judged"


def sample_lines(stored: Mapping[str, Any] | None, applied: bool) -> list[str]:
    """The label sample's score (T3), the rater's agreement (T1r) and the label policy, from a label_policy fact's
    value (labels.store_policy), for the readout."""
    if not stored:
        return ["  Judged by hand (the monthly label sample): no batch scored yet."]
    sample = stored.get("sample") or {}
    n = int(sample.get("judged") or 0)
    batches = ", ".join(stored.get("batches") or ()) or "none"
    if not n:
        return [f"  Judged by hand (the monthly label sample, {batches}): none judged yet."]
    lc = sample.get("label_copy") or {}
    out = [f"  Judged by hand (the monthly label sample, {batches}): label exactly right "
           f"{_ci(int(sample.get('label_right') or 0), n)}; group right {_ci(int(sample.get('group_right') or 0), n)}."]
    if lc.get("judged"):
        out.append(f"  Emails sent a label's own copy: {lc.get('wrong_pitch', 0)} of {lc['judged']} pitched the wrong "
                   f"label ({_ci(int(lc.get('wrong_pitch') or 0), int(lc['judged']))}).")
    if sample.get("rejudged"):
        out.append(f"  Judged twice: the second verdict matched the first for {sample.get('rejudge_agreed', 0)} of "
                   f"{sample['rejudged']} (the rater's own drift).")
    from us_outbound.ops.label_sample import policy_line

    routing = stored.get("policy") or {}
    group = sorted(s for s, level in routing.items() if level == labels.GROUP_COPY and labels.COPY_LEVEL.get(s)
                   == labels.LABEL_COPY)
    would = (f"with it, {', '.join(group)} labels get their group's copy" if group
             else "it changes no source's copy")
    state = (f"applied (General copy_level_policy is yes): {would}" if applied
             else f"not applied (General copy_level_policy is no, Harry's D20): {would} when it is")
    out.append(f"  Label policy: {policy_line(stored)}; {state}.")
    return out


def readout_lines(ctx: Context, start: datetime, end: datetime, wrong_industry: tuple[int, int] | None = None
                  ) -> list[str]:
    """The Monday readout's Industry labels block (the module docstring). wrong_industry: the hand-check's
    (wrong-industry rows, random rows judged) pooled (learn/precision.tally), when the readout has it."""
    week = tally(ctx, start, end)
    head = "*Industry labels* (the AI check of each new company, the corrections made by hand, and how often a person " \
           "finds the label right)"
    lines = ["", head]
    if week.corrections:
        lines.append(f"  Corrected last week: {_moves(week.corrections, 10)}")
    if week.checked:
        d = week.decisions
        no_rules = f" ({len(week.no_rules)} had no rules label)" if week.no_rules else ""
        lines.append(f"  Checked last week by the AI model against our industry-code rules: {week.checked} · agreeing "
                     f"on the group for {_pct(week.same_group, week.labelled)}{no_rules} · held for the hand-check "
                     f"{d.get('held', 0)} · disqualified {d.get('disqualified', 0)}")
    elif not week.corrections:
        lines.append("  No company checked or corrected last week.")
    lines += sample_lines(labels.stored_policy(ctx), bool(ctx.settings.general.copy_level_policy))
    if wrong_industry is not None and wrong_industry[1]:
        k, n = wrong_industry
        lines.append(f"  The hand-check found the wrong industry on {_ci(k, n)} random rows.")
    return lines
