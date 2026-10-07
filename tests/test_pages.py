"""The careers and benefits page reader (sources/pages.py, sources/job_posts.py, clean/pages.py; Harry, 2 Oct 2026).

Page text and snippets, robots.txt, job boards found by link or by a confirmed domain stem, the
read outcomes (read, no pages found, blocked, error) and what each means for scoring, the facts
and their shapes, the queue order and refresh, the run's caps, coverage and the decision rule,
`us-outbound pages show`, the daily post, the schedule and the Signals tab. No network: every
page and feed answers on a fake transport.
"""

from __future__ import annotations

import dataclasses
import html
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, SentRequest, make_context
from us_outbound.clean import pages as text
from us_outbound.clients.guard import Boundaries, Guard, GuardViolation
from us_outbound.clients.http import Response
from us_outbound.clients.public import USER_AGENT, Public
from us_outbound.learn import daily_post
from us_outbound.ops import cli
from us_outbound.ops.schedule import by_name
from us_outbound.scoring.score import match_signal, score_account
from us_outbound.settings import load as loader
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import Focus
from us_outbound.sources import job_posts, pages

NOW = datetime(2026, 10, 5, 2, 45, tzinfo=UTC)  # Monday 5 Oct, 03:45 UK
GH_FEED = "https://boards-api.greenhouse.io/v1/boards/acmecreative/jobs"


class Web(FakeTransport):
    """Pages by exact URL: url -> (status, body, headers), or an exception to raise. Anything else is a 404."""

    def __init__(self):
        super().__init__()
        self.pages: dict[str, tuple[int, object, dict] | Exception] = {}

    def page(self, url, body="", status=200, headers=None, ctype="text/html; charset=utf-8"):
        self.pages[url] = (status, body, {"Content-Type": ctype, **(headers or {})})
        return self

    def fail(self, url, exc: Exception):
        self.pages[url] = exc
        return self

    def send(self, method, url, *, headers, params=None, json=None, data=None, timeout=30.0, idempotent=True):
        self.requests.append(SentRequest(method.upper(), url, dict(headers), params, json, data, idempotent, timeout))
        got = self.pages.get(url)
        if isinstance(got, Exception):
            raise got
        if got is None:
            return Response(404, "Not found", {"Content-Type": "text/html"})
        status, body, hdrs = got
        return Response(status, body, hdrs)

    def urls(self) -> list[str]:
        return [r.url for r in self.requests]


def account(aid="acc-1", domain="acmecreative.com", **kw) -> dict:
    return {"account_id": aid, "domain": domain, "clean_name": "Acme Creative", "status": "queued", "tier": "Standard",
            "score": 30, "industry": "Advertising agencies", "industry_group": "Marketing & Creative Agencies",
            "employees": 40, "size_band": "20-49", "hq_state": "NY", "first_seen": NOW - timedelta(days=1), **kw}


HOME = """<html><head><title>Acme</title><script src="https://cdn.example/x.js"></script></head><body>
<nav><a href="/about">About</a><a href="/careers">Careers</a></nav>
<h1>Acme Creative</h1><p>We make brands for clinics that offer therapy.</p>
<a href="/our-values">Our values</a> <a href="https://boards.greenhouse.io/acmecreative">Open roles</a>
</body></html>"""
CAREERS = """<html><body><h1>Join us</h1><p>We help people. Mental health matters to us.</p>
<h2>Benefits</h2><ul><li>Medical, dental and vision</li><li>An EAP through ComPsych</li>
<li>A free Headspace app subscription</li><li>16 weeks of paid parental leave</li></ul>
<h2>Our team</h2><p>We offer two mental health days a quarter.</p>
<a href="/careers/benefits">Full benefits</a></body></html>"""
BENEFITS = """<html><body><h1>Benefits</h1><p>Therapy sessions through Spring Health, paid by us.</p>
<p>Unlimited PTO and a sabbatical after five years.</p></body></html>"""


def greenhouse(company="Acme Creative", url="https://boards.greenhouse.io/acmecreative/jobs/1", benefits=True) -> dict:
    content = "<p>You will design things.</p>"
    if benefits:
        content += "<h3>What we offer</h3><ul><li>A $1,000 wellness stipend</li><li>Counseling through Lyra</li></ul>"
    return {"jobs": [{"id": 1, "title": "Senior Designer", "absolute_url": url, "content": html.escape(content),
                      "company_name": company}], "meta": {"total": 1}}


def site(web: Web, domain="acmecreative.com", robots="User-agent: *\nAllow: /\n", home=HOME, careers=CAREERS,
         benefits=BENEFITS) -> Web:
    web.page(f"https://{domain}/robots.txt", robots, ctype="text/plain")
    web.page(f"https://{domain}/", home)
    if careers is not None:
        web.page(f"https://{domain}/careers", careers)
    if benefits is not None:
        web.page(f"https://{domain}/careers/benefits", benefits)
    return web


@pytest.fixture
def world(default_settings, monkeypatch):
    monkeypatch.setattr(pages, "_clock", lambda: 0.0)
    web = Web()
    ctx = make_context(default_settings, transport=web, job=pages.JOB, now=NOW)
    return ctx, web


