"""Tests for the Phase 4 token estimators.

Covers the contract (``estimate`` returns ``(input, max_output)``),
the conservative-upper-bound property for each provider, the
``estimate_messages`` chat helpers, and tiktoken-related fall-back
behavior on unknown model names.
"""

from __future__ import annotations

import pytest

from snipz import Estimator
from snipz.estimators import (
    AnthropicEstimator,
    FallbackEstimator,
    OpenAIEstimator,
)

# ---------------------------------------------------------------------------
# Protocol conformance — all three classes structurally satisfy Estimator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "estimator",
    [FallbackEstimator(), AnthropicEstimator(), OpenAIEstimator()],
    ids=["fallback", "anthropic", "openai"],
)
def test_estimator_returns_tuple_of_two_ints(estimator: Estimator) -> None:
    result = estimator.estimate("hello world", model="gpt-4o", max_output_tokens=128)
    assert isinstance(result, tuple)
    assert len(result) == 2
    input_tokens, max_out = result
    assert isinstance(input_tokens, int)
    assert input_tokens >= 0
    assert max_out == 128


@pytest.mark.parametrize(
    "estimator",
    [FallbackEstimator(), AnthropicEstimator(), OpenAIEstimator()],
    ids=["fallback", "anthropic", "openai"],
)
def test_estimator_empty_text_returns_zero_input_tokens(estimator: Estimator) -> None:
    input_tokens, _ = estimator.estimate("", model="gpt-4o", max_output_tokens=10)
    assert input_tokens == 0


@pytest.mark.parametrize(
    "estimator",
    [FallbackEstimator(), AnthropicEstimator(), OpenAIEstimator()],
    ids=["fallback", "anthropic", "openai"],
)
def test_estimator_echoes_max_output_tokens(estimator: Estimator) -> None:
    _, max_out = estimator.estimate("anything", model="gpt-4o", max_output_tokens=999)
    assert max_out == 999


# ---------------------------------------------------------------------------
# FallbackEstimator
# ---------------------------------------------------------------------------


def test_fallback_ceil_division() -> None:
    e = FallbackEstimator()
    # 1 char → 1 token (ceil division)
    assert e.estimate("a", model="x", max_output_tokens=0) == (1, 0)
    assert e.estimate("ab", model="x", max_output_tokens=0) == (1, 0)
    assert e.estimate("abc", model="x", max_output_tokens=0) == (1, 0)
    assert e.estimate("abcd", model="x", max_output_tokens=0) == (1, 0)
    assert e.estimate("abcde", model="x", max_output_tokens=0) == (2, 0)


def test_fallback_ignores_model() -> None:
    """All models share the same heuristic — never under-/over-counts by model."""
    e = FallbackEstimator()
    a = e.estimate("hello world", model="gpt-4o", max_output_tokens=0)
    b = e.estimate("hello world", model="claude-3-5-sonnet-20241022", max_output_tokens=0)
    assert a == b


# ---------------------------------------------------------------------------
# AnthropicEstimator
# ---------------------------------------------------------------------------


def test_anthropic_estimate_is_upper_bound_vs_fallback() -> None:
    """Claude estimator must be at least as conservative as the fallback.

    Claude's tokenizer averages ~3.5 chars/token (fewer chars per
    token = more tokens), so the Anthropic estimator should always
    produce >= tokens than the fallback's 4-chars-per-token rule.
    """
    text = "The quick brown fox jumps over the lazy dog. " * 10
    ant = AnthropicEstimator()
    fb = FallbackEstimator()
    ant_tokens, _ = ant.estimate(text, model="claude-3-5-sonnet-20241022", max_output_tokens=0)
    fb_tokens, _ = fb.estimate(text, model="claude-3-5-sonnet-20241022", max_output_tokens=0)
    assert ant_tokens >= fb_tokens


