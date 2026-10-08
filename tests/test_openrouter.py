"""OpenRouter provider tests (M3.2) — httpx.MockTransport only, no network, no key.

Also proves the API key never reaches the logs (PRD §41 / AGENTS.md §9).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import httpx
import pytest

from backend.ai import AIResponseError, AITimeoutError, AIUnavailableError, OpenRouterProvider
from backend.ai.openrouter import CHAT_COMPLETIONS_URL, EMBEDDINGS_URL
from backend.config import Settings, load_settings

TEST_API_KEY = "sk-or-SECRET-KEY-MUST-NEVER-LOG"

VALID_ANALYSIS: dict[str, Any] = {
    "description": "A cartoon character looks shocked and turns to the viewer.",
    "tags": ["shocked", "confused", "reaction"],
    "emotions": ["surprise", "confusion"],
    "subjects": ["cartoon character"],
    "meme_context": "reaction meme",
    "suggested_search_phrases": ["shocked reaction", "confused reaction"],
}

Handler = Callable[[httpx.Request], httpx.Response]


def make_ai_settings(**ai_overrides: Any) -> Settings:
    """OpenRouter settings with a test key and instant (0s) retry backoff."""
    settings = load_settings()
    values: dict[str, Any] = {
        "provider": "openrouter",
        "api_key": TEST_API_KEY,
        "retry_backoff_seconds": 0.0,
        "retry_attempts": 3,
        **ai_overrides,
    }
    return replace(settings, ai=replace(settings.ai, **values))


def chat_ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@asynccontextmanager
async def provider_for(handler: Handler, **ai_overrides: Any) -> AsyncIterator[OpenRouterProvider]:
    """OpenRouterProvider wired to a MockTransport handler; client closed on exit."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        yield OpenRouterProvider(make_ai_settings(**ai_overrides).ai, client=client)
    finally:
        await client.aclose()


def decode_data_uri(data_uri: str) -> bytes:
    prefix, _, encoded = data_uri.partition(";base64,")
    assert prefix.startswith("data:")
    return base64.b64decode(encoded)


# ---------------------------------------------------------------------------
# Vision analysis (chat completions)
# ---------------------------------------------------------------------------


async def test_analyze_image_success_parses_and_sends_vision_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return chat_ok(json.dumps(VALID_ANALYSIS))

    async with provider_for(handler) as provider:
        result = await provider.analyze_image(b"png-bytes-here", mime_type="image/png")

    assert result == VALID_ANALYSIS
    request = seen[0]
    assert str(request.url) == CHAT_COMPLETIONS_URL
    assert request.headers["authorization"] == f"Bearer {TEST_API_KEY}"
    body = json.loads(request.content)
    assert body["model"] == provider.model
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    parts = body["messages"][1]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert decode_data_uri(parts[1]["image_url"]["url"]) == b"png-bytes-here"


async def test_response_format_400_falls_back_to_prompt_only_json() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return chat_ok(json.dumps(VALID_ANALYSIS))

    async with provider_for(handler) as provider:
        result = await provider.analyze_image(b"img", mime_type="image/png")

    assert result == VALID_ANALYSIS
    assert len(bodies) == 2
    assert "response_format" in bodies[0]
    assert "response_format" not in bodies[1]


async def test_analyze_gif_sends_one_part_per_frame() -> None:
    frames = [b"frame-one", b"frame-two", b"frame-three"]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        parts = body["messages"][1]["content"]
        assert parts[0]["type"] == "text"
        image_parts = [part for part in parts[1:] if part["type"] == "image_url"]
        assert [decode_data_uri(part["image_url"]["url"]) for part in image_parts] == frames
        return chat_ok(json.dumps(VALID_ANALYSIS))

    async with provider_for(handler) as provider:
        result = await provider.analyze_gif(frames)

    assert result == VALID_ANALYSIS


@pytest.mark.parametrize(
    "content",
    ["sorry, no json today", '{"description": "trailing comma",', "[1, 2, 3]"],
)
async def test_malformed_or_wrong_shape_json_raises_ai_response_error(content: str) -> None:
    async with provider_for(lambda request: chat_ok(content)) as provider:
        with pytest.raises(AIResponseError):
            await provider.analyze_image(b"img", mime_type="image/png")


