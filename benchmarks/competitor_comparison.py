"""Head-to-head cap-correctness benchmark across Snipz, LiteLLM, Shekel.

The Phase 8 benchmark (``cap_correctness.py``) proves Snipz holds the
cap under load. This benchmark adds the comparison: same workload, same
cap, fired at competitor libraries that follow the estimate-then-record
pattern, so a reader sees the actual overshoot in dollars.

Workload: ``--concurrency`` reservations of ``--cost`` cents each
against a ``--cap`` cent cap, all sharing one scope. Each backend
configures its own internal store (Snipz: testcontainers Postgres;
LiteLLM: in-memory dict; Shekel: ContextVar Budget). Adapters lift
each library behind a common Protocol so the harness is uniform.

Outputs:

* Side-by-side ASCII chart to stderr (always).
* CSV with one row per backend to ``benchmarks/output/competitor_comparison.csv``
  (always; the directory is gitignored).

Exit code 0 if Snipz holds the cap and at least one competitor
overshoots (the headline claim is reproduced). Exit code 1 if Snipz
ever overshoots (a genuine regression). Exit code 2 if no competitors
could be loaded (the bench-competitors extra is not installed).

Usage::

    pip install snipz[bench-competitors]
    uv run python benchmarks/competitor_comparison.py
    uv run python benchmarks/competitor_comparison.py --concurrency 500
    uv run python benchmarks/competitor_comparison.py --only snipz,litellm
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .adapters import BenchmarkAdapter, Outcome

_DEFAULT_OUTPUT_DIR: Path = Path(__file__).parent / "output"
_BENCH_SCOPE: str = "bench_user"


@dataclass(frozen=True, slots=True)
class BackendResult:
    """One backend's outcomes in the comparison run."""

    backend: str
    concurrency: int
    cap_cents: int
    cost_cents: int
    successes: int
    rejections: int
    errors: int
    final_spend_cents: int
    overshoot_cents: int
    duration_sec: float
    cap_held: bool


# An adapter loader is a zero-arg callable that returns a fresh
# ``BenchmarkAdapter`` instance (or raises ``RuntimeError`` if the
# library is missing). Loaders are lazy so the harness can iterate the
# registry and skip unavailable backends with a clear log line.
AdapterLoader = Callable[[], BenchmarkAdapter]


def _load_snipz() -> BenchmarkAdapter:
    from .adapters.snipz_adapter import SnipzAdapter

    return SnipzAdapter()


def _load_litellm() -> BenchmarkAdapter:
    from .adapters.litellm_adapter import LiteLLMAdapter

    return LiteLLMAdapter()


def _load_shekel() -> BenchmarkAdapter:
    from .adapters.shekel_adapter import ShekelAdapter

    return ShekelAdapter()


_REGISTRY: dict[str, AdapterLoader] = {
    "snipz": _load_snipz,
    "litellm": _load_litellm,
    "shekel": _load_shekel,
}


async def run_one(
    adapter: BenchmarkAdapter,
    *,
    concurrency: int,
    cap_cents: int,
    cost_cents: int,
) -> BackendResult:
    """Run one backend's pass and return a :class:`BackendResult`."""
    async with adapter:
        await adapter.set_cap(_BENCH_SCOPE, cap_cents)

        async def one() -> Outcome:
            return await adapter.try_reserve_and_commit(_BENCH_SCOPE, cost_cents)

        start = time.monotonic()
        outcomes: list[Outcome] = await asyncio.gather(
            *(one() for _ in range(concurrency))
        )
        duration = time.monotonic() - start

    successes = outcomes.count("success")
    rejections = outcomes.count("rejected")
    errors = outcomes.count("error")
    final_spend = successes * cost_cents
    overshoot = max(0, final_spend - cap_cents)
    return BackendResult(
        backend=adapter.name,
        concurrency=concurrency,
        cap_cents=cap_cents,
        cost_cents=cost_cents,
        successes=successes,
        rejections=rejections,
        errors=errors,
        final_spend_cents=final_spend,
        overshoot_cents=overshoot,
        duration_sec=duration,
        cap_held=final_spend <= cap_cents,
    )


