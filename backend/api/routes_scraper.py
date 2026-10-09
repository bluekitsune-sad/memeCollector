"""Scrape API — start a crawl and poll its status (PRD §0, §5.1–§5.2).

Start point for Milestone 5's Add Source flow:

* ``POST /api/scrape`` validates the URL against the adapter registry **before**
  anything is created — an unknown site returns ``400`` with the explicit
  "Site not supported: add an adapter for …" message (PRD §0, AGENTS.md §5).
* ``scope="auto"`` infers the scope from the URL via :func:`infer_scope`
  (chapter-like path → ``current_chapter``, otherwise ``current_page``); every
  explicit scope value behaves exactly as before.
* The crawl runs as a background task through :func:`backend.jobs.pipeline.start_crawl`
  — the handler waits only until the ``jobs`` row exists so the response can
  carry it (the crawl itself never blocks the response). The shared pipeline
  chains PROCESS → INDEX (thumbnails → dup scan → AI queue → FTS rebuild, each
  with its own ``jobs`` row — one stage per worker, PRD §57) and auto-registers
  the scanned comic in ``watched_comics`` so the passive watcher picks it up.
* Live crawl handles are kept on ``app.state.running_crawls`` (``job_id`` →
  ``CrawlJob``) for ``/api/jobs/{id}/pause|resume|cancel``; entries are removed
  when the task ends.

The pipeline itself lives in :mod:`backend.jobs.pipeline` — the watch routes
(:mod:`backend.api.routes_watch`) reuse the exact same code path.
"""

from __future__ import annotations

import logging
import re
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.schemas import JobOut, job_from_row
from backend.jobs.crawl_job import CrawlJob
from backend.jobs.pipeline import run_ai_stage, start_crawl
from backend.scraper.adapters import CrawlScope, ScopeKind, UnsupportedSiteError, get_adapter

# Re-export: the AI stage moved to backend.jobs.pipeline; the pipeline tests
# import it from here, so keep the old name importable.
_run_ai_stage = run_ai_stage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scrape", tags=["scraper"])

#: PRD §5.1 scope selection accepted by the API, plus URL-based inference.
ScrapeScope = Literal[
    "current_page", "current_chapter", "multiple_chapters", "entire_comic", "custom_urls"
]

#: ``scope="auto"``: infer the scope from the URL instead of forcing one.
ScrapeScopeInput = ScrapeScope | Literal["auto"]

#: Chapter-like path segment: ``/chapter-12``, ``/chapter/12``, ``/ch/3`` —
#: also matches the real formats of the MVP sites (asurascans ``/chapter/<n>``,
#: mangadex ``/chapter/<uuid>``, mangapark/comix ``…/<chapter-slug>``).
_CHAPTER_PATH_PATTERN = re.compile(r"/(?:chapter|ch)[-_/][^/]+", re.IGNORECASE)


class ScrapeRequest(BaseModel):
    """Body for ``POST /api/scrape`` (MVP default: current page, no rescan)."""

    url: str = Field(min_length=1, max_length=2048)
    scope: ScrapeScopeInput = "current_page"
    force_rescan: bool = False
    urls: list[str] = Field(
        default_factory=list,
        max_length=500,
        description="Entry URLs for scope=custom_urls / multiple_chapters",
    )


class ScrapeStarted(BaseModel):
    """Accepted crawl: its jobs-row id and current status."""

    job_id: int
    status: str


def infer_scope(url: str) -> Literal["current_page", "current_chapter"]:
    """Infer the PRD §5.1 scope of ``url`` for ``scope="auto"`` — a pure helper.

    Chapter-like paths (``/chapter-12``, ``/chapter/12``, ``/ch-3``) resolve to
    ``current_chapter``; everything else (comic/entry/title pages, bare hosts)
    resolves to ``current_page``. Both failure directions are graceful: an
    adapter receiving ``current_chapter`` from a non-chapter URL logs a warning
    and scans that page anyway, and ``current_page`` always scans exactly what
    the user pasted.
    """
    path = urlparse(url).path
    return "current_chapter" if _CHAPTER_PATH_PATTERN.search(path) else "current_page"


@router.post("", response_model=ScrapeStarted, status_code=202)
async def start_scrape(request: Request, payload: ScrapeRequest) -> ScrapeStarted:
    """Validate the site, start the crawl in the background, return its ``job_id``."""
    try:
        get_adapter(payload.url)
    except UnsupportedSiteError as exc:
        # PRD §0: an unsupported URL is reported explicitly, never silently.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved_scope = infer_scope(payload.url) if payload.scope == "auto" else payload.scope
    settings = request.app.state.settings
    job = CrawlJob(
        payload.url,
        CrawlScope(ScopeKind(resolved_scope), tuple(payload.urls)),
        settings=settings,
        db=request.app.state.db,
        force_rescan=payload.force_rescan,
        ingest=True,
    )
    started = await start_crawl(request.app, job)
    logger.info(
        "scrape started job_id=%d url=%s scope=%s", started.job_id, payload.url, resolved_scope
    )
    return ScrapeStarted(job_id=started.job_id, status=started.status)


@router.get("/{job_id}", response_model=JobOut)
async def scrape_status(request: Request, job_id: int) -> JobOut:
    """Status/progress/counter message of one scrape (reads its ``jobs`` row)."""
    row = request.app.state.db.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job_from_row(row)
