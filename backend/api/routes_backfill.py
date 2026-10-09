"""Site-backfill API — start/inspect/control a whole-catalog crawl (PRD §36).

Shown **separately** from the regular jobs history: a backfill is a long-lived
passive run (one comic at a time, resumable across restarts) whose item list —
"which comics are done" — is user-visible state, so it lives in its own
``backfill_*`` tables and its own endpoints (backend/jobs/backfill.py).

* ``POST /api/backfill/start`` — begin a backfill of a catalog index URL
  (default: the AsuraScans catalog). ``202`` with the new id; ``400`` for an
  unsupported site or when ``backfill.enabled=false``; ``409`` when a run is
  already live or a row is stranded ``running`` (cancel it first).
* ``GET  /api/backfill`` — newest-first history (latest run by default).
* ``GET  /api/backfill/{id}`` — one run plus per-status item counts and the
  ``live`` flag (is this process running it right now).
* ``GET  /api/backfill/{id}/items`` — the comic list, filterable/paged.
* ``POST /api/backfill/{id}/pause|resume|cancel`` — cooperative control of the
  live run; ``cancel`` also clears a stale ``running`` row (left behind by a
  crash) exactly like the jobs stale-cancel, and ``resume`` relaunches it.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from backend.database.database import transaction
from backend.jobs.backfill import (
    BackfillJob,
    ITEM_STATUSES,
    launch_backfill,
    live_backfill,
)
from backend.scraper.adapters import UnsupportedSiteError, get_adapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backfill", tags=["backfill"])

#: The catalog most recently proven to expose ``discover_series`` (AGENTS.md §5).
DEFAULT_CATALOG_URL = "https://asurascans.com/comics"


class BackfillOut(BaseModel):
    """One backfill run — the header the Jobs tab renders."""

    id: int
    site_url: str
    adapter_site: str
    status: str
    total: int
    done: int
    failed: int
    current_url: str | None = None
    current_title: str | None = None
    message: str | None = None
    error: str | None = None
    started_at: str
    completed_at: str | None = None
    #: True while this process has a live task for the run.
    live: bool = False


class BackfillDetailOut(BackfillOut):
    """One run plus its per-status item counts (pending/running/done/failed)."""

    counts: dict[str, int] = Field(default_factory=dict)


class BackfillListResponse(BaseModel):
    """``GET /api/backfill`` envelope."""

    items: list[BackfillOut]


class BackfillItemOut(BaseModel):
    """One comic in the run's work list."""

    id: int
    url: str
    title: str | None = None
    status: str
    error: str | None = None
    finished_at: str | None = None


class BackfillItemListResponse(BaseModel):
    """``GET /api/backfill/{id}/items`` envelope."""

    items: list[BackfillItemOut]
    total: int


class BackfillStartRequest(BaseModel):
    """Body for ``POST /api/backfill/start``."""

    url: str = Field(default=DEFAULT_CATALOG_URL, min_length=1, max_length=2048)


class BackfillActionResponse(BaseModel):
    """``pause``/``resume``/``cancel`` result — ``applied`` false on no-ops."""

    backfill: BackfillOut
    applied: bool


def _row_out(request: Request, row: sqlite3.Row) -> BackfillOut:
    live = live_backfill(request.app)
    return BackfillOut(
        id=int(row["id"]),
        site_url=str(row["site_url"]),
        adapter_site=str(row["adapter_site"]),
        status=str(row["status"]),
        total=int(row["total"]),
        done=int(row["done"]),
        failed=int(row["failed"]),
        current_url=row["current_url"],
        current_title=row["current_title"],
        message=row["message"],
        error=row["error"],
        started_at=str(row["started_at"]),
        completed_at=row["completed_at"],
        live=live is not None and live.backfill_id == int(row["id"]),
    )


