"""Site-wide backfill — walk a site's whole catalog one comic at a time (PRD §36 resumability).

A backfill is the "big passive job" surface: ``POST /api/backfill/start``
points it at a catalog index (e.g. ``https://asurascans.com/comics``), the
adapter enumerates every series (:meth:`~backend.scraper.adapters.base.SiteAdapter.discover_series`)
into ``backfill_items``, and each comic is then crawled **sequentially** through
the same shared pipeline a manual crawl uses (COLLECT → thumbnails → dup scan →
AI → FTS rebuild — :mod:`backend.jobs.pipeline`).

Designed to keep continuing:

* **Restart survival** — the parent row stays ``status='running'`` across a
  process death; :func:`resume_interrupted_backfill` (wired in
  backend/main.py) relaunches it at the next startup and re-queues the comic
  that was mid-flight (its item is flipped back to ``pending``).
* **Failure isolation** — a comic that fails is recorded and skipped; the run
  moves on (PRD §36). Crawl cancellation from the Jobs tab is external — that
  comic is marked ``failed`` so the loop cannot spin on it.
* **Cooperation** — it waits while any crawl it does not own is live
  (``app.state.running_crawls``) and yields between comics via
  ``backfill.delay_seconds``; pause/resume/cancel are cooperative, like
  :class:`~backend.jobs.crawl_job.CrawlJob`.

Its per-comic crawl rows carry ``params.owner='backfill'`` so the generic
startup crawl resume (:mod:`backend.jobs.resume`) never double-runs them.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import FastAPI

from backend.database.database import transaction
from backend.jobs.crawl_job import CrawlJob
from backend.jobs.pipeline import start_crawl
from backend.scraper.adapters import CrawlScope, ScopeKind, SiteAdapter, get_adapter
from backend.security.text import sanitize_optional_text

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    import httpx
    from backend.scraper.crawler import PageFetcher

logger = logging.getLogger(__name__)

#: Poll interval for pause holds and shutdown/cancel checks.
_POLL_SECONDS = 0.25

#: How long to wait between foreign-crawl checks before looking again.
_FOREIGN_WAIT_SECONDS = 5.0

#: Item statuses (mirrors the migration 006 CHECK constraint).
ITEM_STATUSES = ("pending", "running", "done", "failed")


@dataclass(frozen=True)
class BackfillSummary:
    """End-of-run counters for one backfill (tests + logs; the DB row is the UI source)."""

    backfill_id: int
    total: int = 0
    done: int = 0
    failed: int = 0
    cancelled: bool = False
    stopping: bool = False


class BackfillJob:
    """One site-wide backfill run: catalog discovery + sequential per-comic crawls.

    Construct with ``backfill_id`` to continue an existing (interrupted) row,
    or without to start fresh. Call :meth:`run` in a background task —
    :func:`launch_backfill` wires the app-state bookkeeping.
    """

    def __init__(
        self,
        app: FastAPI,
        index_url: str,
        *,
        backfill_id: int | None = None,
        fetcher: PageFetcher | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app = app
        self._index_url = index_url
        self._backfill_id = backfill_id
        self._fetcher = fetcher
        self._http_client = http_client
        self._paused = False
        self._cancelled = False
        self._stopping = False
        self._active_crawl: CrawlJob | None = None

    @property
    def backfill_id(self) -> int | None:
        """The ``backfill_jobs.id`` once :meth:`run` has created/claimed the row."""
        return self._backfill_id

    # -- cooperative control -------------------------------------------------

    def cancel(self) -> None:
        """Stop after in-flight work: the current crawl is cancelled too."""
        self._cancelled = True
        if self._active_crawl is not None:
            self._active_crawl.cancel()

    def stop(self) -> None:
        """Server shutdown: stop now, but leave the row ``running`` for resume."""
        self._stopping = True
        if self._active_crawl is not None:
            self._active_crawl.cancel()

    def pause(self) -> None:
        """Hold before the next comic (and pause the crawl in flight)."""
        self._paused = True
        if self._active_crawl is not None:
            self._active_crawl.pause()

    def resume(self) -> None:
        self._paused = False
        if self._active_crawl is not None:
            self._active_crawl.resume()

    @property
    def paused(self) -> bool:
        return self._paused

    # -- main loop -----------------------------------------------------------

    async def run(self) -> BackfillSummary:
        """Discover the catalog, then crawl every comic sequentially; never raises."""
        db = self._app.state.db
        try:
            adapter = get_adapter(self._index_url)
        except Exception as exc:
            # Defensive: the API validates before launching; a resume can hit
            # this when an adapter was removed — record, don't crash the task.
            logger.warning("backfill cannot resolve adapter url=%s error=%s", self._index_url, exc)
            return BackfillSummary(backfill_id=self._backfill_id or 0, failed=1)
        if self._backfill_id is None:
            self._backfill_id = self._create_row(db, adapter.site)
        else:
            self._requeue_stuck_items(db)
        logger.info(
            "backfill started backfill_id=%d url=%s", self._backfill_id, self._index_url
        )
        try:
            await self._discover(db, adapter)
        except Exception as exc:
            self._finish_row(db, "failed", error=f"discovery failed: {exc}")
            logger.warning("backfill discovery failed backfill_id=%d error=%s", self._backfill_id, exc)
            return BackfillSummary(backfill_id=int(self._backfill_id), failed=1)
        while not (self._cancelled or self._stopping):
            if not await self._wait_until_runnable():
                break
            item = self._next_pending(db)
            if item is None:
                break
            await self._crawl_comic(db, item)
            await self._rest_between_comics()
        return self._finish(db)

    # -- discovery -----------------------------------------------------------

    async def _discover(self, db: sqlite3.Connection, adapter: SiteAdapter) -> None:
        """Populate ``backfill_items`` from the catalog index (idempotent on resume)."""
        existing = int(
            db.execute(
                "SELECT COUNT(*) FROM backfill_items WHERE backfill_id = ?",
                (self._backfill_id,),
            ).fetchone()[0]
        )
        if existing:
            self._update_row(db, total=existing, message=f"resumed with {existing} comics")
            return
        series = await asyncio.to_thread(adapter.discover_series, self._index_url)
        if not series:
            raise RuntimeError(f"no comics discovered on {self._index_url}")
        with transaction(db):
            for ref in series:
                db.execute(
                    "INSERT OR IGNORE INTO backfill_items (backfill_id, url, title) "
                    "VALUES (?, ?, ?)",
                    (self._backfill_id, ref.url, sanitize_optional_text(ref.title)),
                )
        total = int(
            db.execute(
                "SELECT COUNT(*) FROM backfill_items WHERE backfill_id = ?",
                (self._backfill_id,),
            ).fetchone()[0]
        )
        self._update_row(db, total=total, message=f"discovered {total} comics")
        logger.info("backfill discovery done backfill_id=%d comics=%d", self._backfill_id, total)

    # -- per-comic crawl ------------------------------------------------------

    async def _crawl_comic(self, db: sqlite3.Connection, item: sqlite3.Row) -> None:
        """Run one comic's ENTIRE_COMIC crawl through the shared pipeline."""
        comic_id = int(item["id"])
        url = str(item["url"])
        title = item["title"]
        position = self._progress_message(db, prefix="comic")
        self._set_item_status(db, comic_id, "running")
        self._update_row(db, current_url=url, current_title=title, message=position)
        settings = self._app.state.settings
        job = CrawlJob(
            url,
            CrawlScope(ScopeKind.ENTIRE_COMIC),
            settings=settings,
            db=db,
            force_rescan=False,
            fetcher=self._fetcher,
            http_client=self._http_client,
            ingest=True,
            extra_params={"owner": "backfill", "backfill_id": self._backfill_id},
        )
        try:
            started = await start_crawl(self._app, job)
        except Exception as exc:
            self._mark_item(db, comic_id, "failed", error=f"crawl failed to start: {exc}")
            return
        self._active_crawl = job
        try:
            await started.task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # run_pipeline isolates stage errors; this is belt-and-braces
            self._mark_item(db, comic_id, "failed", error=str(exc))
            return
        finally:
            self._active_crawl = None
        row = db.execute(
            "SELECT status, error FROM jobs WHERE id = ?", (started.job_id,)
        ).fetchone()
        crawl_status = str(row["status"]) if row is not None else "failed"
        if crawl_status == "completed":
            self._mark_item(db, comic_id, "done")
        elif crawl_status == "cancelled" and (self._cancelled or self._stopping):
            # Cancelled *by us*: the comic was not finished — re-queue it so a
            # later resume picks it up again.
            self._set_item_status(db, comic_id, "pending")
        elif crawl_status == "cancelled":
            # Cancelled from the Jobs tab by the user: record it, move on —
            # otherwise the loop would spin on the same comic forever.
            self._mark_item(db, comic_id, "failed", error="crawl cancelled from the jobs tab")
        else:
            error = str(row["error"]) if row is not None and row["error"] else "crawl failed"
            self._mark_item(db, comic_id, "failed", error=error)

    # -- cooperation helpers ---------------------------------------------------

    async def _wait_until_runnable(self) -> bool:
        """Wait out pause holds and foreign crawls; ``False`` when the run should stop."""
        while True:
            if self._cancelled or self._stopping:
                return False
            if self._paused:
                await asyncio.sleep(_POLL_SECONDS)
                continue
            foreign = sorted(self._app.state.running_crawls)
            if not foreign:
                return True
            logger.debug(
                "backfill waiting for foreign crawls backfill_id=%s job_ids=%s",
                self._backfill_id, foreign,
            )
            await asyncio.sleep(_FOREIGN_WAIT_SECONDS)

    async def _rest_between_comics(self) -> None:
        """Configured pause between comics; cancellable and pause-aware."""
        delay = max(0.0, float(self._app.state.settings.backfill.delay_seconds))
        deadline = asyncio.get_running_loop().time() + delay
        while not (self._cancelled or self._stopping):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(_POLL_SECONDS, remaining))

    # -- database bookkeeping ---------------------------------------------------

    def _create_row(self, db: sqlite3.Connection, adapter_site: str) -> int:
        """Insert a fresh ``running`` row for a new backfill; returns its id."""
        with transaction(db):
            cursor = db.execute(
                "INSERT INTO backfill_jobs (site_url, adapter_site, status, message) "
                "VALUES (?, ?, 'running', 'starting')",
                (self._index_url, adapter_site),
            )
        return int(cursor.lastrowid)

    def _finish(self, db: sqlite3.Connection) -> BackfillSummary:
        counts = self._counts(db)
        total, done, failed = counts["total"], counts["done"], counts["failed"]
        if self._stopping:
            # Keep the row running: the next startup resumes exactly here.
            self._update_row(
                db, total=total, done=done, failed=failed,
                message="interrupted — resumes at next startup",
                current_url=None, current_title=None,
            )
            logger.info("backfill stopped for shutdown backfill_id=%d done=%d", self._backfill_id, done)
        elif self._cancelled:
            self._finish_row(
                db, "cancelled", total=total, done=done, failed=failed,
                message=f"cancelled after {done} comics",
            )
        else:
            self._finish_row(
                db, "completed", total=total, done=done, failed=failed,
                message=f"finished: {done} crawled, {failed} failed",
            )
            logger.info(
                "backfill completed backfill_id=%d total=%d done=%d failed=%d",
                self._backfill_id, total, done, failed,
            )
        return BackfillSummary(
            backfill_id=int(self._backfill_id or 0),
            total=total, done=done, failed=failed,
            cancelled=self._cancelled, stopping=self._stopping,
        )

    def _counts(self, db: sqlite3.Connection) -> dict[str, int]:
        """Live counters straight from the item rows (never drifts from reality)."""
        rows = db.execute(
            "SELECT status, COUNT(*) AS n FROM backfill_items "
            "WHERE backfill_id = ? GROUP BY status",
            (self._backfill_id,),
        ).fetchall()
        found = {str(row["status"]): int(row["n"]) for row in rows}
        return {
            "total": sum(found.values()),
            "done": found.get("done", 0),
            "failed": found.get("failed", 0),
            "pending": found.get("pending", 0),
            "running": found.get("running", 0),
        }

    def _next_pending(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        return db.execute(
            "SELECT id, url, title FROM backfill_items "
            "WHERE backfill_id = ? AND status = 'pending' ORDER BY id LIMIT 1",
            (self._backfill_id,),
        ).fetchone()

    def _progress_message(self, db: sqlite3.Connection, *, prefix: str) -> str:
        counts = self._counts(db)
        finished = counts["done"] + counts["failed"]
        return f"{prefix} {finished + 1}/{counts['total']}"

    def _set_item_status(
        self, db: sqlite3.Connection, item_id: int, status: str
    ) -> None:
        finished_at = "datetime('now')" if status in ("done", "failed") else "NULL"
        with transaction(db):
            db.execute(
                f"UPDATE backfill_items SET status = ?, error = NULL, "
                f"finished_at = {finished_at} WHERE id = ?",
                (status, item_id),
            )

    def _mark_item(
        self, db: sqlite3.Connection, item_id: int, status: str, *, error: str | None = None
    ) -> None:
        finished_at = "datetime('now')" if status in ("done", "failed") else "NULL"
        with transaction(db):
            db.execute(
                f"UPDATE backfill_items SET status = ?, error = ?, "
                f"finished_at = {finished_at} WHERE id = ?",
                (status, sanitize_optional_text(error), item_id),
            )

    def _requeue_stuck_items(self, db: sqlite3.Connection) -> None:
        """Resume hygiene: items left ``running`` by a crash go back to ``pending``."""
        with transaction(db):
            cursor = db.execute(
                "UPDATE backfill_items SET status = 'pending', error = NULL "
                "WHERE backfill_id = ? AND status = 'running'",
                (self._backfill_id,),
            )
        if cursor.rowcount:
            logger.info(
                "backfill requeued interrupted comics backfill_id=%d count=%d",
                self._backfill_id, cursor.rowcount,
            )

    def _update_row(self, db: sqlite3.Connection, **fields: object) -> None:
        """Persist progress fields; telemetry failures are logged, never fatal (PRD §36)."""
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        try:
            with transaction(db):
                db.execute(
                    f"UPDATE backfill_jobs SET {assignments} WHERE id = ?",
                    (*fields.values(), self._backfill_id),
                )
        except sqlite3.Error:
            logger.exception("backfill row update failed backfill_id=%s", self._backfill_id)

    def _finish_row(
        self, db: sqlite3.Connection, status: str, *, error: str | None = None,
        total: int | None = None, done: int | None = None, failed: int | None = None,
        message: str | None = None,
    ) -> None:
        with transaction(db):
            db.execute(
                "UPDATE backfill_jobs SET status = ?, error = ?, message = ?, "
                "current_url = NULL, current_title = NULL, "
                "total = COALESCE(?, total), done = COALESCE(?, done), "
                "failed = COALESCE(?, failed), completed_at = datetime('now') "
                "WHERE id = ?",
                (status, sanitize_optional_text(error), message,
                 total, done, failed, self._backfill_id),
            )


# ---------------------------------------------------------------------------
# App-state wiring (mirrors ``app.state.running_crawls`` for crawls).
# ---------------------------------------------------------------------------


def live_backfill(app: FastAPI) -> BackfillJob | None:
    """The backfill currently running in this process, if any."""
    job = getattr(app.state, "backfill_job", None)
    return job if isinstance(job, BackfillJob) else None


def launch_backfill(app: FastAPI, job: BackfillJob) -> asyncio.Task[None]:
    """Background ``job.run()``, tracked on ``app.state.backfill_job`` until it ends.

    The ``backfill_jobs`` row is created **synchronously** here (not inside the
    task) so callers like the start endpoint can read ``job.backfill_id``
    immediately after this returns. Exactly one backfill runs at a time —
    callers must have checked :func:`live_backfill` / the ``running`` rows
    first (the API turns a duplicate start into a 409).
    """
    if job.backfill_id is None:
        adapter = get_adapter(job._index_url)
        job._backfill_id = job._create_row(app.state.db, adapter.site)
    app.state.backfill_job = job
    task = asyncio.create_task(_run_tracked(app, job))
    app.state.backfill_task = task
    return task


async def _run_tracked(app: FastAPI, job: BackfillJob) -> None:
    try:
        await job.run()
    finally:
        if live_backfill(app) is job:
            app.state.backfill_job = None


def resume_interrupted_backfill(app: FastAPI) -> bool:
    """Relaunch a backfill row stranded ``running`` by a crash/restart.

    Returns ``True`` when a resume was launched. Gated by
    ``backfill.enabled`` and by there being no live run already — startup
    ordering makes a ``running`` row without a live task unambiguous
    ("interrupted"), the same assumption recovery makes for ``jobs``.
    """
    settings = app.state.settings
    if not settings.backfill.enabled:
        return False
    if live_backfill(app) is not None:
        return False
    row = app.state.db.execute(
        "SELECT id, site_url FROM backfill_jobs WHERE status = 'running' "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return False
    job = BackfillJob(app, str(row["site_url"]), backfill_id=int(row["id"]))
    launch_backfill(app, job)
    logger.info(
        "interrupted backfill resumed backfill_id=%s url=%s", row["id"], row["site_url"]
    )
    return True
