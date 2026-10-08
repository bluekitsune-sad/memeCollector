"""AsuraScans adapter tests — offline fixture HTML only, no network (AGENTS.md §8).

Proves the §6 media rule: comment attachments are collected (including relative
URLs resolved against ``page_url``), while panels, avatars, logos, ads, the
composer, and review blocks are never comments and never media.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from backend.scraper.adapters import CrawlScope, ScopeKind, SiteAdapter, get_adapter, list_sites
from backend.scraper.adapters.asurascans import AsuraScansAdapter
from backend.scraper.adapters.base import MediaRef

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CHAPTER_URL = "https://asurascans.com/comics/the-regressor-and-the-blind-saint-bd5bdaf8/chapter/20"
SERIES_URL = "https://asurascans.com/comics/the-regressor-and-the-blind-saint-bd5bdaf8"
PAGE_URL = CHAPTER_URL

#: The only collectable media in asurascans_chapter.html (comment attachments).
EXPECTED_MEDIA = {
    "5001": {
        "https://cdn.asurascans.com/media/hype-cat.png",
        "https://asurascans.com/media/rel-sticker.gif",
    },
    "5002": {"https://cdn.asurascans.com/media/reply-sticker.png"},
    "5003": set(),
}

#: Every other image URL in the fixture — page chrome, never collected (§6).
CHROME_URLS = {
    "https://asurascans.com/assets/logo.png",
    "https://asurascans.com/assets/nav-icon.png",
    "https://asurascans.com/assets/search.png",
    "https://asurascans.com/uploads/comics/regressor/ch20/panel-1.png",
    "https://asurascans.com/uploads/comics/regressor/ch20/panel-2.png",
    "https://asurascans.com/assets/sidebar-trending.png",
    "https://asurascans.com/assets/related-1.png",
    "https://asurascans.com/uploads/reviews/review-attach.png",
    "https://asurascans.com/avatars/me.png",
    "https://asurascans.com/avatars/alice.png",
    "https://asurascans.com/avatars/bob.png",
    "https://asurascans.com/avatars/carol.png",
    "https://asurascans.com/assets/attach-icon.png",
    "https://ads.example-network.com/banner/asura-728x90.png",
    "https://asurascans.com/assets/footer-ad.png",
}


def _fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _no_fetch(url: str) -> str:
    raise AssertionError(f"fetch must not be called, got {url}")


def _media_by_comment(adapter: SiteAdapter, html: str, page_url: str) -> dict[str, set[str]]:
    """Mimic the crawler: find_comments → provenance backfill → find_comment_media."""
    media: dict[str, set[str]] = {}
    for comment in adapter.find_comments(html):
        comment.meta.page_url = page_url
        media[comment.meta.comment_id] = {ref.url for ref in adapter.find_comment_media(comment)}
    return media


def _media_refs(adapter: SiteAdapter, html: str, page_url: str) -> dict[str, MediaRef]:
    refs: dict[str, MediaRef] = {}
    for comment in adapter.find_comments(html):
        comment.meta.page_url = page_url
        for ref in adapter.find_comment_media(comment):
            refs[ref.url] = ref
    return refs


def test_can_handle_accepts_asurascans_hosts() -> None:
    adapter = AsuraScansAdapter()
    assert adapter.can_handle(CHAPTER_URL)
    assert adapter.can_handle("https://www.asurascans.com/comics/x")
    assert adapter.can_handle("http://asurascans.com/comics/x")


def test_can_handle_rejects_other_hosts_and_schemes() -> None:
    adapter = AsuraScansAdapter()
    assert not adapter.can_handle("https://asurascans.com.evil.test/comics/x")
    assert not adapter.can_handle("ftp://asurascans.com/comics/x")
    assert not adapter.can_handle("https://comick.io/comic/solo-leveling")
    assert not adapter.can_handle("asurascans.com/comics/x")


def test_registry_resolves_asurascans_with_rendering() -> None:
    adapter = get_adapter(CHAPTER_URL)
    assert adapter.site == "asurascans"
    assert adapter.render is True  # comments are client-loaded (PRD §8).
    assert "asurascans" in list_sites()


def test_find_comments_parses_comments_not_composer_or_reviews() -> None:
    """Composer and review blocks carry comment-like markup but are not comments."""
    adapter = get_adapter(CHAPTER_URL)
    comments = adapter.find_comments(_fixture_html("asurascans_chapter.html"))
    assert [comment.meta.comment_id for comment in comments] == ["5001", "5002", "5003"]
    assert [comment.meta.author_name for comment in comments] == ["alice", "bob", "carol"]
    assert comments[0].meta.text == "This chapter is fire, look at this"
    assert comments[1].meta.text == "replying with my own sticker"
    assert comments[2].meta.text == "no image from me, just hype"
    ids = {comment.meta.comment_id for comment in comments}
    assert "5001" in ids and "9001" not in ids  # review-9001 excluded, comment-5001 kept


def test_media_extracts_only_comment_attachments_per_comment() -> None:
    adapter = get_adapter(CHAPTER_URL)
    media = _media_by_comment(adapter, _fixture_html("asurascans_chapter.html"), PAGE_URL)
    assert media == EXPECTED_MEDIA
    assert media["5001"].isdisjoint(CHROME_URLS)
    assert media["5002"].isdisjoint(CHROME_URLS)


def test_page_chrome_is_never_collected() -> None:
    """AGENTS.md §6: panels, avatars, logos, ads, sidebar images are never media."""
    adapter = get_adapter(CHAPTER_URL)
    media = _media_by_comment(adapter, _fixture_html("asurascans_chapter.html"), PAGE_URL)
    collected = set().union(*media.values())
    assert collected == set().union(*EXPECTED_MEDIA.values())
    assert collected.isdisjoint(CHROME_URLS)


def test_media_kind_and_original_filename() -> None:
    adapter = get_adapter(CHAPTER_URL)
    refs = _media_refs(adapter, _fixture_html("asurascans_chapter.html"), PAGE_URL)
    assert refs["https://cdn.asurascans.com/media/hype-cat.png"].kind == "image"
    assert refs["https://cdn.asurascans.com/media/reply-sticker.png"].kind == "image"
    gif = refs["https://asurascans.com/media/rel-sticker.gif"]
    assert gif.kind == "gif"
    assert gif.original_filename == "rel-sticker.gif"


def test_relative_attachment_resolved_against_page_url() -> None:
    adapter = get_adapter(CHAPTER_URL)
    media = _media_by_comment(adapter, _fixture_html("asurascans_chapter.html"), PAGE_URL)
    assert "https://asurascans.com/media/rel-sticker.gif" in media["5001"]


def test_warns_when_comment_selectors_match_nothing(caplog) -> None:
    adapter = get_adapter(CHAPTER_URL)
    with caplog.at_level(logging.WARNING):
        comments = adapter.find_comments(_fixture_html("asurascans_series.html"))
    assert comments == []
    assert "matched nothing" in caplog.text
    assert "site=asurascans" in caplog.text


def test_discover_current_page_and_chapter_urls() -> None:
    adapter = AsuraScansAdapter(fetch_html=_no_fetch)  # URL-derived: must not fetch.
    page_refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.CURRENT_PAGE))
    assert [ref.url for ref in page_refs] == [CHAPTER_URL]
    chapter_refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.CURRENT_CHAPTER))
    assert [ref.url for ref in chapter_refs] == [CHAPTER_URL]
    assert chapter_refs[0].chapter == "20"
    assert chapter_refs[0].site == "asurascans"


def test_discover_current_chapter_from_series_url_warns_and_falls_back(caplog) -> None:
    adapter = AsuraScansAdapter(fetch_html=_no_fetch)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(SERIES_URL, CrawlScope(ScopeKind.CURRENT_CHAPTER))
    assert [ref.url for ref in refs] == [SERIES_URL]
    assert "enter a chapter url" in caplog.text


def test_discover_custom_urls_skips_unsupported_urls(caplog) -> None:
    adapter = AsuraScansAdapter(fetch_html=_no_fetch)
    scope = CrawlScope(
        ScopeKind.CUSTOM_URLS,
        urls=(CHAPTER_URL, "https://mangapark.net/title/test-title-en-english/3-x"),
    )
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(SERIES_URL, scope)
    assert [ref.url for ref in refs] == [CHAPTER_URL]
    assert refs[0].chapter == "20"
    assert "skipping url handled by no adapter" in caplog.text


def test_discover_entire_comic_lists_series_chapters() -> None:
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return _fixture_html("asurascans_series.html")

    adapter = AsuraScansAdapter(fetch_html=fetch)
    refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert fetched == [SERIES_URL]
    assert [ref.url for ref in refs] == [
        f"{SERIES_URL}/chapter/{number}" for number in ("0", "1", "2")
    ]
    assert [ref.chapter for ref in refs] == ["0", "1", "2"]


def test_discover_entire_comic_falls_back_to_continue_reading_json() -> None:
    html = (
        '<html><body><script type="application/json" id="continue-reading-data">'
        '{"kind":"comic","slug":"the-regressor-and-the-blind-saint",'
        '"base":"/comics/the-regressor-and-the-blind-saint-bd5bdaf8","numbers":[2,1,0]}'
        "</script></body></html>"
    )
    adapter = AsuraScansAdapter(fetch_html=lambda url: html)
    refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.chapter for ref in refs] == ["2", "1", "0"]
    assert all(ref.url.startswith(f"{SERIES_URL}/chapter/") for ref in refs)


def test_discover_entire_comic_fetch_failure_falls_back_to_entry(caplog) -> None:
    def fetch(url: str) -> str:
        raise httpx.ConnectError("connection refused")

    adapter = AsuraScansAdapter(fetch_html=fetch)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [CHAPTER_URL]
    assert "series page fetch failed" in caplog.text


def test_discover_entire_comic_without_chapter_links_matches_nothing(caplog) -> None:
    adapter = AsuraScansAdapter(fetch_html=lambda url: "<html><body><p>shell</p></body></html>")
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [CHAPTER_URL]
    assert "matched nothing" in caplog.text


def test_discover_entire_comic_needs_series_or_chapter_url(caplog) -> None:
    adapter = AsuraScansAdapter(fetch_html=_no_fetch)
    entry = "https://asurascans.com/popular"
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(entry, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [entry]
    assert "needs a series or chapter url" in caplog.text
