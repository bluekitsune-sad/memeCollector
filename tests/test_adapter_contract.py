"""Adapter contract tests (M1.1) — registry, scope, and the §6 media-identification rule.

The chrome assertions here are the AGENTS.md §6 proof: only comment
attachments are extracted; logos, nav icons, comic panels, avatars, ads, and
sidebar images are never returned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.scraper.adapters import (
    Comment,
    CommentMeta,
    CrawlScope,
    MediaRef,
    ScopeKind,
    UnsupportedSiteError,
    get_adapter,
    list_sites,
    register,
)
from tests.fixtures.fake_adapter import FakeSiteAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Attachment URLs in comment_attachments_heavy_chrome.html — the ONLY collectable media.
HEAVY_CHROME_EXPECTED = {
    "https://cdn.example.com/media/heavy-one.png",
    "https://cdn.example.com/media/heavy-two.gif",
    "https://cdn.example.com/media/heavy-clip.mp4",
    "https://fixture.test/uploads/comments/rel-sticker.png",
    "https://cdn.example.com/media/eye-roll.webm",
}

# Page chrome in the same fixture — none of it may ever be collected.
HEAVY_CHROME_IGNORED = {
    "https://fixture.test/assets/logo.png",
    "https://fixture.test/assets/nav-icon.png",
    "https://fixture.test/assets/search.png",
    "https://fixture.test/assets/sidebar-trending.png",
    "https://fixture.test/assets/grey.gif",
    "https://fixture.test/assets/related-1.png",
    "https://fixture.test/uploads/comics/ch42/panel-1.png",
    "https://fixture.test/uploads/comics/ch42/panel-2.png",
    "https://fixture.test/avatars/user_a.png",
    "https://fixture.test/avatars/user_b.png",
    "https://fixture.test/avatars/user_c.png",
    "https://fixture.test/avatars/user_d.png",
    "https://fixture.test/assets/footer-ad.png",
}

# Lazy fixture: data-src / data-original / srcset candidates only.
LAZY_EXPECTED = {
    "https://cdn.example.com/media/late-load.gif",
    "https://cdn.example.com/media/orig.png",
    "https://cdn.example.com/media/small.jpg",
}


def _fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _comment_media(adapter: FakeSiteAdapter, comment: Comment, page_url: str) -> list[MediaRef]:
    """Media for one comment after the crawler-style provenance backfill."""
    comment.meta.page_url = page_url
    return adapter.find_comment_media(comment)


def _media_urls(adapter: FakeSiteAdapter, html: str, page_url: str) -> set[str]:
    """Mimic the crawler's order: find_comments → provenance backfill → find_comment_media."""
    urls: set[str] = set()
    for comment in adapter.find_comments(html):
        adapter.get_comment_metadata(comment).page_url = page_url
        urls.update(ref.url for ref in adapter.find_comment_media(comment))
    return urls


def test_scope_kinds_match_prd_5_1() -> None:
    assert [kind.value for kind in ScopeKind] == [
        "current_page",
        "current_chapter",
        "multiple_chapters",
        "entire_comic",
        "custom_urls",
    ]


def test_registry_resolves_supported_url(fake_adapter: FakeSiteAdapter) -> None:
    assert get_adapter("https://fixture.test/comic/chapter-42?page=1") is fake_adapter
    assert "fixture" in list_sites()


def test_registry_rejects_duplicate_site(fake_adapter: FakeSiteAdapter) -> None:
    with pytest.raises(ValueError, match="duplicate adapter registration"):
        register(FakeSiteAdapter())


def test_unknown_url_raises_unsupported_site_error_with_hostname(
    fake_adapter: FakeSiteAdapter,
) -> None:
    with pytest.raises(UnsupportedSiteError) as excinfo:
        get_adapter("https://unknown.example/comic/chapter-9")
    message = str(excinfo.value)
    assert "Site not supported" in message
    assert "unknown.example" in message


def test_can_handle_ignores_other_hosts(fake_adapter: FakeSiteAdapter) -> None:
    assert not fake_adapter.can_handle("https://other.test/comic")
    assert fake_adapter.can_handle("https://fixture.test/comic")


def test_discover_current_page_is_single_ref(fake_adapter: FakeSiteAdapter) -> None:
    refs = fake_adapter.discover_pages(
        "https://fixture.test/comic/chapter-42?page=7", CrawlScope(ScopeKind.CURRENT_PAGE)
    )
    assert [ref.url for ref in refs] == ["https://fixture.test/comic/chapter-42?page=7"]
    assert refs[0].page_number == 7
    assert refs[0].chapter == "chapter-42"
    assert refs[0].site == "fixture"


