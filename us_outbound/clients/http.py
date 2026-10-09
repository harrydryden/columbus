"""The one HTTP path. Every API client subclasses HttpClient, so no request can skip the guard.

Tests swap RequestsTransport for tests.fakes.FakeTransport, which records every request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

from us_outbound.clients.guard import Guard, Op

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})  # resent only when the request is idempotent
RATE_LIMITED = 429  # the server refused the request unprocessed, so any request may be resent
# The longest Retry-After waited out: a longer one is handed back as the 429 it is, so a 4-minute job is not killed
# asleep (9 Oct 2026: three waits of a Slack Retry-After: 120 came to 360 s).
MAX_RETRY_WAIT = 30.0
# Headers that carry a key. requests re-sends custom ones (X-Api-Key, clay-api-key) to the host
# a redirect names, so a request carrying any of them never follows a redirect.
CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "clay-api-key"})


@dataclass
class Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float = 30.0,
        idempotent: bool = True,
    ) -> Response: ...


class ApiError(Exception):
    def __init__(self, system: str, status: int, body: Any, url: str = ""):
        self.system, self.status, self.body, self.url = system, status, body, url
        super().__init__(f"{system} HTTP {status} for {urlparse(url).path}: {str(body)[:200]}")


class TransportError(ApiError):
    """No answer at all to a read, or to a write that is safe to repeat: a timeout or a dropped connection (status 0).
    An ApiError, so each caller's per-item handling covers it as it covers a refusal (9 Oct 2026: one Clay read
    timeout got past pick_contacts' handlers and aborted the whole run). reason is the transport's own error, such as
    ReadTimeout. A write that may have happened all the same (a POST that adds a lead, sends a reply, creates a
    record) is not one: its error is raised as it came, so the caller's own recovery for an unknown outcome runs (a
    send approval left "sending" and looked up later) and nothing is taken as refused and sent again."""

    def __init__(self, system: str, error: BaseException, url: str = ""):
        self.reason = type(error).__name__
        super().__init__(system, 0, f"{self.reason}: {error}", url)


class RequestsTransport:
    """requests-based transport with retries, honouring Retry-After.

    A request that never reached the server (connect timeout) or was refused with 429 is
    retried whatever it is. A timeout, dropped connection or 5xx can come after the server
    acted, so only an idempotent request is resent then; a write that may have happened is
    left to the caller, which can re-read state before trying again.

    follow_redirects=False hands every redirect back to the caller as it came (the page reader,
    sources/pages.py, puts each hop through the guard and robots.txt itself).
    """

    def __init__(self, attempts: int = 4, backoff: float = 2.0, follow_redirects: bool = True):
        import requests

        self._session = requests.Session()
        self.attempts, self.backoff, self.follow_redirects = attempts, backoff, follow_redirects

    def send(
        self, method, url, *, headers, params=None, json=None, data=None, timeout=30.0, idempotent=True
    ) -> Response:
        import requests

        retry = RETRY_STATUSES if idempotent else {RATE_LIMITED}
        # HEAD is only used to see one redirect (Public.resolve_redirect), so never follow it;
        # and never let a redirect carry a key to another host.
        follow = (self.follow_redirects and method.upper() != "HEAD"
                  and not any(k.lower() in CREDENTIAL_HEADERS for k in headers))
        delay = self.backoff
        for attempt in range(1, self.attempts + 1):
            try:
                r = self._session.request(
                    method, url, headers=headers, params=params, json=json, data=data, timeout=timeout,
                    allow_redirects=follow,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                # ConnectTimeout subclasses both: nothing was sent, so it is always safe to resend.
                if attempt == self.attempts or not (idempotent or isinstance(exc, requests.ConnectTimeout)):
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in retry and attempt < self.attempts:
                asked = r.headers.get("Retry-After")
                wait = float(asked) if asked and asked.replace(".", "", 1).isdigit() else delay
                if wait <= MAX_RETRY_WAIT:
                    time.sleep(wait)
                    delay *= 2
                    continue
            ctype = r.headers.get("Content-Type", "")
            body: Any = r.text
            if "json" in ctype and r.content:
                try:
                    body = r.json()
                except ValueError:
                    pass
            return Response(r.status_code, body, dict(r.headers))
        raise RuntimeError("unreachable")


class HttpClient:
    """Base for API clients. Subclasses set system and base_url and describe each call as an Op."""

    system: ClassVar[str]
    base_url: ClassVar[str]
    # Read actions that spend credits: never resent after a timeout or 5xx, since a second
    # charge would miss the credit ledger (SPEC 1.5).
    paid_reads: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, guard: Guard, transport: Transport, token: str = ""):
        self.guard = guard
        self.transport = transport
        self.token = token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def request(
        self,
        method: str,
        path: str,
        op: Op,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        dry_result: Any = None,
        base_url: str | None = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """Authorize op, then send. Returns dry_result without sending when dry-run skips a write.

        raw=True returns the whole Response (status, body, headers) and leaves the status
        to the caller, who then raises for errors itself. timeout (seconds) replaces the
        transport's default for this request.
        """
        if not self.guard.authorize(self.system, op):
            return dry_result
        url = path if path.startswith("http") else (base_url or self.base_url) + path
        extra = {"timeout": timeout} if timeout is not None else {}
        idempotent = is_idempotent(method, op, self.paid_reads)
        try:
            resp = self.transport.send(
                method, url, headers=headers or self.headers(), params=params, json=json, data=data,
                idempotent=idempotent, **extra,
            )
        except OSError as exc:  # requests' ConnectionError and Timeout are OSErrors
            if op.write and not idempotent:
                raise  # it may have happened: never to be taken as refused (TransportError)
            raise TransportError(self.system, exc, url) from exc
        if raw:
            return resp
        if resp.status >= 300:  # a redirect is never followed with a key, so it is not a success
            raise ApiError(self.system, resp.status, resp.body, url)
        return resp.body


def is_idempotent(method: str, op: Op, paid_reads: frozenset[str] = frozenset()) -> bool:
    """Whether sending this request twice does no harm: not a POST that writes, not a paid read.

    GET, PUT, PATCH (which sets fields here) and DELETE are safe to repeat, as are the POSTs
    that only read (HubSpot search, Instantly lists). A POST write (a reply, a create, a lead
    add, an append) is not.
    """
    if op.action in paid_reads:
        return False
    return not (op.write and method.upper() == "POST")
