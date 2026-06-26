# Snipz

**An LLM cost reservation ledger for Python.** Cap your spend per user, per tenant, per feature — and never overshoot, even under concurrent load.

> **Status:** v0.0.x — pre-1.0. The engine is feature-complete and the [cap-correctness benchmark](#cap-correctness-benchmark) passes on real Postgres. API may shift before v1.0.

```python
async with await budget.reserve(Scope("user", "u_42"), Decimal("10")) as r:
    response = await call_anthropic(...)
    await r.observe(price(response))   # exit auto-commits at observed cost
# on exception: auto-release; the cap is never overshot
```

---

## Why

Every team building LLM features rebuilds cost guardrails from scratch. Existing libraries (LiteLLM `BudgetManager`, Shekel) follow an **estimate-then-record** pattern — they check the cap, run the call, then log the spend. Under concurrent load this lets two requests both pass a cap check at $4.95 of a $5.00 cap and both run, blowing the cap.

Snipz is a **reservation ledger**: every call holds budget inside a transaction with `SELECT … FOR UPDATE` (or `BEGIN IMMEDIATE` on SQLite) before the LLM runs, commits the actual cost on success, releases on failure. The cap-check and the ledger insert are a single atomic step. The cap is never overshot.

---

## Cap-correctness benchmark

The proof: fire 1000 concurrent reservations of $0.10 each against a $5.00 cap on real Postgres.

```
Snipz Postgres pool near capacity (10/10). Consider increasing max_size for high-throughput environments.

Snipz cap-correctness benchmark
===============================
  Concurrency: 1000
  Cap:         $5.00
  Per-req:     $0.10
  Duration:    2.755s

  Cap     [########################################] $5.00
  Spend   [########################################] $5.00

  Reservations attempted: 1000
  Expected successes:     50
  Actual successes:         50  (  5%)
  Rejected (cap):          950  ( 95%)
  Lock timeouts:             0  (  0%)
  Other errors:              0  (  0%)

  CAP HELD: $5.00 <= $5.00 (no overshoot)
```

Exactly 50 reservations committed, 950 raised `BudgetExceededError`, final spend `$5.00`. The line above the chart is Snipz's own pool-utilization warning firing — observability is a feature ([architecture.md](architecture.md) Decision Log #21).

Reproduce in one command (needs Docker for the auto-spun Postgres container):

```bash
uv run python benchmarks/cap_correctness.py --testcontainers-postgres --concurrency 1000
```

No Docker? Default SQLite scenario at 100 concurrent works without any setup:

```bash
uv run python benchmarks/cap_correctness.py
```

---

## Quickstart

```python
import asyncio
from decimal import Decimal
from snipz import Budget, Scope

async def main():
    budget = Budget("snipz.db")                                # or "postgresql://..."
    await budget.migrate()
    await budget.set_limit(Scope("user", "u_42"), Decimal("500"))   # $5/month cap

    async with await budget.reserve(Scope("user", "u_42"), Decimal("10")) as r:
        response = await call_anthropic(...)
        await r.observe(price_from_usage(response.usage))
        # exit auto-commits at observed cost; auto-releases on exception

    await budget.close()

asyncio.run(main())
```

What you can rely on:

- **Atomic cap-check.** Two concurrent reserves at the cap → one wins, one raises `BudgetExceededError`. Verified by the benchmark above.
- **Idempotent retries.** Pass `request_id="..."` to `reserve()`; parallel retries with the same id converge on one ledger row.
- **Streaming-aware.** `r.observe(actual)` updates the in-flight cost mid-stream; the cap-check formula uses `MAX(actual, estimated)` so concurrent requests see the true running total.
- **Late-commit safety.** If your call takes longer than the TTL, the sweeper releases the row; a subsequent `commit()` still settles cleanly and fires `on_overrun`.
- **Multi-scope.** Reserve against `[user_scope, tenant_scope, feature_scope]` in one call — all caps checked atomically, atomic rollback on any failure.

---

## Install

```bash
pip install snipz                  # core: SQLite, async
pip install snipz[postgres]        # + asyncpg for Postgres
pip install snipz[openai]          # + tiktoken for exact OpenAI token counts
```

---

## What's in the box

| What | Where | What it's for |
|---|---|---|
| `Budget`, `Reservation`, `Scope` | `from snipz import ...` | The async engine — reserve / commit / release / observe |
| Sync wrapper (experimental) | `from snipz.sync import Budget` | For sync codebases — background event loop, raises if called from inside an async loop |
| `Pricing` | `from snipz import Pricing` | Vendored price book (`Pricing.default()`) + DB overrides (`Pricing.with_backend(...)`) |
| Estimators | `from snipz.estimators import AnthropicEstimator, OpenAIEstimator, FallbackEstimator` | Pre-flight token counters; OpenAI is exact via tiktoken |
| `@budget.guard` | `budget.guard(scope=..., estimate=..., actual=...)` | Decorator that wraps an async LLM call with the full reserve/observe/commit/release lifecycle |
| Hooks | `budget.on_reserved`, `on_committed`, `on_released`, `on_overrun` | Plug-in points for metrics, alerting, audit logs |
| Sweeper | `snipz sweep [--interval N]` CLI or `snipz.sweep.sweep_loop()` | Background job that releases expired reservations |
| `snipz update-pricing` | CLI | Refresh the vendored pricing.toml from LiteLLM upstream |

All async/sync surfaces share the same engine and correctness guarantees.

---

## Deep dives

- [`snipz.md`](snipz.md) — positioning, competitor analysis, build phases
- [`architecture.md`](architecture.md) — layered architecture, schema, full decision log
- [`snipz-protocol.md`](snipz-protocol.md) — wire protocol spec (DRAFT — comments open)
- [`scenarios.md`](scenarios.md) — concurrency walkthroughs

---

## Development

```bash
uv sync                          # install all deps + .venv
uv run pytest                    # 140 tests against SQLite
uv run pytest --postgres         # + 15 Postgres integration tests (needs Docker)
uv run ruff check src/ tests/ benchmarks/
uv run mypy src/snipz benchmarks/
```

---

## License

MIT
