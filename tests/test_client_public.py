"""Public sources: allowlisted GETs only; one-hop HEAD redirect resolution."""

import pytest

from tests.fakes import FakeTransport, SentRequest
from us_outbound.clients.guard import Boundaries, Guard, GuardViolation
from us_outbound.clients.http import ApiError, Response
from us_outbound.clients.public import Public, absolute_location, split_url


class HeadTransport:
    """Answers every request with one status and header set; records requests."""

    def __init__(self, status=200, headers=None, error: Exception | None = None):
        self.status, self.headers, self.error = status, headers or {}, error
        self.requests: list[SentRequest] = []

    def send(self, method, url, *, headers, params=None, json=None, data=None, timeout=30.0, idempotent=True):
        self.requests.append(SentRequest(method, url, dict(headers), params, json, data))
        if self.error:
            raise self.error
        return Response(self.status, "", dict(self.headers))


def make(transport=None, live=False):
    t = transport or FakeTransport()
    guard = Guard(live=live, bounds=Boundaries())
    return Public(guard, t), t, guard


def test_get_allowlisted_host():
    public, t, guard = make()
    t.route("GET", "boards-api.greenhouse.io", body={"jobs": [{"id": 1}]})
    out = public.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs", params={"content": "true"})
    assert out == {"jobs": [{"id": 1}]}
    [req] = t.requests
    assert (req.method, req.url, req.params) == ("GET", "https://boards-api.greenhouse.io/v1/boards/acme/jobs", {"content": "true"})
    assert "Authorization" not in req.headers and req.headers["User-Agent"].startswith("spill-us-outbound")
    [call] = guard.calls
    assert (call.action, call.target, call.write, call.sent) == ("get", "boards-api.greenhouse.io", False, True)


@pytest.mark.parametrize("url", [
    "https://evil.example/jobs",
    "https://api.lever.co.evil.example/v0/postings/acme",
    "https://user@evil.example:8443/x",
    "http://www.acme.example/careers",
])
def test_unlisted_host_refused_before_any_request(url):
    public, t, _ = make()
    with pytest.raises(GuardViolation):
        public.get(url)
    assert t.requests == []


def test_get_http_errors_raise():
    public, t, _ = make()
    t.route("GET", "api.lever.co", status=404, body="Not found")
    with pytest.raises(ApiError):
        public.get("https://api.lever.co/v0/postings/acme?mode=json")


@pytest.mark.parametrize("url", [
    "ftp://www.irs.gov/file.csv", "www.irs.gov/file.csv", "https:///nohost",
    # requests would connect to evil.example, while the host after "@" is allowlisted
    "https://evil.example\\@boards-api.greenhouse.io/v1/boards/x/jobs",
    "https://boards-api.greenhouse.io /v1/boards/x/jobs",
])
def test_non_http_urls_rejected(url):
    public, t, _ = make()
    with pytest.raises(ValueError):
        public.get(url)
    assert t.requests == []


@pytest.mark.parametrize("location, expected", [
    ("https://www.acme.example/", "https://www.acme.example/"),
    ("/us/home", "https://acme.example/us/home"),
    ("//cdn.acme.example/x", "https://cdn.acme.example/x"),
    ("welcome", "https://acme.example/path/welcome"),
])
def test_resolve_redirect_one_hop_absolute(location, expected):
    public, t, guard = make(HeadTransport(301, {"location": location}))
    assert public.resolve_redirect("https://acme.example/path/page?x=1") == expected
    [req] = t.requests
    assert req.method == "HEAD"
    [call] = guard.calls
    assert (call.action, call.target, call.write) == ("resolve_redirect", "acme.example", False)


@pytest.mark.parametrize("transport", [
    HeadTransport(200),
    HeadTransport(302, {}),  # redirect without a Location
    HeadTransport(405),
    HeadTransport(503),
    HeadTransport(error=ConnectionError("refused")),
    HeadTransport(error=TimeoutError("slow")),
])
def test_resolve_redirect_returns_the_url_when_there_is_no_redirect(transport):
    public, t, _ = make(transport)
    assert public.resolve_redirect("https://acme.example") == "https://acme.example"
    assert len(t.requests) == 1


def test_split_and_join_helpers():
    assert split_url("HTTPS://User@WWW.IRS.gov:443/pub/eo.csv?x=1#frag") == ("https", "User@WWW.IRS.gov:443", "www.irs.gov", "/pub/eo.csv?x=1")
    assert absolute_location("http://a.example", "b") == "http://a.example/b"
