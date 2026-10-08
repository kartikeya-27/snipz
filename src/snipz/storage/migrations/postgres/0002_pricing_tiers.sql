-- 0002: long-prompt pricing tiers and the 1-hour cache-write rate.
--
-- * cache_write_1h_cents_per_m — 1-hour TTL cache-write rate, priced
--   separately from the default 5-minute rate by Anthropic (2x vs 1.25x
--   base input).
-- * tiers — JSON array of long-prompt tiers, e.g.
--   [{"above_tokens": 100000, "input_cents_per_m": "50", ...}].
--   TEXT rather than JSONB so rates keep their exact decimal strings and
--   the column matches SQLite; validated by snipz.pricing on load.
--
-- PostgresBackend.migrate() runs each file in its own transaction.

ALTER TABLE snipz_pricing ADD COLUMN cache_write_1h_cents_per_m NUMERIC(20, 6);
ALTER TABLE snipz_pricing ADD COLUMN tiers TEXT;

INSERT INTO snipz_schema_version (version) VALUES (2);
