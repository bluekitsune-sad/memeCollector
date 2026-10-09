"""Site adapter contract — the single interface every site module implements (AGENTS.md §5, PRD §7).

Contract decisions (the rest of the collector codes against these signatures):

* **Page input is HTML text.** ``SiteAdapter.find_comments`` receives the raw HTML
  string of one page and parses it internally (BeautifulSoup). This keeps adapters
  testable with local fixture HTML and free of any browser/HTTP dependency — the
  crawler owns fetching (``backend.scraper.crawler``), adapters own interpretation.
* **``Comment.element`` is the parsed ``bs4.Tag``** returned by the adapter's own
  parse of that HTML; ``find_comment_media`` and ``get_comment_metadata`` operate
  on the ``Comment`` value the adapter produced.
* **The crawler backfills provenance.** ``find_comments`` only sees HTML, so it
  may leave ``CommentMeta.page_url`` / ``chapter`` / ``page_number`` empty; after
  ``find_comments`` returns, the crawler fills any empty fields from the
  ``PageRef`` being scanned *before* calling ``find_comment_media`` (so adapters
  may rely on ``comment.meta.page_url`` for resolving relative attachment URLs).
* **No network inside the crawler hot path.** ``discover_pages`` must be cheap
  URL-derived discovery where possible; the crawler runs it in a worker thread
  (``asyncio.to_thread``) so a synchronous lookup inside an adapter never blocks
  the event loop.
* **Registry:** site modules call :func:`register` at import time.
  ``backend/scraper/adapters/__init__.py`` imports every site module from one
  line each in its ``_SITE_MODULES`` tuple — adding a site is one line there
  plus the new module.

Unknown URLs must raise :class:`UnsupportedSiteError`, which the API surfaces as
a clear "site not supported" message (PRD §0, AGENTS.md §5).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Literal
from urllib.parse import urlparse

from bs4 import Tag

from backend.security.text import sanitize_optional_text, sanitize_text

logger = logging.getLogger(__name__)

#: What kind of comment attachment a :class:`MediaRef` points at (PRD §5.3, §44).
MediaKind = Literal["image", "gif", "video"]


class ScopeKind(str, Enum):
    """What to scan relative to the entered URL (PRD §5.1)."""

    CURRENT_PAGE = "current_page"
    CURRENT_CHAPTER = "current_chapter"
    MULTIPLE_CHAPTERS = "multiple_chapters"
    ENTIRE_COMIC = "entire_comic"
    CUSTOM_URLS = "custom_urls"


@dataclass(frozen=True)
class CrawlScope:
    """Scope selection passed to :meth:`SiteAdapter.discover_pages`.

    ``urls`` is the explicit URL list for :attr:`ScopeKind.CUSTOM_URLS` and the
    selected chapter entry URLs for :attr:`ScopeKind.MULTIPLE_CHAPTERS`;
    it is ignored for the other kinds.
    """

    kind: ScopeKind
    urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageRef:
    """One scannable page (a comic page URL) discovered for a crawl."""

    url: str
    site: str
    chapter: str | None = None
    page_number: int | None = None


@dataclass(frozen=True)
class SeriesRef:
    """One comic/series entry on a site's catalog index (site-wide backfill).

    Produced by :meth:`SiteAdapter.discover_series` from a catalog URL such as
    ``https://asurascans.com/comics``; ``title`` may be ``None`` when the index
    does not expose one (the backfill then shows the URL).
    """

    url: str
    title: str | None = None


@dataclass
class CommentMeta:
    """Provenance for one comment (PRD §31). Mutable: the crawler backfills
    ``page_url``/``chapter``/``page_number`` when the adapter leaves them empty.

    Storage choke point for untrusted text: ``comment_id``, ``author_name`` and
    ``text`` are sanitized on construction (AGENTS.md §9), so every adapter
    returning ``comment.meta`` hands the crawler/DB clean values only.
    """

    comment_id: str
    author_name: str | None = None
    page_url: str = ""
    chapter: str | None = None
    page_number: int | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        self.comment_id = sanitize_text(self.comment_id)
        self.author_name = sanitize_optional_text(self.author_name)
        self.text = sanitize_optional_text(self.text)


@dataclass
class Comment:
    """A discovered comment: its metadata plus the parsed element it came from."""

    meta: CommentMeta
    element: Tag


@dataclass(frozen=True)
class MediaRef:
    """One comment attachment to collect — the output unit of the COLLECT stage."""

    url: str
    kind: MediaKind
    comment: CommentMeta
    original_filename: str | None = None


class UnsupportedSiteError(Exception):
    """No registered adapter can handle the URL (AGENTS.md §5); message names the hostname."""

    def __init__(self, url: str) -> None:
        self.url = url
        hostname = urlparse(url).hostname or url
        super().__init__(
            f"Site not supported: add an adapter for {hostname} "
            f"(no registered adapter handles {url})"
        )


class SiteAdapter(ABC):
    """Per-site implementation of the five-step adapter contract (AGENTS.md §5).

    Subclasses must set the :attr:`site` class attribute (registry key and
    ``crawl_history.site`` value) and may set :attr:`render` to request
    browser-rendered pages instead of the plain httpx fetch.
    """

    #: Registry key / site name recorded in crawl history and logs.
    site: str

    #: Fetch strategy chosen by the crawler: ``False`` = httpx + BeautifulSoup
    #: (static HTML), ``True`` = Playwright Chromium render (PRD §8).
    render: bool = False

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return True when this adapter owns ``url``."""

    @abstractmethod
    def discover_pages(self, url: str, scope: CrawlScope) -> list[PageRef]:
        """Expand ``url`` + ``scope`` into the list of pages to scan (capped by the crawler)."""

    @abstractmethod
    def find_comments(self, page: str) -> list[Comment]:
        """Parse one page's HTML and return its comments (page chrome is never a comment)."""

    @abstractmethod
    def find_comment_media(self, comment: Comment) -> list[MediaRef]:
        """Return the media attached to ``comment`` — never avatars, panels, logos, or ads (PRD §6)."""

    @abstractmethod
    def get_comment_metadata(self, comment: Comment) -> CommentMeta:
        """Return the metadata for ``comment``."""

    def discover_series(self, url: str) -> list[SeriesRef]:
        """Expand a catalog-index URL into its comic series pages (site-wide backfill).

        Optional step of the contract (AGENTS.md §5): sites whose index cannot
        be enumerated keep the default empty result and simply cannot be the
        target of a backfill. Like ``discover_pages`` this may perform a fetch
        and runs inside a worker thread (``asyncio.to_thread``) — obey the
        configured crawl delay (AGENTS.md §9).
        """
        return []


# ---------------------------------------------------------------------------
# Registry (AGENTS.md §5): resolves a URL to exactly one adapter.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, SiteAdapter] = {}


def register(adapter: SiteAdapter) -> None:
    """Register ``adapter`` under its ``site`` key; called at site-module import time."""
    site = getattr(adapter, "site", "")
    if not site:
        raise ValueError("adapter class must define a non-empty 'site' attribute")
    if site in _REGISTRY:
        raise ValueError(f"duplicate adapter registration for site={site!r}")
    _REGISTRY[site] = adapter
    logger.debug("adapter registered site=%s", site)


def get_adapter(url: str) -> SiteAdapter:
    """Return the adapter that can handle ``url``; raise :class:`UnsupportedSiteError` if none."""
    for adapter in list(_REGISTRY.values()):
        try:
            if adapter.can_handle(url):
                return adapter
        except Exception:
            # A broken can_handle must not poison resolution of other sites.
            logger.exception("adapter.can_handle raised site=%s url=%s", adapter.site, url)
    raise UnsupportedSiteError(url)


def list_sites() -> list[str]:
    """Sorted site keys of every registered adapter."""
    return sorted(_REGISTRY)
