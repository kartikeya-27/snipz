"""LiteLLM BudgetManager adapter for the competitor comparison benchmark.

LiteLLM's ``BudgetManager`` tracks spend in a plain Python dict. The
cap-enforcement pattern in real callers is::

    if budget_manager.get_current_cost(user) + estimate <= budget_manager.get_total_budget(user):
        response = call_llm(...)          # <- the LLM call sits here
        budget_manager.update_cost(user, response)

The race window in production is the entire LLM call between the check
and the update (100ms-2s). This adapter reproduces that pattern:

1. Read ``current_cost`` from the shared dict.
2. Yield to the event loop (the simulated LLM call gap).
3. Write ``current_cost + cost`` back if and only if the check passed.

If steps 1 and 3 had been atomic — as they are in Snipz, guarded by a
``SELECT … FOR UPDATE`` transaction — the cap would hold. They are
not, so concurrent callers all pass the check during the gap and all
write, overshooting.

For benchmark fairness, we use ``_LLM_GAP_SECONDS = 0.001`` (1 ms) -
roughly 500x shorter than a real LLM call. Increasing it makes the
overshoot more extreme; decreasing it eventually loses the race to
the GIL. 1 ms is enough to reliably reproduce at 100 concurrent
attempts on modern Python.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Self

from . import Outcome

if TYPE_CHECKING:
    from types import TracebackType


_LLM_GAP_SECONDS: float = 0.001
"""Simulated LLM-call duration sitting between the cap check and the
cost record. Real LLM calls are 100-2000 ms; this is a deliberately
conservative 1 ms - the race condition is real, not amplified."""


class LiteLLMAdapter:
    """:class:`BenchmarkAdapter` implementation backed by LiteLLM ``BudgetManager``."""

    name: str = "LiteLLM BudgetManager"

    def __init__(self) -> None:
        self._bm: Any | None = None

    async def __aenter__(self) -> Self:
        try:
            from litellm import BudgetManager  # type: ignore[attr-defined]
        except ImportError as exc:
            raise RuntimeError(
                "LiteLLMAdapter requires `litellm`. Install with "
                "`pip install snipz[bench-competitors]`."
            ) from exc
        # client_type="local" keeps state in-memory; no disk writes
        # during the benchmark.
        self._bm = BudgetManager(project_name="snipz_bench", client_type="local")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._bm = None

    async def set_cap(self, scope: str, cap_cents: int) -> None:
        bm = self._require_bm()
        bm.create_budget(total_budget=cap_cents / 100.0, user=scope)

    async def try_reserve_and_commit(self, scope: str, cost_cents: int) -> Outcome:
        bm = self._require_bm()
        cost_usd = cost_cents / 100.0
        try:
            # Step 1: the cap check.
            current = bm.user_dict[scope].get("current_cost", 0.0)
            cap = bm.user_dict[scope]["total_budget"]
            if current + cost_usd > cap:
                return "rejected"
            # Step 2: the simulated LLM call. In real workflows this is
            # the ``call_llm(...)`` step. All concurrent callers that
            # passed the check at the same ``current`` value will
            # complete their gap and proceed to write below.
            await asyncio.sleep(_LLM_GAP_SECONDS)
            # Step 3: record the cost. Read-modify-write on a shared
            # dict, no lock — the race condition is here.
            current_now = bm.user_dict[scope].get("current_cost", 0.0)
            bm.user_dict[scope]["current_cost"] = current_now + cost_usd
        except Exception:
            return "error"
        return "success"

    def _require_bm(self) -> Any:
        if self._bm is None:
            raise RuntimeError(
                "LiteLLMAdapter not started; use `async with LiteLLMAdapter()`."
            )
        return self._bm
