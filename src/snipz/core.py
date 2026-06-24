"""Reservation engine: ``Budget``, ``Reservation``, ``Scope``, ``BudgetExceededError``.

This module owns the public API. It is dialect-agnostic: every storage
operation is dispatched through :class:`snipz.storage.LedgerConnection`,
not against a driver-specific connection. Pure cap arithmetic helpers
live in :mod:`snipz.ledger`; SQL constants live in
:mod:`snipz.storage.sql`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import UUID

from snipz import ledger
from snipz.events import EventDispatcher, Handler
from snipz.storage import Backend, CommitOutcome, RequestIdConflictError
from snipz.storage.sqlite import SqliteBackend

__all__ = [
    "Budget",
    "BudgetExceededError",
    "InvalidStateError",
    "Reservation",
    "Scope",
]


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class Scope:
    """A scope is the unit of budget enforcement: ``(type, id, window)``.

    Examples::

        Scope("user", "u_42")                  # default window: month
        Scope("tenant", "acme", window="day")
        Scope("global", "anthropic-api")
    """

    type: str
    id: str
    window: str = "month"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BudgetExceededError(Exception):
    """Raised when a reservation would push a scope above its cap."""

    def __init__(
        self,
        *,
        scope: Scope,
        cap_cents: Decimal,
        spent_cents: Decimal,
        attempted_cents: Decimal,
    ) -> None:
        self.scope = scope
        self.cap_cents = cap_cents
        self.spent_cents = spent_cents
        self.attempted_cents = attempted_cents
        super().__init__(
            f"budget exceeded for {scope.type}/{scope.id} ({scope.window}): "
            f"spent={spent_cents}c attempting={attempted_cents}c cap={cap_cents}c"
        )


class InvalidStateError(Exception):
    """Raised when a :class:`Reservation` method is called in an invalid state."""


# ---------------------------------------------------------------------------
# Reservation — value object with lifecycle methods
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Reservation:
    """An in-flight or settled reservation against one or more scopes.

    Construct via :meth:`Budget.reserve`. Use as an async context manager
    to auto-commit on success and auto-release on exception, or call
    :meth:`commit` / :meth:`release` / :meth:`observe` explicitly.
    """

    id: UUID
    scopes: tuple[Scope, ...]
    estimated_cents: Decimal
    actual_cents: Decimal | None
    state: str
    late: bool
    request_id: str | None
    expires_at: datetime
    _budget: Budget = field(repr=False, compare=False)

    async def observe(self, actual_cents: Decimal) -> None:
        """Update ``actual_cents`` to reflect cost observed during streaming."""
        if self.state != "reserved":
            raise InvalidStateError(
                f"observe() requires state 'reserved', got {self.state!r}"
            )
        if actual_cents < 0:
            raise ValueError(
                f"actual_cents must be non-negative, got {actual_cents}"
            )
        await self._budget._observe(self.id, actual_cents)
        self.actual_cents = actual_cents

    async def commit(self, actual_cents: Decimal | None = None) -> None:
        """Settle the reservation as committed.

        If ``actual_cents`` is omitted, uses the most recently observed
        value, or the original estimate if ``observe()`` was never
        called.

        On a sweeper-released row (``state == 'released'`` and
        ``late``), the commit succeeds and marks the row as a late
        commit.
        """
        if self.state == "committed":
            return  # idempotent
        if self.state == "released" and not self.late:
            raise InvalidStateError("cannot commit a caller-released reservation")

        cost = actual_cents
        if cost is None:
            cost = self.actual_cents if self.actual_cents is not None else self.estimated_cents
        if cost < 0:
            raise ValueError(f"actual_cents must be non-negative, got {cost}")

        outcome = await self._budget._commit(self.id, cost)
        if outcome.rows_affected == 0:
            raise InvalidStateError(
                f"reservation {self.id} could not be committed; "
                "row may have been caller-released or removed"
            )
        self.state = "committed"
        self.actual_cents = cost
        if outcome.was_late:
            self.late = True
            await self._budget._events.fire("overrun", self)
        await self._budget._events.fire("committed", self)

    async def release(self) -> None:
        """Refund the reservation. No-op if already settled."""
        if self.state in ("released", "committed"):
            return
        await self._budget._release(self.id)
        self.state = "released"
        await self._budget._events.fire("released", self)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.state != "reserved":
            return
        if exc_type is None:
            await self.commit()
        else:
            await self.release()


# ---------------------------------------------------------------------------
# Budget — top-level orchestrator
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _build_backend(spec: str | Path | Backend) -> Backend:
    """Resolve a backend specification to a concrete :class:`Backend`.

    * ``"postgres://..."`` / ``"postgresql://..."`` → :class:`PostgresBackend`
      (the ``snipz[postgres]`` extra must be installed).
    * Any other ``str`` or :class:`pathlib.Path` → :class:`SqliteBackend`,
      treating the value as a filesystem path.
    * Anything else is assumed to satisfy the :class:`Backend` protocol
      and is returned as-is. Use this path to inject a backend with a
      pre-configured pool, custom timeouts, etc.
    """
    if isinstance(spec, str):
        if spec.startswith(("postgres://", "postgresql://")):
            from snipz.storage.postgres import PostgresBackend
            return PostgresBackend(spec)
        return SqliteBackend(spec)
    if isinstance(spec, Path):
        return SqliteBackend(spec)
    return spec


class Budget:
    """Top-level orchestrator. One instance per database.

    The ``backend`` argument accepts a SQLite path, a Postgres
    connection string, or a pre-built :class:`Backend` instance.

    Examples::

        # SQLite, file path
        budget = Budget("snipz.db")

        # Postgres, managed pool
        budget = Budget("postgres://localhost/snipz")

        # Postgres, injected pool (advanced)
        from snipz.storage.postgres import PostgresBackend
        backend = PostgresBackend(pool=app_pool)
        budget = Budget(backend)

        await budget.migrate()
        await budget.set_limit(Scope("user", "u_42"), Decimal("500"))

        async with await budget.reserve(Scope("user", "u_42"), Decimal("10")) as r:
            response = await call_anthropic(...)
            await r.observe(cost_for_response(response))

        await budget.close()
    """

    def __init__(
        self,
        backend: str | Path | Backend,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend: Backend = _build_backend(backend)
        self._now = now if now is not None else _utc_now
        self._events: EventDispatcher = EventDispatcher()

    # -- event hooks ----------------------------------------------------------

    def on_reserved(self, handler: Handler) -> Handler:
        """Register a handler called after each newly created reservation.

        Returns the handler so it doubles as a decorator. Does not fire
        on idempotent cached returns (same ``request_id`` as a prior
        reserve).
        """
        return self._events.register("reserved", handler)

    def on_committed(self, handler: Handler) -> Handler:
        """Register a handler called after each successful commit.

        Returns the handler so it doubles as a decorator. Does not fire
        on idempotent re-commits (state already ``committed``).
        """
        return self._events.register("committed", handler)

    def on_released(self, handler: Handler) -> Handler:
        """Register a handler called after a caller-initiated release.

        Returns the handler so it doubles as a decorator. Does not fire
        on the bulk sweep path — see :mod:`snipz.events`.
        """
        return self._events.register("released", handler)

    def on_overrun(self, handler: Handler) -> Handler:
        """Register a handler called when a commit succeeds via the late path.

        Fires *in addition to* the ``committed`` handler when
        :class:`Reservation` was already released by the sweeper but
        the caller's commit reclaimed it.
        """
        return self._events.register("overrun", handler)

    async def migrate(self) -> None:
        """Apply pending schema migrations."""
        await self._backend.migrate()

    async def close(self) -> None:
        """Release backend-owned resources (pool, etc.).

        Idempotent. Safe to call from a sync context too via the
        experimental :mod:`snipz.sync` wrapper.
        """
        await self._backend.close()

    def guard(
        self,
        *,
        scope: object,
        estimate: object,
        actual: object = None,
        request_id: object = None,
        ttl: int = 300,
        model: object = None,
        provider: object = None,
    ) -> object:
        """Return a decorator that wraps an async LLM call with budget enforcement.

        Each parameter (``scope``, ``estimate``, ``actual``,
        ``request_id``, ``model``, ``provider``) accepts either a
        static value or a callable. Callables receive the wrapped
        function's ``*args, **kwargs``; the special case is ``actual``,
        which additionally receives the wrapped function's return
        value as its first positional argument:
        ``actual(response, *args, **kwargs)``.

        Callables may return either a value or an awaitable; the
        decorator awaits both transparently.

        Example::

            @budget.guard(
                scope=lambda user_id, **kw: Scope("user", user_id),
                estimate=lambda *a, **kw: Decimal("10"),
                actual=lambda response, *a, **kw: pricing.cost(
                    provider="anthropic", model="claude-3-5-sonnet-20241022",
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                ),
            )
            async def call_llm(user_id: str, prompt: str) -> Response:
                return await anthropic.messages.create(...)

        See :mod:`snipz.decorator` for the full contract.
        """
        from snipz.decorator import make_guard

        return make_guard(
            self,
            scope=scope,
            estimate=estimate,
            actual=actual,  # type: ignore[arg-type]
            request_id=request_id,
            ttl=ttl,
            model=model,
            provider=provider,
        )

    async def set_limit(
        self,
        scope: Scope,
        cap_cents: Decimal,
        *,
        grace_pct: int = 0,
    ) -> None:
        """Configure or update the cap for a scope."""
        if cap_cents < 0:
            raise ValueError("cap_cents must be non-negative")
        if grace_pct < 0:
            raise ValueError("grace_pct must be non-negative")
        async with self._backend.transaction() as conn:
            await conn.upsert_limit(
                scope_type=scope.type,
                scope_id=scope.id,
                window=scope.window,
                cap_cents=cap_cents,
                grace_pct=grace_pct,
            )

    async def reserve(
        self,
        scopes: Scope | Sequence[Scope],
        estimated_cents: Decimal,
        *,
        request_id: str | None = None,
        ttl: int = 300,
        model: str | None = None,
        provider: str | None = None,
    ) -> Reservation:
        """Reserve budget against one or more scopes.

        Atomically checks each scope's cap and inserts ledger rows. If
        any cap would be exceeded, raises :class:`BudgetExceededError`
        and inserts nothing.

        ``request_id`` provides idempotency: parallel retries with the
        same ``request_id`` produce exactly one ledger row group; all
        callers receive the same :class:`Reservation`.
        """
        if estimated_cents < 0:
            raise ValueError("estimated_cents must be non-negative")
        if ttl <= 0:
            raise ValueError("ttl must be positive")

        scopes_seq: tuple[Scope, ...] = (
            (scopes,) if isinstance(scopes, Scope) else tuple(scopes)
        )
        if not scopes_seq:
            raise ValueError("at least one scope required")

        # Idempotency pre-check: cheap SELECT outside the write transaction.
        if request_id is not None:
            cached = await self._reservation_by_request_id(request_id)
            if cached is not None:
                return cached

        sorted_scopes = tuple(sorted(scopes_seq))
        now = self._now()
        expires_at = ledger.compute_expires_at(now, ttl)

        try:
            reservation = await self._reserve_tx(
                scopes=sorted_scopes,
                estimated_cents=estimated_cents,
                request_id=request_id,
                expires_at=expires_at,
                now=now,
                model=model,
                provider=provider,
            )
        except RequestIdConflictError:
            # Concurrent request_id collision — another writer landed
            # first. Re-query to converge on the canonical reservation.
            # No on_reserved event: this code path returns a cached
            # reservation whose original creation already fired.
            if request_id is None:
                raise
            cached = await self._reservation_by_request_id(request_id)
            if cached is None:
                # The conflicting row vanished between our INSERT and
                # the re-query (only possible if someone deleted it).
                # Surface as the original error.
                raise
            return cached

        await self._events.fire("reserved", reservation)
        return reservation

    async def sweep(self) -> int:
        """Release reservations whose ``expires_at`` has passed.

        Returns the number of rows released. Sweeper-released rows are
        marked ``late=True``; a subsequent :meth:`Reservation.commit`
        is honored as a late commit.
        """
        async with self._backend.transaction() as conn:
            return await conn.sweep_expired(now=self._now())

    # ------------------------------------------------------------------
    # Internal — called by Reservation
    # ------------------------------------------------------------------

    async def _observe(self, reservation_id: UUID, actual_cents: Decimal) -> None:
        async with self._backend.transaction() as conn:
            await conn.update_observation(reservation_id, actual_cents)

    async def _commit(
        self, reservation_id: UUID, actual_cents: Decimal
    ) -> CommitOutcome:
        async with self._backend.transaction() as conn:
            return await conn.commit_reservation(
                reservation_id, actual_cents, now=self._now()
            )

    async def _release(self, reservation_id: UUID) -> None:
        async with self._backend.transaction() as conn:
            await conn.release_reservation(reservation_id, now=self._now())

    # ------------------------------------------------------------------
    # Internal — reservation construction
    # ------------------------------------------------------------------

    async def _reserve_tx(
        self,
        *,
        scopes: tuple[Scope, ...],
        estimated_cents: Decimal,
        request_id: str | None,
        expires_at: datetime,
        now: datetime,
        model: str | None,
        provider: str | None,
    ) -> Reservation:
        reservation_id = ledger.new_uuid()

        async with self._backend.transaction() as conn:
            # Lock all limit rows in sorted order, then run cap checks.
            # Locking in a deterministic order across all transactions
            # prevents deadlocks under composite reservations.
            for scope in scopes:
                await conn.lock_limit(
                    scope_type=scope.type,
                    scope_id=scope.id,
                    window=scope.window,
                )
                limit = await conn.fetch_limit(
                    scope_type=scope.type,
                    scope_id=scope.id,
                    window=scope.window,
                )
                if limit is None:
                    continue  # tracker mode — no cap configured

                spent = await conn.current_spend(
                    scope_type=scope.type,
                    scope_id=scope.id,
                    window_start_at=ledger.window_start(scope.window, now),
                )
                cap = ledger.cap_with_grace(limit)
                if spent + estimated_cents > cap:
                    raise BudgetExceededError(
                        scope=scope,
                        cap_cents=limit.cap_cents,
                        spent_cents=spent,
                        attempted_cents=estimated_cents,
                    )

            for scope in scopes:
                await conn.insert_ledger_row(
                    row_id=ledger.new_uuid(),
                    reservation_id=reservation_id,
                    scope_type=scope.type,
                    scope_id=scope.id,
                    estimated_cents=estimated_cents,
                    model=model,
                    provider=provider,
                    request_id=request_id,
                    expires_at=expires_at,
                )

        return Reservation(
            id=reservation_id,
            scopes=scopes,
            estimated_cents=estimated_cents,
            actual_cents=None,
            state="reserved",
            late=False,
            request_id=request_id,
            expires_at=expires_at,
            _budget=self,
        )

    async def _reservation_by_request_id(self, request_id: str) -> Reservation | None:
        async with self._backend.connect() as conn:
            rows = await conn.find_by_request_id(request_id)

        if not rows:
            return None
        first = rows[0]
        scopes = tuple(
            sorted(Scope(type=r.scope_type, id=r.scope_id) for r in rows)
        )
        return Reservation(
            id=first.reservation_id,
            scopes=scopes,
            estimated_cents=first.estimated_cents,
            actual_cents=first.actual_cents,
            state=first.state,
            late=first.late,
            request_id=first.request_id,
            expires_at=first.expires_at,
            _budget=self,
        )
