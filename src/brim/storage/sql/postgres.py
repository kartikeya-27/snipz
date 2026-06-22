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

The cap-check formula in :data:`SPEND_QUERY` matches the canonical
formula in ``brim-protocol.md`` §7. Any change here MUST be mirrored
in the SQLite dialect and re-verified against the conformance suite.
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
      FROM brim_limits
     WHERE scope_type = $1 AND scope_id = $2 AND "window" = $3 AND enabled = TRUE
"""


# Acquire a row-level write lock on the limit row before reading current
# spend, so concurrent reservers serialize cleanly. Returns 0 rows when
# no limit is configured for the scope (tracker mode); the caller treats
# that the same as a NULL fetch.
LOCK_LIMIT: Final = """
    SELECT 1
      FROM brim_limits
     WHERE scope_type = $1 AND scope_id = $2 AND "window" = $3 AND enabled = TRUE
     FOR UPDATE
"""


# State-aware cap-check sum.
#
# Committed rows count at ``actual_cents`` (the truth).
# Reserved rows count at ``GREATEST(actual, estimated)`` so streaming
# overruns are visible to the next request.
#
# A flat ``GREATEST`` over both states would over-count committed rows
# that came in under estimate. Documented in decision log entry 11.
SPEND_QUERY: Final = """
    SELECT COALESCE(SUM(
        CASE
            WHEN state = 'committed' THEN actual_cents
            WHEN state = 'reserved'  THEN GREATEST(COALESCE(actual_cents, 0), estimated_cents)
        END
    ), 0)
      FROM brim_ledger
     WHERE scope_type = $1 AND scope_id = $2
       AND state IN ('reserved', 'committed')
       AND created_at >= $3
"""


INSERT_LEDGER: Final = """
    INSERT INTO brim_ledger (
        id, reservation_id, scope_type, scope_id, state, late,
        estimated_cents, actual_cents, model, provider,
        request_id, expires_at
    ) VALUES ($1, $2, $3, $4, 'reserved', FALSE, $5, NULL, $6, $7, $8, $9)
"""


FIND_BY_REQUEST_ID: Final = """
    SELECT id, reservation_id, scope_type, scope_id, state, late,
           estimated_cents, actual_cents, request_id, expires_at, created_at
      FROM brim_ledger
     WHERE request_id = $1
"""


FIND_BY_RESERVATION_ID: Final = """
    SELECT id, reservation_id, scope_type, scope_id, state, late,
           estimated_cents, actual_cents, request_id, expires_at, created_at
      FROM brim_ledger
     WHERE reservation_id = $1
"""


OBSERVE_UPDATE: Final = """
    UPDATE brim_ledger
       SET actual_cents = $1
     WHERE reservation_id = $2 AND state = 'reserved'
"""


COMMIT_NORMAL: Final = """
    UPDATE brim_ledger
       SET state = 'committed', actual_cents = $1, settled_at = $2
     WHERE reservation_id = $3 AND state = 'reserved'
"""


COMMIT_LATE: Final = """
    UPDATE brim_ledger
       SET state = 'committed', actual_cents = $1, settled_at = $2, late = TRUE
     WHERE reservation_id = $3 AND state = 'released' AND late = TRUE
"""


RELEASE_BY_CALLER: Final = """
    UPDATE brim_ledger
       SET state = 'released', settled_at = $1
     WHERE reservation_id = $2 AND state = 'reserved'
"""


SWEEP_EXPIRED: Final = """
    UPDATE brim_ledger
       SET state = 'released', late = TRUE, settled_at = $1
     WHERE state = 'reserved' AND expires_at < $2
"""


UPSERT_LIMIT: Final = """
    INSERT INTO brim_limits (scope_type, scope_id, "window", cap_cents, grace_pct)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (scope_type, scope_id, "window") DO UPDATE SET
        cap_cents = EXCLUDED.cap_cents,
        grace_pct = EXCLUDED.grace_pct,
        updated_at = NOW()
"""


# Used at the start of each transaction to bound how long a contended
# cap-check waits on the limit-row lock. ``set_config`` accepts the
# value as a parameter, so we never concatenate the timeout into SQL.
SET_LOCK_TIMEOUT: Final = "SELECT pg_catalog.set_config('lock_timeout', $1, true)"


SCHEMA_VERSION_TABLE_EXISTS: Final = """
    SELECT 1 FROM information_schema.tables
     WHERE table_schema = current_schema() AND table_name = 'brim_schema_version'
"""


SCHEMA_VERSION_MAX: Final = "SELECT MAX(version) FROM brim_schema_version"
