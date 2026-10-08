"""MangaPark adapter tests — offline fixture HTML only, no network (AGENTS.md §8).

Proves the §6 media rule: only media inside each comment's ``div.my-2`` content
wrapper is collected — never avatars, chapter panels, logos, or the decoy image
carrying ``alt="Comment media"`` outside the comment list. Nested replies keep
their own attachments.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from backend.scraper.adapters import CrawlScope, ScopeKind, SiteAdapter, get_adapter, list_sites
from backend.scraper.adapters.base import MediaRef
from backend.scraper.adapters.mangapark import MangaParkAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TITLE_URL = "https://mangapark.net/title/test-title-en-english"
CHAPTER_URL = f"{TITLE_URL}/3-test-chapter-1"
PAGE_URL = CHAPTER_URL

#: The only collectable media in mangapark_chapter.html (per-comment).
EXPECTED_MEDIA = {
    "64f0a1b2c3d4e5f607182930": {"https://i.imgur.com/hype555.gif"},
    "64f0a1b2c3d4e5f607182931": {
        "https://mangapark.net/media/rel-sticker.gif",
        "https://cdn.mangapark.net/media/wow.png",
    },
    "611222333444555666777888": set(),
    "6109876543210fedcba98765": {"https://i.imgur.com/deletedpic.png"},
}

#: Page chrome in the fixture — never collected (§6).
CHROME_URLS = {
    "https://cdn.mangapark.net/logo.svg",
    "https://cdn.mangapark.net/series/test-title/ch3/p1.jpg",
    "https://cdn.mangapark.net/series/test-title/ch3/p2.jpg",
    "https://i.imgur.com/decoy-attach.png",
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


def test_can_handle_accepts_mangapark_net_hosts() -> None:
    adapter = MangaParkAdapter()
    assert adapter.can_handle(CHAPTER_URL)
    assert adapter.can_handle("https://mangapark.net/title/x")
    assert adapter.can_handle("https://www.mangapark.net/title/x")


def test_can_handle_rejects_unverified_mirrors_and_schemes() -> None:
    """``.io``/``.to`` mirrors are deliberately not claimed (see module docstring)."""
    adapter = MangaParkAdapter()
    assert not adapter.can_handle("https://mangapark.io/title/x/1-y")
    assert not adapter.can_handle("https://mangapark.to/title/x/1-y")
    assert not adapter.can_handle("ftp://mangapark.net/title/x")
    assert not adapter.can_handle("mangapark.net/title/x")


def test_registry_resolves_mangapark_with_rendering() -> None:
    adapter = get_adapter(CHAPTER_URL)
    assert adapter.site == "mangapark"
    assert adapter.render is True  # Qwik hydrates comments client-side (PRD §8).
    assert "mangapark" in list_sites()


def test_find_comments_parses_comment_items_including_deleted_fallback() -> None:
    adapter = get_adapter(CHAPTER_URL)
    comments = adapter.find_comments(_fixture_html("mangapark_chapter.html"))
    assert [comment.meta.comment_id for comment in comments] == [
        "64f0a1b2c3d4e5f607182930",
        "64f0a1b2c3d4e5f607182931",
        "611222333444555666777888",
        "6109876543210fedcba98765",
    ]
    assert [comment.meta.author_name for comment in comments] == [
        "someuser",
        "nestedpal",
        "quietreader",
        "deletedfan",
    ]
    assert comments[0].meta.text == "First!"
    assert comments[1].meta.text == "reply with media and a host one"
    assert comments[2].meta.text == "just text, no media here"
    assert comments[3].meta.text == "this comment looks deleted but has id shape"


def test_media_is_scoped_to_each_comment_including_nested_reply() -> None:
    adapter = get_adapter(CHAPTER_URL)
    media = _media_by_comment(adapter, _fixture_html("mangapark_chapter.html"), PAGE_URL)
    assert media == EXPECTED_MEDIA
    # The parent never absorbs the reply's media (and vice versa).
    assert media["64f0a1b2c3d4e5f607182930"].isdisjoint(media["64f0a1b2c3d4e5f607182931"])


def test_page_chrome_is_never_collected() -> None:
    """§6: logo, chapter panels, and the alt-text decoy outside comments excluded."""
    adapter = get_adapter(CHAPTER_URL)
    media = _media_by_comment(adapter, _fixture_html("mangapark_chapter.html"), PAGE_URL)
    collected = set().union(*media.values())
    assert collected == set().union(*EXPECTED_MEDIA.values())
    assert collected.isdisjoint(CHROME_URLS)


def test_media_kind_classification() -> None:
    adapter = get_adapter(CHAPTER_URL)
    refs = _media_refs(adapter, _fixture_html("mangapark_chapter.html"), PAGE_URL)
    assert refs["https://i.imgur.com/hype555.gif"].kind == "gif"
    assert refs["https://cdn.mangapark.net/media/wow.png"].kind == "image"


def test_relative_attachment_resolved_against_page_url() -> None:
    adapter = get_adapter(CHAPTER_URL)
    media = _media_by_comment(adapter, _fixture_html("mangapark_chapter.html"), PAGE_URL)
    assert "https://mangapark.net/media/rel-sticker.gif" in media["64f0a1b2c3d4e5f607182931"]


def test_warns_when_comment_selectors_match_nothing(caplog) -> None:
    adapter = get_adapter(CHAPTER_URL)
    with caplog.at_level(logging.WARNING):
        comments = adapter.find_comments(_fixture_html("mangapark_title.html"))
    assert comments == []
    assert "matched nothing" in caplog.text
    assert "site=mangapark" in caplog.text


def test_comment_item_without_usable_id_is_skipped_with_warning(caplog) -> None:
    html = (
        '<div data-name="comment-item"><a href="/u/x">x</a>'
        '<div class="my-2">hello</div></div>'
    )
    adapter = get_adapter(CHAPTER_URL)
    with caplog.at_level(logging.WARNING):
        comments = adapter.find_comments(html)
    assert comments == []
    assert "no usable id" in caplog.text


def test_discover_current_page_and_chapter_urls() -> None:
    adapter = MangaParkAdapter(fetch_html=_no_fetch)  # URL-derived: must not fetch.
    page_refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.CURRENT_PAGE))
    assert [ref.url for ref in page_refs] == [CHAPTER_URL]
    chapter_refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.CURRENT_CHAPTER))
    assert [ref.url for ref in chapter_refs] == [CHAPTER_URL]
    assert chapter_refs[0].chapter == "3-test-chapter-1"
    assert chapter_refs[0].site == "mangapark"


def test_discover_current_chapter_from_title_url_warns_and_falls_back(caplog) -> None:
    adapter = MangaParkAdapter(fetch_html=_no_fetch)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.CURRENT_CHAPTER))
    assert [ref.url for ref in refs] == [TITLE_URL]
    assert "enter a chapter url" in caplog.text


def test_discover_custom_urls_skips_unsupported_urls(caplog) -> None:
    adapter = MangaParkAdapter(fetch_html=_no_fetch)
    scope = CrawlScope(
        ScopeKind.CUSTOM_URLS,
        urls=(CHAPTER_URL, "https://asurascans.com/comics/x/chapter/1"),
    )
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, scope)
    assert [ref.url for ref in refs] == [CHAPTER_URL]
    assert "skipping url handled by no adapter" in caplog.text


def test_discover_entire_comic_lists_title_chapters() -> None:
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return _fixture_html("mangapark_title.html")

    adapter = MangaParkAdapter(fetch_html=fetch)
    refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert fetched == [TITLE_URL]
    assert [ref.url for ref in refs] == [
        f"{TITLE_URL}/3-test-chapter-1",
        f"{TITLE_URL}/4-test-chapter-2",
        f"{TITLE_URL}/5-test-chapter-3",
    ]
    assert [ref.chapter for ref in refs] == ["3-test-chapter-1", "4-test-chapter-2", "5-test-chapter-3"]


def test_discover_entire_comic_fetch_failure_falls_back_to_entry(caplog) -> None:
    def fetch(url: str) -> str:
        raise httpx.ConnectError("connection refused")

    adapter = MangaParkAdapter(fetch_html=fetch)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [CHAPTER_URL]
    assert "title page fetch failed" in caplog.text


def test_discover_entire_comic_without_chapter_links_matches_nothing(caplog) -> None:
    adapter = MangaParkAdapter(
        fetch_html=lambda url: '<html><body><a href="/title/other/1-x">other</a></body></html>'
    )
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [CHAPTER_URL]
    assert "matched nothing" in caplog.text


def test_discover_entire_comic_needs_title_or_chapter_url(caplog) -> None:
    adapter = MangaParkAdapter(fetch_html=_no_fetch)
    entry = "https://mangapark.net/search?keyword=cats"
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(entry, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [entry]
    assert "needs a title or chapter url" in caplog.text
