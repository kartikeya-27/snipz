"""Tests for ``GuardedMessages.stream`` — budget-guarded streaming.

Drives the real ``anthropic.AsyncAnthropic`` client over a scripted HTTP
API (:mod:`tests.fake_anthropic_api`), so SSE parsing, stream
accumulation, and error mapping are the SDK's own. Every amount is read
back from the ledger.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.lib.streaming import AsyncMessageStream

from snipz import Budget, BudgetExceededError, Reservation, Scope
from snipz.integrations import DuplicateRequestError, SettlementError
from snipz.integrations.anthropic import _snapshot, guarded
from tests.fake_anthropic_api import FakeAnthropicAPI, StreamOutcome
from tests.ledger_audit import audit_sqlite
from tests.test_integration_anthropic import PRICES

USER = Scope("user", "u1")

# Model "m" at $3 / $15 per M (tests.test_integration_anthropic.PRICES):
# reserved:  1,000 counted prompt tokens + max_tokens 1,000 of output.
ESTIMATE = Decimal("1.8")
# completed: 800 prompt tokens + 500 output tokens, as billed.
BILLED = Decimal("0.99")
# stopped after message_start: the exact 800 prompt tokens, plus max_tokens
# of output — generation may have continued until the connection closed.
PARTIAL = Decimal("1.74")


def _request(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": "m",
        "max_tokens": 1_000,
        "messages": [{"role": "user", "content": "hello"}],
    }
    params.update(overrides)
    return params


def _api(outcome: StreamOutcome = "complete") -> FakeAnthropicAPI:
    return FakeAnthropicAPI(outcome=lambda _: outcome)


async def _committed(tmp_path: Path) -> Decimal:
    return (await audit_sqlite(tmp_path / "snipz.db")).committed("user", "u1")


# ---------------------------------------------------------------------------
# Streams that run to completion
# ---------------------------------------------------------------------------


async def test_completed_stream_commits_the_billed_usage(budget: Budget, tmp_path: Path) -> None:
    api = _api()
    reserved: list[Reservation] = []
    budget.on_reserved(reserved.append)

    async with guarded(api.client(), budget, pricing=PRICES).messages.stream(
        scope=USER, **_request()
    ) as stream:
        text = "".join([chunk async for chunk in stream.text_stream])
        final = await stream.get_final_message()

    assert text == "hihihi"
    assert final.usage.output_tokens == 500
    assert [r.estimated_cents for r in reserved] == [ESTIMATE]
    assert (await audit_sqlite(tmp_path / "snipz.db")).rows_by_state == {"committed": 1}
    assert await _committed(tmp_path) == BILLED


async def test_exception_after_a_completed_stream_still_commits_billed_usage(
    budget: Budget, tmp_path: Path
) -> None:
    """The caller's own error after reading everything: the stream is paid
    for at its billed usage, and the caller's exception propagates."""
    with pytest.raises(KeyError, match="caller bug"):
        async with guarded(_api().client(), budget, pricing=PRICES).messages.stream(
            scope=USER, **_request()
        ) as stream:
            await stream.until_done()
            raise KeyError("caller bug")

    assert await _committed(tmp_path) == BILLED


# ---------------------------------------------------------------------------
# Streams that stop early
# ---------------------------------------------------------------------------


async def test_consumer_stopping_early_commits_prompt_plus_max_output(
    budget: Budget, tmp_path: Path
) -> None:
    async with guarded(_api().client(), budget, pricing=PRICES).messages.stream(
        scope=USER, **_request()
    ) as stream:
        async for _ in stream.text_stream:
            break

    assert await _committed(tmp_path) == PARTIAL


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("error_event", anthropic.APIStatusError),
        ("connection_drop", Exception),  # the SDK lets the raw httpx2 error escape
    ],
)
async def test_stream_failing_mid_way_commits_prompt_plus_max_output(
    budget: Budget, tmp_path: Path, outcome: StreamOutcome, error: type[Exception]
) -> None:
    with pytest.raises(error):
        async with guarded(_api(outcome).client(), budget, pricing=PRICES).messages.stream(
            scope=USER, **_request()
        ) as stream:
            await stream.until_done()

    assert await _committed(tmp_path) == PARTIAL


