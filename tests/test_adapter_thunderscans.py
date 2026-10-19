"""ThunderScans adapter tests — offline fixture HTML only, no network (AGENTS.md §9).

Proves the §6 media rule on the server-rendered Manga Comments System markup:
comment attachments (plain, figure-wrapped, GIF, lazy-loaded, relative) are
collected, while avatars, comic panels, ads, logos, related-post covers, and
the new-comment form are never comments and never media.
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
from backend.scraper.adapters.thunderscans import ThunderScansAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CHAPTER_URL = "https://en-thunderscans.com/1482765166-leveling-up-by-killing-gods-chapter-65/"
SERIES_URL = "https://en-thunderscans.com/comics/leveling-up-by-killing-gods/"
CATALOG_URL = "https://en-thunderscans.com/comics/"
PAGE_URL = CHAPTER_URL

#: The only collectable media in thunderscans_chapter.html (comment attachments).
EXPECTED_MEDIA = {
    "19546": {
        "https://en-thunderscans.com/wp-content/uploads/2026/10/cap-doubt.jpg",
    },
    "20001": {
        "https://en-thunderscans.com/wp-content/uploads/2026/10/cat-dancing.gif",
        # relative src resolved against page_url:
        "https://en-thunderscans.com/wp-content/uploads/2026/10/rel-meme.png",
        # lazy-loaded via data-src:
        "https://en-thunderscans.com/wp-content/uploads/2026/10/lazy-sticker.png",
    },
    "20002": {
        "https://en-thunderscans.com/wp-content/uploads/2026/10/reply-sticker.png",
    },
    "20003": set(),
}

EXPECTED_AUTHORS = {
    "19546": "ItsCuBeX",
    "20001": "BobTheBinger",
    "20002": "Carol",
    "20003": "Dave",
}

#: Page chrome that must NEVER surface as media (PRD §6).
CHROME_MEDIA = {
    "https://en-thunderscans.com/wp-content/uploads/logo.png",
    "https://en-thunderscans.com/wp-content/uploads/ad-banner.jpg",
    "https://en-thunderscans.com/wp-content/uploads/2026/09/lukg-65-p1.jpg",
    "https://en-thunderscans.com/wp-content/uploads/2026/09/lukg-65-p2.jpg",
    "https://en-thunderscans.com/wp-content/uploads/2026/09/lukg-65-p3.jpg",
    "https://en-thunderscans.com/wp-content/uploads/sticky-ad.png",
    "https://secure.gravatar.com/avatar/aaa111?s=48&d=retro",
    "https://secure.gravatar.com/avatar/bbb222?s=48&d=retro",
    "https://secure.gravatar.com/avatar/ccc333?s=48&d=retro",
    "https://secure.gravatar.com/avatar/ddd444?s=48&d=retro",
    "https://en-thunderscans.com/wp-content/uploads/2026/09/related-1.jpg",
    "https://en-thunderscans.com/wp-content/uploads/2026/09/related-2.jpg",
    "https://en-thunderscans.com/wp-content/uploads/footer-logo.png",
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
    comments = adapter.find_comments(_load("thunderscans_chapter.html"))
    for comment in comments:
        comment.meta.page_url = PAGE_URL
    return comments


class TestRegistry:
    def test_registered_and_flags(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        assert isinstance(adapter, ThunderScansAdapter)
        assert adapter.site == "thunderscans"
        # mcs comments are server-rendered: no browser required (PRD §8).
        assert adapter.render is False
        assert "thunderscans" in list_sites()


class TestCanHandle:
    @pytest.mark.parametrize(
        "url",
        [
            CHAPTER_URL,
            SERIES_URL,
            CATALOG_URL,
            "http://en-thunderscans.com/comics/shadow-slave/",
            "https://www.en-thunderscans.com/comics/",
        ],
    )
    def test_accepts(self, url: str) -> None:
        assert get_adapter(CHAPTER_URL).can_handle(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://asurascans.com/comics/x/chapter/1",
            "https://evil-en-thunderscans.com/comics/",
            "https://en-thunderscans.com.evil.test/comics/",
            "ftp://en-thunderscans.com/comics/",
            "not-a-url",
        ],
    )
    def test_rejects(self, url: str) -> None:
        assert get_adapter(CHAPTER_URL).can_handle(url) is False


class TestFindComments:
    def test_comment_ids_and_authors(self) -> None:
        comments = _backfill_page_url(get_adapter(CHAPTER_URL))
        assert [c.meta.comment_id for c in comments] == ["19546", "20001", "20002", "20003"]
        assert {c.meta.comment_id: c.meta.author_name for c in comments} == EXPECTED_AUTHORS

    def test_comment_text(self) -> None:
        comments = _backfill_page_url(get_adapter(CHAPTER_URL))
        by_id = {c.meta.comment_id: c for c in comments}
        assert "neg diffed the god of destruction" in by_id["19546"].meta.text
        assert by_id["20003"].meta.text == "first"

    def test_form_and_chrome_never_comments(self) -> None:
        comments = _backfill_page_url(get_adapter(CHAPTER_URL))
        texts = " ".join(c.meta.text or "" for c in comments)
        assert "Post Comment" not in texts
        assert "Join the discussion" not in texts
        # The composer textarea/submit carry mcs- prefixes but have no data-comment-id.
        assert all(c.meta.comment_id.isdigit() for c in comments)


class TestFindCommentMedia:
    def test_media_per_comment(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        for comment in comments:
            urls = {ref.url for ref in adapter.find_comment_media(comment)}
            assert urls == EXPECTED_MEDIA[comment.meta.comment_id], comment.meta.comment_id

    def test_chrome_never_media(self) -> None:
        """PRD §6 / AGENTS.md §6: only comment attachments; never panels/avatars/ads."""
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        collected = {ref.url for c in comments for ref in adapter.find_comment_media(c)}
        assert collected & CHROME_MEDIA == set()
        # sanity: the fixture really does contain that chrome
        assert len(CHROME_MEDIA) >= 10

    def test_kinds(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        kinds = {
            ref.url: ref.kind
            for c in comments
            for ref in adapter.find_comment_media(c)
        }
        assert kinds["https://en-thunderscans.com/wp-content/uploads/2026/10/cat-dancing.gif"] == "gif"
        assert kinds["https://en-thunderscans.com/wp-content/uploads/2026/10/cap-doubt.jpg"] == "image"

    def test_relative_urls_resolved(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        urls = {ref.url for c in comments for ref in adapter.find_comment_media(c)}
        assert "https://en-thunderscans.com/wp-content/uploads/2026/10/rel-meme.png" in urls
        assert not any(u.startswith("/") for u in urls)

    def test_metadata_carried(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        comments = _backfill_page_url(adapter)  # type: ignore[arg-type]
        ref = adapter.find_comment_media(comments[0])[0]
        assert ref.comment.comment_id == "19546"
        assert ref.comment.author_name == "ItsCuBeX"
        assert ref.comment.page_url == PAGE_URL
        assert ref.original_filename == "cap-doubt.jpg"

    def test_no_match_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = ThunderScansAdapter()
        with caplog.at_level(logging.WARNING):
            assert adapter.find_comments("<html><body><p>no comments here</p></body></html>") == []
        assert any("site=thunderscans" in record.message for record in caplog.records)


class TestDiscoverPages:
    def test_current_page(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        scope = CrawlScope(ScopeKind.CURRENT_PAGE)
        refs = adapter.discover_pages(SERIES_URL, scope)
        assert [r.url for r in refs] == [SERIES_URL]
        assert refs[0].site == "thunderscans"
        assert refs[0].chapter is None

    def test_current_chapter_from_chapter_url(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        scope = CrawlScope(ScopeKind.CURRENT_CHAPTER)
        refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [CHAPTER_URL]
        assert refs[0].chapter == "65"

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
        adapter = ThunderScansAdapter(
            fetch_html=_fixture_fetch({SERIES_URL: _load("thunderscans_series.html")})
        )
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [
            "https://en-thunderscans.com/1482765166-leveling-up-by-killing-gods-chapter-65/",
            "https://en-thunderscans.com/1482765166-leveling-up-by-killing-gods-chapter-64/",
            "https://en-thunderscans.com/1482765166-leveling-up-by-killing-gods-chapter-63/",
            "https://en-thunderscans.com/1482765166-leveling-up-by-killing-gods-chapter-62/",
        ]
        assert [r.chapter for r in refs] == ["65", "64", "63", "62"]

    def test_entire_comic_from_series_url(self) -> None:
        adapter = ThunderScansAdapter(
            fetch_html=_fixture_fetch({SERIES_URL: _load("thunderscans_series.html")})
        )
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        refs = adapter.discover_pages(SERIES_URL, scope)
        assert len(refs) == 4
        assert refs[0].chapter == "65"

    def test_entire_comic_fetch_failure_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = ThunderScansAdapter(fetch_html=_fixture_fetch({}))
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [CHAPTER_URL]

    def test_entire_comic_from_unrelated_url(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = ThunderScansAdapter(fetch_html=_fixture_fetch({}))
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CATALOG_URL, scope)
        assert [r.url for r in refs] == [CATALOG_URL]
        assert any("needs a series or chapter url" in record.message for record in caplog.records)

    def test_entire_comic_no_chapters_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = ThunderScansAdapter(
            fetch_html=_fixture_fetch({SERIES_URL: "<html><body>empty</body></html>"})
        )
        scope = CrawlScope(ScopeKind.ENTIRE_COMIC)
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [CHAPTER_URL]


class TestDiscoverSeries:
    def test_catalog_page(self) -> None:
        adapter = ThunderScansAdapter(fetch_html=_fixture_fetch({CATALOG_URL: _load("thunderscans_catalog.html")}))
        series = adapter.discover_series(CATALOG_URL)
        assert [s.url for s in series] == [
            "https://en-thunderscans.com/comics/shadow-slave/",
            "https://en-thunderscans.com/comics/leveling-up-by-killing-gods/",
            "https://en-thunderscans.com/comics/demonic-emperor/",
            "https://en-thunderscans.com/comics/when-the-system-opens-after-the-age-of-100/",
        ]
        # /comics/feed/ is the RSS feed, not a series.
        assert all(not s.url.endswith("/feed/") for s in series)

    def test_catalog_pagination_walks_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("backend.scraper.adapters.thunderscans._CATALOG_PAGE_DELAY_SECONDS", 0)
        page2 = """
        <html><body><div class="bxsite"><div class="bs">
          <div class="bsx"><a href="https://en-thunderscans.com/comics/new-series/">
            <span class="tt">New Series</span></a></div>
          <div class="bsx"><a href="https://en-thunderscans.com/comics/shadow-slave/">
            <span class="tt">Shadow Slave</span></a></div>
        </div></div></body></html>
        """

        def fetch(url: str) -> str:
            if url.endswith("/comics/?page=2"):
                return page2
            if "/comics/?page=" in url:
                raise httpx.ConnectError(f"no fixture for {url}")
            return _load("thunderscans_catalog.html")

        adapter = ThunderScansAdapter(fetch_html=fetch)
        series = adapter.discover_series(CATALOG_URL)
        urls = [s.url for s in series]
        assert "https://en-thunderscans.com/comics/new-series/" in urls
        # deduped across pages
        assert urls.count("https://en-thunderscans.com/comics/shadow-slave/") == 1

    def test_non_catalog_url_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = ThunderScansAdapter(fetch_html=_fixture_fetch({}))
        with caplog.at_level(logging.WARNING):
            assert adapter.discover_series(CHAPTER_URL) == []
        assert any("catalog index" in record.message for record in caplog.records)

    def test_fetch_failure_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = ThunderScansAdapter(fetch_html=_fixture_fetch({}))
        with caplog.at_level(logging.WARNING):
            assert adapter.discover_series(CATALOG_URL) == []
        assert any("catalog fetch failed" in record.message for record in caplog.records)
