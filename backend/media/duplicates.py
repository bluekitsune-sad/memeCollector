"""Duplicate flag lifecycle — the STORE-stage rules of PRD §12.1 (AGENTS.md §7).

Every media item carries ``media.dup_status``:

* ``nondup``    — no other row shares its SHA-256 (or this row is the retained copy).
* ``dup``       — a later-collected copy of an existing SHA-256; scheduled for
  automatic deletion ``DUP_RETENTION_DAYS`` (7) days after ``dup_flagged_at``.
* ``unflagged`` — the user cleared a ``dup`` flag; **never** auto-deleted and
  **never** silently re-flagged by a later scan.

Binding rules implemented here (PRD §12.1):

1. :func:`scan_and_flag` flags a row ``dup``/``nondup``; the item collected
   first (``created_at``, tie-break ``id``) is the *retained* copy and is never
   flagged; later copies get ``dup_flagged_at`` + ``dup_of_media_id``.
2. The scan is idempotent: re-scanning a ``dup`` never resets ``dup_flagged_at``
   (the 7-day clock must not restart), and ``unflagged`` rows are left alone.
3. :func:`unflag` moves ``dup`` → ``unflagged`` on explicit user action.
4. :func:`purge_expired_dups` deletes ``dup`` rows whose flag is older than
   7 days — files (best effort) plus ``media``/``source``/``ai_metadata``/
   ``embeddings``/``collection_items``/``media_fts`` rows — **never** the last
   remaining copy of a SHA-256 (an all-expired group keeps its earliest item).
5. File deletion only touches paths inside the configured storage directories
   (PRD §41: a corrupted row must never turn a purge into an arbitrary-file
   delete); anything else is logged and skipped.

Timestamps are UTC ``YYYY-MM-DD HH:MM:SS`` strings (SQLite ``datetime('now')``),
which compare correctly as plain text.

This module owns file *removal* helpers shared by the library delete route;
``backend.media.library`` imports it (one-way dependency — no cycles).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import Settings
from backend.database.database import transaction

logger = logging.getLogger(__name__)

#: Days a ``dup`` item survives before the daily purge removes it (PRD §12.1).
DUP_RETENTION_DAYS = 7

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def dup_expires_at(flagged_at: str | None) -> str | None:
    """When a flagged ``dup`` item is scheduled for deletion (flag time + 7 days).

    Returns ``None`` when ``flagged_at`` is absent or unparseable (defensive:
    a corrupt timestamp must never crash the library UI).
    """
    if not flagged_at:
        return None
    try:
        flagged = datetime.strptime(flagged_at, _TIME_FORMAT)
    except ValueError:
        logger.warning("unparseable dup_flagged_at value=%r", flagged_at)
        return None
    return (flagged + timedelta(days=DUP_RETENTION_DAYS)).strftime(_TIME_FORMAT)


def scan_and_flag(conn: sqlite3.Connection, media_id: int) -> str:
    """Assign the ``dup``/``nondup`` flag for one row; returns its final status.

    Rules (PRD §12.1):

    * ``unflagged`` rows are returned untouched — the system never re-flags a
      user-cleared item.
    * No other row shares the SHA-256 (or the row has none) → ``nondup``,
      clearing any stale flag. This also *repairs* a row whose retained copy
      was deleted by hand.
    * This row is the earliest copy → ``nondup`` (the retained copy is never
      flagged).
    * Otherwise → ``dup`` with ``dup_of_media_id`` pointing at the retained
      copy; ``dup_flagged_at`` is set only on the ``nondup``→``dup`` transition
      so re-scans never restart the 7-day clock.

    Raises ``ValueError`` when ``media_id`` does not exist.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT id, sha256, dup_status, dup_flagged_at, dup_of_media_id, created_at "
            "FROM media WHERE id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no media row with id={media_id}")
        if row["dup_status"] == "unflagged":
            return "unflagged"
        sha256 = row["sha256"]
        if sha256 is None:
            return _set_status(conn, media_id, row, "nondup")
        others = conn.execute(
            "SELECT id, created_at FROM media WHERE sha256 = ? AND id != ? "
            "ORDER BY created_at, id",
            (sha256, media_id),
        ).fetchall()
        if not others:
            return _ensure_nondup(conn, media_id, row)
        retained_id = int(others[0]["id"])
        self_key = (row["created_at"], int(row["id"]))
        earliest_other = (others[0]["created_at"], int(others[0]["id"]))
        if self_key <= earliest_other:
            return _ensure_nondup(conn, media_id, row)
        # A later copy: flag it (idempotently) against the retained item.
        if row["dup_status"] == "dup":
            if int(row["dup_of_media_id"] or 0) != retained_id:
                conn.execute(
                    "UPDATE media SET dup_of_media_id = ? WHERE id = ?",
                    (retained_id, media_id),
                )
            return "dup"
        conn.execute(
            "UPDATE media SET dup_status = 'dup', dup_flagged_at = datetime('now'), "
            "dup_of_media_id = ? WHERE id = ?",
            (retained_id, media_id),
        )
        logger.info("duplicate detected media_id=%d dup_of=%d sha256=%s",
                    media_id, retained_id, sha256[:12])
        return "dup"


def _ensure_nondup(conn: sqlite3.Connection, media_id: int, row: sqlite3.Row) -> str:
    """Return the row to ``nondup`` (clearing stale flag columns); no-op when clean.

    Also the repair path: a row whose retained copy was deleted by hand stops
    being a ``dup`` on its next scan.
    """
    if (
        row["dup_status"] == "nondup"
        and row["dup_flagged_at"] is None
        and row["dup_of_media_id"] is None
    ):
        return "nondup"
    conn.execute(
        "UPDATE media SET dup_status = 'nondup', dup_flagged_at = NULL, "
        "dup_of_media_id = NULL WHERE id = ?",
        (media_id,),
    )
    if row["dup_status"] == "dup":
        logger.info("dup flag cleared (no shared copy remains) media_id=%d", media_id)
    return "nondup"


