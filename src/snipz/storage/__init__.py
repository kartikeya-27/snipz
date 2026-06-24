"""Storage backends and the :class:`LedgerConnection` abstraction.

Each backend wraps its native driver in a class that satisfies the
:class:`LedgerConnection` protocol. The engine (:mod:`snipz.core` and
:mod:`snipz.ledger`) speaks only that interface; it never sees
``aiosqlite.Connection`` or ``asyncpg.Connection`` directly.

Data classes (``LedgerRow``, ``LimitRow``, ``CommitOutcome``) live here
rather than in :mod:`snipz.ledger` because they are returned by
:class:`LedgerConnection` methods; placing them in the engine layer
would create an upward dependency from storage into the engine.

v0 ships :class:`snipz.storage.sqlite.SqliteBackend` and
:class:`snipz.storage.postgres.PostgresBackend`. New backends can be
added without touching the engine by implementing :class:`Backend` and
:class:`LedgerConnection`.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

__all__ = [
    "Backend",
    "CommitOutcome",
    "LedgerConnection",
    "LedgerRow",
    "LimitRow",
    "PricingRow",
    "RequestIdConflictError",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RequestIdConflictError(Exception):
    """Raised by :meth:`LedgerConnection.insert_ledger_row` on idempotency
    collisions.

    Backend-agnostic: each implementation catches its native
    unique-violation exception (``sqlite3.IntegrityError``,
    ``asyncpg.UniqueViolationError``, etc.) and re-raises this type.
    The reserve flow recovers by re-querying ``request_id``.
    """

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"request_id collision: {request_id!r}")


# ---------------------------------------------------------------------------
# Row representations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One row of ``snipz_ledger``."""

    id: UUID
    reservation_id: UUID
    scope_type: str
    scope_id: str
    window: str
    state: str
    late: bool
    estimated_cents: Decimal
    actual_cents: Decimal | None
    request_id: str | None
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LimitRow:
    """One row of ``snipz_limits``."""

    cap_cents: Decimal
    grace_pct: int


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    """Result of a commit attempt: how many rows updated, and whether late."""

    rows_affected: int
    was_late: bool


@dataclass(frozen=True, slots=True)
class PricingRow:
    """One row of ``snipz_pricing`` — the latest ``valid_from`` per scope.

    Mirrored at the engine layer by :class:`snipz.PriceEntry` plus
    ``(provider, model)`` identity. Lives here in storage so the
    :class:`LedgerConnection` protocol can return it without importing
    from the engine — preserving the unidirectional layering
    (engine → storage, never the reverse).
    """

    provider: str
    model: str
    input_cents_per_m: Decimal
    output_cents_per_m: Decimal
    cache_read_cents_per_m: Decimal | None
    cache_write_cents_per_m: Decimal | None


# ---------------------------------------------------------------------------
# LedgerConnection protocol — what the engine speaks
# ---------------------------------------------------------------------------


