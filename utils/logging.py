"""
Structured logging — JSON log lines with extra fields.
Usage: logger.info("message", job_id="abc", url="https://...")
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JSONFormatter(logging.Formatter):
    """Single-line JSON log records for machine parsing."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload["exc"] = str(record.exc_info[1])
        # Gather any extra fields
        if hasattr(record, "_extra") and record._extra:
            payload.update(record._extra)
        return json.dumps(payload, default=str, ensure_ascii=False)


class StructuredLogger:
    """Thin wrapper around stdlib Logger that accepts kwargs as structured fields."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, msg: str, *args, **kwargs):
        if not self._logger.isEnabledFor(level):
            return
        # All kwargs become structured fields in the JSON output
        extra = {"_extra": kwargs} if kwargs else {}
        self._logger.log(level, msg, *args, extra=extra)

    def debug(self, msg, *args, **kwargs):
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._log(logging.ERROR, msg, *args, **kwargs)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with JSON output to stdout."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    # Quiet noisy libs
    for name in ("httpx", "httpcore", "uvicorn", "playwright"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(logging.getLogger(name))


# Common loggers
crawl_log = get_logger("crawler")
api_log = get_logger("api")
