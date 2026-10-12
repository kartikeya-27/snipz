# Snipz

**An LLM cost reservation ledger for Python.** Cap your spend per user, per tenant, per feature — and never overshoot, even under concurrent load.

> **Status:** v0.3.x — pre-1.0. The engine is feature-complete; the [head-to-head benchmark](#head-to-head-correctness-benchmark) holds the cap on real Postgres at 1000 concurrent reservations while LiteLLM `BudgetManager` and Shekel overshoot by 20×. API may shift before v1.0; pin `snipz>=0.3,<0.4` to allow patches and forbid breaks.

```python
async with await budget.reserve(Scope("user", "u_42"), Decimal("10")) as r:
    response = await call_anthropic(...)
    await r.observe(price(response))   # exit auto-commits at observed cost
# on exception: auto-release; the cap is never overshot
```

---

## Why

Every team building LLM features rebuilds cost guardrails from scratch. Existing libraries (LiteLLM `BudgetManager`, Shekel) follow an **estimate-then-record** pattern — they check the cap, run the call, then log the spend. Under concurrent load this lets two requests both pass a cap check at $4.95 of a $5.00 cap and both run, blowing the cap. The [benchmark below](#head-to-head-correctness-benchmark) shows them overshooting a $5.00 cap by **20×** at 1000 concurrent — spending $100.00 instead of $5.00 — not a typo.

Snipz is a **reservation ledger**: every call holds budget inside a transaction with `SELECT … FOR UPDATE` (or `BEGIN IMMEDIATE` on SQLite) before the LLM runs, commits the actual cost on success, releases on failure. The cap-check and the ledger insert are a single atomic step. The cap is never overshot.

---

## Head-to-head correctness benchmark

The proof: 1000 concurrent reservations of $0.10 each against a $5.00 cap. Same workload, three backends, side-by-side.

```
Cap-correctness comparison — Snipz vs. estimate-then-record competitors
=======================================================================
  Concurrency: 1000
  Cap:         $5.00
  Per-req:     $0.10

  Cap     [########################################] $5.00

  Snipz                 [########################################                                        ] $5.00    — ok (held)
  LiteLLM BudgetManager [################################################################################] $100.00  — OVERSHOT by $95.00
  Shekel                [################################################################################] $100.00  — OVERSHOT by $95.00

  Headline claim reproduced: Snipz held the cap; LiteLLM BudgetManager, Shekel overshot.
```

| Backend | Successes | Final spend | Cap held? | Duration |
|---|---:|---:|:---:|---:|
| **Snipz** (Postgres) | 50 / 1000 | **$5.00** | yes | 3.6s |
| LiteLLM `BudgetManager` | 1000 / 1000 | $100.00 (20× cap) | no | 0.03s |
| Shekel | 1000 / 1000 | $100.00 (20× cap) | no | 0.03s |

Snipz adds ~3.6 ms per reservation — a fraction of a percent on top of a 100–2000 ms LLM call. Against real LLM latency, the overhead is invisible: you pay single-digit milliseconds to make overshoot impossible. It does this by actually doing the work — open a transaction, take a row lock, sum the ledger, check the cap, insert if OK, commit. The competitors look ~120× faster in this microbenchmark only because they skip the lock entirely: two concurrent callers both read `current_cost=0.00`, both pass the check, both write — at 1000 concurrent on a $5 cap, every single attempt commits.

The benchmark uses a 1 ms simulated LLM-call gap between cap-check and cost-record. Real LLM calls are 100–2000 ms — the race window in production is **100–2000× larger** than the simulation. This is the conservative number.

Reproduce in one command (needs Docker + the `bench-competitors` extra):

```bash
pip install "snipz[bench-competitors]"
uv run python -m benchmarks.competitor_comparison --concurrency 1000
```

Or run just Snipz's single-backend cap-correctness benchmark (the same numbers, no competitors):

```bash
uv run python benchmarks/cap_correctness.py --testcontainers-postgres --concurrency 1000
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
- **Flat cost at any volume.** The cap check reads a running total kept in the same transaction as each write, so a scope with a million ledger rows this month reserves as fast as an empty one (~2 ms on SQLite).

---

## Anthropic integration

Wrap an `AsyncAnthropic` client and every `messages.create` call is reserved before it is sent and settled from `response.usage` afterwards — no cost code of your own:

```python
from anthropic import AsyncAnthropic
from snipz.integrations.anthropic import guarded

client = guarded(AsyncAnthropic(), budget)
message = await client.messages.create(
    scope=Scope("user", "u_42"),
    model="claude-opus-5-5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

- **Worst-case reservation.** Input is counted exactly with `messages.count_tokens`; output is `max_tokens`; with `cache_control`, every prompt token is priced as a cache write. Long-prompt tiers apply automatically.
- **Exact settlement.** Commits the billed cost from `usage`, including cache reads and 5-minute / 1-hour cache writes.
- **Failures settle safely.** An HTTP error status releases the reservation (nothing was billed); a timeout or cancellation commits the reserved bound (it may have been billed).
- **No double spend.** Pass `request_id=` and a repeat raises `DuplicateRequestError` instead of calling the API again.

Streaming goes through the SDK's own helper, and settles when the block exits:

```python
async with client.messages.stream(scope=Scope("user", "u_42"), model="claude-opus-5-5",
                                  max_tokens=1024, messages=[...]) as stream:
    async for text in stream.text_stream:
        print(text, end="")
```

| The stream… | Ledger records |
|---|---|
| ran to completion | the billed usage |
| stopped early, or failed after the first event | the exact prompt usage + `max_tokens` of output (generation may have continued until disconnect) |
| closed before any event | the reservation |
| failed to open with an HTTP error | nothing — released |

Not covered yet: server-tool fees billed per use (e.g. web search), and batch / priority service-tier rates.

---

## OpenAI integration

The same for the OpenAI Responses API — `create` and `stream`:

```python
from openai import AsyncOpenAI
from snipz.integrations.openai import guarded

client = guarded(AsyncOpenAI(), budget)
response = await client.responses.create(
    scope=Scope("user", "u_42"), model="gpt-5.6", max_output_tokens=1024, input="Hello",
)
```

- **Exact reservation.** Input is counted with `responses.input_tokens.count`; output is `max_output_tokens` (required). Caching is automatic, so on models that charge for cache writes every prompt token is reserved at the write rate.
- **Billed as OpenAI bills.** Uncached input, cached input, and cache writes each at their own rate; streams settle from the final `response.completed` / `incomplete` / `failed` event, or at the reservation if none arrived.
- **Rejected up front:** `background=True`, stored `prompt` templates, and `service_tier="priority"` — each would make the recorded cost wrong.

Not covered yet: Chat Completions, built-in tool fees (web search, file search), and flex / batch discounts.

---

## Install

**Requires Python 3.11+.** Tested on 3.11, 3.12, 3.13, and 3.14.

```bash
pip install snipz                       # core: SQLite, async
pip install snipz[postgres]             # + asyncpg for Postgres
pip install snipz[anthropic]            # + the budget-guarded Anthropic client
pip install snipz[openai]               # + the budget-guarded OpenAI client (and tiktoken)
pip install snipz[bench-competitors]    # + litellm, shekel to reproduce the head-to-head benchmark
```

---

## What's in the box

| What | Where | What it's for |
|---|---|---|
| `Budget`, `Reservation`, `Scope` | `from snipz import ...` | The async engine — reserve / commit / release / observe |
| Sync wrapper (experimental) | `from snipz.sync import Budget` | For sync codebases — background event loop, raises if called from inside an async loop |
| `Pricing` | `from snipz import Pricing` | Vendored price book (`Pricing.default()`) + DB overrides (`Pricing.with_backend(...)`) |
| Estimators | `from snipz.estimators import AnthropicEstimator, OpenAIEstimator, FallbackEstimator` | Pre-flight token counters; OpenAI is exact via tiktoken |
| Anthropic client | `from snipz.integrations.anthropic import guarded` | Budget-guarded `AsyncAnthropic`: reserve, call, settle from `usage` |
| OpenAI client | `from snipz.integrations.openai import guarded` | Budget-guarded `AsyncOpenAI` Responses API: reserve, call, settle from `usage` |
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
uv run pytest                    # SQLite suite (load tests at smoke size)
uv run pytest --postgres         # + Postgres integration tests (needs Docker)
uv run pytest --load -s          # load tests at 10,000 requests
uv run ruff check src/ tests/ benchmarks/
uv run mypy src/snipz benchmarks/
```

---

## License

MIT
