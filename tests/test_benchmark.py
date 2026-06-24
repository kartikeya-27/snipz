"""Regression test for the Phase 8 cap-correctness benchmark.

Runs the harness at a tiny scale and asserts the headline invariants:

* Cap holds (no overshoot).
* Exactly ``floor(cap / cost)`` reservations succeed.
* All other concurrent reservations are rejected with
  :class:`BudgetExceededError` (never silently dropped or counted as
  other errors).

If a future refactor breaks the ``SELECT FOR UPDATE`` /
``BEGIN IMMEDIATE`` serialization story, this test goes red — the
benchmark itself becomes the safety net.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from benchmarks.cap_correctness import (
    BenchResult,
    main,
    render_ascii_chart,
    run_benchmark,
    save_csv,
)

# ---------------------------------------------------------------------------
# Headline correctness invariants
# ---------------------------------------------------------------------------


async def test_benchmark_cap_holds_at_small_scale(tmp_path: Path) -> None:
    """10 concurrent x $0.10 against a $1.00 cap: exactly 10 succeed."""
    db = tmp_path / "bench.db"
    result = await run_benchmark(
        backend=str(db),
        concurrency=10,
        cap_cents=Decimal("100"),
        cost_cents=Decimal("10"),
    )

    assert result.cap_held is True
    assert result.actual_successes == 10
    assert result.rejections == 0
    assert result.other_errors == 0
    assert result.final_spend_cents == Decimal("100")
    assert result.expected_successes == 10


async def test_benchmark_rejects_overshooting_demand(tmp_path: Path) -> None:
    """20 concurrent x $0.10 against a $1.00 cap: only 10 succeed, 10 rejected."""
    db = tmp_path / "bench.db"
    result = await run_benchmark(
        backend=str(db),
        concurrency=20,
        cap_cents=Decimal("100"),
        cost_cents=Decimal("10"),
    )

    assert result.cap_held is True
    assert result.actual_successes == 10
    assert result.rejections == 10
    assert result.other_errors == 0
    assert result.final_spend_cents == Decimal("100")


async def test_benchmark_final_spend_equals_successes_times_cost(
    tmp_path: Path,
) -> None:
    """The benchmark's accounting matches successes * cost exactly."""
    db = tmp_path / "bench.db"
    result = await run_benchmark(
        backend=str(db),
        concurrency=15,
        cap_cents=Decimal("50"),  # exact fit for 5 successes
        cost_cents=Decimal("10"),
    )

    assert result.actual_successes == 5
    assert result.final_spend_cents == Decimal(result.actual_successes) * Decimal("10")
    assert result.final_spend_cents == Decimal("50")
    assert result.cap_held is True


async def test_benchmark_zero_concurrency_still_returns_clean_result(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bench.db"
    result = await run_benchmark(
        backend=str(db),
        concurrency=0,
        cap_cents=Decimal("100"),
        cost_cents=Decimal("10"),
    )

    assert result.actual_successes == 0
    assert result.rejections == 0
    assert result.other_errors == 0
    assert result.final_spend_cents == Decimal("0")
    assert result.cap_held is True


# ---------------------------------------------------------------------------
# Rendering / persistence
# ---------------------------------------------------------------------------


def _make_result(*, successes: int, cap: Decimal, cost: Decimal, n: int) -> BenchResult:
    return BenchResult(
        concurrency=n,
        cap_cents=cap,
        cost_cents=cost,
        expected_successes=int(cap // cost),
        actual_successes=successes,
        rejections=n - successes,
        lock_timeouts=0,
        other_errors=0,
        final_spend_cents=Decimal(successes) * cost,
        duration_sec=0.123,
        cap_held=Decimal(successes) * cost <= cap,
    )


def test_render_ascii_chart_announces_cap_held() -> None:
    """The 'CAP HELD' line is the headline; a reviewer scanning the output sees it."""
    result = _make_result(successes=50, cap=Decimal("500"), cost=Decimal("10"), n=1000)
    chart = render_ascii_chart(result)
    assert "CAP HELD" in chart
    assert "$5.00" in chart  # cap rendered as dollars


def test_render_ascii_chart_announces_overshoot() -> None:
    """The negative case must be loud — a competitor's run lands here."""
    result = _make_result(successes=60, cap=Decimal("500"), cost=Decimal("10"), n=1000)
    chart = render_ascii_chart(result)
    assert "CAP OVERSHOT" in chart
    assert "$6.00" in chart  # spend > cap rendered as dollars
    assert "$5.00" in chart  # cap rendered as dollars


def test_save_csv_round_trips(tmp_path: Path) -> None:
    """The CSV output captures every field of the BenchResult."""
    result = _make_result(successes=42, cap=Decimal("500"), cost=Decimal("10"), n=100)
    path = tmp_path / "out" / "cap_correctness.csv"
    save_csv(result, path)

    text = path.read_text(encoding="utf-8")
    assert "concurrency,100" in text
    assert "actual_successes,42" in text
    assert "cap_held,True" in text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_cli_returns_zero_when_cap_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python benchmarks/cap_correctness.py ...`` returns 0 in the happy case."""
    code = main(
        [
            "--concurrency",
            "10",
            "--cap",
            "100",
            "--cost",
            "10",
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "CAP HELD" in err
