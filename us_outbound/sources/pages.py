"""Source "careers_pages" and the read_pages job: what a company says about its benefits (Harry, 2 Oct 2026).

Harry's decision of 2 Oct 2026: Clay's page reading moves into our own code, with no Clay
credits; Clay is kept for the email waterfall (contacts/pick.py). This job feeds the signals
that read what a company says about its benefits: Mental health support listed, EAP named,
Wellbeing app or perk named, Progressive benefits and Modern mental-health vendor named (a Hold).
Their Signals rows list careers_pages and job_posts beside clay_careers, for when Clay returns.

Each weekday at 03:45 UK, after apollo_signals (03:30) and before verify_accounts (04:30), whose
rescore scores the new facts, first a home-page pass (home_pass) for the label check: each queue account the model
was not sure of whose home page has not been read (labels.wants_home_page; Harry, 7 Oct 2026: the first audit held
half the queue on Apollo's facts alone), robots.txt and the home page only, so verify_accounts asks the model once
more with the page (labels.second_look); `labels audit --live` makes the same pass first. Then, for queue accounts
(new, queued or verified, not Excluded or Held) with a domain: the Focus tab's groups first, then
accounts never read, then queue order. An account is read again after REFRESH_DAYS, or after
RETRY_DAYS when the read failed (error). At most MAX_ACCOUNTS_PER_RUN accounts and RUN_SECONDS a run
(the home-page pass included), ACCOUNT_SECONDS an account, so a run ends well inside its 40-minute
timeout; an account not reached waits for the next run.

One account, cheapest and most structured first:
  1. Its own site (careers_pages): robots.txt for each host, then the home page, then up to
     PAGE_BUDGET pages in all: the careers, jobs and benefits pages its links point to (benefits
     pages first), or the COMMON_PATHS when the home page links none. Only the account's root
     domain and its subdomains are read (the guard refuses any other host); a redirect is
     followed only within them, PAGE_TIMEOUT seconds a page, one attempt, no JavaScript, our
     own User-Agent (clients/public.py). A path robots.txt disallows is not read.
  2. Its job board (job_posts, sources/job_posts.py): a Greenhouse, Lever, Ashby or Workable
     board linked or embedded on the pages read, or else the domain stem as a slug when the
     feed confirms it is the same company.
  3. Extraction is deterministic and free (clean/pages.py): the sentences and list items that
     mention benefits, mental health, the EAP, wellbeing, perks, leave, stipends or any term of
     the page signals on the Signals tab, with the quote and the URL. No model reads the pages.

Facts (the shapes scoring matches, and those of the Clay Accounts function, SPEC 8):
  careers_pages  benefit {item}, mental_health_provision {type, provider}, values_page,
                 read_status, and page_read: the account's read in one fact (outcome, what each
                 part found, the pages tried, the run), which coverage() counts; and home_page
                 {title, meta_description, text}: what the home page says the company is (at most
                 HOME_TEXT_CHARS of its first text), which no signal scores and the industry label
                 check reads beside Apollo's facts (labels.py; Harry, 7 Oct 2026)
  job_posts      posting_text {text, posting}, ats_feed {vendor, slug, found_by, postings},
                 read_status
Outcomes (SPEC 8): read, no_pages_found, blocked, error, per source and for the account. A
failed read (blocked or error) is "not read", never "nothing found": scoring gives that source's
facts no points and a failed read never clears a Hold (scoring/score.py). A failed re-read does
not overwrite an earlier good read_status, so what an earlier read found still counts.

Coverage (Harry: "we should enhance this functionality if we're unable to get the data we need
after testing live on the first few batches"): coverage() counts, for the last run and for every
account read so far, the outcomes, the share with a job-board feed, the share with any benefits
text and the accounts each page signal matches on these facts alone. `us-outbound pages show`
prints it and the daily post carries it, with the decision rule of docs/roadmap.md: after
DECIDE_AFTER accounts, under ENHANCE_BELOW with benefits text means enhance the reader.

Dry-run: the reads are public GETs, which happen in both modes, and facts are database writes,
which dry-run makes too (SPEC 0.3), as apollo_signals does. Nothing is written outside the database.
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import labels
from us_outbound.clean.pages import (
    ALLOW_ALL,
    Page,
    Robots,
    Snippet,
    benefit_snippets,
    parse_html,
    parse_robots,
    provider_in,
    provision,
)
from us_outbound.clients.db import new_id
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.http import ApiError, Response
from us_outbound.clients.public import ROBOTS_AGENT, absolute_location, split_url
from us_outbound.context import UK, Context
from us_outbound.enrol import focus, queue
from us_outbound.logs import log
from us_outbound.scoring.score import match_signal
from us_outbound.settings.conditions import ContextRules
from us_outbound.settings.model import Settings, Signal
from us_outbound.sources import job_posts
from us_outbound.sources.apollo_universe import OPEN_STATUSES, OUT_OF_QUEUE_TIERS

JOB = "read_pages"
SOURCE = "careers_pages"
FEED_SOURCE = job_posts.SOURCE
READER_SOURCES = (SOURCE, FEED_SOURCE)
CLAY_SOURCE = "clay_careers"
READ, NO_PAGES, BLOCKED, ERROR = "read", "no_pages_found", "blocked", "error"
OUTCOMES = (READ, NO_PAGES, BLOCKED, ERROR)
UNREAD = frozenset({BLOCKED, ERROR})  # scoring/score.UNREAD_STATUSES
SUMMARY_FACT, FEED_FACT = "page_read", "ats_feed"

MAX_ACCOUNTS_PER_RUN = 120  # about 15 s each when a site answers: 30 minutes
RUN_SECONDS = 30 * 60  # the run stops starting accounts after this; the scheduler's timeout is 40 minutes
ACCOUNT_SECONDS = 60
PAGE_BUDGET = 6  # pages of the company's own site, the home page included (robots.txt not counted)
MAX_LINKED_BOARDS = 2  # job boards tried from links on the site
PAGE_TIMEOUT = 8.0  # seconds
FEED_TIMEOUT = 15.0
MAX_REDIRECTS = 3
MAX_HTML = 1_500_000  # characters of a page read
PAGES_KEPT = 10  # pages listed in the page_read fact
HOME_FACT = "home_page"  # the home page's title, meta description and first text, for the label check (labels.py)
HOME_TEXT_CHARS = 600
HOME_PASS_ACCOUNTS = 150  # the nightly home-page pass for the label check, before the careers reads ...
HOME_PASS_SECONDS = 8 * 60  # ... inside the run's RUN_SECONDS
HOME_SECONDS = 20  # one account's home-page read: robots.txt, the home page, and www. once when there is no answer
REFRESH_DAYS = 180  # SPEC 7: re-read after 180 days
RETRY_DAYS = 14  # a read that failed with an error is tried again sooner
COMMON_PATHS = ("/careers", "/jobs", "/benefits")
BLOCK_CODES = frozenset({401, 403, 429, 451, 999})
DECIDE_AFTER = 200  # docs/roadmap.md: decide after the first 200 accounts read ...
ENHANCE_BELOW = 1 / 3  # ... enhance if fewer than about a third yield benefits text
ID_CHUNK = 1000
LIST_LIMIT = 50

BENEFITS_LINK = re.compile(r"benefit|perks?\b|total[-_ ]?rewards|well[-_ ]?being|wellness", re.I)
CAREERS_LINK = re.compile(
    r"career|\bjobs?\b|join[-_ ]?(us|the[-_ ]?team|our[-_ ]?team)|work[-_ ]?(with|for|at)[-_ ]?us|hiring|"
    r"open[-_ ]?(roles|positions)|vacanc|opportunit|life[-_ ]?at|culture|why[-_ ]?(join|work)",
    re.I,
)
VALUES_LINK = re.compile(r"\bvalues\b|\bour[-_ ]?culture\b|\bculture\b", re.I)
SKIP_EXTENSIONS = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3|css|js|xml|json|ico)$", re.I)
HTML_TYPES = ("html", "text/plain", "xml")

_clock = time.monotonic  # tests replace it


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _header(resp: Response, name: str) -> str:
    want = name.lower()
    return next((str(v) for k, v in (resp.headers or {}).items() if k.lower() == want), "")


def on_domain(url: str, domain: str) -> bool:
    """Whether url is on the root domain or one of its subdomains."""
    try:
        host = split_url(url)[2]
    except ValueError:
        return False
    return host == domain or host.endswith("." + domain)


def _path(url: str) -> str:
    try:
        return split_url(url)[3].split("?", 1)[0] or "/"
    except ValueError:
        return ""


# -- the Signals tab ---------------------------------------------------------------------------


def reader_signals(settings: Settings) -> list[Signal]:
    """The active signals that read this job's facts (the page signals)."""
    return [s for s in settings.active_signals() if set(s.sources) & set(READER_SOURCES)]


