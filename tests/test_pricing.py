"""Tests for the :class:`snipz.Pricing` price book."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from snipz import PriceEntry, PriceTier, Pricing, UnknownPricingError
from snipz.storage.sqlite import SqliteBackend

# ---------------------------------------------------------------------------
# Vendored default
# ---------------------------------------------------------------------------
#
# The vendored file is regenerated from LiteLLM by ``snipz update-pricing``,
# so these tests assert shape and coverage, never exact prices — a price
# change upstream must not break the suite.


def test_default_loads_known_models() -> None:
    """The vendored TOML must load and cover the first-party providers."""
    pricing = Pricing.default()

    assert len(pricing) > 0
    providers = {p for p, _ in pricing.models()}
    assert {"anthropic", "openai", "gemini", "mistral"} <= providers


def test_default_current_anthropic_model_has_full_cache_pricing() -> None:
    entry = Pricing.default().get("anthropic", "claude-opus-5-5")

    assert entry is not None
    assert entry.cache_read_cents_per_m is not None
    assert entry.cache_write_cents_per_m is not None


def test_default_first_party_model_ids_have_no_routing_prefix() -> None:
    """``update-pricing`` strips LiteLLM's ``<provider>/`` routing prefix, so
    ``("mistral", "codestral-latest")`` resolves rather than
    ``("mistral", "mistral/codestral-latest")``.

    Restricted to first-party APIs: aggregators such as OpenRouter have
    real model ids like ``openrouter/auto`` that legitimately keep it.
    """
    first_party = {"anthropic", "openai", "gemini", "mistral", "xai", "deepseek"}
    prefixed = [
        (provider, model)
        for provider, model in Pricing.default().models()
        if provider in first_party and model.startswith(f"{provider}/")
    ]
    assert prefixed == []


# ---------------------------------------------------------------------------
# Fixed price book for arithmetic tests
# ---------------------------------------------------------------------------
#
# Frozen figures, independent of the vendored snapshot: one model with
# cache pricing ($3 / $15 per M, cache read $0.30, cache write $3.75) and
# one without.

_FIXTURE_TOML = """
[anthropic."claude-3-5-sonnet-20241022"]
input_cents_per_m = "300"
output_cents_per_m = "1500"
cache_read_cents_per_m = "30"
cache_write_cents_per_m = "375"

[openai."gpt-4o"]
input_cents_per_m = "250"
output_cents_per_m = "1000"
"""


def _fixture_pricing() -> Pricing:
    return Pricing.from_toml(_FIXTURE_TOML)


# ---------------------------------------------------------------------------
# cost() arithmetic
# ---------------------------------------------------------------------------


def test_cost_basic_input_output() -> None:
    pricing = _fixture_pricing()
    # 1M input tokens * $3/M + 0 output = $3 = 300 cents.
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cents == Decimal("300")


def test_cost_mixed_input_output() -> None:
    pricing = _fixture_pricing()
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
    pricing = _fixture_pricing()
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
    pricing = _fixture_pricing()
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=1_000_000,
    )
    assert cents == Decimal("375")


def test_cost_zero_tokens_returns_zero() -> None:
    pricing = _fixture_pricing()
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=0,
        output_tokens=0,
    )
    assert cents == Decimal("0")


def test_cost_preserves_decimal_precision() -> None:
    """A floating-point implementation would lose precision here."""
    pricing = _fixture_pricing()
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
    pricing = _fixture_pricing()
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
    pricing = _fixture_pricing()
    with pytest.raises(ValueError, match="non-negative"):
        pricing.cost(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            input_tokens=-1,
            output_tokens=100,
        )


def test_cost_cache_tokens_without_cache_pricing_raises() -> None:
    """GPT-4o has no cache pricing; passing cache tokens must fail."""
    pricing = _fixture_pricing()
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
# Long-prompt tiers and the 1-hour cache-write rate
# ---------------------------------------------------------------------------
#
# Shaped like Claude Haiku 5.5 (base $0.10 / $0.50; over 100K tokens
# $0.50 / $2.50) plus a second tier to exercise tier selection.

_TIERED_TOML = """
[anthropic."tiered"]
input_cents_per_m = "10"
output_cents_per_m = "50"
cache_read_cents_per_m = "1"
cache_write_cents_per_m = "12.5"
cache_write_1h_cents_per_m = "20"

[[anthropic."tiered".tiers]]
above_tokens = 500000
input_cents_per_m = "90"

