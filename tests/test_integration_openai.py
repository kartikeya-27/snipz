"""Tests for :mod:`snipz.integrations.openai` (Responses API).

Drives the real ``openai.AsyncOpenAI`` client over a scripted HTTP API
(:mod:`tests.fake_openai_api`), so request building, SSE parsing, stream
state, and error mapping are the SDK's own. Every amount is read back
from the ledger.
"""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import openai
import pytest
from openai.resources.responses.input_tokens import AsyncInputTokens

from snipz import Budget, BudgetExceededError, Pricing, Reservation, Scope
from snipz.integrations import DuplicateRequestError, SettlementError
from snipz.integrations.openai import _COUNT_PARAMS, guarded
from tests.fake_openai_api import FakeOpenAIAPI, Outcome
from tests.ledger_audit import audit_sqlite

# Fixed prices so every expected amount can be checked by hand. "gpt-x":
# $3 / $15 per M, cached $0.30, cache write $3.75 (1.25x), and a long-prompt
# tier above 272K tokens at $6 / $22.50. "legacy" has no cache-write price.
PRICES = Pricing.from_toml(
    """
[openai."gpt-x"]
input_cents_per_m = "300"
output_cents_per_m = "1500"
cache_read_cents_per_m = "30"
cache_write_cents_per_m = "375"

[[openai."gpt-x".tiers]]
above_tokens = 272000
input_cents_per_m = "600"
output_cents_per_m = "2250"

[openai."legacy"]
input_cents_per_m = "300"
output_cents_per_m = "1500"
cache_read_cents_per_m = "30"
"""
)

USER = Scope("user", "u1")

# 1,000 counted prompt tokens, all possibly cache writes (375), plus
# max_output_tokens 1,000: 1000 * 375 / 1M + 1000 * 1500 / 1M.
ESTIMATE = Decimal("1.875")
# The fake's default usage, 800 in / 500 out: 0.24 + 0.75.
BILLED = Decimal("0.99")


def _request(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"model": "gpt-x", "max_output_tokens": 1_000, "input": "hello"}
    params.update(overrides)
    return params


def _api(outcome: Outcome = "completed", **usage: int) -> FakeOpenAIAPI:
    return FakeOpenAIAPI(outcome=lambda _: outcome, **usage)


def _responses(api: FakeOpenAIAPI, budget: Budget) -> Any:
    return guarded(api.client(), budget, pricing=PRICES).responses


async def _committed(tmp_path: Path) -> Decimal:
    return (await audit_sqlite(tmp_path / "snipz.db")).committed("user", "u1")


async def _states(tmp_path: Path) -> dict[str, int]:
    return dict((await audit_sqlite(tmp_path / "snipz.db")).rows_by_state)


# ---------------------------------------------------------------------------
# Construction and SDK assumptions
# ---------------------------------------------------------------------------


def test_guarded_accepts_the_real_async_client_and_rejects_sync(budget: Budget) -> None:
    client = openai.AsyncOpenAI(api_key="test")
    assert guarded(client, budget, pricing=PRICES).client is client
    with pytest.raises(TypeError, match="async client"):
        guarded(openai.OpenAI(api_key="test"), budget, pricing=PRICES)  # type: ignore[arg-type]


def test_count_params_match_installed_sdk() -> None:
    sdk_params = set(inspect.signature(AsyncInputTokens.count).parameters) - {"self"}
    assert sdk_params == _COUNT_PARAMS


# ---------------------------------------------------------------------------
# responses.create
# ---------------------------------------------------------------------------


async def test_create_reserves_estimate_and_commits_billed_usage(
    budget: Budget, tmp_path: Path
) -> None:
    api = _api()
    reserved: list[Reservation] = []
    budget.on_reserved(reserved.append)
    params = _request(instructions="be brief", temperature=0.2)

    response = await _responses(api, budget).create(scope=USER, **params)

    assert response.usage is not None
    assert [r.estimated_cents for r in reserved] == [ESTIMATE]
    assert await _states(tmp_path) == {"committed": 1}
    assert await _committed(tmp_path) == BILLED
    # Only the counting endpoint's own parameters reach it; snipz's never
    # reach the SDK.
    assert api.requests == [params]


