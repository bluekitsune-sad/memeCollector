"""Configuration loading for MemeCollector.

Resolution order (highest priority first):

1. Environment variables — generic ``MEME_<SECTION>_<KEY>`` (e.g.
   ``MEME_CRAWLER_DELAY_SECONDS``) plus the documented aliases
   ``OPENROUTER_API_KEY``, ``AI_MODEL``, ``EMBEDDING_MODEL``, ``MEME_HOST``,
   ``MEME_PORT``, ``MEME_CONFIG_PATH``.
2. YAML file — ``config/config.yaml`` by default, ``MEME_CONFIG_PATH`` to point
   at an alternate file.
3. Dataclass defaults declared in this module.

Relative paths from YAML or env resolve against the project root. The AI API key
is read from the environment only and is never stored in YAML (PRD §40–41);
empty environment variables are treated as unset.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "config.yaml"

# Environment variable aliases: env name -> (section, key).
_ENV_ALIASES: dict[str, tuple[str, str]] = {
    "OPENROUTER_API_KEY": ("ai", "api_key"),
    "AI_MODEL": ("ai", "model"),
    "EMBEDDING_MODEL": ("ai", "embedding_model"),
    "MEME_HOST": ("server", "host"),
    "MEME_PORT": ("server", "port"),
}


@dataclass(frozen=True)
class ServerSettings:
    """Local bind address — localhost only (PRD §41)."""

    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True)
class StorageSettings:
    """Runtime artifact locations, all under ``data/`` (AGENTS.md §3)."""

    media_directory: Path = Path("data/media")
    thumbnail_directory: Path = Path("data/thumbnails")
    preview_directory: Path = Path("data/previews")
    database_path: Path = Path("data/database.sqlite")


@dataclass(frozen=True)
class CrawlerSettings:
    """Crawl rate limits and browser flags (PRD §10, §37; AGENTS.md §9)."""

    delay_seconds: float = 1.0
    concurrency: int = 2
    max_pages: int = 100
    download_limit: int = 500
    max_file_size_mb: int = 100
    headless: bool = True
    debug: bool = False
    retry_attempts: int = 3
    request_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class AISettings:
    """AI provider settings; ``api_key`` comes from the environment only.

    The retry fields govern the *item-level* budget of the background queue
    (PRD §18/§36): transient failures are deferred with exponential backoff
    (``retry_interval_seconds`` doubling up to ``retry_interval_max_seconds``)
    until ``max_item_attempts`` is spent; ``supervisor_poll_seconds`` is the
    idle wake interval of the AI supervisor loop.

    ``revive_after_seconds`` delays the automatic **second try** of a terminally
    failed item whose failure was transient (429/5xx/timeout/malformed): the
    supervisor flips it back into the queue with a fresh ``max_item_attempts``
    budget once the wait elapses. ``max_item_revives`` caps how many such
    automatic revivals one item may use, so a deterministic-but-retryable-looking
    failure cannot cycle forever; permanent failures (video, bad key, unknown
    model) are never revived, and a manual reanalyze resets the counter.
    """

    provider: str = "openrouter"
    model: str = "dots-studio/dots-3-note-preview:free"
    embedding_model: str = "nvidia/nemotron-3-embed-1b:free"
    ai_concurrency: int = 2
    api_key: str | None = None
    timeout_seconds: float = 60.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    mock_embedding_dim: int = 384
    max_item_attempts: int = 6
    retry_interval_seconds: float = 15.0
    retry_interval_max_seconds: float = 600.0
    supervisor_poll_seconds: float = 5.0
    revive_after_seconds: float = 1800.0
    max_item_revives: int = 3


@dataclass(frozen=True)
class SearchSettings:
    """Hybrid ranking weights (PRD §22): 0.25 keyword + 0.50 semantic + 0.20 tag + 0.05 metadata."""

    keyword_weight: float = 0.25
    semantic_weight: float = 0.50
    tag_weight: float = 0.20
    metadata_weight: float = 0.05


@dataclass(frozen=True)
class WatchSettings:
    """Passive background watcher (PRD §39): rescan watched comics on an interval.

    ``interval_minutes`` is a float so a fractional value can be injected in
    tests (and by ``MEME_WATCH_INTERVAL_MINUTES``); every crawl inside a pass
    still honours ``crawler.delay_seconds`` / ``crawler.concurrency`` (AGENTS.md
    §9) and is bounded by ``max_pages_per_run`` pages per comic.
    """

    enabled: bool = True
    interval_minutes: float = 60.0
    max_pages_per_run: int = 20


@dataclass(frozen=True)
class BackfillSettings:
    """Site-wide backfill (backend/jobs/backfill.py): crawl a whole catalog one comic at a time.

    ``enabled`` gates both ``POST /api/backfill/start`` and the startup resume
    of an interrupted backfill; ``delay_seconds`` is the pause between comics
    (on top of each crawl's own ``crawler.delay_seconds``), keeping the pass
    inside the AGENTS.md §9 crawl-rate limits.
    """

    enabled: bool = True
    delay_seconds: float = 2.0


@dataclass(frozen=True)
class Settings:
    """Fully resolved application settings."""

    server: ServerSettings
    storage: StorageSettings
    crawler: CrawlerSettings
    ai: AISettings
    search: SearchSettings
    watch: WatchSettings
    backfill: BackfillSettings
    config_path: Path


def _coerce(value: Any, reference: Any) -> Any:
    """Convert ``value`` to the type of ``reference``; resolve relative paths against the project root."""
    if isinstance(reference, Path):
        path = Path(str(value))
        return path if path.is_absolute() else PROJECT_ROOT / path
    if isinstance(reference, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"cannot parse boolean from {value!r}")
    if isinstance(reference, int):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return str(value)


def _build_section(section_cls: type, data: Any, section_name: str) -> Any:
    """Instantiate a settings section from YAML data, coercing types against field defaults."""
    if not isinstance(data, dict):
        raise ValueError(f"config section {section_name!r} must be a mapping, got {type(data).__name__}")
    field_map = {field.name: field for field in fields(section_cls)}
    for key in data:
        if key not in field_map:
            logger.warning("unknown config key ignored section=%s key=%s", section_name, key)
    values: dict[str, Any] = {}
    for field in field_map.values():
        if field.name in data:
            if section_name == "ai" and field.name == "api_key":
                logger.warning("config key ignored: set OPENROUTER_API_KEY in .env instead of YAML")
                continue
            values[field.name] = _coerce(data[field.name], field.default)
        elif field.default is not None:
            values[field.name] = _coerce(field.default, field.default)
    return section_cls(**values)


def _env_overrides(section: Any, section_name: str) -> Any:
    """Apply ``MEME_<SECTION>_<KEY>`` env vars (then alias vars) to a section; returns the updated section."""
    current = section
    for field in fields(section):
        raw = os.environ.get(f"MEME_{section_name.upper()}_{field.name.upper()}")
        if raw:
            current = replace(current, **{field.name: _coerce(raw, field.default)})
    for env_name, (alias_section, alias_key) in _ENV_ALIASES.items():
        if alias_section != section_name:
            continue
        raw = os.environ.get(env_name)
        if not raw:
            continue
        field = next(item for item in fields(current) if item.name == alias_key)
        current = replace(current, **{alias_key: _coerce(raw, field.default)})
    return current


def _resolve_config_path(config_path: Path | str | None) -> Path:
    """Pick the config file: explicit argument, then ``MEME_CONFIG_PATH``, then the default."""
    if config_path is not None:
        candidate = Path(config_path)
    else:
        env_path = os.environ.get("MEME_CONFIG_PATH")
        candidate = Path(env_path) if env_path else DEFAULT_CONFIG_PATH
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read the YAML config file; a missing file falls back to built-in defaults."""
    if not path.is_file():
        logger.warning("config file not found path=%s — using built-in defaults", path)
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in config file: {path}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping of sections: {path}")
    return raw


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Load and fully resolve application settings.

    Reads ``.env.local`` then ``.env`` from the project root (real environment
    variables always win, and ``.env.local`` wins over ``.env`` for keys defined
    in both), then YAML, then applies environment overrides.
    """
    load_dotenv(PROJECT_ROOT / ".env.local")
    load_dotenv(PROJECT_ROOT / ".env")
    path = _resolve_config_path(config_path)
    raw = _read_yaml(path)
    section_classes: dict[str, type] = {
        "server": ServerSettings,
        "storage": StorageSettings,
        "crawler": CrawlerSettings,
        "ai": AISettings,
        "search": SearchSettings,
        "watch": WatchSettings,
        "backfill": BackfillSettings,
    }
    for name in sorted(set(raw) - set(section_classes)):
        logger.warning("unknown config section ignored section=%s", name)
    sections = {
        name: _build_section(cls, raw.get(name, {}), name) for name, cls in section_classes.items()
    }
    sections = {name: _env_overrides(section, name) for name, section in sections.items()}
    logger.debug("config loaded path=%s", path)
    return Settings(config_path=path, **sections)
