"""SQL constants for the SQLite dialect.

Conventions:

* Uses ``?`` positional placeholders (SQLite / DB-API).
* Booleans stored as ``INTEGER`` (``0``/``1``) with explicit comparisons.
* Datetimes stored as ``TEXT`` in ISO-8601 UTC; the SQLite backend
  formats and parses these at the adapter boundary.
* UUIDs stored as ``TEXT`` (the canonical 36-char form). The SQLite
  backend converts at the adapter boundary.

Cap checks read running per-window totals from ``snipz_limits``; the
canonical spend formula (``snipz-protocol.md`` §7) is applied in
:mod:`snipz.storage.spend`. Any change here MUST be mirrored in the
Postgres dialect.
"""

from __future__ import annotations

from typing import Final

LIMIT_LOOKUP: Final = """
    SELECT cap_cents, grace_pct
      FROM snipz_limits
     WHERE scope_type = ? AND scope_id = ? AND window = ? AND enabled = 1
"""


# Running totals for every window of one scope (see snipz.storage.spend).
WINDOW_TOTALS: Final = """
    SELECT window, window_start_at, window_spend_micros
      FROM snipz_limits
     WHERE scope_type = ? AND scope_id = ?
"""


STORE_WINDOW_TOTAL: Final = """
    UPDATE snipz_limits
       SET window_start_at = ?, window_spend_micros = ?
     WHERE scope_type = ? AND scope_id = ? AND window = ?
"""


_LEDGER_COLUMNS = """
    id, reservation_id, scope_type, scope_id, state, late,
    estimated_cents, actual_cents, request_id, expires_at, created_at
"""


# A scope's spend since a window start, in micro-cents — rebuilds a
# running total. The canonical formula (committed at actual, reserved at
# MAX(actual, estimated)), rounded per row to whole micro-cents exactly as
# snipz.storage.spend.contribution_micros does; a test pins the two
# together. Served by idx_ledger_scope_window.
SCOPE_SPEND_MICROS_SINCE: Final = """
    SELECT COALESCE(SUM(CAST(ROUND(
        CASE
            WHEN state = 'committed' THEN actual_cents
            ELSE MAX(COALESCE(actual_cents, 0), estimated_cents)
        END * 1000000) AS INTEGER)), 0)
      FROM snipz_ledger
     WHERE scope_type = ? AND scope_id = ?
       AND state IN ('reserved', 'committed')
       AND created_at >= ?
"""


RESERVATION_ROWS: Final = f"""
    SELECT {_LEDGER_COLUMNS} FROM snipz_ledger WHERE reservation_id = ?
"""  # noqa: S608


EXPIRED_ROWS: Final = f"""
    SELECT {_LEDGER_COLUMNS}
      FROM snipz_ledger
     WHERE state = 'reserved' AND expires_at < ?
"""  # noqa: S608


INSERT_LEDGER: Final = """
    INSERT INTO snipz_ledger (
        id, reservation_id, scope_type, scope_id, state, late,
        estimated_cents, actual_cents, model, provider,
        request_id, expires_at, created_at
    ) VALUES (?, ?, ?, ?, 'reserved', 0, ?, NULL, ?, ?, ?, ?, ?)
"""


FIND_BY_REQUEST_ID: Final = f"""
    SELECT {_LEDGER_COLUMNS} FROM snipz_ledger WHERE request_id = ?
"""  # noqa: S608


OBSERVE_UPDATE: Final = """
    UPDATE snipz_ledger
       SET actual_cents = ?
     WHERE reservation_id = ? AND state = 'reserved'
"""


COMMIT_NORMAL: Final = """
    UPDATE snipz_ledger
       SET state = 'committed', actual_cents = ?, settled_at = ?
     WHERE reservation_id = ? AND state = 'reserved'
"""


COMMIT_LATE: Final = """
    UPDATE snipz_ledger
       SET state = 'committed', actual_cents = ?, settled_at = ?, late = 1
     WHERE reservation_id = ? AND state = 'released' AND late = 1
"""


RELEASE_BY_CALLER: Final = """
    UPDATE snipz_ledger
       SET state = 'released', settled_at = ?
     WHERE reservation_id = ? AND state = 'reserved'
"""


SWEEP_EXPIRED: Final = """
    UPDATE snipz_ledger
       SET state = 'released', late = 1, settled_at = ?
     WHERE state = 'reserved' AND expires_at < ?
"""


# A new or changed limit starts with an unknown running total
# (window_start_at NULL), rebuilt from the ledger on first use.
UPSERT_LIMIT: Final = """
    INSERT INTO snipz_limits (scope_type, scope_id, window, cap_cents, grace_pct)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(scope_type, scope_id, window) DO UPDATE SET
        cap_cents = excluded.cap_cents,
        grace_pct = excluded.grace_pct,
        window_start_at = NULL,
        window_spend_micros = 0,
        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
"""


SCHEMA_VERSION_TABLE_EXISTS: Final = (
    "SELECT name FROM sqlite_master "
    "WHERE type = 'table' AND name = 'snipz_schema_version'"
)


SCHEMA_VERSION_MAX: Final = "SELECT MAX(version) FROM snipz_schema_version"


# Latest pricing row per (provider, model). Time-versioning is deferred;
# we always pick the most recent ``valid_from``. SQLite supports window
# functions since 3.25, so the same query works in both dialects.
LATEST_PRICING: Final = """
    SELECT provider, model,
           input_cents_per_m, output_cents_per_m,
           cache_read_cents_per_m, cache_write_cents_per_m,
           cache_write_1h_cents_per_m, tiers
      FROM (
          SELECT provider, model,
                 input_cents_per_m, output_cents_per_m,
                 cache_read_cents_per_m, cache_write_cents_per_m,
                 cache_write_1h_cents_per_m, tiers,
                 ROW_NUMBER() OVER (
                     PARTITION BY provider, model ORDER BY valid_from DESC
                 ) AS rn
            FROM snipz_pricing
      ) ranked
     WHERE rn = 1
"""
