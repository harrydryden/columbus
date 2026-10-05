"""Page text for the careers and benefits page reader (sources/pages.py; Harry, 2 Oct 2026).

Pure functions, no network:
  * parse_html(): a page's links (a, iframe and script sources, made absolute) and its text
    as blocks (headings, list items, paragraphs) in document order, without scripts, styles
    or navigation text. No JavaScript is run: what a browser would add later is not seen.
  * benefit_snippets(): the sentences and list items that mention benefits, mental health,
    the EAP, wellbeing, perks, leave or stipends, or any term of the page signals on the
    Signals tab (the vendor names), each at most 300 characters around what it mentions.
    Outside a benefits section (a heading such as "Benefits" or "What we offer", or a whole
    benefits page) a sentence is kept only if it also reads as an offer to staff ("we offer",
    "stipend", "covered", ...), so a company describing its own therapy product is not read
    as offering therapy to its team.
  * provision(): whether a snippet is a mental-health provision, in the shape of the Clay
    Accounts function's mental_health_provision (SPEC 8: type and provider), or a benefit.
  * parse_robots(): the robots.txt rules that apply to us (RFC 9309: our group, else "*"; the
    longest matching rule wins, Allow on a tie; * and $ in paths). The reader fetches robots.txt
    through the guard itself; nothing here touches the network.
The reader stores what the pages say; whether it matches a signal is decided in scoring,
against the Signals tab, so a signal edit never needs a code change (SPEC 8).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser

from us_outbound.clients.public import absolute_location
from us_outbound.settings.conditions import ContextRules, find_terms

QUOTE_LIMIT = 300  # SPEC 6: signal_events.quote is at most 300 characters
MAX_SNIPPETS = 40  # per page or feed
NEVER_CLOSES = 7  # the level of a dt, a summary or a label block: only a real heading (h1-h6) ends its section

# What the reader keeps, besides the Signals tab's own terms (benefit_snippets' terms argument).
BASE_TERMS = (
    "benefit", "benefits", "perk", "perks", "mental health", "mental-health", "EAP", "employee assistance",
    "wellbeing", "well-being", "wellness", "therapy", "therapist", "counseling", "counselling", "counselor",
    "PTO", "paid time off", "time off", "leave", "parental leave", "maternity leave", "paternity leave",
    "sabbatical", "stipend", "stipends", "allowance", "reimbursement", "health insurance", "medical", "dental",
    "vision", "401(k)", "401k", "retirement", "holidays", "vacation", "gym", "fitness",
)
BENEFIT_HEADING = re.compile(
    r"\b(benefits?|perks?|what we offer|what you('ll| will) get|we offer|our offer|compensation|total rewards|"
    r"why (work|join)|life at|working (at|here|with us)|wellbeing|well-being|wellness|time off|"
    r"health( and | & )(wellbeing|well-being|wellness))\b",
    re.IGNORECASE,
)
# A block that is nothing but a section's name: "Benefits", "Perks & benefits:", "What we offer".
BENEFIT_LABEL = re.compile(
    r"((our|the|your) )?(benefits?|perks?|perks (and|&) benefits|benefits (and|&) perks|what we offer|"
    r"what you('ll| will) get|total rewards|compensation( (and|&) benefits)?|wellbeing|well-being|wellness|time off|"
    r"health( (and|&) (wellbeing|well-being|wellness))?)",
    re.IGNORECASE,
)
# An offer to staff, for a sentence outside a benefits section.
OFFER = re.compile(
    r"\b(stipends?|allowances?|reimburs\w*|we offer|offers?|offered|we provide|provided|"
    r"you('ll| will)? (get|receive|enjoy|have)|access to|coverage|covered|paid|eligible|PTO|time off|"
    r"our (team|employees|people|staff))\b",
    re.IGNORECASE,
)
MENTAL_HEALTH = re.compile(
    r"\b(mental[- ]health|EAP|employee assistance|therap(y|ies|ist|ists)|counsell?ing|counsell?ors?|"
    r"psycholog\w*|psychiatr\w*|emotional (wellbeing|well-being|health)|behavioral health)\b",
    re.IGNORECASE,
)
EAP_WORDS = re.compile(r"\b(EAP|employee assistance)\b", re.IGNORECASE)
CARRIERS = re.compile(r"\b(Optum|Cigna|Aetna|Carelon|Anthem|UnitedHealthcare|Humana|Kaiser|Blue Cross|BCBS)\b")
MENTAL_HEALTH_DAYS = re.compile(r"\bmental[- ]health days?\b", re.IGNORECASE)
THERAPY = re.compile(r"\btherap(y|ies|ist|ists)\b", re.IGNORECASE)
STIPEND = re.compile(r"\b(stipends?|reimburs\w*|allowances?)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9\"“(•])")

BLOCK_TAGS = frozenset({
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "tr", "table", "dd", "dt", "dl",
    "section", "article", "main", "header", "footer", "aside", "blockquote", "br", "hr", "summary", "details",
    "figcaption", "form", "button", "label",
})
HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6, "dt": NEVER_CLOSES, "summary": NEVER_CLOSES}
SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head", "select", "title"})
NO_TEXT_TAGS = frozenset({"nav"})  # links are kept, text is not
VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed"})
LINK_ATTRS = {"a": "href", "iframe": "src", "script": "src", "area": "href"}


@dataclass(frozen=True)
class Link:
    url: str  # absolute, without its #fragment
    text: str  # the anchor text ("" for an iframe or script)


@dataclass(frozen=True)
class Block:
    text: str
    level: int = 0  # 1-6 for h1-h6, NEVER_CLOSES for dt and summary; 0 for text


@dataclass
class Page:
    url: str
    links: list[Link] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    raw: str = ""  # the HTML, for links a script builds (an ATS embed)


@dataclass(frozen=True)
class Snippet:
    text: str  # at most QUOTE_LIMIT characters
    terms: tuple[str, ...]  # what it mentions, as written on the page


class _Reader(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links: list[Link] = []
        self.blocks: list[Block] = []
        self._parts: list[str] = []
        self._level = 0
        self._skip = 0
        self._no_text = 0
        self._anchor: tuple[str, list[str]] | None = None

    def _flush(self) -> None:
        text = " ".join("".join(self._parts).split())
        if text:
            self.blocks.append(Block(text, self._level))
        self._parts, self._level = [], 0

    def _link(self, href: str, text: str = "") -> None:
        href = (href or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            return
        try:
            url = absolute_location(self.base, href).split("#", 1)[0]
        except ValueError:
            return
        if url.lower().startswith(("http://", "https://")):
            self.links.append(Link(url, " ".join(text.split())))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        attr = LINK_ATTRS.get(tag)
        if attr and a.get(attr):
            if tag == "a":
                self._anchor = (a[attr], [])
            else:
                self._link(a[attr])
        if tag in SKIP_TAGS and tag not in VOID_TAGS:
            self._skip += 1
        elif tag in NO_TEXT_TAGS:
            self._no_text += 1
        if tag in BLOCK_TAGS:
            self._flush()
            self._level = HEADING_LEVELS.get(tag, 0)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            href, parts = self._anchor
            self._link(href, "".join(parts))
            self._anchor = None
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in NO_TEXT_TAGS and self._no_text:
            self._no_text -= 1
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._anchor is not None:
            self._anchor[1].append(data)
        if not self._no_text:
            self._parts.append(data)

    def close(self) -> None:
        super().close()
        if self._anchor is not None:
            href, parts = self._anchor
            self._link(href, "".join(parts))
            self._anchor = None
        self._flush()


def parse_html(html: str, url: str) -> Page:
    """The page's links and text blocks. Malformed HTML is read as far as it goes."""
    reader = _Reader(url)
    try:
        reader.feed(html or "")
        reader.close()
    except AssertionError:  # html.parser's last resort on badly broken markup
        reader._flush()
    return Page(url=url, links=reader.links, blocks=reader.blocks, raw=html or "")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()] if len(text) > QUOTE_LIMIT else [text]


