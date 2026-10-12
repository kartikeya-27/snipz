# Changelog

All notable changes to this project will be documented in this file. Format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). This project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0 conventions: per semver pre-1.0, MINOR bumps (`0.x.0`) may include breaking
changes; PATCH bumps (`0.x.y`) are bug fixes only.

## [Unreleased]

### Added

- **`snipz.integrations.anthropic.guarded()`** — wraps an `AsyncAnthropic`
  client so every `messages.create` call reserves its worst-case cost before
  it is sent and commits the billed cost from `response.usage` afterwards.
  Install with `pip install snipz[anthropic]`.
  - Input is counted exactly with `messages.count_tokens` by default;
    `input_tokens="estimate"` opts into the local estimator for text-only
    requests (faster, but a heuristic — Claude's tokenizer varies by model).
    Output is bounded by `max_tokens`, which is required.
  - With `cache_control` in the request, every prompt token is reserved at
    the cache-write rate (1-hour if any breakpoint asks for it).
  - Settlement rules: an HTTP error status releases (not billed); a
    timeout, dropped connection, or cancellation commits the reserved bound
    (may have been billed); an unpriceable response commits the bound and
    logs instead of raising; a failed commit raises `SettlementError`
    carrying the paid-for response.
  - A repeated `request_id` raises `DuplicateRequestError` instead of
    calling the API a second time.
  - **Streaming** via `client.messages.stream(scope=..., **params)` — the
    SDK's own helper, with the reservation taken on entering the
    `async with` block and the cost committed on exit: the billed usage for
    a completed stream; the exact prompt usage plus `max_tokens` of output
    for one that stopped early or failed after its first event; the
    reservation if no event arrived; released if opening failed with an
    HTTP error. `messages.create(stream=True)` is rejected in favour of it.
  - Not covered yet: per-use server-tool fees, batch / priority
    service-tier rates, and the beta `client.beta.messages` surface.
- **`Reservation.created`** — `False` when `Budget.reserve` returned an
  existing reservation for a repeated `request_id`, so callers can tell a
  duplicate from fresh work before spending money on it.
- **Load tests** (`tests/test_load.py`): thousands of concurrent guarded
  calls and streams with mixed failures and duplicate `request_id`s,
  reconciled against the ledger. Streams run through the real SDK over a
  scripted HTTP API (`tests/fake_anthropic_api.py`). Smoke size by default;
  `pytest --load` runs 10,000 requests, and CI runs them on SQLite and
  Postgres.

### Fixed

- **`guarded()` rejected every real `AsyncAnthropic` client** (unreleased
  code): the SDK wraps resource methods in a synchronous decorator, so the
  async-client check saw a plain function. It now unwraps first. Found by
  the new streaming tests, which drive the real SDK over a scripted HTTP API
  instead of a hand-written fake.
- **Connection errors escaping a stream's body counted as definitive.** The
  SDK raises the raw `httpx2` transport error mid-stream rather than
  `APIConnectionError`; those now count as possibly billed.
- **Cap checks slowed down as a scope's ledger grew.** Every reservation
  summed the scope's ledger rows for the window, so cost grew with traffic:
  492 ms per reserve + commit with 1M rows in the window (~2/s on that
  scope), 52 ms at 100K. Each limit row now keeps a running total for its
  current window, updated in the same transaction as every ledger write:
  2.2 ms at 1M rows — the same as an empty scope. Totals are integer
  micro-cents and match the ledger exactly; a 3,000-step randomized test
  checks every total against the ledger after every operation, on SQLite
  and Postgres.
- **SQLite failed with `database is locked` under concurrency.** With a few
  hundred concurrent reservations, writers waited in SQLite's polling busy
  handler and enough of them outlasted `busy_timeout` to fail — including
  commits after a paid call. Writers now queue in-process and share one
  long-lived writer connection: of 2,000 concurrent reserve + commit pairs,
  1,674 (84%) used to fail; now none do, and the batch finishes in ~2 s.

### Changed

- **Schema migration 0003** adds `window_start_at` and `window_spend_micros`
  to `snipz_limits`. Run `budget.migrate()` after upgrading. Existing limits
  start with an unknown total, rebuilt once on the scope's next reservation
  with a single SQL sum (~0.4 s for a 1M-row window).
- **Amounts are stored at 6 decimal places** (micro-cents, rounded half up)
  on every backend. Postgres already stored `NUMERIC(20, 6)`; SQLite stored
  floats.
- **Ledger rows take `created_at` from the engine's clock** (the one the cap
  check uses) instead of the database's.
