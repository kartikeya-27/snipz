"""Token estimators for pre-flight cost calculation.

Each estimator implements the :class:`Estimator` protocol and answers
"how many input tokens will this call use, and what's the most output
tokens it could produce?" — the inputs to
:meth:`snipz.Pricing.cost` for an ``estimated_cents`` value to pass to
:meth:`snipz.Budget.reserve`.

Three implementations ship:

* :class:`~snipz.estimators.fallback.FallbackEstimator` — pure Python,
  char-based. Works for every provider, but coarse.
* :class:`~snipz.estimators.anthropic.AnthropicEstimator` — char-based
  with a Claude-tuned ratio, plus an ``estimate_messages`` helper for
  Anthropic's chat shape. No SDK dependency.
* :class:`~snipz.estimators.openai.OpenAIEstimator` — ``tiktoken``-based
  exact counting. Requires the ``snipz[openai]`` extra.

All estimators MUST never under-estimate input tokens — conservative is
safe for a budget cap.
"""

from __future__ import annotations

from typing import Protocol

from snipz.estimators.anthropic import AnthropicEstimator
from snipz.estimators.fallback import FallbackEstimator
from snipz.estimators.openai import OpenAIEstimator

__all__ = [
    "AnthropicEstimator",
    "Estimator",
    "FallbackEstimator",
    "OpenAIEstimator",
]


class Estimator(Protocol):
    """Pre-flight token estimator.

    Implementations turn a piece of text plus a model name into the
    ``(input_tokens, max_output_tokens)`` pair that :meth:`Pricing.cost`
    expects.

    The contract is one-way conservative: ``input_tokens`` MUST be an
    upper bound on the real count. Callers will pay for the
    over-estimate by holding a slightly larger reservation than
    strictly necessary; that is far cheaper than under-estimating and
    overshooting the cap.
    """

    def estimate(
        self,
        text: str,
        *,
        model: str,
        max_output_tokens: int,
    ) -> tuple[int, int]:
        """Return ``(input_tokens_upper_bound, max_output_tokens)``.

        ``max_output_tokens`` is echoed back as the second element —
        the estimator never tries to predict actual output length;
        callers MUST cap output via the provider's ``max_tokens``
        parameter to keep the reservation honest.
        """
        ...
