"""M2.2 thumbnail/preview tests — WebP generation, GIF first frame, video poster.

All offline: images are written with Pillow, videos use the synthetic container
bytes from the downloader suite, and originals are verified byte-identical after
processing (PRD §14: the original is read-only).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from backend.config import Settings, load_settings
from backend.jobs.thumbnail_job import run_thumbnail_job
from backend.media.library import ingest_download
from backend.media.thumbnails import (
    PREVIEW_MAX_PX,
    THUMBNAIL_MAX_PX,
    ThumbnailError,
    generate_thumbnails,
)
from tests.test_downloader import MP4_BYTES
from tests.test_library import CDN, _comment, _make_image, _result


@pytest.fixture
def thumb_settings(tmp_path: Path) -> Settings:
    """Settings with every storage path inside the test's tmp dir."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "thumbs.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    return replace(base, storage=storage)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _open_rgb(path: Path) -> Image.Image:
    image = Image.open(path)
    return image.convert("RGB")


def _center(path: Path) -> tuple[int, int, int]:
    with _open_rgb(path) as image:
        return image.getpixel((image.width // 2, image.height // 2))


def _corner(path: Path) -> tuple[int, int, int]:
    with _open_rgb(path) as image:
        return image.getpixel((0, 0))


def _assert_close(actual: tuple[int, ...], expected: tuple[int, ...], tolerance: int) -> None:
    assert all(abs(a - e) <= tolerance for a, e in zip(actual, expected)), (
        f"{actual} not within {tolerance} of {expected}"
    )


def _no_temp_leftovers(*directories: Path) -> None:
    for directory in directories:
        assert list(directory.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# generate_thumbnails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_generate_thumbnails_writes_webp_within_bounds(
    tmp_path: Path, thumb_settings: Settings, image_format: str
) -> None:
    source = _make_image(tmp_path / "src" / f"big.{image_format.lower()}",
                         color=(90, 120, 160), image_format=image_format, size=(640, 480))
    before = _digest(source)

    thumb, preview = generate_thumbnails(7, source, thumb_settings)

    storage = thumb_settings.storage
    assert thumb == storage.thumbnail_directory / "00000007.webp"
    assert preview == storage.preview_directory / "00000007.webp"
    for target, bound in ((thumb, THUMBNAIL_MAX_PX), (preview, PREVIEW_MAX_PX)):
        with Image.open(target) as image:
            assert image.format == "WEBP"
            assert max(image.size) <= bound
    assert thumb.stat().st_size > 0 and preview.stat().st_size > 0
    assert _digest(source) == before, "original must stay byte-identical (PRD §14)"
    _no_temp_leftovers(storage.thumbnail_directory, storage.preview_directory)


def test_small_sources_are_not_upscaled(tmp_path: Path, thumb_settings: Settings) -> None:
    source = _make_image(tmp_path / "small.png", color=(10, 200, 10), size=(64, 64))

    thumb, _preview = generate_thumbnails(1, source, thumb_settings)

    with Image.open(thumb) as image:
        assert image.size == (64, 64)


def test_gif_outputs_a_single_static_frame(tmp_path: Path, thumb_settings: Settings) -> None:
    source = tmp_path / "anim.gif"
    frames = [
        Image.new("RGB", (64, 64), (20, 40, 180)),
        Image.new("RGB", (64, 64), (80, 40, 140)),
        Image.new("RGB", (64, 64), (140, 40, 100)),
    ]
    frames[0].save(source, format="GIF", save_all=True, append_images=frames[1:],
                   duration=120, loop=0)

    thumb, preview = generate_thumbnails(2, source, thumb_settings)

    for target in (thumb, preview):
        with Image.open(target) as image:
            assert image.n_frames == 1, "thumbnail is static; the original keeps animating"
        _assert_close(_center(target), (20, 40, 180), tolerance=30)


def test_transparent_png_keeps_alpha(tmp_path: Path, thumb_settings: Settings) -> None:
    source = tmp_path / "alpha.png"
    tile = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    for x in range(32):
        for y in range(64):
            tile.putpixel((x, y), (0, 0, 0, 0))
    tile.save(source, format="PNG")

    thumb, _preview = generate_thumbnails(3, source, thumb_settings)

    with Image.open(thumb) as image:
        assert image.mode == "RGBA"
        alpha = image.getchannel("A")
        assert alpha.getpixel((0, 0)) < 16, "transparent corner must stay transparent"
        assert alpha.getpixel((image.width - 1, 0)) == 255


def test_missing_original_raises_thumbnail_error(tmp_path: Path, thumb_settings: Settings) -> None:
    with pytest.raises(ThumbnailError, match="not found"):
        generate_thumbnails(4, tmp_path / "ghost.png", thumb_settings)


def test_malformed_original_raises_and_leaves_no_targets(
    tmp_path: Path, thumb_settings: Settings
) -> None:
    source = tmp_path / "broken.png"
    source.write_bytes(b"this is definitely not an image")

    with pytest.raises(ThumbnailError):
        generate_thumbnails(5, source, thumb_settings)

    storage = thumb_settings.storage
    assert list(storage.thumbnail_directory.glob("*")) == []
    assert list(storage.preview_directory.glob("*")) == []


def test_video_gets_generated_poster(tmp_path: Path, thumb_settings: Settings) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(MP4_BYTES)

    thumb, preview = generate_thumbnails(6, source, thumb_settings)

    storage = thumb_settings.storage
    assert thumb.is_file() and preview.is_file()
    with Image.open(thumb) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= THUMBNAIL_MAX_PX
    _assert_close(_corner(thumb), (32, 36, 44), tolerance=16)   # dark tile
    _assert_close(_center(thumb), (210, 214, 222), tolerance=40)  # play triangle
    _no_temp_leftovers(storage.thumbnail_directory, storage.preview_directory)


# ---------------------------------------------------------------------------
# run_thumbnail_job
# ---------------------------------------------------------------------------


def _ingest(
    db, settings: Settings, tmp_path: Path, name: str, *, image_format: str = "PNG"
) -> int:
    """Store one freshly generated image through the real ingest path.

    The color is derived from ``name`` so every ingest is byte-unique (no
    accidental Level-2 duplicates between fixtures).
    """
    suffix = image_format.lower()
    shade = (sum(ord(char) for char in name) % 200) + 20
    source = _make_image(tmp_path / "dl" / f"{name}.{suffix}", color=(shade, 60, 90),
                         image_format=image_format)
    content_type = {"png": "image/png", "gif": "image/gif"}[suffix]
    kind = "gif" if suffix == "gif" else "image"
    media_id = ingest_download(
        db,
        _result(source, url=f"{CDN}/{name}.{suffix}", content_type=content_type, kind=kind),
        _comment(),
        settings=settings,
    )
    assert media_id is not None
    return media_id


async def test_thumbnail_job_generates_all_pending(
    db, thumb_settings: Settings, tmp_path: Path
) -> None:
    _ingest(db, thumb_settings, tmp_path, "one")
    _ingest(db, thumb_settings, tmp_path, "two", image_format="GIF")

    summary = await run_thumbnail_job(db=db, settings=thumb_settings)

    assert (summary.total, summary.processed, summary.failed) == (2, 2, 0)
    rows = db.execute(
        "SELECT id, thumbnail_path, preview_path FROM media ORDER BY id"
    ).fetchall()
    for row in rows:
        assert row["thumbnail_path"] and Path(row["thumbnail_path"]).is_file()
        assert row["preview_path"] and Path(row["preview_path"]).is_file()

    job = db.execute("SELECT * FROM jobs WHERE id = ?", (summary.job_id,)).fetchone()
    assert job["job_type"] == "thumbnail"
    assert job["status"] == "completed"
    assert job["progress"] == 1.0
    assert job["message"] == "processed=2/2 failed=0"
    assert job["completed_at"] is not None


async def test_thumbnail_job_records_failure_and_keeps_going(
    db, thumb_settings: Settings, tmp_path: Path
) -> None:
    good = _ingest(db, thumb_settings, tmp_path, "good")
    bad = _ingest(db, thumb_settings, tmp_path, "bad")
    bad_file = thumb_settings.storage.media_directory / f"{bad:08d}.png"
    bad_file.write_bytes(b"corrupted beyond decoding")

    summary = await run_thumbnail_job(db=db, settings=thumb_settings)

    assert (summary.total, summary.processed, summary.failed) == (2, 1, 1)
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (summary.job_id,)).fetchone()
    assert job["status"] == "completed", "one bad file must not fail the batch (PRD §36)"
    assert job["message"] == "processed=1/2 failed=1"

    rows = {row["id"]: row for row in db.execute("SELECT * FROM media").fetchall()}
    assert rows[good]["thumbnail_path"]
    assert rows[bad]["thumbnail_path"] is None, "failed items stay pending for retry"

    # Repair the file: the next run picks up only what is still missing.
    Image.new("RGB", (48, 48), (5, 5, 5)).save(bad_file, format="PNG")
    retry = await run_thumbnail_job(db=db, settings=thumb_settings)
    assert (retry.total, retry.processed, retry.failed) == (1, 1, 0)
    assert db.execute(
        "SELECT thumbnail_path FROM media WHERE id = ?", (bad,)
    ).fetchone()["thumbnail_path"]


async def test_thumbnail_job_skips_already_processed_and_handles_empty_library(
    db, thumb_settings: Settings, tmp_path: Path
) -> None:
    empty = await run_thumbnail_job(db=db, settings=thumb_settings)
    assert (empty.total, empty.processed, empty.failed) == (0, 0, 0)
    assert db.execute(
        "SELECT status FROM jobs WHERE id = ?", (empty.job_id,)
    ).fetchone()["status"] == "completed"

    _ingest(db, thumb_settings, tmp_path, "only")
    first = await run_thumbnail_job(db=db, settings=thumb_settings)
    second = await run_thumbnail_job(db=db, settings=thumb_settings)
    assert first.processed == 1
    assert second.total == 0, "items with a thumbnail are never re-processed"