@dataclass(frozen=True)
class Vocabulary:
    """What the extraction keeps, from the Signals tab: every page signal's terms and term context rules."""

    terms: tuple[str, ...] = ()
    context: ContextRules = field(default_factory=dict)
    signal_terms: tuple[tuple[tuple[str, ...], ContextRules], ...] = ()


def vocabulary(settings: Settings) -> Vocabulary:
    sigs = [s for s in reader_signals(settings) if s.terms]
    context: dict[str, tuple[str, ...]] = {}
    for s in sigs:
        for term, words in s.context.items():
            if term != "*":  # a rule for every term of one signal is that signal's alone
                context[term] = context.get(term, ()) + words
    terms = tuple(dict.fromkeys(t for s in sigs for t in s.terms))
    return Vocabulary(terms, context, tuple((s.terms, s.context) for s in sigs))


def sheet_notice(settings: Settings) -> str | None:
    """Why the page signals can't fire on this job's facts yet: a sheet loaded before 2 Oct 2026."""
    stale = [s.signal for s in settings.active_signals() if CLAY_SOURCE in s.sources and SOURCE not in s.sources]
    if not stale:
        return None
    return (f"the Signals tab's {', '.join(stale)} do not read {SOURCE}, so what the page reader finds on company "
            "sites adds nothing to them; run `us-outbound settings load --tab Signals --live` (its source column "
            f"adds {SOURCE})")


