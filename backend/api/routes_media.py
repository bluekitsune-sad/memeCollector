"""Library API — gallery listing, detail, edits, favorites, files (PRD §25–§28, §50).

Thin HTTP over :mod:`backend.media.library` / :mod:`backend.media.duplicates`
(no business logic here — PRD §57 stage separation). All handlers are async and
use the short-lived transactions of the shared WAL connection held on
``app.state``.

Security (PRD §41): file endpoints map ``{id}`` → the row's stored path and
additionally require that path to resolve **inside** the configured storage
directory; no path component ever comes from the client, so traversal is
impossible by construction (``/api/media/../../etc/passwd/file`` cannot parse
as an int id and is rejected with 422).
"""

from __future__ import annotations

import json
import logging
import mimetypes
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.ai.provider import AIProviderError, create_provider
from backend.ai.queue import process_single_media
from backend.api.schemas import parse_json_list
from backend.database.database import transaction
from backend.media.duplicates import unflag
from backend.media.library import (
    MediaFilters,
    count_by_dup_status,
    count_by_processing_status,
    count_media,
    get_media,
    list_media,
    remove_media,
)
from backend.search.keyword import index_media

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/media", tags=["media"])

#: ``media.type`` values (extensions the collector accepts, PRD §5.3).
TypeFilter = Literal["image", "gif", "video"]

#: PRD §19 processing states.
ProcessingFilter = Literal["DOWNLOADED", "ANALYZING", "ANALYZED", "EMBEDDING", "READY", "FAILED"]

#: PRD §12.1 dup flags.
DupFilter = Literal["dup", "nondup", "unflagged"]

#: ``/random`` scopes (PRD §30).
RandomScope = Literal["everything", "favorites", "gifs"]

#: Which stored file each file endpoint serves → (media column, storage setting).
_SERVE_TARGETS: dict[str, tuple[str, str]] = {
    "file": ("file_path", "media_directory"),
    "thumbnail": ("thumbnail_path", "thumbnail_directory"),
    "preview": ("preview_path", "preview_directory"),
}

#: Allowed PATCH fields → media columns (whitelisted — never interpolate input).
_PATCH_COLUMNS: dict[str, str] = {
    "title": "title",
    "user_description": "user_description",
    "user_tags": "user_tags",
    "is_favorite": "is_favorite",
}


# ---------------------------------------------------------------------------
# Response/request models
# ---------------------------------------------------------------------------


class MediaSourceOut(BaseModel):
    """One provenance row (PRD §31): where this media was collected from."""

    id: int
    media_id: int
    site: str
    page_url: str
    chapter: str | None = None
    page_number: int | None = None
    comment_id: str | None = None
    media_url: str
    author_name: str | None = None
    collected_at: str


class AiMetadataOut(BaseModel):
    """AI analysis record (PRD §15); absent until Milestone 3 runs."""

    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    emotions: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    meme_context: str | None = None
    suggested_search_phrases: list[str] = Field(default_factory=list)
    ai_provider: str | None = None
    model: str | None = None
    processed_at: str | None = None


class MediaOut(BaseModel):
    """One gallery card (list item). Storage paths are deliberately not exposed."""

    id: int
    title: str | None = None
    original_filename: str | None = None
    mime_type: str | None = None
    extension: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    site: str | None = None
    processing_status: str
    dup_status: str
    dup_flagged_at: str | None = None
    dup_expires_at: str | None = None
    is_favorite: bool
    created_at: str


class MediaDetailOut(MediaOut):
    """Detail page payload (PRD §26, §31): provenance + AI + dup + manual overrides."""

    sha256: str | None = None
    phash: str | None = None
    dup_of_media_id: int | None = None
    user_description: str | None = None
    user_tags: list[str] = Field(default_factory=list)
    sources: list[MediaSourceOut] = Field(default_factory=list)
    ai_metadata: AiMetadataOut | None = None


class MediaListResponse(BaseModel):
    """Paginated gallery page + counts for the header/filter chips (PRD §19, §50)."""

    items: list[MediaOut]
    page: int
    page_size: int
    total: int
    dup_counts: dict[str, int]
    status_counts: dict[str, int] = Field(
        description="PRD §19 strip: ready / processing (DOWNLOADED|ANALYZING|ANALYZED|"
        "EMBEDDING) / failed — the three always sum to total"
    )


