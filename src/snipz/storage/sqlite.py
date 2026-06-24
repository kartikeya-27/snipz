"""SQLite backend.

Two classes:

* :class:`SqliteLedgerConnection` — implements
  :class:`snipz.storage.LedgerConnection` over an ``aiosqlite.Connection``.
  Owns the dialect-specific stringification of UUIDs, datetimes, and
  booleans on the way down to the database, and parsing on the way up.

* :class:`SqliteBackend` — owns connection lifecycle and migration
  discovery. ``transaction()`` opens a connection wrapped in
  ``BEGIN IMMEDIATE`` ... ``COMMIT``/``ROLLBACK``; ``connect()`` opens a
  non-transactional connection for read-only paths.

For single-node deployments and tests. SQLite has no row-level locking,
so the writer-lock acquired by ``BEGIN IMMEDIATE`` is what serializes
concurrent cap checks. ``lock_limit`` on this backend is therefore a
no-op.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final
from uuid import UUID

import aiosqlite

from snipz.storage import (
    CommitOutcome,
    LedgerRow,
    LimitRow,
    RequestIdConflictError,
)
from snipz.storage.sql import sqlite as sql

# ---------------------------------------------------------------------------
# Process-wide type adapters
# ---------------------------------------------------------------------------
#
# ``str`` round-trip preserves Decimal precision; ``float`` would not.
# Registered once per process; aiosqlite respects the global registry.

sqlite3.register_adapter(Decimal, str)


def _decimal_converter(value: bytes) -> Decimal:
    return Decimal(value.decode("ascii"))


sqlite3.register_converter("NUMERIC", _decimal_converter)


# ---------------------------------------------------------------------------
# Datetime helpers (SQLite stores TIMESTAMPTZ as TEXT in ISO-8601 UTC)
# ---------------------------------------------------------------------------


_ISO_PREFIX: Final = "%Y-%m-%dT%H:%M:%S"


def _fmt_datetime(value: datetime) -> str:
    """Format a UTC datetime to the schema's ISO-8601 string with ms resolution."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    utc = value.astimezone(UTC)
    # ``%f`` emits microseconds; trim to milliseconds to match the schema.
    formatted = utc.strftime(f"{_ISO_PREFIX}.%f")
    return f"{formatted[:-3]}Z"


def _parse_datetime(value: str) -> datetime:
    """Parse a schema ISO-8601 string back to an aware UTC datetime."""
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Pragmas
# ---------------------------------------------------------------------------
#
# WAL mode: readers do not block the single writer, and vice versa, for
# the non-cap-check paths.
# busy_timeout: bound how long a writer waits on the database lock
# before raising ``sqlite3.OperationalError``.

_PRAGMAS: Final = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)

_MIGRATIONS_PACKAGE: Final = "snipz.storage.migrations.sqlite"


# ---------------------------------------------------------------------------
# LedgerConnection implementation
# ---------------------------------------------------------------------------


