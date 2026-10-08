"""FastAPI application entry point for MemeVault.

Startup applies pending database migrations and exposes the route modules
(media / scraper / jobs / search). CORS allows the Next.js dev server on
localhost:3000 (PRD §0 frontend decision); the server itself keeps the
localhost-only binding from config — run from the project root:

``uvicorn backend.main:app --host 127.0.0.1 --port 8000`` (PRD §41).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes_jobs import router as jobs_router
from backend.api.routes_media import router as media_router
from backend.api.routes_scraper import router as scraper_router
from backend.api.routes_search import router as search_router
from backend.api.routes_settings import router as settings_router
from backend.config import Settings, load_settings
from backend.database.database import initialize_database
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
    # Live CrawlJob handles for /api/jobs pause/resume/cancel (job_id → CrawlJob).
    app.state.running_crawls = {}
    # Concurrency guard for the scrape pipeline's AI stage (one queue at a time).
    app.state.ai_queue_running = False
    logger.info("application starting db=%s", resolved.storage.database_path)
    try:
        yield
    finally:
        connection.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application (schema migrations run at startup).

    ``settings`` lets tests point the database/storage at a temp directory;
    production uses ``app = create_app()`` → :func:`backend.config.load_settings`.
    Structured logging (PRD §53) is configured here — idempotently, so test
    suites that build several apps never stack handlers.
    """
    configure_logging()
    app = FastAPI(
        title="MemeVault",
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
    app.include_router(media_router)
    app.include_router(scraper_router)
    app.include_router(jobs_router)
    app.include_router(search_router)
    app.include_router(settings_router)
    logger.info("routes registered media scraper jobs search settings")
    return app


app = create_app()


if __name__ == "__main__":
    # Dev convenience entry point: `python -m backend.main` == the uvicorn CLI,
    # honoring MEME_HOST/MEME_PORT from the configuration (PRD §41: localhost only).
    import uvicorn

    _settings = load_settings()
    if _settings.server.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "binding to a non-loopback address host=%s — MemeVault is a local-first "
            "tool and must not be exposed publicly (PRD §41)",
            _settings.server.host,
        )
    uvicorn.run(app, host=_settings.server.host, port=_settings.server.port)