def test_anthropic_estimate_messages_handles_string_content() -> None:
    e = AnthropicEstimator()
    messages = [
        {"role": "user", "content": "Hello, Claude."},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "Tell me a joke."},
    ]
    input_tokens, max_out = e.estimate_messages(
        messages,
        model="claude-3-5-sonnet-20241022",
        max_output_tokens=256,
    )
    assert input_tokens > 0
    assert max_out == 256


def test_anthropic_estimate_messages_handles_content_blocks() -> None:
    """``content`` may be a list of blocks; we only count the text blocks."""
    e = AnthropicEstimator()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at this image:"},
                {"type": "image", "source": {"type": "base64", "data": "ignored"}},
            ],
        },
    ]
    input_tokens, _ = e.estimate_messages(
        messages,
        model="claude-3-5-sonnet-20241022",
        max_output_tokens=0,
    )
    # The image block contributes 0 to the char count — we only
    # counted "Look at this image:" plus per-message overhead.
    assert input_tokens > 0
    # And it must not crash on the image block.


def test_anthropic_estimate_messages_includes_system_prompt() -> None:
    """A larger system prompt must increase the estimate."""
    e = AnthropicEstimator()
    without_system, _ = e.estimate_messages(
        [{"role": "user", "content": "Hi"}],
        system=None,
        model="claude-3-5-sonnet-20241022",
        max_output_tokens=0,
    )
    with_system, _ = e.estimate_messages(
        [{"role": "user", "content": "Hi"}],
        system="You are a meticulous helpful assistant.",
        model="claude-3-5-sonnet-20241022",
        max_output_tokens=0,
    )
    assert with_system > without_system


def test_anthropic_estimate_messages_per_message_overhead_scales() -> None:
    """Splitting one block into many adds overhead per message."""
    e = AnthropicEstimator()
    text = "x" * 100
    one_msg, _ = e.estimate_messages(
        [{"role": "user", "content": text}],
        model="claude-3-5-sonnet-20241022",
        max_output_tokens=0,
    )
    ten_msgs, _ = e.estimate_messages(
        [{"role": "user", "content": text[i*10:(i+1)*10]} for i in range(10)],
        model="claude-3-5-sonnet-20241022",
        max_output_tokens=0,
    )
    assert ten_msgs > one_msg


# ---------------------------------------------------------------------------
# OpenAIEstimator
# ---------------------------------------------------------------------------


def test_openai_uses_exact_tiktoken_count() -> None:
    """Should match tiktoken directly for a known model."""
    import tiktoken

    e = OpenAIEstimator()
    text = "The quick brown fox jumps over the lazy dog."
    enc = tiktoken.encoding_for_model("gpt-4o")
    expected = len(enc.encode(text))

    actual, _ = e.estimate(text, model="gpt-4o", max_output_tokens=0)
    assert actual == expected


def test_openai_falls_back_to_cl100k_for_unknown_model() -> None:
    """Models not registered in tiktoken should still estimate without raising."""
    import tiktoken

    e = OpenAIEstimator()
    text = "Hello world"
    cl100k = tiktoken.get_encoding("cl100k_base")
    expected = len(cl100k.encode(text))

    actual, _ = e.estimate(text, model="not-a-real-future-model", max_output_tokens=0)
    assert actual == expected


def test_openai_estimate_messages_counts_each_message() -> None:
    e = OpenAIEstimator()
    messages = [
        {"role": "user", "content": "Hi."},
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "How are you?"},
    ]
    input_tokens, max_out = e.estimate_messages(
        messages,
        model="gpt-4o",
        max_output_tokens=128,
    )
    assert input_tokens > 0
    assert max_out == 128


def test_openai_estimate_messages_handles_content_parts() -> None:
    """OpenAI also has list-of-parts content (text + image_url)."""
    e = OpenAIEstimator()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            ],
        }
    ]
    # Must not raise on the image_url part; counts only the text.
    input_tokens, _ = e.estimate_messages(
        messages,
        model="gpt-4o",
        max_output_tokens=0,
    )
    assert input_tokens > 0
