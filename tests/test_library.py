"""M2.1 media persistence tests — ingest rows/files, Level-2 dups, library queries.

All offline: images are written with Pillow, downloads are synthesized
:class:`DownloadResult` values, and the crawl integration runs the fake adapter
+ MockTransport harness from the M1 suites.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from PIL import Image

from backend.config import Settings, load_settings
from backend.jobs.crawl_job import CrawlJob
from backend.media.hashing import sha256_file
from backend.media.library import (
    MediaFilters,
    count_by_dup_status,
    count_media,
    get_media,
    ingest_download,
    list_media,
    remove_media,
)
from backend.scraper.adapters.base import CommentMeta
from backend.scraper.downloader import DownloadResult, DownloadStatus
from tests.fixtures.fake_adapter import FixtureFetcher
from tests.test_crawl_job import CHAPTER_SCOPE, HEAVY_CHROME, chapter_pages, media_handler
from tests.test_downloader import MP4_BYTES

ENTRY_URL = "https://fixture.test/comic/chapter-42?page=1"
CDN = "https://cdn.example.com/media"


@pytest.fixture
def library_settings(tmp_path: Path) -> Settings:
    """Settings with every storage path inside the test's tmp dir."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "library.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    return replace(base, storage=storage)


def _comment(**overrides: object) -> CommentMeta:
    values: dict = {
        "comment_id": "918271",
        "author_name": "reader_one",
        "page_url": "https://fixture.test/comic/chapter-42?page=3",
        "chapter": "chapter-42",
        "page_number": 3,
        "text": "hehe",
    }
    values.update(overrides)
    return CommentMeta(**values)


def _make_image(path: Path, *, color: tuple[int, int, int] = (10, 20, 30),
                image_format: str = "PNG", size: tuple[int, int] = (32, 32)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format=image_format)
    return path


def _result(
    path: Path,
    *,
    url: str = f"{CDN}/confused-cat.png",
    content_type: str | None = "image/png",
    kind: str | None = "image",
    status: DownloadStatus = DownloadStatus.OK,
) -> DownloadResult:
    if status is not DownloadStatus.OK:
        return DownloadResult(url=url, status=status, error="HTTP 404")
    return DownloadResult(
        url=url,
        status=status,
        path=path,
        sha256=sha256_file(path),
        size=path.stat().st_size,
        content_type=content_type,
        kind=kind,
    )


def _ids(rows: list[dict]) -> set[int]:
    return {int(row["id"]) for row in rows}


# ---------------------------------------------------------------------------
# ingest_download
# ---------------------------------------------------------------------------


def test_ingest_stores_rows_and_id_named_file(db, library_settings, tmp_path) -> None:
    src = _make_image(tmp_path / "downloads" / "confused-cat.png", color=(1, 2, 3))
    result = _result(src, url=f"{CDN}/confused-cat.png")

    media_id = ingest_download(db, result, _comment(), settings=library_settings)

    assert media_id == 1
    final = library_settings.storage.media_directory / "00000001.png"
    assert final.is_file()
    assert not src.exists(), "the downloaded file must be moved, not copied"

    row = db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    assert row["file_path"] == str(final)
    assert row["original_filename"] == "confused-cat.png"
    assert row["mime_type"] == "image/png"
    assert row["extension"] == "png"
    assert row["file_size"] == final.stat().st_size
    assert (row["width"], row["height"]) == (32, 32)
    assert row["duration"] is None
    assert row["sha256"] == sha256_file(final)
    assert row["processing_status"] == "DOWNLOADED"
    assert row["dup_status"] == "nondup"
    assert row["dup_flagged_at"] is None
    assert row["created_at"]

    source = db.execute("SELECT * FROM source WHERE media_id = ?", (media_id,)).fetchone()
    assert source["site"] == "fixture.test"
    assert source["page_url"] == "https://fixture.test/comic/chapter-42?page=3"
    assert source["chapter"] == "chapter-42"
    assert source["page_number"] == 3
    assert source["comment_id"] == "918271"
    assert source["media_url"] == f"{CDN}/confused-cat.png"
    assert source["author_name"] == "reader_one"
    assert source["collected_at"]


