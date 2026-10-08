"""``GET /api/search`` contract tests — the exact envelope the frontend is coded against.

Offline end-to-end over the real app: rows are seeded directly into the
app's database, the configured provider is the deterministic mock (no key, no
network), and the assertions pin the response shape byte-for-byte:

``{items[], page, page_size, total, mode, weights}`` where every item is the
gallery card (``MediaOut``) plus ``description`` / ``tags`` / ``score``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.ai.mock import MockVisionProvider
from backend.config import Settings, load_settings
from backend.api.routes_media import MediaOut
from backend.api.routes_search import SearchResponse, SearchWeightsOut
from backend.main import create_app
from tests.test_search import _put_embedding, _seed

#: The exact top-level contract keys (drift here breaks the search view).
RESPONSE_KEYS = {"items", "page", "page_size", "total", "mode", "weights"}

#: Search items = gallery card + the three ranking fields.
ITEM_KEYS = set(MediaOut.model_fields) | {"description", "tags", "score"}


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    """Search-API settings: local storage + ``ai.provider=mock`` (offline, deterministic)."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "search_api.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    crawler = replace(base.crawler, delay_seconds=0.0, concurrency=1)
    ai = replace(base.ai, provider="mock")
    return replace(base, storage=storage, crawler=crawler, ai=ai)


@pytest.fixture
def client(api_settings: Settings):
    """TestClient with lifespan applied; binds no port (in-process only)."""
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


