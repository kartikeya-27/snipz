"""Running per-window spend totals — the dialect-neutral arithmetic.

A cap check needs one number: a scope's spend in the current window.
Summing the ledger for it costs time proportional to the scope's
traffic, so each ``snipz_limits`` row instead carries a running total
for the window it is in — ``window_start_at`` and
``window_spend_micros``. Backends keep it current inside the same
transaction as every ledger write; this module holds the rules.

* **Contribution.** A ledger row adds to its scope's spend exactly as
  the canonical cap-check formula counts it: committed rows at
  ``actual_cents``, reserved rows at ``MAX(actual, estimated)``,
  released rows at zero.
* **Micro-cents.** Amounts are stored rounded to 6 decimal places
  (:func:`quantize_cents`) and totals are integer micro-cents, so a
  total is the exact sum of its rows however many times it is updated.
* **Buckets.** A row belongs to the window bucket containing its
  ``created_at``. A total only absorbs changes to rows in its bucket;
  a new row in a later bucket rolls the total over to that bucket.
* **Unknown totals.** ``window_start_at = NULL`` means "rebuild from
  the ledger before use". Set by migration, by ``set_limit``, and
  whenever a change cannot be placed — the total then heals itself on
  its next read instead of drifting.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from uuid import UUID

from snipz.ledger import window_start
from snipz.storage import LedgerRow

__all__ = [
    "WindowTotal",
    "apply_row_changes",
    "contribution_micros",
    "from_micros",
    "quantize_cents",
    "spent_in_window",
]

_QUANTUM: Final = Decimal("0.000001")
_MICROS_PER_CENT: Final = 1_000_000


def quantize_cents(cents: Decimal) -> Decimal:
    """Round an amount to the stored precision, 6 decimal places.

    Matches Postgres ``NUMERIC(20, 6)`` rounding, so every backend
    stores — and later reads back — exactly this value.
    """
    return cents.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def _to_micros(cents: Decimal) -> int:
    return int(quantize_cents(cents) * _MICROS_PER_CENT)


def from_micros(micros: int) -> Decimal:
    return Decimal(micros) / _MICROS_PER_CENT


def contribution_micros(row: LedgerRow | None) -> int:
    """What ``row`` adds to its scope's spend, in micro-cents."""
    if row is None or row.state == "released":
        return 0
    if row.state == "committed":
        return _to_micros(row.actual_cents if row.actual_cents is not None else Decimal(0))
    actual = row.actual_cents if row.actual_cents is not None else Decimal(0)
    return _to_micros(max(actual, row.estimated_cents))


@dataclass(frozen=True, slots=True)
class WindowTotal:
    """One limit row's running total. ``window_start_at=None`` = unknown."""

    scope_type: str
    scope_id: str
    window: str
    window_start_at: datetime | None
    spent_micros: int


def spent_in_window(total: WindowTotal, now: datetime) -> Decimal | None:
    """Spend in the window containing ``now``, or ``None`` to rebuild.

    A total from an earlier bucket means nothing has been spent since the
    window rolled over. A total that is unknown, or from a later bucket
    (another writer's clock is ahead), cannot be trusted.
    """
    if total.window_start_at is None:
        return None
    current = window_start(total.window, now)
    if total.window_start_at == current:
        return from_micros(total.spent_micros)
    if total.window_start_at < current:
        return Decimal(0)
    return None


def apply_row_changes(
    totals: Sequence[WindowTotal],
    before: Mapping[UUID, LedgerRow],
    after: Mapping[UUID, LedgerRow],
) -> list[WindowTotal]:
    """Fold ledger-row changes into ``totals``; return the totals that changed.

    ``before`` and ``after`` map row id to the row as read before and
    after a write (absent = did not exist). Each total absorbs the
    contribution change of the rows in its own scope.
    """
    current = {(t.scope_type, t.scope_id, t.window): t for t in totals}
    changed: set[tuple[str, str, str]] = set()
    for row_id in sorted(before.keys() | after.keys()):
        old, new = before.get(row_id), after.get(row_id)
        delta = contribution_micros(new) - contribution_micros(old)
        if delta == 0:
            continue
        row = new if new is not None else old
        if row is None:
            continue
        for key, total in list(current.items()):
            if (total.scope_type, total.scope_id) != (row.scope_type, row.scope_id):
                continue
            updated = _absorb(total, row.created_at, delta, inserted=old is None)
            if updated != total:
                current[key] = updated
                changed.add(key)
    return [current[key] for key in sorted(changed)]


def _absorb(total: WindowTotal, created_at: datetime, delta: int, *, inserted: bool) -> WindowTotal:
    if total.window_start_at is None:
        return total  # unknown; rebuilt on next read
    bucket = window_start(total.window, created_at)
    if bucket == total.window_start_at:
        return replace(total, spent_micros=total.spent_micros + delta)
    if inserted and bucket > total.window_start_at:
        # First row of a new window: the total rolls over.
        return replace(total, window_start_at=bucket, spent_micros=delta)
    if not inserted and bucket < total.window_start_at:
        return total  # a row from an earlier window; not counted in this one
    # A change this total cannot place (e.g. clock skew between writers):
    # mark it unknown so the next read rebuilds it from the ledger.
    return replace(total, window_start_at=None, spent_micros=0)
