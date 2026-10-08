"""Migration runner + schema smoke tests (M0.3): tables, constraints, pragmas, FTS5."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.database.database import initialize_database
from backend.database.migrations import migrate, migration_files

EXPECTED_TABLES = {
    "schema_migrations",
    "media",
    "source",
    "ai_metadata",
    "embeddings",
    "jobs",
    "collections",
    "collection_items",
    "crawl_history",
    "media_fts",
}


def _insert_media(conn: sqlite3.Connection, sha256: str = "abc123") -> int:
    cursor = conn.execute(
        "INSERT INTO media (file_path, sha256) VALUES (?, ?)",
        ("data/media/00000001.png", sha256),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_migrate_creates_all_tables(db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    names = {row["name"] for row in rows}
    assert EXPECTED_TABLES <= names


def test_migrations_recorded_in_schema_migrations(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert len(rows) >= 1
    assert rows[0]["version"] == 1
    assert rows[0]["name"].startswith("001_")
    assert rows[0]["name"].endswith(".sql")
    # Bookkeeping matches the files on disk.
    assert {row["name"] for row in rows} == {name for _, name, _ in migration_files()}


def test_migrate_is_idempotent(db: sqlite3.Connection) -> None:
    assert migrate(db) == []
    tables_after = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0]
    assert migrate(db) == []
    tables_again = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0]
    assert tables_after == tables_again


def test_initialize_database_applies_migrations(db_path: Path) -> None:
    conn = initialize_database(db_path)
    try:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert EXPECTED_TABLES <= names
        # Second startup is a no-op.
        assert migrate(conn) == []
    finally:
        conn.close()


def test_wal_mode_and_foreign_keys_enabled(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_media_defaults_and_dup_status_constraint(db: sqlite3.Connection) -> None:
    media_id = _insert_media(db)
    row = db.execute(
        "SELECT dup_status, processing_status, is_favorite FROM media WHERE id = ?",
        (media_id,),
    ).fetchone()
    assert row["dup_status"] == "nondup"
    assert row["processing_status"] == "DOWNLOADED"
    assert row["is_favorite"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO media (file_path, dup_status) VALUES ('x.png', 'maybe')")


def test_processing_status_constraint(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO media (file_path, processing_status) VALUES ('x.png', 'PENDING')"
        )


def test_foreign_key_enforced_for_source(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO source (media_id, site, page_url, media_url) "
            "VALUES (999999, 'example.com', 'https://example.com/page', 'https://cdn.example.com/a.png')"
        )


def test_source_row_links_to_media(db: sqlite3.Connection) -> None:
    media_id = _insert_media(db)
    db.execute(
        "INSERT INTO source (media_id, site, page_url, comment_id, media_url, author_name) "
        "VALUES (?, 'asurascans.com', 'https://asurascans.com/chapter-42', '918271', "
        "'https://cdn.example.com/confused-cat.png', 'reader_one')",
        (media_id,),
    )
    row = db.execute(
        "SELECT site, comment_id, author_name FROM source WHERE media_id = ?",
        (media_id,),
    ).fetchone()
    assert row["site"] == "asurascans.com"
    assert row["comment_id"] == "918271"
    assert row["author_name"] == "reader_one"


def test_media_fts_is_queryable(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO media_fts (rowid, media_id, description, tags, filename, source_text) "
        "VALUES (1, 1, 'confused cartoon stares at viewer', 'confused;cartoon;reaction', "
        "'00000001.png', 'chapter 42 asurascans')"
    )
    hits = db.execute(
        "SELECT rowid FROM media_fts WHERE media_fts MATCH 'confused'"
    ).fetchall()
    assert [hit["rowid"] for hit in hits] == [1]
    no_hits = db.execute(
        "SELECT rowid FROM media_fts WHERE media_fts MATCH 'unfindable'"
    ).fetchall()
    assert no_hits == []