def _search(client: TestClient, **params) -> dict:
    response = client.get("/api/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _seed_analyzed(client: TestClient, **kwargs) -> int:
    """One READY row with AI description/tags through the shared search seed helper."""
    kwargs.setdefault("description", "generic meme description")
    kwargs.setdefault("tags", ("reaction",))
    return _seed(client.app.state.db, **kwargs)


# ---------------------------------------------------------------------------
# Envelope + item shape
# ---------------------------------------------------------------------------


def test_search_envelope_and_item_keys_are_exact(client: TestClient) -> None:
    payload = _search(client)

    assert set(payload) == RESPONSE_KEYS
    assert payload["mode"] == "filters_only"
    assert set(payload["weights"]) == set(SearchWeightsOut.model_fields)
    assert sum(payload["weights"].values()) == pytest.approx(1.0)
    assert payload["page"] == 1 and payload["page_size"] == 50

    _seed_analyzed(client)
    item = _search(client)["items"][0]
    assert set(item) == ITEM_KEYS
    # Contract items expose the AI fields the search card renders.
    assert item["description"] == "generic meme description"
    assert item["tags"] == ["reaction"]
    assert item["score"] is None  # filters_only mode is unranked
    # No storage path ever leaks into a search item.
    assert "file_path" not in item


def test_search_validates_against_the_response_model(client: TestClient) -> None:
    """What we return parses as the documented model (guards silent drift)."""
    payload = _search(client, q="")
    SearchResponse.model_validate(payload)


# ---------------------------------------------------------------------------
# Ranking behavior over HTTP
# ---------------------------------------------------------------------------


def test_search_q_returns_hybrid_mode_with_scores(client: TestClient) -> None:
    provider = MockVisionProvider()
    query = "confused cat reaction"
    target = _seed_analyzed(client, description="pixel feline stares", tags=("confused",))
    other = _seed_analyzed(client, description="spreadsheet budget memo", tags=("office",))
    _put_embedding(
        client.app.state.db, target, asyncio.run(provider.generate_embedding(query))
    )
    _put_embedding(
        client.app.state.db, other, asyncio.run(provider.generate_embedding("quarterly budget memo"))
    )

    payload = _search(client, q=query)

    assert payload["mode"] == "hybrid"
    assert payload["total"] == 2
    top = payload["items"][0]
    assert top["id"] == target
    assert isinstance(top["score"], float) and 0.0 <= top["score"] <= 1.0
    assert {item["id"] for item in payload["items"]} == {target, other}


def test_search_without_embeddings_degrades_to_keyword_only(client: TestClient) -> None:
    """mock provider but zero stored vectors → keyword_only, still 200 (PRD §36)."""
    row = _seed_analyzed(client, description="a startled banana stand")

    payload = _search(client, q="startled")

    assert payload["mode"] == "keyword_only"
    assert [item["id"] for item in payload["items"]] == [row]
    assert payload["weights"]["semantic"] == 0.0
    assert sum(payload["weights"].values()) == pytest.approx(1.0)


def test_search_empty_query_is_filters_only_newest_first(client: TestClient) -> None:
    db = client.app.state.db
    old = _seed(db, created_at="2026-01-01 00:00:00")
    new = _seed(db, created_at="2026-01-09 00:00:00")

    payload = _search(client, q="")

    assert payload["mode"] == "filters_only"
    assert [item["id"] for item in payload["items"]] == [new, old]
    assert all(item["score"] is None for item in payload["items"])


def test_search_adversarial_query_returns_empty_page(client: TestClient) -> None:
    _seed_analyzed(client)

    payload = _search(client, q="foo OR (bar")

    assert payload["total"] == 0
    assert payload["items"] == []
    assert payload["mode"] in ("hybrid", "keyword_only")


# ---------------------------------------------------------------------------
# Filters + pagination + validation
# ---------------------------------------------------------------------------


def test_search_filters_narrow_results_like_the_gallery(client: TestClient) -> None:
    db = client.app.state.db
    joy = _seed(db, description="shared text", emotions=("joy",), extension="png")
    _seed(db, description="shared text", emotions=("sadness",), extension="gif")

    def ids(**params) -> set[int]:
        return {item["id"] for item in _search(client, q="shared", **params)["items"]}

    assert ids(emotion="JOY") == {joy}
    assert ids(type="gif") == {2}
    assert ids(dup_status="nondup") == {1, 2}
    assert ids(site="asurascans") == {1, 2}
    assert ids(format="png") == {joy}
    # filters_only runs honor the same filters.
    filtered = _search(client, q="", emotion="joy")
    assert [item["id"] for item in filtered["items"]] == [joy]


def test_search_pagination_slices_ranked_results(client: TestClient) -> None:
    for index in range(3):
        _seed_analyzed(client, description=f"meme number {index}")

    page_one = _search(client, q="meme", page=1, page_size=2)
    page_two = _search(client, q="meme", page=2, page_size=2)

    assert page_one["total"] == page_two["total"] == 3
    assert len(page_one["items"]) == 2 and len(page_two["items"]) == 1
    seen = [item["id"] for item in page_one["items"] + page_two["items"]]
    assert len(set(seen)) == 3  # no overlap, no gaps


def test_search_rejects_invalid_query_values(client: TestClient) -> None:
    assert client.get("/api/search", params={"type": "audio"}).status_code == 422
    assert client.get("/api/search", params={"dup_status": "maybe"}).status_code == 422
    assert client.get(
        "/api/search", params={"processing_status": "DONE"}
    ).status_code == 422
    assert client.get("/api/search", params={"date_from": "not-a-date"}).status_code == 422
    assert client.get("/api/search", params={"page": 0}).status_code == 422
    assert client.get("/api/search", params={"page_size": 0}).status_code == 422
    assert client.get("/api/search", params={"page_size": 500}).status_code == 422
    assert client.get("/api/search", params={"q": "x" * 501}).status_code == 422


def test_search_serializes_ai_tag_json_safely(client: TestClient) -> None:
    """Malformed/odd tag JSON never 500s the endpoint."""
    db = client.app.state.db
    row = _seed_analyzed(client, description="weird metadata row")
    with db:
        db.execute(
            "INSERT OR REPLACE INTO ai_metadata (media_id, description, tags) VALUES (?, ?, ?)",
            (row, "weird metadata row", json.dumps(["ok", ""])),
        )

    payload = _search(client, q="weird")

    assert payload["total"] == 1
    assert payload["items"][0]["tags"] == ["ok", ""]
