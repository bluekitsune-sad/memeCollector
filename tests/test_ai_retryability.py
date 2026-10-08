"""Retry-classification tests — which AI failures defer an item and which fail it (PRD §18, §36).

Everything runs through ``httpx.MockTransport`` (no network, no key): the
OpenRouter provider maps HTTP/transport/model outcomes onto the
``AIProviderError`` hierarchy and each mapping is asserted here on its
``retryable`` flag, because that flag is the only thing deciding between
*defer with backoff* (``backend.ai.queue``) and *fail the item now*.

Classification table under test:

==============================================  =========================  ==========
failure                                         raised                     retryable
==============================================  =========================  ==========
HTTP 429 (retries exhausted)                    AIUnavailableError         yes
HTTP 5xx (retries exhausted)                    AIUnavailableError         yes
request timeout (retries exhausted)             AITimeoutError             yes
transport error (retries exhausted)             AIUnavailableError         yes
HTTP 401 / 403 bad key                          AIUnavailableError         no
HTTP 404 unknown model                          AIUnavailableError         no
other 4xx                                       AIUnavailableError         no
200 body carrying an ``error`` object           AIResponseError            yes
reasoning-only answer, no content               AIResponseError            yes
malformed JSON / wrong-shape analysis           AIResponseError            yes
embeddings wrong shape                          AIResponseError            no
embeddings HTTP 4xx                             AIUnavailableError         no
missing API key / unknown provider              AI*Error at construction   no
==============================================  =========================  ==========
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import httpx
import pytest

from backend.ai import AIResponseError, AITimeoutError, AIUnavailableError, OpenRouterProvider
from backend.ai.provider import AIProviderError, create_provider
from backend.config import Settings, load_settings

TEST_API_KEY = "sk-or-SECRET-KEY-MUST-NEVER-LOG"

VALID_ANALYSIS: dict[str, Any] = {
    "description": "A cartoon character looks shocked.",
    "tags": ["shocked", "reaction"],
    "emotions": ["surprise"],
    "subjects": ["cartoon character"],
    "meme_context": "reaction meme",
    "suggested_search_phrases": ["shocked reaction"],
}

Handler = Any  # Callable[[httpx.Request], httpx.Response]


def make_ai_settings(**ai_overrides: Any) -> Settings:
    """OpenRouter settings with a test key and instant (0s) retry backoff."""
    settings = load_settings()
    values: dict[str, Any] = {
        "provider": "openrouter",
        "api_key": TEST_API_KEY,
        "retry_backoff_seconds": 0.0,
        "retry_attempts": 2,
        **ai_overrides,
    }
    return replace(settings, ai=replace(settings.ai, **values))


def chat_ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@asynccontextmanager
async def provider_for(
    handler: Handler, **ai_overrides: Any
) -> AsyncIterator[OpenRouterProvider]:
    """OpenRouterProvider wired to a MockTransport handler; client closed on exit."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        yield OpenRouterProvider(make_ai_settings(**ai_overrides).ai, client=client)
    finally:
        await client.aclose()


async def analyze_fails(provider: OpenRouterProvider) -> AIProviderError:
    """Run one analysis and return the (guaranteed) provider error."""
    with pytest.raises(AIProviderError) as excinfo:
        await provider.analyze_image(b"img", mime_type="image/png")
    return excinfo.value


# ---------------------------------------------------------------------------
# Transient HTTP → retryable
# ---------------------------------------------------------------------------


async def test_429_exhaustion_is_retryable(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"})

    caplog.set_level(logging.INFO)
    async with provider_for(handler) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AIUnavailableError)
    assert error.retryable is True
    assert len(calls) == 2  # retry_attempts=2
    assert "openrouter backoff" in caplog.text  # the backoff path really ran


async def test_5xx_exhaustion_is_retryable() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async with provider_for(handler) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AIUnavailableError)
    assert error.retryable is True
    assert len(calls) == 2


async def test_timeout_exhaustion_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    async with provider_for(handler) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AITimeoutError)
    assert error.retryable is True


async def test_transport_error_exhaustion_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with provider_for(handler) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AIUnavailableError)
    assert error.retryable is True
    assert "unreachable" in str(error)


