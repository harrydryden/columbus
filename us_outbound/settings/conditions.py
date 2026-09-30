"""What a Signals-tab `looks_for` means (SPEC 5).

Field sources: a condition, `field op value`, joined by AND / OR.
  * Operators: = != > >= < <= in contains
  * Values: a number, a 'quoted' or "quoted" string, a [list], or true / false
  * AND binds tighter than OR; parentheses group
  * A small hand-written tokenizer and recursive-descent parser. Never eval.

Text sources: terms separated by semicolons, matched case-insensitively as whole words.
An optional context_rule names terms that must appear near a match, for ambiguous
brand names, e.g.  Headspace: for Work, app, subscription; Calm: app, premium, business
A context entry without "Term:" applies to every term of the signal.

A looks_for that parses as a condition is a condition; anything else is a term list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Union

NEAR_CHARS = 60  # how close a context term must be to a match, either side

Value = Union[int, float, str, bool, tuple]


class ConditionError(ValueError):
    pass


# -- tokenizer ---------------------------------------------------------------

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>-?\d+(?:\.\d+)?)(?![\w.])
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<op>!=|>=|<=|=|>|<)
  | (?P<punct>[\[\](),])
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str  # num | str | op | punct | word | end
    text: str
    pos: int


def _tokenize(src: str) -> list[_Tok]:
    toks: list[_Tok] = []
    pos = 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise ConditionError(f"unexpected {src[pos]!r} at position {pos}")
        kind = m.lastgroup or ""
        if kind != "ws":
            toks.append(_Tok(kind, m.group(kind), pos))
        pos = m.end()
    toks.append(_Tok("end", "", len(src)))
    return toks


# -- AST ---------------------------------------------------------------------


@dataclass(frozen=True)
class Compare:
    field: str
    op: str
    value: Value

    def evaluate(self, facts: Mapping[str, Any]) -> bool:
        if self.field not in facts or facts[self.field] is None:
            return False  # a fact we do not have never matches (not read is not "nothing found")
        return _compare(facts[self.field], self.op, self.value)

    def fields(self) -> set[str]:
        return {self.field}

    def __str__(self) -> str:
        return f"{self.field} {self.op} {_fmt(self.value)}"


@dataclass(frozen=True)
class BoolOp:
    op: str  # AND | OR
    items: tuple[Node, ...]

    def evaluate(self, facts: Mapping[str, Any]) -> bool:
        if self.op == "AND":
            return all(i.evaluate(facts) for i in self.items)
        return any(i.evaluate(facts) for i in self.items)

    def fields(self) -> set[str]:
        return set().union(*(i.fields() for i in self.items))

    def __str__(self) -> str:
        return f" {self.op} ".join(f"({i})" if isinstance(i, BoolOp) else str(i) for i in self.items)


Node = Union[Compare, BoolOp]


@dataclass(frozen=True)
class Condition:
    text: str
    root: Node

    def evaluate(self, facts: Mapping[str, Any]) -> bool:
        return self.root.evaluate(facts)

    @property
    def fields(self) -> frozenset[str]:
        return frozenset(self.root.fields())

    def __str__(self) -> str:
        return str(self.root)


# -- parser ------------------------------------------------------------------


class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = _tokenize(src)
        self.i = 0

    @property
    def tok(self) -> _Tok:
        return self.toks[self.i]

    def _next(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _is_word(self, *words: str) -> bool:
        return self.tok.kind == "word" and self.tok.text.upper() in words

    def parse(self) -> Node:
        node = self._or()
        if self.tok.kind != "end":
            raise ConditionError(f"unexpected {self.tok.text!r} at position {self.tok.pos}")
        return node

    def _or(self) -> Node:
        items = [self._and()]
        while self._is_word("OR"):
            self._next()
            items.append(self._and())
        return items[0] if len(items) == 1 else BoolOp("OR", tuple(items))

    def _and(self) -> Node:
        items = [self._term()]
        while self._is_word("AND"):
            self._next()
            items.append(self._term())
        return items[0] if len(items) == 1 else BoolOp("AND", tuple(items))

    def _term(self) -> Node:
        if self.tok.kind == "punct" and self.tok.text == "(":
            self._next()
            node = self._or()
            if not (self.tok.kind == "punct" and self.tok.text == ")"):
                raise ConditionError(f"missing ')' at position {self.tok.pos}")
            self._next()
            return node
        return self._compare()

    def _compare(self) -> Compare:
        t = self._next()
        if t.kind != "word" or t.text.upper() in {"AND", "OR", "IN", "CONTAINS", "TRUE", "FALSE"}:
            raise ConditionError(f"expected a field name at position {t.pos}, got {t.text!r}")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", t.text):
            raise ConditionError(f"field names are lower_snake_case: {t.text!r}")
        field = t.text
        o = self._next()
        if o.kind == "op":
            op = o.text
        elif o.kind == "word" and o.text.lower() in {"in", "contains"}:
            op = o.text.lower()
        else:
            raise ConditionError(f"expected an operator after {field!r} at position {o.pos}")
        value = self._value()
        if op == "in" and not isinstance(value, tuple):
            raise ConditionError(f"'in' needs a [list] at position {o.pos}")
        if op in {">", ">=", "<", "<="} and (isinstance(value, (bool, tuple)) or not isinstance(value, (int, float))):
            raise ConditionError(f"{op} needs a number at position {o.pos}")
        return Compare(field, op, value)

    def _value(self) -> Value:
        t = self.tok
        if t.kind == "punct" and t.text == "[":
            self._next()
            items: list[Value] = []
            if self.tok.kind == "punct" and self.tok.text == "]":
                self._next()
                return tuple(items)
            while True:
                items.append(self._scalar())
                if self.tok.kind == "punct" and self.tok.text == ",":
                    self._next()
                    continue
                if self.tok.kind == "punct" and self.tok.text == "]":
                    self._next()
                    return tuple(items)
                raise ConditionError(f"expected ',' or ']' at position {self.tok.pos}")
        return self._scalar()

    def _scalar(self) -> Value:
        t = self._next()
        if t.kind == "num":
            return float(t.text) if "." in t.text else int(t.text)
        if t.kind == "str":
            return re.sub(r"\\(.)", r"\1", t.text[1:-1])
        if t.kind == "word" and t.text.lower() in {"true", "false"}:
            return t.text.lower() == "true"
        raise ConditionError(f"expected a number, a quoted string, true/false or a [list] at position {t.pos}")


def parse_condition(text: str) -> Condition:
    if not text or not text.strip():
        raise ConditionError("empty condition")
    return Condition(text.strip(), _Parser(text).parse())


def try_parse_condition(text: str) -> Condition | None:
    try:
        return parse_condition(text)
    except ConditionError:
        return None


# -- evaluation ----------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _eq(have: Any, want: Value) -> bool:
    if isinstance(want, bool) or isinstance(have, bool):
        if isinstance(have, str):
            have = {"true": True, "yes": True, "false": False, "no": False}.get(have.strip().lower(), have)
        return isinstance(have, bool) and isinstance(want, bool) and have == want
    hn, wn = _num(have), _num(want)
    if hn is not None and wn is not None and not isinstance(want, str):
        return hn == wn
    if isinstance(have, str) and isinstance(want, str):
        return have.strip().casefold() == want.strip().casefold()
    return have == want


def _compare(have: Any, op: str, want: Value) -> bool:
    if op == "=":
        return _eq(have, want)
    if op == "!=":
        return not _eq(have, want)
    if op == "in":
        return any(_eq(have, w) for w in want)  # type: ignore[union-attr]
    if op == "contains":
        if isinstance(have, (list, tuple, set, frozenset)):
            wants = want if isinstance(want, tuple) else (want,)
            return any(_eq(h, w) for h in have for w in wants)
        if isinstance(have, str) and isinstance(want, str):
            return want.casefold() in have.casefold()
        return False
    hn, wn = _num(have), _num(want)
    if hn is None or wn is None:
        return False
    return {">": hn > wn, ">=": hn >= wn, "<": hn < wn, "<=": hn <= wn}[op]


def _fmt(v: Value) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace('"', '\\"') + '"'
    if isinstance(v, tuple):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return str(v)


# -- term lists ----------------------------------------------------------------

ContextRules = dict[str, tuple[str, ...]]  # lower-case term (or "*") -> context terms


def parse_terms(looks_for: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for part in looks_for.split(";"):
        term = " ".join(part.split())
        if term and term.casefold() not in seen:
            seen[term.casefold()] = term
    return tuple(seen.values())


def parse_context_rule(rule: str, terms: tuple[str, ...]) -> ContextRules:
    """Parse "Headspace: for Work, app, subscription; Calm: app, premium" into rules.

    Raises ConditionError if a named term is not one of the signal's terms.
    """
    rules: ContextRules = {}
    known = {t.casefold() for t in terms}
    for entry in (rule or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            term, _, ctx = entry.partition(":")
            key = " ".join(term.split()).casefold()
            if key not in known:
                raise ConditionError(f"context rule names {term.strip()!r}, which is not in looks_for")
        else:
            key, ctx = "*", entry
        words = tuple(" ".join(w.split()) for w in re.split(r",|\bor\b", ctx) if w.strip())
        if not words:
            raise ConditionError(f"context rule for {key!r} lists no terms")
        rules[key] = rules.get(key, ()) + words
    return rules


def term_pattern(term: str) -> re.Pattern[str]:
    pieces = [re.escape(p) for p in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(pieces) + r"(?!\w)", re.IGNORECASE)


@dataclass(frozen=True)
class TermMatch:
    term: str  # the term as written on the sheet
    matched: str  # the text as it appears in the source
    start: int
    end: int


def find_terms(text: str, terms: tuple[str, ...], context: ContextRules | None = None) -> list[TermMatch]:
    """Return the first qualifying match of each term in text (at most one per term)."""
    context = context or {}
    out: list[TermMatch] = []
    for term in terms:
        needs = context.get(term.casefold(), ()) + context.get("*", ())
        for m in term_pattern(term).finditer(text or ""):
            if needs:
                window = text[max(0, m.start() - NEAR_CHARS) : m.end() + NEAR_CHARS]
                if not any(term_pattern(c).search(window) for c in needs):
                    continue
            out.append(TermMatch(term, m.group(0), m.start(), m.end()))
            break
    return out
