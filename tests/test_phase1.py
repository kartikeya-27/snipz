"""Phase 1 tests — reservation lifecycle, concurrency, idempotency, late commit.

These exercise the core engine end-to-end against the SQLite backend.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from calyx import Budget, BudgetExceededError, InvalidStateError, Reservation, Scope

if TYPE_CHECKING:
    from tests.conftest import FrozenClock


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_reserve_under_cap_succeeds(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    r = await budget.reserve(scope, Decimal("100"))

    assert r.state == "reserved"
    assert r.estimated_cents == Decimal("100")
    assert r.actual_cents is None
    assert r.scopes == (scope,)


async def test_reserve_over_cap_raises(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    with pytest.raises(BudgetExceededError) as excinfo:
        await budget.reserve(scope, Decimal("150"))

    assert excinfo.value.scope == scope
    assert excinfo.value.cap_cents == Decimal("100")
    assert excinfo.value.attempted_cents == Decimal("150")
    assert excinfo.value.spent_cents == Decimal("0")


async def test_reserve_with_no_limit_succeeds_in_tracker_mode(budget: Budget) -> None:
    """Without a configured limit, reservations always succeed (spend tracking only)."""
    scope = Scope("user", "u1")
    r = await budget.reserve(scope, Decimal("999999"))
    assert r.state == "reserved"


async def test_commit_settles_reservation(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    r = await budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))

    assert r.state == "committed"
    assert r.actual_cents == Decimal("80")


async def test_commit_idempotent(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    r = await budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))
    await r.commit(Decimal("80"))  # should be a no-op
    assert r.state == "committed"


async def test_release_refunds_reservation(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    r = await budget.reserve(scope, Decimal("80"))
    await r.release()
    assert r.state == "released"

    # Released cost is excluded from cap-check; new reservation succeeds.
    r2 = await budget.reserve(scope, Decimal("80"))
    assert r2.state == "reserved"


async def test_release_idempotent(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    r = await budget.reserve(scope, Decimal("80"))
    await r.release()
    await r.release()  # no-op
    assert r.state == "released"


# ---------------------------------------------------------------------------
# observe() and the GREATEST/MAX cap-check formula
# ---------------------------------------------------------------------------


async def test_observe_updates_actual_cents(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    r = await budget.reserve(scope, Decimal("100"))
    await r.observe(Decimal("50"))

    assert r.actual_cents == Decimal("50")


async def test_observe_overrun_blocks_subsequent_reservations(budget: Budget) -> None:
    """If observe() pushes actual past estimate, cap-check uses the higher number."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    r1 = await budget.reserve(scope, Decimal("50"))
    await r1.observe(Decimal("80"))  # overrun: actual exceeds estimate

    # Cap remaining = 100 - max(80, 50) = 20.
    with pytest.raises(BudgetExceededError):
        await budget.reserve(scope, Decimal("30"))

    r3 = await budget.reserve(scope, Decimal("15"))
    assert r3.state == "reserved"


async def test_commit_under_estimate_releases_budget(budget: Budget) -> None:
    """Once committed, only actual_cents counts — not the higher estimate."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    r1 = await budget.reserve(scope, Decimal("80"))
    await r1.commit(Decimal("30"))  # came in cheap

    # Spent now = 30; remaining = 70.
    r2 = await budget.reserve(scope, Decimal("60"))
    assert r2.state == "reserved"


async def test_observe_negative_raises(budget: Budget) -> None:
    scope = Scope("user", "u1")
    r = await budget.reserve(scope, Decimal("100"))
    with pytest.raises(ValueError):
        await r.observe(Decimal("-1"))


# ---------------------------------------------------------------------------
# Async context manager
# ---------------------------------------------------------------------------


async def test_async_with_auto_commits_on_success(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    reservation = await budget.reserve(scope, Decimal("100"))
    async with reservation as r:
        await r.observe(Decimal("80"))

    assert reservation.state == "committed"
    assert reservation.actual_cents == Decimal("80")


async def test_async_with_auto_releases_on_exception(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    class _BoomError(Exception):
        pass

    reservation = await budget.reserve(scope, Decimal("80"))
    with pytest.raises(_BoomError):
        async with reservation as r:
            await r.observe(Decimal("60"))
            raise _BoomError

    assert reservation.state == "released"

    # Budget freed; next reservation succeeds.
    r2 = await budget.reserve(scope, Decimal("80"))
    assert r2.state == "reserved"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_idempotent_reserve_returns_same_reservation(budget: Budget) -> None:
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    r1 = await budget.reserve(scope, Decimal("100"), request_id="req_abc")
    r2 = await budget.reserve(scope, Decimal("100"), request_id="req_abc")

    assert r1.id == r2.id
    assert r2.state == "reserved"


async def test_idempotent_reserve_after_commit(budget: Budget) -> None:
    """Retrying the same request_id after commit returns the committed reservation."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    r1 = await budget.reserve(scope, Decimal("100"), request_id="req_abc")
    await r1.commit(Decimal("80"))

    r2 = await budget.reserve(scope, Decimal("100"), request_id="req_abc")
    assert r2.id == r1.id
    assert r2.state == "committed"