def test_ingest_assigns_sequential_id_names_across_formats(db, library_settings, tmp_path) -> None:
    png = _make_image(tmp_path / "a.png", color=(9, 9, 9))
    gif = _make_image(tmp_path / "b.gif", color=(8, 8, 8), image_format="GIF")
    mp4 = tmp_path / "c.mp4"
    mp4.write_bytes(MP4_BYTES)

    assert ingest_download(db, _result(png, url=f"{CDN}/a.png"),
                           _comment(), settings=library_settings) == 1
    assert ingest_download(db, _result(gif, url=f"{CDN}/b.gif", content_type="image/gif",
                                       kind="gif"),
                           _comment(), settings=library_settings) == 2
    assert ingest_download(db, _result(mp4, url=f"{CDN}/c.mp4", content_type="video/mp4",
                                       kind="video"),
                           _comment(), settings=library_settings) == 3

    media_dir = library_settings.storage.media_directory
    assert sorted(p.name for p in media_dir.iterdir()) == [
        "00000001.png", "00000002.gif", "00000003.mp4",
    ]
    video = db.execute("SELECT * FROM media WHERE id = 3").fetchone()
    assert video["extension"] == "mp4"
    assert video["mime_type"] == "video/mp4"
    assert video["width"] is None and video["height"] is None  # no decoder required (M1.9)


def test_ingest_level2_duplicate_is_recorded_and_flagged(db, library_settings, tmp_path) -> None:
    first = _make_image(tmp_path / "first.png", color=(7, 7, 7))
    # Same bytes, different URL: an exact (Level-2) duplicate (PRD §12).
    second = tmp_path / "second.png"
    shutil.copyfile(first, second)

    retained = ingest_download(db, _result(first, url=f"{CDN}/one.png"),
                               _comment(), settings=library_settings)
    duplicate = ingest_download(db, _result(second, url=f"{CDN}/elsewhere/two.png"),
                                _comment(comment_id="918999"), settings=library_settings)

    assert retained == 1
    assert duplicate is None, "Level-2 duplicate must signal 'not a new unique item'"

    kept = db.execute("SELECT * FROM media WHERE id = 1").fetchone()
    copy = db.execute("SELECT * FROM media WHERE id = 2").fetchone()
    assert kept["dup_status"] == "nondup", "first-collected copy is retained, never flagged"
    assert copy["dup_status"] == "dup"
    assert copy["dup_of_media_id"] == 1
    assert copy["dup_flagged_at"] is not None
    assert copy["sha256"] == kept["sha256"]
    assert (library_settings.storage.media_directory / "00000001.png").is_file()
    assert (library_settings.storage.media_directory / "00000002.png").is_file()

    sources = db.execute("SELECT media_id, media_url FROM source ORDER BY id").fetchall()
    assert [s["media_url"] for s in sources] == [f"{CDN}/one.png", f"{CDN}/elsewhere/two.png"]


def test_ingest_rejects_a_failed_download(db, library_settings) -> None:
    result = DownloadResult(url=f"{CDN}/gone.png", status=DownloadStatus.FAILED, error="HTTP 404")
    with pytest.raises(ValueError):
        ingest_download(db, result, _comment(), settings=library_settings)
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 0


def test_ingest_unreadable_file_leaves_no_rows(db, library_settings, tmp_path) -> None:
    missing = tmp_path / "not-there.png"
    result = DownloadResult(url=f"{CDN}/phantom.png", status=DownloadStatus.OK, path=missing)
    with pytest.raises(OSError):
        ingest_download(db, result, _comment(), settings=library_settings)
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 0


