-- Snipz schema v1 — PostgreSQL dialect.
-- Lockstep with src/snipz/storage/migrations/sqlite/0001_initial.sql.
--
-- Differences from the SQLite dialect:
--   * Native UUID, JSONB, BOOLEAN, TIMESTAMPTZ, NUMERIC(20, 6).
--   * No CHECK on enabled / late — native BOOLEAN already constrains values.
--   * `created_at` / `updated_at` use NOW() instead of strftime.
--   * Indexes are written as PostgreSQL partial indexes (same shape as SQLite).

CREATE TABLE snipz_limits (
    scope_type   TEXT           NOT NULL,
    scope_id     TEXT           NOT NULL,
    -- ``window`` is a reserved keyword in PostgreSQL (used for window functions
    -- with OVER (PARTITION BY ...)). Quoted as "window" so it remains a regular
    -- column identifier; the column name itself is unchanged across dialects.
    "window"     TEXT           NOT NULL,
    cap_cents    NUMERIC(20, 6) NOT NULL,
    grace_pct    INTEGER        NOT NULL DEFAULT 0,
    enabled      BOOLEAN        NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope_type, scope_id, "window")
);

CREATE TABLE snipz_ledger (
    id              UUID           PRIMARY KEY,
    reservation_id  UUID           NOT NULL,
    scope_type      TEXT           NOT NULL,
    scope_id        TEXT           NOT NULL,
    state           TEXT           NOT NULL CHECK (state IN ('reserved', 'committed', 'released')),
    late            BOOLEAN        NOT NULL DEFAULT FALSE,
    estimated_cents NUMERIC(20, 6) NOT NULL,
    actual_cents    NUMERIC(20, 6),
    model           TEXT,
    provider        TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cached_tokens   INTEGER,
    request_id      TEXT,
    metadata        JSONB          NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    settled_at      TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ    NOT NULL,
    CHECK (state != 'committed' OR actual_cents IS NOT NULL)
);

CREATE TABLE snipz_pricing (
    provider                TEXT           NOT NULL,
    model                   TEXT           NOT NULL,
    input_cents_per_m       NUMERIC(20, 6) NOT NULL,
    output_cents_per_m      NUMERIC(20, 6) NOT NULL,
    cache_read_cents_per_m  NUMERIC(20, 6),
    cache_write_cents_per_m NUMERIC(20, 6),
    valid_from              TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, model, valid_from)
);

CREATE TABLE snipz_schema_version (
    version    INTEGER     PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hot path: cap-check aggregate over current window.
CREATE INDEX idx_ledger_scope_window
    ON snipz_ledger (scope_type, scope_id, created_at DESC)
    WHERE state IN ('reserved', 'committed');

-- Sweeper: find expired reservations fast.
CREATE INDEX idx_ledger_expiring
    ON snipz_ledger (expires_at)
    WHERE state = 'reserved';

-- Idempotency: O(1) request_id lookup; partial so NULLs are not constrained.
CREATE UNIQUE INDEX idx_ledger_request_id
    ON snipz_ledger (request_id)
    WHERE request_id IS NOT NULL;

-- Multi-scope reservation grouping.
CREATE INDEX idx_ledger_reservation_id
    ON snipz_ledger (reservation_id);

INSERT INTO snipz_schema_version (version) VALUES (1);
