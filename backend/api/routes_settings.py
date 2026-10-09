"""Settings API — ``GET``/``PATCH /api/settings`` (PRD §40, §42, §43).

``GET`` returns the exact document the frontend renders: ``server``, storage
paths, the crawler block, the AI block (``key_present`` only — never the key
itself, PRD §41), the four ranking weights and the two read-only notices
(privacy §42, copyright/responsible-use §43).

``PATCH`` applies **only the sections present** in the body:

* every value is validated server-side — crawl-limit ranges, the provider
  whitelist (``mock``/``openrouter``; anything else cannot start), non-empty
  model names, each weight in ``[0, 1]`` with the merged four weights summing
  to exactly 1.0 (PRD §22);
* unknown sections or fields are rejected with ``422`` (``extra="forbid"``), so
  ``server``/``storage`` (the localhost bind, PRD §41) and ``ai.api_key`` (a
  secret) can never be written through the API;
* accepted changes are persisted to ``settings.config_path`` with a YAML
  round-trip that touches **only the edited keys**, so a restart keeps them —
  environment variables still win on reload (documented resolution order in
  :mod:`backend.config.loader`);
* the merged settings object replaces ``app.state.settings``, so the running
  app serves subsequent requests with the new values.

The response is always the full ``SettingsResponse`` (the frontend swaps its
whole cache after a save).
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: AI backends that can actually run (``create_provider`` rejects anything else).
SUPPORTED_PROVIDERS: tuple[str, ...] = ("mock", "openrouter")

#: PRD §42 — shown whenever analysis is sent to an external provider.
PRIVACY_NOTICE = (
    "MemeCollector is local-first: media, database, search and embeddings stay on this machine. "
    "When an external AI provider is configured, the selected media is sent to that provider "
    "for analysis and the returned description/tags are stored in the local database."
)

#: PRD §43 — shown read-only in Settings.
COPYRIGHT_NOTICE = (
    "You are responsible for ensuring your collection and use of this media complies with the "
    "source sites' terms and applicable copyright law. MemeCollector keeps source attribution, uses "
    "reasonable crawl rates and never redistributes collected content automatically — it is "
    "intended for personal archival use only."
)


# ---------------------------------------------------------------------------
# Response models (the documented contract)
# ---------------------------------------------------------------------------


class ServerSettingsOut(BaseModel):
    """Local bind address — informational; the bind itself is not editable (PRD §41)."""

    host: str
    port: int


class StorageSettingsOut(BaseModel):
    """Runtime artifact locations (read-only in the UI)."""

    media_directory: str
    thumbnail_directory: str
    preview_directory: str
    database_path: str


class CrawlerSettingsOut(BaseModel):
    """Crawl limits + browser flags (PRD §10, §37)."""

    delay_seconds: float
    concurrency: int
    max_pages: int
    download_limit: int
    max_file_size_mb: int
    headless: bool
    debug: bool
    retry_attempts: int
    request_timeout_seconds: float


class AiSettingsOut(BaseModel):
    """AI block — the API key is exposed as a boolean only (PRD §41)."""

    provider: str
    model: str
    embedding_model: str
    key_present: bool
    external_provider: bool


class SearchSettingsOut(BaseModel):
    """Hybrid ranking weights (PRD §22)."""

    keyword_weight: float
    semantic_weight: float
    tag_weight: float
    metadata_weight: float


class SettingsNoticesOut(BaseModel):
    """Read-only privacy (§42) and responsible-use (§43) text."""

    privacy: str
    copyright: str


class SettingsResponse(BaseModel):
    """``GET /api/settings`` / ``PATCH /api/settings`` envelope."""

    server: ServerSettingsOut
    storage: StorageSettingsOut
    crawler: CrawlerSettingsOut
    ai: AiSettingsOut
    search: SearchSettingsOut
    notices: SettingsNoticesOut


# ---------------------------------------------------------------------------
# Patch models (partial, strict)
# ---------------------------------------------------------------------------


class _StrictSection(BaseModel):
    """Strict partial section: unknown keys are a ``422``, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


class CrawlerPatch(_StrictSection):
    """Editable crawl limits (the Settings form) plus the remaining crawler flags."""

    delay_seconds: float | None = Field(None, ge=0, le=60)
    concurrency: int | None = Field(None, ge=1, le=8)
    max_pages: int | None = Field(None, ge=0, le=1_000_000)
    download_limit: int | None = Field(None, ge=0, le=1_000_000)
    max_file_size_mb: int | None = Field(None, ge=1, le=4096)
    headless: bool | None = None
    debug: bool | None = None
    retry_attempts: int | None = Field(None, ge=1, le=10)
    request_timeout_seconds: float | None = Field(None, ge=1, le=300)


class AiPatch(_StrictSection):
    """Editable AI fields; ``api_key`` is deliberately absent (env-only secret)."""

    provider: str | None = None
    model: str | None = Field(None, min_length=1, max_length=200)
    embedding_model: str | None = Field(None, min_length=1, max_length=200)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"unsupported AI provider {value!r} — expected one of: {', '.join(SUPPORTED_PROVIDERS)}"
            )
        return normalized

    @field_validator("model", "embedding_model")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("model names must not be empty")
        return trimmed