class MediaUpdateRequest(BaseModel):
    """Manual overrides (PRD §27): only fields present in the body are applied."""

    title: str | None = Field(None, max_length=300)
    user_description: str | None = Field(None, max_length=5000)
    user_tags: list[str] | None = Field(None, max_length=200)
    is_favorite: bool | None = None


class MediaDeleteResponse(BaseModel):
    id: int
    deleted: bool


# ---------------------------------------------------------------------------
# Routes — declaration order matters: /random before /{media_id}.
# ---------------------------------------------------------------------------


@router.get("", response_model=MediaListResponse)
async def media_list(
    request: Request,
    media_type: TypeFilter | None = Query(None, alias="type"),
    site: str | None = Query(None, description="exact source hostname"),
    media_format: str | None = Query(None, alias="format", description="extension e.g. png"),
    chapter: str | None = Query(None),
    date_from: date | None = Query(None, description="inclusive, YYYY-MM-DD"),
    date_to: date | None = Query(None, description="inclusive, YYYY-MM-DD"),
    processing_status: ProcessingFilter | None = Query(None),
    dup_status: DupFilter | None = Query(None),
    is_favorite: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> MediaListResponse:
    """One filtered, newest-first page plus totals and the dup-status breakdown."""
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
    )
    conn = request.app.state.db
    return MediaListResponse(
        items=list_media(conn, filters, page=page, page_size=page_size),
        page=page,
        page_size=page_size,
        total=count_media(conn, filters),
        dup_counts=count_by_dup_status(conn, filters),
        status_counts=count_by_processing_status(conn, filters),
    )


@router.get("/random", response_model=MediaDetailOut)
async def media_random(
    request: Request,
    scope: RandomScope = Query("everything"),
) -> MediaDetailOut:
    """Pick one random item (PRD §30): ``everything``, ``favorites`` or ``gifs``."""
    conditions = {
        "everything": "",
        "favorites": "WHERE m.is_favorite = 1",
        "gifs": "WHERE LOWER(m.extension) = 'gif'",
    }
    row = request.app.state.db.execute(
        f"SELECT m.id FROM media m {conditions[scope]} ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no media matches scope {scope!r}")
    return _require_media(request, int(row["id"]))


@router.get("/{media_id}", response_model=MediaDetailOut)
async def media_detail(request: Request, media_id: int) -> MediaDetailOut:
    """Full record: media fields, source provenance, AI metadata, dup info."""
    return _require_media(request, media_id)


@router.patch("/{media_id}", response_model=MediaDetailOut)
async def media_update(
    request: Request, media_id: int, payload: MediaUpdateRequest
) -> MediaDetailOut:
    """Apply manual overrides (title/description/tags/favorite — PRD §27)."""
    _require_media(request, media_id)
    updates = payload.model_dump(exclude_unset=True)
    if updates:
        assignments: list[str] = []
        params: list[object] = []
        for field, value in updates.items():
            column = _PATCH_COLUMNS[field]
            if field == "user_tags" and value is not None:
                value = json.dumps(value, ensure_ascii=False)
            if field == "is_favorite" and value is not None:
                value = int(value)
            assignments.append(f"{column} = ?")
            params.append(value)
        params.append(media_id)
        with transaction(request.app.state.db):
            request.app.state.db.execute(
                f"UPDATE media SET {', '.join(assignments)} WHERE id = ?", params
            )
        logger.info("media updated media_id=%d fields=%s",
                    media_id, ",".join(updates))
        index_media(request.app.state.db, media_id)
    return _require_media(request, media_id)


@router.delete("/{media_id}", response_model=MediaDeleteResponse)
async def media_delete(request: Request, media_id: int) -> MediaDeleteResponse:
    """Remove files + rows for one item (children cascade; files best effort)."""
    removed = remove_media(
        request.app.state.db, media_id, settings=request.app.state.settings
    )
    if not removed:
        raise HTTPException(status_code=404, detail=f"media {media_id} not found")
    return MediaDeleteResponse(id=media_id, deleted=True)


