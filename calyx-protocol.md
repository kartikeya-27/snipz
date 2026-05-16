# Calyx Protocol — Specification v1.0 (DRAFT)

Last updated: 2026-05-06
Status: **v1.0 DRAFT — comments open**

This document will be promoted to v1.0 final once:

1. At least one non-Python reference implementation has passed the
   conformance suite.
2. The "Known underspecified areas" section (Appendix C) is empty.
3. No outstanding objections from implementers.

Until then, breaking changes within v1.0 DRAFT are permitted with notice.
Implementations targeting v1.0 SHOULD pin to a specific DRAFT revision
until v1.0 final ships.

This document is the canonical wire specification for the Calyx reservation
ledger. A conforming implementation in any language reads this document once
and produces a client that interoperates with every other conforming client
against the same backing database.

The Python library (`calyx`), HTTP facade (`calyx-server`), and the planned
`calyx-go` and `calyx-node` clients are reference implementations of this
protocol. The protocol — not any particular implementation — is the
canonical artifact.

For positioning and roadmap see [calyx.md](calyx.md).
For the Python implementation's internals see [architecture.md](architecture.md).

---

## 1. Scope and non-goals

**In scope.** The schema, the five SQL transactions a client must implement,
the reservation state machine, idempotency rules, and the conformance suite
every client must pass.

**Out of scope.** Token estimation, pricing tables, provider-SDK wrapping,
event hooks, decorators, CLI tools, dashboards. These are implementation
concerns layered above the protocol.

## 2. Conformance language

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**
in this document are to be interpreted as in RFC 2119.

A client that fails any **MUST** clause is non-conforming and **MUST NOT**
ship under the Calyx name.

## 3. Conceptual model

Calyx is a *reservation ledger*. Three operations form the lifecycle:

```
                reserve()
                    │
                    ▼
              ┌──────────┐
              │ reserved │ ◀── observe()
              └─────┬────┘
                    │
        ┌───────────┼─────────────┐
        ▼           ▼             ▼
    commit()    release()    sweeper (TTL)
        │           │             │
        ▼           ▼             ▼
    committed   released    released[late]
```

Spend is enforced against a *cap* defined per *scope* per *window*. A scope is
a `(type, id, window)` triple, e.g. `(user, u_42, month)`. A reservation
holds money against one or more scopes; the cap check **MUST** be atomic
across all of them.

The cap **MUST NOT** be exceeded under any concurrent execution.

## 4. Storage requirements

A conforming backend **MUST** provide:

1. ACID transactions.
2. Row-level write locks acquirable inside a transaction (`SELECT … FOR UPDATE`
   in PostgreSQL; `BEGIN IMMEDIATE` whole-database locking in SQLite is
   sufficient for single-writer single-node deployments).
3. A unique index that rejects duplicate inserts atomically (used for
   idempotency).

PostgreSQL ≥ 12 and SQLite ≥ 3.35 are reference backends. Other engines
**MAY** be used if they satisfy clauses 1–3.

## 5. Schema

Every conforming installation **MUST** carry these tables, with these column
names, types, and constraints. Additional columns **MAY** be added by an
implementation as long as they are nullable or defaulted.

### 5.1 `calyx_limits`

```sql
CREATE TABLE calyx_limits (
    scope_type   TEXT NOT NULL,
    scope_id     TEXT NOT NULL,
    window       TEXT NOT NULL,
    cap_cents    NUMERIC(20, 6) NOT NULL,
    grace_pct    INT NOT NULL DEFAULT 0,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id, window)
);
```

`window` **MUST** be one of: `minute`, `hour`, `day`, `month`, `lifetime`.

### 5.2 `calyx_ledger`

