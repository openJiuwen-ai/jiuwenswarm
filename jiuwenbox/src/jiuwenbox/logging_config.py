# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Shared logging configuration for jiuwenbox."""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time

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


_STREAM_QUEUE_MAX_RECORDS = 10000
_stream_queue: "queue.Queue[tuple[logging.Handler, logging.LogRecord]] | None" = None
_stream_lock = threading.Lock()


class _NonBlockingStreamHandler(logging.Handler):
    """Hand records to a writer thread; drop them when the queue is full."""

    def __init__(self, target: logging.Handler, records: queue.Queue) -> None:
        super().__init__(target.level)
        self.target = target
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.put_nowait((self.target, record))
        except queue.Full:
            pass


def _drain_stream_queue(records: queue.Queue) -> None:
    while True:
        target, record = records.get()
        try:
            target.handle(record)
        except Exception:  # noqa: BLE001 - never let the writer thread die
            pass


def _flush_stream_queue(timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while _stream_queue is not None and not _stream_queue.empty() and time.monotonic() < deadline:
        time.sleep(0.02)


def make_stream_logging_nonblocking() -> None:
    """Move stdout/stderr log writes off the calling thread.

    box-server's stdout/stderr are pipes read by its parent. When the parent
    stops reading, a full pipe would otherwise block the event loop on the
    next log call; with this only the writer thread waits.
    """
    global _stream_queue
    with _stream_lock:
        if _stream_queue is None:
            _stream_queue = queue.Queue(_STREAM_QUEUE_MAX_RECORDS)
            threading.Thread(
                target=_drain_stream_queue, args=(_stream_queue,),
                name="jiuwenbox-log-writer", daemon=True,
            ).start()
            atexit.register(_flush_stream_queue)
        for name in ("", *UVICORN_LOGGER_NAMES):
            target_logger = logging.getLogger(name)
            for index, handler in enumerate(list(target_logger.handlers)):
                if type(handler) is logging.StreamHandler:
                    target_logger.handlers[index] = _NonBlockingStreamHandler(handler, _stream_queue)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure process logging with jiuwenbox's default timestamped format."""
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    formatter = _timestamp_formatter()
    _set_handler_formatters(logging.getLogger(), formatter)
    for logger_name in UVICORN_LOGGER_NAMES:
        _set_handler_formatters(logging.getLogger(logger_name), formatter)
    patch_uvicorn_logging()