"""Source "job_posts": benefit text in the public job-board feeds (SPEC 7; Harry, 2 Oct 2026).

The four free, keyless feeds of SPEC 7, read by the read_pages job (sources/pages.py), which
finds each company's board from links on its own site:
  Greenhouse  boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  Lever       api.lever.co/v0/postings/{slug}?mode=json
  Ashby       api.ashbyhq.com/posting-api/job-board/{slug}
  Workable    apply.workable.com/api/v1/widget/accounts/{slug}?details=true
A board is the company's when:
  * its slug came from a link, embed or redirect on the company's own site (found_by "link"); or
  * the slug is the domain stem (acmecreative.com -> acmecreative; found_by "stem") and the feed
    confirms it: its company name is the account's, a posting links to the account's domain, or
    the postings name the domain or the account's name. An unconfirmed stem board is not used.
Each posting's description is read as a page (clean/pages.py) and its benefit sentences become
posting_text facts {text, posting}, with the posting's URL. open_roles stays with apollo_jobs
(docs/pipeline.md, one owning source per fact).

PHASE0-CONFIRM: the response shapes below are from each vendor's public docs (Oct 2026), not
from a live call: Greenhouse's jobs[].company_name and content (HTML, entity-escaped); Lever's
list of postings with description, lists[] and additional; Ashby's jobs[].descriptionHtml;
Workable's name and jobs[].description with details=true.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from us_outbound.clean.names import strip_entity_suffix
from us_outbound.clean.pages import Snippet, benefit_snippets, parse_html
from us_outbound.clients.public import split_url
from us_outbound.settings.conditions import ContextRules

SOURCE = "job_posts"
GREENHOUSE, LEVER, ASHBY, WORKABLE = "greenhouse", "lever", "ashby", "workable"
VENDORS = (GREENHOUSE, LEVER, ASHBY, WORKABLE)
FEEDS: dict[str, tuple[str, dict[str, str] | None]] = {
    GREENHOUSE: ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", {"content": "true"}),
    LEVER: ("https://api.lever.co/v0/postings/{slug}", {"mode": "json"}),
    ASHBY: ("https://api.ashbyhq.com/posting-api/job-board/{slug}", None),
    WORKABLE: ("https://apply.workable.com/api/v1/widget/accounts/{slug}", {"details": "true"}),
}
LINK, STEM = "link", "stem"
MAX_POSTINGS = 30  # read per feed; benefit sections repeat from posting to posting
NAME_MIN_CHARS = 7  # a one-word account name shorter than this is too common to confirm a stem board alone

_SLUG = r"([A-Za-z0-9][A-Za-z0-9_.-]{0,80})"
SLUG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (GREENHOUSE, re.compile(r"greenhouse\.io/embed/job_\w+(?:/js)?\?(?:[^\"'\s<>]*?&(?:amp;)?)?for=" + _SLUG, re.I)),
    (GREENHOUSE, re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + _SLUG, re.I)),
    (GREENHOUSE, re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/" + _SLUG, re.I)),
    (LEVER, re.compile(r"(?:jobs|api)(?:\.eu)?\.lever\.co/(?:v0/postings/)?" + _SLUG, re.I)),
    (ASHBY, re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _SLUG, re.I)),
    (ASHBY, re.compile(r"jobs\.ashbyhq\.com/" + _SLUG, re.I)),
    (WORKABLE, re.compile(r"apply\.workable\.com/(?:api/v\d/widget/accounts/)?" + _SLUG, re.I)),
    (WORKABLE, re.compile(r"(?<![\w.-])" + _SLUG + r"\.workable\.com", re.I)),
)
# Path words that are not a board's slug.
NOT_SLUGS = frozenset({"embed", "v0", "v1", "api", "j", "jobs", "www", "apply", "careers", "static", "assets", "js",
                       "posting-api", "widget", "accounts", "job-board", "privacy", "login", "signup"})


@dataclass(frozen=True)
class Board:
    vendor: str
    slug: str
    found_by: str  # LINK or STEM

    @property
    def url(self) -> str:
        return FEEDS[self.vendor][0].format(slug=self.slug)

    @property
    def params(self) -> dict[str, str] | None:
        return FEEDS[self.vendor][1]


@dataclass(frozen=True)
class Posting:
    title: str
    url: str
    html: str  # the description as HTML


def find_boards(texts: Iterable[str]) -> list[Board]:
    """The boards named in links, embeds and page source, in the order first seen, one per vendor and slug."""
    out: dict[tuple[str, str], Board] = {}
    for text in texts:
        for vendor, pattern in SLUG_PATTERNS:
            for m in pattern.finditer(text or ""):
                slug = m.group(1).rstrip(".-_")
                if not slug or slug.lower() in NOT_SLUGS:
                    continue
                out.setdefault((vendor, slug.lower()), Board(vendor, slug, LINK))
    return list(out.values())


def stem_boards(domain: str) -> list[Board]:
    """A board per vendor named after the domain stem ("acmecreative.com" -> "acmecreative"), to be confirmed."""
    stem = domain.split(".", 1)[0]
    return [Board(v, stem, STEM) for v in VENDORS] if stem and stem.lower() not in NOT_SLUGS else []


def _text(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def postings(vendor: str, body: Any) -> list[Posting]:
    """The feed's postings (at most MAX_POSTINGS), each with its description as HTML."""
    rows: list[Mapping[str, Any]]
    if vendor == LEVER:
        rows = [r for r in body if isinstance(r, Mapping)] if isinstance(body, list) else []
    else:
        jobs = body.get("jobs") if isinstance(body, Mapping) else None
        rows = [r for r in jobs if isinstance(r, Mapping)] if isinstance(jobs, list) else []
    out: list[Posting] = []
    for r in rows[:MAX_POSTINGS]:
        if vendor == GREENHOUSE:
            out.append(Posting(_text(r.get("title")), _text(r.get("absolute_url")), html.unescape(_text(r.get("content")))))
        elif vendor == LEVER:
            parts = [_text(r.get("description")) or _text(r.get("descriptionPlain"))]
            for section in r.get("lists") or ():
                if isinstance(section, Mapping):
                    parts.append(f"<h3>{html.escape(_text(section.get('text')))}</h3><ul>{_text(section.get('content'))}</ul>")
            parts.append(_text(r.get("additional")) or _text(r.get("additionalPlain")))
            out.append(Posting(_text(r.get("text")), _text(r.get("hostedUrl")) or _text(r.get("applyUrl")), "\n".join(parts)))
        elif vendor == ASHBY:
            out.append(Posting(_text(r.get("title")), _text(r.get("jobUrl")) or _text(r.get("applyUrl")),
                               _text(r.get("descriptionHtml")) or html.escape(_text(r.get("descriptionPlain")))))
        else:
            out.append(Posting(_text(r.get("title")), _text(r.get("url")) or _text(r.get("application_url")),
                               _text(r.get("description"))))
    return out


