"""Import smoke tests (M0.5): every backend package imports cleanly."""

from __future__ import annotations

import importlib

import pytest

PACKAGES = [
    "backend",
    "backend.main",
    "backend.api",
    "backend.config",
    "backend.config.loader",
    "backend.database",
    "backend.database.database",
    "backend.database.migrations",
    "backend.scraper",
    "backend.scraper.adapters",
    "backend.media",
    "backend.ai",
    "backend.search",
    "backend.jobs",
]


@pytest.mark.parametrize("package_name", PACKAGES)
def test_package_imports(package_name: str) -> None:
    importlib.import_module(package_name)


def test_app_factory_builds() -> None:
    from backend.main import create_app

    app = create_app()
    assert app.title == "MemeVault"
