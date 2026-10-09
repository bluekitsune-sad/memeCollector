"""Startup recovery for job rows stranded by a crash or restart (PRD §36).

A ``jobs`` row can be left ``status='running'`` when the process dies mid-job —
a backend restart during a crawl, a killed uvicorn, a crash. No live task backs
such a row, so it would sit in the Jobs UI showing frozen progress forever and
the pipeline stages chained after it would never run.

Nothing can be legitimately running before the app starts serving, so recovery
is unconditional: every ``running`` row at startup belongs to a dead process.
It is marked ``failed`` with an explicit error (the Jobs UI surfaces ``error``)
and can simply be re-run — the crawl is idempotent (``force_rescan=False``
skips already-scanned pages) and the AI queue re-claims its own leftovers.
"""

from __future__ import annotations

import logging
import sqlite3

from backend.database.database import transaction

logger = logging.getLogger(__name__)

#: Recorded on rows whose prior error text never got a chance to be written.
INTERRUPTED_ERROR = "interrupted by server restart"


def recover_interrupted_jobs(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Fail every ``running`` job row — a leftover from a dead process.

    Returns one ``{"id", "job_type", "params"}`` dict per recovered row (empty
    when nothing was stranded) so :mod:`backend.jobs.resume` can relaunch the
    interrupted crawls from their original parameters. Safe to call on every
    startup: at lifespan time no job of this process is running yet, so
    ``status='running'`` can only mean "interrupted".
    """
    with transaction(conn):
        rows = conn.execute(
            "SELECT id, job_type, params FROM jobs WHERE status = 'running'"
        ).fetchall()
        conn.execute(
            "UPDATE jobs SET status = 'failed', completed_at = datetime('now'), "
            "error = COALESCE(error, ?) WHERE status = 'running'",
            (INTERRUPTED_ERROR,),
        )
    recovered: list[dict[str, object]] = [
        {"id": int(row["id"]), "job_type": str(row["job_type"]), "params": row["params"]}
        for row in rows
    ]
    if recovered:
        logger.warning(
            "recovered interrupted jobs count=%d error=%s", len(recovered), INTERRUPTED_ERROR
        )
    return recovered
