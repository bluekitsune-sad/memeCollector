"""AISupervisor tests — leftover pickup, hold/unavailable states, guard, lifecycle.

Everything offline: the mock provider (or a local stub) is injected by
monkeypatching the module-level ``create_provider`` the supervisor calls; the
supervisor runs on the test's own event loop and is always stopped/awaited via
the ``running`` helper (mirroring the app lifespan shutdown).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

import pytest

from backend.ai import AIResponseError, MockVisionProvider
from backend.ai.provider import AIUnavailableError, VisionProvider
from backend.ai.runner import AIQueueGate
from backend.ai.supervisor import AISupervisor
from backend.config import Settings, load_settings
from backend.database.database import transaction

#: Never leak the key into the status payload (PRD §41).
SECRET_KEY = "sk-or-SECRET-MUST-NEVER-LEAK-INTO-STATUS"

#: All states the status contract may report.
KNOWN_STATES = {"processing", "on_hold", "idle", "unavailable", "stopped"}


def make_settings(**ai_overrides: Any) -> Settings:
    """Offline AI settings with fast supervisor polling; optional fake secret key."""
    settings = load_settings()
    values: dict[str, Any] = {
        "provider": "mock",
        "api_key": None,
        "retry_backoff_seconds": 0.0,
        "ai_concurrency": 1,
        "supervisor_poll_seconds": 0.05,
        "retry_interval_seconds": 60.0,
        **ai_overrides,
    }
    return replace(settings, ai=replace(settings.ai, **values))


def make_gate() -> AIQueueGate:
    """A fresh gate over a bare state object (same flag shape as ``app.state``)."""
    return AIQueueGate(SimpleNamespace(ai_queue_running=False))


def insert_media(db: sqlite3.Connection, path: Path) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO media (file_path, mime_type, extension, original_filename, "
            "processing_status) VALUES (?, 'image/png', 'png', ?, 'DOWNLOADED')",
            (str(path), path.name),
        )
    return int(cursor.lastrowid)


def media_status(db: sqlite3.Connection, media_id: int) -> str:
    row = db.execute("SELECT processing_status FROM media WHERE id = ?", (media_id,)).fetchone()
    assert row is not None
    return str(row["processing_status"])


async def await_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.01
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    pytest.fail("condition not reached within timeout")


@asynccontextmanager
async def running(supervisor: AISupervisor) -> AsyncIterator[None]:
    """Start ``supervisor.run()`` and always stop + await it (lifespan shutdown)."""
    task = asyncio.create_task(supervisor.run())
    try:
        yield
    finally:
        await supervisor.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class TransientProvider(MockVisionProvider):
    """Always fails vision with a retryable error → the item is deferred, never READY."""

    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        raise AIResponseError("OpenRouter chat HTTP 429", retryable=True)


class RecordingProvider(MockVisionProvider):
    """Mock that records ``aclose`` so lifecycle tests can observe it."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def provider_returning(provider: VisionProvider) -> Callable[[Settings], VisionProvider]:
    return lambda settings: provider


def provider_raising(message: str) -> Callable[[Settings], VisionProvider]:
    def _raise(settings: Settings) -> VisionProvider:
        raise AIUnavailableError(message)

    return _raise


# ---------------------------------------------------------------------------
# First loop: startup recovery
# ---------------------------------------------------------------------------