async def test_cancelled_stream_commits_prompt_plus_max_output(
    budget: Budget, tmp_path: Path
) -> None:
    first_chunk = asyncio.Event()

    async def consume() -> None:
        async with guarded(_api().client(), budget, pricing=PRICES).messages.stream(
            scope=USER, **_request()
        ) as stream:
            async for _ in stream.text_stream:
                first_chunk.set()
                await asyncio.sleep(10)

    task = asyncio.create_task(consume())
    async with asyncio.timeout(5):  # fail, never hang, if the stream never starts
        await first_chunk.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _committed(tmp_path) == PARTIAL


async def test_stream_closed_before_any_event_commits_the_reservation(
    budget: Budget, tmp_path: Path
) -> None:
    """The request was accepted but no usage arrived: commit the bound."""
    async with guarded(_api().client(), budget, pricing=PRICES).messages.stream(
        scope=USER, **_request()
    ):
        pass

    assert await _committed(tmp_path) == ESTIMATE


# ---------------------------------------------------------------------------
# Streams that never open
# ---------------------------------------------------------------------------


async def test_http_error_opening_the_stream_releases(budget: Budget, tmp_path: Path) -> None:
    with pytest.raises(anthropic.APIStatusError):
        async with guarded(_api("status_error").client(), budget, pricing=PRICES).messages.stream(
            scope=USER, **_request()
        ):
            pass

    assert (await audit_sqlite(tmp_path / "snipz.db")).rows_by_state == {"released": 1}


async def test_over_cap_raises_before_the_stream_is_requested(
    budget: Budget, tmp_path: Path
) -> None:
    await budget.set_limit(USER, Decimal("1"))  # below the 1.8-cent estimate
    api = _api()

    with pytest.raises(BudgetExceededError):
        async with guarded(api.client(), budget, pricing=PRICES).messages.stream(
            scope=USER, **_request()
        ):
            pass

    assert api.stream_requests == []
    assert (await audit_sqlite(tmp_path / "snipz.db")).rows_by_state == {}


async def test_repeated_request_id_never_streams_twice(budget: Budget, tmp_path: Path) -> None:
    api = _api()
    messages = guarded(api.client(), budget, pricing=PRICES).messages

    async with messages.stream(scope=USER, request_id="r1", **_request()) as stream:
        await stream.until_done()
    with pytest.raises(DuplicateRequestError):
        async with messages.stream(scope=USER, request_id="r1", **_request()):
            pass

    assert len(api.stream_requests) == 1
    assert await _committed(tmp_path) == BILLED


def test_create_with_stream_true_points_to_messages_stream(budget: Budget) -> None:
    messages = guarded(_api().client(), budget, pricing=PRICES).messages
    with pytest.raises(ValueError, match=r"messages\.stream\(\)"):
        asyncio.run(messages.create(scope=USER, stream=True, **_request()))


async def test_a_guarded_stream_can_only_be_entered_once(budget: Budget) -> None:
    manager = guarded(_api().client(), budget, pricing=PRICES).messages.stream(
        scope=USER, **_request()
    )
    async with manager as stream:
        await stream.until_done()
    with pytest.raises(RuntimeError, match="only be entered once"):
        await manager.__aenter__()


# ---------------------------------------------------------------------------
# Settlement failure and SDK assumptions
# ---------------------------------------------------------------------------


async def test_commit_failure_hands_back_the_streamed_message(
    budget: Budget, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_commit(*_: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(budget, "_commit", failing_commit)

    with pytest.raises(SettlementError) as excinfo:
        async with guarded(_api().client(), budget, pricing=PRICES).messages.stream(
            scope=USER, **_request()
        ) as stream:
            await stream.until_done()

    assert excinfo.value.response.usage.output_tokens == 500
    assert isinstance(excinfo.value.__cause__, RuntimeError)


async def test_snapshot_is_none_before_the_first_event() -> None:
    """Pins the SDK behaviour ``_snapshot`` relies on: reading the snapshot
    before ``message_start`` fails rather than returning a partial message."""
    async with _api().client().messages.stream(**_request()) as stream:
        assert isinstance(stream, AsyncMessageStream)
        assert _snapshot(stream) is None
        await stream.until_done()
        snapshot = _snapshot(stream)
        assert snapshot is not None
        assert snapshot.stop_reason == "end_turn"
