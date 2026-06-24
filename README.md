# Snipz

LLM cost reservation ledger for Python. Pre-flight reserve → commit on success → release on failure. Embedded library, Postgres-first, transactional under concurrent load.

> **Status:** v0.0.x — implementation through Phase 7 shipped (reservation engine, Postgres + SQLite backends, sync wrapper, pricing book + CLI, token estimators, decorator, sweeper, event hooks). v0.1.0 follows Phase 8's correctness benchmark.

## Why

Every team building LLM features rebuilds cost guardrails from scratch. Existing libraries (LiteLLM `BudgetManager`, Shekel) estimate then record — they cannot prevent over-spending under concurrent load. Snipz is a reservation ledger: it reserves budget inside a transaction with `SELECT … FOR UPDATE`, commits on success, releases on failure. The cap is never overshot.

## Quickstart

```python
import asyncio
from decimal import Decimal
from snipz import Budget, Scope

async def main():
    budget = Budget("snipz.db")  # SQLite path; or "postgresql://..." for Postgres
    await budget.migrate()
    await budget.set_limit(Scope("user", "u_42"), Decimal("500"))   # $5.00 monthly cap

    async with await budget.reserve(Scope("user", "u_42"), Decimal("10")) as r:
        response = await call_anthropic(...)            # your LLM call
        actual_cents = compute_cost(response)
        await r.observe(actual_cents)                   # streaming-aware update
        # on success: auto-commit at observed cost
        # on exception: auto-release

    await budget.close()

asyncio.run(main())
```

## Install

```bash
pip install snipz                  # core (SQLite, async)
pip install snipz[postgres]        # + asyncpg for Postgres backend
pip install snipz[openai]          # + tiktoken for exact OpenAI token counting
```

## API surface

- **`from snipz import Budget`** — async reservation engine
- **`from snipz.sync import Budget`** — experimental sync wrapper (background event loop; raises `RuntimeError` if called from inside an active asyncio loop)
- **`from snipz import Pricing`** — `Pricing.default()` for vendored prices, `Pricing.with_backend(...)` for DB overrides, `.cost(...)` for per-call computation
- **`from snipz.estimators import AnthropicEstimator, OpenAIEstimator, FallbackEstimator`** — pre-flight token counters
- **`@budget.guard(scope=..., estimate=..., actual=...)`** — decorator that wraps an async LLM call with reserve/observe/commit/release
- **`budget.on_reserved`, `on_committed`, `on_released`, `on_overrun`** — observability hooks for metrics, alerting, audit logs
- **`from snipz.sweep import sweep_loop`** — long-running expirer for stuck reservations (plus `snipz sweep --interval N` CLI)
- **`snipz update-pricing`** — refresh the vendored pricing.toml from LiteLLM upstream

Both async and sync surfaces wrap the same engine and share the same correctness guarantees.

## Design documents

- [`snipz.md`](snipz.md) — positioning, competitor analysis, build phases.
- [`architecture.md`](architecture.md) — layered architecture, schema, decision log.
- [`snipz-protocol.md`](snipz-protocol.md) — wire protocol spec (DRAFT — comments open).
- [`scenarios.md`](scenarios.md) — Phase 0 concurrency walkthroughs.

## Development

```bash
uv sync                          # install all deps + .venv
uv run pytest                    # SQLite tests (default; ~130 tests)
uv run pytest --postgres         # + Postgres integration tests (requires Docker)
uv run ruff check src/ tests/
uv run mypy src/snipz
```

## License

MIT
