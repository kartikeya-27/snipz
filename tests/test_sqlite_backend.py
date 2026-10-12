"""Tests for :class:`snipz.storage.sqlite.SqliteBackend` connection handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from snipz import Budget, Scope
from snipz.storage.sqlite import SqliteBackend


@pytest_asyncio.fixture
async def backend(tmp_path: Path) -> AsyncIterator[SqliteBackend]:
    instance = SqliteBackend(tmp_path / "snipz.db")
    await instance.migrate()
    try:
        yield instance
    finally:
        await instance.close()


async def _set_cap(backend: SqliteBackend, cap: str) -> None:
    async with backend.transaction() as conn:
        await conn.upsert_limit(
            scope_type="user", scope_id="u1", window="month", cap_cents=Decimal(cap), grace_pct=0
        )


async def test_concurrent_writers_queue_instead_of_hitting_busy_timeout(
    tmp_path: Path,
) -> None:
    """Regression: 300 concurrent reserve+commit pairs used to fail with
    ``database is locked`` once SQLite's busy handler outlasted
    ``busy_timeout``. Writers now queue in-process."""
    budget = Budget(tmp_path / "snipz.db")
    await budget.migrate()

    async def reserve_and_commit(i: int) -> None:
        reservation = await budget.reserve(Scope("user", f"u{i % 50}"), Decimal("1"))
        await reservation.commit(Decimal("1"))

    try:
        await asyncio.gather(*(reserve_and_commit(i) for i in range(300)))
    finally:
        await budget.close()


async def test_close_is_idempotent_and_backend_reopens_on_next_use(
    backend: SqliteBackend,
) -> None:
    await backend.close()
    await backend.close()

    await _set_cap(backend, "100")

    async with backend.connect() as conn:
        limit = await conn.fetch_limit(scope_type="user", scope_id="u1", window="month")
    assert limit is not None
    assert limit.cap_cents == Decimal("100")


async def test_failed_commit_discards_the_writer_connection(
    backend: SqliteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If COMMIT fails the connection is left mid-transaction; it must be
    dropped so the next transaction starts on a clean connection."""
    await _set_cap(backend, "100")
    writer = backend._writer
    assert writer is not None
    real_execute = writer.execute

    async def execute(sql: str, *args: Any) -> Any:
        if sql == "COMMIT":
            raise RuntimeError("disk I/O error")
        return await real_execute(sql, *args)

    monkeypatch.setattr(writer, "execute", execute)

    with pytest.raises(RuntimeError, match="disk I/O error"):
        await _set_cap(backend, "200")

    assert backend._writer is None
    await _set_cap(backend, "300")  # fresh connection; the failed write never landed
    async with backend.connect() as conn:
        limit = await conn.fetch_limit(scope_type="user", scope_id="u1", window="month")
    assert limit is not None
    assert limit.cap_cents == Decimal("300")


async def test_rolled_back_transaction_keeps_the_writer_connection(
    backend: SqliteBackend,
) -> None:
    """An exception inside the block rolls back cleanly; the connection is
    still healthy and is reused."""
    await _set_cap(backend, "100")
    writer = backend._writer

    with pytest.raises(ValueError, match="caller bug"):
        async with backend.transaction() as conn:
            await conn.upsert_limit(
                scope_type="user", scope_id="u1", window="month",
                cap_cents=Decimal("999"), grace_pct=0,
            )
            raise ValueError("caller bug")

    assert backend._writer is writer
    async with backend.connect() as conn:
        limit = await conn.fetch_limit(scope_type="user", scope_id="u1", window="month")
    assert limit is not None
    assert limit.cap_cents == Decimal("100")
