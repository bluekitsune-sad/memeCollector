"""``GET /api/ai/status`` tests — exact live-status contract (PRD §18, §19, §35, §36).

Offline: ``ai.provider=mock`` + a temp database, so the background supervisor
spawned by the lifespan really runs (polling every 50 ms) but never touches the
network or the live ``data/database.sqlite``. Assertions pin the exact response
keys the Jobs card renders, the ``job`` counters after a real queue run, the
``on_hold`` retry countdown, the stopped fallback for a bare app, and the rule
that the provider API key never appears in the payload (PRD §41).
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings, load_settings
from backend.database.database import transaction
from backend.main import create_app

#: Fake secret proving the key itself never reaches the client (PRD §41).
SECRET_KEY = "sk-or-SECRET-MUST-NEVER-LEAK-INTO-STATUS"

#: Exact top-level contract (frontend ``AiStatus`` — lib/types.ts).
RESPONSE_KEYS = {
    "state",
    "reason",
    "provider",
    "model",
    "embedding_model",
    "key_present",
    "retry_in_seconds",
    "next_retry_at",
    "last_error",
    "job",
    "updated_at",
}

#: Exact ``job`` fragment keys.
JOB_KEYS = {"id", "done", "total", "ready", "failed", "deferred"}

#: Every state the supervisor may report.
KNOWN_STATES = {"processing", "on_hold", "idle", "unavailable", "stopped"}


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    """Temp DB + storage, mock provider, no key, 50 ms supervisor polling."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "status.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    ai = replace(base.ai, provider="mock", api_key=None, supervisor_poll_seconds=0.05)
    return replace(base, storage=storage, ai=ai)


@pytest.fixture
def client(api_settings: Settings):
    """TestClient whose lifespan starts the real background supervisor."""
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


def wait_for_status(
    client: TestClient, predicate, *, timeout: float = 5.0
) -> dict:
    """Poll ``GET /api/ai/status`` until ``predicate(payload)`` holds."""
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        response = client.get("/api/ai/status")
        assert response.status_code == 200
        last = response.json()
        if predicate(last):
            return last
        time.sleep(0.05)
    pytest.fail(f"status condition not reached within {timeout}s last={last}")


def seed_media(client: TestClient, sample_images: dict[str, Path]) -> int:
    """Insert a claimable DOWNLOADED row so the supervisor has real work."""
    db = client.app.state.db
    path = sample_images["png"]
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO media (file_path, mime_type, extension, original_filename, "
            "processing_status) VALUES (?, 'image/png', 'png', ?, 'DOWNLOADED')",
            (str(path), path.name),
        )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_status_returns_the_exact_contract(client: TestClient) -> None:
    response = client.get("/api/ai/status")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == RESPONSE_KEYS
    assert payload["state"] in KNOWN_STATES
    assert payload["provider"] == "mock"
    assert payload["model"] and isinstance(payload["model"], str)
    assert payload["embedding_model"] and isinstance(payload["embedding_model"], str)
    assert payload["key_present"] is False  # keyless mock settings
    assert payload["reason"] is None  # only `unavailable` explains itself
    assert payload["job"] is None  # no queue run has happened yet
    assert payload["last_error"] is None
    assert payload["retry_in_seconds"] is None and payload["next_retry_at"] is None
    assert isinstance(payload["updated_at"], str) and payload["updated_at"]
    # JSON-serializable end to end (the API returns it verbatim).
    assert json.loads(json.dumps(payload)) == payload


def test_status_reports_the_latest_job_after_a_real_queue_run(
    client: TestClient, sample_images: dict[str, Path]
) -> None:
    """Seeded leftover → the supervisor runs it → ``job`` carries live counters."""
    seed_media(client, sample_images)

    finished = wait_for_status(
        client,
        lambda payload: payload["job"] is not None
        and payload["job"]["done"] == payload["job"]["total"]
        and payload["job"]["total"] > 0,
    )
    assert set(finished["job"]) == JOB_KEYS
    assert finished["job"]["ready"] == 1
    assert finished["job"]["failed"] == 0
    assert finished["job"]["deferred"] == 0
    assert finished["state"] in KNOWN_STATES  # settled back to idle after the run
    assert finished["last_error"] is None

    media = client.app.state.db.execute("SELECT processing_status FROM media").fetchone()
    assert media is not None and media["processing_status"] == "READY"


