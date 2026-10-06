"""UTM parameters on the links in our emails, so a website visit or a booking can be traced to them (Harry, 6 Oct 2026).

docs/roadmap.md §4 "Weeks 2–4" item 4: "UTMs on the industry and demo links". With General utm_links = yes
(the default), render.render_step tags, in the email's HTML only:
  * the body's links to Spill's site: the industry page ({{industry_url}}), the demo page ({{demo_url}},
    General booking_page) and the US site ({{site_url}}), and any other spill.chat address the copy names;
  * the signature's website line (General site_url) and its booking line (General booking_link, Harry's HubSpot
    meetings link, which books into the same calendar as the demo page).
The tags are utm_source=us_outbound, utm_medium=email, utm_campaign=the Copy row's copy_version (its industry,
role and version, like proptech-people-v1, so a visit or a booking names the sequence that brought it; a Slack edit
of a send card keeps its card's copy_version, so it tags the same) and utm_content=step1 to step4, with
"-signature" for the signature's link, so a click on the signature can be told from the body's.

What never changes:
  * the words: links are embedded in the copy ("how Spill works for agencies"), and only the address behind
    them gains the tags, so what a prospect reads is the same with utm_links on or off. The plain-text version
    (General email_format = text, previews, QA, the Slack card) shows its addresses written out, so it keeps
    them bare rather than put a long tracking address in plain sight;
  * the signature's wording, which is fixed (CLAUDE.md; templates/copy/signature.txt): only its address can gain
    tags, and the Trustpilot line is never tagged;
  * Instantly's unsubscribe link: the campaign's step template adds it after the body (clients/instantly.py
    UNSUBSCRIBE_TAG, a placeholder Instantly swaps per lead), so render never sees it, and tag() refuses its
    host besides;
  * the copy rules (copy_rules.email_violations) and the signature's choice of line (render.signature_kind):
    both read the bare addresses (copy_markup.Rendered.links), before any tag is added;
  * the campaign drift check (registry/mailboxes.campaign_drift compares the step templates' links): the
    template holds only the {{sN_body}} variable and the unsubscribe link; the tags live in the lead's
    custom variables.
An address that already has a utm_ parameter is left alone. With utm_links = no every link stays bare.
PHASE0-CONFIRM: that HubSpot's meetings page keeps the tags it is opened with on the contact it creates (its
original-source drill-downs), and that Webflow keeps them on spill.chat; neither changes what is sent.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from us_outbound.clients.instantly import UNSUBSCRIBE_TAG
from us_outbound.settings.model import GENERAL_COPY, Settings

SOURCE = "us_outbound"
MEDIUM = "email"
SIGNATURE_CONTENT = "signature"
# An address in its parts. Read with a pattern, not urllib: only the client layer imports urllib
# (tests/test_guardrails.py), and these addresses are our own settings' and copy's.
_URL = re.compile(r"^(?P<base>https?://(?P<host>[^/?#\s]+)(?P<path>[^?#\s]*))(?:\?(?P<query>[^#\s]*))?(?P<frag>#\S*)?$",
                  re.IGNORECASE)


def campaign_for(copy_version: str) -> str:
    """utm_campaign: the copy_version as a slug ("proptech-people-v1" stays as it is; "Edited & v2" ->
    "edited-and-v2"), so the address needs no escaping."""
    words = (copy_version or "").strip() or GENERAL_COPY
    return re.sub(r"[^a-z0-9]+", "-", words.casefold().replace("&", " and ")).strip("-") or "general"


def content_for(step: int, *, signature: bool = False) -> str:
    """"step2", or "step2-signature" for the signature's link."""
    return f"step{step}" + (f"-{SIGNATURE_CONTENT}" if signature else "")


def _host(url: str) -> str:
    """The address's host in lower case, without a port or "www."; "" when it is not an http(s) address."""
    m = _URL.match(url.strip())
    return m.group("host").casefold().rsplit("@", 1)[-1].split(":", 1)[0].removeprefix("www.") if m else ""


def _page(url: str) -> str:
    """Host and path, compared loosely: no scheme, "www.", query, fragment or trailing slash."""
    m = _URL.match(url.strip())
    return f"{_host(url)}{m.group('path').rstrip('/')}" if m else ""


NEVER_HOSTS = frozenset({_host(UNSUBSCRIBE_TAG), "trustpilot.com"})


def _never(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in NEVER_HOSTS)


def ours(url: str, settings: Settings) -> bool:
    """An https address on Spill's site (General site_url's host) or Harry's booking link (General booking_link),
    and never the unsubscribe placeholder or Trustpilot."""
    host = _host(url)
    if not host or _never(host) or not url.strip().casefold().startswith("https://"):
        return False
    g = settings.general
    if g.site_url.strip() and host == _host(g.site_url):
        return True
    return bool(g.booking_link.strip()) and _page(url) == _page(g.booking_link)


def tag(url: str, settings: Settings, *, campaign: str, content: str) -> str:
    """url with the UTM parameters added when utm_links is yes and it is one of ours (see the module docstring)."""
    if not settings.general.utm_links or not ours(url, settings):
        return url
    m = _URL.match(url.strip())
    query = m.group("query") or ""
    if any(p.split("=", 1)[0].casefold().startswith("utm_") for p in query.split("&") if p):
        return url
    tags = "&".join(f"{k}={v}" for k, v in (("utm_source", SOURCE), ("utm_medium", MEDIUM),
                                            ("utm_campaign", campaign), ("utm_content", content)))
    return f"{m.group('base')}?{query + '&' if query else ''}{tags}{m.group('frag') or ''}"


def tagger(settings: Settings, *, copy_version: str, step: int,
           signature: bool = False) -> Callable[[str], str] | None:
    """The href function render gives copy_markup.render for one email's body or signature; None with utm_links = no."""
    if not settings.general.utm_links:
        return None
    campaign, content = campaign_for(copy_version), content_for(step, signature=signature)
    return lambda url: tag(url, settings, campaign=campaign, content=content)
