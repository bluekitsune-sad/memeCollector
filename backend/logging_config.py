"""Structured logging for the backend (PRD §53, AGENTS.md §4).

:func:`configure_logging` attaches two handlers to the ``backend`` package
logger — the parent of every module logger in this codebase:

* a **console** handler on stderr, so records sit next to uvicorn's own output;
* a **rotating file** handler (``logs/memecollector.log``, 2 MiB × 4 files,
  gitignored per AGENTS.md §10), created lazily so importing the app never
  touches the disk.

Record format follows the PRD §53 example — timestamp, level, emitting module,
then the message, which every module writes in ``key=value`` form::

    2026-10-06 13:42:12 INFO  backend.scraper.crawler: page discovered page=17

The module name is included because §53 asks the logs to be "useful for
debugging site adapters" — it tells you which adapter emitted the line.

Configuration rules:

* **idempotent** — a marker attribute on the logger means a second call (every
  ``create_app()`` in the test suite) is a no-op; ``force=True`` replaces the
  handlers instead (used to point the file handler at a temp dir);
* **secret redaction** — every handler carries a
  :class:`_SecretRedactionFilter` that scrubs the value of
  ``OPENROUTER_API_KEY`` out of the formatted message (PRD §41: API keys never
  in logs); exception tracebacks are deliberately left as-is because they never
  carry request headers;
* **``propagate=True``** stays untouched, so pytest's ``caplog`` and any root
  handler still see backend records — handlers are *added*, never a takeover;
* level comes from ``MEME_LOG_LEVEL`` (``DEBUG``/``INFO``/``WARNING``/…),
  defaulting to ``INFO``; an unknown value logs a warning and falls back;
* uvicorn's loggers are deliberately **not** configured — they keep their own
  defaults so access logs stay separate from application logs.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

#: Where the rotating file lands (AGENTS.md §3 layout, gitignored).
LOG_DIRECTORY: Path = PROJECT_ROOT / "logs"

#: Rotating file name and size policy.
LOG_FILE_NAME = "memecollector.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

#: Env var holding the level (AGENTS.md §4: tunables come from config/env).
LEVEL_ENV_VAR = "MEME_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"

#: Package logger every backend module logs through (``logging.getLogger(__name__)``).
PACKAGE_LOGGER_NAME = "backend"

#: Env vars whose values must never appear in any log line (PRD §41).
SECRET_ENV_VARS: tuple[str, ...] = ("OPENROUTER_API_KEY",)

#: Secrets shorter than this are not masked — replacing e.g. "test" would mangle ordinary text.
MIN_SECRET_LENGTH = 8

REDACTED_PLACEHOLDER = "[redacted]"

_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MARKER = "_memecollector_logging_configured"


def resolve_level(level: str | int | None = None) -> int:
    """Numeric level from an explicit value, ``MEME_LOG_LEVEL``, or the default.

    Unknown names fall back to ``INFO`` with a warning (a typo in an env var
    must never silence or crash the application).
    """
    if level is None:
        level = os.environ.get(LEVEL_ENV_VAR, DEFAULT_LEVEL)
    if isinstance(level, int):
        return level
    candidate = str(level).strip().upper()
    resolved = logging.getLevelNamesMapping().get(candidate)
    if isinstance(resolved, int):
        return resolved
    logging.getLogger(PACKAGE_LOGGER_NAME).warning(
        "unknown log level level=%s falling back to %s", candidate, DEFAULT_LEVEL
    )
    return logging.getLevelNamesMapping()[DEFAULT_LEVEL]


def _mask_secrets(message: str) -> str:
    """Return ``message`` with every configured secret value replaced by ``[redacted]``."""
    masked = message
    for env_var in SECRET_ENV_VARS:
        secret = os.environ.get(env_var, "")
        if len(secret) >= MIN_SECRET_LENGTH and secret in masked:
            masked = masked.replace(secret, REDACTED_PLACEHOLDER)
    return masked


class _SecretRedactionFilter(logging.Filter):
    """Scrub API-key values out of log records before a handler formats them.

    Attached to each handler (not to the logger) so the record is cleaned as
    soon as it reaches the first backend handler — every handler and any
    downstream ``caplog``/root handler then sees the masked text. The secret is
    read lazily from the environment, so a key configured after
    :func:`configure_logging` still gets masked, and ``record.args`` is cleared
    once the message is rewritten so ``getMessage()`` cannot re-format it.
    Exception tracebacks are intentionally not rewritten (they never carry
    request headers or the key).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True  # malformed record — let normal formatting raise where it always did
        masked = _mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def configure_logging(
    *,
    level: str | int | None = None,
    log_directory: Path | None = None,
    force: bool = False,
) -> logging.Logger:
    """Attach the console + rotating-file handlers to the ``backend`` logger.

    Returns the configured logger. Repeated calls are no-ops unless ``force``
    is set, in which case previous handlers are closed and replaced (the
    intended way to re-point logging, e.g. in tests).
    """
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    if getattr(logger, _MARKER, False) and not force:
        return logger
    if force:
        _detach(logger)

    logger.setLevel(resolve_level(level))
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(_SecretRedactionFilter())
    logger.addHandler(console)

    directory = Path(log_directory) if log_directory is not None else LOG_DIRECTORY
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.error("log directory unavailable, file logging disabled path=%s", directory)
    else:
        file_handler = RotatingFileHandler(
            directory / LOG_FILE_NAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,  # the file is only created once something is logged
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_SecretRedactionFilter())
        logger.addHandler(file_handler)

    setattr(logger, _MARKER, True)
    logger.debug("logging configured level=%s directory=%s", logger.level, directory)
    return logger


def _detach(logger: logging.Logger) -> None:
    """Close and remove every handler this module previously attached."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            logger.warning("could not close log handler handler=%r", handler)
    setattr(logger, _MARKER, False)
