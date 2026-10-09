"""Watch API — manage watched comics and trigger a targeted scan (PRD §39, §5.1).

Endpoints (prefix ``/api/watch``):

* ``GET  /api/watch`` — every watched comic (oldest first).
* ``POST /api/watch`` — validate the URL against the adapter registry and
  upsert it (unknown site → ``400`` with the same "Site not supported: add an
  adapter for …" message as ``/api/scrape``, PRD §0 / AGENTS.md §5). Existing
  rows keep their ``enabled``/``title`` state (idempotent, ``200``; new → ``201``).
* ``DELETE /api/watch/{id}`` — stop watching a comic.
* ``PATCH /api/watch/{id}`` — enable/disable the passive pass for one comic.
* ``POST /api/watch/{id}/scan`` — **targeted fetch**: immediately start the
  watch crawl for that one comic (``202`` + its ``job_id``) — no full scan, and
  the passive supervisor skips while it runs because the handle is live on
  ``app.state.running_crawls``.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.api.routes_scraper import ScrapeStarted
from backend.jobs.pipeline import start_crawl
from backend.jobs.watch import build_watch_crawl
from backend.jobs.watched_comics import (
    delete_watched_comic,
    ensure_watched_comic,
    get_watched_comic,
    list_watched_comics,
    set_watched_enabled,
)
from backend.scraper.adapters import UnsupportedSiteError, get_adapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watch", tags=["watch"])


class WatchedComicOut(BaseModel):
    """One watched comic row — the passive scanner's list entry."""

    id: int
    url: str
    site: str
    title: str | None = None
    enabled: bool
    last_scanned_at: str | None = None
    created_at: str


class WatchListResponse(BaseModel):
    """``GET /api/watch`` envelope."""

    items: list[WatchedComicOut]


class WatchCreate(BaseModel):
    """Body for ``POST /api/watch``."""

    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=500)


class WatchUpdate(BaseModel):
    """Body for ``PATCH /api/watch/{id}`` — only ``enabled`` is mutable."""

    enabled: bool


class WatchDeleteResponse(BaseModel):
    """``DELETE /api/watch/{id}`` result."""

    id: int
    deleted: bool


def _comic_out(row: sqlite3.Row) -> WatchedComicOut:
    """Map a ``watched_comics`` row to the response model (SQLite 0/1 → bool)."""
    return WatchedComicOut(
        id=int(row["id"]),
        url=str(row["url"]),
        site=str(row["site"]),
        title=row["title"],
        enabled=bool(row["enabled"]),
        last_scanned_at=row["last_scanned_at"],
        created_at=str(row["created_at"]),
    )


@router.get("", response_model=WatchListResponse)
async def watch_list(request: Request) -> WatchListResponse:
    """Every watched comic, oldest first (what the passive pass iterates)."""
    return WatchListResponse(items=[_comic_out(row) for row in list_watched_comics(request.app.state.db)])


@router.post("", response_model=WatchedComicOut, status_code=201)
async def watch_add(request: Request, payload: WatchCreate, response: Response) -> WatchedComicOut:
    """Register a comic to watch — validated against the adapter registry (PRD §0)."""
    try:
        adapter = get_adapter(payload.url)
    except UnsupportedSiteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    comic_id, created = ensure_watched_comic(
        request.app.state.db, payload.url, adapter.site, title=payload.title
    )
    if not created:
        response.status_code = 200
    row = get_watched_comic(request.app.state.db, comic_id)
    assert row is not None
    return _comic_out(row)


@router.delete("/{comic_id}", response_model=WatchDeleteResponse)
async def watch_delete(request: Request, comic_id: int) -> WatchDeleteResponse:
    """Stop watching a comic (the crawl history and collected media stay)."""
    if not delete_watched_comic(request.app.state.db, comic_id):
        raise HTTPException(status_code=404, detail=f"watched comic {comic_id} not found")
    return WatchDeleteResponse(id=comic_id, deleted=True)


@router.patch("/{comic_id}", response_model=WatchedComicOut)
async def watch_update(request: Request, comic_id: int, payload: WatchUpdate) -> WatchedComicOut:
    """Enable/disable one comic's passive scanning without deleting the row."""
    if not set_watched_enabled(request.app.state.db, comic_id, payload.enabled):
        raise HTTPException(status_code=404, detail=f"watched comic {comic_id} not found")
    row = get_watched_comic(request.app.state.db, comic_id)
    assert row is not None
    return _comic_out(row)


@router.post("/{comic_id}/scan", response_model=ScrapeStarted, status_code=202)
async def watch_scan(request: Request, comic_id: int) -> ScrapeStarted:
    """Targeted fetch: run one watch pass for this comic only, right now.

    Same crawl as the passive pass (``force_rescan=False``, page-capped) so only
    new pages are fetched, and the same shared pipeline chains thumbnails → dup
    scan → AI → FTS afterwards. Returns as soon as the ``jobs`` row exists.
    """
    comic = get_watched_comic(request.app.state.db, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail=f"watched comic {comic_id} not found")
    try:
        get_adapter(str(comic["url"]))
    except UnsupportedSiteError as exc:
        # Defensive: a watched comic whose adapter was removed must still be
        # reported the standard way instead of failing after a 5s job-row wait.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = build_watch_crawl(request.app, comic)
    started = await start_crawl(request.app, job)
    logger.info(
        "watch scan started comic_id=%d job_id=%d url=%s",
        comic_id, started.job_id, comic["url"],
    )
    return ScrapeStarted(job_id=started.job_id, status=started.status)
