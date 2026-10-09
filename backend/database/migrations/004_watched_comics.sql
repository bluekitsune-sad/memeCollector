-- 004_watched_comics.sql — passive watch list (PRD §39 incremental crawling, §57).
--
-- Comics the user has crawled are recorded here so the background watcher can
-- rescan them on an interval (backend/jobs/watch.py) with force_rescan=False:
-- `crawl_history` (001) makes the rescan fetch only pages never seen before.
--
-- url is UNIQUE so both the API upsert and the post-crawl auto-registration
-- (backend/jobs/watched_comics.py) are idempotent — a repeat never duplicates
-- a row and never clobbers the user's `enabled`/`title` state.
-- `last_scanned_at` stores SQLite datetime('now') text (UTC, second precision),
-- the same format `crawl_history` uses. Written idempotently per the migration
-- runner contract (backend/database/migrations.py).

CREATE TABLE IF NOT EXISTS watched_comics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    site TEXT NOT NULL,
    title TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_scanned_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_watched_comics_enabled ON watched_comics (enabled);
CREATE INDEX IF NOT EXISTS idx_watched_comics_site ON watched_comics (site);
