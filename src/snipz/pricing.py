"""LLM price book.

Maps ``(provider, model)`` to per-token costs. Used by callers to
compute ``actual_cents`` after an LLM call:

    from snipz import Pricing
    pricing = Pricing.default()
    cents = pricing.cost(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    await r.commit(cents)

Pricing is a pure value class — it does not touch the database or
the network. The vendored TOML at :data:`PRICING_TOML_RESOURCE` is the
default seed; per-deployment overrides via the ``snipz_pricing`` table
land in Stage 3b.

All arithmetic uses :class:`decimal.Decimal` to avoid float drift on
financial calculations.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from snipz.storage import Backend

__all__ = [
    "PRICING_TOML_RESOURCE",
    "PriceEntry",
    "Pricing",
    "UnknownPricingError",
]


PRICING_TOML_RESOURCE: Final = "pricing.toml"
_MILLION: Final = Decimal("1000000")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnknownPricingError(KeyError):
    """Raised when ``(provider, model)`` has no pricing entry."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        super().__init__(f"no pricing entry for {provider}/{model}")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceEntry:
    """Per-million-token cents for one model.

    Cache fields are ``None`` for providers / models that do not support
    prompt caching. Passing nonzero cache tokens at :meth:`Pricing.cost`
    time when the field is ``None`` raises :class:`ValueError`.
    """

    input_cents_per_m: Decimal
    output_cents_per_m: Decimal
    cache_read_cents_per_m: Decimal | None = None
    cache_write_cents_per_m: Decimal | None = None


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class Pricing:
    """A read-only price book keyed by ``(provider, model)``.

    Construct via :meth:`default` (vendored TOML), :meth:`from_toml`
    (custom TOML string), or by passing a pre-built ``entries`` dict
    directly. Instances are immutable; merge two price books with
    :meth:`overridden_by`.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries: dict[tuple[str, str], PriceEntry]) -> None:
        # Defensive copy so callers cannot mutate after construction.
        self._entries = dict(entries)

    # -- constructors ---------------------------------------------------------

    @classmethod
    def default(cls) -> Pricing:
        """Load the vendored pricing snapshot shipped with the package."""
        text = (files("snipz") / PRICING_TOML_RESOURCE).read_text(encoding="utf-8")
        return cls.from_toml(text)

    @classmethod
    async def with_backend(cls, backend: Backend) -> Pricing:
        """Load vendored defaults and layer ``snipz_pricing`` overrides on top.

        Reads every row of ``snipz_pricing`` (latest ``valid_from`` per
        ``(provider, model)``) and overlays them on the vendored TOML.
        DB rows always win — that is the override semantic. Returns a
        plain :class:`Pricing` instance that can be cached and reused.
        """
        defaults = cls.default()
        async with backend.connect() as conn:
            rows = await conn.load_pricing()
        if not rows:
            return defaults
        db_entries = {
            (row.provider, row.model): PriceEntry(
                input_cents_per_m=row.input_cents_per_m,
                output_cents_per_m=row.output_cents_per_m,
                cache_read_cents_per_m=row.cache_read_cents_per_m,
                cache_write_cents_per_m=row.cache_write_cents_per_m,
            )
            for row in rows
        }
        return defaults.overridden_by(cls(db_entries))

    @classmethod
    def from_toml(cls, text: str) -> Pricing:
        """Parse a TOML document into a :class:`Pricing`.

        Expected shape::

            [<provider>."<model>"]
            input_cents_per_m = "300"
            output_cents_per_m = "1500"
            cache_read_cents_per_m = "30"      # optional
            cache_write_cents_per_m = "375"    # optional

        Numeric values are stored as quoted strings to preserve
        ``Decimal`` precision through the TOML round-trip.
        """
        raw = tomllib.loads(text)
        entries: dict[tuple[str, str], PriceEntry] = {}
        for provider, models in raw.items():
            if not isinstance(models, dict):
                raise ValueError(
                    f"top-level key {provider!r} must be a table of models, "
                    f"got {type(models).__name__}"
                )
            for model, fields in models.items():
                if not isinstance(fields, dict):
                    raise ValueError(
                        f"entry {provider}/{model} must be a table, "
                        f"got {type(fields).__name__}"
                    )
                entries[provider, model] = _parse_entry(provider, model, fields)
        return cls(entries)

    # -- mutators (return new instance) ---------------------------------------

    def overridden_by(self, other: Pricing) -> Pricing:
        """Return a new :class:`Pricing` where ``other``'s entries win.

        Used to layer DB overrides on top of the vendored TOML. Entries
        in ``self`` not present in ``other`` are preserved unchanged.
        """
        merged = dict(self._entries)
        merged.update(other._entries)
        return Pricing(merged)

    # -- lookup ---------------------------------------------------------------

    def cost(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> Decimal:
        """Compute the total cost of an LLM call in cents.

        Raises :class:`UnknownPricingError` when ``(provider, model)``
        has no entry. Raises :class:`ValueError` when nonzero cache
        tokens are passed for a model without cache pricing, or when
        any token count is negative.
        """
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        if cache_read_tokens < 0 or cache_write_tokens < 0:
            raise ValueError("cache token counts must be non-negative")

        try:
            entry = self._entries[provider, model]
        except KeyError as exc:
            raise UnknownPricingError(provider, model) from exc

        total = (
            Decimal(input_tokens) * entry.input_cents_per_m
            + Decimal(output_tokens) * entry.output_cents_per_m
        )
        if cache_read_tokens:
            if entry.cache_read_cents_per_m is None:
                raise ValueError(
                    f"{provider}/{model} has no cache_read pricing; "
                    "cannot bill cache_read_tokens"
                )
            total += Decimal(cache_read_tokens) * entry.cache_read_cents_per_m
        if cache_write_tokens:
            if entry.cache_write_cents_per_m is None:
                raise ValueError(
                    f"{provider}/{model} has no cache_write pricing; "
                    "cannot bill cache_write_tokens"
                )
            total += Decimal(cache_write_tokens) * entry.cache_write_cents_per_m

        return total / _MILLION

    # -- introspection --------------------------------------------------------

    def get(self, provider: str, model: str) -> PriceEntry | None:
        """Return the raw entry for inspection, or ``None`` if absent."""
        return self._entries.get((provider, model))

    def models(self) -> tuple[tuple[str, str], ...]:
        """Return all priced ``(provider, model)`` pairs."""
        return tuple(self._entries.keys())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries


# ---------------------------------------------------------------------------
# Internal — TOML row parser
# ---------------------------------------------------------------------------


def _parse_entry(provider: str, model: str, fields: dict[str, object]) -> PriceEntry:
    """Validate and convert one TOML entry into a :class:`PriceEntry`."""
    try:
        input_str = fields["input_cents_per_m"]
        output_str = fields["output_cents_per_m"]
    except KeyError as exc:
        missing = exc.args[0]
        raise ValueError(
            f"entry {provider}/{model} missing required field {missing!r}"
        ) from exc

    if not isinstance(input_str, str):
        raise ValueError(
            f"{provider}/{model}.input_cents_per_m must be a quoted string, "
            f"got {type(input_str).__name__}"
        )
    if not isinstance(output_str, str):
        raise ValueError(
            f"{provider}/{model}.output_cents_per_m must be a quoted string, "
            f"got {type(output_str).__name__}"
        )

    cache_read = _opt_decimal(provider, model, fields, "cache_read_cents_per_m")
    cache_write = _opt_decimal(provider, model, fields, "cache_write_cents_per_m")

    return PriceEntry(
        input_cents_per_m=Decimal(input_str),
        output_cents_per_m=Decimal(output_str),
        cache_read_cents_per_m=cache_read,
        cache_write_cents_per_m=cache_write,
    )


def _opt_decimal(
    provider: str,
    model: str,
    fields: dict[str, object],
    key: str,
) -> Decimal | None:
    raw = fields.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"{provider}/{model}.{key} must be a quoted string, "
            f"got {type(raw).__name__}"
        )
    return Decimal(raw)