@router.post("/{media_id}/unflag-dup", response_model=MediaDetailOut)
async def media_unflag_dup(request: Request, media_id: int) -> MediaDetailOut:
    """User action: ``dup`` → ``unflagged`` (PRD §12.1 rule 3 — never re-flagged)."""
    _require_media(request, media_id)
    if not unflag(request.app.state.db, media_id):
        raise HTTPException(
            status_code=409,
            detail=f"media {media_id} is not flagged dup — only dup items can be unflagged",
        )
    return _require_media(request, media_id)


@router.post("/{media_id}/reanalyze", response_model=MediaDetailOut)
async def media_reanalyze(request: Request, media_id: int) -> MediaDetailOut:
    """Retry AI analysis once for one item (PRD §36 — "Retry").

    Only ``FAILED`` (a recorded per-item error, including videos the MVP cannot
    analyze) and ``READY`` (re-embed after a model change) items may be retried;
    anything else is a ``400`` because a live queue may already own the row.
    Unknown ids are ``404``; a provider that cannot be built (e.g. ``openrouter``
    without a key) answers ``503`` with the configuration hint instead of a 500.
    """
    record = _require_media(request, media_id)
    if record.processing_status not in ("FAILED", "READY"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"media {media_id} is {record.processing_status} — "
                "reanalyze is only allowed for FAILED or READY items"
            ),
        )
    settings = request.app.state.settings
    try:
        provider = create_provider(settings)
    except AIProviderError as exc:
        raise HTTPException(status_code=503, detail=f"AI provider unavailable: {exc}") from exc
    try:
        result = await process_single_media(request.app.state.db, media_id, provider, settings)
    finally:
        await provider.aclose()
    index_media(request.app.state.db, media_id)
    logger.info("reanalyze finished media_id=%d status=%s error=%s",
                media_id, result.status, result.error)
    return _require_media(request, media_id)


@router.get("/{media_id}/file", response_class=FileResponse)
async def media_file(request: Request, media_id: int) -> FileResponse:
    """Serve the original file with its verified MIME type (PRD §26)."""
    return await _serve(request, media_id, "file")


@router.get("/{media_id}/thumbnail", response_class=FileResponse)
async def media_thumbnail(request: Request, media_id: int) -> FileResponse:
    """Serve the 256 px WebP thumbnail (404 until generated)."""
    return await _serve(request, media_id, "thumbnail")


@router.get("/{media_id}/preview", response_class=FileResponse)
async def media_preview(request: Request, media_id: int) -> FileResponse:
    """Serve the 1024 px WebP preview (404 until generated)."""
    return await _serve(request, media_id, "preview")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_media(request: Request, media_id: int) -> MediaDetailOut:
    """Fetch a detail record or raise 404; shapes JSON list columns."""
    record = get_media(request.app.state.db, media_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"media {media_id} not found")
    record["user_tags"] = parse_json_list(record.get("user_tags"))
    ai_record = record.get("ai_metadata")
    if ai_record is not None:
        for key in ("tags", "emotions", "subjects", "suggested_search_phrases"):
            ai_record[key] = parse_json_list(ai_record.get(key))
    return MediaDetailOut.model_validate(record)


async def _serve(request: Request, media_id: int, target: str) -> FileResponse:
    """Resolve ``media_id`` → stored path → containment-checked FileResponse.

    Returns 404 for a missing item, a missing/unset path, or a path outside its
    configured storage directory (PRD §41 — a corrupted row must not leak files).
    """
    column, directory_name = _SERVE_TARGETS[target]
    settings = request.app.state.settings
    directory: Path = getattr(settings.storage, directory_name)
    row = request.app.state.db.execute(
        f"SELECT {column}, mime_type FROM media WHERE id = ?", (media_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"media {media_id} not found")
    stored = row[column]
    if not stored:
        raise HTTPException(
            status_code=404, detail=f"media {media_id} has no {target} yet"
        )
    path = Path(stored).resolve()
    if not path.is_file() or not path.is_relative_to(directory.resolve()):
        logger.warning("refusing to serve unsafe/missing path target=%s media_id=%d path=%s",
                       target, media_id, stored)
        raise HTTPException(status_code=404, detail=f"{target} for media {media_id} not found")
    media_type = (
        "image/webp"
        if target != "file"
        else (row["mime_type"] or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    )
    return FileResponse(path, media_type=media_type)
