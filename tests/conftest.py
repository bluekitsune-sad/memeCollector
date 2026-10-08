"""Shared pytest fixtures (M0.4): fresh DB, sample media, fixture HTML, mock AI provider.

Also provides the Milestone 1 scraper-test helpers: ``make_settings`` (fast,
offline config overrides) and ``fake_adapter`` (registers the fixture-site
adapter for the duration of one test, restoring the registry afterwards).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from backend.config import Settings, load_settings
from backend.database.database import get_connection
from backend.database.migrations import migrate
from tests.fixtures.fake_adapter import FakeSiteAdapter
from tests.mock_provider import MockVisionProvider

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

ImageFactory = Callable[..., Path]

#: Factory for overridden :class:`~backend.config.loader.Settings` (defaults keep tests offline/fast).
SettingsFactory = Callable[..., Settings]


@pytest.fixture
def fixtures_dir() -> Path:
    """Directory containing local fixture files (HTML pages, etc.)."""
    return FIXTURES_DIR


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path for a fresh, throwaway SQLite database inside the test's tmp dir."""
    return tmp_path / "memevault_test.sqlite"


@pytest.fixture
def db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open connection to a fresh database with the full schema applied."""
    conn = get_connection(db_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def make_image(tmp_path: Path) -> ImageFactory:
    """Factory: ``make_image(name, format, size=(32, 32), frames=1)`` writes a sample media file."""

    def _make(
        name: str,
        image_format: str,
        size: tuple[int, int] = (32, 32),
        frames: int = 1,
    ) -> Path:
        path = tmp_path / name
        if frames > 1:
            if image_format != "GIF":
                raise ValueError("multi-frame samples are only supported for GIF")
            layers = [
                Image.new("RGB", size, (20 + 60 * index, 40, 180 - 40 * index))
                for index in range(frames)
            ]
            layers[0].save(
                path,
                format=image_format,
                save_all=True,
                append_images=layers[1:],
                duration=120,
                loop=0,
            )
        else:
            Image.new("RGB", size, (200, 30, 30)).save(path, format=image_format)
        return path

    return _make


@pytest.fixture
def sample_images(make_image: ImageFactory) -> dict[str, Path]:
    """One small valid sample per supported format; the GIF has 3 frames."""
    return {
        "png": make_image("sample.png", "PNG"),
        "jpeg": make_image("sample.jpeg", "JPEG"),
        "gif": make_image("sample.gif", "GIF", frames=3),
        "webp": make_image("sample.webp", "WEBP"),
    }


@pytest.fixture
def sample_comment_html_path() -> Path:
    """Local fixture page with comment attachments plus page chrome that must be ignored."""
    return FIXTURES_DIR / "sample_comment_page.html"


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Factory for test settings: delay 0 (fast) unless overridden; optional media directory override."""

    def _make(*, media_directory: Path | None = None, **crawler_overrides: Any) -> Settings:
        settings = load_settings()
        crawler_values: dict[str, Any] = {"delay_seconds": 0.0, **crawler_overrides}
        crawler = replace(settings.crawler, **crawler_values)
        storage = settings.storage
        if media_directory is not None:
            storage = replace(settings.storage, media_directory=media_directory)
        return replace(settings, crawler=crawler, storage=storage)

    return _make


@pytest.fixture
def fake_adapter() -> Iterator[FakeSiteAdapter]:
    """Register the fixture-site adapter for one test, restoring the registry afterwards."""
    from backend.scraper.adapters import base

    saved = dict(base._REGISTRY)
    adapter = FakeSiteAdapter()
    base.register(adapter)
    try:
        yield adapter
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(saved)


@pytest.fixture
def mock_vision_provider() -> MockVisionProvider:
    """Deterministic mock AI provider — no API key, no network."""
    return MockVisionProvider()
