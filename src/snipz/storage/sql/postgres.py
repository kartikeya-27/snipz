"""SQL constants for the PostgreSQL dialect.

Conventions:

* Uses ``$1, $2, ...`` positional placeholders (asyncpg / libpq).
* Booleans use native ``TRUE`` / ``FALSE`` literals.
* Datetimes, decimals, and UUIDs round-trip natively through asyncpg's
  type codecs — no string conversion at the Python boundary.
* ``GREATEST`` is the scalar max function (PostgreSQL); SQLite's
  equivalent is ``MAX(a, b)``. The semantics match for the cap-check use
  case because both ignore a NULL second argument when wrapped in
  ``COALESCE``, which is how the formula is written.

Cap checks read running per-window totals from ``snipz_limits``; the
canonical spend formula (``snipz-protocol.md`` §7) is applied in
:mod:`snipz.storage.spend`. Any change here MUST be mirrored in the
SQLite dialect.

Locking: every ledger write takes, per scope in sorted
``(scope_type, scope_id)`` order, the scope's shared advisory lock and
then all of the scope's limit rows ``FOR UPDATE`` — before touching
ledger rows. ``upsert_limit`` takes the advisory lock exclusively. One
global order means concurrent writers cannot deadlock.
"""

from __future__ import annotations

from typing import Final

#
# Note: ``window`` is a reserved keyword in PostgreSQL (window functions).
# We quote it as ``"window"`` everywhere the column is referenced by name.
# The column name itself is identical to the SQLite dialect; only the
# parsing/quoting differs.

LIMIT_LOOKUP: Final = """
    SELECT cap_cents, grace_pct
      FROM snipz_limits
     WHERE scope_type = $1 AND scope_id = $2 AND "window" = $3 AND enabled = TRUE
"""


# Advisory lock keyed by scope. Shared by ledger writes (they do not block
# each other), exclusive for upsert_limit, so a limit never changes while
# a write on its scope is in flight. chr(31) separates type from id.
SCOPE_LOCK_SHARED: Final = (
    "SELECT pg_advisory_xact_lock_shared(hashtextextended($1 || chr(31) || $2, 0))"
)
SCOPE_LOCK_EXCLUSIVE: Final = (
    "SELECT pg_advisory_xact_lock(hashtextextended($1 || chr(31) || $2, 0))"
)


# Every limit row of a scope, locked in window order. Their running
# totals all move when a ledger row of the scope changes.
LOCK_SCOPE_LIMITS: Final = """
    SELECT 1
      FROM snipz_limits
     WHERE scope_type = $1 AND scope_id = $2
     ORDER BY "window"
       FOR UPDATE
"""


# Running totals for every window of one scope (see snipz.storage.spend).
WINDOW_TOTALS: Final = """
    SELECT "window", window_start_at, window_spend_micros
      FROM snipz_limits
     WHERE scope_type = $1 AND scope_id = $2
"""


STORE_WINDOW_TOTAL: Final = """
    UPDATE snipz_limits
       SET window_start_at = $1, window_spend_micros = $2
     WHERE scope_type = $3 AND scope_id = $4 AND "window" = $5
"""


# A scope's spend since a window start, in micro-cents — rebuilds a
# running total. The canonical formula (committed at actual, reserved at
# GREATEST(actual, estimated)), rounded per row to whole micro-cents
# exactly as snipz.storage.spend.contribution_micros does; a test pins the
# two together. Served by idx_ledger_scope_window.
SCOPE_SPEND_MICROS_SINCE: Final = """
    SELECT COALESCE(SUM(ROUND(
        CASE
            WHEN state = 'committed' THEN actual_cents
            ELSE GREATEST(COALESCE(actual_cents, 0), estimated_cents)
        END * 1000000)), 0)::BIGINT
      FROM snipz_ledger
     WHERE scope_type = $1 AND scope_id = $2
       AND state IN ('reserved', 'committed')
       AND created_at >= $3
"""


_LEDGER_COLUMNS = """
    id, reservation_id, scope_type, scope_id, state, late,
    estimated_cents, actual_cents, request_id, expires_at, created_at
"""


RESERVATION_SCOPES: Final = """
    SELECT DISTINCT scope_type, scope_id
      FROM snipz_ledger
     WHERE reservation_id = $1
     ORDER BY scope_type, scope_id
"""