def events(ctx, aid="acc-1", source=None, fact=None) -> list[dict]:
    where = {"account_id": aid, **({"source": source} if source else {}), **({"fact": fact} if fact else {})}
    return ctx.store.select("signal_events", where)


def summary_of(ctx, aid="acc-1") -> dict:
    [row] = events(ctx, aid, pages.SOURCE, pages.SUMMARY_FACT)
    return row["value"]


# -- page text ---------------------------------------------------------------------------------------


def test_parse_html_keeps_links_and_text_blocks_without_scripts_or_navigation():
    page = text.parse_html(HOME, "https://acmecreative.com/")
    urls = [link.url for link in page.links]
    assert urls == ["https://cdn.example/x.js", "https://acmecreative.com/about", "https://acmecreative.com/careers",
                    "https://acmecreative.com/our-values", "https://boards.greenhouse.io/acmecreative"]
    assert [b.text for b in page.blocks] == [
        "Acme Creative", "We make brands for clinics that offer therapy.", "Our values Open roles"]
    assert page.blocks[0].level == 1 and "Acme" not in " ".join(b.text for b in page.blocks[1:2])  # no <title>, no nav


def test_snippets_come_from_benefits_sections_or_read_as_an_offer():
    page = text.parse_html(CAREERS, "https://acmecreative.com/careers")
    got = [s.text for s in text.benefit_snippets(page, ("ComPsych", "Headspace"))]
    assert got == ["Medical, dental and vision", "An EAP through ComPsych", "A free Headspace app subscription",
                   "16 weeks of paid parental leave", "We offer two mental health days a quarter."]
    # "Mental health matters to us." is not an offer and not under a benefits heading; "Benefits" is a heading.
    product = text.parse_html("<p>We build therapy software for counseling practices.</p>", "https://x.com/")
    assert text.benefit_snippets(product) == []
    # A benefits page counts as a benefits section from top to bottom.
    whole = text.parse_html("<p>Therapy through Talkspace.</p>", "https://x.com/benefits")
    assert text.benefit_snippets(whole) == [] and len(text.benefit_snippets(whole, whole_page=True)) == 1


def test_a_long_paragraph_is_clipped_to_300_characters_around_what_it_mentions():
    long = "Words " * 80 + "and an employee assistance program for everyone, which we offer " + "more " * 80
    [s] = text.benefit_snippets(text.parse_html(f"<h2>Perks</h2><p>{long}</p>", "https://x.com/"))
    assert len(s.text) <= 300 and "employee assistance" in s.text and s.text.startswith("…") and s.text.endswith("…")


@pytest.mark.parametrize("sentence, provider, expected", [
    ("An EAP through ComPsych", "ComPsych", {"type": "eap", "provider": "ComPsych"}),
    ("Employee assistance through Cigna", "Cigna", {"type": "carrier_eap", "provider": "Cigna"}),
    ("Two mental health days a quarter", None, {"type": "mental_health_days", "provider": None}),
    ("A therapy stipend of $100 a month", None, {"type": "therapy_stipend", "provider": None}),
    ("Counseling stipend of $100 a month", None, {"type": "general_support", "provider": None}),
    ("A free Headspace app subscription", "Headspace", {"type": "named_vendor", "provider": "Headspace"}),
    ("Medical, dental and vision", None, None),
])
def test_provision_types_say_only_what_the_words_say(sentence, provider, expected):
    assert text.provision(sentence, provider) == expected


def test_provider_names_come_from_the_signals_terms_with_their_context():
    terms = ((("Headspace", "Calm", "EAP"), {"calm": ("app", "premium")}),)
    assert text.provider_in("A calm office and an EAP", terms) is None  # "calm" needs app or premium nearby; EAP is no company
    assert text.provider_in("Calm app and Headspace", terms) == "Calm"


@pytest.mark.parametrize("robots, path, allowed", [
    ("User-agent: *\nDisallow: /careers", "/careers/benefits", False),
    ("User-agent: *\nDisallow: /careers", "/", True),
    ("User-agent: *\nDisallow: /\nUser-agent: spill-us-outbound\nAllow: /", "/careers", True),  # our own group wins
    ("User-agent: spill-us-outbound\nDisallow: /", "/", False),
    ("User-agent: googlebot\nDisallow: /", "/careers", True),  # rules for someone else
    ("User-agent: *\nDisallow: /*.pdf$\n", "/benefits.pdf", False),
    ("User-agent: *\nDisallow: /*.pdf$\n", "/benefits.pdf?x=1", True),
    ("User-agent: *\nDisallow: /careers\nAllow: /careers/benefits", "/careers/benefits", True),  # longest wins
    ("User-agent: *\nDisallow:", "/careers", True),
    ("", "/careers", True),
])
def test_robots_rules(robots, path, allowed):
    assert text.parse_robots(robots, "spill-us-outbound").allows(path) is allowed


# -- job boards --------------------------------------------------------------------------------------


