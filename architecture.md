# Brim — Architecture and Schema

Last updated: 2026-05-05

Companion to [brim.md](brim.md), which covers positioning and the build plan. This document is the engineering reference: how Brim is structured, how it persists state, and the design decisions that lock those choices in.

---

## Layered architecture

```
┌─────────────────────────────────────────────────┐
│  Provider integrations (optional)               │
│  brim-anthropic, brim-openai                  │
└─────────────────┬───────────────────────────────┘
                  │ uses
┌─────────────────▼───────────────────────────────┐
│  Public API                                     │
│  @budget.guard (decorator), budget.reserve (CM) │
└─────────────────┬───────────────────────────────┘
                  │ uses
┌─────────────────▼───────────────────────────────┐
│  Core engine                                    │
│  Reservation, Ledger, BudgetExceededError            │
└─────────────────┬───────────────────────────────┘
                  │ uses
┌─────────────────▼───────────────────────────────┐
│  Pricing & estimators                           │
│  Pricing.cost(), Estimator.estimate()           │
└─────────────────┬───────────────────────────────┘
                  │ persists via
┌─────────────────▼───────────────────────────────┐
│  Storage backend (Backend protocol)             │
│  PostgresBackend  ·  SqliteBackend              │
└─────────────────────────────────────────────────┘
```

## Module layout

```
brim/
  __init__.py            # public re-exports
  core.py                # Budget, Reservation, BudgetExceededError
  ledger.py              # SQL transactions, cap arithmetic
  pricing.py             # cost calculation
  pricing.toml           # vendored prices (LiteLLM-derived)
  estimators/
    __init__.py
    anthropic.py
    openai.py
    fallback.py          # tiktoken-based generic
  storage/
    __init__.py          # Backend + LedgerConnection protocols
    sqlite.py            # SqliteBackend
    postgres.py          # PostgresBackend (Phase 2)
    sql/
      sqlite.py          # SQL constants for SQLite dialect
      postgres.py        # SQL constants for Postgres dialect
    migrations/
      sqlite/            # numbered .sql files
      postgres/          # numbered .sql files (lockstep versions)
  sync.py                # sync wrapper (experimental, Phase 2)
  aio.py                 # AsyncBudget re-export (async is the default)
  decorator.py           # @budget.guard
  events.py              # hook system
  sweep.py               # reservation expirer
  cli.py                 # `brim` entry point
```

## Core types

```python
class Budget:
    def __init__(self, db_url: str, pricing: Pricing | str = "builtin"): ...
    def reserve(
        self,
        scopes: Scope | list[Scope],
        cents: Decimal,
        *,
        request_id: str | None = None,
        ttl: int = 300,
        model: str | None = None,
    ) -> Reservation: ...
    def guard(self, scope, estimate, actual, model) -> Callable: ...  # decorator
    def sweep(self) -> int: ...

class Reservation:
    id: UUID
    scopes: list[Scope]
    estimated_cents: Decimal
    actual_cents: Decimal | None
    state: Literal['reserved', 'committed', 'released']
    late: bool   # true if settled/released after TTL

    def observe(self, *, input_tokens: int, output_tokens: int, model: str) -> None: ...
    def commit(self) -> None: ...
    def release(self) -> None: ...
    def __enter__(self): ...
    def __exit__(self, exc_type, exc, tb): ...   # auto-release on exception

class Scope(NamedTuple):
    type: str        # 'user' | 'tenant' | 'feature' | 'global'
    id: str
    window: str = 'month'
```

## Reservation state machine

```
        reserve()
            │
            ▼
      ┌──────────┐
      │ reserved │ ◀── observe() (updates actual_cents in place)
      └────┬─────┘
           │
   ┌───────┼─────────────┬───────────────┐
   │       │             │               │
   ▼       ▼             ▼               ▼
commit() release()   sweeper          late commit
                  (TTL expired)    (after sweeper)
   │       │             │               │
   ▼       ▼             ▼               ▼
committed released  released[late]  committed[late]
```