[[anthropic."tiered".tiers]]
above_tokens = 100000
input_cents_per_m = "50"
output_cents_per_m = "250"
cache_read_cents_per_m = "5"
cache_write_cents_per_m = "62.5"
cache_write_1h_cents_per_m = "100"
"""


def _tiered_cost(**tokens: int) -> Decimal:
    tokens.setdefault("output_tokens", 0)
    return Pricing.from_toml(_TIERED_TOML).cost(
        provider="anthropic", model="tiered", **tokens
    )


def test_tiers_parse_sorted_by_threshold() -> None:
    entry = Pricing.from_toml(_TIERED_TOML).get("anthropic", "tiered")
    assert entry is not None
    assert [t.above_tokens for t in entry.tiers] == [100_000, 500_000]
    assert entry.cache_write_1h_cents_per_m == Decimal("20")


def test_tier_threshold_is_strictly_greater_than() -> None:
    """"Prompts over 100,000 tokens": exactly 100,000 stays at base rates."""
    assert _tiered_cost(input_tokens=100_000) == Decimal("1")  # 100K * 10 / 1M
    assert _tiered_cost(input_tokens=100_001) == Decimal("5.00005")  # * 50


def test_tier_applies_to_output_of_a_long_prompt() -> None:
    """The whole request moves to the tier, output included."""
    cents = _tiered_cost(input_tokens=200_000, output_tokens=1_000_000)
    assert cents == Decimal("260")  # 200K * 50 / 1M + 1M * 250 / 1M


def test_tier_prompt_size_counts_cache_tokens() -> None:
    """60K uncached + 50K cache-read is a 110K-token prompt: tier rates."""
    cents = _tiered_cost(input_tokens=60_000, cache_read_tokens=50_000)
    assert cents == Decimal("3.25")  # 60K * 50 + 50K * 5, / 1M


def test_highest_matching_tier_wins_and_unset_rates_fall_back_to_base() -> None:
    """Above 500K the second tier sets only input; output falls back to the
    *base* rate, not the 100K tier's — tiers are independent overrides."""
    cents = _tiered_cost(input_tokens=600_000, output_tokens=1_000_000)
    assert cents == Decimal("104")  # 600K * 90 / 1M + 1M * 50 / 1M


def test_cache_write_1h_tokens_bill_at_1h_rate() -> None:
    cents = _tiered_cost(
        input_tokens=0, cache_write_tokens=10_000, cache_write_1h_tokens=10_000
    )
    assert cents == Decimal("0.325")  # 10K * 12.5 + 10K * 20, / 1M


def test_cache_write_1h_tokens_without_1h_pricing_raises() -> None:
    with pytest.raises(ValueError, match="no cache_write_1h pricing"):
        _fixture_pricing().cost(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            input_tokens=0,
            output_tokens=0,
            cache_write_1h_tokens=1,
        )


@pytest.mark.parametrize(
    ("tier_toml", "message"),
    [
        ('above_tokens = "100000"\ninput_cents_per_m = "1"', "positive integer"),
        ('above_tokens = 0\ninput_cents_per_m = "1"', "positive integer"),
        ("above_tokens = 100000", "sets no rates"),
        ("above_tokens = 100000\ninput_cents_per_m = 1", "must be a quoted string"),
    ],
)
def test_invalid_tier_raises(tier_toml: str, message: str) -> None:
    toml = (
        '[openai."m"]\ninput_cents_per_m = "1"\noutput_cents_per_m = "1"\n\n'
        f'[[openai."m".tiers]]\n{tier_toml}\n'
    )
    with pytest.raises(ValueError, match=message):
        Pricing.from_toml(toml)


def test_duplicate_tier_thresholds_raise() -> None:
    tier = '[[openai."m".tiers]]\nabove_tokens = 1000\ninput_cents_per_m = "2"\n'
    toml = '[openai."m"]\ninput_cents_per_m = "1"\noutput_cents_per_m = "1"\n' + tier + tier
    with pytest.raises(ValueError, match="duplicate above_tokens"):
        Pricing.from_toml(toml)


def test_default_haiku_5_5_has_long_prompt_tier() -> None:
    """Haiku 5.5 is the one current Claude model priced by prompt length;
    the refreshed vendored file must carry that tier (shape, not price)."""
    entry = Pricing.default().get("anthropic", "claude-haiku-5-5")
    assert entry is not None
    assert [t.above_tokens for t in entry.tiers] == [100_000]
    assert entry.cache_write_1h_cents_per_m is not None


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
    assert pricing.get("anthropic", "claude-opus-5-5") is not None
    assert pricing.get("openai", "gpt-5") is not None


async def test_with_backend_db_row_overrides_vendored(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    backend, db_path = sqlite_backend
    # Replace the vendored Opus price with a custom rate.
    await _insert_db_pricing(
        db_path,
        provider="anthropic",
        model="claude-opus-5-5",
        input_cpm="100",
        output_cpm="500",
    )
    pricing = await Pricing.with_backend(backend)
    entry = pricing.get("anthropic", "claude-opus-5-5")
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
        model="claude-opus-5-5",
        input_cpm="100",
        output_cpm="500",
        valid_from="2026-01-01T00:00:00.000Z",
    )
    # Newer row — should win.
    await _insert_db_pricing(
        db_path,
        provider="anthropic",
        model="claude-opus-5-5",
        input_cpm="200",
        output_cpm="1000",
        valid_from="2026-06-01T00:00:00.000Z",
    )
    pricing = await Pricing.with_backend(backend)
    entry = pricing.get("anthropic", "claude-opus-5-5")
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
    assert pricing.get("anthropic", "claude-opus-5-5") is not None


