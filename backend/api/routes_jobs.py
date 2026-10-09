"""Jobs API — list background job progress and control live crawls (PRD §35, §36).

* ``GET /api/jobs`` — newest-first list with status/progress/message/error.
* ``POST /api/jobs/{id}/pause|resume|cancel`` — cooperative control of a
  **running crawl** via the handle registered on ``app.state.running_crawls``
  (started by ``POST /api/scrape``). The action is *no-op-safe* everywhere
  else: finished or stale crawl rows and other job types (thumbnail/dup_scan
  run to completion inside their starter task) simply return the unchanged row
  with ``applied: false`` — except ``cancel`` on a **stale** ``running`` row
  (no live process behind it, older than the registration grace period), which
  records the stop so a zombie job can always be cleared from the UI.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from backend.api.schemas import JobActionResponse, JobListResponse, job_from_row
from backend.database.database import transaction

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

#: A ``running`` row younger than this may simply be mid-registration (the live
#: handle lands right after the row appears — see ``start_crawl``); cancelling
#: before the grace period passes stays a no-op so that race can never mark a
#: live crawl cancelled.
_STALE_GRACE_SECONDS = 5

#: Job statuses written by the job modules (PRD §35/§36 lifecycle).
JobStatusFilter = Literal["running", "completed", "cancelled", "failed"]

#: Cooperative control actions supported for live crawl jobs.
JobAction = Literal["pause", "resume", "cancel"]


@router.get("", response_model=JobListResponse)
async def jobs_list(
    request: Request,
    status: JobStatusFilter | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> JobListResponse:
    """Paginated jobs history, newest first (PRD §35 progress display)."""
    where = "WHERE status = ?" if status is not None else ""
    params: tuple[object, ...] = (status, limit) if status is not None else (limit,)
    rows = request.app.state.db.execute(
        f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    return JobListResponse(items=[job_from_row(row) for row in rows])


@router.post("/{job_id}/pause", response_model=JobActionResponse)
async def jobs_pause(request: Request, job_id: int) -> JobActionResponse:
    """Hold a running crawl before its next page/download (PRD §5.2)."""
    return _control(request, job_id, "pause")


@router.post("/{job_id}/resume", response_model=JobActionResponse)
async def jobs_resume(request: Request, job_id: int) -> JobActionResponse:
    """Continue a paused crawl."""
    return _control(request, job_id, "resume")


@router.post("/{job_id}/cancel", response_model=JobActionResponse)
async def jobs_cancel(request: Request, job_id: int) -> JobActionResponse:
    """Cooperatively stop a running crawl after in-flight work finishes (PRD §5.2)."""
    return _control(request, job_id, "cancel")


def _control(request: Request, job_id: int, action: JobAction) -> JobActionResponse:
    """Apply ``action`` to a live crawl handle if one exists; never raises for no-ops.

    ``cancel`` additionally clears a **stale** ``running`` row: a job whose
    process died (crash, or a restart before job recovery ran) has no live
    handle and would otherwise sit at ``running`` forever with no way out from
    the UI (PRD §36 — the user can always record the stop themselves). The
    grace period keeps the just-created-row race from ever cancelling a crawl
    that is about to register its handle.
    """
    row = request.app.state.db.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    registry = getattr(request.app.state, "running_crawls", {})
    job = registry.get(job_id)
    applied = False
    if job is not None and row["job_type"] == "crawl":
        if action == "pause":
            job.pause()
        elif action == "resume":
            job.resume()
        else:
            job.cancel()
        applied = True
        row = request.app.state.db.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    elif (
        action == "cancel"
        and row["status"] == "running"
        and _is_stale(request.app.state.db, job_id)
    ):
        with transaction(request.app.state.db):
            request.app.state.db.execute(
                "UPDATE jobs SET status = 'cancelled', "
                "error = COALESCE(error, 'cancelled: job process no longer running'), "
                "completed_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
        row = request.app.state.db.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        applied = True
        logger.info("stale running job cancelled by user job_id=%d", job_id)
    else:
        logger.info(
            "job control no-op action=%s job_id=%d job_type=%s live_handle=%s",
            action, job_id, row["job_type"], job is not None,
        )
    return JobActionResponse(job=job_from_row(row), applied=applied)


def _is_stale(db, job_id: int) -> bool:
    """True when a ``running`` row is old enough that its handle cannot still be registering."""
    row = db.execute(
        "SELECT started_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None or row["started_at"] is None:
        return True
    registering = db.execute(
        "SELECT 1 FROM jobs WHERE id = ? AND started_at > datetime('now', ?)",
        (job_id, f"-{_STALE_GRACE_SECONDS} seconds"),
    ).fetchone()
    return registering is None
