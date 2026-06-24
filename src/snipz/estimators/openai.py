"""Exact token estimator for OpenAI models via ``tiktoken``.

Requires the ``snipz[openai]`` extra. Constructing
:class:`OpenAIEstimator` without ``tiktoken`` installed raises a clear
:class:`ImportError`.

Uses ``tiktoken.encoding_for_model(model)`` for the right BPE encoding;
falls back to ``cl100k_base`` for models tiktoken does not recognize
(e.g., very new releases not yet in the ``tiktoken`` registry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover
    import tiktoken

__all__ = ["OpenAIEstimator"]


_IMPORT_HINT: Final = (
    "tiktoken is required for OpenAIEstimator. "
    "Install with: pip install 'snipz[openai]'"
)


# OpenAI chat models add a few tokens of overhead per message for role
# markers — the exact recipe varies by model. We use a generous value
# so the estimate stays an upper bound.
_PER_MESSAGE_OVERHEAD: Final = 4


# Fallback BPE when tiktoken does not recognize the model name.
_FALLBACK_ENCODING: Final = "cl100k_base"


class OpenAIEstimator:
    """Exact token estimator for OpenAI models using ``tiktoken``.

    Constructed once and reused — the underlying ``tiktoken.Encoding``
    object is cached on the instance.
    """

    __slots__ = ("_tiktoken",)

    def __init__(self) -> None:
        try:
            import tiktoken
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_IMPORT_HINT) from exc
        self._tiktoken = tiktoken

    def estimate(
        self,
        text: str,
        *,
        model: str,
        max_output_tokens: int,
    ) -> tuple[int, int]:
        encoding = self._encoding_for(model)
        return len(encoding.encode(text)), max_output_tokens

    def estimate_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_output_tokens: int,
    ) -> tuple[int, int]:
        """Estimate tokens for an OpenAI chat-shaped request.

        ``messages`` is the list passed to ``client.chat.completions.create``;
        each entry is a ``{"role": ..., "content": ...}`` dict where
        ``content`` is either a string or a list of content parts.
        Non-text content parts (images, audio) are ignored — count
        those yourself if you mix them in.
        """
        encoding = self._encoding_for(model)
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += len(encoding.encode(content))
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str):
                            total += len(encoding.encode(text))
            total += _PER_MESSAGE_OVERHEAD
        return total, max_output_tokens

    def _encoding_for(self, model: str) -> tiktoken.Encoding:
        try:
            return self._tiktoken.encoding_for_model(model)
        except KeyError:
            return self._tiktoken.get_encoding(_FALLBACK_ENCODING)
