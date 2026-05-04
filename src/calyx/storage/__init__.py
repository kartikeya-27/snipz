"""Storage backends for Calyx.

The :class:`Backend` protocol is the extension point. v0 ships SQLite
(Phase 1) and Postgres (Phase 2). Additional backends can be added
without touching the core engine.
"""

from __future__ import annotations