async def test_with_backend_carries_cache_pricing_through_db_override(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    """A DB row with cache_read/write must end up on the merged ``PriceEntry``."""
    backend, db_path = sqlite_backend
    await _insert_db_pricing(
        db_path,
        provider="custom",
        model="caching-model",
        input_cpm="100",
        output_cpm="200",
        cache_read_cpm="10",
        cache_write_cpm="125",
    )
    pricing = await Pricing.with_backend(backend)
    entry = pricing.get("custom", "caching-model")
    assert entry == PriceEntry(
        input_cents_per_m=Decimal("100"),
        output_cents_per_m=Decimal("200"),
        cache_read_cents_per_m=Decimal("10"),
        cache_write_cents_per_m=Decimal("125"),
    )


async def test_with_backend_computes_cost_using_db_override(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    """End-to-end: the override flows from ``with_backend`` through to ``cost()``."""
    backend, db_path = sqlite_backend
    await _insert_db_pricing(
        db_path,
        provider="anthropic",
        model="claude-opus-5-5",
        input_cpm="100",
        output_cpm="500",
    )
    pricing = await Pricing.with_backend(backend)
    # 1M input * 100 cents + 1M output * 500 cents = 600 cents.
    cents = pricing.cost(
        provider="anthropic",
        model="claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cents == Decimal("600")


async def test_with_backend_reads_1h_rate_and_tiers_from_db(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    """Migration 0002 columns: a DB override can carry the 1-hour cache-write
    rate and long-prompt tiers, so overriding a tiered model keeps its tiers."""
    backend, db_path = sqlite_backend
    tiers = '[{"above_tokens": 100000, "input_cents_per_m": "50", "output_cents_per_m": "250"}]'
    async with aiosqlite.connect(str(db_path), isolation_level=None) as conn:
        await conn.execute(
            "INSERT INTO snipz_pricing (provider, model, input_cents_per_m, "
            "output_cents_per_m, cache_write_1h_cents_per_m, tiers, valid_from) "
            "VALUES ('custom', 'tiered', '10', '50', '20', ?, '2026-01-01T00:00:00.000Z')",
            (tiers,),
        )
    pricing = await Pricing.with_backend(backend)

    entry = pricing.get("custom", "tiered")
    assert entry is not None
    assert entry.cache_write_1h_cents_per_m == Decimal("20")
    assert entry.tiers == (
        PriceTier(
            above_tokens=100_000,
            input_cents_per_m=Decimal("50"),
            output_cents_per_m=Decimal("250"),
        ),
    )
    cents = pricing.cost(
        provider="custom", model="tiered", input_tokens=200_000, output_tokens=0
    )
    assert cents == Decimal("10")  # 200K * 50 / 1M


async def test_with_backend_rejects_invalid_tiers_json(
    sqlite_backend: tuple[SqliteBackend, Path],
) -> None:
    backend, db_path = sqlite_backend
    async with aiosqlite.connect(str(db_path), isolation_level=None) as conn:
        await conn.execute(
            "INSERT INTO snipz_pricing (provider, model, input_cents_per_m, "
            "output_cents_per_m, tiers, valid_from) "
            "VALUES ('custom', 'broken', '10', '50', '[{oops', '2026-01-01T00:00:00.000Z')"
        )
    with pytest.raises(ValueError, match="invalid tiers JSON"):
        await Pricing.with_backend(backend)


async def test_migrate_upgrades_v1_database_to_v2(tmp_path: Path) -> None:
    """A database created by 0.2.x (schema v1) gains the 0002 columns on
    ``migrate()`` without losing existing pricing rows."""
    from importlib.resources import files

    db_path = tmp_path / "v1.db"
    v1_sql = files("snipz.storage.migrations.sqlite").joinpath("0001_initial.sql").read_text()
    async with aiosqlite.connect(str(db_path), isolation_level=None) as conn:
        await conn.executescript(v1_sql)
    await _insert_db_pricing(
        db_path, provider="custom", model="legacy", input_cpm="10", output_cpm="50"
    )

    backend = SqliteBackend(db_path)
    try:
        await backend.migrate()
        await backend.migrate()  # idempotent
        async with aiosqlite.connect(str(db_path)) as conn:
            cur = await conn.execute("SELECT MAX(version) FROM snipz_schema_version")
            assert await cur.fetchone() == (2,)
        entry = (await Pricing.with_backend(backend)).get("custom", "legacy")
        assert entry == PriceEntry(
            input_cents_per_m=Decimal("10"), output_cents_per_m=Decimal("50")
        )
    finally:
        await backend.close()
