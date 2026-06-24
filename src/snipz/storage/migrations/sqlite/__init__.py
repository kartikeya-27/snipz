"""SQLite-dialect migration files.

Numbered ``NNNN_<name>.sql`` files in this package are discovered and
applied in sort order by :class:`snipz.storage.sqlite.SqliteBackend.migrate`.
The Postgres dialect lives in the sibling ``postgres/`` package and stays
in lockstep on schema versions.
"""

from __future__ import annotations
