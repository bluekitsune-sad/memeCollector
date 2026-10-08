"""AI status API — live background-analysis state (PRD §18, §19, §35, §36).

``GET /api/ai/status`` reports the :class:`backend.ai.supervisor.AISupervisor`
state machine (``processing`` / ``on_hold`` / ``idle`` / ``unavailable`` /
``stopped``) plus everything the Jobs UI's live card needs: provider/model
provenance, ``key_present`` (never the key itself — PRD §41), the retry
countdown while items are held for backoff, the last recorded failure, and the
counters of the newest ``ai_analysis`` job. The payload is a plain dict from
``supervisor.status`` validated against an exact pydantic contract.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.ai.supervisor import latest_ai_job, utc_now_text

router = APIRouter(prefix="/api/ai", tags=["ai"])


class AiJobStatus(BaseModel):
    """Counters of the newest ``ai_analysis`` job (``null`` before any run)."""

    id: int
    done: int
    total: int
    ready: int
    failed: int
    deferred: int


class AiStatusOut(BaseModel):
    """Exact ``GET /api/ai/status`` response contract — never carries the API key."""

    state: str
    reason: str | None = None
    provider: str
    model: str
    embedding_model: str
    key_present: bool
    retry_in_seconds: int | None = None
    next_retry_at: str | None = None
    last_error: str | None = None
    job: AiJobStatus | None = None
    updated_at: str


@router.get("/status", response_model=AiStatusOut)
async def ai_status(request: Request) -> AiStatusOut:
    """Live state of the background AI supervisor and its latest queue run."""
    supervisor = getattr(request.app.state, "ai_supervisor", None)
    if supervisor is not None:
        return AiStatusOut(**supervisor.status)
    # Bare app (no lifespan supervisor): same shape, state `stopped`, job history intact.
    settings = request.app.state.settings
    ai = settings.ai
    job, last_error = latest_ai_job(request.app.state.db)
    return AiStatusOut(
        state="stopped",
        provider=ai.provider,
        model=ai.model,
        embedding_model=ai.embedding_model,
        key_present=bool(ai.api_key),
        last_error=last_error,
        job=job,
        updated_at=utc_now_text(),
    )
