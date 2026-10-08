"""AI analysis queue worker (PRD §18, §19, §36) — COLLECT/STORE vs PROCESS separation.

One async worker drains media into the searchable state, bounded by
``settings.ai.ai_concurrency`` (batch-parallel ``asyncio.gather``; a failed item
never stops the rest of the queue — PRD §36).

**Status flow** (``media.processing_status``, each transition in its own short
transaction — never held across an ``await``)::

    DOWNLOADED ──▶ ANALYZING ──▶ ANALYZED ──▶ EMBEDDING ──▶ READY
         │              │             │            │
         └──────────────┴─────▶ FAILED ◀──────────┘
              ▲               retryable failure + attempt budget spent
              │
              └── retry re-entry: a *retryable* provider error while the
                  budget lasts parks the row (``ai_next_retry_at`` backoff)
                  back at DOWNLOADED/ANALYZED instead of failing it

* **claim** — rows are claimed by flipping ``DOWNLOADED``/``ANALYZED``/
  stale ``ANALYZING``/``EMBEDDING`` → ``ANALYZING``. ``ANALYZED`` and stale rows
  are the **backfill** path: they already have an ``ai_metadata`` row, so vision
  is skipped and only the embedding is (re)generated — embeddings are produced
  for every item the provider can serve. Rows whose ``ai_next_retry_at`` is in
  the future (deferred by a previous run) are **excluded** — they belong to the
  next run, which the background supervisor schedules. One queue instance at a
  time is assumed (the M5 job layer runs a single AI job, guarded by
  ``app.state.ai_queue_running``), which makes status-based claiming safe.
* **analysis** — image → ``analyze_image``; GIF → :func:`~backend.ai.frames.sample_frames`
  → ``analyze_gif``; ``ai_metadata`` row upserted + ``ANALYZED`` in one
  transaction.
* **embedding** — text = description + tags (fallback: original filename), then
  ``embeddings`` row upserted + ``READY`` in one transaction. If ``ai_metadata``
  already exists (backfill/retry), vision is skipped and the item resumes at
  ``EMBEDDING``.
* **retry** — a provider error flagged ``retryable`` (429/5xx/timeout,
  reasoning-only answer, malformed model JSON) does **not** fail the item while
  ``ai_attempts + 1 < settings.ai.max_item_attempts``: the row is bumped to
  ``ai_attempts + 1`` and parked with
  ``ai_next_retry_at = now + min(retry_interval_seconds * 2**attempts, retry_interval_max_seconds)``,
  returning to ``ANALYZED`` when vision already succeeded (the retry only redoes
  the embedding — vision tokens are not spent twice) or ``DOWNLOADED``
  otherwise. Reaching the budget is terminal: ``FAILED`` with ``attempts=k`` in
  the reason. Any success (``READY``) resets ``ai_attempts``/``ai_next_retry_at``.
* **video** — download-only in the MVP (PRD §44), so videos go straight to
  ``FAILED`` with reason ``"video analysis not supported"``; they never get an
  ``ai_metadata`` row and are not claimed again (``FAILED`` is terminal for the
  queue; retry via :func:`process_single_media`).
* **errors** — any per-item exception ends in ``FAILED`` + reason in the
  returned summary, the logs, and the jobs ``message`` counters
  (``last_error=…``, since the schema has no per-media error column); retryable
  failures are counted as ``deferred`` instead of failed until the budget runs
  out. The queue keeps going either way (PRD §36).

**Embedding serialization** — ``embeddings.embedding`` BLOB format (documented
for M4 readers): raw little-endian IEEE-754 **float32** bytes of the vector in
C order (numpy ``dtype="<f4"``), i.e. ``len(blob) == dimension * 4``; the vector
dimension is recoverable from the blob length alone. The producing model is
stored alongside in ``embeddings.embedding_model`` (e.g.
``"openai/text-embedding-3-small"`` or ``"mock/384"``). Use
:func:`serialize_embedding` / :func:`deserialize_embedding` instead of touching
the bytes directly.

Jobs telemetry (PRD §35): every run creates a ``jobs`` row
(``job_type='ai_analysis'``) with ``progress = done/total`` and ``key=value``
message counters, finishing ``completed`` / ``cancelled`` / ``failed``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from backend.ai.frames import guess_mime_type, is_gif, is_video, sample_frames
from backend.ai.provider import AIProviderError, AIResponseError, VisionProvider
from backend.config import Settings
from backend.database.database import transaction

logger = logging.getLogger(__name__)

#: Reason recorded for videos (MVP: downloaded but not analyzed, PRD §44).
VIDEO_UNSUPPORTED_REASON = "video analysis not supported"

#: Statuses the queue claims for (re)processing: new work, embedding backfill,
#: and rows stranded by a crashed run.
CLAIMABLE_STATUSES: tuple[str, ...] = ("DOWNLOADED", "ANALYZED", "ANALYZING", "EMBEDDING")

#: Bytes per float in an ``embeddings.embedding`` BLOB (little-endian float32).
EMBEDDING_BYTES_PER_VALUE = 4


def serialize_embedding(vector: Sequence[float]) -> bytes:
    """Serialize a vector to the ``embeddings.embedding`` BLOB format (module docstring)."""
    return np.asarray(vector, dtype="<f4").tobytes(order="C")


def deserialize_embedding(blob: bytes) -> np.ndarray:
    """Inverse of :func:`serialize_embedding`; raises ``ValueError`` on a malformed blob."""
    if len(blob) == 0 or len(blob) % EMBEDDING_BYTES_PER_VALUE != 0:
        raise ValueError(f"embedding blob length {len(blob)} is not a positive multiple of 4")
    return np.frombuffer(blob, dtype="<f4")


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of processing exactly one media item.

    ``deferred`` marks a retryable failure parked for a later run (budget not
    spent) — neither a success nor a terminal failure.
    """

    media_id: int
    status: str
    error: str | None = None
    deferred: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "READY"


