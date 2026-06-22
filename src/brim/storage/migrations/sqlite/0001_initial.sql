-- Brim schema v1 — SQLite dialect.
-- Canonical Postgres schema lives in architecture.md; differences:
--   • UUIDs generated in Python (no gen_random_uuid).
--   • JSONB stored as TEXT (validate in app code).
--   • TIMESTAMPTZ stored as TEXT in ISO-8601 UTC (e.g., 2026-05-05T12:34:56.789Z).
--   • BOOLEAN stored as INTEGER (0/1) with explicit CHECK constraints.
--   • NUMERIC affinity; Python Decimal adapter handles precision in app code.

CREATE TABLE brim_limits (
    scope_type   TEXT    NOT NULL,
    scope_id     TEXT    NOT NULL,
    window       TEXT    NOT NULL,
    cap_cents    NUMERIC NOT NULL,
    grace_pct    INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (scope_type, scope_id, window)
);

CREATE TABLE brim_ledger (
    id              TEXT    PRIMARY KEY,
    reservation_id  TEXT    NOT NULL,
    scope_type      TEXT    NOT NULL,
    scope_id        TEXT    NOT NULL,
    state           TEXT    NOT NULL CHECK (state IN ('reserved', 'committed', 'released')),
    late            INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0, 1)),
    estimated_cents NUMERIC NOT NULL,
    actual_cents    NUMERIC,
    model           TEXT,
    provider        TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cached_tokens   INTEGER,
    request_id      TEXT,
    metadata        TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    settled_at      TEXT,
    expires_at      TEXT    NOT NULL,
    CHECK (state != 'committed' OR actual_cents IS NOT NULL)
);

CREATE TABLE brim_pricing (
    provider                TEXT    NOT NULL,
    model                   TEXT    NOT NULL,
    input_cents_per_m       NUMERIC NOT NULL,
    output_cents_per_m      NUMERIC NOT NULL,
    cache_read_cents_per_m  NUMERIC,
    cache_write_cents_per_m NUMERIC,
    valid_from              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (provider, model, valid_from)
);

CREATE TABLE brim_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Hot path: cap-check aggregate over current window.
CREATE INDEX idx_ledger_scope_window
    ON brim_ledger (scope_type, scope_id, created_at DESC)
    WHERE state IN ('reserved', 'committed');

-- Sweeper: find expired reservations fast.
CREATE INDEX idx_ledger_expiring
    ON brim_ledger (expires_at)
    WHERE state = 'reserved';

-- Idempotency: O(1) request_id lookup; partial so NULLs are not constrained.
CREATE UNIQUE INDEX idx_ledger_request_id
    ON brim_ledger (request_id)
    WHERE request_id IS NOT NULL;

-- Multi-scope reservation grouping.
CREATE INDEX idx_ledger_reservation_id
    ON brim_ledger (reservation_id);

INSERT INTO brim_schema_version (version) VALUES (1);
