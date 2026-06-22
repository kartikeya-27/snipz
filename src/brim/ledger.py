"""Cap arithmetic and window helpers.

Pure functions used by :mod:`brim.core`. All SQL is dispatched through
:class:`brim.storage.LedgerConnection`; the per-dialect SQL constants
live in :mod:`brim.storage.sql`. This module deliberately holds no
dialect or driver knowledge — it is safe to call from any backend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from brim.storage import LimitRow

__all__ = [
    "cap_with_grace",
    "compute_expires_at",
    "new_uuid",
    "window_start",
]


def window_start(window: str, now: datetime) -> datetime:
    """Return the inclusive start of the current window in UTC.

    Calendar boundaries: ``minute`` resets at second 0, ``hour`` at
    minute 0, ``day`` at UTC midnight, ``month`` on the first of the
    month at UTC midnight. ``lifetime`` returns the epoch.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if window == "minute":
        return now.replace(second=0, microsecond=0)
    if window == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if window == "lifetime":
        return datetime.min.replace(tzinfo=UTC)
    raise ValueError(f"unknown window: {window!r}")


def cap_with_grace(limit: LimitRow) -> Decimal:
    """Effective cap = ``cap_cents * (1 + grace_pct / 100)``."""
    if limit.grace_pct == 0:
        return limit.cap_cents
    return limit.cap_cents * (Decimal(100 + limit.grace_pct) / Decimal(100))


def compute_expires_at(now: datetime, ttl_seconds: int) -> datetime:
    """Return the absolute expiration time for a TTL-bound reservation."""
    return now + timedelta(seconds=ttl_seconds)


def new_uuid() -> UUID:
    """Return a fresh UUIDv4. Wrapped so tests can monkey-patch one entry point."""
    return uuid4()
