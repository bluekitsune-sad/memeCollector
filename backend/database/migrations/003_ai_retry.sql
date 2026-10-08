-- 003_ai_retry.sql — per-item AI retry budget (PRD §18, §19, §36).
--
-- Transient provider failures (429/5xx/timeouts, reasoning-only model answers)
-- must not be terminal: the queue bumps `ai_attempts` and parks a
-- `ai_next_retry_at` backoff timestamp, returning the row to a claimable
-- status so a later run (the AI supervisor) picks it up again. Once
-- `ai_attempts` reaches `ai.max_item_attempts` the item fails for good.
--
-- `ai_next_retry_at` stores SQLite `datetime('now', '+NNN seconds')` text (UTC,
-- second precision) — directly comparable with `datetime('now')` in the claim
-- query (`(ai_next_retry_at IS NULL OR ai_next_retry_at <= datetime('now'))`).
-- The index serves the supervisor's `MIN(ai_next_retry_at)` hold query.
ALTER TABLE media ADD COLUMN ai_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE media ADD COLUMN ai_next_retry_at TEXT;
CREATE INDEX IF NOT EXISTS idx_media_ai_next_retry ON media (ai_next_retry_at);
