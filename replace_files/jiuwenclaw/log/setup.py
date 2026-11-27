# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Runtime logger setup with optional async queue-based writes."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import queue
import sys
import threading
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from typing import Any, Optional

from jiuwenclaw.log.config import LoggingLevels, resolve_logging_levels
from jiuwenclaw.log.filters import ComponentNameFilter, CompositeFilter
from jiuwenclaw.log.formatters import JsonOnlyFormatter, RuntimeLogFormatter
from jiuwenclaw.log.handlers import LOG_FILE_BACKUP_COUNT, LOG_FILE_MAX_BYTES, SafeRotatingFileHandler
from jiuwenclaw.log.privacy import SensitiveDataFilter

_EXCEPTION_LOGGING_INSTALLED = False
_LOG_QUEUE: queue.Queue[logging.LogRecord] | None = None
_LOG_LISTENER: QueueListener | None = None
_LOG_SINK_HANDLERS: list[logging.Handler] = []
_LOG_LISTENER_LOCK = threading.Lock()
_LOG_ASYNC_SHUTDOWN_REGISTERED = False


def async_logging_enabled() -> bool:
    """Whether runtime logs are enqueued and written by a background QueueListener."""
    raw = os.getenv("LOG_ASYNC", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def shutdown_logging() -> None:
    """Flush and stop the async log listener, if any."""
    global _LOG_QUEUE, _LOG_LISTENER, _LOG_SINK_HANDLERS

    root = logging.getLogger("jiuwenclaw")
    for handler in root.handlers[:]:
        try:
            handler.close()
        except Exception:
            pass
        root.removeHandler(handler)

    with _LOG_LISTENER_LOCK:
        listener = _LOG_LISTENER
        sink_handlers = list(_LOG_SINK_HANDLERS)
        _LOG_LISTENER = None
        _LOG_QUEUE = None
        _LOG_SINK_HANDLERS = []

    if listener is not None:
        listener.stop()

    for handler in sink_handlers:
        try:
            handler.close()
        except Exception:
            pass


def asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Log asyncio loop/task failures with stack traces on the jiuwenclaw logger."""
    exc = context.get("exception")
    message = context.get("message") or "asyncio event loop error"
    task = context.get("task") or context.get("future")
    task_name = getattr(task, "get_name", lambda: "")() if task is not None else ""
    log = logging.getLogger("jiuwenclaw.asyncio")
    if exc is not None:
        log.error(
            "%s (task=%s)",
            message,
            task_name or task,
            exc_info=exc,
        )
        return
    log.error("%s context=%r", message, context)


def configure_asyncio_event_loop_logging(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Attach :func:`asyncio_exception_handler` to the running (or given) event loop."""
    target = loop or asyncio.get_running_loop()
    target.set_exception_handler(asyncio_exception_handler)


def install_global_exception_logging() -> None:
    """Route uncaught main-thread / thread exceptions to jiuwenclaw logger with tracebacks."""
    global _EXCEPTION_LOGGING_INSTALLED
    if _EXCEPTION_LOGGING_INSTALLED:
        return
    _EXCEPTION_LOGGING_INSTALLED = True

    log = logging.getLogger("jiuwenclaw")
    original_sys_excepthook = sys.excepthook

    def _sys_excepthook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            original_sys_excepthook(exc_type, exc_value, exc_tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        original_sys_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        original_thread_excepthook = threading.excepthook

        def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
            log.critical(
                "Uncaught exception in thread %s",
                args.thread,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            original_thread_excepthook(args)

        threading.excepthook = _thread_excepthook


def _build_rotating_handler(
    logs_root: Path,
    filename: str,
    level: int,
    formatter: logging.Formatter,
    privacy_filter: SensitiveDataFilter,
    name_filter: Optional[ComponentNameFilter] = None,
    custom_formatter: Optional[logging.Formatter] = None,
) -> SafeRotatingFileHandler:
    handler = SafeRotatingFileHandler(
        filename=logs_root / filename,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(custom_formatter if custom_formatter is not None else formatter)
    handler.addFilter(privacy_filter)
    if name_filter is not None:
        handler.addFilter(name_filter)
    return handler


def _register_async_logging_shutdown() -> None:
    global _LOG_ASYNC_SHUTDOWN_REGISTERED
    if _LOG_ASYNC_SHUTDOWN_REGISTERED:
        return
    _LOG_ASYNC_SHUTDOWN_REGISTERED = True
    atexit.register(shutdown_logging)


def setup_logger(log_level: Optional[str] = None) -> logging.Logger:
    """Configure the ``jiuwenclaw`` root logger: console + per-component files.

    Modules should use ``logging.getLogger(__name__)``. Routing rules:
    - ``jiuwenclaw.channel.*`` → channel.log
    - ``jiuwenclaw.agentserver.*`` → agent_server.log
    - other ``jiuwenclaw.*`` → gateway.log

    Output directory: ``~/.jiuwenclaw/agent/.logs/``.

    Levels come from ``config.yaml`` ``logging`` section; ``LOG_LEVEL`` overrides when
    ``log_level`` is ``None``. Passing ``log_level`` (e.g. in tests) sets all sinks to that level.

    Async disk writes are enabled by default (``LOG_ASYNC=1``): hot-path ``logger.info`` only
    enqueues; a background ``QueueListener`` writes handlers. Set ``LOG_ASYNC=0`` for sync writes.
    Call :func:`shutdown_logging` before process exit to drain the queue.
    """
    from jiuwenclaw.utils import get_logs_dir

    global _LOG_QUEUE, _LOG_LISTENER, _LOG_SINK_HANDLERS

    shutdown_logging()

    log_root_path = os.getenv("LOG_ROOT_PATH", "").strip()
    logs_root = Path(log_root_path).expanduser().resolve() if log_root_path else get_logs_dir()
    logs_root.mkdir(parents=True, exist_ok=True)

    levels = resolve_logging_levels(log_level)

    root = logging.getLogger("jiuwenclaw")
    root.setLevel(levels.logger)
    root.propagate = False

    formatter = RuntimeLogFormatter()
    privacy_filter = SensitiveDataFilter()
    json_formatter = JsonOnlyFormatter()

    sink_handlers: list[logging.Handler] = [
        _build_rotating_handler(
            logs_root, "gateway.log", levels.gateway, formatter, privacy_filter,
            ComponentNameFilter("gateway"),
        ),
        _build_rotating_handler(
            logs_root, "channel.log", levels.channel, formatter, privacy_filter,
            ComponentNameFilter("channel"),
        ),
        _build_rotating_handler(
            logs_root, "agent_server.log", levels.agent_server, formatter, privacy_filter,
            CompositeFilter([ComponentNameFilter("agent_server"), ComponentNameFilter("permissions")]),
        ),
        _build_rotating_handler(
            logs_root, "permissions.log", levels.agent_server, formatter, privacy_filter,
            ComponentNameFilter("permissions"), custom_formatter=json_formatter,
        ),
    ]
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(levels.console)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(privacy_filter)
    sink_handlers.append(stream_handler)

    with _LOG_LISTENER_LOCK:
        _LOG_SINK_HANDLERS = list(sink_handlers)

    if async_logging_enabled():
        log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
        queue_handler = QueueHandler(log_queue)
        queue_handler.setLevel(levels.logger)
        root.addHandler(queue_handler)
        listener = QueueListener(
            log_queue,
            *sink_handlers,
            respect_handler_level=True,
        )
        listener.start()
        with _LOG_LISTENER_LOCK:
            _LOG_QUEUE = log_queue
            _LOG_LISTENER = listener
        _register_async_logging_shutdown()
    else:
        for handler in sink_handlers:
            root.addHandler(handler)

    install_global_exception_logging()
    return root
