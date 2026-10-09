"""MangaDex adapter for ``mangadex.org`` and its comment home ``forums.mangadex.org``.

Design note — where the comments live (verified 2026-10-08):

* ``mangadex.org`` is a pure SPA (``<div id="app">`` + ``/assets/index-*.js``);
  its chapter/title pages contain **no comment DOM**. Per the official docs
  (``api.mangadex.org/docs/01-concepts/comments/``), comment counts are exposed
  through the statistics API and point at XenForo threads on
  ``forums.mangadex.org`` — e.g. ``GET /statistics/chapter/{id}`` →
  ``statistics.{id}.comments = {threadId, repliesCount}`` and
  ``GET /statistics/manga/{id}`` for the title-level thread (both verified live).
* A chapter's comment section **is** that forum thread, so discovery expands
  mangadex.org entry URLs into the corresponding
  ``https://forums.mangadex.org/threads/{threadId}/`` page (injectable JSON
  fetcher, documented per scope); forum URLs are used verbatim. When a lookup
  fails the adapter logs and falls back to the entry URL unchanged.
* Forum pages are server-rendered XenForo — ``render = False``. Verified on a
  live thread: ``article.message--post[id=js-post-{postId}][data-author]`` wraps
  ``article.message-body`` → ``div.message-userContent`` → ``div.bbWrapper``;
  avatars (``img.avatar-*``), logos and nav live outside ``.message-body``;
  smilies/reacties are ``data:`` sprites with ``smilie``/``reaction`` classes and
  chapter-link cards point at ``og.mangadex.org`` — all excluded by rule.
* ENTIRE_COMIC: ``GET /manga/{id}/feed`` (paginated, English chapters) then one
  statistics call per chapter to resolve its thread; thread ids are deduped.
  Per-chapter statistics are required — batch ``ids[]=`` queries are rejected by
  the API (verified: HTTP 400 validation_exception).

Unverified / defensive: individual attachment markup follows standard XenForo
(linked image under ``.bbWrapper``); fixtures model it and any deviation logs a
clear warning instead of collecting page chrome.
"""

from __future__ import annotations

import logging
import re
import time
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

logger = logging.getLogger(__name__)

_SITE_HOSTS = frozenset({"mangadex.org", "www.mangadex.org", "forums.mangadex.org"})

_API_BASE = "https://api.mangadex.org"
_FORUMS_BASE = "https://forums.mangadex.org"

#: Entry URL shapes on mangadex.org (UUID segments).
_TITLE_PATH_RE = re.compile(r"^/title/([0-9a-fA-F-]{36})$")
_CHAPTER_PATH_RE = re.compile(r"^/chapter/([0-9a-fA-F-]{36})(?:/[^/]+)?$")
_POST_ID_RE = re.compile(r"^js-post-(\d+)$")

#: URL suffix → media kind (PRD §5.3).
_KIND_BY_SUFFIX: dict[str, MediaKind] = {".gif": "gif", ".mp4": "video", ".webm": "video"}

_MEDIA_SUFFIXES: tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".mp4", ".webm", ".mov",
)

#: Never collect these: theme sprites, avatars, OpenGraph cards, favicons.
_BLOCKED_SRC_MARKERS: tuple[str, ...] = (
    "/styles/", "/community/avatars/", "og.mangadex.org", "/sprite", "favicon",
)
_BLOCKED_CLASS_MARKERS: tuple[str, ...] = ("smilie", "reaction", "avatar", "emoji")

_LAZY_SRC_ATTRIBUTES: tuple[str, ...] = ("data-src", "data-original", "srcset", "src")

#: Feed paging guard for very large series (500 chapters per page).
_MAX_FEED_PAGES = 40

_FETCH_TIMEOUT_SECONDS = 30.0
_REQUEST_GAP_SECONDS = 0.2  # crawl-rate politeness between API calls (AGENTS.md §9).
_RETRY_DELAY_SECONDS = 2.0
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
    path = urlparse(url).path.lower()
    if any(path.endswith(suffix) for suffix in _MEDIA_SUFFIXES):
        return True
    return "/attachments/" in path


def _blocked_media(node: Tag, value: str) -> bool:
    classes = " ".join(node.get("class") or [])
    if any(marker in classes for marker in _BLOCKED_CLASS_MARKERS):
        return True
    return any(marker in value for marker in _BLOCKED_SRC_MARKERS)


def _get(url: str) -> httpx.Response:
    # Guarded GET: every hop is validated before the request is issued (PRD §41).
    return guarded_get(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        timeout=_FETCH_TIMEOUT_SECONDS,
    )


def _default_fetch_json(url: str) -> Any:
    """Live API fetch with 429 backoff and a small inter-request gap (AGENTS.md §9)."""
    response = _get(url)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        try:
            time.sleep(float(retry_after))
        except ValueError:
            time.sleep(_RETRY_DELAY_SECONDS)
        response = _get(url)
    response.raise_for_status()
    time.sleep(_REQUEST_GAP_SECONDS)
    return response.json()