def clip_around(text: str, start: int, end: int, limit: int = QUOTE_LIMIT) -> str:
    """At most limit characters of text, keeping [start, end) and as much on each side as fits."""
    if len(text) <= limit:
        return text
    room = limit - 2 - (end - start)
    lo = max(0, start - max(0, room) // 2)
    hi = min(len(text), lo + limit - 2)
    lo = max(0, hi - (limit - 2))
    out = text[lo:hi].strip()
    return ("…" if lo > 0 else "") + out + ("…" if hi < len(text) else "")


def is_label(text: str) -> bool:
    """The text is only a benefits section's name ("Perks & benefits:")."""
    return bool(BENEFIT_LABEL.fullmatch(" ".join(text.split()).strip(" :.!-–")))


def is_heading(block: Block) -> bool:
    """An h1-h6, dt or summary; or a block of other markup that is only a section's name ("<p><b>Perks</b></p>")."""
    return bool(block.level) or is_label(block.text)


def benefit_snippets(page: Page, terms: Sequence[str] = (), *, whole_page: bool = False,
                     context: ContextRules | None = None, limit: int = MAX_SNIPPETS) -> list[Snippet]:
    """The page's benefit sentences and list items, in page order, each once.

    whole_page: the page is a benefits page (its address says so), so every sentence that
    mentions a term counts, as if under a benefits heading.
    """
    vocabulary = tuple(dict.fromkeys([*BASE_TERMS, *terms]))
    out: list[Snippet] = []
    seen: set[str] = set()
    in_section, section_level = False, 0
    for block in page.blocks:
        if is_heading(block):
            level = block.level or NEVER_CLOSES
            label = BENEFIT_HEADING.search(block.text)
            opens = bool(label or find_terms(block.text, vocabulary, context))
            if opens and (not in_section or level <= section_level):
                in_section, section_level = True, level
            elif in_section and level <= section_level:
                in_section = False
            if is_label(block.text):
                continue  # "Benefits", "Time off": a section's name, not something it offers
        for piece in _sentences(block.text):
            found = find_terms(piece, vocabulary, context)
            if not found:
                continue
            if not (whole_page or in_section or OFFER.search(piece)):
                continue
            first = min(found, key=lambda t: t.start)
            text = clip_around(piece, first.start, first.end)
            key = " ".join(text.casefold().split())
            if key in seen:
                continue
            seen.add(key)
            ordered = sorted(found, key=lambda t: t.start)
            out.append(Snippet(text, tuple(dict.fromkeys(t.matched for t in ordered))))
            if len(out) >= limit:
                return out
    return out


def is_provider_term(term: str) -> bool:
    """A sheet term that names a company: capitalized, and not an acronym ("ComPsych", "Spring Health"; not "EAP")."""
    t = term.strip()
    return bool(t) and t[0].isupper() and not t.replace(" ", "").isupper()


def provider_in(text: str, signal_terms: Iterable[tuple[tuple[str, ...], ContextRules]]) -> str | None:
    """The first company a page signal names in text, as written there (each signal's context rule applies)."""
    best: tuple[int, str] | None = None
    for terms, context in signal_terms:
        for tm in find_terms(text, tuple(t for t in terms if is_provider_term(t)), context):
            if best is None or tm.start < best[0]:
                best = (tm.start, tm.matched)
    return best[1] if best else None


@dataclass(frozen=True)
class Robots:
    """The Allow and Disallow rules of the robots.txt group that applies to us."""

    rules: tuple[tuple[str, bool], ...] = ()  # (path pattern, allowed)

    def allows(self, path: str) -> bool:
        best: tuple[int, bool] | None = None
        for pattern, allowed in self.rules:
            if pattern and _robots_pattern(pattern).match(path or "/"):
                if best is None or len(pattern) > best[0] or (len(pattern) == best[0] and allowed):
                    best = (len(pattern), allowed)
        return True if best is None else best[1]


ALLOW_ALL = Robots()
DISALLOW_ALL = Robots((("/", False),))


def _robots_pattern(pattern: str) -> re.Pattern[str]:
    end = pattern.endswith("$")
    body = "".join(".*" if ch == "*" else re.escape(ch) for ch in (pattern[:-1] if end else pattern))
    return re.compile(body + ("$" if end else ""))


def parse_robots(text: str, agent: str) -> Robots:
    """The rules for agent: every group naming it (merged), else every "*" group, else none (all allowed)."""
    groups: list[tuple[list[str], list[tuple[str, bool]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, bool]] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in ("allow", "disallow") and agents:
            rules.append((value, key == "allow"))
    if agents:
        groups.append((agents, rules))
    me = agent.lower()
    mine = [r for names, rs in groups if any(n and n != "*" and n in me for n in names) for r in rs]
    if not mine and not any(any(n and n != "*" and n in me for n in names) for names, _ in groups):
        mine = [r for names, rs in groups if "*" in names for r in rs]
    return Robots(tuple(mine))


def provision(text: str, provider: str | None) -> dict | None:
    """The snippet as a mental_health_provision value {type, provider} (SPEC 8 types), or None for a benefit.

    The type is only ever what the words say: scoring adds the type's words to the searchable
    text (scoring/score.fact_text), so "eap" is typed only where the page says EAP or employee
    assistance, and "therapy_stipend" only where it says therapy.
    """
    if not (MENTAL_HEALTH.search(text) or provider):
        return None
    if MENTAL_HEALTH_DAYS.search(text):
        kind = "mental_health_days"
    elif EAP_WORDS.search(text):
        kind = "carrier_eap" if CARRIERS.search(text) else "eap"
    elif provider:
        kind = "named_vendor"
    elif THERAPY.search(text) and STIPEND.search(text):
        kind = "therapy_stipend"
    else:
        kind = "general_support"
    return {"type": kind, "provider": provider}
