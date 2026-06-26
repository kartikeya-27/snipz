"""Tests for the head-to-head competitor benchmark adapters and harness.

Coverage:

* The adapter ``Protocol`` is implemented correctly by all three
  concrete adapters (structural typing checks).
* The harness aggregates ``Outcome`` literals into ``BackendResult``
  exactly as documented.
* The LiteLLM and Shekel adapters' check-then-record patterns
  overshoot the cap under concurrency at a small but reliable
  workload — this is the core anti-claim of the benchmark and is
  reproduced here so it cannot silently break.

Snipz isn't tested here for cap-correctness — that's the job of
``test_phase1.py`` and ``test_postgres.py``. The SnipzAdapter is
covered by structural-conformance only; the actual Postgres
benchmark run lives behind the ``--postgres`` flag in its own test.
"""

from __future__ import annotations

import asyncio

import pytest
from benchmarks.adapters import BenchmarkAdapter
from benchmarks.competitor_comparison import (
    BackendResult,
    render_comparison_chart,
    run_one,
)


def test_protocol_runtime_checkable() -> None:
    """All concrete adapters MUST satisfy the BenchmarkAdapter Protocol."""
    from benchmarks.adapters.snipz_adapter import SnipzAdapter

    assert isinstance(SnipzAdapter(), BenchmarkAdapter)


def test_litellm_adapter_protocol() -> None:
    pytest.importorskip("litellm")
    from benchmarks.adapters.litellm_adapter import LiteLLMAdapter

    assert isinstance(LiteLLMAdapter(), BenchmarkAdapter)


def test_shekel_adapter_protocol() -> None:
    pytest.importorskip("shekel")
    from benchmarks.adapters.shekel_adapter import ShekelAdapter

    assert isinstance(ShekelAdapter(), BenchmarkAdapter)


# ---------------------------------------------------------------------------
# LiteLLM: the estimate-then-record race must overshoot under concurrency
# ---------------------------------------------------------------------------


async def test_litellm_adapter_overshoots_under_concurrency() -> None:
    """100 concurrent attempts at $0.10 vs a $1 cap MUST overshoot.

    This is the anti-claim of the comparison benchmark and is the
    entire reason Snipz exists. If this test starts passing the
    cap-held invariant on its own, either LiteLLM has fixed their
    race (unlikely — would require an atomic operation we'd see in
    the adapter) or our adapter is no longer reproducing the race.
    """
    pytest.importorskip("litellm")
    from benchmarks.adapters.litellm_adapter import LiteLLMAdapter

    result = await run_one(
        LiteLLMAdapter(), concurrency=100, cap_cents=100, cost_cents=10
    )
    assert result.successes >= 10, "expected at least one set of successes"
    # At 100 concurrent threads with a 10-success cap, the race
    # virtually guarantees more than 10 successes commit. Allow some
    # variance — the assertion is on the cap-held invariant, not on
    # an exact count.
    assert (
        result.successes > 10 or result.errors > 0
    ), "LiteLLM adapter did not exhibit the race condition"
    assert not result.cap_held, (
        f"LiteLLM unexpectedly held the cap ({result.successes} successes, "
        f"spend=${result.final_spend_cents / 100:.2f}); race may not be reproducing"
    )


# ---------------------------------------------------------------------------
# Shekel: same expectation, different storage shape
# ---------------------------------------------------------------------------


async def test_shekel_adapter_overshoots_under_concurrency() -> None:
    pytest.importorskip("shekel")
    from benchmarks.adapters.shekel_adapter import ShekelAdapter

    result = await run_one(
        ShekelAdapter(), concurrency=100, cap_cents=100, cost_cents=10
    )
    assert result.successes >= 10
    assert (
        result.successes > 10 or result.errors > 0
    ), "Shekel adapter did not exhibit the race condition"
    assert not result.cap_held, (
        f"Shekel unexpectedly held the cap ({result.successes} successes, "
        f"spend=${result.final_spend_cents / 100:.2f}); race may not be reproducing"
    )


# ---------------------------------------------------------------------------
# Harness shape: results have the right fields, chart renders, CSV writes
# ---------------------------------------------------------------------------


def test_render_chart_handles_empty() -> None:
    assert "(no backends ran)" in render_comparison_chart([])


def test_render_chart_headline_when_snipz_holds_and_others_overshoot() -> None:
    chart = render_comparison_chart(
        [
            _result("Snipz", successes=10, cap=100, cost=10),
            _result("LiteLLM BudgetManager", successes=42, cap=100, cost=10),
        ]
    )
    assert "Headline claim reproduced" in chart
    assert "LiteLLM BudgetManager" in chart
    assert "OVERSHOT" in chart


def test_render_chart_regression_when_snipz_overshoots() -> None:
    chart = render_comparison_chart(
        [
            _result("Snipz", successes=20, cap=100, cost=10),
        ]
    )
    assert "REGRESSION" in chart


def _result(backend: str, *, successes: int, cap: int, cost: int) -> BackendResult:
    """Construct a BackendResult for chart-rendering tests without running."""
    final_spend = successes * cost
    return BackendResult(
        backend=backend,
        concurrency=successes * 2,  # arbitrary, not exercised here
        cap_cents=cap,
        cost_cents=cost,
        successes=successes,
        rejections=0,
        errors=0,
        final_spend_cents=final_spend,
        overshoot_cents=max(0, final_spend - cap),
        duration_sec=0.0,
        cap_held=final_spend <= cap,
    )


# ---------------------------------------------------------------------------
# asyncio_mode='auto' is set in pyproject; these functions are picked up.
# Silence unused warnings on a non-test helper module.
# ---------------------------------------------------------------------------

__all__ = ["asyncio"]
