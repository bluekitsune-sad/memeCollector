"""Site-backfill tests — discovery, sequential crawls, isolation, cancel, resume (PRD §36).

Everything offline: the ``fixture.test`` adapter advertises the catalog via
``discover_series`` and discovers chapter pages from local listing HTML through
a :class:`FixtureFetcher`; downloads go through an ``httpx.MockTransport``; the
app is the duck-typed state stand-in (the pipeline only touches ``app.state``).
The run is driven with ``asyncio.run(job.run())`` — the same seam production
uses via :func:`~backend.jobs.backfill.launch_backfill`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.config import Settings, load_settings
from backend.database.database import initialize_database, transaction
from backend.jobs.backfill import (
    BackfillJob,
    launch_backfill,
    live_backfill,
    maybe_start_next_target,
    next_rotation_target,
    resume_interrupted_backfill,
)
from backend.main import create_app
from backend.scraper.adapters import SeriesRef
from tests.fixtures.fake_adapter import FakeSiteAdapter, FixtureFetcher, wait_until
from tests.test_crawl_job import media_handler
from tests.watch_support import chapter_listing, chapter_page_urls, registered, state_app

INDEX_URL = "https://fixture.test/comics"

#: Three catalog entries; each comic has one chapter with one attachment page.
COMIC_SLUGS = ("one", "two", "three")
COMIC_URLS = tuple(f"https://fixture.test/comic/{slug}" for slug in COMIC_SLUGS)

#: One chapter, one page per comic.
LISTING_HTML = chapter_listing(
    *((f"/comic/{slug}/chapter-1", 1) for slug in COMIC_SLUGS)
)


def _page_html(slug: str) -> str:
    """A chapter page with exactly one comment attachment (PRD §6)."""
    return (
        '<html><body>'
        f'<article class="comment" data-comment-id="{slug}-1">'
        f'<img class="attachment" src="https://cdn.example.com/media/{slug}.png"/>'
        "</article></body></html>"
    )


def _pages() -> dict[str, str]:
    pages: dict[str, str] = {}
    for slug, comic_url in zip(COMIC_SLUGS, COMIC_URLS):
        for url in chapter_page_urls(comic_url, f"/comic/{slug}/chapter-1", 1):
            pages[url] = _page_html(slug)
    return pages


def _series() -> list[SeriesRef]:
    return [
        SeriesRef(url=url, title=f"Comic {slug.title()}")
        for slug, url in zip(COMIC_SLUGS, COMIC_URLS)
    ]


def backfill_settings(tmp_path: Path, **backfill_overrides: Any) -> Settings:
    """Offline settings: tmp storage, zero delay, mock AI, watcher off."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "backfill.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    crawler = replace(base.crawler, delay_seconds=0.0, concurrency=1)
    ai = replace(base.ai, provider="mock")
    watch = replace(base.watch, enabled=False)
    backfill_overrides.setdefault("targets", [])  # offline by default; rotation tests opt in
    backfill = replace(base.backfill, delay_seconds=0.0, **backfill_overrides)
    return replace(
        base, storage=storage, crawler=crawler, ai=ai, watch=watch, backfill=backfill
    )


def _adapter() -> FakeSiteAdapter:
    return FakeSiteAdapter(
        listing_html=LISTING_HTML, series=_series(), own_chapters_only=True
    )


#: Populated by the autouse ``_sample_images`` fixture (conftest's PIL samples).
SAMPLE_IMAGES: dict[str, Path] = {}


def _make_job(app, **kwargs) -> tuple[BackfillJob, FixtureFetcher, httpx.AsyncClient]:
    fetcher = FixtureFetcher(_pages())
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(media_handler(SAMPLE_IMAGES))
    )
    return BackfillJob(app, INDEX_URL, fetcher=fetcher, http_client=client, **kwargs), fetcher, client


async def _run(job: BackfillJob, client: httpx.AsyncClient):
    try:
        return await job.run()
    finally:
        await client.aclose()


