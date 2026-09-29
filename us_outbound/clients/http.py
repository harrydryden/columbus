"""The one HTTP path. Every API client subclasses HttpClient, so no request can skip the guard.

Tests swap RequestsTransport for tests.fakes.FakeTransport, which records every request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

from us_outbound.clients.guard import Guard, Op

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


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
    ) -> Response: ...


class ApiError(Exception):
    def __init__(self, system: str, status: int, body: Any, url: str = ""):
        self.system, self.status, self.body, self.url = system, status, body, url
        super().__init__(f"{system} HTTP {status} for {urlparse(url).path}: {str(body)[:200]}")


class RequestsTransport:
    """requests-based transport with retries on 429 and 5xx, honouring Retry-After."""

    def __init__(self, attempts: int = 4, backoff: float = 2.0):
        import requests

        self._session = requests.Session()
        self.attempts, self.backoff = attempts, backoff

    def send(self, method, url, *, headers, params=None, json=None, data=None, timeout=30.0) -> Response:
        import requests

        delay = self.backoff
        for attempt in range(1, self.attempts + 1):
            try:
                # HEAD is only used to see one redirect (Public.resolve_redirect), so never follow it.
                r = self._session.request(
                    method, url, headers=headers, params=params, json=json, data=data, timeout=timeout,
                    allow_redirects=method.upper() != "HEAD",
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.attempts:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in RETRY_STATUSES and attempt < self.attempts:
                wait = r.headers.get("Retry-After")
                time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else delay)
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
    ) -> Any:
        """Authorize op, then send. Returns dry_result without sending when dry-run skips a write.

        raw=True returns the whole Response (status, body, headers) and leaves the status
        to the caller, who then raises for errors itself.
        """
        if not self.guard.authorize(self.system, op):
            return dry_result
        url = path if path.startswith("http") else (base_url or self.base_url) + path
        resp = self.transport.send(
            method, url, headers=headers or self.headers(), params=params, json=json, data=data
        )
        if raw:
            return resp
        if resp.status >= 400:
            raise ApiError(self.system, resp.status, resp.body, url)
        return resp.body
