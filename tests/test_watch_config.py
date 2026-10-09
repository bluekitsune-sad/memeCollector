"""Config tests for the ``watch:`` section — dataclass defaults, YAML, env overrides (PRD §40)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config.loader import DEFAULT_CONFIG_PATH, WatchSettings, load_settings


def test_watch_dataclass_defaults() -> None:
    watch = WatchSettings()
    assert watch.enabled is True
    assert watch.interval_minutes == 60.0
    assert watch.max_pages_per_run == 20


def test_load_settings_exposes_the_watch_section_with_defaults() -> None:
    watch = load_settings().watch
    assert watch.enabled is True
    assert watch.interval_minutes == 60
    assert watch.max_pages_per_run == 20


def test_default_config_yaml_declares_the_watch_section() -> None:
    yaml_text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    assert "\nwatch:" in yaml_text


def test_watch_yaml_values_are_read(tmp_path: Path) -> None:
    config_file = tmp_path / "watch.yaml"
    config_file.write_text(
        "watch:\n  enabled: false\n  interval_minutes: 5\n  max_pages_per_run: 3\n",
        encoding="utf-8",
    )
    watch = load_settings(config_file).watch
    assert watch.enabled is False
    assert watch.interval_minutes == 5
    assert watch.max_pages_per_run == 3


def test_missing_config_file_keeps_watch_defaults(tmp_path: Path) -> None:
    watch = load_settings(tmp_path / "does_not_exist.yaml").watch
    assert watch.enabled is True
    assert watch.interval_minutes == 60.0
    assert watch.max_pages_per_run == 20


def test_watch_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEME_WATCH_ENABLED", "false")
    monkeypatch.setenv("MEME_WATCH_INTERVAL_MINUTES", "0.25")
    monkeypatch.setenv("MEME_WATCH_MAX_PAGES_PER_RUN", "7")
    watch = load_settings().watch
    assert watch.enabled is False
    assert watch.interval_minutes == 0.25
    assert watch.max_pages_per_run == 7
