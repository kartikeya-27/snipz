"""Command-line interface for snipz.

Currently one subcommand:

* ``snipz update-pricing`` — fetches LiteLLM's model price catalogue and
  rewrites the vendored ``snipz/pricing.toml``.

Design:

* Stdlib only (``argparse``, ``urllib``, ``json``). No new runtime deps.
* The translator (:func:`_litellm_to_toml`) is a pure function — easy to
  unit-test without network access.
* The HTTP fetch (:func:`_fetch_upstream`) is a tiny wrapper so tests
  can monkey-patch it.
* Writes atomically via tempfile + rename so a partial download cannot
  corrupt the on-disk pricing file.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

__all__ = ["main"]


# Authoritative source for model pricing — LiteLLM's vendored JSON.
_LITELLM_PRICE_URL: str = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window_backup.json"
)

# Conversion factor: a price expressed as dollars per token becomes
# cents per million tokens after multiplying by 10**8.
_HUNDRED_MILLION: Decimal = Decimal("100000000")

# Vendored pricing file location relative to this module.
_VENDORED_PRICING: Path = Path(__file__).parent / "pricing.toml"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="snipz",
        description="Snipz: LLM cost reservation ledger.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser(
        "update-pricing",
        help="Refresh the vendored pricing.toml from LiteLLM upstream.",
    )
    up.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: the vendored pricing.toml inside the package).",
    )
    up.add_argument(
        "--source",
        default=_LITELLM_PRICE_URL,
        help="Upstream JSON URL (default: LiteLLM's vendored catalogue).",
    )

    sw = sub.add_parser(
        "sweep",
        help="Release expired reservations (one-shot or looping).",
    )
    sw.add_argument(
        "--db",
        required=True,
        help="Backend spec: SQLite path or 'postgres://...' connection string.",
    )
    sw.add_argument(
        "--interval",
        type=float,
        default=None,
        help=(
            "If set, loop sweeping every N seconds until SIGINT/SIGTERM. "
            "Omit for a one-shot sweep (cron / scheduler use)."
        ),
    )

    args = parser.parse_args(argv)
    if args.command == "update-pricing":
        return _cmd_update_pricing(args.output, args.source)
    if args.command == "sweep":
        return _cmd_sweep(args.db, args.interval)
    # argparse already enforces `required=True`, but keep mypy happy.
    return 0  # pragma: no cover


# ---------------------------------------------------------------------------
# update-pricing
# ---------------------------------------------------------------------------


def _cmd_update_pricing(output: Path | None, source: str) -> int:
    target = output if output is not None else _VENDORED_PRICING
    print(f"Fetching {source}", file=sys.stderr)
    try:
        data = _fetch_upstream(source)
    except URLError as exc:
        print(f"error: failed to fetch {source}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: upstream response is not valid JSON: {exc}", file=sys.stderr)
        return 1

    toml_text = _litellm_to_toml(data)

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(toml_text, encoding="utf-8")
    tmp.replace(target)  # atomic on POSIX; best-effort on Windows
    print(f"Wrote {target}", file=sys.stderr)
    return 0


def _fetch_upstream(url: str) -> Any:
    """Fetch JSON from ``url``. Factored out so tests can monkey-patch.

    Returns whatever ``json.loads`` returns; the translator below
    defensively handles non-dict top-level shapes.
    """
    with urlopen(url, timeout=30) as response:  # noqa: S310 — caller supplies URL
        raw = response.read()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def _cmd_sweep(db: str, interval: float | None) -> int:
    """CLI handler for ``snipz sweep`` — one-shot or looping."""
    import asyncio
    import logging

    from snipz import Budget
    from snipz.sweep import sweep_loop, sweep_once

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    async def run() -> int:
        budget = Budget(db)
        try:
            if interval is None:
                return await sweep_once(budget)
            stop = asyncio.Event()
            _install_sweep_signal_handlers(stop)
            return await sweep_loop(budget, interval=interval, stop=stop)
        finally:
            await budget.close()

    total = asyncio.run(run())
    print(f"Released {total} expired reservations.", file=sys.stderr)
    return 0


def _install_sweep_signal_handlers(stop: Any) -> None:
    """Install portable SIGINT/SIGTERM handlers that set ``stop``.

    Uses ``signal.signal`` rather than ``loop.add_signal_handler`` so the
    same code works on Unix and Windows. Failures (signal not available
    on this platform, not in main thread) are silently ignored — the
    sweeper still works, just without graceful early-stop.
    """
    import asyncio
    import signal
    from contextlib import suppress

    loop = asyncio.get_running_loop()

    def _handler(_signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(OSError, ValueError):
            signal.signal(sig, _handler)


# ---------------------------------------------------------------------------
# Translator — pure function
# ---------------------------------------------------------------------------


def _litellm_to_toml(data: Any) -> str:
    """Translate LiteLLM's price catalogue JSON into snipz pricing TOML.

    Entries without ``litellm_provider``, ``input_cost_per_token``, or
    ``output_cost_per_token`` are skipped (LiteLLM ships some
    non-LLM entries we cannot price). The output groups entries by
    provider and sorts deterministically so the file diffs cleanly
    across refreshes.
    """
    by_provider: dict[str, dict[str, dict[str, str]]] = {}

    if not isinstance(data, dict):
        data = {}

    for model_name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        provider = entry.get("litellm_provider")
        input_per_token = entry.get("input_cost_per_token")
        output_per_token = entry.get("output_cost_per_token")
        if not isinstance(provider, str):
            continue
        if input_per_token is None or output_per_token is None:
            continue

        toml_entry: dict[str, str] = {
            "input_cents_per_m": _to_cpm(input_per_token),
            "output_cents_per_m": _to_cpm(output_per_token),
        }
        cache_read = entry.get("cache_read_input_token_cost")
        if cache_read is not None:
            toml_entry["cache_read_cents_per_m"] = _to_cpm(cache_read)
        cache_write = entry.get("cache_creation_input_token_cost")
        if cache_write is not None:
            toml_entry["cache_write_cents_per_m"] = _to_cpm(cache_write)

        by_provider.setdefault(provider, {})[model_name] = toml_entry

    lines: list[str] = [
        "# Snipz pricing — regenerated from LiteLLM upstream.",
        "# Run `snipz update-pricing` to refresh.",
        "",
    ]
    for provider in sorted(by_provider):
        lines.append(f"# {'-' * 73}")
        lines.append(f"# {provider}")
        lines.append(f"# {'-' * 73}")
        lines.append("")
        for model in sorted(by_provider[provider]):
            entry = by_provider[provider][model]
            lines.append(f'[{provider}."{model}"]')
            for key in (
                "input_cents_per_m",
                "output_cents_per_m",
                "cache_read_cents_per_m",
                "cache_write_cents_per_m",
            ):
                if key in entry:
                    lines.append(f'{key} = "{entry[key]}"')
            lines.append("")

    return "\n".join(lines)


def _to_cpm(dollars_per_token: object) -> str:
    """Convert ``$X / token`` to ``cents per million tokens`` as a string.

    Goes through :class:`Decimal` to avoid float drift; trims trailing
    zeros after the decimal point so integer values stay clean.
    """
    decimal_value = Decimal(str(dollars_per_token)) * _HUNDRED_MILLION
    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