def _get_row(request: Request, backfill_id: int) -> sqlite3.Row:
    row = request.app.state.db.execute(
        "SELECT * FROM backfill_jobs WHERE id = ?", (backfill_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"backfill {backfill_id} not found")
    return row


def _item_counts(db: sqlite3.Connection, backfill_id: int) -> dict[str, int]:
    counts = {status: 0 for status in ITEM_STATUSES}
    for row in db.execute(
        "SELECT status, COUNT(*) AS n FROM backfill_items "
        "WHERE backfill_id = ? GROUP BY status",
        (backfill_id,),
    ):
        counts[str(row["status"])] = int(row["n"])
    return counts


@router.post("/start", status_code=202)
async def backfill_start(request: Request, payload: BackfillStartRequest, response: Response) -> BackfillOut:
    """Begin a backfill of a catalog index — validated, then backgrounded."""
    settings = request.app.state.settings
    if not settings.backfill.enabled:
        raise HTTPException(status_code=400, detail="backfill is disabled (backfill.enabled=false)")
    try:
        adapter = get_adapter(payload.url)
    except UnsupportedSiteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if live_backfill(request.app) is not None:
        raise HTTPException(
            status_code=409, detail="a backfill is already running — cancel it first"
        )
    stranded = request.app.state.db.execute(
        "SELECT id FROM backfill_jobs WHERE status = 'running' ORDER BY id LIMIT 1"
    ).fetchone()
    if stranded is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"backfill {stranded['id']} is marked running but has no live process — "
                f"cancel it (POST /api/backfill/{stranded['id']}/cancel) or resume it first"
            ),
        )
    job = BackfillJob(request.app, payload.url)
    launch_backfill(request.app, job)
    assert job.backfill_id is not None  # launch_backfill creates the row synchronously
    row = _get_row(request, job.backfill_id)
    logger.info(
        "backfill start requested url=%s site=%s backfill_id=%d",
        payload.url, adapter.site, job.backfill_id,
    )
    return _row_out(request, row)


