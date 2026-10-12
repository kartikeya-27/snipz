"""Tests for :mod:`snipz.integrations.anthropic`.

Run against :class:`tests.fake_anthropic.FakeAsyncAnthropic` and a real
SQLite ledger. Every money assertion reads the ledger directly
(:mod:`tests.ledger_audit`), so "what was recorded" is checked
independently of the code that recorded it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import pytest
import pytest_asyncio
from anthropic.resources.messages import AsyncMessages
from anthropic.types import Message

from snipz import Budget, BudgetExceededError, Pricing, Reservation, Scope, UnknownPricingError
from snipz.integrations import DuplicateRequestError, SettlementError
from snipz.integrations.anthropic import _COUNT_TOKENS_PARAMS, guarded
from tests.fake_anthropic import FakeAsyncAnthropic, Usage
from tests.ledger_audit import audit_sqlite

# Fixed prices so every expected amount below can be checked by hand.
# Model "m": $3 / $15 per M, cache read $0.30, writes $3.75 (5m) / $6 (1h),
# and a long-prompt tier above 100K tokens at $6 / $22.50.
PRICES = Pricing.from_toml(
    """
[anthropic."m"]
input_cents_per_m = "300"
output_cents_per_m = "1500"
cache_read_cents_per_m = "30"
cache_write_cents_per_m = "375"
cache_write_1h_cents_per_m = "600"

[[anthropic."m".tiers]]
above_tokens = 100000
input_cents_per_m = "600"
output_cents_per_m = "2250"

