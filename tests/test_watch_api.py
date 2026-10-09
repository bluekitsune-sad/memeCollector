"""Watch API tests — CRUD, unsupported-site 400, targeted scan, crawl auto-registration.

Fully offline: the ``fixture.test`` adapter is registered with a listing that
has no chapters, so watch scans and scrapes discover zero pages (no fetches).
The scan endpoint's chain is polled through ``/api/jobs`` exactly like the
scrape pipeline tests do (tests/test_api.py).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings, load_settings
from backend.jobs.watched_comics import ensure_watched_comic
from backend.main import create_app
from tests.test_api import ENTRY_URL, PIPELINE_JOB_TYPES, _wait_for_pipeline
from tests.watch_support import EMPTY_LISTING_HTML, listing_adapter, registered
from tests.fixtures.fake_adapter import FakeSiteAdapter


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    """Settings with database + storage inside the test's tmp dir; mock AI, fast crawler."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "watch_api.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    crawler = replace(base.crawler, delay_seconds=0.0, concurrency=1)
    ai = replace(base.ai, provider="mock")
    return replace(base, storage=storage, crawler=crawler, ai=ai)


@pytest.fixture
def watch_adapter() -> Iterator[FakeSiteAdapter]:
    """``fixture.test`` adapter whose ENTIRE_COMIC discovery yields zero pages (offline)."""
    with registered(listing_adapter(EMPTY_LISTING_HTML)) as adapter:
        yield adapter


@pytest.fixture
def client(api_settings: Settings, watch_adapter: FakeSiteAdapter) -> Iterator[TestClient]:
    """TestClient with lifespan applied; the fixture-site adapter is registered."""
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