- **Storage protocol (custom backends):** `LedgerConnection.lock_limit` is
  replaced by `lock_scope(scope_type, scope_id)`, which locks every limit row
  of the scope; `current_spend` takes `window` and `now` instead of
  `window_start_at`; `insert_ledger_row` takes `created_at`; and every
  ledger write must keep the running totals current (`snipz.storage.spend`).
  On Postgres, writes also take a per-scope advisory lock (shared; exclusive
  for `set_limit`), and the sweeper now locks the scopes it releases —
  `snipz-protocol.md` §7.2 documents the contract for other clients.
- **Cost:** a few more statements per write. On SQLite, multi-scope
  reservations run at ~400 req/s instead of ~580 in the 10k load test;
  single-scope throughput is unchanged. `SqliteBackend.close()` now
  closes that connection (it reopens on next use); use one backend per
  event loop.

## [0.3.0] — 2026-10-09

The **compatibility and pricing release**: installs on Python 3.11+,
`Pricing.default()` prices current models again, and long prompts and
1-hour cache writes are no longer under-billed.

### Changed

- **Python floor lowered from 3.13 to 3.11.** Nothing in the engine needed
  3.13; the only 3.12+ syntax was two PEP 695 `type` aliases in
  `snipz.events` and one PEP 695 generic in `snipz.sync`, now spelled
  with `TypeAlias` / `TypeVar`. No runtime dependencies added. Python 3.10
  is not supported — it reaches end-of-life in October 2026.
