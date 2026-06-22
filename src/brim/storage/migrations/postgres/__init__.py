"""PostgreSQL-dialect migration files.

Numbered ``NNNN_<name>.sql`` files in this package are discovered and
applied in sort order by :class:`brim.storage.postgres.PostgresBackend.migrate`.
The SQLite dialect lives in the sibling ``sqlite/`` package and stays
in lockstep on schema versions.
"""

from __future__ import annotations
