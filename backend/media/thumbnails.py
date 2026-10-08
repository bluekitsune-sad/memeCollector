"""Thumbnail + preview generation — the PROCESS stage for images (PRD §13, §14).

For every stored item :func:`generate_thumbnails` writes two WebP files,
atomically (temp file → ``os.replace``), never touching the original:

* ``data/thumbnails/<id>.webp`` — max 256 px, gallery-fast.
* ``data/previews/<id>.webp``   — max 1024 px, detail/lightbox view.

Both are saved at quality 80. Animated sources (GIF, animated WebP) contribute
their **first frame** only — the original keeps animating; frame extraction for
AI analysis is a separate Milestone 3 concern (PRD §13).

MP4/WebM have no guaranteed decoder (ffmpeg is not a dependency), so instead of
frame extraction these get a neutral generated *poster* (dark tile + play
triangle, Pillow only) — the gallery always has something to show, and the
thumbnail job never re-processes the same video forever. A real extracted
poster frame can replace this later without schema changes.

Malformed/truncated/unreadable images raise :class:`ThumbnailError`: the caller
(thumbnail job) records a per-item failure and the batch continues (PRD §36).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, UnidentifiedImageError

from backend.config import Settings

logger = logging.getLogger(__name__)

#: Maximum edge length of a gallery thumbnail (PRD §14).
THUMBNAIL_MAX_PX = 256

#: Maximum edge length of the detail-page preview (PRD §14).
PREVIEW_MAX_PX = 1024

#: WebP quality for both generated files (PRD §14 — fast browsing).
WEBP_QUALITY = 80

#: Video containers get a generated poster instead of an extracted frame.
_VIDEO_SUFFIXES = frozenset({".mp4", ".webm"})

#: Pillow failures that mean "this file is not a usable image".
_IMAGE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    SyntaxError,
    ValueError,
    Image.DecompressionBombError,
)


class ThumbnailError(Exception):
    """An original could not be turned into thumbnails — recorded per item (PRD §36)."""


def generate_thumbnails(
    media_id: int, original_path: Path | str, settings: Settings
) -> tuple[Path, Path]:
    """Write ``(thumbnail_path, preview_path)`` for one media item.

    Returns absolute-ish paths (as configured) inside
    ``settings.storage.thumbnail_directory`` / ``preview_directory``, named
    ``<media_id>.webp``. Raises :class:`ThumbnailError` when the original is
    missing or undecodable — never partially-written targets are left visible
    (each file is written to a temp name and renamed into place).
    """
    original = Path(original_path)
    if not original.is_file():
        raise ThumbnailError(f"original file not found: {original}")

    thumbnail_dir = settings.storage.thumbnail_directory
    preview_dir = settings.storage.preview_directory
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)
    thumb_target = thumbnail_dir / f"{media_id:08d}.webp"
    preview_target = preview_dir / f"{media_id:08d}.webp"

    if original.suffix.lower() in _VIDEO_SUFFIXES:
        poster = _video_poster()
        _save_webp(poster, thumb_target, THUMBNAIL_MAX_PX)
        _save_webp(poster, preview_target, PREVIEW_MAX_PX)
        logger.debug("video poster written media_id=%d", media_id)
        return thumb_target, preview_target

    try:
        with Image.open(original) as image:
            image.load()  # force a full decode now: truncated files fail here
            frame = _rgba_or_rgb(image)
            thumb = frame.copy()
            thumb.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX), Image.LANCZOS)
            preview = frame.copy()
            preview.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX), Image.LANCZOS)
    except _IMAGE_ERRORS as exc:
        raise ThumbnailError(f"cannot decode {original.name}: {type(exc).__name__}: {exc}") from exc

    _atomic_save(thumb, thumb_target)
    _atomic_save(preview, preview_target)
    return thumb_target, preview_target


def _rgba_or_rgb(image: Image.Image) -> Image.Image:
    """A copy of ``image`` in a mode WebP can store, preserving transparency.

    GIFs arrive in palette mode (``P``) — converted to ``RGBA`` when they carry
    transparency, otherwise to ``RGB`` for smaller output. The original object
    is never modified (PRD §14: originals are read-only).
    """
    if image.mode in ("RGB", "RGBA"):
        return image.copy()
    if image.mode in ("P", "LA", "PA") or "transparency" in image.info:
        return image.convert("RGBA")
    return image.convert("RGB")


def _save_webp(image: Image.Image, target: Path, max_px: int) -> None:
    """Scale ``image`` down to ``max_px`` (in a copy) and write it as WebP."""
    scaled = image.copy()
    scaled.thumbnail((max_px, max_px), Image.LANCZOS)
    _atomic_save(scaled, target)


def _atomic_save(image: Image.Image, target: Path) -> None:
    """Write WebP to a temp sibling, then ``os.replace`` into place (no partial files)."""
    temp = target.with_name(f"{target.name}.tmp")
    try:
        image.save(temp, format="WEBP", quality=WEBP_QUALITY)
        os.replace(temp, target)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def _video_poster() -> Image.Image:
    """Neutral poster tile (dark background + play triangle) at preview resolution."""
    size = (PREVIEW_MAX_PX, PREVIEW_MAX_PX)
    poster = Image.new("RGB", size, (32, 36, 44))
    draw = ImageDraw.Draw(poster)
    triangle = _centered_triangle(size[0], size[1], fraction=0.34)
    draw.polygon(triangle, fill=(210, 214, 222))
    return poster


def _centered_triangle(width: int, height: int, *, fraction: float) -> list[tuple[float, float]]:
    """Vertices of a right-pointing play triangle centered in ``width``×``height``."""
    half = min(width, height) * fraction / 2.0
    cx, cy = width / 2.0, height / 2.0
    return [(cx - half * 0.6, cy - half), (cx - half * 0.6, cy + half), (cx + half, cy)]
