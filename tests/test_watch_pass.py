"""Watch pass tests — skip guard, page cap, incremental discovery, shared pipeline (PRD §39).

Everything offline: the ``fixture.test`` adapter discovers pages from local
listing HTML through a :class:`FixtureFetcher`, downloads go through an
``httpx.MockTransport``, and the app is a duck-typed state stand-in (the
pipeline only touches ``app.state``). The pass is driven with
``asyncio.run(run_watch_pass(...))`` — the same seam production uses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from backend.database.database import transaction
from backend.jobs.watch import run_watch_pass
from backend.jobs.watched_comics import ensure_watched_comic, set_watched_enabled
from tests.fixtures.fake_adapter import FixtureFetcher
from tests.test_crawl_job import media_handler
from tests.watch_support import (
    EMPTY_LISTING_HTML,
    ENTRY_URL,
    chapter_listing,
    chapter_page_urls,
    listing_adapter,
    registered,
    state_app,
    watch_settings,
)

#: A comment with one attachment on the "new" chapter page (PRD §6: only this
#: attachment is collected — the surrounding markup is page chrome).
COMMENT_PAGE_HTML = (
    '<html><body>'
    '<article class="comment" data-comment-id="919101">'
    '<img class="attachment" src="https://cdn.example.com/new-meme.png"/>'
    '</article>'
    '</body></html>'
)

#: The five job types every watch scan must chain through the shared pipeline.
PIPELINE_JOB_TYPES = ("crawl", "thumbnail", "dup_scan", "ai_analysis", "index_rebuild")


async def _run_pass(app, **kwargs):
    """Run one pass, always closing an injected HTTP client on the same loop."""
    client = kwargs.pop("http_client", None)
    if client is None:
        return await run_watch_pass(app, **kwargs)
    try:
        return await run_watch_pass(app, http_client=client, **kwargs)
    finally:
        await client.aclose()


def _job_types(db) -> dict[str, str]:
    rows = db.execute("SELECT job_type, status FROM jobs ORDER BY id").fetchall()
    return {row["job_type"]: row["status"] for row in rows}


# ---------------------------------------------------------------------------
# Skip guard: passive never runs alongside a user crawl
# ---------------------------------------------------------------------------


def test_pass_skips_entirely_while_a_user_crawl_is_live(db, tmp_path: Path) -> None:
    app = state_app(db, watch_settings(tmp_path))
    with registered(listing_adapter(chapter_listing(("/comic/chapter-1", 2)))):
        ensure_watched_comic(db, ENTRY_URL, "fixture")
        pages = {url: "<html></html>" for url in chapter_page_urls(ENTRY_URL, "/comic/chapter-1", 2)}
        fetcher = FixtureFetcher(pages)
        app.state.running_crawls[99] = object()  # a user-initiated crawl is live

        summary = asyncio.run(_run_pass(app, fetcher=fetcher))

        assert summary.skipped is True
        assert summary.skip_reason is not None and "job_ids=[99]" in summary.skip_reason
        assert summary.scanned == []
        assert fetcher.fetched == [], "no page may be fetched while a user crawl runs"
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        last = db.execute("SELECT last_scanned_at FROM watched_comics").fetchone()[0]
        assert last is None, "a skipped pass must not mark the comic as scanned"


def test_pass_with_no_watched_comics_is_a_noop(db, tmp_path: Path) -> None:
    app = state_app(db, watch_settings(tmp_path))

    summary = asyncio.run(_run_pass(app))

    assert summary.scanned == [] and summary.skipped is False
    assert summary.failures == []
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# max_pages_per_run bound
# ---------------------------------------------------------------------------


def test_pass_respects_max_pages_per_run(db, tmp_path: Path) -> None:
    app = state_app(db, watch_settings(tmp_path, max_pages_per_run=1))
    with registered(listing_adapter(chapter_listing(("/comic/chapter-1", 3)))):
        comic_id, _ = ensure_watched_comic(db, ENTRY_URL, "fixture")
        urls = chapter_page_urls(ENTRY_URL, "/comic/chapter-1", 3)
        fetcher = FixtureFetcher({url: "<html></html>" for url in urls})

        summary = asyncio.run(_run_pass(app, fetcher=fetcher))

        assert summary.scanned == [comic_id]
        assert summary.failures == []
        assert fetcher.fetched == [urls[0]], "discovery is capped at watch.max_pages_per_run"
        params = json.loads(
            db.execute("SELECT params FROM jobs WHERE job_type = 'crawl'").fetchone()["params"]
        )
        # ``urls`` is written so a restart can resume multi-chapter crawls.
        assert params == {
            "url": ENTRY_URL,
            "scope": "entire_comic",
            "force_rescan": False,
            "urls": [],
        }
        assert db.execute("SELECT COUNT(*) FROM crawl_history").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Incremental: only pages never seen before are fetched (PRD §38, §39)
# ---------------------------------------------------------------------------


def test_pass_only_fetches_pages_not_yet_in_history(db, tmp_path: Path) -> None:
    app = state_app(db, watch_settings(tmp_path))
    listing = chapter_listing(("/comic/chapter-1", 2), ("/comic/chapter-2", 1))
    with registered(listing_adapter(listing)):
        comic_id, _ = ensure_watched_comic(db, ENTRY_URL, "fixture")
        seen = chapter_page_urls(ENTRY_URL, "/comic/chapter-1", 2)
        fresh = chapter_page_urls(ENTRY_URL, "/comic/chapter-2", 1)
        # What a previous crawl recorded for this comic (crawl_history, PRD §38).
        with transaction(db):
            for url in seen:
                db.execute(
                    "INSERT INTO crawl_history (url, site, pages_scanned, media_found) "
                    "VALUES (?, 'fixture', 2, 4)",
                    (url,),
                )
        fetcher = FixtureFetcher({url: "<html></html>" for url in seen + fresh})

        summary = asyncio.run(_run_pass(app, fetcher=fetcher))

        assert fetcher.fetched == fresh, "already-scanned pages must never be refetched"
        assert summary.scanned == [comic_id]
        job = db.execute("SELECT status, message FROM jobs WHERE job_type = 'crawl'").fetchone()
        assert job["status"] == "completed"
        assert "skipped=2" in job["message"]
        assert db.execute("SELECT COUNT(*) FROM crawl_history").fetchone()[0] == 3


def test_pass_collects_new_comment_media_and_chains_the_pipeline(
    db, tmp_path: Path, sample_images: dict[str, Path]
) -> None:
    app = state_app(db, watch_settings(tmp_path))
    with registered(listing_adapter(chapter_listing(("/comic/chapter-2", 1)))):
        comic_id, _ = ensure_watched_comic(db, ENTRY_URL, "fixture")
        page_url = chapter_page_urls(ENTRY_URL, "/comic/chapter-2", 1)[0]
        fetcher = FixtureFetcher({page_url: COMMENT_PAGE_HTML})
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(media_handler(sample_images))
        )

        summary = asyncio.run(
            _run_pass(app, fetcher=fetcher, http_client=client)
        )

        assert summary.scanned == [comic_id]
        assert summary.failures == []
        # COLLECT → STORE: the new attachment was ingested (media + provenance).
        assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
        source = db.execute("SELECT media_url, page_url FROM source").fetchone()
        assert source["media_url"] == "https://cdn.example.com/new-meme.png"
        assert source["page_url"] == page_url
        stored = Path(db.execute("SELECT file_path FROM media").fetchone()[0])
        assert stored.is_file()
        # PROCESS → INDEX: the shared pipeline chain ran to completion (PRD §57).
        statuses = _job_types(db)
        assert set(statuses) == set(PIPELINE_JOB_TYPES)
        assert all(statuses[kind] == "completed" for kind in PIPELINE_JOB_TYPES)
        media_row = db.execute("SELECT processing_status FROM media").fetchone()
        assert media_row["processing_status"] == "READY", "mock AI analyzed the new item"
        assert db.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Failure isolation and the enabled filter
# ---------------------------------------------------------------------------


def test_pass_records_unsupported_comic_and_keeps_scanning(db, tmp_path: Path) -> None:
    app = state_app(db, watch_settings(tmp_path))
    with registered(listing_adapter(EMPTY_LISTING_HTML)):
        gone_id, _ = ensure_watched_comic(db, "https://gone.example/comic", "gone")
        good_id, _ = ensure_watched_comic(db, ENTRY_URL, "fixture")

        summary = asyncio.run(_run_pass(app))

        assert len(summary.failures) == 1
        assert f"comic {gone_id}" in summary.failures[0]
        assert "gone.example" in summary.failures[0]
        assert summary.scanned == [good_id], "one bad comic never stops the pass (PRD §36)"


def test_pass_only_scans_enabled_comics(db, tmp_path: Path) -> None:
    app = state_app(db, watch_settings(tmp_path))
    with registered(listing_adapter(EMPTY_LISTING_HTML)):
        off_id, _ = ensure_watched_comic(db, "https://fixture.test/comic-off", "fixture")
        set_watched_enabled(db, off_id, False)
        on_id, _ = ensure_watched_comic(db, ENTRY_URL, "fixture")

        summary = asyncio.run(_run_pass(app))

        assert summary.scanned == [on_id]
        assert summary.failures == []