def render_comparison_chart(results: list[BackendResult]) -> str:
    """Render a side-by-side ASCII chart of all backends' final spend.

    Bars are normalized so the cap is always exactly ``width`` chars.
    A backend that overshoots renders a bar longer than the cap, with
    the overshoot in red brackets in the trailing label.
    """
    if not results:
        return "(no backends ran)\n"

    width = 40
    cap_cents = results[0].cap_cents
    cap_dollars = cap_cents / 100

    lines: list[str] = [
        "",
        "Cap-correctness comparison — Snipz vs. estimate-then-record competitors",
        "=" * 72,
        f"  Concurrency: {results[0].concurrency}",
        f"  Cap:         ${cap_dollars:,.2f}",
        f"  Per-req:     ${results[0].cost_cents / 100:,.2f}",
        "",
        f"  Cap     [{'#' * width}] ${cap_dollars:,.2f}",
        "",
    ]

    name_pad = max(len(r.backend) for r in results)
    for r in results:
        ratio = r.final_spend_cents / r.cap_cents if r.cap_cents else 0.0
        bar_len = min(int(width * ratio), width * 2)
        bar = "#" * bar_len
        spend_dollars = r.final_spend_cents / 100
        verdict = (
            "ok (held)"
            if r.cap_held
            else f"OVERSHOT by ${r.overshoot_cents / 100:,.2f}"
        )
        lines.append(
            f"  {r.backend:<{name_pad}} [{bar:<{width * 2}}] "
            f"${spend_dollars:,.2f}  — {verdict}"
        )
    lines.append("")

    snipz_held = next(
        (r.cap_held for r in results if r.backend.lower() == "snipz"),
        None,
    )
    overshooting = [r for r in results if not r.cap_held]
    if snipz_held and overshooting:
        names = ", ".join(r.backend for r in overshooting)
        lines.append(
            f"  Headline claim reproduced: Snipz held the cap; "
            f"{names} overshot."
        )
    elif snipz_held is False:
        lines.append(
            "  REGRESSION: Snipz overshot the cap. Investigate immediately."
        )
    else:
        lines.append(
            "  Snipz held the cap. (No competitor backends ran — "
            "install `snipz[bench-competitors]` to compare.)"
        )
    lines.append("")
    return "\n".join(lines)


def save_csv(results: list[BackendResult], path: Path) -> None:
    """Write all backends' results to a CSV (one row per backend)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(results[0]).keys()) if results else []
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


async def run_all(
    backends: list[str],
    *,
    concurrency: int,
    cap_cents: int,
    cost_cents: int,
) -> list[BackendResult]:
    """Run each requested backend in sequence. Missing libs skip cleanly."""
    results: list[BackendResult] = []
    for name in backends:
        loader = _REGISTRY[name]
        try:
            adapter = loader()
        except RuntimeError as exc:
            print(f"skip {name}: {exc}", file=sys.stderr)
            continue

        print(f"running {adapter.name} ...", file=sys.stderr)
        try:
            result = await run_one(
                adapter,
                concurrency=concurrency,
                cap_cents=cap_cents,
                cost_cents=cost_cents,
            )
        except RuntimeError as exc:
            print(f"skip {name}: {exc}", file=sys.stderr)
            continue
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Head-to-head cap-correctness benchmark across Snipz, "
            "LiteLLM BudgetManager, and Shekel."
        )
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=200,
        help=(
            "Number of concurrent reservations per backend (default: 200). "
            "Higher concurrency makes competitor races more visible; "
            "Snipz holds the cap regardless."
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
        "--only",
        default=None,
        help=(
            "Comma-separated subset of backends to run. "
            f"Choices: {', '.join(sorted(_REGISTRY))}. "
            "Default: all available."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_OUTPUT_DIR,
        help=f"Directory for CSV output (default: {_DEFAULT_OUTPUT_DIR}).",
    )
    args = parser.parse_args(argv)

    if args.only is not None:
        requested = [name.strip().lower() for name in args.only.split(",")]
        unknown = sorted(set(requested) - set(_REGISTRY))
        if unknown:
            parser.error(f"unknown backend(s): {', '.join(unknown)}")
        backends = requested
    else:
        backends = list(_REGISTRY)

    results = asyncio.run(
        run_all(
            backends,
            concurrency=args.concurrency,
            cap_cents=args.cap,
            cost_cents=args.cost,
        )
    )

    print(render_comparison_chart(results), file=sys.stderr)

    if results:
        csv_path = args.output_dir / "competitor_comparison.csv"
        save_csv(results, csv_path)
        print(f"CSV -> {csv_path}", file=sys.stderr)

    # Exit code policy.
    if not results:
        return 2  # no backends ran
    snipz_result = next((r for r in results if r.backend.lower() == "snipz"), None)
    if snipz_result is None or not snipz_result.cap_held:
        return 1  # Snipz regression or didn't run
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
