"""MangaDex adapter tests — offline fixture HTML + canned API JSON, no network.

mangadex.org is an SPA without comment DOM, so the adapter maps entry URLs to
their ``forums.mangadex.org`` XenForo threads through the statistics API
(injectable ``fetch_json``). The fixture thread proves the §6 rule: attachments
inside ``.bbWrapper`` only — never avatars, logos, smilies, reactions, or
OpenGraph cards.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from backend.scraper.adapters import CrawlScope, ScopeKind, SiteAdapter, get_adapter, list_sites
from backend.scraper.adapters.base import MediaRef, PageRef
from backend.scraper.adapters.mangadex import MangaDexAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TITLE_UUID = "9c1f5f86-6b70-4b52-b9b6-32e0e1b7ccd4"
CHAPTER_UUID = "0d1a2b3c-1111-2222-3333-444455556666"
MANGA_ID = "1a2b3c4d-aaaa-bbbb-cccc-ddddeeeeffff"

TITLE_URL = f"https://mangadex.org/title/{TITLE_UUID}"
CHAPTER_URL = f"https://mangadex.org/chapter/{CHAPTER_UUID}"
THREAD_URL = "https://forums.mangadex.org/threads/1444828/"
PAGE_URL = THREAD_URL

#: Attachments of post 741001 — the only collectable media in the fixture.
EXPECTED_MEDIA = {
    "741001": {
        "https://forums.mangadex.org/attachments/panel-1-jpg.90001/panel-1.jpg",
        "https://i.imgur.com/abc123.jpg",
        "https://i.imgur.com/def456.png",
        "https://i.imgur.com/funny789.gif",
        "https://i.imgur.com/clip9.webm",
    },
    "741002": set(),
}

#: Avatars, logos, and the og-card in the fixture — never collected (§6).
CHROME_URLS = {
    "https://forums.mangadex.org/styles/default/xenforo/logo.png",
    "https://forums.mangadex.org/data/avatars/s/12/12345.jpg?1690000000",
    "https://forums.mangadex.org/data/avatars/s/6/67890.jpg",
    "https://og.mangadex.org/og/en/manga/solo-leveling/9c1f5f86/1.png",
}

_FEED: dict[str, Any] = {
    "data": [
        {"type": "chapter", "id": "ch-a", "attributes": {"chapter": "1"}},
        {"type": "chapter", "id": "ch-b", "attributes": {"chapter": "2"}},
        {"type": "chapter", "id": "ch-c", "attributes": {"chapter": "3"}},
    ],
    "next": None,
}

_THREAD_IDS: dict[str, int] = {
    TITLE_UUID: 999,
    CHAPTER_UUID: 55555,
    "ch-a": 111,
    "ch-b": 222,
    "ch-c": 111,  # duplicates thread 111 — discovery must dedupe it.
}


def _fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _no_fetch_json(url: str) -> Any:
    raise AssertionError(f"fetch_json must not be called, got {url}")


def _statistics(entity_id: str, thread_id: int) -> dict[str, Any]:
    return {"statistics": {entity_id: {"comments": {"threadId": thread_id, "repliesCount": 12}}}}


def _fetch_json(url: str) -> Any:
    """Canned API dispatcher: statistics / feed / chapter lookup."""
    if "/statistics/" in url:
        entity_id = url.rstrip("/").rsplit("/", 1)[-1]
        return _statistics(entity_id, _THREAD_IDS[entity_id])
    if "/feed" in url:
        return _FEED
    return {"data": {"relationships": [{"type": "manga", "id": MANGA_ID}]}}


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


def test_can_handle_accepts_site_and_forums_hosts() -> None:
    adapter = MangaDexAdapter()
    assert adapter.can_handle(TITLE_URL)
    assert adapter.can_handle(CHAPTER_URL)
    assert adapter.can_handle(THREAD_URL)
    assert adapter.can_handle("https://www.mangadex.org/title/x")


def test_can_handle_rejects_other_hosts_and_schemes() -> None:
    adapter = MangaDexAdapter()
    assert not adapter.can_handle("https://api.mangadex.org/manga/x")  # API ≠ site pages
    assert not adapter.can_handle("https://mangadex.org.evil.test/title/x")
    assert not adapter.can_handle("ftp://mangadex.org/title/x")
    assert not adapter.can_handle("mangadex.org/title/x")


def test_registry_resolves_both_hosts_without_rendering() -> None:
    assert get_adapter(TITLE_URL).site == "mangadex"
    adapter = get_adapter(THREAD_URL)
    assert adapter.site == "mangadex"
    assert adapter.render is False  # XenForo threads are server-rendered (PRD §8).
    assert "mangadex" in list_sites()


def test_find_comments_parses_xenforo_posts() -> None:
    adapter = get_adapter(THREAD_URL)
    comments = adapter.find_comments(_fixture_html("mangadex_thread.html"))
    assert [comment.meta.comment_id for comment in comments] == ["741001", "741002"]
    assert [comment.meta.author_name for comment in comments] == ["Kei", "Rin"]
    assert comments[0].meta.text is not None
    assert "this panel goes hard" in comments[0].meta.text
    assert "https://i.imgur.com/def456.png" in comments[0].meta.text
    assert comments[1].meta.text == "This chapter was peak. No spoilers please."


def test_media_extracts_only_attachments_inside_message_body() -> None:
    adapter = get_adapter(THREAD_URL)
    media = _media_by_comment(adapter, _fixture_html("mangadex_thread.html"), PAGE_URL)
    assert media == EXPECTED_MEDIA


def test_page_chrome_sprites_and_og_cards_are_never_collected() -> None:
    """§6: avatars, logos, smilies/reactions (data: sprites), og-cards excluded."""
    adapter = get_adapter(THREAD_URL)
    media = _media_by_comment(adapter, _fixture_html("mangadex_thread.html"), PAGE_URL)
    collected = set().union(*media.values())
    assert collected.isdisjoint(CHROME_URLS)
    assert not any(url.startswith("data:") for url in collected)


def test_media_kind_classification() -> None:
    adapter = get_adapter(THREAD_URL)
    refs = _media_refs(adapter, _fixture_html("mangadex_thread.html"), PAGE_URL)
    assert refs["https://i.imgur.com/abc123.jpg"].kind == "image"
    assert refs["https://i.imgur.com/funny789.gif"].kind == "gif"
    clip = refs["https://i.imgur.com/clip9.webm"]
    assert clip.kind == "video"
    assert clip.comment.comment_id == "741001"  # attribution to the right comment


def test_post_without_id_is_skipped_with_warning(caplog) -> None:
    html = (
        '<article class="message message--post" id="weird">'
        '<div class="message-body"><blockquote class="message-body bbWrapper">hi</blockquote>'
        "</div></article>"
    )
    adapter = get_adapter(THREAD_URL)
    with caplog.at_level(logging.WARNING):
        comments = adapter.find_comments(html)
    assert comments == []
    assert "no post id" in caplog.text


def test_warns_when_comment_selectors_match_nothing(caplog) -> None:
    adapter = get_adapter(THREAD_URL)
    with caplog.at_level(logging.WARNING):
        comments = adapter.find_comments("<html><body><div id='app'></div></body></html>")
    assert comments == []
    assert "matched nothing" in caplog.text
    assert "site=mangadex" in caplog.text


def test_discover_custom_urls_verbatim_and_skips_unsupported(caplog) -> None:
    adapter = MangaDexAdapter(fetch_json=_no_fetch_json)  # CUSTOM is verbatim: no lookups.
    scope = CrawlScope(
        ScopeKind.CUSTOM_URLS,
        urls=(CHAPTER_URL, THREAD_URL, "https://example.com/comic"),
    )
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, scope)
    assert [ref.url for ref in refs] == [CHAPTER_URL, THREAD_URL]
    assert refs[0].chapter == CHAPTER_UUID
    assert refs[1].chapter is None
    assert "skipping url handled by no adapter" in caplog.text


def test_discover_multiple_chapters_resolves_threads_per_entry(caplog) -> None:
    scope = CrawlScope(
        ScopeKind.MULTIPLE_CHAPTERS,
        urls=(THREAD_URL, TITLE_URL, CHAPTER_URL, "https://example.com/comic"),
    )
    adapter = MangaDexAdapter(fetch_json=_fetch_json)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, scope)
    assert [ref.url for ref in refs] == [
        THREAD_URL,
        "https://forums.mangadex.org/threads/999/",
        "https://forums.mangadex.org/threads/55555/",
    ]
    assert "skipping url handled by no adapter" in caplog.text


def test_discover_current_page_forums_url_needs_no_lookup() -> None:
    adapter = MangaDexAdapter(fetch_json=_no_fetch_json)
    refs = adapter.discover_pages(THREAD_URL, CrawlScope(ScopeKind.CURRENT_PAGE))
    assert [ref.url for ref in refs] == [THREAD_URL]
    assert refs[0].site == "mangadex"


def test_discover_current_page_chapter_url_resolves_comment_thread() -> None:
    adapter = MangaDexAdapter(fetch_json=_fetch_json)
    refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.CURRENT_PAGE))
    assert [ref.url for ref in refs] == ["https://forums.mangadex.org/threads/55555/"]
    assert refs[0].chapter == CHAPTER_UUID


def test_statistics_failures_fall_back_to_entry_page(caplog) -> None:
    scope = CrawlScope(ScopeKind.CURRENT_PAGE)

    def raising(url: str) -> Any:
        raise httpx.ConnectError("connection refused")

    adapter = MangaDexAdapter(fetch_json=raising)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(CHAPTER_URL, scope)
    assert [ref.url for ref in refs] == [CHAPTER_URL]
    assert "statistics lookup failed" in caplog.text
    assert "falling back to entry page" in caplog.text

    caplog.clear()

    def without_thread(url: str) -> Any:
        entity_id = url.rstrip("/").rsplit("/", 1)[-1]
        return {"statistics": {entity_id: {"comments": {"repliesCount": 0}}}}

    adapter = MangaDexAdapter(fetch_json=without_thread)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(CHAPTER_URL, scope)
    assert [ref.url for ref in refs] == [CHAPTER_URL]
    assert "statistics returned no comment thread" in caplog.text


def test_discover_entire_comic_resolves_and_dedupes_threads() -> None:
    calls: list[str] = []

    def fetch_json(url: str) -> Any:
        calls.append(url)
        return _fetch_json(url)

    adapter = MangaDexAdapter(fetch_json=fetch_json)
    refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert refs == [
        PageRef(url="https://forums.mangadex.org/threads/111/", site="mangadex", chapter="1"),
        PageRef(url="https://forums.mangadex.org/threads/222/", site="mangadex", chapter="2"),
    ]
    # One feed call + one statistics call per chapter (batch ids[] is rejected: verified HTTP 400).
    assert len(calls) == 4
    assert not any("ids" in call for call in calls)


def test_discover_entire_comic_from_chapter_url_resolves_manga_first() -> None:
    calls: list[str] = []

    def fetch_json(url: str) -> Any:
        calls.append(url)
        return _fetch_json(url)

    adapter = MangaDexAdapter(fetch_json=fetch_json)
    refs = adapter.discover_pages(CHAPTER_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [
        "https://forums.mangadex.org/threads/111/",
        "https://forums.mangadex.org/threads/222/",
    ]
    assert calls[0] == f"https://api.mangadex.org/chapter/{CHAPTER_UUID}?includes[]=manga"


def test_discover_entire_comic_feed_failures_fall_back_to_entry(caplog) -> None:
    def raising(url: str) -> Any:
        raise httpx.ConnectError("gateway timeout")

    adapter = MangaDexAdapter(fetch_json=raising)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [TITLE_URL]
    assert "chapter feed request failed" in caplog.text

    caplog.clear()

    adapter = MangaDexAdapter(fetch_json=lambda url: {"data": [], "next": None})
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(TITLE_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [TITLE_URL]
    assert "chapter feed matched nothing" in caplog.text


def test_discover_entire_comic_from_thread_url_cannot_guess(caplog) -> None:
    adapter = MangaDexAdapter(fetch_json=_no_fetch_json)
    with caplog.at_level(logging.WARNING):
        refs = adapter.discover_pages(THREAD_URL, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [THREAD_URL]
    assert "could not resolve a manga id" in caplog.text
