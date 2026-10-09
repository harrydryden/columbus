"""Which signals predict replies: the evidence for reweighting the Signals tab (Harry, 5 Oct 2026).

`us-outbound signals review` is read-only. For each active Score signal on the Signals tab it takes the
companies emailed (step 1 delivered) and splits them by whether they showed the signal when their contact
was enrolled (contacts.signals_at_enrol, written by enrol._record_enrolled; for a contact enrolled before
that column existed, the account's current matches). It compares the two groups' human-reply rates within
REPLY_WINDOW_DAYS of step 1 (28 days, as v_account_outcomes counts them) with a two-proportion test, and
gives each signal a verdict:
  raise     the companies with it replied more, and the difference is unlikely to be chance (p < P_VALUE);
  lower     they replied less, likewise;
  keep      no clear difference yet;
  too few   fewer than MIN_COMPANIES companies on either side.
It also shows the reply rate by tier at enrolment: the plainest test of whether scoring works at all is
that Priority replies more than Standard, and Standard more than Control. And one line for email 1's subject
split (Harry, 5 Oct 2026; render.subject_arm): the companies whose email 1 had the personal subject (General
email1_subject) against those with the Copy row's (contacts.subject_arm, recorded from 5 Oct 2026; a contact
enrolled before has none and is left out), with the same test and the same MIN_COMPANIES on each side.

Nothing changes by itself: Harry changes weights on the Signals tab (SPEC 12). Replies in windows that have
not closed yet are counted as they stand, so an early read (two or three weeks in) understates every rate a
little and the comparison stays fair; the header says how many windows have closed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from us_outbound.clients.instantly import REPLY_WINDOW_DAYS
from us_outbound.context import Context
from us_outbound.enrol.render import COPY_SUBJECT, PERSONAL_SUBJECT
from us_outbound.learn import kill_rules
from us_outbound.scoring.score import MATCH_FACT, SCORING_SOURCE
from us_outbound.timeparse import utc

MIN_COMPANIES = 30  # emailed companies on each side of a signal before a verdict
P_VALUE = 0.10  # two-sided; a pilot's samples are small, so this is a lead to follow, not proof
READY_DELIVERED = 60  # the daily post says the review is worth reading from this many emailed companies
READY_DAYS = 14  # ... and this many days after the first send
NOT_HUMAN = "out_of_office"
POSITIVE = frozenset({"positive", "referral"})
TIERS = ("Priority", "Standard", "Control")


@dataclass
class Company:
    account_id: str
    step1_at: datetime
    replied: bool = False
    positive: bool = False
    signals: frozenset[str] = frozenset()
    tier: str = ""
    subject_arm: str = ""  # personal or copy (contacts.subject_arm); "" before the split


@dataclass
class Verdict:
    signal: str
    weight: int
    with_n: int
    with_replied: int
    without_n: int
    without_replied: int
    p: float | None
    verdict: str

    @property
    def with_rate(self) -> float | None:
        return self.with_replied / self.with_n if self.with_n else None

    @property
    def without_rate(self) -> float | None:
        return self.without_replied / self.without_n if self.without_n else None


@dataclass
class Review:
    companies: list[Company] = field(default_factory=list)
    closed: int = 0
    first_send: datetime | None = None
    verdicts: list[Verdict] = field(default_factory=list)
    tiers: dict[str, tuple[int, int]] = field(default_factory=dict)  # tier -> (emailed, replied)
    subject: Verdict | None = None  # email 1's personal subject (with) against the Copy row's (without)


def emailed(ctx: Context) -> tuple[list[Company], int]:
    """Each company's first delivered step 1, whether a human replied within the window, and how many windows closed."""
    ev = kill_rules._Events(ctx.store, ctx.now)
    first: dict[str, kill_rules._Send] = {}
    for s in ev.sends:  # oldest first
        if s.step == 1 and s.account_id and s.account_id not in first:
            first[s.account_id] = s
    out = {aid: Company(aid, s.at) for aid, s in first.items() if not s.bounced}
    window = timedelta(days=REPLY_WINDOW_DAYS)
    for r in ev.replies:
        c = out.get(ev.account(r))
        t = utc(r.get("occurred_at"))
        if c is None or t is None or not c.step1_at <= t < c.step1_at + window:
            continue
        cls = str(r.get("reply_class") or "").strip().lower()
        if cls == NOT_HUMAN:
            continue
        c.replied = True
        c.positive = c.positive or cls in POSITIVE
    contact_of = {aid: s.contact_id for aid, s in first.items()}
    contacts = {c["contact_id"]: c for c in ctx.store.select("contacts", {"contact_id": [v for v in contact_of.values() if v]})}
    missing: list[str] = []
    for aid, c in out.items():
        row = contacts.get(contact_of.get(aid, ""), {})
        snap = row.get("signals_at_enrol")
        if isinstance(snap, list):
            c.signals = frozenset(str(m.get("signal")) for m in snap if isinstance(m, Mapping) and m.get("signal"))
        else:
            missing.append(aid)
        c.tier = str(row.get("tier_at_enrol") or "")
        c.subject_arm = str(row.get("subject_arm") or "").strip().lower()
    if missing:  # enrolled before the snapshot existed: today's matches are the best we have
        now: dict[str, set[str]] = {aid: set() for aid in missing}
        for e in ctx.store.select("signal_events", {"account_id": missing, "source": SCORING_SOURCE, "fact": MATCH_FACT}):
            v = e.get("value") if isinstance(e.get("value"), Mapping) else {}
            if v.get("signal"):
                now[str(e["account_id"])].add(str(v["signal"]))
        for aid, names in now.items():
            out[aid].signals = frozenset(names)
    if any(not c.tier for c in out.values()):
        tiers = {a["account_id"]: a.get("tier") for a in ctx.store.select("accounts", {"account_id": list(out)})}
        for c in out.values():
            c.tier = c.tier or str(tiers.get(c.account_id) or "")
    closed = sum(1 for c in out.values() if ctx.now >= c.step1_at + window)
    return sorted(out.values(), key=lambda c: (c.step1_at, c.account_id)), closed


