"""Startup auto-resume tests — interrupted crawls restart "when they can" (PRD §36).

Unit level: ``params`` parsing/eligibility. Integration level: a recovered
crawl row is relaunched through the shared pipeline (offline via the fixture
fetcher), owner-managed (backfill) rows are skipped, and orphaned processing
stages are re-run exactly when nothing else would cover them.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx

from backend.config import Settings, load_settings
from backend.database.database import transaction
from backend.jobs.recovery import recover_interrupted_jobs
from backend.jobs.resume import (
    crawl_params,
    resumable_crawl_spec,
    resume_interrupted_work,
)
from backend.jobs.watched_comics import ensure_watched_comic
from tests.fixtures.fake_adapter import FakeSiteAdapter, FixtureFetcher
from tests.watch_support import registered, state_app

ENTRY_URL = "https://fixture.test/comic/chapter-9?page=1"
PAGE_HTML = "<html><body></body></html>"


class _StaticFetcher(FixtureFetcher):
    """Serves exactly the one entry page the resumed crawl fetches."""

    def __init__(self) -> None:
        super().__init__({ENTRY_URL: PAGE_HTML})


def _crawl_params(**overrides: object) -> str:
    payload: dict[str, object] = {
        "url": ENTRY_URL,
        "scope": "current_page",
        "force_rescan": False,
        "urls": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _recovered(params: str, *, job_type: str = "crawl", job_id: int = 7) -> list[dict[str, object]]:
    return [{"id": job_id, "job_type": job_type, "params": params}]


def _settings(tmp_path: Path, **watch_overrides: object) -> Settings:
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "resume.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    crawler = replace(base.crawler, delay_seconds=0.0, concurrency=1)
    ai = replace(base.ai, provider="mock")
    watch = replace(base.watch, **{"enabled": False, **watch_overrides})
    return replace(base, storage=storage, crawler=crawler, ai=ai, watch=watch)


def _insert_running_crawl(db, params: str, *, job_id: int = 7) -> int:
    with transaction(db):
        db.execute(
            "INSERT INTO jobs (id, job_type, status, progress, message, params, started_at) "
            "VALUES (?, 'crawl', 'running', 0.4, 'pages=2/5', ?, datetime('now'))",
            (job_id, params),
        )
    return job_id


# ---------------------------------------------------------------------------
# Unit: params parsing / eligibility
# ---------------------------------------------------------------------------


def test_crawl_params_parses_valid_json() -> None:
    assert crawl_params(_crawl_params())["url"] == ENTRY_URL


def test_crawl_params_rejects_unusable_values() -> None:
    assert crawl_params(None) is None
    assert crawl_params(42) is None
    assert crawl_params("") is None
    assert crawl_params("{not json") is None
    assert crawl_params('["a", "list"]') is None


def test_resumable_spec_requires_a_url() -> None:
    assert resumable_crawl_spec(_crawl_params(url="")) is None
    assert resumable_crawl_spec(_crawl_params()) is not None


def test_resumable_spec_skips_backfill_owned_rows() -> None:
    assert resumable_crawl_spec(_crawl_params(owner="backfill", backfill_id=3)) is None


# ---------------------------------------------------------------------------
# Integration: relaunch through the shared pipeline
# ---------------------------------------------------------------------------


def test_interrupted_crawl_is_relaunched_and_annotated(db, tmp_path: Path) -> None:
    app = state_app(db, _settings(tmp_path))
    old_id = _insert_running_crawl(db, _crawl_params())
    # Production order: recovery fails the stranded row first, then resumes.
    recovered = recover_interrupted_jobs(db)
    fetcher = _StaticFetcher()

    with registered(FakeSiteAdapter()):
        resumed = asyncio.run(
            resume_interrupted_work(app, recovered, fetcher=fetcher)
        )

    assert resumed == 1
    rows = db.execute(
        "SELECT id, status, params, error FROM jobs WHERE job_type = 'crawl' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    old = next(row for row in rows if row["id"] == old_id)
    new = next(row for row in rows if row["id"] != old_id)
    assert old["status"] == "failed"
    assert "interrupted by server restart" in old["error"]
    assert "resumed as crawl job" in old["error"]
    assert json.loads(new["params"])["url"] == ENTRY_URL


def test_backfill_owned_rows_are_left_for_the_backfill_resume(db, tmp_path: Path) -> None:
    app = state_app(db, _settings(tmp_path))
    params = _crawl_params(owner="backfill", backfill_id=4)
    _insert_running_crawl(db, params)

    resumed = asyncio.run(resume_interrupted_work(app, _recovered(params)))

    assert resumed == 0
    # Only the original (now failed) row exists — the backfill resumes it itself.
    rows = db.execute("SELECT id FROM jobs WHERE job_type = 'crawl'").fetchall()
    assert [row["id"] for row in rows] == [7]


def test_non_crawl_rows_are_never_relaunched(db, tmp_path: Path) -> None:
    app = state_app(db, _settings(tmp_path))

    resumed = asyncio.run(
        resume_interrupted_work(app, _recovered(_crawl_params(), job_type="dup_scan"))
    )

    assert resumed == 0
    # No crawl relaunch — the only rows are the orphaned-stage re-run
    # (thumbnail/dup/index), which always happens when no crawl resumed.
    crawls = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_type = 'crawl'"
    ).fetchone()[0]
    assert crawls == 0
    types = {
        row["job_type"] for row in db.execute("SELECT job_type FROM jobs").fetchall()
    }
    assert {"thumbnail", "dup_scan", "index_rebuild"} <= types


def test_orphaned_stages_rerun_when_nothing_else_covers_them(db, tmp_path: Path) -> None:
    app = state_app(db, _settings(tmp_path))  # watcher disabled

    asyncio.run(resume_interrupted_work(app, []))

    types = {
        row["job_type"]
        for row in db.execute("SELECT job_type FROM jobs").fetchall()
    }
    assert {"thumbnail", "dup_scan", "index_rebuild"} <= types


def test_orphaned_stage_rerun_defers_to_the_watcher(db, tmp_path: Path) -> None:
    app = state_app(db, _settings(tmp_path, enabled=True))
    ensure_watched_comic(db, "https://fixture.test/comic", "fixture")

    asyncio.run(resume_interrupted_work(app, []))

    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
