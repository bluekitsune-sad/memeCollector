"""Deterministic mock AI provider tests (M0.4): shape per PRD §15, reproducible outputs."""

from __future__ import annotations

from tests.mock_provider import EMBEDDING_DIMENSION, MockVisionProvider

ANALYSIS_KEYS = {
    "description",
    "tags",
    "emotions",
    "subjects",
    "meme_context",
    "suggested_search_phrases",
}


def test_analyze_image_is_deterministic(mock_vision_provider: MockVisionProvider) -> None:
    first = mock_vision_provider.analyze_image(b"fake-image-bytes")
    second = mock_vision_provider.analyze_image(b"fake-image-bytes")
    assert first == second
    assert set(first) == ANALYSIS_KEYS
    assert isinstance(first["description"], str) and first["description"]
    assert isinstance(first["tags"], list) and first["tags"]
    assert isinstance(first["emotions"], list) and first["emotions"]


def test_analyze_image_differs_for_different_images(
    mock_vision_provider: MockVisionProvider,
) -> None:
    first = mock_vision_provider.analyze_image(b"image-a")
    second = mock_vision_provider.analyze_image(b"image-b")
    assert first != second


def test_analyze_gif_reports_frame_count(mock_vision_provider: MockVisionProvider) -> None:
    result = mock_vision_provider.analyze_gif([b"frame-1", b"frame-2", b"frame-3"])
    assert set(result) == ANALYSIS_KEYS
    assert "3-frame GIF" in result["description"]


def test_generate_embedding_deterministic_and_sized(
    mock_vision_provider: MockVisionProvider,
) -> None:
    first = mock_vision_provider.generate_embedding("confused reaction")
    second = mock_vision_provider.generate_embedding("confused reaction")
    other = mock_vision_provider.generate_embedding("totally different query")
    assert first == second
    assert first != other
    assert len(first) == EMBEDDING_DIMENSION
    assert all(isinstance(value, float) for value in first)
    assert all(-1.0 <= value <= 1.0 for value in first)
