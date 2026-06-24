"""SQL constants for the SQLite dialect.

Conventions:

* Uses ``?`` positional placeholders (SQLite / DB-API).
* Booleans stored as ``INTEGER`` (``0``/``1``) with explicit comparisons.
* Datetimes stored as ``TEXT`` in ISO-8601 UTC; the SQLite backend
  formats and parses these at the adapter boundary.
* UUIDs stored as ``TEXT`` (the canonical 36-char form). The SQLite
  backend converts at the adapter boundary.

The cap-check formula in :data:`SPEND_QUERY` matches the canonical
formula in ``snipz-protocol.md`` §7. Any change here MUST be mirrored
in the Postgres dialect and re-verified against the conformance suite.
"""

from __future__ import annotations

from typing import Final

LIMIT_LOOKUP: Final = """
    SELECT cap_cents, grace_pct
      FROM snipz_limits
     WHERE scope_type = ? AND scope_id = ? AND window = ? AND enabled = 1
"""


# State-aware cap-check sum.
#
# Committed rows count at ``actual_cents`` (the truth).
# Reserved rows count at ``MAX(actual, estimated)`` so streaming overruns
# are visible to the next request.
#
# A flat ``MAX`` over both states would over-count committed rows that
# came in under estimate. Documented in decision log entry 11.
SPEND_QUERY: Final = """
    SELECT COALESCE(SUM(
        CASE
            WHEN state = 'committed' THEN actual_cents
            WHEN state = 'reserved'  THEN MAX(COALESCE(actual_cents, 0), estimated_cents)
        END
    ), 0)
      FROM snipz_ledger
     WHERE scope_type = ? AND scope_id = ?
       AND state IN ('reserved', 'committed')
       AND created_at >= ?
"""


INSERT_LEDGER: Final = """
    INSERT INTO snipz_ledger (
        id, reservation_id, scope_type, scope_id, state, late,
        estimated_cents, actual_cents, model, provider,
        request_id, expires_at
    ) VALUES (?, ?, ?, ?, 'reserved', 0, ?, NULL, ?, ?, ?, ?)
"""


FIND_BY_REQUEST_ID: Final = """
    SELECT id, reservation_id, scope_type, scope_id, state, late,
           estimated_cents, actual_cents, request_id, expires_at, created_at
      FROM snipz_ledger
     WHERE request_id = ?
"""


FIND_BY_RESERVATION_ID: Final = """
    SELECT id, reservation_id, scope_type, scope_id, state, late,
           estimated_cents, actual_cents, request_id, expires_at, created_at
      FROM snipz_ledger
     WHERE reservation_id = ?
"""


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


UPSERT_LIMIT: Final = """
    INSERT INTO snipz_limits (scope_type, scope_id, window, cap_cents, grace_pct)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(scope_type, scope_id, window) DO UPDATE SET
        cap_cents = excluded.cap_cents,
        grace_pct = excluded.grace_pct,
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
           cache_read_cents_per_m, cache_write_cents_per_m
      FROM (
          SELECT provider, model,
                 input_cents_per_m, output_cents_per_m,
                 cache_read_cents_per_m, cache_write_cents_per_m,
                 ROW_NUMBER() OVER (
                     PARTITION BY provider, model ORDER BY valid_from DESC
                 ) AS rn
            FROM snipz_pricing
      ) ranked
     WHERE rn = 1
"""
