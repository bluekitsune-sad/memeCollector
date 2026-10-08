"""Replaceable AI vision-provider interface (PRD §16) + registry and output validation (PRD §15).

``VisionProvider`` is the single seam between the rest of the application and any
AI backend: the OpenRouter provider (:mod:`backend.ai.openrouter`) and the
deterministic offline mock (:mod:`backend.ai.mock`) implement it, and
:func:`create_provider` builds the instance named in ``settings.ai.provider``.
Swapping providers later (local AI, another cloud) requires no changes outside
this package (PRD §16, §57).

All provider failures derive from :class:`AIProviderError` so callers can record
them per PRD §36 without string-matching messages:

* :class:`AITimeoutError` — no answer within ``settings.ai.timeout_seconds``
  (retryable by default);
* :class:`AIResponseError` — the provider answered, but the JSON is malformed
  or has the wrong shape for PRD §15;
* :class:`AIUnavailableError` — the provider is unreachable, unauthorized, or
  rejected the request (including "no API key configured").

Every error carries ``retryable``: transient failures (rate limits, 5xx,
timeouts, reasoning-only/malformed model answers) are marked
``retryable=True`` so the queue can defer the item and try again later
(:mod:`backend.ai.queue`), while permanent/local problems (bad API key,
unsupported media, corrupt stored metadata) stay ``retryable=False`` and fail
immediately.

:func:`normalize_analysis` validates raw provider JSON into the exact PRD §15
shape: missing/``None`` fields fall back to defaults with a warning log, wrong
types raise :class:`AIResponseError`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from backend.config import Settings

logger = logging.getLogger(__name__)

#: Fields that must hold a JSON array of strings (PRD §15).
_LIST_KEYS: tuple[str, ...] = ("tags", "emotions", "subjects", "suggested_search_phrases")

#: Fields that must hold a JSON string (PRD §15).
_STRING_KEYS: tuple[str, ...] = ("description", "meme_context")


class AIProviderError(Exception):
    """Base class for every AI provider failure (PRD §36, §52).

    ``retryable`` marks failures another attempt may fix (rate limits, 5xx,
    timeouts, a flaky model answer); the AI queue defers retryable items with
    exponential backoff instead of failing them permanently.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class AITimeoutError(AIProviderError):
    """The provider did not answer within the configured timeout."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message, retryable=retryable)


class AIResponseError(AIProviderError):
    """The provider answered with malformed JSON or an unusable response shape."""


class AIUnavailableError(AIProviderError):
    """The provider is unreachable, rejected the API key, or refused the request."""


class VisionProvider(ABC):
    """PRD §16 interface: describe media and embed text.

    Implementations must be safe to call from an async queue: methods are
    declared ``async`` even when the work is synchronous (the mock), so callers
    never need to branch on the implementation.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier stored in ``ai_metadata.ai_provider`` (e.g. ``"openrouter"``)."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Vision model identifier stored in ``ai_metadata.model`` for provenance."""

    @property
    @abstractmethod
    def embedding_model(self) -> str:
        """Embedding model identifier stored in ``embeddings.embedding_model``."""

    @abstractmethod
    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        """Return PRD §15 analysis JSON for one still image."""

    @abstractmethod
    async def analyze_gif(self, frames: list[bytes]) -> dict[str, Any]:
        """Return PRD §15 analysis JSON describing the whole animation from sampled frames."""

    @abstractmethod
    async def generate_embedding(self, text: str) -> list[float]:
        """Return the embedding vector for ``text`` (dimension is provider-specific)."""

    async def aclose(self) -> None:
        """Release resources the provider created itself (HTTP clients). Default: nothing."""


def normalize_analysis(raw: Any) -> dict[str, Any]:
    """Validate ``raw`` provider JSON into the exact PRD §15 analysis shape.

    Missing or ``None`` fields become defaults (``""`` / ``[]``) with a warning
    log so one sloppy model response never fails an item; fields of the wrong
    type raise :class:`AIResponseError` because silently coercing them would
    corrupt search metadata.
    """
    if not isinstance(raw, dict):
        raise AIResponseError(f"analysis JSON must be an object, got {type(raw).__name__}")
    result: dict[str, Any] = {}
    for key in _STRING_KEYS:
        value = raw.get(key)
        if value is None:
            logger.warning("analysis field missing key=%s — using default", key)
            result[key] = ""
        elif not isinstance(value, str):
            raise AIResponseError(f"analysis field {key!r} must be a string, got {type(value).__name__}")
        else:
            result[key] = value
    for key in _LIST_KEYS:
        value = raw.get(key)
        if value is None:
            logger.warning("analysis field missing key=%s — using default", key)
            result[key] = []
        elif not isinstance(value, list):
            raise AIResponseError(f"analysis field {key!r} must be an array, got {type(value).__name__}")
        elif not all(isinstance(item, str) for item in value):
            raise AIResponseError(f"analysis field {key!r} must contain only strings")
        else:
            result[key] = value
    return result


def create_provider(settings: Settings) -> VisionProvider:
    """Build the provider named by ``settings.ai.provider`` (``"openrouter"`` or ``"mock"``).

    Raises :class:`AIUnavailableError` when OpenRouter is selected but no API key
    is configured (the message tells the user to set ``OPENROUTER_API_KEY`` in
    ``.env``) and :class:`AIProviderError` for an unknown provider name. The
    caller records the error — a missing key must never crash startup (PRD §36).

    Implementation modules are imported lazily so this module stays free of
    HTTP/Pillow dependencies and import-order cycles.
    """
    ai = settings.ai
    provider_name = ai.provider.strip().lower()
    if provider_name == "mock":
        from backend.ai.mock import MockVisionProvider

        return MockVisionProvider(embedding_dim=ai.mock_embedding_dim)
    if provider_name == "openrouter":
        if not ai.api_key:
            raise AIUnavailableError(
                "AI provider 'openrouter' requires an API key — set OPENROUTER_API_KEY in .env "
                "(copy .env.example) or switch ai.provider to 'mock' for offline mode"
            )
        from backend.ai.openrouter import OpenRouterProvider

        return OpenRouterProvider(ai)
    raise AIProviderError(
        f"unknown AI provider {ai.provider!r} in settings ai.provider — expected 'openrouter' or 'mock'"
    )
