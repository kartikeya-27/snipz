"""Budget-guarded OpenAI client (Responses API).

Wraps an ``openai.AsyncOpenAI`` so every ``responses.create`` call
reserves its worst-case cost before the request is sent and commits the
billed cost from ``response.usage`` afterwards::

    from openai import AsyncOpenAI
    from snipz import Budget, Scope
    from snipz.integrations.openai import guarded

    client = guarded(AsyncOpenAI(), budget)
    response = await client.responses.create(
        scope=Scope("user", "u_42"),
        model="gpt-5.6",
        max_output_tokens=1024,
        input="Hello",
    )

Streaming works the same way through ``responses.stream``::

    async with client.responses.stream(scope=scope, **params) as stream:
        async for event in stream:
            ...

The reservation is an upper bound:

* **Input** is counted exactly with ``responses.input_tokens.count``,
  which takes the same input, instructions, tools, and conversation as
  the request (one extra API call).
* **Output** is ``max_output_tokens`` (required; reasoning tokens count
  against it).
* **Caching** is automatic on OpenAI, so on models that charge for cache
  writes every prompt token is priced at the higher of the input and
  cache-write rates.

Settlement follows OpenAI's billing: uncached input is ``input_tokens``
less ``cached_tokens`` and ``cache_write_tokens``, each billed at its own
rate; models with no cache-write price bill written tokens as input. A
stream settles when its ``async with`` block exits: at the usage carried
by its final ``response.completed`` / ``incomplete`` / ``failed`` event,
or at the reservation if none arrived (OpenAI reports no partial usage).

Rejected before any request: ``background=True`` (nothing to settle when
the call returns), stored ``prompt`` templates (the token counter cannot
see them), ``service_tier="priority"`` (billed at rates not modelled),
and ``stream=True`` on ``create`` — use ``responses.stream``. Not
covered: per-call fees of built-in tools (web search, file search, code
interpreter) and flex / batch discounts (billed here at standard rates).

Requires the ``snipz[openai]`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Self

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
    import httpx2
    import openai as _openai
    from openai.types.responses import (
        ResponseCompletedEvent,
        ResponseFailedEvent,
        ResponseIncompleteEvent,
    )
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "snipz.integrations.openai requires the OpenAI SDK. "
        "Install with: pip install 'snipz[openai]'"
    ) from _exc

if TYPE_CHECKING:
    from types import TracebackType

    from openai import AsyncOpenAI
    from openai.lib.streaming.responses import AsyncResponseStream, AsyncResponseStreamManager
    from openai.types.responses import Response, ResponseStreamEvent, ResponseUsage

    from snipz.core import Budget, Reservation, Scope

__all__ = ["GuardedAsyncOpenAI", "GuardedResponseStream", "GuardedResponses", "guarded"]


# Parameters of ``AsyncInputTokens.count`` (openai 3.29). Forwarded from
# the create() call so the count sees exactly the prompt the request will
# send. A test pins this to the installed SDK's signature to catch drift.
_COUNT_PARAMS: Final = frozenset(
    {
        "conversation",
        "input",
        "instructions",
        "model",
        "parallel_tool_calls",
        "personality",
        "previous_response_id",
        "reasoning",
        "text",
        "tool_choice",
        "tools",
        "truncation",
        "extra_headers",
        "extra_query",
        "extra_body",
        "timeout",
    }
)

# Stream events that carry the final response, with its usage.
_FINAL_EVENTS: Final = (ResponseCompletedEvent, ResponseIncompleteEvent, ResponseFailedEvent)


def guarded(
    client: AsyncOpenAI,
    budget: Budget,
    *,
    pricing: Pricing | None = None,
    provider: str = "openai",
    ttl: int = 300,
) -> GuardedAsyncOpenAI:
    """Wrap ``client`` so its ``responses`` calls are budget-guarded.

    ``pricing`` defaults to :meth:`Pricing.default`. ``provider`` is the
    price-book provider key — override it when ``client`` points at a
    platform with its own price table. ``ttl`` is the reservation
    lifetime in seconds; set it above your longest request.
    """
    check_guarded_args(client.responses.create, client, ttl, expected="openai.AsyncOpenAI")
    return GuardedAsyncOpenAI(
        client,
        GuardedResponses(
            client,
            budget,
            pricing=pricing if pricing is not None else Pricing.default(),
            provider=provider,
            ttl=ttl,
        ),
    )


class GuardedAsyncOpenAI:
    """An ``AsyncOpenAI`` whose ``responses`` calls are budget-guarded.

    Only ``responses`` is wrapped. Reach the underlying client through
    :attr:`client` for anything else — those calls are not guarded.
    """

    __slots__ = ("client", "responses")

    def __init__(self, client: AsyncOpenAI, responses: GuardedResponses) -> None:
        self.client = client
        self.responses = responses


class GuardedResponses:
    """The guarded ``responses`` resource. Construct via :func:`guarded`."""

    __slots__ = ("_budget", "_client", "_pricing", "_provider", "_ttl")

    def __init__(
        self,
        client: AsyncOpenAI,
        budget: Budget,
        *,
        pricing: Pricing,
        provider: str,
        ttl: int,
    ) -> None:
        self._client = client
        self._budget = budget
        self._pricing = pricing
        self._provider = provider
        self._ttl = ttl

    async def create(
        self,
        *,
        scope: Scope | Sequence[Scope],
        request_id: str | None = None,
        **params: Any,
    ) -> Response:
        """``responses.create`` with a budget reservation around it.

        ``scope`` is the scope (or composite scopes) to charge.
        ``request_id`` makes retries idempotent: a repeat raises
        :class:`~snipz.integrations.DuplicateRequestError` instead of
        calling the API again. Every other keyword goes to
        ``responses.create`` unchanged.

        Raises :class:`~snipz.BudgetExceededError` before any request is
        sent when the reservation does not fit.
        """
        model = self._priced_model(params)
        estimated_cents = await self._estimate_cents(model, params)

        async def call() -> Response:
            response: Response = await self._client.responses.create(**params)
            return response

        return await run_guarded(
            budget=self._budget,
            scopes=scope,
            estimated_cents=estimated_cents,
            call=call,
            actual_cents=lambda response: self._response_cents(model, response, estimated_cents),
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
    ) -> GuardedResponseStreamManager:
        """``responses.stream`` with a budget reservation around it.

        Use as ``async with``; the block receives a
        :class:`GuardedResponseStream`, which passes the SDK's events
        through unchanged. The reservation is taken — and
        :class:`~snipz.BudgetExceededError` or
        :class:`~snipz.integrations.DuplicateRequestError` raised —
        before the request is sent, on entering the block; the cost is
        committed when it exits. ``scope`` and ``request_id`` work as in
        :meth:`create`.
        """
        return GuardedResponseStreamManager(self, scope=scope, request_id=request_id, params=params)

    # -- pricing --------------------------------------------------------------

    def _priced_model(self, params: Mapping[str, Any]) -> str:
        """Validate the request; return its model, which must be priced."""
        model = _validate_params(params)
        if self._pricing.get(self._provider, model) is None:
            raise UnknownPricingError(self._provider, model)
        return model

    async def _estimate_cents(self, model: str, params: Mapping[str, Any]) -> Decimal:
        counted = await self._client.responses.input_tokens.count(
            **{k: v for k, v in params.items() if k in _COUNT_PARAMS}
        )
        prompt_tokens = counted.input_tokens
        output_tokens: int = params["max_output_tokens"]
        candidates = [
            self._pricing.cost(
                provider=self._provider,
                model=model,
                input_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )
        ]
        # Caching is automatic: any prompt token may be written to the cache.
        with contextlib.suppress(ValueError):  # no cache-write rate on file
            candidates.append(
                self._pricing.cost(
                    provider=self._provider,
                    model=model,
                    input_tokens=0,
                    output_tokens=output_tokens,
                    cache_write_tokens=prompt_tokens,
                )
            )
        return max(candidates)

    def _response_cents(
        self, model: str, response: Response | None, estimated_cents: Decimal
    ) -> Decimal:
        """A finished response's billed cost; the reservation if it has no usage."""
        if response is None or response.usage is None:
            return estimated_cents
        return self._usage_cents(model, response.usage)

    def _usage_cents(self, model: str, usage: ResponseUsage) -> Decimal:
        entry = self._pricing.get(self._provider, model)
        details = usage.input_tokens_details
        cached = details.cached_tokens or 0
        written = details.cache_write_tokens or 0
        # Without a dedicated rate, OpenAI bills these at the input rate.
        if entry is not None and entry.cache_write_cents_per_m is None:
            written = 0
        if entry is not None and entry.cache_read_cents_per_m is None:
            cached = 0
        return self._pricing.cost(
            provider=self._provider,
            model=model,
            input_tokens=usage.input_tokens - cached - written,
            output_tokens=usage.output_tokens,
            cache_read_tokens=cached,
            cache_write_tokens=written,
        )


