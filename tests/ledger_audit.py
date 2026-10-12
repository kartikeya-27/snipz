"""Read the ledger straight from the database to reconcile tests against it.

Bypasses the engine on purpose: an assertion about what was recorded
should not trust the code that did the recording. Amounts are summed as
``Decimal`` in Python — SQLite's ``SUM`` over NUMERIC would go through
floats.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiosqlite

_QUERY = "SELECT scope_type, scope_id, state, actual_cents FROM snipz_ledger"


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
