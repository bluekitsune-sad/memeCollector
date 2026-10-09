"""Startup job recovery tests — no zombie ``running`` rows after a restart (PRD §36).

Unit level: :func:`recover_interrupted_jobs` on a temp DB. Integration level:
a ``TestClient`` lifespan fails a ``running`` row seeded *before* the app
started — the exact state a backend restart mid-crawl leaves behind.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from backend.config import Settings, load_settings
from backend.database.database import initialize_database, transaction
from backend.jobs.recovery import INTERRUPTED_ERROR, recover_interrupted_jobs
from backend.main import create_app


def _insert_job(db, status: str, *, error: str | None = None) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO jobs (job_type, status, progress, message, error, started_at) "
            "VALUES ('crawl', ?, 0.18, 'pages=30/100', ?, datetime('now'))",
            (status, error),
        )
    return int(cursor.lastrowid)


def _job(db, job_id: int):
    return db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


# ---------------------------------------------------------------------------
# Unit: recover_interrupted_jobs
# ---------------------------------------------------------------------------


def test_running_job_is_failed_with_an_explicit_error(db) -> None:
    job_id = _insert_job(db, "running")

    recovered = recover_interrupted_jobs(db)

    assert len(recovered) == 1
    assert recovered[0] == {
        "id": job_id,
        "job_type": "crawl",
        "params": recovered[0]["params"],  # None here — seeded without params
    }
    assert recovered[0]["params"] is None
    row = _job(db, job_id)
    assert row["status"] == "failed"
    assert row["error"] == INTERRUPTED_ERROR
    assert row["completed_at"] is not None
    # The frozen progress snapshot stays visible in the Jobs UI.
    assert row["progress"] == 0.18


def test_preexisting_error_is_preserved(db) -> None:
    job_id = _insert_job(db, "running", error="download blew up first")

    recover_interrupted_jobs(db)

    assert _job(db, job_id)["error"] == "download blew up first"


def test_finished_jobs_are_left_alone(db) -> None:
    completed = _insert_job(db, "completed")
    failed = _insert_job(db, "failed")
    cancelled = _insert_job(db, "cancelled")

    assert recover_interrupted_jobs(db) == []

    assert [_job(db, i)["status"] for i in (completed, failed, cancelled)] == [
        "completed",
        "failed",
        "cancelled",
    ]


def test_recovery_is_idempotent(db) -> None:
    _insert_job(db, "running")

    assert len(recover_interrupted_jobs(db)) == 1
    assert recover_interrupted_jobs(db) == []


# ---------------------------------------------------------------------------
# Integration: the app lifespan performs the recovery at startup
# ---------------------------------------------------------------------------


def _recovery_settings(tmp_path: Path) -> Settings:
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "recovery.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    return replace(base, storage=storage, ai=replace(base.ai, provider="mock"))


def test_lifespan_fails_jobs_stranded_by_a_restart(tmp_path: Path) -> None:
    """A crawl row left ``running`` by a dead process is failed on startup."""
    settings = _recovery_settings(tmp_path)
    # Seed the zombie *before* the app exists — exactly what a killed uvicorn leaves.
    conn = initialize_database(settings.storage.database_path)
    job_id = _insert_job(conn, "running")
    conn.close()

    with TestClient(create_app(settings)) as client:
        items = client.get("/api/jobs").json()["items"]
        row = next(item for item in items if item["id"] == job_id)

    assert row["status"] == "failed"
    assert row["error"] == INTERRUPTED_ERROR
    assert row["completed_at"] is not None
