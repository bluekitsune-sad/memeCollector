"""OpenRouter vision provider: OpenAI-compatible chat completions + embeddings (PRD §0, §16).

Endpoints (OpenAI-compatible):

* vision/analysis — ``POST https://openrouter.ai/api/v1/chat/completions`` with
  ``response_format: {"type": "json_object"}``; image content is sent as
  ``image_url`` parts carrying base64 data URIs. If a model rejects
  ``response_format`` (HTTP 400 mentioning it), the request is retried once
  without it, relying on the strict JSON-only system prompt; the response text
  is then extracted with one local repair pass (code-fence stripping, then a
  first-``{``…last-``}`` substring) before parsing — anything still malformed
  raises :class:`~backend.ai.provider.AIResponseError`.
* embeddings — ``POST https://openrouter.ai/api/v1/embeddings``. **Support
  reality (checked 2026-10):** OpenRouter does expose an OpenAI-compatible
  embeddings router; availability depends on the configured model being an
  embeddings-enabled model (listable via ``GET /api/v1/embeddings/models``).
  If the endpoint/model is rejected, :meth:`OpenRouterProvider.generate_embedding`
  raises :class:`~backend.ai.provider.AIUnavailableError` with an actionable
  message instead of silently skipping semantic indexing.

Retries: HTTP 429/5xx and transport/timeout failures are retried with
exponential backoff (:mod:`backend.scraper.backoff`, same math as the crawler)
up to ``settings.ai.retry_attempts``; other 4xx fail immediately with a mapped
:class:`~backend.ai.provider.AIProviderError`. Exhausted transient failures are
raised with ``retryable=True`` and model-response problems (reasoning-only
answers, provider ``error`` bodies, malformed JSON) likewise, so the AI queue
can defer the item and try again later; permanent errors (bad key, unknown
model, unsupported embeddings model, corrupt stored metadata) stay
``retryable=False``. Concurrency is deliberately NOT handled here — the AI
queue bounds it with ``settings.ai.ai_concurrency``.

Security/privacy (PRD §41–42): the API key is read from ``settings.ai.api_key``
(env only) and travels solely in the ``Authorization`` header of outgoing
requests — it is never logged, never included in exception messages, and never
written to the database. Only the media bytes being analyzed are sent to the
cloud, and only when this provider is enabled. Untrusted strings that reach the
model (the reported mime type, embedding input) are sanitized
(:mod:`backend.security.text`) and quoted as data via
:func:`backend.security.prompt.wrap_untrusted_text`, while the system prompt
stays a fixed constant — prompt injection in crawled metadata cannot become an
instruction.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from backend.ai.provider import (
    AIResponseError,
    AITimeoutError,
    AIUnavailableError,
    VisionProvider,
    normalize_analysis,
)
from backend.config.loader import AISettings
from backend.scraper.backoff import backoff_delay, clamp_retry_after, parse_retry_after
from backend.security.prompt import wrap_untrusted_text
from backend.security.text import sanitize_text

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"

#: JSON-only system prompt: schema per PRD §15, no markdown, no commentary.
ANALYSIS_SYSTEM_PROMPT = (
    "You are a meme-analysis engine for a personal image search index. "
    "Reply with ONLY a single JSON object — no markdown, no code fences, no commentary — "
    "containing exactly these keys: "
    '{"description": string, "tags": string[], "emotions": string[], "subjects": string[], '
    '"meme_context": string, "suggested_search_phrases": string[]}. '
    "description: one concise sentence about the image. "
    "tags: 3-8 lowercase keywords (emotion, subject, style). "
    "emotions: emotions visibly expressed. "
    "subjects: the main subjects. "
    'meme_context: a short label such as "reaction meme" or "reaction GIF". '
    "suggested_search_phrases: 2-4 natural-language queries this media should answer in search."
)


class OpenRouterProvider(VisionProvider):
    """PRD §16 provider backed by the OpenRouter chat-completions/embeddings APIs."""

    def __init__(self, settings: AISettings, *, client: httpx.AsyncClient | None = None) -> None:
        if not settings.api_key:
            raise AIUnavailableError(
                "AI provider 'openrouter' requires an API key — set OPENROUTER_API_KEY in .env "
                "(copy .env.example) or switch ai.provider to 'mock' for offline mode"
            )
        self._settings = settings
        self._api_key: str = settings.api_key
        self._client = client
        self._owns_client = client is None

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def embedding_model(self) -> str:
        return self._settings.embedding_model

    async def analyze_image(self, image_bytes: bytes, *, mime_type: str) -> dict[str, Any]:
        # ``mime_type`` derives from stored (attacker-influenceable) metadata: it is
        # sanitized for the data URI and quoted as untrusted data in the prompt so
        # instruction-like content in it cannot steer the model (AGENTS.md §9).
        safe_mime = sanitize_text(mime_type)
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Analyze this comment-section image. Its reported content type "
                    f"is quoted here as data:\n{wrap_untrusted_text(safe_mime)}"
                ),
            },
            _image_part(image_bytes, safe_mime),
        ]
        return await self._analyze(content)

    async def analyze_gif(self, frames: list[bytes]) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"These are {len(frames)} frames sampled from ONE animated GIF, in temporal order. "
                    "Describe the overall animation across frames, not a single frame."
                ),
            }
        ]
        content.extend(_image_part(frame, "image/png") for frame in frames)
        return await self._analyze(content)

    async def generate_embedding(self, text: str) -> list[float]:
        """Embed ``text`` via OpenRouter's embeddings router (see module docstring for support reality).

        The input is untrusted text (filenames/metadata from crawled pages), so
        it is sanitized before it leaves the machine.
        """
        payload = {"model": self._settings.embedding_model, "input": sanitize_text(text)}
        response = await self._post(EMBEDDINGS_URL, payload, endpoint="embeddings")
        if response.status_code >= 400:
            raise AIUnavailableError(
                f"OpenRouter embeddings request failed (HTTP {response.status_code}) for model "
                f"{self._settings.embedding_model!r} — OpenRouter's embeddings router only serves "
                "embeddings-enabled models (list: GET https://openrouter.ai/api/v1/embeddings/models); "
                "set ai.embedding_model to one in config.yaml, or use ai.provider=mock for offline runs"
            )
        try:
            body = response.json()
            raw = body["data"][0]["embedding"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIResponseError(f"unexpected embeddings response shape: {type(exc).__name__}") from exc
        if not isinstance(raw, list) or not raw:
            raise AIResponseError("embeddings response contains an empty vector")
        if not all(isinstance(value, (int, float)) for value in raw):
            raise AIResponseError("embeddings response contains non-numeric values")
        return [float(value) for value in raw]

    async def aclose(self) -> None:
        """Close the HTTP client only when this provider created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # -- internals ---------------------------------------------------------

    async def _analyze(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        text = await self._chat_json(content)
        try:
            return normalize_analysis(_extract_json(text))
        except AIResponseError as exc:
            if exc.retryable:
                raise
            # A wrong-shape/wrong-type model answer can come out right next attempt.
            raise AIResponseError(str(exc), retryable=True) from exc

    async def _chat_json(self, content: list[dict[str, Any]]) -> str:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
        }
        response = await self._post(CHAT_COMPLETIONS_URL, payload, endpoint="chat")
        if response.status_code == 400 and "response_format" in response.text.lower():
            logger.info(
                "model rejected response_format — retrying with prompt-only JSON enforcement model=%s",
                self._settings.model,
            )
            payload.pop("response_format")
            response = await self._post(CHAT_COMPLETIONS_URL, payload, endpoint="chat")
        if response.status_code >= 400:
            raise _http_error(response, endpoint="chat", model=self._settings.model)
        return _message_content(response)

    async def _post(self, url: str, payload: dict[str, Any], *, endpoint: str) -> httpx.Response:
        """POST with exponential backoff on 429/5xx/timeouts; non-retryable 4xx returned as-is."""
        client = self._ensure_client()
        attempts = max(1, self._settings.retry_attempts)
        base_delay = self._settings.retry_backoff_seconds
        last_error: AIProviderError | None = None
        retry_after: float | None = None
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        for attempt in range(attempts):
            if attempt:
                wait = clamp_retry_after(retry_after, backoff_delay(base_delay, attempt - 1))
                logger.info(
                    "openrouter backoff endpoint=%s wait=%.1fs attempt=%d/%d",
                    endpoint, wait, attempt + 1, attempts,
                )
                await asyncio.sleep(wait)
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = AITimeoutError(
                    f"OpenRouter {endpoint} request timed out "
                    f"(timeout={self._settings.timeout_seconds}s, attempts={attempt + 1})",
                    retryable=True,
                )
                logger.warning("openrouter timeout endpoint=%s attempt=%d/%d", endpoint, attempt + 1, attempts)
                continue
            except httpx.HTTPError as exc:
                last_error = AIUnavailableError(
                    f"OpenRouter {endpoint} unreachable: {type(exc).__name__} (attempts={attempt + 1})",
                    retryable=True,
                )
                logger.warning(
                    "openrouter transport error endpoint=%s reason=%s attempt=%d/%d",
                    endpoint, type(exc).__name__, attempt + 1, attempts,
                )
                continue
            status = response.status_code
            if 200 <= status < 300:
                return response
            if status == 429 or status >= 500:
                last_error = AIUnavailableError(f"OpenRouter {endpoint} HTTP {status}", retryable=True)
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                logger.warning(
                    "openrouter transient failure endpoint=%s status=%d attempt=%d/%d",
                    endpoint, status, attempt + 1, attempts,
                )
                continue
            return response  # non-retryable 4xx — the caller maps it to a typed error
        if last_error is None:  # unreachable: the loop always sets it before the final continue
            last_error = AIUnavailableError(f"OpenRouter {endpoint} request failed", retryable=True)
        raise last_error

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._settings.timeout_seconds))
        return self._client


