"""Claude API client for copy drafts and QA, reply classification, drafts and readout notes (SPEC 1.1, 11, 12).

Uses the official anthropic SDK with structured outputs (output_config.format =
json_schema), so every answer is one JSON object matching the caller's schema.

The key's spend is capped at claude_monthly_cap_usd (SPEC 1.1 set $10; the sheet may set up to $100). Before each call the
month's spend is read from credit_ledger (system "claude", this UTC month) and the call is
refused if that spend plus a conservative estimate of this call would pass the cap less a
small reserve. After each call its actual cost, from response.usage, is written to the
ledger. A model with no price here is refused, so the cap cannot be passed silently.
Prompt text is never logged.

effort sets output_config.effort. Sonnet 5.5 and Opus 5.5 think before answering, the thinking
is billed as output and counts against max_tokens, and Sonnet's default effort is high
(docs/gtm-review/04-tool-capabilities.md §5). A caller that leaves effort out gets the model's
default, as before.

cache_system (Harry, 7 Oct 2026; the industry label check, labels.py, asks with the same long system
block for every company): the system prompt goes as one text block marked cache_control ephemeral
(5 minutes), so each call after the first reads it at the cache rate. usage_cost_usd prices the cache
write and reads; the cap's estimate stays the uncached upper bound. A prefix under the model's minimum
(512 tokens on Sonnet 5.5) is simply not cached.
"""

from __future__ import annotations

import json as jsonlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from us_outbound.clients.db import Store, new_id
from us_outbound.clients.guard import Guard, Op
from us_outbound.logs import log


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float

    @property
    def cache_write_5m(self) -> float:
        return self.input * 1.25

    @property
    def cache_write_1h(self) -> float:
        return self.input * 2.0


PRICES: dict[str, Price] = {
    "claude-haiku-4-5": Price(input=1.0, output=5.0, cache_read=0.10),
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_read=0.20),
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_read=0.20),
    "claude-fable-5-1": Price(input=10.0, output=50.0, cache_read=0.25),
}

CHARS_PER_TOKEN = 3.5  # deliberately low, so the estimate errs high
OVERHEAD_TOKENS = 300  # the system prompt structured outputs adds, plus message framing
# Held back from the cap for estimate error and jobs calling at the same time.
CAP_RESERVE_SHARE = 0.01
TIMEOUT_SECONDS = 120.0
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # output_config.effort


class ClaudeError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BudgetExceeded(ClaudeError):
    """The call would take the month's Claude spend past claude_monthly_cap_usd."""


def _check_schema(schema: Any, path: str = "$") -> None:
    """Structured outputs need additionalProperties false and a required list on every object."""
    if not isinstance(schema, dict):
        return
    t = schema.get("type")
    if t == "object" or (isinstance(t, list) and "object" in t) or "properties" in schema:
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"schema object at {path} must set additionalProperties: false")
        if not isinstance(schema.get("required"), list):
            raise ValueError(f"schema object at {path} must have a required list")
        for name, sub in (schema.get("properties") or {}).items():
            _check_schema(sub, f"{path}.{name}")
    if isinstance(schema.get("items"), dict):
        _check_schema(schema["items"], f"{path}[]")
    for key in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(schema.get(key) or ()):
            _check_schema(sub, f"{path}.{key}[{i}]")
    for key in ("$defs", "definitions"):
        for name, sub in (schema.get(key) or {}).items():
            _check_schema(sub, f"{path}.{key}.{name}")


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def estimate_call_usd(model: str, system: str, prompt: str, schema: dict, max_tokens: int) -> float:
    """An upper-leaning estimate of one call: input from characters, output at the full max_tokens.

    Needs no key, so a dry run can say what a live one would spend. A model with no price is refused.
    """
    price = PRICES.get(model)
    if price is None:
        raise ClaudeError(f"no price for Claude model {model!r}; add it to PRICES before using it")
    chars = len(system) + len(prompt) + len(jsonlib.dumps(schema))
    input_tokens = chars / CHARS_PER_TOKEN + OVERHEAD_TOKENS
    return (input_tokens * price.input + max_tokens * price.output) / 1_000_000


def usage_cost_usd(price: Price, usage: Any) -> float:
    """Cost of one response from its usage block, including prompt-cache reads and writes."""

    def n(obj: Any, name: str) -> int:
        return int(getattr(obj, name, 0) or 0)

    write_total = n(usage, "cache_creation_input_tokens")
    write_1h = n(getattr(usage, "cache_creation", None), "ephemeral_1h_input_tokens")
    return (
        n(usage, "input_tokens") * price.input
        + n(usage, "output_tokens") * price.output
        + n(usage, "cache_read_input_tokens") * price.cache_read
        + max(write_total - write_1h, 0) * price.cache_write_5m
        + write_1h * price.cache_write_1h
    ) / 1_000_000


def month_spend_usd(store: Store, now: datetime) -> float:
    """USD recorded for Claude in credit_ledger in now's UTC calendar month: what the cap counts, and what the
    daily post and its spend alerts show (learn/spend.py), without a key or a model."""
    now = _as_utc(now) or datetime.now(UTC)
    total = 0.0
    for row in store.select("credit_ledger", {"system": "claude"}):
        at = _as_utc(row.get("occurred_at"))
        if at and (at.year, at.month) == (now.year, now.month):
            total += float(row.get("usd") or 0.0)
    return total


