"""Background AI supervisor — resilient queue runs with live status (PRD §18, §36).

The scrape pipeline runs the AI queue as one pipeline stage; this supervisor
owns the *background* half of the same job. Its loop:

* picks up every claimable leftover on its first iteration (startup recovery
  for interrupted runs, rows stranded by a crash, work stored before a restart);
* rebuilds the FTS index after any run that produced READY rows, so keyword
  and tag search see fresh descriptions immediately (the pipeline path does
  the same via its INDEX stage — PRD §57);
* waits out per-item retry backoffs (:mod:`backend.ai.queue` — deferred rows
  re-enter claimable when ``ai_next_retry_at`` passes);
* re-checks provider availability every 30s so fixing a config problem
  recovers without a restart;
* exposes the live state machine behind ``GET /api/ai/status``.

Loop states (``status["state"]``):

* ``processing``  — a queue run is active: this supervisor's own run, or the
  scrape pipeline's while it owns :class:`backend.ai.runner.AIQueueGate`;
* ``on_hold``     — deferred items exist and the earliest ``ai_next_retry_at``
  is in the future; the payload carries a live ``retry_in_seconds`` countdown,
  ``next_retry_at`` and the ``last_error`` that caused the deferral;
* ``idle``        — no claimable work; sleeps ``settings.ai.supervisor_poll_seconds``
  and wakes on :meth:`nudge` (fresh crawl items) or :meth:`stop`;
* ``unavailable`` — ``create_provider`` failed (missing key, unknown provider);
  ``reason`` explains it and the next attempt happens within 30s;
* ``stopped``     — not running (before :meth:`run` starts, after :meth:`stop`).

Guard: every run goes through :class:`~backend.ai.runner.AIQueueGate` — the
same ``app.state.ai_queue_running`` flag the scrape pipeline's AI stage uses —
so the two paths never double-run. Providers are created per queue run and
always ``aclose()``d in ``finally``. The loop catches everything (single-tick
failures are logged, never fatal) and logs only state transitions
(``ai supervisor state=old→new reason=…``), so polling never floods the log.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from backend.ai.provider import AIProviderError, create_provider
from backend.ai.queue import (
    AIQueueController,
    AIQueueSummary,
    count_claimable,
    run_ai_queue,
)
from backend.ai.runner import AIQueueGate
from backend.config import Settings
from backend.search.keyword import rebuild_fts_index

logger = logging.getLogger(__name__)

#: How often an ``unavailable`` supervisor re-checks whether a provider can be built.
_UNAVAILABLE_RECHECK_SECONDS = 30.0

#: Never sleep longer than this between status re-checks (keeps nudges cheap).
_MAX_SLEEP_SECONDS = 300.0

_DONE_PATTERN = re.compile(r"done=(\d+)/(\d+)")
_READY_PATTERN = re.compile(r"\bready=(\d+)")
_FAILED_PATTERN = re.compile(r"\bfailed=(\d+)")
_DEFERRED_PATTERN = re.compile(r"\bdeferred=(\d+)")
_LAST_ERROR_PATTERN = re.compile(r"last_error=(.*)")


def _counter(pattern: re.Pattern[str], message: str, group: int, default: int = 0) -> int:
    match = pattern.search(message)
    return int(match.group(group)) if match else default


def latest_ai_job(db: sqlite3.Connection) -> tuple[dict[str, int] | None, str | None]:
    """Newest ``ai_analysis`` job row → (``/api/ai/status`` job fragment, last_error).

    The fragment/status are parsed from the ``jobs`` counters both this
    supervisor and the scrape pipeline's AI stage write, so the payload stays
    correct no matter who ran the queue. ``last_error`` prefers the job's
    ``error`` column (crashed runs), then the ``last_error=…`` counter
    (per-item failures and deferrals). ``None`` means no run has happened.
    """
    row = db.execute(
        "SELECT id, message, error FROM jobs "
        "WHERE job_type = 'ai_analysis' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None, None
    message = str(row["message"] or "")
    fragment = {
        "id": int(row["id"]),
        "done": _counter(_DONE_PATTERN, message, 1),
        "total": _counter(_DONE_PATTERN, message, 2),
        "ready": _counter(_READY_PATTERN, message, 1),
        "failed": _counter(_FAILED_PATTERN, message, 1),
        "deferred": _counter(_DEFERRED_PATTERN, message, 1),
    }
    error = str(row["error"] or "") or None
    if error is None:
        match = _LAST_ERROR_PATTERN.search(message)
        error = match.group(1).strip() or None if match else None
    return fragment, error


def utc_now_text() -> str:
    """SQLite ``datetime('now')`` format: UTC, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _seconds_until(timestamp: str) -> int:
    """Whole seconds from now until ``timestamp`` (UTC, ``datetime('now')`` format)."""
    target = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return max(0, int((target - now).total_seconds()))


