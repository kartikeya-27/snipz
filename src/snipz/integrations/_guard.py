"""Provider-neutral reserve → call → settle lifecycle.

Every provider wrapper funnels through :func:`run_guarded`, which owns
the rules that keep the ledger honest once money may have been spent:

* **Duplicates never reach the provider.** A repeated ``request_id``
  returns the existing reservation (``created=False``); the call is
  refused with :class:`DuplicateRequestError` instead of being paid for
  a second time against one ledger row.
* **Definitive failures release.** The provider rejected the request
  (an HTTP error status), so nothing was billed.
* **Ambiguous failures commit the estimate.** A timeout, dropped
  connection, or cancellation may still have been billed; recording the
  reserved upper bound keeps the cap safe at the cost of over-counting.
* **Success never raises over a pricing gap.** If the actual cost cannot
  be computed (unknown model, missing cache rate), the estimate is
  committed and the gap is logged — raising here would release the
  reservation and lose the record of a call that was already paid for.
* **A failed commit keeps the response.** The caller paid for it, so
  :class:`SettlementError` carries it alongside the database error.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Generic, TypeVar

if TYPE_CHECKING:
    from snipz.core import Budget, Reservation, Scope

__all__ = [
    "DuplicateRequestError",
    "SettlementError",
    "reserve",
    "run_guarded",
    "settle_failure",
    "settle_quietly",
    "settle_success",
]

_LOGGER: Final = logging.getLogger("snipz.integrations")

_R = TypeVar("_R")


class DuplicateRequestError(Exception):
    """Raised instead of calling the provider for a repeated ``request_id``.

    ``state`` is the existing reservation's state: ``"reserved"`` while
    the original call is still in flight, ``"committed"`` once it has
    been paid for, ``"released"`` when it failed without being billed.
    A ``request_id`` identifies one attempt — to retry a released call,
    send it with a new ``request_id``.
    """

    def __init__(self, request_id: str, state: str) -> None:
        self.request_id = request_id
        self.state = state
        super().__init__(
            f"request_id {request_id!r} already has a reservation in state "
            f"{state!r}; refusing to call the provider again"
        )


class SettlementError(Exception, Generic[_R]):
    """The provider call succeeded but recording its cost failed.

    ``response`` is the provider's response — already paid for, so it is
    handed back rather than dropped. The reservation stays ``reserved``
    until the sweeper releases it; ``__cause__`` is the storage error.
    """

    def __init__(self, response: _R, reservation: Reservation) -> None:
        self.response = response
        self.reservation = reservation
        super().__init__(
            f"provider call succeeded but committing reservation {reservation.id} failed"
        )


async def run_guarded(
    *,
    budget: Budget,
    scopes: Scope | Sequence[Scope],
    estimated_cents: Decimal,
    call: Callable[[], Awaitable[_R]],
    actual_cents: Callable[[_R], Decimal],
    is_ambiguous_failure: Callable[[BaseException], bool],
    request_id: str | None,
    ttl: int,
    model: str,
    provider: str,
) -> _R:
    """Reserve ``estimated_cents``, run ``call``, and settle the ledger.

    ``actual_cents`` prices the response; ``is_ambiguous_failure`` tells
    a possibly-billed failure from a definitive rejection. See the
    module docstring for the settlement rules.
    """
    reservation = await reserve(
        budget,
        scopes=scopes,
        estimated_cents=estimated_cents,
        request_id=request_id,
        ttl=ttl,
        model=model,
        provider=provider,
    )
    try:
        response = await call()
    except BaseException as exc:
        await settle_failure(reservation, exc, is_ambiguous_failure=is_ambiguous_failure)
        raise
    await settle_success(
        reservation, response, lambda: actual_cents(response), provider=provider, model=model
    )
    return response


# ---------------------------------------------------------------------------
# Lifecycle steps — composed by run_guarded, and by wrappers whose provider
# call outlives one await (streams).
# ---------------------------------------------------------------------------


async def reserve(
    budget: Budget,
    *,
    scopes: Scope | Sequence[Scope],
    estimated_cents: Decimal,
    request_id: str | None,
    ttl: int,
    model: str,
    provider: str,
) -> Reservation:
    """Reserve, refusing a repeated ``request_id`` with :class:`DuplicateRequestError`."""
    reservation = await budget.reserve(
        scopes,
        estimated_cents,
        request_id=request_id,
        ttl=ttl,
        model=model,
        provider=provider,
    )
    if not reservation.created:
        # Only reachable with a request_id: reserve() never returns an
        # existing reservation otherwise.
        raise DuplicateRequestError(reservation.request_id or "", reservation.state)
    return reservation


async def settle_failure(
    reservation: Reservation,
    exc: BaseException,
    *,
    is_ambiguous_failure: Callable[[BaseException], bool],
) -> None:
    """Settle after the provider call raised ``exc``, without raising.

    Commits the reserved bound if the call may have been billed; releases
    otherwise.
    """
    if is_ambiguous_failure(exc):
        await _settle_quietly(reservation.commit(reservation.estimated_cents), reservation)
    else:
        await _settle_quietly(reservation.release(), reservation)


async def settle_success(
    reservation: Reservation,
    response: _R,
    cost: Callable[[], Decimal],
    *,
    provider: str,
    model: str,
) -> None:
    """Commit ``cost()`` for a paid call; :class:`SettlementError` if that fails."""
    amount = _price_or_estimate(cost, reservation, provider, model)
    try:
        await reservation.commit(amount)
    except Exception as exc:
        raise SettlementError(response, reservation) from exc


async def settle_quietly(
    reservation: Reservation,
    cost: Callable[[], Decimal],
    *,
    provider: str,
    model: str,
) -> None:
    """Commit ``cost()`` while another exception propagates; never raises."""
    amount = _price_or_estimate(cost, reservation, provider, model)
    await _settle_quietly(reservation.commit(amount), reservation)


async def _settle_quietly(settle: Awaitable[None], reservation: Reservation) -> None:
    """Settle while an exception propagates.

    Shielded so a cancelled caller still settles; a settlement failure is
    logged rather than raised so it cannot mask the original exception.
    The sweeper releases the row if this never lands.
    """
    try:
        await asyncio.shield(settle)
    except Exception:
        _LOGGER.exception(
            "could not settle reservation %s after a failed provider call", reservation.id
        )


def _price_or_estimate(
    cost: Callable[[], Decimal],
    reservation: Reservation,
    provider: str,
    model: str,
) -> Decimal:
    try:
        return cost()
    except Exception:
        _LOGGER.exception(
            "could not price %s/%s response for reservation %s; committing the estimate",
            provider,
            model,
            reservation.id,
        )
        return reservation.estimated_cents
