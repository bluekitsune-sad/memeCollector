"""Deterministic mock AI provider for tests (PRD §16).

Conforms to the interface shape that ``backend/ai/provider.py`` will define in
Milestone 3: ``analyze_image``, ``analyze_gif``, ``generate_embedding``. No
network access and no API key are required — identical inputs always produce
identical outputs, so tests are reproducible and the full suite runs offline
(AGENTS.md §8).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

EMBEDDING_DIMENSION = 64

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


def _pick_vocabulary(vocabulary: tuple[str, ...], digest: bytes, count: int) -> list[str]:
    """Deterministically pick up to ``count`` unique vocabulary entries from digest bytes."""
    picks: list[str] = []
    for byte in digest:
        candidate = vocabulary[byte % len(vocabulary)]
        if candidate not in picks:
            picks.append(candidate)
        if len(picks) == count:
            break
    return picks


class MockVisionProvider:
    """Deterministic stand-in for the OpenRouter vision provider."""

    def analyze_image(self, image: bytes) -> dict[str, Any]:
        """Return PRD §15 analysis JSON derived from a SHA-256 of the image bytes."""
        digest = hashlib.sha256(image).digest()
        return self._build_analysis(digest, kind_label="image")

    def analyze_gif(self, frames: Sequence[bytes]) -> dict[str, Any]:
        """Return PRD §15 analysis JSON derived from a SHA-256 of the concatenated frames."""
        digest = hashlib.sha256(b"".join(frames)).digest()
        return self._build_analysis(digest, kind_label=f"{len(frames)}-frame GIF")

    def generate_embedding(self, text: str) -> list[float]:
        """Return a deterministic unit-ish vector for ``text`` (values in [-1, 1])."""
        raw = hashlib.shake_256(text.encode("utf-8")).digest(EMBEDDING_DIMENSION * 4)
        return [
            int.from_bytes(raw[offset : offset + 4], "big") / float(0xFFFFFFFF) * 2.0 - 1.0
            for offset in range(0, EMBEDDING_DIMENSION * 4, 4)
        ]

    @staticmethod
    def _build_analysis(digest: bytes, kind_label: str) -> dict[str, Any]:
        short_hash = digest.hex()[:8]
        tags = _pick_vocabulary(_TAG_VOCABULARY, digest, count=4)
        emotions = _pick_vocabulary(_EMOTION_VOCABULARY, digest[4:], count=2)
        subjects = _pick_vocabulary(_SUBJECT_VOCABULARY, digest[8:], count=1)
        return {
            "description": f"Mock {kind_label} analysis hash={short_hash}",
            "tags": tags,
            "emotions": emotions,
            "subjects": subjects,
            "meme_context": "reaction meme",
            "suggested_search_phrases": [
                f"{tags[0]} reaction",
                f"mock meme {short_hash}",
            ],
        }
