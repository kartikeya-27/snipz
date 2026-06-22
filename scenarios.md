# Brim — Concurrency Scenarios (Phase 0 Paper Validation)

Last updated: 2026-05-05

Purpose: walk through 5 concurrency scenarios against the schema in [architecture.md](architecture.md) before any code is written. Each scenario has a trace, an assertion, and any findings that surface design bugs.

---

## Findings summary

Three issues surfaced during this walkthrough. **Architecture must be updated before Phase 1.**

1. **Cap-check SUM formula is wrong for committed rows.** The current `SUM(GREATEST(actual_cents, estimated_cents))` over-counts committed rows where actual < estimated. Should be a CASE expression on state. Details in Scenario 4.
2. **Idempotency flow is under-specified.** `architecture.md` says there's a unique index on `request_id`, but doesn't spell out the SELECT-then-INSERT-with-conflict-handling required to make retries safe. Details in Scenario 2.
3. **Streaming partial-failure semantics depend on caller behavior.** If `observe()` recorded actual > 0 and then the call fails, the caller must `commit()` not `release()` — otherwise the budget refunds money that was actually billed by the provider. Needs a docs note. Details in Scenario 3.

---

## Scenario 1 — Burst at cap edge

Tests: cap is never exceeded under concurrent reservations.

### Setup

```
brim_limits:
  ('user', 'u1', 'month', cap_cents=500.000000, grace_pct=0)

brim_ledger (current month):
  state='committed', estimated=495.000000, actual=495.000000
  -- $4.95 already spent
```

Two requests A and B arrive concurrently, each wanting to reserve $0.10.

### Trace

```
T=0ms   A: BEGIN
T=0ms   B: BEGIN
T=1ms   A: SELECT * FROM brim_limits WHERE (user,u1,month) FOR UPDATE
        → acquires lock; cap=500, grace=0
T=1ms   B: SELECT FOR UPDATE → blocks waiting for A's lock
T=2ms   A: SELECT SUM(...) → spent = 495
T=2ms   A: 495 + 10 = 505 > 500 → BudgetExceededError
T=2ms   A: ROLLBACK   (lock released)
T=3ms   B: now acquires lock
T=3ms   B: SELECT SUM(...) → spent = 495 (A rolled back, no change)
T=3ms   B: 495 + 10 = 505 > 500 → BudgetExceededError
T=3ms   B: ROLLBACK
```

**Both fail. Cap stays at $4.95 spent. Assertion holds.**

### Counter-example: what happens without `SELECT FOR UPDATE`

If both A and B read `SUM` in parallel without locking the limit row:

```
T=2ms   A reads SUM=495, computes 495+10=505 > 500 → fail
T=2ms   B reads SUM=495 (parallel, no lock), computes 495+10=505 > 500 → fail
```

Same result here. Now change setup to $4.80 spent:

```
T=2ms   A reads SUM=480, 480+10=490 ≤ 500 → pass; INSERT (reserved, est=10)
T=2ms   B reads SUM=480 (parallel, no lock), 480+10=490 ≤ 500 → pass; INSERT (reserved, est=10)
        Total reserved = 500. OK in this case.
```

Now $4.95 with $0.10 each:

```
T=2ms   A reads SUM=495, 495+10=505 > 500 → fail
        OR with looser slack ($4.85 + 2× $0.10):
T=2ms   A reads SUM=485, 485+10=495 ≤ 500 → pass; INSERT
T=2ms   B reads SUM=485 (no lock!), 485+10=495 ≤ 500 → pass; INSERT
        Total reserved = 505. CAP EXCEEDED BY $0.05.
```

This is the bug Shekel and LiteLLM `BudgetManager` have. `SELECT FOR UPDATE` is what prevents it. **Verified.**

---

## Scenario 2 — Idempotent retry storm

Tests: 10 parallel requests with the same `request_id` produce exactly one ledger row.

### Setup

```
brim_limits: ('user', 'u1', 'month', cap_cents=10000)
brim_ledger: (empty for this scope)
```

Caller fires the same `request_id='req_abc'` 10 times in parallel (network timeout caused retries while the original was actually still in flight).

### Required flow per request

```
1. SELECT * FROM brim_ledger WHERE request_id = $1
   If found → return existing Reservation. Done.

2. BEGIN
3. SELECT * FROM brim_limits WHERE ... FOR UPDATE
4. SELECT SUM(...) → spent
5. Check spent + estimate ≤ cap; if no, ROLLBACK and raise BudgetExceededError
6. INSERT INTO brim_ledger (..., request_id = $1)
   On UNIQUE violation:
     ROLLBACK
     SELECT * FROM brim_ledger WHERE request_id = $1
     return that Reservation
7. COMMIT
```

### Trace