def unflag(conn: sqlite3.Connection, media_id: int) -> bool:
    """User action: move a ``dup`` row to ``unflagged`` (PRD §12.1 rule 3).

    Returns ``True`` when the flag changed; ``False`` when the row is already
    ``unflagged``/``nondup`` (idempotent, safe to call twice). ``dup_flagged_at``
    and ``dup_of_media_id`` are kept as provenance — purge only ever looks at
    ``dup_status = 'dup'``, so an unflagged item is never auto-deleted.
    Raises ``ValueError`` when ``media_id`` does not exist.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT dup_status FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no media row with id={media_id}")
        if row["dup_status"] != "dup":
            return False
        conn.execute(
            "UPDATE media SET dup_status = 'unflagged' WHERE id = ? AND dup_status = 'dup'",
            (media_id,),
        )
    logger.info("dup unflagged by user media_id=%d", media_id)
    return True


def purge_expired_dups(
    conn: sqlite3.Connection, settings: Settings, now: datetime
) -> list[int]:
    """Delete ``dup`` rows flagged 7+ days before ``now``; returns purged ids.

    ``now`` is injected so tests control the clock; aware datetimes are
    converted to UTC (stored timestamps are UTC). Steps:

    1. Select expired candidates (``dup_flagged_at <= now - 7 days``).
    2. Per SHA-256 group, drop candidates only when another copy survives;
       an entirely expired group keeps its earliest row — the last copy of a
       SHA-256 is never deleted (PRD §12.1 rule 5). Rows with no SHA-256 are
       skipped defensively (their uniqueness cannot be proven).
    3. One transaction removes ``media_fts`` rows and the ``media`` rows
       (``source``/``ai_metadata``/``embeddings``/``collection_items`` cascade).
    4. After commit, remove original/thumbnail/preview files best effort —
       failures are logged, never raised, and never abort the batch (PRD §36).
    """
    cutoff = _utc_text(now - timedelta(days=DUP_RETENTION_DAYS))
    with transaction(conn):
        candidates = conn.execute(
            "SELECT id, sha256, created_at, file_path, thumbnail_path, preview_path "
            "FROM media "
            "WHERE dup_status = 'dup' AND dup_flagged_at IS NOT NULL AND dup_flagged_at <= ? "
            "ORDER BY created_at, id",
            (cutoff,),
        ).fetchall()
        purge_ids = _select_safe_purge(conn, candidates)
        if not purge_ids:
            return []
        paths: list[str | None] = []
        for media_id in purge_ids:
            row = next(item for item in candidates if int(item["id"]) == media_id)
            paths.extend((row["file_path"], row["thumbnail_path"], row["preview_path"]))
            conn.execute(
                "DELETE FROM media_fts WHERE rowid = ? OR media_id = ?", (media_id, media_id)
            )
            conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
    removed = unlink_media_files(paths, settings)
    logger.info(
        "expired dups purged count=%d ids=%s file_errors=%d",
        len(purge_ids), ",".join(str(item) for item in purge_ids), len(removed),
    )
    return purge_ids


def _select_safe_purge(
    conn: sqlite3.Connection, candidates: list[sqlite3.Row]
) -> list[int]:
    """Expired candidates that may be deleted without removing a SHA-256's last copy."""
    by_sha: dict[str, list[sqlite3.Row]] = {}
    skipped_no_sha = 0
    for row in candidates:
        if row["sha256"] is None:
            skipped_no_sha += 1
            continue
        by_sha.setdefault(str(row["sha256"]), []).append(row)
    if skipped_no_sha:
        logger.warning("expired dup without sha256 skipped count=%d", skipped_no_sha)
    purge_ids: list[int] = []
    for sha256, group in by_sha.items():
        total = conn.execute(
            "SELECT COUNT(*) FROM media WHERE sha256 = ?", (sha256,)
        ).fetchone()[0]
        # Candidates are ordered by (created_at, id): the group's first row is the
        # earliest copy — keep it when every copy has expired.
        keep = int(group[0]["id"]) if int(total) <= len(group) else None
        for row in group:
            media_id = int(row["id"])
            if media_id == keep:
                logger.warning(
                    "purge would delete the last copy of sha256=%s — retained media_id=%d",
                    sha256[:12], media_id,
                )
                continue
            purge_ids.append(media_id)
    return purge_ids


def unlink_media_files(
    paths: Iterable[str | Path | None], settings: Settings
) -> list[str]:
    """Best-effort file removal for purges/deletes; returns paths that stayed.

    ``None``/empty entries are ignored. Only paths resolving inside
    ``settings.storage`` media/thumbnail/preview directories are unlinked
    (PRD §41 path-traversal guard); every failure is logged, never raised.
    """
    storage = settings.storage
    roots = (storage.media_directory, storage.thumbnail_directory, storage.preview_directory)
    failed: list[str] = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        if not any(_is_within(path, root) for root in roots):
            logger.warning("refusing to delete file outside storage path=%s", path)
            failed.append(str(path))
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not delete file path=%s", path, exc_info=True)
            failed.append(str(path))
    return failed


def _is_within(candidate: Path, directory: Path) -> bool:
    """True when ``candidate`` resolves inside ``directory`` (either may not exist yet)."""
    try:
        candidate.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _utc_text(moment: datetime) -> str:
    """Format ``moment`` as a UTC ``YYYY-MM-DD HH:MM:SS`` string."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.strftime(_TIME_FORMAT)