@router.get("", response_model=BackfillListResponse)
async def backfill_list(request: Request, limit: int = Query(10, ge=1, le=100)) -> BackfillListResponse:
    """Newest-first backfill history (the Jobs tab's separate section)."""
    rows = request.app.state.db.execute(
        "SELECT * FROM backfill_jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return BackfillListResponse(items=[_row_out(request, row) for row in rows])


@router.get("/{backfill_id}", response_model=BackfillDetailOut)
async def backfill_detail(request: Request, backfill_id: int) -> BackfillDetailOut:
    """One run plus per-status counts for its comic list."""
    row = _get_row(request, backfill_id)
    detail = BackfillDetailOut(**_row_out(request, row).model_dump())
    detail.counts = _item_counts(request.app.state.db, backfill_id)
    return detail


@router.get("/{backfill_id}/items", response_model=BackfillItemListResponse)
async def backfill_items(
    request: Request,
    backfill_id: int,
    status: str | None = Query(None, pattern="^(pending|running|done|failed)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> BackfillItemListResponse:
    """The run's comic list, oldest first (discovery order), paged."""
    _get_row(request, backfill_id)  # 404 before touching the item table
    where = "WHERE backfill_id = ?" + (" AND status = ?" if status else "")
    params: tuple[object, ...] = (backfill_id, status) if status else (backfill_id,)
    total = int(
        request.app.state.db.execute(
            f"SELECT COUNT(*) FROM backfill_items {where}", params
        ).fetchone()[0]
    )
    rows = request.app.state.db.execute(
        f"SELECT * FROM backfill_items {where} ORDER BY id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return BackfillItemListResponse(
        items=[
            BackfillItemOut(
                id=int(r["id"]), url=str(r["url"]), title=r["title"],
                status=str(r["status"]), error=r["error"], finished_at=r["finished_at"],
            )
            for r in rows
        ],
        total=total,
    )


@router.post("/{backfill_id}/pause", response_model=BackfillActionResponse)
async def backfill_pause(request: Request, backfill_id: int) -> BackfillActionResponse:
    """Hold the live run before its next comic."""
    return _control(request, backfill_id, "pause")


@router.post("/{backfill_id}/resume", response_model=BackfillActionResponse)
async def backfill_resume(request: Request, backfill_id: int) -> BackfillActionResponse:
    """Continue a paused run — or relaunch one stranded ``running`` by a crash."""
    return _control(request, backfill_id, "resume")


@router.post("/{backfill_id}/cancel", response_model=BackfillActionResponse)
async def backfill_cancel(request: Request, backfill_id: int) -> BackfillActionResponse:
    """Stop the live run (remaining comics stay ``pending``); clears stale rows too."""
    return _control(request, backfill_id, "cancel")


@router.post("/{backfill_id}/retry-failed", response_model=BackfillActionResponse)
async def backfill_retry_failed(request: Request, backfill_id: int) -> BackfillActionResponse:
    """Re-queue every ``failed`` comic and (re)start the run — the "retry failed" button.

    Failed comics go back to ``pending``; when no live process owns the run it
    is relaunched immediately (the row flips back to ``running`` so the UI
    reflects reality). A live run picks the re-queued comics up on its next
    iteration — no restart needed. No-op (``applied: false``) when nothing
    failed.
    """
    row = _get_row(request, backfill_id)
    db = request.app.state.db
    with transaction(db):
        cursor = db.execute(
            "UPDATE backfill_items SET status = 'pending', error = NULL, "
            "finished_at = NULL WHERE backfill_id = ? AND status = 'failed'",
            (backfill_id,),
        )
        retried = cursor.rowcount
        if retried and str(row["status"]) != "running":
            db.execute(
                "UPDATE backfill_jobs SET status = 'running', error = NULL, "
                "message = 'retrying failed comics', completed_at = NULL "
                "WHERE id = ?",
                (backfill_id,),
            )
    if retried and live_backfill(request.app) is None:
        job = BackfillJob(request.app, str(row["site_url"]), backfill_id=backfill_id)
        launch_backfill(request.app, job)
    if retried:
        logger.info("backfill retry-failed backfill_id=%d requeued=%d", backfill_id, retried)
    return BackfillActionResponse(
        backfill=_row_out(request, _get_row(request, backfill_id)), applied=retried > 0
    )


def _control(request: Request, backfill_id: int, action: str) -> BackfillActionResponse:
    """Apply a cooperative action; no-ops (finished run, unknown state) never raise."""
    row = _get_row(request, backfill_id)
    live = live_backfill(request.app)
    applied = False
    if live is not None and live.backfill_id == backfill_id:
        if action == "pause":
            live.pause()
        elif action == "resume":
            live.resume()
        else:
            live.cancel()
        applied = True
    elif action == "resume" and str(row["status"]) == "running":
        # Stranded row (crash/restart) with no live process — relaunch it now.
        job = BackfillJob(request.app, str(row["site_url"]), backfill_id=backfill_id)
        launch_backfill(request.app, job)
        applied = True
        logger.info("backfill relaunched from api backfill_id=%d", backfill_id)
    elif action == "cancel" and str(row["status"]) == "running":
        # Stale running row: record the stop so the UI can always clear it
        # (same guarantee as the jobs stale-cancel, routes_jobs._control).
        with transaction(request.app.state.db):
            request.app.state.db.execute(
                "UPDATE backfill_jobs SET status = 'cancelled', "
                "current_url = NULL, current_title = NULL, "
                "error = COALESCE(error, 'cancelled: backfill process no longer running'), "
                "completed_at = datetime('now') WHERE id = ?",
                (backfill_id,),
            )
            request.app.state.db.execute(
                "UPDATE backfill_items SET status = 'pending' "
                "WHERE backfill_id = ? AND status = 'running'",
                (backfill_id,),
            )
        applied = True
        logger.info("stale backfill cancelled by user backfill_id=%d", backfill_id)
    else:
        logger.info(
            "backfill control no-op action=%s backfill_id=%d status=%s live=%s",
            action, backfill_id, row["status"], live is not None,
        )
    return BackfillActionResponse(backfill=_row_out(request, _get_row(request, backfill_id)), applied=applied)