def _items(db, backfill_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT url, title, status, error FROM backfill_items "
        "WHERE backfill_id = ? ORDER BY id",
        (backfill_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _row(db, backfill_id: int):
    return db.execute(
        "SELECT * FROM backfill_jobs WHERE id = ?", (backfill_id,)
    ).fetchone()


@pytest.fixture(autouse=True)
def _sample_images(sample_images: dict[str, Path]) -> None:
    """Expose the conftest sample images to ``_make_job`` (module-level seam)."""
    SAMPLE_IMAGES.update(sample_images)


# ---------------------------------------------------------------------------
# Happy path: discovery + one sequential crawl per comic
# ---------------------------------------------------------------------------


def test_backfill_discovers_and_crawls_every_comic(db, tmp_path: Path) -> None:
    app = state_app(db, backfill_settings(tmp_path))
    with registered(_adapter()):
        job, _fetcher, client = _make_job(app)
        summary = asyncio.run(_run(job, client))

    assert (summary.total, summary.done, summary.failed) == (3, 3, 0)
    assert summary.cancelled is False
    row = _row(db, job.backfill_id)
    assert row["status"] == "completed"
    assert (row["total"], row["done"], row["failed"]) == (3, 3, 0)
    assert row["current_url"] is None and row["completed_at"] is not None
    items = _items(db, job.backfill_id)
    assert [item["status"] for item in items] == ["done", "done", "done"]
    assert [item["title"] for item in items] == [
        "Comic One", "Comic Two", "Comic Three"
    ]
    # Every crawl row is tagged as backfill-owned (the generic resume skips it).
    crawls = db.execute(
        "SELECT params FROM jobs WHERE job_type = 'crawl' ORDER BY id"
    ).fetchall()
    assert len(crawls) == 3
    for crawl in crawls:
        params = json.loads(crawl["params"])
        assert params["owner"] == "backfill"
    # The shared pipeline ran: one attachment per comic was stored (PRD §6).
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 3


def test_backfill_is_idempotent_when_resumed_with_items(db, tmp_path: Path) -> None:
    """A resumed row never re-discovers or duplicates its item list."""
    app = state_app(db, backfill_settings(tmp_path))
    with registered(_adapter()):
        job, _fetcher, client = _make_job(app)
        asyncio.run(_run(job, client))
        again, _f2, client2 = _make_job(app, backfill_id=job.backfill_id)
        summary = asyncio.run(_run(again, client2))

    assert summary.total == 3
    assert len(_items(db, job.backfill_id)) == 3


# ---------------------------------------------------------------------------
# Failure isolation + cooperative cancel/pause
# ---------------------------------------------------------------------------


def test_comic_failure_is_recorded_and_the_run_continues(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = state_app(db, backfill_settings(tmp_path))
    import backend.jobs.backfill as backfill_module

    real_start = backfill_module.start_crawl

    async def failing_start(app_arg, job):
        if "/comic/two" in job.url:
            raise RuntimeError("simulated start failure")
        return await real_start(app_arg, job)

    monkeypatch.setattr(backfill_module, "start_crawl", failing_start)
    with registered(_adapter()):
        job, _fetcher, client = _make_job(app)
        summary = asyncio.run(_run(job, client))

    assert (summary.done, summary.failed) == (2, 1)
    items = _items(db, job.backfill_id)
    assert [item["status"] for item in items] == ["done", "failed", "done"]
    assert "simulated start failure" in items[1]["error"]
    assert _row(db, job.backfill_id)["status"] == "completed"


def test_cancel_requeues_the_comic_in_flight(db, tmp_path: Path) -> None:
    app = state_app(db, backfill_settings(tmp_path))
    with registered(_adapter()):
        job, fetcher, client = _make_job(app)

        original_on_fetch = fetcher.on_fetch

        def cancel_on_comic_two(url: str) -> None:
            if "/comic/two" in url:
                job.cancel()
            if original_on_fetch is not None:
                original_on_fetch(url)

        fetcher.on_fetch = cancel_on_comic_two
        summary = asyncio.run(_run(job, client))

    assert summary.cancelled is True
    row = _row(db, job.backfill_id)
    assert row["status"] == "cancelled"
    items = _items(db, job.backfill_id)
    # Comic one finished; the cancelled comic returns to pending (a later run
    # picks it up again); comic three was never started.
    assert [item["status"] for item in items] == ["done", "pending", "pending"]


def test_pause_holds_the_run_until_resumed(db, tmp_path: Path) -> None:
    app = state_app(db, backfill_settings(tmp_path))

    async def scenario() -> tuple[int, str]:
        with registered(_adapter()):
            job, _fetcher, client = _make_job(app)
            task = asyncio.create_task(job.run())
            await wait_until(
                lambda: db.execute(
                    "SELECT COUNT(*) FROM backfill_items WHERE backfill_id = ? "
                    "AND status = 'done'",
                    (job.backfill_id,),
                ).fetchone()[0]
                >= 1
            )
            job.pause()
            await asyncio.sleep(0.2)
            running = _row(db, job.backfill_id)["status"]
            # Counts sync after every comic, not just at run end — the row's
            # progress must reflect the finished comic while the run is live.
            assert _row(db, job.backfill_id)["done"] >= 1
            remaining = db.execute(
                "SELECT COUNT(*) FROM backfill_items WHERE backfill_id = ? "
                "AND status IN ('pending', 'running')",
                (job.backfill_id,),
            ).fetchone()[0]
            job.resume()
            try:
                await task
            finally:
                await client.aclose()
            return remaining, running

    remaining, running = asyncio.run(scenario())
    # While paused at least one comic was still un-finished and the row was live.
    assert remaining >= 1
    assert running == "running"
    assert db.execute(
        "SELECT COUNT(*) FROM backfill_items WHERE status = 'done'"
    ).fetchone()[0] == 3


# ---------------------------------------------------------------------------
# Restart survival: a stranded row resumes exactly where it left off
# ---------------------------------------------------------------------------


def test_interrupted_backfill_requeues_and_completes(db, tmp_path: Path) -> None:
    """A row left 'running' (crash) with a stuck 'running' item resumes cleanly."""
    app = state_app(db, backfill_settings(tmp_path))
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO backfill_jobs (site_url, adapter_site, status, total, done) "
            "VALUES (?, 'fixture', 'running', 3, 1)",
            (INDEX_URL,),
        )
        backfill_id = int(cursor.lastrowid)
        for index, (url, title, status) in enumerate(
            zip(COMIC_URLS, ("Comic One", "Comic Two", "Comic Three"),
                ("done", "running", "pending"))
        ):
            db.execute(
                "INSERT INTO backfill_items (backfill_id, url, title, status) "
                "VALUES (?, ?, ?, ?)",
                (backfill_id, url, title, status),
            )

    with registered(_adapter()):
        job, _fetcher, client = _make_job(app, backfill_id=backfill_id)
        summary = asyncio.run(_run(job, client))

    assert (summary.total, summary.done, summary.failed) == (3, 3, 0)
    assert _row(db, backfill_id)["status"] == "completed"


def test_resume_interrupted_backfill_launches_a_live_run(db, tmp_path: Path) -> None:
    settings = backfill_settings(tmp_path)
    app = state_app(db, settings)
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO backfill_jobs (site_url, adapter_site, status) "
            "VALUES (?, 'fixture', 'running')",
            (INDEX_URL,),
        )
        backfill_id = int(cursor.lastrowid)

    async def scenario() -> tuple[bool, BackfillJob | None, bool]:
        with registered(_adapter()):
            launched = resume_interrupted_backfill(app)
            live = live_backfill(app)
            again = resume_interrupted_backfill(app)  # only one live run ever
            if live is not None:
                live.stop()  # keep the launched task from crawling anything
                task = getattr(app.state, "backfill_task", None)
                if task is not None:
                    await task
            return launched, live, again

    launched, live, again = asyncio.run(scenario())
    assert launched is True
    assert live is not None and live.backfill_id == backfill_id
    assert again is False
    # The row stays 'running' across the stop — that is what the next boot resumes.
    assert _row(db, backfill_id)["status"] == "running"


def test_resume_interrupted_backfill_respects_enabled_flag(db, tmp_path: Path) -> None:
    app = state_app(db, backfill_settings(tmp_path, enabled=False))
    with transaction(db):
        db.execute(
            "INSERT INTO backfill_jobs (site_url, adapter_site, status) "
            "VALUES (?, 'fixture', 'running')",
            (INDEX_URL,),
        )

    assert resume_interrupted_backfill(app) is False
    assert live_backfill(app) is None


# ---------------------------------------------------------------------------
# API surface (TestClient): start/list/detail/items/controls
# ---------------------------------------------------------------------------


def _fake_launch(app, job: BackfillJob):
    """Deterministic stand-in for launch_backfill: creates the row, never runs."""
    if job.backfill_id is None:
        from backend.scraper.adapters import get_adapter

        adapter = get_adapter(job._index_url)
        job._backfill_id = job._create_row(app.state.db, adapter.site)
    app.state.backfill_job = job


def _api_settings(tmp_path: Path, **backfill_overrides: Any) -> Settings:
    settings = backfill_settings(tmp_path, **backfill_overrides)
    return replace(settings, watch=replace(settings.watch, enabled=False))


def _seed_row(db, status: str = "running", **columns: Any) -> int:
    keys = {"site_url": INDEX_URL, "adapter_site": "fixture", "status": status, **columns}
    names = ", ".join(keys)
    placeholders = ", ".join("?" for _ in keys)
    with transaction(db):
        cursor = db.execute(
            f"INSERT INTO backfill_jobs ({names}) VALUES ({placeholders})",
            tuple(keys.values()),
        )
    return int(cursor.lastrowid)


def _seed_item(db, backfill_id: int, url: str, status: str, title: str | None = None) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO backfill_items (backfill_id, url, title, status) "
            "VALUES (?, ?, ?, ?)",
            (backfill_id, url, title, status),
        )
    return int(cursor.lastrowid)


