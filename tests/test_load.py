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
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import aiosqlite
import anthropic
import pytest
import pytest_asyncio

from snipz import Budget, BudgetExceededError, Pricing, Scope
from snipz.integrations import DuplicateRequestError
from snipz.integrations.anthropic import guarded
from tests.fake_anthropic import FakeAsyncAnthropic, Outcome
from tests.fake_anthropic_api import FakeAnthropicAPI, StreamOutcome
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
    # Insert N committed 1-cent rows for a scope straight into the ledger,
    # generated server-side so a million rows take seconds.
    seed_committed_rows: Callable[[Scope, int], Awaitable[None]]


_SQLITE_SEED = """
    WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < ?)
    INSERT INTO snipz_ledger (
        id, reservation_id, scope_type, scope_id, state, late,
        estimated_cents, actual_cents, expires_at, created_at, settled_at
    )
    SELECT lower(hex(randomblob(16))), lower(hex(randomblob(16))), ?, ?, 'committed', 0,
           1, 1, ?, ?, ?
      FROM seq
"""

_POSTGRES_SEED = """
    INSERT INTO snipz_ledger (
        id, reservation_id, scope_type, scope_id, state, late,
        estimated_cents, actual_cents, expires_at, created_at, settled_at
    )
    SELECT gen_random_uuid(), gen_random_uuid(), $1, $2, 'committed', FALSE,
           1, 1, now(), now(), now()
      FROM generate_series(1, $3)
"""


async def _seed_sqlite(db_path: Path, scope: Scope, rows: int) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(_SQLITE_SEED, (rows, scope.type, scope.id, stamp, stamp, stamp))
        await conn.commit()


async def _seed_postgres(dsn: str, scope: Scope, rows: int) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_POSTGRES_SEED, scope.type, scope.id, rows)
    finally:
        await conn.close()


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
            yield LoadLedger(
                budget,
                lambda: audit_sqlite(db_path),
                lambda scope, rows: _seed_sqlite(db_path, scope, rows),
            )
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
            yield LoadLedger(
                budget,
                lambda: audit_postgres(dsn),
                lambda scope, rows: _seed_postgres(dsn, scope, rows),
            )
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


@pytest.fixture
def ledger_rows(pytestconfig: pytest.Config) -> int:
    return 1_000_000 if pytestconfig.getoption("--load") else 20_000


