"""API-key log-redaction tests (PRD §41, AGENTS.md §9) — offline, no server.

``configure_logging`` attaches a secret-redaction filter to both handlers; these
tests prove the key value is masked in the file line and in the record pytest's
caplog sees, that ordinary records are untouched, and that short values (which
would mangle ordinary text) are deliberately not masked.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.logging_config import (
    LOG_FILE_NAME,
    PACKAGE_LOGGER_NAME,
    configure_logging,
)

SECRET = "sk-or-REDACT-THIS-KEY-123456"


def test_api_key_is_redacted_from_records_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    configure_logging(log_directory=tmp_path, force=True)
    try:
        with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER_NAME):
            logging.getLogger("backend.ai.openrouter").info(
                "openrouter request failed detail=%s", SECRET
            )

        log_file = tmp_path / LOG_FILE_NAME
        line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert SECRET not in line
        assert "detail=[redacted]" in line  # key=value shape preserved

        # The backend handler masks the record before caplog/root handlers see it.
        assert SECRET not in caplog.text
        assert "[redacted]" in caplog.text
    finally:
        configure_logging(force=True)


def test_short_secrets_are_not_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Values below the minimum length would mangle ordinary text — left untouched."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "abcd")
    configure_logging(log_directory=tmp_path, force=True)
    try:
        with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER_NAME):
            logging.getLogger("backend.scraper.crawler").info("page scanned abcd notes=1")

        assert "page scanned abcd notes=1" in caplog.text
        assert "[redacted]" not in caplog.text
    finally:
        configure_logging(force=True)


def test_records_without_the_secret_are_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    configure_logging(log_directory=tmp_path, force=True)
    try:
        with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER_NAME):
            logging.getLogger("backend.media.library").info("dup flagged media_id=3")

        log_file = tmp_path / LOG_FILE_NAME
        line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert "dup flagged media_id=3" in line
        assert "[redacted]" not in line
    finally:
        configure_logging(force=True)
