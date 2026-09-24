# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound repeated DB and browser-health diagnostics without filtering other errors."""

import atexit
import logging
import threading
import time
from dataclasses import dataclass


@dataclass
class _Repeat:
    emitted_at: float
    total: int = 1
    suppressed: int = 0


def _diagnostic_category(name: str, message: str) -> str:
    category = ""
    if "QueuePool limit of size " in message and "connection timed out" in message:
        category = "database_pool_timeout"
    elif "DbSessions.read() exceeded" in message:
        category = "database_read_timeout"
    elif "DbSessions.write() exceeded" in message:
        category = "database_write_timeout"
    elif "create_cur_session_tables hit a transient DB error" in message:
        category = "database_setup_retry"
    elif name in _BROWSER_LOGGERS and message.startswith("BrowserService heartbeat:"):
        if "connection unhealthy" in message:
            category = "browser_unhealthy"
        elif "restart deferred" in message:
            category = "browser_restart_deferred"
    return category


class RuntimeLogFilter(logging.Filter):
    """Keep the first traceback and report repeat counts at most once a minute."""

    def __init__(self) -> None:
        super().__init__()
        self._repeats: dict[tuple[str, str, int, int, str], _Repeat] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        # Normal request logs never format messages or acquire the lock here.
        if record.levelno < logging.WARNING:
            if record.name not in _BROWSER_LOGGERS:
                return True
            if not str(record.msg).startswith("BrowserService heartbeat: restart deferred"):
                return True
        message = record.getMessage()
        category = _diagnostic_category(record.name, message)
        if not category:
            return True

        now = time.monotonic()
        key = (record.name, record.pathname, record.lineno, record.levelno, category)
        with self._lock:
            state = self._repeats.get(key)
            if state is None:
                self._repeats[key] = _Repeat(now)
                return True
            state.total += 1
            if now - state.emitted_at < 60.0:
                state.suppressed += 1
                return False
            original = record.getMessage()
            record.msg = (
                "%s [runtime diagnostic: category=%s total=%d suppressed=%d]"
            )
            record.args = (original, category, state.total, state.suppressed)
            state.emitted_at = now
            state.suppressed = 0
        return True

    def flush(self) -> None:
        """Preserve counts for short bursts too, without a background timer."""
        pending = []
        with self._lock:
            for (name, path, line, level, category), state in self._repeats.items():
                if state.suppressed:
                    pending.append(
                        (name, path, line, level, category, state.total, state.suppressed)
                    )
                    state.suppressed = 0
        for name, path, line, level, category, total, suppressed in pending:
            target = logging.getLogger(name)
            if not target.isEnabledFor(level):
                continue
            record = target.makeRecord(
                name, level, path, line,
                "Repeated runtime diagnostic: category=%s total=%d suppressed=%d",
                (category, total, suppressed), None,
            )
            # At exit an embedding host may already have closed its console.
            # Still flush to live files, respecting each sink's level/filters.
            while target is not None:
                for handler in target.handlers:
                    if level < handler.level:
                        continue
                    if getattr(getattr(handler, "stream", None), "closed", False):
                        continue
                    handler.handle(record)
                if not target.propagate:
                    break
                target = target.parent


_BROWSER_LOGGERS = ("browser_agent", "openjiuwen.browser_agent")
_FILTER = RuntimeLogFilter()


def install_runtime_log_filter() -> None:
    """Install once per emitting logger, before its console/file fan-out."""
    for name in (
        "common", "team", "runner", *_BROWSER_LOGGERS,
        "jiuwenswarm.gateway.message_handler.message_handler",
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers",
    ):
        logging.getLogger(name).addFilter(_FILTER)


atexit.register(_FILTER.flush)
