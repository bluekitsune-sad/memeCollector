"""Shared scrape pipeline — one code path for every crawl (PRD §36, §57).

Extracted from :mod:`backend.api.routes_scraper` so the scrape API and the
passive watcher (:mod:`backend.jobs.watch`) share exactly the same stages
instead of maintaining two copies:

* :func:`start_crawl` — backgrounds :func:`run_pipeline`, waits for the
  ``jobs`` row, and registers the live ``CrawlJob`` handle on
  ``app.state.running_crawls`` (removed again when the task ends).
* :func:`run_pipeline` — **COLLECT → PROCESS → INDEX**: crawl → thumbnails →
  dup scan → AI queue → FTS rebuild, each failure-isolated (PRD §36) with its
  own ``jobs`` row so the stages stay separate workers (PRD §57).
* :func:`run_ai_stage` — the AI half of PROCESS: guarded by the shared
  :class:`backend.ai.runner.AIQueueGate` (also held by the background AI
  supervisor) and degraded by design when no provider/key exists (a missing
  key never breaks a scrape, PRD §36).

After every successful (non-cancelled) crawl the pipeline auto-registers the
scanned comic in ``watched_comics`` (backend/jobs/watched_comics.py): the
passive watcher then rescans it automatically, and the upsert is idempotent so
repeat crawls never duplicate or re-enable anything.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException

from backend.ai.provider import AIUnavailableError, VisionProvider, create_provider
from backend.ai.queue import run_ai_queue
from backend.ai.runner import AIQueueGate
from backend.jobs.crawl_job import CrawlJob
from backend.jobs.dup_job import run_dup_job
from backend.jobs.index_job import run_index_job
from backend.jobs.thumbnail_job import run_thumbnail_job
from backend.jobs.watched_comics import record_comic_scan
from backend.scraper.adapters import UnsupportedSiteError, get_adapter

logger = logging.getLogger(__name__)

#: How long to wait for ``CrawlJob.run()`` to create its ``jobs`` row.
_START_TIMEOUT_SECONDS = 5.0

#: Poll interval while waiting for the job row.
_START_POLL_SECONDS = 0.005


@dataclass(frozen=True)
class StartedCrawl:
    """A backgrounded pipeline: its ``jobs``-row id, live status, and the task."""

    job_id: int
    status: str
    task: asyncio.Task[None]


async def start_crawl(app: FastAPI, job: CrawlJob) -> StartedCrawl:
    """Run ``run_pipeline`` in the background and return once its ``jobs`` row exists.

    The live handle is registered on ``app.state.running_crawls`` for
    ``/api/jobs/{id}/pause|resume|cancel`` before any ``await``-free race window
    can strand a stale entry; if the crawl stage ended before/during
    registration the entry is popped immediately. Raises ``500`` (cancelling the
    task) when no row appears within the startup timeout.
    """
    task = asyncio.create_task(run_pipeline(app, job))
    try:
        job_id = await _await_job_row(job)
    except HTTPException:
        task.cancel()
        raise
    registry: dict[int, CrawlJob] = app.state.running_crawls
    registry[job_id] = job
    row = app.state.db.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    status = str(row["status"]) if row is not None else "running"
    if status != "running" or task.done():
        # The crawl stage ended before/during registration — its own cleanup has
        # already run, so pop immediately rather than strand a stale handle.
        registry.pop(job_id, None)
    if task.done() and not task.cancelled():
        error = task.exception()
        if error is not None:
            logger.warning("crawl ended before response job_id=%d error=%s", job_id, error)
    return StartedCrawl(job_id=job_id, status=status, task=task)


async def run_pipeline(app: FastAPI, job: CrawlJob) -> None:
    """COLLECT → PROCESS → INDEX: crawl, thumbnails, dup scan, AI queue, FTS rebuild.

    Each stage is failure-isolated (PRD §36). ``CrawlJob.run()`` records its
    own failure before re-raising; swallowing it here keeps the chained stages
    running (whatever was stored still deserves thumbnails, a dup scan,
    analysis and indexing) and avoids an unretrieved task exception.
    """
    try:
        summary = await job.run()
    except Exception as exc:
        logger.warning("crawl stage ended with error job_id=%s error=%s", job.job_id, exc)
    else:
        if not summary.cancelled:
            _record_scanned_comic(app.state.db, job.url)
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
    await run_ai_stage(app, job.job_id)
    # Wake the background supervisor: any leftovers (guard contention, a key
    # configured since startup, deferred rows) deserve a prompt run, not a poll.
    supervisor = getattr(app.state, "ai_supervisor", None)
    if supervisor is not None:
        supervisor.nudge()
    try:
        await run_index_job(app.state.db)
    except Exception as exc:
        logger.warning("index stage failed after crawl job_id=%s error=%s", job.job_id, exc)


async def run_ai_stage(app: FastAPI, job_id: int | None) -> None:
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


def _record_scanned_comic(db: sqlite3.Connection, url: str) -> None:
    """Auto-register the comic behind ``url`` in ``watched_comics`` (idempotent).

    The adapter is re-resolved for its registry ``site`` name; an unsupported
    URL (impossible in practice — the crawl validated it) or a database hiccup
    is logged and skipped, never fatal to the pipeline (PRD §36).
    """
    try:
        adapter = get_adapter(url)
        record_comic_scan(db, url, adapter.site)
    except UnsupportedSiteError:
        logger.debug("watch auto-register skipped: no adapter url=%s", url)
    except sqlite3.Error:
        logger.exception("watch auto-register failed url=%s", url)


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
