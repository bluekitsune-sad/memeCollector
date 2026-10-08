"""Duplicate scan + purge job — the daily PRD §12.1 rule-7 worker (PRD §35).

:func:`run_dup_job` performs the two halves of the duplicate lifecycle under
one tracked ``jobs`` row (``job_type='dup_scan'``, visible in the Jobs UI):

1. **Scan** — every non-``unflagged`` row goes through
   :func:`backend.media.duplicates.scan_and_flag` (idempotent: it never
   restarts the 7-day clock and never touches user-cleared ``unflagged``
   items). This catches rows inserted outside the ingest path and *repairs*
   stale flags (e.g. a ``dup`` whose retained copy was deleted by hand).
   Runs in a worker thread — it is pure SQLite/CPU work (AGENTS.md §4).
2. **Purge** — :func:`backend.media.duplicates.purge_expired_dups` deletes
   ``dup`` items flagged 7+ days ago, never the last copy of a SHA-256.

``now`` is injectable for tests; per-item surprises are logged, infrastructure
errors mark the job ``failed`` and re-raise (PRD §36).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.config import Settings
from backend.database.database import transaction
from backend.media.duplicates import purge_expired_dups, scan_and_flag

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DupJobSummary:
    """End-of-job counters for the jobs row / Jobs UI."""

    job_id: int
    scanned: int = 0
    flagged: int = 0
    purged: tuple[int, ...] = ()


async def run_dup_job(
    *, db: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> DupJobSummary:
    """Scan flags, then purge expired dups; returns the counter summary."""
    job_id = _create_job_row(db)
    moment = now if now is not None else datetime.now(timezone.utc)
    logger.info("dup job started job_id=%d", job_id)
    try:
        scanned, flagged = await asyncio.to_thread(_scan_all, db)
        _update_progress(
            db, job_id, 0.7, f"scanned={scanned} flagged={flagged} purged=0 purging"
        )
        purged = await asyncio.to_thread(purge_expired_dups, db, settings, moment)
    except Exception as exc:
        logger.exception("dup job crashed job_id=%d", job_id)
        _finish(db, job_id, "failed", "dup scan failed", error=str(exc))
        raise
    summary = DupJobSummary(
        job_id=job_id, scanned=scanned, flagged=flagged, purged=tuple(purged)
    )
    _finish(db, job_id, "completed", _summary_message(summary))
    logger.info("dup job finished job_id=%d scanned=%d flagged=%d purged=%d",
                job_id, scanned, flagged, len(purged))
    return summary


def _scan_all(db: sqlite3.Connection) -> tuple[int, int]:
    """Flag every non-``unflagged`` row; returns ``(scanned, currently_dup)``.

    ``unflagged`` rows are excluded by the query itself — the system must
    never silently re-flag a user-cleared item (PRD §12.1 rule 6).
    """
    media_ids = [
        int(row[0])
        for row in db.execute(
            "SELECT id FROM media WHERE dup_status != 'unflagged' ORDER BY id"
        ).fetchall()
    ]
    scanned = 0
    flagged = 0
    for media_id in media_ids:
        try:
            status = scan_and_flag(db, media_id)
        except ValueError:
            logger.warning("dup scan skipped missing media_id=%d", media_id)
            continue
        scanned += 1
        if status == "dup":
            flagged += 1
    return scanned, flagged


def _create_job_row(db: sqlite3.Connection) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO jobs (job_type, status, progress, message, started_at) "
            "VALUES ('dup_scan', 'running', 0.0, 'starting', datetime('now'))"
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
        logger.exception("dup job progress update failed job_id=%s", job_id)


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


def _summary_message(summary: DupJobSummary) -> str:
    """``key=value`` completion message for the jobs row."""
    return (
        f"scanned={summary.scanned} flagged={summary.flagged} "
        f"purged={len(summary.purged)}"
    )
