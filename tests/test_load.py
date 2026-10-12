"""High-volume load tests for the guarded Anthropic client.

Drives thousands of concurrent ``messages.create`` calls through
:func:`snipz.integrations.anthropic.guarded` against a real ledger and
checks the invariants that matter in production, straight from the
database:

* the cap holds — committed spend never exceeds it;
* nothing leaks — every ledger row ends ``committed`` or ``released``;
* money reconciles — committed spend equals exactly what the fake
  provider billed, plus the reserved bound for ambiguous failures;
* no double calls — a ``request_id`` reaches the provider at most once;
* every request is accounted for by exactly one outcome.

Runs at smoke size (500 requests) by default; ``pytest --load`` runs
10,000. Postgres variants also need ``--postgres``.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import pytest
import pytest_asyncio

from snipz import Budget, BudgetExceededError, Pricing, Scope
from snipz.integrations import DuplicateRequestError
from snipz.integrations.anthropic import guarded
from tests.fake_anthropic import FakeAsyncAnthropic, Outcome
from tests.ledger_audit import LedgerAudit, audit_postgres, audit_sqlite

pytestmark = pytest.mark.load

FULL_SIZE = 10_000
SMOKE_SIZE = 500
CONCURRENCY = 200

PRICES = Pricing.from_toml(
    """
