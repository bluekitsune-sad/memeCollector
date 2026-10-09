"""AI revive tests — terminally-failed items get a second try (PRD §36 resilience).

Covers the revival slate written by ``_mark_failed`` (scheduling rules:
retryable vs permanent failure, revive-budget cap) and
``revive_due_failures`` (the supervisor's flip-back with the
ANALYZED/DOWNLOADED choice, attempts reset and budget accounting). Fully
offline — media rows are seeded directly, no provider is involved.
"""

from __future__ import annotations

from pathlib import Path

from backend.ai.queue import _mark_failed, revive_due_failures
from backend.database.database import transaction
from tests.test_ai_queue import insert_analysis, insert_media, make_settings

#: A content file the row can point at (never opened by these tests).
def _blob(tmp_path: Path, name: str = "revive.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"placeholder")
    return path


def _row(db, media_id: int):
    return db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()


def _make_media(db, tmp_path: Path, **media_kwargs) -> int:
    return insert_media(
        db, _blob(tmp_path), mime_type="image/png", extension="png", **media_kwargs
    )


def _force_due(db, media_id: int) -> None:
    """Push the revive deadline into the past (the supervisor's due check)."""
    with transaction(db):
        db.execute(
            "UPDATE media SET ai_next_retry_at = datetime('now', '-1 second') "
            "WHERE id = ?",
            (media_id,),
        )


def test_retryable_failure_schedules_a_revive(db, tmp_path: Path) -> None:
    settings = make_settings(revive_after_seconds=1800.0, max_item_revives=3)
    media_id = _make_media(db, tmp_path)

    _mark_failed(db, media_id, retryable=True, settings=settings)

    row = _row(db, media_id)
    assert row["processing_status"] == "FAILED"
    assert row["ai_failed_at"] is not None
    assert row["ai_error_retryable"] == 1
    assert row["ai_next_retry_at"] is not None  # now + 1800s — watched by the supervisor


def test_permanent_failure_never_schedules_a_revive(db, tmp_path: Path) -> None:
    settings = make_settings()
    media_id = _make_media(db, tmp_path)

    _mark_failed(db, media_id, retryable=False, settings=settings)

    row = _row(db, media_id)
    assert row["processing_status"] == "FAILED"
    assert row["ai_error_retryable"] == 0
    assert row["ai_next_retry_at"] is None


def test_exhausted_revive_budget_stops_scheduling(db, tmp_path: Path) -> None:
    settings = make_settings(max_item_revives=2)
    media_id = _make_media(db, tmp_path)
    with transaction(db):
        db.execute("UPDATE media SET ai_revives = 2 WHERE id = ?", (media_id,))

    _mark_failed(db, media_id, retryable=True, settings=settings)

    row = _row(db, media_id)
    assert row["ai_next_retry_at"] is None  # no deadline → the supervisor never revives it
    assert row["ai_error_retryable"] == 1  # the failure class is still recorded


def test_due_retryable_failure_is_revived_to_downloaded(db, tmp_path: Path) -> None:
    settings = make_settings()
    media_id = _make_media(db, tmp_path, status="ANALYZING")
    with transaction(db):
        db.execute("UPDATE media SET ai_attempts = 5 WHERE id = ?", (media_id,))
    _mark_failed(db, media_id, retryable=True, settings=settings)
    _force_due(db, media_id)

    revived = revive_due_failures(db, settings)

    assert revived == 1
    row = _row(db, media_id)
    assert row["processing_status"] == "DOWNLOADED"  # no metadata yet → full re-analysis
    assert row["ai_attempts"] == 0  # fresh budget
    assert row["ai_failed_at"] is None and row["ai_error_retryable"] == 0
    assert row["ai_next_retry_at"] is None
    assert row["ai_revives"] == 1  # budget accounted


def test_due_failure_with_existing_metadata_goes_straight_to_analyzed(
    db, tmp_path: Path
) -> None:
    settings = make_settings()
    media_id = _make_media(db, tmp_path)
    insert_analysis(db, media_id)
    _mark_failed(db, media_id, retryable=True, settings=settings)
    _force_due(db, media_id)

    revived = revive_due_failures(db, settings)

    assert revived == 1
    assert _row(db, media_id)["processing_status"] == "ANALYZED"  # only the embedding is redone


def test_not_due_failure_stays_failed(db, tmp_path: Path) -> None:
    settings = make_settings(revive_after_seconds=1800.0)
    media_id = _make_media(db, tmp_path)
    _mark_failed(db, media_id, retryable=True, settings=settings)

    assert revive_due_failures(db, settings) == 0
    assert _row(db, media_id)["processing_status"] == "FAILED"


def test_permanent_failure_is_never_revived(db, tmp_path: Path) -> None:
    settings = make_settings()
    media_id = _make_media(db, tmp_path)
    _mark_failed(db, media_id, retryable=False, settings=settings)

    assert revive_due_failures(db, settings) == 0
    assert _row(db, media_id)["processing_status"] == "FAILED"


def test_spent_revive_budget_is_never_revived(db, tmp_path: Path) -> None:
    settings = make_settings(max_item_revives=2)
    media_id = _make_media(db, tmp_path)
    _mark_failed(db, media_id, retryable=True, settings=settings)
    with transaction(db):
        # Simulate two prior revives and a now-due deadline.
        db.execute(
            "UPDATE media SET ai_revives = 2, "
            "ai_next_retry_at = datetime('now', '-1 second') WHERE id = ?",
            (media_id,),
        )

    assert revive_due_failures(db, settings) == 0
    assert _row(db, media_id)["processing_status"] == "FAILED"
