"""Crawler core — the COLLECT stage (PRD §5, §8, §10, §36–39; AGENTS.md §6).

Flow: resolve adapter → ``discover_pages(scope)`` (capped by ``max_pages``) →
per page: fetch → ``find_comments`` → backfill provenance → ``find_comment_media``
→ record crawl history. Output is an in-memory :class:`CrawlResult` of
:class:`~backend.scraper.adapters.base.MediaRef` values for the download stage —
**the crawler never writes media files** (PRD §57 separation; it may only update
``crawl_history`` through the injected connection, jobs rows are updated by the
caller's progress callback).

Fetch strategies behind one :class:`PageFetcher` interface:

* :class:`HttpxFetcher` — plain httpx GET for static pages (adapter ``render = False``).
* :class:`PlaywrightFetcher` — Chromium render for JS-heavy pages (adapter
  ``render = True``); headless unless ``crawler.debug`` is set (PRD §8), and
  guarded with :class:`BrowserNotInstalledError` when Chromium is missing.

Both fetchers share exponential backoff for transport errors / 429 / 5xx
(PRD §37, AGENTS.md §9);4xx responses fail the page immediately. A failed page
is recorded as a :class:`CrawlFailure` and never aborts the crawl (PRD §36).
Rate limits come from ``Settings.crawler``: ``delay_seconds`` between page fetch
slots, ``concurrency`` in-flight pages, ``retry_attempts`` per fetch.

Navigation guard (PRD §41): every URL is validated by :mod:`backend.security.urls`
*before* any request or browser navigation is issued, redirects are followed one
hop at a time so each target is re-validated, and a blocked URL surfaces as
:class:`PageFetchError` — logged as ``key=value`` by the guard and recorded as a
per-page failure, never as a crashed crawl.

Tests never hit the network: inject any object implementing :class:`PageFetcher`
that returns fixture HTML.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from backend.config import Settings
from backend.database.database import transaction
from backend.scraper.adapters import (
    CrawlScope,
    MediaRef,
    PageRef,
    ScopeKind,
    SiteAdapter,
    get_adapter,
)
from backend.scraper.backoff import backoff_delay, clamp_retry_after, parse_retry_after
from backend.security.urls import (
    MAX_REDIRECTS,
    REDIRECT_STATUSES,
    UnsafeURLError,
    ensure_safe_url,
    reject_url,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = frozenset({429})


class PageFetchError(Exception):
    """A page could not be fetched (HTTP error or exhausted retries)."""


class BrowserNotInstalledError(RuntimeError):
    """Playwright Chromium is not downloaded — user must run ``playwright install chromium``."""


class PageFetcher(Protocol):
    """Fetch strategy for one page's rendered HTML (static httpx or Playwright)."""

    async def fetch(self, url: str) -> str:
        """Return the page's HTML; raise :class:`PageFetchError` when unrecoverable."""
        ...

    async def aclose(self) -> None:
        """Release owned resources (no-op for injected clients the caller owns)."""
        ...


@dataclass(frozen=True)
class _Attempt:
    """Outcome of one fetch attempt: ``status`` is None for transport-level failures."""

    status: int | None
    body: str = ""
    retry_after: float | None = None
    error: str | None = None


async def _retrying(
    attempt_fn: Callable[[], Awaitable[_Attempt]],
    *,
    url: str,
    settings: Settings,
    source: str,
) -> str:
    """Run ``attempt_fn`` until success; retry transient failures with exponential backoff (PRD §37).

    Transient = transport error (``status is None``), HTTP 429, or 5xx. Any other
    4xx raises :class:`PageFetchError` immediately — retrying cannot fix it.
    A URL blocked by the navigation guard also raises immediately (retrying
    cannot make an unsafe URL safe).
    """
    attempts = max(1, settings.crawler.retry_attempts)
    base_delay = settings.crawler.delay_seconds
    reason = "unknown"
    retry_after: float | None = None
    for attempt in range(attempts):
        if attempt:
            wait = clamp_retry_after(retry_after, backoff_delay(base_delay, attempt - 1))
            logger.info("fetch backoff source=%s url=%s wait=%.1fs attempt=%d/%d",
                        source, url, wait, attempt + 1, attempts)
            await asyncio.sleep(wait)
        result = await attempt_fn()
        if result.status is not None and 200 <= result.status < 300:
            return result.body
        if (
            result.status is not None
            and 400 <= result.status < 500
            and result.status not in _RETRYABLE_STATUSES
        ):
            raise PageFetchError(f"HTTP {result.status} for {url}")
        reason = result.error or f"HTTP {result.status}"
        retry_after = result.retry_after
        logger.warning("transient fetch failure source=%s url=%s attempt=%d/%d reason=%s",
                       source, url, attempt + 1, attempts, reason)
    raise PageFetchError(f"fetch failed after {attempts} attempts url={url} reason={reason}")


