"""Smoke tests for the Snipz package scaffold.

These verify that the package imports, package data is shipped, and the
Phase 0 schema applies cleanly to a fresh SQLite database. Real reservation
logic is covered by tests added in Phase 1.
"""

from __future__ import annotations

import sqlite3
import tomllib
from importlib.resources import files
from pathlib import Path

import snipz


def test_version_is_set() -> None:
    """``snipz.__version__`` MUST match ``pyproject.toml``'s ``version``.

    Drift between these two slipped through during the v0.1.0 release
    prep: the wheel installed as 0.1.0 while ``__version__`` still read
    0.0.1. This test pins both to the same source of truth so the
    failure mode never recurs.
    """
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert snipz.__version__ == pyproject["project"]["version"]


_SQLITE_MIGRATIONS = "snipz.storage.migrations.sqlite"


def test_initial_migration_applies_cleanly() -> None:
    """0001_initial.sql must apply to an empty SQLite database without error."""
    sql = files(_SQLITE_MIGRATIONS).joinpath("0001_initial.sql").read_text()
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql)
        cur = conn.execute("SELECT version FROM snipz_schema_version")
        assert cur.fetchone() == (1,)
    finally:
        conn.close()


def test_initial_migration_creates_expected_tables() -> None:
    """The four core tables must exist after the migration."""
    sql = files(_SQLITE_MIGRATIONS).joinpath("0001_initial.sql").read_text()
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'snipz_%' ORDER BY name"
        )
        tables = [row[0] for row in cur.fetchall()]
        assert tables == ["snipz_ledger", "snipz_limits", "snipz_pricing", "snipz_schema_version"]
    finally:
        conn.close()
