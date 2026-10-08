-- 001_initial_schema.sql — initial schema per PRD §20 and §12.1 (dup flag lifecycle).
-- FTS strategy: media_fts is a standalone FTS5 table (rowid == media.id) maintained by
-- explicit indexing code in backend/search — see backend/database/migrations.py docstring.
-- Written idempotently (IF NOT EXISTS) per the migration runner contract.

-- ---------------------------------------------------------------------------
-- media: one row per unique downloaded file (originals are never modified).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    sha256 TEXT UNIQUE,
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

CREATE INDEX IF NOT EXISTS idx_media_dup_status ON media (dup_status);
CREATE INDEX IF NOT EXISTS idx_media_processing_status ON media (processing_status);
CREATE INDEX IF NOT EXISTS idx_media_created_at ON media (created_at);
CREATE INDEX IF NOT EXISTS idx_media_is_favorite ON media (is_favorite);

-- ---------------------------------------------------------------------------
-- source: provenance for every collected item (PRD §31). One media item can
-- have been discovered on several pages, hence multiple rows per media.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    site TEXT NOT NULL,
    page_url TEXT NOT NULL,
    chapter TEXT,
    page_number INTEGER,
    comment_id TEXT,
    media_url TEXT NOT NULL,
    author_name TEXT,
    collected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_source_media_id ON source (media_id);
CREATE INDEX IF NOT EXISTS idx_source_page_url ON source (page_url);
-- Level 1 URL duplicate check (PRD §12): has this media URL been downloaded?
CREATE INDEX IF NOT EXISTS idx_source_media_url ON source (media_url);

-- ---------------------------------------------------------------------------
-- ai_metadata: structured AI analysis per PRD §15. JSON columns store arrays.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_metadata (
    media_id INTEGER PRIMARY KEY REFERENCES media (id) ON DELETE CASCADE,
    description TEXT,
    tags TEXT,
    emotions TEXT,
    subjects TEXT,
    meme_context TEXT,
    suggested_search_phrases TEXT,
    ai_provider TEXT,
    model TEXT,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- embeddings: vector blob for semantic search (PRD §21C).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embeddings (
    media_id INTEGER PRIMARY KEY REFERENCES media (id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    embedding_model TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- jobs: background job progress/errors (PRD §35, §36).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0,
    message TEXT,
    error TEXT,
    params TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);

-- ---------------------------------------------------------------------------
-- collections: user-defined groups; a media item can be in many (PRD §29).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    emoji TEXT
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, media_id)
);

-- ---------------------------------------------------------------------------
-- crawl_history: skip already-scanned pages (PRD §38). URL is unique so a
-- rescan upserts the row instead of duplicating it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    site TEXT,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    media_found INTEGER NOT NULL DEFAULT 0,
    last_scanned_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- media_fts: standalone FTS5 index for keyword search (PRD §20, §21B).
-- rowid == media.id; maintained by explicit index code, not triggers.
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5 (
    media_id UNINDEXED,
    description,
    tags,
    filename,
    source_text,
    tokenize = 'unicode61'
);