def p_value(a_n: int, a_k: int, b_n: int, b_k: int) -> float | None:
    """Two-sided p of a pooled two-proportion z test; None when it cannot be computed."""
    if not a_n or not b_n:
        return None
    pooled = (a_k + b_k) / (a_n + b_n)
    se = math.sqrt(pooled * (1 - pooled) * (1 / a_n + 1 / b_n))
    if se == 0:
        return 1.0
    z = (a_k / a_n - b_k / b_n) / se
    return math.erfc(abs(z) / math.sqrt(2))


def compare(name: str, weight: int, with_: list[Company], without: list[Company]) -> Verdict:
    """The two groups' reply rates, the two-proportion test and the verdict (raise, lower, keep or too few)."""
    wk, ok = sum(c.replied for c in with_), sum(c.replied for c in without)
    p = p_value(len(with_), wk, len(without), ok)
    if len(with_) < MIN_COMPANIES or len(without) < MIN_COMPANIES or p is None:
        verdict = "too few"
    elif p < P_VALUE:
        verdict = "raise" if wk / len(with_) > ok / len(without) else "lower"
    else:
        verdict = "keep"
    return Verdict(name, weight, len(with_), wk, len(without), ok, p, verdict)


def review(ctx: Context) -> Review:
    companies, closed = emailed(ctx)
    out = Review(companies, closed, companies[0].step1_at if companies else None)
    for sig in ctx.settings.signals:
        if not sig.active or sig.action != "Score":
            continue
        out.verdicts.append(compare(sig.signal, sig.weight, [c for c in companies if sig.signal in c.signals],
                                    [c for c in companies if sig.signal not in c.signals]))
    for tier in TIERS:
        group = [c for c in companies if c.tier == tier]
        out.tiers[tier] = (len(group), sum(c.replied for c in group))
    out.subject = compare("email 1 subject", 0, [c for c in companies if c.subject_arm == PERSONAL_SUBJECT],
                          [c for c in companies if c.subject_arm == COPY_SUBJECT])
    return out


