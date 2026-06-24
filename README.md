# Snipz

LLM cost reservation ledger for Python. Pre-flight reserve → commit on success → release on failure. Embedded library, Postgres-first, transactional under concurrent load.

> **Status:** v0.0.1 — scaffolding. The reservation engine is being built per the phase plan in [snipz.md](snipz.md).

## API

Default is async (`from snipz import Budget`). A sync wrapper is also
available — marked **experimental** until it has been exercised in
real callers:

```python
from snipz.sync import Budget  # experimental sync API
```

Both wrap the same engine and share the same correctness guarantees.
The sync wrapper dispatches each call onto a per-process background
event loop; calling it from inside an active asyncio event loop raises
`RuntimeError` rather than deadlocking.

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