def test_boards_are_found_in_links_embeds_and_page_source():
    found = job_posts.find_boards([
        "https://boards.greenhouse.io/embed/job_board?for=acmecreative&b=https://acme.com",
        "https://boards.greenhouse.io/acmecreative/jobs/123",
        "https://jobs.lever.co/acme-co/abc-123",
        '<script>var u = "https://jobs.ashbyhq.com/AcmeCo";</script>',
        "https://acme.workable.com/", "https://apply.workable.com/acme/j/ABC123/",
        "https://apply.workable.com/j/ABC123", "https://boards.greenhouse.io/embed/job_app?token=1",
    ])
    assert [(b.vendor, b.slug, b.found_by) for b in found] == [
        ("greenhouse", "acmecreative", "link"), ("lever", "acme-co", "link"), ("ashby", "AcmeCo", "link"),
        ("workable", "acme", "link"),
    ]
    assert [b.url for b in job_posts.stem_boards("acmecreative.com")] == [
        "https://boards-api.greenhouse.io/v1/boards/acmecreative/jobs", "https://api.lever.co/v0/postings/acmecreative",
        "https://api.ashbyhq.com/posting-api/job-board/acmecreative",
        "https://apply.workable.com/api/v1/widget/accounts/acmecreative",
    ]


def test_a_stem_board_is_used_only_when_the_feed_confirms_the_company():
    acct = account()
    gh = job_posts.Board("greenhouse", "acmecreative", job_posts.STEM)
    assert job_posts.confirms(gh, greenhouse(), acct) == "the feed's company name"
    assert job_posts.confirms(gh, greenhouse(company="Acme Plumbing"), acct) is None
    on_site = greenhouse(company="Acme Plumbing", url="https://www.acmecreative.com/careers/job?gh_jid=1")
    assert job_posts.confirms(gh, on_site, acct) == "a posting on the company's domain"
    lever = job_posts.Board("lever", "acmecreative", job_posts.STEM)
    post = {"text": "Designer", "hostedUrl": "https://jobs.lever.co/acmecreative/1", "description": "<p>Join us.</p>"}
    assert job_posts.confirms(lever, [post], acct) is None  # Lever names no company
    assert job_posts.confirms(lever, [{**post, "additional": "<p>Acme Creative is an equal opportunity employer.</p>"}],
                              acct) == "the postings name the company"
    assert job_posts.confirms(lever, [{**post, "additional": "<p>See acmecreative.com/about.</p>"}],
                              acct) == "the postings name the company's domain"
    short = {**acct, "clean_name": "Acme"}  # one short common word confirms nothing alone
    assert job_posts.confirms(lever, [{**post, "additional": "<p>Acme is hiring.</p>"}], short) is None
    assert job_posts.confirms(job_posts.Board("lever", "x", job_posts.LINK), [], acct) == "linked from the company's site"


def test_each_feed_s_postings_are_read_as_html():
    lever = [{"text": "Designer", "hostedUrl": "https://jobs.lever.co/a/1", "description": "<p>Design.</p>",
              "lists": [{"text": "Benefits", "content": "<li>Unlimited PTO</li>"}], "additional": ""}]
    ashby = {"jobs": [{"title": "PM", "jobUrl": "https://jobs.ashbyhq.com/a/1", "descriptionHtml": "<h2>Perks</h2><p>Gym</p>"}]}
    workable = {"name": "Acme", "jobs": [{"title": "Ops", "url": "https://apply.workable.com/a/j/1",
                                          "description": "<p>We offer a sabbatical.</p>"}]}
    for vendor, body, want in (("lever", lever, "Unlimited PTO"), ("ashby", ashby, "Gym"),
                               ("workable", workable, "We offer a sabbatical."), ("greenhouse", greenhouse(), "Counseling through Lyra")):
        posts = job_posts.postings(vendor, body)
        assert [s.text for s, _ in job_posts.posting_snippets(posts, ("Lyra",))][-1] == want, vendor
    assert job_posts.postings("greenhouse", {"jobs": "nope"}) == [] and job_posts.postings("lever", {}) == []


# -- one account, end to end -----------------------------------------------------------------------


