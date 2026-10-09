"""Comix adapter for ``comix.to`` comment sections (AGENTS.md §5, PRD §0/§6).

Domain choice — why "comix" means comix.to (reasoning, verified 2026-10-08):

* The PRD/TODO say "comix domain as resolvable at build time". Candidates were
  checked: comick.io, globalcomix, comiconlinefree (no comment sections),
  comixship/.org/.net (NXDOMAIN), comix.im (pre-launch "Comixx" landing page
  without a reader). **comix.to** is a live comic reader literally branded
  "Comix" whose manga/reader pages carry an active comment widget (community
  threads discuss posting image links in Comix comments), and the PRD's wording
  fits a ``.to`` reader domain.
* Direct access is blocked from this network (local DNS/SNI reset; public DNS
  via dns.google resolves it), so evidence was gathered from Wayback snapshots
  of the production bundles and pages — all selector claims below come from
  that source and are marked accordingly.

Selector provenance (production JS fetched via Wayback):

* **Comment items:** the widget renders ``<li class="cm-item …" id="cm-{id}">``
  with ``div.cm-item__main`` → ``a.cm-item__avatar`` (avatar — never media),
  ``div.cm-item__detail`` → ``div.cm-item__header`` → ``span.cm-item__user > a``
  (author link ``/u/{hashId}``), ``div.cm-item__content.content-rendered``
  (``dangerouslySetInnerHTML`` of ``contentHtml`` — attachments live here) and
  ``span.cm-item__time``. Replies render as nested ``ul.cm-item__replies > li.cm-item``.
* **Not comments:** the ``ul.comments > li.comment`` "Latest Comments" sidebar
  strips images from ``contentHtml`` (bundle function ``Un`` replaces ``<img>``
  with ``[img]``) and its ``comment__poster`` thumbnails are page chrome — it is
  deliberately outside the selector.
* **URLs:** title pages ``/title/{hid}-{slug}``, reader pages
  ``/title/{hid}-{slug}/{chapterId}-chapter-{n}`` (verified from Wayback CDX),
  comment deep-links ``?cmid=``/``?cm_id=``.
* **Comments auto-load** on manga and read pages (``autoLoadComments ?? true``)
  — hence ``render = True`` (a static fetch only yields the Vue shell).

ENTIRE_COMIC discovery (documented, injectable, offline-testable): the chapter
list is client-fetched from ``GET /api/v1/manga/{hid}/chapters?page=&limit=20
&order[number]=desc`` (endpoint verified via Wayback; the client contract
``{items: [{url, id, number, …}], meta}`` is verified from the production
bundle). Archived API captures return an obfuscated ``{"e": …}`` envelope, so a
response without ``items`` logs a warning and discovery degrades to the verified
``#initial-data`` ``firstChapterUrl``/``latestChapterUrl`` fields plus any
server-rendered chapter links, then finally to the entry URL.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any
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
from backend.security.fetch import guarded_get
from backend.security.urls import UnsafeURLError

logger = logging.getLogger(__name__)

#: Hosts this adapter owns.
_SITE_HOSTS = frozenset({"comix.to", "www.comix.to"})

_TITLE_PATH_RE = re.compile(r"^/title/[^/]+$")
_CHAPTER_PATH_RE = re.compile(r"^/title/[^/]+/[^/]+$")

#: Widget items carry ``id="cm-{id}"`` (numeric ids verified via ``cmid=`` deep-links).
_COMMENT_ID_RE = re.compile(r"^cm-(\w+)$")

_KIND_BY_SUFFIX: dict[str, MediaKind] = {".gif": "gif", ".mp4": "video", ".webm": "video"}

_MEDIA_SUFFIXES: tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".mp4", ".webm", ".mov",
)
_BLOCKED_CLASS_MARKERS: tuple[str, ...] = ("emoji", "emote", "smilie", "avatar", "icon")

_LAZY_SRC_ATTRIBUTES: tuple[str, ...] = ("data-src", "data-original", "srcset", "src")

#: The site's observed chapter-list page size (Wayback captures of the endpoint).
_CHAPTER_PAGE_LIMIT = 20
#: Safety cap on chapter-list pagination.
_MAX_CHAPTER_PAGES = 100

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


def _default_fetch(url: str) -> str:
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


def _default_fetch_json(url: str) -> Any:
    response = guarded_get(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        timeout=_FETCH_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _widget_owner(node: Tag) -> Tag | None:
    """Nearest ancestor comment item (replies nest inside their parent)."""
    for parent in node.parents:
        if parent.name == "li" and "cm-item" in (parent.get("class") or []):
            return parent
    return None


def _is_own(root: Tag, node: Tag) -> bool:
    owner = _widget_owner(node)
    return owner is None or owner is root


class ComixAdapter(SiteAdapter):
    """Collect comment attachments from comix.to manga/reader pages (render = True)."""

    site = "comix"

    #: Vue SPA: comments hydrate client-side after the shell loads (PRD §8).
    render = True

    def __init__(
        self,
        *,
        fetch_html: Callable[[str], str] | None = None,
        fetch_json: Callable[[str], Any] | None = None,
    ) -> None:
        self._fetch_html = fetch_html if fetch_html is not None else _default_fetch
        self._fetch_json = fetch_json if fetch_json is not None else _default_fetch_json

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
                        "skipping url handled by no adapter site=comix url=%s", given
                    )
            return refs
        if scope.kind is ScopeKind.CURRENT_PAGE:
            return [self._page_ref(url)]
        if scope.kind is ScopeKind.CURRENT_CHAPTER:
            if _CHAPTER_PATH_RE.fullmatch(urlparse(url).path.rstrip("/")):
                return [self._page_ref(url)]
            logger.warning(
                "current-chapter scope entered from a non-chapter url site=comix url=%s "
                "— scanning the entry page instead (enter a reader url for chapter-level "
                "scans)",
                url,
            )
            return [self._page_ref(url)]
        return self._discover_entire_comic(url)

    def find_comments(self, page: str) -> list[Comment]:
        soup = BeautifulSoup(page, "html.parser")
        nodes = soup.select("li.cm-item")
        if not nodes:
            logger.warning(
                "comment selectors matched nothing site=comix selector=li.cm-item "
                "page_chars=%d (comments hydrate client-side — is render=True enabled?)",
                len(page),
            )
            return []
        comments: list[Comment] = []
        for node in nodes:
            match = _COMMENT_ID_RE.match(str(node.get("id", "")))
            if match is None:
                logger.warning(
                    "comment item has no usable id site=comix id=%r", node.get("id")
                )
                continue
            comments.append(
                Comment(
                    meta=CommentMeta(
                        comment_id=match.group(1),
                        author_name=self._author(node),
                        text=self._text(node),
                    ),
                    element=node,
                )
            )
        return comments

    def find_comment_media(self, comment: Comment) -> list[MediaRef]:
        body = self._own_content(comment.element)
        if body is None:
            logger.warning(
                "comment content selector matched nothing site=comix comment=%s "
                "selector=div.cm-item__content",
                comment.meta.comment_id,
            )
            return []
        refs: dict[str, MediaRef] = {}
        for node in body.select("img, video, source"):
            if _blocked_media(node):
                continue
            candidate = _candidate_src(node)
            if not candidate:
                continue
            self._store(refs, comment, urljoin(comment.meta.page_url, candidate))
        for anchor in body.select("a[href]"):
            href = str(anchor.get("href", ""))
            if not href or href.startswith("data:") or not _looks_like_media_url(href):
                continue
            self._store(refs, comment, urljoin(comment.meta.page_url, href))
        if not refs:
            logger.debug(
                "comment attachment selectors matched nothing site=comix comment=%s "
                "selectors=div.cm-item__content img/video/source, a[href]",
                comment.meta.comment_id,
            )
        return list(refs.values())

    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        return comment.meta

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _own_content(root: Tag) -> Tag | None:
        for body in root.select("div.cm-item__content"):
            if _is_own(root, body):
                return body
        return None

    @staticmethod
    def _author(root: Tag) -> str | None:
        for link in root.select('a[href^="/u/"]'):
            if not _is_own(root, link):
                continue
            label = link.get_text(" ", strip=True)
            if label:
                return label
        return None

    @staticmethod
    def _text(root: Tag) -> str | None:
        body = ComixAdapter._own_content(root)
        if body is None:
            logger.debug(
                "comment body selector matched nothing site=comix id=%r "
                "selector=div.cm-item__content",
                root.get("id"),
            )
            return None
        return body.get_text(" ", strip=True)

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
                "entire-comic scope needs a title or reader url site=comix url=%s "
                "— scanning the entry page instead",
                url,
            )
            return [entry_ref]
        title_url = f"{_origin(url)}{title_path}"
        chapter_paths = self._chapter_paths_from_api(title_path)
        if not chapter_paths:
            chapter_paths = self._chapter_paths_from_html(title_url, title_path)
        if not chapter_paths:
            logger.warning(
                "chapter discovery matched nothing site=comix url=%s selectors="
                "api items[].url, a[href], #initial-data firstChapterUrl/latestChapterUrl "
                "— scanning the entry page instead",
                title_url,
            )
            return [entry_ref]
        return [
            self._page_ref(f"{_origin(title_url)}{path}") for path in chapter_paths
        ]

    @staticmethod
    def _title_path(url: str) -> str | None:
        path = urlparse(url).path.rstrip("/")
        if _CHAPTER_PATH_RE.fullmatch(path):
            return path.rsplit("/", 1)[0]
        if _TITLE_PATH_RE.fullmatch(path):
            return path
        return None

    def _chapter_paths_from_api(self, title_path: str) -> list[str]:
        """Chapter paths from the verified client endpoint (documented in the docstring)."""
        manga_hid = title_path.rsplit("/", 1)[-1].split("-", 1)[0]
        prefix = f"{title_path}/"
        paths: list[str] = []
        seen: set[str] = set()
        for page in range(1, _MAX_CHAPTER_PAGES + 1):
            endpoint = (
                f"https://comix.to/api/v1/manga/{manga_hid}/chapters"
                f"?page={page}&limit={_CHAPTER_PAGE_LIMIT}&order%5Bnumber%5D=desc"
            )
            try:
                payload = self._fetch_json(endpoint)
            except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
                logger.warning(
                    "chapter-list request failed site=comix manga=%s page=%d error=%s",
                    manga_hid,
                    page,
                    exc,
                )
                break
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                logger.warning(
                    "chapter-list response had no items array site=comix manga=%s "
                    "page=%d (obfuscated/anti-bot envelope? falling back to page data)",
                    manga_hid,
                    page,
                )
                break
            for item in items:
                item_path = item.get("url") if isinstance(item, dict) else None
                if not isinstance(item_path, str):
                    continue
                path = urlparse(item_path).path
                if path.startswith(prefix) and "/" not in path[len(prefix):] and path not in seen:
                    seen.add(path)
                    paths.append(path)
            if len(items) < _CHAPTER_PAGE_LIMIT:
                break
        return paths

    def _chapter_paths_from_html(self, title_url: str, title_path: str) -> list[str]:
        """Server-rendered chapter links plus the verified ``#initial-data`` URLs."""
        try:
            html = self._fetch_html(title_url)
        except (httpx.HTTPError, OSError, UnsafeURLError):
            html = None
        paths: list[str] = []
        seen: set[str] = set()
        prefix = f"{title_path}/"
        if html is not None:
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.select("a[href]"):
                path = urlparse(urljoin(title_url, str(anchor.get("href", "")))).path
                if path.startswith(prefix) and "/" not in path[len(prefix):] and path not in seen:
                    seen.add(path)
                    paths.append(path)
            payload = ComixAdapter._initial_data(html)
            for chapter_url in ComixAdapter._initial_chapter_urls(payload):
                path = urlparse(chapter_url).path
                if path.startswith(prefix) and "/" not in path[len(prefix):] and path not in seen:
                    seen.add(path)
                    paths.append(path)
        return paths

    @staticmethod
    def _initial_data(html: str) -> dict[str, Any] | None:
        match = re.search(
            r'<script[^>]*id="initial-data"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if match is None:
            return None
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.warning(
                "initial-data json is malformed site=comix selector=#initial-data"
            )
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _initial_chapter_urls(payload: dict[str, Any] | None) -> list[str]:
        """``firstChapterUrl`` / ``latestChapterUrl`` from the manga detail query."""
        if payload is None:
            return []
        queries = payload.get("queries")
        if not isinstance(queries, dict):
            return []
        urls: list[str] = []
        for query in queries.values():
            if not isinstance(query, dict):
                continue
            for key in ("firstChapterUrl", "latestChapterUrl"):
                value = query.get(key)
                if isinstance(value, str) and value.startswith("/title/"):
                    urls.append(value)
        return urls

    @staticmethod
    def _page_ref(url: str) -> PageRef:
        path = urlparse(url).path.rstrip("/")
        chapter = path.rsplit("/", 1)[-1] if _CHAPTER_PATH_RE.fullmatch(path) else None
        return PageRef(url=url, site="comix", chapter=chapter)


register(ComixAdapter())