@dataclass(frozen=True)
class AIQueueSummary:
    """End-of-run counters and recorded per-item failures (PRD §35/§36).

    ``deferred`` counts items parked for a retry in a later run;
    ``last_error`` is the most recent failure *or* deferral reason (the jobs
    message and the ``/api/ai/status`` payload surface it).
    """

    job_id: int
    total: int
    ready: int
    failed: int
    cancelled: bool = False
    failures: list[str] = field(default_factory=list)
    deferred: int = 0
    last_error: str | None = None

    @property
    def done(self) -> int:
        return self.ready + self.failed


class AIQueueController:
    """Cooperative pause/resume/cancel for one queue run (``asyncio.Event`` based).

    Pause/cancel take effect at the next batch boundary — items already in
    flight finish first, so no row is ever stranded mid-status.
    """

    def __init__(self) -> None:
        self._resume_event = asyncio.Event()
        self._paused = False
        self._cancelled = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._resume_event.set()

    def cancel(self) -> None:
        """Stop after the current batch; also unblocks a paused queue."""
        self._cancelled = True
        self._paused = False
        self._resume_event.set()

    async def wait_while_paused(self) -> None:
        """Block until :meth:`resume` or :meth:`cancel`; re-checks after every wakeup."""
        while True:
            self._resume_event.clear()
            if not self._paused or self._cancelled:
                return
            await self._resume_event.wait()