def test_a_site_and_its_linked_board_are_read_into_facts_scoring_matches(world):
    ctx, web = world
    site(web).page(GH_FEED, greenhouse(), ctype="application/json")
    ctx.store.insert("accounts", [account()])
    out = pages.run(ctx)
    assert (out["status"], out["attempted"], out["candidates"]) == ("ok", 1, 1)
    # Only the account's own hosts and the board's feed, with our User-Agent, short timeouts and robots.txt first.
    assert web.urls() == ["https://acmecreative.com/robots.txt", "https://acmecreative.com/",
                          "https://acmecreative.com/careers", "https://acmecreative.com/careers/benefits", GH_FEED]
    assert all(r.headers["User-Agent"] == USER_AGENT and "Authorization" not in r.headers for r in web.requests)
    assert {r.timeout for r in web.requests} == {pages.PAGE_TIMEOUT, pages.FEED_TIMEOUT}
    assert web.requests[-1].params == {"content": "true"}
    assert {c.system for c in ctx.guard.calls if c.write} == {"db"}  # reads only, outside the database
    # The facts, in the shapes of the Clay Accounts function (SPEC 8).
    mh = {e["quote"]: e["value"] for e in events(ctx, source="careers_pages", fact="mental_health_provision")}
    assert mh["An EAP through ComPsych"] == {"type": "eap", "provider": "ComPsych"}
    assert mh["A free Headspace app subscription"] == {"type": "named_vendor", "provider": "Headspace"}
    assert mh["Therapy sessions through Spring Health, paid by us."] == {"type": "named_vendor", "provider": "Spring Health"}
    assert mh["We offer two mental health days a quarter."]["type"] == "mental_health_days"
    benefit = {e["quote"]: e for e in events(ctx, source="careers_pages", fact="benefit")}
    assert benefit["16 weeks of paid parental leave"]["value"] == {"item": "parental leave; leave"}  # as on the page
    assert benefit["Unlimited PTO and a sabbatical after five years."]["source_url"] == "https://acmecreative.com/careers/benefits"
    [values] = events(ctx, source="careers_pages", fact="values_page")
    assert values["value"] is True
    posting = {e["quote"]: e for e in events(ctx, source="job_posts", fact="posting_text")}
    assert posting["A $1,000 wellness stipend"]["value"] == {"text": "A $1,000 wellness stipend", "posting": "Senior Designer"}
    assert posting["Counseling through Lyra"]["source_url"] == "https://boards.greenhouse.io/acmecreative/jobs/1"
    [feed] = events(ctx, source="job_posts", fact="ats_feed")
    assert feed["value"] == {"vendor": "greenhouse", "slug": "acmecreative", "found_by": "link",
                             "confirmed_by": "linked from the company's site", "postings": 1}
    statuses = {e["source"]: e["value"] for e in events(ctx, fact="read_status")}
    assert statuses == {"careers_pages": "read", "job_posts": "read"}
    s = summary_of(ctx)
    assert (s["outcome"], s["site"], s["feed"], s["vendor"], s["run_id"]) == ("read", "read", "read", "greenhouse", ctx.run_id)
    assert s["requests"] == 5 and s["snippets"] >= 7 and s["posting_snippets"] == 2
    # The five page signals fire on these facts, from the Signals tab (no code knows their terms).
    r = score_account(account(), events(ctx), ctx.settings, NOW.date())
    matched = {m.signal.signal: m for m in r.matches}
    assert {"Mental health support listed", "EAP named", "Wellbeing app or perk named", "Progressive benefits",
            "Modern mental-health vendor named"} <= set(matched)
    assert r.tier == "Held"  # Spring Health and Lyra are competitors: held for review
    assert matched["Progressive benefits"].weight_applied == 15  # three distinct benefits, capped
    # Coverage of the run.
    assert out["run"]["outcomes"]["read"] == 1 and out["run"]["feed_share"] == 1.0
    assert out["run"]["benefits_text_share"] == 1.0
    assert out["run"]["signals"]["EAP named"] == 1 and out["run"]["signals"]["Modern mental-health vendor named"] == 1


def test_the_opener_names_nothing_the_page_said(world):
    ctx, web = world
    benefits_only = "<h2>Perks</h2><ul><li>A free Calm app subscription</li><li>Counseling for the whole team</li></ul>"
    site(web, home=f"<a href='/careers'>Careers</a>{benefits_only}", careers=None, benefits=None)
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    r = score_account(account(), events(ctx), ctx.settings, NOW.date())
    assert (r.score, r.tier, r.angle) == (35, "Standard", "Progressive employer")  # 15 + 5, + Team of 10–49 (its band)
    # Harry, 2 Oct 2026: the benefits-page lines are context too, never "your careers page mentions counseling".
    assert r.opener == "Most teams know pressure from work and home adds up, and the hard part is making help easy to use."


def test_robots_disallow_reads_nothing_and_a_failed_read_adds_no_points_and_keeps_a_hold(world):
    ctx, web = world
    site(web, robots="User-agent: *\nDisallow: /\n")
    ctx.store.insert("accounts", [account()])
    earlier = NOW - timedelta(days=200)
    ctx.store.insert("signal_events", [
        {"event_id": "old-1", "account_id": "acc-1", "source": "careers_pages", "fact": "benefit",
         "value": {"item": "unlimited PTO"}, "quote": "Unlimited PTO for everyone.", "source_url": "", "observed_at": earlier},
        {"event_id": "old-2", "account_id": "acc-1", "source": "careers_pages", "fact": "mental_health_provision",
         "value": {"type": "named_vendor", "provider": "Talkspace"}, "quote": "Therapy through Talkspace.",
         "source_url": "", "observed_at": earlier},
    ])
    pages.run(ctx)
    assert web.urls()[:1] == ["https://acmecreative.com/robots.txt"]
    assert "https://acmecreative.com/" not in web.urls()  # robots.txt said no
    s = summary_of(ctx)
    assert (s["outcome"], s["site"]) == ("blocked", "blocked")
    assert [e["value"] for e in events(ctx, source="careers_pages", fact="read_status")] == ["blocked"]
    assert events(ctx, source="careers_pages", fact="values_page") == []  # not read is not "no values page"
    r = score_account(account(), events(ctx), ctx.settings, NOW.date())
    matched = {m.signal.signal for m in r.matches}
    assert "Progressive benefits" not in matched  # not read: no points from this source
    assert "Modern mental-health vendor named" in matched and r.tier == "Held"  # a failed read never clears a Hold


