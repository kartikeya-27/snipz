"""Calyx — LLM cost reservation ledger.

Pre-flight reserve, commit on success, release on failure. Embedded library,
Postgres-first, transactional under concurrent load. See ``architecture.md``
for the design spec and ``calyx.md`` for positioning and the build plan.
"""

from __future__ import annotations

__version__ = "0.0.1"
__all__ = ["__version__"]
