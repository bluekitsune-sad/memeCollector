"""Comix (comix.to) adapter tests — offline fixture HTML + canned API JSON, no network.

The fixture models the verified ``li.cm-item`` comment widget: parent items,
nested replies, and the text-only ``ul.comments`` sidebar that must never be
treated as comments (AGENTS.md §6). Discovery covers the chapter API with
degradation to ``#initial-data`` page data and finally the entry URL.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from backend.scraper.adapters import CrawlScope, ScopeKind, SiteAdapter, get_adapter, list_sites
from backend.scraper.adapters.base import MediaRef
from backend.scraper.adapters.comix import ComixAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TITLE_HID = "6aa111222333444555666ff77788899a00011122"
TITLE_URL = f"https://comix.to/title/{TITLE_HID}-test-title"
READER_URL = f"{TITLE_URL}/111-chapter-1"
PAGE_URL = READER_URL

#: The only collectable media in comix_read.html (per-comment).
EXPECTED_MEDIA = {
    "414360": {
        "https://i.imgur.com/panel888.png",
        "https://comix.to/storage/media/rel-sticker.webp",
        "https://i.imgur.com/clip42.mp4",
        "https://i.imgur.com/dance9.gif",
    },
    "414361": {"https://i.imgur.com/agree42.png"},
    "414363": set(),
}

#: Avatars, logo, reader pages, poster, emoji, sidebar decoy — never collected (§6).
CHROME_URLS = {
    "https://comix.to/assets/logo.svg",
    "https://comix.to/storage/avatars/9/9a12.jpg",
    "https://comix.to/storage/avatars/3/3b77.jpg",
    "https://comix.to/storage/avatars/5/5c91.jpg",
    "https://zcomix.b-cdn.net/series/test-title/ch1/page1.jpg",
    "https://zcomix.b-cdn.net/series/test-title/ch1/page2.jpg",
    "https://comix.to/assets/poster/test-title.jpg",
    "https://i.imgur.com/widget-decoy.png",
    "https://comix.to/assets/emoji/joy.svg",
}


def _fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _no_fetch(url: str) -> str:
    raise AssertionError(f"fetch must not be called, got {url}")


def _no_fetch_json(url: str) -> Any:
    raise AssertionError(f"fetch_json must not be called, got {url}")


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


def test_can_handle_accepts_comix_to_hosts() -> None:
    adapter = ComixAdapter()
    assert adapter.can_handle(READER_URL)
    assert adapter.can_handle(TITLE_URL)
    assert adapter.can_handle("https://www.comix.to/title/x")


def test_can_handle_rejects_lookalike_domains_and_schemes() -> None:
    adapter = ComixAdapter()
    assert not adapter.can_handle("https://comick.io/comic/x")
    assert not adapter.can_handle("https://comix.im/title/x")
    assert not adapter.can_handle("https://comix.org/title/x")
    assert not adapter.can_handle("ftp://comix.to/title/x")
    assert not adapter.can_handle("comix.to/title/x")


def test_registry_resolves_comix_with_rendering() -> None:
    adapter = get_adapter(READER_URL)
    assert adapter.site == "comix"
    assert adapter.render is True  # the comment widget hydrates client-side (PRD §8).
    assert "comix" in list_sites()


def test_find_comments_parses_widget_items_and_ignores_sidebar() -> None:
    """``ul.comments`` sidebar entries are not ``li.cm-item`` and must not appear."""
    adapter = get_adapter(READER_URL)
    comments = adapter.find_comments(_fixture_html("comix_read.html"))
    assert [comment.meta.comment_id for comment in comments] == ["414360", "414361", "414363"]
    assert [comment.meta.author_name for comment in comments] == [
        "meme_lord",
        "reply_gal",
        "quiet_one",
    ]
    assert comments[0].meta.text is not None
    assert "this panel goes hard" in comments[0].meta.text
    assert comments[1].meta.text == "fr fr"
    assert comments[2].meta.text == "no media in this one"


def test_media_is_scoped_to_each_comment_content_block() -> None:
    adapter = get_adapter(READER_URL)
    media = _media_by_comment(adapter, _fixture_html("comix_read.html"), PAGE_URL)
    assert media == EXPECTED_MEDIA
    assert media["414360"].isdisjoint(media["414361"])  # replies keep their own media


def test_page_chrome_avatars_and_emoji_are_never_collected() -> None:
    """§6: avatars, logo, reader panels, poster, emoji, sidebar decoy excluded."""
    adapter = get_adapter(READER_URL)
    media = _media_by_comment(adapter, _fixture_html("comix_read.html"), PAGE_URL)
    collected = set().union(*media.values())
    assert collected == set().union(*EXPECTED_MEDIA.values())
    assert collected.isdisjoint(CHROME_URLS)


def test_media_kind_classification() -> None:
    adapter = get_adapter(READER_URL)
    refs = _media_refs(adapter, _fixture_html("comix_read.html"), PAGE_URL)
    assert refs["https://i.imgur.com/panel888.png"].kind == "image"
    assert refs["https://comix.to/storage/media/rel-sticker.webp"].kind == "image"
    clip = refs["https://i.imgur.com/clip42.mp4"]
    assert clip.kind == "video"
    assert clip.comment.comment_id == "414360"
    assert refs["https://i.imgur.com/dance9.gif"].kind == "gif"


def test_relative_attachment_resolved_against_page_url() -> None:
    adapter = get_adapter(READER_URL)
    media = _media_by_comment(adapter, _fixture_html("comix_read.html"), PAGE_URL)
    assert "https://comix.to/storage/media/rel-sticker.webp" in media["414360"]


def test_warns_when_comment_selectors_match_nothing(caplog) -> None:
    adapter = get_adapter(READER_URL)
    with caplog.at_level(logging.WARNING):
        comments = adapter.find_comments(_fixture_html("comix_title.html"))
    assert comments == []
    assert "matched nothing" in caplog.text
    assert "site=comix" in caplog.text


def test_comment_item_without_usable_id_is_skipped_with_warning(caplog) -> None:
    html = '<li class="cm-item"><span class="cm-item__user"><a href="/u/x">x</a></span></li>'
    adapter = get_adapter(READER_URL)
    with caplog.at_level(logging.WARNING):
        comments = adapter.find_comments(html)
    assert comments == []
    assert "no usable id" in caplog.text


def test_discover_current_page_and_chapter_urls() -> None:
    adapter = ComixAdapter(fetch_html=_no_fetch, fetch_json=_no_fetch_json)
    page_refs = adapter.discover_pages(READER_URL, CrawlScope(ScopeKind.CURRENT_PAGE))
    assert [ref.url for ref in page_refs] == [READER_URL]
    chapter_refs = adapter.discover_pages(READER_URL, CrawlScope(ScopeKind.CURRENT_CHAPTER))
    assert [ref.url for ref in chapter_refs] == [READER_URL]
    assert chapter_refs[0].chapter == "111-chapter-1"
    assert chapter_refs[0].site == "comix"


def test_discover_current_chapter_from_title_url_warns_and_falls_back(caplog) -> None:
    adapter = ComixAdapter(fetch_html=_no_fetch, fetch_json=_no_fetch_json)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.CURRENT_CHAPTER))
    assert [ref.url for ref in refs] == [TITLE_URL]
    assert "enter a reader url" in caplog.text


def test_discover_custom_urls_skips_unsupported_urls(caplog) -> None:
    adapter = ComixAdapter(fetch_html=_no_fetch, fetch_json=_no_fetch_json)
    scope = CrawlScope(
        ScopeKind.CUSTOM_URLS,
        urls=(READER_URL, "https://comick.io/comic/x"),
    )
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, scope)
    assert [ref.url for ref in refs] == [READER_URL]
    assert refs[0].chapter == "111-chapter-1"
    assert "skipping url handled by no adapter" in caplog.text


def test_discover_entire_comic_uses_chapter_api() -> None:
    calls: list[str] = []

    def fetch_json(url: str) -> Any:
        calls.append(url)
        return {
            "items": [
                {"url": f"/title/{TITLE_HID}-test-title/9002-chapter-2", "number": "2"},
                {"url": f"/title/{TITLE_HID}-test-title/9001-chapter-1", "number": "1"},
            ],
            "meta": {"itemCount": 2},
        }

    adapter = ComixAdapter(fetch_html=_no_fetch, fetch_json=fetch_json)
    refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert len(calls) == 1  # < limit items → single page, no runaway pagination
    assert f"/api/v1/manga/{TITLE_HID}/chapters" in calls[0]
    assert [ref.url for ref in refs] == [
        f"{TITLE_URL}/9002-chapter-2",
        f"{TITLE_URL}/9001-chapter-1",
    ]
    assert [ref.chapter for ref in refs] == ["9002-chapter-2", "9001-chapter-1"]


def test_discover_entire_comic_degrades_to_page_data_on_envelope(caplog) -> None:
    """Obfuscated API envelopes (verified in Wayback captures) → #initial-data fallback."""
    adapter = ComixAdapter(
        fetch_json=lambda url: {"e": "obfuscated"},
        fetch_html=lambda url: _fixture_html("comix_title.html"),
    )
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert "no items array" in caplog.text
    assert [ref.url for ref in refs] == [
        f"{TITLE_URL}/111-chapter-1",  # server-rendered chapter link
        f"{TITLE_URL}/333-chapter-3",  # latestChapterUrl from #initial-data
    ]


