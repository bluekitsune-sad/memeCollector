"""Level-2 duplicate detection primitives: SHA-256 and image metadata (PRD §12).

``sha256_file`` is the exact-duplicate hash used by the download pipeline and
(Milestone 2) by the ``media.sha256`` unique column. ``compute_image_meta``
extracts dimensions/format/animation duration via Pillow and is exception-safe:
a malformed or unreadable file yields an all-``None`` :class:`ImageMeta`
instead of raising, so one bad file never stops a batch (PRD §36).

Perceptual hashing (Level 3, phash) is deliberately not implemented here —
it belongs to a later milestone (PRD §12 Level 3).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

#: Read chunk size for hashing large files without loading them into memory.
_HASH_CHUNK_SIZE = 1024 * 1024

#: Pillow exceptions that mean "this file is not a usable image" (malformed/truncated).
_IMAGE_ERRORS = (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError)


def sha256_file(path: Path | str, *, chunk_size: int = _HASH_CHUNK_SIZE) -> str:
    """Return the hex SHA-256 digest of ``path``, read in ``chunk_size`` chunks.

    Raises ``OSError`` if the file cannot be read and ``ValueError`` on a
    non-positive ``chunk_size``.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ImageMeta:
    """Basic image metadata (PRD §12/§20): ``duration`` is animation length in seconds."""

    width: int | None = None
    height: int | None = None
    format: str | None = None
    duration: float | None = None


def compute_image_meta(path: Path | str) -> ImageMeta:
    """Read width/height/format (and total animation duration) with Pillow.

    Never raises for unreadable content: malformed files, non-images, and
    unreadable paths return an all-``None`` :class:`ImageMeta` and log a debug line.
    """
    try:
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
            duration: float | None = None
            if getattr(image, "n_frames", 1) > 1:
                total_ms = 0
                for frame_index in range(image.n_frames):
                    image.seek(frame_index)
                    total_ms += int(image.info.get("duration", 0) or 0)
                duration = total_ms / 1000.0
            return ImageMeta(width=width, height=height, format=image_format, duration=duration)
    except _IMAGE_ERRORS as exc:
        logger.debug("image metadata unavailable path=%s reason=%s", path, exc)
        return ImageMeta()