Allowed transitions:
- `reserved → committed` — normal commit, `late=false`.
- `reserved → released` — caller-initiated release (or `__exit__` on exception). `late=false`.
- `reserved → released[late=true]` — sweeper release after TTL.
- `released[late=true] → committed[late=true]` — late commit reclaims a sweeper-released row.
- `reserved → committed[late=true]` — late commit before sweeper got to it.

Caller-released rows (`late=false`) are terminal — a late commit on those is rejected (caller explicitly aborted).

### Late-commit semantics

If the provider call takes longer than `ttl`, the sweeper releases the reservation. When the call eventually returns and `commit()` is invoked:

1. The commit succeeds and writes the actual cost.
2. The row transitions to `committed` with `late=true`.
3. The `on_overrun` event fires.
4. Future cap checks include the late-committed cost as real spend.

This is a deliberate choice over raising an error or silently dropping the cost:
- The actual money was spent — losing it from the ledger destroys the audit trail.
- The caller can't recover from a `LateCommitError` (the call already returned), so raising adds boilerplate without value.
- Operators can query `WHERE late = TRUE` to find slow-call patterns.

### Streaming partial-failure semantics

If a streaming call records billable output via `observe()` and then fails:

- **Caller MUST `commit()`** with the observed actual — the provider already billed for the partial output.
- Calling `release()` would refund the budget for money that was actually spent.

Default `__exit__` behavior auto-releases on exception, which assumes nothing was billed. Callers handling streaming exceptions must override:

```python
with budget.reserve(scope, est) as r:
    try:
        for event in stream:
            r.observe(input_tokens=..., output_tokens=...)
    except StreamError:
        if r.actual_cents and r.actual_cents > 0:
            r.commit()  # bill what we actually used
        raise
```

This is a documented caller responsibility — the library cannot detect "billable content received" without provider-specific knowledge.

### Sweeper coordination

The sweeper does **not** lock the limit row when releasing expired reservations:

```sql
UPDATE brim_ledger
   SET state = 'released', late = TRUE
 WHERE state = 'reserved'
   AND expires_at < NOW();
```

Released rows are excluded from the cap-check aggregate (`WHERE state IN ('reserved', 'committed')`), so a release operation does not change anyone's reported spend. Concurrent reserves on the same scope can proceed without coordinating with the sweeper.

## Concurrency model

Two concurrent requests on the same `(scope, window)`:

```
Request A                          Request B
─────────                          ─────────
BEGIN                              BEGIN
SELECT FOR UPDATE limit row        │
                                  blocks…
compute cap check                   │
INSERT ledger row (reserved)        │
COMMIT                              │
                                   SELECT FOR UPDATE acquires
                                   cap check now sees A's reservation
                                   pass or raise BudgetExceededError
                                   COMMIT
```

**Invariant:** cap check + ledger insert are inside one transaction with the limit row locked. No interleaving. The cap is never overshot.

## Sync vs async

Async is the source of truth. Sync wrappers run the async core via a dedicated background event loop, so sync users do not fight asyncio:

```python
from brim import Budget          # sync API (experimental in Phase 2)
from brim.aio import AsyncBudget  # async API
```

Sync calls from inside an active event loop raise a clear error rather than deadlocking. Same backends, same SQL, same correctness guarantees.

## Backend abstraction

`ledger.py` is dialect-agnostic — it speaks only the `LedgerConnection` Protocol. Each backend wraps its native driver to satisfy that interface.

```
Budget ─▶ Backend ─▶ LedgerConnection ─▶ ledger.py (cap-check orchestration)
                              │
                              ├─▶ wraps aiosqlite.Connection (SqliteBackend)
                              └─▶ wraps asyncpg.Connection  (PostgresBackend)
```

The wrapper exposes one async API; per-backend implementations dispatch to dialect-specific SQL (`storage/sql/{sqlite,postgres}.py`) and to native type adapters. UUIDs, Decimals, datetimes, and booleans flow as Python-native types through `ledger.py` — string conversion happens inside the backend, not in the engine.

`lock_limit` is the most dialect-divergent operation and lives as a method on `LedgerConnection`:

- **SqliteBackend**: no-op. `BEGIN IMMEDIATE` in `transaction()` already holds the DB-wide writer lock.
- **PostgresBackend**: `SET LOCAL lock_timeout = '5s'; SELECT cap_cents … FOR UPDATE`. A worker waiting >5s on a contended row sees a clear error rather than hanging.

