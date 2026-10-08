"""Numbered SQL migration runner.

Pending ``NNN_name.sql`` files in ``backend/database/migrations/`` are applied in
numeric order exactly once; applied versions are tracked in the
``schema_migrations`` table. Migration scripts should be written idempotently
(``IF NOT EXISTS`` guards) because ``executescript`` cannot be wrapped in the
same transaction as its bookkeeping row.

Full-text search approach — chosen over sync triggers: ``media_fts`` is a
standalone FTS5 table (its own content store, ``rowid == media.id``) populated by
explicit indexing code in ``backend/search`` (Milestone M4.1), not by triggers.
The indexed columns span three tables (media filename, ai_metadata
description/tags, source page text), so a single reindex pass that rebuilds rows
via JOINs is simpler and easier to keep correct than cross-table triggers, and it
makes re-indexing after model changes a one-call operation. The trade-off: code
must keep the table in sync on insert/update/delete of media rows — the indexer
owns that responsibility.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR: Path = Path(__file__).resolve().parent / "migrations"

_MIGRATION_PATTERN = re.compile(r"^(\d{3,})_([A-Za-z0-9_\-]+)\.sql$")

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def migration_files() -> list[tuple[int, str, Path]]:
    """Return ``(version, filename, path)`` for every migration, ordered by version."""
    found: list[tuple[int, str, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_PATTERN.match(path.name)
        if match is None:
            raise ValueError(f"migration filename must match NNN_name.sql: {path.name}")
        found.append((int(match.group(1)), path.name, path))
    versions = [version for version, _, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration versions in {MIGRATIONS_DIR}")
    return sorted(found, key=lambda item: item[0])


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply all pending migrations; returns the filenames applied during this call."""
    conn.execute(_BOOTSTRAP_SQL)
    conn.commit()
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    applied_now: list[str] = []
    for version, name, path in migration_files():
        if version in applied:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (version, name),
        )
        conn.commit()
        applied_now.append(name)
        logger.info("migration applied version=%d name=%s", version, name)
    return applied_now
