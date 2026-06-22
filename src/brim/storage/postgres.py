"""PostgreSQL backend.

Two classes:

* :class:`PostgresLedgerConnection` — implements
  :class:`brim.storage.LedgerConnection` over an ``asyncpg.Connection``.
  asyncpg's type codecs round-trip UUIDs, decimals, datetimes, and
  booleans natively, so this class does no string conversion at the
  Python boundary.

* :class:`PostgresBackend` — owns the connection pool and migration
  discovery. ``transaction()`` opens a pool connection, starts a
  transaction, applies a session-local ``lock_timeout`` to bound the
  cap-check critical section, and yields the wrapped connection. The
  pool may be managed by the backend (DSN constructor) or injected
  (``pool=`` keyword).

asyncpg is an optional dependency (``pip install brim[postgres]``).
This module imports cleanly without it; constructing
:class:`PostgresBackend` raises a clear :class:`ImportError` if asyncpg
is not installed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import TYPE_CHECKING, Final
from uuid import UUID

from brim.storage import (
    CommitOutcome,
    LedgerRow,
    LimitRow,
    RequestIdConflictError,
)
from brim.storage.sql import postgres as sql

if TYPE_CHECKING:  # pragma: no cover
    from asyncpg import Connection, Pool, Record


# ---------------------------------------------------------------------------
# Optional asyncpg import
# ---------------------------------------------------------------------------
#
# The Postgres backend is opt-in. We attempt to import asyncpg at module
# load so the runtime check is cheap; if it is missing we surface a
# clear error from :meth:`PostgresBackend.__init__` rather than at
# import time, so applications that only use SQLite are unaffected.

try:
    import asyncpg as _asyncpg
    _IMPORT_ERROR: ImportError | None = None
except ImportError as _exc:  # pragma: no cover
    _asyncpg = None
    _IMPORT_ERROR = ImportError(
        "asyncpg is required for the Postgres backend. "
        "Install with: pip install 'brim[postgres]'"
    )
    _IMPORT_ERROR.__cause__ = _exc


_MIGRATIONS_PACKAGE: Final = "brim.storage.migrations.postgres"
_LOGGER: Final = logging.getLogger("brim.storage.postgres")


# ---------------------------------------------------------------------------
# LedgerConnection implementation
# ---------------------------------------------------------------------------


class PostgresLedgerConnection:
    """LedgerConnection implementation backed by ``asyncpg``.

    Wraps an open ``asyncpg.Connection`` and dispatches to SQL constants
    from :mod:`brim.storage.sql.postgres`. asyncpg's native type
    codecs handle UUIDs, decimals, datetimes, and booleans transparently
    so this class does no value conversion.

    Constructed by :class:`PostgresBackend.transaction` /
    :class:`PostgresBackend.connect`; not part of the public API.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # -- cap-check operations -------------------------------------------------

    async def lock_limit(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: str,
    ) -> None:
        # Row-level write lock on the limit row. If no limit is
        # configured for the scope (tracker mode), this returns 0 rows
        # and locks nothing — fetch_limit will return None and the
        # caller will skip this scope.
        await self._conn.execute(sql.LOCK_LIMIT, scope_type, scope_id, window)

    async def fetch_limit(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: str,
    ) -> LimitRow | None:
        row = await self._conn.fetchrow(
            sql.LIMIT_LOOKUP, scope_type, scope_id, window
        )
        if row is None:
            return None
        return LimitRow(
            cap_cents=Decimal(str(row["cap_cents"])),
            grace_pct=int(row["grace_pct"]),
        )

    async def current_spend(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window_start_at: datetime,
    ) -> Decimal:
        result = await self._conn.fetchval(
            sql.SPEND_QUERY, scope_type, scope_id, window_start_at
        )
        if result is None:
            return Decimal("0")
        # COALESCE may yield int when SUM is NULL; normalize to Decimal.
        return Decimal(str(result))

    # -- ledger writes --------------------------------------------------------

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
        try:
            await self._conn.execute(
                sql.INSERT_LEDGER,
                row_id,
                reservation_id,
                scope_type,
                scope_id,
                estimated_cents,
                model,
                provider,
                request_id,
                expires_at,
            )
        except _asyncpg.exceptions.UniqueViolationError as exc:
            # The only unique index hit by this insert is the partial
            # index on ``request_id``; the primary key is a freshly
            # generated UUID. A NULL ``request_id`` therefore cannot
            # collide.
            if request_id is None:
                raise
            raise RequestIdConflictError(request_id) from exc

    async def update_observation(
        self,
        reservation_id: UUID,
        actual_cents: Decimal,
    ) -> int:
        status = await self._conn.execute(
            sql.OBSERVE_UPDATE, actual_cents, reservation_id
        )
        return _parse_rowcount(status)

    async def commit_reservation(
        self,
        reservation_id: UUID,
        actual_cents: Decimal,
        *,
        now: datetime,
    ) -> CommitOutcome:
        status = await self._conn.execute(
            sql.COMMIT_NORMAL, actual_cents, now, reservation_id
        )
        normal_count = _parse_rowcount(status)
        if normal_count > 0:
            return CommitOutcome(rows_affected=normal_count, was_late=False)

        status = await self._conn.execute(
            sql.COMMIT_LATE, actual_cents, now, reservation_id
        )
        late_count = _parse_rowcount(status)
        return CommitOutcome(rows_affected=late_count, was_late=late_count > 0)

    async def release_reservation(
        self,
        reservation_id: UUID,
        *,
        now: datetime,
    ) -> int:
        status = await self._conn.execute(
            sql.RELEASE_BY_CALLER, now, reservation_id
        )
        return _parse_rowcount(status)

    async def sweep_expired(self, *, now: datetime) -> int:
        status = await self._conn.execute(sql.SWEEP_EXPIRED, now, now)
        return _parse_rowcount(status)

    # -- queries --------------------------------------------------------------

    async def find_by_request_id(self, request_id: str) -> list[LedgerRow]:
        records = await self._conn.fetch(sql.FIND_BY_REQUEST_ID, request_id)
        return [_record_to_ledger(r) for r in records]

    async def find_by_reservation_id(
        self,
        reservation_id: UUID,
    ) -> list[LedgerRow]:
        records = await self._conn.fetch(sql.FIND_BY_RESERVATION_ID, reservation_id)
        return [_record_to_ledger(r) for r in records]

    # -- limits ---------------------------------------------------------------

    async def upsert_limit(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: str,
        cap_cents: Decimal,
        grace_pct: int,
    ) -> None:
        await self._conn.execute(
            sql.UPSERT_LIMIT,
            scope_type,
            scope_id,
            window,
            cap_cents,
            grace_pct,
        )


