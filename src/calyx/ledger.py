"""Cap-check arithmetic and SQL transactions.

The cap-check formula uses a CASE expression on row state — ``actual_cents``
for committed rows, ``GREATEST(actual, estimated)`` for reserved rows
(decision log entry #11 in ``architecture.md``).

Idempotent reserves follow the SELECT-then-INSERT-with-conflict-recovery
flow described in ``architecture.md`` (decision log entry #12).

Phase 1 populates this module.
"""

from __future__ import annotations
