# Snipz

LLM cost reservation ledger for Python. Pre-flight reserve → commit on success → release on failure. Embedded library, Postgres-first, transactional under concurrent load.

> **Status:** v0.0.1 — scaffolding. The reservation engine is being built per the phase plan in [snipz.md](snipz.md).

## Design documents

- [`snipz.md`](snipz.md) — positioning, competitor analysis, build phases.
- [`architecture.md`](architecture.md) — layered architecture, schema, decision log.
- [`scenarios.md`](scenarios.md) — Phase 0 concurrency walkthroughs.

## Development

```bash
uv sync           # install deps + create .venv
uv run pytest     # run tests
```

## License

MIT