@pytest.mark.parametrize(
    "wrap",
    [
        lambda text: f"```json\n{text}\n```",
        lambda text: f"Sure! Here it is: {text} hope that helps",
    ],
)
async def test_json_repairs_are_applied(wrap: Callable[[str], str]) -> None:
    async with provider_for(lambda request: chat_ok(wrap(json.dumps(VALID_ANALYSIS)))) as provider:
        result = await provider.analyze_image(b"img", mime_type="image/png")
    assert result == VALID_ANALYSIS


async def test_missing_fields_are_defaulted_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    async with provider_for(lambda request: chat_ok('{"description": "sparse"}')) as provider:
        result = await provider.analyze_image(b"img", mime_type="image/png")
    assert result["description"] == "sparse"
    assert result["tags"] == []
    assert "analysis field missing" in caplog.text


# ---------------------------------------------------------------------------
# Retries, timeouts, and error mapping
# ---------------------------------------------------------------------------


async def test_429_retries_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return chat_ok(json.dumps(VALID_ANALYSIS))

    async with provider_for(handler) as provider:
        result = await provider.analyze_image(b"img", mime_type="image/png")
    assert result == VALID_ANALYSIS
    assert len(calls) == 2


async def test_timeout_raises_ai_timeout_error_after_retries() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("read timed out")

    async with provider_for(handler, retry_attempts=2) as provider:
        with pytest.raises(AITimeoutError, match="timed out"):
            await provider.analyze_image(b"img", mime_type="image/png")
    assert len(calls) == 2


async def test_transport_error_raises_ai_unavailable_after_retries() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    async with provider_for(handler, retry_attempts=2) as provider:
        with pytest.raises(AIUnavailableError, match="unreachable"):
            await provider.analyze_image(b"img", mime_type="image/png")
    assert len(calls) == 2


async def test_5xx_exhausts_retries_with_unavailable_error() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    async with provider_for(handler, retry_attempts=2) as provider:
        with pytest.raises(AIUnavailableError, match="HTTP 500"):
            await provider.analyze_image(b"img", mime_type="image/png")
    assert len(calls) == 2


async def test_401_fails_immediately_with_key_guidance() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401)

    async with provider_for(handler) as provider:
        with pytest.raises(AIUnavailableError, match="OPENROUTER_API_KEY"):
            await provider.analyze_image(b"img", mime_type="image/png")
    assert len(calls) == 1


async def test_api_key_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == EMBEDDINGS_URL:
            return httpx.Response(200, json={"data": [{"embedding": [0.5, 0.5]}]})
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})  # exercise backoff logging
        return chat_ok(json.dumps(VALID_ANALYSIS))

    async with provider_for(handler) as provider:
        await provider.analyze_image(b"img", mime_type="image/png")
        await provider.analyze_gif([b"frame"])
        await provider.generate_embedding("confused reaction")

    assert TEST_API_KEY not in caplog.text
    for record in caplog.records:
        assert TEST_API_KEY not in record.getMessage()


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


async def test_embeddings_success_parses_vector_and_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, -0.2, 0.3]}]})

    async with provider_for(handler) as provider:
        vector = await provider.generate_embedding("confused reaction")

    assert vector == [0.1, -0.2, 0.3]
    request = seen[0]
    assert str(request.url) == EMBEDDINGS_URL
    body = json.loads(request.content)
    assert body["model"] == provider.embedding_model
    assert body["input"] == "confused reaction"


async def test_embeddings_http_error_raises_actionable_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "no such model"}})

    async with provider_for(handler) as provider:
        with pytest.raises(AIUnavailableError) as excinfo:
            await provider.generate_embedding("text")
    message = str(excinfo.value)
    assert "embeddings" in message
    assert "embedding_model" in message


@pytest.mark.parametrize(
    "body",
    [{}, {"data": []}, {"data": [{"embedding": []}]}, {"data": [{"embedding": ["x"]}]}],
)
async def test_embeddings_malformed_shape_raises_ai_response_error(body: dict[str, Any]) -> None:
    async with provider_for(lambda request: httpx.Response(200, json=body)) as provider:
        with pytest.raises(AIResponseError):
            await provider.generate_embedding("text")