class LedgerConnection(Protocol):
    """Dialect-agnostic operations against a single open connection.

    Implementations bind to native driver connections (aiosqlite,
    asyncpg) and dispatch to dialect-specific SQL. All UUIDs, datetimes,
    decimals, and booleans are exchanged as native Python types — any
    storage-specific stringification is the implementation's concern.
    """

    async def lock_limit(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: str,
    ) -> None:
        """Acquire a writer lock on the limit row for this scope.

        SQLite implementations are typically a no-op: the whole-database
        write lock acquired via ``BEGIN IMMEDIATE`` already serializes
        cap checks. Postgres implementations issue
        ``SELECT ... FOR UPDATE`` on the limit row, with a bounded
        ``lock_timeout`` so contended waiters fail fast.

        MUST be called inside a transaction.
        """
        ...

    async def fetch_limit(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: str,
    ) -> LimitRow | None:
        """Return the configured limit for a scope, or ``None`` if no cap is set."""
        ...

    async def current_spend(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window_start_at: datetime,
    ) -> Decimal:
        """Sum the in-window spend for a scope using the state-aware formula.

        Committed rows count at ``actual_cents``; reserved rows count at
        ``MAX(actual_cents, estimated_cents)`` so streaming overruns are
        visible to subsequent cap checks.
        """
        ...

    async def insert_ledger_row(
        self,
        *,
        row_id: UUID,
        reservation_id: UUID,
        scope_type: str,
        scope_id: str,
        estimated_cents: Decimal,
        model: str | None,
        provider: str | None,
        request_id: str | None,
        expires_at: datetime,
    ) -> None:
        """Insert a new reserved row.

        Raises :class:`RequestIdConflictError` on ``request_id`` collision;
        callers catch and re-query to converge concurrent retries onto
        a single canonical reservation.
        """
        ...

    async def find_by_request_id(self, request_id: str) -> list[LedgerRow]:
        """Return all ledger rows sharing a ``request_id`` (one per scope)."""
        ...

    async def find_by_reservation_id(
        self,
        reservation_id: UUID,
    ) -> list[LedgerRow]:
        """Return all ledger rows sharing a ``reservation_id``."""
        ...

    async def update_observation(
        self,
        reservation_id: UUID,
        actual_cents: Decimal,
    ) -> int:
        """Update ``actual_cents`` on all reserved rows of a reservation.

        Returns the number of rows affected.
        """
        ...

    async def commit_reservation(
        self,
        reservation_id: UUID,
        actual_cents: Decimal,
        *,
        now: datetime,
    ) -> CommitOutcome:
        """Settle a reservation as committed.

        Implementations MUST first try the normal
        ``reserved → committed`` transition. If no rows match (the
        sweeper released them first), they MUST try the late-commit
        transition ``released[late=true] → committed[late=true]``.
        """
        ...

    async def release_reservation(
        self,
        reservation_id: UUID,
        *,
        now: datetime,
    ) -> int:
        """Release a reservation initiated by the caller.

        Returns the number of rows affected. No-op on rows that are
        already settled.
        """
        ...

    async def sweep_expired(self, *, now: datetime) -> int:
        """Release any reservations whose ``expires_at`` has passed.

        Sweeper-released rows are flagged ``late=True`` so a subsequent
        commit can be honored as a late commit.
        """
        ...

    async def upsert_limit(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: str,
        cap_cents: Decimal,
        grace_pct: int,
    ) -> None:
        """Configure or update a scope's cap."""
        ...

    async def load_pricing(self) -> list[PricingRow]:
        """Return the latest pricing row per ``(provider, model)``.

        Time-versioning is deferred — the row with the most recent
        ``valid_from`` wins. Returns ``[]`` if the table is empty
        (which is the normal case: pricing lives in the vendored TOML
        until a deployment chooses to override).
        """
        ...


# ---------------------------------------------------------------------------
# Backend protocol — what core.Budget holds
# ---------------------------------------------------------------------------


class Backend(Protocol):
    """Storage backend.

    A backend owns connection management (file handle, pool, etc.) and
    schema lifecycle. It is a factory for :class:`LedgerConnection`.
    """

    def transaction(self) -> AbstractAsyncContextManager[LedgerConnection]:
        """Open a transactional :class:`LedgerConnection`.

        On normal exit the transaction commits; on exception it rolls
        back. Cap-check operations MUST run inside ``transaction()`` so
        the limit-row lock and the ledger insert are atomic.
        """
        ...

    def connect(self) -> AbstractAsyncContextManager[LedgerConnection]:
        """Open a non-transactional :class:`LedgerConnection`.

        Used for read-only paths that do not need atomicity, such as the
        idempotency pre-check. The connection closes when the context
        manager exits.
        """
        ...

    async def migrate(self) -> None:
        """Apply pending schema migrations idempotently."""
        ...

    async def close(self) -> None:
        """Release backend-owned resources (pool, etc.).

        Idempotent. After ``close()``, the backend MUST raise a clear
        error on any subsequent operation rather than silently
        reconnecting.
        """
        ...
