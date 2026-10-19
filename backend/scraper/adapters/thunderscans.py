"""ThunderScans adapter for ``en-thunderscans.com`` (AGENTS.md §5, PRD §0/§6).

WordPress + Madara theme with the "Manga Comments System" plugin — the first
adapter whose comments are fully **server-rendered** (``render = False``).

Selector provenance — verified 2026-10-13 against live production pages:

* **Comment root:** every comment is
  ``<li class="mcs-comment" id="mcs-comment-{id}" data-comment-id="{id}"
  data-parent-id="…" data-mcs-depth="…">``; replies are FLAT siblings carrying
  parent ids (verified live: no nested ``li.mcs-comment``), so each ``li`` owns
  only its own content. The new-comment form and per-comment edit textarea share
  the ``mcs-`` prefix but are not comments.
* **Attachments:** the plugin's frontend JS  (
  ``mcs-frontend.js``: ``.mcs-comment-image-input`` file input +
  ``uploadImageAction`` admin-ajax upload, accepted mimes ``image/jpeg``,
  ``image/png``, ``image/gif``, 2 MiB cap) renders uploaded images inside
  ``div.mcs-comment-content-view`` — plain ``<img>`` or figure-wrapped.
* **Avatars:** ``span.mcs-comment-avatar > img`` (gravatar) is a separate
  column and never appears inside the content view (structural §6 exclusion).
* **Author / date:** ``span.mcs-comment-author``; ``time.mcs-comment-date``.
* **URLs:** chapters live at the site ROOT as
  ``/{optional-id-}{slug}-chapter-{n}/`` — the leading numeric prefix is a
  chapter id, NOT part of the series slug (verified: ``1482765166-leveling-up-
  by-killing-gods-chapter-65`` → series ``/comics/leveling-up-by-killing-gods/``).
  Series pages are Madara ``/comics/{slug}/`` with a ``.bxcl``/``#chapterlist``
  chapter list; the catalog index is ``/comics/`` with server-rendered
  ``?page=N`` pagination (verified: page 2 returns a fresh series grid).

Unverified / defensive: a chapter URL whose slug derivation 404s simply yields
an empty chapter list and the crawl falls back to the entry page with a warning;
catalog pagination stops at the first page without new series or at the safety
cap.
"""

from __future__ import annotations

import logging
import re
import time
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
_SITE_HOSTS = frozenset({"en-thunderscans.com", "www.en-thunderscans.com"})

#: URL shapes: root-level chapters ``/{id-}{slug}-chapter-{n}/`` and Madara
#: series ``/comics/{slug}/``. The slug itself may contain digits (many slugs
#: carry a numeric prefix on the series side too — that one IS part of the slug).
_CHAPTER_PATH_RE = re.compile(r"^/(?:(?:\d+)-)?([a-z0-9][a-z0-9-]*-chapter-\d+)/?$")
_SERIES_PATH_RE = re.compile(r"^/comics/([a-z0-9][a-z0-9-]*)/?$")
_CHAPTER_LINK_RE = re.compile(r"^/(?:(?:\d+)-)?[a-z0-9][a-z0-9-]*-chapter-\d+/?$")

#: Real comments carry ``data-comment-id``; the composer/form does not.
_COMMENT_SELECTOR = "li.mcs-comment[data-comment-id]"

#: URL suffix → media kind (PRD §5.3); anything else is a plain image.
_KIND_BY_SUFFIX: dict[str, MediaKind] = {".gif": "gif", ".mp4": "video", ".webm": "video"}

#: Ordered attribute preference for lazy-loaded images.
_LAZY_SRC_ATTRIBUTES: tuple[str, ...] = ("data-src", "data-original", "data-lazy-src", "srcset", "src")

_FETCH_TIMEOUT_SECONDS = 30.0

#: Politeness gap between catalog page fetches (AGENTS.md §9: ≥1s between pages).
_CATALOG_PAGE_DELAY_SECONDS = 1.0

#: Safety cap for catalog pagination (a misbehaving pager cannot loop forever).
_MAX_CATALOG_PAGES = 100

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


