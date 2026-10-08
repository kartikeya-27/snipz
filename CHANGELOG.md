# Changelog

All notable changes to this project will be documented in this file. Format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). This project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0 conventions: per semver pre-1.0, MINOR bumps (`0.x.0`) may include breaking
changes; PATCH bumps (`0.x.y`) are bug fixes only.

## [Unreleased]

_Nothing yet._

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