def cap_reached_at(monthly_cap_usd: float) -> float:
    """The month's spend from which every call is refused: the cap less its reserve (Claude.json)."""
    return float(monthly_cap_usd) * (1 - CAP_RESERVE_SHARE)


class Claude:
    def __init__(
        self,
        guard: Guard,
        store: Store,
        api_key: str | None,
        model: str,
        monthly_cap_usd: float,
        sdk: Any = None,
    ):
        self.guard, self.store = guard, store
        self.model = model
        self.monthly_cap_usd = float(monthly_cap_usd)
        if sdk is None:
            import anthropic

            sdk = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SECONDS)
        self.client = sdk

    # -- budget ----------------------------------------------------------------

    def month_spend_usd(self, now: datetime) -> float:
        """USD recorded for Claude in credit_ledger in now's UTC calendar month."""
        return month_spend_usd(self.store, now)

    def _price(self) -> Price:
        price = PRICES.get(self.model)
        if price is None:
            raise ClaudeError(f"no price for Claude model {self.model!r}; add it to PRICES before using it")
        return price

    def estimate_usd(self, system: str, prompt: str, schema: dict, max_tokens: int) -> float:
        """An upper-leaning estimate: input from characters, output at the full max_tokens."""
        return estimate_call_usd(self.model, system, prompt, schema, max_tokens)

    # -- the call ----------------------------------------------------------------

    def json(
        self,
        system: str,
        prompt: str,
        schema: dict,
        *,
        max_tokens: int = 1024,
        purpose: str = "classify",
        now: datetime | None = None,
        timeout: float | None = None,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> dict:
        """One structured-output call; returns the parsed JSON object. cache_system: the system prompt is cached."""
        import anthropic

        _check_schema(schema)
        if effort is not None and effort not in EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(EFFORTS)}, not {effort!r}")
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if effort is not None:
            output_config["effort"] = effort
        now = _as_utc(now) or datetime.now(UTC)
        price = self._price()
        spent = self.month_spend_usd(now)
        estimate = self.estimate_usd(system, prompt, schema, max_tokens)
        limit = cap_reached_at(self.monthly_cap_usd)
        if spent + estimate > limit:
            log("claude_budget_refused", model=self.model, purpose=purpose, spent_usd=round(spent, 4),
                estimate_usd=round(estimate, 4), cap_usd=self.monthly_cap_usd)
            raise BudgetExceeded(
                f"Claude spend ${spent:.2f} this month plus ${estimate:.4f} for this call "
                f"would pass the ${self.monthly_cap_usd:.2f} cap"
            )

        self.guard.authorize("claude", Op("messages.create", target=self.model, detail={"purpose": purpose}))
        client = self.client
        if timeout is not None and hasattr(client, "with_options"):
            client = client.with_options(timeout=timeout)  # a long draft outlasts the default timeout
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cache_system
                        else system),
                messages=[{"role": "user", "content": prompt}],
                output_config=output_config,
            )
        except anthropic.RateLimitError as exc:
            log("claude_error", model=self.model, purpose=purpose, status=429, kind="rate_limit")
            raise ClaudeError("Claude API rate limit (after SDK retries)", status=429) from exc
        except anthropic.APIStatusError as exc:
            log("claude_error", model=self.model, purpose=purpose, status=exc.status_code, kind="status")
            raise ClaudeError(f"Claude API error {exc.status_code}", status=exc.status_code) from exc
        except anthropic.APIConnectionError as exc:
            log("claude_error", model=self.model, purpose=purpose, status=None, kind="connection")
            raise ClaudeError("Claude API connection error (after SDK retries)") from exc

        # Billed whatever the outcome, so record it before checking the answer.
        usage = getattr(response, "usage", None)
        cost = usage_cost_usd(price, usage)
        self.store.insert(
            "credit_ledger",
            [
                {
                    "entry_id": new_id(),
                    "system": "claude",
                    "job": purpose,
                    "run_id": None,
                    "account_id": None,
                    "credits": 0.0,
                    "usd": cost,
                    "occurred_at": now,
                    "note": f"{self.model} in={getattr(usage, 'input_tokens', 0)} out={getattr(usage, 'output_tokens', 0)}"
                            + (f" cache_read={getattr(usage, 'cache_read_input_tokens', 0) or 0}"
                               f" cache_write={getattr(usage, 'cache_creation_input_tokens', 0) or 0}"
                               if cache_system else ""),
                }
            ],
        )
        stop = getattr(response, "stop_reason", None)
        log("claude_call", model=self.model, purpose=purpose, stop_reason=stop, usd=round(cost, 6),
            input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None),
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None))

        if stop == "refusal":
            raise ClaudeError("Claude declined the request (stop_reason refusal)")
        if stop == "max_tokens":
            raise ClaudeError(f"Claude hit max_tokens={max_tokens} before finishing the JSON")
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise ClaudeError("Claude returned no text block")
        try:
            data = jsonlib.loads(text)
        except ValueError as exc:
            raise ClaudeError("Claude returned text that is not JSON") from exc
        if not isinstance(data, dict):
            raise ClaudeError("Claude returned JSON that is not an object")
        return data
