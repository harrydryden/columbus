"""The outside watchdog's pinger (Harry, 7 Oct 2026): a GET of a Healthchecks.io ping URL, or of its /fail form.

Every Slack alert comes from inside the Railway worker, so a dead worker, database or Slack token says
nothing. heartbeat_check therefore ends each hourly run by pinging US_OUTBOUND_WATCHDOG_URL
(ops/watchdog.py decides which form): Healthchecks.io emails Harry when a ping is late or says /fail.

The URL holds the check's secret id, so it is never logged or put in an error: a failed ping is reported
by its HTTP status or the exception's type alone. The guard allows a GET of exactly the URL the job was
given, or its /fail form (Guard._check_watchdog), and the request carries no key header.
"""

from __future__ import annotations

from typing import Any

from us_outbound.clients.guard import Op
from us_outbound.clients.http import HttpClient

TIMEOUT_SECONDS = 10.0  # a ping is tiny; a slow Healthchecks must not hold heartbeat_check up
FAIL_SUFFIX = "/fail"


class PingFailed(Exception):
    """Healthchecks did not take the ping. The message names the HTTP status, never the URL."""


class Watchdog(HttpClient):
    system = "watchdog"
    base_url = ""

    def __init__(self, guard: Any, transport: Any, url: str):
        super().__init__(guard, transport, "")
        self.url = url.rstrip("/")

    def headers(self) -> dict[str, str]:
        return {"User-Agent": "us-outbound heartbeat_check"}

    def ping(self, *, fail: bool = False) -> int:
        """GET the ping URL, or its /fail form; returns the HTTP status. Raises PingFailed on an answer over 299."""
        url = self.url + (FAIL_SUFFIX if fail else "")
        op = Op("get", target="fail" if fail else "ok", write=True, detail={"url": url})
        resp = self.request("GET", url, op, raw=True, timeout=TIMEOUT_SECONDS)
        if resp is None:  # the guard never skips a ping, but a skipped one is no answer
            raise PingFailed("the ping was not sent")
        if resp.status >= 300:
            raise PingFailed(f"Healthchecks answered HTTP {resp.status}")
        return resp.status
