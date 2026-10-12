"""Model-based check that running spend totals always match the ledger.

Drives a seeded random sequence of every ledger operation — reserve
(single and composite), observe, commit, release, sweep, late commit,
``set_limit`` — while a frozen clock jumps across minute, hour, and day
boundaries. After every step, each stored total is compared with an
independent recomputation from the raw ledger rows
(:mod:`tests.ledger_audit`). Periodically every total is invalidated and
rebuilt, checking the database's SQL rebuild agrees with the
incrementally maintained value.

300 steps by default; ``pytest --load`` runs 3,000. Postgres variant
needs ``--postgres``.
"""

from __future__ import annotations

import contextlib
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from snipz import Budget, BudgetExceededError, Reservation, Scope
from tests.conftest import FrozenClock
from tests.ledger_audit import (
    TotalCheck,
    check_totals_postgres,
    check_totals_sqlite,
    invalidate_totals_postgres,
    invalidate_totals_sqlite,
)

# Each scope's capped windows. u3 starts uncapped (tracker mode) and gains a
# limit mid-run, exercising totals created after the scope already has rows.
LIMITS: dict[Scope, Decimal] = {
    Scope("user", "u1", "hour"): Decimal("400"),
    Scope("user", "u1", "month"): Decimal("5000"),
    Scope("user", "u2", "day"): Decimal("800"),
    Scope("org", "o1", "minute"): Decimal("60"),
    Scope("org", "o1", "lifetime"): Decimal("100000"),
}
LATE_LIMIT = Scope("user", "u3", "minute")
SCOPES = [*LIMITS, LATE_LIMIT]


@dataclass
class Ledger:
    budget: Budget
    clock: FrozenClock
    check_totals: Callable[[], Awaitable[list[TotalCheck]]]
    invalidate_totals: Callable[[], Awaitable[None]]


@pytest.fixture
def steps(pytestconfig: pytest.Config) -> int:
    return 3_000 if pytestconfig.getoption("--load") else 300


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
async def ledger(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Ledger]:
    clock = FrozenClock(datetime(2026, 10, 12, 23, 58, tzinfo=UTC))
    if request.param == "sqlite":
        db_path = tmp_path / "consistency.db"
        budget = Budget(db_path, now=clock)
        await budget.migrate()
        try:
            yield Ledger(
                budget,
                clock,
                lambda: check_totals_sqlite(db_path),
                lambda: invalidate_totals_sqlite(db_path),
            )
        finally:
            await budget.close()
        return

    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )
        budget = Budget(dsn, now=clock)
        await budget.migrate()
        try:
            yield Ledger(
                budget,
                clock,
                lambda: check_totals_postgres(dsn),
                lambda: invalidate_totals_postgres(dsn),
            )
        finally:
            await budget.close()
    finally:
        container.stop()


def _amount(rng: random.Random) -> Decimal:
    """Up to 5 cents with 7 decimal places, so storage rounding is exercised."""
    return Decimal(rng.randint(1, 50_000_000)) / Decimal(10_000_000)


async def _assert_totals_match_ledger(ledger: Ledger, step: int, op: str) -> None:
    mismatches = [c for c in await ledger.check_totals() if c.stored_micros != c.expected_micros]
    assert not mismatches, f"step {step} ({op}): {mismatches}"


async def _rebuild_all(ledger: Ledger) -> None:
    """Invalidate every total and force a rebuild through a cap check."""
    await ledger.invalidate_totals()
    for scope in [*LIMITS, LATE_LIMIT]:
        try:
            reservation = await ledger.budget.reserve(scope, Decimal(0))
        except BudgetExceededError:
            continue
        await reservation.release()


async def test_running_totals_always_match_the_ledger(ledger: Ledger, steps: int) -> None:
    rng = random.Random(42)
    budget = ledger.budget
    for scope, cap in LIMITS.items():
        await budget.set_limit(scope, cap)

    open_reservations: list[Reservation] = []
    swept: list[Reservation] = []
    late_limit_set = False

    for step in range(steps):
        op = rng.choices(
            ["reserve", "observe", "commit", "release", "advance", "sweep", "late", "set_limit"],
            weights=[30, 8, 20, 8, 10, 3, 3, 2],
        )[0]

        if op == "reserve":
            scopes = rng.sample(SCOPES, k=rng.choice([1, 1, 2]))
            if len({(s.type, s.id) for s in scopes}) < len(scopes):
                scopes = scopes[:1]  # one row per (type, id) per reservation
            with contextlib.suppress(BudgetExceededError):
                open_reservations.append(
                    await budget.reserve(scopes, _amount(rng), ttl=rng.choice([60, 600, 7200]))
                )
        elif op in ("observe", "commit", "release") and open_reservations:
            reservation = open_reservations[rng.randrange(len(open_reservations))]
            if op == "observe" and reservation.state == "reserved":
                await reservation.observe(_amount(rng) * rng.choice([1, 2]))
            elif op == "commit":
                await reservation.commit(_amount(rng))
                open_reservations.remove(reservation)
            elif op == "release":
                await reservation.release()
                open_reservations.remove(reservation)
        elif op == "advance":
            # Seconds to days: crosses every window boundary over a run.
            ledger.clock.advance(rng.choice([1, 30, 61, 600, 3_601, 86_401]))
        elif op == "sweep":
            if await budget.sweep():
                expired = [r for r in open_reservations if r.expires_at < ledger.clock()]
                for reservation in expired:
                    reservation.state, reservation.late = "released", True
                    open_reservations.remove(reservation)
                swept.extend(expired)
        elif op == "late" and swept:
            await swept.pop(rng.randrange(len(swept))).commit(_amount(rng))
        elif op == "set_limit":
            if not late_limit_set:
                await budget.set_limit(LATE_LIMIT, Decimal("50"))
                late_limit_set = True
            else:
                scope, cap = rng.choice(list(LIMITS.items()))
                await budget.set_limit(scope, cap * rng.choice([1, 2]))

        await _assert_totals_match_ledger(ledger, step, op)
        if step % 50 == 49:
            await _rebuild_all(ledger)
            await _assert_totals_match_ledger(ledger, step, "rebuild")

    # The run actually exercised the interesting paths.
    assert late_limit_set
    assert any(c.stored_micros for c in await ledger.check_totals())