RESERVATION_ROWS: Final = f"""
    SELECT {_LEDGER_COLUMNS} FROM snipz_ledger WHERE reservation_id = $1
"""  # noqa: S608 — constant column list, no user input


RESERVATION_ROWS_FOR_UPDATE: Final = f"""
    SELECT {_LEDGER_COLUMNS} FROM snipz_ledger WHERE reservation_id = $1 FOR UPDATE
"""  # noqa: S608


EXPIRED_SCOPES: Final = """
    SELECT DISTINCT scope_type, scope_id
      FROM snipz_ledger
     WHERE state = 'reserved' AND expires_at < $1
     ORDER BY scope_type, scope_id
"""


# Expired rows of the scopes the sweep has locked ($2 / $3: parallel
# arrays of scope_type / scope_id). Rows of other scopes wait for the
# next sweep rather than being locked out of order.
EXPIRED_ROWS_FOR_UPDATE: Final = f"""
    SELECT {_LEDGER_COLUMNS}
      FROM snipz_ledger
     WHERE state = 'reserved' AND expires_at < $1
       AND (scope_type, scope_id) IN (SELECT * FROM unnest($2::text[], $3::text[]))
       FOR UPDATE
"""  # noqa: S608


RELEASE_EXPIRED_ROWS: Final = """
    UPDATE snipz_ledger
       SET state = 'released', late = TRUE, settled_at = $1
     WHERE id = ANY($2::uuid[]) AND state = 'reserved'
"""


INSERT_LEDGER: Final = """
    INSERT INTO snipz_ledger (
        id, reservation_id, scope_type, scope_id, state, late,
        estimated_cents, actual_cents, model, provider,
        request_id, expires_at, created_at
    ) VALUES ($1, $2, $3, $4, 'reserved', FALSE, $5, NULL, $6, $7, $8, $9, $10)
"""


FIND_BY_REQUEST_ID: Final = f"""
    SELECT {_LEDGER_COLUMNS} FROM snipz_ledger WHERE request_id = $1
"""  # noqa: S608


OBSERVE_UPDATE: Final = """
    UPDATE snipz_ledger
       SET actual_cents = $1
     WHERE reservation_id = $2 AND state = 'reserved'
"""


COMMIT_NORMAL: Final = """
    UPDATE snipz_ledger
       SET state = 'committed', actual_cents = $1, settled_at = $2
     WHERE reservation_id = $3 AND state = 'reserved'
"""


COMMIT_LATE: Final = """
    UPDATE snipz_ledger
       SET state = 'committed', actual_cents = $1, settled_at = $2, late = TRUE
     WHERE reservation_id = $3 AND state = 'released' AND late = TRUE
"""


RELEASE_BY_CALLER: Final = """
    UPDATE snipz_ledger
       SET state = 'released', settled_at = $1
     WHERE reservation_id = $2 AND state = 'reserved'
"""


# A new or changed limit starts with an unknown running total
# (window_start_at NULL), rebuilt from the ledger on first use.
UPSERT_LIMIT: Final = """
    INSERT INTO snipz_limits (scope_type, scope_id, "window", cap_cents, grace_pct)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (scope_type, scope_id, "window") DO UPDATE SET
        cap_cents = EXCLUDED.cap_cents,
        grace_pct = EXCLUDED.grace_pct,
        window_start_at = NULL,
        window_spend_micros = 0,
        updated_at = NOW()
"""


# Used at the start of each transaction to bound how long a contended
# cap-check waits on the limit-row lock. ``set_config`` accepts the
# value as a parameter, so we never concatenate the timeout into SQL.
SET_LOCK_TIMEOUT: Final = "SELECT pg_catalog.set_config('lock_timeout', $1, true)"


SCHEMA_VERSION_TABLE_EXISTS: Final = """
    SELECT 1 FROM information_schema.tables
     WHERE table_schema = current_schema() AND table_name = 'snipz_schema_version'
"""


SCHEMA_VERSION_MAX: Final = "SELECT MAX(version) FROM snipz_schema_version"


# Latest pricing row per (provider, model). Time-versioning is deferred;
# we always pick the most recent ``valid_from``. The same window-function
# form works in SQLite; only the placeholder style differs (this dialect
# has no parameters in the query so it is identical to the SQLite version).
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
