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
from typing import TYPE_CHECKING, Any

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


# ---------------------------------------------------------------------------
# Pricing overrides (migration 0002 columns)
# ---------------------------------------------------------------------------


async def test_pg_pricing_override_round_trips_1h_rate_and_tiers() -> None:
    """``snipz_pricing.cache_write_1h_cents_per_m`` and ``.tiers`` (migration
    0002) load through ``Pricing.with_backend`` on Postgres, and re-running
    ``migrate()`` is a no-op."""
    import asyncpg
    from testcontainers.postgres import PostgresContainer

    from snipz import PriceTier, Pricing
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

        backend = PostgresBackend(dsn)
        try:
            await backend.migrate()
            await backend.migrate()

            conn = await asyncpg.connect(dsn)
            try:
                assert await conn.fetchval("SELECT MAX(version) FROM snipz_schema_version") == 2
                await conn.execute(
                    "INSERT INTO snipz_pricing (provider, model, input_cents_per_m, "
                    "output_cents_per_m, cache_write_1h_cents_per_m, tiers) "
                    "VALUES ('custom', 'tiered', 10, 50, 20, $1)",
                    '[{"above_tokens": 100000, "input_cents_per_m": "50"}]',
                )
            finally:
                await conn.close()

            entry = (await Pricing.with_backend(backend)).get("custom", "tiered")
            assert entry is not None
            assert entry.cache_write_1h_cents_per_m == Decimal("20")
            assert entry.tiers == (
                PriceTier(above_tokens=100_000, input_cents_per_m=Decimal("50")),
            )
        finally:
            await backend.close()
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Running spend totals under real concurrency (migration 0003)
# ---------------------------------------------------------------------------


def _pg_dsn(container: Any) -> str:
    return (
        f"postgresql://{container.username}:{container.password}"
        f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
        f"/{container.dbname}"
    )


async def test_pg_limit_created_mid_traffic_counts_in_flight_writes() -> None:
    """``set_limit`` on a busy uncapped scope must not miss reservations in
    flight at that moment: the exclusive scope lock waits them out."""
    from testcontainers.postgres import PostgresContainer

    from tests.ledger_audit import check_totals_postgres

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        dsn = _pg_dsn(container)
        budget = Budget(dsn)
        await budget.migrate()
        scope = Scope("tenant", "busy")
        try:

            async def traffic(i: int) -> None:
                reservation = await budget.reserve(scope, Decimal("1"))
                await asyncio.sleep(0.001 * (i % 5))
                await reservation.commit(Decimal("0.5"))

            async def add_limit() -> None:
                await asyncio.sleep(0.05)
                await budget.set_limit(scope, Decimal("1000000"))

            await asyncio.gather(add_limit(), *(traffic(i) for i in range(400)))
            # Rebuild the total, then keep using it.
            for _ in range(20):
                reservation = await budget.reserve(scope, Decimal("1"))
                await reservation.commit(Decimal("0.5"))
        finally:
            await budget.close()

        checks = await check_totals_postgres(dsn)
        assert checks, "the total was never rebuilt"
        assert all(c.stored_micros == c.expected_micros for c in checks), checks
    finally:
        container.stop()


async def test_pg_composite_writes_and_sweeps_do_not_deadlock() -> None:
    """Overlapping composite reservations committing, releasing, and being
    swept concurrently: every scope is locked in one global order, so no
    deadlock or lock-timeout error, and every total matches the ledger."""
    from datetime import UTC, datetime

    from testcontainers.postgres import PostgresContainer

    from tests.conftest import FrozenClock
    from tests.ledger_audit import check_totals_postgres

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        dsn = _pg_dsn(container)
        clock = FrozenClock(datetime(2026, 10, 12, 12, tzinfo=UTC))
        budget = Budget(dsn, now=clock)
        await budget.migrate()
        scopes = [Scope("user", f"u{i}", "month") for i in range(4)]
        for scope in scopes:
            await budget.set_limit(scope, Decimal("1000000"))

        async def worker(i: int) -> None:
            pair = [scopes[i % 4], scopes[(i + 1) % 4]]
            if i % 2:
                pair.reverse()  # callers pass scopes in any order
            reservation = await budget.reserve(pair, Decimal("2"), ttl=1 if i % 7 == 0 else 300)
            await asyncio.sleep(0)
            if i % 3 == 0:
                await reservation.release()
            else:
                await reservation.commit(Decimal("1"))

        async def sweeper() -> None:
            for _ in range(20):
                clock.advance(0.1)
                await budget.sweep()
                await asyncio.sleep(0)

        try:
            results = await asyncio.gather(
                sweeper(), *(worker(i) for i in range(300)), return_exceptions=True
            )
        finally:
            await budget.close()

        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, errors[:3]
        checks = await check_totals_postgres(dsn)
        assert len(checks) == len(scopes)
        assert all(c.stored_micros == c.expected_micros for c in checks), checks
    finally:
        container.stop()
