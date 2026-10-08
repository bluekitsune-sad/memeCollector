"""Crawler tests (M1.7) — fully offline: fixture HTML via injected fetchers, MockTransport HTTP.

Covers the COLLECT pipeline (comments → media → backfill), scope/max-pages,
crawl-history skip + force rescan (PRD §38), recorded page failures (PRD §36),
pause/cancel (PRD §5.2), rate/concurrency limits, 429/5xx backoff (PRD §37),
fetch-strategy selection, and the guarded Playwright launch (PRD §8).
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import httpx
import pytest

from backend.config import load_settings
from backend.scraper.adapters import CrawlScope, ScopeKind
from backend.scraper.crawler import (
    BrowserNotInstalledError,
    CrawlController,
    CrawlProgress,
    Crawler,
    HttpxFetcher,
    PageFetchError,
    PlaywrightFetcher,
    build_fetcher,
)
from tests.fixtures.fake_adapter import FakeSiteAdapter, FixtureFetcher, once, wait_until

ENTRY_URL = "https://fixture.test/comic/chapter-42?page=1"
CHAPTER_SCOPE = CrawlScope(ScopeKind.CURRENT_CHAPTER)

MODULES_UNDER_TEST = (
    "backend.scraper.adapters.base",
    "backend.scraper.crawler",
    "backend.scraper.downloader",
    "backend.scraper.backoff",
    "backend.media.hashing",
    "backend.jobs.crawl_job",
)


def _chapter_pages(fixtures_dir: Path, *names: str) -> dict[str, str]:
    """Map every discovered chapter page URL to the same fixture HTML."""
    html = "\n".join((fixtures_dir / name).read_text(encoding="utf-8") for name in names)
    return {f"{ENTRY_URL.rsplit('?', 1)[0]}?page={number}": html for number in (1, 2, 3)}


def test_modules_import_cleanly() -> None:
    for module_name in MODULES_UNDER_TEST:
        importlib.import_module(module_name)


async def test_crawl_collects_only_comment_media(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    fetcher = FixtureFetcher(_chapter_pages(fixtures_dir, "comment_attachments_heavy_chrome.html"))
    crawler = Crawler(settings=make_settings(concurrency=1), fetcher=fetcher)

    result = await crawler.crawl(ENTRY_URL, CHAPTER_SCOPE)

    assert result.progress.pages_total == 3
    assert result.progress.pages_scanned == 3
    assert result.progress.comments_discovered == 12  # 4 comments x 3 pages
    assert result.progress.media_found == 15  # 5 attachments x 3 pages
    assert result.failures == []
    # Chrome never appears; relative attachment resolved per page URL.
    assert not any("/assets/" in ref.url or "/avatars/" in ref.url for ref in result.media)
    assert {ref.url for ref in result.media} == {
        "https://cdn.example.com/media/heavy-one.png",
        "https://cdn.example.com/media/heavy-two.gif",
        "https://cdn.example.com/media/heavy-clip.mp4",
        "https://fixture.test/uploads/comments/rel-sticker.png",
        "https://cdn.example.com/media/eye-roll.webm",
    }


async def test_crawl_backfills_comment_provenance(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    fetcher = FixtureFetcher(_chapter_pages(fixtures_dir, "sample_comment_page.html"))
    crawler = Crawler(settings=make_settings(), fetcher=fetcher)

    result = await crawler.crawl(ENTRY_URL, CHAPTER_SCOPE)

    assert len(result.media) == 6  # 2 attachments x 3 pages
    cat_refs = [ref for ref in result.media if ref.url == "https://cdn.example.com/media/confused-cat.png"]
    assert {ref.comment.page_number for ref in cat_refs} == {1, 2, 3}
    assert all(
        ref.comment.page_url.startswith("https://fixture.test/comic/chapter-42?page=")
        for ref in cat_refs
    )
    assert cat_refs[0].comment.author_name == "reader_one"
    assert cat_refs[0].comment.chapter == "chapter-42"
    # Provenance spans every scanned page.
    assert {ref.comment.page_number for ref in result.media} == {1, 2, 3}


async def test_scope_current_page_scans_one_page(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    fetcher = FixtureFetcher(_chapter_pages(fixtures_dir, "sample_comment_page.html"))
    crawler = Crawler(settings=make_settings(), fetcher=fetcher)

    result = await crawler.crawl(ENTRY_URL, CrawlScope(ScopeKind.CURRENT_PAGE))

    assert result.progress.pages_total == 1
    assert result.progress.pages_scanned == 1
    assert len(fetcher.fetched) == 1


async def test_max_pages_caps_discovery(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    fetcher = FixtureFetcher(_chapter_pages(fixtures_dir, "sample_comment_page.html"))
    crawler = Crawler(settings=make_settings(max_pages=2), fetcher=fetcher)

    result = await crawler.crawl(ENTRY_URL, CHAPTER_SCOPE)

    assert result.progress.pages_total == 2
    assert len(fetcher.fetched) == 2


async def test_unsupported_site_raises_before_fetching(fake_adapter: FakeSiteAdapter) -> None:
    from backend.scraper.adapters import UnsupportedSiteError

    crawler = Crawler(settings=load_settings(), fetcher=FixtureFetcher({}))
    with pytest.raises(UnsupportedSiteError):
        await crawler.crawl("https://nope.example/comic")


async def test_crawl_history_skips_scanned_pages_unless_force_rescan(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings, db
) -> None:
    pages = _chapter_pages(fixtures_dir, "sample_comment_page.html")
    settings = make_settings()

    first_fetcher = FixtureFetcher(pages)
    first = await Crawler(settings=settings, db=db, fetcher=first_fetcher).crawl(ENTRY_URL, CHAPTER_SCOPE)
    assert first.progress.pages_scanned == 3
    history = db.execute("SELECT url, site, pages_scanned FROM crawl_history ORDER BY url").fetchall()
    assert len(history) == 3
    assert all(row["site"] == "fixture" for row in history)

    # Second crawl: every page skipped, zero fetches (PRD §38).
    second_fetcher = FixtureFetcher(pages)
    second = await Crawler(settings=settings, db=db, fetcher=second_fetcher).crawl(ENTRY_URL, CHAPTER_SCOPE)
    assert second.progress.pages_skipped == 3
    assert second.progress.pages_scanned == 0
    assert second_fetcher.fetched == []
    assert second.media == []

    # force_rescan: pages fetched again, history counters increment.
    third_fetcher = FixtureFetcher(pages)
    third = await Crawler(
        settings=settings, db=db, fetcher=third_fetcher
    ).crawl(ENTRY_URL, CHAPTER_SCOPE, force_rescan=True)
    assert third.progress.pages_scanned == 3
    assert len(third_fetcher.fetched) == 3
    row = db.execute(
        "SELECT pages_scanned FROM crawl_history WHERE url = ?",
        ("https://fixture.test/comic/chapter-42?page=1",),
    ).fetchone()
    assert row["pages_scanned"] == 2


async def test_failed_page_recorded_but_crawl_continues(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    pages = _chapter_pages(fixtures_dir, "sample_comment_page.html")
    missing = "https://fixture.test/comic/chapter-42?page=2"
    pages.pop(missing)
    fetcher = FixtureFetcher(pages)
    crawler = Crawler(settings=make_settings(concurrency=1), fetcher=fetcher)

    result = await crawler.crawl(ENTRY_URL, CHAPTER_SCOPE)

    assert result.progress.pages_scanned == 2
    assert result.progress.pages_failed == 1
    assert len(result.failures) == 1
    assert result.failures[0].url == missing
    assert "PageFetchError" in result.failures[0].reason
    assert len(result.media) == 4  # two good pages still produced media (2 attachments each)


async def test_cancel_stops_remaining_pages(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    controller = CrawlController()
    fetcher = FixtureFetcher(
        _chapter_pages(fixtures_dir, "sample_comment_page.html"),
        on_fetch=lambda url: controller.cancel(),
    )
    crawler = Crawler(
        settings=make_settings(concurrency=1), controller=controller, fetcher=fetcher
    )

    result = await crawler.crawl(ENTRY_URL, CHAPTER_SCOPE)

    assert len(fetcher.fetched) == 1
    assert result.progress.pages_scanned == 1
    assert result.progress.pages_total == 3


async def test_pause_blocks_new_pages_until_resume(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    controller = CrawlController()
    pause_once = once(lambda: controller.pause())
    fetcher = FixtureFetcher(
        _chapter_pages(fixtures_dir, "sample_comment_page.html"), on_fetch=pause_once
    )
    crawler = Crawler(
        settings=make_settings(concurrency=1), controller=controller, fetcher=fetcher
    )

    crawl_task = asyncio.create_task(crawler.crawl(ENTRY_URL, CHAPTER_SCOPE))
    await wait_until(lambda: len(fetcher.fetched) >= 1)
    await asyncio.sleep(0.05)
    assert len(fetcher.fetched) == 1, "paused crawl must not start new pages"
    assert controller.paused

    controller.resume()
    result = await asyncio.wait_for(crawl_task, timeout=10)
    assert result.progress.pages_scanned == 3


async def test_concurrency_never_exceeds_settings(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    class CountingFetcher(FixtureFetcher):
        def __init__(self, pages: dict[str, str]) -> None:
            super().__init__(pages)
            self.active = 0
            self.max_active = 0

        async def fetch(self, url: str) -> str:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return await super().fetch(url)
            finally:
                self.active -= 1

    fetcher = CountingFetcher(_chapter_pages(fixtures_dir, "sample_comment_page.html"))
    crawler = Crawler(settings=make_settings(concurrency=2), fetcher=fetcher)

    await crawler.crawl(ENTRY_URL, CHAPTER_SCOPE)

    assert 1 <= fetcher.max_active <= 2


async def test_progress_callback_reports_increasing_snapshots(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    snapshots: list[CrawlProgress] = []
    fetcher = FixtureFetcher(_chapter_pages(fixtures_dir, "sample_comment_page.html"))
    crawler = Crawler(
        settings=make_settings(concurrency=1), fetcher=fetcher, on_progress=snapshots.append
    )

    result = await crawler.crawl(ENTRY_URL, CHAPTER_SCOPE)

    assert len(snapshots) >= 4  # initial + at least one per page
    assert snapshots[0].pages_total == 3
    assert snapshots[0].pages_handled == 0
    assert snapshots[-1].pages_handled == result.progress.pages_total
    assert all(snapshot.pages_handled <= snapshot.pages_total for snapshot in snapshots)
    # Snapshots are copies — later mutation cannot corrupt earlier ones.
    assert snapshots[0].pages_scanned == 0


async def test_injected_fetcher_is_not_closed_by_crawler(
    fake_adapter: FakeSiteAdapter, fixtures_dir: Path, make_settings
) -> None:
    fetcher = FixtureFetcher(_chapter_pages(fixtures_dir, "sample_comment_page.html"))
    await Crawler(settings=make_settings(), fetcher=fetcher).crawl(ENTRY_URL, CrawlScope(ScopeKind.CURRENT_PAGE))
    assert not fetcher.closed  # caller owns injected fetchers


# ---------------------------------------------------------------------------
# HTTP fetcher: 429/5xx backoff via httpx.MockTransport (no network).
# ---------------------------------------------------------------------------


async def test_http_fetcher_retries_429_then_succeeds(make_settings) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) <= 2:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, text="<html>ok</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(settings=make_settings(retry_attempts=3), client=client)
    try:
        html = await fetcher.fetch("https://fixture.test/page")
    finally:
        await client.aclose()
    assert html == "<html>ok</html>"
    assert len(calls) == 3


async def test_http_fetcher_gives_up_after_configured_attempts(make_settings) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(settings=make_settings(retry_attempts=2), client=client)
    try:
        with pytest.raises(PageFetchError, match="after 2 attempts"):
            await fetcher.fetch("https://fixture.test/broken")
    finally:
        await client.aclose()
    assert len(calls) == 2


async def test_http_fetcher_4xx_fails_without_retry(make_settings) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(settings=make_settings(retry_attempts=3), client=client)
    try:
        with pytest.raises(PageFetchError, match="HTTP 404"):
            await fetcher.fetch("https://fixture.test/missing")
    finally:
        await client.aclose()
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Fetch-strategy selection + guarded Playwright launch (PRD §8).
# ---------------------------------------------------------------------------


def test_build_fetcher_follows_adapter_render_flag(make_settings) -> None:
    settings = make_settings()
    assert isinstance(build_fetcher(FakeSiteAdapter(), settings), HttpxFetcher)

    class RenderedAdapter(FakeSiteAdapter):
        site = "rendered-fixture"
        render = True

    fetcher = build_fetcher(RenderedAdapter(), settings)
    assert isinstance(fetcher, PlaywrightFetcher)


def test_playwright_fetcher_headless_follows_debug_flag(make_settings) -> None:
    assert PlaywrightFetcher(settings=make_settings()).headless is True
    assert PlaywrightFetcher(settings=make_settings(debug=True)).headless is False
    assert PlaywrightFetcher(settings=make_settings(headless=False)).headless is False


async def test_browser_launch_guarded_when_chromium_missing(make_settings) -> None:
    """Chromium is not downloaded in this environment — the guard must fire.

    Skips (justified) only if a machine unexpectedly has Chromium installed,
    where the failure path cannot be observed without uninstalling it.
    """
    fetcher = PlaywrightFetcher(settings=make_settings())
    try:
        await fetcher.start()
    except BrowserNotInstalledError:
        await fetcher.aclose()
        return
    await fetcher.aclose()
    pytest.skip("chromium installed — BrowserNotInstalledError path not observable")