All user-supplied values flow as bound parameters. SQL is never built via string concatenation, regardless of dialect.

## Extension points

- **`Backend` protocol** — implement to add storage. Methods: `transaction()`, `connect()`, `migrate()`, `close()`. v0 ships SQLite (Phase 1) and Postgres (Phase 2).
- **`LedgerConnection` protocol** — implement alongside a new backend to expose dialect-specific operations (`lock_limit`, `fetch_limit`, `current_spend`, `insert_ledger_row`, etc.) under one async interface.
- **`Estimator` protocol** — implement to add a provider. Method: `estimate(prompt, model) → (input_tokens, max_output_tokens)`.
- **Event hooks** — `on_reserved`, `on_committed`, `on_released`, `on_overrun`. Sync + async variants. Handler errors are logged, not propagated.

---

# Schema

## Tables

```sql
-- One row per (scope, window). The cap definition.
CREATE TABLE brim_limits (
    scope_type   TEXT NOT NULL,    -- 'user' | 'tenant' | 'feature' | 'global'
    scope_id     TEXT NOT NULL,
    window       TEXT NOT NULL,    -- 'minute' | 'hour' | 'day' | 'month' | 'lifetime'
    cap_cents    NUMERIC(20, 6) NOT NULL,
    grace_pct    INT NOT NULL DEFAULT 0,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id, window)
);

-- Append-mostly. One row per (reservation, scope).
CREATE TABLE brim_ledger (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reservation_id   UUID NOT NULL,           -- groups multi-scope reservations
    scope_type       TEXT NOT NULL,
    scope_id         TEXT NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('reserved','committed','released')),
    late             BOOLEAN NOT NULL DEFAULT FALSE,
    estimated_cents  NUMERIC(20, 6) NOT NULL,
    actual_cents     NUMERIC(20, 6),
    model            TEXT,
    provider         TEXT,
    input_tokens     INT,
    output_tokens    INT,
    cached_tokens    INT,
    request_id       TEXT,                    -- idempotency key
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at       TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ NOT NULL
);

-- Provider × model → cost per 1M tokens. Versioned by valid_from.
CREATE TABLE brim_pricing (
    provider                 TEXT NOT NULL,
    model                    TEXT NOT NULL,
    input_cents_per_m        NUMERIC(20, 6) NOT NULL,
    output_cents_per_m       NUMERIC(20, 6) NOT NULL,
    cache_read_cents_per_m   NUMERIC(20, 6),
    cache_write_cents_per_m  NUMERIC(20, 6),
    valid_from               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, model, valid_from)
);

CREATE TABLE brim_schema_version (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Indexes

```sql
-- Hot path: cap-check aggregate over current window
CREATE INDEX idx_ledger_scope_window
  ON brim_ledger (scope_type, scope_id, created_at DESC)
  WHERE state IN ('reserved', 'committed');

-- Sweeper: find expired reservations fast
CREATE INDEX idx_ledger_expiring
  ON brim_ledger (expires_at)
  WHERE state = 'reserved';

-- Idempotency: O(1) request_id lookup
CREATE UNIQUE INDEX idx_ledger_request_id
  ON brim_ledger (request_id)
  WHERE request_id IS NOT NULL;

-- Multi-scope reservation grouping
CREATE INDEX idx_ledger_reservation_id
  ON brim_ledger (reservation_id);
```

## The cap-check query

```sql
BEGIN;

-- 1. Lock the limit row (no deadlock — scopes are sorted before locking)
SELECT cap_cents, grace_pct FROM brim_limits
 WHERE scope_type = $1 AND scope_id = $2 AND window = $3
   AND enabled = TRUE
 FOR UPDATE;

-- 2. Compute current spend in this window
SELECT COALESCE(SUM(
  CASE
    WHEN state = 'committed' THEN actual_cents
    WHEN state = 'reserved'  THEN GREATEST(COALESCE(actual_cents, 0), estimated_cents)
  END
), 0) AS spent
  FROM brim_ledger
 WHERE scope_type = $1 AND scope_id = $2
   AND state IN ('reserved', 'committed')
   AND created_at >= $4;   -- window start, computed in app code

