"""Shared Pydantic models for the route modules (thin API layer — AGENTS.md §4).

``JobOut`` is used by both ``routes_jobs`` and ``routes_scraper``; media models
live in ``routes_media`` next to their endpoints. JSON columns stored as text
(``jobs.params``, ``media.user_tags``, ``ai_metadata`` array columns) are parsed
into structured shapes here — tolerant of missing/malformed values so one bad
row never breaks a response (PRD §36).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, Field


def parse_json_list(raw: Any) -> list[str]:
    """Decode a JSON-array text column into ``list[str]``; anything else → ``[]``."""
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def parse_json_object(raw: Any) -> dict[str, Any] | None:
    """Decode a JSON-object text column into a dict; anything else → ``None``."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class JobOut(BaseModel):
    """One row of the ``jobs`` table (PRD §35 progress display, §36 errors)."""

    id: int
    job_type: str
    status: str
    progress: float
    message: str | None = None
    error: str | None = None
    params: dict[str, Any] | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None


class JobListResponse(BaseModel):
    """``GET /api/jobs`` envelope — newest job first."""

    items: list[JobOut]


class JobActionResponse(BaseModel):
    """Result of pause/resume/cancel: the job row plus whether a handle acted on it."""

    job: JobOut
    applied: bool = Field(
        description="True when a live crawl job received the control action; "
        "false for finished, stale, or non-crawl jobs (no-op-safe)."
    )


def job_from_row(row: sqlite3.Row) -> JobOut:
    """Map a ``jobs`` row to :class:`JobOut` (``params`` JSON-tolerantly parsed)."""
    return JobOut(
        id=int(row["id"]),
        job_type=str(row["job_type"]),
        status=str(row["status"]),
        progress=float(row["progress"]),
        message=row["message"],
        error=row["error"],
        params=parse_json_object(row["params"]),
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )
