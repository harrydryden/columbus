"""The one HTTP path: what RequestsTransport resends, and that a key never follows a redirect."""

from __future__ import annotations

import pytest
import requests

from us_outbound.clients.apollo import Apollo
from us_outbound.clients.clay import Clay
from us_outbound.clients.guard import Boundaries, Guard, Op
from us_outbound.clients.http import ApiError, AuthError, HttpClient, RequestsTransport, TransportError, is_idempotent
from us_outbound.clients.public import Public
from tests.fakes import FakeTransport


class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = "{}"
        self.content = b"{}"

    def json(self):
        return {}


class StubSession:
    """Stands in for requests.Session: request() pops the next response or exception."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def transport(*outcomes) -> tuple[RequestsTransport, StubSession]:
    t = RequestsTransport(backoff=0)
    t._session = StubSession(*outcomes)
    return t, t._session


def send(t, method="POST", idempotent=True, headers=None):
    return t.send(method, "https://api.instantly.ai/api/v2/emails/reply", headers=headers or {}, idempotent=idempotent)


def test_a_write_is_not_resent_after_a_read_timeout():
    t, s = transport(requests.ReadTimeout("slow"), FakeResponse(200))
    with pytest.raises(requests.ReadTimeout):
        send(t, idempotent=False)
    assert len(s.calls) == 1


def test_a_write_is_not_resent_after_a_dropped_connection():
    t, s = transport(requests.ConnectionError("aborted"), FakeResponse(200))
    with pytest.raises(requests.ConnectionError):
        send(t, idempotent=False)
    assert len(s.calls) == 1


def test_a_read_is_resent_after_a_read_timeout():
    t, s = transport(requests.ReadTimeout("slow"), FakeResponse(200))
    assert send(t, "GET").status == 200
    assert len(s.calls) == 2


def test_a_write_that_got_a_5xx_is_returned_not_resent():
    t, s = transport(FakeResponse(504), FakeResponse(200))
    assert send(t, idempotent=False).status == 504
    assert len(s.calls) == 1


def test_a_read_that_got_a_5xx_is_resent():
    t, s = transport(FakeResponse(503), FakeResponse(200))
    assert send(t, "GET").status == 200
    assert len(s.calls) == 2


def test_a_write_is_resent_after_429():
    t, s = transport(FakeResponse(429, {"Retry-After": "0"}), FakeResponse(200))
    assert send(t, idempotent=False).status == 200
    assert len(s.calls) == 2


def test_a_long_retry_after_is_handed_back_not_slept(monkeypatch):
    slept = []
    monkeypatch.setattr("us_outbound.clients.http.time.sleep", slept.append)
    t, s = transport(FakeResponse(429, {"Retry-After": "120"}), FakeResponse(200))
    assert send(t, method="GET").status == 429 and len(s.calls) == 1 and slept == []
    t, s = transport(FakeResponse(429, {"Retry-After": "5"}), FakeResponse(200))
    assert send(t, method="GET").status == 200 and slept == [5.0]


def test_a_write_is_resent_after_a_connect_timeout():
    t, s = transport(requests.ConnectTimeout("no route"), FakeResponse(200))
    assert send(t, idempotent=False).status == 200
    assert len(s.calls) == 2


def test_a_5xx_on_a_write_becomes_an_api_error():
    class Client(HttpClient):
        system, base_url = "slack", "https://slack.com/api/"

    t = FakeTransport().route("POST", "chat.postMessage", status=503)
    client = Client(Guard(live=True, bounds=Boundaries()), t)
    with pytest.raises(ApiError):
        client.request("POST", "chat.postMessage", Op("chat.postMessage", target="#us-outbound", write=True))
    assert len(t.requests) == 1 and t.requests[0].idempotent is False


def test_no_answer_becomes_an_api_error_with_status_0():
    """9 Oct 2026: a Clay read timeout got past pick_contacts' `except ApiError` handlers and aborted the whole run."""

    class Silent:
        def send(self, method, url, **kw):
            raise requests.ReadTimeout("read timed out")

    client = Public(Guard(live=True, bounds=Boundaries()), Silent())
    with pytest.raises(ApiError) as caught:
        client.request("GET", "https://acme.com/careers", Op("site.get", target="acme.com", detail={"domain": "acme.com"}))
    exc = caught.value
    assert isinstance(exc, TransportError) and exc.status == 0 and exc.reason == "ReadTimeout"
    assert str(exc) == "public HTTP 0 for /careers: ReadTimeout: read timed out"