def _image_part(image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    """OpenAI-style vision content part carrying the image as a base64 data URI."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}


def _http_error(response: httpx.Response, *, endpoint: str, model: str) -> AIUnavailableError:
    """Map a non-retryable chat HTTP error to an actionable :class:`AIUnavailableError`.

    Only 429/5xx-class outcomes (which reach here only if retries were
    exhausted) are marked ``retryable``; 401/403/404/other 4xx need a
    configuration fix and stay permanent.
    """
    status = response.status_code
    retryable = status == 429 or status >= 500
    if status in (401, 403):
        return AIUnavailableError(
            f"OpenRouter rejected the API key (HTTP {status}) — check OPENROUTER_API_KEY in .env"
        )
    if status == 404:
        return AIUnavailableError(
            f"OpenRouter {endpoint} model not found (HTTP 404) — check ai.model={model!r} in config.yaml"
        )
    return AIUnavailableError(
        f"OpenRouter {endpoint} request failed (HTTP {status})", retryable=retryable
    )


def _model_reasoning(body: Any) -> bool:
    """True when ``choices[0].message`` carries ``reasoning``/``reasoning_content`` only."""
    try:
        message = body["choices"][0]["message"]
        return bool(message.get("reasoning") or message.get("reasoning_content"))
    except (AttributeError, IndexError, KeyError, TypeError):
        return False


def _message_content(response: httpx.Response) -> str:
    """Extract ``choices[0].message.content`` from a chat-completions response.

    Raises a **retryable** :class:`AIResponseError` for a body carrying an
    ``error`` object (``"provider error: …"``), a reasoning-only message with
    no content, empty content, or a shape the OpenAI schema does not explain —
    the free thinking-models answer these differently from run to run, so the
    queue defers the item instead of failing it permanently.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise AIResponseError(
            f"unexpected chat completion response shape: {type(exc).__name__}", retryable=True
        ) from exc
    if isinstance(body, dict) and "error" in body:
        error = body["error"]
        detail = error.get("message", error) if isinstance(error, dict) else error
        raise AIResponseError(f"provider error: {detail}", retryable=True)
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        if _model_reasoning(body):
            raise AIResponseError(
                "model returned only reasoning, no content", retryable=True
            ) from exc
        raise AIResponseError(
            f"unexpected chat completion response shape: {type(exc).__name__}", retryable=True
        ) from exc
    if not isinstance(content, str) or not content.strip():
        if _model_reasoning(body):
            raise AIResponseError("model returned only reasoning, no content", retryable=True)
        raise AIResponseError("chat completion returned empty message content", retryable=True)
    return content


def _extract_json(text: str) -> Any:
    """Parse ``text`` as JSON; one repair pass tries fences, then the outermost braces."""
    candidates = [text]
    fenced = _strip_fences(text)
    if fenced is not None:
        candidates.append(fenced)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise AIResponseError(f"malformed JSON in AI response: {text[:160]!r}", retryable=True)


def _strip_fences(text: str) -> str | None:
    """Remove a surrounding ```/```json code fence, if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return None
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1])
    return None
