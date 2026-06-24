"""Postgres backend integration tests.

These exercise the same lifecycle paths as ``test_phase1.py`` but
against a real Postgres instance running in a testcontainers Docker
container. Skipped by default; run with ``pytest --postgres``.

Coverage in this file is intentionally a subset of ``test_phase1.py`` —
just enough to verify the core invariants on Postgres. Full conformance
testing across both dialects lives in the Phase 8.5 conformance suite.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from snipz import Budget, BudgetExceededError, InvalidStateError, Scope

if TYPE_CHECKING:
    from tests.conftest import FrozenClock


pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_pg_reserve_under_cap_succeeds(pg_budget: Budget) -> None:
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("500"))

    r = await pg_budget.reserve(scope, Decimal("100"))

    assert r.state == "reserved"
    assert r.estimated_cents == Decimal("100")
    assert r.actual_cents is None
    assert r.scopes == (scope,)


async def test_pg_reserve_over_cap_raises(pg_budget: Budget) -> None:
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("100"))

    with pytest.raises(BudgetExceededError) as excinfo:
        await pg_budget.reserve(scope, Decimal("150"))

    assert excinfo.value.scope == scope
    assert excinfo.value.cap_cents == Decimal("100")
    assert excinfo.value.attempted_cents == Decimal("150")
    assert excinfo.value.spent_cents == Decimal("0")


async def test_pg_reserve_with_no_limit_succeeds_in_tracker_mode(
    pg_budget: Budget,
) -> None:
    """Without a configured limit, reserve always succeeds (audit-only mode)."""
    scope = Scope("user", "untracked")
    r = await pg_budget.reserve(scope, Decimal("99999999"))
    assert r.state == "reserved"


async def test_pg_commit_settles_reservation(pg_budget: Budget) -> None:
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("500"))

    r = await pg_budget.reserve(scope, Decimal("100"))
    await r.commit(Decimal("80"))

    assert r.state == "committed"
    assert r.actual_cents == Decimal("80")


async def test_pg_release_refunds_budget(pg_budget: Budget) -> None:
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("100"))

    r = await pg_budget.reserve(scope, Decimal("80"))
    await r.release()

    assert r.state == "released"
    # Released budget is reclaimed — a second reserve at the cap succeeds.
    r2 = await pg_budget.reserve(scope, Decimal("80"))
    assert r2.state == "reserved"


async def test_pg_async_context_manager_auto_commits_on_success(
    pg_budget: Budget,
) -> None:
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("500"))

    async with await pg_budget.reserve(scope, Decimal("100")) as r:
        await r.observe(Decimal("90"))

    assert r.state == "committed"
    assert r.actual_cents == Decimal("90")


async def test_pg_async_context_manager_auto_releases_on_exception(
    pg_budget: Budget,
) -> None:
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("500"))

    class _SyntheticError(Exception):
        pass

    with pytest.raises(_SyntheticError):
        async with await pg_budget.reserve(scope, Decimal("100")) as r:
            raise _SyntheticError("call failed")

    assert r.state == "released"


# ---------------------------------------------------------------------------
# Concurrency: the headline correctness story
# ---------------------------------------------------------------------------


async def test_pg_concurrent_reserves_serialize_at_cap(pg_budget: Budget) -> None:
    """Two concurrent reserves at the cap: one wins, one raises.

    This is the proof that ``SELECT FOR UPDATE`` does what it says on
    Postgres. SQLite gets the same property via ``BEGIN IMMEDIATE``;
    here we verify the row-level lock implementation holds.
    """
    scope = Scope("user", "u_concurrent")
    await pg_budget.set_limit(scope, Decimal("100"))
    # spend $0.85 first; only one of two $0.10 reserves should fit.
    pre = await pg_budget.reserve(scope, Decimal("85"))
    await pre.commit()

    results = await asyncio.gather(
        pg_budget.reserve(scope, Decimal("10")),
        pg_budget.reserve(scope, Decimal("10")),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BudgetExceededError)]

    assert len(successes) == 1, f"expected exactly one success, got {results}"
    assert len(failures) == 1, f"expected exactly one BudgetExceededError, got {results}"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_pg_request_id_idempotency(pg_budget: Budget) -> None:
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("500"))

    r1 = await pg_budget.reserve(scope, Decimal("100"), request_id="req-abc")
    r2 = await pg_budget.reserve(scope, Decimal("100"), request_id="req-abc")

    assert r1.id == r2.id
    assert r1.request_id == "req-abc"


async def test_pg_request_id_concurrent_retries_converge(pg_budget: Budget) -> None:
    """N concurrent reserves with the same request_id produce one ledger row."""
    scope = Scope("user", "u_idem")
    await pg_budget.set_limit(scope, Decimal("500"))

    results = await asyncio.gather(
        *(pg_budget.reserve(scope, Decimal("50"), request_id="dup") for _ in range(5))
    )
    ids = {r.id for r in results}
    assert len(ids) == 1, f"all retries should converge on one id; got {ids}"


# ---------------------------------------------------------------------------
# Composite scopes
# ---------------------------------------------------------------------------


async def test_pg_composite_scope_all_caps_enforced(pg_budget: Budget) -> None:
    user = Scope("user", "u1")
    tenant = Scope("tenant", "acme")
    await pg_budget.set_limit(user, Decimal("200"))
    await pg_budget.set_limit(tenant, Decimal("80"))

    # User cap is $2.00 (passes), tenant cap is $0.80 (fails for $1.00 attempt).
    with pytest.raises(BudgetExceededError) as excinfo:
        await pg_budget.reserve([user, tenant], Decimal("100"))

    assert excinfo.value.scope == tenant


async def test_pg_composite_scope_settles_atomically(pg_budget: Budget) -> None:
    user = Scope("user", "u1")
    tenant = Scope("tenant", "acme")
    await pg_budget.set_limit(user, Decimal("500"))
    await pg_budget.set_limit(tenant, Decimal("500"))

    r = await pg_budget.reserve([user, tenant], Decimal("100"))
    assert len(r.scopes) == 2
    await r.commit(Decimal("75"))
    assert r.state == "committed"


# ---------------------------------------------------------------------------
# Late commit (TTL expiry → sweep → late commit)
# ---------------------------------------------------------------------------


async def test_pg_late_commit_after_sweep(
    clocked_pg_budget: tuple[Budget, FrozenClock],
) -> None:
    pg_budget, clock = clocked_pg_budget
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("500"))

    r = await pg_budget.reserve(scope, Decimal("100"), ttl=300)
    clock.advance(301)
    released = await pg_budget.sweep()
    assert released == 1

    # Reservation row is now released[late=true]; the in-memory state
    # hasn't been refreshed but commit still succeeds and flips it.
    await r.commit(Decimal("80"))
    assert r.state == "committed"
    assert r.late is True


async def test_pg_caller_release_blocks_late_commit(pg_budget: Budget) -> None:
    """A caller-released row (late=False) cannot be committed late."""
    scope = Scope("user", "u1")
    await pg_budget.set_limit(scope, Decimal("500"))

    r = await pg_budget.reserve(scope, Decimal("100"))
    await r.release()

    with pytest.raises(InvalidStateError):
        await r.commit(Decimal("80"))


# ---------------------------------------------------------------------------
# Postgres-specific: pool injection
# ---------------------------------------------------------------------------


async def test_pg_injected_pool_is_not_closed_by_backend() -> None:
    """When the user injects a pool, ``Backend.close()`` MUST NOT close it.

    Caller owns the pool's lifecycle; only the backend's *managed* pool
    is drained on close.
    """
    import asyncpg
    from testcontainers.postgres import PostgresContainer

    from snipz.storage.postgres import PostgresBackend

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{host}:{port}/{container.dbname}"
        )

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            backend = PostgresBackend(pool=pool)
            budget = Budget(backend)
            await budget.migrate()
            await budget.set_limit(Scope("user", "u1"), Decimal("100"))
            await budget.close()

            # Pool must still be usable after backend.close().
            async with pool.acquire() as conn:
                value = await conn.fetchval("SELECT 1")
                assert value == 1
        finally:
            await pool.close()
    finally:
        container.stop()
