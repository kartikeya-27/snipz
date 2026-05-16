"""Per-dialect SQL constants.

Each module in this package holds the canonical SQL for one storage
dialect. Templated SQL was rejected (see decision log entry 15) because
the cap-check formula is critical and explicit SQL is easier to audit
than rendered SQL.

Drift between dialects is caught by the conformance suite, which runs
the same fixtures against every backend.
"""

from __future__ import annotations