class SqliteLedgerConnection:
    """LedgerConnection implementation backed by ``aiosqlite``.

    Wraps an open ``aiosqlite.Connection`` and dispatches to SQL
    constants from :mod:`snipz.storage.sql.sqlite`. Stringification of
    UUIDs, datetimes, and booleans happens here so the engine layer
    deals only in native Python types.

    Constructed by :class:`SqliteBackend.transaction` /
    :class:`SqliteBackend.connect`; not part of the public API.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # -- cap-check operations -------------------------------------------------

    async def lock_limit(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: str,
    ) -> None:
        # SQLite has no row-level locks. ``BEGIN IMMEDIATE`` (issued by
        # ``SqliteBackend.transaction``) already holds the database
        # writer lock, so cap checks on every scope are serialized.
        # Arguments are accepted for protocol compatibility.
        del scope_type, scope_id, window

    async def fetch_limit(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: str,
    ) -> LimitRow | None:
        cur = await self._conn.execute(
            sql.LIMIT_LOOKUP, (scope_type, scope_id, window)
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        if row is None:
            return None
        return LimitRow(
            cap_cents=Decimal(str(row[0])),
            grace_pct=int(row[1]),
        )

    async def current_spend(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window_start_at: datetime,
    ) -> Decimal:
        cur = await self._conn.execute(
            sql.SPEND_QUERY,
            (scope_type, scope_id, _fmt_datetime(window_start_at)),
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        if row is None or row[0] is None:
            return Decimal("0")
        return Decimal(str(row[0]))

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
                (
                    str(row_id),
                    str(reservation_id),
                    scope_type,
                    scope_id,
                    estimated_cents,
                    model,
                    provider,
                    request_id,
                    _fmt_datetime(expires_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # The only UNIQUE index hit by an insert is the partial
            # index on ``request_id``; the primary key is a freshly
            # generated UUID. A NULL request_id therefore cannot
            # collide.
            if request_id is None:
                raise
            raise RequestIdConflictError(request_id) from exc

    async def update_observation(
        self,
        reservation_id: UUID,
        actual_cents: Decimal,
    ) -> int:
        cur = await self._conn.execute(
            sql.OBSERVE_UPDATE, (actual_cents, str(reservation_id))
        )
        try:
            return cur.rowcount
        finally:
            await cur.close()

    async def commit_reservation(
        self,
        reservation_id: UUID,
        actual_cents: Decimal,
        *,
        now: datetime,
    ) -> CommitOutcome:
        settled_at = _fmt_datetime(now)
        rid = str(reservation_id)

        cur = await self._conn.execute(
            sql.COMMIT_NORMAL, (actual_cents, settled_at, rid)
        )
        try:
            normal_count = cur.rowcount
        finally:
            await cur.close()

        if normal_count > 0:
            return CommitOutcome(rows_affected=normal_count, was_late=False)

        cur = await self._conn.execute(
            sql.COMMIT_LATE, (actual_cents, settled_at, rid)
        )
        try:
            late_count = cur.rowcount
        finally:
            await cur.close()

        return CommitOutcome(rows_affected=late_count, was_late=late_count > 0)

    async def release_reservation(
        self,
        reservation_id: UUID,
        *,
        now: datetime,
    ) -> int:
        cur = await self._conn.execute(
            sql.RELEASE_BY_CALLER, (_fmt_datetime(now), str(reservation_id))
        )
        try:
            return cur.rowcount
        finally:
            await cur.close()

    async def sweep_expired(self, *, now: datetime) -> int:
        formatted = _fmt_datetime(now)
        cur = await self._conn.execute(sql.SWEEP_EXPIRED, (formatted, formatted))
        try:
            return cur.rowcount
        finally:
            await cur.close()

    # -- queries --------------------------------------------------------------

    async def find_by_request_id(self, request_id: str) -> list[LedgerRow]:
        cur = await self._conn.execute(sql.FIND_BY_REQUEST_ID, (request_id,))
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        return [_row_to_ledger(r) for r in rows]

    async def find_by_reservation_id(
        self,
        reservation_id: UUID,
    ) -> list[LedgerRow]:
        cur = await self._conn.execute(
            sql.FIND_BY_RESERVATION_ID, (str(reservation_id),)
        )
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        return [_row_to_ledger(r) for r in rows]

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
            (scope_type, scope_id, window, cap_cents, grace_pct),
        )


def _row_to_ledger(row: Sequence[object]) -> LedgerRow:
    # ``window`` is not stored on ledger rows; the engine fills it in
    # from the corresponding scope before returning to the caller. The
    # empty string here is a sentinel — callers MUST overwrite it.
    return LedgerRow(
        id=UUID(str(row[0])),
        reservation_id=UUID(str(row[1])),
        scope_type=str(row[2]),
        scope_id=str(row[3]),
        window="",
        state=str(row[4]),
        late=bool(row[5]),
        estimated_cents=Decimal(str(row[6])),
        actual_cents=Decimal(str(row[7])) if row[7] is not None else None,
        request_id=str(row[8]) if row[8] is not None else None,
        expires_at=_parse_datetime(str(row[9])),
        created_at=_parse_datetime(str(row[10])),
    )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class SqliteBackend:
    """SQLite storage backend.

    WAL mode lets readers proceed concurrently with the single writer.
    Reservation transactions use ``BEGIN IMMEDIATE`` to acquire the
    database writer lock at txn start, serializing cap checks across
    writers.

    ``close()`` is a no-op — SQLite has no pool to drain. The method
    exists for protocol parity with backends that do (Postgres).
    """

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqliteLedgerConnection]:
        """Open a transactional :class:`SqliteLedgerConnection`.

        Acquires the SQLite writer lock immediately via
        ``BEGIN IMMEDIATE`` so concurrent reservers serialize on the
        lock rather than racing each other's cap checks.
        """
        conn = await self._open_raw()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield SqliteLedgerConnection(conn)
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")
        finally:
            await conn.close()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[SqliteLedgerConnection]:
        """Open a non-transactional :class:`SqliteLedgerConnection`."""
        conn = await self._open_raw()
        try:
            yield SqliteLedgerConnection(conn)
        finally:
            await conn.close()

    async def migrate(self) -> None:
        """Apply pending migrations idempotently."""
        conn = await self._open_raw()
        try:
            current = await self._current_schema_version(conn)
            for entry, _ in self._pending_migrations(current):
                migration_sql = entry.read_text(encoding="utf-8")
                await conn.executescript(migration_sql)
        finally:
            await conn.close()

    async def close(self) -> None:
        """No-op for SQLite — there is no pool to drain."""
        return None

    # -- internals ------------------------------------------------------------

    async def _open_raw(self) -> aiosqlite.Connection:
        """Open and configure a raw aiosqlite connection."""
        conn = await aiosqlite.connect(
            self._db_path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        for pragma in _PRAGMAS:
            await conn.execute(pragma)
        return conn

    @staticmethod
    async def _current_schema_version(conn: aiosqlite.Connection) -> int:
        cur = await conn.execute(sql.SCHEMA_VERSION_TABLE_EXISTS)
        try:
            present = await cur.fetchone()
        finally:
            await cur.close()
        if not present:
            return 0
        cur = await conn.execute(sql.SCHEMA_VERSION_MAX)
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        return int(row[0]) if row and row[0] is not None else 0

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