class ThunderScansAdapter(SiteAdapter):
    """Collect comment attachments from ThunderScans chapter pages (render = False)."""

    site = "thunderscans"

    #: mcs comments are server-rendered in the WP page — no browser needed (PRD §8).
    render = False

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
                        "skipping url handled by no adapter site=thunderscans url=%s", given
                    )
            return refs
        if scope.kind is ScopeKind.CURRENT_PAGE:
            return [self._page_ref(url)]
        if scope.kind is ScopeKind.CURRENT_CHAPTER:
            if _CHAPTER_PATH_RE.fullmatch(urlparse(url).path.rstrip("/")):
                return [self._page_ref(url)]
            logger.warning(
                "current-chapter scope entered from a non-chapter url site=thunderscans "
                "url=%s — scanning the entry page instead (enter a chapter url for "
                "chapter-level scans)",
                url,
            )
            return [self._page_ref(url)]
        return self._discover_entire_comic(url)

    def find_comments(self, page: str) -> list[Comment]:
        soup = BeautifulSoup(page, "html.parser")
        container = soup.select_one("#comments")
        scope = container if container is not None else soup
        elements = scope.select(_COMMENT_SELECTOR)
        if not elements:
            logger.warning(
                "comment selectors matched nothing site=thunderscans "
                "selector=li.mcs-comment[data-comment-id] within #comments "
                "(container %s) page_chars=%d",
                "present" if container is not None else "absent",
                len(page),
            )
            return []
        comments: list[Comment] = []
        for node in elements:
            comment_id = str(node.get("data-comment-id", "")).strip()
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
        for view in comment.element.select(".mcs-comment-content-view"):
            for node in view.select("img"):
                if not _is_own(comment.element, node):
                    continue  # a nested reply's attachment belongs to the reply.
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
                "comment attachment selector matched nothing site=thunderscans "
                "comment=%s selector=.mcs-comment-content-view img",
                comment.meta.comment_id,
            )
        return list(refs.values())

    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        return comment.meta

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _author(root: Tag) -> str | None:
        author = root.select_one(".mcs-comment-author")
        label = author.get_text(" ", strip=True) if author else ""
        return label or None

    @staticmethod
    def _text(root: Tag) -> str | None:
        view = root.select_one(".mcs-comment-content-view")
        if view is None:
            logger.debug(
                "comment content selector matched nothing site=thunderscans comment=%s "
                "selector=.mcs-comment-content-view",
                str(root.get("id", "")),
            )
            return None
        return view.get_text(" ", strip=True) or None

    def discover_series(self, url: str) -> list[SeriesRef]:
        """Every series on the Madara ``/comics/`` catalog index, pagination included.

        The index paginates with server-rendered ``?page=N`` links (verified live
        2026-10-13: page 2 returns a fresh grid). Pages are fetched sequentially
        with the configured politeness gap and stop at the first page without new
        series or the safety cap.
        """
        path = urlparse(url).path.rstrip("/")
        if path != "/comics":
            logger.warning(
                "series discovery needs the catalog index site=thunderscans url=%s", url
            )
            return []
        origin = _origin(url)
        host = urlparse(url).netloc
        series: list[SeriesRef] = []
        seen: set[str] = set()
        page_number = 1
        next_url: str | None = url
        while next_url is not None and page_number <= _MAX_CATALOG_PAGES:
            try:
                html = self._fetch_html(next_url)
            except (httpx.HTTPError, OSError, UnsafeURLError) as exc:
                logger.warning(
                    "catalog fetch failed site=thunderscans url=%s page=%d error=%s",
                    next_url, page_number, exc,
                )
                break
            soup = BeautifulSoup(html, "html.parser")
            new_on_page = 0
            for anchor in soup.select("a[href]"):
                absolute = urljoin(next_url, str(anchor.get("href", "")))
                parsed = urlparse(absolute)
                if parsed.netloc and parsed.netloc != host:
                    continue
                series_path = parsed.path.rstrip("/")
                match = _SERIES_PATH_RE.fullmatch(series_path)
                if match is None or series_path in seen:
                    continue
                slug = match.group(1)
                if slug in ("feed", "page"):
                    continue
                seen.add(series_path)
                new_on_page += 1
                title = anchor.get_text(" ", strip=True) or None
                # Madara canonical series URLs always carry the trailing slash.
                series.append(SeriesRef(url=f"{origin}{series_path}/", title=title))
            if new_on_page == 0:
                break
            page_number += 1
            next_url = f"{origin}/comics/?page={page_number}"
            time.sleep(_CATALOG_PAGE_DELAY_SECONDS)
        if not series:
            logger.warning(
                "catalog discovery matched nothing site=thunderscans url=%s "
                "selector=a[href^=/comics/]",
                url,
            )
        else:
            logger.info(
                "catalog discovery done site=thunderscans series=%d pages=%d",
                len(series), page_number,
            )
        return series

    def _discover_entire_comic(self, url: str) -> list[PageRef]:
        entry_ref = self._page_ref(url)
        series_url = self._series_url_for(url)
        if series_url is None:
            logger.warning(
                "entire-comic scope needs a series or chapter url site=thunderscans "
                "url=%s — scanning the entry page instead",
                url,
            )
            return [entry_ref]
        try:
            html = self._fetch_html(series_url)
        except (httpx.HTTPError, OSError, UnsafeURLError) as exc:
            logger.warning(
                "series page fetch failed site=thunderscans url=%s error=%s", series_url, exc
            )
            return [entry_ref]
        chapter_urls = self._chapter_urls(html, series_url)
        if not chapter_urls:
            logger.warning(
                "chapter discovery matched nothing site=thunderscans url=%s "
                "selector=a[href*=chapter-] — scanning the entry page instead",
                series_url,
            )
            return [entry_ref]
        return [self._page_ref(chapter_url) for chapter_url in chapter_urls]

    @staticmethod
    def _series_url_for(url: str) -> str | None:
        """Series page URL for a chapter or series URL (strips the chapter-id prefix)."""
        path = urlparse(url).path.rstrip("/")
        series_match = _SERIES_PATH_RE.fullmatch(path)
        if series_match is not None:
            return f"{_origin(url)}/comics/{series_match.group(1)}/"
        chapter_match = _CHAPTER_PATH_RE.fullmatch(path)
        if chapter_match is not None:
            slug_with_chapter = chapter_match.group(1)
            slug = re.sub(r"-chapter-\d+$", "", slug_with_chapter)
            if slug:
                return f"{_origin(url)}/comics/{slug}/"
        return None

    @staticmethod
    def _chapter_urls(html: str, series_url: str) -> list[str]:
        """Chapter URLs from a Madara series page (``.bxcl`` list first, any anchor second)."""
        soup = BeautifulSoup(html, "html.parser")
        origin = _origin(series_url)
        origin_netloc = urlparse(series_url).netloc
        urls: list[str] = []
        seen: set[str] = set()

        def collect(anchor: Tag) -> None:
            absolute = urljoin(series_url, str(anchor.get("href", "")))
            parsed = urlparse(absolute)
            if parsed.netloc and parsed.netloc != origin_netloc:
                return
            path = parsed.path.rstrip("/")
            if not _CHAPTER_LINK_RE.fullmatch(path):
                return
            if path in seen:
                return
            seen.add(path)
            urls.append(f"{origin}{path}/")

        for anchor in soup.select(".bxcl a[href], #chapterlist a[href]"):
            collect(anchor)
        if not urls:
            for anchor in soup.select("a[href]"):
                collect(anchor)
        return urls

    @staticmethod
    def _page_ref(url: str) -> PageRef:
        path = urlparse(url).path.rstrip("/")
        match = _CHAPTER_PATH_RE.fullmatch(path)
        chapter = None
        if match is not None:
            chapter = match.group(1).rsplit("-chapter-", 1)[-1]
        return PageRef(url=url, site="thunderscans", chapter=chapter)


def _is_own(root: Tag, node: Tag) -> bool:
    """True when ``node`` is not inside a nested comment (replies own their media)."""
    owner = node.find_parent("li", class_="mcs-comment")
    return owner is None or owner is root


register(ThunderScansAdapter())
