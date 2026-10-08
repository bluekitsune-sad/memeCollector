"""Settings API tests — ``GET``/``PATCH /api/settings`` (PRD §40, §41, §42, §43).

Offline and non-destructive: the config file is copied into the test's tmp dir
(``settings.config_path``), so a ``PATCH`` round-trips through
``yaml.safe_load``/``safe_dump`` in the copy and never rewrites the repo's
``config/config.yaml``.

Pinned behavior: the exact GET envelope the frontend renders, no secret ever
leaves or enters through the API (``extra="forbid"``), crawl/provider/range
validation, and the merged search weights summing to 1.0.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.api.routes_settings import SettingsResponse
from backend.config import Settings, load_settings
from backend.config.loader import DEFAULT_CONFIG_PATH
from backend.main import create_app

#: Exact top-level contract keys (frontend ``SettingsResponse``).
RESPONSE_KEYS = {"server", "storage", "crawler", "ai", "search", "notices"}


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    """A private copy of ``config/config.yaml`` used as the app's ``config_path``."""
    path = tmp_path / "config.yaml"
    shutil.copy(DEFAULT_CONFIG_PATH, path)
    return path


@pytest.fixture
def api_settings(config_copy: Path, tmp_path: Path) -> Settings:
    """Settings pointing at the config copy + local storage + offline mock AI.

    ``api_key=None`` pins the keyless state so the ``key_present`` assertions
    hold even when a developer's ``.env.local`` carries a real key (secrets are
    never read from YAML, and none may leak through the API).
    """
    base = load_settings(config_copy)
    storage = replace(
        base.storage,
        database_path=tmp_path / "settings.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    return replace(base, storage=storage, ai=replace(base.ai, provider="mock", api_key=None))


@pytest.fixture
def client(api_settings: Settings):
    """TestClient with lifespan applied; the PATCHes below write only to tmp files."""
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


def _get(client: TestClient) -> dict:
    response = client.get("/api/settings")
    assert response.status_code == 200, response.text
    return response.json()


def _patch(client: TestClient, body: dict):
    return client.patch("/api/settings", json=body)


def _saved_yaml(config_copy: Path) -> dict:
    return yaml.safe_load(config_copy.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# GET contract
# ---------------------------------------------------------------------------


def test_get_settings_returns_the_exact_documented_shape(
    client: TestClient, api_settings: Settings
) -> None:
    payload = _get(client)
    SettingsResponse.model_validate(payload)

    assert set(payload) == RESPONSE_KEYS
    assert set(payload["server"]) == {"host", "port"}
    assert set(payload["storage"]) == {
        "media_directory",
        "thumbnail_directory",
        "preview_directory",
        "database_path",
    }
    crawler = payload["crawler"]
    for key in ("delay_seconds", "concurrency", "max_pages", "download_limit", "max_file_size_mb"):
        assert key in crawler
    assert set(payload["ai"]) == {
        "provider",
        "model",
        "embedding_model",
        "key_present",
        "external_provider",
    }
    assert set(payload["search"]) == {
        "keyword_weight",
        "semantic_weight",
        "tag_weight",
        "metadata_weight",
    }
    assert set(payload["notices"]) == {"privacy", "copyright"}
    assert payload["notices"]["privacy"].strip()
    assert payload["notices"]["copyright"].strip()
    assert payload["storage"]["media_directory"] == str(api_settings.storage.media_directory)


def test_get_settings_never_exposes_the_api_key(client: TestClient, api_settings: Settings) -> None:
    response = client.get("/api/settings")

    assert "api_key" not in response.text
    assert response.json()["ai"]["key_present"] is bool(api_settings.ai.api_key)
    if api_settings.ai.api_key:
        assert api_settings.ai.api_key not in response.text


def test_mock_provider_reports_staying_local(client: TestClient) -> None:
    payload = _get(client)

    assert payload["ai"]["provider"] == "mock"
    assert payload["ai"]["external_provider"] is False


# ---------------------------------------------------------------------------
# PATCH: apply + persist
# ---------------------------------------------------------------------------


def test_patch_crawler_applies_in_memory_and_persists_to_yaml(
    client: TestClient, config_copy: Path
) -> None:
    before = _saved_yaml(config_copy)

    response = _patch(client, {"crawler": {"delay_seconds": 2.5, "concurrency": 1}})
    assert response.status_code == 200, response.text

    payload = response.json()
    assert payload["crawler"]["delay_seconds"] == 2.5
    assert payload["crawler"]["concurrency"] == 1
    # PATCH returns the full document the frontend swaps in.
    assert set(payload) == RESPONSE_KEYS
    assert _get(client)["crawler"]["delay_seconds"] == 2.5
    # The running app serves the new value immediately.
    assert client.app.state.settings.crawler.delay_seconds == 2.5

    after = _saved_yaml(config_copy)
    assert after["crawler"]["delay_seconds"] == 2.5
    # Only the edited keys move; everything else survives the round-trip.
    assert after["crawler"]["max_pages"] == before["crawler"]["max_pages"]
    assert after["server"] == before["server"]
    assert after["storage"] == before["storage"]
    assert after["search"] == before["search"]


def test_patch_ai_block_persists_and_flips_external_provider(
    client: TestClient, config_copy: Path
) -> None:
    response = _patch(
        client,
        {
            "ai": {
                "provider": "openrouter",
                "model": "acme/vision-1",
                "embedding_model": "acme/embed-1",
            }
        },
    )
    assert response.status_code == 200, response.text

    ai = response.json()["ai"]
    assert ai["provider"] == "openrouter"
    assert ai["model"] == "acme/vision-1"
    assert ai["embedding_model"] == "acme/embed-1"
    assert ai["external_provider"] is True  # analysis now leaves this machine (§42)
    assert ai["key_present"] is False

    saved = _saved_yaml(config_copy)
    assert saved["ai"]["model"] == "acme/vision-1"
    assert "api_key" not in saved["ai"]  # secrets are never written to YAML (§41)

    # Provider names are normalized, not rejected, for case/whitespace variants.
    normalized = _patch(client, {"ai": {"provider": "  MOCK "}})
    assert normalized.status_code == 200
    assert normalized.json()["ai"]["provider"] == "mock"
    assert normalized.json()["ai"]["external_provider"] is False


def test_patch_search_weights_must_sum_to_one(client: TestClient) -> None:
    before = _get(client)["search"]

    valid = _patch(
        client,
        {"search": {"keyword_weight": 0.4, "semantic_weight": 0.4,
                    "tag_weight": 0.15, "metadata_weight": 0.05}},
    )
    assert valid.status_code == 200, valid.text
    assert sum(valid.json()["search"].values()) == pytest.approx(1.0)

    # Partial patch that breaks the merged sum → 422, settings untouched.
    invalid = _patch(client, {"search": {"semantic_weight": 0.9}})
    assert invalid.status_code == 422
    assert _get(client)["search"] == valid.json()["search"]
    assert before["keyword_weight"] == 0.25  # documented default never applied


# ---------------------------------------------------------------------------
# PATCH: rejection paths
# ---------------------------------------------------------------------------


def test_patch_rejects_secrets_and_non_editable_sections(client: TestClient, config_copy: Path) -> None:
    raw_before = config_copy.read_text(encoding="utf-8")

    assert _patch(client, {"ai": {"api_key": "sk-evil-secret"}}).status_code == 422
    assert _patch(client, {"server": {"host": "0.0.0.0"}}).status_code == 422
    assert _patch(client, {"storage": {"database_path": "/etc/passwd"}}).status_code == 422
    assert _patch(client, {"crawler": {"nonsense": 1}}).status_code == 422
    assert _patch(client, {}).status_code == 422  # nothing to apply
    assert _patch(client, {"crawler": {}}).status_code == 422

    # Nothing was persisted, no secret reached disk or the response.
    assert config_copy.read_text(encoding="utf-8") == raw_before
    assert "sk-evil-secret" not in client.get("/api/settings").text
    assert _get(client)["server"]["host"] == "127.0.0.1"  # localhost bind intact (§41)


def test_patch_rejects_out_of_range_and_unknown_values(client: TestClient) -> None:
    bad_bodies = (
        {"crawler": {"delay_seconds": -1}},
        {"crawler": {"delay_seconds": 61}},
        {"crawler": {"concurrency": 0}},
        {"crawler": {"concurrency": 9}},
        {"crawler": {"max_file_size_mb": 0}},
        {"crawler": {"max_pages": -5}},
        {"ai": {"provider": "deepseek"}},
        {"ai": {"model": ""}},
        {"ai": {"embedding_model": "   "}},
        {"search": {"keyword_weight": 1.5}},
        {"search": {"semantic_weight": -0.1}},
    )
    for body in bad_bodies:
        response = _patch(client, body)
        assert response.status_code == 422, f"{body} → {response.status_code}: {response.text}"


def test_patch_crawler_leaves_every_other_section_intact(
    client: TestClient, config_copy: Path
) -> None:
    """Saving one section never rewrites the others (or sneaks secrets into YAML)."""
    before = _saved_yaml(config_copy)

    assert _patch(client, {"crawler": {"download_limit": 42}}).status_code == 200

    after = _saved_yaml(config_copy)
    assert after["crawler"]["download_limit"] == 42
    assert before["server"] == after["server"]
    assert before["storage"] == after["storage"]
    assert before["ai"] == after["ai"]
    assert before["search"] == after["search"]
    assert "api_key" not in after["ai"]