-- 3. If spent + new_estimate > cap_cents * (1 + grace_pct/100): raise BudgetExceededError
-- 4. Else INSERT the reservation row(s)

INSERT INTO brim_ledger (...) VALUES (...);

COMMIT;
```

The CASE expression is deliberate:
- **Committed rows** count at `actual_cents` — the call finished and we know the real cost.
- **Reserved (in-flight) rows** count at `GREATEST(COALESCE(actual_cents, 0), estimated_cents)`:
  - If `observe()` has not been called, `actual` is NULL → COALESCE to 0 → estimate dominates.
  - If `observe()` pushed actual past estimate (overrun in progress), actual dominates.
  - In-flight reservations are always counted at their *upper bound* to defend against burst over-issuance.

A naive `SUM(GREATEST(actual, est))` over both states would over-count committed rows where the call cost less than estimated — quietly shrinking the budget for the rest of the window. The CASE fixes that.

## Composite scopes

A single reserve can list multiple scopes — all must pass their cap check.

1. Sort scopes by `(scope_type, scope_id, window)` deterministically.
2. `SELECT FOR UPDATE` each limit row in that order. (Deterministic order avoids deadlocks.)
3. Run the cap check for each scope.
4. If any fails, the transaction aborts; nothing is held.
5. If all pass, INSERT one ledger row per scope, sharing one `reservation_id`.
6. COMMIT.

`Reservation.commit()` updates all rows with that `reservation_id` atomically.

## Idempotency

A unique index on `request_id` is necessary but not sufficient — the reserve flow has to handle concurrent retries deterministically. The full sequence:

1. **Pre-check** (outside the transaction):
   ```sql
   SELECT id, ... FROM brim_ledger WHERE request_id = $1
   ```
   If found → return that Reservation. No locking, no cap-check.

2. **BEGIN** transaction.

3. **Cap-check** as in [The cap-check query](#the-cap-check-query) — lock limit row(s), compute spent, compare to cap.

4. **INSERT** the new ledger row(s) with `request_id = $1`.

5. **On UNIQUE violation** (a concurrent retry won the race):
   - ROLLBACK
   - `SELECT … WHERE request_id = $1` → return that Reservation.
   - The first INSERT to land is the canonical reservation; all retries converge on it.

6. **COMMIT** on the success path.

Guarantee: N parallel retries with the same `request_id` produce **exactly one ledger row**, and all callers receive the same Reservation. If the first attempt fails cap-check, retries see the same `BudgetExceededError` (or, if the cap state changed, the first one to succeed wins).

## Migrations

- Raw SQL files in `storage/migrations/`, numbered `0001_initial.sql`, `0002_*.sql`, ...
- `brim_schema_version` tracks current version.
- `brim migrate` applies pending migrations idempotently.
- No Alembic in v0 — avoids a SQLAlchemy dependency.

## SQLite differences (handled inside the backend)

| Postgres | SQLite | How backend abstracts it |
|---|---|---|
| `gen_random_uuid()` | n/a | Generate UUIDs in Python |
| `JSONB` | n/a | Store `TEXT`, validate in app code |
| `NUMERIC(20, 6)` | NUMERIC affinity | Decimal in Python, no precision issues for v0 scales |
| `SELECT … FOR UPDATE` | n/a | Use `BEGIN IMMEDIATE` (write-locks entire DB — acceptable single-node) |
| `date_trunc('month', now())` | n/a | Compute window-start in Python, pass as parameter |
| `TIMESTAMPTZ` | TEXT | Always store ISO-8601 UTC; parse in Python |

The `Backend` protocol abstracts these; users see one API.

---

# Decision log

Locked-in choices, recorded so we don't relitigate them.

1. **Reservation ledger, not estimate-and-record** — load-bearing claim of the project.
2. **Storage units = `NUMERIC(20, 6)` cents, Python `Decimal` internally** — never floats in financial arithmetic.
3. **Async-first core, sync wrapper** — supports both call styles without forking the SQL.
4. **One ledger row per (reservation, scope)** — simpler aggregation, slightly more rows; worth it.
5. **Calendar-window semantics by default** (`day` resets at UTC midnight, `month` at month boundary) — rolling 30-day as opt-in later.
6. **Late commits succeed with `late=true` flag, fire `on_overrun`** — preserves the audit trail; no `LateCommitError`. Rejecting late commits would lose real spend data; silent drops would hide it. Flagging keeps it queryable.
7. **Postgres + SQLite only in v0** — no Redis, no MySQL, no DynamoDB. Add via the `Backend` protocol if demand exists.
8. **Vendor LiteLLM's `pricing.toml`** — don't reinvent price tables.
9. **Raw SQL migrations, no Alembic** — keeps the dependency tree small.
10. **No monkey-patching of provider SDKs in core** — that lives in optional `brim-anthropic` / `brim-openai` packages.
11. **Cap-check SUM uses CASE on state** — `actual` for committed rows, `GREATEST(actual, estimated)` for reserved rows. A flat `GREATEST` over both states over-counts committed rows that came in under estimate. Surfaced during Phase 0 paper validation.
12. **Idempotent reserve = SELECT-first → INSERT with unique-conflict recovery** — the unique index alone is not enough; flow must converge N parallel retries onto exactly one ledger row.
13. **Streaming partial-failure = caller commits, not releases** — the provider already billed; the library cannot infer billability without provider-specific knowledge. Documented caller responsibility.
14. **Sweeper does not lock the limit row** — released rows are excluded from cap-check, so coordinating with concurrent reserves is unnecessary.
15. **Per-backend SQL modules, not templated** — `storage/sql/sqlite.py` and `storage/sql/postgres.py` hold dialect-pure constants. Drift is caught by the conformance suite; templated SQL would be one fewer file but harder to audit.
16. **Schema version bumps to v2 in Phase 2** — even without column changes, v2 marks "validated against both backends." Migrations stay lockstep across dialects: a Postgres v3 implies a SQLite v3 (with a no-op file if needed).
17. **`LedgerConnection` Protocol over per-dialect ledger modules** — each backend wraps its native driver; `ledger.py` speaks one async interface. Avoids duplicating cap-check orchestration. ~80 LOC of wrapper.
18. **Type stringification lives in backend adapters, not `ledger.py`** — UUIDs, Decimals, datetimes, and booleans flow as Python-native types into the wrapper; the wrapper handles dialect-specific conversion. Keeps the engine dialect-blind.
19. **`lock_limit` is a method on `LedgerConnection`** — dialect dispatch lives where dialect lives. SQLite: no-op (`BEGIN IMMEDIATE` already holds the lock). Postgres: `SET LOCAL lock_timeout = '5s'; SELECT … FOR UPDATE` — bounded wait, clear error on contention.
20. **All user-supplied values flow as bound parameters; SQL is never concatenated** — hard invariant across all backends. Code review or a lint check enforces it.
21. **Postgres pool: dual-mode with conservative defaults** — `Budget("postgres://...")` creates a managed pool (`min_size=2, max_size=10`); `PostgresBackend(pool=...)` accepts an injected pool for shops sharing one with their app. Utilization ≥80% emits `logging.warning`, rate-limited to once per 60s. Threshold and cooldown configurable.
22. **`migrate()` stays on `Backend`; no separate `Migrator`** — migration discovery is dialect-specific anyway, splitting into a separate utility just creates indirection. Refactor only if rollbacks / dry-runs / ranged migrations become necessary.
23. **No `check_connectivity()` on the Backend protocol** — asyncpg's defaults (warm pool, idle recycling) handle 99% of real cases. DB health is surfaced via observability (Phase 7 events), not a protocol method. Add only if a real user reports flaky pool behavior.
24. **Sync wrapper ships experimental in Phase 2** — `sync.py` runs the async core via a dedicated background event loop. Calls from inside an active event loop raise rather than deadlock. Marked `experimental` in the README; promote to stable in Phase 3 once exercised.
25. **testcontainers for Postgres in CI; SQLite tests stay in-process** — adds a Docker dependency for development and CI, but exercises real Postgres semantics. Conformance suite runs against both backends in a CI matrix.
