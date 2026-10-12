-- 0003: running per-window spend totals on snipz_limits.
--
-- A cap check reads the scope's spend in the current window from these
-- columns instead of summing the ledger (which costs time proportional
-- to the scope's traffic). Kept current in the same transaction as every
-- ledger write; see snipz/storage/spend.py for the rules.
--
-- * window_start_at — start of the window bucket the total is for
--   (ISO-8601 UTC, same format as snipz_ledger.created_at). NULL means
--   unknown: the total is rebuilt from the ledger on its next read, so
--   existing limits need no backfill here.
-- * window_spend_micros — spend in that bucket, in micro-cents.

BEGIN;

ALTER TABLE snipz_limits ADD COLUMN window_start_at TEXT;
ALTER TABLE snipz_limits ADD COLUMN window_spend_micros INTEGER NOT NULL DEFAULT 0;

INSERT INTO snipz_schema_version (version) VALUES (3);

COMMIT;