def test_discover_entire_comic_degrades_to_page_data_on_api_error(caplog) -> None:
    def fetch_json(url: str) -> Any:
        raise httpx.ConnectError("connection reset")

    adapter = ComixAdapter(
        fetch_json=fetch_json,
        fetch_html=lambda url: _fixture_html("comix_title.html"),
    )
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert "chapter-list request failed" in caplog.text
    assert [ref.url for ref in refs] == [
        f"{TITLE_URL}/111-chapter-1",
        f"{TITLE_URL}/333-chapter-3",
    ]


def test_discover_entire_comic_falls_back_to_entry_when_everything_fails(caplog) -> None:
    def fetch_json(url: str) -> Any:
        raise httpx.ConnectError("connection reset")

    adapter = ComixAdapter(
        fetch_json=fetch_json,
        fetch_html=lambda url: "<html><body><p>vue shell</p></body></html>",
    )
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [TITLE_URL]
    assert "chapter discovery matched nothing" in caplog.text


def test_discover_entire_comic_needs_title_or_reader_url(caplog) -> None:
    adapter = ComixAdapter(fetch_html=_no_fetch, fetch_json=_no_fetch_json)
    entry = "https://comix.to/search?keyword=cats"
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(entry, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [entry]
    assert "needs a title or reader url" in caplog.text