async def test_cached_and_written_tokens_bill_at_their_own_rates(
    budget: Budget, tmp_path: Path
) -> None:
    """Both are subsets of input_tokens: 650 uncached + 100 cached + 50
    written, plus 500 output."""
    api = _api(cached_tokens=100, cache_write_tokens=50)
    await _responses(api, budget).create(scope=USER, **_request())
    # 650 * 300 + 100 * 30 + 50 * 375 + 500 * 1500, all / 1M.
    assert await _committed(tmp_path) == Decimal("0.96675")


async def test_writes_bill_as_input_on_models_without_a_write_price(
    budget: Budget, tmp_path: Path
) -> None:
    api = _api(cached_tokens=100, cache_write_tokens=50)
    await _responses(api, budget).create(scope=USER, **_request(model="legacy"))
    # 700 * 300 + 100 * 30 + 500 * 1500, all / 1M.
    assert await _committed(tmp_path) == Decimal("0.963")


async def test_long_prompt_is_reserved_and_billed_at_tier_rates(
    budget: Budget, tmp_path: Path
) -> None:
    api = FakeOpenAIAPI(counted_tokens=300_000, input_tokens=300_000)
    reserved: list[Reservation] = []
    budget.on_reserved(reserved.append)

    await _responses(api, budget).create(scope=USER, **_request())

    # 300K * 600 + 1000 * 2250, then 300K * 600 + 500 * 2250, all / 1M.
    assert [r.estimated_cents for r in reserved] == [Decimal("182.25")]
    assert await _committed(tmp_path) == Decimal("181.125")


async def test_incomplete_response_bills_its_usage(budget: Budget, tmp_path: Path) -> None:
    await _responses(_api("incomplete"), budget).create(scope=USER, **_request())
    assert await _committed(tmp_path) == BILLED


async def test_http_error_releases(budget: Budget, tmp_path: Path) -> None:
    with pytest.raises(openai.InternalServerError):
        await _responses(_api("status_error"), budget).create(scope=USER, **_request())
    assert await _states(tmp_path) == {"released": 1}


async def test_dropped_connection_commits_the_estimate(budget: Budget, tmp_path: Path) -> None:
    with pytest.raises(openai.APIConnectionError):
        await _responses(_api("connection_drop"), budget).create(scope=USER, **_request())
    assert await _committed(tmp_path) == ESTIMATE


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"max_output_tokens": None}, TypeError),
        ({"max_output_tokens": 0}, TypeError),
        ({"model": None}, TypeError),
        ({"stream": True}, ValueError),
        ({"background": True}, ValueError),
        ({"prompt": {"id": "pmpt_1"}}, ValueError),
        ({"service_tier": "priority"}, ValueError),
    ],
    ids=["no-max", "zero-max", "no-model", "stream", "background", "stored-prompt", "priority"],
)
async def test_unguardable_requests_are_rejected_before_any_api_call(
    budget: Budget, tmp_path: Path, overrides: dict[str, Any], error: type[Exception]
) -> None:
    api = _api()
    params = {k: v for k, v in _request(**overrides).items() if v is not None}

    with pytest.raises(error):
        await _responses(api, budget).create(scope=USER, **params)

    assert api.requests == []
    assert await _states(tmp_path) == {}


async def test_over_cap_raises_before_the_api_is_called(budget: Budget, tmp_path: Path) -> None:
    await budget.set_limit(USER, Decimal("1"))  # below the 1.875-cent estimate
    api = _api()
    with pytest.raises(BudgetExceededError):
        await _responses(api, budget).create(scope=USER, **_request())
    assert api.requests == []


async def test_repeated_request_id_never_calls_the_api_twice(
    budget: Budget, tmp_path: Path
) -> None:
    api = _api()
    responses = _responses(api, budget)
    await responses.create(scope=USER, request_id="r1", **_request())
    with pytest.raises(DuplicateRequestError):
        await responses.create(scope=USER, request_id="r1", **_request())
    assert len(api.requests) == 1


