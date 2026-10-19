"""VortexScans adapter tests — offline fixture HTML only, no network (AGENTS.md §9).

Proves the §6 media rule on the vortex white-label platform's client-rendered
comment markup: attachments inside ``.comment-body`` are collected (absolute,
relative, lazy), while reaction emotes (``/theme/emotes/…``), achievement
badges (``/theme/achievements/…``), avatars, reader panels, covers, logos,
and the logged-out composer are never comments and never media. Nested replies
own their own media — a reply's attachment is never attributed to its parent.
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
from backend.scraper.adapters.vortexscans import VortexScansAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CHAPTER_URL = "https://vortexscans.org/series/i-was-thrown-into-an-unknown-manga/chapter-5"
SERIES_URL = "https://vortexscans.org/series/i-was-thrown-into-an-unknown-manga"
CATALOG_URL = "https://vortexscans.org/series"
PAGE_URL = CHAPTER_URL

#: The only collectable media in vortexscans_chapter.html (attachments inside
#: ``.comment-body`` only).
EXPECTED_MEDIA = {
    "68395": set(),  # text-only: badge + like emote live OUTSIDE the body
    "73178": {
        "https://storage.vortexscans.org/comments/nested-meme.png",
    },
    "73334": {
        # absolute src in body:
        "https://cdn.vortexscans.org/attachments/epic-panel.png",
        # root-relative src resolved against page_url, classified as gif:
        "https://vortexscans.org/uploads/attachments/rel-meme.gif",
        # lazy-loaded via data-src (placeholder src is data: — never chosen):
        "https://cdn.vortexscans.org/attachments/lazy-sticker.webp",
    },
    "68360": set(),
}

EXPECTED_AUTHORS = {
    "68395": "racerz1",
    "73178": "Blossom Teli",
    "73334": "SaitamaTV",
    "68360": "silent_reader",
}

#: Page chrome that must NEVER surface as media (PRD §6) — verified present in
#: the fixture so an exclusion failure would actually be caught.
CHROME_MEDIA = {
    "https://vortexscans.org/theme/logo/vortex-full.svg",
    "https://vortexscans.org/theme/logo/vortex-footer.svg",
    "https://storage.vortexscans.org/banners/site-banner.jpg",
    "https://storage.vortexscans.org/covers/i-was-thrown-into-an-unknown-manga.jpg",
    "https://storage.vortexscans.org/covers/contract-killer-enrollment.jpg",
    "https://storage.vortexscans.org/covers/hard-carry-supporter.jpg",
    "https://storage.vortexscans.org/episodes/unknown-manga-5-p1.jpg",
    "https://storage.vortexscans.org/episodes/unknown-manga-5-p2.jpg",
    "https://vortexscans.org/theme/icons/prev-chapter.svg",
    "https://vortexscans.org/theme/icons/settings.svg",
    "https://vortexscans.org/theme/icons/next-chapter.svg",
    # comment-level chrome (inside the comment element, OUTSIDE the body):
    "https://vortexscans.org/theme/achievements/dragon-emblem.webp",
    "https://vortexscans.org/theme/achievements/streak-flame.webp",
    "https://vortexscans.org/theme/emotes/webp/like.webp",
    "https://storage.vortexscans.org/avatars/user-73334.png",
}

#: Chapters of the fixture series, in document order (newest first).
EXPECTED_CHAPTERS = [
    f"{SERIES_URL}/chapter-{n}" for n in (5, 4, 3, 2, 1)
]


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


def _backfill_page_url(adapter: SiteAdapter, html: str | None = None) -> list:
    """Mirror the crawler: backfill meta.page_url into each comment (base.py)."""
    comments = adapter.find_comments(html if html is not None else _load("vortexscans_chapter.html"))
    for comment in comments:
        comment.meta.page_url = PAGE_URL
    return comments


class TestRegistry:
    def test_registered_and_flags(self) -> None:
        adapter = get_adapter(CHAPTER_URL)
        assert isinstance(adapter, VortexScansAdapter)
        assert adapter.site == "vortexscans"
        # comments are client-rendered AND lazy-mount on scroll (verified live).
        assert adapter.render is True
        assert adapter.scroll_before_capture is True
        # ...and items only mount after the "Comments {n}" drawer button (live 2026-10-19).
        assert adapter.click_before_capture == 'button[aria-label^="Comments"]'
        assert "vortexscans" in list_sites()


class TestCanHandle:
    @pytest.mark.parametrize(
        "url",
        [
            CHAPTER_URL,
            SERIES_URL,
            CATALOG_URL,
            "http://www.vortexscans.org/series/foo/chapter-1",
            "https://WWW.VORTEXSCANS.ORG/series/foo",
            "ftp://vortexscans.org/series/foo",
        ],
    )
    def test_scheme_and_host(self, url: str) -> None:
        adapter = VortexScansAdapter()
        # ftp:// is a non-http scheme → rejected even on the right host.
        expected = url.startswith(("http://", "https://"))
        assert adapter.can_handle(url) is expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://hivetoons.org/series/foo/chapter-1",
            "https://example.com/series/foo",
            "vortexscans.org/series/foo",
            "https://vortexscans.org.evil.example/series/foo",
            "https://sub.vortexscans.org/series/foo",
        ],
    )
    def test_rejects(self, url: str) -> None:
        assert VortexScansAdapter().can_handle(url) is False


class TestFindComments:
    def test_comment_ids_and_authors(self) -> None:
        comments = _backfill_page_url(VortexScansAdapter())
        assert [c.meta.comment_id for c in comments] == ["68395", "73178", "73334", "68360"]
        assert {c.meta.comment_id: c.meta.author_name for c in comments} == EXPECTED_AUTHORS

    def test_nested_reply_is_its_own_comment(self) -> None:
        comments = _backfill_page_url(VortexScansAdapter())
        ids = [c.meta.comment_id for c in comments]
        assert "73178" in ids  # the nested reply parsed as a separate comment

    def test_comment_text(self) -> None:
        comments = _backfill_page_url(VortexScansAdapter())
        by_id = {c.meta.comment_id: (c.meta.text or "") for c in comments}
        assert "FUCK!! IT GOT AXED!!!" in by_id["68395"]
        assert "What does axed mean" in by_id["73178"]
        assert "the last panel was insane" in by_id["73334"]
        assert by_id["68360"] == "following"

    def test_composer_and_chrome_never_comments(self) -> None:
        html = _load("vortexscans_chapter.html")
        assert "Join the discussion" in html  # composer chrome is in the fixture
        comments = _backfill_page_url(VortexScansAdapter())
        texts = " ".join((c.meta.text or "") for c in comments)
        assert "Join the discussion" not in texts
        # sort controls / reaction chips / profile links never become comments
        assert [c.meta.comment_id for c in comments] == ["68395", "73178", "73334", "68360"]

    def test_empty_comment_id_skipped(self) -> None:
        html = '<div id="comments"><div data-comment-id=""><p>ghost</p></div></div>'
        assert VortexScansAdapter().find_comments(html) == []

    def test_no_match_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = VortexScansAdapter()
        with caplog.at_level(logging.WARNING):
            assert adapter.find_comments("<html><body><p>no comments here</p></body></html>") == []
        assert any("comment selectors matched nothing" in record.message for record in caplog.records)

    def test_missing_author_and_body_are_none(self) -> None:
        html = """
        <div id="comments">
          <div data-comment-id="1">
            <article><div class="prose comment-body"><p>hi</p></div></article>
          </div>
          <div data-comment-id="2"><article aria-label="ann"><span>no body</span></article></div>
        </div>
        """
        comments = VortexScansAdapter().find_comments(html)
        assert [c.meta.author_name for c in comments] == [None, "ann"]
        assert [c.meta.text for c in comments] == ["hi", None]

    def test_get_comment_metadata_roundtrip(self) -> None:
        adapter = VortexScansAdapter()
        comments = _backfill_page_url(adapter)
        assert adapter.get_comment_metadata(comments[0]) is comments[0].meta


class TestFindCommentMedia:
    def test_media_per_comment(self) -> None:
        comments = _backfill_page_url(VortexScansAdapter())
        adapter = VortexScansAdapter()
        for comment in comments:
            urls = {ref.url for ref in adapter.find_comment_media(comment)}
            assert urls == EXPECTED_MEDIA[comment.meta.comment_id], comment.meta.comment_id

    def test_parent_never_owns_nested_reply_attachment(self) -> None:
        """§6 ownership: reply media belongs to the reply, not its parent thread."""
        comments = _backfill_page_url(VortexScansAdapter())
        adapter = VortexScansAdapter()
        parent = next(c for c in comments if c.meta.comment_id == "68395")
        nested = next(c for c in comments if c.meta.comment_id == "73178")
        parent_urls = {ref.url for ref in adapter.find_comment_media(parent)}
        assert "https://storage.vortexscans.org/comments/nested-meme.png" not in parent_urls
        assert {ref.url for ref in adapter.find_comment_media(nested)} == {
            "https://storage.vortexscans.org/comments/nested-meme.png"
        }

    def test_chrome_never_media(self) -> None:
        """Every chrome URL is really in the fixture; none of it is ever collected."""
        html = _load("vortexscans_chapter.html")
        assert all(url.replace("https://vortexscans.org", "") in html or url in html
                   for url in CHROME_MEDIA)
        comments = _backfill_page_url(VortexScansAdapter())
        adapter = VortexScansAdapter()
        collected = {ref.url for c in comments for ref in adapter.find_comment_media(c)}
        assert collected.isdisjoint(CHROME_MEDIA)
        # ...and the collected set is exactly the attachments, nothing more.
        expected = {u for urls in EXPECTED_MEDIA.values() for u in urls}
        assert collected == expected

    def test_kinds(self) -> None:
        comments = _backfill_page_url(VortexScansAdapter())
        adapter = VortexScansAdapter()
        kinds = {
            ref.url: ref.kind for c in comments for ref in adapter.find_comment_media(c)
        }
        assert kinds["https://vortexscans.org/uploads/attachments/rel-meme.gif"] == "gif"
        assert kinds["https://cdn.vortexscans.org/attachments/epic-panel.png"] == "image"
        assert kinds["https://cdn.vortexscans.org/attachments/lazy-sticker.webp"] == "image"

    def test_relative_urls_resolved_against_page_url(self) -> None:
        comments = _backfill_page_url(VortexScansAdapter())
        adapter = VortexScansAdapter()
        collected = {ref.url for c in comments for ref in adapter.find_comment_media(c)}
        assert "https://vortexscans.org/uploads/attachments/rel-meme.gif" in collected
        assert not any(u.startswith("/") for u in collected)

    def test_metadata_carried(self) -> None:
        comments = _backfill_page_url(VortexScansAdapter())
        adapter = VortexScansAdapter()
        target = next(c for c in comments if c.meta.comment_id == "73334")
        refs = {ref.url: ref for ref in adapter.find_comment_media(target)}
        ref = refs["https://vortexscans.org/uploads/attachments/rel-meme.gif"]
        assert ref.kind == "gif"
        assert ref.comment.comment_id == "73334"
        assert ref.comment.author_name == "SaitamaTV"
        assert ref.comment.page_url == PAGE_URL
        assert ref.original_filename == "rel-meme.gif"

    def test_data_placeholder_src_skipped(self) -> None:
        html = """
        <div id="comments"><div data-comment-id="9">
          <article aria-label="a"><div class="comment-body">
            <img src="data:image/gif;base64,R0lGOD" alt=""/>
            <img data-src="https://cdn.vortexscans.org/real.webp" src="data:image/gif;base64,x" alt=""/>
          </div></article>
        </div></div>
        """
        comments = VortexScansAdapter().find_comments(html)
        for comment in comments:
            comment.meta.page_url = PAGE_URL
        adapter = VortexScansAdapter()
        urls = {ref.url for c in comments for ref in adapter.find_comment_media(c)}
        assert urls == {"https://cdn.vortexscans.org/real.webp"}

    def test_duplicate_src_deduped(self) -> None:
        html = """
        <div id="comments"><div data-comment-id="9">
          <article aria-label="a"><div class="comment-body">
            <img src="/uploads/a.png"/><img src="/uploads/a.png"/>
          </div></article>
        </div></div>
        """
        comments = VortexScansAdapter().find_comments(html)
        for comment in comments:
            comment.meta.page_url = PAGE_URL
        adapter = VortexScansAdapter()
        urls = [ref.url for c in comments for ref in adapter.find_comment_media(c)]
        assert urls == ["https://vortexscans.org/uploads/a.png"]


class TestDiscoverPages:
    def test_current_page(self) -> None:
        adapter = VortexScansAdapter()
        refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(kind=ScopeKind.CURRENT_PAGE))
        assert [r.url for r in refs] == [CHAPTER_URL]
        assert refs[0].site == "vortexscans"
        assert refs[0].chapter == "5"

    def test_current_chapter_from_chapter_url(self) -> None:
        refs = VortexScansAdapter().discover_pages(
            CHAPTER_URL, CrawlScope(kind=ScopeKind.CURRENT_CHAPTER)
        )
        assert [r.url for r in refs] == [CHAPTER_URL]
        assert refs[0].chapter == "5"

    def test_current_chapter_from_series_url_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = VortexScansAdapter()
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(SERIES_URL, CrawlScope(kind=ScopeKind.CURRENT_CHAPTER))
        assert [r.url for r in refs] == [SERIES_URL]
        assert any("non-chapter url" in record.message for record in caplog.records)

    def test_custom_urls_skip_unsupported(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = VortexScansAdapter()
        scope = CrawlScope(kind=ScopeKind.CUSTOM_URLS, urls=(CHAPTER_URL, "https://example.com/x"))
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CHAPTER_URL, scope)
        assert [r.url for r in refs] == [CHAPTER_URL]
        assert any("handled by no adapter" in record.message for record in caplog.records)

    def test_entire_comic_from_chapter_url(self) -> None:
        adapter = VortexScansAdapter(fetch_html=_fixture_fetch({SERIES_URL: _load("vortexscans_series.html")}))
        refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(kind=ScopeKind.ENTIRE_COMIC))
        assert [r.url for r in refs] == EXPECTED_CHAPTERS
        assert all(r.site == "vortexscans" for r in refs)
        assert [r.chapter for r in refs] == ["5", "4", "3", "2", "1"]
        # other-series chapters and sibling-series links are excluded (§6-adjacent hygiene)
        assert not any("contract-killer" in r.url for r in refs)
        assert not any(r.url.endswith("/hard-carry-supporter") for r in refs)

    def test_entire_comic_from_series_url(self) -> None:
        adapter = VortexScansAdapter(fetch_html=_fixture_fetch({SERIES_URL: _load("vortexscans_series.html")}))
        refs = adapter.discover_pages(SERIES_URL, CrawlScope(kind=ScopeKind.ENTIRE_COMIC))
        assert [r.url for r in refs] == EXPECTED_CHAPTERS

    def test_entire_comic_fetch_failure_falls_back(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = VortexScansAdapter(fetch_html=_fixture_fetch({}))
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(kind=ScopeKind.ENTIRE_COMIC))
        assert [r.url for r in refs] == [CHAPTER_URL]
        assert any("series page fetch failed" in record.message for record in caplog.records)

    def test_entire_comic_from_unrelated_url(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = VortexScansAdapter(fetch_html=_fixture_fetch({}))  # any fetch would raise
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages("https://vortexscans.org/", CrawlScope(kind=ScopeKind.ENTIRE_COMIC))
        assert [r.url for r in refs] == ["https://vortexscans.org/"]
        assert any("entire-comic scope needs a series or chapter url" in record.message for record in caplog.records)

    def test_entire_comic_no_chapters_falls_back(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = VortexScansAdapter(
            fetch_html=_fixture_fetch({SERIES_URL: "<html><body><p>no chapters</p></body></html>"})
        )
        with caplog.at_level(logging.WARNING):
            refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(kind=ScopeKind.ENTIRE_COMIC))
        assert [r.url for r in refs] == [CHAPTER_URL]
        assert any("chapter discovery matched nothing" in record.message for record in caplog.records)


class TestDiscoverSeries:
    def test_catalog_page(self) -> None:
        adapter = VortexScansAdapter(fetch_html=_fixture_fetch({CATALOG_URL: _load("vortexscans_catalog.html")}))
        series = adapter.discover_series(CATALOG_URL)
        assert [s.url for s in series] == [
            "https://vortexscans.org/series/contract-killer-enrollment",
            "https://vortexscans.org/series/hard-carry-supporter",
            "https://vortexscans.org/series/the-villains-profiler",
            "https://vortexscans.org/series/martial-god-regressor",
        ]
        assert series[0].title == "Contract Killer Enrollment"
        # pagination self-link (/series?page=N) is not a series.
        assert all("?" not in s.url for s in series)

    def test_catalog_pagination_walks_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("backend.scraper.adapters.vortexscans._CATALOG_PAGE_DELAY_SECONDS", 0)
        page2 = """
        <html><body><div class="grid">
          <div class="series-card"><a href="/series/new-series"><span class="title">New Series</span></a></div>
          <div class="series-card"><a href="/series/contract-killer-enrollment"><span class="title">Dup</span></a></div>
        </div></body></html>
        """

        def fetch(url: str) -> str:
            if url.endswith("/series?page=3"):
                raise httpx.ConnectError(f"no fixture for {url}")
            if url.endswith("/series?page=2"):
                return page2
            return _load("vortexscans_catalog.html")

        adapter = VortexScansAdapter(fetch_html=fetch)
        urls = [s.url for s in adapter.discover_series(CATALOG_URL)]
        assert "https://vortexscans.org/series/new-series" in urls
        # deduped across pages
        assert urls.count("https://vortexscans.org/series/contract-killer-enrollment") == 1

    def test_non_catalog_url_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = VortexScansAdapter(fetch_html=_fixture_fetch({}))
        with caplog.at_level(logging.WARNING):
            assert adapter.discover_series(CHAPTER_URL) == []
        assert any("catalog index" in record.message for record in caplog.records)

    def test_fetch_failure_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = VortexScansAdapter(fetch_html=_fixture_fetch({}))
        with caplog.at_level(logging.WARNING):
            assert adapter.discover_series(CATALOG_URL) == []
        assert any("catalog fetch failed" in record.message for record in caplog.records)
