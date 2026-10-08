"""M2.4 API tests — media/scrape/jobs routes over ``create_app`` + TestClient.

Fully offline: rows are seeded through the real ingest path, the scrape test
runs the registered fixture adapter with an empty custom-URL list (zero page
fetches — no network), and the chained pipeline is polled to completion through
the jobs endpoint.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.config import Settings, load_settings
from backend.database.database import transaction
from backend.main import create_app
from backend.media.library import ingest_download
from backend.media.thumbnails import generate_thumbnails
from tests.test_duplicates import _ingest_copy, _ingest_unique
from tests.test_library import CDN, _comment, _result

#: The five job types one scrape request must chain (PRD §57 stage split:
#: crawl → thumbnail → dup_scan → ai_analysis → index_rebuild).
PIPELINE_JOB_TYPES = ("crawl", "thumbnail", "dup_scan", "ai_analysis", "index_rebuild")

ENTRY_URL = "https://fixture.test/comic/chapter-42?page=1"


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    """Settings with database + storage inside the test's tmp dir, fast crawler.

    ``ai.provider=mock`` keeps the chained AI stage offline and deterministic
    regardless of any key in the developer's ``.env.local``.
    """
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "api.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    crawler = replace(base.crawler, delay_seconds=0.0, concurrency=1)
    ai = replace(base.ai, provider="mock")
    return replace(base, storage=storage, crawler=crawler, ai=ai)


@pytest.fixture
def client(api_settings: Settings):
    """TestClient with lifespan applied (migrations, app.state, CORS)."""
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


def _seed(client: TestClient, tmp_path: Path, name: str) -> int:
    """Store one byte-unique PNG through the real ingest path."""
    state = client.app.state
    return _ingest_unique(state.db, state.settings, tmp_path, name)


def _seed_dup(client: TestClient, tmp_path: Path, name: str, of: int) -> int:
    """Store a byte-identical copy → flagged ``dup`` row; returns its id."""
    state = client.app.state
    _ingest_copy(state.db, state.settings, tmp_path, name, of)
    row = state.db.execute("SELECT MAX(id) FROM media").fetchone()
    return int(row[0])


def _seed_gif(client: TestClient, tmp_path: Path, name: str) -> int:
    state = client.app.state
    source = tmp_path / "api-dl" / f"{name}.gif"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), (10, 200, 60)).save(source, format="GIF")
    media_id = ingest_download(
        state.db,
        _result(source, url=f"{CDN}/{name}.gif", content_type="image/gif", kind="gif"),
        _comment(),
        settings=state.settings,
    )
    assert media_id is not None
    return media_id


def _insert_job(db, job_type: str, status: str) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO jobs (job_type, status, progress, message, started_at) "
            "VALUES (?, ?, 1.0, 'done', datetime('now'))",
            (job_type, status),
        )
    return int(cursor.lastrowid)


def _wait_for_pipeline(client: TestClient, timeout: float = 10.0) -> dict[str, dict]:
    """Poll ``GET /api/jobs`` until crawl + thumbnail + dup_scan have all finished."""
    by_type: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = client.get("/api/jobs").json()["items"]
        by_type = {item["job_type"]: item for item in items}
        if all(job_type in by_type for job_type in PIPELINE_JOB_TYPES) and all(
            by_type[job_type]["status"] in ("completed", "failed")
            for job_type in PIPELINE_JOB_TYPES
        ):
            return by_type
        time.sleep(0.05)
    pytest.fail(f"pipeline not finished within {timeout}s: {by_type}")


# ---------------------------------------------------------------------------
# GET /api/media — list, filters, pagination, counts
# ---------------------------------------------------------------------------


def test_media_list_empty_shape_and_random_404(client: TestClient) -> None:
    payload = client.get("/api/media").json()
    assert payload == {
        "items": [],
        "page": 1,
        "page_size": 50,
        "total": 0,
        "dup_counts": {"dup": 0, "nondup": 0, "unflagged": 0},
        "status_counts": {"ready": 0, "processing": 0, "failed": 0},
    }
    assert client.get("/api/media/random").status_code == 404


def test_media_list_pagination_newest_first_and_dup_counts(
    client: TestClient, tmp_path: Path
) -> None:
    _seed(client, tmp_path, "alpha")
    beta = _seed(client, tmp_path, "beta")
    _seed_dup(client, tmp_path, "alpha-copy", of=1)

    page_one = client.get("/api/media", params={"page_size": 2}).json()
    assert [item["id"] for item in page_one["items"]] == [3, 2]
    assert page_one["total"] == 3
    assert page_one["dup_counts"] == {"dup": 1, "nondup": 2, "unflagged": 0}
    assert page_one["status_counts"] == {"ready": 0, "processing": 3, "failed": 0}

    page_two = client.get("/api/media", params={"page": 2, "page_size": 2}).json()
    assert [item["id"] for item in page_two["items"]] == [1]

    dup_only = client.get("/api/media", params={"dup_status": "dup"}).json()
    assert [item["id"] for item in dup_only["items"]] == [3]
    assert dup_only["total"] == 1

    favorite = client.patch(f"/api/media/{beta}", json={"is_favorite": True})
    assert favorite.status_code == 200
    favorites = client.get("/api/media", params={"is_favorite": "true"}).json()
    assert [item["id"] for item in favorites["items"]] == [beta]
    assert client.get("/api/media", params={"is_favorite": "false"}).json()["total"] == 2


def test_media_list_type_site_format_chapter_filters(
    client: TestClient, tmp_path: Path
) -> None:
    _seed(client, tmp_path, "gamma")
    _seed_gif(client, tmp_path, "dancing")

    def ids(params: dict) -> set[int]:
        return {item["id"] for item in client.get("/api/media", params=params).json()["items"]}

    assert ids({"type": "image"}) == {1}
    assert ids({"type": "gif"}) == {2}
    assert ids({"type": "video"}) == set()
    assert ids({"format": ".PNG"}) == {1}  # case/dot insensitive
    assert ids({"site": "fixture.test"}) == {1, 2}
    assert ids({"site": "other.test"}) == set()
    assert ids({"chapter": "chapter-42"}) == {1, 2}
    assert ids({"date_from": "2099-01-01"}) == set()


def test_media_list_rejects_invalid_query_values(client: TestClient) -> None:
    assert client.get("/api/media", params={"type": "audio"}).status_code == 422
    assert client.get("/api/media", params={"dup_status": "maybe"}).status_code == 422
    assert client.get(
        "/api/media", params={"processing_status": "PENDING"}
    ).status_code == 422
    assert client.get("/api/media", params={"page_size": 0}).status_code == 422
    assert client.get("/api/media", params={"page_size": 500}).status_code == 422
    assert client.get("/api/media", params={"date_from": "not-a-date"}).status_code == 422


# ---------------------------------------------------------------------------
# GET/PATCH/DELETE /api/media/{id} — detail, overrides, removal
# ---------------------------------------------------------------------------


def test_media_detail_provenance_without_storage_paths(
    client: TestClient, tmp_path: Path
) -> None:
    _seed(client, tmp_path, "delta")
    detail = client.get("/api/media/1").json()

    assert detail["id"] == 1
    assert detail["site"] == "fixture.test"
    assert detail["dup_status"] == "nondup"
    assert detail["dup_expires_at"] is None
    assert detail["is_favorite"] is False
    assert detail["ai_metadata"] is None
    assert detail["user_tags"] == []
    assert len(detail["sha256"]) == 64
    source = detail["sources"][0]
    assert source["comment_id"] == "918271"
    assert source["media_url"] == f"{CDN}/delta.png"
    assert source["page_url"] == "https://fixture.test/comic/chapter-42?page=3"
    # API hygiene: absolute storage paths never reach the client.
    assert "file_path" not in detail
    assert "thumbnail_path" not in detail
    assert "preview_path" not in detail


def test_media_detail_404_and_422(client: TestClient) -> None:
    assert client.get("/api/media/999").status_code == 404
    assert client.get("/api/media/abc").status_code == 422


def test_patch_applies_manual_overrides_and_ignores_unknown_fields(
    client: TestClient, tmp_path: Path
) -> None:
    _seed(client, tmp_path, "epsilon")

    patched = client.patch(
        "/api/media/1",
        json={
            "title": "Nice cat",
            "user_description": "the classic confused face",
            "user_tags": ["cat", "mood"],
            "is_favorite": True,
        },
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["title"] == "Nice cat"
    assert body["user_description"] == "the classic confused face"
    assert body["user_tags"] == ["cat", "mood"]
    assert body["is_favorite"] is True

    # Persisted across requests (read back from the database).
    reread = client.get("/api/media/1").json()
    assert reread["user_tags"] == ["cat", "mood"]
    assert reread["is_favorite"] is True

    # Unknown fields are dropped by the whitelist — no effect, no 500.
    ignored = client.patch("/api/media/1", json={"bogus": "x", "title": None})
    assert ignored.status_code == 200
    assert ignored.json()["title"] is None

    assert client.patch(
        "/api/media/1", json={"title": "x" * 301}
    ).status_code == 422
    assert client.patch("/api/media/999", json={"title": "nope"}).status_code == 404


def test_delete_media_removes_rows_and_files(client: TestClient, tmp_path: Path) -> None:
    _seed(client, tmp_path, "zeta")
    stored = Path(
        client.app.state.db.execute("SELECT file_path FROM media WHERE id = 1").fetchone()[0]
    )
    assert stored.is_file()

    deleted = client.delete("/api/media/1")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": 1, "deleted": True}
    assert not stored.exists()
    assert client.app.state.db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert client.app.state.db.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 0

    assert client.delete("/api/media/1").status_code == 404
    assert client.delete("/api/media/abc").status_code == 422


# ---------------------------------------------------------------------------
# POST /api/media/{id}/unflag-dup (PRD §12.1 rule 3)
# ---------------------------------------------------------------------------


def test_unflag_dup_endpoint_transitions_and_conflicts(
    client: TestClient, tmp_path: Path
) -> None:
    _seed(client, tmp_path, "eta")
    dup_id = _seed_dup(client, tmp_path, "eta-copy", of=1)

    unflagged = client.post(f"/api/media/{dup_id}/unflag-dup")
    assert unflagged.status_code == 200
    assert unflagged.json()["dup_status"] == "unflagged"

    conflict = client.post(f"/api/media/{dup_id}/unflag-dup")
    assert conflict.status_code == 409, "already unflagged → only dup items qualify"
    assert "not flagged dup" in conflict.json()["detail"]

    assert client.post("/api/media/1/unflag-dup").status_code == 409
    assert client.post("/api/media/999/unflag-dup").status_code == 404


# ---------------------------------------------------------------------------
# GET /api/media/random (PRD §30)
# ---------------------------------------------------------------------------


def test_random_scopes(client: TestClient, tmp_path: Path) -> None:
    _seed(client, tmp_path, "theta")
    _seed_gif(client, tmp_path, "spin")

    gif = client.get("/api/media/random", params={"scope": "gifs"}).json()
    assert gif["id"] == 2 and gif["extension"] == "gif"

    missing = client.get("/api/media/random", params={"scope": "favorites"})
    assert missing.status_code == 404, "no favorite exists yet"
    assert "no media matches scope" in missing.json()["detail"]

    assert client.patch("/api/media/1", json={"is_favorite": True}).status_code == 200
    favorite = client.get("/api/media/random", params={"scope": "favorites"}).json()
    assert favorite["id"] == 1
    assert client.get("/api/media/random", params={"scope": "everything"}).status_code == 200
    assert client.get(
        "/api/media/random", params={"scope": "nonsense"}
    ).status_code == 422


# ---------------------------------------------------------------------------
# File serving: /file, /thumbnail, /preview (PRD §26, §41)
# ---------------------------------------------------------------------------


def test_file_serving_content_types_and_containment(
    client: TestClient, tmp_path: Path
) -> None:
    _seed(client, tmp_path, "iota")
    state = client.app.state
    row = state.db.execute("SELECT * FROM media WHERE id = 1").fetchone()

    original = client.get("/api/media/1/file")
    assert original.status_code == 200
    assert original.headers["content-type"] == "image/png"
    assert original.content == Path(row["file_path"]).read_bytes()

    no_thumb = client.get("/api/media/1/thumbnail")
    assert no_thumb.status_code == 404
    assert "has no thumbnail yet" in no_thumb.json()["detail"]
    assert client.get("/api/media/1/preview").status_code == 404

    thumb, preview = generate_thumbnails(1, row["file_path"], state.settings)
    state.db.execute(
        "UPDATE media SET thumbnail_path = ?, preview_path = ? WHERE id = 1",
        (str(thumb), str(preview)),
    )
    state.db.commit()
    for endpoint in ("thumbnail", "preview"):
        generated = client.get(f"/api/media/1/{endpoint}")
        assert generated.status_code == 200
        assert generated.headers["content-type"] == "image/webp"
        assert generated.content == Path(str(thumb if endpoint == "thumbnail" else preview)).read_bytes()


def test_file_serving_rejects_bad_ids_and_paths_outside_storage(
    client: TestClient, tmp_path: Path
) -> None:
    _seed(client, tmp_path, "kappa")
    assert client.get("/api/media/abc/file").status_code == 422
    assert client.get("/api/media/999/file").status_code == 404

    # A corrupted row pointing outside storage must not leak the file (PRD §41).
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be served", encoding="utf-8")
    state = client.app.state
    state.db.execute("UPDATE media SET file_path = ? WHERE id = 1", (str(outside),))
    state.db.commit()

    refused = client.get("/api/media/1/file")
    assert refused.status_code == 404
    assert outside.exists()


# ---------------------------------------------------------------------------
# POST /api/scrape — validation, unsupported site, chained pipeline
# ---------------------------------------------------------------------------


def test_scrape_rejects_unsupported_site_without_creating_a_job(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/scrape", json={"url": "https://unknown.example/comic/1"}
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Site not supported: add an adapter for unknown.example" in detail
    assert client.get("/api/jobs").json()["items"] == []


def test_scrape_rejects_invalid_payloads(client: TestClient) -> None:
    assert client.post("/api/scrape", json={"url": ""}).status_code == 422
    assert client.post(
        "/api/scrape", json={"url": ENTRY_URL, "scope": "everything"}
    ).status_code == 422
    assert client.post("/api/scrape", json={}).status_code == 422


def test_scrape_chains_crawl_thumbnail_dup_ai_and_index_jobs(
    client: TestClient, fake_adapter
) -> None:
    started = client.post(
        "/api/scrape",
        json={"url": ENTRY_URL, "scope": "custom_urls", "urls": []},
    )
    assert started.status_code == 202
    body = started.json()
    job_id = body["job_id"]
    assert isinstance(job_id, int)
    assert body["status"] in ("running", "completed")

    status = client.get(f"/api/scrape/{job_id}")
    assert status.status_code == 200
    assert status.json()["job_type"] == "crawl"
    assert client.get("/api/scrape/999999").status_code == 404

    by_type = _wait_for_pipeline(client)
    assert by_type["crawl"]["status"] == "completed"
    assert by_type["crawl"]["params"]["scope"] == "custom_urls"
    assert "new=0" in by_type["crawl"]["message"]
    assert by_type["thumbnail"]["status"] == "completed"
    assert by_type["dup_scan"]["status"] == "completed"
    assert by_type["dup_scan"]["message"] == "scanned=0 flagged=0 purged=0"
    assert by_type["ai_analysis"]["status"] == "completed", "mock provider, zero items"
    assert by_type["index_rebuild"]["status"] == "completed"

    items = client.get("/api/jobs").json()["items"]
    assert items[0]["job_type"] == "index_rebuild", "newest first: the last stage ran last"
    assert client.get("/api/media").json()["total"] == 0, "no URLs → nothing collected"
    assert client.app.state.running_crawls == {}, "live handles are released"


# ---------------------------------------------------------------------------
# GET /api/jobs + control endpoints (PRD §35, §5.2)
# ---------------------------------------------------------------------------


def test_jobs_list_filters_and_order(client: TestClient) -> None:
    db = client.app.state.db
    running_id = _insert_job(db, "crawl", "running")
    _insert_job(db, "thumbnail", "completed")

    items = client.get("/api/jobs").json()["items"]
    assert [item["job_type"] for item in items] == ["thumbnail", "crawl"]
    assert items[0]["created_at"]

    only_running = client.get("/api/jobs", params={"status": "running"}).json()["items"]
    assert [item["id"] for item in only_running] == [running_id]

    limited = client.get("/api/jobs", params={"limit": 1}).json()["items"]
    assert len(limited) == 1
    assert client.get("/api/jobs", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422


def test_job_control_is_noop_safe_for_finished_and_unknown_jobs(
    client: TestClient,
) -> None:
    db = client.app.state.db
    crawl_id = _insert_job(db, "crawl", "completed")
    thumb_id = _insert_job(db, "thumbnail", "completed")

    for action in ("pause", "resume", "cancel"):
        response = client.post(f"/api/jobs/{crawl_id}/{action}")
        assert response.status_code == 200
        assert response.json()["applied"] is False, "no live handle → no-op"
        assert response.json()["job"]["status"] == "completed"

    other_type = client.post(f"/api/jobs/{thumb_id}/pause")
    assert other_type.status_code == 200
    assert other_type.json()["applied"] is False

    missing = client.post("/api/jobs/99999/pause")
    assert missing.status_code == 404
    assert "job 99999 not found" in missing.json()["detail"]


# ---------------------------------------------------------------------------
# CORS (PRD §0 — Next.js dev server)
# ---------------------------------------------------------------------------


def test_cors_preflight_allows_only_the_dev_origin(client: TestClient) -> None:
    allowed = client.options(
        "/api/media",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"

    foreign = client.options(
        "/api/media",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert foreign.headers.get("access-control-allow-origin") != "https://evil.example"
