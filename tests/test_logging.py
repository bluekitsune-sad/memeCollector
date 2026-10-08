"""Structured-logging tests (PRD §53, AGENTS.md §4).

Covered: the record format from the PRD §53 example, key=value message
propagation into pytest's ``caplog`` (``propagate=True`` is never disabled),
idempotent configuration (no stacked handlers across ``create_app`` calls),
the ``MEME_LOG_LEVEL`` override and its unknown-level fallback, and the
rotating file handler writing into a temp directory.

The suite restores the default handlers afterwards, so no test leaves the
process pointing at a deleted temp file.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from backend.logging_config import (
    DEFAULT_LEVEL,
    LEVEL_ENV_VAR,
    LOG_FILE_NAME,
    PACKAGE_LOGGER_NAME,
    configure_logging,
    resolve_level,
)

#: One emitted line: ``2026-10-06 13:42:12 INFO  backend.jobs.index_job: msg``.
_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) +"
    r"(?P<logger>backend\S*): (?P<message>.*)$"
)


@pytest.fixture
def temp_logging(tmp_path: Path):
    """Point the file handler at a temp dir; restore the default afterwards."""
    configure_logging(log_directory=tmp_path, force=True)
    yield tmp_path
    configure_logging(force=True)


def test_configure_logging_is_idempotent(temp_logging: Path) -> None:
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    attached = list(logger.handlers)
    assert attached  # the fixture attached console + file handlers

    configure_logging()  # every create_app() call in the suite hits this path

    assert logger.handlers == attached
    assert len(logger.handlers) == 2  # console + rotating file, never more


def test_records_reach_the_rotating_file_in_prd_53_format(temp_logging: Path) -> None:
    logging.getLogger("backend.scraper.crawler").info(
        "comment media found count=8 site=fixture"
    )

    log_file = temp_logging / LOG_FILE_NAME
    assert log_file.is_file()
    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    match = _LINE_RE.match(line)
    assert match is not None, line
    assert match.group("level") == "INFO"
    assert match.group("logger") == "backend.scraper.crawler"
    assert match.group("message") == "comment media found count=8 site=fixture"


def test_records_propagate_to_root_so_caplog_sees_them(
    temp_logging: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER_NAME):
        logging.getLogger("backend.api.routes_search").info("search served mode=hybrid total=3")

    messages = [record.message for record in caplog.records]
    assert "search served mode=hybrid total=3" in messages


def test_force_reconfigure_replaces_handlers_without_stacking(temp_logging: Path) -> None:
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    assert len(logger.handlers) == 2
    old_handlers = list(logger.handlers)  # kept alive so identities stay unique

    configure_logging(log_directory=temp_logging, force=True)

    # Fresh handler objects, still exactly two — old ones were closed, not stacked.
    new_handlers = list(logger.handlers)
    assert len(new_handlers) == 2
    assert all(new is not old for new in new_handlers for old in old_handlers)


def test_create_app_configures_logging_once() -> None:
    from backend.main import create_app

    create_app()
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    before = len(logger.handlers)
    assert before >= 1

    create_app()
    create_app()

    assert len(logger.handlers) == before


def test_resolve_level_from_argument_env_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert resolve_level("debug") == logging.DEBUG
    assert resolve_level(logging.WARNING) == logging.WARNING

    monkeypatch.setenv(LEVEL_ENV_VAR, "ERROR")
    assert resolve_level() == logging.ERROR

    monkeypatch.setenv(LEVEL_ENV_VAR, "not-a-level")
    assert resolve_level() == logging.getLevelNamesMapping()[DEFAULT_LEVEL]


def test_level_env_var_drives_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(LEVEL_ENV_VAR, "WARNING")
    try:
        logger = configure_logging(log_directory=tmp_path, force=True)
        assert logger.level == logging.WARNING
        # A DEBUG record below the threshold never reaches the file.
        logging.getLogger("backend.media.library").debug("hidden detail=x")
        assert not (tmp_path / LOG_FILE_NAME).exists()

        logging.getLogger("backend.media.library").warning("dup flagged media_id=1")
        assert (tmp_path / LOG_FILE_NAME).is_file()
    finally:
        monkeypatch.delenv(LEVEL_ENV_VAR, raising=False)
        configure_logging(force=True)
