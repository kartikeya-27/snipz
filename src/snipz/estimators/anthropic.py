"""Char-based estimator tuned for Anthropic's Claude family.

No SDK dependency. Uses a Claude-tuned chars-per-token ratio plus a
small fixed safety margin so the resulting count is a true upper bound
across English, code, and mixed content.

For exact counts, call Anthropic's ``client.messages.count_tokens()``
yourself and pass the result directly to :meth:`Pricing.cost`. This
estimator is for the common pre-flight path where rough is good enough.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["AnthropicEstimator"]


# Claude's tokenizer averages ~3.5 chars per English token. We use the
# 7/2 = 3.5 ratio expressed as integer arithmetic to avoid float drift.
_DOUBLED_TEXT_LENGTH_DIVISOR: Final = 7


# Per-message role-marker overhead Claude adds when serializing a chat
# turn. Slightly generous so we over-estimate rather than under.
_PER_MESSAGE_OVERHEAD: Final = 4


# Overall safety margin applied to the final token count — a small
# buffer for tokenizer disagreement around punctuation and Unicode.
_SAFETY_MARGIN_PCT: Final = 5


class AnthropicEstimator:
    """Pre-flight token estimator for Anthropic Claude models.

    Char-based with a Claude-tuned ratio and a small safety margin.
    Per-message overhead is added by :meth:`estimate_messages`; the
    plain :meth:`estimate` treats input as one block.

    Always returns an upper bound — never under-estimates.
    """

    __slots__ = ()

    def estimate(
        self,
        text: str,
        *,
        model: str,
        max_output_tokens: int,
    ) -> tuple[int, int]:
        del model  # all Claude models share the same tokenizer
        input_tokens = _tokens_from_chars(len(text))
        return _with_margin(input_tokens), max_output_tokens

    def estimate_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        model: str,
        max_output_tokens: int,
    ) -> tuple[int, int]:
        """Estimate tokens for an Anthropic chat-shaped request.

        ``messages`` is the list passed to ``client.messages.create``;
        each entry is a ``{"role": ..., "content": ...}`` dict where
        ``content`` is either a string or a list of content blocks.
        Non-text content blocks (images, tool results) are ignored;
        if you mix them in, count those tokens yourself and add to
        the returned value.

        ``system`` is the optional system prompt — passed at the top
        level by Anthropic's API.
        """
        del model

        total_chars = 0
        if system:
            total_chars += len(system)
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str):
                            total_chars += len(text)

        input_tokens = _tokens_from_chars(total_chars)
        input_tokens += _PER_MESSAGE_OVERHEAD * len(messages)
        return _with_margin(input_tokens), max_output_tokens


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tokens_from_chars(char_count: int) -> int:
    """Convert char count to a conservative token count.

    Uses ``ceil(2 * chars / 7)`` so empty text -> 0 tokens, 1-3 chars
    -> 1 token, etc. The 2/7 factor is the integer-arithmetic form of
    1/3.5, the chars-per-token ratio Claude's tokenizer averages on
    English prose.
    """
    doubled = char_count * 2
    return (doubled + _DOUBLED_TEXT_LENGTH_DIVISOR - 1) // _DOUBLED_TEXT_LENGTH_DIVISOR


def _with_margin(tokens: int) -> int:
    """Apply the safety margin and round up."""
    if tokens == 0:
        return 0
    return tokens + (tokens * _SAFETY_MARGIN_PCT + 99) // 100