@pytest.mark.parametrize("setup, outcome", [
    (lambda w: w.page("https://acmecreative.com/robots.txt", "", status=403), "blocked"),
    (lambda w: w.page("https://acmecreative.com/robots.txt", "", status=503), "error"),
    (lambda w: w.fail("https://acmecreative.com/robots.txt", TimeoutError("timed out")), "error"),
    (lambda w: site(w).page("https://acmecreative.com/", "Access denied", status=403), "blocked"),
    (lambda w: site(w).page("https://acmecreative.com/", "", status=500), "error"),
])
def test_a_site_that_refuses_or_fails_is_not_read(world, setup, outcome):
    ctx, web = world
    setup(web)
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    s = summary_of(ctx)
    assert (s["outcome"], s["site"], s["feed"]) == (outcome, outcome, "no_pages_found")
    assert [e for e in events(ctx) if e["fact"] in ("benefit", "mental_health_provision", "posting_text")] == []


def test_a_home_page_that_times_out_is_tried_on_www_once(world):
    ctx, web = world
    web.page("https://acmecreative.com/robots.txt", "", status=404)
    web.fail("https://acmecreative.com/", ConnectionError("refused"))
    web.page("https://www.acmecreative.com/robots.txt", "", status=404)
    web.page("https://www.acmecreative.com/", "<a href='/jobs'>Jobs</a>")
    web.page("https://www.acmecreative.com/jobs", "<h2>Benefits</h2><ul><li>Paid parental leave</li></ul>")
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    assert web.urls().count("https://acmecreative.com/") == 1  # one attempt
    assert summary_of(ctx)["site"] == "read"
    assert [e["source_url"] for e in events(ctx, source="careers_pages", fact="benefit")] == ["https://www.acmecreative.com/jobs"]


def test_no_careers_link_tries_the_common_paths_then_says_no_pages_found(world):
    ctx, web = world
    site(web, home="<p>Hello</p>", careers=None, benefits=None)
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    assert [u for u in web.urls() if "acmecreative.com/" in u and "robots" not in u] == [
        "https://acmecreative.com/", "https://acmecreative.com/careers", "https://acmecreative.com/jobs",
        "https://acmecreative.com/benefits"]
    s = summary_of(ctx)
    assert (s["outcome"], s["site"], s["feed"]) == ("no_pages_found", "no_pages_found", "no_pages_found")
    assert [e["value"] for e in events(ctx, source="careers_pages", fact="values_page")] == [False]


def test_redirects_are_followed_on_the_domain_and_an_ats_redirect_names_the_board(world):
    ctx, web = world
    site(web, home="<a href='/careers'>Careers</a>", careers=None, benefits=None)
    web.page("https://acmecreative.com/careers", "", status=302, headers={"Location": "https://jobs.lever.co/acmecreative"})
    lever = [{"text": "Designer", "hostedUrl": "https://jobs.lever.co/acmecreative/1",
              "lists": [{"text": "Benefits", "content": "<li>Mental health days</li>"}]}]
    web.page("https://api.lever.co/v0/postings/acmecreative", lever, ctype="application/json")
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    assert "https://jobs.lever.co/acmecreative" not in web.urls()  # never fetched: only its feed
    s = summary_of(ctx)
    assert (s["site"], s["feed"], s["vendor"], s["found_by"]) == ("no_pages_found", "read", "lever", "link")
    assert s["outcome"] == "read"
    [p] = events(ctx, source="job_posts", fact="posting_text")
    assert p["quote"] == "Mental health days"


def test_a_stem_board_needs_the_feed_to_confirm_it(world):
    ctx, web = world
    site(web, home="<p>Hello</p>", careers=None, benefits=None)
    web.page(GH_FEED, greenhouse(company="Acme Plumbing Supply"), ctype="application/json")
    web.page("https://api.ashbyhq.com/posting-api/job-board/acmecreative",
             {"jobs": [{"title": "PM", "jobUrl": "https://acmecreative.com/jobs/pm", "descriptionHtml": "<h2>Perks</h2><p>Gym</p>"}]},
             ctype="application/json")
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    tried = [u for u in web.urls() if "acmecreative.com/" not in u]
    assert tried == [GH_FEED, "https://api.lever.co/v0/postings/acmecreative",
                     "https://api.ashbyhq.com/posting-api/job-board/acmecreative"]  # stops at the confirmed one
    s = summary_of(ctx)
    assert (s["vendor"], s["found_by"], s["feed"]) == ("ashby", "stem", "read")
    [feed] = events(ctx, source="job_posts", fact="ats_feed")
    assert feed["value"]["confirmed_by"] == "a posting on the company's domain"