def _wait_for_job_count(client: TestClient, job_type: str, count: int, timeout: float = 10.0) -> None:
    """Poll until ``count`` finished jobs of ``job_type`` exist (index_rebuild = last stage)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = client.app.state.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = ? AND status IN ('completed', 'failed')",
            (job_type,),
        ).fetchone()
        if int(row[0]) >= count:
            return
        time.sleep(0.05)
    pytest.fail(f"{count} finished {job_type} jobs not reached within {timeout}s")


def _scrape(client: TestClient, expected_index_jobs: int) -> None:
    """Start an empty-URL scrape (zero fetches) and wait for its full chain to finish."""
    started = client.post(
        "/api/scrape", json={"url": ENTRY_URL, "scope": "custom_urls", "urls": []}
    )
    assert started.status_code == 202
    _wait_for_job_count(client, "index_rebuild", expected_index_jobs)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_watch_create_roundtrip_is_idempotent(client: TestClient) -> None:
    created = client.post("/api/watch", json={"url": ENTRY_URL})
    assert created.status_code == 201, "a new comic is created"
    body = created.json()
    assert set(body) == {
        "id", "url", "site", "title", "enabled", "last_scanned_at", "created_at",
    }
    assert body["url"] == ENTRY_URL
    assert body["site"] == "fixture"
    assert body["enabled"] is True
    assert body["title"] is None
    assert body["last_scanned_at"] is None
    assert body["created_at"]

    again = client.post("/api/watch", json={"url": ENTRY_URL, "title": "ignored"})
    assert again.status_code == 200, "an existing comic is upserted, not duplicated"
    assert again.json()["id"] == body["id"]
    assert again.json()["title"] is None, "an upsert never clobbers existing state"

    items = client.get("/api/watch").json()["items"]
    assert len(items) == 1 and items[0]["url"] == ENTRY_URL


def test_watch_patch_and_delete(client: TestClient) -> None:
    comic_id = client.post("/api/watch", json={"url": ENTRY_URL}).json()["id"]

    disabled = client.patch(f"/api/watch/{comic_id}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert client.get("/api/watch").json()["items"][0]["enabled"] is False
    assert client.patch(f"/api/watch/{comic_id}", json={"enabled": True}).json()["enabled"] is True

    deleted = client.delete(f"/api/watch/{comic_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": comic_id, "deleted": True}
    assert client.get("/api/watch").json()["items"] == []
    assert client.delete(f"/api/watch/{comic_id}").status_code == 404


def test_watch_rejects_bad_payloads_and_unknown_ids(client: TestClient) -> None:
    assert client.post("/api/watch", json={}).status_code == 422
    assert client.post("/api/watch", json={"url": ""}).status_code == 422
    assert client.patch("/api/watch/1", json={}).status_code == 422
    assert client.patch("/api/watch/999", json={"enabled": True}).status_code == 404
    assert client.delete("/api/watch/999").status_code == 404
    assert client.post("/api/watch/999/scan").status_code == 404


def test_watch_rejects_unsupported_site_with_the_standard_message(client: TestClient) -> None:
    response = client.post("/api/watch", json={"url": "https://unknown.example/comic/1"})
    assert response.status_code == 400
    assert "Site not supported: add an adapter for unknown.example" in response.json()["detail"]
    assert client.get("/api/watch").json()["items"] == []
    assert client.get("/api/jobs").json()["items"] == []


def test_watched_comics_url_is_unique(db: sqlite3.Connection) -> None:
    ensure_watched_comic(db, ENTRY_URL, "fixture")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO watched_comics (url, site) VALUES (?, 'fixture')", (ENTRY_URL,)
        )
    db.rollback()


# ---------------------------------------------------------------------------
# Targeted scan: 202 + job id, chains the shared pipeline
# ---------------------------------------------------------------------------


def test_scan_returns_a_job_id_and_chains_the_pipeline(client: TestClient) -> None:
    comic_id = client.post("/api/watch", json={"url": ENTRY_URL}).json()["id"]

    started = client.post(f"/api/watch/{comic_id}/scan")
    assert started.status_code == 202
    payload = started.json()
    job_id = payload["job_id"]
    assert isinstance(job_id, int)
    assert payload["status"] in ("running", "completed")

    by_type = _wait_for_pipeline(client)
    assert by_type["crawl"]["status"] == "completed"
    assert by_type["crawl"]["params"]["url"] == ENTRY_URL
    assert by_type["crawl"]["params"]["scope"] == "entire_comic"
    assert by_type["crawl"]["params"]["force_rescan"] is False
    for job_type in PIPELINE_JOB_TYPES:
        assert by_type[job_type]["status"] == "completed", job_type

    status = client.get(f"/api/scrape/{job_id}")
    assert status.status_code == 200
    assert status.json()["job_type"] == "crawl"
    assert client.get("/api/watch").json()["items"][0]["last_scanned_at"] is not None
    assert client.app.state.running_crawls == {}, "live handles are released"


# ---------------------------------------------------------------------------
# Auto-registration after a user crawl (idempotent upsert)
# ---------------------------------------------------------------------------


def test_scrape_auto_registers_the_comic_and_never_re_enables_it(client: TestClient) -> None:
    _scrape(client, expected_index_jobs=1)
    items = client.get("/api/watch").json()["items"]
    assert len(items) == 1, "a successful crawl auto-registers its comic"
    comic = items[0]
    assert comic["url"] == ENTRY_URL
    assert comic["site"] == "fixture"
    assert comic["enabled"] is True
    assert comic["last_scanned_at"] is not None

    _scrape(client, expected_index_jobs=2)
    items = client.get("/api/watch").json()["items"]
    assert len(items) == 1, "re-crawling upserts the same row (idempotent)"

    # The user disabled it — a later crawl must not re-enable their choice.
    client.patch(f"/api/watch/{comic['id']}", json={"enabled": False})
    _scrape(client, expected_index_jobs=3)
    items = client.get("/api/watch").json()["items"]
    assert len(items) == 1
    assert items[0]["enabled"] is False, "auto-registration never clobbers user state"
