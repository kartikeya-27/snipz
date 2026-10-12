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
concurrent cap checks and the running spend totals they read.
``lock_scope`` on this backend is therefore a no-op.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final
from uuid import UUID

import aiosqlite

from snipz.ledger import window_start
from snipz.storage import (
    CommitOutcome,
    LedgerRow,
    LimitRow,
    PricingRow,
    RequestIdConflictError,
    spend,
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

    async def lock_scope(self, *, scope_type: str, scope_id: str) -> None:
        # SQLite has no row-level locks. ``BEGIN IMMEDIATE`` (issued by
        # ``SqliteBackend.transaction``) already holds the database
        # writer lock, so every writer on every scope is serialized.
        del scope_type, scope_id

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
        window: str,
        now: datetime,
    ) -> Decimal:
        total = next(
            t for t in await self._window_totals(scope_type, scope_id) if t.window == window
        )
        spent = spend.spent_in_window(total, now)
        if spent is not None:
            return spent
        return await self._rebuild_total(total, now)

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
        created_at: datetime,
    ) -> None:
        estimated = spend.quantize_cents(estimated_cents)
        try:
            await self._conn.execute(
                sql.INSERT_LEDGER,
                (
                    str(row_id),
                    str(reservation_id),
                    scope_type,
                    scope_id,
                    estimated,
                    model,
                    provider,
                    request_id,
                    _fmt_datetime(expires_at),
                    _fmt_datetime(created_at),
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
        inserted = LedgerRow(
            id=row_id,
            reservation_id=reservation_id,
            scope_type=scope_type,
            scope_id=scope_id,
            window="",
            state="reserved",
            late=False,
            estimated_cents=estimated,
            actual_cents=None,
            request_id=request_id,
            expires_at=expires_at,
            created_at=created_at,
        )
        await self._track(before={}, after={row_id: inserted})

    async def update_observation(
        self,
        reservation_id: UUID,
        actual_cents: Decimal,
    ) -> int:
        rid = str(reservation_id)
        before = await self._fetch_rows(sql.RESERVATION_ROWS, rid)
        count = await self._execute(
            sql.OBSERVE_UPDATE, spend.quantize_cents(actual_cents), rid
        )
        await self._track(before, await self._fetch_rows(sql.RESERVATION_ROWS, rid))
        return count

    async def commit_reservation(
        self,
        reservation_id: UUID,
        actual_cents: Decimal,
        *,
        now: datetime,
    ) -> CommitOutcome:
        settled_at = _fmt_datetime(now)
        rid = str(reservation_id)
        actual = spend.quantize_cents(actual_cents)
        before = await self._fetch_rows(sql.RESERVATION_ROWS, rid)

        count = await self._execute(sql.COMMIT_NORMAL, actual, settled_at, rid)
        was_late = False
        if count == 0:
            count = await self._execute(sql.COMMIT_LATE, actual, settled_at, rid)
            was_late = count > 0

        await self._track(before, await self._fetch_rows(sql.RESERVATION_ROWS, rid))
        return CommitOutcome(rows_affected=count, was_late=was_late)

    async def release_reservation(
        self,
        reservation_id: UUID,
        *,
        now: datetime,
    ) -> int:
        rid = str(reservation_id)
        before = await self._fetch_rows(sql.RESERVATION_ROWS, rid)
        count = await self._execute(sql.RELEASE_BY_CALLER, _fmt_datetime(now), rid)
        await self._track(before, await self._fetch_rows(sql.RESERVATION_ROWS, rid))
        return count

    async def sweep_expired(self, *, now: datetime) -> int:
        formatted = _fmt_datetime(now)
        # The writer lock is held, so the UPDATE below changes exactly the
        # rows selected here.
        expired = await self._fetch_rows(sql.EXPIRED_ROWS, formatted)
        count = await self._execute(sql.SWEEP_EXPIRED, formatted, formatted)
        released = {
            row_id: replace(row, state="released", late=True) for row_id, row in expired.items()
        }
        await self._track(before=expired, after=released)
        return count

    # -- queries --------------------------------------------------------------

    async def find_by_request_id(self, request_id: str) -> list[LedgerRow]:
        cur = await self._conn.execute(sql.FIND_BY_REQUEST_ID, (request_id,))
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

    # -- pricing --------------------------------------------------------------

    async def load_pricing(self) -> list[PricingRow]:
        cur = await self._conn.execute(sql.LATEST_PRICING)
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        return [_row_to_pricing(r) for r in rows]

    # -- running totals -------------------------------------------------------

    async def _window_totals(self, scope_type: str, scope_id: str) -> list[spend.WindowTotal]:
        cur = await self._conn.execute(sql.WINDOW_TOTALS, (scope_type, scope_id))
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        return [
            spend.WindowTotal(
                scope_type=scope_type,
                scope_id=scope_id,
                window=str(row[0]),
                window_start_at=_parse_datetime(str(row[1])) if row[1] is not None else None,
                spent_micros=int(row[2]),
            )
            for row in rows
        ]

    async def _store_totals(self, totals: Iterable[spend.WindowTotal]) -> None:
        for total in totals:
            start = total.window_start_at
            await self._conn.execute(
                sql.STORE_WINDOW_TOTAL,
                (
                    _fmt_datetime(start) if start is not None else None,
                    total.spent_micros,
                    total.scope_type,
                    total.scope_id,
                    total.window,
                ),
            )

    async def _rebuild_total(self, total: spend.WindowTotal, now: datetime) -> Decimal:
        """Recompute ``total`` for the window containing ``now`` and store it."""
        start = window_start(total.window, now)
        cur = await self._conn.execute(
            sql.SCOPE_SPEND_MICROS_SINCE,
            (total.scope_type, total.scope_id, _fmt_datetime(start)),
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        micros = int(row[0]) if row is not None else 0
        await self._store_totals([replace(total, window_start_at=start, spent_micros=micros)])
        return spend.from_micros(micros)

    async def _track(self, before: dict[UUID, LedgerRow], after: dict[UUID, LedgerRow]) -> None:
        """Fold a write's row changes into the affected scopes' totals."""
        rows = (*before.values(), *after.values())
        scopes = sorted({(row.scope_type, row.scope_id) for row in rows})
        totals = [t for scope in scopes for t in await self._window_totals(*scope)]
        await self._store_totals(spend.apply_row_changes(totals, before, after))

    async def _fetch_rows(self, query: str, *params: object) -> dict[UUID, LedgerRow]:
        cur = await self._conn.execute(query, params)
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        return {row.id: row for row in map(_row_to_ledger, rows)}

    async def _execute(self, query: str, *params: object) -> int:
        cur = await self._conn.execute(query, params)
        try:
            return cur.rowcount
        finally:
            await cur.close()


def _row_to_pricing(row: Sequence[object]) -> PricingRow:
    return PricingRow(
        provider=str(row[0]),
        model=str(row[1]),
        input_cents_per_m=Decimal(str(row[2])),
        output_cents_per_m=Decimal(str(row[3])),
        cache_read_cents_per_m=Decimal(str(row[4])) if row[4] is not None else None,
        cache_write_cents_per_m=Decimal(str(row[5])) if row[5] is not None else None,
        cache_write_1h_cents_per_m=Decimal(str(row[6])) if row[6] is not None else None,
        tiers_json=str(row[7]) if row[7] is not None else None,
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

    Writers in this process queue on an :class:`asyncio.Lock` and share
    one long-lived writer connection. SQLite admits one writer at a time
    anyway; queueing in-process keeps concurrent writers out of SQLite's
    polling busy handler (where, under a few hundred concurrent
    reservations, enough of them outlast ``busy_timeout`` to fail with
    ``database is locked``), and reusing the connection saves opening
    one per transaction. ``busy_timeout`` still governs contention with
    other processes sharing the file. Use one backend per event loop —
    the queue and the writer connection belong to the loop that first
    uses them.

    ``close()`` closes the writer connection; the backend reopens it on
    next use.
    """

    __slots__ = ("_db_path", "_write_queue", "_writer")

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._write_queue = asyncio.Lock()
        self._writer: aiosqlite.Connection | None = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqliteLedgerConnection]:
        """Open a transactional :class:`SqliteLedgerConnection`.

        Acquires the SQLite writer lock immediately via
        ``BEGIN IMMEDIATE`` so concurrent reservers serialize on the
        lock rather than racing each other's cap checks.
        """
        async with self._write_queue:
            conn = await self._writer_connection()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield SqliteLedgerConnection(conn)
                except BaseException:
                    await conn.execute("ROLLBACK")
                    raise
                await conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    # ROLLBACK or COMMIT itself failed: the connection's
                    # state is unknown, so never reuse it.
                    await self._discard_writer()
                raise

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
        async with self._write_queue:
            conn = await self._writer_connection()
            current = await self._current_schema_version(conn)
            for entry, _ in self._pending_migrations(current):
                migration_sql = entry.read_text(encoding="utf-8")
                await conn.executescript(migration_sql)

    async def close(self) -> None:
        """Close the writer connection. Safe to call repeatedly."""
        async with self._write_queue:
            await self._discard_writer()

    # -- internals ------------------------------------------------------------

    async def _writer_connection(self) -> aiosqlite.Connection:
        """The shared writer connection; caller holds ``_write_queue``."""
        if self._writer is None:
            self._writer = await self._open_raw()
        return self._writer

    async def _discard_writer(self) -> None:
        """Close and forget the writer connection; caller holds ``_write_queue``."""
        writer, self._writer = self._writer, None
        if writer is not None:
            await writer.close()

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
