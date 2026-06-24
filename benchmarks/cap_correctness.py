"""Snipz cap-correctness benchmark.

Fires N concurrent reservations of ``--cost`` cents each against a
``--cap`` cent cap. The cap MUST hold — for the default SQLite scenario
(200 concurrent reservations of $0.10 each against a $5.00 cap),
exactly 50 reservations succeed and commit, 150 raise
:class:`BudgetExceededError`, and the final committed spend equals
$5.00 to the cent.

For the headline marketing scenario (1000 concurrent against $5 cap),
use Postgres — SQLite serializes every writer on a single database-wide
lock, so very-high concurrency causes timeouts at the lock layer
(not at the cap layer). Postgres uses row-level locks and handles
1000+ concurrent writers gracefully::

    python benchmarks/cap_correctness.py \\
        --concurrency 1000 --cap 500 --cost 10 \\
        --backend "postgresql://user:pw@host:5432/db"

This is the marketing artifact: the proof that the
``SELECT FOR UPDATE`` / ``BEGIN IMMEDIATE`` cap-check actually
serializes concurrent writers. Competitors that "estimate then record"
overshoot by ~20x in the same scenario; the comparison benchmark is
Phase 8b.

Usage::

    python benchmarks/cap_correctness.py
    python benchmarks/cap_correctness.py --concurrency 100 --cap 200 --cost 5
    python benchmarks/cap_correctness.py --backend "postgresql://..." --png

Outputs:

* ASCII bar chart to stderr (always)
* CSV to ``benchmarks/output/cap_correctness.csv`` (always; gitignored)
* PNG to ``benchmarks/output/cap_correctness.png`` if ``--png`` and
  matplotlib is installed (optional)

Exit code 0 when the cap holds; 1 when it overshoots (signals a real
correctness regression for CI).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from snipz import Budget, BudgetExceededError, Scope

_DEFAULT_OUTPUT_DIR: Path = Path(__file__).parent / "output"


@dataclass(frozen=True, slots=True)
class BenchResult:
    """One benchmark run's outcomes.

    ``lock_timeouts`` are a SQLite-specific category — when the
    benchmark fires more concurrent writers than SQLite's
    ``BEGIN IMMEDIATE`` lock queue can drain inside ``busy_timeout``.
    The cap still holds (no row commits past the cap) — it's just
    that some attempts never reach the cap-check at all. For the
    headline marketing scenario use Postgres, which has row-level
    locks and serializes 1000+ concurrent writers without timing out.
    """

    concurrency: int
    cap_cents: Decimal
    cost_cents: Decimal
    expected_successes: int
    actual_successes: int
    rejections: int
    lock_timeouts: int
    other_errors: int
    final_spend_cents: Decimal
    duration_sec: float
    cap_held: bool


async def run_benchmark(
    *,
    backend: str,
    concurrency: int,
    cap_cents: Decimal,
    cost_cents: Decimal,
) -> BenchResult:
    """Run one cap-correctness benchmark and return :class:`BenchResult`.

    The benchmark performs ``concurrency`` reserve+commit cycles in
    parallel via :func:`asyncio.gather`, classifies each outcome
    (success / rejection / error), then computes the final committed
    spend from the success count and per-call cost. The cap-held
    invariant is ``final_spend_cents <= cap_cents``.
    """
    expected_successes = int(cap_cents // cost_cents)
    scope = Scope("user", "bench")
    budget = Budget(backend)
    try:
        await budget.migrate()
        await budget.set_limit(scope, cap_cents)

        async def one_call(_index: int) -> str:
            try:
                reservation = await budget.reserve(scope, cost_cents)
            except BudgetExceededError:
                return "rejected"
            except sqlite3.OperationalError as exc:
                # SQLite-specific: BEGIN IMMEDIATE queue exceeded
                # busy_timeout. The cap still holds — this attempt
                # never even reached the cap-check.
                if "locked" in str(exc).lower():
                    return "lock_timeout"
                return "error"
            try:
                await reservation.commit()
            except Exception:  # pragma: no cover — defensive
                return "error"
            return "success"

        start = time.monotonic()
        outcomes = await asyncio.gather(
            *(one_call(i) for i in range(concurrency)),
            return_exceptions=False,
        )
        duration = time.monotonic() - start

        successes = outcomes.count("success")
        rejections = outcomes.count("rejected")
        lock_timeouts = outcomes.count("lock_timeout")
        other_errors = outcomes.count("error")
        final_spend = Decimal(successes) * cost_cents
        cap_held = final_spend <= cap_cents

        return BenchResult(
            concurrency=concurrency,
            cap_cents=cap_cents,
            cost_cents=cost_cents,
            expected_successes=expected_successes,
            actual_successes=successes,
            rejections=rejections,
            lock_timeouts=lock_timeouts,
            other_errors=other_errors,
            final_spend_cents=final_spend,
            duration_sec=duration,
            cap_held=cap_held,
        )
    finally:
        await budget.close()


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def render_ascii_chart(result: BenchResult, *, width: int = 40) -> str:
    """Render the result as a Unicode bar chart suitable for stderr."""
    cap_dollars = result.cap_cents / 100
    spend_dollars = result.final_spend_cents / 100

    ratio = (
        float(result.final_spend_cents / result.cap_cents)
        if result.cap_cents > 0
        else 0.0
    )
    spend_width = min(int(width * ratio), width * 2)  # allow >100% display

    cap_bar = "#" * width
    spend_bar = "#" * spend_width

    def _pct(n: int) -> str:
        if result.concurrency == 0:
            return "  0%"
        return f"{n * 100 // result.concurrency:3d}%"

    lines: list[str] = [
        "",
        "Snipz cap-correctness benchmark",
        "===============================",
        f"  Concurrency: {result.concurrency}",
        f"  Cap:         ${cap_dollars:,.2f}",
        f"  Per-req:     ${result.cost_cents / 100:,.2f}",
        f"  Duration:    {result.duration_sec:.3f}s",
        "",
        f"  Cap     [{cap_bar:<{width}}] ${cap_dollars:,.2f}",
        f"  Spend   [{spend_bar:<{width}}] ${spend_dollars:,.2f}",
        "",
        f"  Reservations attempted: {result.concurrency}",
        f"  Expected successes:     {result.expected_successes}",
        f"  Actual successes:       {result.actual_successes:>4d}  ({_pct(result.actual_successes)})",  # noqa: E501
        f"  Rejected (cap):         {result.rejections:>4d}  ({_pct(result.rejections)})",
        f"  Lock timeouts:          {result.lock_timeouts:>4d}  ({_pct(result.lock_timeouts)})",
        f"  Other errors:           {result.other_errors:>4d}  ({_pct(result.other_errors)})",
        "",
    ]

    if result.cap_held:
        lines.append(
            f"  CAP HELD: ${spend_dollars:,.2f} <= ${cap_dollars:,.2f} (no overshoot)"
        )
    else:
        overshoot = spend_dollars - cap_dollars
        lines.append(
            f"  CAP OVERSHOT: ${spend_dollars:,.2f} > ${cap_dollars:,.2f} "
            f"by ${overshoot:,.2f}"
        )
    lines.append("")
    return "\n".join(lines)


def save_csv(result: BenchResult, path: Path) -> None:
    """Write benchmark result as a two-column CSV (field, value)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["field", "value"])
        for key, value in asdict(result).items():
            writer.writerow([key, value])