[anthropic."plain"]
input_cents_per_m = "100"
output_cents_per_m = "200"
"""
)

USER = Scope("user", "u1")

# 1,000 prompt tokens (the fake's count) + max_tokens 1,000 on model "m":
# 1000 * 300 / 1M + 1000 * 1500 / 1M.
ESTIMATE = Decimal("1.8")
# The fake's default usage, 1,000 in / 500 out: 0.3 + 0.75.
BILLED = Decimal("1.05")


def _request(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": "m",
        "max_tokens": 1_000,
        "messages": [{"role": "user", "content": "hello"}],
    }
    params.update(overrides)
    return params


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> AsyncIterator[tuple[Budget, Path]]:
    db_path = tmp_path / "snipz.db"
    budget = Budget(db_path)
    await budget.migrate()
    try:
        yield budget, db_path
    finally:
        await budget.close()


def _capture_reservations(budget: Budget) -> list[Reservation]:
    seen: list[Reservation] = []
    budget.on_reserved(seen.append)
    return seen


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_guarded_rejects_sync_client(ledger: tuple[Budget, Path]) -> None:
    budget, _ = ledger
    with pytest.raises(TypeError, match="async client"):
        guarded(anthropic.Anthropic(api_key="test"), budget, pricing=PRICES)  # type: ignore[arg-type]


def test_guarded_exposes_underlying_client(ledger: tuple[Budget, Path]) -> None:
    budget, _ = ledger
    client = FakeAsyncAnthropic()
    assert guarded(client, budget, pricing=PRICES).client is client  # type: ignore[arg-type]


def test_count_tokens_params_match_installed_sdk() -> None:
    """Pins the forwarded-parameter list to the installed SDK so a new
    ``count_tokens`` parameter (one that changes the count) is noticed."""
    sdk_params = set(inspect.signature(AsyncMessages.count_tokens).parameters) - {"self"}
    assert sdk_params == _COUNT_TOKENS_PARAMS


# ---------------------------------------------------------------------------
# Successful calls
# ---------------------------------------------------------------------------


async def test_create_reserves_estimate_and_commits_billed_usage(
    ledger: tuple[Budget, Path],
) -> None:
    budget, db_path = ledger
    client = FakeAsyncAnthropic()
    reserved = _capture_reservations(budget)

    message = await guarded(client, budget, pricing=PRICES).messages.create(  # type: ignore[arg-type]
        scope=USER, **_request()
    )

    assert isinstance(message, Message)
    assert [r.estimated_cents for r in reserved] == [ESTIMATE]
    audit = await audit_sqlite(db_path)
    assert audit.rows_by_state == {"committed": 1}
    assert audit.committed("user", "u1") == BILLED
    # snipz's own keywords never reach the SDK.
    assert client.messages.create_calls == [_request()]


async def test_count_tokens_receives_only_count_parameters(
    ledger: tuple[Budget, Path],
) -> None:
    budget, _ = ledger
    client = FakeAsyncAnthropic()
    params = _request(system="be brief", temperature=0.2, metadata={"user_id": "u1"})

    await guarded(client, budget, pricing=PRICES).messages.create(scope=USER, **params)  # type: ignore[arg-type]

    assert client.messages.count_calls == [
        {"model": "m", "messages": params["messages"], "system": "be brief"}
    ]


async def test_composite_scopes_are_all_charged(ledger: tuple[Budget, Path]) -> None:
    budget, db_path = ledger
    tenant = Scope("tenant", "acme")

    await guarded(FakeAsyncAnthropic(), budget, pricing=PRICES).messages.create(  # type: ignore[arg-type]
        scope=[USER, tenant], **_request()
    )

    audit = await audit_sqlite(db_path)
    assert audit.committed("user", "u1") == BILLED
    assert audit.committed("tenant", "acme") == BILLED


async def test_usage_bills_cache_reads_and_splits_5m_and_1h_writes(
    ledger: tuple[Budget, Path],
) -> None:
    budget, db_path = ledger
    usage = Usage(
        input_tokens=100,
        output_tokens=100,
        cache_read_input_tokens=1_000,
        cache_creation_5m=1_000,
        cache_creation_1h=1_000,
    )

    await guarded(FakeAsyncAnthropic(usage=usage), budget, pricing=PRICES).messages.create(  # type: ignore[arg-type]
        scope=USER, **_request()
    )

    # 100*300 + 100*1500 + 1000*30 + 1000*375 + 1000*600, all / 1M.
    assert (await audit_sqlite(db_path)).committed("user", "u1") == Decimal("1.185")


async def test_long_prompt_is_reserved_and_billed_at_tier_rates(
    ledger: tuple[Budget, Path],
) -> None:
    budget, db_path = ledger
    client = FakeAsyncAnthropic(
        prompt_tokens=150_000, usage=Usage(input_tokens=150_000, output_tokens=500)
    )
    reserved = _capture_reservations(budget)

    await guarded(client, budget, pricing=PRICES).messages.create(scope=USER, **_request())  # type: ignore[arg-type]

    # 150K * 600 / 1M + 1000 * 2250 / 1M, then 150K * 600 / 1M + 500 * 2250 / 1M.
    assert [r.estimated_cents for r in reserved] == [Decimal("92.25")]
    assert (await audit_sqlite(db_path)).committed("user", "u1") == Decimal("91.125")


@pytest.mark.parametrize(
    ("cache_control", "estimate"),
    [
        ({"type": "ephemeral"}, Decimal("1.875")),  # 1000 * 375 / 1M + 1.5
        ({"type": "ephemeral", "ttl": "1h"}, Decimal("2.1")),  # 1000 * 600 / 1M + 1.5
    ],
)
async def test_cache_control_reserves_every_prompt_token_as_a_write(
    ledger: tuple[Budget, Path], cache_control: dict[str, str], estimate: Decimal
) -> None:
    budget, _ = ledger
    reserved = _capture_reservations(budget)
    system = [{"type": "text", "text": "long context", "cache_control": cache_control}]

    await guarded(FakeAsyncAnthropic(), budget, pricing=PRICES).messages.create(  # type: ignore[arg-type]
        scope=USER, **_request(system=system)
    )

    assert [r.estimated_cents for r in reserved] == [estimate]


# ---------------------------------------------------------------------------
# input_tokens="estimate"
# ---------------------------------------------------------------------------


async def test_estimate_mode_skips_count_tokens_for_text_only_requests(
    ledger: tuple[Budget, Path],
) -> None:
    budget, db_path = ledger
    client = FakeAsyncAnthropic()
    system = [{"type": "text", "text": "be brief"}]

    await guarded(client, budget, pricing=PRICES, input_tokens="estimate").messages.create(  # type: ignore[arg-type]
        scope=USER, **_request(system=system)
    )

    assert client.messages.count_calls == []
    assert (await audit_sqlite(db_path)).committed("user", "u1") == BILLED


@pytest.mark.parametrize(
    "overrides",
    [
        {"tools": [{"name": "t", "input_schema": {"type": "object"}}]},
        {"output_config": {"format": {"type": "json_schema", "schema": {}}}},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}
                    ],
                }
            ]
        },
    ],
    ids=["tools", "structured-output", "image"],
)
async def test_estimate_mode_counts_requests_it_cannot_see(
    ledger: tuple[Budget, Path], overrides: dict[str, Any]
) -> None:
    budget, _ = ledger
    client = FakeAsyncAnthropic()

    await guarded(client, budget, pricing=PRICES, input_tokens="estimate").messages.create(  # type: ignore[arg-type]
        scope=USER, **_request(**overrides)
    )

    assert len(client.messages.count_calls) == 1


# ---------------------------------------------------------------------------
# Rejected before any request is sent
# ---------------------------------------------------------------------------


async def test_over_cap_raises_before_the_api_is_called(ledger: tuple[Budget, Path]) -> None:
    budget, db_path = ledger
    await budget.set_limit(USER, Decimal("1"))  # below the 1.8-cent estimate
    client = FakeAsyncAnthropic()

    with pytest.raises(BudgetExceededError):
        await guarded(client, budget, pricing=PRICES).messages.create(scope=USER, **_request())  # type: ignore[arg-type]

    assert client.messages.create_calls == []
    assert (await audit_sqlite(db_path)).rows_by_state == {}


async def test_unknown_model_raises_before_any_api_call(ledger: tuple[Budget, Path]) -> None:
    budget, _ = ledger
    client = FakeAsyncAnthropic()

    with pytest.raises(UnknownPricingError):
        await guarded(client, budget, pricing=PRICES).messages.create(  # type: ignore[arg-type]
            scope=USER, **_request(model="unpriced")
        )

    assert client.messages.count_calls == []
    assert client.messages.create_calls == []


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"max_tokens": None}, TypeError),
        ({"max_tokens": 0}, TypeError),
        ({"model": None}, TypeError),
        ({"stream": True}, ValueError),
    ],
    ids=["no-max-tokens", "zero-max-tokens", "no-model", "stream"],
)
async def test_invalid_requests_are_rejected_before_any_api_call(
    ledger: tuple[Budget, Path], overrides: dict[str, Any], error: type[Exception]
) -> None:
    budget, db_path = ledger
    client = FakeAsyncAnthropic()
    params = {k: v for k, v in _request(**overrides).items() if v is not None}

    with pytest.raises(error):
        await guarded(client, budget, pricing=PRICES).messages.create(scope=USER, **params)  # type: ignore[arg-type]

    assert client.messages.count_calls == []
    assert client.messages.create_calls == []
    assert (await audit_sqlite(db_path)).rows_by_state == {}


# ---------------------------------------------------------------------------
# Failed calls
# ---------------------------------------------------------------------------


async def test_http_error_status_releases_the_reservation(ledger: tuple[Budget, Path]) -> None:
    """Anthropic answered with an error: nothing was billed."""
    budget, db_path = ledger
    client = FakeAsyncAnthropic(outcome=lambda _: "status_error")

    with pytest.raises(anthropic.InternalServerError):
        await guarded(client, budget, pricing=PRICES).messages.create(scope=USER, **_request())  # type: ignore[arg-type]

    assert (await audit_sqlite(db_path)).rows_by_state == {"released": 1}


async def test_timeout_commits_the_estimate(ledger: tuple[Budget, Path]) -> None:
    """A timed-out request may still have been billed: record the bound."""
    budget, db_path = ledger
    client = FakeAsyncAnthropic(outcome=lambda _: "connection_error")

    with pytest.raises(anthropic.APITimeoutError):
        await guarded(client, budget, pricing=PRICES).messages.create(scope=USER, **_request())  # type: ignore[arg-type]

    assert (await audit_sqlite(db_path)).committed("user", "u1") == ESTIMATE


async def test_cancelled_call_commits_the_estimate(ledger: tuple[Budget, Path]) -> None:
    budget, db_path = ledger
    client = FakeAsyncAnthropic(latency=10)
    task = asyncio.create_task(
        guarded(client, budget, pricing=PRICES).messages.create(scope=USER, **_request())  # type: ignore[arg-type]
    )
    await client.messages.called.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await audit_sqlite(db_path)).committed("user", "u1") == ESTIMATE


async def test_unpriceable_response_commits_the_estimate(
    ledger: tuple[Budget, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """Model "plain" has no cache-read rate, so the billed usage cannot be
    priced. The call was paid for — never raise, commit the bound."""
    budget, db_path = ledger
    client = FakeAsyncAnthropic(usage=Usage(cache_read_input_tokens=10))

    with caplog.at_level(logging.ERROR, logger="snipz.integrations"):
        message = await guarded(client, budget, pricing=PRICES).messages.create(  # type: ignore[arg-type]
            scope=USER, **_request(model="plain")
        )

    assert isinstance(message, Message)
    # 1000 * 100 / 1M + 1000 * 200 / 1M.
    assert (await audit_sqlite(db_path)).committed("user", "u1") == Decimal("0.3")
    assert "committing the estimate" in caplog.text


async def test_commit_failure_hands_back_the_paid_response(
    ledger: tuple[Budget, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    budget, _ = ledger

    async def failing_commit(*_: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(budget, "_commit", failing_commit)

    with pytest.raises(SettlementError) as excinfo:
        await guarded(FakeAsyncAnthropic(), budget, pricing=PRICES).messages.create(  # type: ignore[arg-type]
            scope=USER, **_request()
        )

    assert isinstance(excinfo.value.response, Message)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_repeated_request_id_never_calls_the_api_twice(
    ledger: tuple[Budget, Path],
) -> None:
    budget, db_path = ledger
    client = FakeAsyncAnthropic()
    messages = guarded(client, budget, pricing=PRICES).messages  # type: ignore[arg-type]

    await messages.create(scope=USER, request_id="r1", **_request())
    with pytest.raises(DuplicateRequestError) as excinfo:
        await messages.create(scope=USER, request_id="r1", **_request())

    assert excinfo.value.state == "committed"
    assert len(client.messages.create_calls) == 1
    assert (await audit_sqlite(db_path)).committed("user", "u1") == BILLED


async def test_concurrent_duplicates_call_the_api_once(ledger: tuple[Budget, Path]) -> None:
    budget, db_path = ledger
    client = FakeAsyncAnthropic(latency=0.05)
    messages = guarded(client, budget, pricing=PRICES).messages  # type: ignore[arg-type]

    results = await asyncio.gather(
        *(messages.create(scope=USER, request_id="r1", **_request()) for _ in range(5)),
        return_exceptions=True,
    )

    assert sum(isinstance(r, Message) for r in results) == 1
    assert sum(isinstance(r, DuplicateRequestError) for r in results) == 4
    assert len(client.messages.create_calls) == 1
    assert (await audit_sqlite(db_path)).committed("user", "u1") == BILLED
