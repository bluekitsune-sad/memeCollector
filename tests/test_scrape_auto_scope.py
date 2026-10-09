"""``scope="auto"`` tests — pure URL→scope inference table plus API acceptance (PRD §5.1).

``infer_scope`` must classify chapter-like paths (``/chapter-12``,
``/chapter/12``, ``/ch/3`` — the shapes used by the four MVP sites) as
``current_chapter`` and everything else as ``current_page``, while every
explicit scope value keeps behaving exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.routes_scraper import ScrapeRequest, infer_scope
from backend.config import Settings, load_settings
from backend.main import create_app

#: (url, expected scope) — chapter-like paths vs entry/title/listing pages.
SCOPE_TABLE = [
    # Spec examples.
    ("https://example.com/comic/chapter-12", "current_chapter"),
    ("https://example.com/comic/chapter/12", "current_chapter"),
    # The four MVP sites (AGENTS.md §5 / PRD §0).
    ("https://asurascans.com/comics/solo-leveling/chapter-42", "current_chapter"),
    ("https://asurascans.com/comics/solo-leveling/chapter/42?page=1", "current_chapter"),
    ("https://mangadex.org/chapter/0f90c7ef-0f70-4f2e-9e69-11f2f0d0f9c3/1", "current_chapter"),
    ("https://mangapark.net/title/solo-leveling/chapter-12", "current_chapter"),
    ("https://comix.to/title/solo-leveling/chapter-3", "current_chapter"),
    ("https://example.com/ch/3", "current_chapter"),
    # Entry / listing / title pages stay current_page.
    ("https://example.com", "current_page"),
    ("https://example.com/comic", "current_page"),
    ("https://example.com/chapter", "current_page"),
    ("https://example.com/chapters", "current_page"),
    ("https://asurascans.com/comics", "current_page"),
    ("https://asurascans.com/comics/solo-leveling", "current_page"),
    ("https://mangadex.org/title/96a73f84-6f17-4b17-9e05-2ad4d7b1c8a5", "current_page"),
    ("https://mangapark.net/title/solo-leveling", "current_page"),
    ("https://comix.to/comic/solo-leveling", "current_page"),
    # The fixture-site entry used across the test suite.
    ("https://fixture.test/comic/chapter-42?page=1", "current_chapter"),
    ("https://fixture.test/comic?page=1", "current_page"),
]


@pytest.mark.parametrize(("url", "expected"), SCOPE_TABLE)
def test_infer_scope_table(url: str, expected: str) -> None:
    assert infer_scope(url) == expected


def test_scrape_request_default_preserved_and_auto_accepted() -> None:
    assert ScrapeRequest(url="https://fixture.test/comic").scope == "current_page"
    assert ScrapeRequest(url="https://fixture.test/comic", scope="auto").scope == "auto"
    assert (
        ScrapeRequest(url="https://fixture.test/comic", scope="custom_urls", urls=["a"]).scope
        == "custom_urls"
    )
    with pytest.raises(ValidationError):
        ScrapeRequest(url="https://fixture.test/comic", scope="everything")


# ---------------------------------------------------------------------------
# End-to-end acceptance: the endpoint validates `auto` (422 would precede the
# site check) and still reports unknown sites with the standard message.
# ---------------------------------------------------------------------------


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    """Settings with database + storage inside the test's tmp dir; mock AI, fast crawler."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "auto_scope.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    crawler = replace(base.crawler, delay_seconds=0.0, concurrency=1)
    ai = replace(base.ai, provider="mock")
    return replace(base, storage=storage, crawler=crawler, ai=ai)


@pytest.fixture
def client(api_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


def test_scrape_endpoint_accepts_scope_auto(client: TestClient) -> None:
    # fixture.test has no registered adapter here, so the site check answers 400 —
    # proving scope="auto" passed request validation (a 422 would mean rejection).
    response = client.post(
        "/api/scrape", json={"url": "https://fixture.test/comic/chapter-42", "scope": "auto"}
    )
    assert response.status_code == 400
    assert "Site not supported: add an adapter for fixture.test" in response.json()["detail"]
    assert client.get("/api/jobs").json()["items"] == []
    assert client.get("/api/watch").json()["items"] == []