def _parse_rowcount(status: str) -> int:
    """Parse asyncpg's command status string for the affected row count.

    asyncpg returns strings like ``"UPDATE 3"``, ``"INSERT 0 5"``,
    ``"DELETE 1"``. The trailing integer is the row count in every case.
    """
    parts = status.split()
    if not parts:
        return 0
    last = parts[-1]
    return int(last) if last.isdigit() else 0


def _record_to_ledger(record: Record) -> LedgerRow:
    # ``window`` is not stored on ledger rows; the engine fills it in
    # from the corresponding scope before returning to the caller. The
    # empty string here is a sentinel — callers MUST overwrite it.
    return LedgerRow(
        id=record["id"],
        reservation_id=record["reservation_id"],
        scope_type=record["scope_type"],
        scope_id=record["scope_id"],
        window="",
        state=record["state"],
        late=record["late"],
        estimated_cents=record["estimated_cents"],
        actual_cents=record["actual_cents"],
        request_id=record["request_id"],
        expires_at=record["expires_at"],
        created_at=record["created_at"],
    )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class PostgresBackend:
    """PostgreSQL storage backend.

    Owns an ``asyncpg.Pool`` (or accepts an injected one). Each
    transaction runs ``SET LOCAL lock_timeout`` to bound how long a
    contended cap-check waits on the limit-row lock.

    Two construction modes:

    Default (managed pool)::

        backend = PostgresBackend("postgres://localhost/brim")

    Injected pool (share with host application)::

        pool = await asyncpg.create_pool(...)
        backend = PostgresBackend(pool=pool)

    Pool utilization at or above ``pool_warn_threshold`` triggers a
    rate-limited ``logging.warning`` so operators learn about pressure
    before exhaustion. Both threshold and cooldown are configurable.
    """

    __slots__ = (
        "_closed",
        "_dsn",
        "_last_warned_monotonic",
        "_lock_timeout",
        "_max_size",
        "_min_size",
        "_owns_pool",
        "_pool",
        "_pool_warn_cooldown",
        "_pool_warn_threshold",
    )

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: Pool | None = None,
        min_size: int = 2,
        max_size: int = 10,
        lock_timeout: str = "5s",
        pool_warn_threshold: float = 0.8,
        pool_warn_cooldown: float = 60.0,
    ) -> None:
        if _asyncpg is None:  # pragma: no cover
            raise _IMPORT_ERROR if _IMPORT_ERROR is not None else ImportError(
                "asyncpg is required for the Postgres backend"
            )

        if dsn is None and pool is None:
            raise ValueError("PostgresBackend requires either `dsn` or `pool`")
        if dsn is not None and pool is not None:
            raise ValueError(
                "PostgresBackend accepts `dsn` or `pool`, not both"
            )
        if min_size < 0 or max_size <= 0 or min_size > max_size:
            raise ValueError(
                f"invalid pool sizing: min_size={min_size}, max_size={max_size}"
            )
        if not 0.0 < pool_warn_threshold <= 1.0:
            raise ValueError(
                f"pool_warn_threshold must be in (0.0, 1.0]: got {pool_warn_threshold}"
            )
        if pool_warn_cooldown < 0:
            raise ValueError(
                f"pool_warn_cooldown must be non-negative: got {pool_warn_cooldown}"
            )

        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None
        self._min_size = min_size
        self._max_size = max_size
        self._lock_timeout = lock_timeout
        self._pool_warn_threshold = pool_warn_threshold
        self._pool_warn_cooldown = pool_warn_cooldown
        self._last_warned_monotonic = 0.0
        self._closed = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PostgresLedgerConnection]:
        """Open a transactional :class:`PostgresLedgerConnection`.

        Acquires a connection from the pool, starts a transaction, and
        sets a session-local ``lock_timeout`` so a contended cap-check
        fails fast instead of hanging.
        """
        pool = await self._ensure_pool()
        self._maybe_warn_pool_pressure(pool)
        async with pool.acquire() as conn, conn.transaction():
            # SET LOCAL lock_timeout — parameterized via set_config
            # so the timeout value is never concatenated into SQL.
            await conn.fetchval(sql.SET_LOCK_TIMEOUT, self._lock_timeout)
            yield PostgresLedgerConnection(conn)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[PostgresLedgerConnection]:
        """Open a non-transactional :class:`PostgresLedgerConnection`."""
        pool = await self._ensure_pool()
        self._maybe_warn_pool_pressure(pool)
        async with pool.acquire() as conn:
            yield PostgresLedgerConnection(conn)

    async def migrate(self) -> None:
        """Apply pending migrations idempotently.

        Each migration file is executed inside its own transaction so a
        failed migration rolls back cleanly.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            current = await self._current_schema_version(conn)
            for entry, _version in self._pending_migrations(current):
                migration_sql = entry.read_text(encoding="utf-8")
                async with conn.transaction():
                    await conn.execute(migration_sql)

    async def close(self) -> None:
        """Drain the pool if owned by this backend.

        No-op for injected pools (caller owns the lifecycle). Idempotent;
        safe to call multiple times.
        """
        if self._closed:
            return
        self._closed = True
        if self._owns_pool and self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- internals ------------------------------------------------------------

    async def _ensure_pool(self) -> Pool:
        if self._closed:
            raise RuntimeError(
                "PostgresBackend has been closed; cannot acquire a connection"
            )
        if self._pool is None:
            # __init__ guarantees _asyncpg is non-None and exactly one of
            # _dsn / _pool was set; if we are here, _dsn is the one.
            if _asyncpg is None or self._dsn is None:  # pragma: no cover
                raise RuntimeError(
                    "PostgresBackend invariant violated: "
                    "lazy pool creation reached without dsn or asyncpg"
                )
            self._pool = await _asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
            )
        return self._pool

    def _maybe_warn_pool_pressure(self, pool: Pool) -> None:
        """Emit a rate-limited warning when the pool is under pressure.

        The check is on the hot path; cost is two integer reads and a
        ratio compare. The warning's string formatting and log emit
        only happen when actually warning.
        """
        max_size = pool.get_max_size()
        if max_size <= 0:
            return
        size = pool.get_size()
        if size / max_size < self._pool_warn_threshold:
            return
        now = time.monotonic()
        if now - self._last_warned_monotonic < self._pool_warn_cooldown:
            return
        self._last_warned_monotonic = now
        _LOGGER.warning(
            "Brim Postgres pool near capacity (%d/%d). "
            "Consider increasing max_size for high-throughput environments.",
            size,
            max_size,
        )

    @staticmethod
    async def _current_schema_version(conn: Connection) -> int:
        present = await conn.fetchval(sql.SCHEMA_VERSION_TABLE_EXISTS)
        if present is None:
            return 0
        version = await conn.fetchval(sql.SCHEMA_VERSION_MAX)
        return int(version) if version is not None else 0

    @staticmethod
    def _pending_migrations(
        current_version: int,
    ) -> Iterator[tuple[Traversable, int]]:
        migrations_dir = files(_MIGRATIONS_PACKAGE)
        entries = sorted(
            (e for e in migrations_dir.iterdir() if e.name.endswith(".sql")),
            key=lambda e: e.name,
        )
        for entry in entries:
            try:
                version = int(entry.name.split("_", 1)[0])
            except ValueError:
                continue
            if version > current_version:
                yield entry, version
