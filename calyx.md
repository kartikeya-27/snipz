# Calyx — Positioning and Design Plan

Last updated: 2026-05-05

## What it is

A Python library that enforces LLM cost limits as a **reservation ledger** — pre-flight reserve, commit on success, release on failure. Embedded (no gateway), Postgres-first, transactional under concurrent load.

## Why it exists

Every team building LLM features rebuilds cost guardrails from scratch. The closest things on the market are either:
- gateways you route traffic through (Portkey, TrueFoundry, LiteLLM Proxy), or
- importable libraries that *track* spend but cannot prevent over-spending under concurrency (LiteLLM `BudgetManager`, Shekel, litellm-cost-tracker).

None ship a real reservation ledger. That is the gap.

---

## Competitor landscape (May 2026)

| Capability | LiteLLM BudgetManager (SDK) | Shekel | Portkey | LiteLLM Proxy | Calyx |
|---|---|---|---|---|---|
| Embedded library (no gateway) | Yes | Yes | No | No | Yes |
| Pre-flight cap enforcement | Yes | Yes | Yes | Yes | Yes |
| Per-user / per-tenant scopes | Yes | Via named contexts | Yes | Yes | Yes |
| Reservation → commit/release ledger | No (estimate-then-record) | No (post-hoc tracking) | No | No | **Yes** |
| Postgres backend | No (JSON file or HTTP) | No (Redis only) | n/a (SaaS) | Yes via proxy | **Yes** |
| SQLite single-node | No | No (in-process only) | No | No | **Yes** |
| Concurrency-safe cap check | No (plain dict, race conditions) | Single-process via ContextVar | Yes | Yes | **Yes** (`SELECT FOR UPDATE`) |
| Streaming-aware (abort mid-stream) | No | No | No | No | **Yes** |
| Idempotency keys (retry-safe) | No | No | No | Partial | **Yes** |
| Refund on failure | No | No | No | No | **Yes** |
| Composite scopes (user AND tenant atomically) | No | Hierarchy only | Partial | Partial | **Yes** |

---

## What others lack — what Calyx delivers

### 1. Reservation-ledger semantics
LiteLLM's BudgetManager and Shekel both estimate, check, call, log. If two requests arrive at $4.95 of a $5.00 cap concurrently, both pass the check and both execute — overshooting the cap. There is no atomic reserve-and-decrement.

**Calyx:** pre-flight reserve inside a transaction with `SELECT FOR UPDATE` on the limit row. Commit on success, release on failure. The cap is never exceeded under concurrent load.

### 2. Postgres-first storage
Shekel's distributed mode is Redis-only. LiteLLM SDK persists to JSON-on-disk. Neither offers:
- Postgres — for teams who already run it and want one DB
- SQLite — for single-node deployments and tests
- Auditable, queryable spend history — a JSON blob is not a real ledger

**Calyx:** protocol-based backends. Postgres and SQLite from day one. A schema you can query directly.

### 3. Streaming-native
Neither competitor handles the case where actuals diverge from estimates mid-stream. With Anthropic's 200k-context responses this matters — a streaming response can blow through a tight cap before the SDK call returns.

**Calyx:** `Reservation.observe()` updates actuals incrementally. If projected total breaches the cap, you can abort cooperatively. Reconciles correctly on partial failure.

---

## Threat assessment

- **Shekel** — 5 stars, single author, Redis-only, no reservations. Real but not a moat. Roughly 6–9 months of mind-share lead.
- **LiteLLM BudgetManager** — bundled with a popular framework but architecturally weak. Assume someone PRs reservations into LiteLLM within ~12 months.

That gives a window of roughly 12 months to ship a clearly better primitive and become the reference implementation.

## Reuse vs. rebuild

- **Do not fork Shekel.** Its monkey-patching architecture fights the reservation model — you would need to intercept *before* the SDK call, but Shekel intercepts *at* the SDK call. Architectural mismatch.
- **Do reuse LiteLLM's pricing data.** They maintain prices for 100+ models, MIT-licensed. Vendor it.
- **Do study Shekel's nested-budget DX.** `with budget(max_usd=5.00):` is good ergonomics. Adopt the pattern, replace the engine.

