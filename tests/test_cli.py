"""Tests for the :mod:`snipz.cli` command-line interface.

Network access is monkey-patched so tests do not hit LiteLLM upstream.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from snipz import Pricing
from snipz.cli import _litellm_to_toml, _to_cpm, main

# ---------------------------------------------------------------------------
# Sample LiteLLM JSON — covers the conversion edge cases
# ---------------------------------------------------------------------------


SAMPLE_LITELLM_JSON: dict[str, Any] = {
    # Anthropic — full cache support.
    "claude-3-5-sonnet-20241022": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "cache_read_input_token_cost": 3e-7,
        "cache_creation_input_token_cost": 0.00000375,
    },
    # OpenAI — no cache pricing.
    "gpt-4o": {
        "litellm_provider": "openai",
        "input_cost_per_token": 0.0000025,
        "output_cost_per_token": 0.00001,
    },
    # Skipped: no litellm_provider.
    "no-provider-model": {
        "input_cost_per_token": 0.001,
        "output_cost_per_token": 0.001,
    },
    # Skipped: missing input price.
    "no-input-price": {
        "litellm_provider": "anthropic",
        "output_cost_per_token": 0.000015,
    },
    # Skipped: not a dict at all.
    "garbage-value": "not a dict",
}


# ---------------------------------------------------------------------------
# _to_cpm — unit conversion
# ---------------------------------------------------------------------------


def test_to_cpm_integer_value() -> None:
    """$3e-6 per token → 300 cents per million."""
    assert _to_cpm(0.000003) == "300"


def test_to_cpm_fractional_value() -> None:
    """$7.5e-8 per token → 7.5 cents per million."""
    assert _to_cpm(7.5e-8) == "7.5"


def test_to_cpm_zero() -> None:
    assert _to_cpm(0) == "0"


def test_to_cpm_preserves_precision() -> None:
    """Conversion must round-trip through Decimal without float drift."""
    cents = _to_cpm(3.75e-6)
    assert Decimal(cents) == Decimal("375")


# ---------------------------------------------------------------------------
# _litellm_to_toml — translation
# ---------------------------------------------------------------------------


def test_translator_emits_known_entries() -> None:
    toml_text = _litellm_to_toml(SAMPLE_LITELLM_JSON)
    pricing = Pricing.from_toml(toml_text)

    sonnet = pricing.get("anthropic", "claude-3-5-sonnet-20241022")
    assert sonnet is not None
    assert sonnet.input_cents_per_m == Decimal("300")
    assert sonnet.output_cents_per_m == Decimal("1500")
    assert sonnet.cache_read_cents_per_m == Decimal("30")
    assert sonnet.cache_write_cents_per_m == Decimal("375")

    gpt = pricing.get("openai", "gpt-4o")
    assert gpt is not None
    assert gpt.input_cents_per_m == Decimal("250")
    assert gpt.output_cents_per_m == Decimal("1000")
    assert gpt.cache_read_cents_per_m is None


def test_translator_skips_entries_without_provider() -> None:
    toml_text = _litellm_to_toml(SAMPLE_LITELLM_JSON)
    pricing = Pricing.from_toml(toml_text)
    assert pricing.get("anthropic", "no-provider-model") is None
    # And no orphan provider key.
    for provider, _ in pricing.models():
        assert provider != "no-provider"


def test_translator_skips_entries_without_prices() -> None:
    toml_text = _litellm_to_toml(SAMPLE_LITELLM_JSON)
    pricing = Pricing.from_toml(toml_text)
    assert pricing.get("anthropic", "no-input-price") is None


def test_translator_skips_non_dict_entries() -> None:
    """LiteLLM occasionally has stringified placeholders; must not crash."""
    toml_text = _litellm_to_toml(SAMPLE_LITELLM_JSON)
    # No exception; produces valid TOML.
    Pricing.from_toml(toml_text)


def test_translator_output_is_deterministic() -> None:
    """Same input → same output, so refreshes diff cleanly."""
    a = _litellm_to_toml(SAMPLE_LITELLM_JSON)
    b = _litellm_to_toml(SAMPLE_LITELLM_JSON)
    assert a == b


def test_translator_groups_by_provider_with_section_headers() -> None:
    toml_text = _litellm_to_toml(SAMPLE_LITELLM_JSON)
    # Section headers help reviewers diff the file.
    assert "# anthropic" in toml_text
    assert "# openai" in toml_text


def test_translator_empty_input_returns_header_only() -> None:
    toml_text = _litellm_to_toml({})
    # Still parses as empty TOML.
    pricing = Pricing.from_toml(toml_text)
    assert len(pricing) == 0


# ---------------------------------------------------------------------------
# main(update-pricing) — end-to-end with monkey-patched fetch
# ---------------------------------------------------------------------------


def test_update_pricing_writes_target_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``snipz update-pricing --output PATH`` writes a parseable TOML there."""
    output = tmp_path / "pricing.toml"
    monkeypatch.setattr("snipz.cli._fetch_upstream", lambda _url: SAMPLE_LITELLM_JSON)

    exit_code = main(["update-pricing", "--output", str(output)])

    assert exit_code == 0
    assert output.exists()
    pricing = Pricing.from_toml(output.read_text(encoding="utf-8"))
    assert pricing.get("anthropic", "claude-3-5-sonnet-20241022") is not None


def test_update_pricing_atomic_write_leaves_no_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "pricing.toml"
    monkeypatch.setattr("snipz.cli._fetch_upstream", lambda _url: SAMPLE_LITELLM_JSON)

    main(["update-pricing", "--output", str(output)])

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"unexpected .tmp leftover: {tmp_files}"


def test_update_pricing_fetch_failure_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import URLError

    output = tmp_path / "pricing.toml"

    def _raise(_url: str) -> Any:
        raise URLError("simulated network failure")

    monkeypatch.setattr("snipz.cli._fetch_upstream", _raise)

    exit_code = main(["update-pricing", "--output", str(output)])

    assert exit_code == 1
    assert not output.exists()


def test_update_pricing_invalid_json_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "pricing.toml"

    def _bad_json(_url: str) -> Any:
        raise json.JSONDecodeError("simulated decode failure", "doc", 0)

    monkeypatch.setattr("snipz.cli._fetch_upstream", _bad_json)

    exit_code = main(["update-pricing", "--output", str(output)])

    assert exit_code == 1
    assert not output.exists()
