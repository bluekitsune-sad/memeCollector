"""VisionProvider interface + registry tests (M3.1, M3.3) — offline, no network.

Covers the production mock (deterministic, PRD §15-shaped), the provider
registry/factory errors, and ``normalize_analysis`` validation rules.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest

from backend.ai import (
    AIProviderError,
    AIResponseError,
    AIUnavailableError,
    MockVisionProvider,
    OpenRouterProvider,
    VisionProvider,
    create_provider,
    normalize_analysis,
)
from backend.config import Settings, load_settings

#: Exact key set of the PRD §15 analysis object.
PRD_15_KEYS = {
    "description",
    "tags",
    "emotions",
    "subjects",
    "meme_context",
    "suggested_search_phrases",
}

VALID_ANALYSIS: dict[str, Any] = {
    "description": "A cartoon character looks shocked.",
    "tags": ["shocked", "reaction"],
    "emotions": ["surprise"],
    "subjects": ["cartoon character"],
    "meme_context": "reaction meme",
    "suggested_search_phrases": ["shocked reaction"],
}


def make_settings(**ai_overrides: Any) -> Settings:
    """Settings with offline-safe AI defaults; explicit ``api_key`` beats any ambient .env."""
    settings = load_settings()
    values: dict[str, Any] = {
        "provider": "mock",
        "api_key": None,
        "retry_backoff_seconds": 0.0,
        **ai_overrides,
    }
    return replace(settings, ai=replace(settings.ai, **values))


# ---------------------------------------------------------------------------
# Mock provider (M3.3)
# ---------------------------------------------------------------------------


async def test_mock_is_a_vision_provider_with_provenance() -> None:
    provider = create_provider(make_settings(provider="mock"))
    assert isinstance(provider, VisionProvider)
    assert provider.name == "mock"
    assert provider.model == "mock-vision"
    assert provider.embedding_model.startswith("mock/")


async def test_mock_analysis_matches_prd_15_shape() -> None:
    provider = MockVisionProvider()
    analysis = await provider.analyze_image(b"fake-image-bytes", mime_type="image/png")
    assert set(analysis) == PRD_15_KEYS
    assert isinstance(analysis["description"], str) and analysis["description"]
    assert isinstance(analysis["meme_context"], str) and analysis["meme_context"]
    for key in ("tags", "emotions", "subjects", "suggested_search_phrases"):
        assert isinstance(analysis[key], list) and analysis[key]
        assert all(isinstance(item, str) for item in analysis[key])


async def test_mock_analysis_is_deterministic_and_input_sensitive() -> None:
    provider = MockVisionProvider()
    first = await provider.analyze_image(b"image-a", mime_type="image/png")
    again = await provider.analyze_image(b"image-a", mime_type="image/png")
    other = await provider.analyze_image(b"image-b", mime_type="image/png")
    assert first == again
    assert first != other


async def test_mock_gif_analysis_reports_frame_count() -> None:
    provider = MockVisionProvider()
    three = await provider.analyze_gif([b"f1", b"f2", b"f3"])
    two = await provider.analyze_gif([b"f1", b"f2"])
    assert set(three) == PRD_15_KEYS
    assert "frames=3" in three["description"]
    assert three != two


async def test_mock_embedding_is_unit_vector_of_configured_dim() -> None:
    provider = MockVisionProvider(embedding_dim=32)
    vector = await provider.generate_embedding("confused reaction")
    other = await provider.generate_embedding("totally different query")
    again = await provider.generate_embedding("confused reaction")
    assert len(vector) == 32
    assert vector == again
    assert vector != other
    assert all(isinstance(value, float) for value in vector)
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-9


def test_mock_embedding_dim_is_validated() -> None:
    with pytest.raises(ValueError, match="embedding_dim"):
        MockVisionProvider(embedding_dim=1)


async def test_create_provider_honours_configured_mock_dimension() -> None:
    provider = create_provider(make_settings(provider="mock", mock_embedding_dim=16))
    assert isinstance(provider, MockVisionProvider)
    assert len(await provider.generate_embedding("dim check")) == 16
    assert provider.embedding_model == "mock/16"


# ---------------------------------------------------------------------------
# Registry / factory (M3.1)
# ---------------------------------------------------------------------------


def test_create_provider_unknown_name_raises_clear_error() -> None:
    with pytest.raises(AIProviderError, match="banana"):
        create_provider(make_settings(provider="banana"))


def test_create_provider_openrouter_without_key_raises_actionable_error() -> None:
    with pytest.raises(AIUnavailableError) as excinfo:
        create_provider(make_settings(provider="openrouter", api_key=None))
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_create_provider_openrouter_with_key_builds_provider() -> None:
    provider = create_provider(
        make_settings(provider="openrouter", api_key="sk-or-test", model="acme/vision")
    )
    assert isinstance(provider, OpenRouterProvider)
    assert provider.name == "openrouter"
    assert provider.model == "acme/vision"
    assert provider.embedding_model  # provenance for embeddings.embedding_model


# ---------------------------------------------------------------------------
# normalize_analysis validation (M3.1)
# ---------------------------------------------------------------------------


def test_normalize_analysis_passes_valid_shape_through() -> None:
    assert normalize_analysis(VALID_ANALYSIS) == VALID_ANALYSIS


def test_normalize_analysis_fills_missing_fields_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    result = normalize_analysis({"description": "only a description"})
    assert result["description"] == "only a description"
    assert result["tags"] == []
    assert result["emotions"] == []
    assert result["subjects"] == []
    assert result["meme_context"] == ""
    assert result["suggested_search_phrases"] == []
    assert "analysis field missing" in caplog.text


@pytest.mark.parametrize(
    "bad_raw",
    [
        ["not", "an", "object"],
        {**VALID_ANALYSIS, "description": 42},
        {**VALID_ANALYSIS, "tags": "shocked"},
        {**VALID_ANALYSIS, "tags": ["ok", 7]},
        {**VALID_ANALYSIS, "meme_context": ["reaction meme"]},
    ],
)
def test_normalize_analysis_rejects_wrong_types(bad_raw: Any) -> None:
    with pytest.raises(AIResponseError):
        normalize_analysis(bad_raw)