async def run_ai_queue(
    db: sqlite3.Connection,
    provider: VisionProvider,
    settings: Settings,
    *,
    controller: AIQueueController | None = None,
    on_job_created: Callable[[int], None] | None = None,
) -> AIQueueSummary:
    """Drain all claimable media through analysis + embedding; the M5 API entry point.

    Creates and finalizes the ``jobs`` row (``job_type='ai_analysis'``);
    ``on_job_created`` is notified of the ``jobs.id`` as soon as it exists (the
    :class:`~backend.jobs.ai_job.AIJob` façade uses it to expose ``job_id``
    during the run). Unexpected pipeline errors are recorded on the job row and
    re-raised (PRD §36); per-item failures never raise.
    """
    controller = controller if controller is not None else AIQueueController()
    job_id = _create_job_row(db, provider)
    if on_job_created is not None:
        on_job_created(job_id)
    try:
        summary = await _drain(db, provider, settings, controller, job_id)
    except Exception as exc:
        logger.exception("ai queue crashed job_id=%d", job_id)
        try:
            _finish_job(db, job_id, status="failed", message=f"ai analysis failed: {exc}",
                        progress=0.0, error=str(exc))
        except sqlite3.Error:
            logger.exception("could not record failed ai job job_id=%d", job_id)
        raise
    if summary.cancelled:
        status, progress = "cancelled", _fraction(summary.done, summary.total)
    else:
        status, progress = "completed", 1.0
    _finish_job(db, job_id, status=status, message=_summary_message(summary), progress=progress)
    logger.info(
        "ai queue finished job_id=%d status=%s total=%d ready=%d failed=%d deferred=%d",
        job_id, status, summary.total, summary.ready, summary.failed, summary.deferred,
    )
    return summary