---

## Design steps

### Phase 0 — Validate the reservation model on paper (1 day)
- Write the cap-check SQL with `SELECT FOR UPDATE`.
- Walk through 5 concurrency scenarios: two requests at the cap, retry storm, streaming overrun, crashed reservation, idempotent retry.
- Sanity-check the schema can answer "current spend per scope per window" in O(log n).

### Phase 1 — Core engine (~250 LOC)
- `Budget`, `Reservation`, `BudgetExceeded`, the SQL transactions.
- No decorators, no estimators, no provider integrations yet.
- API: `budget.reserve(scope, cents) → Reservation` with `commit()` / `release()` / `observe()`.
- Property tests for cap arithmetic — this is where most projects get it wrong.

### Phase 2 — Storage backends (~200 LOC)
- Protocol-based backend interface.
- `storage/postgres.py` — production.
- `storage/sqlite.py` — tests and single-node.
- Migrations versioned (Alembic or hand-rolled).

### Phase 3 — Pricing (~150 LOC)
- Vendor LiteLLM's pricing TOML.
- `Pricing.cost(provider, model, input_tokens, output_tokens) → cents`.
- `calyx update-pricing` CLI to refresh.

### Phase 4 — Estimators (~200 LOC)
- `estimators/anthropic.py` — Anthropic token counter.
- `estimators/openai.py` — tiktoken.
- `estimators/fallback.py` — generic.
- Each returns `(input_tokens, max_output_tokens)`.

### Phase 5 — Decorator API (~100 LOC)
- `@budget.guard(scope=..., estimate=..., actual=...)` thin wrapper.
- Match Shekel's `with budget(...)` ergonomics where it makes sense.

### Phase 6 — Reservation sweeper (~50 LOC)
- Background job to expire stuck reservations.
- CLI: `calyx sweep --interval 60`.
- Or callable from any existing scheduler.

### Phase 7 — Events / hooks (~50 LOC)
- `on_reserved`, `on_committed`, `on_released`, `on_overrun`.
- Plug-in points for metrics, alerting, audit logs — no coupling.

### Phase 8 — The killer demo (the marketing artifact)
- Concurrency correctness benchmark.
- Fire 1000 concurrent requests against a $5 cap.
- Verify Calyx never exceeds $5.
- Show LiteLLM BudgetManager and Shekel exceeding the cap.
- This is what sells the project — must ship before any landing-page work.

### Phase 9 — Provider integrations
- `calyx-anthropic` — auto-wraps `anthropic.messages.create`.
- `calyx-openai` — same for OpenAI.
- Optional packages; core stays clean.

### Phase 10 — v0 release
- README leads with the correctness story, not "no gateway."
- Three bullets:
  1. Reservation-ledger model — never overshoots the cap.
  2. Postgres-first — fits your existing stack.
  3. Streaming-native — observe and abort mid-flight.
- Link the benchmark prominently.

---

## Open questions to resolve before Phase 1

1. **Storage units** — `NUMERIC(20, 6)` cents (readable, no float drift via decimal arithmetic) vs. integer micro-dollars. Lean: numeric.
2. **Composite scopes** — does a single call debit both `(user, X)` and `(tenant, Y)` budgets atomically? Probably yes. Adds SQL complexity.
3. **Time windows** — calendar boundary (resets on day/month start) or rolling 30 days? Calendar simpler; rolling as opt-in.
4. **Pricing freshness** — versioned TOML in package + manual `update-pricing`, or pull from a CDN at runtime? Lean: versioned TOML.

## Open questions for the longer term

1. Generalize beyond LLM — any API call with a known unit price? Broader, but loses focus. Stay narrow for v0.
2. Anthropic prompt caching — pricing schema needs `cache_read_cents_per_m` (already in the proposed schema). Estimators must split cached vs. uncached.
3. Multi-region / multi-account budgets — out of scope for v0.

