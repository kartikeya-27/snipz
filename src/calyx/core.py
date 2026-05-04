"""Reservation engine: ``Budget``, ``Reservation``, ``BudgetExceeded``.

This module owns the public API surface for reserving, committing, releasing,
and observing in-flight reservations. The SQL transactions that back these
operations live in :mod:`calyx.ledger`; storage adapters live in
:mod:`calyx.storage`.

Phase 1 populates this module. See ``architecture.md`` for the spec.
"""

from __future__ import annotations