async def test_concurrent_idempotent_retries_collapse(budget: Budget) -> None:
    """Ten parallel retries with the same request_id produce one ledger row group."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("10000"))

    results = await asyncio.gather(
        *(
            budget.reserve(scope, Decimal("100"), request_id="dedup_key")
            for _ in range(10)
        )
    )

    ids = {r.id for r in results}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Concurrency — burst at cap edge
# ---------------------------------------------------------------------------


async def test_concurrent_burst_at_cap_edge(budget: Budget) -> None:
    """50 concurrent reservations of 20c against a 500c cap — exactly 25 succeed."""
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))

    async def attempt() -> Reservation | None:
        try:
            return await budget.reserve(scope, Decimal("20"))
        except BudgetExceededError:
            return None

    results = await asyncio.gather(*(attempt() for _ in range(50)))
    successes = [r for r in results if r is not None]
    failures = [r for r in results if r is None]

    assert len(successes) == 25, "exactly 25 of 50 reservations should fit a 500c cap"
    assert len(failures) == 25
    total_reserved = sum((r.estimated_cents for r in successes), Decimal("0"))
    assert total_reserved == Decimal("500")


# ---------------------------------------------------------------------------
# Composite scopes
# ---------------------------------------------------------------------------


async def test_composite_scope_partial_failure_aborts_all(budget: Budget) -> None:
    """If any scope fails its cap-check, no scope is debited."""
    user = Scope("user", "u1")
    tenant = Scope("tenant", "t1")

    await budget.set_limit(user, Decimal("100"))
    await budget.set_limit(tenant, Decimal("200"))

    # Pre-fill tenant nearly to its cap.
    pre = await budget.reserve(tenant, Decimal("199"))
    await pre.commit()

    # Reserving against both should fail at tenant; user must remain untouched.
    with pytest.raises(BudgetExceededError) as excinfo:
        await budget.reserve([user, tenant], Decimal("5"))

    assert excinfo.value.scope == tenant

    # User scope had nothing inserted — full cap available.
    r = await budget.reserve(user, Decimal("99"))
    assert r.state == "reserved"


async def test_composite_scope_both_pass_share_reservation_id(budget: Budget) -> None:
    user = Scope("user", "u1")
    tenant = Scope("tenant", "t1")
    await budget.set_limit(user, Decimal("100"))
    await budget.set_limit(tenant, Decimal("200"))

    r = await budget.reserve([user, tenant], Decimal("50"))

    assert set(r.scopes) == {user, tenant}
    await r.commit(Decimal("40"))
    assert r.state == "committed"


# ---------------------------------------------------------------------------
# Sweeper + late commit
# ---------------------------------------------------------------------------


async def test_sweeper_releases_expired_reservations(
    clocked_budget: tuple[Budget, FrozenClock],
) -> None:
    budget, clock = clocked_budget
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    await budget.reserve(scope, Decimal("80"), ttl=60)
    clock.advance(61)

    released = await budget.sweep()
    assert released == 1

    # Released budget is freed.
    r2 = await budget.reserve(scope, Decimal("80"))
    assert r2.state == "reserved"


async def test_late_commit_after_sweeper_marks_late(
    clocked_budget: tuple[Budget, FrozenClock],
) -> None:
    budget, clock = clocked_budget
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("200"))

    r1 = await budget.reserve(scope, Decimal("80"), ttl=60)
    clock.advance(61)
    await budget.sweep()

    # Late commit — should succeed and mark late=True.
    await r1.commit(Decimal("60"))

    assert r1.state == "committed"
    assert r1.late is True
    assert r1.actual_cents == Decimal("60")


async def test_late_commit_counts_in_subsequent_cap_checks(
    clocked_budget: tuple[Budget, FrozenClock],
) -> None:
    """Late-committed cost must show up in spend totals."""
    budget, clock = clocked_budget
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("100"))

    r1 = await budget.reserve(scope, Decimal("80"), ttl=60)
    clock.advance(61)
    await budget.sweep()
    await r1.commit(Decimal("60"))  # late commit

    # 60c late-committed; 40c remaining.
    with pytest.raises(BudgetExceededError):
        await budget.reserve(scope, Decimal("50"))

    r2 = await budget.reserve(scope, Decimal("30"))
    assert r2.state == "reserved"


async def test_caller_release_is_terminal(budget: Budget) -> None:
    """commit() on a row the caller explicitly released must raise."""
    scope = Scope("user", "u1")
    r = await budget.reserve(scope, Decimal("80"))
    await r.release()

    with pytest.raises(InvalidStateError):
        await r.commit(Decimal("60"))
