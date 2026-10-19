"""FlameComics adapter for ``flamecomics.xyz`` (AGENTS.md §5, PRD §0/§6).

Next.js + Mantine reader whose comments are **client-rendered** (``render = True``).

Selector provenance — verified 2026-10-13 against live rendered chapter pages:

* **Comment root:** every comment is ``div.Comment-module__{hash}__item`` with a
  short uuid-like ``id`` (e.g. ``id="afdffdac"``). The middle ``{hash}`` is a
  CSS-module build hash that **changes per deploy** (``iXHq0G`` at verification
  time), so every selector here matches only the stable ``Comment-module__``
  prefix and the ``__item``/``__commentContent``/``__avatar`` suffixes — and the
  test fixtures deliberately use a *different* hash to prove deploy independence.
  Sort bar (``__sortBarWrapper``), action buttons, and the login overlay share the
  module prefix but never ``__item`` + an ``id``.
* **Attachments:** the comment composer ships a native file input
  (``accept="image/jpeg,image/png,image/webp,image/gif"`` — verified live), so
  comments can carry image/GIF attachments; they render as ``<img>`` inside
  ``div[id^="comment-content-"][aria-label="Comment content"]`` (the id mirrors the
  comment id: ``comment-content-{id}``).
* **Avatars:** ``div[class*=__avatar]`` (Mantine avatar — placeholder or img) sits
  OUTSIDE the content div, so the §6 attachments-only rule holds structurally.
* **Author:** ``a[aria-label^="View profile: "]`` inside the item; ``title`` attr
  carries the display name.
* **Replies** mount lazily behind a "Show N replies" button — only rendered
  replies are collected (each expanded reply is its own ``__item`` div).
* **URLs:** series pages ``/series/{numeric-id}`` (server-rendered chapter links
  ``/series/{id}/{hex-hash}``, 200 links verified live); chapter pages add the
  hex-hash segment; the full catalog is the single-page ``/browse`` index
  (154 series, no pagination — verified live 2026-10-13).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from backend.scraper.adapters.base import (
    Comment,
    CommentMeta,
    CrawlScope,
    MediaKind,
    MediaRef,
    PageRef,
    ScopeKind,
    SeriesRef,
    SiteAdapter,
    register,
)
from backend.security.fetch import guarded_get
from backend.security.urls import UnsafeURLError

logger = logging.getLogger(__name__)

#: Hosts this adapter owns (www variant included).
_SITE_HOSTS = frozenset({"flamecomics.xyz", "www.flamecomics.xyz"})

#: URL shapes: ``/series/{id}`` and ``/series/{id}/{hex-hash}``; catalog ``/browse``.
_SERIES_PATH_RE = re.compile(r"^/series/(\d+)/?$")
_CHAPTER_PATH_RE = re.compile(r"^/series/(\d+)/([a-z0-9]{8,32})/?$")

#: Real comments carry an ``id`` on the ``__item`` div; chrome sharing the module
#: prefix (sort bar, action buttons) never has one.
_COMMENT_SELECTOR = "div[class*='Comment-module__'][class*='__item'][id]"

#: Content div of one comment — id mirrors the comment id.
_CONTENT_SELECTOR = "div[id^='comment-content-']"

#: Author link — ``aria-label="View profile: {name}"``.
_AUTHOR_SELECTOR = "a[aria-label^='View profile: ']"
_AUTHOR_LABEL_PREFIX = "View profile: "

#: URL suffix → media kind (PRD §5.3); anything else is a plain image.
_KIND_BY_SUFFIX: dict[str, MediaKind] = {".gif": "gif", ".mp4": "video", ".webm": "video"}

#: Ordered attribute preference for lazy-loaded images.
_LAZY_SRC_ATTRIBUTES: tuple[str, ...] = ("data-src", "data-original", "data-lazy-src", "srcset", "src")

_FETCH_TIMEOUT_SECONDS = 30.0

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _classify_kind(url: str) -> MediaKind:
    """Classify by URL suffix: .gif → gif, .mp4/.webm → video, else image."""
    path = urlparse(url).path.lower()
    for suffix, kind in _KIND_BY_SUFFIX.items():
        if path.endswith(suffix):
            return kind
    return "image"


def _original_filename(url: str) -> str | None:
    return urlparse(url).path.rsplit("/", 1)[-1] or None


def _candidate_src(node: Tag) -> str | None:
    """Best media URL for a node: lazy attributes before ``src``; skip ``data:`` placeholders."""
    for attribute in _LAZY_SRC_ATTRIBUTES:
        value = node.get(attribute)
        if not value:
            continue
        if attribute == "srcset":
            value = value.split(",")[0].strip().split(" ")[0]
        value = value.strip()
        if value and not value.startswith("data:"):
            return value
    return None


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _default_fetch_html(url: str) -> str:
    """Live fetch used by discovery (runs inside ``asyncio.to_thread``).

    Goes through the URL guard so a hostile redirect cannot pivot the fetch
    onto a private/metadata address (PRD §41).
    """
    response = guarded_get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_FETCH_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.text


class FlameComicsAdapter(SiteAdapter):
    """Collect comment attachments from FlameComics chapter pages (render = True)."""

    site = "flamecomics"

    #: Comment threads hydrate client-side after the Next.js payload lands (PRD §8).
    render = True

    #: The comment module lazy-mounts only when scrolled into view (verified live).
    scroll_before_capture = True

    def __init__(self, *, fetch_html: Callable[[str], str] | None = None) -> None:
        self._fetch_html = fetch_html if fetch_html is not None else _default_fetch_html

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        return (parsed.hostname or "").lower() in _SITE_HOSTS

    def discover_pages(self, url: str, scope: CrawlScope) -> list[PageRef]:
        if scope.kind in (ScopeKind.CUSTOM_URLS, ScopeKind.MULTIPLE_CHAPTERS):
            refs: list[PageRef] = []
            for given in scope.urls:
                if self.can_handle(given):
                    refs.append(self._page_ref(given))
                else:
                    logger.warning(
                        "skipping url handled by no adapter site=flamecomics url=%s", given
                    )
            return refs
        if scope.kind is ScopeKind.CURRENT_PAGE:
            return [self._page_ref(url)]
        if scope.kind is ScopeKind.CURRENT_CHAPTER:
            if _CHAPTER_PATH_RE.fullmatch(urlparse(url).path.rstrip("/")):
                return [self._page_ref(url)]
            logger.warning(
                "current-chapter scope entered from a non-chapter url site=flamecomics "
                "url=%s — scanning the entry page instead (enter a chapter url for "
                "chapter-level scans)",
                url,
            )
            return [self._page_ref(url)]
        return self._discover_entire_comic(url)

    def find_comments(self, page: str) -> list[Comment]:
        soup = BeautifulSoup(page, "html.parser")
        elements = soup.select(_COMMENT_SELECTOR)
        if not elements:
            logger.warning(
                "comment selectors matched nothing site=flamecomics "
                "selector=div[class*=Comment-module__][class*=__item][id] "
                "page_chars=%d",
                len(page),
            )
            return []
        comments: list[Comment] = []
        for node in elements:
            comment_id = str(node.get("id", "")).strip()
            if not comment_id:
                continue
            meta = CommentMeta(
                comment_id=comment_id,
                author_name=self._author(node),
                text=self._text(node),
            )
            comments.append(Comment(meta=meta, element=node))
        return comments

    def find_comment_media(self, comment: Comment) -> list[MediaRef]:
        refs: dict[str, MediaRef] = {}
        for content in comment.element.select(_CONTENT_SELECTOR):
            for node in content.select("img"):
                if not _is_own(comment.element, node):
                    continue  # an expanded reply's attachment belongs to the reply.
                candidate = _candidate_src(node)
                if not candidate:
                    continue
                absolute = urljoin(comment.meta.page_url, candidate)
                if absolute in refs:
                    continue
                refs[absolute] = MediaRef(
                    url=absolute,
                    kind=_classify_kind(absolute),
                    comment=comment.meta,
                    original_filename=_original_filename(absolute),
                )
        if not refs:
            logger.debug(
                "comment attachment selector matched nothing site=flamecomics "
                "comment=%s selector=div[id^=comment-content-] img",
                comment.meta.comment_id,
            )
        return list(refs.values())

    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        return comment.meta

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _author(root: Tag) -> str | None:
        link = root.select_one(_AUTHOR_SELECTOR)
        if link is None:
            return None
        label = str(link.get("title", "")).strip()
        if not label:
            aria = str(link.get("aria-label", ""))
            if aria.startswith(_AUTHOR_LABEL_PREFIX):
                label = aria[len(_AUTHOR_LABEL_PREFIX):].strip()
        return label or None

    @staticmethod
    def _text(root: Tag) -> str | None:
        content = root.select_one(_CONTENT_SELECTOR)
        if content is None:
            logger.debug(
                "comment content selector matched nothing site=flamecomics comment=%s "
                "selector=div[id^=comment-content-]",
                str(root.get("id", "")),
            )
            return None
        return content.get_text(" ", strip=True) or None

    def discover_series(self, url: str) -> list[SeriesRef]:
        """Every series on the single-page ``/browse`` catalog index.

        Verified live 2026-10-13: ``/browse`` server-renders all 154 series links
        with no pagination.
        """
        path = urlparse(url).path.rstrip("/")
        if path != "/browse":
            logger.warning(
                "series discovery needs the catalog index site=flamecomics url=%s", url
            )
            return []
        try:
            html = self._fetch_html(url)
        except (httpx.HTTPError, OSError, UnsafeURLError) as exc:
            logger.warning("catalog fetch failed site=flamecomics url=%s error=%s", url, exc)
            return []
        origin = _origin(url)
        host = urlparse(url).netloc
        series: list[SeriesRef] = []
        seen: set[str] = set()
        for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
            absolute = urljoin(url, str(anchor.get("href", "")))
            parsed = urlparse(absolute)
            if parsed.netloc and parsed.netloc != host:
                continue
            series_path = parsed.path.rstrip("/")
            if not _SERIES_PATH_RE.fullmatch(series_path) or series_path in seen:
                continue
            seen.add(series_path)
            title = anchor.get_text(" ", strip=True) or None
            series.append(SeriesRef(url=f"{origin}{series_path}", title=title))
        if not series:
            logger.warning(
                "catalog discovery matched nothing site=flamecomics url=%s "
                "selector=a[href^=/series/]",
                url,
            )
        else:
            logger.info(
                "catalog discovery done site=flamecomics series=%d url=%s", len(series), url
            )
        return series

    def _discover_entire_comic(self, url: str) -> list[PageRef]:
        entry_ref = self._page_ref(url)
        path = urlparse(url).path.rstrip("/")
        series_match = _CHAPTER_PATH_RE.fullmatch(path) or _SERIES_PATH_RE.fullmatch(path)
        if series_match is None:
            logger.warning(
                "entire-comic scope needs a series or chapter url site=flamecomics "
                "url=%s — scanning the entry page instead",
                url,
            )
            return [entry_ref]
        series_path = f"/series/{series_match.group(1)}"
        series_url = f"{_origin(url)}{series_path}"
        try:
            html = self._fetch_html(series_url)
        except (httpx.HTTPError, OSError, UnsafeURLError) as exc:
            logger.warning(
                "series page fetch failed site=flamecomics url=%s error=%s", series_url, exc
            )
            return [entry_ref]
        chapter_urls = self._chapter_urls(html, series_url)
        if not chapter_urls:
            logger.warning(
                "chapter discovery matched nothing site=flamecomics url=%s "
                "selector=a[href^=/series/{id}/] — scanning the entry page instead",
                series_url,
            )
            return [entry_ref]
        return [self._page_ref(chapter_url) for chapter_url in chapter_urls]

    @staticmethod
    def _chapter_urls(html: str, series_url: str) -> list[str]:
        """Chapter URLs from a series page (server-rendered ``/series/{id}/{hash}`` links)."""
        soup = BeautifulSoup(html, "html.parser")
        origin = _origin(series_url)
        origin_netloc = urlparse(series_url).netloc
        series_id = _SERIES_PATH_RE.fullmatch(urlparse(series_url).path.rstrip("/"))
        prefix = f"/series/{series_id.group(1)}/" if series_id else "/series/"
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            absolute = urljoin(series_url, str(anchor.get("href", "")))
            parsed = urlparse(absolute)
            if parsed.netloc and parsed.netloc != origin_netloc:
                continue
            path = parsed.path.rstrip("/")
            if not path.startswith(prefix) or not _CHAPTER_PATH_RE.fullmatch(path):
                continue
            if path in seen:
                continue
            seen.add(path)
            urls.append(f"{origin}{path}")
        return urls

    @staticmethod
    def _page_ref(url: str) -> PageRef:
        match = _CHAPTER_PATH_RE.fullmatch(urlparse(url).path.rstrip("/"))
        chapter = match.group(2) if match else None
        return PageRef(url=url, site="flamecomics", chapter=chapter)


def _is_own(root: Tag, node: Tag) -> bool:
    """True when ``node`` is not inside a nested comment (expanded replies own their media)."""
    owner = node.find_parent(
        "div", class_=lambda c: "Comment-module__" in c and c.endswith("__item")
    )
    return owner is None or owner is root


register(FlameComicsAdapter())
