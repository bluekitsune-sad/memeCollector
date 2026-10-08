"""Configuration loading (YAML + environment overrides, PRD §40)."""

from backend.config.loader import PROJECT_ROOT, Settings, load_settings

__all__ = ["PROJECT_ROOT", "Settings", "load_settings"]
