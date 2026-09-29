"""Public, unauthenticated sources (SPEC 7, 13): job-post feeds, IRS BMF files, layoffs data.

get() reads only the hosts in guard.PUBLIC_HOSTS (the guard refuses any other host).
resolve_redirect() sends one HEAD to a prospect's own domain and returns where it
redirects, without following further (SPEC 13 data cleaning: "follow one redirect").
No credentials, no cookies, and nothing is ever sent but a GET or a HEAD.
"""

from __future__ import annotations

from typing import Any

from us_outbound.clients.guard import Op
from us_outbound.clients.http import HttpClient, Response
from us_outbound.logs import log

USER_AGENT = "spill-us-outbound/1.0 (+https://www.spill.chat/us)"


def split_url(url: str) -> tuple[str, str, str, str]:
    """(scheme, netloc, host, path+query) of an http(s) URL. Host is lower case, without port or userinfo."""
    scheme, sep, rest = url.strip().partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in ("http", "https"):
        raise ValueError(f"not an http(s) URL: {url!r}")
    netloc = rest
    for ch in "/?#":
        netloc = netloc.split(ch, 1)[0]
    host = netloc.rsplit("@", 1)[-1]
    host = host.split("]")[0].lstrip("[") if host.startswith("[") else host.split(":", 1)[0]
    host = host.lower().rstrip(".")
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    return scheme, netloc, host, rest[len(netloc):].split("#", 1)[0]


def absolute_location(base: str, location: str) -> str:
    """A Location header made absolute against the URL that returned it."""
    loc = location.strip()
    if loc.lower().startswith(("http://", "https://")):
        return loc
    scheme, netloc, _, path = split_url(base)
    if loc.startswith("//"):
        return f"{scheme}:{loc}"
    if loc.startswith("/"):
        return f"{scheme}://{netloc}{loc}"
    directory = path.split("?", 1)[0].rsplit("/", 1)[0] if "/" in path else ""
    return f"{scheme}://{netloc}{directory}/{loc}"


def _header(resp: Response, name: str) -> str:
    want = name.lower()
    return next((v for k, v in (resp.headers or {}).items() if k.lower() == want), "")


class Public(HttpClient):
    system = "public"
    base_url = ""

    def headers(self) -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept": "application/json, text/csv, text/plain, */*"}

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET an allowlisted public source. JSON comes back parsed, anything else as text."""
        _, _, host, path = split_url(url)
        return self.request("GET", url, Op("get", target=host, detail={"path": path.split("?", 1)[0]}), params=params)

    def resolve_redirect(self, url: str) -> str:
        """Where url redirects to (one hop, absolute), or url itself if it does not redirect or fails."""
        _, _, host, _ = split_url(url)
        try:
            resp = self._head(url, Op("resolve_redirect", target=host))
        except OSError as exc:  # requests' ConnectionError and Timeout are OSErrors
            log("redirect_unresolved", host=host, reason=type(exc).__name__)
            return url
        if resp is None or not 300 <= resp.status < 400:
            if resp is not None and resp.status >= 400:
                log("redirect_unresolved", host=host, status=resp.status)
            return url
        location = _header(resp, "Location")
        return absolute_location(url, location) if location else url

    def _head(self, url: str, op: Op) -> Response | None:
        """HEAD through the guard, returning the raw Response so the Location header is visible.

        RequestsTransport never follows redirects on HEAD, so the Location is the first hop.
        """
        return self.request("HEAD", url, op, raw=True)
