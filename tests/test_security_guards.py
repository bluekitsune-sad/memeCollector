"""Choke-point tests for the booby-trap guards (PRD §36, §41; AGENTS.md §6, §9).

Each test exercises a *real* guard location — the downloader stream, the crawler
fetchers, the adapters' default fetch helpers, comment-metadata storage — with
``httpx.MockTransport`` / monkeypatched DNS only (no network, no live site).
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.scraper.adapters import CrawlScope, ScopeKind
from backend.scraper.adapters import asurascans as asurascans_adapter
from backend.scraper.adapters import comix as comix_adapter
from backend.scraper.adapters import mangadex as mangadex_adapter
from backend.scraper.adapters import mangapark as mangapark_adapter
from backend.scraper.adapters.base import CommentMeta
from backend.scraper.crawler import (
    Crawler,
    HttpxFetcher,
    PageFetchError,
    PlaywrightFetcher,
)
from backend.scraper.downloader import DownloadStatus, Downloader
from backend.security import urls as urls_module
from backend.security.fetch import guarded_get
from backend.security.urls import MAX_REDIRECTS, UnsafeURLError
from tests.test_downloader import CDN, make_client, media_ref

GUARD_LOGGER = "memevault.security.urls"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every lookup with a public IP so guard tests stay offline and fast."""

    def getaddrinfo(host: str, port: object = None, *args: object, **kwargs: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(
        urls_module,
        "socket",
        SimpleNamespace(getaddrinfo=getaddrinfo, gaierror=socket.gaierror),
    )


def _make_downloader(destination: Path, client: httpx.AsyncClient, make_settings) -> Downloader:
    return Downloader(destination_dir=destination, settings=make_settings(), client=client)


# ---------------------------------------------------------------------------
# Downloader (STORE choke point): block before the stream, per redirect hop.
# ---------------------------------------------------------------------------


async def test_downloader_blocks_metadata_address_without_touching_network(
    tmp_path, make_settings
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"png", headers={"content-type": "image/png"})

    client = make_client(handler)
    downloader = _make_downloader(tmp_path / "media", client, make_settings)
    try:
        result = await downloader.download(
            media_ref("http://169.254.169.254/latest/meta-data/")
        )
    finally:
        await client.aclose()

    assert result.status is DownloadStatus.FAILED
    assert result.error is not None and "unsafe url" in result.error
    assert "failed after" not in result.error  # a block is permanent, never retried
    assert calls == []


async def test_downloader_refuses_redirect_to_private_address(
    tmp_path, make_settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=GUARD_LOGGER)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret.png"})

    client = make_client(handler)
    downloader = _make_downloader(tmp_path / "media", client, make_settings)
    try:
        result = await downloader.download(media_ref(f"{CDN}/lure.png"))
    finally:
        await client.aclose()

    assert result.status is DownloadStatus.FAILED
    assert result.error is not None and "unsafe url" in result.error
    assert len(calls) == 1  # the private hop never received a request
    assert "url blocked" in caplog.text
    assert "reason=" in caplog.text and "url=http://127.0.0.1/secret.png" in caplog.text


# ---------------------------------------------------------------------------
# Crawler fetchers (COLLECT choke point).
# ---------------------------------------------------------------------------


async def test_http_fetcher_rejects_dangerous_scheme_before_any_request(
    make_settings,
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="<html>ok</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(settings=make_settings(), client=client)
    try:
        with pytest.raises(PageFetchError, match="unsafe url blocked"):
            await fetcher.fetch("file:///etc/passwd")
    finally:
        await client.aclose()
    assert calls == []


async def test_http_fetcher_blocks_redirect_to_private_address(make_settings) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://10.0.0.9/loot"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(settings=make_settings(), client=client)
    try:
        with pytest.raises(PageFetchError, match="unsafe url blocked"):
            await fetcher.fetch("https://fixture.test/start")
    finally:
        await client.aclose()
    assert len(calls) == 1  # first hop allowed, private hop rejected before request


async def test_http_fetcher_follows_safe_redirects(make_settings) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, text="<html>ok</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(settings=make_settings(), client=client)
    try:
        html = await fetcher.fetch("https://fixture.test/start")
    finally:
        await client.aclose()
    assert html == "<html>ok</html>"
    assert calls == ["/start", "/final"]


async def test_http_fetcher_gives_up_on_redirect_loop(make_settings) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(302, headers={"location": "/again"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(settings=make_settings(), client=client)
    try:
        with pytest.raises(PageFetchError, match="redirect limit exceeded"):
            await fetcher.fetch("https://fixture.test/loop")
    finally:
        await client.aclose()
    assert len(calls) == MAX_REDIRECTS + 1


async def test_playwright_fetcher_rejects_hostile_url_before_browser_launch(
    make_settings,
) -> None:
    """The guard must fire before ``start()`` — no Chromium needed to block a URL."""
    fetcher = PlaywrightFetcher(settings=make_settings())
    try:
        with pytest.raises(PageFetchError, match="unsafe url blocked"):
            await fetcher.fetch("javascript:alert(document.cookie)")
    finally:
        await fetcher.aclose()
    assert fetcher._browser is None  # nothing launched


async def test_unsafe_page_url_is_recorded_as_failure_not_a_crash(
    fake_adapter, make_settings, caplog: pytest.LogCaptureFixture
) -> None:
    """PRD §36: a hostile page URL becomes a per-page failure, not a dead crawl."""
    caplog.set_level(logging.WARNING, logger=GUARD_LOGGER)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="<html></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(settings=make_settings(), client=client)
    scope = CrawlScope(ScopeKind.CUSTOM_URLS, urls=("file:///etc/passwd",))
    try:
        result = await Crawler(settings=make_settings(), fetcher=fetcher).crawl(
            "https://fixture.test/entry", scope
        )
    finally:
        await client.aclose()

    assert result.media == []
    assert result.progress.pages_failed == 1
    assert len(result.failures) == 1
    assert "unsafe url blocked" in result.failures[0].reason
    assert calls == []
    assert "url blocked" in caplog.text


# ---------------------------------------------------------------------------
# Adapter default fetch helpers (COLLECT discovery choke point).
# ---------------------------------------------------------------------------


def test_adapter_default_fetch_helpers_are_guarded() -> None:
    """Every adapter's no-injection fetch path rejects hostile URLs before I/O."""
    with pytest.raises(UnsafeURLError):
        asurascans_adapter._default_fetch_html("file:///etc/passwd")
    with pytest.raises(UnsafeURLError):
        mangapark_adapter._default_fetch_html("ftp://evil.example/payload")
    with pytest.raises(UnsafeURLError):
        comix_adapter._default_fetch("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(UnsafeURLError):
        comix_adapter._default_fetch_json("http://[::1]/secrets")
    with pytest.raises(UnsafeURLError):
        mangadex_adapter._get("javascript:alert(1)")


def test_guarded_get_revalidates_every_redirect_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(str(url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/steal"})

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(UnsafeURLError) as excinfo:
        guarded_get("https://guard-hop.test/start")
    assert len(calls) == 1
    assert excinfo.value.url.startswith("http://169.254.169.254/")


def test_guarded_get_follows_safe_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/start"):
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, text="done")

    monkeypatch.setattr(httpx, "get", fake_get)
    response = guarded_get("https://guard-follow.test/start")
    assert response.status_code == 200
    assert response.text == "done"


def test_adapter_discovery_survives_a_blocked_fetch() -> None:
    """A guard hit during discovery falls back to the entry page (never raises out)."""

    def blocked(url: str) -> str:
        raise UnsafeURLError(url, "blocked scheme file")

    adapter = asurascans_adapter.AsuraScansAdapter(fetch_html=blocked)
    entry = "https://asurascans.com/comics/solo-leveling/chapter-9"
    refs = adapter.discover_pages(entry, CrawlScope(ScopeKind.ENTIRE_COMIC))
    assert [ref.url for ref in refs] == [entry]


# ---------------------------------------------------------------------------
# Storage choke point: comment metadata is sanitized on construction.
# ---------------------------------------------------------------------------


def test_comment_meta_sanitizes_untrusted_fields() -> None:
    meta = CommentMeta(
        comment_id="c1\u200b\x07",
        author_name="evil\u202ename\u00a0",
        text="hello\u200b world\u202e!",
    )
    assert meta.comment_id == "c1"
    assert meta.author_name == "evilname"
    assert meta.text == "hello world!"


def test_comment_meta_keeps_ordinary_text_and_none() -> None:
    meta = CommentMeta(
        comment_id="42",
        author_name="reader",
        text="First! \U0001F602\nsecond line",
    )
    assert meta.author_name == "reader"
    assert meta.text == "First! \U0001F602\nsecond line"

    empty = CommentMeta(comment_id="7")
    assert empty.author_name is None
    assert empty.text is None
