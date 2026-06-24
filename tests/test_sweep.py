"""Tests for :mod:`snipz.sweep` and the ``snipz sweep`` CLI subcommand.

Covers:

* :func:`sweep_once` against a real expired reservation (via the
  ``clocked_budget`` fixture).
* :func:`sweep_loop` exit behaviour: the stop event wakes the loop
  promptly mid-interval.
* :func:`sweep_loop` error recovery: a single iteration that raises
  does not kill the loop.
* Argument validation: zero / negative ``interval``.
* CLI argparse plumbing for the ``sweep`` subcommand.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from snipz import Budget, Scope
from snipz.cli import main as cli_main
from snipz.sweep import sweep_loop, sweep_once

if TYPE_CHECKING:
    from tests.conftest import FrozenClock


# ---------------------------------------------------------------------------
# sweep_once — single sweep against a real expired reservation
# ---------------------------------------------------------------------------


async def test_sweep_once_releases_expired_reservation(
    clocked_budget: tuple[Budget, FrozenClock],
) -> None:
    budget, clock = clocked_budget
    scope = Scope("user", "u1")
    await budget.set_limit(scope, Decimal("500"))
    await budget.reserve(scope, Decimal("100"), ttl=300)

    clock.advance(301)  # past TTL
    released = await sweep_once(budget)

    assert released == 1


async def test_sweep_once_returns_zero_when_nothing_expired(budget: Budget) -> None:
    released = await sweep_once(budget)
    assert released == 0


# ---------------------------------------------------------------------------
# sweep_loop — stop-event exit
# ---------------------------------------------------------------------------


async def test_sweep_loop_exits_promptly_when_stop_set(budget: Budget) -> None:
    """Setting stop mid-interval must wake the loop within ms, not seconds."""
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    setter = asyncio.create_task(stop_soon())

    # interval=10s — loop would block 10s if stop didn't wake it.
    total = await asyncio.wait_for(
        sweep_loop(budget, interval=10.0, stop=stop),
        timeout=1.0,
    )
    await setter
    assert total >= 0  # at least one sweep ran before stop was set


async def test_sweep_loop_runs_multiple_iterations(budget: Budget) -> None:
    """With a short interval, several sweeps execute before stop is set."""
    stop = asyncio.Event()
    counter = {"sweeps": 0}

    real_sweep = budget.sweep

    async def counting_sweep() -> int:
        counter["sweeps"] += 1
        return await real_sweep()

    budget.sweep = counting_sweep  # type: ignore[method-assign]

    async def stop_after_three() -> None:
        # Wait long enough for ~3 iterations at 20ms intervals.
        await asyncio.sleep(0.08)
        stop.set()

    setter = asyncio.create_task(stop_after_three())
    await asyncio.wait_for(
        sweep_loop(budget, interval=0.02, stop=stop),
        timeout=1.0,
    )
    await setter

    assert counter["sweeps"] >= 2, f"expected >= 2 iterations, got {counter['sweeps']}"


# ---------------------------------------------------------------------------
# sweep_loop — error recovery
# ---------------------------------------------------------------------------


async def test_sweep_loop_continues_after_iteration_error(budget: Budget) -> None:
    """One iteration raising must not kill the loop."""
    stop = asyncio.Event()
    calls = {"count": 0}

    real_sweep = budget.sweep

    async def flaky_sweep() -> int:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated transient failure")
        return await real_sweep()

    budget.sweep = flaky_sweep  # type: ignore[method-assign]

    async def stop_after_two_iterations() -> None:
        await asyncio.sleep(0.08)
        stop.set()

    setter = asyncio.create_task(stop_after_two_iterations())
    await asyncio.wait_for(
        sweep_loop(budget, interval=0.02, stop=stop),
        timeout=1.0,
    )
    await setter

    # Loop survived the raise and called sweep again.
    assert calls["count"] >= 2


# ---------------------------------------------------------------------------
# sweep_loop — validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_interval", [0, -1, -0.5])
async def test_sweep_loop_rejects_non_positive_interval(
    budget: Budget, bad_interval: float
) -> None:
    with pytest.raises(ValueError, match="interval must be positive"):
        await sweep_loop(budget, interval=bad_interval)


# ---------------------------------------------------------------------------
# CLI — `snipz sweep` argparse + one-shot path
# ---------------------------------------------------------------------------


def test_cli_sweep_one_shot_returns_zero_on_empty_db(tmp_path: Path) -> None:
    """`snipz sweep --db PATH` with no expired reservations exits 0."""
    db = tmp_path / "snipz.db"

    # Migrate the database so the schema exists before sweeping.
    async def setup() -> None:
        budget = Budget(db)
        await budget.migrate()
        await budget.close()

    asyncio.run(setup())

    exit_code = cli_main(["sweep", "--db", str(db)])
    assert exit_code == 0


def test_cli_sweep_requires_db(capsys: pytest.CaptureFixture[str]) -> None:
    """`snipz sweep` without --db must fail argparse validation."""
    with pytest.raises(SystemExit):
        cli_main(["sweep"])
    err = capsys.readouterr().err
    assert "--db" in err
