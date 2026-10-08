"""Scrape API — start a crawl and poll its status (PRD §0, §5.1–§5.2).

Start point for Milestone 5's Add Source flow:

* ``POST /api/scrape`` validates the URL against the adapter registry **before**
  anything is created — an unknown site returns ``400`` with the explicit
  "Site not supported: add an adapter for …" message (PRD §0, AGENTS.md §5).
* The crawl runs as a background ``asyncio`` task holding the returned
  ``job_id``; the handler waits only until the ``jobs`` row exists so the
  response can carry it (the crawl itself never blocks the response).
* After the crawl the same task chains the PROCESS-stage jobs — thumbnails,
  then the dup scan/purge — each with its own ``jobs`` row, so one scrape
  request produces crawl → thumbnail → dup_scan entries in the Jobs UI while
  the stages remain separate workers (PRD §57).
* Next comes the AI queue (PROCESS, PRD §18): it is guarded by
  ``app.state.ai_queue_running`` (the shared :class:`backend.ai.runner.AIQueueGate`,
  also held by the background AI supervisor) so two overlapping runs never
  claim the queue at once, and it degrades gracefully — when no provider can be
  built (``openrouter`` without ``OPENROUTER_API_KEY``) the stage logs a
  warning and the pipeline finishes with the media still ``DOWNLOADED``
  (PRD §19, §36: a missing key never breaks a scrape).
* The final stage is INDEX (PRD §57): ``run_index_job`` rebuilds ``media_fts``
  so freshly analyzed descriptions/tags are keyword-searchable.
* Live crawl handles are kept on ``app.state.running_crawls`` (``job_id`` →
  ``CrawlJob``) for ``/api/jobs/{id}/pause|resume|cancel``; entries are removed
  when the task ends. Registration happens before any ``await``-free race window
  can strand a stale entry (see ``start_scrape``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from backend.ai.provider import AIUnavailableError, VisionProvider, create_provider
from backend.ai.queue import run_ai_queue
from backend.ai.runner import AIQueueGate
from backend.api.schemas import JobOut, job_from_row
from backend.jobs.crawl_job import CrawlJob
from backend.jobs.dup_job import run_dup_job
from backend.jobs.index_job import run_index_job
from backend.jobs.thumbnail_job import run_thumbnail_job
from backend.scraper.adapters import CrawlScope, ScopeKind, UnsupportedSiteError, get_adapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scrape", tags=["scraper"])

#: How long to wait for ``CrawlJob.run()`` to create its ``jobs`` row.
_START_TIMEOUT_SECONDS = 5.0

#: Poll interval while waiting for the job row.
_START_POLL_SECONDS = 0.005

#: PRD §5.1 scope selection accepted by the API.
ScrapeScope = Literal[
    "current_page", "current_chapter", "multiple_chapters", "entire_comic", "custom_urls"
]


class ScrapeRequest(BaseModel):
    """Body for ``POST /api/scrape`` (MVP default: current page, no rescan)."""

    url: str = Field(min_length=1, max_length=2048)
    scope: ScrapeScope = "current_page"
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


@router.post("", response_model=ScrapeStarted, status_code=202)
async def start_scrape(request: Request, payload: ScrapeRequest) -> ScrapeStarted:
    """Validate the site, start the crawl in the background, return its ``job_id``."""
    try:
        get_adapter(payload.url)
    except UnsupportedSiteError as exc:
        # PRD §0: an unsupported URL is reported explicitly, never silently.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = request.app.state.settings
    job = CrawlJob(
        payload.url,
        CrawlScope(ScopeKind(payload.scope), tuple(payload.urls)),
        settings=settings,
        db=request.app.state.db,
        force_rescan=payload.force_rescan,
        ingest=True,
    )
    task = asyncio.create_task(_run_pipeline(request.app, job))
    try:
        job_id = await _await_job_row(job)
    except HTTPException:
        task.cancel()
        raise
    registry: dict[int, CrawlJob] = request.app.state.running_crawls
    registry[job_id] = job
    row = request.app.state.db.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    status = str(row["status"]) if row is not None else "running"
    if status != "running" or task.done():
        # The crawl stage ended before/during registration — its own cleanup has
        # already run, so pop immediately rather than strand a stale handle.
        registry.pop(job_id, None)
    if task.done():
        error = task.exception()
        if error is not None:
            logger.warning("crawl ended before response job_id=%d error=%s", job_id, error)
    logger.info("scrape started job_id=%d url=%s scope=%s", job_id, payload.url, payload.scope)
    return ScrapeStarted(job_id=job_id, status=status)


@router.get("/{job_id}", response_model=JobOut)
async def scrape_status(request: Request, job_id: int) -> JobOut:
    """Status/progress/counter message of one scrape (reads its ``jobs`` row)."""
    row = request.app.state.db.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job_from_row(row)


async def _run_pipeline(app: FastAPI, job: CrawlJob) -> None:
    """COLLECT → PROCESS → INDEX: crawl, thumbnails, dup scan, AI queue, FTS rebuild.

    Each stage is failure-isolated (PRD §36). ``CrawlJob.run()`` records its
    own failure before re-raising; swallowing it here keeps the chained stages
    running (whatever was stored still deserves thumbnails, a dup scan,
    analysis and indexing) and avoids an unretrieved task exception.
    """
    try:
        await job.run()
    except Exception as exc:
        logger.warning("crawl stage ended with error job_id=%s error=%s", job.job_id, exc)
    finally:
        if job.job_id is not None:
            registry: dict[int, CrawlJob] = app.state.running_crawls
            if registry.get(job.job_id) is job:
                registry.pop(job.job_id, None)
    try:
        await run_thumbnail_job(db=app.state.db, settings=app.state.settings)
    except Exception as exc:
        logger.warning("thumbnail stage failed after crawl job_id=%s error=%s", job.job_id, exc)
    try:
        await run_dup_job(db=app.state.db, settings=app.state.settings)
    except Exception as exc:
        logger.warning("dup stage failed after crawl job_id=%s error=%s", job.job_id, exc)
    await _run_ai_stage(app, job.job_id)
    # Wake the background supervisor: any leftovers (guard contention, a key
    # configured since startup, deferred rows) deserve a prompt run, not a poll.
    supervisor = getattr(app.state, "ai_supervisor", None)
    if supervisor is not None:
        supervisor.nudge()
    try:
        await run_index_job(app.state.db)
    except Exception as exc:
        logger.warning("index stage failed after crawl job_id=%s error=%s", job.job_id, exc)


async def _run_ai_stage(app: FastAPI, job_id: int | None) -> None:
    """Drain the AI queue for everything this scrape stored (PRD §18, §19, §36).

    Concurrency guard: the stage claims :class:`backend.ai.runner.AIQueueGate`
    — a compare-and-set over ``app.state.ai_queue_running`` shared with the
    background :class:`backend.ai.supervisor.AISupervisor` — for the duration
    of the run, so a second scrape (or the supervisor) that finishes its work
    while a queue is live skips this stage (claiming is status-based — one
    queue at a time is assumed, see :mod:`backend.ai.queue`) instead of
    double-processing rows.

    Missing provider/key is *not* a failure: ``create_provider`` raises
    :class:`~backend.ai.provider.AIUnavailableError` for ``openrouter`` without
    ``OPENROUTER_API_KEY``, which logs the expected offline-mode warning and
    leaves the media ``DOWNLOADED`` for a later run (the supervisor picks it up
    once a key is configured). The queue owns its own ``jobs`` row
    (``job_type='ai_analysis'``).
    """
    gate = AIQueueGate(app.state)
    if not gate.try_acquire():
        logger.info("ai stage skipped: queue already running job_id=%s", job_id)
        return
    provider: VisionProvider | None = None
    try:
        try:
            provider = create_provider(app.state.settings)
        except AIUnavailableError as exc:
            logger.warning("AI stage skipped (no provider/key) — media remains DOWNLOADED (%s)", exc)
            return
        except Exception as exc:
            logger.warning("ai stage skipped: provider setup failed job_id=%s error=%s", job_id, exc)
            return
        try:
            summary = await run_ai_queue(app.state.db, provider, app.state.settings)
            logger.info(
                "ai stage finished job_id=%s queue_job=%d total=%d ready=%d failed=%d",
                job_id, summary.job_id, summary.total, summary.ready, summary.failed,
            )
        except Exception as exc:
            logger.warning("ai stage failed job_id=%s error=%s", job_id, exc)
    finally:
        gate.release()
        if provider is not None:
            try:
                await provider.aclose()
            except Exception as exc:
                logger.warning("ai provider close failed job_id=%s error=%s", job_id, exc)


async def _await_job_row(job: CrawlJob) -> int:
    """Wait until ``job.run()`` has created its ``jobs`` row; 500 if it never does."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_TIMEOUT_SECONDS
    while job.job_id is None:
        if loop.time() >= deadline:
            raise HTTPException(
                status_code=500, detail="crawl job failed to start (no jobs row)"
            )
        await asyncio.sleep(_START_POLL_SECONDS)
    return job.job_id
