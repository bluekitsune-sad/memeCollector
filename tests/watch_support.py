"""Shared offline helpers for the watch test modules (``tests/test_watch_*``).

Provides:

* :func:`listing_adapter` / :func:`registered` — a ``fixture.test`` adapter
  whose ``ENTIRE_COMIC`` discovery is driven by local listing HTML, wired into
  the adapter registry for the duration of a block (mirrors the conftest
  ``fake_adapter`` fixture, but parameterizable per test).
* :data:`EMPTY_LISTING_HTML` — a listing with no chapter links, so a watch scan
  discovers zero pages and never fetches anything (fully offline).
* :func:`chapter_listing` / :func:`chapter_page_urls` — build listings and the
  exact page URLs :class:`~tests.fixtures.fake_adapter.FakeSiteAdapter` will
  discover from them, so tests can assert precisely which pages were fetched.
* :func:`watch_settings` / :func:`state_app` — tmp-storage settings (mock AI,
  zero delay) and a duck-typed ``app`` stand-in: the pipeline/watch code only
  touches ``app.state``, so no FastAPI lifespan is needed for unit tests.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin

from backend.config import Settings, load_settings
from tests.fixtures.fake_adapter import FakeSiteAdapter

#: Listing HTML with no chapter links: ENTIRE_COMIC discovery yields zero pages.
EMPTY_LISTING_HTML = "<html><body><!-- no chapters --></body></html>"

#: Entry URL the watch tests watch (handled by the fixture-site adapter).
ENTRY_URL = "https://fixture.test/comic"


def chapter_listing(*chapters: tuple[str, int]) -> str:
    """Build listing HTML: one chapter link per ``(href, page_count)`` tuple."""
    links = "".join(
        f'<a class="chapter-link" href="{href}" data-pages="{page_count}"></a>'
        for href, page_count in chapters
    )
    return f"<html><body>{links}</body></html>"


def chapter_page_urls(entry_url: str, href: str, page_count: int) -> list[str]:
    """The exact page URLs the fake adapter discovers for one listing link."""
    chapter_url = urljoin(entry_url, href)
    return [f"{chapter_url}?page={number}" for number in range(1, page_count + 1)]


def listing_adapter(listing_html: str) -> FakeSiteAdapter:
    """A ``fixture.test`` adapter whose ENTIRE_COMIC discovery reads ``listing_html``."""
    return FakeSiteAdapter(listing_html=listing_html)


@contextlib.contextmanager
def registered(adapter: FakeSiteAdapter) -> Iterator[FakeSiteAdapter]:
    """Register ``adapter`` for the block, restoring the registry afterwards."""
    from backend.scraper.adapters import base

    saved = dict(base._REGISTRY)
    base.register(adapter)
    try:
        yield adapter
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(saved)


def watch_settings(tmp_path: Path, **watch_overrides: Any) -> Settings:
    """Offline settings: tmp storage, zero delay, mock AI, plus ``watch.*`` overrides."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "watch.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    crawler = replace(base.crawler, delay_seconds=0.0, concurrency=1)
    ai = replace(base.ai, provider="mock")
    watch = replace(base.watch, **watch_overrides)
    return replace(base, storage=storage, crawler=crawler, ai=ai, watch=watch)


def state_app(db: sqlite3.Connection, settings: Settings) -> SimpleNamespace:
    """Duck-typed ``app`` stand-in exposing everything the pipeline/watch touch."""
    return SimpleNamespace(
        state=SimpleNamespace(
            db=db,
            settings=settings,
            running_crawls={},
            ai_queue_running=False,
        )
    )
