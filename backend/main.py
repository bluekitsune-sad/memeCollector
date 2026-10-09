"""FastAPI application entry point for MemeCollector.

Startup applies pending database migrations, fails any ``jobs`` row stranded
``running`` by a previous process (:mod:`backend.jobs.recovery`) and then
**auto-resumes** the interrupted work in the background
(:mod:`backend.jobs.resume` — crawls relaunched from their params, a stranded
site-wide backfill continued), spawns the background
:class:`backend.ai.supervisor.AISupervisor` task (resilient AI analysis with a
live ``GET /api/ai/status`` surface) and the background
:class:`backend.jobs.watch.WatchSupervisor` task (passive rescans of watched
comics, ``watch.interval_minutes`` — PRD §39), and exposes the route
modules (ai / media / scraper / watch / jobs / backfill / search / settings). CORS allows the Next.js dev
server on
localhost:3000 (PRD §0 frontend decision); the server itself keeps the
localhost-only binding from config — run from the project root:

``uvicorn backend.main:app --host 127.0.0.1 --port 8000`` (PRD §41).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from functools import partial

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.ai.runner import AIQueueGate
from backend.ai.supervisor import AISupervisor
from backend.api.routes_ai import router as ai_router
from backend.api.routes_backfill import router as backfill_router
from backend.api.routes_jobs import router as jobs_router
from backend.api.routes_media import router as media_router
from backend.api.routes_scraper import router as scraper_router
from backend.api.routes_search import router as search_router
from backend.api.routes_settings import router as settings_router
from backend.api.routes_watch import router as watch_router
from backend.config import Settings, load_settings
from backend.database.database import initialize_database
from backend.jobs.backfill import resume_interrupted_backfill
from backend.jobs.recovery import recover_interrupted_jobs
from backend.jobs.resume import resume_interrupted_work
from backend.jobs.watch import WatchSupervisor
from backend.logging_config import configure_logging

logger = logging.getLogger(__name__)

#: Origins allowed to call the API: the Next.js dev server (PRD §0).
_DEV_ORIGINS: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")


@asynccontextmanager
async def _lifespan(app: FastAPI, settings: Settings | None) -> AsyncIterator[None]:
    resolved = settings if settings is not None else load_settings()
    connection = initialize_database(resolved.storage.database_path)
    app.state.db = connection
    app.state.settings = resolved
    # Jobs stranded 'running' by a previous process (restart/crash mid-crawl)
    # are failed here — nothing is live yet, so 'running' can only be a zombie.
    # The list is handed to the auto-resume so the crawls restart "when they can".
    interrupted = recover_interrupted_jobs(connection)
    # Live CrawlJob handles for /api/jobs pause/resume/cancel (job_id → CrawlJob).
    app.state.running_crawls = {}
    # Site-wide backfill (backend/jobs/backfill.py): live handle + task.
    app.state.backfill_job = None
    app.state.backfill_task = None
    # Concurrency guard for the AI queue (one run at a time) — shared by the
    # scrape pipeline's AI stage and the background supervisor (backend/ai/runner.py).
    app.state.ai_queue_running = False
    supervisor = AISupervisor(connection, resolved, AIQueueGate(app.state))
    app.state.ai_supervisor = supervisor
    # Wake channel for the supervisor: setting this event == calling supervisor.nudge().
    app.state.ai_nudge = supervisor.wake_event
    ai_task = asyncio.create_task(supervisor.run())
    app.state.ai_task = ai_task
    # Passive watcher (PRD §39): rescans watched comics every watch.interval_minutes
    # through the same pipeline the scrape API uses; no-ops when watch.enabled=false.
    watch = WatchSupervisor(app)
    app.state.watch_supervisor = watch
    app.state.watch_nudge = watch.wake_event
    watch_task = asyncio.create_task(watch.run())
    app.state.watch_task = watch_task
    # Auto-resume (PRD §36): relaunch interrupted crawls from their params and
    # continue a backfill stranded 'running' — in the background so startup is
    # never blocked ("it will start when it can").
    resume_task = asyncio.create_task(_resume_after_restart(app, interrupted))
    app.state.resume_task = resume_task
    logger.info("application starting db=%s", resolved.storage.database_path)
    try:
        yield
    finally:
        # Backfill first: it may own a live crawl; stop() cancels it cooperatively
        # and leaves the row 'running' so the next boot resumes exactly here.
        backfill = getattr(app.state, "backfill_job", None)
        if backfill is not None:
            backfill.stop()
        backfill_task = getattr(app.state, "backfill_task", None)
        if backfill_task is not None:
            backfill_task.cancel()
            with suppress(asyncio.CancelledError):
                await backfill_task
        resume_task.cancel()
        with suppress(asyncio.CancelledError):
            await resume_task
        await supervisor.stop()
        ai_task.cancel()
        with suppress(asyncio.CancelledError):
            await ai_task
        await watch.stop()
        watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await watch_task
        connection.close()


async def _resume_after_restart(app: FastAPI, interrupted: list[dict[str, object]]) -> None:
    """Background startup resume: interrupted crawls, orphaned stages, the backfill.

    Never raises — every path degrades to "the next crawl/watch pass or a
    manual action finishes the work" (PRD §36).
    """
    try:
        if interrupted:
            await resume_interrupted_work(app, interrupted)
        resume_interrupted_backfill(app)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("startup resume failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application (schema migrations run at startup).

    ``settings`` lets tests point the database/storage at a temp directory;
    production uses ``app = create_app()`` → :func:`backend.config.load_settings`.
    Structured logging (PRD §53) is configured here — idempotently, so test
    suites that build several apps never stack handlers.
    """
    configure_logging()
    app = FastAPI(
        title="MemeCollector",
        version="0.1.0",
        lifespan=partial(_lifespan, settings=settings),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_DEV_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai_router)
    app.include_router(media_router)
    app.include_router(scraper_router)
    app.include_router(watch_router)
    app.include_router(jobs_router)
    app.include_router(backfill_router)
    app.include_router(search_router)
    app.include_router(settings_router)
    logger.info("routes registered ai media scraper watch jobs backfill search settings")
    return app


app = create_app()


if __name__ == "__main__":
    # Dev convenience entry point: `python -m backend.main` == the uvicorn CLI,
    # honoring MEME_HOST/MEME_PORT from the configuration (PRD §41: localhost only).
    import uvicorn

    _settings = load_settings()
    if _settings.server.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "binding to a non-loopback address host=%s — MemeCollector is a local-first "
            "tool and must not be exposed publicly (PRD §41)",
            _settings.server.host,
        )
    uvicorn.run(app, host=_settings.server.host, port=_settings.server.port)
