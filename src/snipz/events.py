"""Event dispatch — observability hooks for reservation lifecycle.

Four hook types:

* ``reserved`` — fires after :meth:`Budget.reserve` returns a freshly
  inserted reservation. Does NOT fire on the idempotent cached return
  (when ``request_id`` matched an existing row).
* ``committed`` — fires after :meth:`Reservation.commit` successfully
  settles the reservation. Does NOT fire on the idempotent re-commit
  (when state is already ``committed``).
* ``released`` — fires after a caller-initiated
  :meth:`Reservation.release`. Does NOT fire on idempotent re-release.
* ``overrun`` — fires when a commit succeeds via the late-commit path
  (the sweeper had already released the row; the caller's late commit
  reclaimed it). Fires *in addition to* ``committed``.

Handlers may be sync or async. Async handlers are awaited inline.
Handler exceptions are logged at ``ERROR`` level and the dispatch
continues to the next handler — exceptions never reach the caller of
``reserve()`` / ``commit()`` / ``release()``.

``Budget.sweep()`` does not fire ``released`` — the sweeper runs a bulk
``UPDATE`` and has no per-row reservation object. Observability on the
sweep path is the sweeper's own log (see :mod:`snipz.sweep`).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from snipz.core import Reservation

__all__ = ["EventDispatcher", "EventName", "Handler"]


type EventName = Literal["reserved", "committed", "released", "overrun"]
type Handler = Callable[["Reservation"], Any]


_LOGGER: Final = logging.getLogger("snipz.events")
_VALID_EVENTS: Final[tuple[EventName, ...]] = (
    "reserved",
    "committed",
    "released",
    "overrun",
)


class EventDispatcher:
    """Per-Budget registry of lifecycle handlers.

    Handlers are stored per event name and fired in registration order.
    A handler that raises is logged via ``logger.exception`` and the
    dispatch loop continues to the next handler.
    """

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[EventName, list[Handler]] = {
            name: [] for name in _VALID_EVENTS
        }

    def register(self, event: EventName, handler: Handler) -> Handler:
        """Register ``handler`` for ``event`` and return it.

        Returning the handler lets this method double as a decorator::

            @budget.on_committed
            def log_commit(r): ...

        is equivalent to::

            budget.on_committed(log_commit)
        """
        if event not in self._handlers:  # pragma: no cover
            raise ValueError(f"unknown event {event!r}")
        self._handlers[event].append(handler)
        return handler

    async def fire(self, event: EventName, reservation: Reservation) -> None:
        """Call every handler registered for ``event`` with ``reservation``.

        Sync handlers are called directly; async handlers are awaited
        inline. Per-handler exceptions are caught, logged, and the
        next handler runs.
        """
        for handler in self._handlers[event]:
            try:
                result = handler(reservation)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _LOGGER.exception(
                    "snipz.events: %s handler raised; continuing", event
                )
