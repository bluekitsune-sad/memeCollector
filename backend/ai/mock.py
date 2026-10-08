"""Deterministic offline mock vision provider (AGENTS.md §2, PRD §52).

Used by the test suite and by demo/offline runs (``ai.provider: mock``) so the
full pipeline — analysis, statuses, embeddings — works with no API key and no
network access. Outputs are hash-seeded: identical input bytes always produce
identical analysis JSON, and different inputs diverge, making tests reproducible
while still exercising per-item differences.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from backend.ai.provider import VisionProvider

logger = logging.getLogger(__name__)

#: Default embedding dimension for the mock (matches the size of typical
#: production embedding models in spirit; configurable via ``ai.mock_embedding_dim``).
DEFAULT_EMBEDDING_DIM = 384

_TAG_VOCABULARY = (
    "reaction",
    "confused",
    "shocked",
    "funny",
    "angry",
    "sad",
    "happy",
    "cartoon",
    "stare",
    "cat",
)
_EMOTION_VOCABULARY = ("confusion", "surprise", "joy", "anger", "sadness", "awkwardness")
_SUBJECT_VOCABULARY = ("cartoon character", "person", "animal", "text overlay")


def _pick(vocabulary: tuple[str, ...], digest: bytes, count: int) -> list[str]:
    """Pick up to ``count`` unique vocabulary entries, seeded by ``digest`` bytes."""
    picks: list[str] = []
    for byte in digest:
        candidate = vocabulary[byte % len(vocabulary)]
        if candidate not in picks:
            picks.append(candidate)
        if len(picks) == count:
            break
    return picks


class MockVisionProvider(VisionProvider):
    """Hash-seeded stand-in for a cloud vision provider — offline and deterministic."""

    def __init__(self, *, embedding_dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        if embedding_dim < 2:
            raise ValueError("embedding_dim must be >= 2")
        self._embedding_dim = embedding_dim

    @property
    def name(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return "mock-vision"

    @property
    def embedding_model(self) -> str:
        return f"mock/{self._embedding_dim}"

    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        """PRD §15 JSON seeded from the image bytes (content and size both feed the hash)."""
        digest = hashlib.sha256(mime_type.encode("utf-8") + image_bytes).digest()
        return self._build_analysis(digest, kind=f"image mime={mime_type} size={len(image_bytes)}")

    async def analyze_gif(self, frames: list[bytes]) -> dict[str, Any]:
        """PRD §15 JSON seeded from the concatenated sampled frames + frame count."""
        digest = hashlib.sha256(len(frames).to_bytes(4, "big") + b"".join(frames)).digest()
        return self._build_analysis(digest, kind=f"GIF frames={len(frames)}")

    async def generate_embedding(self, text: str) -> list[float]:
        """Deterministic pseudo-random **unit** vector of the configured dimension."""
        raw = hashlib.shake_256(text.encode("utf-8")).digest(self._embedding_dim * 4)
        values = [
            int.from_bytes(raw[offset : offset + 4], "big") / float(0xFFFFFFFF) * 2.0 - 1.0
            for offset in range(0, self._embedding_dim * 4, 4)
        ]
        norm = sum(value * value for value in values) ** 0.5
        if norm == 0.0:  # astronomically unlikely, but keep the unit-norm contract exact
            values[0] = 1.0
            return values
        return [value / norm for value in values]

    @staticmethod
    def _build_analysis(digest: bytes, *, kind: str) -> dict[str, Any]:
        short_hash = digest.hex()[:8]
        tags = _pick(_TAG_VOCABULARY, digest, count=4)
        emotions = _pick(_EMOTION_VOCABULARY, digest[4:], count=2)
        subjects = _pick(_SUBJECT_VOCABULARY, digest[8:], count=1)
        return {
            "description": f"Mock {kind} analysis hash={short_hash}",
            "tags": tags,
            "emotions": emotions,
            "subjects": subjects,
            "meme_context": "reaction meme",
            "suggested_search_phrases": [f"{tags[0]} reaction", f"mock meme {short_hash}"],
        }
