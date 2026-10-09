"""Facts (signal_events rows) read one way everywhere: which one is the newest (9 Oct 2026; ~24 modules had their own).

The newest row is the one with the latest observed_at; of two with the same time, the one with the higher event_id,
so a tie has one answer whatever order the database returns the rows in. A row with no readable observed_at is older
than every dated one but can still be the newest, unless the reader asks for dated rows only (dated=True).

The readers take and return the stored rows themselves, so a caller goes on reading a row's value, quote and
source_url as before. newest_by() picks the newest row per key (per account, or per account and fact) in one pass.
texts() reads a list fact (Apollo's keywords and NAICS codes) whether it was stored as a list or as comma-separated
text. load() reads the rows for many accounts, ID_CHUNK at a time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Hashable, Iterable, Iterator, Mapping
from datetime import datetime
from typing import Any, TypeVar

from us_outbound.timeparse import EPOCH, utc

Row = Mapping[str, Any]
Names = str | Collection[str] | None  # one source or fact, or several
K = TypeVar("K", bound=Hashable)
ID_CHUNK = 1000


def at(row: Row) -> datetime | None:
    """When the fact was observed (an aware UTC datetime), or None when the row has no readable time."""
    return utc(row.get("observed_at"))


def order(row: Row) -> tuple[bool, datetime, str]:
    """The sort key, newest greatest: dated after undated, then by time, then by event_id."""
    t = at(row)
    return t is not None, t or EPOCH, str(row.get("event_id") or "")


def _chosen(rows: Iterable[Row], fact: str | None, source: Names, where: Callable[[Row], bool] | None,
            dated: bool) -> Iterator[Row]:
    sources = {source} if isinstance(source, str) else None if source is None else set(source)
    for r in rows:
        if ((fact is None or r.get("fact") == fact) and (sources is None or r.get("source") in sources)
                and (not dated or at(r) is not None) and (where is None or where(r))):
            yield r


def newest(rows: Iterable[Row], fact: str | None = None, source: Names = None, *,
           where: Callable[[Row], bool] | None = None, dated: bool = False) -> Row | None:
    """The newest row of the fact (any fact when None), from the source or sources (any when None), that where
    accepts; dated leaves out rows with no time (where then sees only dated rows). None when no row is left."""
    return max(_chosen(rows, fact, source, where, dated), key=order, default=None)


def newest_by(rows: Iterable[Row], key: Callable[[Row], K], fact: str | None = None, source: Names = None, *,
              where: Callable[[Row], bool] | None = None, dated: bool = False) -> dict[K, Row]:
    """key(row) -> the newest row with that key, over the rows newest() would choose from."""
    out: dict[K, tuple[tuple[bool, datetime, str], Row]] = {}
    for r in _chosen(rows, fact, source, where, dated):
        k, o = key(r), order(r)
        if k not in out or o > out[k][0]:
            out[k] = (o, r)
    return {k: r for k, (_, r) in out.items()}


def latest_by_fact(rows: Iterable[Row], sources: Names = None, skip_facts: Collection[str] = ()) -> dict[str, Row]:
    """fact -> its newest row, over the rows that name a fact (from sources, when given), but those in skip_facts.
    A fact two sources write is taken from whichever wrote it last."""
    return newest_by(rows, lambda r: str(r["fact"]), source=sources,
                     where=lambda r: bool(r.get("fact")) and r["fact"] not in skip_facts)


def value(rows: Iterable[Row], fact: str, source: Names = None, default: Any = None) -> Any:
    """The newest row's value (newest()), or default when there is no such row."""
    r = newest(rows, fact, source)
    return default if r is None else r.get("value")


def history(rows: Iterable[Row], fact: str | None = None, source: Names = None) -> list[Row]:
    """The rows (of the fact, from the source or sources, when given), newest first."""
    return sorted(_chosen(rows, fact, source, None, False), key=order, reverse=True)


def texts(value: Any) -> list[str]:
    """The items of a list fact, stored as a list or as comma-separated text, each with its spaces folded; blank
    items are dropped (labels and the opener's "what they do" phrase read Apollo's keywords this one way)."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return [t for x in items if (t := " ".join(str(x or "").split()))]


def load(store: Any, ids: Iterable[str], sources: Names = None, facts: Names = None) -> dict[str, list[dict]]:
    """account_id -> its signal_events rows (from sources, of facts, when given; an account with none reads as []),
    one select per ID_CHUNK accounts."""
    ids = list(ids)
    where = {col: [want] if isinstance(want, str) else list(want)
             for col, want in (("source", sources), ("fact", facts)) if want is not None}
    out: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(ids), ID_CHUNK):
        for e in store.select("signal_events", {**where, "account_id": ids[i : i + ID_CHUNK]}):
            out[str(e.get("account_id"))].append(e)
    return out
