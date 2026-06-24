"""Reservation sweeper — expires stuck reservations.

Two entry points:

* :func:`sweep_once` — run one sweep, return the count released. For
  cron / k8s ``CronJob`` / systemd timer integration.
* :func:`sweep_loop` — loop forever (or until ``stop`` is set) sweeping
  every ``interval`` seconds. For long-running processes.

The :mod:`snipz.cli` subcommand ``snipz sweep`` wires :func:`sweep_loop`
up with portable SIGINT/SIGTERM handlers so a graceful stop just works.

Per-iteration errors are logged and swallowed — the loop survives
transient DB hiccups. A persistent failure mode (bad config, schema
drift) will fill the log; an operator monitoring the log can react.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from snipz import Budget

__all__ = ["sweep_loop", "sweep_once"]


_LOGGER: Final = logging.getLogger("snipz.sweep")


async def sweep_once(budget: Budget) -> int:
    """Run a single sweep, returning the count of released reservations.

    Logs the result at ``INFO`` level with the duration.
    """
    start = time.monotonic()
    released = await budget.sweep()
    duration = time.monotonic() - start
    _LOGGER.info("sweep complete: released=%d in %.3fs", released, duration)
    return released


async def sweep_loop(
    budget: Budget,
    *,
    interval: float,
    stop: asyncio.Event | None = None,
) -> int:
    """Loop calling :func:`sweep_once` every ``interval`` seconds.

    Returns the cumulative count of released reservations across all
    iterations. Per-iteration errors are caught, logged at ``ERROR``
    level, and the loop continues.

    The loop exits when ``stop`` is set (if provided) or when the task
    is cancelled. ``stop`` is checked both before and after each sleep
    so a signal received mid-interval wakes the loop promptly.

    Raises :class:`ValueError` if ``interval`` is not strictly positive.
    """
    if interval <= 0:
        raise ValueError(f"interval must be positive, got {interval}")

    total = 0

    while True:
        try:
            total += await sweep_once(budget)
        except Exception:
            _LOGGER.exception("sweep iteration failed; loop continuing")

        if stop is not None and stop.is_set():
            return total

        if stop is None:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return total
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return total  # stop was set during the wait
            except TimeoutError:
                pass  # interval elapsed; sweep again
            except asyncio.CancelledError:
                return total
