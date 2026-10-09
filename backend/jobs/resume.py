"""Startup auto-resume — interrupted crawls restart "when they can" (PRD §36).

:func:`backend.jobs.recovery.recover_interrupted_jobs` fails every ``jobs``
row stranded ``running`` by a dead process and hands this module the list.
Nothing of *this* process is live yet at lifespan time, so relaunching is safe:

* **crawls** are rebuilt from their ``params`` JSON (url / scope /
  force_rescan / urls — written by ``CrawlJob._create_job_row``) and re-run
  through the shared pipeline. The crawl is idempotent: ``force_rescan=False``
  lets ``crawl_history`` skip already-seen pages, so the relaunch only does
  the work the dead process never finished. The old failed row keeps its
  history and gets a "resumed as job N" note.
* **backfill-owned crawls** (``params.owner == "backfill"``) are skipped —
  the site-wide backfill resumes itself from its own table
  (:func:`backend.jobs.backfill.resume_interrupted_backfill`) and would
  otherwise process the same comic twice.
* **processing stages** (thumbnail / dup scan / index rebuild) left orphaned
  are re-run once, but only when no crawl was relaunched *and* the passive
  watcher will not cover them anyway (its first pass runs the same chain) —
  no duplicate ``jobs``-row noise on a normal boot. AI leftovers have their
  own startup recovery (the supervisor), so they are never touched here.

Everything runs inside one background task so startup is never blocked; the
lifespan cancels it on shutdown (backend/main.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import TYPE_CHECKING

from backend.database.database import transaction
from backend.jobs.crawl_job import CrawlJob
from backend.jobs.dup_job import run_dup_job
from backend.jobs.index_job import run_index_job
from backend.jobs.pipeline import start_crawl
from backend.jobs.recovery import INTERRUPTED_ERROR
from backend.jobs.thumbnail_job import run_thumbnail_job
from backend.scraper.adapters import CrawlScope, ScopeKind

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    import httpx

    from backend.scraper.crawler import PageFetcher

    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def crawl_params(raw: object) -> dict[str, object] | None:
    """Parsed ``jobs.params`` JSON for one recovered row; ``None`` when unusable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("interrupted job has malformed params — not resumable params=%s", raw[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


def resumable_crawl_spec(raw_params: object) -> dict[str, object] | None:
    """The ``CrawlJob`` constructor spec for a recovered crawl row, or ``None``.

    ``None`` covers: unparsable params, a missing url, and backfill-owned rows
    (the backfill's own resume owns those — see module docstring).
    """
    params = crawl_params(raw_params)
    if params is None or not params.get("url"):
        return None
    if params.get("owner") == "backfill":
        return None
    return params


async def resume_interrupted_work(
    app: FastAPI,
    recovered: list[dict[str, object]],
    *,
    fetcher: PageFetcher | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    """Relaunch recovered crawl rows sequentially; returns how many were started.

    Sequential (awaited one after another) to respect the AGENTS.md §9
    crawl-rate limits; per-row failures are logged and skipped, never fatal.
    ``fetcher``/``http_client`` are offline-injection seams for tests —
    production builds its own inside :class:`~backend.jobs.crawl_job.CrawlJob`.
    """
    resumed = 0
    for row in recovered:
        if row["job_type"] != "crawl":
            continue
        spec = resumable_crawl_spec(row["params"])
        if spec is None:
            logger.info(
                "interrupted crawl not resumable (owner-managed or no params) job_row=%s",
                row["id"],
            )
            continue
        try:
            scope_kind = ScopeKind(str(spec.get("scope", ScopeKind.CURRENT_PAGE.value)))
            urls = tuple(str(u) for u in spec.get("urls", []) if u)
            job = CrawlJob(
                str(spec["url"]),
                CrawlScope(scope_kind, urls),
                settings=app.state.settings,
                db=app.state.db,
                force_rescan=bool(spec.get("force_rescan", False)),
                fetcher=fetcher,
                http_client=http_client,
                ingest=True,
            )
            started = await start_crawl(app, job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("crawl resume failed job_row=%s error=%s", row["id"], exc)
            continue
        resumed += 1
        _annotate_resumed(app.state.db, int(row["id"]), started.job_id)
        logger.info(
            "interrupted crawl resumed old_job=%s new_job=%d url=%s",
            row["id"], started.job_id, spec["url"],
        )
        try:
            await started.task
        except asyncio.CancelledError:
            raise
        except Exception:
            # The crawl already recorded its own failure row; keep going.
            logger.warning("resumed crawl ended with error new_job=%d", started.job_id)
    if resumed == 0:
        await _run_orphaned_stages(app)
    return resumed


def _annotate_resumed(db: sqlite3.Connection, old_job_id: int, new_job_id: int) -> None:
    """Note the replacement job on the failed row so the history stays traceable."""
    with transaction(db):
        db.execute(
            "UPDATE jobs SET error = COALESCE(error, ?) || ? WHERE id = ?",
            (INTERRUPTED_ERROR, f" — resumed as crawl job {new_job_id}", old_job_id),
        )


async def _run_orphaned_stages(app: FastAPI) -> None:
    """Re-run thumbnail/dup/index once when nothing else will (module docstring)."""
    if _watch_will_process(app):
        logger.info("orphaned stage rerun skipped: the passive watcher will cover them")
        return
    for name, runner in (
        ("thumbnail", run_thumbnail_job),
        ("dup_scan", run_dup_job),
    ):
        try:
            await runner(db=app.state.db, settings=app.state.settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("orphaned stage rerun failed stage=%s error=%s", name, exc)
    try:
        await run_index_job(app.state.db)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("orphaned stage rerun failed stage=index_rebuild error=%s", exc)


def _watch_will_process(app: FastAPI) -> bool:
    """True when the passive watcher's first pass will run the same stage chain."""
    if not app.state.settings.watch.enabled:
        return False
    row = app.state.db.execute(
        "SELECT 1 FROM watched_comics WHERE enabled = 1 LIMIT 1"
    ).fetchone()
    return row is not None
