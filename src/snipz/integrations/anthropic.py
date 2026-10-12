"""Budget-guarded Anthropic client.

Wraps an ``anthropic.AsyncAnthropic`` so every ``messages.create`` call
reserves its worst-case cost before the request is sent and commits the
billed cost from ``response.usage`` afterwards::

    from anthropic import AsyncAnthropic
    from snipz import Budget, Scope
    from snipz.integrations.anthropic import guarded

    client = guarded(AsyncAnthropic(), budget)
    message = await client.messages.create(
        scope=Scope("user", "u_42"),
        model="claude-opus-5-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": "Hello"}],
    )

Streaming works the same way through ``messages.stream``, the SDK's
streaming helper::

    async with client.messages.stream(scope=scope, **params) as stream:
        async for text in stream.text_stream:
            ...

The reservation is an upper bound:

* **Input** is counted exactly with ``messages.count_tokens`` (one extra
  API call). ``input_tokens="estimate"`` uses a local character-based
  estimate for text-only requests instead — faster, but a heuristic:
  Claude's tokenizer varies by model, so it is not a guaranteed bound.
* **Output** is ``max_tokens`` (thinking tokens are billed as output and
  count against it).
* **Caching**: when the request carries ``cache_control``, every prompt
  token is priced as a cache write (the 1-hour rate if any breakpoint
  asks for ``ttl="1h"``), the most a prompt token can cost.

A stream settles when its ``async with`` block exits: at the billed
usage if it ran to completion; at the exact prompt usage plus
``max_tokens`` of output if it stopped early or failed after the first
event (generation may have continued until the connection closed); at
the reservation if no event arrived.

Not covered: server-tool fees billed per use (e.g. web search), batch
and priority service-tier rates, and the beta ``client.beta.messages``
surface. ``messages.create(stream=True)`` is rejected — use
``messages.stream``.

Requires the ``snipz[anthropic]`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal

from snipz.estimators.anthropic import AnthropicEstimator
from snipz.integrations._guard import (
    check_guarded_args,
    reserve,
    run_guarded,
    settle_failure,
    settle_quietly,
    settle_success,
)
from snipz.pricing import Pricing, UnknownPricingError

try:
    import anthropic as _anthropic
    import httpx2
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "snipz.integrations.anthropic requires the Anthropic SDK. "
        "Install with: pip install 'snipz[anthropic]'"
    ) from _exc

if TYPE_CHECKING:
    from types import TracebackType

    from anthropic import AsyncAnthropic
    from anthropic.lib.streaming import AsyncMessageStream, AsyncMessageStreamManager
    from anthropic.types import Message, Usage

    from snipz.core import Budget, Reservation, Scope

__all__ = ["GuardedAsyncAnthropic", "GuardedMessageStream", "GuardedMessages", "guarded"]


InputTokenMode = Literal["count", "estimate"]

# Parameters of ``AsyncMessages.count_tokens`` (anthropic 1.13). Forwarded
# from the create() call so the count sees exactly what the request will
# send — tools, system prompt, structured-output schema included. A test
# pins this to the installed SDK's signature to catch drift.
_COUNT_TOKENS_PARAMS: Final = frozenset(
    {
        "messages",
        "model",
        "cache_control",
        "output_config",
        "output_format",
        "system",
        "thinking",
        "tool_choice",
        "tools",
        "user_profile_id",
        "workspace_id",
        "extra_headers",
        "extra_query",
        "extra_body",
        "timeout",
    }
)

# Request parameters that add prompt tokens the local estimator cannot
# see. Any of them forces an exact count in "estimate" mode.
_NON_TEXT_PARAMS: Final = frozenset(
    {"tools", "tool_choice", "output_format", "container", "mcp_servers"}
)


def guarded(
    client: AsyncAnthropic,
    budget: Budget,
    *,
    pricing: Pricing | None = None,
    provider: str = "anthropic",
    ttl: int = 300,
    input_tokens: InputTokenMode = "count",
) -> GuardedAsyncAnthropic:
    """Wrap ``client`` so its ``messages.create`` calls are budget-guarded.

    ``pricing`` defaults to :meth:`Pricing.default`. ``provider`` is the
    price-book provider key — override it when ``client`` reaches Claude
    through a platform with its own price table. ``ttl`` is the
    reservation lifetime in seconds; set it above your longest request.
    """
    check_guarded_args(
        client.messages.create, client, ttl, expected="anthropic.AsyncAnthropic"
    )
    if input_tokens not in ("count", "estimate"):
        raise ValueError(f"input_tokens must be 'count' or 'estimate', got {input_tokens!r}")
    return GuardedAsyncAnthropic(
        client,
        GuardedMessages(
            client,
            budget,
            pricing=pricing if pricing is not None else Pricing.default(),
            provider=provider,
            ttl=ttl,
            input_tokens=input_tokens,
        ),
    )


class GuardedAsyncAnthropic:
    """An ``AsyncAnthropic`` whose ``messages.create`` is budget-guarded.

    Only ``messages`` is wrapped. Reach the underlying client through
    :attr:`client` for anything else — those calls are not guarded.
    """

    __slots__ = ("client", "messages")

    def __init__(self, client: AsyncAnthropic, messages: GuardedMessages) -> None:
        self.client = client
        self.messages = messages


class GuardedMessages:
    """The guarded ``messages`` resource. Construct via :func:`guarded`."""

    __slots__ = ("_budget", "_client", "_input_tokens", "_pricing", "_provider", "_ttl")

    def __init__(
        self,
        client: AsyncAnthropic,
        budget: Budget,
        *,
        pricing: Pricing,
        provider: str,
        ttl: int,
        input_tokens: InputTokenMode,
    ) -> None:
        self._client = client
        self._budget = budget
        self._pricing = pricing
        self._provider = provider
        self._ttl = ttl
        self._input_tokens = input_tokens

    async def create(
        self,
        *,
        scope: Scope | Sequence[Scope],
        request_id: str | None = None,
        **params: Any,
    ) -> Message:
        """``messages.create`` with a budget reservation around it.

        ``scope`` is the scope (or composite scopes) to charge.
        ``request_id`` makes retries idempotent: a repeat raises
        :class:`~snipz.integrations.DuplicateRequestError` instead of
        calling the API again. Every other keyword goes to
        ``messages.create`` unchanged.

        Raises :class:`~snipz.BudgetExceededError` before any request is
        sent when the reservation does not fit.
        """
        model = self._priced_model(params)
        estimated_cents = await self._estimate_cents(model, params)

        async def call() -> Message:
            message: Message = await self._client.messages.create(**params)
            return message

        return await run_guarded(
            budget=self._budget,
            scopes=scope,
            estimated_cents=estimated_cents,
            call=call,
            actual_cents=lambda message: self._usage_cents(model, message.usage),
            is_ambiguous_failure=_is_ambiguous_failure,
            request_id=request_id,
            ttl=self._ttl,
            model=model,
            provider=self._provider,
        )

    def stream(
        self,
        *,
        scope: Scope | Sequence[Scope],
        request_id: str | None = None,
        **params: Any,
    ) -> GuardedMessageStream:
        """``messages.stream`` with a budget reservation around it.

        Use as ``async with``; the block receives the SDK's
        ``AsyncMessageStream`` unchanged. The reservation is taken — and
        :class:`~snipz.BudgetExceededError` or
        :class:`~snipz.integrations.DuplicateRequestError` raised —
        before the request is sent, on entering the block; the cost is
        committed when it exits. ``scope`` and ``request_id`` work as in
        :meth:`create`.
        """
        return GuardedMessageStream(self, scope=scope, request_id=request_id, params=params)

    # -- pricing --------------------------------------------------------------

    def _priced_model(self, params: Mapping[str, Any]) -> str:
        """Validate the request; return its model, which must be priced."""
        model = _validate_params(params)
        if self._pricing.get(self._provider, model) is None:
            raise UnknownPricingError(self._provider, model)
        return model

    async def _estimate_cents(self, model: str, params: Mapping[str, Any]) -> Decimal:
        prompt_tokens = await self._prompt_tokens(model, params)
        output_tokens: int = params["max_tokens"]
        candidates = [
            self._pricing.cost(
                provider=self._provider,
                model=model,
                input_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )
        ]
        cache_ttl = _cache_ttl(params)
        if cache_ttl is not None:
            written = (
                {"cache_write_1h_tokens": prompt_tokens}
                if cache_ttl == "1h"
                else {"cache_write_tokens": prompt_tokens}
            )
            # Without a cache-write rate on file, the input rate is the bound.
            with contextlib.suppress(ValueError):
                candidates.append(
                    self._pricing.cost(
                        provider=self._provider,
                        model=model,
                        input_tokens=0,
                        output_tokens=output_tokens,
                        **written,
                    )
                )
        return max(candidates)

    async def _prompt_tokens(self, model: str, params: Mapping[str, Any]) -> int:
        if self._input_tokens == "estimate" and _is_text_only(params):
            tokens, _ = AnthropicEstimator().estimate_messages(
                [_as_dict(m) for m in params["messages"]],
                system=_system_text(params.get("system")),
                model=model,
                max_output_tokens=0,
            )
            return tokens
        counted = await self._client.messages.count_tokens(
            **{k: v for k, v in params.items() if k in _COUNT_TOKENS_PARAMS}
        )
        return counted.input_tokens

    def _stream_cents(
        self,
        model: str,
        snapshot: Message | None,
        params: Mapping[str, Any],
        reservation: Reservation,
    ) -> Decimal:
        """What a stream cost, given how far it got.

        Completed (``stop_reason`` set by the final ``message_delta``): the
        billed usage. Stopped after ``message_start``: the exact prompt
        usage, with output at ``max_tokens`` — the model may have kept
        generating until the connection closed. No events: the reservation.
        """
        if snapshot is None:
            return reservation.estimated_cents
        usage = snapshot.usage
        if snapshot.stop_reason is None:
            usage = usage.model_copy(update={"output_tokens": params["max_tokens"]})
        return self._usage_cents(model, usage)

    def _usage_cents(self, model: str, usage: Usage) -> Decimal:
        # ``cache_creation_input_tokens`` is the total written; the
        # ``cache_creation`` breakdown, when present, splits out 1-hour writes.
        written = usage.cache_creation_input_tokens or 0
        written_1h = 0
        breakdown = usage.cache_creation
        if breakdown is not None:
            written_1h = breakdown.ephemeral_1h_input_tokens or 0
            written = max(written, written_1h + (breakdown.ephemeral_5m_input_tokens or 0))
        return self._pricing.cost(
            provider=self._provider,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=written - written_1h,
            cache_write_1h_tokens=written_1h,
        )


class GuardedMessageStream:
    """Async context manager returned by :meth:`GuardedMessages.stream`.

    Entering reserves the worst-case cost and opens the stream; exiting
    closes it and commits what it cost (see the module docstring).
    Single use, like the SDK manager it wraps.
    """

    __slots__ = (
        "_manager",
        "_messages",
        "_model",
        "_params",
        "_request_id",
        "_reservation",
        "_scope",
        "_stream",
    )

    def __init__(
        self,
        messages: GuardedMessages,
        *,
        scope: Scope | Sequence[Scope],
        request_id: str | None,
        params: dict[str, Any],
    ) -> None:
        self._messages = messages
        self._scope = scope
        self._request_id = request_id
        self._params = params
        self._model = ""
        self._reservation: Reservation | None = None
        self._manager: AsyncMessageStreamManager[Any] | None = None
        self._stream: AsyncMessageStream[Any] | None = None

    async def __aenter__(self) -> AsyncMessageStream[Any]:
        if self._reservation is not None:
            raise RuntimeError("a guarded message stream can only be entered once")
        messages = self._messages
        self._model = messages._priced_model(self._params)
        estimated_cents = await messages._estimate_cents(self._model, self._params)
        reservation = await reserve(
            messages._budget,
            scopes=self._scope,
            estimated_cents=estimated_cents,
            request_id=self._request_id,
            ttl=messages._ttl,
            model=self._model,
            provider=messages._provider,
        )
        self._reservation = reservation
        self._manager = messages._client.messages.stream(**self._params)
        try:
            self._stream = await self._manager.__aenter__()
        except BaseException as exc:
            await settle_failure(reservation, exc, is_ambiguous_failure=_is_ambiguous_failure)
            raise
        return self._stream

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        reservation, manager, stream = self._reservation, self._manager, self._stream
        if reservation is None or manager is None or stream is None:
            return  # __aenter__ raised and already settled
        try:
            await manager.__aexit__(exc_type, exc, tb)
        finally:
            snapshot = _snapshot(stream)
            messages = self._messages

            def cost() -> Decimal:
                return messages._stream_cents(self._model, snapshot, self._params, reservation)

            if exc is None:
                await settle_success(
                    reservation, snapshot, cost, provider=messages._provider, model=self._model
                )
            else:
                await settle_quietly(
                    reservation, cost, provider=messages._provider, model=self._model
                )


def _snapshot(stream: AsyncMessageStream[Any]) -> Message | None:
    """The stream's accumulated message, or ``None`` before ``message_start``.

    The SDK guards ``current_message_snapshot`` with an ``assert``: it
    raises ``AssertionError`` before the first event, or returns ``None``
    under ``python -O``. A test pins this to the installed SDK.
    """
    try:
        return stream.current_message_snapshot
    except AssertionError:
        return None


# ---------------------------------------------------------------------------
# Request inspection
# ---------------------------------------------------------------------------


def _validate_params(params: Mapping[str, Any]) -> str:
    """Check the parameters the reservation depends on; return the model."""
    if "stream" in params:
        raise ValueError("pass streaming requests to messages.stream(), not stream=True")
    model = params.get("model")
    if not isinstance(model, str):
        raise TypeError("messages.create() requires model=<str>")
    max_tokens = params.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise TypeError("messages.create() requires max_tokens=<positive int> to bound the cost")
    if "messages" not in params:
        raise TypeError("messages.create() requires messages=[...]")
    return model


def _is_ambiguous_failure(exc: BaseException) -> bool:
    """Whether a failed call may still have been billed.

    An HTTP error status means Anthropic answered and rejected the
    request. A timeout, dropped connection, or cancellation leaves the
    outcome unknown — including the raw ``httpx2`` transport errors the
    SDK lets escape from a stream's body.
    """
    if isinstance(exc, _anthropic.APIStatusError):
        return False
    return isinstance(
        exc,
        (
            _anthropic.APIConnectionError,
            httpx2.TransportError,
            asyncio.CancelledError,
            TimeoutError,
        ),
    )


def _cache_ttl(params: Mapping[str, Any]) -> Literal["5m", "1h"] | None:
    """The longest cache TTL any ``cache_control`` in the request asks for."""
    found: Literal["5m", "1h"] | None = None
    stack: list[Any] = [params]
    while stack:
        node = _as_dict(stack.pop())
        if isinstance(node, Mapping):
            control = node.get("cache_control")
            if isinstance(control, Mapping):
                if control.get("ttl") == "1h":
                    return "1h"
                found = "5m"
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return found


def _is_text_only(params: Mapping[str, Any]) -> bool:
    """Whether every prompt token is in text the local estimator can see."""
    if _NON_TEXT_PARAMS & params.keys():
        return False
    output_config = params.get("output_config")
    if isinstance(output_config, Mapping) and "format" in output_config:
        return False
    system = params.get("system")
    if system is not None and not isinstance(system, str) and not _all_text_blocks(system):
        return False
    for message in params["messages"]:
        content = _as_dict(message).get("content")
        if not isinstance(content, str) and not _all_text_blocks(content):
            return False
    return True


def _all_text_blocks(blocks: object) -> bool:
    if not isinstance(blocks, (list, tuple)):
        return False
    return all(_as_dict(block).get("type") == "text" for block in blocks)


def _system_text(system: str | Sequence[Any] | None) -> str | None:
    """Flatten a system prompt given as text blocks (checked by ``_is_text_only``)."""
    if system is None or isinstance(system, str):
        return system
    return "\n".join(str(_as_dict(block).get("text", "")) for block in system)


def _as_dict(value: Any) -> Any:
    """SDK response objects (pydantic models) reused in a request → dicts."""
    dump = getattr(value, "model_dump", None)
    return dump() if callable(dump) else value
