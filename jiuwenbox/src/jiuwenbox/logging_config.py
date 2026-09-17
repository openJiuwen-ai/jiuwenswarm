# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Shared logging configuration for jiuwenbox."""

from __future__ import annotations

import logging

LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _timestamp_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def _set_handler_formatters(logger: logging.Logger, formatter: logging.Formatter) -> None:
    for handler in logger.handlers:
        handler.setFormatter(formatter)


def patch_uvicorn_logging() -> None:
    """Patch uvicorn's default LOGGING_CONFIG and rename ``uvicorn.error`` logger.

    Uvicorn uses the logger name ``uvicorn.error`` for normal server lifecycle
    messages (not errors). Rename it to ``uvicorn`` for clearer log output, and
    apply jiuwenbox's timestamped format to the default formatter.
    """
    from uvicorn.config import LOGGING_CONFIG

    LOGGING_CONFIG["formatters"]["default"]["fmt"] = LOG_FORMAT
    LOGGING_CONFIG["formatters"]["default"]["datefmt"] = LOG_DATE_FORMAT
    logging.getLogger("uvicorn.error").name = "uvicorn"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure process logging with jiuwenbox's default timestamped format."""
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    formatter = _timestamp_formatter()
    _set_handler_formatters(logging.getLogger(), formatter)
    for logger_name in UVICORN_LOGGER_NAMES:
        _set_handler_formatters(logging.getLogger(logger_name), formatter)
    patch_uvicorn_logging()