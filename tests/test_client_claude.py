"""Claude client: structured-output call shape, the monthly cap, cost recording, errors."""

from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import pytest

from us_outbound.clients.bq import MemoryStore
from us_outbound.clients.claude import BudgetExceeded, Claude, ClaudeError, PRICES, usage_cost_usd
from us_outbound.clients.guard import Guard

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SCHEMA = {
    "type": "object",
    "properties": {"class": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["class", "confidence"],
    "additionalProperties": False,
}


class FakeSDK:
    """Stands in for anthropic.Anthropic: records calls, returns a canned message or raises."""

    def __init__(self, text='{"class": "positive", "confidence": 0.9}', stop_reason="end_turn", raises=None,
                 input_tokens=1000, output_tokens=200):
        self.calls: list[dict] = []
        self.text, self.stop_reason, self.raises = text, stop_reason, raises
        self.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                                     cache_creation_input_tokens=0, cache_read_input_tokens=0, cache_creation=None)
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason=self.stop_reason,
                               usage=self.usage)


def sdk_error(cls, status=None):
    """An SDK exception without building an HTTP response object."""
    exc = cls.__new__(cls)
    Exception.__init__(exc, "boom")
    if status is not None:
        exc.status_code = status
    return exc


def make(sdk=None, model="claude-haiku-4-5", cap=10.0):
    guard = Guard(live=False)
    store = MemoryStore(guard)
    sdk = sdk or FakeSDK()
    return Claude(guard, store, api_key=None, model=model, monthly_cap_usd=cap, sdk=sdk), store, sdk, guard


def ledger(store, usd, at, system="claude"):
    store.insert("credit_ledger", [{"entry_id": f"e{len(store.tables['credit_ledger'])}", "system": system,
                                    "job": "classify", "usd": usd, "credits": 0.0, "occurred_at": at}])


def test_call_shape_parse_and_cost_recorded():
    claude, store, sdk, guard = make()
    out = claude.json("You classify replies.", "Sounds good, send times.", SCHEMA, purpose="classify", now=NOW)
    assert out == {"class": "positive", "confidence": 0.9}
    [call] = sdk.calls
    assert call == {
        "model": "claude-haiku-4-5",
        "max_tokens": 1024,
        "system": "You classify replies.",
        "messages": [{"role": "user", "content": "Sounds good, send times."}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }
    [row] = store.tables["credit_ledger"]
    assert row["system"] == "claude" and row["job"] == "classify" and row["credits"] == 0.0
    assert row["usd"] == pytest.approx(1000 * 1 / 1e6 + 200 * 5 / 1e6)
    assert row["occurred_at"] == NOW
    assert claude.month_spend_usd(NOW) == pytest.approx(0.002)
    [rec] = [c for c in guard.calls if c.system == "claude"]
    assert (rec.action, rec.target, rec.write) == ("messages.create", "claude-haiku-4-5", False)


def test_cap_refuses_when_ledger_holds_9_99_this_month():
    claude, store, sdk, _ = make()
    ledger(store, 9.99, datetime(2026, 10, 3, tzinfo=UTC))
    with pytest.raises(BudgetExceeded):
        claude.json("sys", "prompt", SCHEMA, now=NOW)
    assert sdk.calls == []
    assert len(store.tables["credit_ledger"]) == 1


def test_cap_counts_only_this_utc_month_and_claude_rows():
    claude, store, sdk, _ = make()
    ledger(store, 9.99, datetime(2026, 9, 30, 23, 59, tzinfo=UTC))  # last month
    ledger(store, 9.99, "2026-10-02T10:00:00+00:00", system="clay")  # another system
    ledger(store, 1.00, "2026-10-02T10:00:00Z")  # string timestamps are read too
    assert claude.month_spend_usd(NOW) == pytest.approx(1.0)
    claude.json("sys", "prompt", SCHEMA, now=NOW)
    assert len(sdk.calls) == 1


def test_estimate_includes_full_max_tokens():
    claude, store, sdk, _ = make(model="claude-sonnet-5-5")
    ledger(store, 9.50, NOW)
    # 9.50 + ~0.08 of output at $10/MTok stays under 9.90; 60k tokens ($0.60) does not.
    claude.json("sys", "prompt", SCHEMA, max_tokens=8000, now=NOW)
    with pytest.raises(BudgetExceeded):
        claude.json("sys", "prompt", SCHEMA, max_tokens=60000, now=NOW)
    assert len(sdk.calls) == 1


def test_unknown_model_refused():
    claude, _, sdk, _ = make(model="claude-mystery-9")
    with pytest.raises(ClaudeError):
        claude.json("sys", "prompt", SCHEMA, now=NOW)
    assert sdk.calls == []


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
        {**SCHEMA, "properties": {"nested": {"type": "object", "properties": {}, "required": []}}},
    ],
)
def test_schema_must_be_closed(schema):
    claude, _, sdk, _ = make()
    with pytest.raises(ValueError):
        claude.json("sys", "prompt", schema, now=NOW)
    assert sdk.calls == []


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_bad_stop_reason_raises_but_cost_is_recorded(stop):
    claude, store, _, _ = make(sdk=FakeSDK(text='{"class": "pos', stop_reason=stop))
    with pytest.raises(ClaudeError):
        claude.json("sys", "prompt", SCHEMA, now=NOW)
    assert len(store.tables["credit_ledger"]) == 1


def test_non_json_text_raises():
    claude, _, _, _ = make(sdk=FakeSDK(text="not json"))
    with pytest.raises(ClaudeError):
        claude.json("sys", "prompt", SCHEMA, now=NOW)


@pytest.mark.parametrize(
    "exc,status",
    [
        (sdk_error(anthropic.RateLimitError, 429), 429),
        (sdk_error(anthropic.InternalServerError, 500), 500),
        (sdk_error(anthropic.APIConnectionError), None),
    ],
)
def test_sdk_errors_become_claude_errors(exc, status):
    claude, store, _, _ = make(sdk=FakeSDK(raises=exc))
    with pytest.raises(ClaudeError) as err:
        claude.json("sys", "prompt", SCHEMA, now=NOW)
    assert err.value.status == status
    assert not isinstance(err.value, BudgetExceeded)
    assert store.tables["credit_ledger"] == []


def test_usage_cost_includes_cache_tokens():
    usage = SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=1000,
                            cache_creation_input_tokens=400, cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=100))
    price = PRICES["claude-opus-5-5"]
    expected = (100 * 4 + 10 * 20 + 1000 * 0.20 + 300 * 5.0 + 100 * 8.0) / 1e6
    assert usage_cost_usd(price, usage) == pytest.approx(expected)


def test_prompt_never_logged(capsys):
    claude, _, _, _ = make()
    claude.json("sys", "SECRET-PROMPT-TEXT from jane@acme.com", SCHEMA, now=NOW)
    assert "SECRET-PROMPT-TEXT" not in capsys.readouterr().out
