"""Thumbnail batch job — PROCESS stage worker with a tracked jobs row (PRD §14, §35).

:func:`run_thumbnail_job` picks every media row whose ``thumbnail_path`` is
still ``NULL`` and generates thumbnail + preview for it
(:func:`backend.media.thumbnails.generate_thumbnails`, CPU work in a worker
thread — AGENTS.md §4). Progress is written to the ``jobs`` row
(``job_type='thumbnail'``), and **one bad file never aborts the batch**: a
malformed original (or a vanished file) is counted, logged with its media id,
and processing moves on (PRD §36). Items that fail keep
``thumbnail_path = NULL`` and are retried on the next run.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from backend.config import Settings
from backend.database.database import transaction
from backend.media.thumbnails import ThumbnailError, generate_thumbnails

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThumbnailJobSummary:
    """End-of-job counters for the jobs row / Jobs UI."""

    job_id: int
    total: int = 0
    processed: int = 0
    failed: int = 0


async def run_thumbnail_job(
    *, db: sqlite3.Connection, settings: Settings, limit: int | None = None
) -> ThumbnailJobSummary:
    """Generate thumbnails for items where ``thumbnail_path IS NULL``.

    Creates a ``jobs`` row (``running`` → ``completed`` | ``failed``), updates
    progress after every item, records per-item failures without failing the
    batch, and re-raises unexpected infrastructure errors after marking the job
    ``failed`` (PRD §36).
    """
    job_id = _create_job_row(db)
    rows = db.execute(
        "SELECT id, file_path FROM media WHERE thumbnail_path IS NULL ORDER BY id"
        + (" LIMIT ?" if limit is not None else ""),
        (limit,) if limit is not None else (),
    ).fetchall()
    total = len(rows)
    processed = 0
    failed = 0
    logger.info("thumbnail job started job_id=%d total=%d", job_id, total)
    try:
        for position, row in enumerate(rows, start=1):
            media_id = int(row["id"])
            try:
                thumb_path, preview_path = await asyncio.to_thread(
                    generate_thumbnails, media_id, row["file_path"], settings
                )
            except (ThumbnailError, OSError) as exc:
                failed += 1
                logger.warning("thumbnail failed job_id=%d media_id=%d reason=%s",
                               job_id, media_id, exc)
            else:
                with transaction(db):
                    db.execute(
                        "UPDATE media SET thumbnail_path = ?, preview_path = ? WHERE id = ?",
                        (str(thumb_path), str(preview_path), media_id),
                    )
                processed += 1
            _update_progress(db, job_id, position / max(1, total),
                             f"processed={processed}/{total} failed={failed}")
    except Exception as exc:
        logger.exception("thumbnail job crashed job_id=%d", job_id)
        _finish(db, job_id, "failed", f"processed={processed}/{total} failed={failed}",
                error=str(exc))
        raise
    summary = ThumbnailJobSummary(job_id=job_id, total=total, processed=processed, failed=failed)
    _finish(db, job_id, "completed", _summary_message(summary))
    logger.info("thumbnail job finished job_id=%d processed=%d failed=%d",
                job_id, processed, failed)
    return summary


def _create_job_row(db: sqlite3.Connection) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO jobs (job_type, status, progress, message, started_at) "
            "VALUES ('thumbnail', 'running', 0.0, 'starting', datetime('now'))"
        )
    return int(cursor.lastrowid)


def _update_progress(db: sqlite3.Connection, job_id: int, progress: float, message: str) -> None:
    """Persist progress; telemetry failures are logged, never fatal (PRD §36)."""
    try:
        with transaction(db):
            db.execute(
                "UPDATE jobs SET progress = ?, message = ? WHERE id = ?",
                (progress, message, job_id),
            )
    except sqlite3.Error:
        logger.exception("thumbnail progress update failed job_id=%s", job_id)


def _finish(
    db: sqlite3.Connection,
    job_id: int,
    status: str,
    message: str,
    *,
    progress: float = 1.0,
    error: str | None = None,
) -> None:
    with transaction(db):
        db.execute(
            "UPDATE jobs SET status = ?, progress = ?, message = ?, error = ?, "
            "completed_at = datetime('now') WHERE id = ?",
            (status, progress, message, error, job_id),
        )


def _summary_message(summary: ThumbnailJobSummary) -> str:
    """``key=value`` completion message for the jobs row."""
    return f"processed={summary.processed}/{summary.total} failed={summary.failed}"
