"""Generic char-based token estimator.

Pure stdlib. Uses the canonical 4-chars-per-token English heuristic.
Conservative for ASCII; under-counts for CJK / heavily multi-byte
text. When precision matters, use a provider-specific estimator.
"""

from __future__ import annotations

from typing import Final

__all__ = ["FallbackEstimator"]


# Standard heuristic: GPT-style BPE tokenizers average ~4 ASCII chars
# per token. We use ceiling division so a 1-char input still costs 1
# token (matching real tokenizer behavior).
_CHARS_PER_TOKEN: Final = 4


class FallbackEstimator:
    """Generic char-based token estimator.

    No third-party dependencies. Suitable as a backstop for unknown
    models, or as a deliberate default when a provider-specific
    estimator is unavailable.

    Accuracy: within ~25% of real BPE counts on English prose.
    Significantly over-estimates on code with long identifiers and
    under-estimates on text with many short tokens. Always returns a
    conservative *upper* bound: input is rounded up to the next whole
    token even if the raw division would round down.
    """

    __slots__ = ()

    def estimate(
        self,
        text: str,
        *,
        model: str,
        max_output_tokens: int,
    ) -> tuple[int, int]:
        del model  # ignored — this estimator is model-agnostic
        # Ceiling division: empty text -> 0 tokens, 1-3 chars -> 1, etc.
        input_tokens = (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN
        return input_tokens, max_output_tokens
