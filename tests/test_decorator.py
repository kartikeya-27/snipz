"""Tests for the :meth:`Budget.guard` decorator API.

These exercise every contract point: static and callable parameter
specs, composite scope, the ``actual`` callback signature
``(response, *args, **kwargs)``, async-callable specs, exception
paths (auto-release), and the wrapped function's return value being
preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from snipz import Budget, BudgetExceededError, Scope

# ---------------------------------------------------------------------------
# Static specs — basic shape
# ---------------------------------------------------------------------------


async def test_guard_static_scope_and_estimate(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    @budget.guard(scope=scope, estimate=Decimal("100"))
    async def call() -> str:
        return "ok"

    result = await call()
    assert result == "ok"


async def test_guard_returns_wrapped_function_value_unchanged(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    @budget.guard(scope=scope, estimate=Decimal("10"))
    async def call() -> dict[str, int]:
        return {"answer": 42}

    result = await call()
    assert result == {"answer": 42}


async def test_guard_raises_budget_exceeded(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    @budget.guard(scope=scope, estimate=Decimal("150"))
    async def call() -> str:
        return "should not reach"  # pragma: no cover

    with pytest.raises(BudgetExceededError):
        await call()


# ---------------------------------------------------------------------------
# Callable specs — resolved per call
# ---------------------------------------------------------------------------


async def test_guard_callable_scope_uses_call_args(budget: Budget) -> None:
    """``scope=lambda user_id, **kw: Scope(...)`` resolves at call time."""
    await budget.set_limit(Scope("user", "alice"), Decimal("500"))
    await budget.set_limit(Scope("user", "bob"), Decimal("500"))

    @budget.guard(
        scope=lambda user_id, **_: Scope("user", user_id),
        estimate=Decimal("10"),
    )
    async def call(user_id: str) -> str:
        return f"hi {user_id}"

    # Both users have their own caps; both calls succeed.
    assert await call("alice") == "hi alice"
    assert await call("bob") == "hi bob"


async def test_guard_callable_estimate_uses_call_args(budget: Budget) -> None:
    """``estimate=lambda prompt, **kw: ...`` resolves at call time."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    @budget.guard(
        scope=scope,
        estimate=lambda prompt, **_: Decimal(len(prompt)),
    )
    async def call(prompt: str) -> int:
        return len(prompt)

    assert await call("hello") == 5
    assert await call("a much longer prompt") == 20


# ---------------------------------------------------------------------------
# `actual` callback — receives (response, *args, **kwargs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakeResponse:
    tokens: int


async def test_guard_actual_observes_committed_cost(budget: Budget) -> None:
    """When ``actual`` is supplied, the reservation commits at actual_cents."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    @budget.guard(
        scope=scope,
        estimate=Decimal("50"),
        actual=lambda response, **_: Decimal(response.tokens),
    )
    async def call() -> _FakeResponse:
        return _FakeResponse(tokens=20)

    await call()

    # The cap is $1.00; we reserved $0.50 then committed at $0.20.
    # A second call estimating $0.85 should now fit ($0.20 + $0.85 = $1.05? No, $0.20 already
    # committed; $0.85 estimate; $0.20+$0.85 = $1.05 > $1.00 — over). Let's verify with $0.80:
    @budget.guard(
        scope=scope,
        estimate=Decimal("80"),
    )
    async def call2() -> str:
        return "ok"

    assert await call2() == "ok"


async def test_guard_actual_receives_response_and_call_args(budget: Budget) -> None:
    """``actual(response, *args, **kwargs)`` lets cost derive from both ends."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    captured: list[tuple[object, ...]] = []

    def actual_fn(response: _FakeResponse, prompt: str, multiplier: int) -> Decimal:
        captured.append((response, prompt, multiplier))
        return Decimal(response.tokens * multiplier)

    @budget.guard(scope=scope, estimate=Decimal("10"), actual=actual_fn)
    async def call(prompt: str, multiplier: int) -> _FakeResponse:
        return _FakeResponse(tokens=len(prompt))

    await call("hello", 2)

    assert len(captured) == 1
    response, prompt, multiplier = captured[0]
    assert isinstance(response, _FakeResponse)
    assert response.tokens == 5
    assert prompt == "hello"
    assert multiplier == 2


