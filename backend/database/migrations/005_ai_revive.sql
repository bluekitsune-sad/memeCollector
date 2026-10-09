-- 005_ai_revive.sql — automatic second chance for terminally-failed AI items (PRD §36).
--
-- An item reaches `processing_status='FAILED'` when its per-item budget is spent
-- or the provider returns a non-retryable error (video, bad key, unknown model).
-- Transient-caused terminal failures (429/5xx/timeout/malformed) must NOT be the
-- end of the road: the queue records *why* the item failed here so the AI
-- supervisor can revive it later with a fresh budget.
--
--   ai_failed_at        when the item reached FAILED (SQLite UTC text)
--   ai_error_retryable  1 = transient cause, eligible for automatic revival
--                       (0 = permanent: video/bad-key/unknown-model)
--   ai_revives          how many automatic revivals this item has used; the
--                       `ai.max_item_revives` cap keeps a deterministic failure
--                       from cycling forever.
--
-- `ai_next_retry_at` doubles as the revival schedule: on a revivable terminal
-- failure the queue parks `now + ai.revive_after_seconds` there. The supervisor
-- sleeps until then, flips the row back to a claimable status with a fresh
-- budget, and the normal drain path takes over.
ALTER TABLE media ADD COLUMN ai_failed_at TEXT;
ALTER TABLE media ADD COLUMN ai_error_retryable INTEGER NOT NULL DEFAULT 0;
ALTER TABLE media ADD COLUMN ai_revives INTEGER NOT NULL DEFAULT 0;

-- Backfill for rows already FAILED before this migration: a row that carries an
-- `ai_next_retry_at` was deferred at least once, i.e. it failed transiently
-- (videos and bad-key/unknown-model items fail on their first attempt and never
-- get a retry timestamp). Schedule those for revival immediately so live
-- libraries recover on first boot after the upgrade. Rows misclassified by the
-- heuristic self-correct: their revival fails non-retryably, the flag flips to
-- 0, and they stay failed.
UPDATE media
   SET ai_failed_at = datetime('now'),
       ai_error_retryable = 1,
       ai_next_retry_at = datetime('now')
 WHERE processing_status = 'FAILED'
   AND ai_next_retry_at IS NOT NULL;