# ---------------------------------------------------------------------------
# on_hold: backoff countdown surfaced live
# ---------------------------------------------------------------------------


def test_on_hold_state_surfaces_the_retry_countdown(
    client: TestClient, sample_images: dict[str, Path]
) -> None:
    """A deferred row (future ``ai_next_retry_at``) → live countdown, no job."""
    # Single INSERT with the retry fields already set: the row is never
    # claimable, so the live supervisor (50 ms poll) can't race and run it.
    db = client.app.state.db
    path = sample_images["png"]
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO media (file_path, mime_type, extension, original_filename, "
            "processing_status, ai_attempts, ai_next_retry_at) "
            "VALUES (?, 'image/png', 'png', ?, 'DOWNLOADED', 2, "
            "datetime('now', '+120 seconds'))",
            (str(path), path.name),
        )
    media_id = int(cursor.lastrowid)

    held = wait_for_status(client, lambda payload: payload["state"] == "on_hold")
    assert set(held) == RESPONSE_KEYS
    assert held["retry_in_seconds"] is not None
    assert 0 < held["retry_in_seconds"] <= 120
    assert held["next_retry_at"] is not None
    assert held["reason"] is None  # held, not broken
    assert held["job"] is None  # claimable==0 → the supervisor never ran a queue

    row = db.execute(
        "SELECT processing_status, ai_attempts FROM media WHERE id = ?", (media_id,)
    ).fetchone()
    assert row is not None
    assert row["processing_status"] == "DOWNLOADED"  # parked, not failed
    assert int(row["ai_attempts"]) == 2


# ---------------------------------------------------------------------------
# Secrets (PRD §41)
# ---------------------------------------------------------------------------


def test_status_never_contains_the_api_key(tmp_path: Path) -> None:
    """Keyed config → ``key_present`` true, value absent from every serialization."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "keyed.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    ai = replace(
        base.ai,
        provider="openrouter",
        api_key=SECRET_KEY,
        supervisor_poll_seconds=0.05,
    )
    settings = replace(base, storage=storage, ai=ai)

    with TestClient(create_app(settings)) as keyed_client:
        payload = wait_for_status(
            keyed_client, lambda body: body["state"] in {"idle", "unavailable"}
        )
        raw_body = keyed_client.get("/api/ai/status").text

    assert set(payload) == RESPONSE_KEYS
    assert payload["key_present"] is True
    assert SECRET_KEY not in json.dumps(payload)
    assert SECRET_KEY not in raw_body
    assert payload["provider"] == "openrouter"


# ---------------------------------------------------------------------------
# Bare app: no lifespan supervisor → `stopped`, history intact
# ---------------------------------------------------------------------------


def test_bare_app_without_a_supervisor_reports_stopped(client: TestClient) -> None:
    """The route degrades to ``state=stopped`` instead of failing (500)."""
    db = client.app.state.db
    with transaction(db):
        db.execute(
            "INSERT INTO jobs (job_type, status, progress, message, completed_at) "
            "VALUES ('ai_analysis', 'completed', 1.0, "
            "'done=3/3 ready=2 failed=0 deferred=1 last_error=media_id=7', datetime('now'))"
        )

    state = client.app.state
    del state.ai_supervisor  # a bare app that never spawned the loop

    response = client.get("/api/ai/status")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == RESPONSE_KEYS
    assert payload["state"] == "stopped"
    assert payload["reason"] is None
    assert set(payload["job"]) == JOB_KEYS
    assert payload["job"] == {"id": payload["job"]["id"], "done": 3, "total": 3,
                              "ready": 2, "failed": 0, "deferred": 1}
    assert payload["last_error"] == "media_id=7"  # history still readable
    assert payload["provider"] == "mock"
    assert SECRET_KEY not in json.dumps(payload)


def test_status_is_reachable_on_the_default_route_set(client: TestClient) -> None:
    """The router is mounted under ``/api/ai`` alongside the other modules."""
    assert client.get("/api/ai/status").status_code == 200
    assert client.get("/api/ai/nonexistent").status_code == 404
