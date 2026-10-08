"""Crawl job tests (M1.11) — end-to-end over the fake adapter + MockTransport downloader.

Verifies the jobs-row lifecycle (created → progress updated → completed /
cancelled), counter summaries per PRD §5.2, cooperative pause/cancel, the
Level-1 URL duplicate check against the ``source`` table, download failures
recorded without failing the job (PRD §36), and the download limit (PRD §10).
No network, no browser, files land in a tmp directory.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from backend.jobs.crawl_job import CrawlJob, CrawlJobSummary
from backend.scraper.adapters import CrawlScope, ScopeKind, UnsupportedSiteError
from backend.scraper.adapters import base as adapter_base
from backend.scraper.crawler import PageFetcher
from backend.scraper.downloader import DownloadStatus
from tests.fixtures.fake_adapter import FixtureFetcher, once, wait_until
from tests.test_downloader import MP4_BYTES, WEBM_BYTES

ENTRY_URL = "https://fixture.test/comic/chapter-42?page=1"
CHAPTER_SCOPE = CrawlScope(ScopeKind.CURRENT_CHAPTER)
HEAVY_CHROME = "comment_attachments_heavy_chrome.html"
MEDIA_URL_ONE = "https://cdn.example.com/media/heavy-one.png"

#: MockTransport handler signature.
Handler = Callable[[httpx.Request], httpx.Response]


def chapter_pages(html: str) -> dict[str, str]:
    """All three discovered chapter pages serve the same fixture HTML."""
    return {f"https://fixture.test/comic/chapter-42?page={n}": html for n in (1, 2, 3)}


def media_handler(sample_images: dict[str, Path], *, missing: tuple[str, ...] = ()) -> Handler:
    """Serve fixture bytes by URL suffix; 404 for suffixes listed in ``missing``."""
    bodies: dict[str, tuple[bytes, str]] = {
        ".png": (sample_images["png"].read_bytes(), "image/png"),
        ".gif": (sample_images["gif"].read_bytes(), "image/gif"),
        ".mp4": (MP4_BYTES, "video/mp4"),
        ".webm": (WEBM_BYTES, "video/webm"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if any(path.endswith(suffix) for suffix in missing):
            return httpx.Response(404)
        for suffix, (body, content_type) in bodies.items():
            if path.endswith(suffix):
                return httpx.Response(200, content=body, headers={"content-type": content_type})
        return httpx.Response(404)

    return handler


def build_job(
    *,
    db: sqlite3.Connection,
    settings,
    destination: Path,
    fetcher: PageFetcher,
    client: httpx.AsyncClient,
    url: str = ENTRY_URL,
    force_rescan: bool = False,
) -> CrawlJob:
    return CrawlJob(
        url,
        CHAPTER_SCOPE,
        settings=settings,
        db=db,
        destination_dir=destination,
        force_rescan=force_rescan,
        fetcher=fetcher,
        http_client=client,
    )


def read_job(db: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row is not None
    return row


async def test_job_completes_end_to_end(
    fake_adapter, fixtures_dir: Path, make_settings, db: sqlite3.Connection, tmp_path: Path,
    sample_images: dict[str, Path],
) -> None:
    html = (fixtures_dir / HEAVY_CHROME).read_text(encoding="utf-8")
    destination = tmp_path / "media"
    fetcher = FixtureFetcher(chapter_pages(html))
    client = httpx.AsyncClient(transport=httpx.MockTransport(media_handler(sample_images)))
    job = build_job(db=db, settings=make_settings(concurrency=1), destination=destination,
                    fetcher=fetcher, client=client)
    try:
        summary = await job.run()
    finally:
        await client.aclose()

    assert isinstance(summary, CrawlJobSummary)
    assert summary.pages_scanned == 3
    assert summary.comments_discovered == 12
    assert summary.media_found == 15
    assert summary.downloaded_new == 15
    assert summary.skipped_duplicate == 0
    assert summary.download_failed == 0
    assert not summary.cancelled
    assert summary.failures == []

    # 5 unique attachment names; repeated URLs overwrite the same final name.
    assert sorted(p.name for p in destination.iterdir()) == [
        "eye-roll.webm",
        "heavy-clip.mp4",
        "heavy-one.png",
        "heavy-two.gif",
        "rel-sticker.png",
    ]
    # COLLECT/STORE split: the job never writes media/source rows (Milestone 2 owns those).
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM crawl_history").fetchone()[0] == 3

    job_row = read_job(db, summary.job_id)
    assert job_row["job_type"] == "crawl"
    assert job_row["status"] == "completed"
    assert job_row["progress"] == 1.0
    assert "new=15" in job_row["message"]
    assert job_row["error"] is None
    assert job_row["completed_at"] is not None


async def test_job_progress_rows_updated_during_run(
    fake_adapter, fixtures_dir: Path, make_settings, db: sqlite3.Connection, tmp_path: Path,
    sample_images: dict[str, Path],
) -> None:
    html = (fixtures_dir / HEAVY_CHROME).read_text(encoding="utf-8")
    seen: list[tuple[float, str]] = []
    holder: dict[str, CrawlJob] = {}

    def record(url: str) -> None:
        job = holder["job"]
        if job.job_id is None:
            return
        row = read_job(db, job.job_id)
        seen.append((row["progress"], row["message"]))

    fetcher = FixtureFetcher(chapter_pages(html), on_fetch=record)
    client = httpx.AsyncClient(transport=httpx.MockTransport(media_handler(sample_images)))
    job = build_job(db=db, settings=make_settings(concurrency=1),
                    destination=tmp_path / "media", fetcher=fetcher, client=client)
    holder["job"] = job
    try:
        summary = await job.run()
    finally:
        await client.aclose()

    # Progress was observed mid-crawl (crawl phase is 0.0–0.6) and completed at 1.0.
    assert any(0.0 < progress <= 0.6 for progress, _ in seen)
    assert all("pages=" in message for _, message in seen)
    assert read_job(db, summary.job_id)["progress"] == 1.0


async def test_job_cancel_stops_crawl_and_downloads(
    fake_adapter, fixtures_dir: Path, make_settings, db: sqlite3.Connection, tmp_path: Path,
    sample_images: dict[str, Path],
) -> None:
    html = (fixtures_dir / HEAVY_CHROME).read_text(encoding="utf-8")
    destination = tmp_path / "media"
    holder: dict[str, CrawlJob] = {}
    fetcher = FixtureFetcher(chapter_pages(html), on_fetch=once(lambda: holder["job"].cancel()))
    client = httpx.AsyncClient(transport=httpx.MockTransport(media_handler(sample_images)))
    job = build_job(db=db, settings=make_settings(concurrency=1), destination=destination,
                    fetcher=fetcher, client=client)
    holder["job"] = job
    try:
        summary = await job.run()
    finally:
        await client.aclose()

    assert summary.cancelled
    assert summary.pages_scanned == 1
    assert summary.downloaded_new == 0  # cancel lands before the download phase
    assert len(fetcher.fetched) == 1

    job_row = read_job(db, summary.job_id)
    assert job_row["status"] == "cancelled"
    assert job_row["progress"] < 1.0
    assert not destination.exists() or list(destination.iterdir()) == []


async def test_job_pause_holds_then_resume_completes(
    fake_adapter, fixtures_dir: Path, make_settings, db: sqlite3.Connection, tmp_path: Path,
    sample_images: dict[str, Path],
) -> None:
    html = (fixtures_dir / HEAVY_CHROME).read_text(encoding="utf-8")
    holder: dict[str, CrawlJob] = {}
    fetcher = FixtureFetcher(
        chapter_pages(html), on_fetch=once(lambda: holder["job"].pause())
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(media_handler(sample_images)))
    job = build_job(db=db, settings=make_settings(concurrency=1),
                    destination=tmp_path / "media", fetcher=fetcher, client=client)
    holder["job"] = job

    run_task = asyncio.create_task(job.run())
    await wait_until(lambda: len(fetcher.fetched) >= 1)
    await asyncio.sleep(0.05)
    assert len(fetcher.fetched) == 1, "paused job must not scan further pages"
    assert job.job_id is not None
    assert read_job(db, job.job_id)["message"] == "paused"
    assert read_job(db, job.job_id)["status"] == "running"

    job.resume()
    summary = await asyncio.wait_for(run_task, timeout=10)
    assert summary.pages_scanned == 3
    assert not summary.cancelled
    assert read_job(db, summary.job_id)["status"] == "completed"


async def test_job_unsupported_site_creates_no_job_row(
    fake_adapter, fixtures_dir: Path, make_settings, db: sqlite3.Connection, tmp_path: Path
) -> None:
    job = CrawlJob(
        "https://unknown.example/comic/chapter-9",
        settings=make_settings(),
        db=db,
        destination_dir=tmp_path / "media",
        fetcher=FixtureFetcher({}),
    )
    with pytest.raises(UnsupportedSiteError):
        await job.run()
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


async def test_job_level1_url_duplicate_counted(
    fake_adapter, fixtures_dir: Path, make_settings, db: sqlite3.Connection, tmp_path: Path,
    sample_images: dict[str, Path],
) -> None:
    html = (fixtures_dir / HEAVY_CHROME).read_text(encoding="utf-8")
    # Seed a prior collection of one attachment URL in the source table (PRD §12 level 1).
    cursor = db.execute("INSERT INTO media (file_path) VALUES ('data/media/00000001.png')")
    db.execute(
        "INSERT INTO source (media_id, site, page_url, comment_id, media_url, author_name) "
        "VALUES (?, 'fixture', ?, '919001', ?, 'meme_lord')",
        (cursor.lastrowid, ENTRY_URL, MEDIA_URL_ONE),
    )
    db.commit()

    fetcher = FixtureFetcher(chapter_pages(html))
    client = httpx.AsyncClient(transport=httpx.MockTransport(media_handler(sample_images)))
    job = build_job(db=db, settings=make_settings(concurrency=1),
                    destination=tmp_path / "media", fetcher=fetcher, client=client)
    try:
        summary = await job.run()
    finally:
        await client.aclose()

    # The seeded URL appears on each of the 3 pages — all skipped before any request.
    assert summary.skipped_duplicate == 3
    assert summary.downloaded_new == 12
    assert not (tmp_path / "media" / "heavy-one.png").exists()


async def test_job_records_download_failure_without_failing_job(
    fake_adapter, fixtures_dir: Path, make_settings, db: sqlite3.Connection, tmp_path: Path,
    sample_images: dict[str, Path],
) -> None:
    html = (fixtures_dir / HEAVY_CHROME).read_text(encoding="utf-8")
    fetcher = FixtureFetcher(chapter_pages(html))
    handler = media_handler(sample_images, missing=(".webm",))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    job = build_job(db=db, settings=make_settings(concurrency=1),
                    destination=tmp_path / "media", fetcher=fetcher, client=client)
    try:
        summary = await job.run()
    finally:
        await client.aclose()

    assert summary.download_failed == 3  # one bad URL per page, recorded not fatal
    assert summary.downloaded_new == 12
    job_row = read_job(db, summary.job_id)
    assert job_row["status"] == "completed"  # PRD §36: a failed item never stops the job
    assert job_row["error"] is None


async def test_job_respects_download_limit(
    fake_adapter, fixtures_dir: Path, make_settings, db: sqlite3.Connection, tmp_path: Path,
    sample_images: dict[str, Path],
) -> None:
    html = (fixtures_dir / HEAVY_CHROME).read_text(encoding="utf-8")
    fetcher = FixtureFetcher(chapter_pages(html))
    client = httpx.AsyncClient(transport=httpx.MockTransport(media_handler(sample_images)))
    job = build_job(db=db, settings=make_settings(concurrency=1, download_limit=2),
                    destination=tmp_path / "media", fetcher=fetcher, client=client)
    try:
        summary = await job.run()
    finally:
        await client.aclose()

    assert summary.downloaded_new == 2
    assert summary.download_limited
    assert len(list((tmp_path / "media").iterdir())) == 2
    assert read_job(db, summary.job_id)["status"] == "completed"


def test_fake_adapter_registered_then_restored(fake_adapter) -> None:
    """Registry hygiene: the harness registers exactly its own site key."""
    assert adapter_base._REGISTRY["fixture"] is fake_adapter
    assert adapter_base._REGISTRY["fixture"].site == "fixture"


def test_download_status_values() -> None:
    assert {status.value for status in DownloadStatus} == {"ok", "duplicate_url", "failed"}
