"""AI analysis job — jobs-row lifecycle + pause/resume/cancel around ``run_ai_queue`` (PRD §35, §36).

``AIJob`` mirrors :class:`backend.jobs.crawl_job.CrawlJob`: construct it with the
database and settings, ``await run()``, and call :meth:`pause` / :meth:`resume` /
:meth:`cancel` while it runs. It delegates the actual work to
:func:`backend.ai.queue.run_ai_queue`, which owns the ``jobs`` row
(``job_type='ai_analysis'``); this class only supplies the provider and the
shared controller.

If no provider is passed, one is built from ``settings.ai.provider``. When that
fails — most commonly ``openrouter`` without ``OPENROUTER_API_KEY`` — the failure
is recorded on a ``failed`` jobs row (PRD §36) and the error re-raised so the
caller can surface it; a missing key never crashes the application.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from backend.ai.provider import AIUnavailableError, VisionProvider, create_provider
from backend.ai.queue import AIQueueController, AIQueueSummary, run_ai_queue
from backend.config import Settings
from backend.database.database import transaction

logger = logging.getLogger(__name__)


class AIJob:
    """One tracked AI run: provider setup + control surface around ``run_ai_queue``."""

    def __init__(
        self,
        *,
        db: sqlite3.Connection,
        settings: Settings,
        provider: VisionProvider | None = None,
        controller: AIQueueController | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._provider = provider
        self._owns_provider = provider is None
        self._controller = controller if controller is not None else AIQueueController()
        self._job_id: int | None = None

    @property
    def controller(self) -> AIQueueController:
        return self._controller

    @property
    def job_id(self) -> int | None:
        """The ``jobs.id`` once :meth:`run` has started, else ``None``."""
        return self._job_id

    def pause(self) -> None:
        """Hold the queue before its next batch; pair with :meth:`resume` (PRD §5.2)."""
        self._controller.pause()

    def resume(self) -> None:
        self._controller.resume()

    def cancel(self) -> None:
        """Stop after the current batch — in-flight items finish first."""
        self._controller.cancel()

    async def run(self) -> AIQueueSummary:
        """Run the AI queue to completion (or cooperative stop).

        Raises :class:`~backend.ai.provider.AIUnavailableError` before the queue
        starts when no provider can be built — recorded on a ``failed`` jobs row
        first (PRD §36). Provider-owned HTTP resources are always released.
        """
        provider = self._provider
        if provider is None:
            try:
                provider = create_provider(self._settings)
            except AIUnavailableError as exc:
                self._job_id = self._record_unavailable_start(str(exc))
                raise
            self._provider = provider
        try:
            return await run_ai_queue(
                self._db,
                provider,
                self._settings,
                controller=self._controller,
                on_job_created=self._capture_job_id,
            )
        finally:
            if self._owns_provider:
                await provider.aclose()

    # -- internals ---------------------------------------------------------

    def _capture_job_id(self, job_id: int) -> None:
        self._job_id = job_id

    def _record_unavailable_start(self, reason: str) -> int:
        """Record a failed AI job before it ever processed an item (PRD §36)."""
        params = json.dumps({"provider": self._settings.ai.provider})
        with transaction(self._db):
            cursor = self._db.execute(
                "INSERT INTO jobs (job_type, status, progress, message, error, params, "
                "started_at, completed_at) VALUES ('ai_analysis', 'failed', 0.0, "
                "'provider unavailable', ?, ?, datetime('now'), datetime('now'))",
                (reason, params),
            )
        job_id = int(cursor.lastrowid)
        logger.error("ai job cannot start job_id=%d reason=%s", job_id, reason)
        return job_id