# -- which accounts ------------------------------------------------------------------------------


def _chunks(items: Sequence[str], n: int = ID_CHUNK) -> list[Sequence[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def history(ctx: Context, account_ids: Sequence[str]) -> tuple[dict[str, tuple[datetime, str]], dict[str, set[str]]]:
    """(account_id -> (when last read, its outcome); account_id -> the sources it has a good read of)."""
    reads: dict[str, tuple[datetime, str]] = {}
    good: dict[str, set[str]] = defaultdict(set)
    for chunk in _chunks(list(account_ids)):
        for e in ctx.store.select("signal_events", {"account_id": list(chunk), "source": list(READER_SOURCES)}):
            t, aid = _ts(e.get("observed_at")), str(e.get("account_id"))
            if t is None:
                continue
            if e.get("fact") == SUMMARY_FACT and (aid not in reads or t > reads[aid][0]):
                value = e.get("value") if isinstance(e.get("value"), Mapping) else {}
                reads[aid] = (t, str(value.get("outcome") or ""))
            elif e.get("fact") == "read_status" and e.get("value") == READ:
                good[aid].add(str(e.get("source")))
    return reads, good


def due(last: tuple[datetime, str] | None, now: datetime) -> bool:
    """Never read, read REFRESH_DAYS ago, or failed with an error RETRY_DAYS ago."""
    if last is None:
        return True
    when, outcome = last
    return now - when >= timedelta(days=RETRY_DAYS if outcome == ERROR else REFRESH_DAYS)


def _queue(ctx: Context) -> list[dict]:
    """Queue accounts with a domain: new, queued or verified, not Excluded or Held."""
    return [a for a in ctx.store.select("accounts", {"status": list(OPEN_STATUSES)})
            if a.get("domain") and a.get("tier") not in OUT_OF_QUEUE_TIERS]


def candidates(ctx: Context) -> tuple[list[dict], dict[str, set[str]]]:
    """(queue accounts with a domain that are due a read, in reading order; the good reads each has)."""
    s = ctx.settings
    rows = _queue(ctx)
    reads, good = history(ctx, [a["account_id"] for a in rows])
    todo = [a for a in rows if due(reads.get(a["account_id"]), ctx.now)]
    todo.sort(key=lambda a: (focus.group_rank(s.industry_group_of(a), s), a["account_id"] in reads,
                             queue.order_key(a, s)))
    return todo, good


# -- one account ---------------------------------------------------------------------------------


@dataclass
class Fetched:
    status: str  # "ok", "missing", "not_html", "offsite", "skipped", BLOCKED or ERROR
    url: str
    html: str = ""
    code: int | None = None
    note: str = ""


def link_rank(url: str, text: str) -> int | None:
    """0 for a benefits page, 1 for a careers or jobs page, None for anything else."""
    if SKIP_EXTENSIONS.search(_path(url)):
        return None
    where = f"{_path(url)} {text}"
    if BENEFITS_LINK.search(where):
        return 0
    if CAREERS_LINK.search(where):
        return 1
    return None


class Reader:
    """One account's read: its own site within PAGE_BUDGET pages, then its job board."""

    def __init__(self, ctx: Context, account: Mapping[str, Any], vocab: Vocabulary, deadline: float):
        self.ctx, self.account, self.vocab, self.deadline = ctx, account, vocab, deadline
        self.domain = str(account.get("domain") or "").strip().lower()
        self.robots: dict[str, Robots | str] = {}
        self.requests = 0
        self.pages: list[dict] = []
        self.site_snippets: list[tuple[Snippet, str]] = []
        self.feed_snippets: list[tuple[Snippet, job_posts.Posting]] = []
        self.board: job_posts.Board | None = None
        self.confirmed_by = ""
        self.postings = 0
        self.site, self.feed = NO_PAGES, NO_PAGES
        self.values_page: bool | None = None
        self.notes: list[str] = []
        self.board_texts: list[str] = []  # links, page source and redirects, where a job board can be named
        self.fetches = 0  # pages of the site asked for, against PAGE_BUDGET
        self.not_started = False  # the run had no time left for this account: it is not recorded
        self._seen_snippets: set[str] = set()
        self.home: dict[str, str] = {}  # the home page's title, description and first text (home_summary)
        self.home_url = ""

    @property
    def outcome(self) -> str:
        """The account's outcome: read if either part read; else blocked, error, then no pages found."""
        parts = (self.site, self.feed)
        return next(o for o in (READ, BLOCKED, ERROR, NO_PAGES) if o in parts)

    def out_of_time(self) -> bool:
        return _clock() >= self.deadline

    # -- the site --

    def _robots(self, host: str) -> Robots | str:
        """The host's robots.txt rules, or BLOCKED or ERROR when it can't be read (then nothing is read there).

        No robots.txt (404 or another 4xx) allows everything; 401 or 403 is read as "keep out",
        and a 5xx, a 429 or no answer as unreachable, so nothing is read, as crawlers do.
        """
        url = f"https://{host}/robots.txt"
        resp: Response | None = None
        for _ in range(MAX_REDIRECTS + 1):
            try:
                resp = self.ctx.clients.sites.site_get(url, self.domain, timeout=PAGE_TIMEOUT)
            except OSError:
                return ERROR
            self.requests += 1
            if not 300 <= resp.status < 400:
                break
            nxt = _header(resp, "Location")
            url = absolute_location(url, nxt) if nxt else ""
            if not url or not on_domain(url, self.domain):
                return ALLOW_ALL  # robots.txt kept elsewhere: no rules for this host
        if resp is None:
            return ERROR
        if resp.status in (401, 403):
            return BLOCKED
        if resp.status == 429 or resp.status >= 500 or 300 <= resp.status < 400:
            return ERROR
        if resp.status >= 400:
            return ALLOW_ALL
        return parse_robots(resp.body if isinstance(resp.body, str) else "", ROBOTS_AGENT)

    def allowed(self, url: str) -> str | None:
        """None when robots.txt lets us read url; BLOCKED or ERROR otherwise."""
        host = split_url(url)[2]
        if host not in self.robots:
            self.robots[host] = self._robots(host)
        rules = self.robots[host]
        if isinstance(rules, str):
            return rules
        return None if rules.allows(split_url(url)[3] or "/") else BLOCKED

    def fetch(self, url: str) -> Fetched:
        """One page of the site, following redirects within the domain; recorded in self.pages."""
        self.fetches += 1
        f = self._fetch(url)
        if len(self.pages) < PAGES_KEPT:
            self.pages.append({"url": f.url, "status": f.status, **({"code": f.code} if f.code else {}),
                               **({"note": f.note} if f.note else {})})
        return f

    def _fetch(self, url: str) -> Fetched:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                split_url(url)
            except ValueError:
                return Fetched("missing", url, note="not a web address")
            if not on_domain(url, self.domain):
                self.board_texts.append(url)
                return Fetched("offsite", url, note="redirects off the company's domain")
            if self.out_of_time():
                return Fetched("skipped", url, note="time budget")
            why = self.allowed(url)
            if why:
                return Fetched(why, url, note="robots.txt")
            try:
                resp = self.ctx.clients.sites.site_get(url, self.domain, timeout=PAGE_TIMEOUT)
            except OSError as exc:  # requests' ConnectionError and Timeout are OSErrors
                return Fetched(ERROR, url, note=type(exc).__name__)
            self.requests += 1
            if 300 <= resp.status < 400:
                location = _header(resp, "Location")
                if not location:
                    return Fetched(ERROR, url, code=resp.status, note="redirect without a location")
                url = absolute_location(url, location).split("#", 1)[0]
                continue
            if resp.status in BLOCK_CODES:
                return Fetched(BLOCKED, url, code=resp.status)
            if resp.status in (404, 410):
                return Fetched("missing", url, code=resp.status)
            if resp.status >= 400:
                return Fetched(ERROR, url, code=resp.status)
            ctype = _header(resp, "Content-Type").lower()
            if ctype and not any(t in ctype for t in HTML_TYPES):
                return Fetched("not_html", url, code=resp.status)
            body = resp.body if isinstance(resp.body, str) else ""
            return Fetched("ok", url, html=body[:MAX_HTML], code=resp.status)
        return Fetched(ERROR, url, note="too many redirects")

    def _use(self, page: Page) -> list[tuple[int, str]]:
        """Take the page's snippets, job boards and values link; return its on-domain careers and benefits links."""
        self.board_texts += [link.url for link in page.links] + [page.raw]
        whole = bool(BENEFITS_LINK.search(_path(page.url)))
        for s in benefit_snippets(page, self.vocab.terms, whole_page=whole, context=self.vocab.context):
            key = " ".join(s.text.casefold().split())
            if key not in self._seen_snippets:
                self._seen_snippets.add(key)
                self.site_snippets.append((s, page.url))
        out = []
        for link in page.links:
            if not on_domain(link.url, self.domain):
                continue
            if VALUES_LINK.search(f"{_path(link.url)} {link.text}"):
                self.values_page = True
            rank = link_rank(link.url, link.text)
            if rank is not None:
                out.append((rank, link.url))
        if self.values_page is None:
            self.values_page = False
        return out

    def read_site(self) -> None:
        home = self.fetch(f"https://{self.domain}/")
        if home.status == ERROR and home.code is None and home.note not in ("robots.txt", "time budget"):
            home = self.fetch(f"https://www.{self.domain}/")  # no answer at all: some sites answer only on www
        if home.status == "skipped":
            self.not_started = self.requests == 0
            self.site = ERROR
            return
        if home.status in (BLOCKED, ERROR):
            self.site = home.status
            return
        found: list[tuple[int, str]] = []
        if home.status == "ok":
            page = parse_html(home.html, home.url)
            self.home, self.home_url = home_summary(page), home.url
            found = self._use(page)
        elif home.status == "offsite":
            self.notes.append(f"the home page redirects to {home.url}")
        queue_ = list(dict.fromkeys(u for _, u in sorted(found, key=lambda x: x[0])))  # benefits pages first
        if not queue_ and home.status in ("ok", "missing"):
            host = split_url(home.url)[2] if home.status == "ok" else self.domain
            queue_ = [f"https://{host}{p}" for p in COMMON_PATHS]
        tried = {home.url.rstrip("/"), f"https://{self.domain}"}
        results: list[Fetched] = []
        while queue_ and self.fetches < PAGE_BUDGET and not self.out_of_time():
            url = queue_.pop(0)
            if url.rstrip("/") in tried:
                continue
            tried.add(url.rstrip("/"))
            f = self.fetch(url)
            results.append(f)
            if f.status == "ok":
                tried.add(f.url.rstrip("/"))
                for rank, u in self._use(parse_html(f.html, f.url)):
                    if rank == 0 and u.rstrip("/") not in tried and u not in queue_:
                        queue_.insert(0, u)  # a benefits page linked from the careers page comes next
        if self.out_of_time() and queue_:
            self.notes.append("time budget reached before every page was read")
        statuses = {f.status for f in results}
        if "ok" in statuses or (home.status == "ok" and self.site_snippets):
            self.site = READ
        elif BLOCKED in statuses:
            self.site = BLOCKED
        elif ERROR in statuses:
            self.site = ERROR
        else:
            self.site = NO_PAGES

    # -- the job board --

    def _board(self, board: job_posts.Board) -> str:
        """READ when the feed is read and is the account's; else missing, unconfirmed, BLOCKED or ERROR."""
        try:
            body = self.ctx.clients.sites.get(board.url, params=board.params, timeout=FEED_TIMEOUT)
        except ApiError as exc:
            self.requests += 1
            if exc.status in (404, 410) or 300 <= exc.status < 400:
                return "missing"
            return BLOCKED if exc.status in BLOCK_CODES else ERROR
        except OSError:
            self.requests += 1
            return ERROR
        self.requests += 1
        why = job_posts.confirms(board, body, self.account)
        if why is None:
            return "unconfirmed"
        posts = job_posts.postings(board.vendor, body)
        self.board, self.confirmed_by, self.postings = board, why, len(posts)
        self.feed_snippets = job_posts.posting_snippets(posts, self.vocab.terms, self.vocab.context)
        return READ

    def read_feeds(self) -> None:
        linked = job_posts.find_boards(self.board_texts)
        failed: list[str] = []
        for board in linked[:MAX_LINKED_BOARDS]:
            if self.out_of_time():
                break
            got = self._board(board)
            if got == READ:
                self.feed = READ
                return
            if got in UNREAD:
                failed.append(got)
        if not linked:
            for board in job_posts.stem_boards(self.domain):
                if self.out_of_time():
                    break
                if self._board(board) == READ:  # a stem guess that fails is a guess, not a failed read
                    self.feed = READ
                    return
        self.feed = BLOCKED if BLOCKED in failed else ERROR if failed else NO_PAGES

    def read(self) -> None:
        self.read_site()
        if not self.not_started:
            self.read_feeds()

    def read_home(self) -> str:
        """The home page only, for the label check (home_pass): READ when it says what the company is (self.home);
        NO_PAGES when it says nothing or is not there; BLOCKED or ERROR; "skipped" when no time was left."""
        home = self.fetch(f"https://{self.domain}/")
        if home.status == ERROR and home.code is None and home.note not in ("robots.txt", "time budget"):
            home = self.fetch(f"https://www.{self.domain}/")  # no answer at all: some sites answer only on www
        if home.status == "ok":
            self.home, self.home_url = home_summary(parse_html(home.html, home.url)), home.url
        if self.home:
            return READ
        return home.status if home.status in (BLOCKED, ERROR, "skipped") else NO_PAGES


# -- facts ---------------------------------------------------------------------------------------


def home_summary(page: Page) -> dict[str, str]:
    """What the home page says the company is, for the label check (labels.py; Harry, 7 Oct 2026): its <title>, its
    meta description and its first text blocks, at most HOME_TEXT_CHARS of text; {} when it says nothing."""
    text = ""
    for b in page.blocks:
        if len(text) >= HOME_TEXT_CHARS:
            break
        text = f"{text} {b.text}".strip()
    out = {"title": page.title, "meta_description": page.description, "text": text[:HOME_TEXT_CHARS]}
    return {k: v for k, v in out.items() if v}


def facts(r: Reader, now: datetime, run_id: str, good: set[str]) -> list[dict]:
    """The account's facts from one read. A failed read_status never replaces an earlier good one."""
    aid = r.account["account_id"]
    rows: list[dict] = []

    def add(source: str, fact: str, value: Any, quote: str = "", url: str = "") -> None:
        rows.append({"event_id": new_id(), "account_id": aid, "source": source, "fact": fact, "value": value,
                     "quote": quote, "source_url": url, "observed_at": now})

    for s, url in r.site_snippets:
        mh = provision(s.text, provider_in(s.text, r.vocab.signal_terms))
        if mh is not None:
            add(SOURCE, "mental_health_provision", mh, s.text, url)
        else:
            add(SOURCE, "benefit", {"item": "; ".join(s.terms)}, s.text, url)
    for s, post in r.feed_snippets:
        add(FEED_SOURCE, "posting_text", {"text": s.text, "posting": post.title}, s.text, post.url)
    if r.values_page is not None and r.site in (READ, NO_PAGES):
        add(SOURCE, "values_page", r.values_page)
    if r.home:  # no Signals row reads it (scoring matches terms in TEXT_FACTS only): the label check's material
        add(SOURCE, HOME_FACT, r.home, (r.home.get("title") or r.home.get("meta_description") or "")[:300], r.home_url)
    if r.board is not None:
        add(FEED_SOURCE, FEED_FACT, {"vendor": r.board.vendor, "slug": r.board.slug, "found_by": r.board.found_by,
                                     "confirmed_by": r.confirmed_by, "postings": r.postings}, url=r.board.url)
    kept = []
    for source, outcome in ((SOURCE, r.site), (FEED_SOURCE, r.feed)):
        if outcome in UNREAD and source in good:
            kept.append(source)
            continue
        add(source, "read_status", outcome)
    add(SOURCE, SUMMARY_FACT, {
        "outcome": r.outcome, "site": r.site, "feed": r.feed,
        "vendor": r.board.vendor if r.board else None, "slug": r.board.slug if r.board else None,
        "found_by": r.board.found_by if r.board else None, "postings": r.postings,
        "snippets": len(r.site_snippets), "posting_snippets": len(r.feed_snippets),
        "pages": r.pages, "requests": r.requests, "run_id": run_id,
        "notes": r.notes + ([f"kept the earlier good read of {', '.join(kept)}"] if kept else []),
    })
    return rows


# -- the label check's home-page pass (Harry, 7 Oct 2026) ------------------------------------------------------------


def unsure(ctx: Context, accounts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The accounts whose home page the label check wants (labels.wants_home_page), in the order given."""
    ids = [str(a["account_id"]) for a in accounts]
    events: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks(ids):
        for e in ctx.store.select("signal_events", {"account_id": list(chunk), "source": [labels.JOB, SOURCE],
                                                    "fact": [labels.VERDICT_FACT, HOME_FACT]}):
            events[str(e["account_id"])].append(e)
    return [a for a in accounts if a.get("domain") and labels.wants_home_page(events[str(a["account_id"])])]


def home_pass(ctx: Context, accounts: Sequence[Mapping[str, Any]], *, seconds: float,
              limit: int | None = None) -> dict[str, int]:
    """Read the home page of each account (unsure() chose them), within seconds and limit: one home_page fact each,
    written as it is read so a stopped pass keeps its work. A page that says nothing, refuses or does not answer gets
    an empty one with the outcome as its quote, so it is not read again for the check (the careers read still reads
    it in its turn). Public GETs, so in both modes, as run() makes them. Returns the counts."""
    todo = list(accounts)[:limit] if limit is not None else list(accounts)
    counts: Counter[str] = Counter()
    if not todo:
        return {"accounts": 0, "said_what_they_do": 0, "said_nothing": 0, BLOCKED: 0, ERROR: 0, "not_reached": 0}
    vocab = vocabulary(ctx.settings)
    stop = _clock() + seconds
    for n, account in enumerate(todo):
        if _clock() >= stop:
            counts["not_reached"] += len(todo) - n
            break
        r = Reader(ctx, account, vocab, min(stop, _clock() + HOME_SECONDS))
        try:
            outcome = r.read_home()
        except GuardViolation:
            raise
        except Exception as exc:  # one odd site never stops the pass
            outcome = ERROR
            log("home_page_failed", account_id=account["account_id"], error=type(exc).__name__)
        if outcome == "skipped":
            if _clock() >= stop:  # the pass's time is up: this one and the rest wait for the next pass
                counts["not_reached"] += len(todo) - n
                break
            outcome = ERROR  # its own HOME_SECONDS ran out: no answer in time
        quote = (r.home.get("title") or r.home.get("meta_description") or "")[:300] if r.home else outcome
        ctx.store.insert("signal_events", [{
            "event_id": new_id(), "account_id": account["account_id"], "source": SOURCE, "fact": HOME_FACT,
            "value": r.home, "quote": quote, "source_url": r.home_url, "observed_at": ctx.now}])
        counts[outcome] += 1
    out = {"accounts": len(todo), "said_what_they_do": counts[READ], "said_nothing": counts[NO_PAGES],
           BLOCKED: counts[BLOCKED], ERROR: counts[ERROR], "not_reached": counts["not_reached"]}
    log("home_pass_done", run_id=ctx.run_id, **out)
    return out


# -- coverage (Harry, 2 Oct 2026: decide on numbers) ------------------------------------------------


@dataclass
class Coverage:
    accounts: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    with_feed: int = 0
    with_text: int = 0
    signals: dict[str, int] = field(default_factory=dict)
    run_at: datetime | None = None

    def share(self, n: int) -> float:
        return n / self.accounts if self.accounts else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"accounts": self.accounts, "outcomes": {o: self.outcomes.get(o, 0) for o in OUTCOMES},
                "with_feed": self.with_feed, "feed_share": round(self.share(self.with_feed), 3),
                "with_benefits_text": self.with_text, "benefits_text_share": round(self.share(self.with_text), 3),
                "signals": dict(self.signals)}

    def lines(self) -> list[str]:
        outcomes = ", ".join(f"{o.replace('_', ' ')} {self.outcomes.get(o, 0)}" for o in OUTCOMES)
        sigs = ", ".join(f"{name} {n}" for name, n in self.signals.items()) or "no page signal is active"
        return [f"{self.accounts} accounts: {outcomes}",
                f"With a job-board feed: {self.with_feed} ({self.share(self.with_feed):.0%}) · "
                f"with benefits text: {self.with_text} ({self.share(self.with_text):.0%})",
                f"Page signals matched on these facts: {sigs}"]


def coverage(store: Any, settings: Settings, today: date, *, run_id: str | None = None) -> Coverage:
    """What the reader has found: for one run (its page_read facts), or for every account read so far.

    The signal counts match each page signal against the reader's own facts only, so they say
    what this job adds, whatever Clay or another source may have.
    """
    events: dict[str, list[dict]] = defaultdict(list)
    for e in store.select("signal_events", {"source": list(READER_SOURCES)}):
        events[str(e.get("account_id"))].append(e)
    summaries: dict[str, tuple[datetime, dict]] = {}
    texted: set[str] = set()  # accounts with benefits text from any of the reads counted
    for aid, evs in events.items():
        for e in evs:
            value, t = e.get("value"), _ts(e.get("observed_at"))
            if e.get("fact") != SUMMARY_FACT or not isinstance(value, Mapping) or t is None:
                continue
            if run_id is not None and value.get("run_id") != run_id:
                continue
            if value.get("snippets") or value.get("posting_snippets"):
                texted.add(aid)
            if aid not in summaries or t > summaries[aid][0]:
                summaries[aid] = (t, dict(value))
    sigs = reader_signals(settings)
    cov = Coverage(signals={s.signal: 0 for s in sigs})
    for aid, (t, v) in summaries.items():
        cov.accounts += 1
        cov.outcomes[str(v.get("outcome"))] += 1
        cov.run_at = t if cov.run_at is None or t > cov.run_at else cov.run_at
        if v.get("feed") == READ and v.get("vendor"):
            cov.with_feed += 1
        cov.with_text += aid in texted
        for s in sigs:
            if match_signal(s, events[aid], today):
                cov.signals[s.signal] += 1
    return cov


def latest_run(store: Any) -> str | None:
    """The run_id of the newest page_read fact."""
    best: tuple[datetime, str] | None = None
    for e in store.select("signal_events", {"source": SOURCE, "fact": SUMMARY_FACT}):
        t, v = _ts(e.get("observed_at")), e.get("value")
        if t is not None and isinstance(v, Mapping) and v.get("run_id") and (best is None or t > best[0]):
            best = (t, str(v["run_id"]))
    return best[1] if best else None


def decision(cov: Coverage) -> str:
    """docs/roadmap.md's rule on the cumulative coverage."""
    if cov.accounts < DECIDE_AFTER:
        return (f"Decide after the first {DECIDE_AFTER} accounts read: {cov.accounts} so far, "
                f"{cov.share(cov.with_text):.0%} with benefits text.")
    share = cov.share(cov.with_text)
    if share < ENHANCE_BELOW:
        return (f"Enhance the reader: {share:.0%} of {cov.accounts} accounts read yield benefits text, under a third "
                "(docs/roadmap.md: Claude extraction, JavaScript rendering, or Clay's Claygent).")
    return f"Keep the reader as it is: {share:.0%} of {cov.accounts} accounts read yield benefits text, a third or more."


def report(ctx: Context) -> list[str]:
    """What `us-outbound pages show` prints: every account read so far, the last run and the decision."""
    today = ctx.today_uk()
    total = coverage(ctx.store, ctx.settings, today)
    lines = ["Careers and benefits pages (read_pages; Harry, 2 Oct 2026)", "So far:"]
    lines += [f"  {line}" for line in total.lines()]
    run_id = latest_run(ctx.store)
    if run_id:
        last = coverage(ctx.store, ctx.settings, today, run_id=run_id)
        when = f"{last.run_at.astimezone(UK):%a %d %b %H:%M}" if last.run_at else "?"
        lines.append(f"Last run ({when} UK):")
        lines += [f"  {line}" for line in last.lines()]
    lines.append(decision(total))
    waiting, _ = candidates(ctx)
    lines.append(f"Waiting to be read: {len(waiting)} queue accounts (at most {MAX_ACCOUNTS_PER_RUN} a run).")
    notice = sheet_notice(ctx.settings)
    if notice:
        lines.append(f"Note: {notice}.")
    return lines


def post_lines(ctx: Context) -> list[str]:
    """The daily post's lines: the last run and the decision (learn/daily_post.py)."""
    today = ctx.today_uk()
    total = coverage(ctx.store, ctx.settings, today)
    if not total.accounts:
        return ["Careers pages: none read yet (read_pages, weekdays 03:45)."]
    run_id = latest_run(ctx.store)
    last = coverage(ctx.store, ctx.settings, today, run_id=run_id) if run_id else Coverage()
    outcomes = ", ".join(f"{o.replace('_', ' ')} {last.outcomes.get(o, 0)}" for o in OUTCOMES)
    sigs = ", ".join(f"{name} {n}" for name, n in total.signals.items() if n) or "none yet"
    lines = [f"Careers pages: last run {last.accounts} accounts ({outcomes}); "
             f"feed {last.share(last.with_feed):.0%}, benefits text {last.share(last.with_text):.0%}.",
             f"  So far {total.accounts} accounts: feed {total.share(total.with_feed):.0%}, "
             f"benefits text {total.share(total.with_text):.0%}; signals matched: {sigs}.",
             f"  {decision(total)}"]
    notice = sheet_notice(ctx.settings)
    if notice:
        lines.append(f"  Note: {notice}.")
    return lines


# -- the job ---------------------------------------------------------------------------------------


def run(ctx: Context) -> dict:
    """The read_pages job (JOB CONTRACT: run(ctx) -> summary)."""
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run}
    start = _clock()
    stop = start + RUN_SECONDS
    todo, good = candidates(ctx)
    due_ids = {a["account_id"] for a in todo}  # a careers read reads the home page too
    summary["home_pages"] = home_pass(ctx, unsure(ctx, [a for a in _queue(ctx) if a["account_id"] not in due_ids]),
                                      seconds=HOME_PASS_SECONDS, limit=HOME_PASS_ACCOUNTS)
    if not todo:
        summary.update(status="ok", candidates=0, stopped_by="no queue account is due a read")
        log("read_pages_done", run_id=ctx.run_id, **summary)
        return summary
    vocab = vocabulary(ctx.settings)
    read, requests, rows_written = 0, 0, 0
    errors: list[str] = []
    stopped = "every account due a read was read"
    if len(todo) > MAX_ACCOUNTS_PER_RUN:
        stopped = f"the run's cap of {MAX_ACCOUNTS_PER_RUN} accounts"
    for account in todo[:MAX_ACCOUNTS_PER_RUN]:
        if _clock() >= stop:
            stopped = "the run's time budget"
            break
        r = Reader(ctx, account, vocab, min(stop, _clock() + ACCOUNT_SECONDS))
        try:
            r.read()
        except GuardViolation:
            raise
        except Exception as exc:  # one odd site never stops the run; the account is "error", tried again later
            r.site = r.feed = ERROR
            r.notes.append(f"{type(exc).__name__}: {str(exc)[:200]}")
            errors.append(f"{account.get('domain')}: {type(exc).__name__}")
            log("read_pages_account_failed", account_id=account["account_id"], error=type(exc).__name__)
        if r.not_started:
            stopped = "the run's time budget"
            break
        rows = facts(r, ctx.now, ctx.run_id, good.get(account["account_id"], set()))
        ctx.store.insert("signal_events", rows)  # each account as it is read, so a stopped run keeps its work
        read += 1
        requests += r.requests
        rows_written += len(rows)
        log("read_pages_account", account_id=account["account_id"], outcome=r.outcome, site=r.site, feed=r.feed,
            requests=r.requests, snippets=len(r.site_snippets) + len(r.feed_snippets))
    this_run = coverage(ctx.store, ctx.settings, ctx.today_uk(), run_id=ctx.run_id)
    total = coverage(ctx.store, ctx.settings, ctx.today_uk())
    summary.update(
        status="ok", stopped_by=stopped, candidates=len(todo), attempted=read, left_for_next_run=len(todo) - read,
        requests=requests, facts=rows_written, seconds=round(_clock() - start, 1),
        run=this_run.as_dict(), cumulative=total.as_dict(), decision=decision(total),
        sheet_notice=sheet_notice(ctx.settings), errors=errors[:LIST_LIMIT],
    )
    log("read_pages_done", run_id=ctx.run_id, **summary)
    return summary