class GuardedResponseStream:
    """The SDK's ``AsyncResponseStream``, observed for settlement.

    Every event passes through unchanged; the response carried by the
    final ``response.completed`` / ``incomplete`` / ``failed`` event is
    kept as :attr:`final_response`. Other attributes delegate to the
    SDK stream.
    """

    __slots__ = ("_stream", "final_response")

    def __init__(self, stream: AsyncResponseStream[Any]) -> None:
        self._stream = stream
        self.final_response: Response | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ResponseStreamEvent:
        event = await self._stream.__anext__()
        if isinstance(event, _FINAL_EVENTS):
            self.final_response = event.response
        return event

    async def until_done(self) -> Self:
        """Consume the rest of the stream."""
        async for _ in self:
            pass
        return self

    async def get_final_response(self) -> Response:
        """Consume the rest of the stream; return the completed response.

        Raises ``RuntimeError`` like the SDK if the stream ended without
        ``response.completed``.
        """
        await self.until_done()
        response: Response = await self._stream.get_final_response()
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class GuardedResponseStreamManager:
    """Async context manager returned by :meth:`GuardedResponses.stream`.

    Entering reserves the worst-case cost and opens the stream; exiting
    closes it and commits what it cost (see the module docstring).
    Single use, like the SDK manager it wraps.
    """

    __slots__ = (
        "_estimated",
        "_manager",
        "_model",
        "_params",
        "_request_id",
        "_reservation",
        "_responses",
        "_scope",
        "_stream",
    )

    def __init__(
        self,
        responses: GuardedResponses,
        *,
        scope: Scope | Sequence[Scope],
        request_id: str | None,
        params: dict[str, Any],
    ) -> None:
        self._responses = responses
        self._scope = scope
        self._request_id = request_id
        self._params = params
        self._model = ""
        self._estimated = Decimal(0)
        self._reservation: Reservation | None = None
        self._manager: AsyncResponseStreamManager[Any] | None = None
        self._stream: GuardedResponseStream | None = None

    async def __aenter__(self) -> GuardedResponseStream:
        if self._reservation is not None:
            raise RuntimeError("a guarded response stream can only be entered once")
        responses = self._responses
        self._model = responses._priced_model(self._params)
        self._estimated = await responses._estimate_cents(self._model, self._params)
        reservation = await reserve(
            responses._budget,
            scopes=self._scope,
            estimated_cents=self._estimated,
            request_id=self._request_id,
            ttl=responses._ttl,
            model=self._model,
            provider=responses._provider,
        )
        self._reservation = reservation
        self._manager = responses._client.responses.stream(**self._params)
        try:
            self._stream = GuardedResponseStream(await self._manager.__aenter__())
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
            responses = self._responses
            final = stream.final_response

            def cost() -> Decimal:
                return responses._response_cents(self._model, final, self._estimated)

            if exc is None:
                await settle_success(
                    reservation, final, cost, provider=responses._provider, model=self._model
                )
            else:
                await settle_quietly(
                    reservation, cost, provider=responses._provider, model=self._model
                )


