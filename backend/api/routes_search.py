"""Search API — ``GET /api/search`` (PRD §21–§24, §52).

Thin HTTP over :func:`backend.search.hybrid.search`; the response shape is the
documented contract the frontend is coded against and must not drift:

``{items[], page, page_size, total, mode, weights}`` where every item is the
**same record** ``GET /api/media`` returns (built through
:func:`backend.media.library.shape_record` and validated as
:class:`~backend.api.routes_media.MediaOut`) plus ``description``, ``tags``
and ``score`` (``null`` in ``filters_only`` mode).

Query params mirror the gallery filters exactly (same validation → the same
``422``s) plus ``q`` (may be empty) and ``emotion`` (ai_metadata membership,
json1). ``mode`` reports how the run was ranked: ``hybrid``, ``keyword_only``
(no provider/key/embeddings — see :mod:`backend.search.hybrid`) or
``filters_only`` (empty query).

The query embedding comes from the configured provider; ``openrouter`` without
a key degrades to ``keyword_only`` instead of failing (PRD §36), so the
endpoint works offline with ``MEME_AI_PROVIDER=mock`` and with no key at all.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from backend.ai.provider import AIProviderError, VisionProvider, create_provider
from backend.api.routes_media import DupFilter, MediaOut, ProcessingFilter, TypeFilter
from backend.api.schemas import parse_json_list
from backend.media.library import MediaFilters, shape_record
from backend.search.hybrid import search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/search", tags=["search"])

#: Ranking modes reported in the response (PRD §21–§22).
SearchMode = Literal["hybrid", "keyword_only", "filters_only"]


class SearchItemOut(MediaOut):
    """Gallery card plus the ranking payload the search view renders."""

    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    score: float | None = None


class SearchWeightsOut(BaseModel):
    """Effective hybrid weights for this run (they always sum to 1.0)."""

    keyword: float
    semantic: float
    tag: float
    metadata: float


class SearchResponse(BaseModel):
    """``GET /api/search`` envelope — the exact contract the frontend expects."""

    items: list[SearchItemOut]
    page: int
    page_size: int
    total: int
    mode: SearchMode
    weights: SearchWeightsOut


@router.get("", response_model=SearchResponse)
async def media_search(
    request: Request,
    q: str = Query("", max_length=500, description="free text; empty → filters_only"),
    media_type: TypeFilter | None = Query(None, alias="type"),
    site: str | None = Query(None, description="exact source hostname"),
    media_format: str | None = Query(None, alias="format", description="extension e.g. png"),
    emotion: str | None = Query(None, max_length=100, description="ai_metadata emotion, case-insensitive"),
    chapter: str | None = Query(None),
    date_from: date | None = Query(None, description="inclusive, YYYY-MM-DD"),
    date_to: date | None = Query(None, description="inclusive, YYYY-MM-DD"),
    processing_status: ProcessingFilter | None = Query(None),
    dup_status: DupFilter | None = Query(None),
    is_favorite: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> SearchResponse:
    """Ranked, filtered, paginated search results (PRD §21–§24)."""
    filters = MediaFilters(
        type=media_type,
        site=site,
        format=media_format,
        chapter=chapter,
        date_from=str(date_from) if date_from else None,
        date_to=str(date_to) if date_to else None,
        processing_status=processing_status,
        dup_status=dup_status,
        is_favorite=is_favorite,
        emotion=emotion,
    )
    conn = request.app.state.db
    settings = request.app.state.settings
    provider: VisionProvider | None = None
    if q.strip():
        provider = _create_provider(settings)
    try:
        results = await search(conn, q, filters, settings, provider=provider)
    finally:
        if provider is not None:
            await _close_provider(provider)
    offset = (page - 1) * page_size
    page_hits = results.hits[offset : offset + page_size]
    records = _fetch_records(conn, [hit.media_id for hit in page_hits])
    items = [
        SearchItemOut.model_validate({**records[hit.media_id], "score": hit.score})
        for hit in page_hits
        if hit.media_id in records
    ]
    return SearchResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=results.total,
        mode=results.mode,
        weights=SearchWeightsOut(**results.weights),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _create_provider(settings: Any) -> VisionProvider | None:
    """Provider for the query embedding; ``None`` → the run degrades to keyword_only."""
    try:
        return create_provider(settings)
    except AIProviderError as exc:
        logger.info("search without embeddings reason=%s", exc)
        return None


async def _close_provider(provider: VisionProvider) -> None:
    """Release provider resources; a close failure must not fail the response."""
    try:
        await provider.aclose()
    except Exception as exc:
        logger.warning("search provider close failed error=%s", exc)


def _fetch_records(conn: Any, media_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Shaped records + AI description/tags for one page of ids, keyed by id.

    Mirrors ``list_media``'s site subquery so search items are byte-for-byte
    consistent with gallery items; ids that vanished between ranking and fetch
    are simply absent (a concurrent delete never breaks a search — PRD §36).
    """
    if not media_ids:
        return {}
    placeholders = ",".join("?" for _ in media_ids)
    rows = conn.execute(
        "SELECT m.*, "
        "(SELECT s.site FROM source s WHERE s.media_id = m.id ORDER BY s.id LIMIT 1) AS site, "
        "a.description AS ai_description, a.tags AS ai_tags "
        "FROM media m LEFT JOIN ai_metadata a ON a.media_id = m.id "
        f"WHERE m.id IN ({placeholders})",
        media_ids,
    ).fetchall()
    records: dict[int, dict[str, Any]] = {}
    for row in rows:
        record = shape_record(row)
        record["description"] = row["ai_description"]
        record["tags"] = parse_json_list(row["ai_tags"])
        records[int(row["id"])] = record
    return records
