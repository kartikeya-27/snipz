"""Shekel adapter for the competitor comparison benchmark.

Shekel is an in-process budget tracker driven by ``ContextVar`` and
instrumented LLM-client wrappers. The cap-enforcement flow in a real
caller looks like::

    with shekel.budget(max_usd=5.00) as b:
        response = openai_client.chat.completions.create(...)
        # Shekel's wrapper records cost into b._spent_direct here

The race window is the LLM call between Shekel's internal cap check
and its internal cost record. This adapter reproduces that pattern:

1. Read ``budget.spent`` (sum of ``_spent_direct`` and child spend).
2. Yield to the event loop (the simulated LLM call gap).
3. If the check passed, increment ``_spent_direct`` via Shekel's
   internal ``_record_spend`` method (the same path Shekel's
   instrumented wrappers take).

``_record_spend`` is the actual mutation entry point inside Shekel.
Calling it directly is a faithful reproduction of how Shekel updates
its counter in production — observers call it with model + tokens
arguments derived from a real response. We pass mock arguments
because the budget arithmetic is the only thing under test here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Self

from . import Outcome

if TYPE_CHECKING:
    from types import TracebackType


_LLM_GAP_SECONDS: float = 0.001
"""Simulated LLM-call duration sitting between the check and the
record. Matches ``litellm_adapter._LLM_GAP_SECONDS`` for parity."""


class ShekelAdapter:
    """:class:`BenchmarkAdapter` implementation backed by ``shekel.Budget``."""

    name: str = "Shekel"

    def __init__(self) -> None:
        self._budget: Any | None = None
        self._cap_usd: float | None = None

    async def __aenter__(self) -> Self:
        try:
            import shekel  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "ShekelAdapter requires `shekel`. Install with "
                "`pip install snipz[bench-competitors]`."
            ) from exc
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._budget = None
        self._cap_usd = None

    async def set_cap(self, scope: str, cap_cents: int) -> None:
        import shekel

        cap_usd = cap_cents / 100.0
        # ``warn_only=True`` suppresses Shekel's own exception path on
        # overshoot so we can attribute the outcome from the check
        # rather than from an uncaught raise mid-record.
        self._budget = shekel.Budget(name=scope, max_usd=cap_usd, warn_only=True)
        self._cap_usd = cap_usd

    async def try_reserve_and_commit(self, scope: str, cost_cents: int) -> Outcome:
        budget = self._require_budget()
        cap_usd = self._require_cap()
        cost_usd = cost_cents / 100.0
        try:
            # Step 1: the cap check.
            spent = budget.spent
            if spent + cost_usd > cap_usd:
                return "rejected"
            # Step 2: the simulated LLM call.
            await asyncio.sleep(_LLM_GAP_SECONDS)
            # Step 3: record the cost via Shekel's internal mutation
            # path (the same path its observers take). The race is
            # between the check above and this record — concurrent
            # callers all pass the check at the same ``spent`` value
            # and all execute the record below.
            budget._record_spend(cost_usd, model="bench", tokens={})
        except Exception:
            return "error"
        return "success"

    def _require_budget(self) -> Any:
        if self._budget is None:
            raise RuntimeError(
                "ShekelAdapter not started or set_cap not called; "
                "use `async with ShekelAdapter()` then `await adapter.set_cap(...)`."
            )
        return self._budget

    def _require_cap(self) -> float:
        if self._cap_usd is None:
            raise RuntimeError("ShekelAdapter.set_cap must be called before attempts.")
        return self._cap_usd
