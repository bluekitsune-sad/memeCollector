"""Crawl job orchestration — runs COLLECT + download with a tracked jobs row (PRD §35, §36, §5.2).

``CrawlJob`` is a plain async class — no HTTP layer. The Milestone 2 API will
construct it, ``await run()``, and call :meth:`pause` / :meth:`resume` /
:meth:`cancel` (cooperative, via :class:`~backend.scraper.crawler.CrawlController`).

Lifecycle of the ``jobs`` row it creates:

``running`` → ``completed`` | ``cancelled`` (cooperative stop) | ``failed``
(an unexpected pipeline error — recorded, then re-raised to the caller).

Progress (``jobs.progress``, 0.0–1.0) is split into the crawl phase (0–60%,
pages handled) and the download phase (60–100%, files processed);
``jobs.message`` carries ``key=value`` counters for the jobs UI.

Downloads are sequential — well inside the concurrency ≤ 2 default
(AGENTS.md §9) — and stop at ``crawler.download_limit``. The Level-1 URL
duplicate check queries the ``source`` table; media/source rows are written by
``backend.media.library.ingest_download`` (Milestone 2) when the job is
constructed with ``ingest=True`` — the API does this, while the default
(``ingest=False``) keeps the pure files-only behaviour the M1 tests exercise.
With ingest on, an exact SHA-256 duplicate found at store time (Level 2,
PRD §12) counts as ``skipped_duplicate`` rather than ``downloaded_new``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from backend.config import Settings
from backend.database.database import transaction
from backend.media.library import ingest_download
from backend.scraper.adapters import CrawlScope, MediaRef, ScopeKind, get_adapter
from backend.scraper.crawler import CrawlController, CrawlProgress, Crawler, PageFetcher
from backend.scraper.downloader import DownloadResult, DownloadStatus, Downloader

logger = logging.getLogger(__name__)

#: Fraction of overall progress attributed to the crawl phase before downloads start.
_CRAWL_PHASE_WEIGHT = 0.6

#: How :meth:`CrawlJob._store_download` classifies a successful download.
_StoreOutcome = Literal["new", "duplicate", "failed"]


@dataclass(frozen=True)
class CrawlJobSummary:
    """End-of-job counters (PRD §5.2 scan screen)."""

    job_id: int
    pages_total: int = 0
    pages_scanned: int = 0
    pages_skipped: int = 0
    pages_failed: int = 0
    comments_discovered: int = 0
    media_found: int = 0
    downloaded_new: int = 0
    skipped_duplicate: int = 0
    download_failed: int = 0
    download_limited: bool = False
    cancelled: bool = False
    failures: list[str] = field(default_factory=list)


def url_seen_in_source(conn: sqlite3.Connection) -> Callable[[str], bool]:
    """Level-1 URL duplicate callback: has this media URL already been collected? (PRD §12)."""

    def check(url: str) -> bool:
        try:
            row = conn.execute("SELECT 1 FROM source WHERE media_url = ? LIMIT 1", (url,)).fetchone()
        except sqlite3.Error:
            logger.exception("url duplicate check failed url=%s — treating as new", url)
            return False
        return row is not None

    return check


class CrawlJob:
    """One tracked crawl: jobs-row bookkeeping around :class:`~backend.scraper.crawler.Crawler`
    plus the download pass. Construct, then ``await run()``."""

    def __init__(
        self,
        url: str,
        scope: CrawlScope | None = None,
        *,
        settings: Settings,
        db: sqlite3.Connection,
        destination_dir: Path | None = None,
        force_rescan: bool = False,
        fetcher: PageFetcher | None = None,
        http_client: httpx.AsyncClient | None = None,
        url_already_downloaded: Callable[[str], bool] | None = None,
        ingest: bool = False,
        extra_params: dict[str, object] | None = None,
    ) -> None:
        self._url = url
        self._scope = scope if scope is not None else CrawlScope(ScopeKind.CURRENT_PAGE)
        self._settings = settings
        self._db = db
        self._destination_dir = destination_dir or settings.storage.media_directory
        self._force_rescan = force_rescan
        self._fetcher = fetcher
        self._http_client = http_client
        self._url_already_downloaded = url_already_downloaded
        self._ingest = ingest
        self._extra_params = extra_params or {}
        self._controller = CrawlController()
        self._job_id: int | None = None
        self._crawl_state = CrawlProgress()
        self._progress_value = 0.0

    @property
    def controller(self) -> CrawlController:
        return self._controller

    @property
    def url(self) -> str:
        """The entry URL this crawl was started from (pipeline auto-registration)."""
        return self._url

    @property
    def job_id(self) -> int | None:
        """The ``jobs.id`` once :meth:`run` has started, else ``None``."""
        return self._job_id

    def cancel(self) -> None:
        """Cooperatively stop after in-flight pages/downloads finish (PRD §5.2)."""
        self._controller.cancel()

    def pause(self) -> None:
        """Hold the job before its next page/download; pair with :meth:`resume`."""
        self._controller.pause()
        if self._job_id is None:
            return
        try:
            row = self._db.execute("SELECT status FROM jobs WHERE id = ?", (self._job_id,)).fetchone()
        except sqlite3.Error:
            logger.exception("job status lookup failed job_id=%s", self._job_id)
            return
        if row is not None and row["status"] == "running":
            self._update_progress(self._progress_value, "paused")

    def resume(self) -> None:
        self._controller.resume()

    async def run(self) -> CrawlJobSummary:
        """Run the crawl + download pipeline; returns the counter summary.

        Raises :class:`~backend.scraper.adapters.base.UnsupportedSiteError`
        before any job row exists, and re-raises unexpected pipeline errors
        after recording them on the job row (PRD §36).
        """
        get_adapter(self._url)  # fail fast — no job row for an unsupported site
        scope = self._scope
        self._create_job_row(scope)
        try:
            summary = await self._execute(scope)
        except Exception as exc:
            try:
                self._finish("failed", f"crawl failed: {exc}", error=str(exc))
            except sqlite3.Error:
                logger.exception("could not record failed job job_id=%s", self._job_id)
            raise
        status = "cancelled" if summary.cancelled else "completed"
        progress = 1.0 if not summary.cancelled else self._progress_value
        self._finish(status, _summary_message(summary), progress=progress)
        logger.info("crawl job finished job_id=%d status=%s new=%d dup=%d failed=%d",
                    summary.job_id, status, summary.downloaded_new,
                    summary.skipped_duplicate, summary.download_failed)
        return summary

    # -- pipeline ----------------------------------------------------------

    async def _execute(self, scope: CrawlScope) -> CrawlJobSummary:
        crawler = Crawler(
            settings=self._settings,
            db=self._db,
            controller=self._controller,
            fetcher=self._fetcher,
            on_progress=self._on_crawl_progress,
        )
        result = await crawler.crawl(self._url, scope, force_rescan=self._force_rescan)
        self._crawl_state = result.progress

        downloader = Downloader(
            destination_dir=self._destination_dir,
            settings=self._settings,
            client=self._http_client,
            url_already_downloaded=self._url_already_downloaded or url_seen_in_source(self._db),
        )
        new = duplicate = failed = 0
        download_limited = False
        limit = max(0, self._settings.crawler.download_limit)
        total_media = len(result.media)
        try:
            for media in result.media:
                if self._controller.cancelled:
                    break
                await self._controller.wait_while_paused()
                if self._controller.cancelled:
                    break
                if new + duplicate + failed >= limit:
                    download_limited = True
                    logger.info("download limit reached limit=%d", limit)
                    break
                outcome = await downloader.download(media)
                if outcome.status is DownloadStatus.OK:
                    if self._ingest:
                        stored = self._store_download(outcome, media)
                        if stored == "new":
                            new += 1
                        elif stored == "duplicate":
                            duplicate += 1
                        else:
                            failed += 1
                    else:
                        new += 1
                elif outcome.status is DownloadStatus.DUPLICATE_URL:
                    duplicate += 1
                else:
                    failed += 1
                    logger.warning("download failed url=%s reason=%s", media.url, outcome.error)
                self._report_download(new, duplicate, failed, total_media)
        finally:
            await downloader.aclose()

        return CrawlJobSummary(
            job_id=int(self._job_id or 0),
            pages_total=result.progress.pages_total,
            pages_scanned=result.progress.pages_scanned,
            pages_skipped=result.progress.pages_skipped,
            pages_failed=result.progress.pages_failed,
            comments_discovered=result.progress.comments_discovered,
            media_found=result.progress.media_found,
            downloaded_new=new,
            skipped_duplicate=duplicate,
            download_failed=failed,
            download_limited=download_limited,
            cancelled=self._controller.cancelled,
            failures=[failure.reason for failure in result.failures],
        )

    # -- jobs-row bookkeeping ---------------------------------------------

    def _store_download(self, outcome: DownloadResult, media: MediaRef) -> _StoreOutcome:
        """Persist one successful download (``ingest=True`` mode).

        ``new`` — unique item stored; ``duplicate`` — Level-2 exact duplicate
        (row recorded + flagged ``dup``, PRD §12); ``failed`` — storage error,
        recorded and counted without stopping the crawl (PRD §36).
        """
        try:
            media_id = ingest_download(
                self._db, outcome, media.comment, settings=self._settings
            )
        except (sqlite3.Error, OSError):
            logger.exception("storing download failed url=%s", media.url)
            return "failed"
        return "duplicate" if media_id is None else "new"

    def _create_job_row(self, scope: CrawlScope) -> None:
        # ``urls`` lets a restart resume custom_urls/multiple_chapters crawls
        # (backend/jobs/resume.py) — the other scope kinds ignore it.
        # ``extra_params`` tags owner-managed crawls (e.g. the site-wide
        # backfill) so the generic resume never relaunches them separately.
        params = json.dumps({"url": self._url, "scope": scope.kind.value,
                             "force_rescan": self._force_rescan,
                             "urls": list(scope.urls), **self._extra_params})
        with transaction(self._db):
            cursor = self._db.execute(
                "INSERT INTO jobs (job_type, status, progress, message, params, started_at) "
                "VALUES ('crawl', 'running', 0.0, 'starting', ?, datetime('now'))",
                (params,),
            )
        self._job_id = int(cursor.lastrowid)
        logger.info("crawl job created job_id=%d url=%s", self._job_id, self._url)

    def _on_crawl_progress(self, state: CrawlProgress) -> None:
        """Crawler progress callback: 0.0–0.6 of overall progress, key=value counters."""
        self._crawl_state = state
        if state.pages_total > 0:
            fraction = state.pages_handled / state.pages_total
        else:
            fraction = 1.0
        self._update_progress(_CRAWL_PHASE_WEIGHT * fraction, self._counters_message())

    def _report_download(self, new: int, duplicate: int, failed: int, total_media: int) -> None:
        """Download-phase progress: 0.6–1.0 of overall progress."""
        fraction = (new + duplicate + failed) / total_media if total_media else 1.0
        progress = _CRAWL_PHASE_WEIGHT + (1.0 - _CRAWL_PHASE_WEIGHT) * fraction
        message = f"{self._counters_message()} new={new} dup={duplicate} failed={failed}"
        self._update_progress(progress, message)

    def _counters_message(self) -> str:
        state = self._crawl_state
        return (
            f"pages={state.pages_handled}/{state.pages_total} "
            f"comments={state.comments_discovered} media={state.media_found}"
        )

    def _update_progress(self, progress: float, message: str) -> None:
        """Persist progress; telemetry failures are logged, never fatal (PRD §36).

        While the job is paused the message is pinned to ``"paused"`` so late
        progress callbacks from the page in flight cannot mask the state.
        """
        if self._job_id is None:
            return
        self._progress_value = progress
        if self._controller.paused:
            message = "paused"
        try:
            with transaction(self._db):
                self._db.execute(
                    "UPDATE jobs SET progress = ?, message = ? WHERE id = ?",
                    (progress, message, self._job_id),
                )
        except sqlite3.Error:
            logger.exception("job progress update failed job_id=%s", self._job_id)

    def _finish(self, status: str, message: str, *, progress: float | None = None,
                error: str | None = None) -> None:
        if self._job_id is None:
            return
        progress_value = self._progress_value if progress is None else progress
        with transaction(self._db):
            self._db.execute(
                "UPDATE jobs SET status = ?, progress = ?, message = ?, error = ?, "
                "completed_at = datetime('now') WHERE id = ?",
                (status, progress_value, message, error, self._job_id),
            )


def _summary_message(summary: CrawlJobSummary) -> str:
    """``key=value`` completion message for the jobs row (PRD §36/§5.2)."""
    return (
        f"pages={summary.pages_scanned}/{summary.pages_total} "
        f"skipped={summary.pages_skipped} page_errors={summary.pages_failed} "
        f"comments={summary.comments_discovered} media={summary.media_found} "
        f"new={summary.downloaded_new} dup={summary.skipped_duplicate} "
        f"failed={summary.download_failed}"
        + (" limit_reached" if summary.download_limited else "")
    )
