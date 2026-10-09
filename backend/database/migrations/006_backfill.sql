-- 006_backfill.sql — site-wide backfill catalog (one comic at a time, resumable).
--
-- A backfill walks a site's whole catalog index (e.g. asurascans.com/comics),
-- discovers every series, and crawls them sequentially through the shared
-- pipeline (backend/jobs/backfill.py). Unlike `jobs` rows this work is meant to
-- outlive restarts: the parent row stays `running` across a server death and
-- the startup resume (backend/jobs/resume.py) relaunches it, so two tables are
-- used instead of one `jobs` row — the item list is user-visible state
-- ("which comics are done") that must survive independently of counters.
--
-- backfill_items.url is UNIQUE per backfill so discovery is idempotent: a
-- resumed run re-discovers the index without ever duplicating a comic, and an
-- item's `running` state flipped back to `pending` at resume time (crash
-- mid-comic) is how the interrupted comic gets its second attempt.
-- Datetimes are SQLite datetime('now') text (UTC), same as `jobs`.

CREATE TABLE IF NOT EXISTS backfill_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_url TEXT NOT NULL,
    adapter_site TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'cancelled', 'failed')),
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    current_url TEXT,
    current_title TEXT,
    message TEXT,
    error TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS backfill_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backfill_id INTEGER NOT NULL REFERENCES backfill_jobs (id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    error TEXT,
    finished_at TEXT,
    UNIQUE (backfill_id, url)
);

CREATE INDEX IF NOT EXISTS idx_backfill_items_status ON backfill_items (backfill_id, status, id);
