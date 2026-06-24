"""``@budget.guard`` decorator for wrapping async LLM calls.

Replaces this boilerplate::

    async def call_llm(user_id, prompt):
        scope = Scope("user", user_id)
        estimated = pricing.cost(...)
        async with await budget.reserve(scope, estimated) as r:
            response = await anthropic.messages.create(...)
            r.observe(pricing.cost(..., response.usage.input_tokens, ...))
            return response

with this::

    @budget.guard(scope=..., estimate=..., actual=...)
    async def call_llm(user_id, prompt):
        return await anthropic.messages.create(...)

The decorator factory lives here so :mod:`snipz.core` stays focused on
the reservation engine; :meth:`Budget.guard` delegates to :func:`make_guard`.

Scope, estimate, actual, request_id, model, and provider can each be
either a static value or a callable receiving the wrapped function's
``*args, **kwargs``. ``actual`` additionally receives the wrapped
function's return value as its first positional argument
(``actual(response, *args, **kwargs)``). Callables may return either
a value or an awaitable; the decorator handles both.

Sync wrapping (``@budget.guard`` on ``def`` instead of ``async def``)
is not supported here. Use :mod:`snipz.sync` if you need a sync API.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

if TYPE_CHECKING:
    from snipz.core import Budget

__all__ = ["make_guard"]


# Type variables for the wrapped function.
P = ParamSpec("P")
R = TypeVar("R")


# Specs for each parameter — either the value itself or a callable that
# computes it from the wrapped function's args. Typed loosely as object
# because Python's type system can't express "Scope or list[Scope] or
# callable returning either" without forcing every caller to spell out
# `cast(...)` at the decorator site.
_Spec = object


def make_guard(
    budget: Budget,
    *,
    scope: _Spec,
    estimate: _Spec,
    actual: Callable[..., Decimal | Awaitable[Decimal]] | None = None,
    request_id: _Spec = None,
    ttl: int = 300,
    model: _Spec = None,
    provider: _Spec = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Return a decorator that wraps an async function with budget enforcement.

    Construct via :meth:`Budget.guard` — direct calls to ``make_guard``
    are an implementation detail.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            resolved_scope = _resolve_scope(scope, args, kwargs)
            estimated_cents = await _resolve_value(estimate, args, kwargs)
            if not isinstance(estimated_cents, Decimal):
                raise TypeError(
                    f"estimate must resolve to Decimal, got {type(estimated_cents).__name__}"
                )

            resolved_request_id = await _resolve_value(request_id, args, kwargs)
            resolved_model = await _resolve_value(model, args, kwargs)
            resolved_provider = await _resolve_value(provider, args, kwargs)

            reservation = await budget.reserve(
                resolved_scope,
                estimated_cents,
                request_id=_cast_optional_str(resolved_request_id, "request_id"),
                ttl=ttl,
                model=_cast_optional_str(resolved_model, "model"),
                provider=_cast_optional_str(resolved_provider, "provider"),
            )

            async with reservation:
                response = await func(*args, **kwargs)
                if actual is not None:
                    actual_cents = await _resolve_actual(
                        actual, response, args, kwargs
                    )
                    if not isinstance(actual_cents, Decimal):
                        raise TypeError(
                            f"actual must resolve to Decimal, "
                            f"got {type(actual_cents).__name__}"
                        )
                    await reservation.observe(actual_cents)
                return response

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _resolve_scope(
    spec: _Spec,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Resolve a scope spec to ``Scope`` or ``Sequence[Scope]``.

    Lists/tuples are passed through as composite scopes; callables are
    invoked with the wrapped function's args. Awaitable callables are
    not supported here — scope resolution must be synchronous because
    it precedes any I/O.

    Scope-type validation is deferred to ``budget.reserve``, which
    already enforces it with clear errors; we return ``Any`` rather
    than fighting mypy through the spec-as-object indirection.
    """
    if callable(spec):
        result = spec(*args, **kwargs)
        if inspect.isawaitable(result):
            raise TypeError(
                "scope callable must return synchronously; got an awaitable"
            )
        return result
    return spec


async def _resolve_value(
    spec: _Spec,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Resolve a value spec, awaiting if the callable returned a coroutine."""
    result = spec(*args, **kwargs) if callable(spec) else spec
    if inspect.isawaitable(result):
        result = await result
    return result


async def _resolve_actual(
    fn: Callable[..., Decimal | Awaitable[Decimal]],
    response: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Call ``fn(response, *args, **kwargs)``, awaiting if it returned a coroutine.

    Returns ``Any`` and lets the caller validate that the result is a
    ``Decimal`` — keeps mypy happy on the spec-as-object indirection.
    """
    result = fn(response, *args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _cast_optional_str(value: Any, field_name: str) -> str | None:
    """Validate that a resolved value is ``str | None`` — for request_id, model, provider."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise TypeError(
        f"{field_name} must resolve to str or None, got {type(value).__name__}"
    )
