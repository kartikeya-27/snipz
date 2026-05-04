"""Smoke tests for the Calyx package scaffold.

These verify that the package imports, package data is shipped, and the
Phase 0 schema applies cleanly to a fresh SQLite database. Real reservation
logic is covered by tests added in Phase 1.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files

import calyx


def test_version_is_set() -> None:
    assert calyx.__version__ == "0.0.1"


def test_initial_migration_applies_cleanly() -> None:
    """0001_initial.sql must apply to an empty SQLite database without error."""
    sql = files("calyx.storage.migrations").joinpath("0001_initial.sql").read_text()
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql)
        cur = conn.execute("SELECT version FROM calyx_schema_version")
        assert cur.fetchone() == (1,)
    finally:
        conn.close()


def test_initial_migration_creates_expected_tables() -> None:
    """The four core tables must exist after the migration."""
    sql = files("calyx.storage.migrations").joinpath("0001_initial.sql").read_text()
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'calyx_%' ORDER BY name"
        )
        tables = [row[0] for row in cur.fetchall()]
        assert tables == ["calyx_ledger", "calyx_limits", "calyx_pricing", "calyx_schema_version"]
    finally:
        conn.close()
