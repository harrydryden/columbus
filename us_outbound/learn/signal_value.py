"""The signal-value table: what each signal on the Signals tab is worth (SPEC 12 "Signal value"; Harry, 6 Oct 2026).

It reads v_signal_value (sql/views/02_v_signal_value.sql). For each active signal, over the enrolled accounts:
how many had it when they were enrolled (contacts.signals_at_enrol), how many were sent step 1, and, of those
whose step 1 was delivered, how many replied (a human, within 28 days of step 1), replied positively (positive
or referral) and booked a meeting (crm/readback.py); then the same rates for the emailed accounts without it.

Small numbers are said to be small. Under MIN_TO_READ (30) emailed accounts on either side the rates are "too
few to read" (the view's too_few). From 30 on each side, a two-proportion test on the reply rates
(signal_review.p_value, as `signals review` uses) says whether the difference is likely to be more than chance:
under signal_review.P_VALUE (0.10) the signal "replies more" or "replies less", else "no clear difference yet". A
pilot's samples are small, so even that is a lead to follow, not proof. SPEC 12's flag stays: below Control, after
200 accounts, when its accounts reply less than the Control tier's.

Nothing re-weights itself: Harry changes weights on the Signals tab, then `us-outbound sync`.
`us-outbound signals value` prints the table; the Monday readout shows its headline (learn/readout.py).
`us-outbound signals review` asks the same question of the events directly, with the tiers and email 1's subject.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound import fmt
from us_outbound.context import Context
from us_outbound.learn import signal_review

MIN_TO_READ = signal_review.MIN_COMPANIES  # 30; sql/views/02_v_signal_value.sql too_few uses the same number
SQL = "SELECT * FROM {schema}.v_signal_value"
TOO_FEW = "too few to read"
MORE, LESS, KEEP = "replies more", "replies less", "no clear difference yet"
HEADLINE_SIGNALS = 5  # signals the readout names


def rows(ctx: Context) -> list[dict] | None:
    """v_signal_value's rows, or None when the store has no views (a MemoryStore without a handler)."""
    try:
        return [dict(r) for r in ctx.store.query(SQL.format(schema=ctx.store.schema))]
    except NotImplementedError:
        return None


def _n(row: Mapping[str, Any], key: str) -> int:
    return int(row.get(key) or 0)


def too_few(row: Mapping[str, Any]) -> bool:
    return _n(row, "accounts_delivered") < MIN_TO_READ or _n(row, "without_delivered") < MIN_TO_READ


def p_value(row: Mapping[str, Any]) -> float | None:
    return signal_review.p_value(_n(row, "accounts_delivered"), _n(row, "accounts_replied"),
                                 _n(row, "without_delivered"), _n(row, "without_replied"))


def verdict(row: Mapping[str, Any]) -> str:
    if too_few(row):
        return TOO_FEW
    p = p_value(row)
    if p is None or p >= signal_review.P_VALUE:
        return KEEP
    with_rate = _n(row, "accounts_replied") / _n(row, "accounts_delivered")
    without = _n(row, "without_replied") / _n(row, "without_delivered")
    return MORE if with_rate > without else LESS


def line(row: Mapping[str, Any]) -> str:
    """One signal in plain words, its verdict last. Too few to read: counts only, no rate."""
    v = verdict(row)
    weight = row.get("weight")
    head = f"{row.get('signal')}" + (f" (weight {weight})" if weight is not None else "")
    enrolled, d, wd = _n(row, "accounts_enrolled"), _n(row, "accounts_delivered"), _n(row, "without_delivered")
    if not _n(row, "accounts_sent"):
        return f"{head}: {enrolled} enrolled with it, none emailed yet"
    replied, without = _n(row, "accounts_replied"), _n(row, "without_replied")
    counts = (f"{replied} replied, {_n(row, 'accounts_positive')} positive, {_n(row, 'accounts_meeting')} "
              f"meetings")
    if v == TOO_FEW:
        side = "with it" if d < MIN_TO_READ else "without it"
        return (f"{head}: {enrolled} enrolled with it, {d} emailed, {counts}: {TOO_FEW} (under {MIN_TO_READ} "
                f"emailed {side})")
    p = p_value(row)
    tail = f"{v} (p = {p:.2f})" if p is not None else v
    if row.get("below_control"):
        tail += "; replies below the Control tier after 200"
    return (f"{head}: {enrolled} enrolled with it, {d} emailed, {counts} ({fmt.pct(replied, d)} replied, against "
            f"{fmt.pct(without, wd)} of {wd} without): {tail}")


def table_lines(table: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """`us-outbound signals value`: every active signal with an enrolled account, then the rest in one line."""
    if table is None:
        return ["The signal table reads the database's views, which this store does not have."]
    seen = [r for r in table if _n(r, "accounts_enrolled") or _n(r, "accounts_sent")]
    out = [f"Signal value: each active signal, over the companies enrolled with it, against the companies emailed "
           f"without it. Under {MIN_TO_READ} emailed on either side a rate is {TOO_FEW}."]
    order = {MORE: 0, LESS: 1, KEEP: 2, TOO_FEW: 3}
    out += [f"  {line(r)}" for r in sorted(seen, key=lambda r: (order[verdict(r)], -_n(r, "accounts_sent"),
                                                              str(r.get("signal"))))]
    idle = sorted(str(r.get("signal")) for r in table if r not in seen)
    if idle:
        out.append(f"  No company enrolled with: {', '.join(idle)}.")
    out.append("Nothing changes by itself: change a weight on the Signals tab, then `us-outbound sync`.")
    return out


def headline(table: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """The Monday readout's lines: the signals that can be read, else the ones closest to it, and how to see all."""
    if table is None:
        return ["  The signal table needs the database's views (v_signal_value), which this store does not have."]
    seen = [r for r in table if _n(r, "accounts_sent")]
    if not seen:
        return ["  No company has been emailed with a signal yet."]
    readable = [r for r in seen if verdict(r) != TOO_FEW]
    out = []
    if readable:
        order = {MORE: 0, LESS: 1, KEEP: 2}
        for r in sorted(readable, key=lambda r: (order[verdict(r)], -_n(r, "accounts_delivered")))[:HEADLINE_SIGNALS]:
            out.append(f"  {line(r)}")
    few = [r for r in seen if verdict(r) == TOO_FEW]
    if few:
        names = ", ".join(f"{r.get('signal')} ({_n(r, 'accounts_delivered')})"
                          for r in sorted(few, key=lambda r: (-_n(r, "accounts_delivered"), str(r.get("signal"))))
                          [:HEADLINE_SIGNALS])
        more = f" and {len(few) - HEADLINE_SIGNALS} more" if len(few) > HEADLINE_SIGNALS else ""
        out.append(f"  {TOO_FEW.capitalize()} (under {MIN_TO_READ} companies emailed with or without it): "
                   f"{names}{more}.")
    flagged = [str(r.get("signal")) for r in seen if r.get("below_control")]
    if flagged:
        out.append(f"  Replying below the Control tier after 200 companies: {', '.join(flagged)}.")
    out.append("  `us-outbound signals value` has the whole table.")
    return out