def test_ingest_rolls_back_rows_when_file_move_fails(
    db, library_settings, tmp_path, monkeypatch
) -> None:
    src = _make_image(tmp_path / "stuck.png", color=(4, 4, 4))
    result = _result(src, url=f"{CDN}/stuck.png")

    def explode(source: Path, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("backend.media.library._move_into_place", explode)
    with pytest.raises(OSError):
        ingest_download(db, result, _comment(), settings=library_settings)

    # Atomicity: DB rows only survive when the file is in place — no ghosts.
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 0
    assert src.exists(), "the downloaded file must remain untouched for inspection"
    assert list(library_settings.storage.media_directory.glob("*")) == []


# ---------------------------------------------------------------------------
# get_media / list_media / counts / remove
# ---------------------------------------------------------------------------


def test_get_media_returns_provenance_and_dup_info(db, library_settings, tmp_path) -> None:
    first = _make_image(tmp_path / "x.png", color=(5, 5, 5))
    second = tmp_path / "y.png"
    shutil.copyfile(first, second)
    ingest_download(db, _result(first, url=f"{CDN}/x.png"), _comment(), settings=library_settings)
    ingest_download(db, _result(second, url=f"{CDN}/y.png"), _comment(), settings=library_settings)

    retained = get_media(db, 1)
    assert retained is not None
    assert retained["site"] == "fixture.test"
    assert len(retained["sources"]) == 1
    assert retained["sources"][0]["comment_id"] == "918271"
    assert retained["ai_metadata"] is None
    assert retained["dup_expires_at"] is None  # nondup: no expiry (PRD §12.1)
    assert retained["sha256"] == sha256_file(
        library_settings.storage.media_directory / "00000001.png"
    )

    copy = get_media(db, 2)
    assert copy is not None
    assert copy["dup_status"] == "dup"
    assert copy["dup_expires_at"] is not None, "pending-deletion items expose expiry (§12.1)"
    assert copy["dup_of_media_id"] == 1

    assert get_media(db, 999) is None


def test_list_media_paginates_newest_first(db, library_settings, tmp_path) -> None:
    for index in range(3):
        image = _make_image(tmp_path / f"n{index}.png", color=(index + 1, 0, 0))
        ingest_download(db, _result(image, url=f"{CDN}/n{index}.png"),
                        _comment(), settings=library_settings)

    page_one = list_media(db, MediaFilters(), page=1, page_size=2)
    page_two = list_media(db, MediaFilters(), page=1 + 1, page_size=2)
    assert [item["id"] for item in page_one] == [3, 2]
    assert [item["id"] for item in page_two] == [1]
    assert count_media(db, MediaFilters()) == 3
    # Gallery cards never expose storage paths (API hygiene).
    assert all("file_path" not in item for item in page_one)


def test_list_media_type_site_format_and_chapter_filters(
    db, library_settings, tmp_path
) -> None:
    png = _make_image(tmp_path / "p.png", color=(11, 0, 0))
    gif = _make_image(tmp_path / "g.gif", color=(12, 0, 0), image_format="GIF")
    ingest_download(db, _result(png, url=f"{CDN}/p.png"), _comment(), settings=library_settings)
    ingest_download(db, _result(gif, url=f"{CDN}/g.gif", content_type="image/gif", kind="gif"),
                    _comment(chapter="chapter-43"), settings=library_settings)

    assert _ids(list_media(db, MediaFilters(type="gif"))) == {2}
    assert _ids(list_media(db, MediaFilters(type="image"))) == {1}
    assert _ids(list_media(db, MediaFilters(type="video"))) == set()
    assert _ids(list_media(db, MediaFilters(site="fixture.test"))) == {1, 2}
    assert _ids(list_media(db, MediaFilters(site="other.test"))) == set()
    assert _ids(list_media(db, MediaFilters(format="GIF"))) == {2}  # case/dot insensitive
    assert _ids(list_media(db, MediaFilters(format="png"))) == {1}
    assert _ids(list_media(db, MediaFilters(chapter="chapter-43"))) == {2}
    assert _ids(list_media(db, MediaFilters(chapter="chapter-42"))) == {1}


def test_list_media_date_status_and_favorite_filters(db, library_settings, tmp_path) -> None:
    for index in range(2):
        image = _make_image(tmp_path / f"z{index}.png", color=(index, 7, 7))
        ingest_download(db, _result(image, url=f"{CDN}/z{index}.png"),
                        _comment(), settings=library_settings)
    db.execute("UPDATE media SET is_favorite = 1 WHERE id = 1")
    db.execute("UPDATE media SET processing_status = 'READY' WHERE id = 1")
    db.commit()

    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    yesterday = today - timedelta(days=1)

    assert _ids(list_media(db, MediaFilters(is_favorite=True))) == {1}
    assert _ids(list_media(db, MediaFilters(is_favorite=False))) == {2}
    assert _ids(list_media(db, MediaFilters(processing_status="READY"))) == {1}
    assert _ids(list_media(db, MediaFilters(processing_status="DOWNLOADED"))) == {2}
    assert _ids(list_media(db, MediaFilters(date_from=str(today)))) == {1, 2}
    assert _ids(list_media(db, MediaFilters(date_from=str(tomorrow)))) == set()
    assert _ids(list_media(db, MediaFilters(date_to=str(yesterday)))) == set()

    with pytest.raises(ValueError):
        list_media(db, MediaFilters(dup_status="maybe"))
    with pytest.raises(ValueError):
        list_media(db, MediaFilters(processing_status="PENDING"))


def test_counts_include_dup_breakdown(db, library_settings, tmp_path) -> None:
    first = _make_image(tmp_path / "d1.png", color=(3, 3, 9))
    second = tmp_path / "d2.png"
    shutil.copyfile(first, second)
    plain = _make_image(tmp_path / "d3.png", color=(3, 3, 10))
    ingest_download(db, _result(first, url=f"{CDN}/d1.png"), _comment(), settings=library_settings)
    ingest_download(db, _result(second, url=f"{CDN}/d2.png"), _comment(), settings=library_settings)
    ingest_download(db, _result(plain, url=f"{CDN}/d3.png"), _comment(), settings=library_settings)

    assert count_media(db) == 3
    breakdown = count_by_dup_status(db)
    assert breakdown == {"dup": 1, "nondup": 2, "unflagged": 0}
    only_dup = count_by_dup_status(db, MediaFilters(dup_status="dup"))
    assert only_dup == {"dup": 1, "nondup": 0, "unflagged": 0}


def test_remove_media_deletes_rows_and_files(db, library_settings, tmp_path) -> None:
    image = _make_image(tmp_path / "rm.png", color=(6, 6, 6))
    media_id = ingest_download(db, _result(image, url=f"{CDN}/rm.png"),
                               _comment(), settings=library_settings)
    assert media_id is not None
    final = library_settings.storage.media_directory / "00000001.png"
    assert final.is_file()

    assert remove_media(db, media_id, settings=library_settings) is True
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 0, "source cascades"
    assert not final.exists()
    assert remove_media(db, media_id, settings=library_settings) is False


# ---------------------------------------------------------------------------
# Crawl pipeline integration (crawl_job wiring, ingest=True)
# ---------------------------------------------------------------------------


async def test_crawl_job_ingests_rows_with_id_named_files(
    fake_adapter, fixtures_dir, db, library_settings, tmp_path, sample_images
) -> None:
    """End-to-end: download → ingest → id-named files, Level-1/2 dup counters."""
    html = (fixtures_dir / HEAVY_CHROME).read_text(encoding="utf-8")
    settings = replace(
        library_settings,
        crawler=replace(library_settings.crawler, delay_seconds=0.0, concurrency=1),
    )
    fetcher = FixtureFetcher(chapter_pages(html))
    client = httpx.AsyncClient(transport=httpx.MockTransport(media_handler(sample_images)))
    job = CrawlJob(
        ENTRY_URL,
        CHAPTER_SCOPE,
        settings=settings,
        db=db,
        fetcher=fetcher,
        http_client=client,
        ingest=True,
    )
    try:
        summary = await job.run()
    finally:
        await client.aclose()

    # 5 unique attachment URLs x 3 pages = 15 refs. Page 1 ingests all five;
    # two PNGs share the same served bytes → one Level-2 dup on page 1; pages
    # 2-3 hit the Level-1 URL check (source rows exist) → 10 URL skips.
    assert summary.pages_scanned == 3
    assert summary.media_found == 15
    assert summary.download_failed == 0
    assert summary.downloaded_new == 4
    assert summary.skipped_duplicate == 11
    assert summary.downloaded_new + summary.skipped_duplicate == 15

    # Every ingested copy is stored under the id-based name (PRD §5.3).
    media_dir = library_settings.storage.media_directory
    assert sorted(p.name for p in media_dir.iterdir()) == [
        "00000001.png", "00000002.gif", "00000003.mp4",
        "00000004.png", "00000005.webm",
    ]

    media_rows = db.execute("SELECT id, sha256, dup_status, dup_of_media_id FROM media ORDER BY id").fetchall()
    source_rows = db.execute("SELECT COUNT(*) FROM source").fetchone()[0]
    assert len(media_rows) == 5
    assert source_rows == 5
    # 00000001.png and 00000004.png are the same bytes: first retained, later dup.
    assert media_rows[0]["dup_status"] == "nondup"
    assert media_rows[3]["sha256"] == media_rows[0]["sha256"]
    assert media_rows[3]["dup_status"] == "dup"
    assert media_rows[3]["dup_of_media_id"] == 1
    assert [row["dup_status"] for row in media_rows].count("dup") == 1

    job_row = db.execute("SELECT * FROM jobs WHERE id = ?", (summary.job_id,)).fetchone()
    assert job_row["status"] == "completed"
    assert "new=4" in job_row["message"] and "dup=11" in job_row["message"]