async def test_retry_after_header_governs_the_backoff_wait() -> None:
    """A 429 ``Retry-After`` is honored (the wait is at least that many seconds)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1"})

    started = time.monotonic()
    async with provider_for(handler, retry_attempts=2) as provider:
        error = await analyze_fails(provider)
    elapsed = time.monotonic() - started

    assert error.retryable is True
    assert elapsed >= 0.9  # second attempt waited out Retry-After: 1


# ---------------------------------------------------------------------------
# Permanent HTTP → not retryable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_auth_and_model_4xx_are_not_retryable(status: int) -> None:
    async with provider_for(lambda request: httpx.Response(status)) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AIUnavailableError)
    assert error.retryable is False


async def test_other_4xx_is_not_retryable() -> None:
    """A plain 400 (not the response_format rejection) fails the item immediately."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request body")

    async with provider_for(handler) as provider:
        error = await analyze_fails(provider)

    assert error.retryable is False
    assert "HTTP 400" in str(error)


async def test_response_format_400_still_falls_back_and_succeeds() -> None:
    """The documented one-shot fallback is not classified as a failure."""
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


# ---------------------------------------------------------------------------
# Model-answer problems → retryable
# ---------------------------------------------------------------------------


async def test_error_object_body_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "model overloaded"}})

    async with provider_for(handler) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AIResponseError)
    assert error.retryable is True
    assert "provider error: model overloaded" in str(error)


async def test_reasoning_only_answer_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"reasoning": "thinking…", "content": None}}]},
        )

    async with provider_for(handler) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AIResponseError)
    assert error.retryable is True
    assert "reasoning" in str(error)


async def test_malformed_json_answer_is_retryable() -> None:
    async with provider_for(lambda request: chat_ok("sorry, no json today")) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AIResponseError)
    assert error.retryable is True
    assert "malformed JSON" in str(error)


async def test_wrong_shape_analysis_is_retryable() -> None:
    """`normalize_analysis` rejects the type; the provider re-raises it as retryable."""
    async with provider_for(lambda request: chat_ok('{"tags": "not-a-list"}')) as provider:
        error = await analyze_fails(provider)

    assert isinstance(error, AIResponseError)
    assert error.retryable is True
    assert "tags" in str(error)


@pytest.mark.parametrize(
    "body",
    [{}, {"data": []}, {"data": [{"embedding": []}]}, {"data": [{"embedding": ["x"]}]}],
)
async def test_embeddings_malformed_shape_is_not_retryable(body: dict[str, Any]) -> None:
    """A wrong-shape embeddings answer is deterministic (model config), not flaky."""
    async with provider_for(lambda request: httpx.Response(200, json=body)) as provider:
        with pytest.raises(AIResponseError) as excinfo:
            await provider.generate_embedding("text")

    assert excinfo.value.retryable is False


async def test_embeddings_5xx_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with provider_for(handler) as provider:
        with pytest.raises(AIUnavailableError) as excinfo:
            await provider.generate_embedding("text")

    assert excinfo.value.retryable is True


async def test_embeddings_4xx_is_not_retryable() -> None:
    """A wrong/unsupported embedding model is a config fix, not a retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "no such model"}})

    async with provider_for(handler) as provider:
        with pytest.raises(AIUnavailableError) as excinfo:
            await provider.generate_embedding("text")

    assert excinfo.value.retryable is False
    assert "embedding_model" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Construction-time failures and the base classes
# ---------------------------------------------------------------------------


def test_missing_api_key_is_not_retryable() -> None:
    ai = replace(load_settings().ai, provider="openrouter", api_key=None)

    with pytest.raises(AIUnavailableError) as excinfo:
        OpenRouterProvider(ai)
    assert excinfo.value.retryable is False

    with pytest.raises(AIUnavailableError) as keyed:
        create_provider(replace(load_settings(), ai=ai))
    assert keyed.value.retryable is False


def test_unknown_provider_is_not_retryable() -> None:
    ai = replace(load_settings().ai, provider="definitely-not-a-provider")

    with pytest.raises(AIProviderError) as excinfo:
        create_provider(replace(load_settings(), ai=ai))
    assert excinfo.value.retryable is False


def test_error_hierarchy_defaults() -> None:
    """The base error is permanent; timeouts are retryable unless told otherwise."""
    assert AIProviderError("boom").retryable is False
    assert AIResponseError("boom").retryable is False
    assert AIUnavailableError("boom").retryable is False
    assert AITimeoutError("boom").retryable is True
    assert AITimeoutError("boom", retryable=False).retryable is False
    assert issubclass(AITimeoutError, AIProviderError)


async def test_auth_failure_message_never_carries_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 401 message is recorded per PRD §36 — the key must not ride along (PRD §41)."""
    caplog.set_level(logging.DEBUG)
    async with provider_for(lambda request: httpx.Response(401)) as provider:
        error = await analyze_fails(provider)

    assert TEST_API_KEY not in str(error)
    assert TEST_API_KEY not in caplog.text
    assert "OPENROUTER_API_KEY" in str(error)  # actionable guidance stays
