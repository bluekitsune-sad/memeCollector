"""HiveToons adapter tests — offline fixture HTML only, no network (AGENTS.md §9).

HiveToons runs the same white-label platform as vortexscans.org (its build
config carries ``SITE_SLUG: "vortexscans"``), so the shared parsing/discovery
logic is proven once in ``test_adapter_vortexscans.py``; this file proves the
per-site registration, host routing, and §6 media rule on HiveToons' own
fixture markup.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from backend.scraper.adapters import CrawlScope, ScopeKind, get_adapter, list_sites
from backend.scraper.adapters.hivetoons import HiveToonsAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CHAPTER_URL = "https://hivetoons.org/series/the-emperors-assistant/chapter-12"
SERIES_URL = "https://hivetoons.org/series/the-emperors-assistant"
CATALOG_URL = "https://hivetoons.org/series"
PAGE_URL = CHAPTER_URL

#: The only collectable media in hivetoons_chapter.html (inside ``.comment-body``).
EXPECTED_MEDIA = {
    "91001": {"https://storage.hivetoons.org/comments/hive-meme.png"},
}

#: Page chrome that must NEVER surface as media (PRD §6).
CHROME_MEDIA = {
    "https://hivetoons.org/theme/logo/hive-full.svg",
    "https://hivetoons.org/theme/logo/hive-footer.svg",
    "https://storage.hivetoons.org/banners/hive-banner.jpg",
    "https://storage.hivetoons.org/episodes/emperors-assistant-12-p1.jpg",
    "https://hivetoons.org/theme/achievements/hive-mind.webp",
    "https://hivetoons.org/theme/emotes/webp/like.webp",
}


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fixture_fetch(mapping: dict[str, str]):
    def fetch(url: str) -> str:
        html = mapping.get(url)
        if html is None:
            raise httpx.ConnectError(f"no fixture for {url}")
        return html

    return fetch


def _comments(adapter: HiveToonsAdapter) -> list:
    comments = adapter.find_comments(_load("hivetoons_chapter.html"))
    for comment in comments:
        comment.meta.page_url = PAGE_URL
    return comments


class TestRegistry:
    def test_registered_and_flags(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        assert isinstance(adapter, HiveToonsAdapter)
        assert adapter.site == "hivetoons"
        assert adapter.render is True
        assert adapter.scroll_before_capture is True
        # platform-wide drawer trigger, verified live 2026-10-19 ("Comments 0" opened the container).
        assert adapter.click_before_capture == 'button[aria-label^="Comments"]'
        assert "hivetoons" in list_sites()


class TestCanHandle:
    @pytest.mark.parametrize(
        "url",
        [
            CHAPTER_URL,
            SERIES_URL,
            CATALOG_URL,
            "http://www.hivetoons.org/series/foo/chapter-1",
        ],
    )
    def test_accepts(self, url: str) -> None:
        assert HiveToonsAdapter().can_handle(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://vortexscans.org/series/foo/chapter-1",
            "https://example.com/series/foo",
            "hivetoons.org/series/foo",
            "https://hivetoons.org.evil.example/series/foo",
            "ftp://hivetoons.org/series/foo",
        ],
    )
    def test_rejects(self, url: str) -> None:
        assert HiveToonsAdapter().can_handle(url) is False


class TestCommentsAndMedia:
    def test_comment_parsed(self) -> None:
        comments = _comments(HiveToonsAdapter())
        assert [c.meta.comment_id for c in comments] == ["91001"]
        assert comments[0].meta.author_name == "hive_fan22"
        assert "chef's kiss chapter" in (comments[0].meta.text or "")

    def test_media_per_comment(self) -> None:
        adapter = HiveToonsAdapter()
        for comment in _comments(adapter):
            urls = {ref.url for ref in adapter.find_comment_media(comment)}
            assert urls == EXPECTED_MEDIA[comment.meta.comment_id]

    def test_chrome_never_media(self) -> None:
        html = _load("hivetoons_chapter.html")
        assert all(url in html or url.replace("https://hivetoons.org", "") in html
                   for url in CHROME_MEDIA)
        adapter = HiveToonsAdapter()
        collected = {ref.url for c in _comments(adapter) for ref in adapter.find_comment_media(c)}
        assert collected.isdisjoint(CHROME_MEDIA)
        assert collected == {"https://storage.hivetoons.org/comments/hive-meme.png"}

    def test_composer_never_comment(self) -> None:
        comments = _comments(HiveToonsAdapter())
        texts = " ".join((c.meta.text or "") for c in comments)
        assert "Join the discussion" not in texts


class TestDiscovery:
    def test_current_chapter(self) -> None:
        refs = HiveToonsAdapter().discover_pages(CHAPTER_URL, CrawlScope(kind=ScopeKind.CURRENT_CHAPTER))
        assert [r.url for r in refs] == [CHAPTER_URL]
        assert refs[0].chapter == "12"

    def test_entire_comic_collects_series_chapters(self) -> None:
        series_html = """
        <html><body>
          <a href="/series/the-emperors-assistant/chapter-13">Chapter 13</a>
          <a href="/series/the-emperors-assistant/chapter-12">Chapter 12</a>
          <a href="/series/another-story/chapter-1">Another story</a>
          <a href="/series/another-story">Sibling series</a>
        </body></html>
        """
        adapter = HiveToonsAdapter(fetch_html=_fixture_fetch({SERIES_URL: series_html}))
        refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(kind=ScopeKind.ENTIRE_COMIC))
        assert [r.url for r in refs] == [
            f"{SERIES_URL}/chapter-13",
            f"{SERIES_URL}/chapter-12",
        ]

    def test_catalog_pagination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("backend.scraper.adapters.vortexscans._CATALOG_PAGE_DELAY_SECONDS", 0)
        page1 = """
        <html><body>
          <a href="/series/hard-carry-supporter">Hard Carry Supporter</a>
          <a href="/series/the-villains-profiler">The Villain's Profiler</a>
          <a href="/series?page=2">2</a>
        </body></html>
        """
        page2 = """
        <html><body>
          <a href="/series/new-hive-series">New Hive Series</a>
          <a href="/series/hard-carry-supporter">Dup</a>
        </body></html>
        """

        def fetch(url: str) -> str:
            if url.endswith("/series?page=3"):
                raise httpx.ConnectError(f"no fixture for {url}")
            if url.endswith("/series?page=2"):
                return page2
            return page1

        urls = [s.url for s in HiveToonsAdapter(fetch_html=fetch).discover_series(CATALOG_URL)]
        assert "https://hivetoons.org/series/new-hive-series" in urls
        assert urls.count("https://hivetoons.org/series/hard-carry-supporter") == 1
        assert all("?" not in u for u in urls)

    def test_non_catalog_url_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = HiveToonsAdapter(fetch_html=_fixture_fetch({}))
        with caplog.at_level(logging.WARNING):
            assert adapter.discover_series(CHAPTER_URL) == []
        assert any("catalog index" in record.message for record in caplog.records)
