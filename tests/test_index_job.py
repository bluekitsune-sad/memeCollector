"""INDEX-stage worker tests — ``run_index_job`` (PRD §35, §36, §57).

The index job is the pipeline's INDEX stage: it rebuilds ``media_fts`` under a
tracked ``index_rebuild`` jobs row. Both paths are covered offline: a successful
rebuild records ``indexed=N`` on a completed row, and a crash records the error
on a failed row *after* re-raising (the caller marks the pipeline failed).
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.database.database import transaction
from backend.jobs.index_job import INDEX_JOB_TYPE, IndexJobSummary, run_index_job
from backend.search import keyword
from tests.test_search import _seed


def _job_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM jobs WHERE job_type = ? ORDER BY id", (INDEX_JOB_TYPE,)
    ).fetchall()


async def test_index_job_rebuilds_index_and_records_completed_row(db: sqlite3.Connection) -> None:
    first = _seed(db, description="first searchable description")
    second = _seed(db, description="second searchable description")
    with transaction(db):
        db.execute("DELETE FROM media_fts")  # simulate a never-indexed library

    summary = await run_index_job(db)

    assert isinstance(summary, IndexJobSummary)
    assert summary.indexed == 2
    assert int(db.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0]) == 2
    assert [hit[0] for hit in keyword.query(db, "first")] == [first]
    assert [hit[0] for hit in keyword.query(db, "second")] == [second]

    (job,) = _job_rows(db)
    assert job["status"] == "completed"
    assert job["message"] == "indexed=2"
    assert job["error"] is None
    assert job["progress"] == 1.0
    assert job["started_at"] is not None and job["completed_at"] is not None


async def test_index_job_is_idempotent(db: sqlite3.Connection) -> None:
    _seed(db, description="only row")

    first = await run_index_job(db)
    second = await run_index_job(db)

    assert first.indexed == second.indexed == 1
    assert int(db.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0]) == 1
    assert len(_job_rows(db)) == 2  # every run leaves its own telemetry row


async def test_index_job_failure_records_error_and_reraises(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db, description="doomed row")

    def _explode(conn: sqlite3.Connection) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("backend.jobs.index_job.rebuild_fts_index", _explode)

    with pytest.raises(sqlite3.OperationalError):
        await run_index_job(db)

    (job,) = _job_rows(db)
    assert job["status"] == "failed"
    assert job["error"] == "disk I/O error"
    assert job["message"] == "index rebuild failed"
    assert job["completed_at"] is not None
