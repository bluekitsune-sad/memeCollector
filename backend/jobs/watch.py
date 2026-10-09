"""Passive background watcher — watched comics rescanned on an interval (PRD §39, §57).

Two surfaces, one code path:

* **Passive** — :class:`WatchSupervisor` runs :func:`run_watch_pass` every
  ``watch.interval_minutes`` inside an app-lifespan task (mirrors
  :mod:`backend.ai.supervisor`'s loop/stop structure). Each enabled watched
  comic is crawled with ``force_rescan=False``, so ``crawl_history`` (PRD §38)
  skips every page already seen and only new chapters/pages are fetched —
  then the shared PROCESS → INDEX chain from :mod:`backend.jobs.pipeline`
  runs (thumbnails, dup scan, AI, FTS rebuild).
* **Targeted** — ``POST /api/watch/{id}/scan`` builds the very same crawl for
  one comic via :func:`build_watch_crawl` and backgrounds it with
  :func:`start_crawl`, returning its ``job_id`` immediately (202).

Rate limits (AGENTS.md §9): comics are scanned **sequentially**, each crawl
honouring the configured ``crawler.delay_seconds`` / ``crawler.concurrency``;
``watch.max_pages_per_run`` caps discovery pages per comic per pass.

Mutual exclusion with user-initiated crawls: the pass skips entirely when a
crawl it does not own is already live in ``app.state.running_crawls``, and it
abandons the remaining comics the moment a user crawl starts mid-pass — passive
work never fights a manual crawl (and the manual crawl registered by
:func:`start_crawl` makes the next passive pass skip too).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from fastapi import FastAPI

from backend.config import Settings
from backend.jobs.crawl_job import CrawlJob
from backend.jobs.pipeline import start_crawl
from backend.jobs.watched_comics import list_watched_comics
from backend.scraper.adapters import CrawlScope, ScopeKind, UnsupportedSiteError, get_adapter
from backend.scraper.crawler import PageFetcher

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchPassSummary:
    """Outcome of one watch pass — every field JSON-testable, never fatal (PRD §36)."""

    scanned: list[int] = field(default_factory=list)
    #: The pass did not run at all: a crawl it does not own was already live.
    skipped: bool = False
    skip_reason: str | None = None
    #: A user crawl started while this pass worked — remaining comics left for the next one.
    abandoned: str | None = None
    #: Per-comic failures (unsupported site, crawl failed to start), recorded, never raised.
    failures: list[str] = field(default_factory=list)


def watch_bounded_settings(settings: Settings) -> Settings:
    """``settings`` with the crawler page cap replaced by ``watch.max_pages_per_run``.

    Only the page cap changes — ``delay_seconds``, ``concurrency``, storage and
    download limits all keep their configured values (AGENTS.md §9).
    """
    watch = settings.watch
    return replace(
        settings, crawler=replace(settings.crawler, max_pages=max(1, watch.max_pages_per_run))
    )


def build_watch_crawl(
    app: FastAPI,
    comic: sqlite3.Row,
    *,
    fetcher: PageFetcher | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> CrawlJob:
    """Crawl job for one watched-comic row: incremental, page-capped, ingest on.

    ``ENTIRE_COMIC`` expands the entry URL to every chapter/page (capped by
    :func:`watch_bounded_settings`); ``force_rescan=False`` then lets
    ``crawl_history`` skip the ones already scanned. ``fetcher``/``http_client``
    are offline-injection seams for tests — production builds its own.
    """
    return CrawlJob(
        str(comic["url"]),
        CrawlScope(ScopeKind.ENTIRE_COMIC),
        settings=watch_bounded_settings(app.state.settings),
        db=app.state.db,
        force_rescan=False,
        fetcher=fetcher,
        http_client=http_client,
        ingest=True,
    )


async def run_watch_pass(
    app: FastAPI,
    *,
    fetcher: PageFetcher | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> WatchPassSummary:
    """Scan every enabled watched comic once; never raises for per-comic failures.

    Skips entirely while a crawl it does not own is live, and stops before the
    next comic when one appears mid-pass — passive and manual crawls never run
    at once. Each comic goes through the shared pipeline (crawl → thumbnail →
    dup scan → AI → index), awaited sequentially to respect rate limits.
    """
    registry: dict[int, CrawlJob] = app.state.running_crawls
    if registry:
        reason = f"crawl already live job_ids={sorted(registry)}"
        logger.info("watch pass skipped reason=%s", reason)
        return WatchPassSummary(skipped=True, skip_reason=reason)

    comics = list_watched_comics(app.state.db, enabled_only=True)
    if not comics:
        return WatchPassSummary()

    scanned: list[int] = []
    failures: list[str] = []
    abandoned: str | None = None
    own: set[int] = set()
    for comic in comics:
        foreign = sorted(job_id for job_id in registry if job_id not in own)
        if foreign:
            abandoned = f"user crawl started mid-pass job_ids={foreign}"
            logger.info("watch pass abandoning remaining comics reason=%s", abandoned)
            break
        comic_id = int(comic["id"])
        try:
            get_adapter(str(comic["url"]))
        except UnsupportedSiteError as exc:
            failures.append(f"comic {comic_id}: {exc}")
            logger.warning("watch comic unsupported id=%d url=%s", comic_id, comic["url"])
            continue
        try:
            job = build_watch_crawl(app, comic, fetcher=fetcher, http_client=http_client)
            started = await start_crawl(app, job)
            own.add(started.job_id)
            await started.task
            scanned.append(comic_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append(f"comic {comic_id}: {exc}")
            logger.warning(
                "watch scan failed id=%d url=%s error=%s", comic_id, comic["url"], exc
            )
    return WatchPassSummary(
        scanned=scanned, abandoned=abandoned, failures=failures
    )


class WatchSupervisor:
    """The background loop: pass → wait interval, with prompt, event-based stops.

    Mirrors :class:`backend.ai.supervisor.AISupervisor`: a wake event cuts the
    interval wait short (:meth:`nudge` / :meth:`stop`), a bad pass is logged
    and never kills the loop, and :meth:`stop` makes shutdown prompt while the
    lifespan cancels + awaits the task the same way it does for the AI
    supervisor (backend/main.py).
    """

    def __init__(self, app: FastAPI) -> None:
        """The supervisor drives :func:`run_watch_pass` over the app's shared state."""
        self._app = app
        self._wake = asyncio.Event()
        self._stopping = False
        self._state = "stopped"
        self._reason: str | None = None

    # -- public surface -----------------------------------------------------

    async def run(self) -> None:
        """Main loop; catches everything so one bad pass never kills the watcher."""
        settings = self._app.state.settings
        if not settings.watch.enabled:
            self._transition("disabled", reason="watch.enabled=false")
            return
        interval = max(0.0, settings.watch.interval_minutes) * 60.0
        self._transition("idle", reason="supervisor started")
        try:
            while not self._stopping:
                self._wake.clear()
                self._transition("running", reason="watch pass starting")
                try:
                    summary = await run_watch_pass(self._app)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("watch pass failed error=%s", exc)
                else:
                    logger.info(
                        "watch pass finished scanned=%d skipped=%s abandoned=%s failures=%d",
                        len(summary.scanned), summary.skipped, summary.abandoned is not None,
                        len(summary.failures),
                    )
                self._transition("idle", reason=f"next pass in {interval:.0f}s")
                await self._sleep(interval)
        finally:
            self._transition("stopped", reason="supervisor stopped")

    def nudge(self) -> None:
        """Wake the loop immediately (run the next pass without waiting the interval)."""
        self._wake.set()

    @property
    def wake_event(self) -> asyncio.Event:
        """The event :meth:`nudge` sets — surfaced as ``app.state.watch_nudge``."""
        return self._wake

    async def stop(self) -> None:
        """Graceful shutdown: mark stopped and cut any interval wait short."""
        self._stopping = True
        self._wake.set()

    @property
    def state(self) -> str:
        """Current loop state: ``disabled`` / ``idle`` / ``running`` / ``stopped``."""
        return self._state

    # -- internals ----------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        """Timed wait between passes; :meth:`nudge` and :meth:`stop` cut it short."""
        if self._stopping or seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _transition(self, state: str, *, reason: str) -> None:
        """Record a state change; log only actual transitions (no per-pass spam)."""
        if state == self._state and reason == self._reason:
            return
        previous = self._state
        self._state, self._reason = state, reason
        logger.info("watch supervisor state=%s→%s reason=%s", previous, state, reason)