def test_a_linked_board_that_fails_is_not_read_but_a_stem_guess_that_fails_is_nothing(world):
    ctx, web = world
    site(web, careers=None, benefits=None)
    web.page(GH_FEED, "Slow down", status=429)
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    assert summary_of(ctx)["feed"] == "blocked"
    web2 = Web()
    ctx2 = make_context(ctx.settings, transport=web2, now=NOW)
    site(web2, home="<p>Hi</p>", careers=None, benefits=None)
    web2.page(GH_FEED, "Oops", status=500)
    ctx2.store.insert("accounts", [account()])
    pages.run(ctx2)
    assert summary_of(ctx2)["feed"] == "no_pages_found"


def test_the_guard_keeps_site_reads_on_the_account_s_domain():
    web = Web().page("https://careers.acmecreative.com/", "<p>hi</p>")
    public = Public(Guard(bounds=Boundaries()), web)
    assert public.site_get("https://careers.acmecreative.com/", "acmecreative.com").status == 200
    for url, domain in (("https://evil.example/", "acmecreative.com"), ("https://notacmecreative.com/", "acmecreative.com"),
                        ("https://acmecreative.com/", ""), ("https://com/", "com")):
        with pytest.raises(GuardViolation):
            public.site_get(url, domain)
    assert len(web.requests) == 1


def test_one_odd_site_never_stops_the_run(world, monkeypatch):
    ctx, web = world
    site(web)
    site(web, domain="other.com")
    ctx.store.insert("accounts", [account(), account("acc-2", domain="other.com", clean_name="Other")])
    real = pages.parse_html
    monkeypatch.setattr(pages, "parse_html", lambda html, url: (_ for _ in ()).throw(ValueError("bad")) if "other.com" in url
                        else real(html, url))
    out = pages.run(ctx)
    assert out["attempted"] == 2 and out["errors"] == ["other.com: ValueError"]
    assert summary_of(ctx, "acc-2")["outcome"] == "error" and summary_of(ctx)["outcome"] == "read"


# -- re-reads ----------------------------------------------------------------------------------------


def test_a_failed_re_read_keeps_the_earlier_good_read_status(world):
    ctx, web = world
    site(web)
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    later = dataclasses.replace(ctx, now=NOW + timedelta(days=181), run_id="run-2")
    web.pages.clear()
    web.page("https://acmecreative.com/robots.txt", "", status=403)
    pages.run(later)
    assert [e["value"] for e in events(ctx, source="careers_pages", fact="read_status")] == ["read"]
    latest = max(events(ctx, source="careers_pages", fact="page_read"), key=lambda e: e["observed_at"])["value"]
    assert latest["outcome"] == "blocked" and latest["notes"] == ["kept the earlier good read of careers_pages"]
    r = score_account(account(), events(ctx), ctx.settings, (NOW + timedelta(days=181)).date())
    assert "EAP named" in {m.signal.signal for m in r.matches}  # what the good read found still counts


def test_the_queue_order_and_when_an_account_is_read_again(world, default_settings):
    ctx, _ = world
    ctx.settings = dataclasses.replace(default_settings, focus=(Focus("Technology & Startups", 0.5),))
    rows = [
        account("agency-new", domain="a1.com"),
        account("tech-new", domain="t1.com", industry="Fintech", industry_group="Technology & Startups", score=10),
        account("agency-read-recently", domain="a2.com"),
        account("agency-read-long-ago", domain="a3.com"),
        account("agency-error", domain="a4.com"),
        account("held", domain="a5.com", tier="Held"),
        account("excluded", domain="a6.com", tier="Excluded"),
        account("enrolled", domain="a7.com", status="enrolled"),
        account("no-domain", domain=None),
    ]
    ctx.store.insert("accounts", rows)

    def read(aid, days, outcome):
        ctx.store.insert("signal_events", [{"event_id": f"pr-{aid}", "account_id": aid, "source": "careers_pages",
                                            "fact": "page_read", "value": {"outcome": outcome, "run_id": "r0"},
                                            "quote": "", "source_url": "", "observed_at": NOW - timedelta(days=days)}])

    read("agency-read-recently", 30, "read")
    read("agency-read-long-ago", 181, "read")
    read("agency-error", 15, "error")
    todo, _ = pages.candidates(ctx)
    # The Focus group first, then never read, then queue order; read 30 days ago waits, as do the out of queue.
    assert [a["account_id"] for a in todo] == ["tech-new", "agency-new", "agency-error", "agency-read-long-ago"]


