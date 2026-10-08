"""Item-level AI retry budget tests — deferral, backoff, exhaustion (PRD §18, §36).

All offline: local provider stubs (mock-derived) raise typed ``retryable``
failures, the clock is advanced by writing ``ai_next_retry_at`` into the past
via SQL, and every run goes through the real :func:`run_ai_queue` on a temp DB.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend.ai import (
    AIResponseError,
    AIUnavailableError,
    MockVisionProvider,
    run_ai_queue,
)
from backend.ai.queue import VIDEO_UNSUPPORTED_REASON, process_single_media
from backend.config import Settings, load_settings
from backend.database.database import transaction

#: Comfortable backoff base so countdown assertions have room to breathe.
RETRY_INTERVAL = 60.0


def make_settings(**ai_overrides: Any) -> Settings:
    """Offline-safe AI settings; explicit ``api_key`` beats any ambient .env."""
    settings = load_settings()
    values: dict[str, Any] = {
        "provider": "mock",
        "api_key": None,
        "retry_backoff_seconds": 0.0,
        "ai_concurrency": 2,
        "retry_interval_seconds": RETRY_INTERVAL,
        **ai_overrides,
    }
    return replace(settings, ai=replace(settings.ai, **values))


def insert_media(db: sqlite3.Connection, path: Path, *, extension: str = "png") -> int:
    """Insert a media row as the download stage would leave it."""
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO media (file_path, mime_type, extension, original_filename, "
            "processing_status) VALUES (?, ?, ?, ?, 'DOWNLOADED')",
            (str(path), "image/png", extension, path.name),
        )
    return int(cursor.lastrowid)


def media_row(db: sqlite3.Connection, media_id: int) -> sqlite3.Row:
    row = db.execute(
        "SELECT processing_status, ai_attempts, ai_next_retry_at FROM media WHERE id = ?",
        (media_id,),
    ).fetchone()
    assert row is not None
    return row


def seconds_until_retry(db: sqlite3.Connection, media_id: int) -> int:
    """Whole seconds from now until the item's backoff expires (SQL-side math)."""
    row = db.execute(
        "SELECT CAST(ROUND((julianday(ai_next_retry_at) - julianday('now')) * 86400) "
        "AS INTEGER) AS seconds_left FROM media WHERE id = ?",
        (media_id,),
    ).fetchone()
    assert row is not None and row["seconds_left"] is not None
    return int(row["seconds_left"])


def expire_backoff(db: sqlite3.Connection, media_id: int) -> None:
    """Advance the clock: park ``ai_next_retry_at`` one second into the past."""
    with transaction(db):
        db.execute(
            "UPDATE media SET ai_next_retry_at = datetime('now', '-1 second') WHERE id = ?",
            (media_id,),
        )


