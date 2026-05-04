"""SQLite backend.

For single-node deployments and tests. Uses ``BEGIN IMMEDIATE`` for
cap-check serialization, since SQLite has no ``SELECT FOR UPDATE``.
A ``Decimal`` adapter pair is registered so that ``NUMERIC`` columns
round-trip through Python's ``decimal.Decimal`` without precision loss.

Phase 1 populates this module.
"""

from __future__ import annotations
