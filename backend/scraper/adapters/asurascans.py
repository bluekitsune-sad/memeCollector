"""AsuraScans adapter for ``asurascans.com`` (AGENTS.md §5, PRD §0/§6).

Selector provenance — verified 2026-10-08 against live production assets:

* **Comment root:** the live ``CommentsSection.*.js`` bundle renders every
  comment as ``<div id="comment-{id}" class="py-4 scroll-mt-20 relative …">``;
  replies nest inside their parent with their own ``comment-{id}``.
  ``div#comment-composer`` (the draft box) shares the prefix and is excluded by
  the numeric-id rule.
* **Attachments:** bundle component ``W`` renders each entry of
  ``media_urls``/``gif_url`` as ``<img src=… alt="Comment media" …>`` inside a
  ``div.relative.inline-block`` wrapper. Avatars are ``a[href^="/user/"] > img``
  and never carry that alt text, so the §6 rule (attachments only) holds
  structurally.
* **Author:** ``a[href="/user/{username}"]`` whose text is the username (the
  avatar link wraps only an ``img``).
* **Body:** ``div.text-sm.text-zinc-300.leading-relaxed.break-words``.
* **Comments container:** chapter pages server-render ``<div
  id="comments-section">`` (composer + sort controls); the comments themselves
  are loaded client-side from ``api.asurascans.com`` — hence ``render = True``.
* **Chapter URLs:** ``/comics/{slug}-{hash}/chapter/{number}``. The series page
  (fetched statically for ENTIRE_COMIC) contains both plain
  ``a[href^="…/chapter/"]`` links and a ``<script id="continue-reading-data">``
  JSON blob with ``base`` + ``numbers`` (both verified on the live page).

Unverified / defensive: markup variants without ``alt="Comment media"`` yield
no attachments (logged at debug per comment — never a guess at page chrome);
if the series-page chapter list cannot be parsed, discovery logs a warning and
falls back to the entry URL instead of failing the crawl.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from html import unescape
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
_SITE_HOSTS = frozenset({"asurascans.com", "www.asurascans.com"})

#: URL shapes on asurascans.com (series page vs. chapter page).
_CHAPTER_PATH_RE = re.compile(r"^/comics/[^/]+/chapter/[^/]+$")
_SERIES_PATH_RE = re.compile(r"^/comics/[^/]+$")

#: Real comments carry ``id="comment-{digits}"``; ``comment-composer`` does not.
_COMMENT_ID_RE = re.compile(r"^comment-(\d+)$")

#: URL suffix → media kind (PRD §5.3); anything else is a plain image.
_KIND_BY_SUFFIX: dict[str, MediaKind] = {".gif": "gif", ".mp4": "video", ".webm": "video"}

#: Catalog payload entries on ``/comics``: ``"slug":[0,"…"],"title":[0,"…"]``
#: (RSC-style pairs; verified live 2026-10-08 after HTML-entity unescaping).
_PAYLOAD_ENTRY_RE = re.compile(
    r'"slug":\[0,"([^"]+)"\][^{}]{0,500}?"title":\[0,"((?:[^"\\]|\\.)*)"\]'
)

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


class AsuraScansAdapter(SiteAdapter):
    """Collect comment attachments from AsuraScans chapter pages (render = True)."""

    site = "asurascans"

    #: Chapter comments are client-loaded from api.asurascans.com (PRD §8).
    render = True

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
                        "skipping url handled by no adapter site=asurascans url=%s", given
                    )
            return refs
        if scope.kind is ScopeKind.CURRENT_PAGE:
            return [self._page_ref(url)]
        if scope.kind is ScopeKind.CURRENT_CHAPTER:
            if _CHAPTER_PATH_RE.fullmatch(urlparse(url).path.rstrip("/")):
                return [self._page_ref(url)]
            logger.warning(
                "current-chapter scope entered from a non-chapter url site=asurascans "
                "url=%s — scanning the entry page instead (enter a chapter url for "
                "chapter-level scans)",
                url,
            )
            return [self._page_ref(url)]
        return self._discover_entire_comic(url)

    def find_comments(self, page: str) -> list[Comment]:
        soup = BeautifulSoup(page, "html.parser")
        container = soup.select_one("#comments-section")
        scope = container if container is not None else soup
        elements = [
            node
            for node in scope.select('div[id^="comment-"]')
            if _COMMENT_ID_RE.match(str(node.get("id", "")))
        ]
        if not elements:
            if container is None:
                logger.warning(
                    "comment selectors matched nothing site=asurascans "
                    "selector=div[id^=comment-] within #comments-section (container "
                    "absent) page_chars=%d",
                    len(page),
                )
            else:
                logger.warning(
                    "comment selectors matched nothing site=asurascans "
                    "selector=div[id^=comment-] within #comments-section page_chars=%d "
                    "(comments are client-rendered — is render=True enabled?)",
                    len(page),
                )
            return []
        comments: list[Comment] = []
        for node in elements:
            comment_id = _COMMENT_ID_RE.match(str(node.get("id", ""))).group(1)
            meta = CommentMeta(
                comment_id=comment_id,
                author_name=self._author(node),
                text=self._text(node),
            )
            comments.append(Comment(meta=meta, element=node))
        return comments

    def find_comment_media(self, comment: Comment) -> list[MediaRef]:
        refs: dict[str, MediaRef] = {}
        for node in comment.element.select('img[alt="Comment media"]'):
            if not _is_own(comment.element, node):
                continue  # a reply's attachment belongs to the reply, not the parent.
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
                "comment attachment selector matched nothing site=asurascans "
                "comment=%s selector=img[alt='Comment media']",
                comment.meta.comment_id,
            )
        return list(refs.values())

    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        return comment.meta

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _author(root: Tag) -> str | None:
        for link in root.select('a[href^="/user/"]'):
            if not _is_own(root, link):
                continue
            label = link.get_text(" ", strip=True)
            if label:
                return label
        for image in root.select('a[href^="/user/"] img[alt]'):
            if _is_own(root, image) and image.get("alt"):
                return str(image.get("alt"))
        return None

    @staticmethod
    def _text(root: Tag) -> str | None:
        for body in root.select("div.text-zinc-300.leading-relaxed"):
            if _is_own(root, body):
                return body.get_text(" ", strip=True)
        logger.debug(
            "comment body selector matched nothing site=asurascans comment=%s "
            "selector=div.text-zinc-300.leading-relaxed",
            str(root.get("id", "")),
        )
        return None

    def discover_series(self, url: str) -> list[SeriesRef]:
        """Every series on the ``/comics`` catalog index: link hrefs + payload titles.

        The index is a single page (verified live 2026-10-08 — query-string
        pagination is ignored by the server). Series URLs come from
        ``a[href^=/comics/]`` anchors; titles come from the embedded catalog
        payload (``slug``/``title`` pairs) with the anchor text as fallback.
        """
        path = urlparse(url).path.rstrip("/")
        if path != "/comics":
            logger.warning(
                "series discovery needs the catalog index site=asurascans url=%s", url
            )
            return []
        try:
            html = self._fetch_html(url)
        except (httpx.HTTPError, OSError, UnsafeURLError) as exc:
            logger.warning("catalog fetch failed site=asurascans url=%s error=%s", url, exc)
            return []
        titles = self._catalog_titles(html)
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
            slug = series_path.rsplit("/", 1)[-1]
            title = titles.get(slug) or anchor.get_text(" ", strip=True) or None
            series.append(SeriesRef(url=f"{origin}{series_path}", title=title))
        if not series:
            logger.warning(
                "catalog discovery matched nothing site=asurascans url=%s "
                "selectors=a[href^=/comics/], catalog payload",
                url,
            )
        return series

    @staticmethod
    def _catalog_titles(html: str) -> dict[str, str]:
        """``slug → title`` from the page's embedded catalog payload (best effort)."""
        titles: dict[str, str] = {}
        for slug, title in _PAYLOAD_ENTRY_RE.findall(unescape(html)):
            if title:
                titles.setdefault(slug, title)
        return titles

    def _discover_entire_comic(self, url: str) -> list[PageRef]:
        entry_ref = self._page_ref(url)
        series_path = re.sub(r"/chapter/[^/]+$", "", urlparse(url).path.rstrip("/"))
        if not _SERIES_PATH_RE.fullmatch(series_path):
            logger.warning(
                "entire-comic scope needs a series or chapter url site=asurascans "
                "url=%s — scanning the entry page instead",
                url,
            )
            return [entry_ref]
        series_url = f"{_origin(url)}{series_path}"
        try:
            html = self._fetch_html(series_url)
        except (httpx.HTTPError, OSError, UnsafeURLError) as exc:
            logger.warning(
                "series page fetch failed site=asurascans url=%s error=%s", series_url, exc
            )
            return [entry_ref]
        chapter_urls = self._chapter_urls(html, series_url)
        if not chapter_urls:
            logger.warning(
                "chapter discovery matched nothing site=asurascans url=%s "
                "selectors=a[href*=/chapter/], #continue-reading-data — scanning the "
                "entry page instead",
                series_url,
            )
            return [entry_ref]
        return [self._page_ref(chapter_url) for chapter_url in chapter_urls]

    @staticmethod
    def _chapter_urls(html: str, series_url: str) -> list[str]:
        """Chapter URLs from the series page: rendered links first, JSON blob as fallback."""
        soup = BeautifulSoup(html, "html.parser")
        series_path = urlparse(series_url).path.rstrip("/")
        prefix = f"{series_path}/chapter/"
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            absolute = urljoin(series_url, str(anchor.get("href", "")))
            path = urlparse(absolute).path
            if path.startswith(prefix) and "/" not in path[len(prefix):]:
                if path not in seen:
                    seen.add(path)
                    urls.append(f"{_origin(series_url)}{path}")
        if urls:
            return urls
        payload = AsuraScansAdapter._continue_reading_data(html)
        if payload is None:
            return []
        base = str(payload.get("base", ""))
        numbers = payload.get("numbers")
        if not _SERIES_PATH_RE.fullmatch(base) or not isinstance(numbers, list):
            return []
        return [f"{_origin(series_url)}{base}/chapter/{number}" for number in numbers]

    @staticmethod
    def _continue_reading_data(html: str) -> dict[str, object] | None:
        match = re.search(
            r'<script[^>]*id="continue-reading-data"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if match is None:
            return None
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.warning(
                "continue-reading-data json is malformed site=asurascans "
                "selector=#continue-reading-data"
            )
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _page_ref(url: str) -> PageRef:
        path = urlparse(url).path.rstrip("/")
        chapter = path.rsplit("/", 1)[-1] if _CHAPTER_PATH_RE.fullmatch(path) else None
        return PageRef(url=url, site="asurascans", chapter=chapter)


def _is_own(root: Tag, node: Tag) -> bool:
    """True when ``node`` is not inside a nested comment (replies own their media)."""
    owner = node.find_parent(id=_COMMENT_ID_RE)
    return owner is None or owner is root


register(AsuraScansAdapter())
