"""Unit tests for :mod:`snipz.storage.spend` — the running-total rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from snipz.storage import LedgerRow
from snipz.storage.spend import (
    WindowTotal,
    apply_row_changes,
    contribution_micros,
    quantize_cents,
    spent_in_window,
)

T0 = datetime(2026, 10, 12, 10, 30, tzinfo=UTC)
HOUR_10 = datetime(2026, 10, 12, 10, tzinfo=UTC)
HOUR_11 = HOUR_10 + timedelta(hours=1)


def _row(
    state: str = "reserved",
    estimated: str = "2",
    actual: str | None = None,
    *,
    created_at: datetime = T0,
    scope_id: str = "u1",
    row_id: UUID | None = None,
) -> LedgerRow:
    return LedgerRow(
        id=row_id or uuid4(),
        reservation_id=uuid4(),
        scope_type="user",
        scope_id=scope_id,
        window="",
        state=state,
        late=False,
        estimated_cents=Decimal(estimated),
        actual_cents=Decimal(actual) if actual is not None else None,
        request_id=None,
        expires_at=created_at + timedelta(minutes=5),
        created_at=created_at,
    )


def _total(start: datetime | None = HOUR_10, micros: int = 0, window: str = "hour") -> WindowTotal:
    return WindowTotal("user", "u1", window, start, micros)


# ---------------------------------------------------------------------------
# Contribution — the canonical cap-check formula
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "micros"),
    [
        (_row("reserved", "2"), 2_000_000),
        (_row("reserved", "2", "3.5"), 3_500_000),  # streaming overrun counts
        (_row("reserved", "2", "0.5"), 2_000_000),  # never below the estimate
        (_row("committed", "2", "0.5"), 500_000),  # the truth, once settled
        (_row("released", "2", "1"), 0),
        (None, 0),
    ],
)
def test_contribution_matches_cap_check_formula(row: LedgerRow | None, micros: int) -> None:
    assert contribution_micros(row) == micros


def test_quantize_rounds_half_up_to_six_places() -> None:
    assert quantize_cents(Decimal("0.0000125")) == Decimal("0.000013")
    assert quantize_cents(Decimal("1.05")) == Decimal("1.050000")


# ---------------------------------------------------------------------------
# Reading a total
# ---------------------------------------------------------------------------


def test_current_bucket_total_is_read_as_is() -> None:
    assert spent_in_window(_total(HOUR_10, 1_500_000), T0) == Decimal("1.5")


def test_total_from_an_earlier_window_reads_as_zero() -> None:
    assert spent_in_window(_total(HOUR_10, 1_500_000), HOUR_11) == Decimal(0)


@pytest.mark.parametrize("start", [None, HOUR_11], ids=["unknown", "future-bucket"])
def test_untrusted_total_must_be_rebuilt(start: datetime | None) -> None:
    assert spent_in_window(_total(start, 1_500_000), T0) is None


# ---------------------------------------------------------------------------
# Folding row changes into totals
# ---------------------------------------------------------------------------


def test_insert_commit_and_release_move_the_total() -> None:
    reserved = _row("reserved", "2")
    (after_insert,) = apply_row_changes([_total()], {}, {reserved.id: reserved})
    assert after_insert.spent_micros == 2_000_000

    committed = _row("committed", "2", "0.75", row_id=reserved.id)
    (after_commit,) = apply_row_changes(
        [after_insert], {reserved.id: reserved}, {committed.id: committed}
    )
    assert after_commit.spent_micros == 750_000

    released = _row("released", "2", row_id=reserved.id)
    (after_release,) = apply_row_changes(
        [after_insert], {reserved.id: reserved}, {released.id: released}
    )
    assert after_release.spent_micros == 0


def test_first_row_of_a_new_window_rolls_the_total_over() -> None:
    row = _row(created_at=HOUR_11 + timedelta(minutes=1))
    (rolled,) = apply_row_changes([_total(HOUR_10, 9_000_000)], {}, {row.id: row})
    assert (rolled.window_start_at, rolled.spent_micros) == (HOUR_11, 2_000_000)


def test_change_to_a_row_from_an_earlier_window_is_ignored() -> None:
    """Committing a reservation made before the window rolled over must not
    touch the new window's total."""
    old = _row("reserved", "2", created_at=HOUR_10)
    settled = _row("committed", "2", "1", created_at=HOUR_10, row_id=old.id)
    changed = apply_row_changes([_total(HOUR_11, 5_000_000)], {old.id: old}, {settled.id: settled})
    assert changed == []


def test_unknown_total_is_left_for_rebuild() -> None:
    row = _row()
    assert apply_row_changes([_total(None)], {}, {row.id: row}) == []


def test_unplaceable_change_marks_the_total_unknown() -> None:
    """A change to an existing row from a *later* bucket than the total means
    the total missed that row's insert (e.g. clock skew between writers)."""
    old = _row("reserved", "2", created_at=HOUR_11)
    settled = _row("committed", "2", "1", created_at=HOUR_11, row_id=old.id)
    (invalidated,) = apply_row_changes(
        [_total(HOUR_10, 5_000_000)], {old.id: old}, {settled.id: settled}
    )
    assert invalidated.window_start_at is None


def test_only_the_row_s_own_scope_and_every_window_of_it_move() -> None:
    row = _row()
    totals = [
        _total(HOUR_10, window="hour"),
        _total(datetime(2026, 10, 1, tzinfo=UTC), window="month"),
        WindowTotal("user", "someone-else", "hour", HOUR_10, 0),
    ]
    changed = apply_row_changes(totals, {}, {row.id: row})
    assert {(t.scope_id, t.window, t.spent_micros) for t in changed} == {
        ("u1", "hour", 2_000_000),
        ("u1", "month", 2_000_000),
    }


def test_unchanged_contribution_writes_nothing() -> None:
    reserved = _row("reserved", "2")
    observed = _row("reserved", "2", "1", row_id=reserved.id)  # still counts at 2
    assert apply_row_changes([_total()], {reserved.id: reserved}, {observed.id: observed}) == []
