"""Company names (SPEC 13, Data cleaning; SPEC 2 job 6).

  * Start from Clay's cleaned name, checked against our rules: the rules are always
    re-applied, so a Clay name that still carries "LLC" or a tagline is fixed here.
  * Strip entity suffixes (Inc, Inc., LLC, L.L.C., Ltd, Corp, Co., PLLC, LLP, PC, P.A. ...).
  * Strip taglines after " | ", " – ", " — ", " - " or ":", and bracketed qualifiers such
    as "(formerly …)" or "[US]".
  * Strip phrases such as "Official Site", "Home", "Welcome to".
  * Keep the legal name separately: the raw name without taglines or brackets, suffix kept.

Casing is never changed: "iCIMS", "HubSpot" and "ACME" stay as written.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Entity suffixes, compared without dots and in upper case ("L.L.C." -> "LLC").
ENTITY_SUFFIXES = frozenset(
    {"INC", "INCORPORATED", "LLC", "LTD", "LIMITED", "CORP", "CORPORATION", "PLLC", "LLP", "LLLP",
     "LP", "PC", "PLC", "GMBH", "PTY"}
)
# Also USPS codes (Colorado, Pennsylvania): stripped only as "Co." / "P.A." or after a comma,
# so "Big Brothers Big Sisters of Southeastern PA" keeps its PA.
AMBIGUOUS_SUFFIXES = frozenset({"CO", "PA"})
# Values Clay (or Apollo) uses for "no name".
MISSING = frozenset({"", "n/a", "na", "none", "null", "unknown", "-"})

_BRACKETED = re.compile(r"\s*(?:\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\})")
_UNCLOSED = re.compile(r"\s*[(\[{][^)\]}]*$")
_STRAY_CLOSE = re.compile(r"[)\]}]")
_MARKS = re.compile(r"[™®©℠]")
# Pipe anywhere; dashes and dots only when spaced; a colon followed by a space or the end
# (so "Re:Build Manufacturing" is kept).
_TAGLINE_SPLIT = re.compile(r"\s*\|\s*|\s+[–—·•-]\s+|:(?=\s|$)")
_BOILERPLATE_SEGMENT = re.compile(
    r"(?:welcome(?:\s+to)?|home(?:\s*page)?|official\s+(?:web\s*)?site|about(?:\s+us)?|main\s+page|index)",
    re.IGNORECASE,
)
_LEADING_PHRASE = re.compile(r"^(?:welcome\s+to\s+|official\s+(?:web\s*)?site\s+(?:of|for)\s+)", re.IGNORECASE)
_TRAILING_PHRASE = re.compile(r"\s+(?:official\s+(?:web\s*)?site|homepage|home\s+page)$", re.IGNORECASE)
_LAST_TOKEN = re.compile(r"(?P<comma>,)?\s*(?P<tok>[A-Za-z][A-Za-z.]*)$")
_AMPERSAND_END = re.compile(r"(?:&|\band)$", re.IGNORECASE)
_DOUBLE_QUOTES = re.compile(r"[\"“”]")


def _squash(text: str) -> str:
    return " ".join(text.split())


def strip_decorations(raw: str) -> str:
    """The name without brackets, taglines, boilerplate phrases or trademark marks."""
    s = _DOUBLE_QUOTES.sub("", _MARKS.sub("", raw or ""))
    prev = None
    while prev != s:  # innermost brackets first, so nested ones go too
        prev, s = s, _BRACKETED.sub("", s)
    s = _STRAY_CLOSE.sub("", _UNCLOSED.sub("", s))
    s = _squash(s)
    for seg in _TAGLINE_SPLIT.split(s):
        seg = _squash(seg).strip(" ,")
        if not seg or _BOILERPLATE_SEGMENT.fullmatch(seg):
            continue
        seg = _TRAILING_PHRASE.sub("", _LEADING_PHRASE.sub("", seg)).strip(" ,")
        if seg and not _BOILERPLATE_SEGMENT.fullmatch(seg):
            return seg
    return ""


def strip_entity_suffix(name: str) -> str:
    """Drop trailing entity suffixes, repeatedly ("Acme, Inc. LLC" -> "Acme"); never the whole name."""
    s = name.strip().rstrip(" ,")
    while True:
        m = _LAST_TOKEN.search(s)
        if not m:
            return s
        head = s[: m.start()].rstrip()
        tok = m["tok"]
        key = tok.replace(".", "").upper()
        if not head.rstrip(" ,"):
            return s
        if key in ENTITY_SUFFIXES:
            pass
        elif key in AMBIGUOUS_SUFFIXES and ("." in tok or m["comma"]):
            if key == "CO" and _AMPERSAND_END.search(head):
                return s  # "Smith & Co." is the name itself
        else:
            return s
        s = head.rstrip(" ,")


def has_entity_suffix(name: str) -> bool:
    return strip_entity_suffix(name) != name.strip().rstrip(" ,")


def _clean(base: str) -> str:
    s = strip_entity_suffix(strip_decorations(base)).strip(" ,-–—&")
    key = s.replace(".", "").upper()
    return "" if key in ENTITY_SUFFIXES or key in AMBIGUOUS_SUFFIXES else s


def clean_company_name(
    raw: str | None, clay_clean_name: str | None = None, *, clay_legal_name: str | None = None
) -> tuple[str, str]:
    """(clean_name, legal_name) for an account.

    clean_name starts from Clay's cleaned name when there is one, with our rules applied
    again; if that leaves nothing, the raw name cleaned; failing that, the raw name trimmed.
    legal_name is the raw name without taglines or brackets, entity suffix kept. When the
    raw name has no suffix but Clay's legal_name does, Clay's is used.
    """
    raw = _squash(raw or "")
    clay = _squash(clay_clean_name or "")
    clean = ""
    if clay.casefold() not in MISSING:
        clean = _clean(clay)
    if not clean:
        clean = _clean(raw) or raw

    legal = strip_decorations(raw) or raw
    clay_legal = strip_decorations(_squash(clay_legal_name or ""))
    if (
        clay_legal
        and clay_legal.casefold() not in MISSING
        and has_entity_suffix(clay_legal)
        and not has_entity_suffix(legal)
        and not names_disagree(clay_legal, legal)
    ):
        legal = clay_legal
    return clean, legal


def _key(name: str) -> list[str]:
    s = strip_entity_suffix(strip_decorations(name) or name).casefold().replace("&", " and ")
    words = re.sub(r"[^\w\s]", "", s).split()
    if words and words[0] == "the":
        words = words[1:]
    return words


def names_disagree(a: str | None, b: str | None) -> bool:
    """True when two names for the same company look like different companies (for the hand-check).

    Loose: case, punctuation, entity suffixes, "&" versus "and", a leading "The", and
    one name being the other plus trailing words ("Acme" / "Acme Creative Agency") all agree.
    """
    ka, kb = _key(a or ""), _key(b or "")
    if not ka or not kb:
        return bool(ka) != bool(kb)
    ja, jb = "".join(ka), "".join(kb)
    if ja == jb:
        return False
    short, long_ = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if long_[: len(short)] == short:
        return False
    return SequenceMatcher(None, ja, jb).ratio() < 0.85
