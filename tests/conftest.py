"""Shared test fixtures and pytest hooks.

The default test run exercises the SQLite backend in-process. Postgres
tests are opt-in via ``pytest --postgres`` (requires Docker, asyncpg,
and testcontainers — all in the dev dependency group).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from snipz import Budget

# ---------------------------------------------------------------------------
# Pytest hooks: --postgres opt-in flag for tests marked @pytest.mark.postgres
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--postgres",
        action="store_true",
        default=False,
        help=(
            "Run Postgres integration tests. Requires Docker plus the "
            "`asyncpg` and `testcontainers[postgres]` dev dependencies."
        ),
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip Postgres-marked tests unless ``--postgres`` was passed."""
    if config.getoption("--postgres"):
        return
    skip_pg = pytest.mark.skip(
        reason="needs --postgres flag (requires Docker; run `pytest --postgres`)"
    )
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip_pg)


# ---------------------------------------------------------------------------
# SQLite fixtures (the default — used by every test that does not opt in)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def budget(tmp_path: Path) -> AsyncIterator[Budget]:
    """A migrated Budget instance backed by a per-test SQLite file."""
    db = tmp_path / "snipz.db"
    instance = Budget(db)
    await instance.migrate()
    try:
        yield instance
    finally:
        await instance.close()


class FrozenClock:
    """A controllable clock for deterministic time-based tests."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@pytest_asyncio.fixture
async def clocked_budget(tmp_path: Path) -> AsyncIterator[tuple[Budget, FrozenClock]]:
    """Budget with a frozen clock the test can advance."""
    clock = FrozenClock(datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC))
    db = tmp_path / "snipz.db"
    instance = Budget(db, now=clock)
    await instance.migrate()
    try:
        yield instance, clock
    finally:
        await instance.close()


# ---------------------------------------------------------------------------
# Postgres fixtures (only constructed when --postgres is passed)
# ---------------------------------------------------------------------------
#
# Each test gets its own Postgres container. Slow (~5-10s of container
# startup per test), but bulletproof: zero cross-test pollution. If
# this becomes painful in CI, the obvious optimization is a session-
# scoped container with one fresh database per test.


@pytest_asyncio.fixture
async def pg_budget() -> AsyncIterator[Budget]:
    """A migrated Budget instance backed by a fresh Postgres container."""
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{host}:{port}/{container.dbname}"
        )

        instance = Budget(dsn)
        await instance.migrate()
        try:
            yield instance
        finally:
            await instance.close()
    finally:
        container.stop()


@pytest_asyncio.fixture
async def clocked_pg_budget() -> AsyncIterator[tuple[Budget, FrozenClock]]:
    """Postgres-backed Budget with a frozen clock the test can advance."""
    from testcontainers.postgres import PostgresContainer

    clock = FrozenClock(datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC))
    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{host}:{port}/{container.dbname}"
        )

        instance = Budget(dsn, now=clock)
        await instance.migrate()
        try:
            yield instance, clock
        finally:
            await instance.close()
    finally:
        container.stop()
