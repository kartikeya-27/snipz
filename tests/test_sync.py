"""Tests for the experimental sync wrapper (:mod:`snipz.sync`).

These cover:

* The lifecycle paths (reserve, commit, release, observe, sweep,
  context-manager auto-commit / auto-release on exception).
* Idempotency under ``request_id``.
* The critical re-entrancy guard — calling the sync API from inside an
  async test raises ``RuntimeError`` rather than deadlocking.

The lifecycle subset is intentionally a parallel of ``test_phase1.py``
expressed against the sync API, to confirm the wrapper preserves every
async semantic.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from snipz.sync import Budget, BudgetExceededError, InvalidStateError, Scope


@pytest.fixture
def sync_budget(tmp_path: Path) -> Iterator[Budget]:
    """A migrated sync Budget instance backed by a per-test SQLite file."""
    db = tmp_path / "snipz.db"
    instance = Budget(db)
    instance.migrate()
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_sync_reserve_under_cap_succeeds(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("500"))

    r = sync_budget.reserve(scope, Decimal("100"))

    assert r.state == "reserved"
    assert r.estimated_cents == Decimal("100")
    assert r.actual_cents is None
    assert r.scopes == (scope,)


def test_sync_reserve_over_cap_raises(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("100"))

    with pytest.raises(BudgetExceededError) as excinfo:
        sync_budget.reserve(scope, Decimal("150"))

    assert excinfo.value.scope == scope
    assert excinfo.value.cap_cents == Decimal("100")
    assert excinfo.value.attempted_cents == Decimal("150")


def test_sync_commit_settles_reservation(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("500"))

    r = sync_budget.reserve(scope, Decimal("100"))
    r.commit(Decimal("80"))

    assert r.state == "committed"
    assert r.actual_cents == Decimal("80")


def test_sync_release_refunds_budget(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("100"))

    r = sync_budget.reserve(scope, Decimal("80"))
    r.release()
    assert r.state == "released"

    # Released budget is reclaimed — a second reserve at the cap succeeds.
    r2 = sync_budget.reserve(scope, Decimal("80"))
    assert r2.state == "reserved"


def test_sync_observe_updates_actual(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("500"))

    r = sync_budget.reserve(scope, Decimal("100"))
    r.observe(Decimal("75"))

    assert r.actual_cents == Decimal("75")


def test_sync_sweep_returns_count(sync_budget: Budget) -> None:
    # No reservations placed, nothing to sweep.
    assert sync_budget.sweep() == 0


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_sync_context_manager_auto_commits_on_success(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("500"))

    with sync_budget.reserve(scope, Decimal("100")) as r:
        r.observe(Decimal("90"))

    assert r.state == "committed"
    assert r.actual_cents == Decimal("90")


def test_sync_context_manager_auto_releases_on_exception(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("500"))

    class _SyntheticError(Exception):
        pass

    with pytest.raises(_SyntheticError), sync_budget.reserve(scope, Decimal("100")) as r:
        raise _SyntheticError("call failed")

    assert r.state == "released"


# ---------------------------------------------------------------------------
# Idempotency + invalid state
# ---------------------------------------------------------------------------


def test_sync_request_id_idempotency(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("500"))

    r1 = sync_budget.reserve(scope, Decimal("100"), request_id="req-abc")
    r2 = sync_budget.reserve(scope, Decimal("100"), request_id="req-abc")

    assert r1.id == r2.id
    assert r1.request_id == "req-abc"


def test_sync_commit_on_caller_released_raises(sync_budget: Budget) -> None:
    scope = Scope("user", "u1")
    sync_budget.set_limit(scope, Decimal("500"))

    r = sync_budget.reserve(scope, Decimal("100"))
    r.release()

    with pytest.raises(InvalidStateError):
        r.commit(Decimal("80"))


# ---------------------------------------------------------------------------
# Re-entrancy guard — the safety-critical test
# ---------------------------------------------------------------------------


async def test_sync_call_from_inside_event_loop_raises(tmp_path: Path) -> None:
    """Calling the sync API from inside an active event loop must raise.

    Without the guard, the sync wrapper would dispatch a coroutine to a
    background loop and block on ``Future.result()`` — but the calling
    coroutine is itself running on an event loop, so the block would
    deadlock indefinitely. The guard detects this and raises a clear
    ``RuntimeError`` instead.
    """
    db = tmp_path / "snipz.db"

    # Constructing the wrapper does not call _run, so no raise here.
    b = Budget(db)

    with pytest.raises(RuntimeError, match="active event loop"):
        b.migrate()
