"""Experimental sync wrapper around the async core.

For callers who do not want to fight asyncio. Each sync method
dispatches its coroutine onto a per-process background event loop
running in a daemon thread, and blocks until the result is available.

**Re-entrancy is not supported.** Calling a sync method from inside an
active event loop in the same thread raises :class:`RuntimeError`
rather than deadlocking. If you are in an async context, use
:mod:`snipz.core` (the async API) directly.

The background loop starts lazily on the first sync call and is shut
down at interpreter exit via :mod:`atexit`. Per-process; do not rely on
it across multiprocessing forks.

Marked **experimental** in Phase 2; promoted to stable in Phase 3 after
exercise. The API surface is intentionally identical to
:mod:`snipz.core` so the only difference is the `await` keyword.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from collections.abc import Coroutine, Sequence
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from snipz import core as _async
from snipz.core import BudgetExceededError, InvalidStateError, Scope
from snipz.storage import Backend

__all__ = [
    "Budget",
    "BudgetExceededError",
    "InvalidStateError",
    "Reservation",
    "Scope",
]


# ---------------------------------------------------------------------------
# Background event loop singleton
# ---------------------------------------------------------------------------


_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start the background event loop on first sync call.

    Thread-safe; idempotent. The loop runs in a daemon thread that
    exits with the process; ``atexit`` handles polite shutdown.
    """
    global _loop, _thread
    loop = _loop
    if loop is not None:
        return loop
    with _lock:
        # Re-check under the lock (classic double-checked locking).
        loop = _loop
        if loop is not None:
            return loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever,
            daemon=True,
            name="snipz-sync-loop",
        )
        thread.start()
        atexit.register(_shutdown)
        _loop = loop
        _thread = thread
    return loop


def _shutdown() -> None:
    """Stop the background loop politely at process exit.

    Idempotent; safe to call multiple times. Daemon thread cleanup is
    handled by the runtime regardless, but stopping the loop avoids
    noisy ``Task was destroyed but it is pending`` warnings.
    """
    global _loop, _thread
    loop = _loop
    thread = _thread
    if loop is None:
        return
    # Loop may already be closed if shutdown was racy.
    with suppress(RuntimeError):
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
    _loop = None
    _thread = None


def _in_running_loop() -> bool:
    """Return True if an event loop is running in the current thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Dispatch a coroutine onto the background loop and block until done.

    Raises :class:`RuntimeError` if called from inside an active event
    loop in the current thread — avoids deadlock by failing loudly.
    """
    if _in_running_loop():
        # Close the coroutine to avoid "coroutine was never awaited"
        # warnings on the caller side.
        coro.close()
        raise RuntimeError(
            "snipz.sync called from inside an active event loop. "
            "Use snipz.core (the async API) from async code."
        )
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


# ---------------------------------------------------------------------------
# Reservation — sync wrapper around an async Reservation
# ---------------------------------------------------------------------------


class Reservation:
    """Sync view over an async :class:`snipz.core.Reservation`.

    State properties forward to the underlying async object, so mutations
    from ``commit`` / ``release`` / ``observe`` are visible immediately.

    Use as a sync context manager to auto-commit on success and
    auto-release on exception, or call :meth:`commit` / :meth:`release` /
    :meth:`observe` explicitly.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: _async.Reservation) -> None:
        self._inner = inner

    # -- state accessors (forward to underlying async reservation) -------------

    @property
    def id(self) -> UUID:
        return self._inner.id

    @property
    def scopes(self) -> tuple[Scope, ...]:
        return self._inner.scopes

    @property
    def estimated_cents(self) -> Decimal:
        return self._inner.estimated_cents

    @property
    def actual_cents(self) -> Decimal | None:
        return self._inner.actual_cents

    @property
    def state(self) -> str:
        return self._inner.state

    @property
    def late(self) -> bool:
        return self._inner.late

    @property
    def request_id(self) -> str | None:
        return self._inner.request_id

    @property
    def expires_at(self) -> datetime:
        return self._inner.expires_at

    # -- lifecycle methods -----------------------------------------------------

    def observe(self, actual_cents: Decimal) -> None:
        """Update ``actual_cents`` to reflect cost observed during streaming."""
        _run(self._inner.observe(actual_cents))

    def commit(self, actual_cents: Decimal | None = None) -> None:
        """Settle the reservation as committed."""
        _run(self._inner.commit(actual_cents))

    def release(self) -> None:
        """Refund the reservation. No-op if already settled."""
        _run(self._inner.release())

    # -- sync context manager --------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._inner.state != "reserved":
            return
        if exc_type is None:
            self.commit()
        else:
            self.release()


# ---------------------------------------------------------------------------
# Budget — sync wrapper around an async Budget
# ---------------------------------------------------------------------------


class Budget:
    """Sync view over an async :class:`snipz.core.Budget`.

    Constructor accepts the same arguments as the async ``Budget``. All
    methods block until the underlying coroutine completes on the
    background loop.

    Example::

        from snipz.sync import Budget
        from snipz import Scope
        from decimal import Decimal

        budget = Budget("snipz.db")
        budget.migrate()
        budget.set_limit(Scope("user", "u_42"), Decimal("500"))

        with budget.reserve(Scope("user", "u_42"), Decimal("10")) as r:
            response = call_anthropic(...)
            r.observe(cost_for_response(response))

        budget.close()
    """

    __slots__ = ("_inner",)

    def __init__(
        self,
        backend: str | Path | Backend,
        *,
        now: object = None,
    ) -> None:
        # ``now`` is typed loosely here because it can be a callable
        # returning a datetime, matching the async Budget signature.
        self._inner = _async.Budget(backend, now=now)  # type: ignore[arg-type]

    def migrate(self) -> None:
        """Apply pending schema migrations."""
        _run(self._inner.migrate())

    def set_limit(
        self,
        scope: Scope,
        cap_cents: Decimal,
        *,
        grace_pct: int = 0,
    ) -> None:
        """Configure or update the cap for a scope."""
        _run(self._inner.set_limit(scope, cap_cents, grace_pct=grace_pct))

    def reserve(
        self,
        scopes: Scope | Sequence[Scope],
        estimated_cents: Decimal,
        *,
        request_id: str | None = None,
        ttl: int = 300,
        model: str | None = None,
        provider: str | None = None,
    ) -> Reservation:
        """Reserve budget against one or more scopes.

        Returns a sync :class:`Reservation`. Raises
        :class:`BudgetExceededError` on cap violation.
        """
        inner = _run(
            self._inner.reserve(
                scopes,
                estimated_cents,
                request_id=request_id,
                ttl=ttl,
                model=model,
                provider=provider,
            )
        )
        return Reservation(inner)

    def sweep(self) -> int:
        """Release reservations whose ``expires_at`` has passed."""
        return _run(self._inner.sweep())

    def close(self) -> None:
        """Release backend-owned resources (pool, etc.). Idempotent."""
        _run(self._inner.close())