# ---------------------------------------------------------------------------
# Request inspection
# ---------------------------------------------------------------------------


def _validate_params(params: Mapping[str, Any]) -> str:
    """Check the parameters the reservation depends on; return the model."""
    if "stream" in params:
        raise ValueError("pass streaming requests to responses.stream(), not stream=True")
    if params.get("background"):
        raise ValueError("background=True returns before the work is done; nothing to settle")
    if "prompt" in params:
        raise ValueError("stored prompt templates cannot be token-counted before the request")
    if "response_id" in params:
        raise ValueError("resuming a stream by response_id is not guarded")
    if params.get("service_tier") == "priority":
        raise ValueError("service_tier='priority' is billed at rates snipz does not model")
    model = params.get("model")
    if not isinstance(model, str):
        raise TypeError("responses.create() requires model=<str>")
    max_output = params.get("max_output_tokens")
    if not isinstance(max_output, int) or isinstance(max_output, bool) or max_output <= 0:
        raise TypeError(
            "responses.create() requires max_output_tokens=<positive int> to bound the cost"
        )
    return model


def _is_ambiguous_failure(exc: BaseException) -> bool:
    """Whether a failed call may still have been billed.

    An HTTP error status means OpenAI answered and rejected the request.
    A timeout, dropped connection, or cancellation leaves the outcome
    unknown.
    """
    if isinstance(exc, _openai.APIStatusError):
        return False
    return isinstance(
        exc,
        (
            _openai.APIConnectionError,
            httpx2.TransportError,
            asyncio.CancelledError,
            TimeoutError,
        ),
    )