async def _guard_url(url: str) -> None:
    """Validate one navigation target before any bytes leave the machine (PRD §41).

    The guard logs the block as ``key=value``; translating it to
    :class:`PageFetchError` makes it a normal per-page failure the crawler
    records instead of an exception that could tear down the crawl (PRD §36).
    """
    try:
        await ensure_safe_url(url)
    except UnsafeURLError as exc:
        raise PageFetchError(f"unsafe url blocked: {exc}") from exc


class HttpxFetcher:
    """Static-page fetcher: httpx with redirects, timeout, and backoff (PRD §11, §37)."""

    def __init__(self, *, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    async def fetch(self, url: str) -> str:
        await _guard_url(url)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.crawler.request_timeout_seconds,
                follow_redirects=False,  # redirects are followed one guarded hop at a time
            )
        return await _retrying(lambda: self._attempt(url), url=url,
                                settings=self._settings, source="httpx")

    async def _attempt(self, url: str) -> _Attempt:
        assert self._client is not None
        current = url
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                await ensure_safe_url(current)
                response = await self._client.get(current, follow_redirects=False)
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current = str(httpx.URL(current).join(location))
                    continue
                break
            else:
                reject_url(url, "redirect limit exceeded")
        except UnsafeURLError as exc:
            raise PageFetchError(f"unsafe url blocked: {exc}") from exc
        except httpx.HTTPError as exc:
            return _Attempt(status=None, error=f"{type(exc).__name__}: {exc}")
        body = response.text if 200 <= response.status_code < 300 else ""
        return _Attempt(
            status=response.status_code,
            body=body,
            retry_after=parse_retry_after(response.headers.get("retry-after")),
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


class PlaywrightFetcher:
    """Rendered-page fetcher: Playwright Chromium, headed when ``crawler.debug`` is set (PRD §8)."""

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._playwright = None
        self._browser = None
        self._page = None

    @property
    def headless(self) -> bool:
        crawler = self._settings.crawler
        return bool(crawler.headless and not crawler.debug)

    async def start(self) -> None:
        """Launch Chromium lazily; raise :class:`BrowserNotInstalledError` if the browser is missing."""
        if self._browser is not None:
            return
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._page = await self._browser.new_page()
        except Exception as exc:
            await self.aclose()
            lowered = str(exc).lower()
            if "executable doesn't exist" in lowered or "playwright install" in lowered:
                raise BrowserNotInstalledError(
                    "Chromium is not installed for Playwright — run: playwright install chromium"
                ) from exc
            raise

    async def fetch(self, url: str) -> str:
        # Guard first: a hostile URL must be rejected before Chromium launches
        # (or navigates), so this path is testable without a browser.
        await _guard_url(url)
        await self.start()
        return await _retrying(lambda: self._attempt(url), url=url,
                                settings=self._settings, source="playwright")

    async def _attempt(self, url: str) -> _Attempt:
        assert self._page is not None
        timeout_ms = int(self._settings.crawler.request_timeout_seconds * 1000)
        try:
            response = await self._page.goto(url, wait_until="load", timeout=timeout_ms)
        except PlaywrightError as exc:
            return _Attempt(status=None, error=f"{type(exc).__name__}: {exc}")
        if response is None:
            return _Attempt(status=None, error="navigation returned no response")
        # Chromium followed redirects internally — re-check where we actually landed.
        try:
            await ensure_safe_url(self._page.url)
        except UnsafeURLError as exc:
            raise PageFetchError(f"unsafe url blocked: {exc}") from exc
        body = await self._page.content() if 200 <= response.status < 300 else ""
        return _Attempt(
            status=response.status,
            body=body,
            retry_after=parse_retry_after(response.headers.get("retry-after")),
        )

    async def aclose(self) -> None:
        for closer in (self._page, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    logger.debug("playwright close failed", exc_info=True)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.debug("playwright stop failed", exc_info=True)
        self._page = None
        self._browser = None
        self._playwright = None


def build_fetcher(adapter: SiteAdapter, settings: Settings) -> PageFetcher:
    """Pick the fetch strategy an adapter declared via its ``render`` flag."""
    if adapter.render:
        return PlaywrightFetcher(settings=settings)
    return HttpxFetcher(settings=settings)


class CrawlController:
    """Cooperative pause/cancel control for a running crawl (PRD §5.2).

    ``pause()`` gates each page before it starts; ``cancel()`` stops the crawl
    after in-flight pages finish (a page mid-fetch completes — cooperative, not
    preemptive).
    """

    def __init__(self) -> None:
        self._cancelled = asyncio.Event()
        self._running = asyncio.Event()
        self._running.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._running.set()  # unblock a paused crawl so it can observe the cancel

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    async def wait_while_paused(self) -> None:
        """Suspend until resumed (returns immediately when running or cancelled)."""
        await self._running.wait()


@dataclass
class CrawlProgress:
    """Counters for PRD §5.2 progress display."""

    pages_total: int = 0
    pages_scanned: int = 0
    pages_skipped: int = 0
    pages_failed: int = 0
    comments_discovered: int = 0
    media_found: int = 0

    @property
    def pages_handled(self) -> int:
        """Pages that reached a terminal state (scanned + skipped + failed)."""
        return self.pages_scanned + self.pages_skipped + self.pages_failed


@dataclass(frozen=True)
class CrawlFailure:
    """One recorded per-page/per-comment failure (PRD §36)."""

    url: str
    reason: str


@dataclass
class CrawlResult:
    """COLLECT output: comment media refs + progress + recorded failures."""

    media: list[MediaRef] = field(default_factory=list)
    progress: CrawlProgress = field(default_factory=CrawlProgress)
    failures: list[CrawlFailure] = field(default_factory=list)


class _PageRateLimiter:
    """Spaces page-fetch starts by ``delay_seconds`` across all workers (AGENTS.md §9)."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay = max(0.0, delay_seconds)
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def wait_turn(self) -> None:
        if self._delay <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._delay
            wait = slot - now
        if wait > 0:
            await asyncio.sleep(wait)


class Crawler:
    """Runs one crawl: discovery → fetch → comment/media extraction → history upsert."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: sqlite3.Connection | None = None,
        controller: CrawlController | None = None,
        fetcher: PageFetcher | None = None,
        on_progress: Callable[[CrawlProgress], None] | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self.controller = controller if controller is not None else CrawlController()
        self._fetcher = fetcher
        self._on_progress = on_progress

    async def crawl(
        self,
        url: str,
        scope: CrawlScope | None = None,
        *,
        force_rescan: bool = False,
    ) -> CrawlResult:
        """Collect every comment attachment for ``url`` under ``scope`` (default: current page)."""
        scope = scope if scope is not None else CrawlScope(ScopeKind.CURRENT_PAGE)
        adapter = get_adapter(url)
        logger.info("crawl started url=%s site=%s scope=%s force_rescan=%s",
                    url, adapter.site, scope.kind.value, force_rescan)

        discovered = await asyncio.to_thread(adapter.discover_pages, url, scope)
        cap = max(1, self._settings.crawler.max_pages)
        pages = _dedupe_pages(discovered)[:cap]
        if len(discovered) > len(pages):
            logger.info("page list capped pages=%d cap=%d", len(pages), cap)

        already_scanned = self._load_history() if (self._db is not None and not force_rescan) else set()

        owned_fetcher = self._fetcher is None
        fetcher = self._fetcher if self._fetcher is not None else build_fetcher(adapter, self._settings)
        controller = self.controller
        state = CrawlProgress(pages_total=len(pages))
        failures: list[CrawlFailure] = []
        media_by_page: dict[int, list[MediaRef]] = {}
        semaphore = asyncio.Semaphore(max(1, self._settings.crawler.concurrency))
        limiter = _PageRateLimiter(self._settings.crawler.delay_seconds)

        self._report(state)

        async def scan(index: int, page: PageRef) -> None:
            if controller.cancelled:
                return
            if page.url in already_scanned:
                state.pages_skipped += 1
                logger.info("page already scanned url=%s — skipping (PRD §38)", page.url)
                self._report(state)
                return
            async with semaphore:
                if controller.cancelled:
                    return
                await controller.wait_while_paused()
                if controller.cancelled:
                    return
                await limiter.wait_turn()
                try:
                    html = await fetcher.fetch(page.url)
                    refs, comment_count = self._extract(adapter, html, page, failures)
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    failures.append(CrawlFailure(page.url, reason))
                    state.pages_failed += 1
                    logger.warning("page scan failed url=%s reason=%s", page.url, reason)
                    self._report(state)
                    return
                media_by_page[index] = refs
                state.pages_scanned += 1
                state.comments_discovered += comment_count
                state.media_found += len(refs)
                self._record_history(page, len(refs))
                logger.info("page scanned url=%s comments=%d media=%d",
                            page.url, comment_count, len(refs))
                self._report(state)

        try:
            await asyncio.gather(*(scan(index, page) for index, page in enumerate(pages)))
        finally:
            if owned_fetcher:
                await fetcher.aclose()

        media = [ref for index in range(len(pages)) for ref in media_by_page.get(index, ())]
        logger.info("crawl finished site=%s scanned=%d skipped=%d failed=%d comments=%d media=%d",
                    adapter.site, state.pages_scanned, state.pages_skipped,
                    state.pages_failed, state.comments_discovered, state.media_found)
        return CrawlResult(media=media, progress=state, failures=failures)

    # -- internals ---------------------------------------------------------

    def _load_history(self) -> set[str]:
        assert self._db is not None
        return {row["url"] for row in self._db.execute("SELECT url FROM crawl_history")}

    def _extract(
        self,
        adapter: SiteAdapter,
        html: str,
        page: PageRef,
        failures: list[CrawlFailure],
    ) -> tuple[list[MediaRef], int]:
        """Comments → provenance backfill → media refs; per-comment failures are recorded, not raised."""
        comments = adapter.find_comments(html)
        for comment in comments:
            meta = adapter.get_comment_metadata(comment)
            if not meta.page_url:
                meta.page_url = page.url
            if meta.chapter is None:
                meta.chapter = page.chapter
            if meta.page_number is None:
                meta.page_number = page.page_number
        refs: list[MediaRef] = []
        for comment in comments:
            try:
                refs.extend(adapter.find_comment_media(comment))
            except Exception as exc:
                comment_id = comment.meta.comment_id
                failures.append(CrawlFailure(page.url, f"comment {comment_id}: {type(exc).__name__}: {exc}"))
                logger.warning("comment media extraction failed url=%s comment=%s reason=%s",
                               page.url, comment_id, exc)
        return refs, len(comments)

    def _record_history(self, page: PageRef, media_count: int) -> None:
        """Upsert crawl history (PRD §38); a history hiccup never fails an otherwise good scan."""
        if self._db is None:
            return
        try:
            with transaction(self._db):
                self._db.execute(
                    """
                    INSERT INTO crawl_history (url, site, pages_scanned, media_found, last_scanned_at)
                    VALUES (?, ?, 1, ?, datetime('now'))
                    ON CONFLICT(url) DO UPDATE SET
                        site = excluded.site,
                        pages_scanned = crawl_history.pages_scanned + 1,
                        media_found = crawl_history.media_found + excluded.media_found,
                        last_scanned_at = datetime('now')
                    """,
                    (page.url, page.site, media_count),
                )
        except sqlite3.Error:
            logger.exception("crawl history update failed url=%s", page.url)

    def _report(self, state: CrawlProgress) -> None:
        """Hand a snapshot to the progress callback; callback bugs are logged, not fatal (PRD §36)."""
        if self._on_progress is None:
            return
        try:
            self._on_progress(replace(state))
        except Exception:
            logger.exception("progress callback raised")


def _dedupe_pages(pages: list[PageRef]) -> list[PageRef]:
    """Keep the first occurrence of each URL so a page is never scanned twice in one crawl."""
    seen: set[str] = set()
    unique: list[PageRef] = []
    for page in pages:
        if page.url in seen:
            logger.info("duplicate page in discovery list url=%s — dropped", page.url)
            continue
        seen.add(page.url)
        unique.append(page)
    return unique