# ---------------------------------------------------------------------------
# responses.stream
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["completed", "incomplete", "failed"])
async def test_stream_ending_with_a_final_event_commits_its_usage(
    budget: Budget, tmp_path: Path, outcome: Outcome
) -> None:
    async with _responses(_api(outcome), budget).stream(scope=USER, **_request()) as stream:
        events = [event.type async for event in stream]

    assert events[-1] == f"response.{outcome}"
    assert stream.final_response is not None
    assert await _committed(tmp_path) == BILLED


async def test_stream_helpers_pass_through(budget: Budget, tmp_path: Path) -> None:
    async with _responses(_api(), budget).stream(scope=USER, **_request()) as stream:
        final = await stream.get_final_response()
        await stream.close()  # delegated to the SDK stream
    assert final.usage is not None
    assert final.usage.output_tokens == 500
    assert await _committed(tmp_path) == BILLED


@pytest.mark.parametrize("outcome", ["error_event", "early_exit"])
async def test_stream_without_final_usage_commits_the_estimate(
    budget: Budget, tmp_path: Path, outcome: str
) -> None:
    """OpenAI reports no partial usage: an SSE error event, or a consumer
    leaving early, settles at the reservation."""
    served: Outcome = "error_event" if outcome == "error_event" else "completed"
    async with _responses(_api(served), budget).stream(scope=USER, **_request()) as stream:
        async for _ in stream:
            if outcome == "early_exit":
                break

    assert await _committed(tmp_path) == ESTIMATE


async def test_stream_dropping_mid_way_commits_the_estimate(
    budget: Budget, tmp_path: Path
) -> None:
    with pytest.raises(openai.APIConnectionError):
        async with _responses(_api("connection_drop"), budget).stream(
            scope=USER, **_request()
        ) as stream:
            await stream.until_done()
    assert await _committed(tmp_path) == ESTIMATE


async def test_cancelled_stream_commits_the_estimate(budget: Budget, tmp_path: Path) -> None:
    first_event = asyncio.Event()

    async def consume() -> None:
        async with _responses(_api(), budget).stream(scope=USER, **_request()) as stream:
            async for _ in stream:
                first_event.set()
                await asyncio.sleep(10)

    task = asyncio.create_task(consume())
    async with asyncio.timeout(5):  # fail, never hang, if the stream never starts
        await first_event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _committed(tmp_path) == ESTIMATE


async def test_caller_exception_after_completion_still_bills_usage(
    budget: Budget, tmp_path: Path
) -> None:
    with pytest.raises(KeyError, match="caller bug"):
        async with _responses(_api(), budget).stream(scope=USER, **_request()) as stream:
            await stream.until_done()
            raise KeyError("caller bug")
    assert await _committed(tmp_path) == BILLED


async def test_http_error_opening_the_stream_releases(budget: Budget, tmp_path: Path) -> None:
    with pytest.raises(openai.InternalServerError):
        async with _responses(_api("status_error"), budget).stream(scope=USER, **_request()):
            pass
    assert await _states(tmp_path) == {"released": 1}


async def test_stream_commit_failure_hands_back_the_final_response(
    budget: Budget, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_commit(*_: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(budget, "_commit", failing_commit)

    with pytest.raises(SettlementError) as excinfo:
        async with _responses(_api(), budget).stream(scope=USER, **_request()) as stream:
            await stream.until_done()

    assert excinfo.value.response.usage.output_tokens == 500
    assert isinstance(excinfo.value.__cause__, RuntimeError)


async def test_a_guarded_stream_can_only_be_entered_once(budget: Budget) -> None:
    manager = _responses(_api(), budget).stream(scope=USER, **_request())
    async with manager as stream:
        await stream.until_done()
    with pytest.raises(RuntimeError, match="only be entered once"):
        await manager.__aenter__()