async def _reserve_commit_p50_ms(budget: Budget, scope: Scope, n: int = 200) -> float:
    timings = []
    for _ in range(n):
        started = time.perf_counter()
        reservation = await budget.reserve(scope, Decimal("1"))
        await reservation.commit(Decimal("1"))
        timings.append((time.perf_counter() - started) * 1000)
    return sorted(timings)[n // 2]


async def test_large_ledger_does_not_slow_reservations(
    load_ledger: LoadLedger, ledger_rows: int
) -> None:
    """A scope with a million committed rows reserves as fast as an empty
    one: the cap check reads a running total instead of summing the
    ledger. Its first reservation rebuilds the total once."""
    budget = load_ledger.budget
    big, fresh = Scope("tenant", "big", "lifetime"), Scope("tenant", "fresh", "lifetime")
    await load_ledger.seed_committed_rows(big, ledger_rows)
    cap = Decimal(ledger_rows * 10)
    await budget.set_limit(big, cap)  # leaves the total unknown: next read rebuilds
    await budget.set_limit(fresh, cap)

    started = time.perf_counter()
    first = await budget.reserve(big, Decimal("1"))
    await first.commit(Decimal("1"))
    rebuild_ms = (time.perf_counter() - started) * 1000
    fresh_p50 = await _reserve_commit_p50_ms(budget, fresh)
    big_p50 = await _reserve_commit_p50_ms(budget, big)
    print(
        f"\n[large ledger] {ledger_rows:,} rows: first reservation (rebuild) {rebuild_ms:.0f} ms;"
        f" p50 reserve+commit {big_p50:.2f} ms vs {fresh_p50:.2f} ms on an empty scope"
    )

    assert big_p50 < fresh_p50 * 2 + 1.0
    audit = await load_ledger.audit()
    # Seeded rows at 1 cent each, the first reservation, and the 200 timed.
    assert audit.committed("tenant", "big") == Decimal(ledger_rows + 201)


# Streams run through the real SDK over a scripted HTTP API
# (tests.fake_anthropic_api): 1,000 counted prompt tokens; message_start
# reports 800; a completed stream bills 500 output tokens.
STREAM_BILLED = Decimal("0.99")  # 800 in + 500 out
STREAM_PARTIAL = Decimal("1.74")  # 800 in + max_tokens (1,000) out
# 80% complete; 5% each: the consumer stops early, an SSE error event, a
# dropped connection, an HTTP 529 when opening.
StreamPlan = StreamOutcome | Literal["early_exit"]
_COMPLETE: StreamPlan = "complete"
_STREAM_PLANS: list[StreamPlan] = [
    *[_COMPLETE] * 16,
    "early_exit",
    "error_event",
    "connection_drop",
    "status_error",
]


async def test_streams_hold_cap_and_reconcile(load_ledger: LoadLedger, request_count: int) -> None:
    """Every way a stream can end, under load, with 5% repeated
    ``request_id`` values against one capped scope."""
    budget = load_ledger.budget
    scope = Scope("tenant", "streams")
    cap = STREAM_BILLED * request_count * Decimal("0.4")
    await budget.set_limit(scope, cap)

    unique_ids = request_count * 95 // 100
    request_ids = [f"req-{i % unique_ids}" for i in range(request_count)]
    random.Random(2).shuffle(request_ids)
    plan: dict[str, StreamPlan] = {
        rid: _STREAM_PLANS[int(rid.removeprefix("req-")) % len(_STREAM_PLANS)]
        for rid in request_ids
    }

    def served_outcome(body: dict[str, Any]) -> StreamOutcome:
        chosen = plan[body["metadata"]["user_id"]]
        # An early exit is the consumer's choice; the API serves it in full.
        return "complete" if chosen == "early_exit" else chosen

    api = FakeAnthropicAPI(outcome=served_outcome)
    messages = guarded(api.client(), budget, pricing=PRICES).messages
    early_exits: list[str] = []

    def call(request_id: str) -> Callable[[], Awaitable[Any]]:
        async def run() -> None:
            async with messages.stream(
                scope=scope,
                request_id=request_id,
                model="m",
                max_tokens=1_000,
                messages=[{"role": "user", "content": "hello"}],
                metadata={"user_id": request_id},
            ) as stream:
                async for _ in stream.text_stream:
                    if plan[request_id] == "early_exit":
                        early_exits.append(request_id)
                        break

        return run

    outcomes, elapsed = await _drive([call(rid) for rid in request_ids], CONCURRENCY)
    _report("streams", request_count, outcomes, elapsed)
    audit = await load_ledger.audit()
    served = api.streams_served

    expected = {"ok", "BudgetExceededError", "DuplicateRequestError"}
    expected |= {"APIStatusError", "OverloadedError", "ReadError"}
    assert set(outcomes) <= expected, outcomes
    assert sum(outcomes.values()) == request_count
    assert outcomes["BudgetExceededError"] > 0
    committed = audit.committed("tenant", "streams")
    assert committed <= cap
    assert audit.rows_by_state["reserved"] == 0
    assert audit.rows_by_state["released"] == served["status_error"]
    # Money reconciles to the cent: completed streams at their billed usage;
    # streams that stopped after message_start at prompt + max_tokens.
    completed = served["complete"] - len(early_exits)
    partial = len(early_exits) + served["error_event"] + served["connection_drop"]
    assert committed == STREAM_BILLED * completed + STREAM_PARTIAL * partial
    sent = Counter(body["metadata"]["user_id"] for body in api.stream_requests)
    assert max(sent.values()) == 1


def test_fake_exceptions_are_the_sdk_types_the_tally_expects() -> None:
    """The outcome tally keys on exception class names; keep them in step
    with the SDK classes the fake raises."""
    assert anthropic.APITimeoutError.__name__ == "APITimeoutError"
    assert anthropic.InternalServerError.__name__ == "InternalServerError"
    assert BudgetExceededError.__name__ == "BudgetExceededError"
    assert DuplicateRequestError.__name__ == "DuplicateRequestError"
