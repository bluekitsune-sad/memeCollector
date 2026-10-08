"""AI queue tests (M3.5) — end-to-end offline: statuses, metadata, embeddings, jobs rows.

Everything runs against the deterministic mock provider (no network, no key);
provider stubs inject failures/slowness where a test needs them.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from backend.ai import (
    AIQueueController,
    AIResponseError,
    AIUnavailableError,
    MockVisionProvider,
    deserialize_embedding,
    process_single_media,
    run_ai_queue,
    serialize_embedding,
)
from backend.ai.queue import VIDEO_UNSUPPORTED_REASON
from backend.config import Settings, load_settings
from backend.database.database import transaction
from backend.jobs.ai_job import AIJob

TEST_API_KEY = "sk-or-TEST-KEY-NOT-A-REAL-ONE"


def make_settings(**ai_overrides: Any) -> Settings:
    """Offline-safe AI settings; explicit ``api_key`` beats any ambient .env."""
    settings = load_settings()
    values: dict[str, Any] = {
        "provider": "mock",
        "api_key": None,
        "retry_backoff_seconds": 0.0,
        "ai_concurrency": 2,
        **ai_overrides,
    }
    return replace(settings, ai=replace(settings.ai, **values))


def insert_media(
    db: sqlite3.Connection,
    path: Path,
    *,
    mime_type: str,
    extension: str,
    status: str = "DOWNLOADED",
    original_filename: str | None = None,
) -> int:
    """Insert a media row as the download stage would leave it."""
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO media (file_path, mime_type, extension, original_filename, "
            "processing_status) VALUES (?, ?, ?, ?, ?)",
            (str(path), mime_type, extension, original_filename or path.name, status),
        )
    return int(cursor.lastrowid)


def insert_analysis(db: sqlite3.Connection, media_id: int) -> None:
    """Insert a pre-existing ``ai_metadata`` row (backfill/resume scenarios)."""
    with transaction(db):
        db.execute(
            "INSERT INTO ai_metadata (media_id, description, tags, emotions, subjects, "
            "meme_context, suggested_search_phrases, ai_provider, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'mock', 'mock-vision')",
            (
                media_id,
                "pre-existing analysis",
                json.dumps(["confused", "reaction"]),
                json.dumps(["confusion"]),
                json.dumps(["person"]),
                "reaction meme",
                json.dumps(["confused reaction"]),
            ),
        )


def media_status(db: sqlite3.Connection, media_id: int) -> str:
    row = db.execute("SELECT processing_status FROM media WHERE id = ?", (media_id,)).fetchone()
    assert row is not None
    return str(row["processing_status"])


def latest_ai_job(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM jobs WHERE job_type = 'ai_analysis' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return row


class FailingVisionProvider(MockVisionProvider):
    """Mock whose *image* analysis always fails (GIF path still works)."""

    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        raise AIResponseError("vision exploded")


class SlowVisionProvider(MockVisionProvider):
    """Mock that sleeps during analysis and records peak concurrency."""

    def __init__(self, delay: float = 0.05) -> None:
        super().__init__()
        self._delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
            return await super().analyze_image(image_bytes, mime_type=mime_type)
        finally:
            self.in_flight -= 1


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


async def test_image_reaches_ready_with_metadata_and_embedding(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(
        db, sample_images["png"], mime_type="image/png", extension="png"
    )
    summary = await run_ai_queue(db, MockVisionProvider(), make_settings())

    assert (summary.total, summary.ready, summary.failed) == (1, 1, 0)
    assert not summary.cancelled
    assert media_status(db, media_id) == "READY"

    analysis = db.execute("SELECT * FROM ai_metadata WHERE media_id = ?", (media_id,)).fetchone()
    assert analysis is not None
    assert analysis["description"]
    assert json.loads(analysis["tags"])  # JSON array column
    assert analysis["ai_provider"] == "mock"
    assert analysis["model"] == "mock-vision"
    assert analysis["processed_at"]

    embedding = db.execute("SELECT * FROM embeddings WHERE media_id = ?", (media_id,)).fetchone()
    assert embedding is not None
    assert embedding["embedding_model"] == "mock/384"
    vector = deserialize_embedding(embedding["embedding"])
    assert vector.shape == (384,)
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5

    job = latest_ai_job(db)
    assert job["status"] == "completed"
    assert job["progress"] == 1.0
    assert "done=1/1" in job["message"] and "ready=1" in job["message"]
    assert job["started_at"] and job["completed_at"]
    params = json.loads(job["params"])
    assert params["provider"] == "mock"


async def test_gif_item_uses_frame_sampling(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(
        db, sample_images["gif"], mime_type="image/gif", extension="gif"
    )
    summary = await run_ai_queue(db, MockVisionProvider(), make_settings())

    assert summary.ready == 1
    assert media_status(db, media_id) == "READY"
    analysis = db.execute("SELECT description FROM ai_metadata WHERE media_id = ?", (media_id,)).fetchone()
    assert analysis is not None
    assert "frames=3" in analysis["description"]  # sampled frames reached analyze_gif


async def test_video_fails_with_documented_reason(db: sqlite3.Connection, tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    media_id = insert_media(db, video, mime_type="video/mp4", extension="mp4")
    summary = await run_ai_queue(db, MockVisionProvider(), make_settings())

    assert summary.failed == 1 and summary.ready == 0
    assert media_status(db, media_id) == "FAILED"
    assert VIDEO_UNSUPPORTED_REASON in summary.failures[0]
    assert db.execute("SELECT 1 FROM ai_metadata WHERE media_id = ?", (media_id,)).fetchone() is None
    job = latest_ai_job(db)
    assert job["status"] == "completed"
    assert "failed=1" in job["message"]


# ---------------------------------------------------------------------------
# Failure isolation, concurrency, control
# ---------------------------------------------------------------------------


async def test_item_failure_never_stops_the_queue(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    bad = insert_media(db, sample_images["png"], mime_type="image/png", extension="png")
    good = insert_media(
        db, sample_images["gif"], mime_type="image/gif", extension="gif", original_filename="ok.gif"
    )
    summary = await run_ai_queue(db, FailingVisionProvider(), make_settings())

    assert (summary.ready, summary.failed) == (1, 1)
    assert media_status(db, bad) == "FAILED"
    assert media_status(db, good) == "READY"
    assert any(f"media_id={bad}" in failure and "vision exploded" in failure
               for failure in summary.failures)
    job = latest_ai_job(db)
    assert job["status"] == "completed"
    assert "failed=1" in job["message"] and "last_error=" in job["message"]


async def test_concurrency_is_bounded_by_ai_concurrency(
    db: sqlite3.Connection, sample_images: dict[str, Path], tmp_path: Path
) -> None:
    for index in range(4):
        image = tmp_path / f"img{index}.png"
        image.write_bytes(sample_images["png"].read_bytes())
        insert_media(db, image, mime_type="image/png", extension="png")
    provider = SlowVisionProvider(delay=0.05)
    summary = await run_ai_queue(db, provider, make_settings(ai_concurrency=2))

    assert summary.ready == 4
    assert provider.max_in_flight == 2  # parallel up to the limit, never beyond it


async def test_cancel_before_run_leaves_items_downloaded(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"], mime_type="image/png", extension="png")
    controller = AIQueueController()
    controller.cancel()
    summary = await run_ai_queue(db, MockVisionProvider(), make_settings(), controller=controller)

    assert summary.cancelled
    assert summary.done == 0
    assert media_status(db, media_id) == "DOWNLOADED"
    job = latest_ai_job(db)
    assert job["status"] == "cancelled"
    assert job["progress"] == 0.0
    assert "cancelled" in job["message"]


async def test_pause_holds_queue_then_resume_completes(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"], mime_type="image/png", extension="png")
    controller = AIQueueController()
    controller.pause()
    task = asyncio.ensure_future(
        run_ai_queue(db, MockVisionProvider(), make_settings(), controller=controller)
    )
    await asyncio.sleep(0.05)

    assert not task.done()
    assert media_status(db, media_id) == "DOWNLOADED"
    assert latest_ai_job(db)["message"] == "paused"

    controller.resume()
    summary = await task
    assert summary.ready == 1
    assert media_status(db, media_id) == "READY"
    assert latest_ai_job(db)["status"] == "completed"


async def test_missing_file_marks_failed_and_queue_continues(
    db: sqlite3.Connection, sample_images: dict[str, Path], tmp_path: Path
) -> None:
    ghost = insert_media(
        db, tmp_path / "ghost.png", mime_type="image/png", extension="png"
    )
    fine = insert_media(db, sample_images["png"], mime_type="image/png", extension="png")
    summary = await run_ai_queue(db, MockVisionProvider(), make_settings())

    assert (summary.ready, summary.failed) == (1, 1)
    assert media_status(db, ghost) == "FAILED"
    assert "FileNotFoundError" in summary.failures[0]
    assert media_status(db, fine) == "READY"


# ---------------------------------------------------------------------------
# Backfill and single-item retries
# ---------------------------------------------------------------------------


async def test_analyzed_backfill_gets_embedding_without_vision(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(
        db, sample_images["png"], mime_type="image/png", extension="png", status="ANALYZED"
    )
    insert_analysis(db, media_id)
    # Vision would fail if called — the backfill must resume at the embedding step.
    summary = await run_ai_queue(db, FailingVisionProvider(), make_settings())

    assert summary.ready == 1
    assert media_status(db, media_id) == "READY"
    embedding = db.execute("SELECT 1 FROM embeddings WHERE media_id = ?", (media_id,)).fetchone()
    assert embedding is not None


async def test_stale_analyzing_row_is_recovered(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(
        db, sample_images["png"], mime_type="image/png", extension="png", status="ANALYZING"
    )
    summary = await run_ai_queue(db, MockVisionProvider(), make_settings())
    assert summary.ready == 1
    assert media_status(db, media_id) == "READY"


async def test_process_single_media_retries_failed_item(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"], mime_type="image/png", extension="png")
    first = await run_ai_queue(db, FailingVisionProvider(), make_settings())
    assert first.failed == 1
    assert media_status(db, media_id) == "FAILED"

    result = await process_single_media(db, media_id, MockVisionProvider(), make_settings())
    assert result.ok
    assert media_status(db, media_id) == "READY"


async def test_process_single_media_resumes_at_embedding_when_metadata_exists(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(
        db, sample_images["png"], mime_type="image/png", extension="png", status="FAILED"
    )
    insert_analysis(db, media_id)
    # Vision still fails — success proves the retry skipped straight to embedding.
    result = await process_single_media(db, media_id, FailingVisionProvider(), make_settings())
    assert result.ok
    assert media_status(db, media_id) == "READY"


async def test_process_single_media_unknown_id_raises(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no media row"):
        await process_single_media(db, 999, MockVisionProvider(), make_settings())


# ---------------------------------------------------------------------------
# Embedding serialization (documented in backend.ai.queue docstring)
# ---------------------------------------------------------------------------


def test_embedding_serialization_roundtrip() -> None:
    vector = [0.5, -0.25, 1.0, 0.0]
    blob = serialize_embedding(vector)
    assert len(blob) == 4 * 4  # little-endian float32, 4 bytes per value
    assert deserialize_embedding(blob).tolist() == pytest.approx(vector)


def test_deserialize_rejects_malformed_blob() -> None:
    with pytest.raises(ValueError, match="multiple of 4"):
        deserialize_embedding(b"\x00\x01\x02")


# ---------------------------------------------------------------------------
# AIJob façade (backend/jobs/ai_job.py)
# ---------------------------------------------------------------------------


async def test_ai_job_runs_queue_and_exposes_job_id(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    insert_media(db, sample_images["png"], mime_type="image/png", extension="png")
    job = AIJob(db=db, settings=make_settings(), provider=MockVisionProvider())
    summary = await job.run()

    assert summary.ready == 1
    assert job.job_id == summary.job_id
    assert latest_ai_job(db)["status"] == "completed"


async def test_ai_job_without_key_records_failed_job(db: sqlite3.Connection) -> None:
    settings = make_settings(provider="openrouter", api_key=None)
    job = AIJob(db=db, settings=settings)
    with pytest.raises(AIUnavailableError, match="OPENROUTER_API_KEY"):
        await job.run()

    row = latest_ai_job(db)
    assert row["status"] == "failed"
    assert "OPENROUTER_API_KEY" in row["error"]
    assert row["completed_at"]


async def test_ai_job_cancel_is_cooperative(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"], mime_type="image/png", extension="png")
    job = AIJob(db=db, settings=make_settings(), provider=MockVisionProvider())
    job.cancel()
    summary = await job.run()
    assert summary.cancelled
    assert media_status(db, media_id) == "DOWNLOADED"
