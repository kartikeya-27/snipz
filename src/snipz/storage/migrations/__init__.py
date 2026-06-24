"""Versioned schema migrations.

Per-dialect subpackages: ``sqlite/`` and ``postgres/``. Migrations are
raw SQL files numbered ``NNNN_description.sql``. Schema versions stay
in lockstep across dialects — a Postgres v3 implies a SQLite v3 (with a
no-op migration if the schema change does not apply).

The ``snipz_schema_version`` table tracks the highest applied version.
Each backend's ``migrate()`` method discovers and applies its own
dialect's pending files.
"""

from __future__ import annotations
