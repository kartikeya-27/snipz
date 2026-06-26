"""Snipz adapter for the competitor comparison benchmark.

Wraps :class:`snipz.Budget` against a real Postgres instance spun up
via testcontainers. Postgres is chosen over SQLite for parity with the
headline scenario — the cap-correctness benchmark already demonstrates
correctness at 1000 concurrency on Postgres, and the comparison
exercises the same code path so the reader sees an apples-to-apples
race-condition test against the competitors.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Self

from snipz import Budget, BudgetExceededError, Scope

from . import Outcome

if TYPE_CHECKING:
    from types import TracebackType


_DEFAULT_IMAGE: str = "postgres:16-alpine"


class SnipzAdapter:
    """:class:`BenchmarkAdapter` implementation backed by Snipz + Postgres."""

    name: str = "Snipz"

    def __init__(self, *, image: str = _DEFAULT_IMAGE) -> None:
        self._image = image
        self._container: object | None = None
        self._budget: Budget | None = None

    async def __aenter__(self) -> Self:
        # Import lazily so SnipzAdapter can be referenced from tests
        # that skip when Docker is unavailable.
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError as exc:
            raise RuntimeError(
                "SnipzAdapter requires the `testcontainers[postgres]` package; "
                "install dev deps with `uv sync` or "
                "`pip install testcontainers[postgres]`."
            ) from exc

        container = PostgresContainer(self._image)
        container.start()
        self._container = container

        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{host}:{port}/{container.dbname}"
        )
        budget = Budget(dsn)
        await budget.migrate()
        self._budget = budget
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._budget is not None:
            try:
                await self._budget.close()
            finally:
                self._budget = None
        if self._container is not None:
            # The container exposes a synchronous ``stop()``; calling
            # it from async context is safe because it's a local
            # Docker IPC call, not a long blocking operation.
            try:
                self._container.stop()  # type: ignore[attr-defined]
            finally:
                self._container = None

    async def set_cap(self, scope: str, cap_cents: int) -> None:
        budget = self._require_budget()
        await budget.set_limit(_scope(scope), Decimal(cap_cents))

    async def try_reserve_and_commit(self, scope: str, cost_cents: int) -> Outcome:
        budget = self._require_budget()
        try:
            reservation = await budget.reserve(_scope(scope), Decimal(cost_cents))
        except BudgetExceededError:
            return "rejected"
        except Exception:
            return "error"
        try:
            await reservation.commit()
        except Exception:
            return "error"
        return "success"

    def _require_budget(self) -> Budget:
        if self._budget is None:
            raise RuntimeError(
                "SnipzAdapter not started; use `async with SnipzAdapter()`."
            )
        return self._budget


def _scope(name: str) -> Scope:
    """Lift the harness-level scope string into Snipz's ``Scope``."""
    return Scope("bench", name)
