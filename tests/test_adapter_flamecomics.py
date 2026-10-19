"""FlameComics adapter tests — offline fixture HTML only, no network (AGENTS.md §9).

Proves the §6 media rule on the client-rendered Mantine comment module *and* the
deploy-independence rule: the fixture uses CSS-module build hash ``zZ9k2A`` while
production was ``iXHq0G`` at verification time — the adapter matches only the
stable ``Comment-module__`` prefix and ``__item``/``__commentContent`` suffixes,
so comments are still found across deploys. Avatars, reader panels, rating
emotes, related covers, the sort bar, and the login overlay are never comments
and never media.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from backend.scraper.adapters import (
    CrawlScope,
    ScopeKind,
    SiteAdapter,
    get_adapter,
    list_sites,
)
from backend.scraper.adapters.flamecomics import FlameComicsAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CHAPTER_URL = "https://flamecomics.xyz/series/1/3efdb83fccbc577a"
SERIES_URL = "https://flamecomics.xyz/series/1"
BROWSE_URL = "https://flamecomics.xyz/browse"
PAGE_URL = CHAPTER_URL

#: The only collectable media in flamecomics_chapter.html (comment attachments).
EXPECTED_MEDIA = {
    "a1b2c3d4": {
        "https://cdn.flamecomics.xyz/comments/epic-reaction.png",
        # relative src resolved against page_url:
        "https://flamecomics.xyz/uploads/comments/local-meme.webp",
        "https://i.imgur.com/laugh-track.gif",
    },
    "e5f6a7b8": set(),
}

EXPECTED_AUTHORS = {
    "a1b2c3d4": "Memelord",
    "e5f6a7b8": "ReaderTwo",
}

#: Page chrome that must NEVER surface as media (PRD §6).
CHROME_MEDIA = {
    "https://cdn.flamecomics.xyz/uploads/images/site/logo.svg",
    "https://cdn.flamecomics.xyz/uploads/images/chapter/1/3efdb/panel-01.jpg",
    "https://cdn.flamecomics.xyz/uploads/images/chapter/1/3efdb/panel-02.jpg",
    "https://cdn.flamecomics.xyz/uploads/images/chapter/1/3efdb/panel-03.jpg",
    "https://cdn.flamecomics.xyz/uploads/images/emotes/happy.webp",
    "https://cdn.flamecomics.xyz/uploads/images/emotes/surprise.webp",
    "https://cdn.flamecomics.xyz/uploads/images/emotes/love.webp",
    "https://cdn.flamecomics.xyz/uploads/images/avatars/memelord.png",
    "https://cdn.flamecomics.xyz/uploads/images/series/2/cover.png",
    "https://cdn.flamecomics.xyz/uploads/images/series/24/cover.png",
}


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fixture_fetch(mapping: dict[str, str]):
    """Injected fetch_html: fixture per URL, ConnectError otherwise (no network)."""

    def fetch(url: str) -> str:
        html = mapping.get(url)
        if html is None:
            raise httpx.ConnectError(f"no fixture for {url}")
        return html

    return fetch


def _backfill_page_url(adapter: SiteAdapter) -> list:
    """Mirror the crawler: backfill meta.page_url into each comment (base.py)."""
    comments = adapter.find_comments(_load("flamecomics_chapter.html"))
    for comment in comments:
        comment.meta.page_url = PAGE_URL
    return comments


class TestRegistry:
    def test_registered_and_flags(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        assert isinstance(adapter, FlameComicsAdapter)
        assert adapter.site == "flamecomics"
        # comment threads hydrate client-side (PRD §8).
        assert adapter.render is True
        assert "flamecomics" in list_sites()


class TestCanHandle:
    @pytest.mark.parametrize(
        "url",
        [
            CHAPTER_URL,
            SERIES_URL,
            BROWSE_URL,
            "http://flamecomics.xyz/series/2",
            "https://www.flamecomics.xyz/browse",
        ],
    )
    def test_accepts(self, url: str) -> None:
        assert get_adapter(CHAPTER_URL).can_handle(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://asurascans.com/comics/x/chapter/1",
            "https://evil-flamecomics.xyz/browse",
            "https://flamecomics.xyz.evil.test/browse",
            "ftp://flamecomics.xyz/browse",
            "not-a-url",
        ],
    )
    def test_rejects(self, url: str) -> None:
        assert get_adapter(CHAPTER_URL).can_handle(url) is False


class TestFindComments:
    def test_comment_ids_and_authors(self) -> None:
        comments = _backfill_page_url(get_adapter(CHAPTER_URL))
        assert [c.meta.comment_id for c in comments] == ["a1b2c3d4", "e5f6a7b8"]
        assert {c.meta.comment_id: c.meta.author_name for c in comments} == EXPECTED_AUTHORS

    def test_comment_text(self) -> None:
        comments = _backfill_page_url(get_adapter(CHAPTER_URL))
        by_id = {c.meta.comment_id: c for c in comments}
        assert "absolutely peak" in by_id["a1b2c3d4"].meta.text
        assert by_id["e5f6a7b8"].meta.text == "nah mid tbh"

    def test_hash_independence(self) -> None:
        """Fixture hash ``zZ9k2A`` differs from production ``iXHq0G`` — still parsed."""
        fixture = _load("flamecomics_chapter.html")
        assert "Comment-module__zZ9k2A__item" in fixture
        assert "iXHq0G" not in fixture
        comments = get_adapter(CHAPTER_URL).find_comments(fixture)
        assert len(comments) == 2

    def test_sort_bar_overlay_and_toolbar_never_comments(self) -> None:
        comments = _backfill_page_url(get_adapter(CHAPTER_URL))
        texts = " ".join(c.meta.text or "" for c in comments)
        for chrome_text in ("Best", "Recent", "Oldest", "Login to Comment", "Show 3 replies"):
            assert chrome_text not in texts


class TestFindCommentMedia:
    def test_media_per_comment(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        for comment in comments:
            urls = {ref.url for ref in adapter.find_comment_media(comment)}
            assert urls == EXPECTED_MEDIA[comment.meta.comment_id], comment.meta.comment_id

    def test_chrome_never_media(self) -> None:
        """PRD §6 / AGENTS.md §6: only comment attachments; never panels/avatars/emotes."""
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        collected = {ref.url for c in comments for ref in adapter.find_comment_media(c)}
        assert collected & CHROME_MEDIA == set()

    def test_kinds(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        kinds = {
            ref.url: ref.kind
            for c in comments
            for ref in adapter.find_comment_media(c)
        }
        assert kinds["https://i.imgur.com/laugh-track.gif"] == "gif"
        assert kinds["https://cdn.flamecomics.xyz/comments/epic-reaction.png"] == "image"

    def test_relative_urls_resolved(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        urls = {ref.url for c in comments for ref in adapter.find_comment_media(c)}
        assert "https://flamecomics.xyz/uploads/comments/local-meme.webp" in urls
        assert not any(u.startswith("/") for u in urls)

    def test_metadata_carried(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        ref = adapter.find_comment_media(comments[0])[0]
        assert ref.comment.comment_id == "a1b2c3d4"
        assert ref.comment.author_name == "Memelord"
        assert ref.comment.page_url == PAGE_URL
        assert ref.original_filename == "epic-reaction.png"

    def test_no_match_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = FlameComicsAdapter()
        with caplog.at_level(logging.WARNING):
            assert adapter.find_comments("<html><body><p>no comments here</p></body></html>") == []
        assert any("site=flamecomics" in record.message for record in caplog.records)


class TestDiscoverPages:
    def test_current_page(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        scope = CrawlScope(ScopeKind.CURRENT_PAGE)
        refs = adapter.discover_pages(SERIES_URL, scope)
        assert [r.url for r in refs] == [SERIES_URL]
        assert refs[0].site == "flamecomics"
        assert refs[0].chapter is None

    def test_current_chapter_from_chapter_url(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        scope = CrawlScope(ScopeKind.CURRENT_CHAPTER)
        refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [CHAPTER_URL]
        assert refs[0].chapter == "3efdb83fccbc577a"

    def test_current_chapter_from_series_url_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = get_adapter(CHAPTER_URL)
        scope = CrawlScope(ScopeKind.CURRENT_CHAPTER)
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(SERIES_URL, scope)
        assert [r.url for r in refs] == [SERIES_URL]
        assert any("non-chapter url" in record.message for record in caplog.records)

    def test_custom_urls_skip_unsupported(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = get_adapter(CHAPTER_URL)
        scope = CrawlScope(
            ScopeKind.CUSTOM_URLS,
            urls=(CHAPTER_URL, "https://asurascans.com/x/chapter/1"),
        )
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [CHAPTER_URL]

    def test_entire_comic_from_chapter_url(self) -> None:
        adapter = FlameComicsAdapter(
            fetch_html=_fixture_fetch({SERIES_URL: _load("flamecomics_series.html")})
        )
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [
            "https://flamecomics.xyz/series/1/3efdb83fccbc577a",
            "https://flamecomics.xyz/series/1/385fe46707bd150c",
            "https://flamecomics.xyz/series/1/748c8a28ffbb7d94",
        ]

    def test_entire_comic_from_series_url(self) -> None:
        adapter = FlameComicsAdapter(
            fetch_html=_fixture_fetch({SERIES_URL: _load("flamecomics_series.html")})
        )
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        refs = adapter.discover_pages(SERIES_URL, scope)
        assert len(refs) == 3

    def test_entire_comic_fetch_failure_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = FlameComicsAdapter(fetch_html=_fixture_fetch({}))
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [CHAPTER_URL]

    def test_entire_comic_no_chapters_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = FlameComicsAdapter(
            fetch_html=_fixture_fetch({SERIES_URL: "<html><body>empty</body></html>"})
        )
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [CHAPTER_URL]


class TestDiscoverSeries:
    def test_browse_catalog(self) -> None:
        adapter = FlameComicsAdapter(fetch_html=_fixture_fetch({BROWSE_URL: _load("flamecomics_browse.html")}))
        series = adapter.discover_series(BROWSE_URL)
        assert [s.url for s in series] == [
            "https://flamecomics.xyz/series/1",
            "https://flamecomics.xyz/series/2",
            "https://flamecomics.xyz/series/24",
            "https://flamecomics.xyz/series/134",
        ]
        assert [s.title for s in series][0] == "Solo Leveling"
        # profile links are not series
        assert all("/profile/" not in s.url for s in series)

    def test_non_catalog_url_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = FlameComicsAdapter(fetch_html=_fixture_fetch({}))
        with caplog.at_level(logging.WARNING):
            assert adapter.discover_series(CHAPTER_URL) == []
        assert any("catalog index" in record.message for record in caplog.records)

    def test_fetch_failure_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = FlameComicsAdapter(fetch_html=_fixture_fetch({}))
        with caplog.at_level(logging.WARNING):
            assert adapter.discover_series(BROWSE_URL) == []
        assert any("catalog fetch failed" in record.message for record in caplog.records)