def test_a_write_that_got_no_answer_is_raised_as_it_came():
    """A lead add or a reply that timed out may have happened: it must not look like a refusal and be sent again."""

    class Client(HttpClient):
        system, base_url = "slack", "https://slack.com/api/"

    class Silent:
        def send(self, method, url, **kw):
            raise requests.ReadTimeout("read timed out")

    client = Client(Guard(live=True, bounds=Boundaries(alert_channel="#us-outbound")), Silent())
    with pytest.raises(requests.ReadTimeout):
        client.request("POST", "chat.postMessage", Op("chat.postMessage", target="#us-outbound", write=True))


def test_which_requests_count_as_idempotent():
    assert not is_idempotent("POST", Op("email.reply", write=True))
    assert not is_idempotent("post", Op("company.create", write=True))
    assert is_idempotent("POST", Op("company.search"))  # a read-only search POST
    assert is_idempotent("PATCH", Op("company.update", write=True))
    assert is_idempotent("GET", Op("lead.get"))
    assert not is_idempotent("POST", Op("function.run"), Clay.paid_reads)
    assert not is_idempotent("POST", Op("people.bulk_match"), Apollo.paid_reads)
    assert is_idempotent("POST", Op("people.search"), Apollo.paid_reads)  # free


def test_http_client_passes_idempotent_to_the_transport():
    t = FakeTransport()
    apollo = Apollo(Guard(bounds=Boundaries()), t, "key")
    apollo._read("people.search", json={})
    apollo._read("people.bulk_match", json={})
    assert [r.idempotent for r in t.requests] == [True, False]


# -- redirects ----------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{"X-Api-Key": "k"}, {"clay-api-key": "k"}, {"Authorization": "Bearer k"}],
)
def test_a_request_carrying_a_key_never_follows_a_redirect(headers):
    t, s = transport(FakeResponse(200))
    send(t, "POST", headers=headers)
    assert s.calls[0]["allow_redirects"] is False


def test_a_public_get_follows_redirects_and_head_never_does():
    t, s = transport(FakeResponse(200), FakeResponse(200))
    public = Public(Guard(bounds=Boundaries()), t)
    public.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    public.resolve_redirect("https://acme.com")
    assert [c["allow_redirects"] for c in s.calls] == [True, False]


@pytest.mark.parametrize("status, auth", [(401, True), (403, True), (404, False), (429, False), (500, False)])
def test_a_401_or_403_is_an_auth_error_and_still_an_api_error(status, auth):
    """9 Oct 2026: eight sources each tested `exc.status in (401, 403)` to stop on a wrong key; they catch AuthError."""
    t = FakeTransport().route("POST", "/mixed_people/api_search", status=status, body={"error": "x"})
    apollo = Apollo(Guard(bounds=Boundaries()), t, "key")
    with pytest.raises(ApiError) as caught:
        apollo._read("people.search", json={})
    assert isinstance(caught.value, AuthError) is auth and caught.value.status == status


def test_a_3xx_is_not_taken_as_success():
    t = FakeTransport().route("POST", "/mixed_people/api_search", status=307, headers={"Location": "https://elsewhere.example/"})
    apollo = Apollo(Guard(bounds=Boundaries()), t, "key")
    with pytest.raises(ApiError):
        apollo._read("people.search", json={})
