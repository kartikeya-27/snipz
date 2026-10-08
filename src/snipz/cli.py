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
import re
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
    "model_prices_and_context_window.json"
)

# Conversion factor: a price expressed as dollars per token becomes
# cents per million tokens after multiplying by 10**8.
_HUNDRED_MILLION: Decimal = Decimal("100000000")

# Vendored pricing file location relative to this module.
_VENDORED_PRICING: Path = Path(__file__).parent / "pricing.toml"

# LiteLLM's catalogue opens with a documentation placeholder whose
# ``litellm_provider`` is prose ("one of https://docs..."). It prices
# nothing, so the translator drops it by name.
_LITELLM_PLACEHOLDER_KEYS: frozenset[str] = frozenset({"sample_spec"})

# TOML bare keys: ASCII letters, digits, underscore, dash.
_TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


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
    """CLI handler for ``snipz sweep`` — one-shot or looping.

    Exit codes:

    * ``0`` — sweep ran to completion (one-shot returned, or loop
      stopped cleanly on SIGINT/SIGTERM).
    * ``1`` — sweep raised an unhandled exception. The traceback is
      printed; cron / scheduler monitors should treat this as a
      hard failure.
    """
    import asyncio
    import logging

    from snipz import Budget
    from snipz.sweep import sweep_loop, sweep_once

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("snipz.cli")

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

    try:
        total = asyncio.run(run())
    except Exception:
        log.exception("snipz sweep failed; exiting non-zero")
        return 1
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

    LiteLLM keys some providers' models by routing name
    (``mistral/codestral-latest``); the ``<provider>/`` prefix is
    stripped so lookups use the provider's own model id. When upstream
    lists the same model both ways, the unprefixed entry wins.

    Long-prompt rates (``<field>_above_<N>k_tokens``) become
    ``[[...tiers]]`` tables and the 1-hour cache-write rate becomes
    ``cache_write_1h_cents_per_m``. Service-tier variants
    (``_batches``, ``_priority``, ``_flex``) are not translated.
    """
    by_provider: dict[str, dict[str, _TomlEntry]] = {}

    if not isinstance(data, dict):
        data = {}

    for model_name, entry in data.items():
        if model_name in _LITELLM_PLACEHOLDER_KEYS or not isinstance(entry, dict):
            continue
        provider = entry.get("litellm_provider")
        if not isinstance(provider, str):
            continue
        rates = _litellm_rates(entry, suffix="")
        if "input_cents_per_m" not in rates or "output_cents_per_m" not in rates:
            continue

        models = by_provider.setdefault(provider, {})
        prefix = f"{provider}/"
        if model_name.startswith(prefix):
            model_name = model_name.removeprefix(prefix)
            if model_name in models:
                continue
        models[model_name] = (rates, _litellm_tiers(entry))

    lines: list[str] = [
        "# Snipz pricing — regenerated from LiteLLM upstream.",
        "# Run `snipz update-pricing` to refresh.",
        "",
    ]
    for provider in sorted(by_provider):
        lines.append(f"# {'-' * 73}")
        lines.append(f"# {_toml_key(provider)}")
        lines.append(f"# {'-' * 73}")
        lines.append("")
        for model in sorted(by_provider[provider]):
            rates, tiers = by_provider[provider][model]
            table = f"{_toml_key(provider)}.{_toml_quoted(model)}"
            lines.append(f"[{table}]")
            lines.extend(_rate_lines(rates))
            lines.append("")
            for above_tokens, tier_rates in tiers:
                lines.append(f"[[{table}.tiers]]")
                lines.append(f"above_tokens = {above_tokens}")
                lines.extend(_rate_lines(tier_rates))
                lines.append("")

    return "\n".join(lines)


# One translated model: base rates plus ``(above_tokens, rates)`` tiers.
_TomlEntry = tuple[dict[str, str], list[tuple[int, dict[str, str]]]]

# LiteLLM per-token cost field -> snipz per-million cents field, in the
# order fields are written to the TOML.
_LITELLM_RATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("input_cost_per_token", "input_cents_per_m"),
    ("output_cost_per_token", "output_cents_per_m"),
    ("cache_read_input_token_cost", "cache_read_cents_per_m"),
    ("cache_creation_input_token_cost", "cache_write_cents_per_m"),
    ("cache_creation_input_token_cost_above_1hr", "cache_write_1h_cents_per_m"),
)

# ``input_cost_per_token_above_200k_tokens`` etc. Anchored at ``$`` so
# service-tier variants (``..._above_200k_tokens_batches``) never match.
_LITELLM_TIER_KEY = re.compile(r"(?P<field>.+)_above_(?P<thousands>\d+)k_tokens")


def _litellm_rates(entry: dict[str, Any], *, suffix: str) -> dict[str, str]:
    """Collect the rates present in ``entry`` for one tier suffix."""
    rates: dict[str, str] = {}
    for litellm_key, snipz_key in _LITELLM_RATE_FIELDS:
        value = entry.get(litellm_key + suffix)
        if value is not None:
            rates[snipz_key] = _to_cpm(value)
    return rates


def _litellm_tiers(entry: dict[str, Any]) -> list[tuple[int, dict[str, str]]]:
    """Long-prompt tiers in ``entry``, sorted by threshold."""
    known_fields = {litellm_key for litellm_key, _ in _LITELLM_RATE_FIELDS}
    thresholds: set[int] = set()
    for key in entry:
        match = _LITELLM_TIER_KEY.fullmatch(key)
        if match and match["field"] in known_fields:
            thresholds.add(int(match["thousands"]))
    tiers = []
    for thousands in sorted(thresholds):
        rates = _litellm_rates(entry, suffix=f"_above_{thousands}k_tokens")
        if rates:
            tiers.append((thousands * 1000, rates))
    return tiers


def _rate_lines(rates: dict[str, str]) -> list[str]:
    return [
        f'{snipz_key} = "{rates[snipz_key]}"'
        for _, snipz_key in _LITELLM_RATE_FIELDS
        if snipz_key in rates
    ]


def _toml_key(key: str) -> str:
    """Emit ``key`` bare when TOML allows it, quoted otherwise.

    Upstream provider names are not under our control; one with a space
    or a dot would otherwise produce an unloadable file.
    """
    return key if _TOML_BARE_KEY.fullmatch(key) else _toml_quoted(key)


def _toml_quoted(key: str) -> str:
    """Emit ``key`` as a TOML basic string.

    JSON's string escapes are a subset of TOML's basic-string escapes,
    so ``json.dumps`` is a valid encoder for quotes, backslashes, and
    control characters alike.
    """
    return json.dumps(key)


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
