"""Read the ledger straight from the database to reconcile tests against it.

Bypasses the engine on purpose: an assertion about what was recorded
should not trust the code that did the recording. Amounts are summed as
``Decimal`` in Python — SQLite's ``SUM`` over NUMERIC would go through
floats.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import aiosqlite

from snipz.ledger import window_start

_QUERY = "SELECT scope_type, scope_id, state, actual_cents FROM snipz_ledger"

_LIMITS_QUERY = (
    'SELECT scope_type, scope_id, "window", window_start_at, window_spend_micros '
    "FROM snipz_limits WHERE window_start_at IS NOT NULL"
)
_ROWS_QUERY = (
    "SELECT scope_type, scope_id, state, estimated_cents, actual_cents, created_at "
    "FROM snipz_ledger"
)


@dataclass(frozen=True)
class LedgerAudit:
    rows_by_state: Counter[str]
    committed_by_scope: dict[tuple[str, str], Decimal]

    def committed(self, scope_type: str, scope_id: str) -> Decimal:
        return self.committed_by_scope.get((scope_type, scope_id), Decimal(0))


def _audit(rows: list[tuple[object, object, object, object]]) -> LedgerAudit:
    states: Counter[str] = Counter()
    committed: dict[tuple[str, str], Decimal] = {}
    for scope_type, scope_id, state, actual in rows:
        states[str(state)] += 1
        if state == "committed":
            key = (str(scope_type), str(scope_id))
            committed[key] = committed.get(key, Decimal(0)) + Decimal(str(actual))
    return LedgerAudit(rows_by_state=states, committed_by_scope=committed)


async def audit_sqlite(db_path: Path) -> LedgerAudit:
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(_QUERY)
        rows = await cur.fetchall()
    return _audit([tuple(r) for r in rows])


async def audit_postgres(dsn: str) -> LedgerAudit:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        records = await conn.fetch(_QUERY)
    finally:
        await conn.close()
    return _audit([tuple(r) for r in records])


# ---------------------------------------------------------------------------
# Running totals
# ---------------------------------------------------------------------------
#
# Re-derives every known running total from raw ledger rows with its own
# copy of the cap-check formula — deliberately not importing
# snipz.storage.spend, so a bug there cannot hide by checking itself.


@dataclass(frozen=True)
class TotalCheck:
    scope: tuple[str, str, str]
    window_start_at: datetime
    stored_micros: int
    expected_micros: int


def _expected_micros(state: str, estimated: Decimal, actual: Decimal | None) -> int:
    if state == "committed":
        amount = actual if actual is not None else Decimal(0)
    elif state == "reserved":
        amount = max(actual if actual is not None else Decimal(0), estimated)
    else:
        return 0
    return int(amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP) * 1_000_000)


def _check(
    limits: list[tuple[str, str, str, datetime, int]],
    rows: list[tuple[str, str, str, Decimal, Decimal | None, datetime]],
) -> list[TotalCheck]:
    checks = []
    for scope_type, scope_id, window, start, stored in limits:
        expected = sum(
            _expected_micros(state, estimated, actual)
            for row_type, row_id, state, estimated, actual, created_at in rows
            if (row_type, row_id) == (scope_type, scope_id)
            and window_start(window, created_at) == start
        )
        checks.append(TotalCheck((scope_type, scope_id, window), start, stored, expected))
    return checks


def _parse_sqlite_time(value: object) -> datetime:
    text = str(value)
    return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


async def check_totals_sqlite(db_path: Path) -> list[TotalCheck]:
    async with aiosqlite.connect(str(db_path)) as conn:
        limits = await (await conn.execute(_LIMITS_QUERY)).fetchall()
        rows = await (await conn.execute(_ROWS_QUERY)).fetchall()
    return _check(
        [(str(t), str(i), str(w), _parse_sqlite_time(s), int(m)) for t, i, w, s, m in limits],
        [
            (str(t), str(i), str(st), Decimal(str(e)), _decimal(a), _parse_sqlite_time(c))
            for t, i, st, e, a, c in rows
        ],
    )


async def check_totals_postgres(dsn: str) -> list[TotalCheck]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        limits = await conn.fetch(_LIMITS_QUERY)
        rows = await conn.fetch(_ROWS_QUERY)
    finally:
        await conn.close()
    # asyncpg returns year-1 timestamptz values (the lifetime window start)
    # naive; a naive/aware == is silently False, so normalize to UTC.
    return _check(
        [(t, i, w, _utc(start), m) for t, i, w, start, m in limits],
        [tuple(r) for r in rows],
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def invalidate_totals_sqlite(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("UPDATE snipz_limits SET window_start_at = NULL")
        await conn.commit()


async def invalidate_totals_postgres(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("UPDATE snipz_limits SET window_start_at = NULL")
    finally:
        await conn.close()