def feed_names(vendor: str, body: Any) -> list[str]:
    """The company names a feed gives (Greenhouse per posting, Workable for the account)."""
    if vendor == WORKABLE and isinstance(body, Mapping):
        return [n for n in [_text(body.get("name"))] if n]
    if vendor == GREENHOUSE and isinstance(body, Mapping) and isinstance(body.get("jobs"), list):
        return list(dict.fromkeys(n for j in body["jobs"] if isinstance(j, Mapping) and (n := _text(j.get("company_name")))))
    return []


def name_key(name: str) -> str:
    """A company name for comparing: no entity suffix, lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", strip_entity_suffix(name or "").lower())


def _on_domain(url: str, domain: str) -> bool:
    try:
        host = split_url(url)[2]
    except ValueError:
        return False
    return host == domain or host.endswith("." + domain)


def confirms(board: Board, body: Any, account: Mapping[str, Any]) -> str | None:
    """Why the feed is the account's, or None. A board found by link needs no more."""
    if board.found_by == LINK:
        return "linked from the company's site"
    domain = str(account.get("domain") or "").lower()
    names = [str(account.get(k) or "") for k in ("clean_name", "legal_name") if account.get(k)]
    keys = {name_key(n) for n in names} - {""}
    if keys & {name_key(n) for n in feed_names(board.vendor, body)}:
        return "the feed's company name"
    posts = postings(board.vendor, body)
    if domain and any(_on_domain(p.url, domain) for p in posts):
        return "a posting on the company's domain"
    text = " ".join(f"{p.title} {p.html}" for p in posts)
    if domain and re.search(r"(?<![\w.-])" + re.escape(domain) + r"(?![\w-])", text, re.I):
        return "the postings name the company's domain"
    for n in names:
        words = re.sub(r"[^A-Za-z0-9&' ]+", " ", strip_entity_suffix(n)).split()
        if words and (len(words) >= 2 or len(words[0]) >= NAME_MIN_CHARS):
            if re.search(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)", text, re.I):
                return "the postings name the company"
    return None


def posting_snippets(posts: Iterable[Posting], terms: Iterable[str] = (), context: ContextRules | None = None,
                     limit: int = 40) -> list[tuple[Snippet, Posting]]:
    """Each benefit sentence once across the feed (boilerplate repeats), with the first posting it came from."""
    out: list[tuple[Snippet, Posting]] = []
    seen: set[str] = set()
    terms = tuple(terms)
    for p in posts:
        page = parse_html(p.html, p.url or "https://invalid.example/")
        for s in benefit_snippets(page, terms, context=context):
            key = " ".join(s.text.casefold().split())
            if key not in seen:
                seen.add(key)
                out.append((s, p))
                if len(out) >= limit:
                    return out
    return out
