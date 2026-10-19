"""Smoke tests for the config loader (M0.2): YAML defaults, path resolution, env overrides."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.config.loader import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_settings


def test_loads_yaml_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite's session guard overrides this key — assert pure YAML here.
    monkeypatch.delenv("MEME_BACKFILL_TARGETS", raising=False)
    settings = load_settings()
    assert settings.config_path == DEFAULT_CONFIG_PATH
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8000
    assert settings.crawler.delay_seconds == 1.0
    assert settings.crawler.concurrency == 2
    assert settings.crawler.max_pages == 1000   # entire-comic crawls (backfill) stay uncapped in practice
    assert settings.crawler.download_limit == 5000
    assert settings.crawler.max_file_size_mb == 100
    assert settings.crawler.headless is True
    assert settings.crawler.debug is False
    assert settings.ai.provider == "openrouter"
    assert settings.ai.ai_concurrency == 2
    # Site-wide backfill section (PRD §36 long-running job).
    backfill = load_settings().backfill
    assert backfill.enabled is True
    assert backfill.delay_seconds == 2.0
    # Passive rotation of the three proven catalogs (asura + the M7 additions).
    assert backfill.targets == [
        "https://asurascans.com/comics",
        "https://en-thunderscans.com/comics/",
        "https://flamecomics.xyz/browse",
    ]


def test_search_weights_match_prd_22() -> None:
    weights = load_settings().search
    assert weights.keyword_weight == 0.25
    assert weights.semantic_weight == 0.50
    assert weights.tag_weight == 0.20
    assert weights.metadata_weight == 0.05
    total = (
        weights.keyword_weight
        + weights.semantic_weight
        + weights.tag_weight
        + weights.metadata_weight
    )
    assert round(total, 6) == 1.0


def test_storage_paths_resolve_under_project_root() -> None:
    storage = load_settings().storage
    assert storage.media_directory == PROJECT_ROOT / "data" / "media"
    assert storage.thumbnail_directory == PROJECT_ROOT / "data" / "thumbnails"
    assert storage.preview_directory == PROJECT_ROOT / "data" / "previews"
    assert storage.database_path == PROJECT_ROOT / "data" / "database.sqlite"


def test_yaml_file_values_are_read(tmp_path: Path) -> None:
    config_file = tmp_path / "custom.yaml"
    config_file.write_text(
        "server:\n  port: 9999\ncrawler:\n  delay_seconds: 3.0\n  max_pages: 5\n",
        encoding="utf-8",
    )
    settings = load_settings(config_file)
    assert settings.config_path == config_file
    assert settings.server.port == 9999
    assert settings.crawler.delay_seconds == 3.0
    assert settings.crawler.max_pages == 5
    # Sections absent from the file keep their built-in defaults.
    assert settings.search.semantic_weight == 0.50


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "does_not_exist.yaml")
    assert settings.crawler.concurrency == 2
    assert settings.storage.database_path == PROJECT_ROOT / "data" / "database.sqlite"


def test_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEME_CRAWLER_DELAY_SECONDS", "2.5")
    monkeypatch.setenv("MEME_CRAWLER_HEADLESS", "false")
    monkeypatch.setenv("AI_MODEL", "acme/vision-test")
    monkeypatch.setenv("MEME_PORT", "9001")
    settings = load_settings()
    assert settings.crawler.delay_seconds == 2.5
    assert settings.crawler.headless is False
    assert settings.ai.model == "acme/vision-test"
    assert settings.server.port == 9001
    # Untouched keys keep YAML values.
    assert settings.crawler.concurrency == 2


def test_backfill_targets_read_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEME_BACKFILL_TARGETS", raising=False)
    config_file = tmp_path / "custom.yaml"
    config_file.write_text(
        "backfill:\n"
        "  targets:\n"
        "    - https://a.example/comics\n"
        "    - https://b.example/browse\n",
        encoding="utf-8",
    )
    settings = load_settings(config_file)
    assert settings.backfill.targets == ["https://a.example/comics", "https://b.example/browse"]


def test_backfill_targets_default_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEME_BACKFILL_TARGETS", raising=False)
    config_file = tmp_path / "custom.yaml"
    config_file.write_text("server:\n  port: 8001\n", encoding="utf-8")
    assert load_settings(config_file).backfill.targets == []


def test_backfill_targets_env_override_splits_commas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MEME_BACKFILL_TARGETS", "https://a.example/comics, https://b.example/browse"
    )
    settings = load_settings()
    assert settings.backfill.targets == ["https://a.example/comics", "https://b.example/browse"]


def test_api_key_never_stored_in_yaml() -> None:
    yaml_text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    assert re.search(r"^\s*api_key\s*:", yaml_text, re.MULTILINE) is None


def test_api_key_only_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    assert load_settings().ai.api_key == "sk-or-test-key"
    # Empty value (e.g. the blank .env.example entry) means "unset".
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert load_settings().ai.api_key is None
