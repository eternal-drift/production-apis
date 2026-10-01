"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_SENSITIVE = {"authorization", "token", "secret", "signature", "password", "jwt_secret"}


def _redact(_: Any, __: str, event: structlog.typing.EventDict) -> structlog.typing.EventDict:
    for key in list(event):
        if key.lower() in _SENSITIVE:
            event[key] = "[REDACTED]"
    return event


def configure_logging(level: str = "INFO", *, pretty: bool = False, quiet: bool = False) -> None:
    logging.basicConfig(
        stream=sys.stdout, level=logging.CRITICAL + 1 if quiet else level.upper(), format="%(message)s"
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer() if pretty else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.CRITICAL if quiet else logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