def test_start_validates_site_and_enabled_flag(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.api.routes_backfill.launch_backfill", _fake_launch)
    settings = _api_settings(tmp_path)
    conn = initialize_database(settings.storage.database_path)
    try:
        with registered(_adapter()):
            with TestClient(create_app(settings)) as client:
                unsupported = client.post(
                    "/api/backfill/start", json={"url": "https://unknown.example/comics"}
                )
                assert unsupported.status_code == 400
                assert "Site not supported" in unsupported.json()["detail"]

        disabled = _api_settings(tmp_path, enabled=False)
        conn2 = initialize_database(disabled.storage.database_path)
        try:
            with TestClient(create_app(disabled)) as client:
                blocked = client.post("/api/backfill/start", json={"url": INDEX_URL})
                assert blocked.status_code == 400
                assert "disabled" in blocked.json()["detail"]
        finally:
            conn2.close()
    finally:
        conn.close()


def test_start_launches_and_second_start_conflicts(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.api.routes_backfill.launch_backfill", _fake_launch)
    settings = _api_settings(tmp_path)
    conn = initialize_database(settings.storage.database_path)
    try:
        with registered(_adapter()):
            with TestClient(create_app(settings)) as client:
                started = client.post("/api/backfill/start", json={"url": INDEX_URL})
                assert started.status_code == 202
                body = started.json()
                assert body["site_url"] == INDEX_URL
                assert body["status"] == "running"
                assert body["live"] is True

                detail = client.get(f"/api/backfill/{body['id']}").json()
                assert detail["counts"] == {"pending": 0, "running": 0, "done": 0, "failed": 0}

                second = client.post("/api/backfill/start", json={"url": INDEX_URL})
                assert second.status_code == 409
                assert "already running" in second.json()["detail"]
    finally:
        conn.close()


def test_start_conflicts_on_a_stranded_running_row(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.api.routes_backfill.launch_backfill", _fake_launch)
    settings = _api_settings(tmp_path)
    with registered(_adapter()):
        with TestClient(create_app(settings)) as client:
            # Seeded after startup: startup auto-resume would otherwise relaunch it.
            stranded = _seed_row(client.app.state.db, "running")
            blocked = client.post("/api/backfill/start", json={"url": INDEX_URL})
            assert blocked.status_code == 409
            assert f"backfill {stranded}" in blocked.json()["detail"]

            # …and the stale-cancel guarantee: it can always be cleared.
            cancelled = client.post(f"/api/backfill/{stranded}/cancel").json()
            assert cancelled["applied"] is True
            assert cancelled["backfill"]["status"] == "cancelled"


def test_list_detail_and_items_endpoints(db, tmp_path: Path) -> None:
    settings = _api_settings(tmp_path)
    conn = initialize_database(settings.storage.database_path)
    try:
        backfill_id = _seed_row(conn, "completed", total=2, done=1, failed=1)
        _seed_item(conn, backfill_id, COMIC_URLS[0], "done", "Comic One")
        _seed_item(conn, backfill_id, COMIC_URLS[1], "failed", "Comic Two")
        with TestClient(create_app(settings)) as client:
            listing = client.get("/api/backfill").json()["items"]
            assert [item["id"] for item in listing] == [backfill_id]

            detail = client.get(f"/api/backfill/{backfill_id}").json()
            assert detail["counts"] == {"pending": 0, "running": 0, "done": 1, "failed": 1}

            items = client.get(f"/api/backfill/{backfill_id}/items").json()
            assert items["total"] == 2
            assert [item["title"] for item in items["items"]] == ["Comic One", "Comic Two"]

            only_failed = client.get(
                f"/api/backfill/{backfill_id}/items", params={"status": "failed"}
            ).json()
            assert only_failed["total"] == 1
            assert only_failed["items"][0]["url"] == COMIC_URLS[1]

            missing = client.get("/api/backfill/999")
            assert missing.status_code == 404
    finally:
        conn.close()


def test_retry_failed_requeues_and_relaunches(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.api.routes_backfill.launch_backfill", _fake_launch)
    settings = _api_settings(tmp_path)
    conn = initialize_database(settings.storage.database_path)
    try:
        backfill_id = _seed_row(conn, "completed", total=2, done=1, failed=1)
        _seed_item(conn, backfill_id, COMIC_URLS[0], "done", "Comic One")
        _seed_item(conn, backfill_id, COMIC_URLS[1], "failed", "Comic Two")
        with TestClient(create_app(settings)) as client:
            retried = client.post(f"/api/backfill/{backfill_id}/retry-failed").json()

            assert retried["applied"] is True
            assert retried["backfill"]["status"] == "running"
            assert retried["backfill"]["live"] is True
            statuses = {
                item["id"]: item["status"]
                for item in client.get(f"/api/backfill/{backfill_id}/items").json()["items"]
            }
            assert sorted(statuses.values()) == ["done", "pending"]

            # Nothing failed anymore → the button becomes a no-op.
            again = client.post(f"/api/backfill/{backfill_id}/retry-failed").json()
            assert again["applied"] is False
    finally:
        conn.close()


def test_cancel_and_resume_controls(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.api.routes_backfill.launch_backfill", _fake_launch)
    settings = _api_settings(tmp_path)
    conn = initialize_database(settings.storage.database_path)
    try:
        with registered(_adapter()):
            with TestClient(create_app(settings)) as client:
                backfill_id = client.post(
                    "/api/backfill/start", json={"url": INDEX_URL}
                ).json()["id"]

                cancelled = client.post(f"/api/backfill/{backfill_id}/cancel").json()
                assert cancelled["applied"] is True  # live handle cancelled

                # A finished run: controls are no-ops, nothing raises.
                resumed = client.post(f"/api/backfill/{backfill_id}/resume").json()
                assert resumed["applied"] in (True, False)
    finally:
        conn.close()


def test_resume_relaunches_a_stranded_row(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.api.routes_backfill.launch_backfill", _fake_launch)
    settings = _api_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        # Seeded after startup: startup auto-resume would otherwise relaunch it.
        stranded = _seed_row(client.app.state.db, "running")
        resumed = client.post(f"/api/backfill/{stranded}/resume").json()
        assert resumed["applied"] is True
        assert resumed["backfill"]["live"] is True


# ---------------------------------------------------------------------------
# Passive rotation: config targets cycle one after another
# ---------------------------------------------------------------------------

#: A second fixture.test catalog — the fake adapter handles any such host.
OTHER_INDEX = "https://fixture.test/comics-b"


def _fixture_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(media_handler(SAMPLE_IMAGES))
    )


def _fixture_job(app, index_url: str, **kwargs) -> BackfillJob:
    return BackfillJob(
        app, index_url, fetcher=FixtureFetcher(_pages()),
        http_client=_fixture_client(), **kwargs,
    )


def test_next_rotation_target_round_robins(db, tmp_path: Path) -> None:
    settings = backfill_settings(tmp_path, targets=[INDEX_URL, OTHER_INDEX])
    # Nothing ran yet → the first configured target goes first.
    assert next_rotation_target(db, settings) == INDEX_URL

    with transaction(db):
        for url in (INDEX_URL, OTHER_INDEX):
            db.execute(
                "INSERT INTO backfill_jobs (site_url, adapter_site, status) "
                "VALUES (?, 'fixture', 'completed')",
                (url,),
            )
    # Both have run → the older row (INDEX_URL) is least recent again.
    assert next_rotation_target(db, settings) == INDEX_URL

    with transaction(db):
        db.execute(
            "INSERT INTO backfill_jobs (site_url, adapter_site, status) "
            "VALUES (?, 'fixture', 'completed')",
            (INDEX_URL,),
        )
    # Now OTHER_INDEX is the least-recently-run target.
    assert next_rotation_target(db, settings) == OTHER_INDEX

    # No targets configured → rotation is off.
    assert next_rotation_target(db, backfill_settings(tmp_path)) is None


def test_maybe_start_next_target_gates(db, tmp_path: Path) -> None:
    """Idle-without-targets / disabled / stranded-row never launch anything."""
    assert maybe_start_next_target(state_app(db, backfill_settings(tmp_path))) is False

    disabled = state_app(db, backfill_settings(tmp_path, enabled=False, targets=[INDEX_URL]))
    assert maybe_start_next_target(disabled) is False

    app = state_app(db, backfill_settings(tmp_path, targets=[INDEX_URL]))
    _seed_row(db, "running")  # a stranded row belongs to the resume path
    assert maybe_start_next_target(app) is False
    assert live_backfill(app) is None


def test_maybe_start_next_target_launches_the_least_recent_target(
    db, tmp_path: Path
) -> None:
    settings = backfill_settings(tmp_path, targets=[INDEX_URL, OTHER_INDEX])
    app = state_app(db, settings)

    async def scenario() -> tuple[int, int]:
        with registered(_adapter()):
            assert maybe_start_next_target(app) is True
            live = live_backfill(app)
            assert live is not None
            assert live._index_url == INDEX_URL
            assert live.backfill_id is not None
            # Exactly one run at a time: a second call while live is a no-op.
            assert maybe_start_next_target(app) is False
            live.stop()  # keep the launched task from crawling anything
            task = getattr(app.state, "backfill_task", None)
            if task is not None:
                await task
            return live.backfill_id, 0

    backfill_id, _ = asyncio.run(scenario())
    row = _row(db, backfill_id)
    assert row["site_url"] == INDEX_URL
    # Stopped, not finished — the row stays 'running' for the next resume.
    assert row["status"] == "running"


def test_naturally_ended_run_continues_the_rotation(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed run auto-starts the next target — and stops after one hop."""
    import backend.jobs.backfill as backfill_module

    created: list[BackfillJob] = []

    class _RotatingJob(BackfillJob):
        """Rotation stand-in: fixture wiring, stopped the moment it is created.

        Stopping bounds the rotation to a single hop (``summary.stopping`` ends
        the next run without chaining further), and subclassing keeps
        :func:`live_backfill`'s isinstance check working across the patch.
        """

        def __init__(self, app, index_url, *, backfill_id=None, **kwargs):
            super().__init__(
                app, index_url, backfill_id=backfill_id,
                fetcher=FixtureFetcher(_pages()), http_client=_fixture_client(),
                **kwargs,
            )
            created.append(self)
            self.stop()

    monkeypatch.setattr(backfill_module, "BackfillJob", _RotatingJob)
    settings = backfill_settings(tmp_path, targets=[INDEX_URL, OTHER_INDEX])
    app = state_app(db, settings)

    async def scenario() -> int:
        with registered(_adapter()):
            first = _fixture_job(app, INDEX_URL)
            launch_backfill(app, first)
            task = getattr(app.state, "backfill_task", None)
            await task
            rotation_task = getattr(app.state, "backfill_task", None)
            if rotation_task is not None and rotation_task is not task:
                await rotation_task
            assert first.backfill_id is not None
            return first.backfill_id

    first_id = asyncio.run(scenario())
    assert _row(db, first_id)["status"] == "completed"
    # The rotation hopped exactly once, to the other configured target.
    assert len(created) == 1
    second = _row(db, created[0].backfill_id)
    assert second["site_url"] == OTHER_INDEX
    assert live_backfill(app) is None
