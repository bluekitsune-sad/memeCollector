"""MangaPark adapter for ``mangapark.net`` comment sections (AGENTS.md §5, PRD §0/§6).

Selector provenance — verified 2026-10-08 against live production assets
(fetched via the Wayback Machine; direct access is blocked from this network):

* **Comment root:** the site is a Qwik SSR app whose comment bodies render
  client-side. The production chunk that builds each item
  (``mangapark.net/build/q-BfVzpuTk.js``) emits
  ``<div id="comment-{id}" data-name="comment-item" class="relative space-y-3 …">``
  with nested replies inside their parent (deleted items render the same id
  with ``class="p-2 bg-base-200"`` and the text "Deleted"). The ``data-name``
  attribute is the primary selector; the ``comment-{id}`` shape is a fallback
  for older renders.
* **Author:** header renders ``a.link.link-hover.link-primary`` pointing at
  ``/u/{userId}`` with the display name as text; the avatar anchor wraps only an
  ``img`` and yields no text.
* **Body:** the content component wraps the rendered comment markdown in
  ``div.my-2``; avatars live in the header outside that wrapper, so scoping
  attachment search to it enforces the §6 rule structurally.
* **URLs (verified from Wayback snapshots):** title pages are
  ``/title/{id}[-en-{slug}]`` and chapter pages are
  ``/title/{id}[-en-{slug}]/{chapterId}-…`` (e.g. ``…/9628255-vol-0-ch-214``);
  title pages server-render the chapter list as plain ``<a href>`` links, which
  powers ENTIRE_COMIC discovery.

Unverified / defensive: comments appear inside title/chapter pages after JS
hydration (``render = True``); if the content wrapper or comment markers are
missing the adapter logs a clear warning instead of scraping page chrome.
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
    SiteAdapter,
    register,
)

logger = logging.getLogger(__name__)

#: Hosts this adapter owns — the ``.io``/``.to`` mirrors are deliberately not
#: claimed: they could not be verified from this network (see module docstring).
_SITE_HOSTS = frozenset({"mangapark.net", "www.mangapark.net"})

_TITLE_PATH_RE = re.compile(r"^/title/[^/]+$")
_CHAPTER_PATH_RE = re.compile(r"^/title/[^/]+/[^/]+$")

#: ``id="comment-{hex-id|digits}"`` (hex object ids verified on live snapshots).
_COMMENT_ID_RE = re.compile(r"^comment-([0-9a-fA-F]{16,32}|\d{1,12})$")

#: Primary selector (verified) plus the id-shape fallback for older renders.
_COMMENT_SELECTOR = 'div[data-name="comment-item"], div[id^="comment-"]'

_KIND_BY_SUFFIX: dict[str, MediaKind] = {".gif": "gif", ".mp4": "video", ".webm": "video"}

_MEDIA_SUFFIXES: tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".mp4", ".webm", ".mov",
)
_BLOCKED_CLASS_MARKERS: tuple[str, ...] = ("emoji", "emote", "smilie", "avatar")

_LAZY_SRC_ATTRIBUTES: tuple[str, ...] = ("data-src", "data-original", "data-lazy-src", "srcset", "src")

_FETCH_TIMEOUT_SECONDS = 30.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _classify_kind(url: str) -> MediaKind:
    path = urlparse(url).path.lower()
    for suffix, kind in _KIND_BY_SUFFIX.items():
        if path.endswith(suffix):
            return kind
    return "image"


def _original_filename(url: str) -> str | None:
    return urlparse(url).path.rsplit("/", 1)[-1] or None


def _candidate_src(node: Tag) -> str | None:
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


def _looks_like_media_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_MEDIA_SUFFIXES)


def _blocked_media(node: Tag) -> bool:
    classes = " ".join(node.get("class") or [])
    return any(marker in classes for marker in _BLOCKED_CLASS_MARKERS)


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _default_fetch_html(url: str) -> str:
    """Live fetch used by discovery (runs inside ``asyncio.to_thread``)."""
    response = httpx.get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def _comment_owner(node: Tag) -> Tag | None:
    """Nearest ancestor comment element (replies nest inside their parent)."""
    for parent in node.parents:
        if parent.name == "div" and (
            parent.get("data-name") == "comment-item"
            or _COMMENT_ID_RE.match(str(parent.get("id", "")))
        ):
            return parent
    return None


def _is_own(root: Tag, node: Tag) -> bool:
    owner = _comment_owner(node)
    return owner is None or owner is root


def _is_comment_node(node: Tag) -> bool:
    if node.get("data-name") == "comment-item":
        return True
    return bool(_COMMENT_ID_RE.match(str(node.get("id", ""))))


class MangaParkAdapter(SiteAdapter):
    """Collect comment attachments from MangaPark title/chapter pages (render = True)."""

    site = "mangapark"

    #: Qwik hydrates comments client-side (verified: SSR ``<main>`` is empty).
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
                        "skipping url handled by no adapter site=mangapark url=%s", given
                    )
            return refs
        if scope.kind is ScopeKind.CURRENT_PAGE:
            return [self._page_ref(url)]
        if scope.kind is ScopeKind.CURRENT_CHAPTER:
            if _CHAPTER_PATH_RE.fullmatch(urlparse(url).path.rstrip("/")):
                return [self._page_ref(url)]
            logger.warning(
                "current-chapter scope entered from a non-chapter url site=mangapark "
                "url=%s — scanning the entry page instead (enter a chapter url for "
                "chapter-level scans)",
                url,
            )
            return [self._page_ref(url)]
        return self._discover_entire_comic(url)

    def find_comments(self, page: str) -> list[Comment]:
        soup = BeautifulSoup(page, "html.parser")
        nodes = [node for node in soup.select(_COMMENT_SELECTOR) if _is_comment_node(node)]
        if not nodes:
            logger.warning(
                "comment selectors matched nothing site=mangapark "
                "selector=div[data-name=comment-item], div[id^=comment-] page_chars=%d "
                "(comments are client-hydrated — is render=True enabled?)",
                len(page),
            )
            return []
        comments: list[Comment] = []
        for node in nodes:
            comment_id = self._comment_id(node)
            if comment_id is None:
                continue
            comments.append(
                Comment(
                    meta=CommentMeta(
                        comment_id=comment_id,
                        author_name=self._author(node),
                        text=self._text(node),
                    ),
                    element=node,
                )
            )
        return comments

    def find_comment_media(self, comment: Comment) -> list[MediaRef]:
        wrappers = [w for w in comment.element.select("div.my-2") if _is_own(comment.element, w)]
        if not wrappers:
            logger.warning(
                "comment content wrapper matched nothing site=mangapark comment=%s "
                "selector=div.my-2",
                comment.meta.comment_id,
            )
            return []
        refs: dict[str, MediaRef] = {}
        for wrapper in wrappers:
            for node in wrapper.select("img, video, source"):
                if _blocked_media(node):
                    continue
                candidate = _candidate_src(node)
                if not candidate:
                    continue
                self._store(refs, comment, urljoin(comment.meta.page_url, candidate))
            for anchor in wrapper.select("a[href]"):
                href = str(anchor.get("href", ""))
                if not href or href.startswith("data:") or not _looks_like_media_url(href):
                    continue
                self._store(refs, comment, urljoin(comment.meta.page_url, href))
        if not refs:
            logger.debug(
                "comment attachment selectors matched nothing site=mangapark comment=%s "
                "selectors=div.my-2 img/video/source, div.my-2 a[href]",
                comment.meta.comment_id,
            )
        return list(refs.values())

    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        return comment.meta

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _comment_id(node: Tag) -> str | None:
        match = _COMMENT_ID_RE.match(str(node.get("id", "")))
        if match is None:
            logger.warning(
                "comment element has no usable id site=mangapark id=%r",
                node.get("id"),
            )
            return None
        return match.group(1)

    @staticmethod
    def _author(root: Tag) -> str | None:
        for link in root.select('a[href^="/u/"]'):
            if not _is_own(root, link):
                continue
            label = link.get_text(" ", strip=True)
            if label:
                return label
        for image in root.select('a[href^="/u/"] img[alt]'):
            if _is_own(root, image) and image.get("alt"):
                return str(image.get("alt"))
        return None

    @staticmethod
    def _text(root: Tag) -> str | None:
        for body in root.select("div.my-2"):
            if _is_own(root, body):
                return body.get_text(" ", strip=True)
        logger.debug(
            "comment body selector matched nothing site=mangapark comment=%s selector=div.my-2",
            str(root.get("id", "")),
        )
        return None

    @staticmethod
    def _store(refs: dict[str, MediaRef], comment: Comment, url: str) -> None:
        if url in refs:
            return
        refs[url] = MediaRef(
            url=url,
            kind=_classify_kind(url),
            comment=comment.meta,
            original_filename=_original_filename(url),
        )

    def _discover_entire_comic(self, url: str) -> list[PageRef]:
        entry_ref = self._page_ref(url)
        title_path = self._title_path(url)
        if title_path is None:
            logger.warning(
                "entire-comic scope needs a title or chapter url site=mangapark url=%s "
                "— scanning the entry page instead",
                url,
            )
            return [entry_ref]
        title_url = f"{_origin(url)}{title_path}"
        try:
            html = self._fetch_html(title_url)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning(
                "title page fetch failed site=mangapark url=%s error=%s", title_url, exc
            )
            return [entry_ref]
        chapter_urls = self._chapter_urls(html, title_url, title_path)
        if not chapter_urls:
            logger.warning(
                "chapter discovery matched nothing site=mangapark url=%s "
                "selector=a[href^=%s/] — scanning the entry page instead",
                title_url,
                title_path,
            )
            return [entry_ref]
        return [self._page_ref(chapter_url) for chapter_url in chapter_urls]

    @staticmethod
    def _title_path(url: str) -> str | None:
        path = urlparse(url).path.rstrip("/")
        if _CHAPTER_PATH_RE.fullmatch(path):
            return path.rsplit("/", 1)[0]
        if _TITLE_PATH_RE.fullmatch(path):
            return path
        return None

    @staticmethod
    def _chapter_urls(html: str, title_url: str, title_path: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        prefix = f"{title_path}/"
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            path = urlparse(urljoin(title_url, str(anchor.get("href", "")))).path
            if path.startswith(prefix) and "/" not in path[len(prefix):] and path not in seen:
                seen.add(path)
                urls.append(f"{_origin(title_url)}{path}")
        return urls

    @staticmethod
    def _page_ref(url: str) -> PageRef:
        path = urlparse(url).path.rstrip("/")
        chapter = path.rsplit("/", 1)[-1] if _CHAPTER_PATH_RE.fullmatch(path) else None
        return PageRef(url=url, site="mangapark", chapter=chapter)


register(MangaParkAdapter())