def latest_ai_job(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM jobs WHERE job_type = 'ai_analysis' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return row


class TransientVisionProvider(MockVisionProvider):
    """Vision that always fails with a retryable provider error (rate limit style)."""

    def __init__(self, message: str = "OpenRouter chat HTTP 429") -> None:
        super().__init__()
        self._message = message

    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        raise AIResponseError(self._message, retryable=True)


class PermanentVisionProvider(MockVisionProvider):
    """Vision that fails with a non-retryable error (must fail immediately)."""

    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        raise AIResponseError("vision exploded")


class FlakyEmbeddingProvider(MockVisionProvider):
    """Vision works; embeddings fail retryably until :attr:`embedding_fails` flips off."""

    def __init__(self) -> None:
        super().__init__()
        self.vision_calls = 0
        self.embedding_calls = 0
        self.embedding_fails = True

    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        self.vision_calls += 1
        return await super().analyze_image(image_bytes, mime_type=mime_type)

    async def generate_embedding(self, text: str) -> list[float]:
        self.embedding_calls += 1
        if self.embedding_fails:
            raise AIUnavailableError("OpenRouter embeddings HTTP 429", retryable=True)
        return await super().generate_embedding(text)


# ---------------------------------------------------------------------------
# Deferral: status returns, budget bumps, claim excludes, summary counts
# ---------------------------------------------------------------------------


async def test_retryable_failure_defers_instead_of_failing(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"])
    summary = await run_ai_queue(db, TransientVisionProvider(), make_settings())

    assert summary.deferred == 1
    assert summary.failed == 0 and summary.ready == 0
    row = media_row(db, media_id)
    assert row["processing_status"] == "DOWNLOADED"  # no analysis yet → back to claimable status
    assert int(row["ai_attempts"]) == 1
    assert row["ai_next_retry_at"] is not None
    assert seconds_until_retry(db, media_id) > 0  # parked in the future
    # Deferrals are visible in the job counters (they are not failures).
    message = str(latest_ai_job(db)["message"])
    assert "deferred=1" in message and "failed=0" in message
    assert "media_id=" in message  # the deferral reason is surfaced as last_error


async def test_deferred_row_is_excluded_from_claims_until_backoff_passes(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"])
    first = await run_ai_queue(db, TransientVisionProvider(), make_settings())
    assert first.deferred == 1

    # Still deferred → a fresh run with a healthy provider must not claim it.
    second = await run_ai_queue(db, MockVisionProvider(), make_settings())
    assert second.total == 0 and second.ready == 0
    assert media_row(db, media_id)["processing_status"] == "DOWNLOADED"

    # Clock advanced → claimed again, and this time it succeeds.
    expire_backoff(db, media_id)
    third = await run_ai_queue(db, MockVisionProvider(), make_settings())
    assert third.ready == 1
    row = media_row(db, media_id)
    assert row["processing_status"] == "READY"
    assert int(row["ai_attempts"]) == 0  # success resets the budget
    assert row["ai_next_retry_at"] is None


async def test_backoff_doubles_per_attempt_and_is_capped(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"])
    settings = make_settings(retry_interval_max_seconds=90.0)

    await run_ai_queue(db, TransientVisionProvider(), settings)
    assert 50 <= seconds_until_retry(db, media_id) <= int(RETRY_INTERVAL) + 2  # base 60s

    expire_backoff(db, media_id)
    await run_ai_queue(db, TransientVisionProvider(), settings)
    assert media_row(db, media_id)["ai_attempts"] == 2
    assert 85 <= seconds_until_retry(db, media_id) <= 92  # 60 * 2 = 120, capped at 90


async def test_budget_exhaustion_fails_with_attempts_in_reason(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"])
    settings = make_settings(max_item_attempts=2)

    first = await run_ai_queue(db, TransientVisionProvider(), settings)
    assert first.deferred == 1
    expire_backoff(db, media_id)

    second = await run_ai_queue(db, TransientVisionProvider(), settings)
    assert second.failed == 1 and second.deferred == 0
    failure = second.failures[0]
    assert f"media_id={media_id}" in failure
    assert "attempts=2 of 2" in failure  # retry-exhaustion made visible (PRD §36)
    assert media_row(db, media_id)["processing_status"] == "FAILED"


async def test_non_retryable_failure_fails_immediately(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"])
    summary = await run_ai_queue(db, PermanentVisionProvider(), make_settings())

    assert summary.failed == 1 and summary.deferred == 0
    row = media_row(db, media_id)
    assert row["processing_status"] == "FAILED"
    assert int(row["ai_attempts"]) == 0
    assert row["ai_next_retry_at"] is None
    assert "attempts=" not in summary.failures[0]  # not a retry-exhaustion


async def test_video_fails_immediately_without_consuming_budget(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO media (file_path, mime_type, extension, original_filename, "
            "processing_status) VALUES (?, 'video/mp4', 'mp4', 'clip.mp4', 'DOWNLOADED')",
            (str(video),),
        )
    media_id = int(cursor.lastrowid)
    summary = await run_ai_queue(db, TransientVisionProvider(), make_settings())

    assert summary.failed == 1 and summary.deferred == 0
    assert VIDEO_UNSUPPORTED_REASON in summary.failures[0]
    row = media_row(db, media_id)
    assert row["processing_status"] == "FAILED"
    assert int(row["ai_attempts"]) == 0 and row["ai_next_retry_at"] is None


# ---------------------------------------------------------------------------
# Vision/embedding split, success reset
# ---------------------------------------------------------------------------


async def test_embedding_failure_retry_skips_vision(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"])
    provider = FlakyEmbeddingProvider()
    first = await run_ai_queue(db, provider, make_settings())

    assert first.deferred == 1
    row = media_row(db, media_id)
    assert row["processing_status"] == "ANALYZED"  # vision succeeded → ANALYZED, not DOWNLOADED
    assert int(row["ai_attempts"]) == 1
    analysis = db.execute("SELECT description FROM ai_metadata WHERE media_id = ?",
                          (media_id,)).fetchone()
    assert analysis is not None
    first_description = str(analysis["description"])
    assert provider.vision_calls == 1 and provider.embedding_calls == 1

    provider.embedding_fails = False
    expire_backoff(db, media_id)
    second = await run_ai_queue(db, provider, make_settings())

    assert second.ready == 1
    assert provider.vision_calls == 1  # the retry skipped vision entirely
    assert provider.embedding_calls == 2
    row = media_row(db, media_id)
    assert row["processing_status"] == "READY"
    assert int(row["ai_attempts"]) == 0 and row["ai_next_retry_at"] is None
    analysis = db.execute("SELECT description FROM ai_metadata WHERE media_id = ?",
                          (media_id,)).fetchone()
    assert analysis is not None and str(analysis["description"]) == first_description


async def test_success_resets_a_preexisting_budget(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    media_id = insert_media(db, sample_images["png"])
    with transaction(db):
        db.execute("UPDATE media SET ai_attempts = 5 WHERE id = ?", (media_id,))
    summary = await run_ai_queue(db, MockVisionProvider(), make_settings())

    assert summary.ready == 1
    row = media_row(db, media_id)
    assert row["processing_status"] == "READY"
    assert int(row["ai_attempts"]) == 0 and row["ai_next_retry_at"] is None


async def test_manual_reanalyze_resets_an_exhausted_budget(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    """A manual Reanalyze (process_single_media) starts a fresh retry cycle.

    An exhausted FAILED item (attempts at the cap) must re-enter the deferral
    loop on the next transient error instead of failing terminally at once.
    """
    settings = make_settings()
    media_id = insert_media(db, sample_images["png"])
    with transaction(db):
        db.execute(
            "UPDATE media SET processing_status = 'FAILED', ai_attempts = ? WHERE id = ?",
            (settings.ai.max_item_attempts, media_id),
        )

    result = await process_single_media(db, media_id, TransientVisionProvider(), settings)

    assert result.deferred  # budget was reset → still within its retry cycle
    row = media_row(db, media_id)
    assert row["processing_status"] == "DOWNLOADED"  # parked for the supervisor
    assert int(row["ai_attempts"]) == 1
    assert row["ai_next_retry_at"] is not None


async def test_job_message_reports_deferred_counters(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    insert_media(db, sample_images["png"])
    summary = await run_ai_queue(db, TransientVisionProvider(), make_settings())

    job = latest_ai_job(db)
    assert job["status"] == "completed"
    assert json.loads(job["params"])["provider"] == "mock"
    message = str(job["message"])
    assert "done=0/1" in message and "deferred=1" in message
    assert summary.last_error is not None
