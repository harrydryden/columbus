"""Domains and email addresses (SPEC 13, Data cleaning; SPEC 1.5; SPEC 5 Roles "Skip").

  * Domain: the root domain, lower case, without www. Follow one redirect, and keep an
    alias list (table domain_aliases), so there is one account per root domain.
  * SPEC 1.5: never email a personal-domain address (gmail.com, outlook.com, yahoo.com ...).
  * SPEC 5: skip generic mailboxes (info@, hr@ ...).
  * Public bodies (.gov, .mil, a US locality's .us name) are not companies: never prospected (Harry,
    7 Oct 2026: the Town of Braintree's council president got a fintech card).

The root is taken under the public suffix list bundled with tldextract (no network fetch).
Private suffixes count as suffixes, so "acme.wixsite.com" and "beta.wixsite.com" stay two
accounts rather than merging into "wixsite.com".
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache

import tldextract

from us_outbound.clients.db import Store
from us_outbound.logs import log

MAX_ALIAS_HOPS = 5

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

# Free and ISP mailbox domains, compared on the root domain (so "nyc.rr.com" is rr.com).
PERSONAL_DOMAINS = frozenset(
    {
        # Google, Microsoft, Yahoo, AOL, Apple
        "gmail.com", "googlemail.com",
        "outlook.com", "hotmail.com", "live.com", "msn.com", "passport.com", "windowslive.com",
        "hotmail.co.uk", "live.co.uk", "outlook.co.uk", "hotmail.ca", "live.ca", "hotmail.fr", "hotmail.de",
        "hotmail.it", "hotmail.es", "live.com.au", "hotmail.com.au", "outlook.com.au",
        "yahoo.com", "ymail.com", "rocketmail.com", "yahoo.co.uk", "yahoo.ca", "yahoo.com.au", "yahoo.fr",
        "yahoo.de", "yahoo.es", "yahoo.it", "yahoo.co.in", "yahoo.com.mx", "yahoo.com.br",
        "aol.com", "aim.com", "love.com", "wow.com", "games.com",
        "icloud.com", "me.com", "mac.com", "privaterelay.appleid.com",
        # Privacy and independent providers
        "proton.me", "protonmail.com", "protonmail.ch", "pm.me",
        "gmx.com", "gmx.us", "gmx.net", "gmx.de", "gmx.co.uk", "web.de", "t-online.de",
        "mail.com", "email.com", "usa.com", "post.com", "consultant.com", "myself.com", "dr.com",
        "zoho.com", "zohomail.com", "yandex.com", "yandex.ru", "ya.ru", "mail.ru", "inbox.ru", "rambler.ru",
        "fastmail.com", "fastmail.fm", "hey.com", "tutanota.com", "tutanota.de", "tuta.io", "tuta.com",
        "mailfence.com", "hushmail.com", "runbox.com", "posteo.de", "startmail.com", "disroot.org",
        "duck.com", "simplelogin.com", "simplelogin.co", "anonaddy.com", "addy.io", "33mail.com",
        "inbox.com", "lycos.com", "excite.com", "rediffmail.com", "juno.com", "netzero.net", "netzero.com",
        "qq.com", "163.com", "126.com", "sina.com", "naver.com", "hanmail.net", "daum.net",
        "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net", "ntlworld.com", "blueyonder.co.uk",
        "shaw.ca", "rogers.com", "sympatico.ca", "bell.net", "videotron.ca", "telus.net",
        "bigpond.com", "optusnet.com.au",
        # US ISPs
        "comcast.net", "xfinity.com", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net",
        "pacbell.net", "swbell.net", "ameritech.net", "prodigy.net", "flash.net", "snet.net", "wans.net",
        "cox.net", "charter.net", "spectrum.net", "rr.com", "roadrunner.com", "twc.com", "brighthouse.com",
        "earthlink.net", "mindspring.com", "optonline.net", "optimum.net", "frontier.com", "frontiernet.net",
        "windstream.net", "centurylink.net", "centurytel.net", "embarqmail.com", "q.com", "qwest.net",
        "mediacombb.net", "suddenlink.net", "cableone.net", "rcn.com", "wowway.com", "wideopenwest.com",
        "ptd.net", "zoominternet.net", "bex.net", "sonic.net", "hughes.net", "peoplepc.com", "netscape.net",
        "cs.com", "compuserve.com", "hotpop.com", "mchsi.com", "grandecom.net", "knology.net", "atlanticbb.net",
        # Disposable
        "mailinator.com", "guerrillamail.com", "sharklasers.com", "yopmail.com", "10minutemail.com",
        "temp-mail.org", "trashmail.com", "getnada.com", "dispostable.com", "maildrop.cc",
    }
)

# Local parts that are shared inboxes rather than a person (SPEC 5 "generic mailboxes").
GENERIC_LOCAL_PARTS = frozenset(
    {
        "info", "information", "hr", "humanresources", "hello", "hi", "hey", "howdy", "contact", "contactus",
        "admin", "administrator", "administration", "office", "frontdesk", "reception", "team", "staff",
        "everyone", "all", "jobs", "job", "careers", "career", "hiring", "recruiting", "recruitment",
        "recruiter", "talent", "apply", "applications", "sales", "support", "help", "helpdesk", "service",
        "services", "customerservice", "customercare", "care", "success", "people", "peopleops",
        "benefits", "payroll", "wellbeing", "wellness", "culture", "enquiries", "enquiry", "inquiries",
        "inquiry", "general", "mail", "email", "webmaster", "postmaster", "hostmaster", "abuse",
        "noreply", "donotreply", "no-reply", "do-not-reply", "privacy", "legal", "compliance", "press",
        "media", "pr", "news", "newsletter", "marketing", "partners", "partnerships", "billing",
        "accounts", "accounting", "invoices", "finance", "orders", "shop", "store",
        "bookings", "booking", "appointments", "reservations", "events", "feedback", "security", "it",
        "tech", "dev", "operations", "ops", "social", "community", "volunteer", "volunteers",
        "donate", "donations", "giving", "membership", "members", "welcome", "studio", "hq",
    }
)
# Words that may accompany a generic word without making it a person ("us.sales", "hr-team").
_GENERIC_QUALIFIERS = frozenset({"us", "usa", "na", "dept", "department", "main", "global", "group", "desk", "the", "our"})

# Redirect targets that are never a company's own site: adopting them as the root would
# merge unrelated accounts (parked domains, site builders, social profiles, shorteners).
REDIRECT_IGNORE = frozenset(
    {
        "facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
        "tiktok.com", "pinterest.com", "yelp.com", "google.com", "goo.gl", "bit.ly", "linktr.ee",
        "medium.com", "substack.com", "github.com", "amazon.com", "etsy.com", "ebay.com",
        "godaddy.com", "secureserver.net", "afternic.com", "dan.com", "sedo.com", "sedoparking.com",
        "parkingcrew.net", "bodis.com", "hugedomains.com", "namecheap.com", "domainmarket.com", "above.com",
        "squarespace.com", "wix.com", "wixsite.com", "weebly.com", "wordpress.com", "shopify.com",
        "myshopify.com", "webflow.io", "carrd.co", "cloudflare.com", "gstatic.com",
    }
)


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    # The bundled snapshot only: no fetch of the live list, no disk cache.
    return tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(), include_psl_private_domains=True)


def host_of(url_or_domain: str | None) -> str | None:
    """The lower-case host of a URL, bare domain or email address; None if there is none."""
    s = (url_or_domain or "").strip().lower()
    if not s:
        return None
    s = _SCHEME.sub("", s)
    for ch in "/?#":
        s = s.split(ch, 1)[0]
    s = s.rsplit("@", 1)[-1]  # userinfo, or an email address
    if s.startswith("["):
        return None  # IPv6 literal
    s = s.split(":", 1)[0].strip().rstrip(".")
    if not s or any(c.isspace() for c in s):
        return None
    try:
        s = s.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = s.split(".")
    if not all(_LABEL.match(label) for label in labels):
        return None
    return s


def root_domain(url_or_domain: str | None) -> str | None:
    """The registrable root domain: "https://www.Acme.co.uk/about" -> "acme.co.uk".

    None for anything that is not a public domain name: IP addresses, "localhost",
    bare suffixes ("co.uk"), text that is not a host.
    """
    host = host_of(url_or_domain)
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    root = _extractor()(host).top_domain_under_public_suffix
    return root or None


def is_personal_domain(domain: str | None) -> bool:
    """True for free or ISP mailbox domains. Takes a domain, a URL or an email address."""
    host = host_of(domain)
    if not host:
        return False
    return host in PERSONAL_DOMAINS or root_domain(host) in PERSONAL_DOMAINS


# A US locality's own .us names (RFC 1480): ci.boston.ma.us, co.kings.ny.us, k12.ny.us, state.ny.us.
_LOCALITY_US = re.compile(r"(^|\.)(ci|co|town|vil|twp|cog|state|k12|lib|tec|cc)\.([a-z0-9-]+\.)?[a-z]{2}\.us$")


def is_public_body(domain: str | None) -> bool:
    """True for a government, military or US locality domain: a public body, not a company we email.
    Takes a domain, a URL or an email address."""
    host = host_of(domain)
    if not host:
        return False
    return host.endswith((".gov", ".mil")) or host in ("gov", "mil") or bool(_LOCALITY_US.search(host))


def is_generic_mailbox(local_part: str | None) -> bool:
    """True for shared inboxes: info@, hr@, hello@, jobs@, "us.sales@", "hr-team@" ...

    Takes the local part or a whole address. A "+tag" is ignored.
    """
    s = (local_part or "").strip().lower()
    if "@" in s:
        s = s.rsplit("@", 1)[0]
    s = s.split("+", 1)[0]
    if not s:
        return False
    if s in GENERIC_LOCAL_PARTS:
        return True
    parts = [p for p in re.split(r"[._-]+", s) if p]
    return (
        bool(parts)
        and any(p in GENERIC_LOCAL_PARTS for p in parts)
        and all(p in GENERIC_LOCAL_PARTS or p in _GENERIC_QUALIFIERS for p in parts)
    )


def follow_redirect(domain: str, resolver: Callable[[str], str | None]) -> tuple[str | None, str | None]:
    """(root, alias): follow one redirect from https://{domain}.

    resolver is ctx.clients.public.resolve_redirect in production: one HEAD, returning the
    absolute Location (or the URL itself). If the redirect lands on a different root, that
    root is the account's domain and the old one is returned as its alias. Redirects to
    parking pages, site builders and hosted sites ("acme.wixsite.com"), social profiles or
    personal mailbox domains are ignored: the company's own domain stays the root.
    """
    host = host_of(domain)
    root = root_domain(host)
    if not root:
        return None, None
    final = resolver(f"https://{host}")
    new_root = root_domain(final) if final else None
    if not new_root or new_root == root:
        return root, None
    if new_root in REDIRECT_IGNORE or is_personal_domain(new_root) or _extractor()(new_root).is_private:
        log("redirect_ignored", domain=root, target=new_root)
        return root, None
    log("redirect_followed", domain=root, target=new_root)
    return new_root, root


def canonical_domain(store: Store, domain: str | None) -> str | None:
    """The account domain for any domain: its root, mapped through the alias table (chains too)."""
    root = root_domain(domain)
    seen: set[str] = set()
    while root and root not in seen and len(seen) < MAX_ALIAS_HOPS:
        seen.add(root)
        row = store.get("domain_aliases", alias=root)
        target = root_domain(row.get("root_domain")) if row else None
        if not target or target in seen:
            break
        root = target
    return root


def record_alias(store: Store, alias: str, root: str, source: str, now: datetime) -> bool:
    """Record that alias resolves to root (domain_aliases). False if nothing was recorded.

    Both are reduced to root domains. An alias that would point to itself, directly or
    through the existing chain, is not recorded.
    """
    a, r = root_domain(alias), root_domain(root)
    if not a or not r:
        return False
    r = canonical_domain(store, r) or r
    if a == r:
        return False
    existing = store.get("domain_aliases", alias=a)
    if existing and existing.get("root_domain") == r:
        return False
    store.upsert("domain_aliases", [{"alias": a, "root_domain": r, "source": source, "added_at": now}])
    log("domain_alias", alias=a, root_domain=r, source=source)
    return True
