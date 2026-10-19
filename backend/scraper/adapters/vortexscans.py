"""VortexScans adapter for ``vortexscans.org`` (AGENTS.md §5, PRD §0/§6).

Next.js reader on the "vortex" white-label platform (hivetoons.org runs the same
software — see ``backend.scraper.adapters.hivetoons``; its build config literally
carries ``SITE_SLUG: "vortexscans"``). The platform's config advertises Disqus /
"VComments", but both are disabled in production — the live comment section is the
platform's own client-rendered system (verified 2026-10-13 with scrolled renders;
no Disqus iframe ever loads).

Selector provenance — verified 2026-10-13 against live rendered chapter pages:

* **Comment root:** ``<div id="comment-{id}" data-comment-id="{id}" class="relative">``;
  nested replies are the same shape INSIDE their parent (each reply is its own
  comment with its own id). The logged-out composer ("Join the discussion"),
  sort controls (Best/New/Old), reaction chips, and rank/badge buttons share the
  container but never carry ``data-comment-id``.
* **Author:** the comment's ``<article aria-label="{username}">``; avatars are
  letter spans or profile-link images in their own column.
* **Body:** ``div.prose…comment-body`` — the semantic ``comment-body`` class is
  stable across builds (the tailwind utility noise around it is not relied on).
* **Attachments (best-effort):** the platform currently exposes no image-attach
  affordance in the composer and every sampled comment body was text+emoji only,
  so live yield is expected to be near-zero; IF an attachment ever renders as
  ``<img>`` inside a comment body it is collected here. Reaction emotes
  (``/theme/emotes/…``) and achievement badges (``/theme/achievements/…``) sit
  OUTSIDE the body and are never collected (structural §6 exclusion).
* **URLs:** series ``/series/{slug}`` (server-rendered chapter links, verified);
  chapters ``/series/{slug}/chapter-{n}``; catalog ``/series?page=N``
  server-rendered pagination (36/page vortex, 50/page hivetoons — verified).
* Comments render behind a drawer: the reader toolbar carries a
  ``button[aria-label^="Comments"]`` (label includes the count) and comment
  items mount only after that button is clicked — verified live 2026-10-19:
  scroll alone → 0 ``div[data-comment-id]``; click → 24 roots with the real
  body attachments (tenor GIFs) present. Hence ``scroll_before_capture = True``
  and ``click_before_capture = 'button[aria-label^="Comments"]'``.
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

#: URL shapes: ``/series/{slug}`` and ``/series/{slug}/chapter-{n}``; catalog ``/series``.
_CHAPTER_PATH_RE = re.compile(r"^/series/([^/]+)/chapter-(\d+)/?$")
_SERIES_PATH_RE = re.compile(r"^/series/([^/]+)/?$")

#: Real comments carry ``data-comment-id``; composer/sort/badge chrome does not.
_COMMENT_SELECTOR = "div[data-comment-id]"

#: Comment body — the stable semantic class amid tailwind prose utilities.
_BODY_SELECTOR = ".comment-body"

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


class VortexPlatformAdapter(SiteAdapter):
    """Shared implementation for the vortex white-label reader platform."""

    #: Client-rendered comments (PRD §8).
    render = True

    #: Comments lazy-mount only when scrolled into view (verified live).
    scroll_before_capture = True

    #: Comment items mount only after the "Comments {n}" drawer button is
    #: clicked — scrolling alone never renders them (verified live 2026-10-19:
    #: zero ``div[data-comment-id]`` without the click, 24 with it).
    click_before_capture = 'button[aria-label^="Comments"]'

    #: Hosts this adapter owns; set by the concrete per-site subclass.
    _hosts: frozenset[str] = frozenset()

    def __init__(self, *, fetch_html: Callable[[str], str] | None = None) -> None:
        self._fetch_html = fetch_html if fetch_html is not None else _default_fetch_html

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        return (parsed.hostname or "").lower() in self._hosts

    def discover_pages(self, url: str, scope: CrawlScope) -> list[PageRef]:
        if scope.kind in (ScopeKind.CUSTOM_URLS, ScopeKind.MULTIPLE_CHAPTERS):
            refs: list[PageRef] = []
            for given in scope.urls:
                if self.can_handle(given):
                    refs.append(self._page_ref(given))
                else:
                    logger.warning(
                        "skipping url handled by no adapter site=%s url=%s", self.site, given
                    )
            return refs
        if scope.kind is ScopeKind.CURRENT_PAGE:
            return [self._page_ref(url)]
        if scope.kind is ScopeKind.CURRENT_CHAPTER:
            if _CHAPTER_PATH_RE.fullmatch(urlparse(url).path.rstrip("/")):
                return [self._page_ref(url)]
            logger.warning(
                "current-chapter scope entered from a non-chapter url site=%s "
                "url=%s — scanning the entry page instead (enter a chapter url for "
                "chapter-level scans)",
                self.site, url,
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
                "comment selectors matched nothing site=%s selector=div[data-comment-id] "
                "within #comments (container %s) page_chars=%d",
                self.site, "present" if container is not None else "absent", len(page),
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
        for body in comment.element.select(_BODY_SELECTOR):
            for node in body.select("img"):
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
                "comment attachment selector matched nothing site=%s comment=%s "
                "selector=.comment-body img",
                self.site, comment.meta.comment_id,
            )
        return list(refs.values())

    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        return comment.meta

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _author(root: Tag) -> str | None:
        article = root.find("article")
        label = str(article.get("aria-label", "")).strip() if article else ""
        return label or None

    def _text(self, root: Tag) -> str | None:
        body = root.select_one(_BODY_SELECTOR)
        if body is None:
            logger.debug(
                "comment body selector matched nothing site=%s comment=%s selector=.comment-body",
                self.site, str(root.get("data-comment-id", "")),
            )
            return None
        return body.get_text(" ", strip=True) or None

    def discover_series(self, url: str) -> list[SeriesRef]:
        """Every series on the paginated ``/series`` index (server-rendered pages)."""
        path = urlparse(url).path.rstrip("/")
        if path != "/series":
            logger.warning(
                "series discovery needs the catalog index site=%s url=%s", self.site, url
            )
            return []
        origin = _origin(url)
        host = urlparse(url).netloc
        series: list[SeriesRef] = []
        seen: set[str] = set()
        page_number = 1
        while page_number <= _MAX_CATALOG_PAGES:
            page_url = url if page_number == 1 else f"{origin}/series?page={page_number}"
            try:
                html = self._fetch_html(page_url)
            except (httpx.HTTPError, OSError, UnsafeURLError) as exc:
                logger.warning(
                    "catalog fetch failed site=%s url=%s page=%d error=%s",
                    self.site, page_url, page_number, exc,
                )
                break
            soup = BeautifulSoup(html, "html.parser")
            new_on_page = 0
            for anchor in soup.select("a[href]"):
                absolute = urljoin(page_url, str(anchor.get("href", "")))
                parsed = urlparse(absolute)
                if parsed.netloc and parsed.netloc != host:
                    continue
                series_path = parsed.path.rstrip("/")
                match = _SERIES_PATH_RE.fullmatch(series_path)
                if match is None or match.group(1) == "series" or series_path in seen:
                    continue
                seen.add(series_path)
                new_on_page += 1
                title = anchor.get_text(" ", strip=True) or None
                series.append(SeriesRef(url=f"{origin}{series_path}", title=title))
            if new_on_page == 0:
                break
            page_number += 1
            time.sleep(_CATALOG_PAGE_DELAY_SECONDS)
        if not series:
            logger.warning(
                "catalog discovery matched nothing site=%s url=%s selector=a[href^=/series/]",
                self.site, url,
            )
        else:
            logger.info(
                "catalog discovery done site=%s series=%d pages=%d",
                self.site, len(series), page_number,
            )
        return series

    def _discover_entire_comic(self, url: str) -> list[PageRef]:
        entry_ref = self._page_ref(url)
        path = urlparse(url).path.rstrip("/")
        series_match = _CHAPTER_PATH_RE.fullmatch(path) or _SERIES_PATH_RE.fullmatch(path)
        if series_match is None:
            logger.warning(
                "entire-comic scope needs a series or chapter url site=%s url=%s "
                "— scanning the entry page instead",
                self.site, url,
            )
            return [entry_ref]
        series_url = f"{_origin(url)}/series/{series_match.group(1)}"
        try:
            html = self._fetch_html(series_url)
        except (httpx.HTTPError, OSError, UnsafeURLError) as exc:
            logger.warning(
                "series page fetch failed site=%s url=%s error=%s", self.site, series_url, exc
            )
            return [entry_ref]
        chapter_urls = self._chapter_urls(html, series_url)
        if not chapter_urls:
            logger.warning(
                "chapter discovery matched nothing site=%s url=%s "
                "selector=a[href*=chapter-] — scanning the entry page instead",
                self.site, series_url,
            )
            return [entry_ref]
        return [self._page_ref(chapter_url) for chapter_url in chapter_urls]

    @staticmethod
    def _chapter_urls(html: str, series_url: str) -> list[str]:
        """Chapter URLs from a (server-rendered) series page."""
        soup = BeautifulSoup(html, "html.parser")
        origin = _origin(series_url)
        origin_netloc = urlparse(series_url).netloc
        slug = _SERIES_PATH_RE.fullmatch(urlparse(series_url).path.rstrip("/"))
        prefix = f"/series/{slug.group(1)}/" if slug else "/series/"
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

    def _page_ref(self, url: str) -> PageRef:
        match = _CHAPTER_PATH_RE.fullmatch(urlparse(url).path.rstrip("/"))
        chapter = match.group(2) if match else None
        return PageRef(url=url, site=self.site, chapter=chapter)


def _is_own(root: Tag, node: Tag) -> bool:
    """True when ``node`` is not inside a nested comment (replies own their media)."""
    owner = node.find_parent("div", attrs={"data-comment-id": True})
    return owner is None or owner is root


class VortexScansAdapter(VortexPlatformAdapter):
    """Collect comment attachments from VortexScans chapter pages."""

    site = "vortexscans"
    _hosts = frozenset({"vortexscans.org", "www.vortexscans.org"})


register(VortexScansAdapter())
