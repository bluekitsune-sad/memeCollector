"""SQLite connection helpers.

Connections enable foreign keys and WAL journaling, are safe to share across
threads (``check_same_thread=False``), and pair with the ``transaction`` context
manager so writes stay in short transactions — never held across network or
browser awaits (AGENTS.md §4: do not block the event loop).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.config import load_settings
from backend.database.migrations import migrate

logger = logging.getLogger(__name__)


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database at ``db_path``; defaults to the configured path."""
    path = Path(db_path) if db_path is not None else load_settings().storage.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    logger.debug("database opened path=%s", path)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on any exception; keeps write transactions short."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        logger.exception("transaction rolled back")
        raise
    conn.commit()


def initialize_database(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, apply pending migrations, and return the connection."""
    conn = get_connection(db_path)
    try:
        applied = migrate(conn)
    except Exception:
        conn.close()
        raise
    if applied:
        logger.info("database initialized applied=%s", ",".join(applied))
    return conn