[anthropic."m"]
input_cents_per_m = "300"
output_cents_per_m = "1500"
"""
)
# Fake defaults: 1,000 prompt tokens; max_tokens 1,000; billed 1,000 in / 500 out.
ESTIMATE = Decimal("1.8")
BILLED = Decimal("1.05")


@dataclass
class LoadLedger:
    budget: Budget
    audit: Callable[[], Awaitable[LedgerAudit]]


@pytest.fixture
def request_count(pytestconfig: pytest.Config) -> int:
    return FULL_SIZE if pytestconfig.getoption("--load") else SMOKE_SIZE


@pytest_asyncio.fixture(
    params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)]
)
async def load_ledger(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[LoadLedger]:
    if request.param == "sqlite":
        db_path = tmp_path / "load.db"
        budget = Budget(db_path)
        await budget.migrate()
        try:
            yield LoadLedger(budget, lambda: audit_sqlite(db_path))
        finally:
            await budget.close()
        return

    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )
        budget = Budget(dsn)
        await budget.migrate()
        try:
            yield LoadLedger(budget, lambda: audit_postgres(dsn))
        finally:
            await budget.close()
    finally:
        container.stop()


def _mixed_outcomes(call_index: int) -> Outcome:
    """90% success, 5% HTTP error (not billed), 5% timeout (maybe billed)."""
    bucket = call_index % 20
    if bucket == 0:
        return "status_error"
    if bucket == 1:
        return "connection_error"
    return "ok"


async def _drive(
    calls: list[Callable[[], Awaitable[Any]]], concurrency: int
) -> tuple[Counter[str], float]:
    """Run every call with bounded concurrency; tally outcomes by type."""
    gate = asyncio.Semaphore(concurrency)
    outcomes: Counter[str] = Counter()

    async def one(call: Callable[[], Awaitable[Any]]) -> None:
        async with gate:
            try:
                await call()
            except Exception as exc:
                outcomes[type(exc).__name__] += 1
            else:
                outcomes["ok"] += 1

    started = time.perf_counter()
    await asyncio.gather(*(one(call) for call in calls))
    return outcomes, time.perf_counter() - started


def _report(name: str, n: int, outcomes: Counter[str], elapsed: float) -> None:
    print(f"\n[{name}] {n} requests in {elapsed:.1f}s ({n / elapsed:.0f} req/s): {dict(outcomes)}")


async def test_hot_scope_holds_cap_and_reconciles(
    load_ledger: LoadLedger, request_count: int
) -> None:
    """Every request charges one scope whose cap fits ~40% of them, with
    mixed provider failures and 5% repeated ``request_id`` values."""
    budget = load_ledger.budget
    scope = Scope("tenant", "hot")
    cap = BILLED * request_count * Decimal("0.4")
    await budget.set_limit(scope, cap)

    client = FakeAsyncAnthropic(latency=0.001, outcome=_mixed_outcomes)
    messages = guarded(client, budget, pricing=PRICES).messages  # type: ignore[arg-type]

    unique_ids = request_count * 95 // 100
    request_ids = [f"req-{i % unique_ids}" for i in range(request_count)]
    random.Random(0).shuffle(request_ids)

    def call(request_id: str) -> Callable[[], Awaitable[Any]]:
        return lambda: messages.create(
            scope=scope,
            request_id=request_id,
            model="m",
            max_tokens=1_000,
            messages=[{"role": "user", "content": "hello"}],
            metadata={"user_id": request_id},
        )

    outcomes, elapsed = await _drive([call(rid) for rid in request_ids], CONCURRENCY)
    _report("hot scope", request_count, outcomes, elapsed)
    audit = await load_ledger.audit()

    timeouts = outcomes["APITimeoutError"]
    status_errors = outcomes["InternalServerError"]

    # Every request ended in exactly one expected outcome.
    expected = {"ok", "BudgetExceededError", "DuplicateRequestError"}
    expected |= {"APITimeoutError", "InternalServerError"}
    assert set(outcomes) <= expected, outcomes
    assert sum(outcomes.values()) == request_count
    assert outcomes["ok"] == client.messages.succeeded
    # The cap was the binding constraint, and it held. Headroom is left
    # only by reservations in flight at the last rejection that later
    # settled below their estimate — at most one estimate per slot.
    assert outcomes["BudgetExceededError"] > 0
    committed = audit.committed("tenant", "hot")
    assert committed <= cap
    assert cap - committed < ESTIMATE * (CONCURRENCY + 1)
    # Nothing leaked; failures settled the right way.
    assert audit.rows_by_state["reserved"] == 0
    assert audit.rows_by_state["released"] == status_errors
    assert audit.rows_by_state["committed"] == outcomes["ok"] + timeouts
    # Money reconciles to the cent.
    assert committed == BILLED * outcomes["ok"] + ESTIMATE * timeouts
    # No request_id reached the provider twice.
    sent = Counter(c["metadata"]["user_id"] for c in client.messages.create_calls)
    assert max(sent.values()) == 1


async def test_many_tenants_each_hold_their_cap(
    load_ledger: LoadLedger, request_count: int
) -> None:
    """Requests spread over 100 users, each also charged to one shared
    uncapped org scope: every user cap holds and the org total equals the
    sum of the users'."""
    budget = load_ledger.budget
    users = [Scope("user", f"u{i}") for i in range(100)]
    org = Scope("org", "acme")
    per_user_requests = request_count // len(users)
    user_cap = BILLED * per_user_requests * Decimal("0.6")
    for user in users:
        await budget.set_limit(user, user_cap)

    client = FakeAsyncAnthropic(latency=0.001)
    messages = guarded(client, budget, pricing=PRICES).messages  # type: ignore[arg-type]
    targets = [users[i % len(users)] for i in range(per_user_requests * len(users))]
    random.Random(1).shuffle(targets)

    def call(user: Scope) -> Callable[[], Awaitable[Any]]:
        return lambda: messages.create(
            scope=[user, org],
            model="m",
            max_tokens=1_000,
            messages=[{"role": "user", "content": "hello"}],
        )

    outcomes, elapsed = await _drive([call(u) for u in targets], CONCURRENCY)
    _report("many tenants", len(targets), outcomes, elapsed)
    audit = await load_ledger.audit()

    assert set(outcomes) <= {"ok", "BudgetExceededError"}, outcomes
    assert outcomes["BudgetExceededError"] > 0
    assert audit.rows_by_state["reserved"] == 0
    per_user = [audit.committed("user", user.id) for user in users]
    assert all(spent <= user_cap for spent in per_user)
    assert audit.committed("org", "acme") == sum(per_user) == BILLED * outcomes["ok"]


def test_fake_exceptions_are_the_sdk_types_the_tally_expects() -> None:
    """The outcome tally keys on exception class names; keep them in step
    with the SDK classes the fake raises."""
    assert anthropic.APITimeoutError.__name__ == "APITimeoutError"
    assert anthropic.InternalServerError.__name__ == "InternalServerError"
    assert BudgetExceededError.__name__ == "BudgetExceededError"
    assert DuplicateRequestError.__name__ == "DuplicateRequestError"
