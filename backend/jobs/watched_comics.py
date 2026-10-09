"""``watched_comics`` persistence — the storage half of the passive watcher (PRD §39).

Two idempotent write paths feed the table:

* :func:`ensure_watched_comic` — the API's "add a comic" upsert: the row is
  created when missing and **left untouched** when present, so a repeat never
  duplicates and never clobbers the user's ``enabled``/``title`` state.
* :func:`record_comic_scan` — called after every successful crawl of a comic
  URL (the scrape pipeline's auto-registration, backend/jobs/pipeline.py):
  creates the row on first crawl or bumps ``last_scanned_at`` (+ ``site``),
  again never touching ``enabled``/``title``.

Reads are plain; ``enabled`` arrives as SQLite's 0/1 and is normalized to
``bool`` by the API layer. All writes are short transactions (database.py).
"""

from __future__ import annotations

import logging
import sqlite3

from backend.database.database import transaction

logger = logging.getLogger(__name__)


def ensure_watched_comic(
    db: sqlite3.Connection, url: str, site: str, *, title: str | None = None
) -> tuple[int, bool]:
    """Insert ``url`` if unseen; returns ``(id, created)`` — never mutates an existing row."""
    with transaction(db):
        existing = db.execute(
            "SELECT id FROM watched_comics WHERE url = ?", (url,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"]), False
        cursor = db.execute(
            "INSERT INTO watched_comics (url, site, title) VALUES (?, ?, ?)",
            (url, site, title),
        )
    comic_id = int(cursor.lastrowid)
    logger.info("watched comic added id=%d site=%s url=%s", comic_id, site, url)
    return comic_id, True


def record_comic_scan(db: sqlite3.Connection, url: str, site: str) -> int:
    """Upsert after a successful scan of ``url``: create or bump ``last_scanned_at``.

    ``enabled``/``title`` are deliberately preserved — auto-registration must
    never re-enable a comic the user disabled (idempotent, user state wins).
    """
    with transaction(db):
        db.execute(
            """
            INSERT INTO watched_comics (url, site, last_scanned_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(url) DO UPDATE SET
                site = excluded.site,
                last_scanned_at = excluded.last_scanned_at
            """,
            (url, site),
        )
        row = db.execute("SELECT id FROM watched_comics WHERE url = ?", (url,)).fetchone()
    comic_id = int(row["id"]) if row is not None else 0
    logger.info("watched comic scan recorded site=%s url=%s", site, url)
    return comic_id


def list_watched_comics(db: sqlite3.Connection, *, enabled_only: bool = False) -> list[sqlite3.Row]:
    """All watched comics oldest-first; ``enabled_only`` filters what a passive pass scans."""
    query = "SELECT * FROM watched_comics"
    if enabled_only:
        query += " WHERE enabled = 1"
    return list(db.execute(query + " ORDER BY id"))


def get_watched_comic(db: sqlite3.Connection, comic_id: int) -> sqlite3.Row | None:
    """One watched comic by id, or ``None`` when it does not exist."""
    return db.execute("SELECT * FROM watched_comics WHERE id = ?", (comic_id,)).fetchone()


def set_watched_enabled(db: sqlite3.Connection, comic_id: int, enabled: bool) -> bool:
    """Toggle ``enabled``; ``False`` when no such comic exists."""
    with transaction(db):
        cursor = db.execute(
            "UPDATE watched_comics SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, comic_id),
        )
    return cursor.rowcount > 0


def delete_watched_comic(db: sqlite3.Connection, comic_id: int) -> bool:
    """Remove a watched comic; ``False`` when no such comic exists."""
    with transaction(db):
        cursor = db.execute("DELETE FROM watched_comics WHERE id = ?", (comic_id,))
    if cursor.rowcount > 0:
        logger.info("watched comic removed id=%d", comic_id)
    return cursor.rowcount > 0