```sql
CREATE TABLE calyx_ledger (
    id               UUID PRIMARY KEY,
    reservation_id   UUID NOT NULL,
    scope_type       TEXT NOT NULL,
    scope_id         TEXT NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('reserved','committed','released')),
    late             BOOLEAN NOT NULL DEFAULT FALSE,
    estimated_cents  NUMERIC(20, 6) NOT NULL,
    actual_cents     NUMERIC(20, 6),
    model            TEXT,
    provider         TEXT,
    request_id       TEXT,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at       TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ NOT NULL
);
```

### 5.3 Required indexes

```sql
CREATE INDEX idx_ledger_scope_window
  ON calyx_ledger (scope_type, scope_id, created_at DESC)
  WHERE state IN ('reserved', 'committed');

CREATE INDEX idx_ledger_expiring
  ON calyx_ledger (expires_at)
  WHERE state = 'reserved';

CREATE UNIQUE INDEX idx_ledger_request_id
  ON calyx_ledger (request_id)
  WHERE request_id IS NOT NULL;

CREATE INDEX idx_ledger_reservation_id
  ON calyx_ledger (reservation_id);
```

The `UNIQUE INDEX` on `request_id` is **load-bearing for idempotency** and
**MUST** be present.

## 6. Operations

A client **MUST** implement these six operations. SQL shown is illustrative;
the *behavior* is normative, not the exact statements.

### 6.1 `set_limit(scope, cap_cents, grace_pct=0)`

Insert or update a row in `calyx_limits`. **MUST** be idempotent.

### 6.2 `reserve(scopes, estimated_cents, request_id?, ttl, metadata?) → Reservation`

The cap-check transaction. The most important operation in the protocol.

**Pre-check (outside the transaction).** If `request_id` is present, query
existing rows by `request_id`; if found, return that reservation. No locking,
no insert.

**Transaction.**

1. **BEGIN.**
2. For each scope in `scopes`, sorted by `(scope_type, scope_id, window)`:
   `SELECT … FOR UPDATE` the matching `calyx_limits` row.
