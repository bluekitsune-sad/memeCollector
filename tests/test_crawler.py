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
from playwright.async_api import Error as PlaywrightError

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


# ---------------------------------------------------------------------------
# Rendered-page capture: domcontentloaded + capped load-wait + opt-in scroll
# and opt-in pre-capture DOM click (SiteAdapter.scroll_before_capture /
# SiteAdapter.click_before_capture — flamecomics/vortexscans/hivetoons).
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.headers: dict[str, str] = {}


class _FakePage:
    """Duck-typed Playwright page recording every interaction (no browser needed).

    ``evaluate`` doubles as the pre-capture DOM-click surface: the click
    expression passes the selector as its single argument, which is recorded
    in ``click_calls`` (or fails, per the constructor flags).
    """

    def __init__(
        self,
        *,
        status: int = 200,
        load_error: bool = False,
        evaluate_error: bool = False,
        click_error: bool = False,
        click_absent: bool = False,
    ) -> None:
        self.url = "https://fixture.test/comic/chapter-42"
        self.goto_calls: list[tuple[str, str | None, int | None]] = []
        self.load_state_calls: list[tuple[str, int | None]] = []
        self.evaluate_calls: list[str] = []
        self.timeouts: list[int] = []
        self.click_calls: list[str] = []
        self.content_calls = 0
        self._status = status
        self._load_error = load_error
        self._evaluate_error = evaluate_error
        self._click_error = click_error
        self._click_absent = click_absent

    async def goto(self, url: str, *, wait_until: str | None = None, timeout: int | None = None):
        self.goto_calls.append((url, wait_until, timeout))
        return _FakeResponse(self._status)

    async def wait_for_load_state(self, state: str, *, timeout: int | None = None) -> None:
        self.load_state_calls.append((state, timeout))
        if self._load_error:
            raise PlaywrightError("Timeout waiting for load state")

    async def evaluate(self, expression: str, *args) -> object:
        if self._evaluate_error:
            raise PlaywrightError("Execution context was destroyed")
        self.evaluate_calls.append(expression)
        if "el.click()" in expression:  # the pre-capture DOM click
            selector = args[0] if args else "<no-arg>"
            if self._click_error:
                raise PlaywrightError("Execution context was destroyed mid-click")
            if self._click_absent:
                return False
            self.click_calls.append(str(selector))
            return True
        return None

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        self.timeouts.append(timeout_ms)

    async def content(self) -> str:
        self.content_calls += 1
        return "<html><body>rendered</body></html>"


def _rendered_fetcher(
    make_settings, *, scroll: bool, click_selector: str | None = None
) -> PlaywrightFetcher:
    return PlaywrightFetcher(
        settings=make_settings(), scroll=scroll, click_selector=click_selector
    )


async def test_rendered_fetch_waits_for_load_with_cap(make_settings) -> None:
    """goto lands on domcontentloaded; the full load event is capped, then captured."""
    from backend.scraper.crawler import _LOAD_WAIT_CAP_MS

    fetcher = _rendered_fetcher(make_settings, scroll=False)
    page = _FakePage()
    fetcher._page = page
    try:
        attempt = await fetcher._attempt("https://fixture.test/comic/chapter-42")
    finally:
        fetcher._page = None
    assert page.goto_calls == [("https://fixture.test/comic/chapter-42", "domcontentloaded", 30_000)]
    assert page.load_state_calls == [("load", _LOAD_WAIT_CAP_MS)]
    assert attempt.status == 200
    assert attempt.body == "<html><body>rendered</body></html>"
    assert page.content_calls == 1
    assert page.evaluate_calls == []  # no scroll requested


async def test_rendered_fetch_tolerates_hanging_load_event(make_settings) -> None:
    """Ad-heavy readers never fire `load` — the DOM must still be captured."""
    fetcher = _rendered_fetcher(make_settings, scroll=False)
    page = _FakePage(load_error=True)
    fetcher._page = page
    try:
        attempt = await fetcher._attempt("https://fixture.test/comic/chapter-42")
    finally:
        fetcher._page = None
    assert attempt.status == 200
    assert attempt.body == "<html><body>rendered</body></html>"
    assert page.content_calls == 1


async def test_scroll_flag_step_scrolls_before_capture(make_settings) -> None:
    from backend.scraper.crawler import (
        _SCROLL_SETTLE_MS,
        _SCROLL_STEP_DELAY_MS,
        _SCROLL_STEPS,
    )

    fetcher = _rendered_fetcher(make_settings, scroll=True)
    page = _FakePage()
    fetcher._page = page
    try:
        attempt = await fetcher._attempt("https://fixture.test/comic/chapter-42")
    finally:
        fetcher._page = None
    assert len(page.evaluate_calls) == _SCROLL_STEPS
    assert all("scrollBy" in expr for expr in page.evaluate_calls)
    # step delays + one settle wait at the end
    assert page.timeouts == [_SCROLL_STEP_DELAY_MS] * _SCROLL_STEPS + [_SCROLL_SETTLE_MS]
    # the load-cap runs first, the DOM is captured last
    assert page.load_state_calls and page.content_calls == 1
    assert attempt.status == 200


