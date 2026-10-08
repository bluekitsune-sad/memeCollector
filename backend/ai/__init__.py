"""AI provider interface, OpenRouter client, mock provider, and analysis queue (PRD §16, §18)."""

from backend.ai.frames import sample_frames
from backend.ai.mock import MockVisionProvider
from backend.ai.openrouter import OpenRouterProvider
from backend.ai.provider import (
    AIProviderError,
    AIResponseError,
    AITimeoutError,
    AIUnavailableError,
    VisionProvider,
    create_provider,
    normalize_analysis,
)
from backend.ai.queue import (
    AIQueueController,
    AIQueueSummary,
    ProcessResult,
    deserialize_embedding,
    process_single_media,
    run_ai_queue,
    serialize_embedding,
)

__all__ = [
    "AIProviderError",
    "AIQueueController",
    "AIQueueSummary",
    "AIResponseError",
    "AITimeoutError",
    "AIUnavailableError",
    "MockVisionProvider",
    "OpenRouterProvider",
    "ProcessResult",
    "VisionProvider",
    "create_provider",
    "deserialize_embedding",
    "normalize_analysis",
    "process_single_media",
    "run_ai_queue",
    "sample_frames",
    "serialize_embedding",
]
