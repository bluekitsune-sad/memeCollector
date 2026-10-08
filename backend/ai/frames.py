"""GIF frame sampling for AI analysis (PRD §13) plus media-kind helpers.

PRD §13: the model should see representative frames, not every frame of an
animation. Sampling rules implemented here:

* **static image** → one frame (so callers can route any image file here);
* **short animation** (≤ 4 frames) → first, middle, last (≤ 3 frames);
* **longer animation** → up to ``max_frames`` evenly spaced samples, endpoints
  included.

Frames are returned as PNG bytes (RGBA, so palette transparency survives).
:func:`sample_frames` never raises: malformed/unreadable input is logged and
yields an empty list (``None`` for callers to treat as a failure), because one
bad file must not stop the queue (PRD §36).

The kind helpers (:func:`is_gif`, :func:`is_video`, :func:`guess_mime_type`)
exist because ``media.mime_type``/``extension`` may each be missing; the queue
uses them to route GIFs to frame sampling and to skip videos (download-only in
the MVP).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

#: Animations with at most this many frames take the first/middle/last rule.
SHORT_GIF_MAX_FRAMES = 4

#: Pillow exceptions that mean "this file is not a usable image" (malformed/truncated).
_FRAME_ERRORS = (
    UnidentifiedImageError,
    EOFError,
    OSError,
    SyntaxError,
    ValueError,
    Image.DecompressionBombError,
)

_EXTENSION_MIMES: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
}

_VIDEO_EXTENSIONS: frozenset[str] = frozenset({"mp4", "webm", "mov", "mkv", "avi"})


def _normalized_extension(extension: str | None) -> str:
    return extension.lower().lstrip(".") if extension else ""


def is_gif(mime_type: str | None, extension: str | None = None) -> bool:
    """True when the media is an animated GIF, based on ``mime_type`` or ``extension``."""
    if mime_type:
        return mime_type.lower() == "image/gif"
    return _normalized_extension(extension) == "gif"


def is_video(mime_type: str | None, extension: str | None = None) -> bool:
    """True when the media is video (MVP: downloaded but not AI-analyzed)."""
    if mime_type and mime_type.lower().startswith("video/"):
        return True
    return _normalized_extension(extension) in _VIDEO_EXTENSIONS


def guess_mime_type(mime_type: str | None, extension: str | None = None) -> str:
    """Best-known content type: the declared ``mime_type``, else the file extension.

    Unknown image extensions fall back to ``image/jpeg`` (the most common
    comment-section format) so an image without a declared type still produces a
    valid data URI for vision models.
    """
    if mime_type:
        return mime_type
    return _EXTENSION_MIMES.get(_normalized_extension(extension), "image/jpeg")


def sample_frames(path: Path | str, *, max_frames: int = 6) -> list[bytes]:
    """Sample representative frames from ``path`` per PRD §13; returns PNG bytes.

    Returns ``[]`` (with a log line) for unreadable or malformed files instead
    of raising; a partially decodable animation returns the frames decoded so
    far.
    """
    if max_frames < 1:
        raise ValueError("max_frames must be >= 1")
    frames: list[bytes] = []
    try:
        with Image.open(path) as image:
            frame_count = int(getattr(image, "n_frames", 1))
            for index in _frame_indices(frame_count, max_frames):
                try:
                    image.seek(index)
                    frames.append(_encode_frame(image))
                except EOFError:
                    break
    except _FRAME_ERRORS as exc:
        logger.warning("frame sampling failed path=%s reason=%s", path, exc)
    return frames


def _frame_indices(frame_count: int, max_frames: int) -> list[int]:
    """Frame positions to sample: short → first/middle/last, long → even spacing."""
    if frame_count <= SHORT_GIF_MAX_FRAMES:
        if frame_count <= 1:
            return [0]
        return sorted({0, frame_count // 2, frame_count - 1})
    if max_frames >= frame_count:
        return list(range(frame_count))
    if max_frames == 1:
        return [0]
    step = (frame_count - 1) / (max_frames - 1)
    return sorted({round(index * step) for index in range(max_frames)})


def _encode_frame(image: Image.Image) -> bytes:
    """Encode the current frame as PNG bytes (RGBA keeps GIF transparency)."""
    buffer = io.BytesIO()
    image.convert("RGBA").save(buffer, format="PNG")
    return buffer.getvalue()