---

## Future scope: Rust core (v2+)

Calyx is Python-first by design — the hot path is database I/O, not CPU. A Rust core makes sense only when there is a measurable performance claim to put on the README.

### Why Python wins for v0/v1

The hot path is `BEGIN → SELECT FOR UPDATE → SUM aggregate → INSERT → COMMIT`. Postgres takes 2–5 ms doing that. A Rust caller vs. a Python caller might shave 100 µs off the overhead — invisible next to the DB round-trip. There is no headline number to print today.

Python also wins on:
- Caller code is Python (token estimators, hooks, decorators, provider SDKs).
- One `pyproject.toml` build vs. PyO3 + maturin + per-platform wheels.
- Faster iteration on semantics — Phase 0–4 are about getting the design right.
- Larger contributor pool.

### Why "Rust-powered" without a number is hollow

Every successful "Rust-powered" Python project earned the badge with a measurable speedup:

| Project | Speedup claim |
|---|---|
| Polars | 10x faster than pandas |
| ruff | 100x faster than flake8/pylint |
| uv | 10–100x faster than pip |
| Pydantic v2 | 5–50x faster validation |
| tiktoken | 3–6x faster tokenization |

Engineers reading "Rust-powered" expect a number. Shipping the badge without one looks like resume-driven development and invites a Python competitor to benchmark against you with identical results — making the Rust badge a liability.

### When the v2 jump is justified

- Production users hit a real ceiling — e.g., 50k+ reservations/sec where Python overhead is measurable.
- Demand for embedding Calyx in non-Python apps (Rust services, Go, Node).
- A benchmark shows a credible 3x+ speedup at burst load.

If none materialize, v2 stays Python. **Don't ship Rust without a number.**

### Properties of the eventual migration

1. **Partial, not total.** Only the hot path moves: cap-check transaction, ledger arithmetic, sweeper. Decorator API, estimators, event hooks, and provider integrations stay in Python — that is where the value and contributor base live.
2. **API-compatible.** `pip install calyx==2.0` keeps the same `from calyx import Budget`. Users see faster code, not a rewrite.

The migration model is **Pydantic v1 → v2**: same public surface, Rust core, headline perf claim. The architecture in [architecture.md](architecture.md) already separates the hot path from the integration layer, so this rewrite path is open.

### Versioning sketch

| Version | Horizon | Implementation |
|---|---|---|
| v0.x | months 0–3 | Pure Python; design settling; breaking changes OK |
| v1.0 | months 3–6 | First stable API; backward-compatible from here |
| v1.x | months 6–12 | Polish, real-world profiling |
| v2.0 | months 12+ | Rust hot path **iff** perf data justifies it |

---

## Sources (deep-dive material)

- [LiteLLM Budget Manager docs](https://docs.litellm.ai/docs/budget_manager)
- [LiteLLM Budgets, Rate Limits (proxy)](https://docs.litellm.ai/docs/proxy/users)
- [LiteLLM Budget enforcement bug #25799](https://github.com/BerriAI/litellm/issues/25799)
- [LiteLLM Budget enforcement bug #12905](https://github.com/BerriAI/litellm/issues/12905)
- [litellm-cost-tracker on PyPI](https://pypi.org/project/litellm-cost-tracker/)
- [Shekel — arieradle/shekel](https://github.com/arieradle/shekel)
- [GitHub topic: llm-budget-control](https://github.com/topics/llm-budget-control)
- [Portkey Enforce Budget Limits](https://portkey.ai/docs/product/administration/enforce-budget-and-rate-limit)
- [Portkey Alternatives 2026](https://www.buildmvpfast.com/alternatives/portkey)
- [Tokencost (counting only)](https://github.com/AgentOps-AI/tokencost)
- [llm_cost_estimation on PyPI](https://pypi.org/project/llm_cost_estimation/)
- [Anthropic API pricing 2026](https://www.finout.io/blog/anthropic-api-pricing)