class SearchPatch(_StrictSection):
    """Ranking weights; each bounded to [0, 1], the merged set must sum to 1.0."""

    keyword_weight: float | None = Field(None, ge=0, le=1)
    semantic_weight: float | None = Field(None, ge=0, le=1)
    tag_weight: float | None = Field(None, ge=0, le=1)
    metadata_weight: float | None = Field(None, ge=0, le=1)


class SettingsPatchRequest(_StrictSection):
    """``PATCH /api/settings`` body — only the sections present are applied."""

    crawler: CrawlerPatch | None = None
    ai: AiPatch | None = None
    search: SearchPatch | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=SettingsResponse)
async def get_settings(request: Request) -> SettingsResponse:
    """The full settings document the Settings page renders (PRD §42–§43)."""
    return render_settings(request.app.state.settings)


@router.patch("", response_model=SettingsResponse)
async def patch_settings(request: Request, patch: SettingsPatchRequest) -> SettingsResponse:
    """Validate, persist and apply a partial settings change."""
    current: Settings = request.app.state.settings
    updated = apply_patch(current, patch)
    sections = [name for name in ("crawler", "ai", "search") if getattr(patch, name) is not None]
    persist_patch(current.config_path, patch)
    request.app.state.settings = updated
    logger.info("settings updated sections=%s", ",".join(sections))
    return render_settings(updated)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def render_settings(settings: Settings) -> SettingsResponse:
    """Settings → API document (secrets excluded by construction)."""
    ai = settings.ai
    return SettingsResponse(
        server=ServerSettingsOut(host=settings.server.host, port=settings.server.port),
        storage=StorageSettingsOut(
            media_directory=str(settings.storage.media_directory),
            thumbnail_directory=str(settings.storage.thumbnail_directory),
            preview_directory=str(settings.storage.preview_directory),
            database_path=str(settings.storage.database_path),
        ),
        crawler=CrawlerSettingsOut(**vars(settings.crawler)),
        ai=AiSettingsOut(
            provider=ai.provider,
            model=ai.model,
            embedding_model=ai.embedding_model,
            key_present=bool(ai.api_key),
            external_provider=ai.provider != "mock",
        ),
        search=SearchSettingsOut(**vars(settings.search)),
        notices=SettingsNoticesOut(privacy=PRIVACY_NOTICE, copyright=COPYRIGHT_NOTICE),
    )


def apply_patch(settings: Settings, patch: SettingsPatchRequest) -> Settings:
    """Merge ``patch`` into ``settings``; raises ``422`` when the merge is invalid."""
    updates: dict[str, dict[str, object]] = {}
    crawler = _section_values(patch.crawler)
    if crawler:
        updates["crawler"] = crawler
    ai = _section_values(patch.ai)
    if ai:
        updates["ai"] = ai
    search_values = _section_values(patch.search)
    if search_values:
        merged = replace(settings.search, **search_values)
        total = (
            merged.keyword_weight
            + merged.semantic_weight
            + merged.tag_weight
            + merged.metadata_weight
        )
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise HTTPException(
                status_code=422,
                detail=(
                    "search weights must sum to 1.0 after merging with the current values "
                    f"(got {total:.4f})"
                ),
            )
        updates["search"] = search_values
    if not updates:
        raise HTTPException(status_code=422, detail="patch body must contain crawler, ai or search")
    return replace(settings, **{name: replace(getattr(settings, name), **values)
                                for name, values in updates.items()})


def persist_patch(config_path: Path, patch: SettingsPatchRequest) -> None:
    """Write only the edited keys into ``config_path`` (YAML round-trip).

    Secrets are unreachable here (``ai.api_key`` cannot be patched), and a
    missing config file is created. ``server``/``storage`` sections are never
    touched by a patch, so the localhost bind (PRD §41) is preserved as-is.
    The rewritten file is a clean ``safe_dump`` of the parsed mapping — a
    hand-edited config keeps every value but loses its inline comments.
    """
    raw: dict = {}
    if config_path.is_file():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            logger.error("config file unreadable, settings not persisted path=%s", config_path)
            raise HTTPException(
                status_code=500,
                detail=f"config file is not valid YAML, settings not saved: {config_path.name}",
            ) from exc
        if loaded is not None and not isinstance(loaded, dict):
            logger.error("config root is not a mapping path=%s", config_path)
            raise HTTPException(
                status_code=500,
                detail=f"config file root must be a mapping, settings not saved: {config_path.name}",
            )
        raw = loaded or {}
    for section in ("crawler", "ai", "search"):
        values = _section_values(getattr(patch, section))
        if values:
            raw.setdefault(section, {}).update(values)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )
    logger.info("settings persisted path=%s", config_path)


def _section_values(section: BaseModel | None) -> dict[str, object]:
    """Present (non-null) fields of a patch section — ``None`` means "leave alone"."""
    if section is None:
        return {}
    return section.model_dump(exclude_none=True)


__all__ = [
    "PRIVACY_NOTICE",
    "COPYRIGHT_NOTICE",
    "SUPPORTED_PROVIDERS",
    "SettingsPatchRequest",
    "SettingsResponse",
    "apply_patch",
    "patch_settings",
    "get_settings",
    "persist_patch",
    "render_settings",
    "router",
]