class MangaDexAdapter(SiteAdapter):
    """Resolve mangadex.org entries to their forums.mangadex.org comment threads."""

    site = "mangadex"

    #: Forum threads are plain server-rendered HTML (PRD §8).
    render = False

    def __init__(self, *, fetch_json: Callable[[str], Any] | None = None) -> None:
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
                if not self.can_handle(given):
                    logger.warning(
                        "skipping url handled by no adapter site=mangadex url=%s", given
                    )
                    continue
                if scope.kind is ScopeKind.CUSTOM_URLS:
                    refs.append(self._page_ref(given))
                else:
                    refs.extend(self._entry_refs(given))
            return refs
        if scope.kind is ScopeKind.ENTIRE_COMIC:
            return self._discover_entire_comic(url)
        return self._entry_refs(url)

    def find_comments(self, page: str) -> list[Comment]:
        soup = BeautifulSoup(page, "html.parser")
        articles = soup.select("article.message--post")
        if not articles:
            logger.warning(
                "comment selectors matched nothing site=mangadex "
                "selector=article.message--post page_chars=%d (expected a "
                "forums.mangadex.org XenForo thread)",
                len(page),
            )
            return []
        comments: list[Comment] = []
        for article in articles:
            comment_id = self._post_id(article)
            if comment_id is None:
                logger.warning(
                    "comment element has no post id site=mangadex selector=article.message--post"
                )
                continue
            body = article.select_one(".message-body .bbWrapper")
            meta = CommentMeta(
                comment_id=comment_id,
                author_name=article.get("data-author"),
                text=body.get_text(" ", strip=True) if body is not None else None,
            )
            comments.append(Comment(meta=meta, element=article))
        return comments

    def find_comment_media(self, comment: Comment) -> list[MediaRef]:
        body = comment.element.select_one(".message-body")
        if body is None:
            logger.debug(
                "message body selector matched nothing site=mangadex comment=%s "
                "selector=.message-body",
                comment.meta.comment_id,
            )
            return []
        refs: dict[str, MediaRef] = {}
        self._add(refs, comment, body, "img, video, source", attribute="src")
        self._add_links(refs, comment, body)
        if not refs:
            logger.debug(
                "message attachment selectors matched nothing site=mangadex comment=%s "
                "selectors=img/video/source, a[href]",
                comment.meta.comment_id,
            )
        return list(refs.values())

    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        return comment.meta

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _post_id(article: Tag) -> str | None:
        match = _POST_ID_RE.match(str(article.get("id", "")))
        if match:
            return match.group(1)
        data_content = str(article.get("data-content", ""))
        if data_content.startswith("post-") and data_content[5:].isdigit():
            return data_content[5:]
        return None

    @staticmethod
    def _add(
        refs: dict[str, MediaRef], comment: Comment, body: Tag, selector: str, *, attribute: str
    ) -> None:
        for node in body.select(selector):
            if attribute == "src":
                candidate = _candidate_src(node)
            else:
                value = node.get(attribute)
                candidate = value if isinstance(value, str) else None
            if not candidate or _blocked_media(node, candidate):
                continue
            MangaDexAdapter._store(refs, comment, urljoin(comment.meta.page_url, candidate))

    def _add_links(self, refs: dict[str, MediaRef], comment: Comment, body: Tag) -> None:
        for anchor in body.select("a[href]"):
            href = str(anchor.get("href", ""))
            if not href or href.startswith("data:") or not _looks_like_media_url(href):
                continue
            if _blocked_media(anchor, href):
                continue
            self._store(refs, comment, urljoin(comment.meta.page_url, href))

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

    def _entry_refs(self, url: str) -> list[PageRef]:
        """mangadex.org entry URL → its comment thread; forum URLs verbatim."""
        parsed = urlparse(url)
        if (parsed.hostname or "").lower().startswith("forums."):
            return [self._page_ref(url)]
        path = parsed.path.rstrip("/")
        chapter_match = _CHAPTER_PATH_RE.fullmatch(path)
        if chapter_match:
            thread_id = self._thread_for_chapter(chapter_match.group(1))
            if thread_id is not None:
                return [self._thread_ref(thread_id, chapter=chapter_match.group(1))]
            logger.warning(
                "falling back to entry page site=mangadex url=%s reason=no comment thread",
                url,
            )
            return [self._page_ref(url)]
        title_match = _TITLE_PATH_RE.fullmatch(path)
        if title_match:
            thread_id = self._thread_for_manga(title_match.group(1))
            if thread_id is not None:
                return [self._thread_ref(thread_id)]
            logger.warning(
                "falling back to entry page site=mangadex url=%s reason=no comment thread",
                url,
            )
            return [self._page_ref(url)]
        logger.warning(
            "unrecognized mangadex entry path site=mangadex url=%s — scanning it as-is",
            url,
        )
        return [self._page_ref(url)]

    def _discover_entire_comic(self, url: str) -> list[PageRef]:
        entry_ref = self._page_ref(url)
        manga_id = self._manga_id_for(url)
        if manga_id is None:
            logger.warning(
                "entire-comic scope could not resolve a manga id site=mangadex url=%s "
                "— scanning the entry page instead",
                url,
            )
            return [entry_ref]
        chapters = self._feed_chapters(manga_id)
        if not chapters:
            logger.warning(
                "chapter feed matched nothing site=mangadex manga=%s", manga_id
            )
            return [entry_ref]
        refs: list[PageRef] = []
        seen_threads: set[int] = set()
        for chapter_id, chapter_number in chapters:
            thread_id = self._thread_for_chapter(chapter_id)
            if thread_id is None or thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)
            refs.append(self._thread_ref(thread_id, chapter=chapter_number))
        if not refs:
            logger.warning(
                "no comment threads resolved for any chapter site=mangadex manga=%s",
                manga_id,
            )
            return [entry_ref]
        return refs

    def _manga_id_for(self, url: str) -> str | None:
        path = urlparse(url).path.rstrip("/")
        title_match = _TITLE_PATH_RE.fullmatch(path)
        if title_match:
            return title_match.group(1)
        chapter_match = _CHAPTER_PATH_RE.fullmatch(path)
        if chapter_match is None:
            return None
        chapter_id = chapter_match.group(1)
        try:
            payload = self._fetch_json(f"{_API_BASE}/chapter/{chapter_id}?includes[]=manga")
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            logger.warning(
                "chapter lookup failed site=mangadex chapter=%s error=%s", chapter_id, exc
            )
            return None
        return self._related_manga_id(payload)

    @staticmethod
    def _related_manga_id(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        for relationship in data.get("relationships") or []:
            if isinstance(relationship, dict) and relationship.get("type") == "manga":
                manga_id = relationship.get("id")
                return manga_id if isinstance(manga_id, str) else None
        return None

    def _feed_chapters(self, manga_id: str) -> list[tuple[str, str | None]]:
        """``(chapter_id, chapter_number)`` for English chapters, in ascending order."""
        chapters: list[tuple[str, str | None]] = []
        next_url: str | None = (
            f"{_API_BASE}/manga/{manga_id}/feed"
            "?limit=500&order[chapter]=asc&translatedLanguage[]=en"
        )
        pages = 0
        while next_url and pages < _MAX_FEED_PAGES:
            try:
                payload = self._fetch_json(next_url)
            except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
                logger.warning(
                    "chapter feed request failed site=mangadex manga=%s error=%s",
                    manga_id,
                    exc,
                )
                break
            if not isinstance(payload, dict):
                logger.warning(
                    "chapter feed response was not a json object site=mangadex manga=%s",
                    manga_id,
                )
                break
            for item in payload.get("data") or []:
                if not isinstance(item, dict) or item.get("type") != "chapter":
                    continue
                chapter_id = item.get("id")
                if not isinstance(chapter_id, str):
                    continue
                attributes = item.get("attributes")
                number = attributes.get("chapter") if isinstance(attributes, dict) else None
                chapters.append((chapter_id, number if isinstance(number, str) else None))
            next_url = payload.get("next") if isinstance(payload.get("next"), str) else None
            pages += 1
        return chapters

    def _thread_for_chapter(self, chapter_id: str) -> int | None:
        return self._thread_id(
            f"{_API_BASE}/statistics/chapter/{chapter_id}", chapter_id, "chapter"
        )

    def _thread_for_manga(self, manga_id: str) -> int | None:
        return self._thread_id(
            f"{_API_BASE}/statistics/manga/{manga_id}", manga_id, "manga"
        )

    def _thread_id(self, endpoint: str, entity_id: str, entity_kind: str) -> int | None:
        try:
            payload = self._fetch_json(endpoint)
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            logger.warning(
                "statistics lookup failed site=mangadex %s=%s error=%s",
                entity_kind,
                entity_id,
                exc,
            )
            return None
        statistics = payload.get("statistics") if isinstance(payload, dict) else None
        entry = statistics.get(entity_id) if isinstance(statistics, dict) else None
        comments = entry.get("comments") if isinstance(entry, dict) else None
        thread_id = comments.get("threadId") if isinstance(comments, dict) else None
        if not isinstance(thread_id, int):
            logger.warning(
                "statistics returned no comment thread site=mangadex %s=%s",
                entity_kind,
                entity_id,
            )
            return None
        return thread_id

    @staticmethod
    def _thread_ref(thread_id: int, chapter: str | None = None) -> PageRef:
        return PageRef(
            url=f"{_FORUMS_BASE}/threads/{thread_id}/",
            site="mangadex",
            chapter=chapter,
        )

    @staticmethod
    def _page_ref(url: str) -> PageRef:
        path = urlparse(url).path.rstrip("/")
        chapter_match = _CHAPTER_PATH_RE.fullmatch(path)
        chapter = chapter_match.group(1) if chapter_match else None
        return PageRef(url=url, site="mangadex", chapter=chapter)


register(MangaDexAdapter())
