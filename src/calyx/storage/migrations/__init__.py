"""Versioned schema migrations.

Migrations are raw SQL files numbered ``NNNN_description.sql``. The
``calyx_schema_version`` table tracks the highest applied version.
The migration runner is part of :mod:`calyx.storage`.
"""

from __future__ import annotations