def test_the_run_stops_at_its_cap_and_its_time_budget(world, monkeypatch):
    ctx, web = world
    for i in range(3):
        site(web, domain=f"a{i}.com")
    ctx.store.insert("accounts", [account(f"acc-{i}", domain=f"a{i}.com") for i in range(3)])
    monkeypatch.setattr(pages, "MAX_ACCOUNTS_PER_RUN", 2)
    out = pages.run(ctx)
    assert (out["attempted"], out["left_for_next_run"]) == (2, 1) and out["stopped_by"] == "the run's cap of 2 accounts"
    ticks = iter([0.0, 0.0] + [pages.RUN_SECONDS + 1.0] * 50)
    monkeypatch.setattr(pages, "_clock", lambda: next(ticks))
    out = pages.run(dataclasses.replace(ctx, run_id="run-2"))
    assert out["attempted"] == 0 and out["stopped_by"] == "the run's time budget"
    assert len(events(ctx, "acc-2", fact="page_read")) == 0  # not reached: not recorded, read next run


def test_nothing_due_reads_nothing(world):
    ctx, web = world
    out = pages.run(ctx)
    assert out["candidates"] == 0 and web.requests == []


# -- coverage, the decision, and where Harry sees them -------------------------------------------------


def _read_fact(aid, outcome, *, feed="no_pages_found", vendor=None, snippets=0, run_id="r1", when=NOW) -> dict:
    return {"event_id": f"pr-{aid}-{run_id}", "account_id": aid, "source": "careers_pages", "fact": "page_read",
            "value": {"outcome": outcome, "site": outcome, "feed": feed, "vendor": vendor, "snippets": snippets,
                      "posting_snippets": 0, "run_id": run_id}, "quote": "", "source_url": "", "observed_at": when}


def test_coverage_counts_outcomes_feeds_text_and_each_page_signal(world):
    ctx, _ = world
    rows = [_read_fact("a1", "read", feed="read", vendor="lever", snippets=2),
            _read_fact("a2", "read", snippets=1), _read_fact("a3", "blocked"), _read_fact("a4", "error"),
            _read_fact("a5", "no_pages_found", run_id="r0", when=NOW - timedelta(days=1))]
    rows += [{"event_id": "b1", "account_id": "a1", "source": "careers_pages", "fact": "mental_health_provision",
              "value": {"type": "eap", "provider": "ComPsych"}, "quote": "An EAP through ComPsych.", "source_url": "",
              "observed_at": NOW},
             {"event_id": "b2", "account_id": "a2", "source": "job_posts", "fact": "posting_text",
              "value": {"text": "Unlimited PTO", "posting": "PM"}, "quote": "Unlimited PTO", "source_url": "",
              "observed_at": NOW}]
    ctx.store.insert("signal_events", rows)
    total = pages.coverage(ctx.store, ctx.settings, NOW.date())
    assert (total.accounts, total.with_feed, total.with_text) == (5, 1, 2)
    assert dict(total.outcomes) == {"read": 2, "blocked": 1, "error": 1, "no_pages_found": 1}
    assert total.signals == {"Mental health support listed": 0, "EAP named": 1, "Modern mental-health vendor named": 0,
                             "Wellbeing app or perk named": 0, "Progressive benefits": 1}
    last = pages.coverage(ctx.store, ctx.settings, NOW.date(), run_id=pages.latest_run(ctx.store))
    assert last.accounts == 4 and last.share(last.with_text) == 0.5
    assert pages.decision(total).startswith("Decide after the first 200 accounts read: 5 so far, 40% with benefits text")


def test_the_decision_rule_after_200_accounts():
    few = pages.Coverage(accounts=200, with_text=60)
    assert pages.decision(few).startswith("Enhance the reader: 30% of 200 accounts read yield benefits text, under a third")
    enough = pages.Coverage(accounts=240, with_text=81)
    assert pages.decision(enough).startswith("Keep the reader as it is: 34% of 240")


def test_pages_show_and_the_daily_post(world, capsys):
    ctx, _ = world
    ctx.store.insert("signal_events", [_read_fact("a1", "read", feed="read", vendor="greenhouse", snippets=3)])
    ctx.store.insert("accounts", [account("waiting", domain="w.com")])
    assert cli.main(["pages", "show"], context_factory=lambda job, live, **kw: ctx) == 0
    out = capsys.readouterr().out
    assert "So far:\n  1 accounts: read 1, no pages found 0, blocked 0, error 0" in out
    assert "With a job-board feed: 1 (100%) · with benefits text: 1 (100%)" in out
    assert "Last run (" in out and "Decide after the first 200 accounts read: 1 so far" in out
    assert "Waiting to be read: 1 queue accounts" in out
    lines, nums = daily_post.build(ctx)
    post = "\n".join(lines)
    assert "Careers pages: last run 1 accounts (read 1, no pages found 0, blocked 0, error 0); feed 100%, benefits text 100%." in post
    assert nums["pages_read"] == 1 and nums["pages_benefits_text_share"] == 1.0


def test_the_daily_post_before_any_read(world):
    ctx, _ = world
    lines, _ = daily_post.build(ctx)
    assert "Careers pages: none read yet (read_pages, weekdays 03:45)." in lines


# -- the schedule and the Signals tab -----------------------------------------------------------------


