"""The Copy tab's small markup, and how it becomes an email (Harry, 30 Sep 2026).

Copy is written in the sheet as readable text; this module turns it into the HTML Instantly
sends (email_format = html), a plain-text version (email_format = text, and previews), and
the words alone (anchor text, no URLs), which the copy rules and word counts read.

  * A blank line starts a new paragraph; a single line break stays a line break.
  * Lines starting "- " (or "* ", "• ") are a bulleted list; a heading may sit on the line above.
  * **bold** makes bold text (the headings of the long-form email).
  * [anchor text](link) is a link. The link is a variable ({{demo_url}}, {{industry_url}})
    or a full https:// address; copy_rules decides which are allowed.
  * {{variables}} are filled with the lead's values. The markup is read from the copy
    before any value goes in, so a company called "[Acme](x)" stays text, and every value
    is escaped for HTML.
  * An optional variable ({{opener}}, {{legal_overlay}}) alone on its line disappears with
    its line when it is empty.

parse() reports what it cannot read (a broken link, stray **, HTML typed into the copy, a
one-item list) as problems; render() fills variables and returns the three versions.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_LINK = re.compile(r"\[([^\[\]\n]*)\]\(([^()\s]*)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BULLET = re.compile(r"^\s*(?:[-*•])\s+")
_HTML_TAG = re.compile(r"</?\s*[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
_BARE_URL = re.compile(r"(?<![\w/])(?:https?://|www\.)[^\s<>()\[\]]+", re.IGNORECASE)
_URLISH = re.compile(r"^(?:https?://|www\.)|\.(?:com|org|chat|net|io)(?:/|$)", re.IGNORECASE)
MIN_BULLETS = 2


@dataclass(frozen=True)
class Link:
    text: str  # anchor text as written (variables unfilled)
    url: str  # as written: "{{demo_url}}" or a full address


@dataclass(frozen=True)
class Block:
    kind: str  # "p" (lines) or "ul" (items)
    lines: tuple[str, ...]


@dataclass
class Rendered:
    html: str
    text: str
    words: str  # anchor text only, no URLs or markup: what the copy rules read
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, filled URL)
    problems: list[str] = field(default_factory=list)


def _blocks(source: str) -> list[Block]:
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    out: list[Block] = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = [line.rstrip() for line in chunk.split("\n") if line.strip()]
        # A run of bullet lines is a list; the lines around it (a heading above) are paragraphs.
        run: list[str] = []
        kind = ""
        for line in lines:
            k = "ul" if _BULLET.match(line) else "p"
            if run and k != kind:
                out.append(_block(kind, run))
                run = []
            kind = k
            run.append(line)
        if run:
            out.append(_block(kind, run))
    return out


def _block(kind: str, lines: list[str]) -> Block:
    if kind == "ul":
        return Block("ul", tuple(_BULLET.sub("", line, count=1).strip() for line in lines))
    return Block("p", tuple(line.strip() for line in lines))


def links(source: str) -> list[Link]:
    """Every [text](link) in the copy, as written."""
    return [Link(m.group(1).strip(), m.group(2).strip()) for m in _LINK.finditer(source)]


def parse(source: str) -> tuple[list[Block], list[str]]:
    """(the blocks, every problem reading the markup)."""
    problems: list[str] = []
    for m in _HTML_TAG.finditer(source):
        problems.append(f'has HTML ("{m.group(0)}"); write the copy markup instead')
    blocks = _blocks(source)
    for b in blocks:
        if b.kind == "ul" and len(b.lines) < MIN_BULLETS:
            problems.append(f'has a list with one bullet ("{b.lines[0][:40]}"); a list needs at least {MIN_BULLETS}')
        for line in b.lines:
            rest = _LINK.sub("", line)
            if "](" in rest or re.search(r"\[[^\]]*$", rest) or re.search(r"^[^\[]*\]", rest):
                problems.append(f'has a broken link in "{line[:60]}"; write [anchor text](link)')
            if rest.count("**") % 2:
                problems.append(f'has an unmatched ** in "{line[:60]}"')
            for m in _BARE_URL.finditer(rest):
                problems.append(f'has the bare address "{m.group(0)}"; write [anchor text]({m.group(0)})')
            for link in (Link(m.group(1).strip(), m.group(2).strip()) for m in _LINK.finditer(line)):
                if not link.text:
                    problems.append(f"has a link with no anchor text ({link.url or 'no link'})")
                elif _URLISH.search(link.text):
                    problems.append(f'has a link whose anchor text is an address ("{link.text}"); use words')
                if not link.url:
                    problems.append(f'has the link "{link.text}" with no address')
    return blocks, list(dict.fromkeys(problems))


def drop_empty_optional(source: str, values: Mapping[str, str], optional: Iterable[str]) -> str:
    """Remove each line that holds nothing but an optional variable whose value is empty."""
    names = {n for n in optional if not str(values.get(n) or "").strip()}
    if not names:
        return source
    keep = []
    for line in source.replace("\r\n", "\n").split("\n"):
        m = re.fullmatch(r"\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*", line)
        if m and m.group(1) in names:
            continue
        keep.append(line)
    return "\n".join(keep)


class _Filler:
    """Fills {{variables}}; collects unknown and empty ones (which stay visible)."""

    def __init__(self, values: Mapping[str, str], optional: frozenset[str]):
        self.values, self.optional = values, optional
        self.problems: list[str] = []

    def value(self, name: str) -> str | None:
        if name not in self.values:
            self.problems.append(f"has the unknown variable {{{{{name}}}}}")
            return None
        v = " ".join(str(self.values[name] or "").split())
        if not v and name not in self.optional:
            self.problems.append(f"has the empty variable {{{{{name}}}}}")
            return None
        return v

    def fill(self, text: str, escape: bool) -> str:
        def one(m: re.Match[str]) -> str:
            v = self.value(m.group(1))
            if v is None:
                return html.escape(m.group(0)) if escape else m.group(0)
            return html.escape(v, quote=False) if escape else v

        out, last = [], 0
        for m in VARIABLE.finditer(text):
            out.append(html.escape(text[last : m.start()], quote=False) if escape else text[last : m.start()])
            out.append(one(m))
            last = m.end()
        out.append(html.escape(text[last:], quote=False) if escape else text[last:])
        return "".join(out)


def _inline(line: str, f: _Filler, fmt: str, found: list[tuple[str, str]],
            href: Callable[[str], str] | None = None) -> str:
    """One line of copy in fmt: "html", "text" (links as "anchor (url)") or "words" (anchor only).

    href, when given, maps a filled address to the one the HTML link carries (enrol/utm.py adds UTM tags);
    found, the text and the words keep the address as written, so the copy rules read it bare.
    """
    out, last = [], 0
    for m in _LINK.finditer(line):
        out.append(_bold(line[last : m.start()], f, fmt))
        anchor = _bold(m.group(1).strip(), f, fmt)
        url = f.fill(m.group(2).strip(), escape=False)
        if fmt == "html":
            found.append((f.fill(m.group(1).strip(), escape=False), url))
            shown = href(url) if href is not None else url
            out.append(f'<a href="{html.escape(shown, quote=True)}">{anchor}</a>')
        elif fmt == "text":
            out.append(f"{anchor} ({url})")
        else:
            out.append(anchor)
        last = m.end()
    out.append(_bold(line[last:], f, fmt))
    return "".join(out)


def _bold(text: str, f: _Filler, fmt: str) -> str:
    out, last = [], 0
    for m in _BOLD.finditer(text):
        out.append(f.fill(text[last : m.start()], escape=fmt == "html"))
        inner = f.fill(m.group(1), escape=fmt == "html")
        out.append(f"<strong>{inner}</strong>" if fmt == "html" else inner)
        last = m.end()
    out.append(f.fill(text[last:], escape=fmt == "html"))
    return "".join(out)


def render(source: str, values: Mapping[str, str], *, optional: Iterable[str] = (),
           href: Callable[[str], str] | None = None) -> Rendered:
    """The copy with its variables filled, as HTML, plain text and words, with every problem.

    href: what each link's address becomes in the HTML (UTM tags, enrol/utm.py); links keeps them bare.
    """
    opt = frozenset(optional)
    source = drop_empty_optional(source, values, opt)
    blocks, problems = parse(source)
    found: list[tuple[str, str]] = []
    versions: dict[str, str] = {}
    fillers: list[_Filler] = []
    for fmt in ("html", "text", "words"):
        f = _Filler(values, opt)
        fillers.append(f)
        parts = []
        for b in blocks:
            lines = [_inline(line, f, fmt, found if fmt == "html" else [], href if fmt == "html" else None)
                     for line in b.lines]
            if fmt == "html":
                parts.append("<ul>" + "".join(f"<li>{x}</li>" for x in lines) + "</ul>" if b.kind == "ul"
                             else "<p>" + "<br>".join(lines) + "</p>")
            else:
                parts.append("\n".join(f"• {x}" for x in lines) if b.kind == "ul" else "\n".join(lines))
        versions[fmt] = "".join(parts) if fmt == "html" else "\n\n".join(parts)
    problems += fillers[0].problems
    return Rendered(versions["html"], versions["text"], versions["words"], found, list(dict.fromkeys(problems)))


def word_count(words: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$][A-Za-z0-9'’$.,-]*", words))


def fill_text(text: str, values: Mapping[str, str], *, optional: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Plain text (a subject) with its {{variables}} filled; (text, problems). No markup is read."""
    f = _Filler(values, frozenset(optional))
    return " ".join(f.fill(text, escape=False).split()), f.problems