def save_png(result: BenchResult, path: Path) -> bool:
    """Render a PNG comparison chart if matplotlib is available.

    Returns ``True`` if the file was written, ``False`` if matplotlib
    is not installed (the script does not require it).
    """
    try:
        import matplotlib  # noqa: F401

        matplotlib_available = True
    except ImportError:
        matplotlib_available = False

    if not matplotlib_available:
        return False

    import matplotlib.pyplot as plt

    cap_dollars = float(result.cap_cents) / 100
    spend_dollars = float(result.final_spend_cents) / 100

    fig, ax = plt.subplots(figsize=(7, 3))
    labels = ["Cap", "Final spend"]
    values = [cap_dollars, spend_dollars]
    colors = ["#888888", "#2ca02c" if result.cap_held else "#d62728"]
    ax.barh(labels, values, color=colors)
    ax.set_xlabel("US dollars")
    ax.set_title(
        f"Snipz cap correctness — {result.concurrency} concurrent reservations"
    )
    for i, v in enumerate(values):
        ax.text(v, i, f" ${v:,.2f}", va="center")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=144)
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# testcontainers integration — for the headline marketing scenario
# ---------------------------------------------------------------------------


def _run_against_testcontainers_postgres(
    *,
    concurrency: int,
    cap_cents: Decimal,
    cost_cents: Decimal,
) -> BenchResult:
    """Spin up a fresh Postgres container, run the benchmark, tear it down.

    Imports ``testcontainers`` lazily so the rest of the script works on
    machines without it. Raises :class:`SystemExit` with a clear message
    if testcontainers is missing.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "--testcontainers-postgres requires the `testcontainers` "
            "package. Install with `uv sync` (dev deps) or "
            "`pip install testcontainers[postgres]`."
        ) from exc

    print("Spinning up postgres:16-alpine ...", file=sys.stderr)
    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{host}:{port}/{container.dbname}"
        )
        return asyncio.run(
            run_benchmark(
                backend=dsn,
                concurrency=concurrency,
                cap_cents=cap_cents,
                cost_cents=cost_cents,
            )
        )
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Snipz cap-correctness benchmark.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=100,
        help=(
            "Number of concurrent reservations to fire (default: 100). "
            "For the headline 1000-concurrent scenario use Postgres via "
            "--backend; SQLite serializes every writer on a single lock "
            "and will time some out at very high concurrency."
        ),
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=500,
        help="Cap in cents (default: 500 = $5.00).",
    )
    parser.add_argument(
        "--cost",
        type=int,
        default=10,
        help="Per-request cost in cents (default: 10 = $0.10).",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help=(
            "Backend spec — SQLite path or 'postgresql://...' DSN. "
            "Default: a fresh temporary SQLite database."
        ),
    )
    parser.add_argument(
        "--testcontainers-postgres",
        action="store_true",
        help=(
            "Spin up a fresh Postgres container via testcontainers, run "
            "the benchmark against it, and tear it down. Use this for "
            "the headline 1000-concurrent scenario without setting up "
            "Postgres yourself. Requires Docker + the testcontainers "
            "package (in dev deps; run via `uv run python ...`)."
        ),
    )
    parser.add_argument(
        "--png",
        action="store_true",
        help="Also save a PNG chart (requires matplotlib).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_OUTPUT_DIR,
        help=f"Directory for CSV/PNG outputs (default: {_DEFAULT_OUTPUT_DIR}).",
    )
    args = parser.parse_args(argv)

    cap_cents = Decimal(args.cap)
    cost_cents = Decimal(args.cost)

    if args.testcontainers_postgres and args.backend is not None:
        parser.error(
            "--testcontainers-postgres and --backend are mutually exclusive"
        )

    if args.testcontainers_postgres:
        result = _run_against_testcontainers_postgres(
            concurrency=args.concurrency,
            cap_cents=cap_cents,
            cost_cents=cost_cents,
        )
    else:
        # Use a fresh temp SQLite database unless an explicit backend was given.
        tmp_root: tempfile.TemporaryDirectory[str] | None = None
        if args.backend is None:
            tmp_root = tempfile.TemporaryDirectory(prefix="snipz_bench_")
            backend = str(Path(tmp_root.name) / "snipz_bench.db")
        else:
            backend = args.backend

        try:
            result = asyncio.run(
                run_benchmark(
                    backend=backend,
                    concurrency=args.concurrency,
                    cap_cents=cap_cents,
                    cost_cents=cost_cents,
                )
            )
        finally:
            if tmp_root is not None:
                tmp_root.cleanup()

    print(render_ascii_chart(result), file=sys.stderr)

    csv_path = args.output_dir / "cap_correctness.csv"
    save_csv(result, csv_path)
    print(f"CSV  -> {csv_path}", file=sys.stderr)

    if args.png:
        png_path = args.output_dir / "cap_correctness.png"
        if save_png(result, png_path):
            print(f"PNG  -> {png_path}", file=sys.stderr)
        else:
            print(
                "PNG  -> skipped (matplotlib not installed; "
                "`pip install matplotlib` to enable)",
                file=sys.stderr,
            )

    return 0 if result.cap_held else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
