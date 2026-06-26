"""Backend adapter Protocol for the cap-correctness comparison benchmark.

Each adapter wraps one library (Snipz, LiteLLM ``BudgetManager``, Shekel
``Budget``) behind a uniform async surface so the harness in
``competitor_comparison.py`` can drive all of them through the same
concurrent workload. Per-library plumbing (sync-to-async bridging,
internal-state pokes for libraries that compute cost from real LLM
responses) lives inside each adapter, not in the harness.

The Protocol is intentionally small: setup, cap configuration, one
attempt operation, teardown. Anything larger leaks library-specific
concepts into the harness.
"""

from __future__ import annotations

from types import TracebackType
from typing import Literal, Protocol, Self, runtime_checkable

# Outcome of a single ``try_reserve_and_commit`` attempt.
#
# - ``success``: the attempt was admitted and recorded a charge.
# - ``rejected``: the backend refused because the cap would be exceeded.
# - ``error``: any other failure (lock timeout, connection issue, etc.).
#
# The harness aggregates these per backend and reports the final spend
# observed against the cap. ``error`` is distinguished from ``rejected``
# because the former does not bear on cap-correctness.
Outcome = Literal["success", "rejected", "error"]


@runtime_checkable
class BenchmarkAdapter(Protocol):
    """Uniform async surface around one budget-control backend.

    Adapters are async context managers so per-backend startup
    (testcontainers, in-memory storage init) and teardown happen
    deterministically around the workload.
    """

    name: str
    """Display name used in the side-by-side chart, e.g. ``"Snipz"``."""

    async def __aenter__(self) -> Self:
        """Initialize backend resources (containers, connections, files)."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release all backend resources. MUST be idempotent on errors."""
        ...

    async def set_cap(self, scope: str, cap_cents: int) -> None:
        """Configure the spend cap for ``scope``.

        Cents (integer) is the canonical unit across adapters even though
        each library uses its own internal representation. The adapter
        translates as needed.
        """
        ...

    async def try_reserve_and_commit(self, scope: str, cost_cents: int) -> Outcome:
        """Attempt a single reserve + commit of ``cost_cents`` against ``scope``.

        Returns one of the ``Outcome`` literals. MUST not raise for
        ``rejected`` or expected failure paths — only re-raise on truly
        unexpected conditions the harness should surface in the report.
        """
        ...


__all__ = ["BenchmarkAdapter", "Outcome"]