3. For each scope, compute *current spend* using the [cap-check formula](#7-cap-check-formula).
4. For each scope: if `spent + estimated_cents > cap_cents × (1 + grace_pct / 100)`,
   raise `BudgetExceededError` and **ROLLBACK**.
5. For each scope, `INSERT` one row into `calyx_ledger` with
   `state='reserved'`, the supplied `estimated_cents`, the supplied
   `request_id`, and `expires_at = now() + ttl`. All inserts **MUST** share
   the same generated `reservation_id`.
6. **COMMIT.**

**On unique-violation against `idx_ledger_request_id`** (a concurrent retry
won the race): **ROLLBACK**, query existing rows by `request_id`, return
that reservation.

The scope sort order in step 2 is **MUST** — it prevents deadlocks under
composite-scope reservations.

### 6.3 `observe(reservation_id, actual_cents)`

Update the `actual_cents` column on every row of the reservation that is
still in state `reserved`. **MUST** be safe to call repeatedly.

```sql
UPDATE calyx_ledger
   SET actual_cents = $1
 WHERE reservation_id = $2 AND state = 'reserved';
```

### 6.4 `commit(reservation_id, actual_cents) → CommitOutcome`

Settle the reservation as committed.

1. Attempt the **normal transition** (`reserved → committed`):
   ```sql
   UPDATE calyx_ledger
      SET state = 'committed', actual_cents = $1, settled_at = $2
    WHERE reservation_id = $3 AND state = 'reserved';
   ```
2. If `rows_affected > 0`: return `{rows_affected, was_late: false}`.
3. Otherwise attempt the **late-commit transition**
   (`released[late=true] → committed[late=true]`):
   ```sql
   UPDATE calyx_ledger
      SET state = 'committed', actual_cents = $1, settled_at = $2, late = TRUE
    WHERE reservation_id = $3 AND state = 'released' AND late = TRUE;
   ```
4. Return `{rows_affected, was_late: rows_affected > 0}`.

A client **MUST NOT** commit a row that is `released` with `late = FALSE`
— that row was caller-released. Surface this as an error to the caller.

### 6.5 `release(reservation_id)`

Refund the reservation. **MUST** be a no-op if the reservation is already
`committed` or `released`.

```sql
UPDATE calyx_ledger
   SET state = 'released', settled_at = $1
 WHERE reservation_id = $2 AND state = 'reserved';
```

### 6.6 `sweep() → released_count`

Background operation. Releases reservations whose `expires_at` has passed.

```sql
UPDATE calyx_ledger
   SET state = 'released', late = TRUE, settled_at = $1
 WHERE state = 'reserved' AND expires_at < $2;
```

The sweep operation **MUST NOT** lock the limit row. Released rows are
excluded from the cap-check aggregate, so concurrent reserves on the same
scope can proceed without coordinating with the sweeper.

## 7. Cap-check formula

Current spend for a scope in the active window **MUST** be computed as:

```sql
SELECT COALESCE(SUM(
  CASE
    WHEN state = 'committed' THEN actual_cents
    WHEN state = 'reserved'  THEN GREATEST(COALESCE(actual_cents, 0), estimated_cents)
  END
), 0) AS spent
  FROM calyx_ledger
 WHERE scope_type = $1 AND scope_id = $2
   AND state IN ('reserved', 'committed')
   AND created_at >= $3;   -- window start
```

**Why the CASE.** Committed rows count at `actual_cents` — the truth.
Reserved rows count at `GREATEST(actual, estimated)` so streaming overruns
are visible to the next request. A flat `GREATEST` over both states would
over-count committed rows that came in under estimate, silently shrinking
the budget for the rest of the window.

A non-conforming client that uses `SUM(estimated_cents)` over reserved rows
**will** under-count streaming overruns and let the cap drift.

### 7.1 Window start computation

Window starts **MUST** be computed in UTC:

| `window` | Start of current window |
|---|---|
| `minute` | `now` truncated to second 0 |
| `hour` | `now` truncated to minute 0 |
| `day` | `now` truncated to hour 0 |
| `month` | first day of `now`'s month at 00:00 |
| `lifetime` | epoch (`0001-01-01T00:00:00Z`) |

## 8. State machine

```
reserved        ──commit()──▶  committed
reserved        ──release()─▶  released  (late=false; terminal)
reserved        ──sweep()───▶  released  (late=true)
released[late]  ──commit()──▶  committed[late=true]
```

Any other transition **MUST** be rejected.

`released[late=false]` is terminal — caller explicitly released; a late
return from the provider call **MUST NOT** be billable.

## 9. Composite scopes

A reservation **MAY** name multiple scopes. All caps **MUST** pass for the
reserve to succeed; if any fails, no rows are inserted.

Implementation requirements:

1. Sort scopes by `(scope_type, scope_id, window)` lexicographically.
2. `SELECT … FOR UPDATE` each limit row in sorted order. **MUST**.
3. Run cap check for each scope.
4. On any failure: ROLLBACK; raise `BudgetExceededError` naming the failing scope.
5. On success: insert one ledger row per scope, all sharing one
   `reservation_id`.

Sorting in step 1 is the deadlock-prevention mechanism. Two transactions
locking `(tenant=acme, user=u_42)` and `(user=u_42, tenant=acme)` will
acquire locks in identical order — no cycle is possible.

## 10. Idempotency

A `request_id` makes the reserve operation safe under retries.

**Guarantee.** N parallel reserves with the same `request_id` produce
exactly one ledger row group. All callers receive the same reservation,
or the same `BudgetExceededError`.

**Required flow:**

1. **Pre-check** (outside transaction): `SELECT … WHERE request_id = $1`.
   If found, return that reservation. Skip locking and cap-check.
2. Run the reserve transaction (section 6.2).
3. **On `UNIQUE` violation** against `idx_ledger_request_id`: ROLLBACK,
   re-query by `request_id`, return that reservation.

The unique index alone is necessary but **NOT sufficient**. The retry-after-
violation step is part of the contract; a client that surfaces the unique-
violation as an error is non-conforming.

## 11. Concurrency and isolation

The cap-check transaction (section 6.2) **MUST** be executed at an
isolation level that prevents the lost-update anomaly between the
"compute current spend" and "insert ledger row" steps. With the prescribed
`SELECT … FOR UPDATE` (PostgreSQL) or `BEGIN IMMEDIATE` (SQLite), the default
isolation level is sufficient.

A client running under `READ UNCOMMITTED` or skipping the row lock is
non-conforming.

## 12. Types and representations

| Concept | Type | Notes |
|---|---|---|
| Money | `NUMERIC(20, 6)` cents, **MUST NOT** be a binary float | Use `Decimal` / `BigDecimal` / fixed-point; never `double` |
| Time | `TIMESTAMPTZ`, always UTC at the wire | Implementations **MAY** convert at boundaries |
| Identifiers | UUIDv4 | Generated client-side or server-side; **MUST** be globally unique |
| Strings | UTF-8 | `scope_type`, `scope_id`, `model`, `provider`, `request_id` |
| `metadata` | JSON object | **MUST** be valid JSON; **SHOULD** be ≤ 4 KiB |

## 13. Error taxonomy

A conforming client **MUST** surface these distinct error conditions:

| Error | When raised |
|---|---|
| `BudgetExceededError` | Cap check failed; **MUST** include `scope`, `cap_cents`, `spent_cents`, `attempted_cents` |
| `InvalidStateError` | Lifecycle method called against an incompatible state (e.g. commit on caller-released row) |
| `BackendError` | Storage operation failed for non-business reasons (connection, schema mismatch, etc.) |

Implementations **MAY** add more specific error subtypes (e.g.
`SchemaVersionMismatch`) but **MUST NOT** collapse the three above into a
single generic error.

## 14. Streaming partial-failure semantics

If a streaming call records billable output via `observe()` and then fails:

- The caller **MUST** call `commit()` with the observed actual.
- Calling `release()` would refund money that the provider already charged.

This is a **caller responsibility**. A conforming client **MUST NOT** auto-
release a reservation whose `actual_cents > 0` on exception. The reference
Python client does this by inspecting `r.actual_cents` in `__exit__`.

## 15. Conformance suite

A client **MUST** pass every fixture in `conformance/v1/`. Fixtures cover:

| Group | Fixtures |
|---|---|
| Basic lifecycle | reserve→commit, reserve→release, reserve→commit→commit (idempotent commit) |
| Cap arithmetic | exact-fit, exceed-by-one-cent, grace-percentage, multi-window |
| Concurrency | two-concurrent-reserves-at-cap, sweep-during-active-reserve |
| Composite scopes | both-pass, first-fails, second-fails, deadlock-free-ordering |
| Idempotency | duplicate-request-id, parallel-retries-converge, retry-after-cap-exceeded |
| Late commit | sweep-then-commit, sweep-then-release-then-commit-rejected |
| Streaming | observe-updates-actual, max-formula-blocks-next-reserve |
| Type fidelity | numeric-precision-roundtrip, utc-timestamp-roundtrip |

Fixtures are language-agnostic YAML. The Python reference client ships the
runner; other clients embed it via a thin adapter (~50 LOC).

## 16. Versioning

This protocol uses semantic versioning at the *protocol* level, distinct
from any client implementation's version.

- **Patch** (1.0.x): clarifications, typo fixes. No behavior change.
- **Minor** (1.x.0): backward-compatible additions. New optional columns,
  new optional operations, new optional error subtypes.
- **Major** (x.0.0): breaking change. Schema migrations required. Old clients
  may not interoperate with new servers.

A `calyx_schema_version` table **MUST** record the highest applied
migration; clients **SHOULD** check it on startup and refuse to run against
an incompatible version.

## 17. Reference implementations

| Implementation | Language | Status |
|---|---|---|
| `calyx` | Python (sync + async) | Phase 1 complete |
| `calyx-server` | Python / FastAPI | Phase 8.5 |
| `calyx-go` | Go | Phase 8.5 |
| `calyx-node` | Node / TypeScript | Phase 8.5 |

A community implementation in any language **MAY** request "official Calyx
client" status by passing the conformance suite and demonstrating
interoperability against the polyglot demo (one DB, three workers in three
languages, one $5 cap).

---

## Appendix A — Minimal client checklist

A new client implementation, sketched as a checklist:

- [ ] Apply migrations to bring schema to current version.
- [ ] Implement the six operations from section 6.
- [ ] Use the cap-check formula from section 7 verbatim. No shortcuts.
- [ ] Sort composite scopes lexicographically before locking.
- [ ] Catch unique-violations on `request_id` and recover via re-query.
- [ ] Use a fixed-point money type. Never a binary float.
- [ ] Pass every fixture in `conformance/v1/`.

If any item is skipped, the client is non-conforming.

## Appendix B — Why these choices

Brief rationales for the three load-bearing protocol decisions:

1. **Postgres-as-truth.** Every other architectural property — language
   agnosticism, multi-process safety, audit trail, durability under crashes
   — falls out of putting state in a transactional, lockable database.
2. **`MAX(actual, estimated)` for reserved rows.** Without it, streaming
   overruns are invisible to the next request, and the cap drifts. The flat
   formula is what every "estimate-and-record" library gets wrong.
3. **`request_id` retry convergence, not just unique-index.** The unique
   index catches duplicates; the protocol's recovery flow makes the
   duplicate transparent to the caller. Without recovery, retry storms
   surface as user-facing errors.

## Appendix C — Known underspecified areas (DRAFT)

The following items will be tightened before v1.0 final, driven by
experience implementing non-Python clients. Listed here so implementers
know exactly what is still squishy and what to watch for.

- **Composite-scope sort order.** Currently "lexicographic" (§9). Will
  tighten to "byte-wise comparison on UTF-8 encoded values" after a
  non-Python client validates this prevents deadlocks across locale
  boundaries. A Java client using `Locale.GERMAN` and a Go client using
  byte-wise comparison would otherwise produce different orders for
  non-ASCII scope IDs and could deadlock against each other.

- **NULL semantics in `MAX(actual, estimated)`.** Postgres `GREATEST` and
  SQLite `MAX()` differ in how they propagate NULL. Currently handled
  by wrapping `actual_cents` in `COALESCE(actual_cents, 0)` (§7) —
  confirmed correct for both reference backends but not validated against
  MySQL, MariaDB, or other engines that may be added later.

- **`NUMERIC(20, 6)` precision in non-Postgres backends.** SQLite stores
  numerics with type affinity (TEXT or REAL), not strict precision.
  Clients **MUST** convert through a fixed-point type (never a float)
  but the boundary conversion rules are not yet pinned. Languages without
  a standard fixed-point type (Node.js, plain Go) need an explicit
  recommended dependency.

- **Conformance fixture file format.** §15 lists fixture *categories* but
  not the concrete file format. The format will be specified once the
  second reference implementation begins, so it can be designed against
  a real cross-language consumer rather than speculatively. Until then,
  the Python reference client's fixture loader is the de facto format.

- **Sweeper concurrency in multi-server deployments.** §6.6 requires that
  sweep not lock the limit row; this is correct. But two sweeper
  processes running simultaneously across multiple pods will both attempt
  the same `UPDATE`, wasting work. May add an advisory-lock recommendation
  (e.g. `pg_advisory_lock` in Postgres) before lock criteria #1 is met.

- **`metadata` size cap.** §12 says SHOULD ≤ 4 KiB. May tighten to a
  hard limit if profiling shows it material; will stay SHOULD if not.

- **Schema version table.** Referenced in §16 ("`calyx_schema_version`
  table MUST record the highest applied migration") but not defined in
  §5. Will add to §5 in the next DRAFT revision.

When this appendix is empty, lock criterion #2 is satisfied.