async def process_single_media(
    conn: sqlite3.Connection,
    media_id: int,
    provider: VisionProvider,
    settings: Settings,
) -> ProcessResult:
    """Run the pipeline for one media id — retry of a ``FAILED`` item or a single backfill.

    Works from any status (``ANALYZING`` is re-set to mark the attempt); vision
    is skipped when ``ai_metadata`` already exists, so retrying an embedding
    failure costs no vision tokens. Raises ``ValueError`` for an unknown id;
    media-level failures are returned as ``ProcessResult(error=…)``, never
    raised. A retryable provider failure can come back
    ``deferred=True`` (the row returns to ``DOWNLOADED``/``ANALYZED`` for the
    background queue); success clears ``ai_attempts``/``ai_next_retry_at``.
    """
    row = conn.execute(
        "SELECT id, file_path, mime_type, extension FROM media WHERE id = ?", (media_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no media row with id={media_id}")
    with transaction(conn):
        conn.execute("UPDATE media SET processing_status = 'ANALYZING' WHERE id = ?", (media_id,))
    return await _process(
        conn, media_id, row["file_path"], row["mime_type"], row["extension"], provider, settings
    )


# -- worker loop -----------------------------------------------------------


async def _drain(
    db: sqlite3.Connection,
    provider: VisionProvider,
    settings: Settings,
    controller: AIQueueController,
    job_id: int,
) -> AIQueueSummary:
    total = count_claimable(db)
    ready = failed = deferred = 0
    failures: list[str] = []
    last_error: str | None = None
    while True:
        if controller.cancelled:
            return AIQueueSummary(job_id, total, ready, failed, True, failures, deferred, last_error)
        if controller.paused:
            _update_job(db, job_id, _fraction(ready + failed, total), "paused")
            await controller.wait_while_paused()
            if controller.cancelled:
                return AIQueueSummary(job_id, total, ready, failed, True, failures, deferred, last_error)
        batch = _claim_batch(db, limit=max(1, settings.ai.ai_concurrency))
        if not batch:
            # Nothing claimable *now*: deferred rows wait for their next run
            # (the supervisor), so this run ends instead of looping on them.
            break
        results = await asyncio.gather(
            *(_process(db, row["id"], row["file_path"], row["mime_type"], row["extension"],
                       provider, settings)
              for row in batch)
        )
        for result in results:
            if result.ok:
                ready += 1
            elif result.deferred:
                deferred += 1
                last_error = f"media_id={result.media_id}: {result.error}"
            else:
                failed += 1
                failure = f"media_id={result.media_id}: {result.error}"
                failures.append(failure)
                last_error = failure
        _update_job(db, job_id, _fraction(ready + failed, total),
                    _progress_message(ready + failed, total, ready, failed, deferred, last_error))
    return AIQueueSummary(job_id, total, ready, failed, False, failures, deferred, last_error)


async def _process(
    db: sqlite3.Connection,
    media_id: int,
    file_path: str,
    mime_type: str | None,
    extension: str | None,
    provider: VisionProvider,
    settings: Settings,
) -> ProcessResult:
    """Full pipeline for a row already flipped to ``ANALYZING``; never raises for item errors.

    Retryable provider failures are parked for a later run while the item's
    ``settings.ai.max_item_attempts`` budget lasts (module docstring — retry
    re-entry); everything else ends ``FAILED``.
    """
    try:
        if is_video(mime_type, extension):
            logger.info("video skipped by ai queue media_id=%d", media_id)
            _set_status(db, media_id, "FAILED")
            return ProcessResult(media_id, "FAILED", error=VIDEO_UNSUPPORTED_REASON)
        if not _has_analysis(db, media_id):
            analysis = await _analyze_file(Path(file_path), mime_type, extension, provider)
            _store_analysis(db, media_id, analysis, provider)
        _set_status(db, media_id, "EMBEDDING")
        description, tags = _load_analysis(db, media_id)
        original_filename = db.execute(
            "SELECT original_filename FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        text = _embedding_text(description, tags, original_filename["original_filename"]
                               if original_filename else None)
        vector = await provider.generate_embedding(text)
        _store_embedding(db, media_id, vector, provider.embedding_model)
        _set_status(db, media_id, "READY")
        logger.info("ai analysis ready media_id=%d provider=%s", media_id, provider.name)
        return ProcessResult(media_id, "READY")
    except AIProviderError as exc:
        reason = str(exc)
        attempts = _load_attempts(db, media_id)
        if exc.retryable and attempts + 1 < settings.ai.max_item_attempts:
            delay = _retry_delay(
                settings.ai.retry_interval_seconds,
                attempts,
                settings.ai.retry_interval_max_seconds,
            )
            _defer_item(db, media_id, delay)
            logger.info(
                "ai item deferred media_id=%d attempts=%d delay=%ds reason=%s",
                media_id, attempts + 1, delay, reason,
            )
            return ProcessResult(media_id, "DEFERRED", error=reason, deferred=True)
        if exc.retryable:
            reason = f"{reason} (attempts={attempts + 1} of {settings.ai.max_item_attempts})"
        logger.warning("ai processing failed media_id=%d reason=%s", media_id, reason)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception("unexpected ai pipeline error media_id=%d", media_id)
    _set_status(db, media_id, "FAILED")
    return ProcessResult(media_id, "FAILED", error=reason)


async def _analyze_file(
    path: Path,
    mime_type: str | None,
    extension: str | None,
    provider: VisionProvider,
) -> dict[str, Any]:
    """Read the file and run the right provider call (GIFs via sampled frames)."""
    if is_gif(mime_type, extension):
        frames = await asyncio.to_thread(sample_frames, path)
        if not frames:
            raise AIResponseError(f"no decodable frames path={path}")
        logger.debug("gif frames sampled path=%s count=%d", path, len(frames))
        return await provider.analyze_gif(frames)
    image_bytes = await asyncio.to_thread(path.read_bytes)
    return await provider.analyze_image(image_bytes, mime_type=guess_mime_type(mime_type, extension))


def _embedding_text(description: str, tags: list[str], original_filename: str | None) -> str:
    """Embeddable text: description + tags, falling back to the filename (PRD §21C)."""
    text = " ".join(part for part in [description, *tags] if part).strip()
    if text:
        return text
    return original_filename or "untagged media"


def _validate_embedding(vector: Any) -> list[float]:
    if not isinstance(vector, (list, tuple)) or not vector:
        raise AIResponseError("provider returned an empty embedding")
    if not all(isinstance(value, (int, float)) for value in vector):
        raise AIResponseError("provider returned a non-numeric embedding")
    return [float(value) for value in vector]


# -- database helpers ------------------------------------------------------


def count_claimable(db: sqlite3.Connection) -> int:
    """Rows claimable *right now*: claimable status **and** not waiting on a backoff.

    Shared with :class:`backend.ai.supervisor.AISupervisor`, which applies the
    same predicate to decide whether a queue run is warranted.
    """
    placeholders = ",".join("?" for _ in CLAIMABLE_STATUSES)
    row = db.execute(
        f"SELECT COUNT(*) AS count FROM media "
        f"WHERE processing_status IN ({placeholders}) "
        f"AND (ai_next_retry_at IS NULL OR ai_next_retry_at <= datetime('now'))",
        CLAIMABLE_STATUSES,
    ).fetchone()
    return int(row["count"])


def _claim_batch(db: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Select up to ``limit`` claimable rows and flip them to ``ANALYZING`` atomically."""
    placeholders = ",".join("?" for _ in CLAIMABLE_STATUSES)
    with transaction(db):
        rows = db.execute(
            f"SELECT id, file_path, mime_type, extension FROM media "
            f"WHERE processing_status IN ({placeholders}) "
            f"AND (ai_next_retry_at IS NULL OR ai_next_retry_at <= datetime('now')) "
            f"ORDER BY id LIMIT ?",
            (*CLAIMABLE_STATUSES, limit),
        ).fetchall()
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        db.execute(
            f"UPDATE media SET processing_status = 'ANALYZING' "
            f"WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        )
    return list(rows)


def _has_analysis(db: sqlite3.Connection, media_id: int) -> bool:
    return db.execute("SELECT 1 FROM ai_metadata WHERE media_id = ?", (media_id,)).fetchone() is not None


def _load_analysis(db: sqlite3.Connection, media_id: int) -> tuple[str, list[str]]:
    row = db.execute(
        "SELECT description, tags FROM ai_metadata WHERE media_id = ?", (media_id,)
    ).fetchone()
    if row is None:  # guarded by _has_analysis upstream; keep the failure typed
        raise AIResponseError(f"ai_metadata row vanished media_id={media_id}")
    try:
        tags = json.loads(row["tags"]) if row["tags"] else []
    except (TypeError, json.JSONDecodeError):
        raise AIResponseError(f"ai_metadata.tags is not valid JSON media_id={media_id}") from None
    return str(row["description"] or ""), tags


def _set_status(db: sqlite3.Connection, media_id: int, status: str) -> None:
    """One status transition per transaction (PRD §19)."""
    with transaction(db):
        db.execute("UPDATE media SET processing_status = ? WHERE id = ?", (status, media_id))


def _load_attempts(db: sqlite3.Connection, media_id: int) -> int:
    """Spent retry budget of one item (0 when the row vanished — upstream guards it)."""
    row = db.execute("SELECT ai_attempts FROM media WHERE id = ?", (media_id,)).fetchone()
    return int(row["ai_attempts"]) if row is not None else 0


def _retry_delay(base_seconds: float, attempts: int, max_seconds: float) -> int:
    """Whole-second backoff for retry ``attempts`` (0-based): base doubling, capped.

    Never below 1s so a deferred row always lands in the *next* run instead of
    being re-claimed by the drain loop that parked it.
    """
    delay = min(max(0.0, base_seconds) * (2**attempts), max(0.0, max_seconds))
    return max(1, int(round(delay)))


def _defer_item(db: sqlite3.Connection, media_id: int, delay_seconds: int) -> None:
    """Park a retryable failure: bump the budget, set ``ai_next_retry_at``, restore
    a claimable status — ``ANALYZED`` when vision already produced an
    ``ai_metadata`` row (the retry only redoes the embedding), else ``DOWNLOADED``.
    """
    status = "ANALYZED" if _has_analysis(db, media_id) else "DOWNLOADED"
    with transaction(db):
        db.execute(
            "UPDATE media SET ai_attempts = ai_attempts + 1, "
            "ai_next_retry_at = datetime('now', '+' || ? || ' seconds'), "
            "processing_status = ? WHERE id = ?",
            (delay_seconds, status, media_id),
        )


def _store_analysis(
    db: sqlite3.Connection,
    media_id: int,
    analysis: dict[str, Any],
    provider: VisionProvider,
) -> None:
    """Persist ``ai_metadata`` and flip ``ANALYZING → ANALYZED`` in one transaction."""
    with transaction(db):
        db.execute(
            "INSERT OR REPLACE INTO ai_metadata "
            "(media_id, description, tags, emotions, subjects, meme_context, "
            " suggested_search_phrases, ai_provider, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                media_id,
                analysis["description"],
                json.dumps(analysis["tags"]),
                json.dumps(analysis["emotions"]),
                json.dumps(analysis["subjects"]),
                analysis["meme_context"],
                json.dumps(analysis["suggested_search_phrases"]),
                provider.name,
                provider.model,
            ),
        )
        db.execute("UPDATE media SET processing_status = 'ANALYZED' WHERE id = ?", (media_id,))


def _store_embedding(
    db: sqlite3.Connection,
    media_id: int,
    vector: list[float],
    embedding_model: str,
) -> None:
    """Persist the embedding BLOB and flip ``EMBEDDING → READY`` in one transaction.

    ``READY`` also clears the retry budget (``ai_attempts``/``ai_next_retry_at``)
    so a future manual reanalyze starts from a clean slate.
    """
    blob = serialize_embedding(_validate_embedding(vector))
    with transaction(db):
        db.execute(
            "INSERT OR REPLACE INTO embeddings (media_id, embedding, embedding_model) VALUES (?, ?, ?)",
            (media_id, blob, embedding_model),
        )
        db.execute(
            "UPDATE media SET processing_status = 'READY', "
            "ai_attempts = 0, ai_next_retry_at = NULL WHERE id = ?",
            (media_id,),
        )


# -- jobs telemetry --------------------------------------------------------


def _create_job_row(db: sqlite3.Connection, provider: VisionProvider) -> int:
    params = json.dumps(
        {"provider": provider.name, "model": provider.model,
         "embedding_model": provider.embedding_model}
    )
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO jobs (job_type, status, progress, message, params, started_at) "
            "VALUES ('ai_analysis', 'running', 0.0, 'starting', ?, datetime('now'))",
            (params,),
        )
    return int(cursor.lastrowid)


def _update_job(db: sqlite3.Connection, job_id: int, progress: float, message: str) -> None:
    """Persist progress; telemetry failures are logged, never fatal (PRD §36)."""
    try:
        with transaction(db):
            db.execute(
                "UPDATE jobs SET progress = ?, message = ? WHERE id = ?",
                (progress, message, job_id),
            )
    except sqlite3.Error:
        logger.exception("job progress update failed job_id=%d", job_id)


def _finish_job(
    db: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    message: str,
    progress: float,
    error: str | None = None,
) -> None:
    with transaction(db):
        db.execute(
            "UPDATE jobs SET status = ?, progress = ?, message = ?, error = ?, "
            "completed_at = datetime('now') WHERE id = ?",
            (status, progress, message, error, job_id),
        )


def _fraction(done: int, total: int) -> float:
    return done / total if total > 0 else 0.0


def _progress_message(
    done: int, total: int, ready: int, failed: int, deferred: int, last_error: str | None
) -> str:
    message = f"done={done}/{total} ready={ready} failed={failed} deferred={deferred}"
    if last_error is not None:
        message += f" last_error={last_error[:200]}"
    return message


def _summary_message(summary: AIQueueSummary) -> str:
    message = (
        f"done={summary.done}/{summary.total} ready={summary.ready} "
        f"failed={summary.failed} deferred={summary.deferred}"
    )
    if summary.cancelled:
        message += " cancelled"
    if summary.last_error:
        message += f" last_error={summary.last_error[:200]}"
    return message
