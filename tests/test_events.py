"""Tests for the Phase 7 event hooks (:mod:`snipz.events`).

Covers every contract point of the dispatcher: per-event firing,
sync + async handlers, decorator usage, registration order,
error isolation, idempotency paths that MUST NOT fire, and the
``overrun`` semantic (fires *in addition to* ``committed`` on late
commits).
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from snipz import Budget, Reservation, Scope

if TYPE_CHECKING:
    from tests.conftest import FrozenClock


# ---------------------------------------------------------------------------
# Per-hook firing
# ---------------------------------------------------------------------------


async def test_on_reserved_fires_with_reservation(budget: Budget) -> None:
    seen: list[Reservation] = []
    budget.on_reserved(seen.append)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))

    assert len(seen) == 1
    assert seen[0].id == r.id


async def test_on_committed_fires_on_normal_commit(budget: Budget) -> None:
    seen: list[Reservation] = []
    budget.on_committed(seen.append)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))

    assert len(seen) == 1
    assert seen[0].id == r.id


async def test_on_released_fires_on_caller_release(budget: Budget) -> None:
    seen: list[Reservation] = []
    budget.on_released(seen.append)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))
    await r.release()

    assert len(seen) == 1
    assert seen[0].id == r.id


async def test_on_overrun_fires_in_addition_to_committed_on_late_commit(
    clocked_budget: tuple[Budget, FrozenClock],
) -> None:
    """A late commit must fire BOTH ``overrun`` and ``committed``."""
    budget, clock = clocked_budget
    committed: list[Reservation] = []
    overruns: list[Reservation] = []
    budget.on_committed(committed.append)
    budget.on_overrun(overruns.append)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"), ttl=300)

    clock.advance(301)
    released = await budget.sweep()
    assert released == 1  # row is now released[late=true]

    await r.commit(Decimal("80"))

    assert len(overruns) == 1
    assert overruns[0].id == r.id
    assert len(committed) == 1
    assert committed[0].id == r.id


async def test_on_overrun_does_not_fire_on_normal_commit(budget: Budget) -> None:
    overruns: list[Reservation] = []
    budget.on_overrun(overruns.append)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))

    assert overruns == []


# ---------------------------------------------------------------------------
# Sync + async handlers
# ---------------------------------------------------------------------------


async def test_async_handler_is_awaited(budget: Budget) -> None:
    seen: list[Reservation] = []

    async def async_handler(r: Reservation) -> None:
        # If we weren't awaited, ``seen`` would still be empty after
        # reserve() returned. The fact that the assertion below holds
        # proves the dispatcher actually awaited us.
        await asyncio.sleep(0)
        seen.append(r)

    budget.on_reserved(async_handler)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))

    assert len(seen) == 1
    assert seen[0].id == r.id


# ---------------------------------------------------------------------------
# Registration order
# ---------------------------------------------------------------------------


async def test_handlers_fire_in_registration_order(budget: Budget) -> None:
    order: list[str] = []
    budget.on_reserved(lambda _r: order.append("first"))
    budget.on_reserved(lambda _r: order.append("second"))
    budget.on_reserved(lambda _r: order.append("third"))

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    await budget.reserve(scope, Decimal("100"))

    assert order == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Error isolation — handler exceptions logged, not propagated
# ---------------------------------------------------------------------------


async def test_handler_exception_is_logged_not_propagated(
    budget: Budget, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="snipz.events")
    second_called: list[bool] = []

    def bad_handler(_r: Reservation) -> None:
        raise RuntimeError("simulated handler failure")

    def good_handler(_r: Reservation) -> None:
        second_called.append(True)

    budget.on_reserved(bad_handler)
    budget.on_reserved(good_handler)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    # reserve() must succeed despite the first handler raising.
    r = await budget.reserve(scope, Decimal("100"))
    assert r.state == "reserved"

    # The second handler still ran.
    assert second_called == [True]

    # The exception was logged at ERROR.
    matching = [rec for rec in caplog.records if "handler raised" in rec.message]
    assert matching, "expected a logged record about the handler raising"


async def test_async_handler_exception_isolated(
    budget: Budget, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="snipz.events")
    second_called: list[bool] = []

    async def bad_handler(_r: Reservation) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("simulated async failure")

    async def good_handler(_r: Reservation) -> None:
        await asyncio.sleep(0)
        second_called.append(True)

    budget.on_committed(bad_handler)
    budget.on_committed(good_handler)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))  # must not raise

    assert second_called == [True]
    matching = [rec for rec in caplog.records if "handler raised" in rec.message]
    assert matching


# ---------------------------------------------------------------------------
# Idempotency paths MUST NOT re-fire
# ---------------------------------------------------------------------------


async def test_idempotent_cached_reserve_does_not_fire(budget: Budget) -> None:
    """A second reserve() with the same request_id returns cached — no event."""
    seen: list[Reservation] = []
    budget.on_reserved(seen.append)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    r1 = await budget.reserve(scope, Decimal("100"), request_id="req-1")
    r2 = await budget.reserve(scope, Decimal("100"), request_id="req-1")

    assert r1.id == r2.id
    assert len(seen) == 1


async def test_idempotent_recommit_does_not_fire(budget: Budget) -> None:
    """Calling commit() twice on the same Reservation is a no-op the second time."""
    seen: list[Reservation] = []
    budget.on_committed(seen.append)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))
    await r.commit(Decimal("80"))

    assert len(seen) == 1


async def test_release_after_settled_does_not_fire(budget: Budget) -> None:
    seen: list[Reservation] = []
    budget.on_released(seen.append)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))
    await r.release()  # no-op: already committed

    assert seen == []


# ---------------------------------------------------------------------------
# Decorator usage
# ---------------------------------------------------------------------------


async def test_on_committed_can_be_used_as_decorator(budget: Budget) -> None:
    seen: list[Reservation] = []

    @budget.on_committed
    def my_handler(r: Reservation) -> None:
        seen.append(r)

    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    r = await budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))

    assert len(seen) == 1
    assert seen[0].id == r.id
    # The decorator returns the original handler so it remains callable.
    assert my_handler is not None