```
T=0ms     R1...R10: SELECT WHERE request_id='req_abc' → not found, all proceed
T=1ms     R1...R10: BEGIN
T=2ms     R1: SELECT FOR UPDATE limit → acquires
T=2ms     R2-R10: blocks on limit lock
T=3ms     R1: cap-check passes; INSERT row (request_id='req_abc'); COMMIT
T=4ms     R2: acquires lock; cap-check (now sees R1's reservation); 
              INSERT WHERE request_id='req_abc' → UNIQUE violation
T=4ms     R2: ROLLBACK; SELECT WHERE request_id='req_abc' → finds R1's row
T=4ms     R2: returns R1's Reservation
T=5ms+    R3-R10: same as R2
```

**Result: exactly one ledger row, all 10 callers receive the same Reservation. Assertion holds.**

### Edge: R1 raised BudgetExceededError

If R1 failed cap-check and rolled back, no row exists. R2 acquires the lock, runs cap-check, and either succeeds (in which case R3-R10 collide on R2's row and return it) or also fails. The "first successful reservation wins" property is preserved.

### Finding 2

`architecture.md` mentions `UNIQUE INDEX ... ON request_id` but doesn't spell out the SELECT → BEGIN → cap-check → INSERT-with-conflict-handling sequence above. **Add this to the architecture spec before Phase 1.**

---

## Scenario 3 — Streaming overrun

Tests: `observe()` updates that drive actual past estimate are reflected correctly in the cap-check for subsequent reservations.

### Setup

```
brim_limits: ('user', 'u1', 'month', cap_cents=1000)
brim_ledger: (empty)
```

### Trace

```
T=0ms     R1: reserve(cents=500)
          INSERT (state='reserved', estimated=500, actual=NULL)

T=100ms   R1: observe(input=500, output=200, model='claude-sonnet-4-6')
          UPDATE actual_cents = 350

T=200ms   R1: observe(input=500, output=400)
          UPDATE actual_cents = 600   -- now exceeds estimate

T=300ms   R2 arrives: reserve(cents=500)
          BEGIN; SELECT FOR UPDATE limit;
          SELECT SUM(...) → R1 contributes max(actual=600, est=500) = 600
          600 + 500 = 1100 > 1000 → BudgetExceededError ✓

T=500ms   R1: observe(input=500, output=600)
          UPDATE actual_cents = 850

T=600ms   R1: commit() → state='committed', actual_cents=850, settled_at=now()
```

After commit, R3 arrives:

```
SELECT SUM(...) WHERE state IN ('reserved','committed')
  R1 (committed, est=500, actual=850)
```

**This is where Finding 1 surfaces.**

The architecture currently says `SUM(GREATEST(actual, est))`. For R1 that gives `GREATEST(850, 500) = 850`. **Correct in this case** — but only because actual > estimate.

Now consider R1 had committed at actual=300 (under estimate, e.g., the response was shorter than expected). `GREATEST(300, 500) = 500`. The query reports $5.00 spent when in reality only $3.00 was — over-counting by $2.00 for the rest of the window.

The estimator-as-floor logic only makes sense for *in-flight* (reserved) rows. Once a row is committed, we know the real cost.

### Corrected SUM formula

```sql
SUM(
  CASE
    WHEN state = 'committed' THEN actual_cents
    WHEN state = 'reserved'  THEN GREATEST(COALESCE(actual_cents, 0), estimated_cents)
  END
)
```

For reserved rows: `actual` may be NULL (no observe yet) → COALESCE to 0 → GREATEST gives estimate. If observe pushed actual above estimate → GREATEST gives actual. Both correct.

For committed rows: just use actual. The estimate served its purpose during the in-flight phase.

### Finding 1 — action

**Update [architecture.md](architecture.md) cap-check query and Migrations section before Phase 1 begins.**

### Finding 3 — partial-failure streams

If R1's call fails mid-stream after observing actual=600:
- If caller calls `release()`: state → released, actual_cents=600 is preserved on the row but excluded from cap-check (released doesn't count). **The user got $6.00 of budget refunded but Anthropic billed them for the partial output.** Real money lost from the budget's perspective.
- If caller calls `commit()`: state → committed, actual_cents=600 counted. The provider was billed; the budget reflects it. Correct.

**Caller responsibility:** if any billable content was received (observe was called with non-zero output), commit on failure rather than release. Document this prominently.

---

## Scenario 4 — Crashed reservation + late commit

Tests: sweeper releases stuck reservations after TTL; late commits succeed with `late=true`.

### Setup

```
brim_limits: ('user', 'u1', 'month', cap_cents=1000)
brim_ledger: (empty)
TTL = 300s
```

### Trace

```
T=0s      R1: reserve(cents=500, ttl=300)
          INSERT (state='reserved', expires_at=NOW+300s)

T=10s     R1's process makes the API call; network stalls indefinitely.

T=305s    Sweeper runs (every 60s).
          UPDATE brim_ledger SET state='released', late=TRUE
            WHERE state='reserved' AND expires_at < NOW()
          → R1's row updated.

T=310s    R2 arrives: reserve(cents=500)
          SUM(...) WHERE state IN ('reserved','committed'):
            R1 is 'released' → excluded
            → spent = 0
          0 + 500 ≤ 1000 → pass; INSERT R2.

T=400s    R1's API call finally returns (network unstuck, response landed). 
          Caller invokes commit() with actual=480.
          
          UPDATE brim_ledger 
            SET state='committed', actual_cents=480, late=TRUE, settled_at=NOW()
            WHERE id=R1 AND state='released' AND late=TRUE
          
          on_overrun event fires.

T=405s    R3 arrives: reserve(cents=300)
          SUM(...) WHERE state IN ('reserved','committed'):
            R1 (committed, late, actual=480) → 480
            R2 (reserved, est=500, actual=NULL) → max(0, 500) = 500
            spent = 980
          980 + 300 = 1280 > 1000 → BudgetExceededError ✓
```

**Late-committed cost (R1's $4.80) correctly accounted in subsequent cap checks. Assertion holds.**

### Sweeper coordination

Does the sweeper need to lock the limit row when releasing reservations? **No.** Released rows are excluded from cap-check, so a release operation doesn't change anyone's spent. Concurrent reserves can proceed without seeing the sweeper. Just `UPDATE … WHERE state='reserved' AND expires_at < NOW()`.

### Late commit on caller-released row

If state='released' but late=FALSE (caller explicitly aborted), the late commit's WHERE clause `AND late=TRUE` returns 0 rows. Caller-aborted rows are terminal — no transition to committed. Correct.

---

## Scenario 5 — Composite scope partial failure

Tests: a reserve against multiple scopes is atomic — if any cap fails, none are debited.

### Setup

```
brim_limits:
  ('tenant', 't1', 'month', cap_cents=20000)   -- $200 tenant cap
  ('user',   'u1', 'month', cap_cents=10000)   -- $100 user cap

brim_ledger:
  ('tenant', 't1', state='committed', actual=19900)   -- tenant has $199 spent
```

User u1 makes a request that must debit BOTH (user, u1) AND (tenant, t1). Wants to reserve $5.

### Trace

```
Step 1  Sort scopes deterministically by (scope_type, scope_id, window):
        ('tenant', 't1', 'month')  ← acquired first
        ('user',   'u1', 'month')

Step 2  BEGIN

Step 3  SELECT * FROM brim_limits WHERE (tenant,t1,month) FOR UPDATE
        → acquires; cap=20000

Step 4  SELECT SUM(...) for tenant/t1 → spent=19900
        19900 + 500 = 20400 > 20000 → BudgetExceededError(scope=tenant/t1)

Step 5  ROLLBACK
        - Tenant lock auto-released
        - User lock NEVER acquired (failed before reaching it)
        - No ledger rows inserted
```

**Tenant cap honored. User scope not partially debited. Atomicity holds.**

### Both pass case

If both caps would pass:

```
Step 3  Lock tenant/t1
Step 4  SUM tenant/t1 = X; check X + 500 ≤ 20000 → pass
Step 5  Lock user/u1   (still inside same tx)
Step 6  SUM user/u1   = Y; check Y + 500 ≤ 10000 → pass
Step 7  reservation_id = R = uuid_generate()
        INSERT (reservation_id=R, scope=tenant/t1, ...)
        INSERT (reservation_id=R, scope=user/u1,   ...)
Step 8  COMMIT
```

`Reservation.commit()` later: `UPDATE brim_ledger SET state='committed', ... WHERE reservation_id=R` updates both rows atomically.

### Deadlock check

Two concurrent callers, each with overlapping scopes:

```
Caller X: scopes (user,u1) AND (tenant,t1)  → sorted: t1, u1
Caller Y: scopes (user,u1) AND (tenant,t2)  → sorted: t2, u1
```

X locks t1 first, then waits for u1.
Y locks t2 first, then waits for u1.
One acquires u1, the other waits.
**No cycle → no deadlock.**

The deterministic sort guarantees every caller acquires locks in the same global ordering for any subset of scopes they touch. Cycles are impossible. Verified.

---

## Phase 1 prerequisites — what must be fixed in `architecture.md`

Before writing any code:

1. **Update the cap-check SUM formula** to the CASE expression in Finding 1.
2. **Add the idempotency flow** (SELECT-then-INSERT-with-conflict-handling) from Scenario 2 to the schema section.
3. **Document caller responsibility** for streaming partial-failure (commit-on-billable-content rule) from Finding 3.
4. **Document sweeper non-locking** behavior from Scenario 4 — released rows don't need limit-row coordination.

Once those are in, the schema is correct and Phase 1 can begin.