- **Vendored `pricing.toml` refreshed from LiteLLM** — 10 hand-seeded models
  → 3,688 models across every LiteLLM provider, including the current Claude
  (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`,
  `claude-fable-5-1`), GPT-5, Gemini 2.5, Mistral, and xAI families. The file
  is now exactly what `snipz update-pricing` emits; `Pricing.default()` loads
  it in ~70 ms. Anthropic figures cross-checked against Anthropic's published
  pricing page.

  **Breaking for `Pricing.default()` lookups:** retired models LiteLLM no
  longer lists are gone (`claude-3-5-sonnet-20241022`, `claude-3-opus-20240229`,
  `claude-3-haiku-20240307`, `gemini-1.5-pro`, `gemini-1.5-flash`), and the
  Gemini provider key is now LiteLLM's `"gemini"` rather than `"google"`. Pin
  your own entries with `Pricing.from_toml` or the `snipz_pricing` table if you
  still need them.

### Fixed

- **`snipz update-pricing` failed with HTTP 404.** LiteLLM moved its
  catalogue; the default source is now
  `model_prices_and_context_window.json` at the repo root.
- **`snipz update-pricing` wrote unloadable TOML.** LiteLLM's `sample_spec`
  placeholder has a prose `litellm_provider` that was emitted as a bare TOML
  key, so `Pricing.from_toml` rejected the whole refreshed file. The
  placeholder is now skipped, provider keys are quoted when they are not
  legal bare keys, and model keys are escaped.
- **Routed model ids were unreachable.** LiteLLM keys Mistral, xAI, and some
  Gemini / DeepSeek models as `<provider>/<model>`, so
  `cost(provider="mistral", model="codestral-latest")` never matched. The
  prefix is stripped on refresh; when upstream lists a model both ways, the
  unprefixed entry wins.
- **Long prompts were under-billed.** Providers price some models by prompt
  length — Claude Haiku 5.5 costs 5× over 100K tokens, GPT-5.x more over
  272K, Gemini more over 200K — but `Pricing` only knew the base rate, so
  reservations and commits for long prompts came in low and spend could
  pass the cap. Long-prompt tiers are now part of the price model (see
  Added); 325 vendored models carry them.
- **1-hour cache writes were billed at the 5-minute rate** (Anthropic: 2×
  vs 1.25× base input). `cost()` now takes `cache_write_1h_tokens` and bills
  them at the 1-hour rate; 233 vendored models carry it.

### Added

- **`PriceTier`** and `PriceEntry.tiers`: long-prompt rates that replace the
  base rates when the prompt (input + cache-read + cache-write tokens) is
  strictly over `above_tokens`. The highest matching tier wins; a rate the
  tier leaves unset falls back to the base rate. In TOML:
  `[[<provider>."<model>".tiers]]` tables with `above_tokens = N`.
- **`PriceEntry.cache_write_1h_cents_per_m`** and the
  `Pricing.cost(cache_write_1h_tokens=...)` argument. `cache_write_tokens`
  keeps meaning the default 5-minute writes; pass each count once
  (Anthropic's `usage.cache_creation_input_tokens` is their sum, broken
  down in `usage.cache_creation`).
- **Schema migration 0002** adds `cache_write_1h_cents_per_m` and `tiers`
  (JSON text) to `snipz_pricing`, so database overrides can express both —
  overriding a tiered model no longer has to drop its tiers. Run
  `budget.migrate()` after upgrading; existing rows are kept.
- `snipz update-pricing` translates LiteLLM's `*_above_<N>k_tokens` and
  `*_above_1hr` fields. Service-tier variants (`_batches`, `_priority`,
  `_flex`) are still not modelled — snipz bills standard-tier rates.

- **PR CI workflow** (`.github/workflows/ci.yml`). Every pull request and
  push to `main` now runs the SQLite suite on Python 3.11, 3.12, 3.13, and
  3.14, the `--postgres` suite against a real Postgres 16 container, and
  ruff + mypy strict pinned to the 3.11 floor. Previously the gates ran
  only at release-tag time.
- This changelog.

## [0.2.0] — 2026-06-26

The **head-to-head benchmark release**. Same workload, same cap, three
backends side-by-side: at 1000 concurrent $0.10 reservations against a $5.00
cap, Snipz holds the cap at $5.00 while LiteLLM `BudgetManager` and Shekel
both spend $100.00 (20× cap).

### Added

- **`benchmarks/competitor_comparison.py`** — registry-based harness that runs
  each backend in sequence and emits an ASCII side-by-side chart + CSV.
- **`benchmarks/adapters/`** — a `BenchmarkAdapter` protocol with Snipz,
  LiteLLM, and Shekel implementations. Competitor adapters put a 1 ms
  simulated LLM-call gap between cap-check and cost-record (real calls are
  100–2000 ms, so the simulated race window is conservative).
- **`bench-competitors` extra** — `pip install snipz[bench-competitors]`
  pulls in `litellm` + `shekel` to reproduce the comparison.
- **Tag-triggered PyPI release workflow** via OIDC Trusted Publishing, with a
  tag ↔ `pyproject.toml` ↔ `__version__` drift check. See `RELEASING.md`.

### Fixed

- **sdist rebuilds.** `tests/test_benchmark.py` and
  `tests/test_competitor_benchmark.py` imported `benchmarks.*`, which the
  sdist already excluded — rebuilding from source hit `ImportError`. Both
  tests are now excluded from the sdist too.

### Post-release (no version bump)

- CI: `uv sync --all-extras` before typecheck so mypy sees the same optional
  deps as a local checkout (#4); action versions bumped off Node 20 (#5).
- Docs: benchmark latency note reframed around absolute overhead (#6);
  Python version requirement stated next to the install command (#8).

## [0.1.0] — 2026-06-26

First public release. The reservation engine and everything around it.

### Added

- **Reservation engine** — `Budget`, `Reservation`, `Scope`,
  `BudgetExceededError`. `reserve()` runs the cap-check and the ledger insert
  in one transaction holding a writer lock on the limit row; the async
  context manager auto-commits on success and auto-releases on exception.
  Multi-scope reservations lock limit rows in sorted order and roll back
  atomically. `request_id` makes retries idempotent. `observe()` updates
  in-flight cost mid-stream; cap checks count `MAX(actual, estimated)`.
- **Storage backends** — SQLite (`BEGIN IMMEDIATE`, default) and Postgres
  (`SELECT … FOR UPDATE`, session-local `lock_timeout`, managed or injected
  `asyncpg` pool) behind one dialect-agnostic `LedgerConnection` protocol.
  `Budget("postgresql://…")` routes automatically. Postgres is the
  `snipz[postgres]` extra.
- **Experimental sync wrapper** — `snipz.sync.Budget`, backed by a
  background event loop; raises if called from inside a running loop.
- **Pricing** — vendored `pricing.toml` (from LiteLLM's MIT price data),
  database overrides via `Pricing.with_backend(...)`, and the
  `snipz update-pricing` CLI.
- **Estimators** — `AnthropicEstimator`, `OpenAIEstimator` (exact via
  `tiktoken`, the `snipz[openai]` extra), and `FallbackEstimator`.
- **`@budget.guard`** — decorator wrapping an async LLM call in the full
  reserve / observe / commit / release lifecycle.
- **Reservation sweeper** — `sweep_once`, `sweep_loop`, and the
  `snipz sweep [--interval N]` CLI release expired reservations. A commit
  that arrives after the sweeper released its row still settles, as a late
  commit.
- **Event hooks** — `on_reserved`, `on_committed`, `on_released`,
  `on_overrun`. Sync or async handlers; handler exceptions are logged, never
  raised to the caller.
- **Cap-correctness benchmark** — `benchmarks/cap_correctness.py`, with a
  `--testcontainers-postgres` flag for a throwaway real database.

[Unreleased]: https://github.com/kartikeya-27/snipz/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/kartikeya-27/snipz/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kartikeya-27/snipz/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kartikeya-27/snipz/releases/tag/v0.1.0