def test_scheduled_on_weekdays_between_apollo_signals_and_verify_accounts():
    jobs = by_name()
    j = jobs["read_pages"]
    assert (j.cron, j.enabled, j.live, j.timeout_minutes) == ("45 3 * * 1-5", True, False, 40)
    assert pages.RUN_SECONDS < j.timeout_minutes * 60 - 5 * 60  # stops starting accounts well inside it
    assert jobs["apollo_signals"].cron == "30 3 * * 1-5" and jobs["verify_accounts"].cron == "30 4 * * 1-5"
    assert cli.JOBS["read_pages"] == "us_outbound.sources.pages:run"


def test_settings_load_brings_the_new_source_column_into_an_old_sheet(default_settings):
    build = default_tabs()["Signals"]
    old = [dict(r) for r in build]
    for r in old:
        r["source"] = r["source"].replace("clay_careers, careers_pages, job_posts", "clay_careers, job_posts")
        r["source"] = r["source"].replace("clay_careers, careers_pages", "clay_careers")
    plan = loader.plan_tab("Signals", old, build)
    page_signals = {"Mental health support listed", "EAP named", "Modern mental-health vendor named",
                    "Wellbeing app or perk named", "Progressive benefits", "Culture or values page"}
    assert set(plan.updated) == page_signals
    assert "source" in loader.BUILD_OWNS["Signals"]  # the build's source column wins, with no --take
    rows = {r["signal"]: r for r in plan.rows}
    assert rows["EAP named"]["source"] == "clay_careers, careers_pages, job_posts"
    # Until the load, settings_sync says why the page signals add nothing.
    from us_outbound.settings.validate import validate_all

    tabs = default_tabs()
    tabs["Signals"] = old
    stale, errors = validate_all(tabs)
    assert not errors["Signals"]
    assert "settings load --tab Signals --live" in pages.sheet_notice(stale)
    assert pages.sheet_notice(default_settings) is None


def test_the_reader_s_vocabulary_comes_from_the_signals_tab(default_settings):
    v = pages.vocabulary(default_settings)
    assert {"ComPsych", "Spring Health", "Headspace", "parental leave", "mental health"} <= set(v.terms)
    assert v.context["calm"] == ("app", "premium", "business", "subscription")
    assert [s.signal for s in pages.reader_signals(default_settings)] == [
        "Mental health support listed", "EAP named", "Modern mental-health vendor named", "Wellbeing app or perk named",
        "Progressive benefits"]  # Culture or values page is off by default
    eap = next(s for s in default_settings.signals if s.signal == "EAP named")
    fact = {"account_id": "a", "source": "careers_pages", "fact": "benefit", "value": {"item": "EAP"},
            "quote": "Our EAP.", "source_url": "", "observed_at": NOW}
    assert match_signal(eap, [fact], NOW.date()) is not None


# -- the home page, for the industry label check (labels.py; Harry, 7 Oct 2026) -----------------------------------


def test_parse_html_keeps_the_title_and_the_meta_description():
    page = text.parse_html('<html><head><title> Acme  Creative | Branding </title>'
                           '<meta name="Description" content="  A branding studio   for clinics. ">'
                           '<meta property="og:description" content="Second"></head><body><p>Hi</p></body></html>',
                           "https://acmecreative.com/")
    assert (page.title, page.description) == ("Acme Creative | Branding", "A branding studio for clinics.")
    assert [b.text for b in page.blocks] == ["Hi"]  # the title is still no text block
    assert text.parse_html("<p>No head</p>", "https://x.com/").title == ""


def test_the_home_page_is_kept_as_a_fact_no_signal_scores_and_the_label_check_reads(world):
    from us_outbound import labels

    ctx, web = world
    site(web, home=HOME.replace(
        "<title>Acme</title>", '<title>Acme Creative</title><meta name="description" content="Brands for clinics.">'))
    ctx.store.insert("accounts", [account()])
    pages.run(ctx)
    [home] = events(ctx, source="careers_pages", fact="home_page")
    assert home["value"] == {"title": "Acme Creative", "meta_description": "Brands for clinics.",
                             "text": "Acme Creative We make brands for clinics that offer therapy. Our values Open roles"}
    assert (home["quote"], home["source_url"]) == ("Acme Creative", "https://acmecreative.com/")
    # Not a text fact: no page signal matches it (scoring/score.TEXT_FACTS), so "therapy" on a home page scores nothing.
    alone = score_account(account(), [home], ctx.settings, NOW.date())
    assert not [m for m in alone.matches if m.signal.action == "Score" and "careers_pages" in m.signal.sources]
    m = labels.Material.of(account(), [home])
    assert m.home.startswith("Acme Creative · Brands for clinics. · Acme Creative We make brands")
    assert "Home page: Acme Creative · Brands for clinics." in m.prompt() and not m.empty


def test_a_long_home_page_is_cut_to_its_first_600_characters():
    long = "<html><body>" + "".join(f"<p>{'word ' * 30}{i}</p>" for i in range(20)) + "</body></html>"
    summary = pages.home_summary(text.parse_html(long, "https://x.com/"))
    assert set(summary) == {"text"} and len(summary["text"]) == pages.HOME_TEXT_CHARS
