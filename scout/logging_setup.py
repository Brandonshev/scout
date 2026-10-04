"""Logging: rotating files in logs/ plus the console.

Every line carries the local (Sydney) time with its UTC offset, the app
version and the mode, e.g.

2026-09-28 20:01:02.123+10:00 | v0.1.0 | DEMO | INFO    | scout.cli | Scout starting
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from scout.config import Settings
from scout.version import APP_VERSION

LOG_FORMAT = "%(asctime)s | v%(app_version)s | %(mode)s | %(levelname)-7s | %(name)s | %(message)s"
LOG_FILE_NAME = "scout.log"

_installed: list[logging.Handler] = []


class _ContextFilter(logging.Filter):
    """Stamps the version and mode onto every record, from any logger."""

    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def filter(self, record: logging.LogRecord) -> bool:
        record.app_version = APP_VERSION
        record.mode = self.mode
        return True


class LocalTimeFormatter(logging.Formatter):
    def __init__(self, tz: ZoneInfo) -> None:
        super().__init__(LOG_FORMAT)
        self.tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created, self.tz).isoformat(sep=" ", timespec="milliseconds")


def setup_logging(settings: Settings, console: bool = True) -> Path:
    """Send logs to logs/scout.log (rotating) and, unless console=False, the console. Safe to call twice.

    Returns the path of the log file.
    """
    reset_logging()
    settings.app.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.app.log_dir / LOG_FILE_NAME

    formatter = LocalTimeFormatter(settings.app.tz)
    context = _ContextFilter(settings.mode.value.upper())
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.app.log_max_bytes,
        backupCount=settings.app.log_backup_count,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler(sys.stderr)

    root = logging.getLogger()
    for handler in (file_handler, console_handler) if console else (file_handler,):
        handler.setFormatter(formatter)
        handler.addFilter(context)
        root.addHandler(handler)
        _installed.append(handler)
    root.setLevel(settings.app.log_level)
    # Libraries that log every request/frame: only show their warnings.
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


def reset_logging() -> None:
    """Remove and close the handlers installed by setup_logging."""
    root = logging.getLogger()
    for handler in _installed:
        root.removeHandler(handler)
        handler.close()
    _installed.clear()
