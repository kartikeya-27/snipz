"""Tests for the :class:`snipz.Pricing` price book."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from snipz import PriceEntry, Pricing, UnknownPricingError
from snipz.storage.sqlite import SqliteBackend

# ---------------------------------------------------------------------------
# Vendored default
# ---------------------------------------------------------------------------


def test_default_loads_known_models() -> None:
    """The vendored TOML must load and contain at least the seeded providers."""
    pricing = Pricing.default()

    assert len(pricing) > 0
    providers = {p for p, _ in pricing.models()}
    assert {"anthropic", "openai", "google", "mistral"} <= providers


def test_default_anthropic_sonnet_has_cache_pricing() -> None:
    pricing = Pricing.default()
    entry = pricing.get("anthropic", "claude-3-5-sonnet-20241022")

    assert entry is not None
    assert entry.input_cents_per_m == Decimal("300")
    assert entry.output_cents_per_m == Decimal("1500")
    assert entry.cache_read_cents_per_m == Decimal("30")
    assert entry.cache_write_cents_per_m == Decimal("375")


def test_default_openai_gpt4o_has_no_cache_pricing() -> None:
    pricing = Pricing.default()
    entry = pricing.get("openai", "gpt-4o")

    assert entry is not None
    assert entry.cache_read_cents_per_m is None
    assert entry.cache_write_cents_per_m is None


# ---------------------------------------------------------------------------
# cost() arithmetic
# ---------------------------------------------------------------------------


def test_cost_basic_input_output() -> None:
    pricing = Pricing.default()
    # 1M input tokens * $3/M + 0 output = $3 = 300 cents.
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cents == Decimal("300")


def test_cost_mixed_input_output() -> None:
    pricing = Pricing.default()
    # 1000 input ($0.003) + 500 output ($0.0075) = $0.0105 = 1.05 cents.
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=1000,
        output_tokens=500,
    )
    # 1000 * 300 / 1_000_000 = 0.3 cents
    # 500 * 1500 / 1_000_000 = 0.75 cents
    # total = 1.05 cents
    assert cents == Decimal("1.05")


def test_cost_with_cache_tokens_included() -> None:
    pricing = Pricing.default()
    # 1M cache_read * $0.30/M = 30 cents.
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cents == Decimal("30")


def test_cost_with_cache_write_tokens_included() -> None:
    pricing = Pricing.default()
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=1_000_000,
    )
    assert cents == Decimal("375")


def test_cost_zero_tokens_returns_zero() -> None:
    pricing = Pricing.default()
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=0,
        output_tokens=0,
    )
    assert cents == Decimal("0")


def test_cost_preserves_decimal_precision() -> None:
    """A floating-point implementation would lose precision here."""
    pricing = Pricing.default()
    # 1 input token at $3/M = 0.0000003 dollars = 0.00003 cents.
    # 1 * 300 / 1_000_000 = 3e-4 cents.
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=1,
        output_tokens=0,
    )
    assert cents == Decimal("3E-4")
    # And the value is bit-for-bit reproducible.
    cents2 = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=1,
        output_tokens=0,
    )
    assert str(cents) == str(cents2)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_cost_unknown_model_raises_unknown_pricing_error() -> None:
    pricing = Pricing.default()
    with pytest.raises(UnknownPricingError) as excinfo:
        pricing.cost(
            provider="anthropic",
            model="not-a-real-model",
            input_tokens=100,
            output_tokens=100,
        )
    assert excinfo.value.provider == "anthropic"
    assert excinfo.value.model == "not-a-real-model"


def test_cost_negative_tokens_raises() -> None:
    pricing = Pricing.default()
    with pytest.raises(ValueError, match="non-negative"):
        pricing.cost(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            input_tokens=-1,
            output_tokens=100,
        )


def test_cost_cache_tokens_without_cache_pricing_raises() -> None:
    """GPT-4o has no cache pricing; passing cache tokens must fail."""
    pricing = Pricing.default()
    with pytest.raises(ValueError, match="no cache_read pricing"):
        pricing.cost(
            provider="openai",
            model="gpt-4o",
            input_tokens=100,
            output_tokens=100,
            cache_read_tokens=10,
        )


# ---------------------------------------------------------------------------
# from_toml — custom pricing
# ---------------------------------------------------------------------------


def test_from_toml_round_trips_simple_entry() -> None:
    toml = """
        [openai."gpt-5"]
        input_cents_per_m = "100"
        output_cents_per_m = "400"
    """
    pricing = Pricing.from_toml(toml)
    entry = pricing.get("openai", "gpt-5")
    assert entry == PriceEntry(
        input_cents_per_m=Decimal("100"),
        output_cents_per_m=Decimal("400"),
    )


def test_from_toml_missing_required_field_raises() -> None:
    toml = """
        [openai."gpt-5"]
        input_cents_per_m = "100"
    """
    with pytest.raises(ValueError, match="missing required field"):
        Pricing.from_toml(toml)


def test_from_toml_non_string_price_raises() -> None:
    """We require quoted strings for prices to preserve Decimal precision."""
    toml = """
        [openai."gpt-5"]
        input_cents_per_m = 100
        output_cents_per_m = "400"
    """
    with pytest.raises(ValueError, match="must be a quoted string"):
        Pricing.from_toml(toml)


# ---------------------------------------------------------------------------
# overridden_by — DB-override layering
# ---------------------------------------------------------------------------


def test_overridden_by_replaces_entries() -> None:
    base = Pricing.from_toml(
        """
        [openai."gpt-5"]
        input_cents_per_m = "100"
        output_cents_per_m = "400"
        """
    )
    override = Pricing.from_toml(
        """
        [openai."gpt-5"]
        input_cents_per_m = "50"
        output_cents_per_m = "200"
        """
    )
    merged = base.overridden_by(override)
    entry = merged.get("openai", "gpt-5")
    assert entry == PriceEntry(
        input_cents_per_m=Decimal("50"),
        output_cents_per_m=Decimal("200"),
    )


def test_overridden_by_preserves_non_overridden_entries() -> None:
    base = Pricing.from_toml(
        """
        [openai."gpt-5"]
        input_cents_per_m = "100"
        output_cents_per_m = "400"

        [anthropic."claude-99"]
        input_cents_per_m = "1"
        output_cents_per_m = "2"
        """
    )
    override = Pricing.from_toml(
        """
        [openai."gpt-5"]
        input_cents_per_m = "50"
        output_cents_per_m = "200"
        """
    )
    merged = base.overridden_by(override)
    # Override replaces openai/gpt-5; anthropic/claude-99 survives.
    assert merged.get("openai", "gpt-5") is not None
    assert merged.get("anthropic", "claude-99") is not None
    assert merged.get("anthropic", "claude-99").input_cents_per_m == Decimal("1")  # type: ignore[union-attr]


def test_overridden_by_does_not_mutate_either_input() -> None:
    base = Pricing.from_toml(
        """
        [openai."gpt-5"]
        input_cents_per_m = "100"
        output_cents_per_m = "400"
        """
    )
    override = Pricing.from_toml(
        """
        [openai."gpt-5"]
        input_cents_per_m = "50"
        output_cents_per_m = "200"
        """
    )
    base.overridden_by(override)
    # Originals are unchanged.
    assert base.get("openai", "gpt-5") == PriceEntry(
        input_cents_per_m=Decimal("100"),
        output_cents_per_m=Decimal("400"),
    )
    assert override.get("openai", "gpt-5") == PriceEntry(
        input_cents_per_m=Decimal("50"),
        output_cents_per_m=Decimal("200"),
    )


# ---------------------------------------------------------------------------
# with_backend — DB override layered on vendored TOML
# ---------------------------------------------------------------------------


_INSERT_PRICING = (
    "INSERT INTO snipz_pricing "
    "(provider, model, input_cents_per_m, output_cents_per_m, "
    " cache_read_cents_per_m, cache_write_cents_per_m, valid_from) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


async def _insert_db_pricing(
    db_path: Path,
    *,
    provider: str,
    model: str,
    input_cpm: str,
    output_cpm: str,
    cache_read_cpm: str | None = None,
    cache_write_cpm: str | None = None,
    valid_from: str = "2026-01-01T00:00:00.000Z",
) -> None:
    """Insert a pricing row via raw aiosqlite (bypasses our layered API)."""
    async with aiosqlite.connect(str(db_path), isolation_level=None) as conn:
        await conn.execute(
            _INSERT_PRICING,
            (provider, model, input_cpm, output_cpm, cache_read_cpm, cache_write_cpm, valid_from),
        )


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path: Path) -> AsyncIterator[tuple[SqliteBackend, Path]]:
    """A migrated SqliteBackend plus the underlying db_path for raw inserts."""
    db_path = tmp_path / "snipz.db"
    backend = SqliteBackend(db_path)
    await backend.migrate()
    try:
        yield backend, db_path
    finally:
        await backend.close()


async def test_with_backend_returns_vendored_when_table_empty(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    backend, _ = sqlite_backend
    pricing = await Pricing.with_backend(backend)
    # Without DB rows, falls back to vendored defaults verbatim.
    assert pricing.get("anthropic", "claude-3-5-sonnet-20241022") is not None
    assert pricing.get("openai", "gpt-4o") is not None


async def test_with_backend_db_row_overrides_vendored(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    backend, db_path = sqlite_backend
    # Replace the vendored sonnet price with a custom rate.
    await _insert_db_pricing(
        db_path,
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_cpm="100",
        output_cpm="500",
    )
    pricing = await Pricing.with_backend(backend)
    entry = pricing.get("anthropic", "claude-3-5-sonnet-20241022")
    assert entry == PriceEntry(
        input_cents_per_m=Decimal("100"),
        output_cents_per_m=Decimal("500"),
        cache_read_cents_per_m=None,
        cache_write_cents_per_m=None,
    )


async def test_with_backend_picks_latest_valid_from(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    backend, db_path = sqlite_backend
    # Older row.
    await _insert_db_pricing(
        db_path,
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_cpm="100",
        output_cpm="500",
        valid_from="2026-01-01T00:00:00.000Z",
    )
    # Newer row — should win.
    await _insert_db_pricing(
        db_path,
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_cpm="200",
        output_cpm="1000",
        valid_from="2026-06-01T00:00:00.000Z",
    )
    pricing = await Pricing.with_backend(backend)
    entry = pricing.get("anthropic", "claude-3-5-sonnet-20241022")
    assert entry is not None
    assert entry.input_cents_per_m == Decimal("200")
    assert entry.output_cents_per_m == Decimal("1000")


async def test_with_backend_adds_models_not_in_vendored(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    backend, db_path = sqlite_backend
    await _insert_db_pricing(
        db_path,
        provider="custom",
        model="my-fine-tune",
        input_cpm="10",
        output_cpm="50",
    )
    pricing = await Pricing.with_backend(backend)
    entry = pricing.get("custom", "my-fine-tune")
    assert entry == PriceEntry(
        input_cents_per_m=Decimal("10"),
        output_cents_per_m=Decimal("50"),
        cache_read_cents_per_m=None,
        cache_write_cents_per_m=None,
    )
    # Vendored entries still present.
    assert pricing.get("anthropic", "claude-3-5-sonnet-20241022") is not None
