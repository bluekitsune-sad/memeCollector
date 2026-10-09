"""Job control tests — pause/resume/cancel semantics incl. stale-running cancel (PRD §36).

A ``running`` row with no live handle can always be cleared by ``cancel`` once
its registration grace period has passed (a crash/restart leaves such rows
behind and the user must be able to clear them from the UI), while pause and
resume stay no-ops everywhere they cannot apply and a *freshly created* row is
protected by the grace window so the just-created-row race can never mark a
live crawl cancelled.

Rows are seeded **after** the app has started (via ``client.app.state.db``):
seeding before the lifespan would let startup recovery fail them first, which
is exactly the behavior ``test_jobs_recovery.py`` covers instead.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from backend.config import Settings, load_settings
from backend.database.database import transaction
from backend.main import create_app


class _FakeCrawlHandle:
    """Duck-typed CrawlJob control surface (the routes only call these)."""

    def __init__(self) -> None:
        self.paused = False
        self.resumed = False
        self.cancelled = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.resumed = True

    def cancel(self) -> None:
        self.cancelled = True


def _settings(tmp_path: Path) -> Settings:
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "control.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    return replace(
        base,
        storage=storage,
        ai=replace(base.ai, provider="mock"),
        watch=replace(base.watch, enabled=False),
    )


def _seed_running_crawl(db, *, age_sql: str) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO jobs (job_type, status, progress, message, started_at) "
            "VALUES ('crawl', 'running', 0.2, 'pages=3/10', datetime('now', ?))",
            (age_sql,),
        )
    return int(cursor.lastrowid)


def test_cancel_clears_a_stale_running_row(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        job_id = _seed_running_crawl(client.app.state.db, age_sql="-1 minute")

        response = client.post(f"/api/jobs/{job_id}/cancel")

        assert response.status_code == 200
        body = response.json()
        assert body["applied"] is True
        assert body["job"]["status"] == "cancelled"
        assert "no longer running" in body["job"]["error"]


def test_pause_and_resume_stay_noops_on_a_stale_row(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        job_id = _seed_running_crawl(client.app.state.db, age_sql="-1 minute")

        paused = client.post(f"/api/jobs/{job_id}/pause").json()
        resumed = client.post(f"/api/jobs/{job_id}/resume").json()

        assert paused["applied"] is False
        assert resumed["applied"] is False
        assert paused["job"]["status"] == "running"


def test_fresh_running_row_is_protected_by_the_grace_period(tmp_path: Path) -> None:
    """A just-created row (handle about to register) must survive a cancel."""
    with TestClient(create_app(_settings(tmp_path))) as client:
        job_id = _seed_running_crawl(client.app.state.db, age_sql="0 seconds")

        response = client.post(f"/api/jobs/{job_id}/cancel")

        assert response.json()["applied"] is False
        assert response.json()["job"]["status"] == "running"


def test_live_handle_receives_the_control_actions(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        job_id = _seed_running_crawl(client.app.state.db, age_sql="-1 minute")
        handle = _FakeCrawlHandle()
        client.app.state.running_crawls[job_id] = handle

        paused = client.post(f"/api/jobs/{job_id}/pause").json()
        resumed = client.post(f"/api/jobs/{job_id}/resume").json()
        cancelled = client.post(f"/api/jobs/{job_id}/cancel").json()

        assert (paused["applied"], resumed["applied"], cancelled["applied"]) == (
            True, True, True,
        )
        assert handle.paused and handle.resumed and handle.cancelled


def test_unknown_job_is_a_404(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post("/api/jobs/12345/cancel")
        assert response.status_code == 404
