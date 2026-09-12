# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Shared logging configuration for jiuwenbox."""

from __future__ import annotations

import logging

from jiuwenbox.log_sanitizer import _sanitize_log_text

LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")


class SensitiveDataFilter(logging.Filter):
    """Mask sensitive data in all log messages and tracebacks.

    Filter-level sanitization is the last line of defense: every handler that
    carries this filter sanitizes both ``record.getMessage()`` and any
    traceback rendered from ``exc_info`` before formatting — independent of
    which formatter a handler uses (mirrors ``jiuwenswarm.common.utils``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            record.msg = _sanitize_log_text(message)
            record.args = ()
        except Exception:
            # Never block logging because of desensitization failure.
            pass

        # Traceback 由 Formatter.formatException() 在 record.exc_text 中单独渲染，
        # 不经过 record.getMessage()，因此 message 脱敏覆盖不到。这里提前把
        # traceback 文本脱敏写入 record.exc_text 并清空 record.exc_info，
        # 使 logger.exception()/exc_info=True 的异常栈也不会泄露 api_key 等。
        try:
            exc_info = record.exc_info
            if exc_info and not record.exc_text:
                import traceback as _traceback

                # exc_info 是 (type, value, tb) 三元组。Python 3.10+ 的
                # format_exception 新签名只接受单个异常实例；用 exc_info[1]
                # 是官方推荐写法，面向未来且行为等价。
                formatted = "".join(_traceback.format_exception(exc_info[1]))
                record.exc_text = _sanitize_log_text(formatted)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = _sanitize_log_text(record.exc_text)
        except Exception:
            # 同样不因脱敏失败而阻断日志输出。
            pass
        return True


def _timestamp_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def _set_handler_formatters(logger: logging.Logger, formatter: logging.Formatter) -> None:
    privacy_filter = SensitiveDataFilter()
    for handler in logger.handlers:
        handler.setFormatter(formatter)
        # 幂等：避免重复添加导致同一 record 多次脱敏（虽有 _is_already_masked 兜底，
        # 但省掉无谓的二次正则扫描）。
        already = False
        for f in handler.filters:
            if isinstance(f, SensitiveDataFilter):
                already = True
                break
        if not already:
            handler.addFilter(privacy_filter)


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