class AISupervisor:
    """The background loop: claim → run → hold/idle, with a live status payload."""

    def __init__(self, db: sqlite3.Connection, settings: Settings, running_flag: AIQueueGate) -> None:
        """``running_flag`` is the shared :class:`AIQueueGate` over ``app.state.ai_queue_running``."""
        self._db = db
        self._settings = settings
        self._gate = running_flag
        self._wake = asyncio.Event()
        self._stopping = False
        self._controller: AIQueueController | None = None
        self._state = "idle"
        self._reason: str | None = None

    # -- public surface -----------------------------------------------------

    async def run(self) -> None:
        """Main loop; catches everything so one bad tick never kills the supervisor."""
        self._transition("idle", reason="supervisor started")
        try:
            while not self._stopping:
                self._wake.clear()
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("ai supervisor tick failed error=%s", exc)
                    await self._sleep(self._settings.ai.supervisor_poll_seconds)
        finally:
            self._controller = None
            self._transition("stopped", reason="supervisor stopped")

    def nudge(self) -> None:
        """Wake the loop immediately (new crawl items — don't wait out a poll/backoff)."""
        self._wake.set()

    @property
    def wake_event(self) -> asyncio.Event:
        """The event :meth:`nudge` sets — surfaced as ``app.state.ai_nudge`` so tests
        (or other components) can wake the loop directly with ``event.set()``."""
        return self._wake

    async def stop(self) -> None:
        """Graceful shutdown: cancel an active run, wake the loop, mark stopped."""
        self._stopping = True
        if self._controller is not None:
            self._controller.cancel()
        self._wake.set()

    @property
    def status(self) -> dict[str, Any]:
        """The ``GET /api/ai/status`` payload — every field JSON-serializable, never the key."""
        ai = self._settings.ai
        job, last_error = latest_ai_job(self._db)
        next_retry_at = self._next_retry_at()
        return {
            "state": self._state,
            "reason": self._reason if self._state == "unavailable" else None,
            "provider": ai.provider,
            "model": ai.model,
            "embedding_model": ai.embedding_model,
            "key_present": bool(ai.api_key),
            "retry_in_seconds": _seconds_until(next_retry_at) if next_retry_at else None,
            "next_retry_at": next_retry_at,
            "last_error": last_error,
            "job": job,
            "updated_at": utc_now_text(),
        }

    # -- loop steps ---------------------------------------------------------

    async def _tick(self) -> None:
        claimable = count_claimable(self._db)
        if claimable == 0:
            await self._idle_or_hold()
            return
        if not self._gate.try_acquire():
            # The scrape pipeline's AI stage owns the queue — wait, never double-run.
            self._transition("processing", reason="scrape pipeline owns the queue")
            await self._sleep(self._settings.ai.supervisor_poll_seconds)
            return
        provider = None
        summary: AIQueueSummary | None = None
        try:
            provider = create_provider(self._settings)
        except AIProviderError as exc:
            self._gate.release()
            self._transition("unavailable", reason=str(exc))
            await self._sleep(_UNAVAILABLE_RECHECK_SECONDS)
            return
        except Exception as exc:
            self._gate.release()
            self._transition("unavailable", reason=f"provider setup failed: {exc}")
            await self._sleep(_UNAVAILABLE_RECHECK_SECONDS)
            return
        self._transition("processing", reason=f"claimable={claimable}")
        controller = AIQueueController()
        self._controller = controller
        try:
            summary = await run_ai_queue(self._db, provider, self._settings, controller=controller)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # run_ai_queue recorded the crash on its jobs row before re-raising (PRD §36).
            logger.warning("ai supervisor queue run failed error=%s", exc)
        finally:
            self._controller = None
            # Release before the awaited close: a cancellation landing inside
            # ``aclose`` must never strand the shared flag as True.
            self._gate.release()
            try:
                await provider.aclose()
            except Exception as exc:
                logger.warning("ai supervisor provider close failed error=%s", exc)
        if summary is not None and not summary.cancelled:
            logger.info(
                "ai supervisor run finished job_id=%d ready=%d failed=%d deferred=%d",
                summary.job_id, summary.ready, summary.failed, summary.deferred,
            )
            if summary.ready > 0:
                await self._reindex()
        await self._idle_or_hold()

    async def _reindex(self) -> None:
        """Refresh keyword/tag search after a run that stored new analysis (PRD §57).

        Mirrors the pipeline's INDEX stage: without this, search self-heal would
        only fire on row-count divergence and stale descriptions would stay
        searchable until the next crawl. Failures are logged, never fatal.
        """
        try:
            indexed = await asyncio.to_thread(rebuild_fts_index, self._db)
            logger.info("ai supervisor reindexed fts indexed=%d", indexed)
        except Exception as exc:
            logger.warning("ai supervisor fts reindex failed error=%s", exc)

    async def _idle_or_hold(self) -> None:
        """Settle into ``on_hold`` (deferred rows wait) or ``idle``, then sleep."""
        next_retry_at = self._next_retry_at()
        if next_retry_at is not None:
            self._transition("on_hold", reason="deferred items awaiting retry")
            wait = min(float(_seconds_until(next_retry_at)),
                       self._settings.ai.supervisor_poll_seconds, _MAX_SLEEP_SECONDS)
            await self._sleep(max(0.0, wait))
            return
        self._transition("idle", reason="no claimable work")
        await self._sleep(min(self._settings.ai.supervisor_poll_seconds, _MAX_SLEEP_SECONDS))

    async def _sleep(self, seconds: float) -> None:
        """Timed wait; :meth:`nudge` and :meth:`stop` cut it short."""
        if self._stopping or seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _transition(self, state: str, *, reason: str) -> None:
        """Record a state change; log only actual transitions (no per-poll spam)."""
        if state == self._state and reason == self._reason:
            return
        previous = self._state
        self._state, self._reason = state, reason
        logger.info("ai supervisor state=%s→%s reason=%s", previous, state, reason)

    def _next_retry_at(self) -> str | None:
        """Earliest ``ai_next_retry_at`` still in the future, if any deferred row exists."""
        row = self._db.execute(
            "SELECT MIN(ai_next_retry_at) AS next_retry_at FROM media "
            "WHERE ai_next_retry_at > datetime('now')"
        ).fetchone()
        value = row["next_retry_at"] if row is not None else None
        return str(value) if value else None
