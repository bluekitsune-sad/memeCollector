"""Full-text index rebuild job — the INDEX stage worker (PRD §35, §57).

``run_index_job`` reindexes the whole ``media_fts`` table under one tracked
``jobs`` row (``job_type='index_rebuild'``, visible in the Jobs UI), exactly
like :func:`backend.jobs.dup_job.run_dup_job`: the row starts ``running``,
records ``indexed=N`` on completion, and records the error before re-raising on
failure (PRD §36). The rebuild itself is pure SQLite work, so it runs in a
worker thread to keep the event loop free (AGENTS.md §4).

Invoked as the final stage of the scrape pipeline (after PROCESS — PRD §57's
COLLECT → STORE → PROCESS → INDEX → SEARCH) so newly analyzed descriptions and
tags become keyword-searchable; :func:`backend.search.keyword.index_media`
handles single-row updates in between.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from backend.database.database import transaction
from backend.search.keyword import rebuild_fts_index

logger = logging.getLogger(__name__)

#: ``jobs.job_type`` recorded for this worker (PRD §35).
INDEX_JOB_TYPE = "index_rebuild"


@dataclass(frozen=True)
class IndexJobSummary:
    """End-of-job counters for the jobs row / Jobs UI."""

    job_id: int
    indexed: int = 0


async def run_index_job(db: sqlite3.Connection) -> IndexJobSummary:
    """Rebuild ``media_fts`` under an ``index_rebuild`` jobs row; returns the summary."""
    job_id = _create_job_row(db)
    logger.info("index job started job_id=%d", job_id)
    try:
        indexed = await asyncio.to_thread(rebuild_fts_index, db)
    except Exception as exc:
        logger.exception("index job crashed job_id=%d", job_id)
        _finish(db, job_id, "failed", "index rebuild failed", error=str(exc))
        raise
    summary = IndexJobSummary(job_id=job_id, indexed=indexed)
    _finish(db, job_id, "completed", f"indexed={summary.indexed}")
    logger.info("index job finished job_id=%d indexed=%d", job_id, indexed)
    return summary


def _create_job_row(db: sqlite3.Connection) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO jobs (job_type, status, progress, message, started_at) "
            "VALUES (?, 'running', 0.0, 'starting', datetime('now'))",
            (INDEX_JOB_TYPE,),
        )
    return int(cursor.lastrowid)


def _finish(
    db: sqlite3.Connection,
    job_id: int,
    status: str,
    message: str,
    *,
    progress: float = 1.0,
    error: str | None = None,
) -> None:
    """Persist the terminal jobs state; telemetry failures are logged, never fatal."""
    try:
        with transaction(db):
            db.execute(
                "UPDATE jobs SET status = ?, progress = ?, message = ?, error = ?, "
                "completed_at = datetime('now') WHERE id = ?",
                (status, progress, message, error, job_id),
            )
    except sqlite3.Error:
        logger.exception("index job finish update failed job_id=%d", job_id)