async def test_scroll_interrupted_still_captures_page(make_settings) -> None:
    """A mid-scroll navigation/teardown is logged, never a failed page (PRD §36)."""
    fetcher = _rendered_fetcher(make_settings, scroll=True)
    page = _FakePage(evaluate_error=True)
    fetcher._page = page
    try:
        attempt = await fetcher._attempt("https://fixture.test/comic/chapter-42")
    finally:
        fetcher._page = None
    assert attempt.status == 200
    assert page.content_calls == 1


def test_build_fetcher_passes_adapter_scroll_flag(make_settings) -> None:
    settings = make_settings()

    class ScrollAdapter(FakeSiteAdapter):
        site = "scroll-fixture"
        render = True
        scroll_before_capture = True

    fetcher = build_fetcher(ScrollAdapter(), settings)
    assert isinstance(fetcher, PlaywrightFetcher)
    assert fetcher._scroll is True

    class RenderedOnly(FakeSiteAdapter):
        site = "rendered-fixture-2"
        render = True

    plain = build_fetcher(RenderedOnly(), settings)
    assert isinstance(plain, PlaywrightFetcher)
    assert plain._scroll is False
    assert plain._click_selector is None


async def test_pre_capture_click_runs_and_settles(make_settings) -> None:
    """The vortex drawer trigger is DOM-clicked after scroll, before the capture."""
    from backend.scraper.crawler import _CLICK_SETTLE_MS

    selector = 'button[aria-label^="Comments"]'
    fetcher = _rendered_fetcher(
        make_settings, scroll=True, click_selector=selector
    )
    page = _FakePage()
    fetcher._page = page
    try:
        attempt = await fetcher._attempt("https://fixture.test/comic/chapter-42")
    finally:
        fetcher._page = None
    assert page.click_calls == [selector]
    # settle wait is the LAST timeout after the scroll step delays
    assert page.timeouts[-1] == _CLICK_SETTLE_MS
    assert attempt.status == 200
    assert page.content_calls == 1


async def test_pre_capture_click_failure_still_captures(make_settings) -> None:
    """A broken click (destroyed context) degrades to capture (PRD §36)."""
    from backend.scraper.crawler import _SCROLL_SETTLE_MS, _SCROLL_STEP_DELAY_MS, _SCROLL_STEPS

    fetcher = _rendered_fetcher(
        make_settings, scroll=True, click_selector="button[aria-label^='Comments']"
    )
    page = _FakePage(click_error=True)
    fetcher._page = page
    try:
        attempt = await fetcher._attempt("https://fixture.test/comic/chapter-42")
    finally:
        fetcher._page = None
    assert attempt.status == 200
    assert page.content_calls == 1
    # only the scroll settle fires — the click's settle is skipped on failure
    assert page.timeouts == [_SCROLL_STEP_DELAY_MS] * _SCROLL_STEPS + [_SCROLL_SETTLE_MS]


async def test_pre_capture_trigger_absent_still_captures(make_settings) -> None:
    """A missing/changed trigger selector degrades to capture (PRD §36)."""
    from backend.scraper.crawler import _SCROLL_SETTLE_MS, _SCROLL_STEP_DELAY_MS, _SCROLL_STEPS

    fetcher = _rendered_fetcher(
        make_settings, scroll=True, click_selector="button[aria-label^='Comments']"
    )
    page = _FakePage(click_absent=True)
    fetcher._page = page
    try:
        attempt = await fetcher._attempt("https://fixture.test/comic/chapter-42")
    finally:
        fetcher._page = None
    assert page.click_calls == []  # reported absent, not clicked
    assert attempt.status == 200
    assert page.content_calls == 1
    # only the scroll settle fires — no click settle when nothing was clicked
    assert page.timeouts == [_SCROLL_STEP_DELAY_MS] * _SCROLL_STEPS + [_SCROLL_SETTLE_MS]


def test_build_fetcher_passes_adapter_click_selector(make_settings) -> None:
    settings = make_settings()

    class DrawerAdapter(FakeSiteAdapter):
        site = "drawer-fixture"
        render = True
        scroll_before_capture = True
        click_before_capture = 'button[aria-label^="Comments"]'

    fetcher = build_fetcher(DrawerAdapter(), settings)
    assert isinstance(fetcher, PlaywrightFetcher)
    assert fetcher._click_selector == 'button[aria-label^="Comments"]'
