"""Hashing tests (M1.10) — SHA-256 correctness and exception-safe image metadata (PRD §12)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.media.hashing import ImageMeta, compute_image_meta, sha256_file

EXPECTED_FORMATS = {"png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP"}


def test_sha256_matches_hashlib_reference(sample_images: dict[str, Path]) -> None:
    for path in sample_images.values():
        data = path.read_bytes()
        assert sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_sha256_chunked_reads_agree(sample_images: dict[str, Path]) -> None:
    path = sample_images["gif"]
    reference = hashlib.sha256(path.read_bytes()).hexdigest()
    assert sha256_file(path, chunk_size=7) == reference
    assert sha256_file(path, chunk_size=1024 * 1024) == reference


def test_sha256_rejects_bad_chunk_size(sample_images: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        sha256_file(sample_images["png"], chunk_size=0)


def test_sha256_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        sha256_file(tmp_path / "does-not-exist.png")


def test_image_meta_for_static_formats(sample_images: dict[str, Path]) -> None:
    for key, path in sample_images.items():
        meta = compute_image_meta(path)
        assert meta.width == 32
        assert meta.height == 32
        assert meta.format == EXPECTED_FORMATS[key]
        if key == "gif":
            # 3 frames x 120ms from the make_image fixture.
            assert meta.duration == pytest.approx(0.36)
        else:
            assert meta.duration is None


def test_malformed_file_returns_empty_meta(tmp_path: Path) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a real image at all")
    assert compute_image_meta(broken) == ImageMeta()


def test_missing_file_returns_empty_meta(tmp_path: Path) -> None:
    assert compute_image_meta(tmp_path / "nope.webp") == ImageMeta()
