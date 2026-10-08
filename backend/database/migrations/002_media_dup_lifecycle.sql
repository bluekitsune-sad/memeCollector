-- 002_media_dup_lifecycle.sql — schema support for the duplicate flag lifecycle (PRD §12.1).
--
-- 001 declared `sha256 TEXT UNIQUE`, which makes the PRD §12.1 lifecycle impossible:
-- later copies must exist as media rows (flagged `dup`, retained until the 7-day
-- purge, unflaggable by the user) and purge must "never delete the last copy of a
-- sha256" — all of which require several rows to share one sha256. SQLite cannot
-- drop a constraint in place, so the media table is rebuilt here (rows and child
-- FKs preserved; verified: `source`/`ai_metadata`/`embeddings`/`collection_items`
-- keep their data and ON DELETE CASCADE behavior).
--
-- Also adds `media.title` — the user-editable title from PRD §27 / M2.4 (PATCH
-- /api/media/{id}), which 001 did not include.
--
-- Rebuild is idempotent: safe to re-run if this script fails part-way (the
-- migration runner books the version only after executescript succeeds).
PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS media_rebuild;

CREATE TABLE media_rebuild (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    file_path TEXT NOT NULL,
    thumbnail_path TEXT,
    preview_path TEXT,
    original_filename TEXT,
    mime_type TEXT,
    extension TEXT,
    file_size INTEGER,
    width INTEGER,
    height INTEGER,
    duration REAL,
    sha256 TEXT,
    phash TEXT,
    dup_status TEXT NOT NULL DEFAULT 'nondup'
        CHECK (dup_status IN ('dup', 'nondup', 'unflagged')),
    dup_flagged_at TEXT,
    dup_of_media_id INTEGER REFERENCES media (id) ON DELETE SET NULL,
    processing_status TEXT NOT NULL DEFAULT 'DOWNLOADED'
        CHECK (processing_status IN (
            'DOWNLOADED', 'ANALYZING', 'ANALYZED', 'EMBEDDING', 'READY', 'FAILED'
        )),
    is_favorite INTEGER NOT NULL DEFAULT 0 CHECK (is_favorite IN (0, 1)),
    user_description TEXT,
    user_tags TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO media_rebuild (
    id, file_path, thumbnail_path, preview_path, original_filename, mime_type,
    extension, file_size, width, height, duration, sha256, phash, dup_status,
    dup_flagged_at, dup_of_media_id, processing_status, is_favorite,
    user_description, user_tags, created_at
)
SELECT
    id, file_path, thumbnail_path, preview_path, original_filename, mime_type,
    extension, file_size, width, height, duration, sha256, phash, dup_status,
    dup_flagged_at, dup_of_media_id, processing_status, is_favorite,
    user_description, user_tags, created_at
FROM media;

DROP TABLE media;
ALTER TABLE media_rebuild RENAME TO media;

-- Recreate the indexes that were dropped with the old table (001's statements
-- are already recorded as applied and will not run again).
CREATE INDEX IF NOT EXISTS idx_media_dup_status ON media (dup_status);
CREATE INDEX IF NOT EXISTS idx_media_processing_status ON media (processing_status);
CREATE INDEX IF NOT EXISTS idx_media_created_at ON media (created_at);
CREATE INDEX IF NOT EXISTS idx_media_is_favorite ON media (is_favorite);
-- Replaces the index 001 got implicitly from UNIQUE(sha256): dup scanning looks
-- up every row sharing a hash (backend/media/duplicates.py).
CREATE INDEX IF NOT EXISTS idx_media_sha256 ON media (sha256);

PRAGMA foreign_keys = ON;
