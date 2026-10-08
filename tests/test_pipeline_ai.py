"""AI-stage tests (T1): pipeline wiring, graceful degradation, reanalyze retry.

Covers the PROCESS stage of the scrape pipeline (PRD §18, §36, §57) and the
``POST /api/media/{id}/reanalyze`` retry endpoint end to end, offline:

* with ``ai.provider=openrouter`` and **no key**, the AI stage is skipped with a
  warning — the pipeline still finishes (crawl → thumbnails → dup scan →
  index), no ``ai_analysis`` job row appears, and stored media stays
  ``DOWNLOADED``;
* with the **mock provider**, the stage claims claimable rows, stores analysis
  + embedding and finishes ``READY`` under a completed ``ai_analysis`` row;
* the ``app.state.ai_queue_running`` guard makes a second stage call a no-op
  while a queue is live;
* ``/reanalyze`` enforces 404 / 400 (status not FAILED|READY) / 503 (provider
  cannot be built) and, on success, re-runs analysis and reindexes the row.

Also pins the documented env override: ``MEME_AI_PROVIDER`` → ``ai.provider``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.routes_scraper import _run_ai_stage
from backend.config import Settings, load_settings
from backend.database.database import transaction
from backend.main import create_app
from backend.search import keyword
from tests.test_api import ENTRY_URL
from tests.test_duplicates import _ingest_unique

#: Stages that must finish even when the AI stage degrades (no key → no ai row).
STAGES_WITHOUT_AI = ("crawl", "thumbnail", "dup_scan", "index_rebuild")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_settings(tmp_path: Path, ai_overrides: dict[str, object]) -> Settings:
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "pipeline_ai.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    crawler = replace(base.crawler, delay_seconds=0.0, concurrency=1)
    return replace(base, storage=storage, crawler=crawler, ai=replace(base.ai, **ai_overrides))


@pytest.fixture
def mock_client(tmp_path: Path):
    """App wired to the offline mock provider (analysis really runs)."""
    settings = _build_settings(tmp_path, {"provider": "mock"})
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def no_key_client(tmp_path: Path):
    """App wired to openrouter with no key — the degraded configuration (PRD §36)."""
    settings = _build_settings(tmp_path, {"provider": "openrouter", "api_key": None})
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_statuses(db) -> dict[str, str]:
    rows = db.execute("SELECT job_type, status FROM jobs ORDER BY id").fetchall()
    return {row["job_type"]: row["status"] for row in rows}


def _wait_for_stages(client: TestClient, wanted: tuple[str, ...], timeout: float = 10.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    statuses: dict[str, str] = {}
    while time.monotonic() < deadline:
        statuses = _job_statuses(client.app.state.db)
        if all(
            stage in statuses and statuses[stage] in ("completed", "failed")
            for stage in wanted
        ):
            return statuses
        time.sleep(0.05)
    pytest.fail(f"stages {wanted} not finished within {timeout}s: {statuses}")


def _set_status(db, media_id: int, status: str) -> None:
    with transaction(db):
        db.execute("UPDATE media SET processing_status = ? WHERE id = ?", (status, media_id))


def _status(db, media_id: int) -> str:
    row = db.execute("SELECT processing_status FROM media WHERE id = ?", (media_id,)).fetchone()
    return str(row["processing_status"])


# ---------------------------------------------------------------------------
# Pipeline: degraded AI stage
# ---------------------------------------------------------------------------


def test_pipeline_without_ai_key_still_finishes_all_other_stages(
    no_key_client: TestClient, fake_adapter
) -> None:
    started = no_key_client.post(
        "/api/scrape",
        json={"url": ENTRY_URL, "scope": "custom_urls", "urls": []},
    )
    assert started.status_code == 202

    statuses = _wait_for_stages(no_key_client, STAGES_WITHOUT_AI)
    assert all(statuses[stage] == "completed" for stage in STAGES_WITHOUT_AI)
    # The unavailable provider means no queue run at all — no job row, no failure.
    assert "ai_analysis" not in statuses
    assert no_key_client.app.state.ai_queue_running is False
    assert no_key_client.get("/api/media").json()["total"] == 0


def test_ai_stage_without_key_is_a_no_op(no_key_client: TestClient, tmp_path: Path) -> None:
    app = no_key_client.app
    media_id = _ingest_unique(app.state.db, app.state.settings, tmp_path, "nokey")

    asyncio.run(_run_ai_stage(app, None))

    assert _job_statuses(app.state.db) == {}
    assert _status(app.state.db, media_id) == "DOWNLOADED"
    assert app.state.ai_queue_running is False
    counted = app.state.db.execute("SELECT COUNT(*) FROM ai_metadata").fetchone()[0]
    assert int(counted) == 0


def test_ai_stage_is_skipped_while_a_queue_is_running(
    mock_client: TestClient, tmp_path: Path
) -> None:
    app = mock_client.app
    media_id = _ingest_unique(app.state.db, app.state.settings, tmp_path, "guarded")
    app.state.ai_queue_running = True  # a live queue owns the claimable rows
    try:
        asyncio.run(_run_ai_stage(app, None))
    finally:
        app.state.ai_queue_running = False

    assert _job_statuses(app.state.db) == {}
    assert _status(app.state.db, media_id) == "DOWNLOADED"


def test_ai_stage_with_mock_provider_analyzes_to_ready(mock_client: TestClient, tmp_path: Path) -> None:
    app = mock_client.app
    media_id = _ingest_unique(app.state.db, app.state.settings, tmp_path, "analyzed")

    asyncio.run(_run_ai_stage(app, None))

    statuses = _job_statuses(app.state.db)
    assert statuses.get("ai_analysis") == "completed"
    assert _status(app.state.db, media_id) == "READY"
    analysis = app.state.db.execute(
        "SELECT description, tags FROM ai_metadata WHERE media_id = ?", (media_id,)
    ).fetchone()
    assert analysis is not None and analysis["description"]
    embedding = app.state.db.execute(
        "SELECT embedding_model FROM embeddings WHERE media_id = ?", (media_id,)
    ).fetchone()
    assert embedding is not None and embedding["embedding_model"].startswith("mock/")
    assert app.state.ai_queue_running is False, "the guard is released even after the run"


# ---------------------------------------------------------------------------
# POST /api/media/{id}/reanalyze
# ---------------------------------------------------------------------------


def test_reanalyze_rejects_unknown_and_ineligible_items(
    mock_client: TestClient, tmp_path: Path
) -> None:
    assert mock_client.post("/api/media/99999/reanalyze").status_code == 404

    app = mock_client.app
    media_id = _ingest_unique(app.state.db, app.state.settings, tmp_path, "queued")
    response = mock_client.post(f"/api/media/{media_id}/reanalyze")

    assert response.status_code == 400
    assert "reanalyze is only allowed for FAILED or READY" in response.json()["detail"]
    assert _status(app.state.db, media_id) == "DOWNLOADED"


def test_reanalyze_success_reanalyzes_reindexes_and_returns_detail(
    mock_client: TestClient, tmp_path: Path
) -> None:
    app = mock_client.app
    media_id = _ingest_unique(app.state.db, app.state.settings, tmp_path, "retryable")
    _set_status(app.state.db, media_id, "FAILED")  # a previous attempt failed

    response = mock_client.post(f"/api/media/{media_id}/reanalyze")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == media_id
    assert body["processing_status"] == "READY"
    assert body["ai_metadata"] is not None
    assert body["ai_metadata"]["description"]
    assert _status(app.state.db, media_id) == "READY"
    # The route reindexes the row so the new description is keyword-searchable now.
    assert [hit[0] for hit in keyword.query(app.state.db, "Mock")] == [media_id]


def test_reanalyze_without_provider_is_503_and_leaves_the_row_untouched(
    no_key_client: TestClient, tmp_path: Path
) -> None:
    app = no_key_client.app
    media_id = _ingest_unique(app.state.db, app.state.settings, tmp_path, "unretryable")
    _set_status(app.state.db, media_id, "FAILED")

    response = no_key_client.post(f"/api/media/{media_id}/reanalyze")

    assert response.status_code == 503
    assert "AI provider unavailable" in response.json()["detail"]
    assert _status(app.state.db, media_id) == "FAILED"
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Config: documented env override
# ---------------------------------------------------------------------------


def test_meme_ai_provider_env_override_selects_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEME_AI_PROVIDER", "mock")
    assert load_settings().ai.provider == "mock"

    monkeypatch.setenv("MEME_AI_PROVIDER", "openrouter")
    assert load_settings().ai.provider == "openrouter"