def test_discover_current_chapter_expands_pages(fake_adapter: FakeSiteAdapter) -> None:
    refs = fake_adapter.discover_pages(
        "https://fixture.test/comic/chapter-42?page=1", CrawlScope(ScopeKind.CURRENT_CHAPTER)
    )
    assert [ref.page_number for ref in refs] == [1, 2, 3]
    assert all(ref.chapter == "chapter-42" for ref in refs)


def test_discover_custom_urls_verbatim(fake_adapter: FakeSiteAdapter) -> None:
    scope = CrawlScope(
        ScopeKind.CUSTOM_URLS,
        urls=("https://fixture.test/a?page=1", "https://fixture.test/b?page=2"),
    )
    refs = fake_adapter.discover_pages("https://fixture.test/comic", scope)
    assert [ref.url for ref in refs] == list(scope.urls)


def test_discover_entire_comic_from_listing_fixture(fake_adapter: FakeSiteAdapter) -> None:
    adapter = FakeSiteAdapter(listing_html=_fixture_html("chapter_listing.html"))
    refs = adapter.discover_pages("https://fixture.test/comic", CrawlScope(ScopeKind.ENTIRE_COMIC))
    # 2 + 3 + 1 pages across the three chapters; decoy links ignored.
    assert len(refs) == 6
    assert {ref.chapter for ref in refs} == {"chapter-41", "chapter-42", "chapter-43"}
    assert sum(1 for ref in refs if ref.chapter == "chapter-42") == 3


def test_sample_fixture_collects_attachments_only(
    fake_adapter: FakeSiteAdapter, sample_comment_html_path: Path
) -> None:
    html = sample_comment_html_path.read_text(encoding="utf-8")
    comments = fake_adapter.find_comments(html)
    assert [comment.meta.comment_id for comment in comments] == ["918271", "918273"]
    assert [comment.meta.author_name for comment in comments] == ["reader_one", "reader_two"]
    assert comments[0].meta.text == "This chapter is gold 😂"

    urls = _media_urls(fake_adapter, html, "https://fixture.test/comic/chapter-42?page=17")
    assert urls == {
        "https://cdn.example.com/media/confused-cat.png",
        "https://cdn.example.com/media/turn-around.gif",
    }


def test_heavy_chrome_fixture_extracts_only_comment_attachments(
    fake_adapter: FakeSiteAdapter,
) -> None:
    """AGENTS.md §6: panels, logos, nav icons, avatars, ads are never collected."""
    html = _fixture_html("comment_attachments_heavy_chrome.html")
    page_url = "https://fixture.test/comic/chapter-42?page=1"

    comments = fake_adapter.find_comments(html)
    assert [comment.meta.comment_id for comment in comments] == [
        "919001",
        "919002",
        "919003",
        "919004",
    ]

    urls = _media_urls(fake_adapter, html, page_url)
    assert urls == HEAVY_CHROME_EXPECTED
    assert urls.isdisjoint(HEAVY_CHROME_IGNORED)

    kinds = {
        ref.url: ref.kind
        for comment in comments
        for ref in _comment_media(fake_adapter, comment, page_url)
    }
    assert kinds["https://cdn.example.com/media/heavy-two.gif"] == "gif"
    assert kinds["https://cdn.example.com/media/heavy-clip.mp4"] == "video"
    assert kinds["https://cdn.example.com/media/eye-roll.webm"] == "video"


def test_lazy_loaded_attributes_are_extracted(fake_adapter: FakeSiteAdapter) -> None:
    html = _fixture_html("lazy_loaded_comments.html")
    page_url = "https://fixture.test/comic/chapter-42?page=2"

    comments = fake_adapter.find_comments(html)
    assert len(comments) == 3

    urls = _media_urls(fake_adapter, html, page_url)
    assert urls == LAZY_EXPECTED
    # Placeholders (data URI, blank.gif, placeholder.png) and chrome lazy image are not media.
    assert not any("placeholder" in url or "blank.gif" in url for url in urls)
    assert "https://fixture.test/assets/chrome-lazy.png" not in urls


def test_relative_attachment_resolved_against_page_url(fake_adapter: FakeSiteAdapter) -> None:
    html = _fixture_html("comment_attachments_heavy_chrome.html")
    urls = _media_urls(fake_adapter, html, "https://fixture.test/comic/chapter-42?page=1")
    assert "https://fixture.test/uploads/comments/rel-sticker.png" in urls


def test_get_comment_metadata_returns_same_meta(fake_adapter: FakeSiteAdapter) -> None:
    comment = fake_adapter.find_comments(_fixture_html("sample_comment_page.html"))[0]
    meta = fake_adapter.get_comment_metadata(comment)
    assert isinstance(meta, CommentMeta)
    assert meta is comment.meta
    assert meta.comment_id == "918271"
