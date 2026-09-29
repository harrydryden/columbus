"""Test doubles: a recording HTTP transport and a context builder. No network, no GCP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from us_outbound.clients.bq import MemoryStore, Store
from us_outbound.clients.guard import Guard
from us_outbound.clients.http import Response
from us_outbound.context import Clients, Context, Secrets, boundaries_for
from us_outbound.settings.model import Settings

TEST_SHEET_ID = "sheet-test"


@dataclass
class SentRequest:
    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, Any] | None
    json: Any
    data: Any


@dataclass
class _Route:
    method: str
    url_part: str
    status: int
    body: Any
    fn: Callable[[SentRequest], Any] | None


@dataclass
class FakeTransport:
    """Records every request. Routes match on method and a URL substring; the latest route wins."""

    requests: list[SentRequest] = field(default_factory=list)
    routes: list[_Route] = field(default_factory=list)
    default_body: Any = field(default_factory=dict)

    def route(self, method: str, url_part: str, body: Any = None, status: int = 200, fn=None) -> "FakeTransport":
        self.routes.append(_Route(method.upper(), url_part, status, body, fn))
        return self

    def send(self, method, url, *, headers, params=None, json=None, data=None, timeout=30.0) -> Response:
        req = SentRequest(method.upper(), url, dict(headers), params, json, data)
        self.requests.append(req)
        for r in reversed(self.routes):
            if r.method == req.method and r.url_part in url:
                body = r.fn(req) if r.fn else r.body
                return Response(r.status, body if body is not None else {})
        return Response(200, self.default_body)

    def writes(self) -> list[SentRequest]:
        return [r for r in self.requests if r.method in {"POST", "PUT", "PATCH", "DELETE"}]


def make_context(
    settings: Settings,
    *,
    live: bool = False,
    job: str = "test",
    now: datetime | None = None,
    transport: FakeTransport | None = None,
    store: Store | None = None,
    claude_sdk: Any = None,
    google_credentials: Any = None,
) -> Context:
    guard = Guard(live=live, bounds=boundaries_for(settings, TEST_SHEET_ID))
    transport = transport or FakeTransport()
    store = store or MemoryStore(guard)
    store.guard = guard
    secrets = Secrets(guard, project="test", fetch=lambda name: f"test-{name}")
    clients = Clients(
        guard, transport, secrets, store, settings, google_credentials=google_credentials, claude_sdk=claude_sdk
    )
    return Context(
        job=job,
        settings=settings,
        store=store,
        guard=guard,
        clients=clients,
        now=now or datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        live_flag=live,
    )