async def test_guard_without_actual_commits_at_estimate(budget: Budget) -> None:
    """``actual=None`` falls back to the original estimate at commit time."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    @budget.guard(scope=scope, estimate=Decimal("60"))
    async def call() -> str:
        return "ok"

    await call()
    # Estimated $0.60; committed at $0.60 (no actual). $0.40 left.
    @budget.guard(scope=scope, estimate=Decimal("40"))
    async def call2() -> str:
        return "ok"

    assert await call2() == "ok"


# ---------------------------------------------------------------------------
# Composite scope
# ---------------------------------------------------------------------------


async def test_guard_composite_scope(budget: Budget) -> None:
    user_scope = Scope("user", "u1")
    tenant_scope = Scope("tenant", "acme")
    await budget.set_limit(user_scope, Decimal("500"))
    await budget.set_limit(tenant_scope, Decimal("80"))

    @budget.guard(scope=[user_scope, tenant_scope], estimate=Decimal("100"))
    async def call() -> str:
        return "should not reach"  # pragma: no cover

    # Tenant cap is $0.80; reserving $1.00 violates it even though the
    # user cap has headroom.
    with pytest.raises(BudgetExceededError) as exc_info:
        await call()
    assert exc_info.value.scope == tenant_scope


# ---------------------------------------------------------------------------
# Exception path — auto-release
# ---------------------------------------------------------------------------


async def test_guard_auto_releases_on_exception(budget: Budget) -> None:
    """If the wrapped function raises, the reservation must release."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    class _SyntheticError(Exception):
        pass

    @budget.guard(scope=scope, estimate=Decimal("80"))
    async def failing_call() -> str:
        raise _SyntheticError("provider broke")

    with pytest.raises(_SyntheticError):
        await failing_call()

    # Cap reclaimed — a second call at $0.80 succeeds.
    @budget.guard(scope=scope, estimate=Decimal("80"))
    async def good_call() -> str:
        return "ok"

    assert await good_call() == "ok"


# ---------------------------------------------------------------------------
# Async-callable specs
# ---------------------------------------------------------------------------


async def test_guard_async_estimate_callable(budget: Budget) -> None:
    """``estimate`` may be async — useful when the estimate needs I/O."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    async def async_estimate(prompt: str, **_: object) -> Decimal:
        return Decimal(len(prompt))

    @budget.guard(scope=scope, estimate=async_estimate)
    async def call(prompt: str) -> int:
        return len(prompt)

    assert await call("hello") == 5


async def test_guard_async_actual_callable(budget: Budget) -> None:
    """``actual`` may also be async."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    async def async_actual(response: _FakeResponse, **_: object) -> Decimal:
        return Decimal(response.tokens)

    @budget.guard(
        scope=scope,
        estimate=Decimal("100"),
        actual=async_actual,
    )
    async def call() -> _FakeResponse:
        return _FakeResponse(tokens=25)

    response = await call()
    assert response.tokens == 25


# ---------------------------------------------------------------------------
# request_id idempotency through the decorator
# ---------------------------------------------------------------------------


async def test_guard_callable_request_id(budget: Budget) -> None:
    """Same ``request_id`` resolves to the same reservation row."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    @budget.guard(
        scope=scope,
        estimate=Decimal("100"),
        request_id=lambda req_id, **_: req_id,
    )
    async def call(req_id: str) -> str:
        return f"ok {req_id}"

    # Two calls with the same request_id should both succeed without
    # double-charging — the second hits the idempotency pre-check.
    await call("req-abc")
    await call("req-abc")

    # And a third call at $4.10 succeeds because only $1.00 has been
    # consumed (one logical reservation).
    @budget.guard(scope=scope, estimate=Decimal("400"))
    async def big_call() -> str:
        return "ok"

    assert await big_call() == "ok"