def _pct(k: int, n: int) -> str:
    return f"{k / n:.1%}" if n else "-"


# What a subject verdict means: "raise" is the personal subject replying more.
SUBJECT_VERDICTS = {
    "raise": "the personal subject replies more: keep it, or raise email1_subject_share",
    "lower": "the Copy row's subject replies more: lower email1_subject_share, or rewrite email1_subject",
    "keep": "no clear difference yet",
    "too few": f"too few to judge (under {MIN_COMPANIES} emailed companies in an arm)",
}


def subject_line(v: Verdict) -> str:
    """Email 1's subject split in one line: personal (General email1_subject) against the Copy row's."""
    p = f" · p = {v.p:.2f}" if v.p is not None else ""
    return (f"Email 1 subject (General email1_subject_share): personal {v.with_n} companies, "
            f"{_pct(v.with_replied, v.with_n)} replied vs the Copy row's {v.without_n}, "
            f"{_pct(v.without_replied, v.without_n)}{p}: {SUBJECT_VERDICTS[v.verdict]}.")


def lines(r: Review) -> list[str]:
    """The review in plain words, the signals with a verdict first."""
    n = len(r.companies)
    if not n:
        return ["No company has been emailed yet, so there is nothing to review."]
    replied = sum(c.replied for c in r.companies)
    positive = sum(c.positive for c in r.companies)
    first = f", the first on {r.first_send:%a %d %b}" if r.first_send else ""
    out = [f"Signal review: {n} companies emailed{first}; {r.closed} of their {REPLY_WINDOW_DAYS}-day reply windows "
           f"have closed. Replied: {replied} ({_pct(replied, n)}), positive: {positive} ({_pct(positive, n)}).",
           "By tier at enrolment (scoring works if Priority replies most and Control least):"]
    out += [f"  {t}: {e} emailed, {_pct(k, e)} replied" for t, (e, k) in r.tiers.items()]
    if r.subject is not None:
        out.append(subject_line(r.subject))
    order = {"raise": 0, "lower": 1, "keep": 2, "too few": 3}
    judged = [v for v in sorted(r.verdicts, key=lambda v: (order[v.verdict], -v.with_n, v.signal)) if v.verdict != "too few"]
    if judged:
        out.append("Signals (replied with the signal vs without it):")
    for v in judged:
        p = f"p = {v.p:.2f}" if v.p is not None else ""
        out.append(f"  {v.verdict.upper():5}  {v.signal} (weight {v.weight}): {v.with_n} companies, "
                   f"{_pct(v.with_replied, v.with_n)} vs {_pct(v.without_replied, v.without_n)} without · {p}")
    few = [v for v in r.verdicts if v.verdict == "too few"]
    if few:
        names = ", ".join(f"{v.signal} ({v.with_n})" for v in sorted(few, key=lambda v: (-v.with_n, v.signal)))
        out.append(f"Too few to judge (under {MIN_COMPANIES} emailed companies with or without it): {names}.")
    out.append("Nothing changes by itself: change a weight on the Signals tab, then `us-outbound sync`. A verdict is "
               "a lead to follow while samples are small, not proof.")
    return out


def ready(ctx: Context) -> str:
    """The daily post's one line once the review is worth reading, else ""."""
    companies, closed = emailed(ctx)
    if len(companies) < READY_DELIVERED or ctx.now - companies[0].step1_at < timedelta(days=READY_DAYS):
        return ""
    return (f"  Signal review: {len(companies)} companies emailed ({closed} reply windows closed). "
            "`us-outbound signals review` shows which signals predict replies, to reweight the Signals tab.")
