"""FakeSiteAdapter — a real ``SiteAdapter`` implementation over local fixture HTML.

This is the crawler/job test harness: it proves the whole COLLECT pipeline
(discovery → fetch → comments → media) against ``fixture.test`` URLs whose HTML
comes from :class:`FixtureFetcher` (in-memory) instead of the network.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import pytest
from bs4 import BeautifulSoup

from backend.scraper.adapters import (
    Comment,
    CommentMeta,
    CrawlScope,
    MediaKind,
    MediaRef,
    PageRef,
    ScopeKind,
    SiteAdapter,
)
from backend.scraper.crawler import PageFetchError

#: URL suffix → media kind for classification (everything else is an image).
_KIND_BY_SUFFIX: dict[str, MediaKind] = {".gif": "gif", ".mp4": "video", ".webm": "video"}

#: Ordered attribute preference for lazy-loaded images.
_LAZY_ATTRIBUTES = ("data-src", "data-original", "srcset", "src")


def classify_media_kind(url: str) -> MediaKind:
    """Classify by URL suffix: .gif → gif, .mp4/.webm → video, else image."""
    path = urlparse(url).path.lower()
    for suffix, kind in _KIND_BY_SUFFIX.items():
        if path.endswith(suffix):
            return kind
    return "image"


class FakeSiteAdapter(SiteAdapter):
    """Adapter for ``fixture.test`` URLs, driven entirely by local HTML strings.

    ``listing_html`` (the chapter-listing fixture) powers ``ENTIRE_COMIC``
    discovery; without it that scope falls back to the current chapter.
    """

    site = "fixture"
    render = False

    def __init__(self, *, listing_html: str | None = None, chapter_pages: int = 3) -> None:
        self._listing_html = listing_html
        self._chapter_pages = chapter_pages

    def can_handle(self, url: str) -> bool:
        return urlparse(url).hostname == "fixture.test"

    def discover_pages(self, url: str, scope: CrawlScope) -> list[PageRef]:
        if scope.kind is ScopeKind.CUSTOM_URLS:
            return [self._page_ref(given) for given in scope.urls]
        if scope.kind is ScopeKind.MULTIPLE_CHAPTERS:
            return [ref for given in scope.urls for ref in self._expand_chapter(given)]
        if scope.kind is ScopeKind.ENTIRE_COMIC and self._listing_html:
            return self._chapters_from_listing(url)
        if scope.kind is ScopeKind.CURRENT_CHAPTER:
            return self._expand_chapter(url)
        return [self._page_ref(url)]

    def find_comments(self, page: str) -> list[Comment]:
        soup = BeautifulSoup(page, "html.parser")
        comments: list[Comment] = []
        for article in soup.select("article.comment"):
            author = article.select_one(".author")
            text = article.select_one(".comment-text")
            # page_url/chapter/page_number are intentionally left for the
            # crawler to backfill from the PageRef being scanned.
            meta = CommentMeta(
                comment_id=str(article.get("data-comment-id", "")),
                author_name=author.get_text(strip=True) if author else None,
                text=text.get_text(" ", strip=True) if text else None,
            )
            comments.append(Comment(meta=meta, element=article))
        return comments

    def find_comment_media(self, comment: Comment) -> list[MediaRef]:
        refs: dict[str, MediaRef] = {}
        nodes = comment.element.select(
            "a.attachment, img.attachment, video.attachment, video.attachment source"
        )
        for node in nodes:
            candidate = self._media_url(node)
            if not candidate:
                continue
            absolute = urljoin(comment.meta.page_url, candidate)
            if absolute in refs:
                continue
            refs[absolute] = MediaRef(
                url=absolute,
                kind=classify_media_kind(absolute),
                comment=comment.meta,
                original_filename=urlparse(absolute).path.rsplit("/", 1)[-1] or None,
            )
        return list(refs.values())

    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        return comment.meta

    # -- discovery helpers -------------------------------------------------

    def _expand_chapter(self, url: str) -> list[PageRef]:
        return [self._with_page(url, number) for number in range(1, self._chapter_pages + 1)]

    def _chapters_from_listing(self, entry_url: str) -> list[PageRef]:
        soup = BeautifulSoup(self._listing_html or "", "html.parser")
        refs: list[PageRef] = []
        for link in soup.select("a.chapter-link"):
            chapter_url = urljoin(entry_url, link["href"])
            pages = int(link.get("data-pages", "1"))
            refs.extend(self._with_page(chapter_url, number) for number in range(1, pages + 1))
        return refs

    @staticmethod
    def _with_page(url: str, page_number: int) -> PageRef:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query["page"] = [str(page_number)]
        rebuilt = parsed._replace(query=urlencode(query, doseq=True)).geturl()
        return FakeSiteAdapter._page_ref(rebuilt)

    @staticmethod
    def _page_ref(url: str) -> PageRef:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        raw_page = query.get("page", ["1"])[0]
        chapter = parsed.path.rstrip("/").rsplit("/", 1)[-1] or None
        return PageRef(
            url=url,
            site="fixture",
            chapter=chapter,
            page_number=int(raw_page) if raw_page.isdigit() else None,
        )

    @staticmethod
    def _media_url(node) -> str | None:
        """Best media URL for a node: ``href`` for links, lazy attributes before ``src``;
        ``data:`` placeholders are skipped."""
        if node.name == "a":
            candidate = node.get("href")
            return candidate if candidate and not candidate.startswith("data:") else None
        for attribute in _LAZY_ATTRIBUTES:
            value = node.get(attribute)
            if not value:
                continue
            if attribute == "srcset":
                value = value.split(",")[0].strip().split(" ")[0]
            if value and not value.startswith("data:"):
                return value
        return None


class FixtureFetcher:
    """In-memory :class:`~backend.scraper.crawler.PageFetcher` — serves fixture HTML, never the network.

    ``on_fetch`` runs (synchronously) at the start of every fetch — tests use it
    to cancel/pause a crawl mid-flight or to inspect the jobs row.
    """

    def __init__(
        self,
        pages: dict[str, str],
        on_fetch: Callable[[str], None] | None = None,
    ) -> None:
        self.pages = pages
        self.on_fetch = on_fetch
        self.fetched: list[str] = []
        self.closed = False

    async def fetch(self, url: str) -> str:
        self.fetched.append(url)
        if self.on_fetch is not None:
            self.on_fetch(url)
        page = self.pages.get(url)
        if page is None:
            raise PageFetchError(f"HTTP 404 for {url}")
        return page

    async def aclose(self) -> None:
        self.closed = True


def once(callback: Callable[[], None]) -> Callable[[str], None]:
    """Wrap ``callback`` so it fires only on the first fetch (idempotent test hooks)."""
    fired = False

    def hook(url: str) -> None:
        nonlocal fired
        if not fired:
            fired = True
            callback()

    return hook


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Poll ``predicate`` briefly; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("condition not met within timeout")
        await asyncio.sleep(0.005)