async def test_first_loop_processes_leftover_downloaded_items(
    db: sqlite3.Connection, sample_images: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backend.ai.supervisor")
    media_id = insert_media(db, sample_images["png"])  # leftover from an "interrupted" run
    supervisor = AISupervisor(db, make_settings(), make_gate())

    async with running(supervisor):
        await await_until(lambda: media_status(db, media_id) == "READY")
        await await_until(lambda: supervisor.status["state"] == "idle")

    job = db.execute(
        "SELECT * FROM jobs WHERE job_type = 'ai_analysis' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert job is not None and job["status"] == "completed"
    assert supervisor.status["state"] == "stopped"
    assert "ai supervisor state=" in caplog.text


async def test_background_run_reindexes_fts_for_fresh_search(
    db: sqlite3.Connection, sample_images: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """READY rows from a background run must be keyword-searchable immediately
    (the pipeline path reindexes via its INDEX stage; the supervisor must too)."""
    caplog.set_level(logging.INFO, logger="backend.ai.supervisor")
    media_id = insert_media(db, sample_images["png"])
    supervisor = AISupervisor(db, make_settings(), make_gate())

    async with running(supervisor):
        await await_until(lambda: media_status(db, media_id) == "READY")
        await await_until(
            lambda: db.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0] == 1
        )

    # The mock description ("Mock image …") is in the FTS index right after the
    # run — not only after some later search self-heal or manual reindex.
    hits = db.execute(
        "SELECT COUNT(*) FROM media_fts WHERE media_fts MATCH 'mock'"
    ).fetchone()
    assert int(hits[0]) == 1
    assert "reindexed fts" in caplog.text


# ---------------------------------------------------------------------------
# on_hold: transient failure → countdown status
# ---------------------------------------------------------------------------


async def test_transient_failure_puts_supervisor_on_hold_with_countdown(
    db: sqlite3.Connection, sample_images: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.ai.supervisor.create_provider", provider_returning(TransientProvider())
    )
    media_id = insert_media(db, sample_images["png"])
    settings = make_settings(api_key=SECRET_KEY, provider="openrouter")
    supervisor = AISupervisor(db, settings, make_gate())

    async with running(supervisor):
        await await_until(lambda: supervisor.status["state"] == "on_hold")
        status = supervisor.status

    assert set(status) == {
        "state", "reason", "provider", "model", "embedding_model", "key_present",
        "retry_in_seconds", "next_retry_at", "last_error", "job", "updated_at",
    }
    assert status["reason"] is None  # only `unavailable` carries a reason
    assert status["state"] in KNOWN_STATES
    assert status["retry_in_seconds"] is not None
    assert 0 < status["retry_in_seconds"] <= 62  # backoff base is 60s
    assert status["next_retry_at"] is not None
    assert status["last_error"] is not None and f"media_id={media_id}" in status["last_error"]
    assert status["job"] is not None
    assert status["job"]["deferred"] == 1 and status["job"]["failed"] == 0
    assert status["key_present"] is True  # settings carry the (fake) key …
    assert SECRET_KEY not in json.dumps(status)  # … but never the value itself

    # The parked item waited, not failed.
    assert media_status(db, media_id) == "DOWNLOADED"
    row = db.execute("SELECT ai_attempts FROM media WHERE id = ?", (media_id,)).fetchone()
    assert row is not None and int(row["ai_attempts"]) == 1


async def test_on_hold_recovers_when_backoff_passes(
    db: sqlite3.Connection, sample_images: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = TransientProvider()
    monkeypatch.setattr("backend.ai.supervisor.create_provider", provider_returning(flaky))
    media_id = insert_media(db, sample_images["png"])
    supervisor = AISupervisor(db, make_settings(), make_gate())

    async with running(supervisor):
        await await_until(lambda: supervisor.status["state"] == "on_hold")
        # Health restored + clock advanced → the next tick claims and finishes it.
        monkeypatch.setattr(
            "backend.ai.supervisor.create_provider", provider_returning(MockVisionProvider())
        )
        with transaction(db):
            db.execute(
                "UPDATE media SET ai_next_retry_at = datetime('now', '-1 second') WHERE id = ?",
                (media_id,),
            )
        supervisor.nudge()
        await await_until(lambda: media_status(db, media_id) == "READY")


# ---------------------------------------------------------------------------
# unavailable
# ---------------------------------------------------------------------------


async def test_unavailable_when_provider_cannot_be_created(
    db: sqlite3.Connection, sample_images: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.ai.supervisor.create_provider",
        provider_raising("AI provider 'openrouter' requires an API key"),
    )
    insert_media(db, sample_images["png"])
    supervisor = AISupervisor(db, make_settings(provider="openrouter", api_key=None), make_gate())

    async with running(supervisor):
        await await_until(lambda: supervisor.status["state"] == "unavailable")
        status = supervisor.status

    assert status["reason"] is not None and "API key" in status["reason"]
    assert status["job"] is None  # no run ever started
    assert status["retry_in_seconds"] is None and status["next_retry_at"] is None
    # No queue run → no jobs row was created by the failed attempts.
    counted = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_type = 'ai_analysis'"
    ).fetchone()
    assert int(counted[0]) == 0


# ---------------------------------------------------------------------------
# Guard: never double-run with the scrape pipeline's AI stage
# ---------------------------------------------------------------------------


async def test_never_double_runs_while_the_shared_flag_is_held(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    gate = make_gate()
    assert gate.try_acquire()  # the scrape pipeline's AI stage owns the slot
    media_id = insert_media(db, sample_images["png"])
    supervisor = AISupervisor(db, make_settings(), gate)

    async with running(supervisor):
        await await_until(lambda: supervisor.status["state"] == "processing")
        await asyncio.sleep(0.15)  # several poll cycles with the flag held
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='ai_analysis'").fetchone()[0] == 0
        assert media_status(db, media_id) == "DOWNLOADED"
        assert gate.running  # the other owner still holds it

        gate.release()
        supervisor.nudge()
        await await_until(lambda: media_status(db, media_id) == "READY")

    assert not gate.running  # supervisor released after its own run


# ---------------------------------------------------------------------------
# nudge / wake channel / stop
# ---------------------------------------------------------------------------


async def test_nudge_wakes_an_idle_supervisor_immediately(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    # Idle poll is effectively forever — only a nudge can pick the work up.
    supervisor = AISupervisor(db, make_settings(supervisor_poll_seconds=3600.0), make_gate())

    async with running(supervisor):
        await await_until(lambda: supervisor.status["state"] == "idle")
        media_id = insert_media(db, sample_images["png"])
        supervisor.nudge()
        await await_until(lambda: media_status(db, media_id) == "READY", timeout=3.0)


async def test_wake_event_channel_wakes_the_supervisor(
    db: sqlite3.Connection, sample_images: dict[str, Path]
) -> None:
    """``app.state.ai_nudge.set()`` (the exposed wake event) must wake the loop."""
    supervisor = AISupervisor(db, make_settings(supervisor_poll_seconds=3600.0), make_gate())

    async with running(supervisor):
        await await_until(lambda: supervisor.status["state"] == "idle")
        media_id = insert_media(db, sample_images["png"])
        supervisor.wake_event.set()
        await await_until(lambda: media_status(db, media_id) == "READY", timeout=3.0)


async def test_stop_terminates_the_task_cleanly(db: sqlite3.Connection) -> None:
    supervisor = AISupervisor(db, make_settings(supervisor_poll_seconds=3600.0), make_gate())
    task = asyncio.create_task(supervisor.run())
    await await_until(lambda: supervisor.status["state"] == "idle")

    await supervisor.stop()
    await asyncio.wait_for(task, timeout=3.0)

    assert task.done() and task.exception() is None
    assert supervisor.status["state"] == "stopped"


async def test_stop_during_an_active_run_terminates_cleanly(
    db: sqlite3.Connection, sample_images: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowProvider(MockVisionProvider):
        async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
            await asyncio.sleep(30)
            return await super().analyze_image(image_bytes, mime_type=mime_type)

    monkeypatch.setattr("backend.ai.supervisor.create_provider", provider_returning(SlowProvider()))
    insert_media(db, sample_images["png"])
    gate = make_gate()
    supervisor = AISupervisor(db, make_settings(), gate)
    task = asyncio.create_task(supervisor.run())
    try:
        await await_until(lambda: supervisor.status["state"] == "processing")
        await supervisor.stop()  # signals + cancels the cooperative controller …
        task.cancel()            # … the lifespan then cancels the task itself
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert task.done()
    assert supervisor.status["state"] == "stopped"
    assert not gate.running  # the shared flag was released on the way out


# ---------------------------------------------------------------------------
# Provider lifecycle
# ---------------------------------------------------------------------------


async def test_provider_is_aclosed_after_every_run(
    db: sqlite3.Connection, sample_images: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = RecordingProvider()
    monkeypatch.setattr("backend.ai.supervisor.create_provider", provider_returning(provider))
    media_id = insert_media(db, sample_images["png"])
    supervisor = AISupervisor(db, make_settings(), make_gate())

    async with running(supervisor):
        await await_until(lambda: media_status(db, media_id) == "READY")
        await await_until(lambda: supervisor.status["state"] == "idle")

    assert provider.closed is True


async def test_key_is_never_in_the_status_payload(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = AISupervisor(
        db, make_settings(api_key=SECRET_KEY, provider="openrouter"), make_gate()
    )
    assert SECRET_KEY not in json.dumps(supervisor.status)
    assert supervisor.status["key_present"] is True
